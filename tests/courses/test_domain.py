"""T2-01 验收：合法迁移通过 / 非法迁移抛错 / job_id 前缀正确。

除了三条必需断言，还锁了两个容易退化的点：迁移表的键必须覆盖全部 10 个状态
（漏一个就等于那个状态永远出不去），以及 `CourseJob` 不能混进做题字段。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import core.courses.domain as domain_module
from core.courses.domain import (
    ALLOWED_TRANSITIONS,
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    ensure_transition,
    job_id,
)
from core.courses.errors import CourseTransitionError


def test_legal_transitions_pass() -> None:
    """断言一：表里每一条边都必须放行，且返回 None。"""
    for src, targets in ALLOWED_TRANSITIONS.items():
        for dst in targets:
            assert ensure_transition(src, dst) is None, f"{src} → {dst} 被误判为非法"


def test_main_path_in_order_is_allowed() -> None:
    path = [
        CourseStatus.DRAFT,
        CourseStatus.AUTHENTICATING,
        CourseStatus.READY,
        CourseStatus.RUNNING,
        CourseStatus.STOPPING,
        CourseStatus.CANCELLED,
    ]
    for src, dst in zip(path, path[1:]):
        ensure_transition(src, dst)


def test_rerun_from_every_terminal_state_is_allowed() -> None:
    for terminal in (
        CourseStatus.COMPLETED,
        CourseStatus.PARTIAL,
        CourseStatus.FAILED,
        CourseStatus.CANCELLED,
    ):
        ensure_transition(terminal, CourseStatus.RUNNING)


def test_illegal_transition_raises_with_both_ends() -> None:
    """断言二：非法迁移抛 CourseTransitionError，消息里带两端状态。"""
    with pytest.raises(CourseTransitionError) as excinfo:
        ensure_transition(CourseStatus.DRAFT, CourseStatus.RUNNING)

    message = str(excinfo.value)
    assert "draft" in message and "running" in message, message
    assert excinfo.value.status_code == 409


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (CourseStatus.DRAFT, CourseStatus.RUNNING),
        (CourseStatus.DRAFT, CourseStatus.DRAFT),  # 自迁移不放行
        (CourseStatus.ARCHIVED, CourseStatus.RUNNING),  # 唯一无出边的状态
        (CourseStatus.RUNNING, CourseStatus.READY),  # 回退必须经 stopping
        (CourseStatus.READY, CourseStatus.COMPLETED),
        (CourseStatus.COMPLETED, CourseStatus.PARTIAL),  # 终态之间不互转
    ],
)
def test_illegal_pairs_rejected(src: CourseStatus, dst: CourseStatus) -> None:
    with pytest.raises(CourseTransitionError):
        ensure_transition(src, dst)


def test_unknown_status_rejected() -> None:
    with pytest.raises(CourseTransitionError):
        ensure_transition("nope", CourseStatus.RUNNING)  # type: ignore[arg-type]
    with pytest.raises(CourseTransitionError):
        ensure_transition(CourseStatus.DRAFT, "nope")  # type: ignore[arg-type]


def test_transition_table_covers_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(CourseStatus)
    assert ALLOWED_TRANSITIONS[CourseStatus.ARCHIVED] == frozenset()


def test_job_id_prefix_and_uniqueness() -> None:
    """断言三：`course-` 前缀 + 32 位 hex，且两次不重样。"""
    first, second = job_id(), job_id()
    assert first.startswith("course-")
    assert len(first) == len("course-") + 32
    assert int(first.removeprefix("course-"), 16) >= 0
    assert first != second


def test_options_defaults() -> None:
    options = CourseOptions()
    assert (options.speed, options.threshold, options.time_limit_minutes) == (1.5, 0.91, 0)
    assert options.process_exam is True
    assert options.model_profile_id is None


def test_course_job_rejects_homework_fields() -> None:
    banned = {
        "queue_state",
        "queue_position",
        "interaction_retry_authorized",
        "page_url",
        "confirmation_id",
        "last_operation",
        "interrupted_operation",
    }
    assert banned.isdisjoint(CourseJob.__dataclass_fields__)


def test_course_job_is_frozen_and_carries_no_hidden_state() -> None:
    job = CourseJob(
        job_id=job_id(),
        site_id="zhihuishu",
        title="高等数学",
        mode=CourseMode.SHARED,
        course_id="c-1",
        class_id=None,
        status=CourseStatus.DRAFT,
        phase=CoursePhase.SETUP,
        current_title=None,
        progress_percent=0.0,
        completed_count=0,
        total_count=0,
        failed_count=0,
        run_id=None,
        options=CourseOptions(),
        created_at="2026-10-23T21:00:00+08:00",
        updated_at="2026-10-23T21:00:00+08:00",
        last_safe_error_category=None,
    )
    assert job.status is CourseStatus.DRAFT
    assert job.options is not None
    with pytest.raises(Exception):
        job.status = CourseStatus.RUNNING  # type: ignore[misc]


def test_domain_never_imports_homework_package() -> None:
    """硬约束：domain.py 不得 import core.tasks（用 AST，避免文档串误伤）。"""
    source = Path(domain_module.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module)
    assert [name for name in imported if name.split(".")[:2] == ["core", "tasks"]] == []
