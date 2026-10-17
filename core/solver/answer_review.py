"""Local question preview and safe answer-package editing API.

This module deliberately uses the existing run-directory JSON files as the
source of truth. It never stores answer text in TaskStore and never invokes the
model or a network client.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.solver import runner
from core.solver.runner import SolverError
from core.tasks.models import TaskStatus


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AnswerReviewError(RuntimeError):
    """A safe, non-content-bearing answer review failure."""

    _MESSAGES = {
        "task_unavailable": "无法读取当前任务。",
        "invalid_state": "当前任务阶段不允许查看或修改答案。",
        "package_invalid": "本地题包结构或引用校验失败。",
        "package_missing": "本地题包暂不可用。",
        "answer_missing": "答案文件不存在；请先完成解题或恢复有效答案。",
        "answer_schema": "答案格式不符合当前适配器要求。",
        "answer_reference": "答案中的题目、选项或空位引用无效。",
        "asset_invalid": "题包附件缺失或路径无效。",
        "adapter_unavailable": "当前适配器的校验规则不可用。",
        "answer_changed": "答案文件已在其他位置更新；请重新载入后再保存。",
        "answer_read": "无法安全读取本地答案文件。",
        "answer_write": "答案未能保存；原有答案文件保持不变。",
    }

    def __init__(self, category: str) -> None:
        self.category = category if category in self._MESSAGES else "package_invalid"
        super().__init__(self._MESSAGES[self.category])

    @property
    def safe_message(self) -> str:
        return self._MESSAGES[self.category]


@dataclass(frozen=True)
class AnswerReviewDocument:
    """Validated task package data for the local review UI."""

    task_id: str
    site_id: str
    run_id: str
    package_directory: Path = field(repr=False)
    questions: list[dict[str, Any]] = field(repr=False)
    answers: list[dict[str, Any]] | None = field(repr=False)
    answers_revision: str | None = field(repr=False)


def _read_json_bytes(
    path: Path,
    *,
    missing_category: str = "package_missing",
    read_category: str = "answer_read",
    invalid_category: str = "package_invalid",
) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise AnswerReviewError(missing_category) from None
    except OSError:
        raise AnswerReviewError(read_category) from None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AnswerReviewError(invalid_category) from None
    if not isinstance(value, dict):
        raise AnswerReviewError(invalid_category)
    return value, raw


def _task_and_package_directory(task_store, task_id: str):
    try:
        task = task_store.get_task(task_id)
    except Exception:
        raise AnswerReviewError("task_unavailable") from None

    editable_state = task.status in {
        TaskStatus.PACKAGE_REVIEW,
        TaskStatus.ANSWER_REVIEW,
    } or (
        task.status == TaskStatus.READY_FOR_INTERACTION
        and task.interaction_retry_authorized
    )
    if not editable_state or task.queue_state is not None:
        raise AnswerReviewError("invalid_state")
    if not task.run_id or not _RUN_ID_PATTERN.fullmatch(task.run_id):
        raise AnswerReviewError("package_missing")

    try:
        runs_root = Path(task_store.runs_directory).resolve()
        unresolved = runs_root / task.run_id
        is_junction = getattr(unresolved, "is_junction", lambda: False)
        if unresolved.is_symlink() or is_junction():
            raise AnswerReviewError("package_missing")
        package_directory = unresolved.resolve()
    except (OSError, RuntimeError, ValueError):
        raise AnswerReviewError("package_missing") from None
    if package_directory.parent != runs_root or not package_directory.is_dir():
        raise AnswerReviewError("package_missing")
    return task, package_directory


def _load_document(task_store, task_id: str) -> AnswerReviewDocument:
    task, package_directory = _task_and_package_directory(task_store, task_id)
    questions_path = package_directory / "questions.json"
    answers_path = package_directory / "answers.json"
    if questions_path.is_symlink() or getattr(
        questions_path, "is_junction", lambda: False
    )():
        raise AnswerReviewError("package_missing")
    try:
        resolved_questions_path = questions_path.resolve()
    except (OSError, RuntimeError):
        raise AnswerReviewError("package_missing") from None
    if resolved_questions_path.parent != package_directory:
        raise AnswerReviewError("package_missing")
    questions_package, _ = _read_json_bytes(
        resolved_questions_path,
        read_category="package_missing",
    )

    if (
        questions_package.get("site_id") != task.site_id
        or questions_package.get("run_id") != task.run_id
    ):
        raise AnswerReviewError("package_invalid")

    try:
        (
            _adapter_directory,
            questions_schema,
            answers_schema,
            _adapter_readme,
            cross_validator,
            question_validator,
        ) = runner._read_adapter_files(task.site_id)
        questions = runner._validate_question_package(
            questions_package, questions_schema
        )
        question_issues = question_validator(questions)
        if question_issues:
            raise AnswerReviewError("package_invalid")
        runner._validate_all_assets(package_directory, questions)
    except AnswerReviewError:
        raise
    except SolverError as error:
        category = {
            "asset_missing": "asset_invalid",
            "asset_path": "asset_invalid",
            "unknown_adapter": "adapter_unavailable",
            "adapter_solver_missing": "adapter_unavailable",
            "file_missing": "package_missing",
            "file_read": "package_missing",
        }.get(error.category, "package_invalid")
        raise AnswerReviewError(category) from None
    except (OSError, ValueError, TypeError, KeyError, ImportError):
        raise AnswerReviewError("package_invalid") from None

    if answers_path.is_symlink() or getattr(
        answers_path, "is_junction", lambda: False
    )():
        raise AnswerReviewError("answer_read")
    try:
        resolved_answers_path = answers_path.resolve()
    except (OSError, RuntimeError):
        raise AnswerReviewError("answer_read") from None
    if resolved_answers_path.parent != package_directory:
        raise AnswerReviewError("answer_read")
    if not resolved_answers_path.is_file():
        return AnswerReviewDocument(
            task_id=task.task_id,
            site_id=task.site_id,
            run_id=task.run_id,
            package_directory=package_directory,
            questions=questions,
            answers=None,
            answers_revision=None,
        )

    answers_package, answer_bytes = _read_json_bytes(
        resolved_answers_path,
        missing_category="answer_missing",
        invalid_category="answer_schema",
    )
    if (
        answers_package.get("site_id") != task.site_id
        or answers_package.get("run_id") != task.run_id
    ):
        raise AnswerReviewError("answer_reference")
    answers = answers_package.get("answers")
    if not isinstance(answers, list):
        raise AnswerReviewError("answer_schema")
    try:
        runner._validate_answer_batch(
            questions,
            answers,
            questions_package,
            answers_schema,
            cross_validator,
        )
    except SolverError as error:
        category = (
            "answer_reference"
            if error.category == "answer_reference"
            else "answer_schema"
        )
        raise AnswerReviewError(category) from None
    except (ValueError, TypeError, KeyError):
        raise AnswerReviewError("answer_schema") from None

    return AnswerReviewDocument(
        task_id=task.task_id,
        site_id=task.site_id,
        run_id=task.run_id,
        package_directory=package_directory,
        questions=questions,
        answers=answers,
        answers_revision=hashlib.sha256(answer_bytes).hexdigest(),
    )


def load_task_answer_review(task_store, task_id: str) -> AnswerReviewDocument:
    """Load validated questions, local assets, and answers for an editable task."""
    return _load_document(task_store, task_id)


def resolve_local_asset(package_directory: Path, asset_path: str) -> Path:
    """Resolve a package-relative image without allowing external/file traversal."""
    try:
        return runner._resolve_asset(package_directory, asset_path)
    except SolverError as error:
        category = "asset_invalid" if error.category in {"asset_path", "asset_missing"} else "package_invalid"
        raise AnswerReviewError(category) from None


def save_task_answers(
    task_store,
    task_id: str,
    answers: list[dict[str, Any]],
    *,
    expected_revision: str | None,
) -> AnswerReviewDocument:
    """Validate structured edits and atomically replace only answers.json."""
    task, _package_directory = _task_and_package_directory(task_store, task_id)
    can_edit = task.status == TaskStatus.ANSWER_REVIEW or (
        task.status == TaskStatus.READY_FOR_INTERACTION
        and task.interaction_retry_authorized
    )
    if not can_edit:
        raise AnswerReviewError("invalid_state")
    current = _load_document(task_store, task_id)
    if current.answers_revision != expected_revision:
        raise AnswerReviewError("answer_changed")

    questions_package, _ = _read_json_bytes(
        current.package_directory / "questions.json"
    )
    try:
        (
            _adapter_directory,
            _questions_schema,
            answers_schema,
            _adapter_readme,
            cross_validator,
            _question_validator,
        ) = runner._read_adapter_files(current.site_id)
        runner._validate_answer_batch(
            current.questions,
            answers,
            questions_package,
            answers_schema,
            cross_validator,
        )
    except SolverError as error:
        category = (
            "answer_reference"
            if error.category == "answer_reference"
            else "answer_schema"
        )
        raise AnswerReviewError(category) from None
    except (OSError, ValueError, TypeError, KeyError, ImportError):
        raise AnswerReviewError("adapter_unavailable") from None

    answer_package = {
        "site_id": questions_package["site_id"],
        "adapter_version": questions_package["adapter_version"],
        "schema_version": questions_package["schema_version"],
        "run_id": questions_package["run_id"],
        "answers": answers,
    }
    try:
        payload = (json.dumps(answer_package, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError, UnicodeError):
        raise AnswerReviewError("answer_schema") from None

    output_path = current.package_directory / "answers.json"
    if output_path.is_symlink() or getattr(
        output_path, "is_junction", lambda: False
    )():
        raise AnswerReviewError("answer_write")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=current.package_directory,
            prefix=".answers.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output_path)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise AnswerReviewError("answer_write") from None

    return AnswerReviewDocument(
        task_id=current.task_id,
        site_id=current.site_id,
        run_id=current.run_id,
        package_directory=current.package_directory,
        questions=current.questions,
        answers=answers,
        answers_revision=hashlib.sha256(payload).hexdigest(),
    )
