"""M4-1 / M4-3 编排循环：状态机、必停、留痕、降级统计。

全部用内存替身（``tests/orchestrator_helpers.py``）：这些断言一条都不该
依赖「今天起没起靶场」。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import db
from core.config import RunConfig
from core.enums import MediaState, ProbeName, QType, QuestionState, TaskType
from core.events import Event
from core.models import PerceptionResult, TaskItem
from core.orchestrator import Orchestrator, RunContext, RunDeps
from core.trace import EventBus, RunLogger
from tests.orchestrator_helpers import (
    NEXT_BOX,
    FakeActuator,
    FakeAdapter,
    FakePage,
    FakePipeline,
    FakeSolver,
    FakeVerifier,
    RecordingPacer,
    make_perception,
    make_question,
    seed_vision_geometry,
)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def make_conn(tmp_path: Path):
    return db.init_db(tmp_path / "autolearn.db")


async def wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> None:
    """等一个同步断言成立。比 ``sleep(固定值)`` 稳，也不会拖慢快路径。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("等待条件超时")


def make_orchestrator(
    tmp_path: Path,
    *,
    cfg: RunConfig | None = None,
    pipeline: FakePipeline | None = None,
    solver: FakeSolver | None = None,
    actuator: FakeActuator | None = None,
    verifier: FakeVerifier | None = None,
    conn=None,
    bus: EventBus | None = None,
    logger: RunLogger | None = None,
    pacer: RecordingPacer | None = None,
    page: FakePage | None = None,
    run_id: str = "run-1",
) -> tuple[Orchestrator, dict]:
    cfg = cfg or RunConfig(auto_apply=True)
    connection = conn if conn is not None else make_conn(tmp_path)
    run_logger = logger if logger is not None else RunLogger(run_id, root=tmp_path / "logs")
    page = page if page is not None else FakePage()
    actuator = actuator or FakeActuator()
    verifier = verifier or FakeVerifier()
    pacer = pacer or RecordingPacer()
    pipeline = pipeline or FakePipeline(generate=1)
    solver = solver or FakeSolver()

    ctx = RunContext(run_id=run_id, cfg=cfg, started_at=datetime.now(UTC))
    deps = RunDeps(
        page=page,
        adapter=FakeAdapter(),
        pipeline=pipeline,
        solver=solver,
        run_logger=run_logger,
        bus=bus,
        conn=connection,
        actuator_factory=lambda _item: actuator,
        verifier_factory=lambda _item: verifier,
        click_gap=pacer.click,
        submit_gap=pacer.submit,
        probe_timeout_s=0.1,
    )
    orchestrator = Orchestrator(ctx, deps=deps)
    seed_batch_geometry(orchestrator, pipeline.total)
    return orchestrator, {
        "actuator": actuator,
        "verifier": verifier,
        "pipeline": pipeline,
        "solver": solver,
        "conn": connection,
        "logger": run_logger,
        "pacer": pacer,
        "page": page,
        "logs": tmp_path / "logs" / run_id,
    }


def seed_batch_geometry(orchestrator: Orchestrator, total: int) -> None:
    """给「假流水线会走到的那些题」补上几何。

    为什么必须补：v0.2.0 起**只有**视觉一条作答路径，几何来自读题那一次
    （真实链路上由 ``_read_question_by_vision`` 顺手存下）。替身流水线直接给出一整道
    ``Question``、绕过了读题，所以要显式补上 —— 否则编排层会**正确地**判定
    「没有可点的坐标」而停下（它绝不猜坐标）。

    **方案里带上总题数**（``total``）：这正是真实链路上开局读图会得到的东西
    （``page.total``）。跑到第 ``total`` 题时，:meth:`Orchestrator._advance` 的第一道
    闸门就会先请视觉组确认「是不是全部完成了」—— 替身 provider 答"是"，
    于是干净收工。这条出口与真实站点跑到卷末时的出口**是同一条**，
    而不是测试专用的捷径。
    """
    questions = [make_question(index) for index in range(1, max(0, total) + 1)]
    if not questions:
        return
    seed_vision_geometry(orchestrator, *questions, next_box=NEXT_BOX, total=total)


