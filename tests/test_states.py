"""T0-2 状态机：``submitted`` 是唯一危险态，续跑不重复提交。"""

from __future__ import annotations

from itertools import pairwise

import pytest

from core.enums import QuestionState
from core.states import (
    DANGER_STATES,
    READBACK_ONLY_SOURCES,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    can_transition,
    is_danger,
    is_terminal,
    require_transition,
    requires_readback,
    resume_entry,
)

EXECUTABLE = frozenset(
    {
        QuestionState.PENDING,
        QuestionState.PERCEIVED,
        QuestionState.SOLVED,
        QuestionState.PENDING_CONFIRM,
        QuestionState.APPLIED,
    }
)


def test_nine_states_frozen() -> None:
    assert len(QuestionState) == 9
    assert set(TRANSITIONS) == set(QuestionState), "转移表必须覆盖全部九态"


def test_submitted_is_the_only_danger_state() -> None:
    assert frozenset({QuestionState.SUBMITTED}) == DANGER_STATES
    assert is_danger(QuestionState.SUBMITTED)
    assert not any(is_danger(s) for s in QuestionState if s is not QuestionState.SUBMITTED)


def test_resume_of_submitted_stays_submitted() -> None:
    """专项：提交后杀进程再续跑，必须原地进入 ``submitted``，绝不回退到可执行态。"""
    assert resume_entry(QuestionState.SUBMITTED) is QuestionState.SUBMITTED
    assert resume_entry(QuestionState.SUBMITTED) not in EXECUTABLE


@pytest.mark.parametrize("dst", sorted(EXECUTABLE, key=lambda s: s.value))
def test_submitted_cannot_go_back_to_executable(dst: QuestionState) -> None:
    """``submitted`` 没有通往任何可执行态的出边 —— 结构上堵死重复提交。"""
    assert not can_transition(QuestionState.SUBMITTED, dst)


@pytest.mark.parametrize(
    "dst",
    [QuestionState.VERIFIED, QuestionState.FAILED],
)
def test_submitted_only_exits_via_readback_targets(dst: QuestionState) -> None:
    assert can_transition(QuestionState.SUBMITTED, dst)


def test_submitted_outbound_edges_are_readback_only() -> None:
    assert requires_readback(QuestionState.SUBMITTED)
    assert frozenset({QuestionState.SUBMITTED}) == READBACK_ONLY_SOURCES
    assert not any(
        requires_readback(s) for s in QuestionState if s is not QuestionState.SUBMITTED
    )


def test_pending_has_no_inbound_edges() -> None:
    """``pending`` 无入边：状态一经离开不可回退重做。"""
    for src in QuestionState:
        assert not can_transition(src, QuestionState.PENDING), f"{src} 不该能回到 pending"


def test_happy_path_is_walkable() -> None:
    path = [
        QuestionState.PENDING,
        QuestionState.PERCEIVED,
        QuestionState.SOLVED,
        QuestionState.APPLIED,
        QuestionState.SUBMITTED,
        QuestionState.VERIFIED,
    ]
    for src, dst in pairwise(path):
        require_transition(src, dst)  # 不抛即合法


def test_self_transition_rejected() -> None:
    for state in QuestionState:
        assert not can_transition(state, state)


def test_illegal_transition_raises_typed_error() -> None:
    with pytest.raises(IllegalTransition) as excinfo:
        require_transition(QuestionState.SUBMITTED, QuestionState.APPLIED)
    assert excinfo.value.src is QuestionState.SUBMITTED
    assert excinfo.value.dst is QuestionState.APPLIED


def test_terminal_states_have_no_outbound_edges() -> None:
    assert frozenset(
        {QuestionState.VERIFIED, QuestionState.FAILED, QuestionState.SKIPPED}
    ) == TERMINAL_STATES
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()
        assert is_terminal(state)


def test_resume_entry_passthrough_for_non_danger_states() -> None:
    for state in QuestionState:
        if state is QuestionState.SUBMITTED:
            continue
        assert resume_entry(state) is state
