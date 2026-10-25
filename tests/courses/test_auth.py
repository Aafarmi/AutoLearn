"""T4-03/04/05 + T5-01 验收：抓取入口 / 凭据白名单 / 预检 / 认证编排。

不碰真浏览器：`cookie_manager` 的浏览器流程在 `tests/` 里没有替身价值（它已经是
被测过的既有代码），这里只锁四件事——
① `capture_for_adapter` 仍然走 `_load_adapter` + `capture_for_module`（做题行为零变化）；
② `_validate_module` 是唯一校验口径，课程侧适配器也能过；
③ 预检按上游 `Fucker.cookies` 的口径判 CASLOGC/uuid，且不回显 Cookie 值；
④ 认证编排的状态迁移与失败回退（`draft|ready → authenticating → ready`，失败回 `draft`）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from core.auth import cookie_manager
from core.credentials import LocalCredentialProvider
from core.courses import service
from core.courses.domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    job_id,
)
from core.courses.errors import (
    CourseAuthRequired,
    CourseBusy,
    CourseNotFound,
    CourseTransitionError,
)
from core.courses.providers.zhihuishu import auth as zhihuishu_auth
from core.courses.providers.zhihuishu import preflight
from core.courses.store import CourseJobStore


def make_job(status: CourseStatus = CourseStatus.DRAFT, run: str | None = None) -> CourseJob:
    return CourseJob(
        job_id=job_id(),
        site_id="zhihuishu",
        title="高等数学（上）",
        mode=CourseMode.SHARED,
        course_id="3001",
        class_id=None,
        status=status,
        phase=CoursePhase.AUTHENTICATION,
        current_title=None,
        progress_percent=0.0,
        completed_count=0,
        total_count=0,
        failed_count=0,
        run_id=run,
        options=CourseOptions(),
        created_at="2026-10-23T10:00:00.000+08:00",
        updated_at="2026-10-23T10:00:00.000+08:00",
        last_safe_error_category=None,
    )



GOOD_HEADER = "CASLOGC=" + quote(json.dumps({"uuid": "u-1234"}))


# --- 预检 -------------------------------------------------------------------


def test_preflight_accepts_upstream_shape() -> None:
    assert preflight.check_cookies({"CASLOGC": quote(json.dumps({"uuid": "u-9"}))}) == "u-9"


@pytest.mark.parametrize(
    "cookies",
    [
        None,
        {},
        {"CASLOGC": ""},
        {"CASLOGC": "   "},
        {"SOMEOTHER": "x"},
        {"CASLOGC": "not json at all"},
        {"CASLOGC": quote(json.dumps({"uuid": ""}))},
        {"CASLOGC": quote(json.dumps(["uuid"]))},
        {"CASLOGC": quote(json.dumps({"uuid": 5}))},
    ],
)
def test_preflight_rejects_unusable_cookies(cookies: object) -> None:
    with pytest.raises(CourseAuthRequired) as excinfo:
        preflight.check_cookies(cookies)  # type: ignore[arg-type]
    assert excinfo.value.status_code == 409
    assert "重新登录" in excinfo.value.message


def test_preflight_never_echoes_cookie_value() -> None:
    secret = "SUPER-SECRET-COOKIE"
    with pytest.raises(CourseAuthRequired) as excinfo:
        preflight.check_cookies({"CASLOGC": secret})
    assert secret not in excinfo.value.message


def test_credentials_whitelist_includes_zhihuishu(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"zhihuishu": {"cookie": GOOD_HEADER}}), encoding="utf-8"
    )
    cookies = LocalCredentialProvider(config).get_site_cookies("zhihuishu")
    assert cookies["CASLOGC"]


# --- 抓取入口 ---------------------------------------------------------------


def test_validate_module_rejects_incomplete_adapter() -> None:
    broken = SimpleNamespace(SITE_ID="zhihuishu")
    with pytest.raises(cookie_manager.CookieManagerError):
        cookie_manager._validate_module(broken)


def test_validate_module_rejects_site_id_mismatch() -> None:
    with pytest.raises(cookie_manager.CookieManagerError) as excinfo:
        cookie_manager._validate_module(zhihuishu_auth, expected_site_id="chaoxing")
    assert "site_id" in str(excinfo.value)


def test_validate_module_accepts_course_provider() -> None:
    """课程适配器不在 core.adapters 下，但校验口径必须是同一份。"""
    cookie_manager._validate_module(zhihuishu_auth)


def test_validate_module_rejects_http_capture_start_url() -> None:
    broken = SimpleNamespace(
        SITE_ID="zhihuishu",
        CONFIG_SECTION="zhihuishu",
        URL_FIELD="url",
        COOKIE_FIELD="cookie",
        ALLOWED_COOKIE_DOMAINS=("zhihuishu.com",),
        is_authenticated=lambda page: True,
        serialize_cookies=lambda records: "",
        CAPTURE_START_URL="http://www.zhihuishu.com/",
    )
    with pytest.raises(cookie_manager.CookieManagerError):
        cookie_manager._validate_module(broken)


def test_capture_for_adapter_still_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """做题侧入口的行为不变：先 _load_adapter，再交给 capture_for_module。"""
    adapter = object()
    seen: list[tuple] = []
    monkeypatch.setattr(cookie_manager, "_load_adapter", lambda site_id: adapter)
    monkeypatch.setattr(
        cookie_manager,
        "capture_for_module",
        lambda module, page_url=None, **kwargs: seen.append((module, page_url, kwargs)),
    )

    cookie_manager.capture_for_adapter("chaoxing", "https://x.chaoxing.com/a")

    assert seen[0][0] is adapter
    assert seen[0][1] == "https://x.chaoxing.com/a"


def test_homework_adapters_still_pass_module_validation() -> None:
    """重构回归：做题侧适配器（超星/雨课堂）必须仍能通过同一套校验。"""
    adapters = cookie_manager._available_adapters()
    assert "chaoxing" in adapters
    for site_id in adapters:
        cookie_manager._load_adapter(site_id)



# --- config.json 段补齐 -----------------------------------------------------


def test_ensure_config_section_creates_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cookie_manager, "CONFIG_PATH", config_path)

    assert cookie_manager.ensure_config_section(zhihuishu_auth) is True
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"zhihuishu": {}}


def test_ensure_config_section_preserves_other_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"chaoxing": {"cookie": "a=1"}, "model_profiles": {"x": 1}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cookie_manager, "CONFIG_PATH", config_path)

    assert cookie_manager.ensure_config_section(zhihuishu_auth) is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["zhihuishu"] == {}
    assert payload["chaoxing"] == {"cookie": "a=1"}
    assert payload["model_profiles"] == {"x": 1}

    assert cookie_manager.ensure_config_section(zhihuishu_auth) is False


def test_ensure_config_section_refuses_non_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"zhihuishu": "oops"}), encoding="utf-8")
    monkeypatch.setattr(cookie_manager, "CONFIG_PATH", config_path)

    with pytest.raises(cookie_manager.CookieManagerError):
        cookie_manager.ensure_config_section(zhihuishu_auth)
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"zhihuishu": "oops"}


# --- 认证编排 ---------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """临时 store + 临时 config.json；capture 由用例自己决定写什么。"""
    store = CourseJobStore(tmp_path / "courses")
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(service, "_store", lambda: store)
    monkeypatch.setattr(cookie_manager, "CONFIG_PATH", config_path)
    monkeypatch.setattr(
        "core.credentials.DEFAULT_CONFIG_PATH", config_path, raising=False
    )
    monkeypatch.setattr(
        service, "_cookies", lambda site_id: LocalCredentialProvider(config_path).get_site_cookies(site_id)
    )
    return SimpleNamespace(store=store, config=config_path)


def write_cookie(config_path: Path, header: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"zhihuishu": {"cookie": header}}
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def test_authenticate_draft_to_ready(env, monkeypatch: pytest.MonkeyPatch) -> None:
    job = make_job(CourseStatus.DRAFT)
    env.store.create(job)
    monkeypatch.setattr(service, "_capture", lambda adapter: write_cookie(env.config, GOOD_HEADER))

    updated = service.authenticate(job.job_id)

    assert updated.status is CourseStatus.READY
    types = [event["event_type"] for event in env.store.read_business_events(job.job_id)]
    assert types == ["auth_started", "authenticated"]


def test_authenticate_ready_reauthenticates(env, monkeypatch: pytest.MonkeyPatch) -> None:
    job = make_job(CourseStatus.READY)
    env.store.create(job)
    monkeypatch.setattr(service, "_capture", lambda adapter: write_cookie(env.config, GOOD_HEADER))

    assert service.authenticate(job.job_id).status is CourseStatus.READY


def test_authenticate_reports_missing_cookie_as_409(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收③：Cookie 被清空（抓取没有拿到可用值）→ 409 且提示重新登录。"""
    job = make_job(CourseStatus.DRAFT)
    env.store.create(job)
    monkeypatch.setattr(service, "_capture", lambda adapter: write_cookie(env.config, ""))

    with pytest.raises(CourseAuthRequired) as excinfo:
        service.authenticate(job.job_id)

    assert excinfo.value.status_code == 409
    assert "重新登录" in excinfo.value.message
    assert env.store.get_job(job.job_id).status is CourseStatus.DRAFT
    types = [event["event_type"] for event in env.store.read_business_events(job.job_id)]
    assert types == ["auth_started", "auth_failed"]


