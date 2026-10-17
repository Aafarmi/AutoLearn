"""Single-worker task runtime shared by the local WebUI and task domain."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from core.auth.cookie_manager import CookieCaptureCancelled, CookieManagerError, capture_for_adapter
from core.credentials import CredentialProviderError, LocalCredentialProvider
from core.interaction.runner import InteractionRunnerError, interact_package
from core.settings import ModelProfileError, ModelProfileStore, profile_changed_after
from core.solver.runner import (
    ModelAPIError,
    ModelProfileConfirmationRequired,
    SolverCancelled,
    SolverError,
    solve_package,
)
from core.tasks import (
    ClaimedOperation,
    Operation,
    QueueItem,
    QueueState,
    TaskRecord,
    TaskStatus,
    TaskStore,
    TaskStoreError,
    TaskTransitionError,
)


_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_ERROR_CATEGORIES = frozenset(
    {
        "interrupted_interaction",
        "interrupted_operation",
        "operation_failed",
        "result_unconfirmed",
        "file_missing",
        "file_read",
        "file_write",
        "invalid_shape",
        "invalid_site",
        "unknown_adapter",
        "config",
        "asset_path",
        "asset_missing",
        "invalid_metadata",
        "schema_definition",
        "execution_result",
        "desktop_confirmation_required",
        "interaction",
        "fill_only_unsupported",
        "adapter_error",
        "model_request",
        "model_response",
        "invalid_answer",
        "answer_reference",
        "provider_error",
    }
)
_SAFE_PROGRESS_STATES = frozenset(
    {
        "queued",
        "started",
        "starting",
        "login_required",
        "authenticated",
        "saving",
        "saved",
        "cancelled",
        "failed",
        "requesting_page",
        "loading_dynamic_page",
        "page_loaded",
        "extracting_questions",
        "package_written",
        "validated_package",
        "model_request_started",
        "model_response_received",
        "answers_written",
        "package_validated",
        "preflight_complete",
        "filling_answers",
        "answers_filled",
        "awaiting_submit_confirmation",
        "submitting",
        "interaction_result_written",
        "completed",
    }
)
_STAGE_BY_OPERATION = {
    Operation.AUTHENTICATE: "authentication",
    Operation.CRAWL: "crawl",
    Operation.SOLVE: "solve",
    Operation.INTERACT: "interaction",
}


@dataclass(frozen=True)
class TaskRuntimeEvent:
    """A safe progress event. It deliberately contains no URL or credentials."""

    sequence: int
    task_id: str
    run_id: str | None
    stage: str
    status: str
    question_count: int | None = None
    asset_count: int | None = None
    completed_count: int | None = None
    safe_error_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelAuthorizationRequest:
    task_id: str
    run_id: str
    profile_id: str
    profile_name: str
    profile_version: int
    provider: str
    model: str
    service_host: str
    service_destination: str
    question_count: int
    image_count: int


@dataclass(frozen=True)
class SubmitConfirmationRequest:
    task_id: str
    run_id: str
    site_id: str
    confirmation_id: str = field(repr=False)
    expires_at: str
    question_count: int


@dataclass(frozen=True)
class TaskExecutionContext:
    """Private stage context; repr omits the URL and credential provider."""

    task_id: str
    site_id: str
    operation: Operation
    run_id: str | None
    page_url: str | None = field(repr=False)
    model_profile_id: str | None = None
    task_created_at: str | None = None
    credential_provider: Any = field(default=None, repr=False, compare=False)


class _ConfirmationGate:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._decision: str | None = None

    def resolve(self, decision: str) -> bool:
        if decision not in {"confirm", "cancel"}:
            decision = "cancel"
        with self._lock:
            if self._decision is not None:
                return False
            self._decision = decision
            self._event.set()
            return True

    def wait(self, cancel_event: threading.Event, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while not self._event.wait(0.1):
            if cancel_event.is_set() or time.monotonic() >= deadline:
                self.resolve("cancel")
                break
        with self._lock:
            return self._decision == "confirm" and not cancel_event.is_set()


@dataclass(frozen=True)
class _WorkerProgress:
    status: str
    question_count: int | None = None
    asset_count: int | None = None
    completed_count: int | None = None


@dataclass(frozen=True)
class _WorkerRequest:
    claim: ClaimedOperation
    context: TaskExecutionContext
    runs_directory: Path = field(repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    confirmed_profile_version: int | None = None


@dataclass(frozen=True)
class _WorkerResult:
    task_id: str
    operation: Operation
    outcome: str
    run_id: str | None = None
    safe_error_category: str | None = None
    question_count: int | None = None
    asset_count: int | None = None


def _safe_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _safe_category(value: Any, fallback: str = "operation_failed") -> str:
    return value if isinstance(value, str) and value in _SAFE_ERROR_CATEGORIES else fallback


def _display_safe(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _safe_service_destination(value: Any) -> tuple[str, str]:
    """Build a model destination summary without credentials or URL query data."""
    try:
        parsed = urlsplit(str(value or ""))
        host = parsed.hostname or ""
        if not host:
            return "", ""
        try:
            port = parsed.port
        except ValueError:
            return _display_safe(host, 253), ""
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        netloc = f"{display_host}:{port}" if port is not None else display_host
        scheme = parsed.scheme.lower()
        destination = (
            urlunsplit((scheme, netloc, parsed.path or "/", "", ""))
            if scheme in {"http", "https"}
            else f"{netloc}{parsed.path or '/'}"
        )
        return _display_safe(host, 253), _display_safe(destination, 512)
    except (TypeError, ValueError):
        return "", ""


def _new_run_id(site_id: str, runs_directory: Path) -> str:
    timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    base = f"{site_id}_{timestamp}"
    candidate = base
    suffix = 2
    while (runs_directory / candidate).exists():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _image_count(questions_package: Any) -> int:
    if not isinstance(questions_package, dict):
        return 0
    questions = questions_package.get("questions")
    if not isinstance(questions, list):
        return 0
    paths: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            continue
        contents = [question.get("stem")]
        options = question.get("options")
        if isinstance(options, list):
            contents.extend(option.get("content") for option in options if isinstance(option, dict))
        for content in contents:
            if not isinstance(content, dict):
                continue
            assets = content.get("asset_paths")
            if isinstance(assets, list):
                paths.update(path for path in assets if isinstance(path, str))
    return len(paths)


class TaskRuntimeController:
    """Own the SQLite queue and run one blocking task at a time off the ASGI loop."""

    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store
        self._runs_directory = task_store.runs_directory
        self._credential_provider = LocalCredentialProvider()
        self._lock = threading.RLock()
        self._wake_worker = threading.Event()
        self._shutdown = threading.Event()
        self._active_request: _WorkerRequest | None = None
        self._stop_requested: set[str] = set()
        self._confirmed_profile_versions: dict[str, int] = {}
        self._pending_model_authorization: dict[
            str, tuple[ModelAuthorizationRequest, bool, bool, bool]
        ] = {}
        self._pending_confirmation: dict[
            str, tuple[SubmitConfirmationRequest, _ConfirmationGate]
        ] = {}
        self._sequence = 0
        self._events: deque[TaskRuntimeEvent] = deque(maxlen=5000)
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="autolearn-task-worker",
            daemon=False,
        )
        self._expiry_thread = threading.Thread(
            target=self._confirmation_expiry_loop,
            name="autolearn-confirmation-expiry",
            daemon=True,
        )
        self._worker_thread.start()
        self._expiry_thread.start()

    @property
    def active_task_id(self) -> str | None:
        with self._lock:
            request = self._active_request
            return request.context.task_id if request is not None else None

    @property
    def has_active_worker(self) -> bool:
        return self.active_task_id is not None

    @property
    def latest_event_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def events_after(
        self,
        sequence: int = 0,
        *,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        with self._lock:
            events = tuple(self._events)
        return [
            event.to_dict()
            for event in events
            if event.sequence > sequence and (task_id is None or event.task_id == task_id)
        ]

    def get_pending_confirmation(self, task_id: str) -> SubmitConfirmationRequest | None:
        with self._lock:
            pending = self._pending_confirmation.get(task_id)
            if pending is None:
                return None
            return pending[0]

    def get_pending_model_authorization(
        self, task_id: str
    ) -> ModelAuthorizationRequest | None:
        with self._lock:
            pending = self._pending_model_authorization.get(task_id)
            if pending is None:
                return None
            return pending[0]

    def enqueue_authentication(self, task_id: str, *, retry_failed: bool = False) -> QueueItem:
        return self._enqueue(task_id, Operation.AUTHENTICATE, retry_failed=retry_failed)

    def enqueue_crawl(self, task_id: str, *, retry_failed: bool = False) -> QueueItem:
        return self._enqueue(task_id, Operation.CRAWL, retry_failed=retry_failed)

    def enqueue_interaction(
        self,
        task_id: str,
        *,
        overwrite_existing_result: bool = False,
    ) -> QueueItem:
        return self._enqueue(
            task_id,
            Operation.INTERACT,
            overwrite_existing_result=overwrite_existing_result,
        )

    def _enqueue(self, task_id: str, operation: Operation, **options: Any) -> QueueItem:
        item = self._task_store.enqueue_operation(task_id, operation, **options)
        task = self._task_store.get_task(task_id)
        self._emit_event(task_id, task.run_id, _STAGE_BY_OPERATION[operation], "queued")
        self._wake_worker.set()
        return item

    def request_model_authorization(
        self,
        task_id: str,
        *,
        confirmed_profile_version: int,
        profile_update_confirmed: bool = False,
        resume_paused_solve: bool = False,
        retry_failed: bool = False,
        overwrite_existing_result: bool = False,
    ) -> ModelAuthorizationRequest:
        task = self._task_store.get_task(task_id)
        if not task.run_id or not task.model_profile_id:
            raise TaskTransitionError("任务缺少题包或模型档案。")
        profile = ModelProfileStore().get_profile(task.model_profile_id)
        if profile.version != confirmed_profile_version:
            raise ModelProfileError("模型档案已再次更新；请重新确认。")
        if profile_changed_after(profile, task.created_at) and not profile_update_confirmed:
            raise ModelProfileError("模型档案尚未确认。")

        paused_solve = (
            resume_paused_solve
            and task.status == TaskStatus.PAUSED
            and task.pending_operation == Operation.SOLVE
            and task.queue_state == QueueState.PAUSED
        )
        failed_solve_retry = (
            retry_failed
            and task.status == TaskStatus.FAILED
            and task.last_operation == Operation.SOLVE
        )
        with self._lock:
            if task_id in self._pending_model_authorization:
                raise TaskTransitionError("此任务已有待处理的模型授权请求。")

        if task.status == TaskStatus.PACKAGE_REVIEW:
            pass
        elif task.status == TaskStatus.WAITING_MODEL_AUTHORIZATION:
            pass
        elif not paused_solve and not failed_solve_retry:
            raise TaskTransitionError("当前任务阶段不能请求模型授权。")
        if failed_solve_retry and not overwrite_existing_result:
            raise TaskTransitionError("重试解题前必须显式确认覆盖已有答案。")

        package_path = self._runs_directory / task.run_id / "questions.json"
        try:
            questions = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise TaskStoreError("无法读取题包统计信息。") from None
        question_items = questions.get("questions") if isinstance(questions, dict) else None
        if not isinstance(question_items, list) or not question_items:
            raise TaskStoreError("题包题目数量无效。")

        if task.status == TaskStatus.PACKAGE_REVIEW:
            self._task_store.request_model_authorization(task_id)
        settings = profile.settings
        service_host, service_destination = _safe_service_destination(settings.get("base_url", ""))
        request = ModelAuthorizationRequest(
            task_id=task.task_id,
            run_id=task.run_id,
            profile_id=profile.profile_id,
            profile_name=_display_safe(profile.name),
            profile_version=profile.version,
            provider=_display_safe(settings.get("provider")),
            model=_display_safe(settings.get("model")),
            service_host=service_host,
            service_destination=service_destination,
            question_count=len(question_items),
            image_count=_image_count(questions),
        )
        with self._lock:
            self._pending_model_authorization[task_id] = (
                request,
                paused_solve,
                failed_solve_retry,
                overwrite_existing_result,
            )
        self._emit_event(
            task_id,
            task.run_id,
            "model_authorization",
            "awaiting_user_authorization",
            question_count=request.question_count,
            asset_count=request.image_count,
        )
        return request

    def respond_model_authorization(self, task_id: str, accepted: bool) -> bool:
        with self._lock:
            pending = self._pending_model_authorization.pop(task_id, None)
        if pending is None:
            return False
        request, resume_paused_solve, retry_failed, overwrite_existing_result = pending
        if not accepted:
            if not resume_paused_solve:
                try:
                    task = self._task_store.get_task(task_id)
                    if task.status == TaskStatus.WAITING_MODEL_AUTHORIZATION:
                        self._task_store.cancel_model_authorization(task_id)
                except (TaskStoreError, OSError, ValueError, KeyError):
                    pass
            self._emit_event(task_id, request.run_id, "model_authorization", "cancelled")
            return True

        try:
            current = ModelProfileStore().get_profile(request.profile_id)
        except (ModelProfileError, OSError, ValueError):
            self._cancel_waiting_model_authorization(task_id, resume_paused_solve)
            self._emit_event(
                task_id,
                request.run_id,
                "model_authorization",
                "profile_unavailable",
                safe_error_category="config",
            )
            return False
        if current.version != request.profile_version:
            self._cancel_waiting_model_authorization(task_id, resume_paused_solve)
            self._emit_event(
                task_id,
                request.run_id,
                "model_authorization",
                "profile_changed",
                safe_error_category="config",
            )
            return False

        if resume_paused_solve:
            try:
                self._task_store.resume_paused_operation(task_id, model_authorized=True)
            except (TaskStoreError, OSError, ValueError, KeyError):
                self._emit_event(
                    task_id,
                    request.run_id,
                    "solve",
                    "resume_unavailable",
                    safe_error_category="operation_failed",
                )
                return False
            with self._lock:
                self._confirmed_profile_versions[task_id] = request.profile_version
            self._emit_event(task_id, request.run_id, "solve", "queued")
            self._wake_worker.set()
            return True

        try:
            self._confirmed_profile_versions[task_id] = request.profile_version
            self._enqueue(
                task_id,
                Operation.SOLVE,
                model_authorized=True,
                retry_failed=retry_failed,
                overwrite_existing_result=overwrite_existing_result,
            )
        except Exception:
            with self._lock:
                self._confirmed_profile_versions.pop(task_id, None)
            self._cancel_waiting_model_authorization(task_id, resume_paused_solve)
            raise
        return True

    def _cancel_waiting_model_authorization(self, task_id: str, paused: bool) -> None:
        if paused:
            return
        try:
            task = self._task_store.get_task(task_id)
            if task.status == TaskStatus.WAITING_MODEL_AUTHORIZATION:
                self._task_store.cancel_model_authorization(task_id)
        except (TaskStoreError, OSError, ValueError, KeyError):
            pass

    def resume_paused_operation(
        self,
        task_id: str,
        *,
        confirmed_profile_version: int | None = None,
        profile_update_confirmed: bool = False,
    ) -> QueueItem | ModelAuthorizationRequest:
        task = self._task_store.get_task(task_id)
        queue_item = self._task_store.get_queue_item(task_id)
        if task.status != TaskStatus.PAUSED or queue_item.state != QueueState.PAUSED:
            raise TaskTransitionError("任务当前没有可恢复的暂停操作。")
        if queue_item.operation == Operation.SOLVE:
            if confirmed_profile_version is None:
                raise TaskTransitionError("恢复解题前必须确认模型请求摘要。")
            return self.request_model_authorization(
                task_id,
                confirmed_profile_version=confirmed_profile_version,
                profile_update_confirmed=profile_update_confirmed,
                resume_paused_solve=True,
            )
        resumed = self._task_store.resume_paused_operation(task_id)
        self._emit_event(
            task_id,
            task.run_id,
            _STAGE_BY_OPERATION[queue_item.operation],
            "queued",
        )
        self._wake_worker.set()
        return resumed

    def respond_submit_confirmation(
        self,
        task_id: str,
        run_id: str,
        confirmation_id: str,
        decision: str,
    ) -> bool:
        if decision not in {"confirm", "cancel"}:
            return False
        with self._lock:
            pending = self._pending_confirmation.get(task_id)
            if pending is None:
                return False
            request, gate = pending
            if request.run_id != run_id or request.confirmation_id != confirmation_id:
                return False
            try:
                accepted = self._task_store.respond_to_submit_confirmation(
                    task_id,
                    run_id,
                    confirmation_id,
                    decision,
                )
            except (TaskStoreError, OSError, ValueError, KeyError):
                self._pending_confirmation.pop(task_id, None)
                gate.resolve("cancel")
                return False
            self._pending_confirmation.pop(task_id, None)
            active = self._active_request
            if accepted == "cancel" and active is not None and active.context.task_id == task_id:
                active.cancel_event.set()
            gate.resolve(accepted)

        self._emit_event(
            task_id,
            run_id,
            "submit_confirmation",
            "confirmed_by_user" if accepted == "confirm" else "cancelled_by_user",
        )
        return True

    @staticmethod
    def _confirmation_expired(expires_at: str) -> bool:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.astimezone()
            return expiry <= datetime.now().astimezone()
        except (AttributeError, TypeError, ValueError):
            return True

    def _confirmation_expiry_loop(self) -> None:
        while not self._shutdown.wait(0.25):
            expired: list[tuple[str, SubmitConfirmationRequest]] = []
            with self._lock:
                candidates = [
                    (task_id, request, gate)
                    for task_id, (request, gate) in self._pending_confirmation.items()
                    if self._confirmation_expired(request.expires_at)
                ]
                # The worker timeout, expiry monitor and browser response can
                # race. Serialize the durable cancel with those paths while
                # holding the same lock used by respond_submit_confirmation.
                for task_id, request, gate in candidates:
                    pending = self._pending_confirmation.get(task_id)
                    if pending is None or pending[1] is not gate:
                        continue
                    try:
                        self._task_store.respond_to_submit_confirmation(
                            request.task_id,
                            request.run_id,
                            request.confirmation_id,
                            "cancel",
                        )
                    except (TaskStoreError, OSError, ValueError, KeyError):
                        pass
                    self._pending_confirmation.pop(task_id, None)
                    active = self._active_request
                    if active is not None and active.context.task_id == task_id:
                        active.cancel_event.set()
                    gate.resolve("cancel")
                    expired.append((task_id, request))
            for task_id, request in expired:
                self._emit_event(task_id, request.run_id, "submit_confirmation", "expired")

    def request_stop(self, task_id: str) -> None:
        with self._lock:
            request = self._active_request
            if request is None or request.context.task_id != task_id:
                return
            self._stop_requested.add(task_id)
            request.cancel_event.set()
            pending = self._pending_confirmation.pop(task_id, None)
            if pending is not None:
                pending[1].resolve("cancel")

    def shutdown(self, timeout_seconds: float | None = 10.0) -> bool:
        self._shutdown.set()
        with self._lock:
            request = self._active_request
            if request is not None:
                request.cancel_event.set()
            pending = tuple(self._pending_confirmation.items())
            for task_id, (confirmation, gate) in pending:
                try:
                    self._task_store.respond_to_submit_confirmation(
                        confirmation.task_id,
                        confirmation.run_id,
                        confirmation.confirmation_id,
                        "cancel",
                    )
                except (TaskStoreError, OSError, ValueError, KeyError):
                    # request_cancel_active_operation may already have
                    # consumed the confirmation token and persisted cancel.
                    pass
                gate.resolve("cancel")
            self._pending_confirmation.clear()
        self._wake_worker.set()
        self._worker_thread.join(timeout_seconds)
        self._expiry_thread.join(1.0)
        return not self._worker_thread.is_alive()

    def _worker_loop(self) -> None:
        while not self._shutdown.is_set():
            if self._active_request is None:
                self._claim_and_execute_next()
            if self._active_request is None:
                self._wake_worker.wait(0.2)
                self._wake_worker.clear()

    def _claim_and_execute_next(self) -> None:
        try:
            claim = self._task_store.claim_next()
        except (TaskStoreError, OSError, ValueError, KeyError):
            return
        if claim is None:
            return
        task: TaskRecord | None = None
        request: _WorkerRequest | None = None
        try:
            task = self._task_store.get_task(claim.task_id)
            run_id = _new_run_id(task.site_id, self._runs_directory) if claim.operation == Operation.CRAWL else task.run_id
            context = TaskExecutionContext(
                task_id=task.task_id,
                site_id=task.site_id,
                operation=claim.operation,
                run_id=run_id,
                page_url=task.page_url,
                model_profile_id=task.model_profile_id,
                task_created_at=task.created_at,
                credential_provider=self._credential_provider,
            )
            with self._lock:
                request = _WorkerRequest(
                    claim=claim,
                    context=context,
                    runs_directory=self._runs_directory,
                    confirmed_profile_version=self._confirmed_profile_versions.get(task.task_id),
                )
                self._active_request = request
            self._emit_event(
                context.task_id,
                context.run_id,
                _STAGE_BY_OPERATION[claim.operation],
                "started",
            )
            result = self._execute_operation(request)
        except Exception:
            fallback_run_id = task.run_id if task is not None and claim.operation in {Operation.SOLVE, Operation.INTERACT} else None
            result = _WorkerResult(
                claim.task_id,
                claim.operation,
                "failed",
                run_id=fallback_run_id,
                safe_error_category="operation_failed",
            )
        self._finish_worker_operation(claim, result)

    def _execute_operation(self, request: _WorkerRequest) -> _WorkerResult:
        context = request.context
        operation = request.claim.operation
        progress = self._progress_callback(request)
        progress("started")
        try:
            if operation == Operation.AUTHENTICATE:
                self._execute_authentication(request, progress)
                return _WorkerResult(context.task_id, operation, "completed", context.run_id)
            if operation == Operation.CRAWL:
                result = self._execute_crawl(request, progress)
                return _WorkerResult(
                    context.task_id,
                    operation,
                    "completed",
                    run_id=result["run_id"],
                    question_count=_safe_count(result.get("question_count")),
                    asset_count=_safe_count(result.get("asset_count")),
                )
            if operation == Operation.SOLVE:
                question_count, image_count = self._execute_solve(request, progress)
                return _WorkerResult(
                    context.task_id,
                    operation,
                    "completed",
                    context.run_id,
                    question_count=question_count,
                    asset_count=image_count,
                )
            if operation == Operation.INTERACT:
                outcome, question_count = self._execute_interaction(request, progress)
                return _WorkerResult(
                    context.task_id,
                    operation,
                    outcome,
                    context.run_id,
                    question_count=question_count,
                )
            return _WorkerResult(
                context.task_id,
                operation,
                "failed",
                context.run_id,
                safe_error_category="operation_failed",
            )
        except (CookieCaptureCancelled, SolverCancelled):
            return _WorkerResult(
                context.task_id,
                operation,
                "cancelled" if request.cancel_event.is_set() else "failed",
                context.run_id,
                safe_error_category=None if request.cancel_event.is_set() else "operation_failed",
            )
        except Exception as error:
            category = self._error_category(error, operation)
            if operation == Operation.INTERACT:
                read_only_errors = {
                    "file_missing",
                    "file_read",
                    "invalid_shape",
                    "invalid_site",
                    "unknown_adapter",
                    "config",
                    "invalid_metadata",
                    "schema_definition",
                    "answer_reference",
                    "invalid_answer",
                }
                error_category = getattr(error, "category", None)
                outcome = (
                    "failed"
                    if isinstance(error, InteractionRunnerError) and error_category in read_only_errors
                    else "unconfirmed"
                )
                if outcome == "unconfirmed":
                    category = "interaction"
            elif operation == Operation.CRAWL and request.cancel_event.is_set() and type(error).__name__ == "CrawlCancelled":
                outcome, category = "cancelled", None
            else:
                outcome = "failed"
            return _WorkerResult(
                context.task_id,
                operation,
                outcome,
                context.run_id,
                safe_error_category=category,
            )

    @staticmethod
    def _error_category(error: Exception, operation: Operation) -> str:
        if isinstance(error, (CookieManagerError, CredentialProviderError, ModelProfileError, ModelProfileConfirmationRequired)):
            return "config"
        if isinstance(error, ModelAPIError):
            return "model_request"
        if isinstance(error, SolverError):
            return _safe_category(getattr(error, "category", None))
        if isinstance(error, InteractionRunnerError):
            return _safe_category(getattr(error, "category", None), "interaction")
        if type(error).__name__ == "CrawlCancelled":
            return "operation_failed"
        return "config" if operation == Operation.AUTHENTICATE else "operation_failed"

    def _progress_callback(self, request: _WorkerRequest) -> Callable[..., None]:
        def report(status: str, **counts: Any) -> None:
            safe_status = status if status in _SAFE_PROGRESS_STATES else "progress"
            if safe_status == "awaiting_submit_confirmation":
                return
            context = request.context
            self._emit_event(
                context.task_id,
                context.run_id,
                _STAGE_BY_OPERATION[context.operation],
                safe_status,
                question_count=_safe_count(counts.get("question_count")),
                asset_count=_safe_count(counts.get("asset_count")),
                completed_count=_safe_count(counts.get("completed_count")),
            )
        return report

    @staticmethod
    def _execute_authentication(request: _WorkerRequest, progress: Callable[..., None]) -> None:
        context = request.context
        if not context.page_url:
            raise CookieManagerError("task URL is unavailable")
        capture_for_adapter(
            context.site_id,
            context.page_url,
            status_callback=lambda status: progress(status),
            cancel_event=request.cancel_event,
        )

    @staticmethod
    def _execute_crawl(request: _WorkerRequest, progress: Callable[..., None]) -> dict[str, Any]:
        context = request.context
        if not context.page_url or not context.run_id:
            raise ValueError("task crawl context is incomplete")
        module = importlib.import_module(f"core.adapters.{context.site_id}.crawler")
        function = getattr(module, f"crawl_{context.site_id}", None)
        if not callable(function):
            raise ValueError("site crawler is unavailable")
        return function(
            context.page_url,
            task_id=context.task_id,
            run_id=context.run_id,
            model_profile_id=context.model_profile_id,
            output_dir=request.runs_directory / context.run_id,
            credential_provider=context.credential_provider,
            cancel_event=request.cancel_event,
            progress_callback=progress,
        )

    @staticmethod
    def _execute_solve(request: _WorkerRequest, progress: Callable[..., None]) -> tuple[int | None, int | None]:
        context = request.context
        if not context.run_id or not context.model_profile_id or not context.task_created_at:
            raise ValueError("task solve context is incomplete")
        package_dir = request.runs_directory / context.run_id
        question_count = image_count = None
        try:
            questions = json.loads((package_dir / "questions.json").read_text(encoding="utf-8"))
            if isinstance(questions, dict) and isinstance(questions.get("questions"), list):
                question_count = len(questions["questions"])
                image_count = _image_count(questions)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        solve_package(
            package_dir,
            overwrite=request.claim.overwrite_result,
            task_id=context.task_id,
            run_id=context.run_id,
            page_url=context.page_url,
            model_profile_id=context.model_profile_id,
            task_created_at=context.task_created_at,
            confirmed_profile_version=request.confirmed_profile_version,
            credential_provider=context.credential_provider,
            cancel_event=request.cancel_event,
            progress_callback=progress,
        )
        return question_count, image_count

    def _execute_interaction(
        self,
        request: _WorkerRequest,
        progress: Callable[..., None],
    ) -> tuple[str, int | None]:
        context = request.context
        if not context.run_id or not context.page_url:
            raise ValueError("task interaction context is incomplete")

        def await_submit_confirmation(question_count: int) -> bool:
            return self._await_submit_confirmation(request, question_count)

        result_path = interact_package(
            request.runs_directory / context.run_id,
            overwrite=request.claim.overwrite_result,
            task_id=context.task_id,
            run_id=context.run_id,
            page_url=context.page_url,
            model_profile_id=context.model_profile_id,
            credential_provider=context.credential_provider,
            cancel_event=request.cancel_event,
            progress_callback=progress,
            submit_confirmation_callback=await_submit_confirmation,
        )
        try:
            result = json.loads(Path(result_path).read_text(encoding="utf-8"))
            items = result.get("items") if isinstance(result, dict) else None
            if not isinstance(items, list) or not items:
                raise ValueError("interaction result is invalid")
            statuses = {
                item.get("status")
                for item in items
                if isinstance(item, dict) and isinstance(item.get("status"), str)
            }
            if len(statuses) != 1:
                outcome = "unconfirmed"
            else:
                status = next(iter(statuses))
                outcome = status if status in {
                    "confirmed", "failed", "unconfirmed", "skipped", "filled_unsubmitted"
                } else "unconfirmed"
            return outcome, len(items)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return "unconfirmed", None

    def _await_submit_confirmation(self, request: _WorkerRequest, question_count: int) -> bool:
        context = request.context
        if not context.run_id or request.cancel_event.is_set():
            return False
        try:
            confirmation = self._task_store.await_submit_confirmation(
                context.task_id,
                request.claim.lease_token,
            )
        except (TaskStoreError, OSError, ValueError, KeyError):
            return False
        gate = _ConfirmationGate()
        pending = SubmitConfirmationRequest(
            task_id=confirmation.task_id,
            run_id=confirmation.run_id,
            site_id=confirmation.site_id,
            confirmation_id=confirmation.confirmation_id,
            expires_at=confirmation.expires_at,
            question_count=_safe_count(question_count) or 0,
        )
        with self._lock:
            if self._shutdown.is_set() or request.cancel_event.is_set():
                gate.resolve("cancel")
            else:
                self._pending_confirmation[context.task_id] = (pending, gate)
        if request.cancel_event.is_set() or self._shutdown.is_set():
            try:
                self._task_store.respond_to_submit_confirmation(
                    pending.task_id,
                    pending.run_id,
                    pending.confirmation_id,
                    "cancel",
                )
            except Exception:
                pass
            return False
        self._emit_event(
            pending.task_id,
            pending.run_id,
            "submit_confirmation",
            "awaiting_submit_confirmation",
            question_count=pending.question_count,
        )
        try:
            expiry = datetime.fromisoformat(pending.expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.astimezone()
            timeout = max(0.0, (expiry - datetime.now().astimezone()).total_seconds())
        except (AttributeError, TypeError, ValueError):
            timeout = 0.0
        confirmed = gate.wait(request.cancel_event, timeout_seconds=timeout)
        with self._lock:
            current = self._pending_confirmation.get(context.task_id)
            if current is not None and current[1] is gate:
                if not confirmed:
                    try:
                        self._task_store.respond_to_submit_confirmation(
                            pending.task_id,
                            pending.run_id,
                            pending.confirmation_id,
                            "cancel",
                        )
                    except (TaskStoreError, OSError, ValueError, KeyError):
                        # A concurrent expiry, stop or user decision may have
                        # already consumed this one-shot token.
                        pass
                self._pending_confirmation.pop(context.task_id, None)
        return confirmed and not self._shutdown.is_set() and not request.cancel_event.is_set()

    def _finish_worker_operation(self, claim: ClaimedOperation, result: _WorkerResult) -> None:
        with self._lock:
            request = self._active_request
            if request is not None and request.context.task_id != result.task_id:
                request = None
        context = request.context if request is not None else None
        finish_run_id = result.run_id
        if result.operation in {Operation.SOLVE, Operation.INTERACT} and context is not None:
            finish_run_id = context.run_id
        if result.operation == Operation.CRAWL and result.outcome != "completed":
            finish_run_id = None
        category = _safe_category(result.safe_error_category) if result.safe_error_category else None
        finish_error: Exception | None = None
        for delay in (0.0, 0.1, 0.25):
            if delay:
                time.sleep(delay)
            try:
                self._task_store.finish_operation(
                    result.task_id,
                    claim.lease_token,
                    outcome=result.outcome,
                    run_id=finish_run_id,
                    safe_error_category=category,
                )
                finish_error = None
                break
            except (TaskStoreError, OSError, ValueError, KeyError) as error:
                finish_error = error
        if finish_error is not None:
            # Do not release the in-memory active slot when the queue lease
            # could not be released durably. The task remains conservatively
            # active until the database is available or the app restarts.
            self._emit_event(
                result.task_id,
                finish_run_id,
                _STAGE_BY_OPERATION[result.operation],
                "failed",
                question_count=result.question_count,
                asset_count=result.asset_count,
                safe_error_category="operation_failed",
            )
            return

        if context is not None:
            with self._lock:
                pending = self._pending_confirmation.pop(context.task_id, None)
                if pending is not None:
                    pending[1].resolve("cancel")
                if self._active_request is request:
                    self._active_request = None
                self._confirmed_profile_versions.pop(context.task_id, None)
                stopping = context.task_id in self._stop_requested
                self._stop_requested.discard(context.task_id)
        else:
            stopping = False
        self._emit_event(
            result.task_id,
            (
                context.run_id if context.operation != Operation.CRAWL else finish_run_id
            ) if context is not None else finish_run_id,
            _STAGE_BY_OPERATION[result.operation],
            result.outcome,
            question_count=result.question_count,
            asset_count=result.asset_count,
            safe_error_category=category,
        )
        if stopping:
            self._emit_event(result.task_id, finish_run_id, "task", "stop_completed")

    def _emit_event(
        self,
        task_id: str,
        run_id: str | None,
        stage: str,
        status: str,
        *,
        question_count: int | None = None,
        asset_count: int | None = None,
        completed_count: int | None = None,
        safe_error_category: str | None = None,
    ) -> None:
        with self._lock:
            self._sequence += 1
            self._events.append(
                TaskRuntimeEvent(
                    sequence=self._sequence,
                    task_id=task_id,
                    run_id=run_id,
                    stage=stage,
                    status=status,
                    question_count=_safe_count(question_count),
                    asset_count=_safe_count(asset_count),
                    completed_count=_safe_count(completed_count),
                    safe_error_category=(
                        _safe_category(safe_error_category)
                        if safe_error_category is not None
                        else None
                    ),
                )
            )


__all__ = [
    "ModelAuthorizationRequest",
    "SubmitConfirmationRequest",
    "TaskRuntimeController",
    "TaskRuntimeEvent",
]