def states_of(conn) -> list[str]:
    rows = conn.execute("SELECT state FROM task_item ORDER BY created_at, item_id").fetchall()
    return [row["state"] for row in rows]


# --------------------------------------------------------------------------- #
# 全自动跑批
# --------------------------------------------------------------------------- #
async def test_auto_apply_runs_every_question_to_verified(tmp_path: Path) -> None:
    pipeline = FakePipeline([make_perception(i) for i in range(1, 4)])
    orchestrator, env = make_orchestrator(tmp_path, pipeline=pipeline)

    await orchestrator.run()

    # v0.2.0 只有「按坐标点选项」这一条作答路径（``select_option``）
    assert env["actuator"].count("select_option") == 3
    assert env["actuator"].count("submit") == 3
    assert env["verifier"].calls == 3, "每题一次提交结果差分回读"
    assert states_of(env["conn"]) == [QuestionState.VERIFIED.value] * 3


async def test_run_row_is_written_and_finished(tmp_path: Path) -> None:
    orchestrator, env = make_orchestrator(tmp_path)
    await orchestrator.run()

    row = env["conn"].execute("SELECT * FROM run").fetchone()
    assert row["run_id"] == "run-1"
    assert row["status"] == "finished"
    assert row["finished_at"] is not None


async def test_states_advance_through_the_frozen_machine(tmp_path: Path) -> None:
    bus = EventBus()
    stream = bus.subscribe()
    orchestrator, _env = make_orchestrator(tmp_path, bus=bus)

    await orchestrator.run()

    transitions: list[tuple[str, str]] = []
    while not stream._queue.empty():
        name, payload = stream._queue.get_nowait()
        if name == Event.TASK_STATE_CHANGED:
            transitions.append((payload["from"], payload["to"]))
    await stream.aclose()

    assert transitions == [
        ("pending", "perceived"),
        ("perceived", "solved"),
        ("solved", "applied"),
        ("applied", "submitted"),
        ("submitted", "verified"),
    ]


async def test_solve_json_carries_samples_and_channel(tmp_path: Path) -> None:
    """M4-3：``solve.json`` 必须有采样明细（界面详情区要渲染它）。"""
    orchestrator, env = make_orchestrator(tmp_path)
    await orchestrator.run()

    # ``logs/<run_id>/`` 下除了**条目目录**，还有一个 run 级的 ``events.jsonl``
    # （事件流）。条目一律是子目录 —— 所以这里按目录筛，不是"应该只有一个孩子"。
    directories = [path for path in env["logs"].iterdir() if path.is_dir()]
    assert len(directories) == 1
    payload = json.loads((directories[0] / "solve.json").read_text(encoding="utf-8"))
    assert payload["chosen_labels"] == ["A"]
    assert "samples" in payload
    assert payload["confidence"] == pytest.approx(0.95)


async def test_all_artifacts_are_written(tmp_path: Path) -> None:
    orchestrator, env = make_orchestrator(tmp_path)
    await orchestrator.run()

    directory = next(path for path in env["logs"].iterdir() if path.is_dir())
    names = {path.name for path in directory.iterdir()}
    assert {"perception.json", "solve.json", "action.json", "verify.json"} <= names
    assert {"before.png", "after.png"} <= names


