"""T2-05/06/07 验收：归档、删除、单实例槽、崩溃恢复。

三条主线（对应任务卡）：
- 归档后 overview（`list_summaries`）不再返回该任务；
- running 的任务不允许删除（`CourseBusy`），目录必须还在；
- 上次崩溃留下的 running 任务，重启后变 `failed(process_exit)` 且有
  `recovered_after_restart` 事件，活跃槽被清空、索引与 `job.json` 对齐。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.courses.domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    job_id,
)
from core.courses.errors import CourseBusy, CourseNotFound, CourseTransitionError
from core.courses.store import CourseJobStore, _pid_alive
from core.webui import task_router


def make_job(status: CourseStatus = CourseStatus.READY, **overrides: object) -> CourseJob:
    values: dict[str, object] = {
        "job_id": job_id(),
        "site_id": "zhihuishu",
        "title": "高等数学（上）",
        "mode": CourseMode.SHARED,
        "course_id": "3001",
        "class_id": None,
        "status": status,
        "phase": CoursePhase.CATALOG,
        "current_title": None,
        "progress_percent": 0.0,
        "completed_count": 0,
        "total_count": 10,
        "failed_count": 0,
        "run_id": None,
        "options": CourseOptions(),
        "created_at": "2026-10-23T10:00:00.000+08:00",
        "updated_at": "2026-10-23T10:00:00.000+08:00",
        "last_safe_error_category": None,
    }
    values.update(overrides)
    return CourseJob(**values)  # type: ignore[arg-type]


@pytest.fixture()
def store(tmp_path: Path) -> CourseJobStore:
    return CourseJobStore(tmp_path / "courses")


def dead_pid() -> int:
    """取一个确定已经退出的 pid（等子进程结束后立刻读取）。"""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


# --- 归档 -------------------------------------------------------------------


def test_archive_hides_job_from_overview(store: CourseJobStore) -> None:
    job = make_job()
    store.create(job)

    archived = store.archive(job.job_id)

    assert archived.status is CourseStatus.ARCHIVED
    assert store.get_job(job.job_id).status is CourseStatus.ARCHIVED
    assert [entry["job_id"] for entry in store.list_summaries()] == []
    assert [entry["job_id"] for entry in store.list_summaries(include_archived=True)] == [
        job.job_id
    ]
    events = store.read_business_events(job.job_id)
    assert [event["event_type"] for event in events] == ["course_archived"]
    assert events[0]["from_status"] == "ready"
    assert events[0]["to_status"] == "archived"


def test_archive_running_is_rejected(store: CourseJobStore) -> None:
    job = make_job(CourseStatus.RUNNING, run_id="run-1")
    store.create(job)

    with pytest.raises(CourseTransitionError):
        store.archive(job.job_id)
    assert store.get_job(job.job_id).status is CourseStatus.RUNNING


def test_archive_missing_job_is_404(store: CourseJobStore) -> None:
    with pytest.raises(CourseNotFound):
        store.archive("course-" + "b" * 32)


# --- 删除 -------------------------------------------------------------------


def test_delete_running_job_is_rejected(store: CourseJobStore) -> None:
    job = make_job(CourseStatus.RUNNING, run_id="run-1")
    store.create(job)

    with pytest.raises(CourseBusy):
        store.delete(job.job_id)

    assert store.job_dir(job.job_id).is_dir()
    assert store.get_job(job.job_id).status is CourseStatus.RUNNING


def test_delete_stopping_job_is_rejected(store: CourseJobStore) -> None:
    job = make_job(CourseStatus.STOPPING, run_id="run-1")
    store.create(job)
    with pytest.raises(CourseBusy):
        store.delete(job.job_id)


def test_delete_removes_directory_and_index(store: CourseJobStore) -> None:
    keep = make_job()
    drop = make_job()
    store.create(keep)
    store.create(drop)
    store.append_business_event(drop.job_id, "created", None, CourseStatus.READY)

    store.delete(drop.job_id)

    assert not store.job_dir(drop.job_id).exists()
    assert [entry["job_id"] for entry in store.list_summaries()] == [keep.job_id]
    assert not list(store.root.joinpath("jobs").glob(".trash-*"))
    with pytest.raises(CourseNotFound):
        store.get_job(drop.job_id)


def test_two_store_instances_preserve_concurrent_updates(
    store: CourseJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """标题更新与进度更新交错时，不能用旧 payload 覆盖另一实例的字段。"""
    other = CourseJobStore(store.root / ".." / "courses")
    job = store.create(make_job())
    writing = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()
    original = store._atomic_write

    def paused_write(path, payload):
        if path.name == "job.json":
            writing.set()
            assert release.wait(5)
        original(path, payload)

    def update_progress():
        second_started.set()
        try:
            return other.update(job, completed_count=7)
        finally:
            second_finished.set()

    monkeypatch.setattr(store, "_atomic_write", paused_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.update, job, title="新标题")
        try:
            assert writing.wait(5)
            second = pool.submit(update_progress)
            assert second_started.wait(5)
            second_finished.wait(0.1)
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    updated = other.get_job(job.job_id)
    assert updated.title == "新标题"
    assert updated.completed_count == 7
    assert other.list_summaries()[0]["completed_count"] == 7


def test_delete_removes_current_and_legacy_run_directories(
    store: CourseJobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.courses.providers.zhihuishu import preflight

    job = store.create(make_job())
    monkeypatch.setattr(preflight, "data_path", lambda *parts: tmp_path.joinpath(*parts))
    run = preflight.prepare_run_dir(job.job_id)
    legacy = store.root / "runs" / job.job_id
    legacy.mkdir(parents=True)
    (legacy / "diagnostics.log").write_text("old run", encoding="utf-8")
    keep = store.root / "runs" / "other-job"
    keep.mkdir()

    store.delete(job.job_id)

    assert not run.exists()
    assert not legacy.exists()
    assert not store.job_dir(job.job_id).exists()
    assert keep.is_dir()


def test_delete_failure_keeps_job_and_run_for_retry(
    store: CourseJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.courses.errors import CourseError

    job = store.create(make_job())
    run = store.job_dir(job.job_id) / "run"
    run.mkdir()
    (run / "marker").write_text("keep", encoding="utf-8")

    def refuse_delete(path):
        raise PermissionError("busy")

    with monkeypatch.context() as scoped:
        scoped.setattr("core.courses.store.shutil.rmtree", refuse_delete)
        with pytest.raises(CourseError):
            store.delete(job.job_id)
    assert store.get_job(job.job_id).job_id == job.job_id
    assert (run / "marker").read_text(encoding="utf-8") == "keep"
    store.delete(job.job_id)
    assert not run.exists()


# --- 单实例槽 ---------------------------------------------------------------


def test_slot_acquire_and_release(store: CourseJobStore) -> None:
    job = make_job()
    store.create(job)

    slot = store.acquire_slot("zhihuishu", job.job_id, 4242)
    assert slot["job_id"] == job.job_id and slot["pid"] == 4242 and slot["since"]
    assert store.active_slot("zhihuishu")["job_id"] == job.job_id

    assert store.release_slot("zhihuishu", job.job_id) is True
    assert store.active_slot("zhihuishu") is None
    assert store.release_slot("zhihuishu") is False


def test_slot_busy_when_holder_alive(store: CourseJobStore) -> None:
    first, second = make_job(), make_job()
    store.create(first)
    store.create(second)
    store.acquire_slot("zhihuishu", first.job_id, __import__("os").getpid())

    with pytest.raises(CourseBusy):
        store.acquire_slot("zhihuishu", second.job_id, 4242)
    assert store.active_slot("zhihuishu")["job_id"] == first.job_id


def test_stale_slot_is_reclaimed_with_event(store: CourseJobStore) -> None:
    stale, fresh = make_job(), make_job()
    store.create(stale)
    store.create(fresh)
    store.acquire_slot("zhihuishu", stale.job_id, dead_pid())

    slot = store.acquire_slot("zhihuishu", fresh.job_id, 4242)

    assert slot["job_id"] == fresh.job_id
    assert store.active_slot("zhihuishu")["job_id"] == fresh.job_id
    types = [event["event_type"] for event in store.read_business_events(fresh.job_id)]
    assert "stale_slot_reclaimed" in types


def test_release_ignores_other_job(store: CourseJobStore) -> None:
    holder, other = make_job(), make_job()
    store.create(holder)
    store.create(other)
    store.acquire_slot("zhihuishu", holder.job_id, 4242)

    assert store.release_slot("zhihuishu", other.job_id) is False
    assert store.active_slot("zhihuishu")["job_id"] == holder.job_id


def test_pid_alive_handles_garbage() -> None:
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False
    assert _pid_alive(None) is False
    assert _pid_alive(True) is False
    assert _pid_alive(__import__("os").getpid()) is True


# --- 崩溃恢复 ---------------------------------------------------------------


def test_running_job_recovers_to_failed(store: CourseJobStore) -> None:
    job = make_job(CourseStatus.RUNNING, run_id="run-1", progress_percent=42.0)
    store.create(job)
    store.acquire_slot("zhihuishu", job.job_id, dead_pid())

    result = store.recover_on_start()

    assert result == {"recovered": 1, "cleared_slots": 1}
    recovered = store.get_job(job.job_id)
    assert recovered.status is CourseStatus.FAILED
    assert recovered.run_id is None
    assert recovered.last_safe_error_category == "process_exit"
    assert store.active_slot("zhihuishu") is None
    events = store.read_business_events(job.job_id)
    assert [event["event_type"] for event in events] == ["recovered_after_restart"]
    assert events[0]["from_status"] == "running"
    assert events[0]["to_status"] == "failed"
    assert store.list_summaries()[0]["status"] == "failed"


def test_recover_covers_authenticating_and_skips_others(store: CourseJobStore) -> None:
    authenticating = make_job(CourseStatus.AUTHENTICATING)
    ready = make_job(CourseStatus.READY)
    completed = make_job(CourseStatus.COMPLETED)
    for job in (authenticating, ready, completed):
        store.create(job)

    result = store.recover_on_start()

    assert result["recovered"] == 1
    assert store.get_job(authenticating.job_id).status is CourseStatus.FAILED
    assert store.get_job(ready.job_id).status is CourseStatus.READY
    assert store.get_job(completed.job_id).status is CourseStatus.COMPLETED


def test_recover_rebuilds_corrupt_index_from_job_json(store: CourseJobStore) -> None:
    job = make_job(CourseStatus.RUNNING, run_id="run-1")
    store.create(job)
    store.root.joinpath("index.json").write_text("{ not json", encoding="utf-8")

    store.recover_on_start()

    entries = store.list_summaries()
    assert len(entries) == 1 and entries[0]["status"] == "failed"
    raw = json.loads(store.root.joinpath("index.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["jobs"][0]["job_id"] == job.job_id


def test_recover_is_idempotent(store: CourseJobStore) -> None:
    job = make_job(CourseStatus.RUNNING, run_id="run-1")
    store.create(job)
    store.recover_on_start()

    assert store.recover_on_start()["recovered"] == 0
    events = store.read_business_events(job.job_id)
    assert len([e for e in events if e["event_type"] == "recovered_after_restart"]) == 1


def test_detail_surfaces_recovery_event(
    store: CourseJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """恢复事件必须能从详情接口看见（DTO 只序列化，事件由路由注入）。"""
    job = make_job(CourseStatus.RUNNING, run_id="run-1")
    store.create(job)
    store.recover_on_start()
    monkeypatch.setattr(task_router, "_course_store", lambda: store)

    payload = task_router.course_detail(job.job_id)

    assert payload["task"]["status"] == "failed"
    assert [event["event_type"] for event in payload["events"]] == [
        "recovered_after_restart"
    ]
    assert payload["events"][0]["safe_error_category"] == "process_exit"

