"""Integration regressions for process and provider failures (all data stays in tmp_path)."""

from __future__ import annotations

import json
import io
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from core.courses import protocol, runtime as runtime_module
from core.common.clock import timestamp
from core.courses.domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    job_id,
)
from core.courses.errors import CourseAuthRequired, CourseBusy
from core.courses.providers.zhihuishu import bridge
from core.courses.providers.zhihuishu.preflight import check_cookies
from core.courses.runtime import CourseRuntime
from core.courses.store import CourseJobStore

STUB_BRIDGE = Path(__file__).resolve().parents[1] / "fixtures" / "stub_fucker.py"


def _ready_job(store: CourseJobStore) -> CourseJob:
    now = timestamp()
    job = CourseJob(
        job_id=job_id(), site_id="zhihuishu", title="稳定性测试",
        mode=CourseMode.SHARED, course_id="stable-course", class_id=None,
        status=CourseStatus.READY, phase=CoursePhase.SETUP,
        current_title=None, progress_percent=0.0, completed_count=0,
        total_count=0, failed_count=0, run_id=None, options=CourseOptions(),
        created_at=now, updated_at=now, last_safe_error_category=None,
    )
    store.create(job)
    return job


def _runtime(store: CourseJobStore, tmp_path: Path) -> CourseRuntime:
    return CourseRuntime(
        store,
        bridge=STUB_BRIDGE,
        run_dir_factory=lambda item: tmp_path / "runs" / item,
    )


def test_force_killed_worker_is_finalized_and_releases_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "_site_cookies", lambda _site: {"CASLOGC": "fixture"})
    store = CourseJobStore(tmp_path / "courses")
    job = _ready_job(store)
    runtime = _runtime(store, tmp_path)
    run = runtime.start(job, {"scenario": "timeout"})

    run.proc.kill()
    assert run.finalized_event.wait(5)
    updated = store.get_job(job.job_id)
    assert run.proc.poll() is not None
    assert updated.status is CourseStatus.FAILED
    assert updated.last_safe_error_category == "process_exit"
    assert store.active_slot(job.site_id) is None
    assert runtime.run_of(job.job_id) is None


def test_network_loss_is_classified_without_echoing_provider_exception() -> None:
    class FakeFucker:
        instances: list["FakeFucker"] = []

        def __init__(self, **_kwargs: Any) -> None:
            self.context: dict[str, Any] = {}
            self.instances.append(self)

        def fuckZhidaoCourse(self, _course_id: str) -> None:
            raise requests.ConnectionError("network down; credential=must-not-leak")

    module = SimpleNamespace(Fucker=FakeFucker, logger=logging.getLogger("stub-fucker"))
    state: dict[str, Any] = {"completed_count": 0, "failed_count": 0, "total_count": 0}
    result = bridge.run_course(
        module,
        {"mode": "shared", "course_id": "stable-course", "cookies": {"CASLOGC": "fixture"}},
        io.StringIO(),
        state,
    )

    assert result["status"] == "failed"
    assert result["safe_error_category"] == "ConnectionError"
    assert "must-not-leak" not in json.dumps(result)


def test_expired_cookie_is_rejected_without_echoing_value() -> None:
    expired = "CASLOGC-secret-cookie-value"
    with pytest.raises(CourseAuthRequired) as error:
        check_cookies({"CASLOGC": expired})
    assert expired not in str(error.value)


def test_double_start_is_busy_and_first_worker_can_be_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "_site_cookies", lambda _site: {"CASLOGC": "fixture"})
    store = CourseJobStore(tmp_path / "courses")
    job = _ready_job(store)
    runtime = _runtime(store, tmp_path)
    run = runtime.start(job, {"scenario": "user_stop"})

    with pytest.raises(CourseBusy):
        runtime.start(job, {"scenario": "normal"})
    assert runtime.stop(job.job_id, timeout_seconds=0.1)
    assert run.proc.poll() is not None
    assert store.active_slot(job.site_id) is None
    assert runtime.run_of(job.job_id) is None


def test_service_shutdown_interrupts_worker_and_records_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "_site_cookies", lambda _site: {"CASLOGC": "fixture"})
    store = CourseJobStore(tmp_path / "courses")
    job = _ready_job(store)
    runtime = _runtime(store, tmp_path)
    run = runtime.start(job, {"scenario": "user_stop"})

    assert runtime.shutdown_all(timeout_seconds=0.1) == {"stopped": 1, "failed": 0, "remaining": 0}
    assert run.proc.poll() is not None
    assert store.get_job(job.job_id).status is CourseStatus.CANCELLED
    assert store.active_slot(job.site_id) is None
    assert "service_shutdown" in [e["event_type"] for e in store.read_business_events(job.job_id)]


def test_section_5_3_event_contract_matches_snapshot() -> None:
    snapshot_path = Path(__file__).resolve().parents[1] / "snapshots" / "course_events_contract.json"
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    current = {
        "event_fields": {kind: sorted(fields) for kind, fields in sorted(protocol.EVENT_FIELDS.items())},
        "phase_statuses": sorted(protocol.PHASE_STATUSES),
        "result_statuses": sorted(protocol.RESULT_STATUSES),
        "limits": {
            "max_line_bytes": protocol.MAX_LINE_BYTES,
            "max_events_per_job": protocol.MAX_EVENTS_PER_JOB,
            "progress_min_interval_ms": protocol.PROGRESS_MIN_INTERVAL_MS,
        },
    }
    assert current == expected
