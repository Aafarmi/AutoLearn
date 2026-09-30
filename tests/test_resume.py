"""M4-1 断点续跑（含 M4 闸门：``submitted`` 续跑不重复提交）。

M4 闸门口径原文是：「**``submitted`` 状态题续跑时不重复提交**（专项演练：
提交后杀进程，再续跑）」。这里的「杀进程」用**抛异常冲出编排层**来模拟 ——
效果等价：进程内存态全丢，只剩 SQLite 与留痕目录。

这些用例全部是内存替身，不依赖浏览器。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from core import db
from core.config import RunConfig
from core.enums import QuestionState, TaskType
from core.models import TaskItem
from core.orchestrator import Orchestrator, RunContext, RunDeps
from core.trace import RunLogger
from tests.orchestrator_helpers import (
    FakeActuator,
    FakeAdapter,
    FakePage,
    FakePipeline,
    FakeSolver,
    FakeVerifier,
    RecordingPacer,
    make_perception,
    make_question,
)
from tests.test_orchestrator import make_conn, seed_batch_geometry, states_of, wait_until


def build(
    tmp_path: Path,
    *,
    conn=None,
    pipeline: FakePipeline | None = None,
    actuator: FakeActuator | None = None,
    verifier: FakeVerifier | None = None,
    run_id: str = "run-1",
    **kwargs,
) -> tuple[Orchestrator, dict]:
    """在同一份 SQLite 上重建编排器 —— 等价于「重启进程后再续跑」。"""
    connection = conn if conn is not None else make_conn(tmp_path)
    actuator = actuator or FakeActuator()
    verifier = verifier or FakeVerifier()
    pacer = RecordingPacer()
    pipeline = pipeline or FakePipeline(generate=1)
    cfg_kwargs: dict = {"auto_apply": True}
    cfg_kwargs.update(kwargs)
    ctx = RunContext(
        run_id=run_id,
        cfg=RunConfig(**cfg_kwargs),
        started_at=datetime.now(UTC),
    )
    deps = RunDeps(
        page=FakePage(),
        adapter=FakeAdapter(),
        pipeline=pipeline,
        solver=FakeSolver(),
        run_logger=None,
        conn=connection,
        actuator_factory=lambda _item: actuator,
        verifier_factory=lambda _item: verifier,
        click_gap=pacer.click,
        submit_gap=pacer.submit,
        probe_timeout_s=0.1,
    )
    orchestrator = Orchestrator(ctx, deps=deps)
    # 假流水线绕过了「读题」那一步，几何要显式补上（v0.2.0 只有视觉一条作答路径）
    seed_batch_geometry(orchestrator, pipeline.total)
    return orchestrator, {
        "actuator": actuator,
        "verifier": verifier,
        "pipeline": pipeline,
        "conn": connection,
        "pacer": pacer,
    }


def seed_item(
    conn,
    *,
    qid: str,
    state: QuestionState,
    run_id: str = "run-1",
) -> None:
    """预置一条「上一次运行留下的」任务。

    ``item_id`` 必须与编排层一致（``<run_id>-<qid>``）—— ``task_item.item_id``
    是全局主键，编排层按这个格式造条目，认领时才接得上。
    """
    item = TaskItem(
        item_id=f"{run_id}-{qid}",
        type=TaskType.QUIZ,
        qid=qid,
        state=state,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.upsert_task_item(conn, run_id, item)


# --------------------------------------------------------------------------- #
# M4 闸门：提交后杀进程，续跑不重复提交
# --------------------------------------------------------------------------- #
async def test_kill_after_submit_then_resume_never_submits_again(tmp_path: Path) -> None:
    """专项演练：run 1 提交成功后**在结果回读前崩掉**，run 2 绝不重新点击。

    v0.2.0 的结果回读是**截图差分**，而差分基准（点提交之前那张图）活在提交那个
    进程的内存里。所以续跑遇到 ``submitted`` 时**判不了结果** —— 此时正确的处置是
    **如实保持 ``submitted``**（结果未知）：既不再点一次提交，也不把它伪造成
    ``verified``。人可以从留痕里看到「这一题提交过、结果未知」。
    """
    conn = make_conn(tmp_path)

    # —— run 1：提交成功，随后模拟进程被杀（校验器抛出）
    first, env1 = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(1)]),
        verifier=FakeVerifier(raise_once=True),
    )
    try:
        await first.run()
    except RuntimeError as exc:
        assert "杀死" in str(exc)
    else:  # pragma: no cover - 模拟必须真的炸掉
        raise AssertionError("模拟的进程崩溃没有生效")

    assert env1["actuator"].count("submit") == 1
    assert states_of(conn) == [QuestionState.SUBMITTED.value], "崩溃时状态应停在 submitted"

    # —— run 2：续跑。危险态**只回读、绝不重新点击**（没有基准就如实保持）
    second, env2 = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(1)]),
    )
    await second.run()

    assert env2["actuator"].count("submit") == 0, "submitted 续跑不得重复提交"
    assert env2["actuator"].count("select_option") == 0, "更不得重新点选项"
    assert env2["verifier"].calls == 0, "没有差分基准，回读也做不成"
    assert states_of(conn) == [QuestionState.SUBMITTED.value], (
        "结果未知就保持 submitted —— 绝不伪造成 verified"
    )


async def test_submitted_item_never_enters_the_apply_path(tmp_path: Path) -> None:
    """即使页面上还停在这道题，``submitted`` 也不允许再走一遍选选项。"""
    conn = make_conn(tmp_path)
    question = make_question(1)
    seed_item(conn, qid=question.qid, state=QuestionState.SUBMITTED)

    orchestrator, env = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(1)]),
    )
    await orchestrator.run()

    assert env["actuator"].count("select_option") == 0
    assert env["actuator"].count("submit") == 0
    assert states_of(conn) == [QuestionState.SUBMITTED.value]


# --------------------------------------------------------------------------- #
# 续跑不重做已完成的题
# --------------------------------------------------------------------------- #
async def test_finished_questions_are_fast_forwarded_not_redone(tmp_path: Path) -> None:
    """续跑时页面从第 1 题开始，已完成的三题只前进、不重做。"""
    conn = make_conn(tmp_path)
    questions = [make_question(i) for i in range(1, 4)]

    first, _ = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(i) for i in range(1, 4)]),
    )
    await first.run()
    assert states_of(conn) == [QuestionState.VERIFIED.value] * 3

    # —— 重启后再跑一遍：页面照样从第 1 题开始
    second, env = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(i) for i in range(1, 4)]),
    )
    await second.run()

    # 只允许「点下一题」前进 —— 绝不允许重新选选项、重新提交或重新回读
    assert env["actuator"].count("select_option") == 0
    assert env["actuator"].count("submit") == 0
    assert env["verifier"].calls == 0
    # 前两题的「下一题」是**照旧点掉**的（页面从第 1 题开始，不点就永远停在那里）
    assert env["actuator"].count("click") >= 2
    assert states_of(conn) == [QuestionState.VERIFIED.value] * 3
    assert [item.qid for item in second.items] == [q.qid for q in questions]


async def test_resume_continues_from_the_first_unfinished_question(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    questions = [make_question(i) for i in range(1, 4)]
    # 第 1 题已完成，第 2/3 题没跑过
    seed_item(conn, qid=questions[0].qid, state=QuestionState.VERIFIED)

    orchestrator, env = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(i) for i in range(1, 4)]),
    )
    await orchestrator.run()

    assert env["actuator"].count("select_option") == 2, "只补第 2、3 题"
    assert states_of(conn) == [
        QuestionState.VERIFIED.value,
        QuestionState.VERIFIED.value,
        QuestionState.VERIFIED.value,
    ]


async def test_attempts_are_counted_across_runs(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    first, _ = build(tmp_path, conn=conn, pipeline=FakePipeline([make_perception(1)]))
    await first.run()

    second, _ = build(tmp_path, conn=conn, pipeline=FakePipeline([make_perception(1)]))
    await second.run()

    row = conn.execute("SELECT attempts FROM task_item").fetchone()
    assert row["attempts"] == 2, "续跑要能看出这题被碰过几次"


# --------------------------------------------------------------------------- #
# 断点落在「已选好、还没提交」
# --------------------------------------------------------------------------- #
async def test_applied_item_with_click_evidence_goes_straight_to_submit(
    tmp_path: Path,
) -> None:
    """杀进程时若停在「选项已选好」，续跑应补提交而**不是**重新选一遍。"""
    conn = make_conn(tmp_path)
    question = make_question(1)
    seed_item(conn, qid=question.qid, state=QuestionState.APPLIED)

    logger = RunLogger("run-1", root=tmp_path / "logs")
    logger.save_json(
        f"run-1-{question.qid}",
        "action",
        {"kind": "select_option", "ok": True, "level_used": "l1_locator"},
    )

    actuator = FakeActuator()
    orchestrator, env = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(1)]),
        actuator=actuator,
    )
    orchestrator.deps.run_logger = logger
    await orchestrator.run()

    assert env["actuator"].count("select_option") == 0, "有落点证据就不该重选"
    assert env["actuator"].count("submit") == 1
    assert states_of(conn) == [QuestionState.VERIFIED.value]


async def test_applied_item_without_evidence_reselects_before_submitting(
    tmp_path: Path,
) -> None:
    """没有落点证据（例如人工刚确认完就崩了）→ 重新选一遍再提交。

    重新选是**幂等**的（P6：已经选中就不动手），比「什么都没选就提交」安全。
    """
    conn = make_conn(tmp_path)
    question = make_question(1)
    seed_item(conn, qid=question.qid, state=QuestionState.APPLIED)

    orchestrator, env = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(1)]),
    )
    await orchestrator.run()

    assert env["actuator"].count("select_option") == 1
    assert env["actuator"].count("submit") == 1
    assert states_of(conn) == [QuestionState.VERIFIED.value]


# --------------------------------------------------------------------------- #
# 半自动决策跨重启
# --------------------------------------------------------------------------- #
async def test_pending_confirm_survives_a_restart(tmp_path: Path) -> None:
    """待确认状态重启后仍然等人 —— 不能被当成「可以继续」。"""
    conn = make_conn(tmp_path)

    first, _env1 = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(1)]),
        auto_apply=False,
    )
    task1 = asyncio.create_task(first.run())
    await wait_until(lambda: first.paused_by == "needs_confirm")
    await first.stop()
    await task1

    assert states_of(conn) == [QuestionState.PENDING_CONFIRM.value]

    # —— 重启：新的编排器仍然要等人
    second, env2 = build(
        tmp_path,
        conn=conn,
        pipeline=FakePipeline([make_perception(1)]),
        auto_apply=False,
    )
    task2 = asyncio.create_task(second.run())
    await wait_until(lambda: second.paused_by == "needs_confirm")

    assert env2["actuator"].count("submit") == 0
    assert states_of(conn) == [QuestionState.PENDING_CONFIRM.value]

    # 人工确认 → 恢复
    item_id = conn.execute("SELECT item_id FROM task_item").fetchone()["item_id"]
    conn.execute(
        "UPDATE task_item SET state = ? WHERE item_id = ?",
        (QuestionState.APPLIED.value, item_id),
    )
    conn.commit()
    await second.resume()
    await asyncio.wait_for(task2, timeout=5)
    assert states_of(conn) == [QuestionState.VERIFIED.value]


async def test_stop_releases_a_waiting_run(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    orchestrator, _env = build(tmp_path, conn=conn, auto_apply=False)
    task = asyncio.create_task(orchestrator.run())
    await wait_until(lambda: orchestrator.paused_by == "needs_confirm")

    await orchestrator.stop()
    await asyncio.wait_for(task, timeout=3)

    assert orchestrator.stopped is True


# --------------------------------------------------------------------------- #
# 落盘契约
# --------------------------------------------------------------------------- #
async def test_resume_reads_back_the_run_config_from_db(tmp_path: Path) -> None:
    """``run`` 表里的配置是**重启后**还能重建现场的依据。"""
    conn = make_conn(tmp_path)
    first, _ = build(tmp_path, conn=conn, pipeline=FakePipeline([make_perception(1)]))
    await first.run()

    row = conn.execute("SELECT config_json FROM run").fetchone()
    payload = json.loads(row["config_json"])
    assert payload["auto_apply"] is True
    assert "api_key" not in row["config_json"], "配置里绝不允许出现密钥"
