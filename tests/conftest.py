"""测试夹具。

两块内容：

1. **P5 夹具**：隔离的运行路径 + 一个**本地假 OpenAI 兼容端点** —— 让「[测试连接] 实测」
   这条验收口径可以零密钥、零外网地完整验证。
2. **P2/P3 夹具**：靶场可达性判定 + 复用系统浏览器的 ``page`` + 探针 / 流水线装配 ——
   感知层的验收口径（题库全量结构化读出、媒体三态、弹题不误报）只能在真浏览器里验。

靶场（P1）独立启动，本套测试**只消费它的 HTTP 接口**，解析顺序：

    1. ``AUTOLEARN_MOCK_URL``  —— 显式指定
    2. ``AUTOLEARN_MOCK_DIR``  —— 指定靶场源码目录，由本 conftest 起服务
    3. 默认地址 ``http://127.0.0.1:8899`` 已在跑 —— 直接用
    4. 都没有 —— ``pytest.skip`` 并说明如何启动（**不静默通过**）

浏览器一律复用**系统浏览器**（默认 msedge，可用 ``AUTOLEARN_BROWSER_CHANNEL`` 换
chrome），不下载自带 chromium（对齐 M6 / P9 的打包策略）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from playwright.async_api import Browser, async_playwright

from adapters.mock_exam.adapter import load_adapter
from core.config import ProbeName, RunConfig
from perception.media_probe import MediaProbe
from perception.pipeline import PerceptionPipeline
from perception.vision_probe import VisionProbe
from ui import deps, server


# --------------------------------------------------------------------------- #
# 隔离的运行路径
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ui_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把所有落盘位置挪进临时目录，测试之间互不污染，也不碰仓库。

    同时把**密钥后端强制成内存**：默认后端会走真实 keyring，而每个用例都用新 uuid
    生成 ``autolearn/<profile_id>`` 条目 —— 跑一轮测试就在用户的 Windows 凭据管理器里
    留一批垃圾，几十轮之后把凭据库写满（``CredWrite`` 报 ``WinError 8``），
    连用户自己存密钥都会失败。详见 ``core/model_registry.py::_default_backend``。
    """
    monkeypatch.setenv("AUTOLEARN_RUN_CONFIG", str(tmp_path / "run_config.json"))
    monkeypatch.setenv("AUTOLEARN_MODELS_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("AUTOLEARN_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("AUTOLEARN_DB_PATH", str(tmp_path / "autolearn.db"))
    monkeypatch.setenv("AUTOLEARN_SECRET_BACKEND", "memory")
    return tmp_path


@pytest.fixture()
def app(ui_paths: Path) -> Iterator[Any]:
    deps.reset_state()
    # 装配工厂换成内存替身：``POST /api/run/start`` 的用例不该真的拉起浏览器
    # （真浏览器路径由 scripts/run_batch.py 与真机验收覆盖）
    deps.set_run_deps_factory(_stub_run_deps)
    try:
        yield server.create_app()
    finally:
        deps.reset_state()


@pytest.fixture()
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _stub_run_deps(**kwargs: Any) -> Any:
    """给 UI 用例用的运行依赖：无浏览器、内存 SQLite、Mock 求解。"""
    from core.db import init_db
    from core.orchestrator import RunDeps
    from core.trace import RunLogger
    from tests.orchestrator_helpers import (
        FakeActuator,
        FakeAdapter,
        FakePage,
        FakePipeline,
        FakeSolver,
        FakeVerifier,
    )
    from ui.store import db_file, log_root

    run_id = str(kwargs.get("run_id", "test-run"))
    actuator = FakeActuator()
    verifier = FakeVerifier()
    return RunDeps(
        page=FakePage(),
        adapter=FakeAdapter(),
        pipeline=FakePipeline(generate=1),
        solver=FakeSolver(),
        run_logger=RunLogger(run_id, root=log_root()),
        bus=kwargs.get("bus"),
        conn=init_db(db_file()),
        actuator_factory=lambda _item: actuator,
        verifier_factory=lambda _item: verifier,
        probe_timeout_s=0.1,
    )


# --------------------------------------------------------------------------- #
# 本地假 OpenAI 兼容端点
# --------------------------------------------------------------------------- #
class FakeProviderHandler(BaseHTTPRequestHandler):
    """行为由 URL 前缀选择：``/mode/<mode>/v1/...``。

    ==============  ====================================================
    ``ok``          一切正常，且支持视觉 + 结构化输出
    ``unauthorized`` 一律 401（鉴权失败）
    ``nomodel``     ``/models`` 只列第一个模型，清单外的模型名请求 404
    ``nostruct``    带 ``response_format`` 的请求 400
    ``novision``    带图片的请求 400
    ``vision``      带图片的请求回 "7"（校验强证据用）
    ``read``        带图片的请求回一份合法的「读题」JSON
    ``advance``     带图片的请求回「找到了下一题按钮」的 JSON
    ``advance_none``带图片的请求回「画面上没有下一题按钮」的 JSON
    ==============  ====================================================
    """

    protocol_version = "HTTP/1.1"
    default_models = ("fake-model", "fake-model-2")

    def log_message(self, *args: object) -> None:  # 别污染测试输出
        return

    # -- 工具 -------------------------------------------------------------- #
    def _path(self) -> str:
        # BaseHTTPRequestHandler.path 有时是 origin-form（``/a/b``），有时是
        # absolute-form（``http://host/a/b``）—— 取决于客户端怎么发。必须规范化，
        # 否则模式解析会静默回落到默认值，把「不支持视觉」这类用例测成通过。
        return urlparse(self.path).path

    def _mode(self) -> str:
        parts = self._path().split("/")
        return parts[2] if len(parts) > 2 and parts[1] == "mode" else "ok"

    def _models(self, mode: str) -> tuple[str, ...]:
        return (self.default_models[0],) if mode == "nomodel" else self.default_models

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- 路由 -------------------------------------------------------------- #
    def do_GET(self) -> None:
        mode = self._mode()
        if self._path().endswith("/models"):
            if mode == "unauthorized":
                self._send_json(401, {"error": "invalid api key"})
                return
            self._send_json(200, {"data": [{"id": m} for m in self._models(mode)]})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        mode = self._mode()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}

        if mode == "unauthorized" or not (self.headers.get("Authorization") or "").startswith(
            "Bearer "
        ):
            self._send_json(401, {"error": "invalid api key"})
            return

        model = body.get("model")
        if model and model not in self._models(mode):
            self._send_json(404, {"error": f"model {model} not found"})
            return

        messages = body.get("messages") or [{}]
        # 图片可能在任意一条消息里（OpenAI 兼容实现普遍发 system + user 两条），
        # 所以整串扫一遍，而不是只看 messages[0]。
        contents = [
            message.get("content") for message in messages if isinstance(message, dict)
        ]
        has_image = any(isinstance(content, list) for content in contents)

        if mode == "nostruct" and "response_format" in body:
            self._send_json(400, {"error": "response_format json_schema is not supported"})
            return
        if mode == "novision" and has_image:
            self._send_json(400, {"error": "image input is not supported by this model"})
            return
        if mode == "read" and has_image:
            # 「读图」契约的端点：照 ``prompts/10-视觉组.md`` 的固定格式回一份合法结果
            # （题目 JSON + 页面观测 JSON，两者一起回）。
            payload = {
                "page": {
                    "progress": "3/10",
                    "total": 10,
                    "current": 3,
                    "next_control": {"box": [0.82, 0.93, 0.14, 0.05], "label": "下一题"},
                    "submit": {"box": [0.89, 0.01, 0.11, 0.03], "scope": "question"},
                    "completed": "not_done",
                    "scrolling": True,
                    "reason": "端点替身",
                },
                "questions": [
                    {
                        "stem": "设 f(x) 为随机变量的概率密度，则其必满足的性质是",
                        "qtype": "single",
                        "options": [
                            {"label": "A", "text": "单调不减函数", "box": [0.05, 0.36, 0.56, 0.03]},
                            {"label": "B", "text": "连续函数", "box": [0.05, 0.41, 0.56, 0.03]},
                            {"label": "C", "text": "非负函数", "box": [0.05, 0.47, 0.56, 0.03]},
                            {"label": "D", "text": "lim f(x) = 1", "box": [0.05, 0.52, 0.56, 0.03]},
                        ],
                    }
                ],
                "more_below": False,
                "note": "端点替身",
            }
            self._send_json(200, {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})
            return
        if mode == "end_screen" and has_image:
            # 收尾那一屏：一道完整题目都没有，但页面观测说「整卷做完了」。
            # 这是 ``parse_read_batch`` 必须放行的一屏（否则收尾闸门永远拿不到观测）。
            payload = {
                "page": {
                    "progress": "10/10",
                    "total": 10,
                    "current": 10,
                    "submit": {"box": [0.42, 0.55, 0.16, 0.05], "scope": "paper"},
                    "completed": "all_done",
                    "reason": "整卷已完成",
                },
                "questions": [],
                "more_below": False,
                "note": "收尾屏",
            }
            self._send_json(200, {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})
            return
        if mode == "vision" and has_image:
            # 「看得见」的端点：把探针图里的数字照实答出来。
            # 用来区分**强证据**（模型真读到了图）与**弱证据**（只是收下了图）。
            self._send_json(200, {"choices": [{"message": {"content": "7"}}]})
            return

        self._send_json(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})


