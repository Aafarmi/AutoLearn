"""课程任务域：与做题任务平级的网课模块（只放公共名字，不放逻辑）。"""

from .errors import (
    CourseBusy,
    CourseError,
    CourseNotFound,
    CourseProtocolError,
    CourseTransitionError,
)
from .domain import (
    ALLOWED_TRANSITIONS,
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    ensure_transition,
    job_id,
)
from .protocol import (
    EVENT_FIELDS,
    EVENT_TYPES,
    MAX_EVENTS_PER_JOB,
    MAX_FRONTEND_EVENTS,
    MAX_LINE_BYTES,
    PROGRESS_MIN_INTERVAL_MS,
    validate_event,
)
from .sites import (
    SITES,
    SiteCapability,
    auth_hint,
    is_course_site,
    modes_for,
    sites_for,
)
from .store import CourseJobStore

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CourseBusy",
    "CourseError",
    "CourseJob",
    "CourseJobStore",
    "CourseMode",
    "CourseNotFound",
    "CourseOptions",
    "CoursePhase",
    "CourseProtocolError",
    "CourseStatus",
    "CourseTransitionError",
    "EVENT_FIELDS",
    "EVENT_TYPES",
    "MAX_EVENTS_PER_JOB",
    "MAX_FRONTEND_EVENTS",
    "MAX_LINE_BYTES",
    "PROGRESS_MIN_INTERVAL_MS",
    "SITES",
    "SiteCapability",
    "auth_hint",
    "ensure_transition",
    "is_course_site",
    "job_id",
    "modes_for",
    "sites_for",
    "validate_event",
]
