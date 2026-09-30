"""P5 运行配置与生命周期路由验收。

对应 P5 验收口径里的（v0.2.0 口径）：
- 配置里**没有**通道优先级 / 通道模式这两个字段，也不接受它们（已随双通道删除）
- 一条模型配置都没有时启动被拦下（400 + 引导码）：页面只能由模型读，没有别的路
- 运行中配置只读
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from core.config import RunConfig
from core.enums import ProbeName, TaskType
from core.orchestrator import Orchestrator, RunContext
from ui import APP_VERSION, code_rev, deps
from ui.guide import GUIDE_AUTH_FAILED


def add_model(client: TestClient, *, name: str = "假厂商", model: str = "fake-model") -> dict:
    """往模型库里塞一套配置。

    v0.2.0 起没有模型就起不了运行，所以凡是要验「启动成功」的用例都得先调用它。
    ``base_url`` 刻意是不可达域名：本用例只走启动前的检查，
    不会真的发请求（真请求路径由 ``test_api_models.py`` 的假端点覆盖）。
    """
    response = client.post(
        "/api/models",
        json={"name": name, "base_url": "https://invalid.example.com/v1", "model": model},
    )
    assert response.status_code == 201, response.text
    return response.json()


class StubOrchestrator:
    """记录被调用的方法，用来验证路由确实把控制权交给了编排器。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self) -> None:
        self.calls.append("run")

    async def pause(self) -> None:
        self.calls.append("pause")

    async def resume(self) -> None:
        self.calls.append("resume")

    async def stop(self) -> None:
        self.calls.append("stop")


def install_stub() -> StubOrchestrator:
    stub = StubOrchestrator()
    deps.set_orchestrator(cast("Orchestrator", stub))
    return stub


# --------------------------------------------------------------------------- #
# health / config
# --------------------------------------------------------------------------- #
def test_health(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["ok"] is True
    assert payload["version"] == APP_VERSION
    assert payload["run_id"] is None


def test_health_exposes_stale_process_markers(client: TestClient) -> None:
    """``/api/health`` 必须能回答「这个进程跑的是当前代码吗」。

    实测踩过的坑：改完代码界面没变化，实际是控制台连着一个改动之前启动的旧
    进程 —— 旧进程的接口定义与新代码不一致，症状离奇（带参数的接口不认参数、
    抓取永远返回空）。所以至少要能看出「它的代码指纹是什么、什么时候起的」。
    """
    payload = client.get("/api/health").json()
    assert payload["code_rev"] == code_rev()
    assert payload["pid"] > 0
    assert payload["started_at"]


def test_health_exposes_boot_rev_and_why_it_matters(client: TestClient) -> None:
    """判陈旧必须用 ``boot_rev``（**进程加载**的代码），不能用 ``code_rev``。

    ``code_rev`` 是**请求时现读磁盘**算的，所以在活着的进程里它恒等于磁盘现状 ——
    拿它做相等比较等于恒真。实测踩到：``check_server.py`` 对一个改动前启动的进程
    报「✅ 一致」，而那个进程里的 ``core/`` 还是老代码。``boot_rev`` 在
    ``ui/__init__.py`` 被导入时算好，代表的才是「这个进程到底加载了哪份代码」。
    """
    payload = client.get("/api/health").json()
    assert payload["boot_rev"]
    assert len(str(payload["boot_rev"])) == 12
    # 本测试进程里两者必然相等（导入之后没人改过磁盘代码）；一旦不等，
    # 说明「代码被改过了而服务还在跑」——那正是要被抓出来的情形。
    assert payload["boot_rev"] == payload["code_rev"]


def test_code_rev_is_stable_and_short() -> None:
    """指纹用于**相等比较**，所以必须确定（同一份代码两次算结果一致）。"""
    first = code_rev()
    assert first == code_rev()
    assert len(first) == 12
    int(first, 16)  # 是十六进制，不是随便一个字符串


def test_config_defaults_drop_the_channel_choices(client: TestClient) -> None:
    """配置草稿的默认值，且**不再有**通道优先级 / 通道模式。

    这条同时是回归守卫：删掉的两个字段如果哪天从 ``RunConfigOut`` 里冒回来，
    前端会照着渲染一个「DOM 优先」，而那条路在 v0.2.0 已经不存在了。
    """
    payload = client.get("/api/run/config").json()
    assert "probe_order" not in payload
    assert "probe_mode" not in payload
    assert "available_modes" not in payload
    assert payload["sample_n"] == 1
    assert payload["task_sequence"] == ["quiz"]
    # 没有任何模型配置 —— 前端据此把「启动」置灰，而不是让用户点下去收 400
    assert payload["model_ready"] is False
    assert payload["vision_ready"] is False
    assert payload["locked"] is False


def test_config_update_persists_and_reflects(client: TestClient, ui_paths: Any) -> None:
    response = client.put(
        "/api/run/config",
        json={
            "sample_n": 7,
            "task_sequence": ["quiz", "video"],
        },
    )
    assert response.status_code == 200
    assert response.json()["sample_n"] == 7

    on_disk = json.loads((ui_paths / "run_config.json").read_text(encoding="utf-8"))
    assert on_disk["sample_n"] == 7
    assert "probe_order" not in on_disk
    assert "probe_mode" not in on_disk


def test_removed_channel_fields_are_rejected(client: TestClient) -> None:
    """老前端 / 老脚本仍传 ``probe_order`` 时必须 422，不能静默吞掉。

    静默忽略的后果是「界面写着模型优先、实际跑的是另一回事」——
    这种同名不同物的漂移正是最难查的一类。``extra="forbid"`` 让它当场响。
    """
    for stale in ({"probe_order": "vision-first"}, {"probe_mode": "dom-only"}):
        response = client.put("/api/run/config", json=stale)
        assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "payload",
    [{"sample_n": 0}, {"task_sequence": []}],
)
def test_config_rejects_illegal_values(client: TestClient, payload: dict[str, Any]) -> None:
    response = client.put("/api/run/config", json=payload)
    assert response.status_code == 422, response.text


