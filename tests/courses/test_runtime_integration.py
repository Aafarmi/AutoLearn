"""M9 终局裁定：以 M8 已排空的合法 result 与进程退出情况核定状态。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.common.clock import timestamp
from core.courses import runtime as runtime_module
from core.courses.domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    job_id,
)
from core.courses.runtime import CourseRun, CourseRuntime, _process_timeout_seconds
from core.courses.store import CourseJobStore


@pytest.fixture()
def runtime_store(tmp_path: Path) -> CourseJobStore:
    return CourseJobStore(tmp_path / "courses")


def running_job(
    store: CourseJobStore,
    *,
    status: CourseStatus = CourseStatus.RUNNING,
    time_limit_minutes: int = 0,
) -> CourseJob:
    now = timestamp()
    job = CourseJob(
        job_id=job_id(),
        site_id="zhihuishu",
        title="终局裁定测试",
        mode=CourseMode.SHARED,
        course_id="course-1",
        class_id=None,
        status=status,
        phase=CoursePhase.LEARNING if status is CourseStatus.RUNNING else CoursePhase.SETUP,
        current_title="第一节",
        progress_percent=40.0,
        completed_count=2,
        total_count=5,
        failed_count=0,
        run_id="run-test" if status is CourseStatus.RUNNING else None,
        options=CourseOptions(time_limit_minutes=time_limit_minutes),
        created_at=now,
        updated_at=now,
        last_safe_error_category=None,
    )
    store.create(job)
    return job


def result(status: str, *, category: str | None = None) -> dict[str, object]:
    return {
        "type": "result",
        "status": status,
        "completed_count": 4,
        "total_count": 5,
        "failed_count": 1 if status != "completed" else 0,
        "safe_error_category": category,
    }


@pytest.mark.parametrize(
    ("scenario", "exit_code", "payload", "cancelled", "timed_out", "expected_status", "expected_category"),
    [
        ("completed-result-wins", 9, result("completed"), False, False, CourseStatus.COMPLETED, None),
        ("partial-result", 0, result("partial", category="time_limit"), False, False, CourseStatus.PARTIAL, "time_limit"),
        ("failed-result", 0, result("failed", category="upstream_error"), False, False, CourseStatus.FAILED, "upstream_error"),
        ("missing-result-zero-exit", 0, None, False, False, CourseStatus.FAILED, "no_result"),
        ("missing-result-nonzero-exit", 3, None, False, False, CourseStatus.FAILED, "process_exit"),
        ("cancel-overrides-result", 0, result("completed"), True, False, CourseStatus.CANCELLED, None),
        ("timeout-overrides-result", 0, result("completed"), False, True, CourseStatus.FAILED, "process_timeout"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_finalize_classifies_terminal_outcomes_and_releases_slot(
    runtime_store: CourseJobStore,
    tmp_path: Path,
    scenario: str,
    exit_code: int,
    payload: dict[str, object] | None,
    cancelled: bool,
    timed_out: bool,
    expected_status: CourseStatus,
    expected_category: str | None,
) -> None:
    job = running_job(runtime_store)
    runtime_store.acquire_slot(job.site_id, job.job_id, 12345)
    run_dir = runtime_store.job_dir(job.job_id) / "run"
    upstream = run_dir / "upstream"
    upstream.mkdir(parents=True)
    (upstream / "module.py").write_text("vendor copy", encoding="utf-8")
    diagnostics = runtime_store.job_dir(job.job_id) / "diagnostics.log"
    diagnostics.write_text("redacted diagnostic", encoding="utf-8")

    process = Mock()
    process.poll.return_value = exit_code
    process.wait.return_value = exit_code
    run = CourseRun(job.job_id, job.site_id, process, "run-test", job.created_at)
    run.counts.update(completed=2, failed=0)
    run.percent = 40.0
    run.had_result = payload is not None
    run.result_payload = payload
    run.process_timed_out = timed_out
    if cancelled:
        run.cancel_event.set()

    finalized = CourseRuntime(runtime_store).finalize(
        run, exit_code, payload is not None, payload
    )

    assert finalized.status is expected_status, scenario
    assert finalized.phase is CoursePhase.RESULT
    assert finalized.last_safe_error_category == expected_category
    assert runtime_store.active_slot(job.site_id) is None
    events = runtime_store.read_business_events(job.job_id)
    finished = [event for event in events if event["event_type"] == "course_finished"]
    assert len(finished) == 1
    expected_from = "stopping" if cancelled else "running"
    assert finished[0]["from_status"] == expected_from
    assert finished[0]["to_status"] == expected_status.value
    assert not upstream.exists()
    assert diagnostics.read_text(encoding="utf-8") == "redacted diagnostic"
    assert (runtime_store.job_dir(job.job_id) / "events.jsonl").exists()


def test_zero_time_limit_has_no_process_deadline() -> None:
    assert _process_timeout_seconds(0) is None
    assert _process_timeout_seconds(2) == 2 * 60 + 10 * 60


def test_watcher_terminates_process_after_configured_timeout(
    runtime_store: CourseJobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "result_then_sleep.py"
    bridge.write_text(
        "import json, sys, time\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'result','status':'completed','completed_count':1,"
        "'total_count':1,'failed_count':0,'safe_error_category':None}), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_module, "_site_cookies", lambda _site: {"CASLOGC": "stub"})
    monkeypatch.setattr(runtime_module, "_process_timeout_seconds", lambda _minutes: 0.1)
    ready = running_job(runtime_store, status=CourseStatus.READY, time_limit_minutes=1)
    runtime = CourseRuntime(
        runtime_store,
        bridge=bridge,
        run_dir_factory=lambda _job_id: tmp_path / "runs" / _job_id,
    )

    run = runtime.start(ready)
    assert run.finalized_event.wait(8)

    updated = runtime_store.get_job(ready.job_id)
    assert run.proc.poll() is not None
    assert run.process_timed_out
    assert updated.status is CourseStatus.FAILED
    assert updated.last_safe_error_category == "process_timeout"
    assert runtime_store.active_slot(ready.site_id) is None


def _ready_job(store: CourseJobStore, *, time_limit_minutes: int = 0) -> CourseJob:
    now = timestamp()
    job = CourseJob(
        job_id=job_id(),
        site_id="zhihuishu",
        title="stub Fucker integration",
        mode=CourseMode.SHARED,
        course_id="stub-course",
        class_id=None,
        status=CourseStatus.READY,
        phase=CoursePhase.SETUP,
        current_title=None,
        progress_percent=0.0,
        completed_count=0,
        total_count=0,
        failed_count=0,
        run_id=None,
        options=CourseOptions(time_limit_minutes=time_limit_minutes),
        created_at=now,
        updated_at=now,
        last_safe_error_category=None,
    )
    store.create(job)
    return job


def _stub_runtime(store: CourseJobStore, tmp_path: Path) -> CourseRuntime:
    bridge = Path(__file__).resolve().parents[1] / "fixtures" / "stub_fucker.py"
    return CourseRuntime(
        store,
        bridge=bridge,
        run_dir_factory=lambda job: tmp_path / "runs" / job,
    )


@pytest.mark.parametrize(
    ("scenario", "exit_code", "expected_status", "expected_category"),
    [
        ("normal", 0, CourseStatus.COMPLETED, None),
        ("partial", 0, CourseStatus.PARTIAL, "incomplete"),
        ("time_limit", 0, CourseStatus.PARTIAL, "time_limit"),
        # A valid result wins over a later nonzero process exit, per §5.3/M9.
        ("nonzero_exit", 7, CourseStatus.COMPLETED, None),
        ("no_result", 0, CourseStatus.FAILED, "no_result"),
        ("malformed", 0, CourseStatus.FAILED, "no_result"),
    ],
)
def test_stub_fucker_runtime_outcomes_release_process_and_slot(
    runtime_store: CourseJobStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    exit_code: int,
    expected_status: CourseStatus,
    expected_category: str | None,
) -> None:
    monkeypatch.setattr(runtime_module, "_site_cookies", lambda _site: {"CASLOGC": "fixture-cookie"})
    job = _ready_job(runtime_store)
    runtime = _stub_runtime(runtime_store, tmp_path)

    run = runtime.start(job, {"scenario": scenario})
    assert run.finalized_event.wait(5), scenario

    updated = runtime_store.get_job(job.job_id)
    assert run.proc.poll() == exit_code
    assert updated.status is expected_status
    assert updated.last_safe_error_category == expected_category
    assert runtime_store.active_slot(job.site_id) is None
    assert runtime.run_of(job.job_id) is None
    assert run.pump_queue is not None and run.pump_queue.empty()
    if scenario == "malformed":
        assert run.protocol_errors >= 1


def test_stub_fucker_user_stop_has_no_residual_process_or_slot(
    runtime_store: CourseJobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "_site_cookies", lambda _site: {"CASLOGC": "fixture-cookie"})
    job = _ready_job(runtime_store)
    runtime = _stub_runtime(runtime_store, tmp_path)
    run = runtime.start(job, {"scenario": "user_stop"})

    assert runtime.stop(job.job_id, timeout_seconds=0.1) is True
    assert run.finalized_event.wait(5)
    assert runtime_store.get_job(job.job_id).status is CourseStatus.CANCELLED
    assert run.proc.poll() is not None
    assert runtime_store.active_slot(job.site_id) is None
    assert runtime.run_of(job.job_id) is None


def test_stub_fucker_timeout_has_no_residual_process_or_slot(
    runtime_store: CourseJobStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_site_cookies", lambda _site: {"CASLOGC": "fixture-cookie"})
    # Zero makes Popen.wait(timeout=0) take its timeout path without a test sleep.
    monkeypatch.setattr(runtime_module, "_process_timeout_seconds", lambda _minutes: 0.0)
    job = _ready_job(runtime_store, time_limit_minutes=1)
    runtime = _stub_runtime(runtime_store, tmp_path)
    run = runtime.start(job, {"scenario": "timeout"})

    assert run.finalized_event.wait(5)
    updated = runtime_store.get_job(job.job_id)
    assert run.process_timed_out
    assert run.proc.poll() is not None
    assert updated.status is CourseStatus.FAILED
    assert updated.last_safe_error_category == "process_timeout"
    assert runtime_store.active_slot(job.site_id) is None
    assert runtime.run_of(job.job_id) is None