async def test_level_stat_is_recorded_for_each_action(tmp_path: Path) -> None:
    orchestrator, env = make_orchestrator(tmp_path)
    await orchestrator.run()

    stats = db.load_level_stats(env["conn"], "run-1")
    kinds = {row["kind"] for row in stats}
    assert "select_option" in kinds
    assert "submit" in kinds
    # 题目侧的动作 v0.2.0 **全在 L6**（只有「按模型给的坐标点」这一级），
    # 而且都必须成功 —— 热力图就是靠这两点看「题目链路是否健康」的。
    question_side = [row for row in stats if row["kind"] in {"select_option", "submit"}]
    assert question_side and all(row["level_used"] == "l6_vision_xy" for row in question_side)
    assert all(row["ok"] for row in question_side)
    # 替身没有页面，滑动手势机制因此不可用（``fake:no_page``）：
    # 这条失败是替身的边界，不是编排层的缺陷 —— 真手势由 ``test_advance_next``
    # 与 ``tests/test_actuator.py`` 用带页面的替身覆盖。
    assert {row["kind"] for row in stats if not row["ok"]} <= {"swipe"}


async def test_answer_is_persisted(tmp_path: Path) -> None:
    orchestrator, env = make_orchestrator(tmp_path)
    await orchestrator.run()

    stored = db.load_answer(env["conn"], make_question(1).qid)
    assert stored is not None
    assert stored["chosen_labels"] == ["A"]
    assert stored["review_flag"] is False


async def test_submit_gap_is_applied_between_questions(tmp_path: Path) -> None:
    """提交级限速（5~15s）必须真的插在两次提交之间，否则批量提交会被风控。"""
    pacer = RecordingPacer()
    orchestrator, env = make_orchestrator(
        tmp_path, pipeline=FakePipeline(generate=3), pacer=pacer
    )

    await orchestrator.run()

    assert env["pacer"].submits == 3
    assert env["pacer"].clicks >= 3, "动作级 click 间隔也要有"


async def test_single_choice_skill_clicks_its_selected_option_coordinates(tmp_path: Path) -> None:
    """有注册技能的单选题按自己的归一化框点击一次。"""
    question = make_question(1, qtype=QType.SINGLE)
    orchestrator, env = make_orchestrator(
        tmp_path,
        pipeline=FakePipeline([make_perception(1, question=question)]),
        solver=FakeSolver(labels=["A"]),
    )

    await orchestrator.run()

    clicked = [box for name, box, _size in env["actuator"].boxes if name == "select_option"]
    assert len(clicked) == 1, f"单选的选项应点击一次，实际 {clicked}"
    assert clicked[0][1] == pytest.approx(0.36)
    assert states_of(env["conn"]) == [QuestionState.VERIFIED.value]


async def test_page_change_mid_action_stops_before_the_next_option(tmp_path: Path) -> None:
    """点了第一个选项之后页面被换掉（URL 变了）→ **剩下的选项一个都不点**。

    替代护栏的由来：v0.2.0 取消了「执行前重校验题干」（题干与几何来自同一次读图，
    拿它校验自己没有意义）。于是对照物换成**页面本身**：URL 一变，
    这份几何就不属于当前画面了，照着它再点下去就是一次真实的误点。
    """
    question = make_question(1, qtype=QType.SINGLE)

    class UrlSwitchingActuator(FakeActuator):
        """第一次点选项就把页面换走 —— 演「下手的瞬间页面自己跳了」。"""

        async def select_option(self, box, size, qtype, *, target_label=None):  # type: ignore[no-untyped-def]
            result = await super().select_option(box, size, qtype, target_label=target_label)
            assert self.page is not None
            self.page.url = "https://real.example/other"
            return result

    page = FakePage()
    orchestrator, env = make_orchestrator(
        tmp_path,
        page=page,
        actuator=UrlSwitchingActuator(page=page),
        pipeline=FakePipeline([make_perception(1, question=question)]),
        solver=FakeSolver(labels=["A", "B"]),
    )

    await orchestrator.run()

    assert env["actuator"].count("select_option") == 1, "换页之后一次都不许再点"
    assert orchestrator.paused_by == "page_changed_mid_action"


