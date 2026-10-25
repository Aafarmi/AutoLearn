"""Thin JSONL adapter around the read-only fuckZHS login and catalog methods."""
from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _emit(value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    stream = getattr(sys.__stdout__, "buffer", None)
    if stream is not None:
        # The parent parses JSONL as UTF-8. Writing bytes avoids Windows' active
        # console code page (for example GBK) corrupting Chinese course names.
        stream.write(payload)
        stream.flush()
        return
    # Keep unit-test / embedded callers with a text-only stdout usable.
    sys.__stdout__.write(payload.decode("utf-8"))
    sys.__stdout__.flush()


def _value(item: Any, *names: str) -> Any:
    for name in names:
        value = item.get(name) if isinstance(item, dict) else getattr(item, name, None)
        if value is not None:
            return value
    return None


def _course_items(value: Any) -> list[Any]:
    """Find the list inside upstream response wrappers without changing their logic."""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        for key in (
            "courseOpenDtos", "startInngcourseList", "studentAICourseList",
            "studentAiCourseList", "aiCourseList", "courseList", "courses",
            "courseInfos", "list", "data", "result",
        ):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
        for nested in value.values():
            found = _course_items(nested)
            if found and any(_value(item, "secret", "courseId", "recruitAndCourseId", "id") is not None for item in found):
                return found
    return []


def _catalog(fucker: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    courses: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []

    # Match fuckZHS/main.py --fetch exactly: shared IDs are `secret`, while
    # hike IDs are `courseId`; both names are `courseName`.
    for mode, method_name, id_field in (
        ("shared", "getZhidaoList", "secret"),
        ("hike", "getHikeList", "courseId"),
    ):
        try:
            values = getattr(fucker, method_name)()
            for item in values or []:
                course_id = _value(item, id_field)
                if course_id is None:
                    continue
                courses.append({
                    "mode": mode,
                    "course_id": str(course_id),
                    "class_id": None,
                    "name": str(_value(item, "courseName") or f"未命名课程（{course_id}）"),
                })
        except Exception as error:
            warnings.append({"mode": mode, "category": type(error).__name__})

    # AI catalog is a separate upstream endpoint and is not part of upstream
    # main.py --fetch. Keep it isolated so its response cannot discard the two
    # catalog lists that the supported upstream fetch flow already returns.
    try:
        ai_values = _course_items(fucker.getZhidaoAiList())
        for item in ai_values:
            course_id = _value(item, "courseId", "id", "scMapId")
            class_id = _value(item, "classId", "class_id")
            if course_id is None or class_id is None:
                continue
            name = _value(item, "courseName", "name", "title")
            if name is None:
                info = _value(item, "courseInfo")
                name = _value(info, "courseName", "name") if info is not None else None
            courses.append({
                "mode": "ai", "course_id": str(course_id), "class_id": str(class_id),
                "name": str(name or f"未命名课程（{course_id}）"),
            })
    except Exception as error:
        warnings.append({"mode": "ai", "category": type(error).__name__})
    return courses, warnings


def _outline_node(title: Any, *, children: list[dict[str, Any]] | None = None, completed: bool | None = None) -> dict[str, Any]:
    text = str(title or "未命名").strip()[:200] or "未命名"
    result: dict[str, Any] = {"title": text}
    if children is not None:
        result["children"] = children
    if completed is not None:
        result["completed"] = completed
    return result


def _limit_outline(items: list[dict[str, Any]], *, max_nodes: int = 2500, max_depth: int = 10) -> tuple[list[dict[str, Any]], bool]:
    remaining = [max_nodes]
    truncated = [False]

    def visit(nodes: list[dict[str, Any]], depth: int) -> list[dict[str, Any]]:
        if depth > max_depth:
            truncated[0] = True
            return []
        result = []
        for item in nodes:
            if remaining[0] <= 0:
                truncated[0] = True
                break
            remaining[0] -= 1
            node: dict[str, Any] = {"title": str(item.get("title") or "未命名")[:200]}
            if isinstance(item.get("completed"), bool):
                node["completed"] = item["completed"]
            children = item.get("children")
            if isinstance(children, list):
                node["children"] = visit(children, depth + 1)
            result.append(node)
        return result

    return visit(items, 0), truncated[0]


def _course_outline(fucker: Any, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    mode = payload.get("mode")
    course_id = payload.get("course_id")
    if mode == "shared":
        context = fucker.getZhidaoContext(course_id)
        chapters = _value(_value(context, "chapters"), "videoChapterDtos") or []
        items = []
        for chapter in chapters:
            lessons = []
            for lesson in _value(chapter, "videoLessons") or []:
                videos = [
                    _outline_node(_value(video, "name"), completed=_value(video, "watchState") == 1)
                    for video in (_value(lesson, "videoSmallLessons") or [])
                ]
                lessons.append(_outline_node(_value(lesson, "name"), children=videos))
            items.append(_outline_node(_value(chapter, "name"), children=lessons))
    elif mode == "hike":
        context = fucker.getHikeContext(course_id)
        threshold = payload.get("threshold", 0.91)
        try:
            threshold = float(threshold)
        except (TypeError, ValueError, OverflowError):
            threshold = 0.91

        def hike_node(value: Any) -> dict[str, Any]:
            children = _value(value, "childList")
            if isinstance(children, (list, tuple)) and children:
                return _outline_node(_value(value, "name"), children=[hike_node(child) for child in children])
            completed = None
            if _value(value, "dataType") == 3:
                try:
                    completed = float(_value(value, "studyTime") or 0) >= float(_value(value, "totalTime")) * threshold
                except (TypeError, ValueError, OverflowError):
                    completed = False
            return _outline_node(_value(value, "name"), completed=completed)

        root = _value(context, "root") or []
        items = [hike_node(node) for node in root]
    elif mode == "ai":
        class_id = _integer_id(payload.get("class_id"), "class_id")
        course_id = _integer_id(course_id, "course_id")
        context = fucker.getAiKnowlegePoints(course_id, class_id)
        items = []
        for theme in _value(context, "cakeThemeList") or []:
            knowledge = [
                _outline_node(
                    _value(point, "knowledgeName"),
                    completed=(
                        isinstance(_value(point, "studyProgress"), (int, float))
                        and _value(point, "studyProgress") >= 100
                    ),
                )
                for point in (_value(theme, "knowledgeList") or [])
            ]
            items.append(_outline_node(_value(theme, "themeName"), children=knowledge))
    else:
        raise ValueError("unsupported course mode")
    return _limit_outline(items)


def _fucker_with_cookies(fucker_type: Any, cookies: Any) -> Any:
    if isinstance(cookies, list):
        return fucker_type(cookies=cookies)
    if isinstance(cookies, dict):
        from requests.utils import cookiejar_from_dict

        return fucker_type(cookies=cookiejar_from_dict(cookies))
    raise ValueError("invalid cookies payload")


def _integer_id(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid {field}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise ValueError(f"invalid {field}")


def main() -> int:
    upstream = Path(sys.argv[1]).resolve()
    os.chdir(upstream.parent)
    sys.path.insert(0, str(upstream))
    from fucker import Fucker
    import logger as vendor_logger
    # The upstream DEBUG logger records the cookie jar verbatim in its files.
    # QR/catalog sessions have no need for that output; suppress it at source.
    vendor_logger.logger.level = "CRITICAL"

    request = json.loads(sys.stdin.readline() or "{}")
    action = request.get("action")
    try:
        if action == "login":
            fucker = Fucker()
            with contextlib.redirect_stdout(sys.stderr):
                fucker.login(use_qr=True, qr_callback=lambda image: _emit({
                    "event": "qr", "image": base64.b64encode(image).decode("ascii")
                }))
            # Match upstream main.py's cookie_jar_to_list() round-trip. Keeping
            # path/domain/secure/rest matters because the next catalog request
            # uses a fresh worker process and must rebuild the same cookie jar.
            from utils import cookie_jar_to_list

            records = cookie_jar_to_list(fucker.cookies)
            _emit({"event": "authenticated", "cookies": records})
            return 0
        if action in {"catalog", "outline"}:
            fucker = _fucker_with_cookies(Fucker, request.get("cookies") or {})
            with contextlib.redirect_stdout(sys.stderr):
                if action == "catalog":
                    courses, warnings = _catalog(fucker)
                    _emit({"event": "catalog", "courses": courses, "warnings": warnings})
                else:
                    items, truncated = _course_outline(fucker, request)
                    _emit({"event": "outline", "items": items, "truncated": truncated})
            return 0
        _emit({"event": "error", "category": "invalid_action"})
        return 2
    except BaseException as error:
        _emit({"event": "error", "category": type(error).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
