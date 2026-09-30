"""任务类型与中断模型（T0-8 / M5-1 / M5-2）。

任务类型枚举 ``video`` / ``quiz``；任务栈用于承载「播放中被弹题打断」这一
**嵌套中断**：弹题到来 → 显式暂停媒体 → 压栈 → 处理弹题 → 弹栈 → 回读
``currentTime`` 未越界 → 恢复播放。

P0 阶段 ``TaskStack`` 为**内存实现**（P0 验收明确允许）；SQLite 落盘属于 P8，
届时在 :meth:`TaskStack.attach_store` 挂上持久化后端即可，接口不变。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.enums import MediaState, TaskType
from core.models import Episode, TaskItem, VideoState

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，避免 config -> tasks 反向依赖
    from core.config import RunConfig

__all__ = [
    "SuspendFrame",
    "TaskStack",
    "TaskStackStore",
    "TaskType",
    "build_task_sequence",
    "make_frame_id",
]


def make_frame_id() -> str:
    """压栈帧的唯一 ID。"""
    return uuid.uuid4().hex[:16]


class SuspendFrame(BaseModel):
    """一次「挂起 → 恢复」的完整快照（M5-2）。"""

    model_config = ConfigDict(extra="forbid")

    frame_id: str = Field(default_factory=make_frame_id)
    parent_item_id: str
    child_item_id: str
    media_state_at_suspend: VideoState
    reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskStackStore(Protocol):
    """栈的持久化后端（P8 提供 SQLite 实现）。"""

    def save(self, frames: list[SuspendFrame]) -> None: ...

    def load(self) -> list[SuspendFrame]: ...


class TaskStack:
    """后进先出的挂起栈。**栈顶优先**恢复。

    进程重启后续跑时，``restore()`` 回来的顺序必须保持原样：列表尾即栈顶。
    """

    def __init__(self, store: TaskStackStore | None = None) -> None:
        self._frames: list[SuspendFrame] = []
        self._store: TaskStackStore | None = store

    # -- 基本操作 ---------------------------------------------------------- #
    def push(self, frame: SuspendFrame) -> None:
        self._frames.append(frame)
        self._flush()

    def pop(self) -> SuspendFrame | None:
        if not self._frames:
            return None
        frame = self._frames.pop()
        self._flush()
        return frame

    def peek(self) -> SuspendFrame | None:
        return self._frames[-1] if self._frames else None

    @property
    def depth(self) -> int:
        return len(self._frames)

    def is_empty(self) -> bool:
        return not self._frames

    # -- 快照 / 恢复 ------------------------------------------------------- #
    def snapshot(self) -> list[SuspendFrame]:
        """返回栈内容（列表尾为栈顶），供落盘。"""
        return list(self._frames)

    def restore(self, frames: list[SuspendFrame]) -> None:
        """用快照重建栈，**保持栈顶优先级**。"""
        self._frames = list(frames)
        self._flush()

    def clear(self) -> None:
        self._frames.clear()
        self._flush()

    def attach_store(self, store: TaskStackStore | None) -> None:
        """挂上 / 摘掉持久化后端（P8）。"""
        self._store = store

    def _flush(self) -> None:
        if self._store is not None:
            self._store.save(self._frames)


def build_task_sequence(
    cfg: RunConfig,
    *,
    run_id: str = "",
    episodes: Sequence[Episode] | None = None,
) -> list[TaskItem]:
    """按运行配置组装题目 + 分集的混合任务序列（M5-1）。

    ``cfg.task_sequence`` 是一个**有序的阶段表**（``[video, quiz]`` 之类）：

    - ``video`` 步：把 ``episodes``（页面事实，由 ``read_episode_catalog`` 读出）
      展开成「一集一条」的 :class:`~core.models.TaskItem`（``type=video``）；
    - ``quiz`` 步：**不入序列** —— 题目身份 ``qid`` 在读过页面前根本不可知，
      硬凑只会造出一批没有身份的占位条目。题目由编排层按 ``qid`` 动态认领
      （见 ``Orchestrator._claim_item``）；网课场景里它则以「弹题中断」的形式
      作为嵌套子任务出现。

    ``item_id`` 一律带 ``run_id`` 前缀：``task_item.item_id`` 是**全局主键**，
    而 ``vid`` 是课程身份 —— 同一门课重跑一次若沿用 ``vid`` 当主键，就会重演
    P7 那个「新运行一条任务都没有」的缺陷（见验收报告 §六·缺陷 2）。

    ``run_id`` 为空时退化为裸 ``vid``（仅供单测与临时组装使用）。
    """
    sequence: list[TaskItem] = []
    now = datetime.now(UTC)
    for task_type in cfg.task_sequence:
        if task_type != TaskType.VIDEO:
            continue
        for episode in episodes or ():
            # ``created_at`` 逐条递增：库是按 ``(created_at, item_id)`` 读回的，
            # 同一微秒内的条目顺序会退化成 ``item_id`` 的字典序 —— 分集顺序就乱了
            # （界面的「第几集」正是按条目顺序推的，见 ``ui/store.py``）。
            stamp = now + timedelta(microseconds=len(sequence))
            sequence.append(
                TaskItem(
                    item_id=f"{run_id}-{episode.vid}" if run_id else episode.vid,
                    type=TaskType.VIDEO,
                    vid=episode.vid,
                    state=MediaState.IDLE,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
    return sequence
