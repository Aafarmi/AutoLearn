"""Thin progress and item instrumentation for the vendored fuckZHS module."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _emit(events: TextIO, payload: dict[str, Any]) -> None:
    events.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _stage(prefix: Any) -> str:
    return "exam" if any(s in str(prefix).lower() for s in ("answering quiz", "fucking exam")) else "learning"


def install(events: TextIO, state: dict[str, Any]) -> Callable[..., None]:
    """Replace the module-level alias imported by fucker.py."""
    module = state.get("fucker_module")
    if module is None:
        import fucker as module  # type: ignore[no-redef]
    state.setdefault("completed_count", 0)
    state.setdefault("failed_count", 0)
    state.setdefault("total_count", 0)
    state.setdefault("current_title", None)
    state.setdefault("current_at", 0.0)

    def progressBar(s: Any, e: Any, prefix: Any = "", suffix: Any = "done", decimals: Any = 1, length: Any = None, fill: Any = "#", progressbar_view: Any = True, **kwargs: Any) -> None:
        del suffix, decimals, length, fill, progressbar_view, kwargs
        try:
            current = max(0, int(round(float(s))))
            total = max(0, int(round(float(e))))
        except (TypeError, ValueError, OverflowError):
            return
        percent = round(min(100.0, max(0.0, current * 100.0 / total)) if total else 0.0, 2)
        _emit(events, {
            "type": "progress", "stage": _stage(prefix), "current": current,
            "total": total, "percent": percent,
            "completed_count": int(state["completed_count"]),
            "total_count": int(state["total_count"]),
        })

    module.progressBar = progressBar
    return progressBar


def instrument_instance(instance: Any, events: TextIO, state: dict[str, Any], mode: str, course_id: Any) -> None:
    """Wrap only item boundaries that upstream does not expose as events."""
    method_names = {
        "shared": ("fuckZhidaoVideo",),
        "hike": ("fuckHikeVideo",),
        "ai": ("fuckAiVideo",),
    }.get(mode, ())
    for name in method_names:
        original = getattr(instance, name, None)
        if not callable(original) or getattr(original, "_progress_wrapped", False):
            continue

        def wrapped(*args: Any, __original: Callable[..., Any] = original, __name: str = name, **kwargs: Any) -> Any:
            ident: Any = None
            title: Any = None
            stage = "learning"
            if __name == "fuckZhidaoVideo":
                ident = args[1] if len(args) > 1 else kwargs.get("video_id")
                context = _get(getattr(instance, "context", {}), str(course_id), _get(getattr(instance, "context", {}), course_id, None))
                video = _get(_get(context, "videos", {}), ident, None)
                title = _get(video, "name")
            elif __name == "fuckHikeVideo":
                ident = args[1] if len(args) > 1 else kwargs.get("file_id")
                caller_file = sys._getframe(1).f_locals.get("file")
                title = _get(caller_file, "name")
                if title is None:
                    title = ident
            elif __name == "_traverse":
                node = args[1] if len(args) > 1 else kwargs.get("node")
                ident = _get(node, "id")
                title = _get(node, "name")
                if _get(node, "childList"):
                    return __original(*args, **kwargs)
            else:
                ident = args[2] if len(args) > 2 else kwargs.get("fileId")
                # The upstream caller has the exact knowledge object in scope; use it without copying its loop.
                caller = sys._getframe(1).f_locals
                knowledge = caller.get("knowledge")
                title = _get(knowledge, "knowledgeName")
                if title is None:
                    title = ident
            title = str(title if title not in (None, "") else ident)
            now = time.monotonic()
            if title != state.get("current_title") and now - float(state.get("current_at", 0.0)) >= 1.0:
                _emit(events, {"type": "current", "stage": stage, "title": title[:200]})
                state["current_title"] = title
                state["current_at"] = now
            state["total_count"] = int(state.get("total_count", 0)) + 1
            try:
                result = __original(*args, **kwargs)
            except Exception as exc:
                if type(exc).__name__ == "TimeLimitExceeded":
                    state["time_limit_exceeded"] = True
                else:
                    state["failed_count"] = int(state.get("failed_count", 0)) + 1
                    state["last_error_type"] = type(exc).__name__
                raise
            else:
                state["completed_count"] = int(state.get("completed_count", 0)) + 1
                return result

        wrapped._progress_wrapped = True  # type: ignore[attr-defined]
        setattr(instance, name, wrapped)
