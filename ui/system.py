"""系统级操作：**关机** —— 收掉本程序自己起的全部资源，再退出。

为什么需要它
------------
程序有三种运行形态，各自留着一批端口，而且**关掉其中一个不等于关掉全部**：

===============================  =============================================
``run.bat``（最常用）             **两个独立进程**：靶场 8899/8900 一个窗口、
                                 UI 8800 一个窗口
``launcher.py``（打包桌面版）     **单进程**：三个服务 + pywebview 窗口
裸 ``uvicorn``                    只有 UI 8800
===============================  =============================================

只让自己退出（``os._exit``）在第一种形态下只解决一半：靶场那个黑窗口还活着，
8899/8900 继续 LISTENING，用户仍要去关它 —— 而「不用去关它」正是这个功能的全部意义。

还有一类更容易被漏掉：``POST /api/targets/launch``「接管启动」起的浏览器。
它带着 ``--remote-debugging-port``（默认 9222）**是独立进程**，父进程退出不会带走它。
改造前它甚至**没有任何长期持有者** —— ``make_browser_source()`` 每次调用都新建实例，
请求一结束对象就被回收，于是程序关了、那个浏览器还在占着调试端口。

所以关机 = 按顺序收掉这三类，最后退出。

红线：只收**本程序自己起的**东西
--------------------------------
- **自管浏览器**：只认 ``BrowserLauncher`` 亲手 ``launch`` 的进程（见
  ``target.browsers.managed_processes``）。附加到用户已有浏览器时那种实例
  ``_process`` 恒为 ``None`` —— 那种情况下**一个浏览器都不许关**。
- **兄弟进程**：必须同时满足 ① 端口在名单里 ② PID 不是自己
  ③ 可执行文件与本进程的**解释器**一致。第三条要小心虚拟环境 ——
  进程镜像路径报的往往是 ``.venv`` 背后的**基础解释器**，所以比对的是
  ``own_executables()`` 给出的**一整组**路径（见那个函数的说明；只比
  ``sys.executable`` 会让 ``run.bat`` 起的靶场被判成外人，实测踩到过）。
  三条缺一 → **不动手**，归入 ``foreign`` 只作报告。
  这条是刻意的：拿不到确凿依据就宁可留着让人自己处理 —— 按端口盲杀会误伤别人的服务，
  而这种错误没法向用户解释。

可测性
------
判定逻辑是**纯函数**（:func:`parse_netstat_listening` / :func:`collect_occupants`），
I/O 集中在 :func:`apply_shutdown` 且**全部可注入**（``kill`` / ``exit_fn`` / ``sleep_fn``）。
单测因此不需要真起进程、真杀进程，更不会把 pytest 自己干掉。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "CONSOLE_PORT",
    "MANAGED_PORTS",
    "OWNER_FOREIGN",
    "OWNER_SELF",
    "OWNER_SIBLING",
    "SHUTDOWN_FORCE_EXIT_S",
    "SHUTDOWN_GRACE_S",
    "Occupant",
    "OwnerKind",
    "ShutdownPlan",
    "ShutdownReport",
    "apply_shutdown",
    "build_plan",
    "collect_occupants",
    "own_executables",
    "parse_netstat_listening",
    "port_owners",
    "process_image_path",
    "reset_exit_fn",
    "set_exit_fn",
    "terminate_pid",
]

logger = logging.getLogger(__name__)

#: 控制台自身端口。
CONSOLE_PORT = 8800

#: 「本程序相关」的端口：控制台 + 靶场主站 + 跨域 frame。
#:
#: 浏览器调试端口**刻意不在名单里** —— 它由配置决定（``browser_debug_port``，默认 9222），
#: 而且它由自管浏览器进程占着；关掉那个进程，端口自然就没了，
#: 不需要（也不该）另外按端口去认一个浏览器。
MANAGED_PORTS: tuple[int, ...] = (CONSOLE_PORT, 8899, 8900)

#: 响应先发出去、再动手的间隔。前端要靠这个响应才能显示「已关闭」，
#: 抢在响应之前杀进程会让它看到一次莫名其妙的连接重置。
SHUTDOWN_GRACE_S = 0.35

#: 兜底硬退时间：收尾里任何一步卡住（例如浏览器不肯 terminate），
#: 也必须在这么多秒后退出 —— 「关不掉」比「关得不够优雅」严重得多。
SHUTDOWN_FORCE_EXIT_S = 4.0

#: 进程可执行文件路径的缓冲长度。Windows 长路径上限 32767，够用。
_PATH_BUFFER = 32768

#: 进程归属的三态。
type OwnerKind = Literal["self", "sibling", "foreign"]

OWNER_SELF: OwnerKind = "self"
"""本进程自己占的 —— 退出即释放，不需要动手。"""
OWNER_SIBLING: OwnerKind = "sibling"
"""同一个解释器起的兄弟进程（``run.bat`` 里的靶场窗口）—— 可以收掉。"""
OWNER_FOREIGN: OwnerKind = "foreign"
"""判不出归属，或归属不是本项目 —— **绝不碰**，只报告。"""


# --------------------------------------------------------------------------- #
# 进程可执行文件查询（Win32 / ctypes）
# --------------------------------------------------------------------------- #

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

if _kernel32 is not None:  # pragma: no cover - 平台分支，Windows 上必然进入
    # 必须声明 argtypes / restype：64 位下 HANDLE 默认按 32 位 int 传递会被**静默截断**
    # （本项目在 ``target/windows.py`` 上已经吃过这个亏，规则同样适用）。
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def process_image_path(pid: int) -> str | None:
    """取某个进程的可执行文件全路径；**取不到就返回 ``None``，不做任何猜测**。

    ``None`` 在本模块里是一个有意义的值：它意味着「无从判定这个进程是不是我们的」，
    调用方必须把它归入 ``foreign``（不碰）。用「取不到就当自己人」当默认，
    会直接把别人的服务杀掉。
    """
    if _kernel32 is None or pid <= 0:
        return None
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 权限不足（别人起的 / 受保护进程）—— 正是最需要保守的那一类，如实返回 None。
        return None
    try:
        size = wintypes.DWORD(_PATH_BUFFER)
        buffer = ctypes.create_unicode_buffer(_PATH_BUFFER)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value or None
    finally:
        _kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# 端口占用
# --------------------------------------------------------------------------- #


def parse_netstat_listening(text: str) -> dict[int, list[int]]:
    """``netstat -ano`` 输出 → ``{端口: [PID, ...]}``。**纯函数**。

    **值为什么是列表**：同一个端口**可能被多个进程同时监听** ——
    ``http.server.HTTPServer`` 默认开 ``SO_REUSEADDR``，重复起一次靶场就会这样
    （实测遇到：8899 上挂着两个 PID）。早先的实现写成 ``dict[端口, PID]``，
    后一个 PID 会**静默覆盖**前一个，于是「关机」只收掉最后一个，
    前面那些就成了没人管的孤儿进程 —— **恰好是这个功能要解决的问题**。

    只认「协议 = TCP 且 状态 = LISTENING」的行：UDP 行没有 LISTENING 状态，
    而 TIME_WAIT / ESTABLISHED 的连接**不代表有人在监听** —— 把它们算进来会让
    「端口到底还占着没有」判错。

    典型行（IPv4 与 IPv6 两种写法都要认）::

        TCP    127.0.0.1:8899    0.0.0.0:0    LISTENING    51556
        TCP    [::1]:8899        [::]:0       LISTENING    51556
    """
    found: dict[int, list[int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        # 本地地址从右往左切**一次**即可：``[::1]:8899`` 的冒号在方括号里，
        # 用 rsplit 而不是 split 才不会把 IPv6 地址切开。
        _, _, port_text = parts[1].rpartition(":")
        if not port_text.isdigit() or not parts[4].isdigit():
            continue
        pids = found.setdefault(int(port_text), [])
        pid = int(parts[4])
        if pid not in pids:  # IPv4 / IPv6 两行报同一个进程时只留一份
            pids.append(pid)
    return found


def _run_netstat() -> str:
    """跑一次 ``netstat -ano -p TCP``。失败返回空串（调用方按「查不到」处理）。"""
    if sys.platform != "win32":  # pragma: no cover - 本项目是 Windows 桌面工具
        return ""
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            errors="replace",  # 输出编码不必猜：非 ASCII 掉字符不影响解析
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("运行 netstat 失败（%s），按「没有端口占用」处理", exc)
        return ""
    return proc.stdout or ""


def port_owners(ports: Sequence[int]) -> dict[int, list[int]]:
    """查这些端口现在被哪些 PID 监听（查不到的不进结果）。**一个端口可能有多个 PID。**"""
    listening = parse_netstat_listening(_run_netstat())
    return {port: listening[port] for port in ports if port in listening}


# --------------------------------------------------------------------------- #
# 归属判定
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Occupant:
    """一个端口占用者，以及它**对本程序而言**的归属。"""

    port: int
    pid: int
    exe: str | None
    owner: OwnerKind

    @property
    def is_ours(self) -> bool:
        return self.owner in (OWNER_SELF, OWNER_SIBLING)


def _same_executable(left: str | None, right: str | None) -> bool:
    """两个路径是否指向同一个可执行文件（Windows 语义：大小写不敏感）。"""
    if not left or not right:
        return False
    left_key = os.path.normcase(str(Path(left).resolve()))
    right_key = os.path.normcase(str(Path(right).resolve()))
    return left_key == right_key


def own_executables() -> tuple[str, ...]:
    """本进程可能被报告成的**全部**可执行文件路径。

    虚拟环境是个必须绕开的陷阱，而且它**一定**会被踩到 —— 本项目永远跑在
    ``.venv`` 里：

    - ``sys.executable`` 说的是 ``.venv/Scripts/python.exe``；
    - 而 ``QueryFullProcessImageNameW`` 报回来的往往是它背后的**基础解释器**
      （``…/python/versions/3.13.12/python.exe``）—— venv 的 python.exe 与它是
      同一个镜像的两种写法。

    只比 ``sys.executable`` 的后果实测到了：``run.bat`` 起的靶场窗口被记成
    ``foreign``，于是「关机」只在清单里写一句「被别的程序占着，不会动它」，
    8899/8900 一个都没释放 —— 功能看着在，其实没生效。

    ``sys._base_executable`` 是 CPython 给 venv 用的标准属性（``venv`` 模块自己
    也这么取基础解释器），非 venv 场景下它等于 ``sys.executable``，加了不亏。
    """
    paths = [sys.executable]
    base = getattr(sys, "_base_executable", None)
    if isinstance(base, str) and base and base not in paths:
        paths.append(base)
    return tuple(paths)


def collect_occupants(
    ports: Sequence[int] = MANAGED_PORTS,
    *,
    own_pid: int | None = None,
    own_exes: Sequence[str] | None = None,
    owners: dict[int, int | Sequence[int]] | None = None,
    exe_lookup: Callable[[int], str | None] | None = None,
) -> list[Occupant]:
    """把「端口被谁占着」翻译成「它是不是我们的」。**除查询外无副作用**。

    一个端口可以对应**多个进程**（``SO_REUSEADDR``），所以这里产出的是
    **逐个 (端口, 进程) 一条** —— 谁都不许被漏掉，否则关机就只收掉一部分。

    所有入参都能注入，是为了让单测构造出「自己的 / 兄弟 / 别人的」三种情形，
    而不必真去起三个进程。``owners`` 的值写成单个 ``int`` 也接受（等同于只挂一个进程）。
    """
    pid_self = os.getpid() if own_pid is None else own_pid
    exes_self = own_executables() if own_exes is None else tuple(own_exes)
    lookup = process_image_path if exe_lookup is None else exe_lookup
    raw = port_owners(ports) if owners is None else owners
    listening = {
        port: [value] if isinstance(value, int) else list(value) for port, value in raw.items()
    }

    occupants: list[Occupant] = []
    for port, pids in sorted(listening.items()):
        for pid in pids:
            exe = lookup(pid)
            if pid == pid_self:
                owner = OWNER_SELF
            elif any(_same_executable(exe, candidate) for candidate in exes_self):
                owner = OWNER_SIBLING
            else:
                owner = OWNER_FOREIGN
            occupants.append(Occupant(port=port, pid=pid, exe=exe, owner=owner))
    return occupants


# --------------------------------------------------------------------------- #
# 计划与执行
# --------------------------------------------------------------------------- #


@dataclass
class ShutdownPlan:
    """关机计划：要做什么，以及**给用户看的**「会关闭什么」清单。"""

    occupants: list[Occupant] = field(default_factory=list)
    browser_count: int = 0
    running: bool = False

    @property
    def siblings(self) -> list[Occupant]:
        """可以收掉的兄弟进程（同一解释器起的）。"""
        return [o for o in self.occupants if o.owner == OWNER_SIBLING]

    @property
    def foreign(self) -> list[Occupant]:
        """不认识的占用者 —— **只报告，绝不动手**。"""
        return [o for o in self.occupants if o.owner == OWNER_FOREIGN]

    def reasons(self) -> list[str]:
        """「关闭会做什么」的逐条说明。

        界面直接渲染它、不自己拼文案 —— 文案与判定必须**同源**，否则会出现
        「界面写着会关靶场、实际没关」这种最伤信任的不一致。
        """
        lines = [f"停止控制台服务（127.0.0.1:{CONSOLE_PORT}）"]
        sibling_ports = sorted({o.port for o in self.siblings})
        if sibling_ports:
            ports_text = " / ".join(str(port) for port in sibling_ports)
            lines.append(f"停止本程序另起的靶场进程（端口 {ports_text}）")
        inner_ports = sorted(
            {
                o.port
                for o in self.occupants
                if o.owner == OWNER_SELF and o.port != CONSOLE_PORT
            }
        )
        if inner_ports:
            ports_text = " / ".join(str(port) for port in inner_ports)
            lines.append(f"停止控制台内的服务（端口 {ports_text}，随本进程一起退出）")
        if self.browser_count:
            lines.append(
                f"关闭本程序「接管启动」的浏览器（{self.browser_count} 个进程）"
                "—— 你自己打开的浏览器不会被关闭"
            )
        if self.running:
            lines.append("正在运行的任务会被中断（想留着就先「暂停 / 停止」）")
        if self.foreign:
            ports_text = " / ".join(str(o.port) for o in self.foreign)
            lines.append(f"端口 {ports_text} 被别的程序占着，**不会**被动它")
        return lines


@dataclass
class ShutdownReport:
    """收尾结果。用于留痕与测试断言；真实运行时它会随进程一起消失。"""

    stopped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


#: 退出函数。**可替换**：测试必须能在不杀掉 pytest 的前提下验证「该退时退了」。
_exit_fn: Callable[[int], None] = os._exit


def set_exit_fn(fn: Callable[[int], None]) -> None:
    """替换退出函数。**仅测试使用**。"""
    global _exit_fn
    _exit_fn = fn


def reset_exit_fn() -> None:
    """恢复真实的 ``os._exit``。**仅测试使用**。"""
    global _exit_fn
    _exit_fn = os._exit


def terminate_pid(pid: int, *, timeout_s: float = 5.0) -> bool:
    """用 ``taskkill`` 结束一个进程，返回是否成功。

    用 ``taskkill`` 而不是 ``ctypes.TerminateProcess``：它自带进程树处理，
    也不必自己管 HANDLE 生命周期。**不看它的输出** —— 中文系统下是 GBK，
    管道里会乱码；只看返回码。
    """
    if sys.platform != "win32" or pid <= 0:  # pragma: no cover - 平台分支
        return False
    try:
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("结束进程 %s 失败：%s", pid, exc)
        return False
    return proc.returncode == 0


def _managed_browser_count() -> int:
    """本程序自己拉起的浏览器进程数（惰性 import，避免顶层拉起 httpx / playwright）。"""
    try:
        from target.browsers import managed_processes
    except Exception as exc:  # pragma: no cover - 只有 target 层被拆坏时才会发生
        logger.warning("查询自管浏览器进程失败：%s", exc)
        return 0
    return len(managed_processes())


def _shutdown_browsers() -> int:
    """关掉本程序「接管启动」的浏览器。**绝不碰用户自己的浏览器**（见模块头红线）。"""
    try:
        from target.browsers import shutdown_managed
    except Exception as exc:  # pragma: no cover
        logger.warning("关闭自管浏览器失败：%s", exc)
        return 0
    return shutdown_managed()


def build_plan(
    *,
    ports: Sequence[int] = MANAGED_PORTS,
    own_pid: int | None = None,
    own_exes: Sequence[str] | None = None,
    owners: dict[int, int | Sequence[int]] | None = None,
    exe_lookup: Callable[[int], str | None] | None = None,
    browser_count: int | None = None,
    running: bool = False,
) -> ShutdownPlan:
    """收集当前状态并生成计划。**只查询，不关闭任何东西**。

    ``browser_count`` 传 ``None`` 时自己去问 ``target`` 层 —— 路由不该知道
    「自管浏览器进程」这件事住哪儿。
    """
    return ShutdownPlan(
        occupants=collect_occupants(
            ports,
            own_pid=own_pid,
            own_exes=own_exes,
            owners=owners,
            exe_lookup=exe_lookup,
        ),
        browser_count=_managed_browser_count() if browser_count is None else browser_count,
        running=running,
    )


def apply_shutdown(
    plan: ShutdownPlan,
    *,
    kill: Callable[[int], bool] = terminate_pid,
    stop_orchestrator: Callable[[], bool] | None = None,
    grace_s: float = SHUTDOWN_GRACE_S,
    force_exit_s: float | None = SHUTDOWN_FORCE_EXIT_S,
    exit_fn: Callable[[int], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> ShutdownReport:
    """按顺序执行关机，**最后一步一定是退出进程**。

    顺序是有讲究的：

    ① 先停编排器 —— 让正在跑的任务有个干净的收尾点，而不是被硬切；
    ② 再关自管浏览器 —— 它是独立进程，父进程退出带不走它；
    ③ 再收兄弟进程（``run.bat`` 起的那个靶场窗口）；
    ④ 最后退出自己 —— 前三步都可能耗时，只有放在退出之前才做得到。

    每一步**失败都不阻断后续**：目标只有一个「把端口都释放掉」，
    某一步没做成不该让其余步骤跟着放弃。所有结果都记进报告。

    ``kill`` / ``stop_orchestrator`` / ``exit_fn`` / ``sleep_fn`` 全部可注入，
    ``force_exit_s=None`` 表示不装兜底闸 —— 三条合起来，单测能在毫秒级跑完，
    而且不会杀掉 pytest 自己。
    """
    report = ShutdownReport()
    exit_hook = exit_fn or _exit_fn

    if grace_s > 0:
        # 让 HTTP 响应先真正到达前端。抢在响应之前退出 = 用户看到一次
        # 莫名其妙的「请求失败」，而其实关机成功了。
        sleep_fn(grace_s)

    if force_exit_s is not None and force_exit_s > 0:
        # 兜底闸：无论下面卡在哪一步，到点都必须退出。
        # 它用的是**同一个** exit_hook，否则测试里会真的把自己杀掉。
        watchdog = threading.Timer(force_exit_s, exit_hook, args=(1,))
        watchdog.daemon = True
        watchdog.start()

    if stop_orchestrator is not None:
        try:
            if stop_orchestrator():
                report.stopped.append("orchestrator")
        except Exception as exc:
            logger.warning("停止编排器失败：%s", exc)
            report.failed.append("orchestrator")

    browsers = _shutdown_browsers()
    if browsers:
        report.stopped.append(f"browsers:{browsers}")
    elif plan.browser_count:
        report.failed.append("browsers")

    # netstat 会为 IPv4/IPv6 各报一行 —— 同一个 PID 可能出现在多个端口上，
    # 按 PID 去重，否则第二次 taskkill 必然失败并被记成「失败」。
    killed: set[int] = set()
    for occupant in plan.siblings:
        if occupant.pid in killed:
            continue
        killed.add(occupant.pid)
        try:
            ok = kill(occupant.pid)
        except Exception as exc:
            logger.warning("结束进程 %s 失败：%s", occupant.pid, exc)
            ok = False
        label = f"port:{occupant.port}(pid {occupant.pid})"
        (report.stopped if ok else report.failed).append(label)

    for occupant in plan.foreign:
        # 只记不杀。这一条是「宁可不关，也不误关」的直接体现。
        report.skipped.append(f"port:{occupant.port}(pid {occupant.pid})")

    logger.info(
        "关机：已停 %s；跳过 %s；失败 %s", report.stopped, report.skipped, report.failed
    )
    exit_hook(0)
    return report
