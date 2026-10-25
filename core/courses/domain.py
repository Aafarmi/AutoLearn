"""课程任务领域模型（T2-01）。

三个枚举是课程域**独有**的词汇表，与做题侧的 `TaskStatus` / `TaskStep`
不重叠、不复用、不互相转换（docs/TASKS.md §4.4）：做题有 queued、cancelling、
solving 等 21 个状态，课程只有 10 个，而且 ready/running/stopping/completed/partial
在两侧语义也不同（课程的 `partial` 对应"时限到"）。

硬约束：
- 本模块**不得 import `core.tasks`**，课程域与做题域不在代码层面耦合；
- `CourseJob` 不带做题专属字段（queue_state / queue_position /
  interaction_retry_authorized / page_url / confirmation_id 等）。课程没有排队，
  也没有"交互重试授权"，硬塞进来会让两侧 DTO 在 T1-03 合并时打架。

本模块只放值类型与迁移表，不做持久化（T2-02）、不做 DTO 转换（T2-04）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

from .errors import CourseTransitionError


class CourseStatus(str, Enum):
    """课程任务状态（§4.4）。取值即落盘字面量，改名等于数据迁移。"""

    DRAFT = "draft"
    AUTHENTICATING = "authenticating"
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class CoursePhase(str, Enum):
    """学习阶段（§4.4）。`exam` 仅在 AI 课程且 process_exam=true 时出现。"""

    SETUP = "setup"
    AUTHENTICATION = "authentication"
    CATALOG = "catalog"
    LEARNING = "learning"
    EXAM = "exam"
    RESULT = "result"


class CourseMode(str, Enum):
    """学习模式（§4.4），与 `sites.SITES` 里各站点的 `modes` 同口径。"""

    SHARED = "shared"
    HIKE = "hike"
    AI = "ai"


@dataclass(frozen=True)
class CourseOptions:
    """课程运行参数（§4.2 的 `course.options`），默认值即产品默认档位。"""

    speed: float = 1.5
    threshold: float = 0.91
    time_limit_minutes: int = 0
    process_exam: bool = True
    model_profile_id: str | None = None


@dataclass(frozen=True)
class CourseJob:
    """一个课程任务的全部持久化状态。

    字段顺序即 §4.1/§4.2 的声明顺序，全部必填以便 `create` 时不留隐式默认值。
    - `run_id` 仅在子进程存活期间非空；对外的任务摘要恒为 `None`（§4.1）。
    - `title` 是任务名（已脱敏），`current_title` 是正在学的章节名（≤200 字，§4.3）。
    - `progress_percent` 用 float，允许 33.33 这类小数。
    """

    job_id: str
    site_id: str
    title: str
    # Course identity is chosen only after QR authentication and catalog fetch.
    mode: CourseMode | None
    course_id: str | None
    class_id: str | None
    status: CourseStatus
    phase: CoursePhase
    current_title: str | None
    progress_percent: float
    completed_count: int
    total_count: int
    failed_count: int
    run_id: str | None
    options: CourseOptions
    created_at: str
    updated_at: str
    last_safe_error_category: str | None


# 状态机（§4.4）。终态重跑统一走 → running，因此 completed/partial/failed/cancelled
# 都能直接回到 running；archived 是唯一没有出边的状态。
ALLOWED_TRANSITIONS: dict[CourseStatus, frozenset[CourseStatus]] = {
    CourseStatus.DRAFT: frozenset({CourseStatus.AUTHENTICATING, CourseStatus.ARCHIVED}),
    CourseStatus.AUTHENTICATING: frozenset(
        {CourseStatus.READY, CourseStatus.DRAFT, CourseStatus.FAILED}
    ),
    CourseStatus.READY: frozenset(
        {CourseStatus.RUNNING, CourseStatus.AUTHENTICATING, CourseStatus.ARCHIVED}
    ),
    CourseStatus.RUNNING: frozenset(
        {
            CourseStatus.STOPPING,
            CourseStatus.COMPLETED,
            CourseStatus.PARTIAL,
            CourseStatus.FAILED,
        }
    ),
    CourseStatus.STOPPING: frozenset({CourseStatus.CANCELLED, CourseStatus.FAILED}),
    CourseStatus.COMPLETED: frozenset({CourseStatus.RUNNING, CourseStatus.ARCHIVED}),
    CourseStatus.PARTIAL: frozenset({CourseStatus.RUNNING, CourseStatus.ARCHIVED}),
    CourseStatus.FAILED: frozenset({CourseStatus.RUNNING, CourseStatus.ARCHIVED}),
    CourseStatus.CANCELLED: frozenset({CourseStatus.RUNNING, CourseStatus.ARCHIVED}),
    CourseStatus.ARCHIVED: frozenset(),
}


def ensure_transition(src: CourseStatus, dst: CourseStatus) -> None:
    """校验一次状态迁移，非法则抛 `CourseTransitionError`。

    消息里带两端状态字面量（`running → ready`），WebUI 可原样透出——
    `errors.CourseError` 的约定是只携带安全文案，不带 URL / Cookie / 上游报错。

    未知状态（不在迁移表里的值）一律按非法迁移处理，不放行；
    同状态自迁移也不放行，幂等由调用方自己判断，避免掩盖重复点击。
    """
    if src in ALLOWED_TRANSITIONS and dst in ALLOWED_TRANSITIONS[src]:
        return
    raise CourseTransitionError(f"不允许的状态迁移：{_label(src)} → {_label(dst)}。")


def job_id() -> str:
    """生成课程任务 ID：`course-` + 32 位 hex（§4.1 的 `course-3f2a…`）。

    与做题任务的 ID 前缀区分开，前端和日志里一眼能看出任务类型。
    """
    return f"course-{uuid4().hex}"


def _label(status: object) -> str:
    """错误消息里的状态字面量：枚举取 value，其它原样转字符串。"""
    value = getattr(status, "value", status)
    return value if isinstance(value, str) else str(value)
