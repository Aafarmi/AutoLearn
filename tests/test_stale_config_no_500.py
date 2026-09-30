"""陈旧运行配置不得让界面接口 500（2026-09-28 线上事故回归）。

事故经过
--------
``state/run_config.json`` 是**跨版本存盘**的，而 ``GuardThresholds`` /
``RateLimits`` 这些嵌套模型一律 ``extra="forbid"``。删掉一个 guard 字段之后，
老文件里残留的那几个键会让整份配置校验失败 —— 而 ``load_run_config()`` 的异常
会一路冒到路由，于是：

    GET /api/run/config  -> 500
    GET /api/targets     -> 500

症状是**每次打开控制台都打不开**，而且看上去像「服务坏了」，
完全看不出是「一个过期字段」。``/api/targets/launch``（「接管启动浏览器」）
同样中招，所以用户看到的是「一拉起浏览器就 HTTP 500」。

本文件把「界面能打开」这件事钉死：只要盘上放着一份**旧版本写的**配置，
所有只读接口都必须正常应答。修法在 ``core/config.py::load_run_config``
（容忍 + 剔除陈旧字段 + 自愈回写，且**永不抛异常**）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ui import deps, server

#: 只读接口里**必须**在陈旧配置下存活的那几个。
#: 刻意列死而不是遍历 ``app.routes``：``/api/events`` 是 SSE，``TestClient.get``
#: 会一直挂着等事件流；``/api/artifacts/*`` 之类带路径参数的也不便空手调用。
READ_ONLY_ENDPOINTS = (
    "/api/health",
    "/api/run/config",
    "/api/run/progress",
    "/api/guide",
    "/api/targets",
    "/api/tasks",
    "/api/items",
    "/api/models",
    "/api/providers/presets",
)


def _stale_payload() -> dict:
    """一份「P14 时代写到盘上、v0.2.0 来读」的配置。

    三层陈旧一次凑齐，模拟真实升级现场：

    1. 顶层已删除的通道开关（``probe_order`` / ``probe_mode``）；
    2. 嵌套 ``guards`` 里 7 个已删除的字段（真正的 500 肇事者）；
    3. 一个仍然有效、但用户改过的值（``sample_n``）—— 用来证明
       「忽略旧字段」不等于「整份配置丢回默认」。
    """
    return {
        "probe_order": "vision-first",
        "probe_mode": "auto",
        "sample_n": 4,
        "guards": {
            "agreement_accept": 0.8,
            "advance_scroll_probe": True,
            "advance_scroll_settle_polls": 2,
            "end_vision_check": True,
            "end_progress_patterns": [r"第\s*(\d+)\s*题"],
            "end_progress_max_total": 500,
            "end_marker_texts": ["已是最后一题"],
            "end_unknown_action": "pause",
            "end_decision_timeout_s": 180.0,
        },
    }


@pytest.fixture()
def stale_client(ui_paths: Path):
    """带着一份陈旧配置启动的控制台。"""
    (ui_paths / "run_config.json").write_text(
        json.dumps(_stale_payload(), ensure_ascii=False), encoding="utf-8"
    )
    deps.reset_state()
    try:
        with TestClient(server.create_app(), raise_server_exceptions=False) as client:
            yield client
    finally:
        deps.reset_state()


@pytest.mark.parametrize("path", READ_ONLY_ENDPOINTS)
def test_read_only_endpoint_survives_stale_config(stale_client: TestClient, path: str) -> None:
    """这是事故本体：陈旧配置下**任何只读接口都不许 500**。"""
    response = stale_client.get(path)
    assert response.status_code < 500, (
        f"{path} 在陈旧配置下返回 {response.status_code} —— "
        "界面会直接打不开，而用户看不出是配置过期"
    )


def test_stale_config_keeps_the_users_other_settings(stale_client: TestClient) -> None:
    """剔除旧字段不等于整份配置回默认 —— 用户的设置必须保住。"""
    payload = stale_client.get("/api/run/config").json()
    assert payload["sample_n"] == 4


def test_stale_config_is_ignored_not_exposed(stale_client: TestClient) -> None:
    """已删除的通道开关不能再从出参里冒出来。"""
    payload = stale_client.get("/api/run/config").json()
    for gone in ("probe_order", "probe_mode", "available_modes"):
        assert gone not in payload


def test_config_file_self_heals_after_first_read(stale_client: TestClient, ui_paths: Path) -> None:
    """读一次之后盘上应被清理 —— 否则每次启动都刷同一条警告。"""
    stale_client.get("/api/run/config")

    healed = json.loads((ui_paths / "run_config.json").read_text(encoding="utf-8"))
    assert "end_vision_check" not in healed["guards"]
    assert healed["sample_n"] == 4


def test_browser_launch_route_does_not_500_on_stale_config(ui_paths: Path) -> None:
    """**用户报的那条路**：「接管启动浏览器」不许因为陈旧配置报 500。

    ``POST /api/targets/launch`` 与 ``GET /api/targets`` 一样都会先
    ``get_run_config()`` —— 所以事故当时用户看到的正是「一点抓起浏览器就 HTTP 500」。

    这里把目标类型写成 ``desktop_window``：那条分支在任何浏览器进程被拉起之前
    就以 400 明确拒绝，于是本用例能**在不真的开浏览器**的前提下走完
    同一段配置加载路径。真正要断言的是「不是 5xx」。
    """
    stale = _stale_payload()
    stale["target_kind"] = "desktop_window"
    (ui_paths / "run_config.json").write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8"
    )
    deps.reset_state()
    try:
        with TestClient(server.create_app(), raise_server_exceptions=False) as client:
            response = client.post("/api/targets/launch")
    finally:
        deps.reset_state()

    assert response.status_code < 500, response.text
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "target_unavailable"
