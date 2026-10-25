"""T2-04 验收：摘要 18 键、详情五壳、course 块、事件白名单与脱敏。

这里的断言全部按 docs/TASKS.md §5.1 / §5.2 / §5.3 写成字面量，**不复用
`dto.SUMMARY_KEYS` 之外的口径**：键集合用常量比对（改了常量这里就红），
其余字段值直接写字面量，避免"用实现证明实现"。
"""

from __future__ import annotations

import json

import pytest

from core.courses import dto
from core.courses.domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
)


def make_job(**overrides: object) -> CourseJob:
    """造一条最普通的课程任务；用例只覆盖自己关心的字段。"""
    values: dict[str, object] = {
        "job_id": "course-" + "a" * 32,
        "site_id": "zhihuishu",
        "title": "高等数学（上）",
        "mode": CourseMode.SHARED,
        "course_id": "3001",
        "class_id": "9001",
        "status": CourseStatus.READY,
        "phase": CoursePhase.CATALOG,
        "current_title": "1.1 极限的定义",
        "progress_percent": 33.33,
        "completed_count": 3,
        "total_count": 10,
        "failed_count": 0,
        "run_id": "run-7",
        "options": CourseOptions(speed=2.0, threshold=0.9, model_profile_id="p-1"),
        "created_at": "2026-10-23T10:00:00.000+08:00",
        "updated_at": "2026-10-23T10:05:00.000+08:00",
        "last_safe_error_category": None,
    }
    values.update(overrides)
    return CourseJob(**values)  # type: ignore[arg-type]


# --- §5.1 摘要 ---------------------------------------------------------------


def test_summary_keys_equal_contract() -> None:
    """键集合必须与 SUMMARY_KEYS 完全一致（多一个少一个都算破坏契约）。"""
    payload = dto.summary(make_job())
    assert set(payload) == dto.SUMMARY_KEYS
    assert len(payload) == 18


def test_summary_course_fixed_values() -> None:
    payload = dto.summary(make_job())
    assert payload["task_type"] == "course"
    assert payload["run_id"] is None
    assert payload["queue_state"] is None
    assert payload["queue_position"] is None
    assert payload["interaction_retry_authorized"] is False


def test_summary_maps_job_fields() -> None:
    payload = dto.summary(make_job())
    assert payload["task_id"] == "course-" + "a" * 32
    assert payload["site_id"] == "zhihuishu"
    assert payload["status"] == "ready"
    assert payload["current_step"] == "catalog"
    assert payload["model_profile_id"] == "p-1"
    assert payload["title"] == "高等数学（上）"


def test_index_summary_matches_job_and_rebuilds_old_index(tmp_path) -> None:
    import json
    from core.courses.store import CourseJobStore

    store = CourseJobStore(tmp_path / "courses")
    job = make_job()
    store.create(job)
    entry = store.list_summaries()[0]
    assert dto.summary(entry) == dto.summary(job)
    assert entry["model_profile_id"] == "p-1"
    index_path = store.root / "index.json"
    old = json.loads(index_path.read_text(encoding="utf-8"))
    del old["jobs"][0]["model_profile_id"]
    index_path.write_text(json.dumps(old), encoding="utf-8")
    assert dto.summary(store.list_summaries()[0]) == dto.summary(job)


def test_summary_desensitizes_url_in_title() -> None:
    payload = dto.summary(make_job(title="看这个 https://studyh5.zhihuishu.com/x?a=1 的课"))
    assert "https://" not in payload["title"]
    assert "studyh5.zhihuishu.com" not in payload["title"]
    assert "网址已隐藏" in payload["title"]


def test_summary_blank_title_falls_back() -> None:
    assert dto.summary(make_job(title="   "))["title"] == "未命名任务"


def test_summary_hint_comes_from_sites() -> None:
    assert dto.summary(make_job())["authentication_hint"] == (
        "在浏览器中完成智慧树登录并停留在课程页面。"
    )
    assert dto.summary(make_job(site_id="unknown-site"))["authentication_hint"] is None


# --- §5.2 course 块 ----------------------------------------------------------