def test_partial_patch_keeps_other_fields(client: TestClient) -> None:
    client.put("/api/run/config", json={"sample_n": 9})
    client.put("/api/run/config", json={"auto_apply": True})
    payload = client.get("/api/run/config").json()
    assert payload["sample_n"] == 9
    assert payload["auto_apply"] is True


def test_config_is_read_only_while_running(client: TestClient) -> None:
    install_stub()
    response = client.put("/api/run/config", json={"sample_n": 5})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "run_locked"

    locked = client.get("/api/run/config").json()
    assert locked["locked"] is True


# --------------------------------------------------------------------------- #
# 启动闸门
# --------------------------------------------------------------------------- #
def test_start_refuses_without_any_model(client: TestClient) -> None:
    """一条模型配置都没有时必须拦在启动前，并给出下一步动作。

    v0.2.0 起页面只由模型读（截图 → 模型 → 坐标），**没有第二条路**，
    所以这条不再只对「模型优先」成立 —— 任何一次运行都要求有模型。
    """
    response = client.post("/api/run/start")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error_code"] == "no_config"
    assert detail["next_action"], "置灰是引导不是静默失败 —— 必须给下一步动作"


def test_start_is_allowed_once_a_model_exists(client: TestClient) -> None:
    """加了一套模型配置之后就能起跑 —— 闸门是「有没有模型」，不是别的。"""
    add_model(client)
    response = client.post("/api/run/start")
    assert response.status_code == 200
    assert len(response.json()["run_id"]) == 12


def test_start_rejects_second_run(client: TestClient) -> None:
    install_stub()
    response = client.post("/api/run/start")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "run_active"


def test_start_task_sequence_override(client: TestClient) -> None:
    add_model(client)
    response = client.post("/api/run/start", json={"task_sequence": ["video", "quiz"]})
    assert response.status_code == 200
    assert deps.get_run_config().task_sequence == [TaskType.VIDEO, TaskType.QUIZ]


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
def test_control_returns_409_without_active_run(client: TestClient) -> None:
    for action in ("pause", "resume", "stop"):
        response = client.post(f"/api/run/{action}")
        assert response.status_code == 409, action
        assert response.json()["detail"]["error_code"] == "run_not_active"


def test_control_delegates_to_orchestrator(client: TestClient) -> None:
    stub = install_stub()
    for action in ("pause", "resume", "stop"):
        assert client.post(f"/api/run/{action}").status_code == 200
    assert stub.calls == ["pause", "resume", "stop"]
    # stop 之后运行态清空
    assert deps.is_running() is False


def test_control_drives_a_real_orchestrator(client: TestClient) -> None:
    """P7 之后暂停 / 恢复 / 停止走的是真方法，不再返回 501。

    ``pause`` / ``resume`` / ``stop`` 只动运行闸，不需要任何依赖，
    所以一个「没接感知/执行」的编排器也能被它们正确驱动。
    """
    ctx = RunContext(run_id="r1", cfg=RunConfig(), started_at=datetime.now(UTC))
    orchestrator = Orchestrator(ctx)
    deps.set_orchestrator(orchestrator)

    assert client.post("/api/run/pause").status_code == 200
    assert orchestrator.paused is True
    assert orchestrator.paused_by == "manual"

    assert client.post("/api/run/resume").status_code == 200
    assert orchestrator.paused is False

    assert client.post("/api/run/stop").status_code == 200
    assert orchestrator.stopped is True
    assert deps.is_running() is False


