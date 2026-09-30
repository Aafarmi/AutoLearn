"""浏览器目标采集：附加到**用户已经在用的**浏览器标签页（P11）。

核心手段是 CDP（Chrome DevTools Protocol）：浏览器以
``--remote-debugging-port=<port>`` 启动后，会在本机开一个 HTTP 端点，
任何进程都能连上去列举标签页、附加、取得页面句柄。Playwright 的
``connect_over_cdp()`` 就架在这个协议上。

v0.2.0 起读题只走模型（截当前视口交给视觉模型），本层因此**不再解析页面结构**，
只负责「把用户那个标签页变成一个可截可点的 ``Page``」。

为什么必须用**自管 Profile 目录**（本模块最重要的一条）
--------------------------------------------------------
最初的做法是复用系统的默认 User Data 目录（``%LOCALAPPDATA%\\Google\\Chrome\\User Data``），
理由是「保留登录态」。**这条路在 Chrome 136+ 上已经彻底不通** ——
Chrome 官方公告（developer.chrome.com/blog/remote-debugging-port）：

    从 Chrome 136 开始，如果尝试调试**默认的** Chrome 数据目录，
    ``--remote-debugging-port`` 将不再被遵循；这些开关必须搭配
    ``--user-data-dir`` 指向**非标准目录**。

注意措辞是「不再被遵循」——**不是报错，是静默忽略**。症状是：进程起来了、
窗口也开了、一切看起来正常，但调试端口根本没监听，于是附加永远失败、
标签页列表永远是空的。这与「浏览器没装」「参数写错」的排查方向完全不同，
极易误判（本项目实测在 Chrome 153 上踩到，且旧版报错文案还错误地把它
归因成「已有实例在运行」）。

自管目录同时解决第二个问题：**不会与用户正在运行的浏览器抢同一个 profile**。
Chrome/Edge 用 ``--user-data-dir`` 标识进程归属，同一目录被占用时新进程会把请求
转交给老实例并丢弃全部参数 —— 用独立目录就没有这回事，用户不必先关掉自己的浏览器。

代价与交代：自管目录是**全新的浏览器身份**，首次需要在这里登录一次，之后长期保留。
这也是官方推荐的做法（「将任何调试作业与任何实际 Profile 隔离」），
且是唯一能同时满足「调试端口可用」与「不动用户真实数据」的方案。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx

from core.enums import TargetKind
from core.targets import DEFAULT_DEBUG_PORT
from target.base import TargetHandle, TargetInfo, TargetSource, TargetUnavailableError

__all__ = [
    "DEFAULT_DEBUG_PORT",
    "DEFAULT_DIR_BLOCKED_MAJOR",
    "MANAGED_PROFILE_ROOT",
    "BrowserTargetSource",
    "browser_major_version",
    "find_browser_executable",
    "managed_processes",
    "managed_user_data_dir",
    "shutdown_managed",
    "system_user_data_dir",
]

logger = logging.getLogger(__name__)

#: 本进程**亲手拉起**的浏览器进程登记表。
#:
#: 为什么需要它：这些浏览器是**独立进程**，带着 ``--remote-debugging-port``，
#: 主进程退出不会带走它们 —— 而改造前**没有任何对象持有它们**：
#: ``ui.assembly.make_browser_source()`` 每次调用都新建实例，请求一结束实例就被回收，
#: 于是「程序关了，那个浏览器还活着、调试端口还占着」，只能手动去关。
#: 登记表让「接管启动过的浏览器」有了一个可被关机流程找到的归属。
#:
#: 表里**只会**出现 :meth:`BrowserTargetSource.launch` 起的进程：
#: 附加到用户已有浏览器时 ``_process`` 恒为 ``None``，一个都不会进表。
_MANAGED: list[subprocess.Popen[bytes]] = []
_MANAGED_LOCK = threading.Lock()


def managed_processes() -> tuple[subprocess.Popen[bytes], ...]:
    """当前仍由本进程持有、且是自己拉起的浏览器进程。

    退出码已被读走（``poll()`` 非 ``None``）的进程不再返回 —— 用户自己关掉的
    那个窗口不该再出现在「会被关闭」的清单里。
    """
    with _MANAGED_LOCK:
        return tuple(process for process in _MANAGED if process.poll() is None)


def shutdown_managed() -> int:
    """结束全部自管浏览器进程，返回**确实收掉**的个数。

    这是关机链路的第 ② 步（见 :mod:`ui.system`）：调用方已经明确要关机，
    这里只负责执行。红线与 :meth:`BrowserTargetSource.shutdown` 完全一致 ——
    只认登记表，表里的每一个进程都是 ``launch`` 亲手起的。
    """
    with _MANAGED_LOCK:
        processes = list(_MANAGED)
        _MANAGED.clear()

    for process in processes:
        with suppress(Exception):
            process.terminate()
    stopped = 0
    for process in processes:
        with suppress(Exception):
            process.wait(timeout=10)
        if process.poll() is not None:
            stopped += 1
    return stopped


def _register_process(process: subprocess.Popen[bytes]) -> None:
    """把亲手起的浏览器进程登记进表（只由 :meth:`BrowserTargetSource.launch` 调用）。"""
    with _MANAGED_LOCK:
        _MANAGED.append(process)


def _unregister_process(process: subprocess.Popen[bytes]) -> None:
    """从表里摘掉一个进程（已被显式关掉时调用，避免重复 terminate）。"""
    with _MANAGED_LOCK, suppress(ValueError):
        _MANAGED.remove(process)

#: 自管 Profile 根目录，每个 channel 一个子目录。
#: **绝不能换成系统的默认 User Data 目录**，理由见模块头。
MANAGED_PROFILE_ROOT = Path("state/browser_profile")

#: CDP 端点只监听回环地址。**绑 127.0.0.1 而不是 0.0.0.0**：调试端口等于
#: 无鉴权的浏览器控制权，绝不能暴露到局域网。
_CDP_HOST = "127.0.0.1"

#: 浏览器可执行文件的候选位置。用环境变量而不是硬编码盘符，
#: ``Program Files`` 与 ``Program Files (x86)`` 都要试。
_BROWSER_PATHS: dict[str, tuple[str, ...]] = {
    "chrome": (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ),
    "msedge": (
        r"%ProgramFiles(x86)%Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    ),
}

#: 各浏览器的**系统默认**用户数据目录。
#:
#: ⚠️ 只用于「告诉用户他的真实 Profile 在哪」这类文案，**不要拿它去启动** ——
#: 见模块头。
_SYSTEM_USER_DATA_DIRS: dict[str, str] = {
    "chrome": r"%LOCALAPPDATA%\Google\Chrome\User Data",
    "msedge": r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
}

#: 不是可操作页面的 URL 前缀。扩展页与 DevTools 页对用户没有意义，
#: 混进列表只会让人选错。
_SKIP_URL_PREFIXES = ("devtools://", "chrome-extension://", "edge-extension://")

#: 主版本号达到它之后，Chrome 拒绝为**默认数据目录**开调试端口。
DEFAULT_DIR_BLOCKED_MAJOR = 136


def _expand(raw: str) -> Path:
    return Path(os.path.expandvars(raw))


def find_browser_executable(channel: str) -> Path | None:
    """按 channel 名找系统浏览器的可执行文件。找不到返回 ``None``。"""
    for raw in _BROWSER_PATHS.get(channel, ()):
        candidate = _expand(raw)
        if candidate.is_file():
            return candidate
    return None


def system_user_data_dir(channel: str) -> Path | None:
    """用户**系统默认**的浏览器 Profile 目录。

    仅供展示与排障。**不要用它启动带调试端口的浏览器** —— 见模块头。
    """
    raw = _SYSTEM_USER_DATA_DIRS.get(channel)
    return _expand(raw) if raw else None


def managed_user_data_dir(channel: str, root: Path | None = None) -> Path:
    """软件自管的 Profile 目录。带调试端口启动时**必须**用它，见模块头。"""
    return (root if root is not None else MANAGED_PROFILE_ROOT) / channel


def browser_major_version(channel: str) -> int | None:
    """从安装目录名读出浏览器主版本号（如 ``153``）。读不到返回 ``None``。

    走目录名而不是读 exe 的版本资源：Chrome 的安装目录恒为
    ``Application\\<完整版本>\\``，这是最省事也最可靠的一条路径。
    """
    exe = find_browser_executable(channel)
    if exe is None:
        return None
    with suppress(OSError):
        for entry in exe.parent.iterdir():
            match = re.match(r"^(\d+)\.", entry.name) if entry.is_dir() else None
            if match:
                return int(match.group(1))
    return None


class BrowserTargetSource(TargetSource):
    """附加到用户浏览器的目标采集源。

    有意**不持有常驻连接**：``list_targets()`` 只走 CDP 的 HTTP 口（纯 httpx，
    不起 Playwright），``open()`` 才建立真正的 playwright 连接并在退出时断开。
    这样「抓取」这个高频动作不会为每次点击付一次浏览器握手的代价，
    也不会有跨请求的事件循环归属问题。
    """

    kind = TargetKind.BROWSER_PAGE

    def __init__(
        self,
        *,
        port: int = DEFAULT_DEBUG_PORT,
        channel: str = "chrome",
        user_data_dir: Path | str | None = None,
        profile_root: Path | None = None,
        profile: str | None = None,
        timeout_s: float = 1.5,
        extra_args: Sequence[str] = (),
    ) -> None:
        self.port = port
        self.channel = channel
        #: 带调试端口启动时用的数据目录。默认走**自管目录**，不是系统默认目录 ——
        #: 理由见模块头（Chrome 136+ 会静默拒绝为默认目录开端口）。
        #:
        #: **一律解析成绝对路径**：Chrome 对相对的 ``--user-data-dir`` 会**立刻退出**
        #: （退出码 21，且 stderr 一个字节都不吐），表现与「浏览器没装」几乎一样，
        #: 极难归因。本项目实测踩到过：自管根目录写成 ``state/...`` 时产品路径必然失败，
        #: 而用 ``tempfile`` 的自检脚本（绝对路径）却是绿的 —— 这种「测试绿、产品挂」
        #: 的偏差正是要在构造处一次性消掉的。
        self.user_data_dir = (
            Path(user_data_dir)
            if user_data_dir is not None
            else managed_user_data_dir(channel, profile_root)
        ).resolve()
        #: 浏览器内的 profile 目录名（``Default`` / ``Profile 1``…）。为空则交给
        #: 浏览器自己选 —— 自管目录是全新的，选哪个都一样。
        self.profile = profile
        self.timeout_s = timeout_s
        #: 追加到命令行末尾的额外参数。主要用途是**验证与排障**
        #: （``--headless=new`` 免开窗口），产品路径留空即可。
        self.extra_args = tuple(extra_args)
        #: 本实例亲手拉起的浏览器进程。附加到已有浏览器时恒为 ``None``。
        self._process: subprocess.Popen[bytes] | None = None

    # ------------------------------------------------------------------ 端点

    @property
    def endpoint(self) -> str:
        return f"http://{_CDP_HOST}:{self.port}"

    async def _get_json(self, path: str) -> Any | None:
        """打一次 CDP 的 HTTP 口。连不上或非 200 都返回 ``None``。

        **``trust_env=False`` 是必须的**：开发机上有 ``HTTP_PROXY`` 时，
        httpx 默认会把请求交给代理，而代理不认识 ``127.0.0.1`` 上的 CDP 端点 ——
        表现是「浏览器明明起着、端口明明开着，探测却超时」。回环地址永远不该走代理。
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, trust_env=False) as client:
                resp = await client.get(f"{self.endpoint}{path}")
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception:  # 连不上就是没端点，这不是异常路径
            return None

    async def is_endpoint_alive(self) -> bool:
        """调试端口上有没有活的浏览器。

        ``/json/version`` 是 CDP 的探活口：任何带调试端口的 Chromium 系浏览器
        都会答它，且不需要先建立 WebSocket 会话。
        """
        return await self._get_json("/json/version") is not None

    @property
    def version_blocked(self) -> bool:
        """该 channel 是否已到「拒绝默认 Profile 目录」的版本。

        只用于把话说清楚（文案里解释为什么要有自管目录），**不参与启动决策** ——
        我们总是用自管目录，所以在任何版本上都是安全的。
        """
        major = browser_major_version(self.channel)
        return major is not None and major >= DEFAULT_DIR_BLOCKED_MAJOR

    # ------------------------------------------------------------------ 列出

    async def list_targets(self) -> list[TargetInfo]:
        """列出可附加的标签页。**端点没起时返回空表而不是抛异常**。

        返回空表是本方法的契约：UI 上「点抓取 → 没结果 → 显示一句怎么把浏览器
        带端口启动」，比弹一个异常更贴近实际用法。
        """
        raw_targets = await self._get_json("/json/list")
        if not isinstance(raw_targets, list):
            return []

        infos: list[TargetInfo] = []
        for raw in raw_targets:
            # background_page / browser_ui / service_worker 都不是可操作目标
            if not isinstance(raw, dict) or raw.get("type") != "page":
                continue
            url = str(raw.get("url") or "")
            if url.startswith(_SKIP_URL_PREFIXES):
                continue
            infos.append(
                TargetInfo(
                    target_id=str(raw.get("id") or ""),
                    kind=self.kind,
                    title=str(raw.get("title") or ""),
                    url=url,
                    app=self.channel,
                )
            )
        return infos

    # ------------------------------------------------------------------ 启动

    def launch(self, *, start_urls: Sequence[str] = ()) -> subprocess.Popen[bytes]:
        """带调试端口拉起浏览器，用**自管 Profile**。

        **不等待、不检查** —— 端口能不能起来由 :meth:`launch_and_wait` 裁决。
        拆开是因为「拉起进程」和「确认端口可用」是两件事，后者要轮询。

        ``start_urls`` 会作为位置参数交给浏览器，每个开一个标签页 ——
        这是「**保证标签页能够正确识别**」的前半段：全新的空 Profile 如果什么都不
        打开，用户面对的就是一个空白新标签页，分不清「扫描没扫到」和「我还没开页面」。
        """
        exe = find_browser_executable(self.channel)
        if exe is None:
            raise TargetUnavailableError(
                f"找不到 {self.channel} 的可执行文件，无法接管启动。"
                f"已尝试：{', '.join(_BROWSER_PATHS.get(self.channel, ()))}"
            )
        args = [
            str(exe),
            f"--remote-debugging-port={self.port}",
            # 只监听回环：调试端口无鉴权，不能暴露到局域网
            f"--remote-debugging-address={_CDP_HOST}",
            # 下面这条是**整个功能的前提**：非标准数据目录。
            # 换成系统默认目录后 Chrome 136+ 会静默忽略调试端口（见模块头）。
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self.profile:
            args.append(f"--profile-directory={self.profile}")
        args.extend(self.extra_args)
        args.extend(start_urls)
        logger.info(
            "接管启动浏览器：%s（调试端口 %s，profile %s）",
            exe,
            self.port,
            self.user_data_dir,
        )
        self._process = subprocess.Popen(args)
        # 登记进自管表：这是**独立进程**，主进程退出带不走它；关机流程要靠这张表
        # 才找得到它（否则它带着调试端口活到用户手动关掉为止）。
        _register_process(self._process)
        return self._process

    async def launch_and_wait(
        self,
        *,
        wait_s: float = 25.0,
        tab_wait_s: float = 10.0,
        start_urls: Sequence[str] = (),
    ) -> None:
        """**把「接管启动」一次做完**：建目录 → 起进程 → 等端口 → 等出标签页。

        调用方只需要调这一个方法，不必自己拼前置步骤。三个阶段各有明确语义：

        ==========================  ===============================================
        端口没起来且进程已退出      参数/策略问题（被杀软拦、策略禁用远程调试）
        进程还活着但端口始终没监听  超时；报出 profile 路径供自查，并点明
                                   「默认数据目录会被 Chrome 136+ 静默忽略」这条
        端口就绪但枚举不到标签页    在 `tab_wait_s` 内没有可识别页面
        ==========================  ===============================================

        已有一个带端口的实例在跑时**直接复用**，不再重复拉起 —— 反复点
        「接管启动」不该越开越多。
        """
        if await self.is_endpoint_alive():
            logger.info("调试端口 %s 已有活端点，复用现成浏览器", self.port)
            return

        # 前置：数据目录必须先存在。某些版本的 Chrome 遇到不存在的
        # --user-data-dir 会走「首次运行向导」，拖慢启动还可能弹窗。
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        process = self.launch(start_urls=start_urls)

        # -- 第一段：等调试端口 ------------------------------------------------
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if await self.is_endpoint_alive():
                break
            exit_code = process.poll()
            if exit_code is not None:
                raise TargetUnavailableError(
                    f"{self.channel} 启动后立刻退出（退出码 {exit_code}），"
                    "调试端口没机会打开。\n"
                    f"数据目录：{self.user_data_dir}\n"
                    "已实测的对应关系：退出码 **21** = 数据目录不可用，最常见的原因是"
                    "路径是**相对路径**（Chrome 不接受）或该目录无写权限；"
                    "其余常见原因：安全软件拦截、企业策略禁用远程调试、浏览器安装损坏。"
                )
            await asyncio.sleep(0.25)
        else:
            raise TargetUnavailableError(
                f"调试端口 {self.port} 在 {wait_s:.0f} 秒内没有监听，"
                f"但 {self.channel} 进程还活着。请检查数据目录 {self.user_data_dir} "
                "是否可用（权限 / 被占用）。\n"
                "注意：若把数据目录换成了浏览器的**系统默认**目录，"
                f"Chrome {DEFAULT_DIR_BLOCKED_MAJOR}+ 会**静默忽略**调试端口开关。"
            )

        # -- 第二段：等出**可识别的标签页** ------------------------------------
        # 端口能连上不等于标签页已经建好，启动瞬间 /json/list 可能是空的。
        # 这一段是「保证标签页能够正确识别」的后半段：不等它，用户点开扫描
        # 会看到空列表，然后误以为功能坏了。
        tab_deadline = time.monotonic() + tab_wait_s
        while time.monotonic() < tab_deadline:
            if await self.list_targets():
                return
            await asyncio.sleep(0.25)

        raise TargetUnavailableError(
            f"调试端口 {self.port} 已就绪，但 {tab_wait_s:.0f} 秒内没有枚举到任何网页"
            f"标签页（数据目录 {self.user_data_dir}）。"
            "请在该浏览器窗口里打开你要抓的页面后重试。"
        )

    def shutdown(self) -> None:
        """结束**本实例自己拉起的**浏览器进程。

        红线：只认 `self._process` —— 那是 :meth:`launch` / :meth:`launch_and_wait`
        亲手起的进程。附加到用户已有浏览器时 `self._process` 是 ``None``，
        这里就什么都不做。**绝不允许把「关掉用户的浏览器」写进这个方法。**

        收掉之后要从自管表里摘掉，否则 :func:`shutdown_managed` 会对着一个
        已经退出的进程再 terminate 一次，把「关机报告」写成一次失败。
        """
        process, self._process = self._process, None
        if process is None:
            return
        _unregister_process(process)
        with suppress(Exception):
            process.terminate()
        with suppress(Exception):
            process.wait(timeout=10)

    # ------------------------------------------------------------------ 附加

    async def ensure_ready(self) -> None:
        """确认可以附加。不能则抛**可操作**的错误。

        启动运行前调一次：附加不上是「目标没准备好」，不是「题目读不出来」，
        两者该给用户的提示完全不同，所以必须在启动前就分清楚。
        """
        if not await self.is_endpoint_alive():
            raise TargetUnavailableError(
                f"调试端口 {self.port} 上没有活端点（{self.endpoint} 连不上）："
                "当前没有由本软件带调试端口启动的浏览器。请先点「接管启动浏览器」。"
            )

    @asynccontextmanager
    async def open(self, target_id: str) -> AsyncIterator[TargetHandle]:
        """附加到指定标签页。退出时**只断开连接**，不动用户的浏览器。"""
        await self.ensure_ready()

        known = {info.target_id: info for info in await self.list_targets()}
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(self.endpoint)
            try:
                page = await self._locate(browser, target_id)
                if page is None:
                    raise TargetUnavailableError(
                        f"目标 {target_id} 不存在或已关闭，可能刚被用户关掉了。"
                    )
                info = known.get(target_id) or TargetInfo(
                    target_id=target_id,
                    kind=self.kind,
                    title="",
                    url=getattr(page, "url", None),
                    app=self.channel,
                )
                handle = TargetHandle(info=info, page=page)
                try:
                    yield handle
                finally:
                    await handle.aclose()
            finally:
                # **只断开 CDP 连接。** 对 connect_over_cdp 得来的 browser，
                # close() 的语义是「断开」而不是「杀死」—— 用户的窗口留着。
                with suppress(Exception):
                    await browser.close()

    @staticmethod
    async def _locate(browser: Any, target_id: str) -> Any | None:
        """按 CDP target id 找回那个 Page。

        Playwright 没把 target id 暴露成属性，所以逐个开一次 CDP 会话问
        ``Target.getTargetInfo``。标签页是「个位数」量级，这个代价可以接受；
        换来的是**按 id 精确匹配** —— 按 URL 猜在同一地址开了两个标签页时就废了。
        """
        for context in browser.contexts:
            for page in context.pages:
                try:
                    session = await context.new_cdp_session(page)
                    try:
                        payload = await session.send("Target.getTargetInfo")
                    finally:
                        with suppress(Exception):
                            await session.detach()
                except Exception as exc:
                    logger.debug("读标签页 target id 失败，跳过：%s", exc)
                    continue
                found = str(payload.get("targetInfo", {}).get("targetId") or "")
                if found == target_id:
                    return page
        return None
