"""系统关机（``/api/system/*`` 与 ``ui.system``）验收。

**全部纯内存** —— 不起靶场、不起浏览器，也**绝不真的退出进程**：

- 文件顶部的 autouse 夹具把退出函数换成「一调用就判测试失败」的替身；
- 路由用例还把 ``apply_shutdown`` 换成只记录调用的假函数 ——
  ``TestClient`` 会在响应返回后执行 background tasks，不换就等于让用例
  亲手把 pytest 关掉。

这个文件真正要钉死的是**那条红线**：「只关确定是本程序起的东西」。
判定错一个方向就是两个事故 ——
判松了会杀掉别人的服务（无法向用户解释），
判紧了会让「关机」关不干净（功能等于没做）。
所以纯函数判定用例是这个文件的主体。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from target import browsers
from ui import deps, system

# --------------------------------------------------------------------------- #
# 夹具：确保任何时候都不会真的退出进程
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _never_really_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """把真实的 ``os._exit`` 换掉。

    万一某个用例漏了 patch 走到收尾，它会**明确失败**而不是把 pytest 关掉 ——
    「测试跑着跑着整个进程消失」是最难查的一类假绿。
    """

    def _boom(code: int) -> None:
        raise AssertionError(f"测试里不该真的退出进程（code={code}）")

    monkeypatch.setattr(system, "_exit_fn", _boom)


@pytest.fixture()
def no_background(monkeypatch: pytest.MonkeyPatch) -> list[system.ShutdownPlan]:
    """把收尾动作换成记录器，并返回记录列表。"""
    seen: list[system.ShutdownPlan] = []
    monkeypatch.setattr(system, "apply_shutdown", lambda plan: seen.append(plan))
    return seen


# --------------------------------------------------------------------------- #
# netstat 解析（纯函数）
# --------------------------------------------------------------------------- #

_NETSTAT_SAMPLE = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    127.0.0.1:8899         0.0.0.0:0              LISTENING       51556
  TCP    127.0.0.1:8900         0.0.0.0:0              LISTENING       51556
  TCP    127.0.0.1:8800         0.0.0.0:0              LISTENING       12345
  TCP    127.0.0.1:53123        127.0.0.1:8800         ESTABLISHED     998
  TCP    127.0.0.1:53124        127.0.0.1:8800         TIME_WAIT       0
  UDP    127.0.0.1:1900         *:*                                    1234
  TCP    [::1]:8899             [::]:0                 LISTENING       51556
"""


def test_parse_netstat_keeps_only_tcp_listening() -> None:
    """**只有 TCP + LISTENING 才算「端口被占着」**。

    ESTABLISHED / TIME_WAIT 是已经建立的连接，不代表有人在监听 ——
    把它们算进来，会让「关完之后端口释放了吗」这个判断失真；
    UDP 行没有 LISTENING 状态，同理。
    """
    found = system.parse_netstat_listening(_NETSTAT_SAMPLE)
    # 值现在是**列表**：一个端口可以被多个进程监听（SO_REUSEADDR），
    # 早先写成单个 PID 会让后一个覆盖前一个，孤儿进程就收不掉了。
    assert found == {8899: [51556], 8900: [51556], 8800: [12345]}


def test_parse_netstat_understands_ipv6_rows() -> None:
    """IPv6 的 ``[::1]:8899`` 也要认出来 —— 冒号在方括号里，切错就整行丢掉。"""
    found = system.parse_netstat_listening("  TCP    [::1]:8899    [::]:0    LISTENING    51556\n")
    assert found == {8899: [51556]}


def test_parse_netstat_ignores_garbage() -> None:
    """表头、空行、缺列、PID 不是数字 —— 一律忽略，不抛异常。"""
    garbage = "\n".join(
        [
            "",
            "  协议  本地地址          外部地址        状态           PID",
            "  TCP    127.0.0.1:8899",
            "  TCP    127.0.0.1:notaport   0.0.0.0:0   LISTENING   123",
            "  TCP    127.0.0.1:8899        0.0.0.0:0   LISTENING   abc",
            "  ???",
        ]
    )
    assert system.parse_netstat_listening(garbage) == {}