def test_control_returns_501_when_a_method_is_unimplemented(client: TestClient) -> None:
    """个别分支仍未落实现时必须返回 501，而不是假装成功或丢一个 500。

    这条守住的是路由层的翻译：``NotImplementedError`` → ``501 + error_code``。
    它是**防御性分支** —— P0→P8 已全部交付、业务 stub 归零，所以这里用一个
    假实现来驱动，而不是等着某个真 stub 出现（那种等待会让用例随进度失效，
    正如它当初为 P8 写、P8 交付后就失去意义一样）。
    """

    class NotYetImplemented:
        async def pause(self) -> None:
            raise NotImplementedError("尚未落实现的分支")

    deps.set_orchestrator(NotYetImplemented())  # type: ignore[arg-type]
    response = client.post("/api/run/pause")

    assert response.status_code == 501
    assert response.json()["detail"]["error_code"] == "not_implemented"


# --------------------------------------------------------------------------- #
# 进度与引导
# --------------------------------------------------------------------------- #
def test_progress_shape_with_empty_db(client: TestClient) -> None:
    payload = client.get("/api/run/progress").json()
    assert payload["total"] == 0
    assert payload["done"] == 0
    assert payload["by_state"] == {}
    assert payload["current_item_id"] is None
    assert payload["running"] is False
    # 运行态条（媒体态 / 弹题 / 任务栈）的栈深在重连后靠这个字段还原
    assert payload["stack_depth"] == 0


def test_progress_reports_stack_depth(client: TestClient) -> None:
    """弹题压栈后，重连对账必须能拿回栈深（SSE 的 stack.* 是增量，断线不补发）。"""
    from core import db
    from core.models import VideoState
    from core.tasks import SuspendFrame
    from ui.store import db_file

    deps.set_current_run_id("run-stack")
    conn = db.init_db(db_file())  # 空库：先把表建出来（正常路径由 run.start 建）
    try:
        db.save_suspend_frames(
            conn,
            "run-stack",
            [
                SuspendFrame(
                    parent_item_id="vid-1",
                    child_item_id="quiz-1",
                    media_state_at_suspend=VideoState(
                        paused=True,
                        ended=False,
                        current_time=6.0,
                        duration=20.0,
                        episode_index=1,
                        episode_total=6,
                    ),
                )
            ],
        )
    finally:
        conn.close()

    payload = client.get("/api/run/progress").json()

    assert payload["stack_depth"] == 1


def test_guide_returns_independent_cards(client: TestClient) -> None:
    auth = client.get("/api/guide", params={"code": "auth_failed"}).json()
    assert auth["title"] == GUIDE_AUTH_FAILED.title

    model = client.get("/api/guide", params={"code": "model_not_found"}).json()
    assert model["title"] != auth["title"]

    first_run = client.get("/api/guide").json()
    assert "MockProvider" in first_run["body_md"]


def test_unknown_guide_code_returns_null_not_a_generic_message(client: TestClient) -> None:
    """未登记的码必须返回 null，逼调用方显式处理。"""
    response = client.get("/api/guide", params={"code": "totally_unknown"})
    assert response.status_code == 200
    assert response.json() is None


# --------------------------------------------------------------------------- #
# 探针链路与运行配置的联动
# --------------------------------------------------------------------------- #
def test_probe_chain_is_always_vision_only(client: TestClient) -> None:
    """v0.2.0 的题目链恒为 ``[vision]``，与配置无关 —— 没有第二个开关可拧。"""
    from core.config import active_probe_chain

    assert active_probe_chain(deps.get_run_config()) == [ProbeName.VISION]

    client.put("/api/run/config", json={"sample_n": 3})
    assert active_probe_chain(deps.get_run_config()) == [ProbeName.VISION]


def test_run_config_enum_types_round_trip(client: TestClient) -> None:
    """枚举字段进出参都是枚举，不是裸字符串。"""
    client.put(
        "/api/run/config",
        json={"target_kind": "desktop_window", "task_sequence": ["video"]},
    )
    cfg = deps.get_run_config()
    assert cfg.task_sequence == [TaskType.VIDEO]
    assert cfg.target_kind.value == "desktop_window"
    assert client.get("/api/run/config").json()["task_sequence"] == ["video"]
