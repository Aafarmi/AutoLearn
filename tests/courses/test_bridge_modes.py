from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest

from core.courses.providers.zhihuishu import bridge


class FakeFuckerBase:
    instances: list["FakeFuckerBase"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.context: dict[Any, Any] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.instances.append(self)


@pytest.fixture(autouse=True)
def clear_instances() -> None:
    FakeFuckerBase.instances.clear()


def _run(mode: str, fake_fucker: type, **payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    instance_state: dict[str, Any] = {
        "fucker_module": SimpleNamespace(Fucker=fake_fucker),
        "completed_count": 0,
        "failed_count": 0,
        "total_count": 0,
    }
    request = {
        "mode": mode,
        "course_id": "course-alpha" if mode == "shared" else "00123",
        "class_id": "007",
        "cookies": {"CASLOGC": "redacted-in-test"},
        "speed": 1.5,
        "threshold": 0.9,
        "time_limit_minutes": 0,
        "process_exam": False,
        "ai_config": {"enabled": True},
        **payload,
    }
    result = bridge.run_course(
        instance_state["fucker_module"], request, StringIO(), instance_state
    )
    return result, instance_state


def test_shared_dispatch_uses_fresh_video_context_for_result() -> None:
    class FakeFucker(FakeFuckerBase):
        def fuckZhidaoCourse(self, course_id: str) -> None:
            self.calls.append(("shared", course_id))
            self.context[course_id] = SimpleNamespace(
                videos={
                    "v1": SimpleNamespace(watchState=1),
                    "v2": SimpleNamespace(watchState=0),
                }
            )

        def getZhidaoContext(self, course_id: str, *, force: bool = False) -> Any:
            self.calls.append(("refresh_shared", course_id, force))
            return self.context[course_id]

    result, state = _run("shared", FakeFucker)

    instance = FakeFucker.instances[-1]
    assert instance.calls == [
        ("shared", "course-alpha"),
        ("refresh_shared", "course-alpha", True),
    ]
    assert instance.kwargs == {
        "cookies": {"CASLOGC": "redacted-in-test"},
        "speed": 1.5,
        "end_thre": 0.9,
        "limit": 0,
        "tree_view": False,
        "progressbar_view": True,
    }
    assert result == {
        "type": "result",
        "status": "partial",
        "completed_count": 1,
        "total_count": 2,
        "failed_count": 0,
        "safe_error_category": "incomplete",
    }
    assert state["stats_mismatch"]["mode"] == "shared"


def test_hike_dispatch_recursively_counts_files_and_keeps_bridge_failures() -> None:
    class FakeFucker(FakeFuckerBase):
        def fuckHikeCourse(self, course_id: str) -> None:
            self.calls.append(("hike", course_id))
            for file_id in ("v1", "v2"):
                try:
                    self.fuckHikeVideo(course_id, file_id)
                except RuntimeError:
                    pass
            self.context[course_id] = SimpleNamespace(
                root=[
                    SimpleNamespace(
                        childList=[
                            SimpleNamespace(
                                childList=None,
                                dataType=3,
                                totalTime=100,
                                studyTime=100,
                            ),
                            SimpleNamespace(
                                childList=None,
                                dataType=3,
                                totalTime=100,
                                studyTime=0,
                            ),
                            SimpleNamespace(childList=None, dataType=2),
                        ]
                    )
                ]
            )

        def fuckHikeVideo(self, course_id: str, file_id: str) -> None:
            if file_id == "v2":
                raise RuntimeError("upstream video failure")

        def getHikeContext(self, course_id: str, *, force: bool = False) -> Any:
            self.calls.append(("refresh_hike", course_id, force))
            return self.context[course_id]

    result, state = _run("hike", FakeFucker, course_id="123")

    assert FakeFucker.instances[-1].calls == [
        ("hike", "123"),
        ("refresh_hike", "123", True),
    ]
    assert result["status"] == "partial"
    assert result["completed_count"] == 1
    assert result["total_count"] == 2
    assert result["failed_count"] == 1
    assert result["safe_error_category"] == "RuntimeError"


def test_ai_dispatch_converts_ids_patches_terminal_and_reads_knowledge_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched: list[Any] = []
    from core.courses.providers.zhihuishu import preflight

    monkeypatch.setattr(preflight, "patch_terminal_size", lambda module: patched.append(module))

    class FakeFucker(FakeFuckerBase):
        def fuckAiCourse(
            self,
            course_id: int,
            class_id: int,
            ai_config: dict[str, Any],
            *,
            no_exam: bool,
        ) -> Any:
            self.calls.append(("ai", course_id, class_id, ai_config, no_exam))
            return SimpleNamespace(
                cakeThemeList=[
                    SimpleNamespace(
                        knowledgeList=[
                            SimpleNamespace(studyProgress=100),
                            SimpleNamespace(studyProgress=100),
                        ]
                    )
                ]
            )

    result, state = _run("ai", FakeFucker)

    instance = FakeFucker.instances[-1]
    assert instance.calls == [("ai", 123, 7, {"enabled": True}, True)]
    assert patched == [state["fucker_module"]]
    assert result["status"] == "completed"
    assert (result["completed_count"], result["total_count"], result["failed_count"]) == (
        2,
        2,
        0,
    )


def test_time_limit_is_partial_and_not_counted_as_failed() -> None:
    class TimeLimitExceeded(TimeoutError):
        pass

    class FakeFucker(FakeFuckerBase):
        def fuckZhidaoCourse(self, course_id: str) -> None:
            self.context[course_id] = SimpleNamespace(
                videos={"v1": SimpleNamespace(watchState=0)}
            )
            try:
                self.fuckZhidaoVideo(course_id, "v1")
            except TimeLimitExceeded:
                pass

        def fuckZhidaoVideo(self, course_id: str, video_id: str) -> None:
            raise TimeLimitExceeded("do not expose this text")

        def getZhidaoContext(self, course_id: str, *, force: bool = False) -> Any:
            return self.context[course_id]

    module = SimpleNamespace(Fucker=FakeFucker, TimeLimitExceeded=TimeLimitExceeded)
    state: dict[str, Any] = {
        "fucker_module": module,
        "completed_count": 0,
        "failed_count": 0,
        "total_count": 0,
    }
    result = bridge.run_course(
        module,
        {
            "mode": "shared",
            "course_id": "course-alpha",
            "cookies": {},
            "time_limit_minutes": 1,
        },
        StringIO(),
        state,
    )

    assert state["time_limit_exceeded"] is True
    assert result["status"] == "partial"
    assert result["safe_error_category"] == "time_limit"
    assert result["failed_count"] == 0
