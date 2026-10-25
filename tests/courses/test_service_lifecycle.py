from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.courses import domain, service
from core.courses.errors import CourseAuthRequired, CourseTransitionError
from core.courses.store import CourseJobStore


def shared_payload(**changes):
    return {
        "mode": "shared",
        "course_id": "sharedCourse123",
        "speed": 1.5,
        "threshold": 0.9,
        "time_limit_minutes": 0,
        "process_exam": True,
        **changes,
    }


def test_create_job_writes_only_course_store(tmp_path, monkeypatch):
    store = CourseJobStore(tmp_path / "courses")
    monkeypatch.setattr(service, "_store", lambda: store)

    job = service.create_job("zhihuishu", "课程标题", shared_payload())

    assert job.status is domain.CourseStatus.DRAFT
    assert store.get_job(job.job_id) == job
    serialized = (store.job_dir(job.job_id) / "job.json").read_text(encoding="utf-8")
    assert "api_key" not in serialized and "cookies" not in serialized


def test_start_draft_uses_legal_auth_transitions_and_existing_credentials(tmp_path, monkeypatch):
    store = CourseJobStore(tmp_path / "courses")
    monkeypatch.setattr(service, "_store", lambda: store)
    job = service.create_job("zhihuishu", "课程", shared_payload())
    checks = []

    class Credentials:
        def get_site_cookies(self, site_id):
            assert site_id == "zhihuishu"
            return {"CASLOGC": "present"}

    class Runtime:
        def start(self, current, params=None):
            checks.append((current.status, params))
            updated = store.update(
                current, status=domain.CourseStatus.RUNNING,
                run_id="run-1", phase=domain.CoursePhase.SETUP,
            )
            return SimpleNamespace(job_id=updated.job_id)

    monkeypatch.setattr(service, "LocalCredentialProvider", Credentials)
    monkeypatch.setattr(service.preflight, "check_cookies", lambda cookies: "uuid")
    monkeypatch.setattr(service, "_runtime", Runtime)

    running = service.start(job.job_id)

    assert running.status is domain.CourseStatus.RUNNING
    assert checks[0][0] is domain.CourseStatus.READY
    assert [event["event_type"] for event in store.read_business_events(job.job_id)] == [
        "auth_started", "authenticated"
    ]


def test_start_draft_with_bad_cookie_rolls_back_to_draft(tmp_path, monkeypatch):
    store = CourseJobStore(tmp_path / "courses")
    monkeypatch.setattr(service, "_store", lambda: store)
    job = service.create_job("zhihuishu", "课程", shared_payload())

    class Credentials:
        def get_site_cookies(self, _site_id):
            return {"CASLOGC": "bad"}

    monkeypatch.setattr(service, "LocalCredentialProvider", Credentials)
    monkeypatch.setattr(
        service.preflight, "check_cookies",
        lambda _cookies: (_ for _ in ()).throw(CourseAuthRequired("Cookie 无效。")),
    )
    with pytest.raises(CourseAuthRequired):
        service.start(job.job_id)

    assert store.get_job(job.job_id).status is domain.CourseStatus.DRAFT
    assert [event["to_status"] for event in store.read_business_events(job.job_id)] == [
        "authenticating", "draft"
    ]


def test_start_rejects_terminal_use_rerun_instead(tmp_path, monkeypatch):
    store = CourseJobStore(tmp_path / "courses")
    monkeypatch.setattr(service, "_store", lambda: store)
    job = service.create_job("zhihuishu", "课程", shared_payload())
    terminal = store.update(job, status=domain.CourseStatus.COMPLETED)

    with pytest.raises(CourseTransitionError):
        service.start(terminal.job_id)


def test_update_options_validates_and_persists(tmp_path, monkeypatch):
    store = CourseJobStore(tmp_path / "courses")
    monkeypatch.setattr(service, "_store", lambda: store)
    job = service.create_job("zhihuishu", "课程", shared_payload())

    updated = service.update_options(job.job_id, {"speed": 2.0, "threshold": 0.95})
    assert updated.options.speed == 2.0
    assert updated.options.threshold == 0.95


def test_lifecycle_actions_delegate_to_existing_store_and_runtime(tmp_path, monkeypatch):
    store = CourseJobStore(tmp_path / "courses")
    monkeypatch.setattr(service, "_store", lambda: store)
    job = service.create_job("zhihuishu", "课程", shared_payload())
    called = []

    class Runtime:
        def stop(self, job_id):
            called.append(("stop", job_id))
            return True

    monkeypatch.setattr(service, "_runtime", Runtime)
    assert service.stop(job.job_id) is True
    archived = service.archive(job.job_id)
    assert archived.status is domain.CourseStatus.ARCHIVED
    service.delete(job.job_id)
    assert called == [("stop", job.job_id)]
    with pytest.raises(Exception):
        store.get_job(job.job_id)