# --------------------------------------------------------------------------- #
# 必停分支
# --------------------------------------------------------------------------- #
async def test_empty_vision_read_pauses_and_creates_no_item(tmp_path: Path) -> None:
    """视觉读图**成功但这一屏没有题** → 停下，且不凭空建条目。

    理由必须是具体的 ``vision_read_empty``（模型回了、但没看出题目），
    而不是笼统的 ``perception_failed`` —— 后者会把人引去查"通道读不到"，
    而这里要做的事是"看看屏幕上到底是什么"。
    """
    empty = PerceptionResult(question=None, channel_used=ProbeName.VISION, warnings=["vision:crop_failed"])
    orchestrator, env = make_orchestrator(tmp_path, pipeline=FakePipeline([empty]))

    await orchestrator.run()

    assert orchestrator.paused is True
    assert orchestrator.paused_by == "vision_read_empty"
    assert states_of(env["conn"]) == [], "读不到题不该凭空建条目"


async def test_question_that_does_not_advance_pauses_instead_of_looping(
    tmp_path: Path,
) -> None:
    """「下一题」没生效时不能无限重读同一道题。"""
    orchestrator, env = make_orchestrator(
        tmp_path, pipeline=FakePipeline([make_perception(1), make_perception(1)])
    )

    await orchestrator.run()

    assert orchestrator.paused_by == "question_did_not_advance"
    assert len(states_of(env["conn"])) == 1, "同一道题只应被处理一次"


async def test_action_failure_marks_failed_and_pauses(tmp_path: Path) -> None:
    actuator = FakeActuator(fail_at="apply")
    orchestrator, env = make_orchestrator(tmp_path, actuator=actuator)

    await orchestrator.run()

    assert states_of(env["conn"]) == [QuestionState.FAILED.value]
    assert orchestrator.paused_by == "action_failed"


async def test_submit_timeout_pauses_without_retry(tmp_path: Path) -> None:
    """提交只点一次；结果没出来就停下等人，**不重放**。"""
    verifier = FakeVerifier(ok=False)
    orchestrator, env = make_orchestrator(tmp_path, verifier=verifier)

    await orchestrator.run()

    assert env["actuator"].count("submit") == 1
    assert env["verifier"].calls == 1
    assert states_of(env["conn"]) == [QuestionState.FAILED.value]
    assert orchestrator.paused_by == "submit_timeout"


# --------------------------------------------------------------------------- #
# 半自动 / ⚠复核
# --------------------------------------------------------------------------- #
async def test_manual_mode_stops_at_pending_confirm(tmp_path: Path) -> None:
    orchestrator, env = make_orchestrator(tmp_path, cfg=RunConfig(auto_apply=False))
    task = asyncio.create_task(orchestrator.run())

    await wait_until(lambda: env["conn"].execute("SELECT * FROM task_item").fetchall() != [])
    await wait_until(lambda: orchestrator.paused_by == "needs_confirm")

    assert states_of(env["conn"]) == [QuestionState.PENDING_CONFIRM.value]
    assert env["actuator"].count("submit") == 0

    item_id = env["conn"].execute("SELECT item_id FROM task_item").fetchone()["item_id"]
    env["conn"].execute(
        "UPDATE task_item SET state = ? WHERE item_id = ?",
        (QuestionState.APPLIED.value, item_id),
    )
    env["conn"].commit()
    await orchestrator.resume()
    await task

    assert states_of(env["conn"]) == [QuestionState.VERIFIED.value]


