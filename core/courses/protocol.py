"""Wire contract for the course worker bridge.

只放常量与校验骨架，不含节流、落盘、状态机（分别属于 T3-04 / T2-03 / T2-01）。
字段口径严格照 docs/TASKS.md §4.3，不要在这里发明新字段。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

# --- 事件类型白名单（§4.3） -------------------------------------------------

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "hello",
        "phase",
        "current",
        "progress",
        "result",
        "diagnostic",
    }
)

# 每个 type 的字段白名单，同时就是该 type 的必填字段（§4.3「必填」列）。
# 白名单之外的键在规范化时被丢弃，不报错，避免上游加字段就断流。
EVENT_FIELDS: dict[str, frozenset[str]] = {
    "hello": frozenset({"version", "mode", "upstream_version"}),
    "phase": frozenset({"stage", "status"}),
    "current": frozenset({"stage", "title"}),
    "progress": frozenset(
        {
            "stage",
            "current",
            "total",
            "percent",
            "completed_count",
            "total_count",
        }
    ),
    "result": frozenset(
        {
            "status",
            "completed_count",
            "total_count",
            "failed_count",
            "safe_error_category",
        }
    ),
    "diagnostic": frozenset({"category", "message"}),
}

# --- 枚举值（§4.3） ---------------------------------------------------------

PHASE_STATUSES: frozenset[str] = frozenset({"started", "completed", "failed"})
RESULT_STATUSES: frozenset[str] = frozenset({"completed", "partial", "failed"})

# --- 硬限制 -----------------------------------------------------------------

MAX_LINE_BYTES = 65536
MAX_EVENTS_PER_JOB = 500
MAX_FRONTEND_EVENTS = 300
PROGRESS_MIN_INTERVAL_MS = 500

# 文案长度上限：current.title ≤200 字、diagnostic.message ≤300 字（§4.3）。
MAX_TITLE_CHARS = 200
MAX_MESSAGE_CHARS = 300
MAX_CATEGORY_CHARS = 64
MAX_DIAGNOSTICS_PER_RUN = 10

# percent 的合法闭区间：越界一律丢弃，不做静默夹取（静默夹取会掩盖上游 bug）。
MIN_PERCENT = 0
MAX_PERCENT = 100

_REPLACEMENT_CHAR = "\ufffd"
_STRING_FIELDS = frozenset(
    {"version", "mode", "upstream_version", "stage", "status", "title", "category", "message"}
)
_COUNT_FIELDS = frozenset({"current", "total", "completed_count", "total_count", "failed_count"})


def validate_event(raw: Any) -> dict[str, Any] | None:
    """校验并规范化一条子进程事件。

    返回只含白名单键的新 dict；任何不合契约的输入返回 `None`，不抛异常。
    调用方负责「丢弃」语义（未知 type / 非法 UTF-8 / 缺 result 视为 failed）。
    """
    if not isinstance(raw, Mapping):
        return None

    event_type = raw.get("type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        return None

    fields = EVENT_FIELDS[event_type]
    for name in fields:
        if name not in raw:
            return None

    event: dict[str, Any] = {"type": event_type}
    for name in fields:
        value = raw[name]
        if isinstance(value, bool):
            return None
        if name in _COUNT_FIELDS or name == "percent":
            if not isinstance(value, (int, float)):
                return None
            event[name] = value
        elif isinstance(value, str):
            if _REPLACEMENT_CHAR in value:
                return None
            event[name] = value
        elif value is None and name == "safe_error_category":
            event[name] = None
        else:
            return None

    if _line_bytes(event) > MAX_LINE_BYTES:
        return None

    if event_type == "hello":
        for name in ("version", "mode", "upstream_version"):
            if not _is_text(event[name]):
                return None
        return event

    if event_type == "phase":
        if not _is_text(event["stage"]) or not _is_text(event["status"]):
            return None
        return event if event["status"] in PHASE_STATUSES else None

    if event_type == "current":
        if not _is_text(event["stage"]) or not _is_text(event["title"]):
            return None
        return event if len(event["title"]) <= MAX_TITLE_CHARS else None

    if event_type == "progress":
        if not _is_text(event["stage"]):
            return None
        for name in ("current", "total", "completed_count", "total_count"):
            count = _as_count(event[name])
            if count is None:
                return None
            event[name] = count
        percent = _as_percent(event["percent"])
        if percent is None:
            return None
        event["percent"] = percent
        return event

    if event_type == "result":
        if not _is_text(event["status"]) or event["status"] not in RESULT_STATUSES:
            return None
        for name in ("completed_count", "total_count", "failed_count"):
            count = _as_count(event[name])
            if count is None:
                return None
            event[name] = count
        category = event["safe_error_category"]
        if category is not None:
            if not _is_text(category) or len(category) > MAX_CATEGORY_CHARS:
                return None
        return event

    # diagnostic
    if not _is_text(event["category"]) or len(event["category"]) > MAX_CATEGORY_CHARS:
        return None
    if not _is_text(event["message"]) or len(event["message"]) > MAX_MESSAGE_CHARS:
        return None
    return event


def is_frontend_event(event: Mapping[str, Any] | None) -> bool:
    """diagnostic 只进日志，不进前端（§4.3）。"""
    return bool(event) and event.get("type") != "diagnostic"


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _as_count(value: Any) -> int | None:
    """计数必须是 >=0 的整数；负数与小数一律判为违约。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not value.is_integer() or value < 0:
            return None
        return int(value)
    return None


def _as_percent(value: Any) -> int | float | None:
    """percent 限定 0-100；越界即丢弃，浮点保留 2 位。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if MIN_PERCENT <= value <= MAX_PERCENT else None
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        if not MIN_PERCENT <= value <= MAX_PERCENT:
            return None
        return round(value, 2)
    return None


def _line_bytes(event: Mapping[str, Any]) -> int:
    try:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return MAX_LINE_BYTES + 1
    return len(payload.encode("utf-8"))
