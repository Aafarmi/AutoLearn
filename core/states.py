"""题目九态状态机（T0-2）。

**`submitted` 是本项目唯一的危险态。**

- 续跑时进入 ``submitted`` 的项目**只做结果回读**，绝不重新点击、绝不重新提交；
- ``submitted`` 的出边仅有 ``verified`` / ``failed`` 两条，且**只能由结果回读触发**，
  由 :data:`READBACK_ONLY_SOURCES` 显式标注，供编排层与执行层双重稽查；
- ``pending`` 没有任何入边——状态一经离开便不可回退重做，这在结构上保证
  「续跑不会把已提交的题重新做一遍」。

违反转移表的调用一律抛 :class:`IllegalTransition`，**不允许用 ``assert`` 做流程控制**。
"""

from __future__ import annotations

from core.enums import QuestionState

__all__ = [
    "DANGER_STATES",
    "READBACK_ONLY_SOURCES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "IllegalTransition",
    "can_transition",
    "is_danger",
    "is_terminal",
    "require_transition",
    "requires_readback",
    "resume_entry",
]

# --------------------------------------------------------------------------- #
# 完整转移表
# --------------------------------------------------------------------------- #
TRANSITIONS: dict[QuestionState, frozenset[QuestionState]] = {
    # 入队后等待感知
    QuestionState.PENDING: frozenset(
        {QuestionState.PERCEIVED, QuestionState.FAILED, QuestionState.SKIPPED}
    ),
    # 题干 / 选项 / 题型已结构化读出
    QuestionState.PERCEIVED: frozenset(
        {QuestionState.SOLVED, QuestionState.FAILED, QuestionState.SKIPPED}
    ),
    # 已有作答；半自动模式进待确认，全自动模式直接 applied
    QuestionState.SOLVED: frozenset(
        {
            QuestionState.PENDING_CONFIRM,
            QuestionState.APPLIED,
            QuestionState.FAILED,
            QuestionState.SKIPPED,
        }
    ),
    # 等人点确认；人工否决 → skipped
    QuestionState.PENDING_CONFIRM: frozenset(
        {QuestionState.APPLIED, QuestionState.SKIPPED, QuestionState.FAILED}
    ),
    # 选项已选好，等提交
    QuestionState.APPLIED: frozenset(
        {QuestionState.SUBMITTED, QuestionState.FAILED, QuestionState.SKIPPED}
    ),
    # ⚠ 危险态：出边只能由「结果回读」触发，见 READBACK_ONLY_SOURCES
    QuestionState.SUBMITTED: frozenset({QuestionState.VERIFIED, QuestionState.FAILED}),
    # 终态
    QuestionState.VERIFIED: frozenset(),
    QuestionState.FAILED: frozenset(),
    QuestionState.SKIPPED: frozenset(),
}

#: 唯一危险态集合
DANGER_STATES: frozenset[QuestionState] = frozenset({QuestionState.SUBMITTED})

#: 出边**只能由结果回读触发**的源状态。任何其他驱动（点击、重放、人工确认）
#: 一旦试图让这些状态发生迁移，编排层必须拒绝。
READBACK_ONLY_SOURCES: frozenset[QuestionState] = frozenset({QuestionState.SUBMITTED})

#: 终态集合
TERMINAL_STATES: frozenset[QuestionState] = frozenset(
    state for state, nxt in TRANSITIONS.items() if not nxt
)


class IllegalTransition(RuntimeError):  # noqa: N818 -- 名字由规划书 §2.2 冻结，不改
    """非法状态迁移。``require_transition()`` 违约时抛出。"""

    def __init__(self, src: QuestionState, dst: QuestionState) -> None:
        self.src = src
        self.dst = dst
        super().__init__(f"illegal transition: {src.value} -> {dst.value}")


def can_transition(src: QuestionState, dst: QuestionState) -> bool:
    """``src`` → ``dst`` 是否合法。纯查表，无副作用。"""
    if src is dst:
        return False
    return dst in TRANSITIONS.get(src, frozenset())


def require_transition(src: QuestionState, dst: QuestionState) -> None:
    """合法则放行，否则抛 :class:`IllegalTransition`。"""
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)


def resume_entry(state: QuestionState) -> QuestionState:
    """续跑时应当**进入**的状态。

    ``submitted`` 是危险态：续跑只回读结果，因此原地进入，绝不回退到
    ``pending`` / ``applied`` 之类的可执行状态。其余状态原样返回。
    """
    if state in DANGER_STATES:
        return QuestionState.SUBMITTED
    return state


def requires_readback(state: QuestionState) -> bool:
    """该状态的出边是否只能由结果回读触发。"""
    return state in READBACK_ONLY_SOURCES


def is_danger(state: QuestionState) -> bool:
    return state in DANGER_STATES


def is_terminal(state: QuestionState) -> bool:
    return state in TERMINAL_STATES