@pytest.fixture(scope="session")
def provider_origin() -> Iterator[str]:
    """假端点根地址。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeProviderHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def provider(provider_origin: str) -> Callable[[str], str]:
    """``provider("ok")`` → 可直接当 ``base_url`` 用的地址。"""

    def build(mode: str = "ok") -> str:
        return f"{provider_origin}/mode/{mode}/v1"

    return build


# --------------------------------------------------------------------------- #
# P2/P3 夹具：靶场可达性 + 系统浏览器
# --------------------------------------------------------------------------- #
DEFAULT_MAIN = "http://127.0.0.1:8899"
DEFAULT_FRAME = "http://127.0.0.1:8900"
BROWSER_CHANNEL = os.environ.get("AUTOLEARN_BROWSER_CHANNEL", "msedge")


def _reachable(url: str, timeout: float = 0.8) -> bool:
    """靶场可达性探测。**必须绕开环境代理**。

    开发机上常有 ``HTTP_PROXY``/``HTTPS_PROXY``（企业网络、抓包、沙箱都会设），
    而 ``urlopen`` 默认会把 ``127.0.0.1`` 的请求也交给代理 —— 代理答不上来
    （502 或超时），于是 ``mock_base`` 判定靶场不可达，**整批真浏览器用例
    静默 skip**，``pytest`` 依然全绿。这正是本仓库反复强调的
    「全绿不等于跑过」。所以这里显式用空 ProxyHandler 建 opener。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