# --------------------------------------------------------------------------- #
# 归属判定（纯函数）—— 本文件的重点
# --------------------------------------------------------------------------- #


def _lookup(mapping: dict[int, str | None]):
    return lambda pid: mapping.get(pid)


def test_occupant_kinds_are_three_way() -> None:
    """自己 / 兄弟 / 外人 —— 三种归属必须分清。"""
    own_exe = r"C:\proj\.venv\Scripts\python.exe"
    occupants = system.collect_occupants(
        [8800, 8899, 9999],
        own_pid=100,
        own_exes=[own_exe],
        owners={8800: 100, 8899: 200, 9999: 300},
        exe_lookup=_lookup(
            {100: own_exe, 200: own_exe, 300: r"C:\other\node.exe"}
        ),
    )
    kinds = {o.port: o.owner for o in occupants}
    assert kinds == {8800: system.OWNER_SELF, 8899: system.OWNER_SIBLING, 9999: system.OWNER_FOREIGN}


def test_unknown_process_is_foreign_not_sibling() -> None:
    """**查不到可执行文件时必须归为外人**。

    这是最关键的一条：``process_image_path`` 在权限不足时返回 ``None``，
    而「权限不足」恰恰多发生在别的服务身上。「查不到就当自己人」的实现
    会直接把用户机器上的别人的服务杀掉。
    """
    occupants = system.collect_occupants(
        [8899],
        own_pid=100,
        own_exes=[r"C:\proj\.venv\Scripts\python.exe"],
        owners={8899: 200},
        exe_lookup=_lookup({200: None}),
    )
    assert occupants[0].owner == system.OWNER_FOREIGN


def test_sibling_match_is_case_insensitive() -> None:
    """Windows 路径大小写不敏感 —— ``C:\\Py\\python.exe`` 与 ``c:\\py\\PYTHON.EXE`` 是同一个。"""
    occupants = system.collect_occupants(
        [8899],
        own_pid=100,
        own_exes=[r"C:\Proj\.venv\Scripts\python.exe"],
        owners={8899: 200},
        exe_lookup=_lookup({200: r"c:\proj\.venv\scripts\PYTHON.EXE"}),
    )
    assert occupants[0].owner == system.OWNER_SIBLING


def test_own_executables_covers_the_venv_base_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """虚拟环境里「自己人」是**一组**路径，不是一条 —— 这条是实测踩出来的。

    ``run.bat`` 起的靶场窗口，进程镜像报的是 ``.venv`` 背后的基础解释器，
    而 ``sys.executable`` 说的是 ``.venv/Scripts/python.exe``。只比后者会把它
    判成外人：关机的清单里出现「端口 8899/8900 被别的程序占着，不会动它」，
    而实际上一个都没释放 —— 功能看着在，其实没生效。
    """
    monkeypatch.setattr(system.sys, "executable", r"C:\proj\.venv\Scripts\python.exe")
    monkeypatch.setattr(system.sys, "_base_executable", r"C:\Python313\python.exe", raising=False)

    assert set(system.own_executables()) == {
        r"C:\proj\.venv\Scripts\python.exe",
        r"C:\Python313\python.exe",
    }

    occupants = system.collect_occupants(
        [8899],
        own_pid=100,
        own_exes=system.own_executables(),
        owners={8899: 200},
        exe_lookup=_lookup({200: r"c:\python313\PYTHON.EXE"}),
    )
    assert occupants[0].owner == system.OWNER_SIBLING