def test_authenticate_maps_capture_error_to_409(env, monkeypatch: pytest.MonkeyPatch) -> None:
    job = make_job(CourseStatus.DRAFT)
    env.store.create(job)

    def boom(adapter):
        raise cookie_manager.CookieManagerError("未识别到已登录的目标作业页面；config.json 未修改。")

    monkeypatch.setattr(service, "_capture", boom)

    with pytest.raises(CourseAuthRequired) as excinfo:
        service.authenticate(job.job_id)
    assert excinfo.value.status_code == 409
    assert "重新登录" in excinfo.value.message
    assert env.store.get_job(job.job_id).status is CourseStatus.DRAFT


def test_authenticate_rejects_running_job(env) -> None:
    job = make_job(CourseStatus.RUNNING, run="run-1")
    env.store.create(job)

    with pytest.raises(CourseBusy):
        service.authenticate(job.job_id)
    assert env.store.get_job(job.job_id).status is CourseStatus.RUNNING


def test_authenticate_rejects_failed_job(env) -> None:
    job = make_job(CourseStatus.FAILED)
    env.store.create(job)

    with pytest.raises(CourseTransitionError):
        service.authenticate(job.job_id, retry_failed=True)


def test_authenticate_missing_job_is_404(env) -> None:
    with pytest.raises(CourseNotFound):
        service.authenticate(job_id())


def test_authenticate_creates_config_section_before_capture(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job(CourseStatus.DRAFT)
    env.store.create(job)
    seen: list[dict] = []

    def capture(adapter):
        seen.append(json.loads(env.config.read_text(encoding="utf-8")))
        write_cookie(env.config, GOOD_HEADER)

    monkeypatch.setattr(service, "_capture", capture)
    service.authenticate(job.job_id)

    assert seen[0]["zhihuishu"] == {}
