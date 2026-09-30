"""SSE 事件常量（P0 契约层）。

事件名规则：``<domain>.<object>.<action>`` 全小写点分（规划书 §3.4）。
**新增事件必须同步本模块与 ``README.md``**，
并在 ``GET /api/events`` 的消费端（``ui/static/app.js``）补处理分支。
"""

from __future__ import annotations

__all__ = ["ALL_EVENTS", "Event", "PerceptionEvent", "is_known_event"]


class Event:
    """事件名常量表。用类属性而非 Enum —— 事件名要能直接当字符串发出去。"""

    # -- 运行级 -------------------------------------------------------------- #
    RUN_STARTED = "run.started"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    RUN_FINISHED = "run.finished"
    RUN_ERROR = "run.error"

    # -- 任务级 -------------------------------------------------------------- #
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_STATE_CHANGED = "task.state_changed"
    TASK_NEEDS_CONFIRM = "task.needs_confirm"

    # -- 感知 ---------------------------------------------------------------- #
    PERCEPTION_DONE = "perception.done"

    # -- 求解 ---------------------------------------------------------------- #
    SOLVE_VOTE = "solve.vote"
    SOLVE_DONE = "solve.done"
    SOLVE_REVIEW_REQUIRED = "solve.review_required"
    #: ``solve.escalated``（「一致率不足 → 升级 Tier2 复算」）已于 2026-09-29 删除：
    #: Tier 分级整体取消，只剩一种求解模式，复算由用户在创建任务时显式决定。

    # -- 执行 ---------------------------------------------------------------- #
    ACT_LEVEL_USED = "act.level_used"
    ACT_READBACK_MISMATCH = "act.readback_mismatch"
    ACT_SUBMIT_TIMEOUT = "act.submit_timeout"

    # -- 媒体 ---------------------------------------------------------------- #
    MEDIA_STATE_CHANGED = "media.state_changed"
    MEDIA_INTERRUPT_DETECTED = "media.interrupt_detected"

    # -- 任务栈 -------------------------------------------------------------- #
    STACK_PUSHED = "stack.pushed"
    STACK_POPPED = "stack.popped"

    # -- 推进：起始标定 + 收尾确认（2026-09-28） ----------------------------- #
    #: 开局标定完成。payload 带 ``total``（题目总数，可能为 null）、
    #: ``method``（``click`` / ``swipe`` / ``scroll``）与 ``reason``（模型给的理由）。
    ADVANCE_CALIBRATED = "advance.calibrated"
    #: 收尾前又问了视觉组一次。payload 带 ``completed``（是否全部完成）、
    #: ``found``（看起来已完成几题）与 ``reason``。
    #:
    #: 这条事件出现的时机：**找不到下一题** 或 **已完成数达到标定总数**。
    #: ``completed=False`` 时不允许静默收工 —— 必须按推进失败停下。
    ADVANCE_COMPLETION_CHECK = "advance.completion_check"

    # -- 训练模式（2026-09-29 加） ------------------------------------------- #
    #: 一次训练（总结经验 → 写进方式库 / 提示词经验区）完成。
    #: payload 带 ``run_id``、``targets``（更新了哪几处）与 ``notes``。
    TRAINING_DONE = "training.done"

    # -- 日志 ---------------------------------------------------------------- #
    LOG_LINE = "log.line"


class PerceptionEvent:
    """感知层事件（M1-1 / M1-2 / M1-3 增量，见 README.md）。

    与 :class:`Event` 同域，单独成类只为让「哪些事件是 P2/P3 引入的」一眼可见。
    """

    PROBE_STARTED = "perception.probe_started"
    PROBE_FALLBACK = "perception.probe_fallback"
    """DOM 读不到 → 降级到下一条通道。"""

    ARBITRATION_DECIDED = "perception.arbitration_decided"
    DUMP_PAUSED = "perception.dump_paused"
    """仲裁「双失败」→ 暂停留档，绝不静默跳过。"""


#: 全部合法事件名。用于发事件前的白名单校验，防止拼错名字默默丢事件。
ALL_EVENTS: frozenset[str] = frozenset(
    value
    for klass in (Event, PerceptionEvent)
    for name, value in vars(klass).items()
    if not name.startswith("_") and isinstance(value, str)
)


def is_known_event(name: str) -> bool:
    """事件名是否在 §2.2 ``core/events.py`` 清单内。"""
    return name in ALL_EVENTS
