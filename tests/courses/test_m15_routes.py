from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from core.courses import domain, service
from core.courses.errors import CourseAuthRequired, CourseError
from core.courses.store import CourseJobStore
from core.tasks import TaskStore
from core.webui import app as webui
from core.webui import task_router
from starlette.requests import Request


class EmptyBodyRequest:
    async def body(self):
        return b""


def _run(coroutine):
    """Drive these test coroutines directly; patched awaits complete synchronously."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("route test unexpectedly suspended")


def _json_body(value):
    async def read(_request):
        return value
    return read


def _count_homework_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _payload(**overrides):
    body = {
        "task_type": "course",
        "site_id": "zhihuishu",
        "title": "M15 shared 任务",
        "page_url": "https://ignored.example/should-not-be-used",
        "course": {
            "mode": "shared",
            "course_id": "sharedCourse123",
            "speed": 1.5,
            "threshold": 0.9,
            "time_limit_minutes": 0,
            "process_exam": True,
        },
    }
    body.update(overrides)
    return body


def test_four_course_endpoints_success_and_failure_without_taskstore(tmp_path, monkeypatch):
    import core.courses.service as course_service

    course_store = CourseJobStore(tmp_path / "courses")
    task_store = TaskStore(
        tmp_path / "tasks.sqlite3", runs_directory=tmp_path / "runs",
        supported_site_ids=("chaoxing",),
    )
    before = _count_homework_rows(task_store.database_path)
    monkeypatch.setattr(webui, "_json_body", _json_body(_payload()))
    monkeypatch.setattr(webui, "_store", lambda _request: (_ for _ in ()).throw(
        AssertionError("course route touched TaskStore")
    ))
    monkeypatch.setattr(task_router, "_course_store", lambda: course_store)
    monkeypatch.setattr(course_service, "_store", lambda: course_store)

    class Credentials:
        def get_site_cookies(self, _site_id):
            return {"CASLOGC": "stub"}

    class Runtime:
        def start(self, job, params=None):
            current = course_store.get_job(job.job_id)
            updated = course_store.update(
                current, status=domain.CourseStatus.RUNNING, run_id="run-stub"
            )
            course_store.append_business_event(
                job.job_id, "course_started", current.status, domain.CourseStatus.RUNNING
            )
            return SimpleNamespace(job_id=updated.job_id)

    monkeypatch.setattr(course_service, "LocalCredentialProvider", Credentials)
    monkeypatch.setattr(course_service.preflight, "check_cookies", lambda _cookies: "uuid")
    monkeypatch.setattr(course_service, "_runtime", lambda: Runtime())

    created = _run(webui.create_task(SimpleNamespace()))
    task_id = created["task_id"]
    assert created["task_type"] == "course"
    assert course_store.get_job(task_id).status is domain.CourseStatus.DRAFT
    assert _count_homework_rows(task_store.database_path) == before

    monkeypatch.setattr(
        webui, "_json_body", _json_body({"speed": 2.0, "threshold": 0.95}),
    )
    settings = _run(webui.update_course_settings(SimpleNamespace(), task_id))
    assert settings["task"]["task_id"] == task_id
    assert course_store.get_job(task_id).options.speed == 2.0

    started = _run(webui.start_course_task(EmptyBodyRequest(), task_id))
    assert started["task"]["status"] == "running"

    rerun_job = service.create_job("zhihuishu", "rerun 任务", _payload()["course"])
    course_store.update(rerun_job, status=domain.CourseStatus.COMPLETED)
    rerun = _run(webui.rerun_course_task(EmptyBodyRequest(), rerun_job.job_id))
    assert rerun["task"]["status"] == "running"

    draft_job = service.create_job("zhihuishu", "draft 任务", _payload()["course"])

    monkeypatch.setattr(
        webui, "_json_body", _json_body(_payload(course={"mode": "shared", "course_id": "123"}))
    )
    with pytest.raises(CourseError) as create_error:
        _run(webui.create_task(SimpleNamespace()))
    assert create_error.value.status_code == 422
    with pytest.raises(CourseError) as start_error:
        _run(webui.start_course_task(EmptyBodyRequest(), "course-" + "0" * 32))
    assert start_error.value.status_code == 404
    with pytest.raises(CourseError) as rerun_error:
        _run(webui.rerun_course_task(EmptyBodyRequest(), draft_job.job_id))
    assert rerun_error.value.status_code == 409
    monkeypatch.setattr(
        webui, "_json_body", _json_body({"threshold": 0})
    )
    with pytest.raises(CourseError) as settings_error:
        _run(webui.update_course_settings(SimpleNamespace(), draft_job.job_id))
    assert settings_error.value.status_code == 422

    assert _count_homework_rows(task_store.database_path) == before
    task_store.close()

    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", None) or ())))
        for route in webui.app.routes
    }
    assert ("/api/tasks", ("POST",)) in routes
    assert ("/api/tasks/{task_id}/course/start", ("POST",)) in routes
    assert ("/api/tasks/{task_id}/course/rerun", ("POST",)) in routes
    assert ("/api/tasks/{task_id}/course-settings", ("PUT",)) in routes


def test_cross_origin_post_remains_rejected():
    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/tasks",
        "raw_path": b"/api/tasks",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:8877"),
            (b"origin", b"http://evil.example"),
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8877),
    })
    coroutine = webui._local_only_and_security_headers(
        request, lambda _request: (_ for _ in ()).throw(AssertionError("request passed"))
    )
    try:
        coroutine.send(None)
    except StopIteration as result:
        response = result.value
    else:
        raise AssertionError("cross-origin check unexpectedly suspended")
    assert response.status_code == 403

