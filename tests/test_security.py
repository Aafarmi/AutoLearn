"""M16 安全十条与课程错误矩阵回归。"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from core.common.clock import timestamp
from core.courses import dto
from core.courses.domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
)
from core.courses.errors import (
    CourseAuthRequired,
    CourseBusy,
    CourseError,
    CourseInvalid,
    CourseNotFound,
    CourseProtocolError,
    CourseTransitionError,
    CourseUnsupported,
    safe_course_message,
)
from core.courses.providers.zhihuishu import bridge, preflight
from core.courses.runtime import CourseRuntime, sanitize_diagnostic
from core.courses.store import CourseJobStore
from core.webui import app as webui


def make_job(
    *,
    status: CourseStatus = CourseStatus.READY,
    title: str = "安全测试课程",
    job_key: str = "a",
) -> CourseJob:
    now = timestamp()
    return CourseJob(
        job_id="course-" + job_key * 32,
        site_id="zhihuishu",
        title=title,
        mode=CourseMode.SHARED,
        course_id="course-123",
        class_id=None,
        status=status,
        phase=CoursePhase.CATALOG,
        current_title="第一章",
        progress_percent=20.0,
        completed_count=1,
        total_count=5,
        failed_count=0,
        run_id=None,
        options=CourseOptions(),
        created_at=now,
        updated_at=now,
        last_safe_error_category=None,
    )


def _run_immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("expected a non-suspending security response")


def _middleware_response(*, client, host: str, origin: str | None):
    headers = [(b"host", host.encode("ascii"))]
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/tasks",
            "raw_path": b"/api/tasks",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": client,
            "server": ("127.0.0.1", 8877),
        }
    )
    return _run_immediate(
        webui._local_only_and_security_headers(
            request,
            lambda _request: (_ for _ in ()).throw(AssertionError("request bypassed guard")),
        )
    )


def test_error_matrix_has_expected_status_and_safe_chinese_messages():
    cases = [
        (CourseInvalid("课程参数无效。"), 422),
        (CourseTransitionError("当前状态不允许此操作。"), 409),
        (CourseBusy("课程任务正在运行，请稍后重试。"), 409),
        (CourseNotFound("课程任务不存在。"), 404),
        (CourseAuthRequired("课程登录状态无效，请重新登录后重试。"), 409),
        (CourseUnsupported("当前课程能力暂不支持。"), 422),
        (CourseProtocolError("课程运行协议异常，请重试。"), 502),
    ]
    for error, expected_status in cases:
        response = _run_immediate(webui._course_error(None, error))
        message = json.loads(response.body)["detail"]
        assert response.status_code == expected_status
        assert message.endswith("。") and any("\u3400" <= ch <= "\u9fff" for ch in message)
        assert not any(token in message.casefold() for token in ("traceback", "cookie", "api_key"))
        assert "http://" not in message.casefold() and "https://" not in message.casefold()
        assert not any(f"{drive}:\\" in message for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        assert "/tmp/" not in message and "/temp/" not in message


def test_error_handler_replaces_unsafe_exception_text_with_safe_fallback():
    error = CourseProtocolError("Traceback: C:\\Temp\\secret\\cookie=abc api_key=xyz")
    response = _run_immediate(webui._course_error(None, error))
    message = json.loads(response.body)["detail"]
    assert response.status_code == 502
    assert message == "课程运行协议异常，请重试。"
    assert "Traceback" not in message and "C:\\" not in message


def test_01_response_keys_are_whitelisted():
    job = make_job()
    summary = dto.summary(job)
    detail = dto.detail(
        job,
        cookie_status={"captured": True, "configured": True, "raw_cookie": "hidden"},
        events=[
            {
                "event_id": 1,
                "task_id": job.job_id,
                "event_type": "created",
                "unknown_secret": "hidden",
            }
        ],
    )
    assert set(summary) == dto.SUMMARY_KEYS
    assert set(detail) == dto.DETAIL_KEYS
    assert set(detail["cookie_status"]) == {"captured", "configured"}
    assert set(detail["events"][0]) == dto.EVENT_KEYS


def test_02_response_does_not_contain_full_url():
    job = make_job(title="课程 https://example.test/course?id=secret")
    response = json.dumps(dto.detail(job), ensure_ascii=False)
    assert "https://example.test" not in response
    assert "id=secret" not in response
    assert "网址已隐藏" in response


def test_03_cookie_is_sent_only_in_child_stdin(tmp_path, monkeypatch):
    store = CourseJobStore(tmp_path / "courses")
    job = make_job()
    store.create(job)
    probe_bridge = tmp_path / "probe_bridge.py"
    probe_bridge.write_text(
        "import json, os, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        "json.dump({'request': request, 'env': dict(os.environ), 'argv': sys.argv}, "
        "open('probe.json', 'w', encoding='utf-8'))\n"
        "def emit(value): print(json.dumps(value), flush=True)\n"
        "emit({'type':'hello','version':'stub','mode':'shared','upstream_version':'stub'})\n"
        "emit({'type':'result','status':'completed','completed_count':1,"
        "'total_count':1,'failed_count':0,'safe_error_category':None})\n",
        encoding="utf-8",
    )
    sentinel = "SECURITY_COOKIE_SENTINEL_24d719"
    monkeypatch.setattr(
        "core.courses.runtime._site_cookies",
        lambda _site_id: {"CASLOGC": sentinel},
    )
    run_root = tmp_path / "run-root"
    runtime = CourseRuntime(
        store,
        bridge=probe_bridge,
        run_dir_factory=lambda job_id: run_root / job_id,
    )
    run = runtime.start(job)
    run.proc.wait(timeout=15)
    assert run.finalized_event.wait(10)

    probe = json.loads((run_root / job.job_id / "probe.json").read_text(encoding="utf-8"))
    assert probe["request"]["cookies"] == {"CASLOGC": sentinel}
    assert sentinel not in json.dumps(probe["env"])
    assert sentinel not in json.dumps(probe["argv"])


def test_04_stderr_diagnostic_redaction_reuses_m8_sanitizer():
    secret = "a1b2c3d4" * 8
    text = sanitize_diagnostic(
        f"CASLOGC={secret}; Cookie: SESSION={secret}; Authorization: Bearer {secret}; "
        f"https://example.test/path?token={secret}"
    )
    assert secret not in text
    assert "https://example.test/path?token=" + secret not in text
    assert "CASLOGC=" + secret not in text


def test_05_upstream_logger_is_lowered_to_warning(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "meta.json").write_text('{"version":"test"}', encoding="utf-8")
    fake_logger = logging.getLogger("security-test-upstream")
    fake_logger.setLevel(logging.DEBUG)
    fake_fucker = SimpleNamespace(logger=fake_logger)
    monkeypatch.setitem(sys.modules, "fucker", fake_fucker)
    monkeypatch.chdir(tmp_path)
    events = io.StringIO()
    result = {
        "type": "result",
        "status": "completed",
        "completed_count": 1,
        "total_count": 1,
        "failed_count": 0,
        "safe_error_category": None,
    }

    assert bridge.run({}, events, execute=lambda *_args: result) == 0
    assert fake_logger.level == logging.WARNING


def test_06_run_data_is_created_under_courses_data_path(tmp_path, monkeypatch):
    data_root = tmp_path / "user-data"
    monkeypatch.setattr(
        preflight, "data_path", lambda *parts: data_root.joinpath(*parts)
    )

    run_dir = preflight.prepare_run_dir("course-security")

    assert run_dir == data_root / "courses" / "jobs" / "course-security" / "run"
    assert (run_dir / "upstream" / "fucker.py").is_file()
    assert not (tmp_path / "courses").exists()


def test_07_delete_cleans_course_run_directory(tmp_path):
    store = CourseJobStore(tmp_path / "data" / "courses")
    job = make_job()
    store.create(job)
    run_dir = store.job_dir(job.job_id) / "run"
    run_dir.mkdir()
    (run_dir / "diagnostics.log").write_text("redacted log", encoding="utf-8")
    (run_dir / "upstream").mkdir()
    (run_dir / "upstream" / "fucker.py").write_text("copy", encoding="utf-8")

    store.delete(job.job_id)

    assert not store.job_dir(job.job_id).exists()
    with pytest.raises(CourseNotFound):
        store.get_job(job.job_id)


def test_08_write_requests_keep_loopback_host_and_origin_guards():
    assert _middleware_response(
        client=("192.0.2.1", 12345),
        host="127.0.0.1:8877",
        origin="http://127.0.0.1:8877",
    ).status_code == 403
    assert _middleware_response(
        client=("127.0.0.1", 12345),
        host="evil.example:8877",
        origin="http://127.0.0.1:8877",
    ).status_code == 400
    assert _middleware_response(
        client=("127.0.0.1", 12345),
        host="127.0.0.1:8877",
        origin="http://evil.example",
    ).status_code == 403


def test_09_dto_never_emits_api_key():
    job = make_job()
    # model_profile_id is safe to expose; the resolved API key exists only in runtime config.
    response = json.dumps(dto.detail(job), ensure_ascii=False).casefold()
    assert "api_key" not in response
    assert "secret-test-key" not in response


def test_10_only_one_job_can_hold_a_site_slot(tmp_path):
    store = CourseJobStore(tmp_path / "courses")
    first = make_job()
    second = make_job(job_key="b")
    store.create(first)
    store.create(second)
    store.acquire_slot("zhihuishu", first.job_id, os.getpid())

    with pytest.raises(CourseBusy):
        store.acquire_slot("zhihuishu", second.job_id, os.getpid())
    assert store.active_slot("zhihuishu")["job_id"] == first.job_id
