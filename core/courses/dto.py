"""课程任务 DTO（T2-04）。

只做序列化：**不读写存储、不判状态迁移、不碰文件**。数据由调用方注入——
`CourseJob` 来自 `CourseJobStore.get_job()`，业务事件来自
`CourseJobStore.read_business_events()`，Cookie 状态来自认证服务（T5-01）。

为什么新写这个文件而不是复用 `app._task_json()`：做题记录的 `TaskRecord` 与
`CourseJob` 字段集不同（课程没有排队、没有页 URL、没有待提交确认，`run_id`
恒为 None，进度在 `course` 块里），硬共用会把两套 DTO 揉成一张宽表。字段名、
脱敏规则与事件键仍照抄 `app.py` 的 `_task_json()` / `_safe_title()` /
`_task_events_json()`——复用规则，不是复制字段。

契约口径（docs/TASKS.md）：

- §5.1 摘要：键集合由 `SUMMARY_KEYS` 固化，一多一少都算破坏契约。
  共 18 个字段，与做题侧同形。
- §5.2 详情外壳：`{"task","cookie_status","queue","events","course"}`，`queue` 恒 None。
- §5.2 `course` 块：`domain.CourseJob` 没有 `course_name` 字段，暂以任务名回填
  （同样走脱敏）；等 T4 把站点上的真实课程名抓回来后再替换这个回填。
- 安全：不返回 Cookie / api_key / 完整 URL；标题里的 http(s) 链接一律替换为
  「网址已隐藏」。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .domain import CourseJob
from .sites import auth_hint

#: 与 `app.TASK_TYPE_COURSE` 同值。不 import app：那会让 webui 与 courses 成环。
TASK_TYPE_COURSE = "course"

#: 标题脱敏：与 `app._URL_IN_TITLE` 逐字一致。
_URL_IN_TITLE = re.compile(r"https?://\S+", re.IGNORECASE)

#: §5.1 的全部摘要键（顺序即响应里的键序）。
SUMMARY_FIELDS: tuple[str, ...] = (
    "task_id",
    "task_type",
    "site_id",
    "title",
    "authentication_hint",
    "model_profile_id",
    "run_id",
    "current_step",
    "status",
    "created_at",
    "updated_at",
    "last_safe_error_category",
    "last_operation",
    "interrupted_operation",
    "pending_operation",
    "interaction_retry_authorized",
    "queue_state",
    "queue_position",
)

#: 测试拿它比对；也方便 M3 概览合并时校验。
SUMMARY_KEYS: frozenset[str] = frozenset(SUMMARY_FIELDS)

#: §5.2 course 块的键（顺序即响应键序）。
COURSE_FIELDS: tuple[str, ...] = (
    "mode",
    "course_id",
    "class_id",
    "course_name",
    "phase",
    "current_title",
    "progress_percent",
    "completed_count",
    "total_count",
    "failed_count",
    "run_id",
    "options",
)

COURSE_KEYS: frozenset[str] = frozenset(COURSE_FIELDS)

#: course.options 只放这四项（`model_profile_id` 在摘要里，不重复）。
OPTION_FIELDS: tuple[str, ...] = (
    "speed",
    "threshold",
    "time_limit_minutes",
    "process_exam",
)

#: §5.3 / `app._task_events_json()` 的业务事件键。
EVENT_FIELDS: tuple[str, ...] = (
    "event_id",
    "task_id",
    "occurred_at",
    "event_type",
    "from_status",
    "to_status",
    "operation",
    "safe_error_category",
)

EVENT_KEYS: frozenset[str] = frozenset(EVENT_FIELDS)

#: 详情的五个外壳键。
DETAIL_KEYS: frozenset[str] = frozenset(
    {"task", "cookie_status", "queue", "events", "course"}
)


def summary(job: CourseJob | Mapping[str, Any]) -> dict[str, Any]:
    """§5.1 的 18 键摘要（做题/课程共用键名，课程侧的固定值见下）。

    课程固定值：`task_type="course"`、`run_id=None`（真正的运行标识在
    `course.run_id`）、`queue_state/queue_position=None`（课程没有排队）、
    `interaction_retry_authorized=False`（课程没有交互重试授权）。
    `current_step` 取 `phase`——两边都是"当前阶段"的语义，前端步骤条靠它。
    """
    # 索引摘要已经包含列表所需字段，不为序列化回读每个 job.json。
    value = job.get if isinstance(job, Mapping) else lambda key: getattr(job, key)
    profile_id = value("model_profile_id") if isinstance(job, Mapping) else job.options.model_profile_id
    return {
        "task_id": value("job_id"),
        "task_type": TASK_TYPE_COURSE,
        "site_id": value("site_id"),
        "title": _safe_title(value("title")),
        "authentication_hint": auth_hint(value("site_id")),
        "model_profile_id": profile_id,
        # 运行标识不进摘要：子进程存活期才有值，列表页拿不到也不必拿。
        "run_id": None,
        "current_step": _enum(value("phase")),
        "status": _enum(value("status")),
        "created_at": value("created_at"),
        "updated_at": value("updated_at"),
        "last_safe_error_category": value("last_safe_error_category"),
        # 课程侧没有这三类操作概念（做题侧的队列/中断/待办），恒 None。
        "last_operation": None,
        "interrupted_operation": None,
        "pending_operation": None,
        "interaction_retry_authorized": False,
        "queue_state": None,
        "queue_position": None,
    }


def course_block(job: CourseJob) -> dict[str, Any]:
    """§5.2 的 `course` 块：只放课程域独有字段与四项运行参数。"""
    return {
        "mode": _enum(job.mode),
        "course_id": job.course_id,
        "class_id": job.class_id,
        "course_name": _safe_title(job.title),
        "phase": _enum(job.phase),
        "current_title": job.current_title,
        "progress_percent": job.progress_percent,
        "completed_count": job.completed_count,
        "total_count": job.total_count,
        "failed_count": job.failed_count,
        "run_id": job.run_id,
        "options": {
            "speed": job.options.speed,
            "threshold": job.options.threshold,
            "time_limit_minutes": job.options.time_limit_minutes,
            "process_exam": job.options.process_exam,
        },
    }


def events(
    job: CourseJob,
    limit: int = 100,
    records: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """业务事件列表（对外键 = `EVENT_FIELDS`）。

    `records` 由调用方从 `CourseJobStore.read_business_events()` 取来后注入；
    本函数不读存储，只做白名单裁剪与枚举取值。`records` 为 None 时返回空表
    （详情接口在事件注入前不该凭空造时间线）。

    未知键一律丢弃：即使上游塞了 URL 或原始报错也进不了响应。
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    if limit == 0 or records is None:
        return []
    kept = list(records)[-limit:]
    return [_event_entry(job, record) for record in kept]


