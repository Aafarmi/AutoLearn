"""T0-8 任务栈：压栈 → 弹栈 → 恢复；``submitted`` 之外的中断都不能丢状态。"""

from __future__ import annotations

from core.models import VideoState
from core.tasks import SuspendFrame, TaskStack, make_frame_id


def _video_state(current_time: float = 42.5, episode_index: int = 3) -> VideoState:
    return VideoState(
        paused=False,
        ended=False,
        current_time=current_time,
        duration=300.0,
        episode_index=episode_index,
        episode_total=8,
        src="/media/ep3.wav",
    )


def _frame(parent: str, child: str, current_time: float = 42.5) -> SuspendFrame:
    return SuspendFrame(
        parent_item_id=parent,
        child_item_id=child,
        media_state_at_suspend=_video_state(current_time),
        reason="弹题打断",
    )


def test_lifo_order() -> None:
    stack = TaskStack()
    a, b, c = _frame("p", "q1"), _frame("p", "q2"), _frame("p", "q3")
    for frame in (a, b, c):
        stack.push(frame)
    assert stack.depth == 3
    assert stack.pop() is c
    assert stack.pop() is b
    assert stack.pop() is a
    assert stack.is_empty()


def test_peek_does_not_pop() -> None:
    stack = TaskStack()
    frame = _frame("p", "q1")
    stack.push(frame)
    assert stack.peek() is frame
    assert stack.depth == 1


def test_pop_empty_returns_none() -> None:
    assert TaskStack().pop() is None
    assert TaskStack().peek() is None


def test_snapshot_is_a_copy() -> None:
    stack = TaskStack()
    stack.push(_frame("p", "q1"))
    snap = stack.snapshot()
    snap.append(_frame("p", "q2"))
    assert stack.depth == 1, "snapshot 修改不应影响栈本体"


def test_restore_preserves_stack_top_priority() -> None:
    """进程重启后 ``restore()`` 回来，栈顶仍是最后压入的那一帧。"""
    original = TaskStack()
    deep, shallow = _frame("p", "q1"), _frame("p", "q2")
    original.push(deep)
    original.push(shallow)
    snapshot = original.snapshot()

    revived = TaskStack()
    revived.restore(snapshot)
    assert revived.depth == 2
    assert revived.peek() is shallow
    assert revived.pop() is shallow
    assert revived.pop() is deep


def test_video_interrupted_by_quiz_then_resumed() -> None:
    """T0 验收场景：视频播放中弹题 → 弹题完成 → 恢复原视频。"""
    stack = TaskStack()
    video_task_id = "vid-task-1"
    quiz_task_id = "quiz-task-9"

    before = _video_state(current_time=42.5)
    stack.push(
        SuspendFrame(
            parent_item_id=video_task_id,
            child_item_id=quiz_task_id,
            media_state_at_suspend=before,
            reason="弹题弹窗出现",
        )
    )

    # 处理弹题期间栈一直持有视频状态
    assert stack.depth == 1
    assert stack.peek().child_item_id == quiz_task_id

    frame = stack.pop()
    assert frame is not None
    assert frame.parent_item_id == video_task_id
    assert stack.is_empty()

    # 恢复依据：挂起时的位置必须原样带回来（偏差 ≤2s 由 Verifier 判）
    after = frame.media_state_at_suspend
    assert abs(after.current_time - before.current_time) <= 2.0
    assert after.current_time < after.duration
    assert after.episode_index == before.episode_index


def test_nested_interrupts_stack_deeper() -> None:
    """弹题里再弹题：两层都要能原样退回来。"""
    stack = TaskStack()
    outer, inner = _frame("video", "quiz-1", 10.0), _frame("quiz-1", "quiz-2", 0.0)
    stack.push(outer)
    stack.push(inner)
    assert stack.depth == 2
    assert stack.pop() is inner
    assert stack.pop() is outer


def test_store_hook_is_called_on_mutation() -> None:
    """落盘钩子必须在每次变更后被触发（P8 接 SQLite 用的就是这个口子）。"""

    class RecordingStore:
        def __init__(self) -> None:
            self.saves: list[int] = []
            self.frames: list[SuspendFrame] = []

        def save(self, frames: list[SuspendFrame]) -> None:
            self.saves.append(len(frames))
            self.frames = list(frames)

        def load(self) -> list[SuspendFrame]:
            return list(self.frames)

    store = RecordingStore()
    stack = TaskStack(store=store)
    stack.push(_frame("p", "q1"))
    stack.pop()
    stack.clear()
    assert store.saves == [1, 0, 0]


def test_frame_ids_are_unique() -> None:
    ids = {make_frame_id() for _ in range(200)}
    assert len(ids) == 200
