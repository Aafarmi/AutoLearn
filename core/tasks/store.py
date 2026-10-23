"""SQLite-backed task records, workflow transitions, and a serial queue.

This module contains no site automation. Callers run the claimed operation and
report its result through TaskStore. Task-list responses never include the full
page URL, and task records have no Cookie or API-key fields.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Iterator, Sequence
from urllib.parse import urlsplit
import uuid

from core.common.validation import validate_json_schema
from core.data_migration import ensure_user_data_migrated
from core.paths import data_path, resource_path
from core.url_protection import UrlProtectionError, protect_page_url, unprotect_page_url

from .models import (
    ClaimedOperation,
    Operation,
    QueueItem,
    QueueState,
    SubmitConfirmation,
    TaskEvent,
    TaskRecord,
    TaskStatus,
    TaskStep,
    TaskSummary,
)


_SCHEMA_VERSION = 2
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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
_TITLE_MAX_LENGTH = 200
_MAX_RECOVERY_JSON_BYTES = 64 * 1024 * 1024
_INTERACTION_UNCERTAIN_STATUSES = {
    TaskStatus.FILLING,
    TaskStatus.AWAITING_SUBMIT_CONFIRMATION,
    TaskStatus.SUBMITTING,
    TaskStatus.CANCELLING,
}
_OPERATION_STATUS = {
    Operation.AUTHENTICATE: (TaskStatus.AUTHENTICATING, TaskStep.AUTHENTICATION),
    Operation.CRAWL: (TaskStatus.CRAWLING, TaskStep.CRAWL),
    Operation.SOLVE: (TaskStatus.SOLVING, TaskStep.SOLVE),
    Operation.INTERACT: (TaskStatus.FILLING, TaskStep.INTERACTION),
}
_INTERACTION_RESULTS = {
    "confirmed": TaskStatus.CONFIRMED,
    "failed": TaskStatus.FAILED,
    "unconfirmed": TaskStatus.UNCONFIRMED,
    "skipped": TaskStatus.READY_FOR_INTERACTION,
    "filled_unsubmitted": TaskStatus.FILLED_UNSUBMITTED,
}


class TaskStoreError(RuntimeError):
    """Base error for task storage and state operations."""


class TaskStoreBusy(TaskStoreError):
    """Another application instance currently owns this task database."""


class TaskTransitionError(TaskStoreError):
    """The requested state change is invalid for the task's current state."""