def test_own_executables_has_no_duplicate_when_not_in_a_venv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 venv 场景 ``_base_executable`` 就是自己 —— 不该出现重复项。"""
    monkeypatch.setattr(system.sys, "executable", r"C:\Python313\python.exe")
    monkeypatch.setattr(system.sys, "_base_executable", r"C:\Python313\python.exe", raising=False)
    assert system.own_executables() == (r"C:\Python313\python.exe",)


def test_self_ports_are_never_marked_sibling() -> None:
    """自己的 PID 永远是 ``self``，哪怕它占着 8899/8900（``launcher.py`` 单进程形态）。"""
    own_exe = r"C:\proj\.venv\Scripts\python.exe"
    occupants = system.collect_occupants(
        [8800, 8899, 8900],
        own_pid=100,
        own_exes=[own_exe],
        owners={8800: 100, 8899: 100, 8900: 100},
        exe_lookup=_lookup({100: own_exe}),
    )
    assert {o.owner for o in occupants} == {system.OWNER_SELF}


# --------------------------------------------------------------------------- #
# 计划与「会关闭什么」清单
# --------------------------------------------------------------------------- #


def _plan(**kwargs: object) -> system.ShutdownPlan:
    base: dict[str, object] = {
        "ports": [8800, 8899],
        "own_pid": 100,
        "own_exes": [r"C:\proj\python.exe"],
        "owners": {8800: 100, 8899: 200},
        "exe_lookup": _lookup({100: r"C:\proj\python.exe", 200: r"C:\proj\python.exe"}),
    }
    base.update(kwargs)
    return system.build_plan(**base)  # type: ignore[arg-type]


def test_plan_reasons_name_every_port_it_touches() -> None:
    """清单里必须点名控制台与兄弟进程 —— 它同时是界面上那句提示的来源。"""
    lines = _plan().reasons()
    text = "\n".join(lines)
    assert "8800" in text
    assert "8899" in text


def test_plan_reasons_mention_browsers_and_running_task() -> None:
    lines = _plan(browser_count=2, running=True).reasons()
    text = "\n".join(lines)
    assert "浏览器" in text and "2 个进程" in text
    assert "中断" in text


def test_plan_reasons_say_foreign_entries_are_left_alone() -> None:
    """别人的服务要**明确告诉用户不会动**，否则他关完发现端口还占着会以为坏了。"""
    plan = system.build_plan(
        ports=[9999],
        own_pid=100,
        own_exes=[r"C:\proj\python.exe"],
        owners={9999: 300},
        exe_lookup=_lookup({300: r"C:\other\node.exe"}),
        browser_count=0,
    )
    assert [o.port for o in plan.foreign] == [9999]
    assert any("不会" in line for line in plan.reasons())


# --------------------------------------------------------------------------- #
# 收尾执行：顺序、隔离、只杀该杀的
# --------------------------------------------------------------------------- #


def test_apply_shutdown_kills_only_siblings() -> None:
    """**只对兄弟进程动手**；自己的端口靠退出释放，外人的端口一律不碰。"""
    killed: list[int] = []
    exits: list[int] = []
    plan = _plan(ports=[8800, 8899, 9999], owners={8800: 100, 8899: 200, 9999: 300})

    system.apply_shutdown(
        plan,
        kill=lambda pid: killed.append(pid) or True,
        grace_s=0,
        force_exit_s=None,
        exit_fn=exits.append,
        sleep_fn=lambda _s: None,
    )

    assert killed == [200]
    assert exits == [0]


def test_apply_shutdown_deduplicates_same_pid_on_two_ports() -> None:
    """IPv4/IPv6 各报一行 → 同一个 PID 出现两次，只能杀一次。

    第二次必然失败（进程已经没了），会把「关机报告」写成一次假失败。
    """
    killed: list[int] = []
    plan = _plan(ports=[8899, 8900], owners={8899: 200, 8900: 200})

    system.apply_shutdown(
        plan,
        kill=lambda pid: killed.append(pid) or True,
        grace_s=0,
        force_exit_s=None,
        exit_fn=lambda _c: None,
        sleep_fn=lambda _s: None,
    )

    assert killed == [200]


def test_apply_shutdown_survives_a_failing_step() -> None:
    """某一步失败不阻断后续 —— 目标只有「把端口释放掉」，不该因为一个失败全放弃。"""
    calls: list[str] = []

    def _bad_kill(pid: int) -> bool:
        calls.append(f"kill:{pid}")
        raise OSError("拒绝访问")

    def _stop_orchestrator() -> bool:
        calls.append("stop")
        return True

    plan = _plan()
    report = system.apply_shutdown(
        plan,
        kill=_bad_kill,
        stop_orchestrator=_stop_orchestrator,
        grace_s=0,
        force_exit_s=None,
        exit_fn=lambda _c: None,
        sleep_fn=lambda _s: None,
    )

    assert calls == ["stop", "kill:200"], "编排器与杀进程都要被尝试"
    assert report.stopped == ["orchestrator"]
    assert any("8899" in item for item in report.failed)
    assert report.skipped == []


def test_apply_shutdown_reports_foreign_as_skipped() -> None:
    plan = _plan(ports=[9999], owners={9999: 300})
    report = system.apply_shutdown(
        plan,
        kill=lambda _pid: pytest.fail("外人的端口不该被碰"),
        grace_s=0,
        force_exit_s=None,
        exit_fn=lambda _c: None,
        sleep_fn=lambda _s: None,
    )
    assert report.skipped == ["port:9999(pid 300)"]


# --------------------------------------------------------------------------- #
# 自管浏览器：登记表只管自己起的
# --------------------------------------------------------------------------- #


class _FakeProcess:
    """够用的假进程：只实现 ``managed_processes`` / ``shutdown_managed`` 用到的那三件事。"""

    def __init__(self, *, alive: bool = True) -> None:
        self.alive = alive
        self.terminated = False
        self.waited = False

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


def test_shutdown_managed_only_touches_registered_processes() -> None:
    """**登记表之外一个都不碰** —— 附加到用户已有浏览器时它不进表，就不该被关。"""
    mine = _FakeProcess()
    browsers._register_process(mine)  # type: ignore[arg-type]
    try:
        assert browsers.managed_processes() == (mine,)
        assert browsers.shutdown_managed() == 1
        assert mine.terminated is True
        assert browsers.managed_processes() == (), "收完后表要清空，重复调用不该再杀一次"
    finally:
        browsers._unregister_process(mine)  # type: ignore[arg-type]


def test_managed_processes_skips_already_dead_ones() -> None:
    """用户自己关掉的那个窗口不该再出现在「会被关闭」的清单里。"""
    dead = _FakeProcess(alive=False)
    browsers._register_process(dead)  # type: ignore[arg-type]
    try:
        assert browsers.managed_processes() == ()
    finally:
        browsers._unregister_process(dead)  # type: ignore[arg-type]


def test_two_processes_on_the_same_port_are_both_collected() -> None:
    """**同一个端口上挂了两个进程时，两个都要被认出、都要被收掉。**

    实测遇到（2026-09-28）：8899 上同时挂着两个 PID —— ``http.server`` 默认开了
    ``SO_REUSEADDR``，重复起一次靶场就会这样。早先的实现用 ``{端口: PID}``，
    后一个 PID 会**静默覆盖**前一个，于是「关机」只收掉最后一个，
    前面那些成了没人管的孤儿进程 —— 恰好是这个功能要解决的问题。
    """
    own_exe = r"C:\proj\python.exe"
    owners = {8899: [200, 300]}
    lookup = _lookup({200: own_exe, 300: own_exe})

    occupants = system.collect_occupants(
        [8899], own_pid=100, own_exes=[own_exe], owners=owners, exe_lookup=lookup
    )
    assert [o.pid for o in occupants] == [200, 300]
    assert {o.owner for o in occupants} == {system.OWNER_SIBLING}

    plan = system.build_plan(
        ports=[8899], own_pid=100, own_exes=[own_exe], owners=owners,
        exe_lookup=lookup, browser_count=0,
    )
    killed: list[int] = []
    system.apply_shutdown(
        plan,
        kill=lambda pid: killed.append(pid) or True,
        grace_s=0,
        force_exit_s=None,
        exit_fn=lambda _c: None,
        sleep_fn=lambda _s: None,
    )
    assert killed == [200, 300], "同端口上的两个进程都要被收掉"


def test_explicit_shutdown_unregisters_so_report_stays_clean() -> None:
    """显式 ``shutdown()`` 之后要从表里摘掉 —— 否则关机会对着死进程再杀一次。"""
    source = browsers.BrowserTargetSource.__new__(browsers.BrowserTargetSource)
    process = _FakeProcess()
    source._process = process  # type: ignore[attr-defined]
    browsers._register_process(process)  # type: ignore[arg-type]
    try:
        source.shutdown()
        assert process.terminated is True
        assert browsers.managed_processes() == ()
    finally:
        browsers._unregister_process(process)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 路由契约
# --------------------------------------------------------------------------- #


def test_status_is_read_only_and_has_preview(client: TestClient) -> None:
    resp = client.get("/api/system/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ports", "browsers", "running", "preview"}
    assert body["preview"], "清单不能是空的 —— 界面靠它说明会关掉什么"
    assert body["running"] is False
    for entry in body["ports"]:
        assert entry["owner"] in {"self", "sibling", "foreign"}


def test_shutdown_returns_receipt(client: TestClient, no_background: list[system.ShutdownPlan]) -> None:
    resp = client.post("/api/system/shutdown")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["preview"]
    assert len(no_background) == 1, "收尾动作应该被排上（这里被换成了记录器）"


def test_shutdown_refuses_while_a_run_is_active(client: TestClient, no_background: list) -> None:
    """有任务在跑时**默认拒绝** —— 静默中断一个跑了半小时的任务是很坏的失败模式。"""
    deps.set_orchestrator(object())  # type: ignore[arg-type]
    try:
        resp = client.post("/api/system/shutdown")
    finally:
        deps.set_orchestrator(None)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error_code"] == "run_active"
    assert detail["next_action"], "拦截必须给下一步动作，否则界面只能说「失败了」"
    assert no_background == [], "被拦下时不该安排任何收尾"


def test_shutdown_force_overrides_the_guard(
    client: TestClient, no_background: list[system.ShutdownPlan]
) -> None:
    """``force=true`` 才允许中断运行 —— 是用户明确的选择，不是系统替他决定。"""
    deps.set_orchestrator(object())  # type: ignore[arg-type]
    try:
        resp = client.post("/api/system/shutdown?force=true")
    finally:
        deps.set_orchestrator(None)

    assert resp.status_code == 200
    assert len(no_background) == 1
    assert no_background[0].running is True, "清单里要留下「有任务在跑」这条"


def test_shutdown_is_reachable_through_the_app_router(client: TestClient) -> None:
    """路由真的挂在 app 上（``ROUTERS`` 注册漏了的话这里就 404）。"""
    assert client.get("/api/system/status").status_code == 200


def test_module_exposes_managed_ports_for_docs() -> None:
    """端口名单是**契约**：8800 控制台 + 8899/8900 靶场，别把调试端口塞进来。"""
    assert system.MANAGED_PORTS == (8800, 8899, 8900)
    assert 9222 not in system.MANAGED_PORTS


def test_repo_root_resolution_does_not_raise_on_missing_paths() -> None:
    """路径比对用的 ``Path.resolve()`` 必须容忍不存在的路径（进程可能已退出）。"""
    assert (
        system._same_executable(str(Path("no/such/python.exe")), str(Path("no/such/python.exe")))
        is True
    )
