from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from core.courses import service
from core.courses.domain import CourseMode, CourseOptions
from core.courses.errors import CourseError, CourseInvalid, CourseUnsupported

PROFILE_ID = "12345678-1234-5678-1234-567812345678"
PROFILE_SETTINGS = {
    "base_url": "https://api.openai.com/v1/chat/completions",
    "api_key": "test-key",
    "model": "gpt-test",
}


@pytest.fixture(autouse=True)
def resolved_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service.ModelProfileStore,
        "resolve_settings",
        lambda _store, _profile_id: dict(PROFILE_SETTINGS),
    )


@pytest.mark.parametrize(
    ("mode", "course_id", "class_id", "options"),
    [
        (CourseMode.SHARED, "abc123", None, CourseOptions()),
        (CourseMode.SHARED, "a" * 128, None, CourseOptions()),
        (CourseMode.HIKE, "123", None, CourseOptions()),
        (CourseMode.HIKE, "0", None, CourseOptions()),
        (
            CourseMode.AI,
            "123",
            "456",
            CourseOptions(model_profile_id=PROFILE_ID),
        ),
        (CourseMode.SHARED, "abc", None, replace(CourseOptions(), speed=0.1)),
        (CourseMode.SHARED, "abc", None, replace(CourseOptions(), speed=500)),
        (CourseMode.SHARED, "abc", None, replace(CourseOptions(), threshold=0.01)),
        (CourseMode.SHARED, "abc", None, replace(CourseOptions(), threshold=1)),
        (
            CourseMode.SHARED,
            "abc",
            None,
            replace(CourseOptions(), time_limit_minutes=0),
        ),
        (
            CourseMode.SHARED,
            "abc",
            None,
            replace(CourseOptions(), time_limit_minutes=14400),
        ),
        (
            CourseMode.SHARED,
            "abc",
            None,
            replace(CourseOptions(), process_exam=False),
        ),
    ],
)
def test_validate_create_accepts_12_valid_parameter_sets(
    mode: CourseMode,
    course_id: str,
    class_id: str | None,
    options: CourseOptions,
) -> None:
    service.validate_create("zhihuishu", mode, course_id, class_id, options)


@pytest.mark.parametrize(
    ("site_id", "mode", "course_id", "class_id", "options", "error_type", "field"),
    [
        (
            "zhihuishu",
            "unsupported",
            "abc",
            None,
            CourseOptions(),
            CourseInvalid,
            "mode",
        ),
        (
            "zhihuishu",
            CourseMode.SHARED,
            "",
            None,
            CourseOptions(),
            CourseInvalid,
            "course_id",
        ),
        (
            "zhihuishu",
            CourseMode.SHARED,
            "123456",
            None,
            CourseOptions(),
            CourseInvalid,
            "course_id",
        ),
        (
            "zhihuishu",
            CourseMode.HIKE,
            "12a",
            None,
            CourseOptions(),
            CourseInvalid,
            "course_id",
        ),
        (
            "zhihuishu",
            CourseMode.AI,
            "12a",
            "456",
            CourseOptions(model_profile_id=PROFILE_ID),
            CourseInvalid,
            "course_id",
        ),
        (
            "zhihuishu",
            CourseMode.AI,
            "123",
            None,
            CourseOptions(model_profile_id=PROFILE_ID),
            CourseInvalid,
            "class_id",
        ),
        (
            "zhihuishu",
            CourseMode.AI,
            "123",
            "456",
            CourseOptions(),
            CourseInvalid,
            "model_profile_id",
        ),
        (
            "zhihuishu",
            CourseMode.SHARED,
            "abc",
            None,
            replace(CourseOptions(), speed=500.01),
            CourseInvalid,
            "speed",
        ),
        (
            "zhihuishu",
            CourseMode.SHARED,
            "abc",
            None,
            replace(CourseOptions(), threshold=0),
            CourseInvalid,
            "threshold",
        ),
        *[
            (
                "zhihuishu",
                CourseMode.SHARED,
                "abc",
                None,
                {"time_limit_minutes": invalid},
                CourseInvalid,
                "time_limit_minutes",
            )
            for invalid in (14401, True)
        ],
        (
            "zhihuishu",
            CourseMode.SHARED,
            "abc",
            None,
            {"process_exam": "false"},
            CourseInvalid,
            "process_exam",
        ),
    ],
)
def test_validate_create_rejects_12_invalid_parameter_cases(
    site_id: str,
    mode: CourseMode | str,
    course_id: str,
    class_id: str | None,
    options: CourseOptions | dict[str, Any],
    error_type: type[CourseError],
    field: str | None,
) -> None:
    with pytest.raises(error_type) as error:
        service.validate_create(site_id, mode, course_id, class_id, options)
    if field:
        assert field in error.value.message