class _InstanceLock:
    """Keep one task scheduler instance active per local database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            stream.close()
            raise TaskStoreBusy(
                "另一个 AutoLearn 实例正在管理此任务库。"
            ) from None
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            stream.close()


def default_data_directory() -> Path:
    """Return the per-user writable AutoLearn data directory."""
    return data_path()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    current = value or _now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _enum_value(enum_type: type, value: Any, field: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} is not a supported value") from None


def _validate_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field} has an invalid format")
    return value


def _validate_run_id(value: str, field: str = "run_id") -> str:
    if not isinstance(value, str) or not _RUN_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field} has an invalid format")
    return value


def _validate_profile_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError("model_profile_id has an invalid format")
    return value


def _validate_title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("title must be a string")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("title cannot be empty")
    if len(cleaned) > _TITLE_MAX_LENGTH:
        raise ValueError(f"title must be at most {_TITLE_MAX_LENGTH} characters")
    return cleaned


def _validate_page_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("page_url must be a non-empty HTTPS URL")
    candidate = value.strip()
    if any(ord(character) < 32 or character.isspace() for character in candidate):
        raise ValueError("page_url contains invalid whitespace")
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        raise ValueError("page_url is invalid") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("page_url must be an HTTPS URL without user info or fragment")
    return candidate


def _protect_stored_page_url(value: str | None) -> str | None:
    try:
        return protect_page_url(value)
    except UrlProtectionError:
        raise TaskStoreError("无法安全保存任务 URL。") from None


def _read_stored_page_url(value: str | None) -> str | None:
    try:
        return unprotect_page_url(value)
    except UrlProtectionError:
        raise TaskStoreError("无法读取受保护的任务 URL。") from None


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > _MAX_RECOVERY_JSON_BYTES:
            return None
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _matches_schema(value: Any, schema_path: Path) -> bool:
    schema = _read_json_object(schema_path)
    if schema is None:
        return False
    try:
        return not validate_json_schema(value, schema)
    except Exception:
        return False


def _run_result_statuses(value: dict[str, Any]) -> list[str] | None:
    items = value.get("items")
    if not isinstance(items, list) or not items:
        return None
    statuses: list[str] = []
    question_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        status = item.get("status")
        question_id = item.get("question_id")
        if not isinstance(status, str) or not isinstance(question_id, str):
            return None
        statuses.append(status)
        question_ids.append(question_id)
    if len(question_ids) != len(set(question_ids)):
        return None
    return statuses


def _discover_site_ids() -> set[str]:
    adapters_root = resource_path("core", "adapters")
    if not adapters_root.is_dir():
        return set()
    available: set[str] = set()
    for adapter_dir in adapters_root.iterdir():
        if (
            adapter_dir.is_dir()
            and _ID_PATTERN.fullmatch(adapter_dir.name)
            and all(
                (adapter_dir / filename).is_file()
                for filename in ("auth.py", "crawler.py", "solver.py", "interaction.py")
            )
        ):
            available.add(adapter_dir.name)
    return available


class TaskStore:
    """Persistent task repository and FIFO single-worker queue.

    A store owns an OS-level lock for its lifetime. The application must stop
    and join its worker before calling close. On opening, queued work is paused
    and interrupted work is reconciled; no work is automatically run.
    """

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        runs_directory: str | Path | None = None,
        supported_site_ids: Sequence[str] | None = None,
    ) -> None:
        if database_path is None:
            ensure_user_data_migrated()
            database_path = data_path("tasks.sqlite3")
        self.database_path = Path(database_path).expanduser().resolve()
        if runs_directory is None:
            runs_directory = data_path("runs")
        self.runs_directory = Path(runs_directory).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _InstanceLock(
            self.database_path.with_name(self.database_path.name + ".lock")
        )
        self._closed = False
        if supported_site_ids is None:
            self.supported_site_ids = _discover_site_ids()
        else:
            self.supported_site_ids = {
                _validate_id(site_id, "site_id") for site_id in supported_site_ids
            }
        self._lock.acquire()
        try:
            self._initialize()
            self._recover_after_restart()
        except Exception:
            self._lock.release()
            raise

    def close(self) -> None:
        """Pause queued work, reconcile leftovers, and release the instance lock.

        Call only after the UI has stopped and joined the worker. An operation
        still marked active is conservatively reconciled before releasing lock.
        """
        if self._closed:
            return
        try:
            self._recover_after_restart()
        finally:
            self._closed = True
            self._lock.release()

    def __enter__(self) -> "TaskStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise TaskStoreError("task store is closed")

    def _connect(self) -> sqlite3.Connection:
        self._ensure_open()
        connection = sqlite3.connect(
            self.database_path,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = sqlite3.connect(self.database_path, timeout=15, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise TaskStoreError("任务数据库版本高于当前应用支持的版本。")
            if version == 0:
                connection.execute("BEGIN IMMEDIATE")
                statements = (
                    """
                    CREATE TABLE tasks (
                        task_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        model_profile_id TEXT,
                        model_authorized_at TEXT,
                        run_id TEXT UNIQUE,
                        current_step TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_safe_error_category TEXT,
                        last_operation TEXT,
                        interrupted_operation TEXT,
                        confirmation_id TEXT,
                        confirmation_expires_at TEXT,
                        confirmation_decision TEXT,
                        interaction_retry_authorized INTEGER NOT NULL DEFAULT 0
                            CHECK (interaction_retry_authorized IN (0, 1))
                    )
                    """,
                    """
                    CREATE TABLE task_context (
                        task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
                        page_url TEXT
                    )
                    """,
                    """
                    CREATE TABLE task_queue (
                        queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
                        operation TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('queued', 'active', 'paused', 'blocked')
                        ),
                        enqueued_at TEXT NOT NULL,
                        started_at TEXT,
                        lease_token TEXT,
                        return_status TEXT NOT NULL,
                        overwrite_result INTEGER NOT NULL DEFAULT 0
                            CHECK (overwrite_result IN (0, 1))
                    )
                    """,
                    """
                    CREATE UNIQUE INDEX task_queue_one_active
                    ON task_queue(state) WHERE state = 'active'
                    """,
                    """
                    CREATE INDEX task_queue_order
                    ON task_queue(state, queue_id)
                    """,
                    """
                    CREATE TABLE task_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        occurred_at TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        from_status TEXT,
                        to_status TEXT,
                        operation TEXT,
                        safe_error_category TEXT
                    )
                    """,
                    """
                    CREATE INDEX task_events_by_task
                    ON task_events(task_id, event_id)
                    """,
                )
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                connection.commit()
            elif version == 1:
                self._backup_database(connection)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if os.name == "nt":
                        rows = connection.execute(
                            "SELECT task_id, page_url FROM task_context WHERE page_url IS NOT NULL"
                        ).fetchall()
                        protected_rows = [
                            (_protect_stored_page_url(row[1]), row[0])
                            for row in rows
                            if not str(row[1]).startswith("dpapi-currentuser:v1:")
                        ]
                        connection.executemany(
                            "UPDATE task_context SET page_url = ? WHERE task_id = ?",
                            protected_rows,
                        )
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            elif version == _SCHEMA_VERSION and os.name == "nt":
                rows = connection.execute(
                    "SELECT task_id, page_url FROM task_context WHERE page_url IS NOT NULL"
                ).fetchall()
                protected_rows = [
                    (_protect_stored_page_url(row[1]), row[0])
                    for row in rows
                    if not str(row[1]).startswith("dpapi-currentuser:v1:")
                ]
                if protected_rows:
                    self._backup_database(connection)
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.executemany(
                            "UPDATE task_context SET page_url = ? WHERE task_id = ?",
                            protected_rows,
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
            elif version != _SCHEMA_VERSION:
                raise TaskStoreError("任务数据库需要升级后才能打开。")
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _backup_database(self, connection: sqlite3.Connection) -> Path:
        backup_directory = self.database_path.parent / "backups"
        backup_directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_directory / (
            f"tasks-before-migration-v{_SCHEMA_VERSION}-{timestamp}-{uuid.uuid4().hex[:8]}.sqlite3"
        )
        backup_connection: sqlite3.Connection | None = None
        try:
            backup_connection = sqlite3.connect(backup_path)
            connection.backup(backup_connection)
            backup_connection.close()
            backup_connection = None
            if os.name == "nt":
                backup_connection = sqlite3.connect(backup_path, isolation_level=None)
                rows = backup_connection.execute(
                    "SELECT task_id, page_url FROM task_context WHERE page_url IS NOT NULL"
                ).fetchall()
                protected_rows = [
                    (_protect_stored_page_url(row[1]), row[0])
                    for row in rows
                    if not str(row[1]).startswith("dpapi-currentuser:v1:")
                ]
                if protected_rows:
                    backup_connection.execute("BEGIN IMMEDIATE")
                    backup_connection.executemany(
                        "UPDATE task_context SET page_url = ? WHERE task_id = ?",
                        protected_rows,
                    )
                    backup_connection.commit()
                backup_connection.close()
                backup_connection = None
            verification_connection = sqlite3.connect(backup_path)
            try:
                integrity = verification_connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                if not integrity or integrity[0] != "ok":
                    raise TaskStoreError("任务数据库备份校验失败；数据库未迁移。")
            finally:
                verification_connection.close()
            try:
                backup_path.chmod(0o600)
            except OSError:
                if os.name != "nt":
                    raise
            return backup_path
        except Exception:
            if backup_connection is not None:
                backup_connection.close()
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise TaskStoreError("无法备份任务数据库；数据库未迁移。") from None

    def _record_event(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        event_type: str,
        from_status: TaskStatus | None,
        to_status: TaskStatus | None,
        operation: Operation | None = None,
        safe_error_category: str | None = None,
    ) -> None:
        if safe_error_category is not None:
            safe_error_category = self._validate_error_category(safe_error_category)
        connection.execute(
            """
            INSERT INTO task_events (
                task_id, occurred_at, event_type, from_status, to_status,
                operation, safe_error_category
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                _timestamp(),
                event_type,
                from_status.value if from_status else None,
                to_status.value if to_status else None,
                operation.value if operation else None,
                safe_error_category,
            ),
        )

    @staticmethod
    def _validate_error_category(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("safe error category must be a string")
        # Persist only known fixed labels; never let caller-supplied detail land
        # in task metadata or the event journal.
        return value if value in _SAFE_ERROR_CATEGORIES else None

    def _run_directory(self, run_id: str | None) -> Path | None:
        if (
            not isinstance(run_id, str)
            or not _RUN_ID_PATTERN.fullmatch(run_id)
        ):
            return None
        unresolved = self.runs_directory / run_id
        is_junction = getattr(unresolved, "is_junction", lambda: False)
        if unresolved.is_symlink() or is_junction():
            return None
        try:
            candidate = unresolved.resolve()
        except OSError:
            return None
        if candidate.parent != self.runs_directory or not candidate.is_dir():
            return None
        return candidate

    @staticmethod
    def _artifact_is_newer_than_start(path: Path, started_at: str | None) -> bool:
        if not started_at:
            return False
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            earliest_expected = _parse_timestamp(started_at) + timedelta(milliseconds=1)
        except (OSError, OverflowError, ValueError):
            return False
        return modified_at >= earliest_expected

    @staticmethod
    def _schema_path(site_id: str, package_name: str) -> Path | None:
        adapters_root = resource_path("core", "adapters").resolve()
        candidate = (
            adapters_root / site_id / "schemas" / f"{package_name}.schema.json"
        ).resolve()
        if adapters_root not in candidate.parents or not candidate.is_file():
            return None
        return candidate

    @staticmethod
    def _run_file(run_directory: Path, filename: str) -> Path | None:
        try:
            candidate = (run_directory / filename).resolve()
        except (OSError, RuntimeError):
            return None
        if candidate.parent != run_directory or not candidate.is_file():
            return None
        return candidate

    def _question_ids(self, run_directory: Path, site_id: str, run_id: str) -> set[str] | None:
        questions_path = self._run_file(run_directory, "questions.json")
        if questions_path is None:
            return None
        package = _read_json_object(questions_path)
        schema_path = self._schema_path(site_id, "questions")
        if (
            package is None
            or schema_path is None
            or package.get("site_id") != site_id
            or package.get("run_id") != run_id
            or not _matches_schema(package, schema_path)
        ):
            return None
        questions = package.get("questions")
        if not isinstance(questions, list) or not questions:
            return None
        question_ids = [
            item.get("question_id")
            for item in questions
            if isinstance(item, dict) and isinstance(item.get("question_id"), str)
        ]
        if len(question_ids) != len(questions) or len(question_ids) != len(set(question_ids)):
            return None
        return set(question_ids)

    def _has_completed_solve_result(
        self, site_id: str, run_id: str | None, started_at: str | None
    ) -> bool:
        run_directory = self._run_directory(run_id)
        if run_directory is None or run_id is None:
            return False
        result_path = self._run_file(run_directory, "answers.json")
        schema_path = self._schema_path(site_id, "answers")
        if result_path is None:
            return False
        package = _read_json_object(result_path)
        if (
            not self._artifact_is_newer_than_start(result_path, started_at)
            or package is None
            or schema_path is None
            or package.get("site_id") != site_id
            or package.get("run_id") != run_id
            or not _matches_schema(package, schema_path)
        ):
            return False
        expected_ids = self._question_ids(run_directory, site_id, run_id)
        answers = package.get("answers")
        if expected_ids is None or not isinstance(answers, list) or not answers:
            return False
        answer_ids = [
            item.get("question_id")
            for item in answers
            if isinstance(item, dict) and isinstance(item.get("question_id"), str)
        ]
        return (
            len(answer_ids) == len(answers)
            and len(answer_ids) == len(set(answer_ids))
            and set(answer_ids) == expected_ids
        )

    def _recovered_interaction_status(
        self, site_id: str, run_id: str | None, started_at: str | None
    ) -> TaskStatus | None:
        run_directory = self._run_directory(run_id)
        if run_directory is None or run_id is None:
            return None
        result_path = self._run_file(run_directory, "execution_result.json")
        schema_path = self._schema_path(site_id, "execution_result")
        if result_path is None:
            return None
        package = _read_json_object(result_path)
        if (
            not self._artifact_is_newer_than_start(result_path, started_at)
            or package is None
            or schema_path is None
            or package.get("site_id") != site_id
            or package.get("run_id") != run_id
            or not _matches_schema(package, schema_path)
        ):
            return None
        expected_ids = self._question_ids(run_directory, site_id, run_id)
        statuses = _run_result_statuses(package)
        items = package.get("items")
        result_ids = {
            item["question_id"] for item in items if isinstance(item, dict)
        } if isinstance(items, list) else set()
        if (
            expected_ids is None
            or statuses is None
            or len(statuses) != len(expected_ids)
            or result_ids != expected_ids
        ):
            return None
        distinct = set(statuses)
        if distinct == {"confirmed"}:
            return TaskStatus.CONFIRMED
        if distinct == {"failed"}:
            return TaskStatus.FAILED
        if distinct == {"skipped"}:
            return TaskStatus.READY_FOR_INTERACTION
        if distinct == {"filled_unsubmitted"}:
            return TaskStatus.FILLED_UNSUBMITTED
        return TaskStatus.UNCONFIRMED

    def _finish_recovered_operation(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        previous_status: TaskStatus,
        operation: Operation,
        new_status: TaskStatus,
    ) -> None:
        safe_error_category = (
            "operation_failed"
            if new_status == TaskStatus.FAILED
            else "result_unconfirmed"
            if new_status == TaskStatus.UNCONFIRMED
            else None
        )
        connection.execute("DELETE FROM task_queue WHERE task_id = ?", (task_id,))
        connection.execute(
            """
            UPDATE tasks
            SET status = ?, current_step = ?, updated_at = ?,
                last_safe_error_category = ?, last_operation = ?,
                interrupted_operation = NULL, confirmation_id = NULL,
                confirmation_expires_at = NULL, confirmation_decision = NULL,
                interaction_retry_authorized = 0,
                model_authorized_at = CASE WHEN ? = 'solve' THEN NULL
                                           ELSE model_authorized_at END
            WHERE task_id = ?
            """,
            (
                new_status.value,
                self._step_for_status(new_status).value,
                _timestamp(),
                safe_error_category,
                operation.value,
                operation.value,
                task_id,
            ),
        )
        self._record_event(
            connection,
            task_id=task_id,
            event_type=f"{operation.value}_result_recovered",
            from_status=previous_status,
            to_status=new_status,
            operation=operation,
            safe_error_category=safe_error_category,
        )

    def _recover_after_restart(self) -> None:
        """Adopt matching completed outputs; pause or inspect all other work."""
        self._ensure_open()
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT q.queue_id, q.task_id, q.operation, q.state, q.started_at,
                       t.status, t.site_id, t.run_id
                FROM task_queue q
                JOIN tasks t ON t.task_id = q.task_id
                WHERE q.state IN ('queued', 'active')
                ORDER BY q.queue_id
                """
            ).fetchall()
            for row in rows:
                task_id = row["task_id"]
                operation = Operation(row["operation"])
                previous_status = TaskStatus(row["status"])
                if row["state"] == QueueState.QUEUED.value:
                    connection.execute(
                        "UPDATE task_queue SET state = 'paused' WHERE queue_id = ?",
                        (row["queue_id"],),
                    )
                    if previous_status == TaskStatus.QUEUED:
                        connection.execute(
                            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                            (TaskStatus.PAUSED.value, _timestamp(), task_id),
                        )
                        self._record_event(
                            connection,
                            task_id=task_id,
                            event_type="queue_paused_on_restart",
                            from_status=previous_status,
                            to_status=TaskStatus.PAUSED,
                            operation=operation,
                        )
                    continue

                if operation == Operation.SOLVE and self._has_completed_solve_result(
                    row["site_id"], row["run_id"], row["started_at"]
                ):
                    self._finish_recovered_operation(
                        connection,
                        task_id=task_id,
                        previous_status=previous_status,
                        operation=operation,
                        new_status=TaskStatus.ANSWER_REVIEW,
                    )
                    continue
                if operation == Operation.INTERACT:
                    recovered_status = self._recovered_interaction_status(
                        row["site_id"], row["run_id"], row["started_at"]
                    )
                    if recovered_status is not None:
                        self._finish_recovered_operation(
                            connection,
                            task_id=task_id,
                            previous_status=previous_status,
                            operation=operation,
                            new_status=recovered_status,
                        )
                        continue

                uncertain_interaction = (
                    operation == Operation.INTERACT
                    or previous_status in _INTERACTION_UNCERTAIN_STATUSES
                )
                if uncertain_interaction:
                    new_status = TaskStatus.NEEDS_INSPECTION
                    error_category = "interrupted_interaction"
                    queue_state = QueueState.BLOCKED.value
                else:
                    new_status = TaskStatus.PAUSED
                    error_category = "interrupted_operation"
                    queue_state = QueueState.PAUSED.value
                connection.execute(
                    """
                    UPDATE task_queue
                    SET state = ?, started_at = NULL, lease_token = NULL
                    WHERE queue_id = ?
                    """,
                    (queue_state, row["queue_id"]),
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, updated_at = ?, last_safe_error_category = ?,
                    interrupted_operation = ?, confirmation_id = NULL,
                    confirmation_expires_at = NULL, confirmation_decision = NULL
                    WHERE task_id = ?
                    """,
                    (
                        new_status.value,
                        _timestamp(),
                        error_category,
                        operation.value,
                        task_id,
                    ),
                )
                self._record_event(
                    connection,
                    task_id=task_id,
                    event_type="operation_interrupted",
                    from_status=previous_status,
                    to_status=new_status,
                    operation=operation,
                    safe_error_category=error_category,
                )

            orphaned = connection.execute(
                """
                SELECT task_id, status, last_operation
                FROM tasks
                WHERE status IN (
                    'authenticating', 'crawling', 'solving', 'filling',
                    'awaiting_submit_confirmation', 'submitting', 'cancelling'
                )
                AND task_id NOT IN (SELECT task_id FROM task_queue)
                """
            ).fetchall()
            for row in orphaned:
                previous_status = TaskStatus(row["status"])
                new_status = (
                    TaskStatus.NEEDS_INSPECTION
                    if previous_status in _INTERACTION_UNCERTAIN_STATUSES
                    else TaskStatus.PAUSED
                )
                error_category = (
                    "interrupted_interaction"
                    if new_status == TaskStatus.NEEDS_INSPECTION
                    else "interrupted_operation"
                )
                operation = (
                    Operation(row["last_operation"])
                    if row["last_operation"]
                    else None
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, updated_at = ?, last_safe_error_category = ?,
                    interrupted_operation = ?, confirmation_id = NULL,
                    confirmation_expires_at = NULL, confirmation_decision = NULL
                    WHERE task_id = ?
                    """,
                    (
                        new_status.value,
                        _timestamp(),
                        error_category,
                        operation.value if operation else None,
                        row["task_id"],
                    ),
                )
                self._record_event(
                    connection,
                    task_id=row["task_id"],
                    event_type="orphaned_operation_recovered",
                    from_status=previous_status,
                    to_status=new_status,
                    operation=operation,
                    safe_error_category=error_category,
                )

    def _validate_site_id(self, site_id: str) -> str:
        site_id = _validate_id(site_id, "site_id")
        if site_id not in self.supported_site_ids:
            raise ValueError("site_id is not in the registered adapter list")
        return site_id

    def create_task(
        self,
        site_id: str,
        *,
        title: str | None = None,
        page_url: str | None = None,
        model_profile_id: str | None = None,
    ) -> TaskRecord:
        """Create a draft task. Secret values are not accepted by this API."""
        site_id = self._validate_site_id(site_id)
        page_url = _validate_page_url(page_url)
        model_profile_id = _validate_profile_id(model_profile_id)
        task_id = str(uuid.uuid4())
        now = _timestamp()
        if title is None:
            hong_kong = timezone(timedelta(hours=8))
            default_title = datetime.now(hong_kong).strftime("%Y-%m-%d %H:%M")
            title = f"{site_id} {default_title}"
        title = _validate_title(title)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, site_id, title, model_profile_id, model_authorized_at,
                    run_id, current_step, status, created_at, updated_at,
                    last_safe_error_category, last_operation, interrupted_operation,
                    confirmation_id, confirmation_expires_at, confirmation_decision,
                    interaction_retry_authorized
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, 0)
                """,
                (
                    task_id,
                    site_id,
                    title,
                    model_profile_id,
                    TaskStep.SETUP.value,
                    TaskStatus.DRAFT.value,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO task_context (task_id, page_url) VALUES (?, ?)",
                (task_id, _protect_stored_page_url(page_url)),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="task_created",
                from_status=None,
                to_status=TaskStatus.DRAFT,
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        """Return task details, including its private page URL context."""
        task_id = _validate_id(task_id, "task_id")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT t.*, c.page_url, q.operation AS pending_operation,
                       q.state AS queue_state, q.queue_id
                FROM tasks t
                LEFT JOIN task_context c ON c.task_id = t.task_id
                LEFT JOIN task_queue q ON q.task_id = t.task_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError("task was not found")
            position = self._queue_position(connection, row["queue_id"], row["queue_state"])
            return self._task_record(row, position)
        finally:
            connection.close()

    def list_tasks(
        self,
        *,
        include_archived: bool = False,
        search: str | None = None,
        site_id: str | None = None,
        status: TaskStatus | str | None = None,
    ) -> list[TaskSummary]:
        """Return filtered task-list rows without URLs or confirmation tokens.

        Search matches task titles and adapter IDs only; the private task URL is
        intentionally never part of a list query.
        """
        if search is not None and not isinstance(search, str):
            raise ValueError("search must be a string")
        search_text = search.strip() if search else ""
        if site_id is not None:
            site_id = self._validate_site_id(site_id)
        if status is None:
            status_value = None
        else:
            try:
                status_value = (
                    status.value if isinstance(status, TaskStatus) else TaskStatus(status).value
                )
            except (TypeError, ValueError):
                raise ValueError("status is not a supported task status") from None

        conditions: list[str] = []
        parameters: list[str] = []
        if status_value is not None:
            conditions.append("t.status = ?")
            parameters.append(status_value)
        elif not include_archived:
            conditions.append("t.status != ?")
            parameters.append(TaskStatus.ARCHIVED.value)
        if site_id is not None:
            conditions.append("t.site_id = ?")
            parameters.append(site_id)
        if search_text:
            conditions.append(
                "(instr(lower(t.title), lower(?)) > 0 "
                "OR instr(lower(t.site_id), lower(?)) > 0)"
            )
            parameters.extend((search_text, search_text))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT t.task_id, t.site_id, t.title, t.model_profile_id, t.run_id,
                       t.current_step, t.status, t.created_at, t.updated_at,
                       t.last_safe_error_category, q.operation AS pending_operation,
                       q.state AS queue_state, q.queue_id
                FROM tasks t
                LEFT JOIN task_queue q ON q.task_id = t.task_id
                {where}
                ORDER BY
                    CASE WHEN q.state IN ('queued', 'active') THEN 0 ELSE 1 END,
                    q.queue_id,
                    t.updated_at DESC
                """,
                parameters,
            ).fetchall()
            queue_rows = connection.execute(
                """
                SELECT queue_id, task_id, state
                FROM task_queue
                ORDER BY queue_id
                """
            ).fetchall()
            positions: dict[str, int] = {}
            position = 0
            for queue_row in queue_rows:
                if queue_row["state"] in (
                    QueueState.QUEUED.value,
                    QueueState.ACTIVE.value,
                ):
                    position += 1
                    positions[queue_row["task_id"]] = position
            return [
                TaskSummary(
                    task_id=row["task_id"],
                    site_id=row["site_id"],
                    title=row["title"],
                    model_profile_id=row["model_profile_id"],
                    run_id=row["run_id"],
                    current_step=TaskStep(row["current_step"]),
                    status=TaskStatus(row["status"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    last_safe_error_category=row["last_safe_error_category"],
                    pending_operation=(
                        Operation(row["pending_operation"])
                        if row["pending_operation"]
                        else None
                    ),
                    queue_state=(
                        QueueState(row["queue_state"]) if row["queue_state"] else None
                    ),
                    queue_position=positions.get(row["task_id"]),
                )
                for row in rows
            ]
        finally:
            connection.close()

    @staticmethod
    def _queue_position(
        connection: sqlite3.Connection,
        queue_id: int | None,
        queue_state: str | None,
    ) -> int | None:
        if queue_id is None or queue_state not in (
            QueueState.QUEUED.value,
            QueueState.ACTIVE.value,
        ):
            return None
        row = connection.execute(
            """
            SELECT COUNT(*) FROM task_queue
            WHERE queue_id <= ? AND state IN ('queued', 'active')
            """,
            (queue_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _task_record(row: sqlite3.Row, queue_position: int | None) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            site_id=row["site_id"],
            title=row["title"],
            page_url=_read_stored_page_url(row["page_url"]),
            model_profile_id=row["model_profile_id"],
            run_id=row["run_id"],
            current_step=TaskStep(row["current_step"]),
            status=TaskStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_safe_error_category=row["last_safe_error_category"],
            last_operation=Operation(row["last_operation"]) if row["last_operation"] else None,
            interrupted_operation=(
                Operation(row["interrupted_operation"])
                if row["interrupted_operation"]
                else None
            ),
            confirmation_id=row["confirmation_id"],
            confirmation_expires_at=row["confirmation_expires_at"],
            confirmation_decision=row["confirmation_decision"],
            pending_operation=(
                Operation(row["pending_operation"]) if row["pending_operation"] else None
            ),
            interaction_retry_authorized=bool(row["interaction_retry_authorized"]),
            queue_state=QueueState(row["queue_state"]) if row["queue_state"] else None,
            queue_position=queue_position,
        )

    def _read_task(self, connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError("task was not found")
        return row

    def update_task_settings(
        self,
        task_id: str,
        *,
        title: str | object = ...,
        site_id: str | object = ...,
        page_url: str | None | object = ...,
        model_profile_id: str | None | object = ...,
    ) -> TaskRecord:
        """Update editable settings; adapter and URL are draft-only."""
        task_id = _validate_id(task_id, "task_id")
        with self._transaction() as connection:
            row = self._read_task(connection, task_id)
            status = TaskStatus(row["status"])
            queued = connection.execute(
                "SELECT 1 FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if queued:
                raise TaskTransitionError("任务已排队或正在运行，不能修改设置。")

            new_title = row["title"] if title is ... else _validate_title(title)  # type: ignore[arg-type]
            new_site_id = (
                row["site_id"]
                if site_id is ...
                else self._validate_site_id(site_id)  # type: ignore[arg-type]
            )
            context = connection.execute(
                "SELECT page_url FROM task_context WHERE task_id = ?", (task_id,)
            ).fetchone()
            old_page_url = (
                _read_stored_page_url(context["page_url"]) if context else None
            )
            new_page_url = (
                old_page_url
                if page_url is ...
                else _validate_page_url(page_url)  # type: ignore[arg-type]
            )
            new_profile_id = (
                row["model_profile_id"]
                if model_profile_id is ...
                else _validate_profile_id(model_profile_id)  # type: ignore[arg-type]
            )
            if (new_site_id != row["site_id"] or new_page_url != old_page_url) and (
                status != TaskStatus.DRAFT or row["run_id"] is not None
            ):
                raise TaskTransitionError("抓取后更换站点或 URL 需要创建新任务。")
            if (
                new_profile_id != row["model_profile_id"]
                and status
                not in {
                    TaskStatus.DRAFT,
                    TaskStatus.PACKAGE_REVIEW,
                    TaskStatus.WAITING_MODEL_AUTHORIZATION,
                }
            ):
                raise TaskTransitionError("答案生成后不能直接更换模型档案。")
            new_status = status
            new_step = TaskStep(row["current_step"])
            if (
                new_profile_id != row["model_profile_id"]
                and status == TaskStatus.WAITING_MODEL_AUTHORIZATION
            ):
                new_status = TaskStatus.PACKAGE_REVIEW
                new_step = TaskStep.PACKAGE_REVIEW
            now = _timestamp()
            connection.execute(
                """
                UPDATE tasks
                SET title = ?, site_id = ?, model_profile_id = ?,
                    status = ?, current_step = ?, updated_at = ?,
                    model_authorized_at = CASE WHEN model_profile_id IS NOT ? THEN NULL
                                               ELSE model_authorized_at END,
                    confirmation_id = CASE WHEN model_profile_id IS NOT ? THEN NULL
                                           ELSE confirmation_id END,
                    confirmation_expires_at = CASE WHEN model_profile_id IS NOT ? THEN NULL
                                                   ELSE confirmation_expires_at END,
                    confirmation_decision = CASE WHEN model_profile_id IS NOT ? THEN NULL
                                                 ELSE confirmation_decision END
                WHERE task_id = ?
                """,
                (
                    new_title,
                    new_site_id,
                    new_profile_id,
                    new_status.value,
                    new_step.value,
                    now,
                    new_profile_id,
                    new_profile_id,
                    new_profile_id,
                    new_profile_id,
                    task_id,
                ),
            )
            connection.execute(
                "UPDATE task_context SET page_url = ? WHERE task_id = ?",
                (_protect_stored_page_url(new_page_url), task_id),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="settings_updated",
                from_status=status,
                to_status=new_status,
            )
        return self.get_task(task_id)

    def request_model_authorization(self, task_id: str) -> TaskRecord:
        task_id = _validate_id(task_id, "task_id")
        with self._transaction() as connection:
            row = self._read_task(connection, task_id)
            current = TaskStatus(row["status"])
            if current != TaskStatus.PACKAGE_REVIEW:
                raise TaskTransitionError(
                    "只有完成题包检查的任务可以请求模型授权。"
                )
            if not row["run_id"]:
                raise TaskTransitionError("请求模型授权需要已关联的 run_id。")
            if not row["model_profile_id"]:
                raise TaskTransitionError("请先为任务选择模型档案。")
            connection.execute(
                """
                UPDATE tasks SET status = ?, current_step = ?, updated_at = ?,
                    last_safe_error_category = NULL, model_authorized_at = NULL
                WHERE task_id = ?
                """,
                (
                    TaskStatus.WAITING_MODEL_AUTHORIZATION.value,
                    TaskStep.MODEL_AUTHORIZATION.value,
                    _timestamp(),
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="model_authorization_requested",
                from_status=current,
                to_status=TaskStatus.WAITING_MODEL_AUTHORIZATION,
            )
        return self.get_task(task_id)

    def cancel_model_authorization(self, task_id: str) -> TaskRecord:
        """Return an unapproved model request to package review without sending it."""
        task_id = _validate_id(task_id, "task_id")
        with self._transaction() as connection:
            row = self._read_task(connection, task_id)
            current = TaskStatus(row["status"])
            if current != TaskStatus.WAITING_MODEL_AUTHORIZATION:
                raise TaskTransitionError("当前任务没有等待中的模型授权。")
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, current_step = ?, updated_at = ?,
                    model_authorized_at = NULL
                WHERE task_id = ?
                """,
                (
                    TaskStatus.PACKAGE_REVIEW.value,
                    TaskStep.PACKAGE_REVIEW.value,
                    _timestamp(),
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="model_authorization_cancelled",
                from_status=current,
                to_status=TaskStatus.PACKAGE_REVIEW,
            )
        return self.get_task(task_id)

    def approve_answer_review(self, task_id: str) -> TaskRecord:
        task_id = _validate_id(task_id, "task_id")
        with self._transaction() as connection:
            row = self._read_task(connection, task_id)
            current = TaskStatus(row["status"])
            if current != TaskStatus.ANSWER_REVIEW:
                raise TaskTransitionError(
                    "只有待复核答案的任务可以进入网页填写阶段。"
                )
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, current_step = ?, updated_at = ?,
                    confirmation_id = NULL, confirmation_expires_at = NULL,
                    confirmation_decision = NULL, interaction_retry_authorized = 0
                WHERE task_id = ?
                """,
                (
                    TaskStatus.READY_FOR_INTERACTION.value,
                    TaskStep.INTERACTION.value,
                    _timestamp(),
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="answers_approved",
                from_status=current,
                to_status=TaskStatus.READY_FOR_INTERACTION,
            )
        return self.get_task(task_id)

    def enqueue_operation(
        self,
        task_id: str,
        operation: Operation | str,
        *,
        model_authorized: bool = False,
        retry_failed: bool = False,
        overwrite_existing_result: bool = False,
    ) -> QueueItem:
        """Queue one explicit operation; SOLVE requires fresh UI authorization."""
        task_id = _validate_id(task_id, "task_id")
        operation = _enum_value(Operation, operation, "operation")
        if type(model_authorized) is not bool:
            raise ValueError("model_authorized must be a boolean")
        if type(retry_failed) is not bool:
            raise ValueError("retry_failed must be a boolean")
        if type(overwrite_existing_result) is not bool:
            raise ValueError("overwrite_existing_result must be a boolean")
        with self._transaction() as connection:
            row = self._read_task(connection, task_id)
            status = TaskStatus(row["status"])
            queue_return_status = status
            if connection.execute(
                "SELECT 1 FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone():
                raise TaskTransitionError("此任务已有等待或运行中的操作。")

            if operation == Operation.AUTHENTICATE:
                allowed = {
                    TaskStatus.DRAFT,
                    TaskStatus.PACKAGE_REVIEW,
                    TaskStatus.WAITING_MODEL_AUTHORIZATION,
                    TaskStatus.ANSWER_REVIEW,
                    TaskStatus.READY_FOR_INTERACTION,
                }
                if status in allowed:
                    queue_return_status = status
                elif (
                    retry_failed
                    and status == TaskStatus.FAILED
                    and row["last_operation"] == Operation.AUTHENTICATE.value
                ):
                    restored_status = self._status_for_step(
                        TaskStep(row["current_step"])
                    )
                    if restored_status not in allowed:
                        raise TaskTransitionError(
                            "无法恢复 Cookie 更新前的任务阶段；请新建任务。"
                        )
                    queue_return_status = restored_status
                elif (
                    retry_failed
                    and status == TaskStatus.FAILED
                    and row["last_operation"] == Operation.CRAWL.value
                    and not row["run_id"]
                ):
                    # 抓取失败时还没有题包，刷新 Cookie 后退回草稿是唯一不覆盖产物的落点。
                    # 解题/填写失败的任务带 run_id，绝不能走这条分支。
                    queue_return_status = TaskStatus.DRAFT
                else:
                    raise TaskTransitionError("当前任务阶段不能更新 Cookie。")
            elif operation == Operation.CRAWL:
                if status != TaskStatus.DRAFT and not (
                    retry_failed
                    and status == TaskStatus.FAILED
                    and row["last_operation"] == Operation.CRAWL.value
                ):
                    raise TaskTransitionError("当前任务阶段不能开始抓取。")
                context = connection.execute(
                    "SELECT page_url FROM task_context WHERE task_id = ?", (task_id,)
                ).fetchone()
                if not context or not context["page_url"]:
                    raise TaskTransitionError("请先填写作业 URL。")
            elif operation == Operation.SOLVE:
                solve_is_retry = (
                    status == TaskStatus.FAILED
                    and row["last_operation"] == Operation.SOLVE.value
                    and retry_failed
                )
                if not model_authorized:
                    raise TaskTransitionError(
                        "每次启动解题前都需要用户确认模型请求摘要。"
                    )
                if status != TaskStatus.WAITING_MODEL_AUTHORIZATION and not solve_is_retry:
                    raise TaskTransitionError(
                        "当前任务阶段不能启动解题或缺少模型授权步骤。"
                    )
                if not row["model_profile_id"]:
                    raise TaskTransitionError("任务缺少模型档案。")
                if not row["run_id"]:
                    raise TaskTransitionError("解题需要已关联的 run_id。")
                if solve_is_retry and not overwrite_existing_result:
                    raise TaskTransitionError(
                        "重试解题前必须显式确认是否覆盖已有答案。"
                    )
            elif operation == Operation.INTERACT:
                if status != TaskStatus.READY_FOR_INTERACTION:
                    raise TaskTransitionError(
                        "当前任务阶段不能开始网页交互；不确定状态须先检查页面。"
                    )
                if not row["run_id"]:
                    raise TaskTransitionError("网页交互需要已关联的 run_id。")
                retry_authorized = bool(row["interaction_retry_authorized"])
                if retry_authorized and not overwrite_existing_result:
                    raise TaskTransitionError(
                        "再次运行交互前必须显式确认覆盖已有交互结果。"
                    )
                if overwrite_existing_result and not retry_authorized:
                    raise TaskTransitionError(
                        "没有经过检查的交互重试授权，不能覆盖交互结果。"
                    )
                connection.execute(
                    """
                    UPDATE tasks SET interaction_retry_authorized = 0
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )

            if operation == Operation.SOLVE:
                connection.execute(
                    "UPDATE tasks SET model_authorized_at = ? WHERE task_id = ?",
                    (_timestamp(), task_id),
                )

            cursor = connection.execute(
                """
                INSERT INTO task_queue (
                    task_id, operation, state, enqueued_at, started_at,
                    lease_token, return_status, overwrite_result
                ) VALUES (?, ?, 'queued', ?, NULL, NULL, ?, ?)
                """,
                (
                    task_id,
                    operation.value,
                    _timestamp(),
                    queue_return_status.value,
                    1 if overwrite_existing_result else 0,
                ),
            )
            connection.execute(
                """
                UPDATE tasks SET status = ?, current_step = ?, updated_at = ?,
                    last_safe_error_category = NULL,
                    last_operation = ?, interrupted_operation = NULL,
                    confirmation_id = NULL, confirmation_expires_at = NULL,
                    confirmation_decision = NULL
                WHERE task_id = ?
                """,
                (
                    TaskStatus.QUEUED.value,
                    _OPERATION_STATUS[operation][1].value,
                    _timestamp(),
                    operation.value,
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="operation_queued",
                from_status=status,
                to_status=TaskStatus.QUEUED,
                operation=operation,
            )
            _ = int(cursor.lastrowid)
        return self.get_queue_item(task_id)

    def resume_paused_operation(
        self,
        task_id: str,
        *,
        model_authorized: bool = False,
    ) -> QueueItem:
        """Explicitly resume queued-before-start work after app restart."""
        task_id = _validate_id(task_id, "task_id")
        if type(model_authorized) is not bool:
            raise ValueError("model_authorized must be a boolean")
        with self._transaction() as connection:
            task = self._read_task(connection, task_id)
            if TaskStatus(task["status"]) != TaskStatus.PAUSED:
                raise TaskTransitionError("任务当前没有可恢复的排队操作。")
            queue = connection.execute(
                "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if queue is None or queue["state"] != QueueState.PAUSED.value:
                raise TaskTransitionError("任务的排队状态不可恢复。")
            operation = Operation(queue["operation"])
            if operation == Operation.SOLVE and not model_authorized:
                raise TaskTransitionError(
                    "恢复解题时需要重新确认模型请求摘要。"
                )
            if operation == Operation.SOLVE:
                connection.execute(
                    "UPDATE tasks SET model_authorized_at = ? WHERE task_id = ?",
                    (_timestamp(), task_id),
                )
            connection.execute(
                """
                UPDATE task_queue SET state = 'queued', enqueued_at = ?,
                    started_at = NULL, lease_token = NULL
                WHERE task_id = ?
                """,
                (_timestamp(), task_id),
            )
            connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?,
                    last_safe_error_category = NULL,
                    interrupted_operation = NULL
                WHERE task_id = ?
                """,
                (TaskStatus.QUEUED.value, _timestamp(), task_id),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="paused_operation_resumed",
                from_status=TaskStatus.PAUSED,
                to_status=TaskStatus.QUEUED,
                operation=operation,
            )
        return self.get_queue_item(task_id)

    def get_queue_item(self, task_id: str) -> QueueItem:
        task_id = _validate_id(task_id, "task_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError("task has no queued operation")
            position = self._queue_position(connection, row["queue_id"], row["state"])
            return self._queue_item(row, position)
        finally:
            connection.close()

    @staticmethod
    def _queue_item(row: sqlite3.Row, queue_position: int | None) -> QueueItem:
        return QueueItem(
            queue_id=int(row["queue_id"]),
            task_id=row["task_id"],
            operation=Operation(row["operation"]),
            state=QueueState(row["state"]),
            enqueued_at=row["enqueued_at"],
            started_at=row["started_at"],
            return_status=TaskStatus(row["return_status"]),
            overwrite_result=bool(row["overwrite_result"]),
            queue_position=queue_position,
        )

    def list_queue(self, *, include_paused: bool = True) -> list[QueueItem]:
        connection = self._connect()
        try:
            states = (
                (
                    QueueState.QUEUED.value,
                    QueueState.ACTIVE.value,
                    QueueState.PAUSED.value,
                    QueueState.BLOCKED.value,
                )
                if include_paused
                else (QueueState.QUEUED.value, QueueState.ACTIVE.value)
            )
            placeholders = ",".join("?" for _ in states)
            rows = connection.execute(
                f"""
                SELECT * FROM task_queue
                WHERE state IN ({placeholders})
                ORDER BY CASE state
                           WHEN 'active' THEN 0
                           WHEN 'queued' THEN 1
                           WHEN 'blocked' THEN 2
                           ELSE 3
                         END,
                         queue_id
                """,
                states,
            ).fetchall()
            positions: dict[int, int] = {}
            index = 0
            for row in connection.execute(
                """
                SELECT queue_id FROM task_queue
                WHERE state IN ('queued', 'active') ORDER BY queue_id
                """
            ).fetchall():
                index += 1
                positions[int(row["queue_id"])] = index
            return [
                self._queue_item(row, positions.get(int(row["queue_id"])))
                for row in rows
            ]
        finally:
            connection.close()

    def cancel_queued_operation(self, task_id: str) -> TaskRecord:
        """Cancel queued/paused work; active browser work needs worker cancellation."""
        task_id = _validate_id(task_id, "task_id")
        with self._transaction() as connection:
            task = self._read_task(connection, task_id)
            queue = connection.execute(
                "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if queue is None or queue["state"] not in (
                QueueState.QUEUED.value,
                QueueState.PAUSED.value,
            ):
                raise TaskTransitionError("只有等待中的操作可以在此取消。")
            operation = Operation(queue["operation"])
            previous = TaskStatus(task["status"])
            return_status = TaskStatus(queue["return_status"])
            was_interrupted = task["interrupted_operation"] == operation.value
            target_status = TaskStatus.FAILED if was_interrupted else return_status
            safe_error_category = "interrupted_operation" if was_interrupted else None
            target_step = self._step_for_status(return_status)
            if was_interrupted and operation != Operation.AUTHENTICATE:
                target_step = _OPERATION_STATUS[operation][1]
            connection.execute("DELETE FROM task_queue WHERE task_id = ?", (task_id,))
            connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?, current_step = ?,
                    last_safe_error_category = ?, interrupted_operation = NULL
                WHERE task_id = ?
                """,
                (
                    target_status.value,
                    _timestamp(),
                    target_step.value,
                    safe_error_category,
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type=(
                    "interrupted_operation_abandoned"
                    if was_interrupted
                    else "queued_operation_cancelled"
                ),
                from_status=previous,
                to_status=target_status,
                operation=operation,
                safe_error_category=safe_error_category,
            )
        return self.get_task(task_id)

    def request_cancel_active_operation(self, task_id: str) -> TaskRecord:
        """Persist a cooperative stop request for the active worker operation.

        The worker must observe its own cancellation signal, stop at a safe
        point, report the appropriate outcome with ``finish_operation``, and
        release the queue lease before the UI closes the task view.
        """
        task_id = _validate_id(task_id, "task_id")
        active_statuses = {
            TaskStatus.AUTHENTICATING,
            TaskStatus.CRAWLING,
            TaskStatus.SOLVING,
            TaskStatus.FILLING,
            TaskStatus.AWAITING_SUBMIT_CONFIRMATION,
            TaskStatus.SUBMITTING,
            TaskStatus.CANCELLING,
        }
        with self._transaction() as connection:
            task = self._read_task(connection, task_id)
            queue = connection.execute(
                "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if queue is None or queue["state"] != QueueState.ACTIVE.value:
                raise TaskTransitionError("只有活动中的任务可以请求停止。")
            current = TaskStatus(task["status"])
            if current not in active_statuses:
                raise TaskTransitionError("任务当前状态不支持停止活动操作。")
            if current == TaskStatus.CANCELLING:
                return self._detail_in_transaction(connection, task_id)

            connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?,
                    confirmation_id = NULL, confirmation_expires_at = NULL,
                    confirmation_decision = ?
                WHERE task_id = ?
                """,
                (
                    TaskStatus.CANCELLING.value,
                    _timestamp(),
                    "cancel"
                    if current == TaskStatus.AWAITING_SUBMIT_CONFIRMATION
                    else task["confirmation_decision"],
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="active_operation_cancel_requested",
                from_status=current,
                to_status=TaskStatus.CANCELLING,
                operation=Operation(queue["operation"]),
            )
        return self.get_task(task_id)

    @staticmethod
    def _step_for_status(status: TaskStatus) -> TaskStep:
        mapping = {
            TaskStatus.DRAFT: TaskStep.SETUP,
            TaskStatus.QUEUED: TaskStep.SETUP,
            TaskStatus.AUTHENTICATING: TaskStep.AUTHENTICATION,
            TaskStatus.CRAWLING: TaskStep.CRAWL,
            TaskStatus.PACKAGE_REVIEW: TaskStep.PACKAGE_REVIEW,
            TaskStatus.WAITING_MODEL_AUTHORIZATION: TaskStep.MODEL_AUTHORIZATION,
            TaskStatus.SOLVING: TaskStep.SOLVE,
            TaskStatus.ANSWER_REVIEW: TaskStep.ANSWER_REVIEW,
            TaskStatus.READY_FOR_INTERACTION: TaskStep.INTERACTION,
            TaskStatus.FILLING: TaskStep.INTERACTION,
            TaskStatus.AWAITING_SUBMIT_CONFIRMATION: TaskStep.SUBMIT_CONFIRMATION,
            TaskStatus.SUBMITTING: TaskStep.SUBMIT_CONFIRMATION,
            TaskStatus.CANCELLING: TaskStep.SUBMIT_CONFIRMATION,
            TaskStatus.FILLED_UNSUBMITTED: TaskStep.RESULT,
            TaskStatus.CONFIRMED: TaskStep.RESULT,
            TaskStatus.FAILED: TaskStep.RESULT,
            TaskStatus.UNCONFIRMED: TaskStep.RESULT,
            TaskStatus.CANCELLED: TaskStep.RESULT,
            TaskStatus.PAUSED: TaskStep.SETUP,
            TaskStatus.NEEDS_INSPECTION: TaskStep.RESULT,
            TaskStatus.ARCHIVED: TaskStep.RESULT,
        }
        return mapping[status]

    @staticmethod
    def _status_for_step(step: TaskStep) -> TaskStatus | None:
        """Recover the pre-authentication task status after an auth failure."""
        mapping = {
            TaskStep.SETUP: TaskStatus.DRAFT,
            TaskStep.PACKAGE_REVIEW: TaskStatus.PACKAGE_REVIEW,
            TaskStep.MODEL_AUTHORIZATION: TaskStatus.WAITING_MODEL_AUTHORIZATION,
            TaskStep.ANSWER_REVIEW: TaskStatus.ANSWER_REVIEW,
            TaskStep.INTERACTION: TaskStatus.READY_FOR_INTERACTION,
        }
        return mapping.get(step)

    def claim_next(self) -> ClaimedOperation | None:
        """Atomically claim the oldest explicitly queued operation."""
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM task_queue WHERE state = 'active'"
            ).fetchone():
                return None
            row = connection.execute(
                """
                SELECT q.*, t.status AS task_status
                FROM task_queue q
                JOIN tasks t ON t.task_id = q.task_id
                WHERE q.state = 'queued'
                ORDER BY q.queue_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            operation = Operation(row["operation"])
            status, step = _OPERATION_STATUS[operation]
            lease_token = str(uuid.uuid4())
            old_status = TaskStatus(row["task_status"])
            connection.execute(
                """
                UPDATE task_queue SET state = 'active', started_at = ?,
                    lease_token = ?
                WHERE queue_id = ? AND state = 'queued'
                """,
                (_timestamp(), lease_token, row["queue_id"]),
            )
            connection.execute(
                """
                UPDATE tasks SET status = ?, current_step = ?, updated_at = ?,
                    last_operation = ?, last_safe_error_category = NULL
                WHERE task_id = ?
                """,
                (status.value, step.value, _timestamp(), operation.value, row["task_id"]),
            )
            self._record_event(
                connection,
                task_id=row["task_id"],
                event_type="operation_started",
                from_status=old_status,
                to_status=status,
                operation=operation,
            )
            claimed = connection.execute(
                "SELECT * FROM task_queue WHERE queue_id = ?", (row["queue_id"],)
            ).fetchone()
            item = self._queue_item(
                claimed,
                self._queue_position(connection, claimed["queue_id"], claimed["state"]),
            )
            return ClaimedOperation(
                queue_id=item.queue_id,
                task_id=item.task_id,
                operation=item.operation,
                state=item.state,
                enqueued_at=item.enqueued_at,
                started_at=item.started_at,
                return_status=item.return_status,
                overwrite_result=item.overwrite_result,
                queue_position=item.queue_position,
                lease_token=claimed["lease_token"],
            )

    def _require_active_operation(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        lease_token: str,
        operation: Operation,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        task = self._read_task(connection, task_id)
        queue = connection.execute(
            "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
        ).fetchone()
        if (
            queue is None
            or queue["state"] != QueueState.ACTIVE.value
            or queue["operation"] != operation.value
            or not isinstance(lease_token, str)
            or queue["lease_token"] != lease_token
        ):
            raise TaskTransitionError("操作租约已失效或不属于当前任务。")
        return task, queue

    def await_submit_confirmation(
        self,
        task_id: str,
        lease_token: str,
        *,
        expires_in_seconds: int = 600,
    ) -> SubmitConfirmation:
        """Persist the UI confirmation checkpoint while keeping interaction active."""
        task_id = _validate_id(task_id, "task_id")
        if isinstance(expires_in_seconds, bool) or not isinstance(
            expires_in_seconds, int
        ):
            raise ValueError("expires_in_seconds must be an integer")
        if not 30 <= expires_in_seconds <= 3600:
            raise ValueError("confirmation timeout must be between 30 and 3600 seconds")
        confirmation_id = str(uuid.uuid4())
        expires_at = _timestamp(_now() + timedelta(seconds=expires_in_seconds))
        with self._transaction() as connection:
            task, _queue = self._require_active_operation(
                connection, task_id, lease_token, Operation.INTERACT
            )
            current = TaskStatus(task["status"])
            if current != TaskStatus.FILLING:
                raise TaskTransitionError(
                    "提交确认只能在页面预检、填写和回显验证完成后等待。"
                )
            if not task["run_id"]:
                raise TaskTransitionError("提交确认需要已关联的 run_id。")
            connection.execute(
                """
                UPDATE tasks SET status = ?, current_step = ?, updated_at = ?,
                    confirmation_id = ?, confirmation_expires_at = ?,
                    confirmation_decision = NULL
                WHERE task_id = ?
                """,
                (
                    TaskStatus.AWAITING_SUBMIT_CONFIRMATION.value,
                    TaskStep.SUBMIT_CONFIRMATION.value,
                    _timestamp(),
                    confirmation_id,
                    expires_at,
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="submit_confirmation_requested",
                from_status=current,
                to_status=TaskStatus.AWAITING_SUBMIT_CONFIRMATION,
                operation=Operation.INTERACT,
            )
            site_id = task["site_id"]
            run_id = task["run_id"]
        return SubmitConfirmation(
            task_id=task_id,
            run_id=run_id,
            site_id=site_id,
            confirmation_id=confirmation_id,
            expires_at=expires_at,
        )

    def respond_to_submit_confirmation(
        self,
        task_id: str,
        run_id: str,
        confirmation_id: str,
        decision: str,
    ) -> str:
        """Consume the current one-shot confirm/cancel decision.

        Expired requests are converted to cancel. The return value is the
        decision the worker must observe; callers must not click the website
        submit control themselves.
        """
        task_id = _validate_id(task_id, "task_id")
        run_id = _validate_run_id(run_id)
        confirmation_id = _validate_id(confirmation_id, "confirmation_id")
        if not isinstance(decision, str) or decision not in {"confirm", "cancel"}:
            raise ValueError("decision must be confirm or cancel")
        with self._transaction() as connection:
            task = self._read_task(connection, task_id)
            queue = connection.execute(
                "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if (
                queue is None
                or queue["state"] != QueueState.ACTIVE.value
                or queue["operation"] != Operation.INTERACT.value
                or TaskStatus(task["status"])
                != TaskStatus.AWAITING_SUBMIT_CONFIRMATION
                or task["run_id"] != run_id
                or task["confirmation_id"] != confirmation_id
                or task["confirmation_decision"] is not None
            ):
                raise TaskTransitionError(
                    "提交确认已过期、已处理或不属于当前任务。"
                )
            expired = _parse_timestamp(task["confirmation_expires_at"]) <= _now()
            accepted_decision = "cancel" if expired else decision
            new_status = (
                TaskStatus.SUBMITTING
                if accepted_decision == "confirm"
                else TaskStatus.CANCELLING
            )
            connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?,
                    confirmation_decision = ?, confirmation_id = NULL,
                    confirmation_expires_at = NULL
                WHERE task_id = ?
                """,
                (
                    new_status.value,
                    _timestamp(),
                    accepted_decision,
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type=(
                    "submit_confirmation_expired"
                    if expired
                    else f"submit_{accepted_decision}_accepted"
                ),
                from_status=TaskStatus.AWAITING_SUBMIT_CONFIRMATION,
                to_status=new_status,
                operation=Operation.INTERACT,
            )
            return accepted_decision

    def finish_operation(
        self,
        task_id: str,
        lease_token: str,
        *,
        outcome: str,
        run_id: str | None = None,
        safe_error_category: str | None = None,
    ) -> TaskRecord:
        """Persist a worker result and release its queue slot.

        Interaction confirmed must only be reported after the adapter sees
        website success evidence. A user confirmation alone is insufficient.
        """
        task_id = _validate_id(task_id, "task_id")
        safe_error_category = self._validate_error_category(safe_error_category)
        if not isinstance(outcome, str):
            raise ValueError("outcome must be a string")
        with self._transaction() as connection:
            task = self._read_task(connection, task_id)
            queue = connection.execute(
                "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if queue is None or queue["state"] != QueueState.ACTIVE.value:
                raise TaskTransitionError("任务当前没有活动操作。")
            operation = Operation(queue["operation"])
            task, queue = self._require_active_operation(
                connection, task_id, lease_token, operation
            )
            previous = TaskStatus(task["status"])
            if operation in {
                Operation.AUTHENTICATE,
                Operation.CRAWL,
                Operation.SOLVE,
            } and outcome not in {"completed", "failed", "cancelled"}:
                raise ValueError(
                    "non-interaction outcome must be completed, failed, or cancelled"
                )
            if operation in {Operation.SOLVE, Operation.INTERACT}:
                if not run_id:
                    raise ValueError("solve and interaction results must provide run_id")
                run_id = _validate_run_id(run_id)
                if run_id != task["run_id"]:
                    raise TaskTransitionError("操作结果的 run_id 与任务关联不匹配。")
            new_run_id = task["run_id"]
            if outcome == "cancelled":
                if previous != TaskStatus.CANCELLING:
                    raise TaskTransitionError(
                        "只有已请求停止的活动操作可以记录 cancelled。"
                    )
                if operation == Operation.INTERACT:
                    raise ValueError(
                        "交互停止必须按页面证据记录具体结果状态。"
                    )
                new_status = TaskStatus.CANCELLED
                new_step = TaskStep.RESULT
            elif operation == Operation.AUTHENTICATE:
                if outcome == "completed":
                    new_status = TaskStatus(queue["return_status"])
                    new_step = self._step_for_status(new_status)
                else:
                    new_status = TaskStatus.FAILED
                    new_step = self._step_for_status(
                        TaskStatus(queue["return_status"])
                    )
            elif operation == Operation.CRAWL:
                if outcome == "completed":
                    if not run_id:
                        raise ValueError("successful crawl must provide run_id")
                    run_id = _validate_run_id(run_id)
                    if new_run_id and new_run_id != run_id:
                        raise TaskTransitionError(
                            "此任务已关联其他 run_id；请新建任务。"
                        )
                    duplicate = connection.execute(
                        "SELECT task_id FROM tasks WHERE run_id = ? AND task_id != ?",
                        (run_id, task_id),
                    ).fetchone()
                    if duplicate:
                        raise TaskTransitionError("run_id 已关联到其他任务。")
                    new_run_id = run_id
                    new_status = TaskStatus.PACKAGE_REVIEW
                    new_step = TaskStep.PACKAGE_REVIEW
                else:
                    new_status = TaskStatus.FAILED
                    new_step = TaskStep.CRAWL
            elif operation == Operation.SOLVE:
                if outcome == "completed":
                    new_status = TaskStatus.ANSWER_REVIEW
                    new_step = TaskStep.ANSWER_REVIEW
                else:
                    new_status = TaskStatus.FAILED
                    new_step = TaskStep.SOLVE
            else:
                if outcome not in _INTERACTION_RESULTS:
                    raise ValueError(
                        "interaction outcome must be confirmed, failed, unconfirmed, "
                        "skipped, or filled_unsubmitted"
                    )
                if outcome == "confirmed" and previous not in {
                    TaskStatus.SUBMITTING,
                    TaskStatus.CANCELLING,
                }:
                    raise TaskTransitionError(
                        "网站成功结果必须来自提交后的页面证据。"
                    )
                if outcome == "skipped" and previous not in {
                    TaskStatus.CANCELLING,
                    TaskStatus.FILLING,
                    TaskStatus.AWAITING_SUBMIT_CONFIRMATION,
                }:
                    raise TaskTransitionError("当前交互状态不能记录 skipped。")
                if outcome == "filled_unsubmitted" and previous not in {
                    TaskStatus.FILLING,
                    TaskStatus.AWAITING_SUBMIT_CONFIRMATION,
                    TaskStatus.CANCELLING,
                }:
                    raise TaskTransitionError(
                        "filled_unsubmitted 只适用于未提交的填写流程。"
                    )
                if outcome == "unconfirmed" and previous not in {
                    TaskStatus.FILLING,
                    TaskStatus.AWAITING_SUBMIT_CONFIRMATION,
                    TaskStatus.SUBMITTING,
                    TaskStatus.CANCELLING,
                }:
                    raise TaskTransitionError(
                        "unconfirmed 只适用于可能产生网页副作用的交互阶段。"
                    )
                new_status = _INTERACTION_RESULTS[outcome]
                new_step = (
                    TaskStep.RESULT
                    if new_status
                    in {
                        TaskStatus.CONFIRMED,
                        TaskStatus.FAILED,
                        TaskStatus.UNCONFIRMED,
                        TaskStatus.FILLED_UNSUBMITTED,
                    }
                    else TaskStep.INTERACTION
                )

            if new_status in {TaskStatus.FAILED, TaskStatus.UNCONFIRMED}:
                if safe_error_category is None:
                    safe_error_category = (
                        "operation_failed"
                        if new_status == TaskStatus.FAILED
                        else "result_unconfirmed"
                    )
            else:
                safe_error_category = None
            connection.execute(
                """
                UPDATE tasks
                SET run_id = ?, status = ?, current_step = ?, updated_at = ?,
                    last_safe_error_category = ?, last_operation = ?,
                    interrupted_operation = NULL, confirmation_id = NULL,
                    confirmation_expires_at = NULL, confirmation_decision = NULL,
                    interaction_retry_authorized = 0,
                    model_authorized_at = CASE WHEN ? = 'solve' THEN NULL
                                               ELSE model_authorized_at END
                WHERE task_id = ?
                """,
                (
                    new_run_id,
                    new_status.value,
                    new_step.value,
                    _timestamp(),
                    safe_error_category,
                    operation.value,
                    operation.value,
                    task_id,
                ),
            )
            connection.execute("DELETE FROM task_queue WHERE task_id = ?", (task_id,))
            self._record_event(
                connection,
                task_id=task_id,
                event_type=f"operation_{outcome}",
                from_status=previous,
                to_status=new_status,
                operation=operation,
                safe_error_category=safe_error_category,
            )
        return self.get_task(task_id)

    def authorize_interaction_retry(
        self,
        task_id: str,
        *,
        page_inspected: bool,
    ) -> TaskRecord:
        """Allow deliberate interaction retry only after page inspection.

        This method never starts work. The following enqueue call must also set
        overwrite_existing_result=True.
        """
        task_id = _validate_id(task_id, "task_id")
        if page_inspected is not True:
            raise TaskTransitionError("恢复不确定交互前必须先检查网站页面。")
        with self._transaction() as connection:
            task = self._read_task(connection, task_id)
            current = TaskStatus(task["status"])
            last_operation = task["last_operation"]
            interrupted_operation = task["interrupted_operation"]
            if current not in {
                TaskStatus.NEEDS_INSPECTION,
                TaskStatus.UNCONFIRMED,
                TaskStatus.FILLED_UNSUBMITTED,
                TaskStatus.FAILED,
                TaskStatus.READY_FOR_INTERACTION,
            }:
                raise TaskTransitionError("此任务当前不需要交互恢复授权。")
            if (
                last_operation != Operation.INTERACT.value
                and interrupted_operation != Operation.INTERACT.value
            ):
                raise TaskTransitionError("此任务没有可恢复的交互操作。")
            if not task["run_id"]:
                raise TaskTransitionError("交互恢复需要已关联的 run_id。")
            connection.execute(
                "DELETE FROM task_queue WHERE task_id = ? AND state = 'blocked'",
                (task_id,),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, current_step = ?, updated_at = ?,
                    interaction_retry_authorized = 1,
                    interrupted_operation = NULL,
                    last_safe_error_category = NULL,
                    confirmation_id = NULL, confirmation_expires_at = NULL,
                    confirmation_decision = NULL
                WHERE task_id = ?
                """,
                (
                    TaskStatus.READY_FOR_INTERACTION.value,
                    TaskStep.INTERACTION.value,
                    _timestamp(),
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="interaction_retry_authorized_after_inspection",
                from_status=current,
                to_status=TaskStatus.READY_FOR_INTERACTION,
                operation=Operation.INTERACT,
            )
        return self.get_task(task_id)

    def archive_task(self, task_id: str) -> TaskRecord:
        task_id = _validate_id(task_id, "task_id")
        with self._transaction() as connection:
            task = self._read_task(connection, task_id)
            current = TaskStatus(task["status"])
            if connection.execute(
                "SELECT 1 FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone():
                raise TaskTransitionError("有等待或运行中操作的任务不能归档。")
            if current == TaskStatus.ARCHIVED:
                return self._detail_in_transaction(connection, task_id)
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, current_step = ?, updated_at = ?,
                    confirmation_id = NULL, confirmation_expires_at = NULL,
                    confirmation_decision = NULL, interaction_retry_authorized = 0
                WHERE task_id = ?
                """,
                (
                    TaskStatus.ARCHIVED.value,
                    TaskStep.RESULT.value,
                    _timestamp(),
                    task_id,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="task_archived",
                from_status=current,
                to_status=TaskStatus.ARCHIVED,
            )
        return self.get_task(task_id)

    def _detail_in_transaction(
        self, connection: sqlite3.Connection, task_id: str
    ) -> TaskRecord:
        row = connection.execute(
            """
            SELECT t.*, c.page_url, q.operation AS pending_operation,
                   q.state AS queue_state, q.queue_id
            FROM tasks t
            LEFT JOIN task_context c ON c.task_id = t.task_id
            LEFT JOIN task_queue q ON q.task_id = t.task_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise KeyError("task was not found")
        return self._task_record(
            row, self._queue_position(connection, row["queue_id"], row["queue_state"])
        )

    def delete_task_record(self, task_id: str) -> None:
        """Delete a task that never produced a run package, whatever its status.

        没有题包的任务（登录验证失败、抓取失败、空草稿等）只有数据库记录，
        ``task_context`` / ``task_queue`` / ``task_events`` 由外键级联删除。
        已经生成题包的任务必须走 ``delete_task_and_package``，避免只删记录、
        把 runs 目录里的文件变成孤儿。
        """
        task_id = _validate_id(task_id, "task_id")
        with self._transaction() as connection:
            task = self._read_task(connection, task_id)
            if task["run_id"] is not None:
                raise TaskTransitionError(
                    "此任务已生成题包，请按题包删除。"
                )
            if connection.execute(
                "SELECT 1 FROM task_queue WHERE task_id = ?", (task_id,)
            ).fetchone():
                raise TaskTransitionError("等待、运行或恢复检查中的任务不能删除。")
            connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

    def delete_task_and_package(self, task_id: str) -> None:
        """Permanently remove an idle task record and its linked run package.

        The package is first renamed to a private staging name inside the
        configured runs directory. If the database delete fails, the package
        is restored to its original name. Symlinked run directories and any
        task with a queued/active operation are rejected.
        """
        task_id = _validate_id(task_id, "task_id")
        original_package: Path | None = None
        staged_package: Path | None = None
        try:
            with self._transaction() as connection:
                task = self._read_task(connection, task_id)
                if connection.execute(
                    "SELECT 1 FROM task_queue WHERE task_id = ?", (task_id,)
                ).fetchone():
                    raise TaskTransitionError(
                        "等待、运行或恢复检查中的任务不能永久删除。"
                    )
                run_id = task["run_id"]
                if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
                    raise TaskTransitionError("此任务没有可删除的关联题包。")

                unresolved = self.runs_directory / run_id
                is_junction = getattr(unresolved, "is_junction", lambda: False)
                if unresolved.is_symlink() or is_junction():
                    raise TaskStoreError("题包路径是符号链接，已拒绝删除。")
                if unresolved.exists():
                    original_package = self._run_directory(run_id)
                    if original_package is None:
                        raise TaskStoreError("题包目录不在允许的运行数据目录中。")
                    staged_package = self.runs_directory / (
                        f".autolearn-delete-{uuid.uuid4().hex}"
                    )
                    original_package.rename(staged_package)
                connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        except Exception:
            if staged_package is not None and staged_package.exists():
                if original_package is None:
                    raise TaskStoreError(
                        "删除未完成，题包原始路径无法确认；请检查本地运行数据。"
                    )
                try:
                    staged_package.rename(original_package)
                except OSError as restore_error:
                    raise TaskStoreError(
                        "删除未完成，题包暂存目录也未能恢复；请检查本地运行数据。"
                    ) from restore_error
            raise

        if staged_package is not None:
            try:
                shutil.rmtree(staged_package)
            except OSError as error:
                raise TaskStoreError(
                    "任务记录已删除，但题包文件清理未完成；请检查本地运行数据。"
                ) from error

    def has_completed_authentication(self, task_id: str) -> bool:
        """Whether this task has completed a cookie-capture operation."""
        task_id = _validate_id(task_id, "task_id")
        connection = self._connect()
        try:
            if connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone() is None:
                raise KeyError("task was not found")
            row = connection.execute(
                """
                SELECT 1 FROM task_events
                WHERE task_id = ?
                  AND event_type = 'operation_completed'
                  AND operation = 'authenticate'
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def list_events(self, task_id: str, *, limit: int = 100) -> list[TaskEvent]:
        task_id = _validate_id(task_id, "task_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        connection = self._connect()
        try:
            if connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone() is None:
                raise KeyError("task was not found")
            rows = connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ?
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
            return [
                TaskEvent(
                    event_id=int(row["event_id"]),
                    task_id=row["task_id"],
                    occurred_at=row["occurred_at"],
                    event_type=row["event_type"],
                    from_status=(
                        TaskStatus(row["from_status"]) if row["from_status"] else None
                    ),
                    to_status=TaskStatus(row["to_status"]) if row["to_status"] else None,
                    operation=Operation(row["operation"]) if row["operation"] else None,
                    safe_error_category=row["safe_error_category"],
                )
                for row in reversed(rows)
            ]
        finally:
            connection.close()


__all__ = [
    "TaskStore",
    "TaskStoreBusy",
    "TaskStoreError",
    "TaskTransitionError",
    "default_data_directory",
]