def detail(
    job: CourseJob,
    cookie_status: Mapping[str, Any] | None = None,
    events: Iterable[Mapping[str, Any]] | None = None,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """§5.2 的详情外壳：`task` + `cookie_status` + `queue:null` + `events` + `course`。

    `cookie_status` 缺省为「未抓取、未配置」；只有 `captured`/`configured`
    两个布尔会出现在响应里，多余键丢弃。
    """
    return {
        "task": summary(job),
        "cookie_status": _cookie_status(cookie_status),
        "queue": None,
        "events": _events_from(job, limit, events),
        "course": course_block(job),
    }


def _events_from(
    job: CourseJob, limit: int, records: Iterable[Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """detail 的事件分支：允许传空列表，语义与 `events()` 一致。"""
    return events(job, limit, records)


def _cookie_status(value: Mapping[str, Any] | None) -> dict[str, bool]:
    """Cookie 状态白名单化；非映射或字段缺失一律按 False，不让详情 500。"""
    if not isinstance(value, Mapping):
        return {"captured": False, "configured": False}
    return {
        "captured": bool(value.get("captured", False)),
        "configured": bool(value.get("configured", False)),
    }


def _event_entry(job: CourseJob, record: Mapping[str, Any]) -> dict[str, Any]:
    """单条业务事件 → 白名单字典。`task_id` 缺失时用 job_id 兜底。"""
    task_id = record.get("task_id")
    return {
        "event_id": record.get("event_id"),
        "task_id": task_id if isinstance(task_id, str) and task_id else job.job_id,
        "occurred_at": record.get("occurred_at"),
        "event_type": record.get("event_type"),
        "from_status": _enum(record.get("from_status")),
        "to_status": _enum(record.get("to_status")),
        "operation": _enum(record.get("operation")),
        "safe_error_category": record.get("safe_error_category"),
    }


def _safe_title(value: Any) -> str:
    """与 `app._safe_title()` 同规则：URL 替换 + 去空白 + 空标题兜底。"""
    text = value if isinstance(value, str) else ""
    return _URL_IN_TITLE.sub("网址已隐藏", text).strip() or "未命名任务"


def _enum(value: Any) -> Any:
    """枚举取 `value`，非枚举原样返回（None 仍为 None）。"""
    return getattr(value, "value", value)


__all__ = [
    "COURSE_FIELDS",
    "COURSE_KEYS",
    "DETAIL_KEYS",
    "EVENT_FIELDS",
    "EVENT_KEYS",
    "OPTION_FIELDS",
    "SUMMARY_FIELDS",
    "SUMMARY_KEYS",
    "TASK_TYPE_COURSE",
    "course_block",
    "detail",
    "events",
    "summary",
]
