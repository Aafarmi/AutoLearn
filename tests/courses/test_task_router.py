"""T1-04 分发层：is_course_id 边界 + resolve_task + 六个转发函数。

课程存储（CourseJobStore，T2-02）尚未落地，因此课程侧用实现同一契约的
FakeCourseStore 注入；真实存储一落地，`_course_store()` 自动改用它，
本文件无需改动。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.courses import domain
from core.courses.errors import CourseError, CourseNotFound
from core.tasks import QueueState
from core.webui import task_router


# --- 替身 -------------------------------------------------------------------


class FakeCourseStore:
    def __init__(self, jobs: dict[str, object] | None = None) -> None:
        self.jobs = dict(jobs or {})
        self.calls: list[tuple] = []

    def get_job(self, job_id: str) -> object:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]

    def request_stop(self, job_id: str) -> object:
        self.calls.append(("request_stop", job_id))
        return self.jobs[job_id]

    def archive_job(self, job_id: str) -> object:
        self.calls.append(("archive_job", job_id))
        return self.jobs[job_id]

    def delete_job(self, job_id: str) -> object:
        self.calls.append(("delete_job", job_id))
        return self.jobs[job_id]

    def events_after(self, job_id: str, after: int = 0) -> list:
        self.calls.append(("events_after", job_id, after))
        return [{"type": "progress", "after": after}]

    def request_authentication(self, job_id: str, retry_failed: bool = False) -> object:
        self.calls.append(("request_authentication", job_id, retry_failed))
        return self.jobs[job_id]


class FakeTaskStore:
    def __init__(self, tasks: dict[str, object] | None = None, queue_state=None) -> None:
        self.tasks = dict(tasks or {})
        self.queue_state = queue_state
        self.deleted: list[str] = []

    def get_task(self, task_id: str) -> object:
        if task_id not in self.tasks:
            raise KeyError(task_id)
        return self.tasks[task_id]

    def get_queue_item(self, task_id: str) -> object:
        return SimpleNamespace(state=self.queue_state, operation="stop")

    def cancel_queued_operation(self, task_id: str) -> object:
        return self.tasks[task_id]

    def request_cancel_active_operation(self, task_id: str) -> None:
        return None

    def archive_task(self, task_id: str) -> object:
        return self.tasks[task_id]

    def delete_task_record(self, task_id: str) -> None:
        self.deleted.append(task_id)

    def delete_task_and_package(self, task_id: str) -> None:
        self.deleted.append(task_id)


class FakeRuntime:
    latest_event_sequence = 7

    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.authenticated: list[tuple] = []

    def events_after(self, after: int, *, task_id: str) -> list:
        return [{"sequence": after + 1, "task_id": task_id}]

    def request_stop(self, task_id: str) -> None:
        self.stopped.append(task_id)

    def enqueue_authentication(self, task_id: str, *, retry_failed: bool = False) -> object:
        self.authenticated.append((task_id, retry_failed))
        return SimpleNamespace(task_id=task_id, operation="authenticate", state="queued")


def make_request(task_store=None, runtime=None) -> SimpleNamespace:
    state = SimpleNamespace(task_store=task_store, runtime=runtime)
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture
def job() -> object:
    return SimpleNamespace(job_id=domain.job_id(), title="高等数学")


@pytest.fixture
def course_store(job: object, monkeypatch: pytest.MonkeyPatch) -> FakeCourseStore:
    store = FakeCourseStore({job.job_id: job})
    monkeypatch.setattr(task_router, "_course_store", lambda: store)
    return store


# --- is_course_id：8 条边界用例 ---------------------------------------------


def test_course_id_accepts_real_job(course_store: FakeCourseStore, job: object) -> None:
    assert task_router.is_course_id(job.job_id) is True


@pytest.mark.parametrize(
    ("case", "task_id"),
    [
        ("只有前缀", "course-"),
        ("前缀+1字符", "course-x"),
        ("大写前缀", "COURSE-" + uuid4().hex),
        ("空串", ""),
        ("带空格", " course-" + uuid4().hex),
        ("普通UUID", str(uuid4())),
        ("hex 长度不对", "course-" + uuid4().hex[:31]),
        ("非 hex 字符", "course-" + "z" * 32),
    ],
)
def test_course_id_rejects_malformed(
    course_store: FakeCourseStore, case: str, task_id: str
) -> None:
    assert task_router.is_course_id(task_id) is False, case


def test_course_id_rejects_well_formed_but_missing(course_store: FakeCourseStore) -> None:
    missing = "course-" + uuid4().hex
    assert task_router.is_course_id(missing) is False


def test_course_id_rejects_non_string(course_store: FakeCourseStore) -> None:
    assert task_router.is_course_id(None) is False
    assert task_router.is_course_id(123) is False


def test_course_id_false_when_store_module_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """T2-02 未落地时：课程 id 一律判 False，而不是抛 ImportError。"""
    monkeypatch.setattr(task_router, "_course_store", lambda: None)
    assert task_router.is_course_id("course-" + uuid4().hex) is False


# --- resolve_task -----------------------------------------------------------


def test_resolve_course(course_store: FakeCourseStore, job: object) -> None:
    kind, target = task_router.resolve_task(make_request(), job.job_id)
    assert kind == "course"
    assert target is job


def test_resolve_course_missing_raises_not_found(course_store: FakeCourseStore) -> None:
    with pytest.raises(CourseNotFound) as excinfo:
        task_router.resolve_task(make_request(), "course-" + uuid4().hex)
    assert excinfo.value.status_code == 404


def test_resolve_homework(monkeypatch: pytest.MonkeyPatch) -> None:
    task = SimpleNamespace(task_id="3f2a0000000000000000000000000000")
    request = make_request(task_store=FakeTaskStore({task.task_id: task}))
    assert task_router.resolve_task(request, task.task_id) == ("homework", task)


def test_resolve_homework_missing_keeps_keyerror() -> None:
    with pytest.raises(KeyError):
        task_router.resolve_task(make_request(task_store=FakeTaskStore()), "nope")


# --- 六个转发函数 -----------------------------------------------------------


def test_get_detail_course(course_store: FakeCourseStore, job: object) -> None:
    assert task_router.get_detail(make_request(), job.job_id) == ("course", job)


def test_stop_course(
    course_store: FakeCourseStore, job: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.courses import service

    called = []
    monkeypatch.setattr(
        service, "_runtime",
        lambda: SimpleNamespace(stop=lambda job_id: called.append(job_id) or True),
    )
    kind, payload = task_router.stop(make_request(), job.job_id)
    assert kind == "course"
    assert payload["stopped"] is True and payload["job"] is True
    assert called == [job.job_id]


def test_archive_course(
    course_store: FakeCourseStore, job: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.courses import service

    monkeypatch.setattr(service, "archive", lambda job_id: course_store.archive_job(job_id))
    kind, payload = task_router.archive(make_request(), job.job_id)
    assert kind == "course" and payload["job"] is job
    assert course_store.calls == [("archive_job", job.job_id)]


def test_delete_course_requires_record_mode(
    course_store: FakeCourseStore, job: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.courses import service

    monkeypatch.setattr(service, "delete", lambda job_id: course_store.delete_job(job_id))
    kind, payload = task_router.delete(make_request(), job.job_id, mode="record")
    assert kind == "course" and payload["deleted"] is True
    with pytest.raises(CourseError) as excinfo:
        task_router.delete(make_request(), job.job_id, mode="package", confirmation="DELETE")
    assert excinfo.value.status_code == 422


def test_runtime_events_course(course_store: FakeCourseStore, job: object) -> None:
    kind, payload = task_router.runtime_events(make_request(), job.job_id, after=3)
    assert kind == "course" and payload["events"][0]["after"] == 3
    assert course_store.calls == [("events_after", job.job_id, 3)]


def test_authenticate_course_goes_through_service(
    course_store: FakeCourseStore, job: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5-01：认证属于服务层，store 不再被问 `request_authentication`。"""
    calls: list[tuple] = []

    def fake_authenticate(job_id: str, *, retry_failed: bool = False) -> object:
        calls.append((job_id, retry_failed))
        return job

    monkeypatch.setattr(
        task_router,
        "_course_service_call",
        lambda action: fake_authenticate if action == "authenticate" else None,
    )
    kind, payload = task_router.authenticate(make_request(), job.job_id, retry_failed=True)

    assert kind == "course" and payload["job"] is job
    assert calls == [(job.job_id, True)]
    assert course_store.calls == []