async def test_confirmation_releases_the_run_and_it_continues(tmp_path: Path) -> None:
    """人工确认就是「放行」—— 不把运行闸放回去，半自动会「确认一次、停一次」（P8 实测踩到）。

    与上一条的区别：这里**不调用** ``orchestrator.resume()``，只写库（界面点确认就是
    这个效果）。运行必须自己继续走到第二题，而不是停在边界上退出。
    """
    pipeline = FakePipeline([make_perception(1), make_perception(2)])
    orchestrator, env = make_orchestrator(
        tmp_path, cfg=RunConfig(auto_apply=False), pipeline=pipeline
    )
    conn = env["conn"]

    def decide(state: QuestionState) -> int:
        cursor = conn.execute(
            "UPDATE task_item SET state = ? WHERE state = ?",
            (state.value, QuestionState.PENDING_CONFIRM.value),
        )
        conn.commit()
        return int(cursor.rowcount)

    task = asyncio.create_task(orchestrator.run())
    await wait_until(lambda: orchestrator.paused_by == "needs_confirm")
    assert decide(QuestionState.APPLIED) == 1

    # 关键断言：确认之后**又**停在第二题等人确认，说明闸门被放行了、循环继续了
    await wait_until(
        lambda: len(orchestrator.items) >= 2
        and QuestionState(orchestrator.items[1].state) is QuestionState.PENDING_CONFIRM
    )
    assert decide(QuestionState.SKIPPED) == 1
    await asyncio.wait_for(task, timeout=5)

    assert orchestrator.paused is False
    assert [QuestionState(item.state) for item in orchestrator.items] == [
        QuestionState.VERIFIED,
        QuestionState.SKIPPED,
    ]


async def test_rejecting_a_question_skips_it_without_submitting(tmp_path: Path) -> None:
    orchestrator, env = make_orchestrator(tmp_path, cfg=RunConfig(auto_apply=False))
    task = asyncio.create_task(orchestrator.run())

    await wait_until(lambda: orchestrator.paused_by == "needs_confirm")
    item_id = env["conn"].execute("SELECT item_id FROM task_item").fetchone()["item_id"]
    env["conn"].execute(
        "UPDATE task_item SET state = ? WHERE item_id = ?",
        (QuestionState.SKIPPED.value, item_id),
    )
    env["conn"].commit()
    await orchestrator.resume()
    await task

    assert states_of(env["conn"]) == [QuestionState.SKIPPED.value]
    assert env["actuator"].count("submit") == 0


async def test_review_flag_always_stops_even_in_auto_mode(tmp_path: Path) -> None:
    """T0-3：``⚠复核`` 必停，不受 auto_apply 影响。"""
    orchestrator, env = make_orchestrator(
        tmp_path, solver=FakeSolver(review=True), cfg=RunConfig(auto_apply=True)
    )
    task = asyncio.create_task(orchestrator.run())

    await wait_until(lambda: orchestrator.paused_by == "needs_confirm")

    assert states_of(env["conn"]) == [QuestionState.PENDING_CONFIRM.value]

    item_id = env["conn"].execute("SELECT item_id FROM task_item").fetchone()["item_id"]
    env["conn"].execute(
        "UPDATE task_item SET state = ? WHERE item_id = ?",
        (QuestionState.APPLIED.value, item_id),
    )
    env["conn"].commit()
    await orchestrator.resume()
    await task
    assert states_of(env["conn"]) == [QuestionState.VERIFIED.value]


async def test_empty_answer_is_never_submitted_even_if_confirmed(tmp_path: Path) -> None:
    """空作答（Provider 全挂）**即使人工点了确认也不许提交** —— 提交空答案比停下糟。"""
    orchestrator, env = make_orchestrator(
        tmp_path,
        solver=FakeSolver(review=True, labels=[]),
        cfg=RunConfig(auto_apply=True),
    )
    task = asyncio.create_task(orchestrator.run())
    await wait_until(lambda: orchestrator.paused_by == "needs_confirm")

    item_id = env["conn"].execute("SELECT item_id FROM task_item").fetchone()["item_id"]
    env["conn"].execute(
        "UPDATE task_item SET state = ? WHERE item_id = ?",
        (QuestionState.APPLIED.value, item_id),
    )
    env["conn"].commit()
    await orchestrator.resume()
    await asyncio.wait_for(task, timeout=5)

    assert env["actuator"].count("submit") == 0
    assert env["actuator"].count("select_option") == 0
    assert states_of(env["conn"]) == [QuestionState.FAILED.value]
    assert orchestrator.paused_by == "empty_answer"


