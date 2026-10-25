"""T2-03 验收：双事件日志的封顶、编号连续、增量读、节流合并。

两条主验收：
1. 2000 条 progress → 文件 ≤500 行、sequence 连续、`after` 增量正确；
2. 3 条同 stage 的 progress 在 300ms 内 → 只落盘 1 条（保留最后一条）。
另外锁住三个容易被"优化"掉的约定：未知键被丢弃、跨 stage 不合并、坏行不致命。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from core.courses.domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    job_id,
)
from core.courses.errors import CourseNotFound
from core.courses.store import (
    BUSINESS_EVENT_FILE,
    EVENT_LOG_MAX_LINES,
    EVENT_THROTTLE_MS,
    RUNTIME_EVENT_FILE,
    CourseJobStore,
)


def _job() -> CourseJob:
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    return CourseJob(
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
        created_at=stamp,
        updated_at=stamp,
        last_safe_error_category=None,
    )


@pytest.fixture()
def store(tmp_path):
    instance = CourseJobStore(root=tmp_path / "courses")
    job = _job()
    instance.create(job)
    return instance, job


def _progress(stage: str, current: int, total: int, percent: float) -> dict:
    return {
        "type": "progress",
        "stage": stage,
        "current": current,
        "total": total,
        "percent": percent,
        "completed_count": current,
        "total_count": total,
    }


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_two_thousand_progress_capped_with_contiguous_sequences(store) -> None:
    """2000 条 progress：封顶 500 行、sequence 连续、after 增量正确。"""
    instance, job = store
    stages = ("catalog", "learning", "exam")  # 轮换 stage，避免触发节流
    for index in range(2000):
        instance.append_runtime_event(
            job.job_id, _progress(stages[index % 3], index, 2000, index / 20)
        )

    path = instance.job_dir(job.job_id) / RUNTIME_EVENT_FILE
    records = _lines(path)

    assert len(records) == EVENT_LOG_MAX_LINES
    sequences = [record["sequence"] for record in records]
    assert sequences == list(range(sequences[0], sequences[0] + EVENT_LOG_MAX_LINES))
    assert sequences[-1] == 2000, "最后一条必须是最新写入的事件"
    assert sequences[0] == 2000 - EVENT_LOG_MAX_LINES + 1

    tail = instance.read_runtime_events(job.job_id, after=sequences[-3] - 1)
    assert [record["sequence"] for record in tail] == sequences[-3:]
    assert instance.read_runtime_events(job.job_id, after=sequences[-1]) == []


def test_three_same_stage_progress_within_window_collapse_to_one(store) -> None:
    """3 条同 stage 的 progress 在 300ms 内：只落盘 1 条，且是最后一条。"""
    instance, job = store
    for percent in (10, 20, 30):
        instance.append_runtime_event(
            job.job_id, _progress("learning", percent, 100, percent)
        )
        time.sleep(0.09)

    path = instance.job_dir(job.job_id) / RUNTIME_EVENT_FILE
    records = _lines(path)

    assert len(records) == 1
    assert records[0]["percent"] == 30, "合并必须保留最后一条，而不是第一条"
    assert records[0]["sequence"] == 1, "合并沿用旧 sequence，编号不能跳"


def test_different_stage_or_expired_window_appends(store) -> None:
    instance, job = store
    instance.append_runtime_event(job.job_id, _progress("learning", 1, 100, 1))
    instance.append_runtime_event(job.job_id, _progress("exam", 2, 100, 2))
    assert len(instance.read_runtime_events(job.job_id)) == 2, "跨 stage 不得合并"

    time.sleep(EVENT_THROTTLE_MS / 1000 + 0.06)
    instance.append_runtime_event(job.job_id, _progress("exam", 3, 100, 3))
    assert len(instance.read_runtime_events(job.job_id)) == 3, "超出窗口必须新起一行"


def test_unknown_keys_never_reach_disk(store) -> None:
    """上游多塞的键（Cookie / URL / 原始报错）必须被白名单丢掉。"""
    instance, job = store
    dirty = {
        **_progress("learning", 1, 10, 10),
        "cookie": "SESSDATA=secret-token",
        "url": "https://studyvideoh5.zhihuishu.com/learn",
        "stderr": "Traceback (most recent call last)",
    }
    instance.append_runtime_event(job.job_id, dirty)

    raw = (instance.job_dir(job.job_id) / RUNTIME_EVENT_FILE).read_text(encoding="utf-8")
    for leaked in ("SESSDATA", "https://", "Traceback"):
        assert leaked not in raw


def test_runtime_event_must_match_contract(store) -> None:
    instance, job = store
    with pytest.raises(ValueError):
        instance.append_runtime_event(job.job_id, {"type": "progress", "stage": "learning"})
    with pytest.raises(ValueError):
        instance.append_runtime_event(job.job_id, {"type": "unknown"})


def test_business_events_are_monotonic_and_limit_from_tail(store) -> None:
    instance, job = store
    for _ in range(5):
        record = instance.append_business_event(
            job.job_id,
            "status",
            CourseStatus.DRAFT,
            CourseStatus.AUTHENTICATING,
            operation="authenticate",
        )
        assert record["task_id"] == job.job_id
        assert record["occurred_at"].endswith("Z")
        assert record["from_status"] == "draft"
        assert record["to_status"] == "authenticating"

    path = instance.job_dir(job.job_id) / BUSINESS_EVENT_FILE
    assert [record["event_id"] for record in _lines(path)] == [1, 2, 3, 4, 5]
    assert [record["event_id"] for record in instance.read_business_events(job.job_id, limit=2)] == [4, 5]
    assert instance.read_business_events(job.job_id, limit=0) == []


def test_business_events_capped_and_ids_keep_growing(store) -> None:
    instance, job = store
    for _ in range(EVENT_LOG_MAX_LINES + 25):
        instance.append_business_event(job.job_id, "status", "draft", "ready")

    events = instance.read_business_events(job.job_id, limit=1000)
    assert len(events) == EVENT_LOG_MAX_LINES
    assert events[0]["event_id"] == 26
    assert events[-1]["event_id"] == EVENT_LOG_MAX_LINES + 25


def test_writes_to_unknown_job_raise_not_create_orphans(store, tmp_path) -> None:
    instance, _ = store
    ghost = "course-00000000000000000000000000000000"
    with pytest.raises(CourseNotFound):
        instance.append_business_event(ghost, "status", None, None)
    with pytest.raises(CourseNotFound):
        instance.append_runtime_event(ghost, _progress("learning", 1, 10, 10))
    assert not (instance.job_dir(ghost)).exists()


def test_broken_lines_are_skipped_not_fatal(store) -> None:
    instance, job = store
    instance.append_business_event(job.job_id, "status", "draft", "ready")
    path = instance.job_dir(job.job_id) / BUSINESS_EVENT_FILE
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"event_id": 99, "truncated"')  # 模拟被强杀在行中间

    events = instance.read_business_events(job.job_id)
    assert len(events) == 1, "半截行必须被跳过"

    record = instance.append_business_event(job.job_id, "status", "ready", "running")
    assert record["event_id"] == 2, "编号要接着干净的记录走，不能跳到 100"