def test_homework_forwarders_use_task_store() -> None:
    task = SimpleNamespace(task_id="aaaa0000000000000000000000000000")
    store = FakeTaskStore({task.task_id: task}, queue_state=QueueState.QUEUED)
    runtime = FakeRuntime()
    request = make_request(task_store=store, runtime=runtime)

    assert task_router.get_detail(request, task.task_id) == ("homework", task)
    assert task_router.stop(request, task.task_id)[1]["stopped"] is True
    assert task_router.archive(request, task.task_id)[1]["task"] is task
    assert task_router.delete(request, task.task_id, mode="record")[1]["deleted"] is True
    assert task_router.runtime_events(request, task.task_id, after=2)[1]["latest_sequence"] == 7

    kind, payload = task_router.authenticate(request, task.task_id, retry_failed=False)
    assert kind == "homework" and payload["task_id"] == task.task_id
    assert runtime.authenticated == [(task.task_id, False)]


def test_course_stop_routes_through_service_even_without_store_method(
    job: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = FakeCourseStore({job.job_id: job})
    monkeypatch.setattr(bare, "request_stop", None, raising=False)
    monkeypatch.setattr(task_router, "_course_store", lambda: bare)
    monkeypatch.setattr(
        task_router, "_course_service_call",
        lambda action: (lambda _job_id: False) if action == "stop" else None,
    )
    kind, payload = task_router.stop(make_request(), job.job_id)
    assert kind == "course"
    assert payload["job"] is False and payload["stopped"] is False


# --- 真实存储联调（T2-02 落地后自动生效，缺席则跳过） -----------------------


def _real_store_or_skip(tmp_path, monkeypatch: pytest.MonkeyPatch):
    store_module = pytest.importorskip("core.courses.store")
    store = store_module.CourseJobStore(root=tmp_path)
    monkeypatch.setattr(task_router, "_course_store", lambda: store)
    return store


def _real_job() -> object:
    return domain.CourseJob(
        job_id=domain.job_id(),
        site_id="zhihuishu",
        title="高等数学",
        mode=domain.CourseMode.SHARED,
        course_id=None,
        class_id=None,
        status=domain.CourseStatus.DRAFT,
        phase=domain.CoursePhase.SETUP,
        current_title=None,
        progress_percent=0.0,
        completed_count=0,
        total_count=0,
        failed_count=0,
        run_id=None,
        options=domain.CourseOptions(),
        created_at="2026-10-23T21:00:00+08:00",
        updated_at="2026-10-23T21:00:00+08:00",
        last_safe_error_category=None,
    )


def test_is_course_id_against_real_store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _real_store_or_skip(tmp_path, monkeypatch)
    job = _real_job()
    store.create(job)

    assert task_router.is_course_id(job.job_id) is True
    assert task_router.is_course_id("course-" + uuid4().hex) is False


def test_resolve_task_against_real_store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _real_store_or_skip(tmp_path, monkeypatch)
    job = _real_job()
    store.create(job)

    kind, target = task_router.get_detail(make_request(), job.job_id)
    assert kind == "course"
    assert isinstance(target, domain.CourseJob)
    assert target == job

    with pytest.raises(CourseNotFound):
        task_router.resolve_task(make_request(), "course-" + uuid4().hex)


# --- T1-05：路由分支用的课程载荷构造器 --------------------------------------


class FakeDto:
    def detail(self, job: object, events: object = None) -> dict:
        return {"task": {"task_id": job.job_id}, "cookie_status": {}, "queue": None,
                "events": list(events or []), "course": {"mode": "shared"}}

    def summary(self, job: object) -> dict:
        return {"task_id": job.job_id, "task_type": "course"}


def test_course_detail_uses_dto(course_store: FakeCourseStore, job: object,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_router, "_course_dto", lambda: FakeDto())
    payload = task_router.course_detail(job.job_id)
    assert set(payload) == {"task", "cookie_status", "queue", "events", "course"}
    assert payload["queue"] is None and payload["course"]["mode"] == "shared"


def test_course_detail_without_dto_is_501(course_store: FakeCourseStore, job: object,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise CourseError("课程 DTO 尚未落地（T2-04）。", 501)

    monkeypatch.setattr(task_router, "_course_dto", boom)
    with pytest.raises(CourseError) as excinfo:
        task_router.course_detail(job.job_id)
    assert excinfo.value.status_code == 501


def test_course_runtime_events_summarizes_sequence(course_store: FakeCourseStore,
                                                   job: object) -> None:
    course_store.events_after = lambda job_id, after=0: [
        {"sequence": 1, "type": "phase"},
        {"sequence": 4, "type": "progress"},
    ]
    payload = task_router.course_runtime_events(job.job_id, after=0)
    assert payload["latest_sequence"] == 4
    assert len(payload["events"]) == 2


def test_course_delete_rejects_package_mode_before_touching_store(
    course_store: FakeCourseStore, job: object
) -> None:
    with pytest.raises(CourseError) as excinfo:
        task_router.course_delete(job.job_id, mode="package", confirmation="DELETE")
    assert excinfo.value.status_code == 422
    assert course_store.calls == []
