"""Small, serializable value types for the local task domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    AUTHENTICATING = "authenticating"
    CRAWLING = "crawling"
    PACKAGE_REVIEW = "package_review"
    WAITING_MODEL_AUTHORIZATION = "waiting_model_authorization"
    SOLVING = "solving"
    ANSWER_REVIEW = "answer_review"
    READY_FOR_INTERACTION = "ready_for_interaction"
    FILLING = "filling"
    AWAITING_SUBMIT_CONFIRMATION = "awaiting_submit_confirmation"
    SUBMITTING = "submitting"
    CANCELLING = "cancelling"
    FILLED_UNSUBMITTED = "filled_unsubmitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    NEEDS_INSPECTION = "needs_inspection"
    ARCHIVED = "archived"


class TaskStep(str, Enum):
    SETUP = "setup"
    AUTHENTICATION = "authentication"
    CRAWL = "crawl"
    PACKAGE_REVIEW = "package_review"
    MODEL_AUTHORIZATION = "model_authorization"
    SOLVE = "solve"
    ANSWER_REVIEW = "answer_review"
    INTERACTION = "interaction"
    SUBMIT_CONFIRMATION = "submit_confirmation"
    RESULT = "result"


class Operation(str, Enum):
    AUTHENTICATE = "authenticate"
    CRAWL = "crawl"
    SOLVE = "solve"
    INTERACT = "interact"


class QueueState(str, Enum):
    QUEUED = "queued"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class TaskRecord:
    """Task detail. page_url is available only from the detail API."""

    task_id: str
    site_id: str
    title: str
    page_url: str | None = field(repr=False)
    model_profile_id: str | None
    run_id: str | None
    current_step: TaskStep
    status: TaskStatus
    created_at: str
    updated_at: str
    last_safe_error_category: str | None
    last_operation: Operation | None
    interrupted_operation: Operation | None
    confirmation_id: str | None = field(repr=False)
    confirmation_expires_at: str | None
    confirmation_decision: str | None
    pending_operation: Operation | None
    interaction_retry_authorized: bool
    queue_state: QueueState | None
    queue_position: int | None


@dataclass(frozen=True)
class TaskSummary:
    """Task list row; intentionally omits page_url and confirmation tokens."""

    task_id: str
    site_id: str
    title: str
    model_profile_id: str | None
    run_id: str | None
    current_step: TaskStep
    status: TaskStatus
    created_at: str
    updated_at: str
    last_safe_error_category: str | None
    pending_operation: Operation | None
    queue_state: QueueState | None
    queue_position: int | None


@dataclass(frozen=True)
class QueueItem:
    queue_id: int
    task_id: str
    operation: Operation
    state: QueueState
    enqueued_at: str
    started_at: str | None
    return_status: TaskStatus
    overwrite_result: bool
    queue_position: int | None


@dataclass(frozen=True)
class ClaimedOperation(QueueItem):
    """Worker-only queue claim carrying the lease capability."""

    lease_token: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class SubmitConfirmation:
    task_id: str
    run_id: str
    site_id: str
    confirmation_id: str = field(repr=False)
    expires_at: str


@dataclass(frozen=True)
class TaskEvent:
    event_id: int
    task_id: str
    occurred_at: str
    event_type: str
    from_status: TaskStatus | None
    to_status: TaskStatus | None
    operation: Operation | None
    safe_error_category: str | None