def test_course_block_keys_and_options() -> None:
    block = dto.course_block(make_job())
    assert set(block) == dto.COURSE_KEYS
    assert set(block["options"]) == {"speed", "threshold", "time_limit_minutes", "process_exam"}
    assert block["mode"] == "shared"
    assert block["progress_percent"] == 33.33
    assert block["run_id"] == "run-7"
    assert block["options"]["speed"] == 2.0
    assert block["options"]["process_exam"] is True


def test_course_block_keeps_phase_literal() -> None:
    block = dto.course_block(make_job(phase=CoursePhase.EXAM))
    assert block["phase"] == "exam"


# --- §5.2 详情外壳 -----------------------------------------------------------


def test_detail_shell_keys() -> None:
    payload = dto.detail(make_job())
    assert set(payload) == dto.DETAIL_KEYS
    assert payload["queue"] is None
    assert payload["cookie_status"] == {"captured": False, "configured": False}
    assert payload["events"] == []
    assert set(payload["task"]) == dto.SUMMARY_KEYS
    assert set(payload["course"]) == dto.COURSE_KEYS


def test_detail_cookie_status_whitelists_and_coerces() -> None:
    payload = dto.detail(make_job(), cookie_status={"captured": 1, "configured": 0, "cookie": "SESSION=secret"})
    assert payload["cookie_status"] == {"captured": True, "configured": False}


def test_detail_cookie_status_tolerates_none() -> None:
    assert dto.detail(make_job(), cookie_status=None)["cookie_status"] == {
        "captured": False,
        "configured": False,
    }


# --- §5.3 事件 ---------------------------------------------------------------


def test_events_whitelist_drops_unknown_keys() -> None:
    job = make_job()
    records = [
        {
            "event_id": 1,
            "task_id": job.job_id,
            "occurred_at": "2026-10-23T10:00:01.000+08:00",
            "event_type": "created",
            "from_status": None,
            "to_status": CourseStatus.DRAFT,
            "operation": None,
            "safe_error_category": None,
            "page_url": "https://secret.example/x",
            "raw_error": "Traceback ...",
        }
    ]
    entries = dto.events(job, records=records)
    assert len(entries) == 1
    assert set(entries[0]) == dto.EVENT_KEYS
    assert entries[0]["to_status"] == "draft"
    assert "secret.example" not in json.dumps(entries, ensure_ascii=False)


def test_events_limit_keeps_tail_and_zero_returns_empty() -> None:
    job = make_job()
    records = [{"event_id": index, "event_type": "status"} for index in range(1, 6)]
    assert [item["event_id"] for item in dto.events(job, 2, records)] == [4, 5]
    assert dto.events(job, 0, records) == []
    assert dto.events(job, 10, None) == []


def test_events_rejects_bad_limit() -> None:
    with pytest.raises(ValueError):
        dto.events(make_job(), -1)


def test_detail_passes_events_through() -> None:
    job = make_job()
    payload = dto.detail(job, events=[{"event_id": 1, "event_type": "created"}])
    assert payload["events"][0]["event_id"] == 1
    assert payload["events"][0]["task_id"] == job.job_id


# --- 安全 -------------------------------------------------------------------


def test_payload_has_no_secret_surface() -> None:
    """课程 DTO 不产出 cookie / api_key / 完整 URL 任何一个字段。"""
    job = make_job(title="https://studyh5.zhihuishu.com/course/1")
    dumped = json.dumps(dto.detail(job), ensure_ascii=False)
    for banned in ("api_key", "Cookie", "cookie=", "https://", "page_url"):
        assert banned not in dumped


def test_course_overview_reads_only_index_after_initialization(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path
    from core.webui import app as webui
    from core.courses.domain import CourseOptions
    from core.courses.store import CourseJobStore

    store = CourseJobStore(tmp_path / "courses")
    job = make_job(options=CourseOptions(model_profile_id="profile-kept"))
    store.create(job)
    monkeypatch.setattr(webui, "CourseJobStore", lambda: store)
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == "job.json":
            raise AssertionError("overview must not read job.json")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    summaries = webui._course_overview(False)
    assert len(summaries) == 1
    assert summaries[0]["task_id"] == job.job_id
    assert summaries[0]["model_profile_id"] == "profile-kept"

