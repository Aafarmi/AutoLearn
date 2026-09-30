"""P5 SSE 验收。

P5 验收口径里的「SSE 三项处理齐全」：

1. ``X-Accel-Buffering: no``
2. 每 15s 注释行心跳
3. 重连后拉 ``/api/run/progress`` 对账

第 1、2 条只能靠**真连接**验：假 ASGI 传输会把响应体缓冲起来，永远测不到
「事件是不是实时到的」。所以这里起一个真 uvicorn，用流式客户端读。

事件投递跨线程：``EventBus.emit()`` 是线程安全的 —— 测试线程直接调
``emit()``，总线内部会切回服务端事件循环投递。这正好顺带验证了「后台线程
发日志」这条生产路径。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from core.db import init_db
from core.events import Event
from ui import deps
from ui.routes import events as events_route

TIMEOUT = 15.0


@pytest.fixture()
def live_server(app: Any) -> Iterator[str]:
    """在空闲端口上跑真 uvicorn，返回 ``base_url``。"""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    if not server.started:  # pragma: no cover - 环境异常
        raise RuntimeError("uvicorn 没起来")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _emit(event: str, payload: dict[str, Any]) -> None:
    """从测试线程投递事件。``EventBus`` 线程安全，会自行切回服务端循环。"""
    deps.get_event_bus().emit(event, payload)


# --------------------------------------------------------------------------- #
# 响应头与首帧
# --------------------------------------------------------------------------- #
def test_sse_headers_and_greeting(live_server: str) -> None:
    with (
        httpx.Client(timeout=TIMEOUT) as client,
        client.stream("GET", f"{live_server}/api/events") as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # 反向代理会缓冲，不加这个头事件就不实时
        assert response.headers["x-accel-buffering"] == "no"
        assert "no-cache" in response.headers["cache-control"]

        lines = response.iter_lines()
        assert next(lines) == ": connected"


# --------------------------------------------------------------------------- #
# 事件投递
# --------------------------------------------------------------------------- #
def test_sse_delivers_events_in_wire_format(live_server: str) -> None:
    with (
        httpx.Client(timeout=TIMEOUT) as client,
        client.stream("GET", f"{live_server}/api/events") as response,
    ):
        lines = response.iter_lines()
        assert next(lines) == ": connected"
        assert next(lines) == ""

        _emit(Event.RUN_STARTED, {"run_id": "r-test"})
        assert next(lines) == "event: run.started"

        data_line = next(lines)
        assert data_line.startswith("data: ")
        assert json.loads(data_line[len("data: ") :]) == {"run_id": "r-test"}
        assert next(lines) == ""  # 事件之间以空行分隔


def test_sse_preserves_order_and_unicode(live_server: str) -> None:
    with (
        httpx.Client(timeout=TIMEOUT) as client,
        client.stream("GET", f"{live_server}/api/events") as response,
    ):
        lines = response.iter_lines()
        next(lines)
        next(lines)

        _emit(Event.LOG_LINE, {"line": "第 1 题 · 已提交"})
        _emit(Event.LOG_LINE, {"line": "第 2 题 · 已提交"})

        assert next(lines) == "event: log.line"
        payload = next(lines)
        assert "第 1 题" in payload, "中文必须原样透出，不能被转义成 \\uXXXX"

        assert next(lines) == ""
        assert next(lines) == "event: log.line"
        assert "第 2 题" in next(lines)


def test_sse_delivers_unknown_event_names(live_server: str) -> None:
    """事件名拼错只记警告，不能把订阅者踢掉。"""
    with (
        httpx.Client(timeout=TIMEOUT) as client,
        client.stream("GET", f"{live_server}/api/events") as response,
    ):
        lines = response.iter_lines()
        next(lines)
        next(lines)

        _emit("totally.made.up", {"x": 1})
        assert next(lines) == "event: totally.made.up"


# --------------------------------------------------------------------------- #
# 心跳
# --------------------------------------------------------------------------- #
def test_sse_sends_heartbeat_when_idle(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空闲超过心跳间隔必须发注释行，否则中间层会掐掉连接。"""
    monkeypatch.setattr(events_route, "HEARTBEAT_S", 0.3)

    with (
        httpx.Client(timeout=TIMEOUT) as client,
        client.stream("GET", f"{live_server}/api/events") as response,
    ):
        lines = response.iter_lines()
        assert next(lines) == ": connected"
        assert next(lines) == ""
        # 没有任何事件，只靠心跳
        assert next(lines) == ": keep-alive"


