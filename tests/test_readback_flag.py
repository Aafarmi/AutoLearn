"""详情页「动作回读不一致」判据（2026-10-01 修的那次误报）。

用户报障：第 28、29 题选的选项与解题组给的一致，详情页却显示
「动作回读不一致」。根因在投影层（``ui/store.py``）：它拿 ``action.json`` 里的
``readback`` 与 ``expected`` 做**字符串比较**，而这两个串含义与来源都不同
（``expected`` 是阈值表达式 ``region_mad≥2.0``，``readback`` 是人话
``region_mad=5.32 state=changed aim=ink_centroid``），并且 ``action.json`` 里
**压根没有** ``expected`` 键 —— 于是「文本不相等」恒为真，每一次成功点击都被
判成不一致。

为什么这些用例**必须走真实投影路径**
------------------------------------
bug 就长在「``action.json`` → ``TaskStore.item_detail`` → ``GET /api/items/{id}``」
这条链上：当时任何针对纯函数的用例都能全绿。所以这里一律先写留痕
（或经 ``RunLogger.save_json`` 写真实的 ``ActionResult``），再读接口出参
``readback_mismatch`` 断言 —— 纯函数单测只能说明「函数按我写的规则跑」，
说明不了「详情页不再误报」。

判据口径（见 :func:`ui.store.readback_mismatch`）：
``readback_ok`` 在 → 以它为准（``True`` = 一致 / 本来就没有可校验的回读）；
不在（老留痕）→ 退回同一条留痕里同样结构化的 ``ok``；都没有 → 按一致处理。
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from act.actuator import Actuator
from core.config import RunConfig
from core.db import init_db
from core.enums import ActionKind, ActLevel
from core.events import Event
from core.models import ActionResult
from core.trace import RunLogger
from tests.act_helpers import FakePage, RecordingBus
from ui.store import db_file, log_root

#: 成功动作会写进 ``action.json`` 的 ``readback`` 形状（全来自 ``act/actuator.py``）：
#: 像素三态命中、轻变化、墨迹上没变（本来就已选中）、已变化、主动跳过、无回读可校验。
SUCCESS_READBACKS: tuple[str | None, ...] = (
    "region_mad=5.32 state=changed aim=ink_centroid",
    "weak_changed:1.20 state=weak aim=ink_centroid",
    "no_change_on_ink:region_mad=0.00 aim=ink_centroid",
    "already_changed:region_mad=2.40 state=changed",
    "skipped:paused=True current_time=12.0",
    "aim=box_center",
    "gesture:mouse:left",
    None,
)

#: 阈值表达式。**故意写进留痕**：即便它和 ``readback`` 一起躺在 action.json 里，
#: 投影层也不许再拿这两个串做比较（用户报障现场就是这个组合）。
EXPECTED_TEXT = "region_mad≥2.0"


# --------------------------------------------------------------------------- #
# 留痕与接口的接线（真实投影路径）
# --------------------------------------------------------------------------- #
def _new_item(client: TestClient, *, state: str = "pending_confirm") -> tuple[str, str]:
    """建一个任务 + 塞一条条目：编排层才是推进状态的地方，这里只验投影。"""
    response = client.post("/api/tasks", json={})
    assert response.status_code == 201, response.text
    run_id = str(response.json()["run_id"])
    item_id = f"{run_id}-q1"

    conn = init_db(db_file())
    try:
        now = "2026-10-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO task_item (item_id, run_id, type, state, attempts, suspended,"
            " created_at, updated_at) VALUES (?,?,?,?,0,0,?,?)",
            (item_id, run_id, "quiz", state, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id, item_id


def _action(**overrides: Any) -> dict[str, Any]:
    """一条 ``select_option`` 留痕的最小形状（键名与 ``ActionResult`` 一致）。"""
    payload: dict[str, Any] = {
        "kind": "select_option",
        "target": "answer:C",
        "level_used": "l6_vision_xy",
        "ok": True,
        "readback": None,
        "elapsed_ms": 193,
        "error": None,
        "screenshot_ref": None,
    }
    payload.update(overrides)
    return payload


def _write_action(run_id: str, item_id: str, payload: dict[str, Any]) -> None:
    directory = log_root() / run_id / item_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "action.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _detail_flag(client: TestClient, item_id: str) -> bool:
    """详情页那个字段 —— **界面显示与否就看它**。"""
    response = client.get(f"/api/items/{item_id}")
    assert response.status_code == 200, response.text
    return bool(response.json()["readback_mismatch"])


# --------------------------------------------------------------------------- #
# 成功 → 绝不报不一致
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("readback", SUCCESS_READBACKS)
def test_a_successful_action_is_not_reported_as_a_mismatch(
    client: TestClient, readback: str | None
) -> None:
    """成功收工的各种 readback 文案（含 ``skipped:``）都必须是「一致」。"""
    run_id, item_id = _new_item(client)
    _write_action(
        run_id,
        item_id,
        _action(readback=readback, readback_ok=True, expected=EXPECTED_TEXT),
    )
    assert _detail_flag(client, item_id) is False


@pytest.mark.parametrize("readback", SUCCESS_READBACKS)
def test_a_legacy_action_without_the_new_field_is_not_reported(
    client: TestClient, readback: str | None
) -> None:
    """**向后兼容**：2026-10-01 之前的 ``action.json`` 没有 ``readback_ok``。

    缺字段只说明留痕老，不说明动作没验成 —— 老留痕里有结构化的 ``ok``，
    照它判即可；**绝不能**因为缺字段就退回文本比较（那样历史任务会全变红）。
    """
    run_id, item_id = _new_item(client)
    payload = _action(readback=readback, expected=EXPECTED_TEXT)
    assert "readback_ok" not in payload, "这条用例要的就是老留痕形状"
    _write_action(run_id, item_id, payload)
    assert _detail_flag(client, item_id) is False


@pytest.mark.parametrize("readback", SUCCESS_READBACKS)
def test_a_successful_action_saved_by_the_real_writer_is_not_reported(
    client: TestClient, readback: str | None
) -> None:
    """``ActionResult`` → ``RunLogger.save_json`` → 接口：字段真的落进了 action.json。

    这一条防的是「模型加了字段、序列化时被丢掉」这类静默漂移
    （例如将来有人给它加 ``exclude_defaults``）。
    """
    run_id, item_id = _new_item(client)
    result = ActionResult(
        kind=ActionKind.SELECT_OPTION,
        target="answer:C",
        level_used=ActLevel.L6_VISION_XY,
        ok=True,
        readback=readback,
        readback_ok=True,
    )
    RunLogger(run_id, root=log_root()).save_json(item_id, "action", result)
    on_disk = json.loads((log_root() / run_id / item_id / "action.json").read_text("utf-8"))
    assert on_disk["readback_ok"] is True
    assert _detail_flag(client, item_id) is False


def test_an_action_without_any_verdict_is_not_reported(client: TestClient) -> None:
    """更老的 / 截断的留痕连判据都没有 → 按「一致」处理，不凭空报一次不一致。"""
    run_id, item_id = _new_item(client)
    _write_action(run_id, item_id, {"kind": "select_option", "target": "answer:C"})
    assert _detail_flag(client, item_id) is False


# --------------------------------------------------------------------------- #
# 真失败 → 必须报
# --------------------------------------------------------------------------- #
def test_an_exhausted_action_is_reported_as_a_mismatch(client: TestClient) -> None:
    """重放耗尽 = 明确没验成（用户必须复核）→ 详情页要报出来。"""
    run_id, item_id = _new_item(client, state="failed")
    _write_action(
        run_id,
        item_id,
        _action(
            ok=False,
            readback=None,
            readback_ok=False,
            error=(
                "action_ladder_exhausted: readback_mismatch: "
                "region_mad=0.01 state=none samples=2 aim=box_center"
            ),
            screenshot_ref="logs/x/error.png",
        ),
    )
    assert _detail_flag(client, item_id) is True


def test_a_legacy_failure_is_still_reported(client: TestClient) -> None:
    """老留痕没有新字段 → 退回 ``ok``：``ok=False`` 照样报，绝不被兼容逻辑放过。"""
    run_id, item_id = _new_item(client, state="failed")
    _write_action(
        run_id,
        item_id,
        _action(ok=False, error="action_ladder_exhausted: readback_mismatch: region_mad=0.01"),
    )
    assert _detail_flag(client, item_id) is True


def test_a_failed_action_saved_by_the_real_writer_is_reported(client: TestClient) -> None:
    """真失败的 ``ActionResult`` 走真实落盘 → 详情页必须报（防止默认值把它吞掉）。"""
    run_id, item_id = _new_item(client, state="failed")
    result = ActionResult(
        kind=ActionKind.SELECT_OPTION,
        target="answer:C",
        level_used=ActLevel.L6_VISION_XY,
        ok=False,
        readback_ok=False,
        error="action_ladder_exhausted: readback_mismatch: region_mad=0.01 state=none",
        screenshot_ref="logs/x/error.png",
    )
    RunLogger(run_id, root=log_root()).save_json(item_id, "action", result)
    on_disk = json.loads((log_root() / run_id / item_id / "action.json").read_text("utf-8"))
    assert on_disk["readback_ok"] is False
    assert _detail_flag(client, item_id) is True


# --------------------------------------------------------------------------- #
# 结构化字段的优先级
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # 显式结论说「没验成」，哪怕 readback 文案看着像成功 → 报警
        ({"ok": True, "readback_ok": False, "readback": "region_mad=5.32 state=changed"}, True),
        # 显式结论说「无需复核」→ 以它为准（``ok`` 只是老留痕的兜底判据）
        ({"ok": False, "readback_ok": True, "readback": "weak_changed:1.20 state=weak"}, False),
        # 字段不是布尔（损坏 / 别的类型）→ 当作缺失，退回 ``ok``
        ({"ok": True, "readback_ok": None, "readback": "region_mad=5.32 state=changed"}, False),
        ({"ok": False, "readback_ok": "yes", "readback": None}, True),
    ],
)
def test_the_structured_field_decides(
    client: TestClient, payload: dict[str, Any], expected: bool
) -> None:
    """判据必须是结构化字段本身，与 ``readback`` 的文本长相无关。"""
    run_id, item_id = _new_item(client)
    _write_action(run_id, item_id, _action(**payload))
    assert _detail_flag(client, item_id) is expected


# --------------------------------------------------------------------------- #
# 写入侧：两个公共出口必须把真实语义填上（字段默认值是 True，漏填就是漏报）
# --------------------------------------------------------------------------- #
async def test_done_and_exhausted_fill_the_structured_flag() -> None:
    """``_done`` → ``True``（成功收工，含 ``skipped:`` / ``no_change_on_ink:``）；
    ``_exhausted`` → **显式** ``False``。

    ⚠️ 这一条是关键：``readback_ok`` 有默认值 ``True``，``_exhausted`` 若不显式填，
    「重放耗尽」这条真失败会被默认值掩盖成「一致」——那比原来的误报更坏
    （误报只是吵，漏报是把失败说成成功）。
    """
    bus = RecordingBus()
    actuator = Actuator(FakePage(), RunConfig(), bus=bus, item_id="i1")
    started = time.perf_counter()

    done = actuator._done(
        ActionKind.SELECT_OPTION,
        "answer:C",
        ActLevel.L6_VISION_XY,
        "region_mad=6.02 state=changed aim=ink_centroid",
        started,
    )
    assert done.readback_ok is True

    skipped = actuator._done(
        ActionKind.PAUSE_MEDIA,
        "media:play_button",
        ActLevel.L4_FOCUS_KEYS,
        "skipped:paused=True",
        started,
    )
    assert skipped.readback_ok is True, "主动不动手不是不一致"

    failed = await actuator._exhausted(
        ActionKind.SELECT_OPTION,
        "answer:C",
        ActLevel.L6_VISION_XY,
        "readback_mismatch: region_mad=0.01 state=none samples=2 aim=box_center",
        3,
        started,
    )
    assert failed.readback_ok is False
    assert failed.model_dump()["readback_ok"] is False

    payloads = bus.payloads(Event.ACT_LEVEL_USED)
    assert [payload["readback_ok"] for payload in payloads] == [True, True, False]
    assert payloads[2]["pause"] is True, "失败照旧要发暂停信号"


def test_a_failed_gesture_is_flagged_for_review() -> None:
    actuator = Actuator(FakePage(), RunConfig(), item_id="i1")
    result = actuator._gesture_failed("gesture:swipe=left", "TimeoutError: boom", 0.0)
    assert result.readback_ok is False
    assert result.ok is False


async def test_a_successful_swipe_has_no_readback_to_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """手势的 ``readback`` 只是「往哪划了多远」的元数据，不是校验结论 ——
    一次正常的滑动不该在详情页报「动作回读不一致」（翻没翻页由编排层比对指纹）。"""
    actuator = Actuator(FakePage(), RunConfig(), bus=RecordingBus(), item_id="i1")

    async def _size(self: Actuator) -> tuple[float, float]:
        return (1280.0, 720.0)

    async def _swipe(
        self: Actuator, start: tuple[float, float], end: tuple[float, float], span_ms: int
    ) -> None:
        return None

    monkeypatch.setattr(Actuator, "_viewport_size", _size)
    monkeypatch.setattr(Actuator, "_mouse_swipe", _swipe)

    result = await actuator.swipe("left")
    assert result.ok is True
    assert result.readback_ok is True