@pytest.fixture(scope="session")
def mock_base() -> str:
    """靶场主站地址。"""
    explicit = os.environ.get("AUTOLEARN_MOCK_URL")
    if explicit:
        return explicit.rstrip("/")
    if _reachable(f"{DEFAULT_MAIN}/quiz.html"):
        return DEFAULT_MAIN

    source_dir = os.environ.get("AUTOLEARN_MOCK_DIR")
    if source_dir:
        script = Path(source_dir) / "scripts" / "serve_mock.py"
        if script.exists() and not _port_open("127.0.0.1", 8899):
            subprocess.Popen(  # 靶场是本仓库自带脚本，路径已校验
                [sys.executable, str(script)],
                cwd=str(source_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(40):
                if _reachable(f"{DEFAULT_MAIN}/quiz.html", timeout=0.5):
                    break
                time.sleep(0.25)
            if _reachable(f"{DEFAULT_MAIN}/quiz.html"):
                return DEFAULT_MAIN

    pytest.skip(
        "靶场不可达。请先启动 P1 靶场：\n"
        "    cd <项目根> && python scripts/serve_mock.py\n"
        "或用 AUTOLEARN_MOCK_URL / AUTOLEARN_MOCK_DIR 指定。"
    )


@pytest.fixture(scope="session")
def frame_origin(mock_base: str) -> str:
    """跨域 iframe 的内嵌源地址。"""
    explicit = os.environ.get("AUTOLEARN_MOCK_FRAME_URL")
    if explicit:
        return explicit.rstrip("/")
    if mock_base == DEFAULT_MAIN:
        return DEFAULT_FRAME
    return mock_base.replace(":8899", ":8900")


# URL 构造与题库读取统一放在 tests/helpers.py，避免两处漂移。
from tests.helpers import course_url, quiz_url  # noqa: E402,F401  (re-export 供测试使用)


@pytest_asyncio.fixture(scope="session")
async def browser() -> Browser:
    """系统浏览器实例（session 级，复用一次启动）。"""
    playwright = await async_playwright().start()
    instance = await playwright.chromium.launch(
        channel=BROWSER_CHANNEL,
        headless=True,
        args=[
            # M3-3：媒体动作要靠可信手势，这条启动参数让自动播放不被策略拦下
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
            "--disable-features=CalculateNativeWinOcclusion",
        ],
    )
    try:
        yield instance
    finally:
        await instance.close()
        await playwright.stop()


@pytest_asyncio.fixture
async def page(browser: Browser):
    """每个用例一个干净上下文（视口 1280×720，与截图上界无关）。"""
    context = await browser.new_context(viewport={"width": 1280, "height": 720})
    try:
        yield await context.new_page()
    finally:
        await context.close()


@pytest.fixture(scope="session")
def adapter():
    """靶场适配器（YAML 驱动，探针与脚本共用）。"""
    return load_adapter()


@pytest.fixture
def run_config() -> RunConfig:
    return RunConfig()


@pytest.fixture
def probes() -> dict[ProbeName, object]:
    """v0.2.0：题目链只剩视觉一条，媒体探针独立调度。"""
    return {
        ProbeName.VISION: VisionProbe(),
        ProbeName.MEDIA: MediaProbe(),
    }


@pytest.fixture
def pipeline(probes, run_config) -> PerceptionPipeline:
    return PerceptionPipeline([probes[ProbeName.VISION]], run_config)


# --------------------------------------------------------------------------- #
# 靶场地面真值（**仅测试判分用**，生产通道不得读）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def questions(mock_base: str) -> list[dict]:
    """靶场题库原始数据（含 expectations 用的 traps / flags / stem）。"""
    from tests.helpers import fetch_questions

    return fetch_questions(mock_base)


@pytest.fixture(scope="session")
def course_meta(mock_base: str) -> dict:
    from tests.helpers import fetch_course

    return fetch_course(mock_base)


@pytest.fixture(scope="session")
def question_by_index(questions: list[dict]):
    by_index = {item["index"]: item for item in questions}
    return lambda index: by_index[index]
