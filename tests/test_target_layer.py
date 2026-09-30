"""目标采集层（P11）单测。

**全部是纯内存用例**：不起靶场、不起浏览器、不碰真机。

这是刻意的 —— 本仓库有条踩过的坑：靶场不起时真浏览器用例会 ``skip``，
于是 ``pytest`` 全绿**并不等于**这些用例跑过。而 P11 恰恰是「接住真实目标」
这一层，最容易写出「只在真机上验一次、以后每次回归都被跳过」的假绿。
所以这里用假 source / 假 page 把**接口契约**钉死：

- 读题通道恒为视觉一条（v0.2.0 起不再随目标类型变化）；
- ``_page_session`` 的**优先顺序**（有目标就附加，没目标才退回靶场兜底）；
- 端点不通时的报错文案含不含可操作信息。

真机路径（真的附加到 Chrome 标签页）由 ``scripts/`` 下的验收工具覆盖。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import RunConfig
from core.enums import ProbeName, TargetKind
from core.orchestrator import Orchestrator, RunContext, RunDeps
from core.targets import channels_for
from target.base import (
    ScreenSurface,
    TargetHandle,
    TargetInfo,
    TargetSource,
    TargetUnavailableError,
)
from target.browsers import (
    DEFAULT_DIR_BLOCKED_MAJOR,
    BrowserTargetSource,
    browser_major_version,
    find_browser_executable,
    managed_user_data_dir,
    system_user_data_dir,
)
from target.mock_source import MOCK_TARGET_ID, MockTargetSource
from target.windows import DesktopWindowSource
from ui import deps

#: 一个**确定没有任何东西在监听**的端口。1 号端口需要 root 权限才能绑定，
#: 所以「连不上」是确定的，不会因为本机恰好跑着什么而变成 flaky。
DEAD_PORT = 1

#: ``_FakeSource`` 的「没给 page」哨兵 —— 与「显式给了 None」区分开。
_NO_PAGE: Any = object()


def _register_model(client: TestClient) -> None:
    """塞一套模型配置进模型库。

    v0.2.0 起页面只能由模型读，**没有模型连启动都过不去**（400 + ``no_config``）。
    所以凡是要验「启动被别的理由拦下」的用例，都得先把模型这一关过了，
    否则测到的是模型缺失，而不是它声称要测的那条护栏。
    """
    response = client.post(
        "/api/models",
        json={
            "name": "假厂商",
            "base_url": "https://invalid.example.com/v1",
            "model": "fake-vlm",
        },
    )
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# 读题通道：v0.2.0 起只有视觉一条，与目标类型无关
# --------------------------------------------------------------------------- #
def test_every_target_kind_reads_by_vision_alone() -> None:
    """浏览器页与原生窗口走的是**同一条**视觉通道。

    这是 v0.2.0 的核心事实：程序只使用模型读页面，所以「目标类型 → 可用通道」
    这张能力矩阵没有存在意义了 —— 它当年存在的唯一理由，是让用户
    在「DOM 优先 / 模型优先」之间做选择时不至于配出必然失败的组合。
    """
    for kind in TargetKind:
        assert channels_for(kind) == frozenset({ProbeName.VISION})


def test_run_config_has_no_channel_convergence() -> None:
    """换目标类型不再需要「收敛通道模式」—— 已经没有可收敛的东西。

    回归守卫：``probe_mode`` 一旦被加回来，说明有人把双通道选择又引入了。
    """
    cfg = RunConfig(target_kind=TargetKind.DESKTOP_WINDOW)
    assert not hasattr(cfg, "probe_mode")
    assert not hasattr(cfg, "probe_order")


def test_run_config_defaults_to_browser_page_without_target() -> None:
    cfg = RunConfig()
    assert cfg.target_kind is TargetKind.BROWSER_PAGE
    assert cfg.target_id is None
    assert cfg.browser_debug_port == 9222


# --------------------------------------------------------------------------- #
# TargetInfo：过 HTTP 的那份数据
# --------------------------------------------------------------------------- #
def test_target_info_serialises_vision_channel() -> None:
    """UI 直接消费这份 JSON，字段名不能悄悄改。"""
    payload = TargetInfo(
        target_id="t1", kind=TargetKind.BROWSER_PAGE, title="第 3 题", url="https://e/x"
    ).model_dump(mode="json")
    assert payload["channels"] == ["vision"]
    assert "modes" not in payload, "通道模式已随双通道选择一起删除"


def test_desktop_target_info_also_vision_only() -> None:
    payload = TargetInfo(target_id="w1", kind=TargetKind.DESKTOP_WINDOW).model_dump(mode="json")
    assert payload["channels"] == ["vision"]
    assert "modes" not in payload


async def test_target_handle_reports_supported_channels() -> None:
    handle = TargetHandle(info=TargetInfo(target_id="t1", kind=TargetKind.BROWSER_PAGE))
    assert handle.supports(ProbeName.VISION)
    assert not handle.supports(ProbeName.MEDIA)  # 媒体是读口，不在通道集合里


async def test_target_handle_aclose_is_idempotent() -> None:
    calls: list[int] = []

    async def cleanup() -> None:
        calls.append(1)

    handle = TargetHandle(
        info=TargetInfo(target_id="t1", kind=TargetKind.BROWSER_PAGE), cleanup=cleanup
    )
    await handle.aclose()
    await handle.aclose()
    assert calls == [1]


# --------------------------------------------------------------------------- #
# BrowserTargetSource：端点不通时的行为
# --------------------------------------------------------------------------- #
def _dead_source(*, channel: str = "chrome", **kwargs: Any) -> BrowserTargetSource:
    return BrowserTargetSource(port=DEAD_PORT, channel=channel, **kwargs)


async def test_list_targets_returns_empty_when_endpoint_absent() -> None:
    """抓取是高频动作：端点不通时回空表，不是抛异常。

    抛异常会让 UI 只能显示一个错误；回空表 + ``hint`` 才能告诉用户
    「你手上那个浏览器附加不上去，点这里接管启动」。
    """
    assert await _dead_source().list_targets() == []


async def test_endpoint_is_not_alive_for_dead_port() -> None:
    assert not await _dead_source().is_endpoint_alive()


async def test_cdp_probe_ignores_ambient_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归守卫：回环地址**不该走 HTTP_PROXY**。

    开发机上设了 ``HTTP_PROXY`` 时，httpx 默认会把 CDP 探测交给代理，
    而代理不认识 ``127.0.0.1`` 上的调试端点 —— 表现是「浏览器明明起着、
    端口明明开着，探测却超时」，极难排查（本机就复现过）。

    这里把代理指到一个必然连不上的地址：实现里一旦漏掉 ``trust_env=False``，
    探测就会失败，用例立刻红。
    """

    class _CdpStub(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CdpStub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    try:
        source = BrowserTargetSource(port=int(httpd.server_address[1]), channel="chrome")
        assert await source.is_endpoint_alive() is True
    finally:
        httpd.shutdown()
        httpd.server_close()


async def test_ensure_ready_raises_actionable_error() -> None:
    with pytest.raises(TargetUnavailableError) as excinfo:
        await _dead_source().ensure_ready()
    message = str(excinfo.value)
    # 报错必须说清「为什么」和「怎么办」，否则用户只会以为软件坏了
    assert "调试端口" in message
    assert "接管启动" in message


async def test_open_raises_when_endpoint_absent() -> None:
    with pytest.raises(TargetUnavailableError):
        async with _dead_source().open("t1"):
            pass  # pragma: no cover - 不该进到这里


def test_launch_rejects_unknown_channel() -> None:
    with pytest.raises(TargetUnavailableError) as excinfo:
        _dead_source(channel="netscape").launch()
    assert "可执行文件" in str(excinfo.value)


def test_browser_lookup_handles_unknown_channel() -> None:
    assert find_browser_executable("netscape") is None
    assert system_user_data_dir("netscape") is None


def test_browser_lookup_returns_paths_for_known_channels() -> None:
    """本机装了什么就返回什么，没装则为 None —— 不做假设，只验路径拼得出来。"""
    for channel in ("chrome", "msedge"):
        exe = find_browser_executable(channel)
        assert exe is None or exe.is_file()
        # 系统默认目录允许不存在（没装过该浏览器），但必须是个绝对路径
        user_dir = system_user_data_dir(channel)
        assert user_dir is None or user_dir.is_absolute()


# --------------------------------------------------------------------------- #
# 自管 Profile：本模块最重要的一条回归守卫
# --------------------------------------------------------------------------- #
def test_browser_source_never_uses_system_default_profile() -> None:
    """**回归守卫**：带调试端口启动时绝不能用系统的默认 Profile 目录。

    从 Chrome 136 起，为**默认**数据目录开 ``--remote-debugging-port`` 会被
    **静默忽略**（不报错，端口就是不开）。这条一旦被改回去，
    表现是「浏览器起来了、窗口也开了，但抓取永远是 0 个标签页」，
    排查方向会完全跑偏 —— 所以必须有一条用例把它钉死。
    """
    source = BrowserTargetSource(channel="chrome")
    system_dir = system_user_data_dir("chrome")
    assert system_dir is not None
    assert source.user_data_dir != system_dir
    assert "browser_profile" in str(source.user_data_dir)


def test_managed_profile_dir_is_per_channel() -> None:
    """Chrome 与 Edge 不能共用同一个数据目录 —— 跨浏览器共用会互相锁库。"""
    assert managed_user_data_dir("chrome") != managed_user_data_dir("msedge")
    assert managed_user_data_dir("chrome").name == "chrome"


def test_browser_source_resolves_user_data_dir_to_absolute() -> None:
    """**回归守卫**：数据目录必须是绝对路径。

    Chrome 对相对的 ``--user-data-dir`` 会**立刻退出**（退出码 21，且 stderr
    一个字节都不吐），表现与「浏览器没装」几乎一样。本项目实测踩到过：
    自管根目录写成 ``state/...`` 时产品路径必然失败，而用 ``tempfile`` 的
    自检脚本（绝对路径）却是绿的 —— 典型的「测试绿、产品挂」。
    """
    source = BrowserTargetSource(channel="chrome")
    assert source.user_data_dir.is_absolute()

    explicit = BrowserTargetSource(channel="chrome", user_data_dir="state/somewhere")
    assert explicit.user_data_dir.is_absolute()


def test_launch_args_include_non_default_user_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动参数里必须同时有调试端口与**非默认**数据目录，缺一不可。

    用假的 ``Popen`` 拦住，只检查拼出来的命令行 —— 不真的起进程。
    """
    if find_browser_executable("chrome") is None:
        pytest.skip("本机没有 Chrome，跳过命令行拼装检查")
    captured: dict[str, list[str]] = {}

    class _FakePopen:
        def __init__(self, args: list[str]) -> None:
            captured["args"] = args

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:  # pragma: no cover
            return

        def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
            return 0

    monkeypatch.setattr("target.browsers.subprocess.Popen", _FakePopen)
    source = BrowserTargetSource(channel="chrome", port=9999)
    source.launch(start_urls=["http://example.test/landing"])

    args = captured["args"]
    assert "--remote-debugging-port=9999" in args
    user_data = [a for a in args if a.startswith("--user-data-dir=")]
    assert len(user_data) == 1
    assert str(source.user_data_dir) in user_data[0]
    # 起始页要以位置参数传给浏览器 —— 这是「保证标签页能识别」的前半段
    assert "http://example.test/landing" in args


def test_default_dir_blocked_threshold_matches_chrome_policy() -> None:
    """阈值本身也是契约：Chrome 从 136 起拒绝为默认目录开调试端口。"""
    assert DEFAULT_DIR_BLOCKED_MAJOR == 136


def test_browser_major_version_reads_none_for_unknown_channel() -> None:
    assert browser_major_version("netscape") is None


def test_browser_major_version_is_plausible_for_chrome() -> None:
    version = browser_major_version("chrome")
    if find_browser_executable("chrome") is None:
        pytest.skip("本机没有 Chrome")
    assert version is not None and version > 50


# --------------------------------------------------------------------------- #
# 桌面窗口目标（P11 桌面侧）
# --------------------------------------------------------------------------- #
def test_desktop_source_kind_is_desktop_window() -> None:
    assert DesktopWindowSource().kind is TargetKind.DESKTOP_WINDOW


def test_desktop_source_excludes_own_process() -> None:
    """枚举结果里不能出现自己 —— 把控制台当成「待抓应用」纯属自找麻烦。"""
    for info in asyncio.run(DesktopWindowSource().list_targets()):
        assert info.app != Path(sys.executable).name
        assert info.target_id.startswith("win:")


def test_desktop_targets_only_advertise_vision() -> None:
    info = TargetInfo(target_id="win:1", kind=TargetKind.DESKTOP_WINDOW, title="某客户端")
    payload = info.model_dump(mode="json")
    assert payload["channels"] == ["vision"]


def test_screen_surface_maps_image_to_screen() -> None:
    """坐标换算是桌面点击最容易错的地方，单独钉一条。

    ``screen = origin + image * scale``。100% 缩放下 scale=1；
    位图与窗口尺寸不同时 scale ≠ 1（用 1.5 这种能整除的值，
    避免把 Python 的「四舍六入五成双」也算进断言里）。
    """
    surface = ScreenSurface(capture=_never_called, click=_never_called_click)
    surface.origin = (100, 50)
    assert surface.to_screen(10, 20) == (110, 70)

    surface.scale = 1.5
    assert surface.to_screen(10, 20) == (115, 80)


async def _never_called() -> bytes:  # pragma: no cover - 只作占位
    raise AssertionError("不该被调用")


async def _never_called_click(x: float, y: float) -> None:  # pragma: no cover
    raise AssertionError("不该被调用")


async def test_desktop_open_rejects_closed_window() -> None:
    source = DesktopWindowSource()
    with pytest.raises(TargetUnavailableError):
        async with source.open("win:1"):
            pass  # pragma: no cover - 不该进到这里


async def test_desktop_open_rejects_foreign_id_format() -> None:
    """浏览器目标的 target id 不能被当成窗口句柄 —— 换目标类型时必须清掉旧 ID。"""
    source = DesktopWindowSource()
    with pytest.raises(TargetUnavailableError):
        async with source.open("3F4A2B1C"):
            pass  # pragma: no cover


# --------------------------------------------------------------------------- #
# MockTargetSource：靶场降级为测试专用
# --------------------------------------------------------------------------- #
async def test_mock_source_lists_single_fixed_target() -> None:
    source = MockTargetSource(url="http://127.0.0.1:8899/quiz.html")
    targets = await source.list_targets()
    assert [t.target_id for t in targets] == [MOCK_TARGET_ID]
    assert targets[0].kind is TargetKind.BROWSER_PAGE


def test_mock_source_kind_is_browser_page() -> None:
    """靶场本身就是一个网页，按浏览器页算 —— 不能因为「是测试用的」就另开一套。"""
    assert MockTargetSource(url="http://x/y").kind is TargetKind.BROWSER_PAGE


# --------------------------------------------------------------------------- #
# 编排层接缝：_page_session 的优先顺序
# --------------------------------------------------------------------------- #
class _FakeSource(TargetSource):
    """假采集源。记录被附加的目标 ID，并交出一个占位 page 对象。

    ``page`` 用哨兵值而不是 ``None`` 兜底 —— 桌面窗口场景要的正是
    「句柄没有页面」这个状态，用 ``None`` 当默认值会让那条用例永远绿得没意义。
    """

    kind = TargetKind.BROWSER_PAGE

    def __init__(
        self, *, page: Any = _NO_PAGE, kind: TargetKind = TargetKind.BROWSER_PAGE
    ) -> None:
        self.kind = kind
        self.page = object() if page is _NO_PAGE else page
        self.opened: list[str] = []

    async def list_targets(self) -> list[TargetInfo]:
        return []

    @asynccontextmanager
    async def open(self, target_id: str) -> AsyncIterator[TargetHandle]:
        self.opened.append(target_id)
        yield TargetHandle(
            info=TargetInfo(target_id=target_id, kind=self.kind), page=self.page
        )


def _orchestrator(deps_kwargs: dict[str, Any], *, cfg: RunConfig | None = None) -> Orchestrator:
    cfg = cfg if cfg is not None else RunConfig(target_id="t1")
    ctx = RunContext(run_id="r1", cfg=cfg, started_at=datetime.now(UTC))
    return Orchestrator(ctx, deps=RunDeps(**deps_kwargs))


async def test_page_session_attaches_to_selected_target() -> None:
    """P11 的核心接缝：给了目标就**附加**，而不是自己起浏览器。

    改造前这里只有自启靶场一条路 —— 也就是「不管用户想抓什么，我们
    都自己开一个浏览器去靶场」，正是被指出的那个偏差。
    """
    source = _FakeSource()
    orchestrator = _orchestrator({"target_source": source, "target_id": "t1"})

    async with orchestrator._page_session() as page:
        assert page is source.page
        assert orchestrator._target.info.target_id == "t1"

    assert source.opened == ["t1"]


async def test_page_session_rejects_target_without_page_handle() -> None:
    """桌面窗口交付的是 ``ScreenSurface`` 而不是 ``Page`` —— 读题链路接不上时必须**明说**。"""
    source = _FakeSource(page=None, kind=TargetKind.DESKTOP_WINDOW)
    orchestrator = _orchestrator({"target_source": source, "target_id": "w1"})

    with pytest.raises(NotImplementedError):
        async with orchestrator._page_session():
            pass  # pragma: no cover - 不该进到这里


async def test_page_session_falls_back_to_start_url_without_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没选目标时退回自启靶场 —— scripts/ 的验收工具与既有测试都靠这条路。"""
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_factory(cfg: RunConfig, *, channel: str, url: str) -> AsyncIterator[Any]:
        seen.update(channel=channel, url=url)
        yield "fallback-page"

    monkeypatch.setattr("core.orchestrator.default_page_factory", fake_factory)

    orchestrator = _orchestrator({}, cfg=RunConfig())
    assert orchestrator.deps.target_source is None
    async with orchestrator._page_session() as page:
        assert page == "fallback-page"
    assert seen["url"].endswith("/quiz.html")


async def test_page_session_ignores_source_without_target_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """source 有、target_id 空 → 仍走兜底。两者缺一都不该附加。

    半截状态（source 在但没选目标）如果被判成「附加模式」，会以一个空 ID
    去附加，报出来的错离真正原因十万八千里。
    """
    source = _FakeSource()

    @asynccontextmanager
    async def fake_factory(cfg: RunConfig, *, channel: str, url: str) -> AsyncIterator[Any]:
        yield "fallback-page"

    monkeypatch.setattr("core.orchestrator.default_page_factory", fake_factory)

    orchestrator = _orchestrator({"target_source": source}, cfg=RunConfig())
    async with orchestrator._page_session() as page:
        assert page == "fallback-page"
    assert source.opened == []


# --------------------------------------------------------------------------- #
# 装配与路由
# --------------------------------------------------------------------------- #
def test_build_target_source_returns_none_without_target() -> None:
    from ui.assembly import build_target_source

    assert build_target_source(RunConfig()) is None


def test_build_target_source_returns_browser_source() -> None:
    from ui.assembly import build_target_source

    source = build_target_source(RunConfig(target_id="t1", browser_channel="msedge"))
    assert isinstance(source, BrowserTargetSource)
    assert source.channel == "msedge"
    assert source.port == 9222


def test_build_target_source_returns_desktop_source_for_windows() -> None:
    """桌面目标现在有真正的采集实现（枚举 / 截图 / 系统级点击）。"""
    from ui.assembly import build_target_source

    source = build_target_source(RunConfig(target_kind=TargetKind.DESKTOP_WINDOW, target_id="win:1"))
    assert isinstance(source, DesktopWindowSource)
    assert source.kind is TargetKind.DESKTOP_WINDOW


def test_start_run_blocks_desktop_target_until_vision_read_lands(
    client: TestClient, ui_paths: Path
) -> None:
    """窗口**采集**可用，但「截图→模型→题干」这条识别链路还没接。

    这时必须在启动前拦下并**说清楚缺什么** —— 给一个跑不动的 run
    比明确拒绝更糟：用户会以为是自己选错了目标。

    v0.2.0：判据不变（原生窗口没有 Playwright ``Page``，读题链路接不上），
    但错误码从已删除的 ``target_channel_unsupported`` 换成 ``target_unavailable``。
    """
    _register_model(client)
    deps.set_run_config(RunConfig(target_kind=TargetKind.DESKTOP_WINDOW, target_id="win:1"))

    resp = client.post("/api/run/start")

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error_code"] == "target_unavailable"
    assert "视觉识别" in detail["message"]


def test_put_config_clears_target_when_kind_changes(
    client: TestClient, ui_paths: Path
) -> None:
    """**回归守卫**：换目标类型必须清掉旧 target_id。

    CDP 的 target id 与窗口句柄互不通用。不清的话，用户从网页切到应用程序后
    启动，会拿着一个标签页 ID 去当窗口句柄，报出来的错离真正原因十万八千里。
    """
    client.put("/api/run/config", json={"target_kind": "browser_page", "target_id": "abc123"})
    assert client.get("/api/run/config").json()["target_id"] == "abc123"

    resp = client.put("/api/run/config", json={"target_kind": "desktop_window"})

    assert resp.status_code == 200
    assert resp.json()["target_id"] is None


def test_put_config_keeps_target_when_kind_unchanged(
    client: TestClient, ui_paths: Path
) -> None:
    """同类型下重设配置不能顺手把目标清掉 —— 那会让用户每改一次参数就重选目标。"""
    client.put("/api/run/config", json={"target_kind": "browser_page", "target_id": "abc123"})

    resp = client.put("/api/run/config", json={"target_kind": "browser_page", "sample_n": 7})

    assert resp.status_code == 200
    assert resp.json()["target_id"] == "abc123"


def test_targets_route_scans_desktop_windows(client: TestClient, ui_paths: Path) -> None:
    """``?kind=desktop_window`` 走窗口枚举，且**不报错**（扫不到也要给提示）。"""
    resp = client.get("/api/targets", params={"kind": "desktop_window"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["kind"] == "desktop_window"
    # 桌面目标没有「端点」这个概念，不能填一个假地址误导排障
    assert payload["endpoint"] is None
    for target in payload["targets"]:
        assert target["target_id"].startswith("win:")
        assert target["channels"] == ["vision"]


def test_targets_launch_rejects_desktop_kind(client: TestClient, ui_paths: Path) -> None:
    """「接管启动」只对浏览器有意义 —— 原生程序是用户自己开的。"""
    deps.set_run_config(RunConfig(target_kind=TargetKind.DESKTOP_WINDOW))

    resp = client.post("/api/targets/launch")

    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "target_unavailable"


def test_targets_launch_returns_targets_without_revalidation_crash(
    client: TestClient, ui_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/targets/launch`` 成功路径必须能回显目标列表。

    回归 2026-09-29 的 500：``TargetLaunchOut(**snapshot.model_dump())`` 把
    ``TargetInfo.channels``（``computed_field``）当字典再喂回去，撞上
    ``extra="forbid"`` 直接 ``targets.0.channels extra_forbidden``。
    修法是**直接复用原对象**，不做 dump→校验 的来回。
    """

    class _FakeBrowserSource:
        kind = TargetKind.BROWSER_PAGE
        endpoint = "http://127.0.0.1:9222"
        port = 9222
        channel = "chrome"

        async def is_endpoint_alive(self) -> bool:
            return True  # 已是活的，跳过真实 launch

        async def list_targets(self) -> list[TargetInfo]:
            return [
                TargetInfo(
                    target_id="tab-1", kind=TargetKind.BROWSER_PAGE,
                    title="要抓的页面", url="https://example.test/homework",
                )
            ]

    monkeypatch.setattr("ui.routes.targets.make_browser_source", lambda cfg: _FakeBrowserSource())
    deps.set_run_config(RunConfig(target_kind=TargetKind.BROWSER_PAGE))

    resp = client.post("/api/targets/launch")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["launched"] is False  # 端点已活，无需重新拉起
    assert [t["target_id"] for t in payload["targets"]] == ["tab-1"]
    assert payload["targets"][0]["channels"] == ["vision"], "computed_field 正常序列化回显"


def test_targets_route_reports_hint_when_endpoint_absent(
    client: TestClient, ui_paths: Path
) -> None:
    deps.set_run_config(RunConfig(browser_debug_port=DEAD_PORT))

    resp = client.get("/api/targets")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["alive"] is False
    assert payload["targets"] == []
    assert payload["endpoint"].endswith(str(DEAD_PORT))
    # 空列表必须配一句「怎么办」，否则用户只能干看着
    assert payload["hint"] and "接管启动" in payload["hint"]


def test_run_config_exposes_target_but_no_channel_modes(
    client: TestClient, ui_paths: Path
) -> None:
    """出参里不该再有「可选的通道模式」—— 只有一条通道，没得选。"""
    config = client.get("/api/run/config").json()
    assert config["target_kind"] == "browser_page"
    assert config["target_id"] is None
    assert "available_modes" not in config
    assert "probe_mode" not in config
    assert "probe_order" not in config


def test_put_run_config_accepts_target_id(client: TestClient, ui_paths: Path) -> None:
    resp = client.put("/api/run/config", json={"target_id": "abc123"})
    assert resp.status_code == 200
    assert resp.json()["target_id"] == "abc123"


def test_start_run_blocks_when_target_endpoint_absent(
    client: TestClient, ui_paths: Path
) -> None:
    """选好了目标但浏览器没带调试端口 → **启动前**拦下。

    否则会返回一个 run_id 然后在后台默默失败，用户只看到一个跑不动的运行。
    """
    _register_model(client)
    deps.set_run_config(RunConfig(target_id="abc123", browser_debug_port=DEAD_PORT))

    resp = client.post("/api/run/start")

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error_code"] == "browser_no_debug_port"
    assert "接管启动" in detail["message"]


def test_start_run_without_target_still_works(client: TestClient, ui_paths: Path) -> None:
    """没有目标时照旧能启动（靶场兜底），不能被新护栏误伤。"""
    _register_model(client)
    resp = client.post("/api/run/start")
    assert resp.status_code == 200
    assert resp.json()["run_id"]


def test_start_run_without_any_model_is_refused(client: TestClient, ui_paths: Path) -> None:
    """**v0.2.0 的行为变化**：一套模型都没有时直接拒绝启动。

    页面只能由模型读，没有模型就完全跑不动。旧行为是「退回 Mock 空跑」——
    那会返回一个 run_id，然后在后台一路上报「读不到题」，
    用户看到的是「跑起来了但什么都没干」，比明确拒绝更难排查。
    """
    resp = client.post("/api/run/start")

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error_code"] == "no_config"
    assert "模型" in detail["message"]