def test_heartbeat_interval_defaults_to_fifteen_seconds() -> None:
    assert events_route.HEARTBEAT_S == 15.0


# --------------------------------------------------------------------------- #
# 断连清理与重连对账
# --------------------------------------------------------------------------- #
def test_subscriber_is_released_after_disconnect(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """客户端断了必须把订阅者摘掉，否则跑一晚上就攒一堆僵尸订阅。

    ⚠️ 服务端**不是**在客户端一断就立刻察觉：它要等到下一次往这条流写数据失败时才
    发现（读侧不轮询）。默认心跳 15s，而本用例只等 5s —— 那就成了一场必输的赛跑：
    实测单独跑稳定失败、混跑偶发通过（15s 内恰好有事件被写出去就过）。
    这里把心跳压到 0.3s 让「下一个写」立刻到来，既保留被验语义
    （摘除动作本身仍由 Starlette 的取消传播 + ``finally`` 完成），又让结果确定。
    """
    monkeypatch.setattr(events_route, "HEARTBEAT_S", 0.3)

    with httpx.Client(timeout=TIMEOUT) as client:
        with client.stream("GET", f"{live_server}/api/events") as response:
            next(response.iter_lines())
            assert deps.get_event_bus().subscriber_count == 1
        # 退出 with 即断开；断开会在下一个心跳写失败时被服务端察觉，订阅随之摘除
        for _ in range(100):
            if deps.get_event_bus().subscriber_count == 0:
                break
            time.sleep(0.05)
    assert deps.get_event_bus().subscriber_count == 0


def test_reconnect_uses_progress_endpoint_for_reconciliation(
    live_server: str, ui_paths: Path
) -> None:
    """断线期间的事件是真的丢了，SSE 不补发 —— 重连只能靠全量进度对账。

    这里验的是「对账数据源是对的」：连上 SSE、断开、再从 progress 拿全量。
    """
    _seed_run(ui_paths / "autolearn.db")

    with httpx.Client(timeout=TIMEOUT) as client:
        with client.stream("GET", f"{live_server}/api/events") as response:
            next(response.iter_lines())
            _emit(Event.TASK_CREATED, {"item_id": "i1"})
            # 故意不等这条事件，直接断开 —— 模拟网络闪断
        payload = client.get(f"{live_server}/api/run/progress").json()

    assert payload["total"] == 3
    assert payload["by_state"]["verified"] == 2
    assert payload["by_state"]["pending"] == 1
    assert payload["done"] == 2
    assert payload["current_item_id"] == "i3"


def _seed_run(db_path: Path) -> None:
    conn = init_db(db_path)
    now = "2026-09-25T00:00:00+00:00"
    conn.execute(
        "INSERT INTO run (run_id, started_at, status, config_json) VALUES (?,?,?,?)",
        ("r1", now, "running", "{}"),
    )
    rows = [
        ("i1", "quiz", "verified"),
        ("i2", "quiz", "verified"),
        ("i3", "quiz", "pending"),
    ]
    for item_id, kind, state in rows:
        conn.execute(
            "INSERT INTO task_item (item_id, run_id, type, state, attempts, suspended,"
            " created_at, updated_at) VALUES (?,?,?,?,0,0,?,?)",
            (item_id, "r1", kind, state, now, now),
        )
    conn.commit()
    conn.close()