async def test_resume_without_a_decision_does_not_count_as_approval(tmp_path: Path) -> None:
    """「恢复运行」不等于「同意一切」—— 没做决策就再等等。"""
    orchestrator, env = make_orchestrator(tmp_path, cfg=RunConfig(auto_apply=False))
    task = asyncio.create_task(orchestrator.run())

    await wait_until(lambda: orchestrator.paused_by == "needs_confirm")
    await orchestrator.resume()
    await asyncio.sleep(0.3)

    assert env["actuator"].count("submit") == 0
    assert states_of(env["conn"]) == [QuestionState.PENDING_CONFIRM.value]
    assert orchestrator.paused_by == "needs_confirm"

    await orchestrator.stop()
    await task


# --------------------------------------------------------------------------- #
# 落盘契约
# --------------------------------------------------------------------------- #
async def test_answer_without_geometry_pauses_instead_of_guessing(tmp_path: Path) -> None:
    """没有几何（模型没给过坐标）→ **停下**，绝不猜一个位置去点。

    v0.2.0 起几何只有一个来源：读题那一次读图。它不在，就等于「没有任何可点的
    目标」—— 猜出来的坐标会变成用户真实页面上的一次误点。
    """
    orchestrator, env = make_orchestrator(tmp_path, pipeline=FakePipeline(generate=1))
    orchestrator._vision_reads.clear()  # 模拟「这道题的几何丢了」

    await orchestrator.run()

    assert env["actuator"].count("select_option") == 0
    assert orchestrator.paused_by == "vision_geometry_missing"
    assert states_of(env["conn"]) == [QuestionState.FAILED.value]


async def test_paused_run_is_not_recorded_as_finished(tmp_path: Path) -> None:
    """「暂停等人」不能写成一跑完 —— 界面靠这个状态决定要不要提示人介入。"""
    orchestrator, env = make_orchestrator(tmp_path, actuator=FakeActuator(fail_at="apply"))

    await orchestrator.run()

    row = env["conn"].execute("SELECT status FROM run").fetchone()
    assert row["status"] == "paused:action_failed"


async def test_completed_run_is_recorded_as_finished(tmp_path: Path) -> None:
    orchestrator, env = make_orchestrator(tmp_path)

    await orchestrator.run()

    row = env["conn"].execute("SELECT status, finished_at FROM run").fetchone()
    assert row["status"] == "finished"
    assert row["finished_at"] is not None


def test_task_item_roundtrip_through_db(tmp_path: Path) -> None:
    """``state`` 一个列装两种状态机 —— 读回来必须还原成正确的枚举。"""
    conn = make_conn(tmp_path)
    item = TaskItem(
        item_id="vid-1",
        type=TaskType.VIDEO,
        vid="vid-1",
        state=MediaState.PLAYING,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.upsert_task_item(conn, "run-1", item)

    restored = db.load_task_items(conn, "run-1")
    assert restored[0].state.value == "playing"
    assert restored[0].type is TaskType.VIDEO


def test_upsert_preserves_identity_columns(tmp_path: Path) -> None:
    """续跑会反复写同一行：**状态列更新，身份列不动**。

    ``created_at`` 一动，``ORDER BY created_at`` 的题目顺序就会在续跑后错乱；
    用 ``INSERT OR REPLACE`` 更容易把整行不该动的东西一起清掉。
    """
    conn = make_conn(tmp_path)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    item = TaskItem(
        item_id="q1",
        type=TaskType.QUIZ,
        qid="q1",
        state=QuestionState.PENDING,
        created_at=created,
        updated_at=created,
    )
    db.upsert_task_item(conn, "run-1", item)

    # 续跑：同一行、状态前进、updated_at 变化
    item.state = QuestionState.PERCEIVED
    item.updated_at = datetime(2026, 1, 2, tzinfo=UTC)
    db.upsert_task_item(conn, "run-1", item)

    row = conn.execute("SELECT * FROM task_item WHERE item_id = 'q1'").fetchone()
    assert row["state"] == "perceived"
    assert row["run_id"] == "run-1"
    assert row["created_at"] == created.isoformat(), "created_at 不得被续跑改写"
    assert row["updated_at"] == item.updated_at.isoformat()
