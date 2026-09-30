"""P8 / M5-2：挂起栈的 SQLite 落盘（``core.db.DbTaskStackStore``）。

这一条是「杀进程续跑栈顶优先」的地基：库读回来的顺序必须与压栈顺序一致
（**列表尾即栈顶**），否则恢复出来的上下文是错的，而且错得不明显。
"""

from __future__ import annotations

from pathlib import Path

from core import db
from core.models import VideoState
from core.tasks import SuspendFrame, TaskStack


def _frame(parent: str, child: str, current_time: float) -> SuspendFrame:
    return SuspendFrame(
        parent_item_id=parent,
        child_item_id=child,
        media_state_at_suspend=VideoState(
            paused=True,
            ended=False,
            current_time=current_time,
            duration=42.0,
            episode_index=2,
            episode_total=6,
            src="/media/ep02.wav",
        ),
        reason="quiz_interrupt",
    )


def test_store_round_trip_keeps_stack_top(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "stack.db")
    try:
        store = db.make_task_stack_store(conn, "run-1")

        stack = TaskStack(store=store)
        deep, shallow = _frame("video", "q1", 3.0), _frame("video", "q2", 7.5)
        stack.push(deep)
        stack.push(shallow)

        revived = TaskStack()
        revived.attach_store(db.make_task_stack_store(conn, "run-1"))
        revived.restore(db.load_suspend_frames(conn, "run-1"))

        assert revived.depth == 2
        top = revived.peek()
        assert top is not None and top.frame_id == shallow.frame_id
        assert revived.pop().frame_id == shallow.frame_id  # type: ignore[union-attr]
        assert revived.pop().frame_id == deep.frame_id  # type: ignore[union-attr]
        assert revived.is_empty()
    finally:
        conn.close()


def test_push_and_pop_are_persisted_immediately(tmp_path: Path) -> None:
    """每次压 / 弹都落盘 —— 进程被强杀时库里的就是最后一次操作的现场。"""
    conn = db.init_db(tmp_path / "stack.db")
    try:
        store = db.make_task_stack_store(conn, "run-1")
        stack = TaskStack(store=store)

        stack.push(_frame("video", "q1", 1.0))
        assert len(db.load_suspend_frames(conn, "run-1")) == 1

        stack.pop()
        assert db.load_suspend_frames(conn, "run-1") == []
    finally:
        conn.close()


def test_suspend_state_survives_the_json_round_trip(tmp_path: Path) -> None:
    """挂起时的媒体态必须**逐字段**回来 —— 「恢复位置连续 ≤2s」靠的就是它。"""
    conn = db.init_db(tmp_path / "stack.db")
    try:
        store = db.make_task_stack_store(conn, "run-1")
        stack = TaskStack(store=store)
        stack.push(_frame("video", "q1", 12.5))

        frame = db.load_suspend_frames(conn, "run-1")[0]
        assert frame.media_state_at_suspend.current_time == 12.5
        assert frame.media_state_at_suspend.paused is True
        assert frame.media_state_at_suspend.episode_index == 2
        assert frame.media_state_at_suspend.duration == 42.0
        assert frame.reason == "quiz_interrupt"
    finally:
        conn.close()


def test_runs_do_not_share_frames(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "stack.db")
    try:
        TaskStack(store=db.make_task_stack_store(conn, "run-a")).push(_frame("v", "q", 1.0))
        TaskStack(store=db.make_task_stack_store(conn, "run-b")).push(_frame("v", "q", 2.0))

        assert len(db.load_suspend_frames(conn, "run-a")) == 1
        assert len(db.load_suspend_frames(conn, "run-b")) == 1
        assert db.load_suspend_frames(conn, "run-a")[0].media_state_at_suspend.current_time == 1.0
    finally:
        conn.close()


def test_stack_without_store_stays_in_memory(tmp_path: Path) -> None:
    """没挂后端时不许碰库（P0 验收明确允许内存实现）。"""
    conn = db.init_db(tmp_path / "stack.db")
    try:
        stack = TaskStack()
        stack.push(_frame("v", "q", 1.0))
        stack.pop()
        assert db.load_suspend_frames(conn, "run-1") == []
    finally:
        conn.close()
