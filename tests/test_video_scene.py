"""P8 / M5-2 ~ M5-5：网课场景编排（分集推进 + 嵌套中断 + 断点续跑）。

**全部用内存替身**，不碰浏览器。理由与 P7 同：这一层最该天天跑的几条 ——
「弹题时有没有显式暂停」「ended 与弹题同刻谁优先」「续跑会不会重播已完成的集」
「栈顶会不会恢复出来」—— 与页面无关；挂在真浏览器上就等于「今天没起靶场就不验了」。

真浏览器只负责证明「在真页面上确实有效」（``scripts/run_course.py`` + 真机验收）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from core import db
from core.config import RunConfig
from core.enums import MediaState, ProbeName, QuestionState, TaskType
from core.events import Event
from core.models import TaskItem, VideoState
from core.orchestrator import Orchestrator, RunContext, RunDeps
from core.tasks import SuspendFrame
from core.trace import RunLogger
from tests.orchestrator_helpers import (
    FakeActuator,
    FakeAdapter,
    FakeMediaWorld,
    FakePage,
    FakePipeline,
    FakeSolver,
    FakeVerifier,
    RecordingBus,
    RecordingPacer,
    install_media,
    make_episode,
    make_perception,
    make_question,
    seed_vision_geometry,
)

RUN_ID = "run-video"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _cfg(**overrides) -> RunConfig:
    base: dict = {"task_sequence": [TaskType.VIDEO], "auto_apply": True}
    base.update(overrides)
    return RunConfig(**base)


def build(
    tmp_path: Path,
    *,
    world: FakeMediaWorld,
    monkeypatch,
    cfg: RunConfig | None = None,
    pipeline: FakePipeline | None = None,
    actuator: FakeActuator | None = None,
    verifier: FakeVerifier | None = None,
    conn=None,
    run_id: str = RUN_ID,
    bus: RecordingBus | None = None,
) -> tuple[Orchestrator, dict]:
    """装一台「网课场景」的编排器。返回 ``(orchestrator, parts)``。"""
    install_media(monkeypatch, world)
    connection = conn if conn is not None else db.init_db(tmp_path / "autolearn.db")
    pacer = RecordingPacer()
    actuator = actuator if actuator is not None else FakeActuator(world=world)
    verifier = verifier or FakeVerifier()
    pipeline = pipeline or FakePipeline()
    bus = bus or RecordingBus()
    logger = RunLogger(run_id, root=tmp_path / "logs")

    ctx = RunContext(run_id=run_id, cfg=cfg or _cfg(), started_at=datetime.now(UTC))
    deps = RunDeps(
        page=FakePage(),
        adapter=FakeAdapter(),
        pipeline=pipeline,
        solver=FakeSolver(),
        run_logger=logger,
        bus=bus,
        conn=connection,
        actuator_factory=lambda _item: actuator,
        verifier_factory=lambda _item: verifier,
        click_gap=pacer.click,
        submit_gap=pacer.submit,
        probe_timeout_s=0.01,
        media_watch_timeout_s=0.05,
    )
    parts = {
        "actuator": actuator,
        "verifier": verifier,
        "pipeline": pipeline,
        "conn": connection,
        "bus": bus,
        "pacer": pacer,
        "logger": logger,
    }
    orchestrator = Orchestrator(ctx, deps=deps)
    # 弹题子任务是**题目**，所以它也要有几何：v0.2.0 起没有几何就一个坐标都不点。
    # 几何本该来自「读题那一次」，而这里的替身流水线直接给出一整道题、绕过了读题。
    for result in pipeline.results:
        if result.question is not None:
            seed_vision_geometry(orchestrator, result.question)
    return orchestrator, parts


def _video_item(vid: str, state: MediaState, *, suspended: bool = False) -> TaskItem:
    now = datetime.now(UTC)
    return TaskItem(
        item_id=f"{RUN_ID}-{vid}",
        type=TaskType.VIDEO,
        vid=vid,
        state=state,
        attempts=1,
        suspended=suspended,
        created_at=now,
        updated_at=now,
    )


def _quiz_item(qid: str, state: QuestionState) -> TaskItem:
    now = datetime.now(UTC)
    return TaskItem(
        item_id=f"{RUN_ID}-{qid}",
        type=TaskType.QUIZ,
        qid=qid,
        state=state,
        attempts=1,
        created_at=now,
        updated_at=now,
    )


def _states(conn, run_id: str = RUN_ID) -> dict[str, str]:
    return {item.item_id: str(item.state) for item in db.load_task_items(conn, run_id)}


# --------------------------------------------------------------------------- #
# 连续播完全部分集（M5-4 / M5-5）
# --------------------------------------------------------------------------- #
async def test_plays_every_episode_in_order(tmp_path: Path, monkeypatch) -> None:
    world = FakeMediaWorld([make_episode(1), make_episode(2), make_episode(3)])
    orch, parts = build(tmp_path, world=world, monkeypatch=monkeypatch)

    await orch.run()

    assert orch.stopped is False and orch.paused is False
    assert parts["actuator"].count("play_media") == 3
    assert parts["actuator"].count("next_episode") == 2  # 集与集之间才需要切
    assert set(_states(parts["conn"]).values()) == {MediaState.ENDED.value}
    assert world.current_index == 3
    assert world.outcome_calls == 3


async def test_finished_episodes_are_not_replayed(tmp_path: Path, monkeypatch) -> None:
    """断点续跑：已完成的分集只前进、不重播（M5-5）。"""
    conn = db.init_db(tmp_path / "autolearn.db")
    db.upsert_task_item(conn, RUN_ID, _video_item("vid01", MediaState.ENDED))
    db.upsert_task_item(conn, RUN_ID, _video_item("vid02", MediaState.ENDED))
    world = FakeMediaWorld([make_episode(1), make_episode(2), make_episode(3)])
    orch, parts = build(tmp_path, world=world, monkeypatch=monkeypatch, conn=conn)

    await orch.run()

    assert parts["actuator"].count("play_media") == 1, "只有第 3 集需要真的播"
    assert parts["actuator"].count("next_episode") == 2, "第 1、2 集靠连点快进跳过"
    states = _states(conn)
    assert states[f"{RUN_ID}-vid03"] == MediaState.ENDED.value
    assert states[f"{RUN_ID}-vid01"] == MediaState.ENDED.value


async def test_empty_catalog_pauses_instead_of_silently_finishing(
    tmp_path: Path, monkeypatch
) -> None:
    world = FakeMediaWorld([])
    orch, _ = build(tmp_path, world=world, monkeypatch=monkeypatch)

    await orch.run()

    assert orch.paused is True
    assert orch.paused_by == "media_unavailable"


# --------------------------------------------------------------------------- #
# 弹题打断 → 显式暂停 → 压栈 → 处理 → 弹栈 → 恢复（M5-2 / M5-3）
# --------------------------------------------------------------------------- #
async def test_interrupt_pauses_media_before_answering(tmp_path: Path, monkeypatch) -> None:
    world = FakeMediaWorld([make_episode(1)], outcomes=["interrupt", "ended"])
    pipeline = FakePipeline(results=[make_perception(21)])
    orch, parts = build(tmp_path, world=world, monkeypatch=monkeypatch, pipeline=pipeline)

    await orch.run()

    order = parts["actuator"].order()
    assert "pause_media" in order, "弹题到来时必须**显式**暂停（弹窗不会暂停视频）"
    assert order.index("pause_media") < order.index("select_option"), "先暂停，再动题目"

    assert world.outcome_calls == 2
    assert parts["actuator"].count("play_media") == 2, "弹题处理完要恢复播放"

    assert orch.stack.is_empty()
    assert db.load_suspend_frames(parts["conn"], RUN_ID) == []
    assert _states(parts["conn"])[f"{RUN_ID}-vid01"] == MediaState.ENDED.value


async def test_interrupt_pushes_and_pops_with_events(tmp_path: Path, monkeypatch) -> None:
    world = FakeMediaWorld([make_episode(1)], outcomes=["interrupt", "ended"])
    pipeline = FakePipeline(results=[make_perception(21)])
    bus = RecordingBus()
    orch, parts = build(
        tmp_path, world=world, monkeypatch=monkeypatch, pipeline=pipeline, bus=bus
    )

    await orch.run()

    names = bus.names()
    assert Event.MEDIA_INTERRUPT_DETECTED in names
    pushed_at = bus.index_of(Event.STACK_PUSHED)
    popped_at = bus.index_of(Event.STACK_POPPED)
    assert pushed_at is not None and popped_at is not None
    assert pushed_at < popped_at

    pushed = next(p for n, p in bus.events if n == Event.STACK_PUSHED)
    popped = next(p for n, p in bus.events if n == Event.STACK_POPPED)
    assert pushed["parent_item_id"] == f"{RUN_ID}-vid01"
    assert pushed["depth"] == 1
    assert popped["depth"] == 0
    # 弹题子任务被真的做完（复用题目主循环那条路）
    assert _states(parts["conn"])[pushed["child_item_id"]] == QuestionState.VERIFIED.value


async def test_media_state_is_recorded_through_the_interrupt(
    tmp_path: Path, monkeypatch
) -> None:
    """媒体态切换要留痕（M5 验收：媒体态切换有记录）。"""
    world = FakeMediaWorld([make_episode(1)], outcomes=["interrupt", "ended"])
    pipeline = FakePipeline(results=[make_perception(21)])
    bus = RecordingBus()
    orch, _ = build(
        tmp_path, world=world, monkeypatch=monkeypatch, pipeline=pipeline, bus=bus
    )

    await orch.run()

    seen = [payload["to"] for name, payload in bus.events if name == Event.MEDIA_STATE_CHANGED]
    assert seen[0] == MediaState.PLAYING.value
    assert MediaState.INTERRUPTED.value in seen
    assert MediaState.RESUMED.value in seen
    assert seen[-1] == MediaState.ENDED.value


async def test_ended_wins_over_interrupt_at_the_same_moment(
    tmp_path: Path, monkeypatch
) -> None:
    """``?interrupt_at=end``：弹题与 ended 同刻 —— 该集视为已完成，**不恢复播放**。"""
    world = FakeMediaWorld([make_episode(1)], outcomes=["interrupt"], finish_after=1)
    pipeline = FakePipeline(results=[make_perception(21)])
    orch, parts = build(tmp_path, world=world, monkeypatch=monkeypatch, pipeline=pipeline)

    await orch.run()

    assert parts["actuator"].count("play_media") == 1, "不再恢复播放"
    assert parts["actuator"].count("pause_media") == 1
    assert orch.stack.is_empty()
    assert _states(parts["conn"])[f"{RUN_ID}-vid01"] == MediaState.ENDED.value


async def test_resume_drift_pauses_instead_of_playing_on(tmp_path: Path, monkeypatch) -> None:
    """恢复位置越界（挂起期间没真的停住）→ 暂停留档，不硬续播。"""
    world = FakeMediaWorld([make_episode(1)], outcomes=["interrupt", "ended"])
    pipeline = FakePipeline(results=[make_perception(21)])
    orch, _ = build(
        tmp_path,
        world=world,
        monkeypatch=monkeypatch,
        pipeline=pipeline,
        verifier=FakeVerifier(resume_ok=False),
    )

    await orch.run()

    assert orch.paused is True
    assert orch.paused_by == "resume_drift"


async def test_progress_assertion_failure_pauses(tmp_path: Path, monkeypatch) -> None:
    world = FakeMediaWorld([make_episode(1)])
    orch, parts = build(
        tmp_path,
        world=world,
        monkeypatch=monkeypatch,
        verifier=FakeVerifier(playing_ok=False),
    )

    await orch.run()

    assert orch.paused_by == "media_stalled"
    assert parts["actuator"].count("play_media") == 1


async def test_play_failure_pauses_without_retry(tmp_path: Path, monkeypatch) -> None:
    """媒体动作失败也不重试 —— 停下留档，让人看（与提交同一条纪律）。"""
    world = FakeMediaWorld([make_episode(1)])
    orch, _ = build(
        tmp_path,
        world=world,
        monkeypatch=monkeypatch,
        actuator=FakeActuator(fail_at="play", world=world),
    )

    await orch.run()

    assert orch.paused_by == "media_failed"
    assert world.paused is True


async def test_pause_failure_does_not_answer_the_quiz(tmp_path: Path, monkeypatch) -> None:
    """暂停没成功就**不许**去处理弹题：视频还在跑，位置校验必然失败。"""
    world = FakeMediaWorld([make_episode(1)], outcomes=["interrupt", "ended"])
    pipeline = FakePipeline(results=[make_perception(21)])
    orch, parts = build(
        tmp_path,
        world=world,
        monkeypatch=monkeypatch,
        pipeline=pipeline,
        actuator=FakeActuator(fail_at="pause", world=world),
    )

    await orch.run()

    assert orch.paused_by == "media_pause_failed"
    assert parts["actuator"].count("select_option") == 0


async def test_stale_suspend_frames_are_dropped_on_restore(
    tmp_path: Path, monkeypatch
) -> None:
    """过期的挂起帧必须丢掉（真机实测踩到）。

    帧只在「弹题处理到一半时进程退出」时才有价值。父分集已经播完、子任务已经了结，
    都说明这段上下文早就结束了 —— 留着它会让栈深虚高，还会把一段早已结束的位置
    当成断点回填（实测表现为「第 1 集末次位置被写成 0.0」）。
    """
    conn = db.init_db(tmp_path / "autolearn.db")
    question = make_question(21)

    db.upsert_task_item(conn, RUN_ID, _video_item("vid01", MediaState.ENDED))
    db.upsert_task_item(conn, RUN_ID, _video_item("vid02", MediaState.IDLE))
    db.upsert_task_item(conn, RUN_ID, _quiz_item(question.qid, QuestionState.VERIFIED))
    db.save_suspend_frames(
        conn,
        RUN_ID,
        [
            SuspendFrame(
                parent_item_id=f"{RUN_ID}-vid01",  # 父集已经播完了
                child_item_id=f"{RUN_ID}-{question.qid}",
                media_state_at_suspend=VideoState(
                    paused=True,
                    ended=True,
                    current_time=10.0,
                    duration=10.0,
                    episode_index=1,
                    episode_total=2,
                ),
                reason="quiz_interrupt",
            )
        ],
    )

    world = FakeMediaWorld([make_episode(1, duration=10.0), make_episode(2, duration=10.0)])
    orch, parts = build(tmp_path, world=world, monkeypatch=monkeypatch, conn=conn)

    await orch.run()

    assert orch.stack.is_empty()
    assert db.load_suspend_frames(conn, RUN_ID) == []
    # 已播完的第 1 集不被重播，也不会被「paused 重建」改掉位置
    assert parts["actuator"].count("play_media") == 1
    assert db.load_media_position(conn, "vid01", 1) is None


async def test_replayed_interrupt_for_an_answered_quiz_pauses(
    tmp_path: Path, monkeypatch
) -> None:
    """弹窗重现、但这道题已经了结 → **停下留档**，不重做、也不静默略过。

    真机上此时弹窗仍盖着播放按钮，硬着头皮恢复播放只会一路降级到超时。
    """
    conn = db.init_db(tmp_path / "autolearn.db")
    question = make_question(21)
    db.upsert_task_item(conn, RUN_ID, _quiz_item(question.qid, QuestionState.VERIFIED))

    world = FakeMediaWorld([make_episode(1)], outcomes=["interrupt"], finish_after=1)
    pipeline = FakePipeline(results=[make_perception(21, question=question)])
    orch, parts = build(
        tmp_path, world=world, monkeypatch=monkeypatch, conn=conn, pipeline=pipeline
    )

    await orch.run()

    assert orch.paused_by == "interrupt_already_answered"
    assert parts["actuator"].count("select_option") == 0, "已了结的题不许重做"
    assert parts["actuator"].count("submit") == 0, "更不许重提交"
    assert orch.stack.is_empty(), "这一帧根本没压进去"
    assert parts["actuator"].count("pause_media") == 1, "暂停仍然先做（挂起纪律）"


# --------------------------------------------------------------------------- #
# 杀进程续跑：栈顶优先 + 视频以 paused 重建（M5-2）
# --------------------------------------------------------------------------- #
async def test_restart_rebuilds_suspended_video_paused_and_reuses_the_frame(
    tmp_path: Path, monkeypatch
) -> None:
    conn = db.init_db(tmp_path / "autolearn.db")
    question = make_question(21)

    db.upsert_task_item(conn, RUN_ID, _video_item("vid01", MediaState.ENDED))
    db.upsert_task_item(conn, RUN_ID, _video_item("vid02", MediaState.INTERRUPTED, suspended=True))
    db.upsert_task_item(conn, RUN_ID, _quiz_item(question.qid, QuestionState.SOLVED))
    db.save_suspend_frames(
        conn,
        RUN_ID,
        [
            SuspendFrame(
                parent_item_id=f"{RUN_ID}-vid02",
                child_item_id=f"{RUN_ID}-{question.qid}",
                media_state_at_suspend=VideoState(
                    paused=True,
                    ended=False,
                    current_time=4.0,
                    duration=30.0,
                    episode_index=2,
                    episode_total=2,
                    src="/media/ep02.wav",
                ),
                reason="quiz_interrupt",
            )
        ],
    )

    world = FakeMediaWorld([make_episode(1), make_episode(2)], outcomes=["interrupt", "ended"])
    pipeline = FakePipeline(results=[make_perception(21, question=question)])
    bus = RecordingBus()
    orch, parts = build(
        tmp_path, world=world, monkeypatch=monkeypatch, conn=conn, pipeline=pipeline, bus=bus
    )

    await orch.run()

    # 1) 视频以 paused 态重建：先落位置、再 seek 回去，然后才播
    order = parts["actuator"].order()
    assert order.count("seek_media") == 1
    assert order.index("seek_media") < order.index("play_media")
    assert parts["actuator"].calls[order.index("seek_media")][1] == "media:seek=4"

    rebuild = [
        payload["to"]
        for name, payload in bus.events
        if name == Event.MEDIA_STATE_CHANGED and payload["item_id"] == f"{RUN_ID}-vid02"
    ]
    assert rebuild[0] == MediaState.PAUSED.value, "重建必须是 paused 态（不自动续播）"
    assert MediaState.PLAYING.value in rebuild[1:], "真正的播放交给正常观看流程"

    # 2) 栈顶上下文被**复用**而不是叠一层：跑完栈是空的
    assert orch.stack.is_empty()
    assert db.load_suspend_frames(conn, RUN_ID) == []

    # 3) 该集播完、弹题也做完
    states = _states(conn)
    assert states[f"{RUN_ID}-vid02"] == MediaState.ENDED.value
    assert states[f"{RUN_ID}-{question.qid}"] == QuestionState.VERIFIED.value


async def test_resume_from_saved_position_when_episode_unfinished(
    tmp_path: Path, monkeypatch
) -> None:
    """未播完的集从落盘位置续上（M5-5：记录 vid + 已播放位置）。"""
    conn = db.init_db(tmp_path / "autolearn.db")
    db.upsert_task_item(conn, RUN_ID, _video_item("vid01", MediaState.ENDED))
    db.upsert_task_item(conn, RUN_ID, _video_item("vid02", MediaState.PAUSED))
    db.save_media_position(conn, vid="vid02", episode_index=2, last_position=6.5)

    world = FakeMediaWorld([make_episode(1), make_episode(2)])
    orch, parts = build(tmp_path, world=world, monkeypatch=monkeypatch, conn=conn)

    await orch.run()

    order = parts["actuator"].order()
    assert "seek_media" in order
    assert parts["actuator"].calls[order.index("seek_media")][1] == "media:seek=6.5"
    assert _states(conn)[f"{RUN_ID}-vid02"] == MediaState.ENDED.value


async def test_episode_that_is_already_at_the_end_is_marked_ended(
    tmp_path: Path, monkeypatch
) -> None:
    """存档位置已经贴着片尾：直接判完成，不必再播一遍。"""
    conn = db.init_db(tmp_path / "autolearn.db")
    db.upsert_task_item(conn, RUN_ID, _video_item("vid01", MediaState.PAUSED))
    db.save_media_position(conn, vid="vid01", episode_index=1, last_position=30.0)

    world = FakeMediaWorld([make_episode(1, duration=30.0)])
    orch, parts = build(tmp_path, world=world, monkeypatch=monkeypatch, conn=conn)

    await orch.run()

    assert parts["actuator"].count("play_media") == 0
    assert _states(conn)[f"{RUN_ID}-vid01"] == MediaState.ENDED.value


# --------------------------------------------------------------------------- #
# 留痕（M4-3 的网课侧）
# --------------------------------------------------------------------------- #
async def test_artifacts_are_written_for_video_items(tmp_path: Path, monkeypatch) -> None:
    world = FakeMediaWorld([make_episode(1)], outcomes=["interrupt", "ended"])
    pipeline = FakePipeline(results=[make_perception(21)])
    orch, parts = build(tmp_path, world=world, monkeypatch=monkeypatch, pipeline=pipeline)

    await orch.run()

    video_dir = parts["logger"].item_dir(f"{RUN_ID}-vid01")
    assert (video_dir / "perception.json").is_file(), "界面靠它显示「第几集」"
    assert (video_dir / "action.json").is_file()
    assert (video_dir / "verify.json").is_file()
    assert (video_dir / "before.png").is_file()

    payload = json.loads((video_dir / "perception.json").read_text(encoding="utf-8"))
    assert payload["video_state"]["episode_index"] == 1
    assert payload["channel_used"] == ProbeName.MEDIA.value
