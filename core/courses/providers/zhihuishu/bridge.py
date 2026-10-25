"""JSONL worker boundary for the isolated zhihuishu upstream copy."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Any, Callable, TextIO

logger = logging.getLogger(__name__)


class _CaptchaRequiredSignal(BaseException):
    """让验证码异常越过上游的 `except Exception` 后立即停在桥接边界。"""


def run_course(
    fucker_module: Any,
    payload: dict[str, Any],
    events: TextIO,
    state: dict[str, Any],
) -> dict[str, Any]:
    """显式按 mode 调用 fuckZHS，并用上游事实源重算本次结果。"""
    mode = payload.get("mode")
    if mode not in {"shared", "hike", "ai"}:
        raise ValueError("unsupported course mode")

    course_id = payload.get("course_id")
    cookies = payload.get("cookies")
    if not isinstance(cookies, Mapping):
        raise ValueError("invalid cookies payload")

    class_id: int | None = None
    if mode == "ai":
        class_id_value = payload.get("class_id")
        ai_config = payload.get("ai_config")
        if not isinstance(ai_config, Mapping):
            raise ValueError("invalid AI configuration")
        course_id = _integer_id(course_id, "course_id")
        class_id = _integer_id(class_id_value, "class_id")
        from core.courses.providers.zhihuishu.preflight import patch_terminal_size

        patch_terminal_size(fucker_module)
    elif not isinstance(course_id, str) or not course_id:
        raise ValueError("invalid course_id")

    instance = fucker_module.Fucker(
        cookies=dict(cookies),
        speed=payload.get("speed", 1.5),
        end_thre=payload.get("threshold", 0.91),
        limit=payload.get("time_limit_minutes", 0),
        tree_view=False,
        progressbar_view=True,
    )
    state["fucker"] = instance

    from core.courses.providers.zhihuishu import progress

    progress.instrument_instance(instance, events, state, mode, course_id)

    # fuckZHS 内部会把 CaptchaException 转成普通日志并继续/返回。把它
    # 转成 BaseException 信号，避免上游宽泛的 `except Exception` 吞掉挑战。
    captcha_exception = getattr(fucker_module, "CaptchaException", None)
    original_query = getattr(instance, "zhidaoQuery", None)
    if isinstance(captcha_exception, type) and callable(original_query):
        def guarded_query(*args: Any, **kwargs: Any) -> Any:
            try:
                return original_query(*args, **kwargs)
            except captcha_exception:
                state["captcha_required"] = True
                raise _CaptchaRequiredSignal() from None

        instance.zhidaoQuery = guarded_query

    result: Any = None
    try:
        if mode == "shared":
            result = instance.fuckZhidaoCourse(course_id)
        elif mode == "hike":
            result = instance.fuckHikeCourse(course_id)
        else:
            result = instance.fuckAiCourse(
                course_id,
                class_id,
                dict(ai_config),
                no_exam=not payload.get("process_exam", True),
            )
    except _CaptchaRequiredSignal:
        # This is an explicit user action requirement, not a normal failed item.
        state["execution_error"] = True
        state["last_error_type"] = "captcha_required"
    except Exception as error:
        if _is_time_limit(error, fucker_module):
            state["time_limit_exceeded"] = True
        else:
            state["failed_count"] = int(state.get("failed_count", 0)) + 1
            state["last_error_type"] = type(error).__name__
            state["execution_error"] = True

    upstream_counts = _counts_from_result(result)
    if upstream_counts is None and mode == "shared":
        context = (
            _context_value(instance, course_id)
            if state.get("execution_error")
            else _refresh_context(instance, "getZhidaoContext", course_id)
        )
        upstream_counts = _shared_counts(context)
    elif upstream_counts is None and mode == "hike":
        context = (
            _context_value(instance, course_id)
            if state.get("execution_error")
            else _refresh_context(instance, "getHikeContext", course_id)
        )
        upstream_counts = _hike_counts(
            context,
            payload.get("threshold", 0.91),
        )
    elif upstream_counts is None and mode == "ai":
        upstream_counts = _ai_counts(result)
        if upstream_counts is None and not state.get("execution_error"):
            context = _context_value(instance, course_id)
            upstream_counts = _ai_counts(context)
        if upstream_counts is None and not state.get("captcha_required"):
            # fuckAiCourse() returns None and does not retain its initial knowledgeList.
            # Reuse the upstream reader for one fresh post-run snapshot; no API is reimplemented.
            getter = getattr(instance, "getAiKnowlegePoints", None)
            if callable(getter):
                try:
                    upstream_counts = _ai_counts(getter(course_id, class_id))
                except Exception as error:
                    state["stats_error_type"] = type(error).__name__

    completed = int(state.get("completed_count", 0))
    failed = int(state.get("failed_count", 0))
    total = int(state.get("total_count", 0))
    if upstream_counts is not None:
        upstream_total, upstream_completed = upstream_counts
        if (upstream_total, upstream_completed) != (total, completed):
            state["stats_mismatch"] = {
                "mode": mode,
                "bridge_total": total,
                "bridge_completed": completed,
                "upstream_total": upstream_total,
                "upstream_completed": upstream_completed,
            }
            logger.warning("课程任务统计采用上游进度：mode=%s，已记录统计不一致", mode)
        total = upstream_total
        completed = upstream_completed
    if state.get("captcha_required"):
        result_status, error_category = "failed", "captcha_required"
    else:
        result_status, error_category = _classify_result(
            total=total,
            completed=completed,
            failed=failed,
            time_limit_exceeded=bool(state.get("time_limit_exceeded")),
            last_error_type=state.get("last_error_type") or state.get("stats_error_type"),
        )
    return {
        "type": "result",
        "status": result_status,
        "completed_count": completed,
        "total_count": total,
        "failed_count": failed,
        "safe_error_category": error_category,
    }


def execute_upstream(
    fucker_module: Any, payload: dict[str, Any], events: TextIO, state: dict[str, Any]
) -> dict[str, Any]:
    """默认 M11 执行器，保留 M10 的注入边界用于替身测试。"""
    return run_course(fucker_module, payload, events, state)


def run(
    payload: dict[str, Any],
    events: TextIO,
    execute: Callable[..., Any] = execute_upstream,
) -> int:
    upstream = Path.cwd() / "upstream"
    sys.path.insert(0, str(upstream))
    import fucker

    fucker.logger.setLevel("WARNING")
    meta = json.loads((upstream / "meta.json").read_text(encoding="utf-8"))
    _write(events, {"type": "hello", "version": 1, "mode": str(payload.get("mode", "unknown")), "upstream_version": str(meta.get("version", "unknown"))})
    # This is an explicit source path for the read-only thin adapter imports;
    # it does not affect run cwd or derive paths from cwd.
    core_root = Path(__file__).resolve().parents[4]
    if str(core_root) not in sys.path:
        sys.path.insert(0, str(core_root))
    from core.courses.providers.zhihuishu import progress
    state: dict[str, Any] = {
        "fucker_module": fucker,
        "completed_count": 0,
        "failed_count": 0,
        "total_count": 0,
    }
    progress.install(events, state)
    try:
        result = execute(fucker, payload, events, state)
    except BaseException as exc:
        # Only the type is protocol-visible; exception text can contain credentials.
        result = _result_event(
            "failed",
            int(state.get("completed_count", 0)),
            int(state.get("total_count", 0)),
            int(state.get("failed_count", 0)) + 1,
            _error_category(type(exc).__name__),
        )
    if not _is_result_event(result):
        result = _result_event(
            "failed", 0, 0, 1, "invalid_upstream_result"
        )
    _write(events, result)
    if result["status"] == "failed":
        return 1
    return 0


def _integer_id(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid {field}")
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value or not value.isdecimal():
        raise ValueError(f"invalid {field}") from None
    try:
        return int(value)
    except (ValueError, OverflowError):
        raise ValueError(f"invalid {field}") from None


def _is_time_limit(error: BaseException, module: Any) -> bool:
    exception_type = getattr(module, "TimeLimitExceeded", None)
    return (
        isinstance(exception_type, type)
        and isinstance(error, exception_type)
    ) or type(error).__name__ == "TimeLimitExceeded"


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _context_value(instance: Any, key: Any) -> Any:
    context = getattr(instance, "context", None)
    if isinstance(context, Mapping):
        return context.get(key, context.get(str(key)))
    try:
        return context[key]
    except (KeyError, TypeError, AttributeError):
        return None


def _refresh_context(instance: Any, getter_name: str, key: Any) -> Any:
    existing = _context_value(instance, key)
    getter = getattr(instance, getter_name, None)
    if not callable(getter):
        return existing
    try:
        refreshed = getter(key, force=True)
    except Exception as error:
        logger.warning(
            "无法刷新上游课程统计快照：%s (%s)", getter_name, type(error).__name__
        )
        return existing
    return refreshed if refreshed is not None else _context_value(instance, key)


def _shared_counts(context: Any) -> tuple[int, int] | None:
    videos = _get(context, "videos")
    if isinstance(videos, Mapping):
        items = list(videos.values())
    elif isinstance(videos, (list, tuple)):
        items = list(videos)
    else:
        return None
    completed = sum(1 for video in items if _get(video, "watchState") == 1)
    return len(items), completed


def _hike_counts(context: Any, threshold: Any) -> tuple[int, int] | None:
    root = _get(context, "root")
    if not isinstance(root, (list, tuple)):
        return None
    try:
        threshold_value = float(threshold)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(threshold_value):
        return None

    total = completed = 0
    pending = list(root)
    while pending:
        node = pending.pop()
        children = _get(node, "childList")
        if children:
            if isinstance(children, (list, tuple)):
                pending.extend(children)
            continue
        if _get(node, "dataType") != 3:
            continue
        total += 1
        try:
            total_time = float(_get(node, "totalTime"))
            study_time = float(_get(node, "studyTime") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if study_time >= total_time * threshold_value:
            completed += 1
    return total, completed


def _ai_counts(value: Any) -> tuple[int, int] | None:
    if isinstance(value, Mapping):
        total_count = value.get("total_count")
        completed_count = value.get("completed_count")
        if _non_negative_int(total_count) and _non_negative_int(completed_count):
            return int(total_count), int(completed_count)

    direct_list = _get(value, "knowledgeList")
    themes = _get(value, "cakeThemeList")
    if isinstance(direct_list, (list, tuple)):
        knowledge_points = list(direct_list)
    elif isinstance(themes, (list, tuple)):
        knowledge_points = []
        for theme in themes:
            points = _get(theme, "knowledgeList")
            if not isinstance(points, (list, tuple)):
                return None
            knowledge_points.extend(points)
    else:
        return None

    completed = 0
    for point in knowledge_points:
        progress = _get(point, "studyProgress")
        try:
            progress_value = float(progress)
        except (TypeError, ValueError, OverflowError):
            return None
        if not isfinite(progress_value):
            return None
        if progress_value >= 100:
            completed += 1
    return len(knowledge_points), completed


def _counts_from_result(result: Any) -> tuple[int, int] | None:
    if isinstance(result, Mapping):
        total = result.get("total_count")
        completed = result.get("completed_count")
        if _non_negative_int(total) and _non_negative_int(completed):
            return int(total), int(completed)
    return None


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _classify_result(
    *,
    total: int,
    completed: int,
    failed: int,
    time_limit_exceeded: bool,
    last_error_type: Any,
) -> tuple[str, str | None]:
    if time_limit_exceeded:
        return "partial", "time_limit"
    if total > 0 and completed >= total and failed == 0:
        return "completed", None
    error_category = _error_category(last_error_type) if last_error_type else None
    if completed > 0:
        return "partial", error_category or ("incomplete" if completed < total else "upstream_error")
    return "failed", error_category or "no_progress"


def _error_category(value: Any) -> str:
    category = value if isinstance(value, str) and value else "upstream_error"
    return category[:64]


def _result_event(
    status: str,
    completed: int,
    total: int,
    failed: int,
    category: str | None,
) -> dict[str, Any]:
    return {
        "type": "result",
        "status": status,
        "completed_count": max(0, completed),
        "total_count": max(0, total),
        "failed_count": max(0, failed),
        "safe_error_category": category,
    }


def _is_result_event(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("type") == "result"
        and value.get("status") in {"completed", "partial", "failed"}
        and _non_negative_int(value.get("completed_count"))
        and _non_negative_int(value.get("total_count"))
        and _non_negative_int(value.get("failed_count"))
        and (
            value.get("safe_error_category") is None
            or (
                isinstance(value.get("safe_error_category"), str)
                and 0 < len(value["safe_error_category"]) <= 64
            )
        )
    )


def _write(events: TextIO, value: dict[str, Any]) -> None:
    events.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> int:
    # Keep this order in sync with the parent runtime's stdout JSONL contract.
    sys.stdout.flush()
    events = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    try:
        raw = sys.stdin.readline()
        if not raw:
            payload: dict[str, Any] = {}
        else:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("worker input must be a JSON object")
        return run(payload, events)
    except BaseException as exc:
        # Import/bootstrap failures happen before hello; keep stdout protocol-only.
        _write(events, {"type": "result", "status": "failed", "completed_count": 0, "total_count": 0, "failed_count": 1, "safe_error_category": type(exc).__name__})
        return 1
    finally:
        events.flush()


if __name__ == "__main__":
    raise SystemExit(main())
