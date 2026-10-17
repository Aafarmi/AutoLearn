"""Validate a run package, dispatch its site adapter, and save the outcome."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import threading
import tempfile
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

from core.common.validation import SchemaDefinitionError, validate_json_schema
from core.data_migration import DataMigrationError, ensure_user_data_migrated
from core.paths import data_path, resource_path


ADAPTERS_ROOT = resource_path("core", "adapters")
LOG_PATH = data_path("logs", "interaction.log")
_SITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class InteractionRunnerError(RuntimeError):
    """An expected, user-actionable interaction-runner error."""

    def __init__(self, message: str, category: str = "interaction") -> None:
        super().__init__(message)
        self.category = category


def _check_cancel(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _report_progress(
    callback: Callable[..., None] | None,
    state: str,
    *,
    question_count: int | None = None,
    asset_count: int | None = None,
) -> None:
    if callback is None:
        return
    try:
        callback(state, question_count=question_count, asset_count=asset_count)
    except Exception:
        pass


def _get_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("autolearn.interaction")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    expected_path = str(LOG_PATH.resolve())
    if not any(
        isinstance(handler, RotatingFileHandler)
        and str(Path(handler.baseFilename).resolve()) == expected_path
        for handler in logger.handlers
    ):
        handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        raise InteractionRunnerError(f"{label} was not found: {path.name}", "file_missing") from None
    except json.JSONDecodeError as error:
        raise InteractionRunnerError(
            f"{label} is invalid JSON at line {error.lineno}, column {error.colno}",
            "invalid_json",
        ) from None
    except OSError:
        raise InteractionRunnerError(f"{label} could not be read: {path.name}", "file_read") from None
    if not isinstance(value, dict):
        raise InteractionRunnerError(f"{label} must contain a JSON object", "invalid_shape")
    return value


def _resolve_package_path(package_argument: str | Path) -> Path:
    supplied = Path(package_argument).expanduser()
    candidates = [supplied] if supplied.is_absolute() else []
    if not supplied.is_absolute():
        current_directory_candidate = Path.cwd() / supplied
        explicit_relative_path = len(supplied.parts) > 1 or str(supplied) in {".", ".."}
        if explicit_relative_path:
            candidates.append(current_directory_candidate)
        for resolver in (
            lambda: data_path(supplied),
            lambda: data_path("runs", supplied),
        ):
            try:
                candidates.append(resolver())
            except ValueError:
                pass
        if not explicit_relative_path:
            candidates.append(current_directory_candidate)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    raise InteractionRunnerError(
        "run directory was not found; pass a run_id under runs/ or a path to a run directory",
        "run_missing",
    )


def _schema_issues(instance: Any, schema: dict[str, Any], label: str) -> list[str]:
    try:
        issues = validate_json_schema(instance, schema)
    except SchemaDefinitionError as error:
        raise InteractionRunnerError(f"{label} is invalid: {error}", "schema_definition") from None
    return [str(issue) for issue in issues]


def _load_adapter(site_id: Any) -> tuple[Path, dict[str, dict[str, Any]], Any, Any, Any]:
    if not isinstance(site_id, str) or not _SITE_ID_PATTERN.fullmatch(site_id):
        raise InteractionRunnerError("questions.json contains an invalid site_id", "invalid_site")
    adapter_root = ADAPTERS_ROOT.resolve()
    adapter_dir = (adapter_root / site_id).resolve()
    if adapter_root not in adapter_dir.parents:
        raise InteractionRunnerError("adapter path is outside the adapters directory", "invalid_site")
    if not adapter_dir.is_dir():
        raise InteractionRunnerError(f"no adapter is registered for site_id {site_id}", "unknown_adapter")

    schemas: dict[str, dict[str, Any]] = {}
    for name in ("questions", "interaction", "answers", "execution_result"):
        schema_path = adapter_dir / "schemas" / f"{name}.schema.json"
        schemas[name] = _read_json(schema_path, f"{name} schema")
    try:
        solver_adapter = importlib.import_module(f"core.adapters.{site_id}.solver")
        interaction_adapter = importlib.import_module(f"core.adapters.{site_id}.interaction")
    except ImportError as error:
        missing_name = getattr(error, "name", None)
        if missing_name and missing_name.startswith("core.adapters."):
            raise InteractionRunnerError(
                f"adapter {site_id} is missing its solver or interaction module",
                "adapter_module_missing",
            ) from None
        raise InteractionRunnerError(
            f"adapter {site_id} could not load a required runtime dependency",
            "adapter_dependency",
        ) from None

    answer_validator = getattr(solver_adapter, "validate_answers_against_questions", None)
    question_validator = getattr(solver_adapter, "validate_question_package", None)
    interaction_validator = getattr(interaction_adapter, "validate_interaction_against_questions", None)
    executor = getattr(interaction_adapter, "execute_answers", None)
    fill_only_executor = getattr(interaction_adapter, "execute_answers_fill_only", None)
    adapter_error_type = getattr(interaction_adapter, "InteractionAdapterError", None)
    if not isinstance(adapter_error_type, type) or not issubclass(adapter_error_type, Exception):
        # Keep compatibility with adapters created before the generic contract.
        adapter_error_type = getattr(interaction_adapter, "ChaoxingInteractionError", None)
    safe_error_types: tuple[type[BaseException], ...] = ()
    if isinstance(adapter_error_type, type) and issubclass(adapter_error_type, Exception):
        safe_error_types = (adapter_error_type,)
    if not callable(answer_validator) or not callable(question_validator):
        raise InteractionRunnerError(
            f"adapter {site_id} has no question and answer cross-reference validator",
            "adapter_validator_missing",
        )
    if not callable(interaction_validator) or not callable(executor):
        raise InteractionRunnerError(
            f"adapter {site_id} has no interaction validator or executor",
            "interaction_adapter_missing",
        )
    return adapter_dir, schemas, answer_validator, question_validator, (
        interaction_validator,
        executor,
        fill_only_executor,
        safe_error_types,
    )


def _validate_schema_file(
    package: dict[str, Any], schema: dict[str, Any], label: str
) -> None:
    issues = _schema_issues(package, schema, label)
    if issues:
        raise InteractionRunnerError(
            f"{label} does not match its adapter schema: " + "; ".join(issues[:8]),
            f"{label}_schema",
        )


def _validate_package_set(
    questions_package: dict[str, Any],
    interaction_package: dict[str, Any],
    answers_package: dict[str, Any],
    schemas: dict[str, dict[str, Any]],
    answer_validator: Any,
    question_validator: Any,
    interaction_validator: Any,
) -> None:
    _validate_schema_file(questions_package, schemas["questions"], "questions.json")
    _validate_schema_file(interaction_package, schemas["interaction"], "interaction.json")
    _validate_schema_file(answers_package, schemas["answers"], "answers.json")

    metadata_keys = ("site_id", "adapter_version", "schema_version", "run_id")
    for key in metadata_keys:
        expected = questions_package.get(key)
        if interaction_package.get(key) != expected or answers_package.get(key) != expected:
            raise InteractionRunnerError(
                f"questions.json, interaction.json, and answers.json must share {key}",
                "metadata_mismatch",
            )

    questions = questions_package["questions"]
    answers = answers_package["answers"]
    question_issues = question_validator(questions)
    if question_issues:
        raise InteractionRunnerError(
            "questions.json contains duplicate question, option, or slot IDs: "
            + "; ".join(question_issues[:8]),
            "question_reference",
        )
    answer_issues = answer_validator(questions, answers)
    if answer_issues:
        raise InteractionRunnerError(
            "answers.json does not match questions.json: " + "; ".join(answer_issues[:8]),
            "answer_reference",
        )
    interaction_issues = interaction_validator(questions_package, interaction_package)
    if interaction_issues:
        raise InteractionRunnerError(
            "interaction.json does not match questions.json: "
            + "; ".join(interaction_issues[:8]),
            "interaction_reference",
        )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fallback_items(questions_package: dict[str, Any], reason: str, attempted: bool) -> list[dict[str, Any]]:
    return [
        {
            "question_id": question["question_id"],
            "status": "unconfirmed" if attempted else "skipped",
            "attempted": attempted,
            "reason": reason,
        }
        for question in questions_package["questions"]
    ]


def _result_items_match_questions(items: Any, questions_package: dict[str, Any]) -> bool:
    if not isinstance(items, list):
        return False
    expected_ids = [question["question_id"] for question in questions_package["questions"]]
    actual_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("question_id"), str):
            return False
        actual_ids.append(item["question_id"])
    return len(actual_ids) == len(set(actual_ids)) and set(actual_ids) == set(expected_ids)


def _write_result(package_dir: Path, result: dict[str, Any]) -> Path:
    output_path = package_dir / "execution_result.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=package_dir,
            prefix=".execution_result.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_path, output_path)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise InteractionRunnerError(
            "execution_result.json could not be written",
            "file_write",
        ) from None
    return output_path


def interact_package(
    package_argument: str | Path,
    *,
    overwrite: bool = False,
    fill_only: bool = False,
    task_id: str | None = None,
    run_id: str | None = None,
    page_url: str | None = None,
    model_profile_id: str | None = None,
    credential_provider: Any | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
    submit_confirmation_callback: Callable[[int], bool] | None = None,
) -> Path:
    """Run a validated package through its site adapter.

    Normal submission is available only through the WebUI confirmation
    bridge. Direct runner callers may use ``fill_only`` for a non-submitting
    visible-browser check.
    """
    if task_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task_id):
        raise InteractionRunnerError("task_id is invalid", "invalid_metadata")
    if page_url is not None and not isinstance(page_url, str):
        raise InteractionRunnerError("task URL context is invalid", "invalid_metadata")
    if not fill_only and not callable(submit_confirmation_callback):
        raise InteractionRunnerError(
            "normal interaction requires the AutoLearn WebUI confirmation flow",
            "desktop_confirmation_required",
        )
    logger = _get_logger()
    package_dir = _resolve_package_path(package_argument)
    output_path = package_dir / "execution_result.json"
    if output_path.exists() and not overwrite:
        raise InteractionRunnerError(
            "execution_result.json already exists; pass --overwrite only when you intend to rerun this package",
            "output_exists",
        )

    questions_package = _read_json(package_dir / "questions.json", "questions.json")
    interaction_package = _read_json(package_dir / "interaction.json", "interaction.json")
    answers_package = _read_json(package_dir / "answers.json", "answers.json")
    adapter_dir, schemas, answer_validator, question_validator, adapter_functions = _load_adapter(
        questions_package.get("site_id")
    )
    interaction_validator, executor, fill_only_executor, safe_error_types = adapter_functions
    _validate_package_set(
        questions_package,
        interaction_package,
        answers_package,
        schemas,
        answer_validator,
        question_validator,
        interaction_validator,
    )
    if run_id is not None and questions_package.get("run_id") != run_id:
        raise InteractionRunnerError("task run_id does not match the question package", "invalid_metadata")

    logger.info(
        "interaction started site_id=%s run_id=%s question_count=%d mode=%s",
        questions_package["site_id"],
        questions_package["run_id"],
        len(questions_package["questions"]),
        "fill_only" if fill_only else "webui_submit_confirmation",
    )
    started_at = _iso_now()
    try:
        _report_progress(
            progress_callback,
            "package_validated",
            question_count=len(questions_package["questions"]),
        )
        if _check_cancel(cancel_event):
            items = _fallback_items(
                questions_package,
                "操作在网页交互开始前已取消。",
                attempted=False,
            )
        elif fill_only:
            if not callable(fill_only_executor):
                raise InteractionRunnerError(
                    f"adapter {questions_package['site_id']} does not support fill-only debugging",
                    "fill_only_unsupported",
                )
            items = fill_only_executor(
                questions_package,
                interaction_package,
                answers_package,
                task_id=task_id,
                run_id=run_id or questions_package["run_id"],
                page_url=page_url,
                model_profile_id=model_profile_id,
                credential_provider=credential_provider,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                submit_confirmation_callback=submit_confirmation_callback,
            )
        else:
            items = executor(
                questions_package,
                interaction_package,
                answers_package,
                task_id=task_id,
                run_id=run_id or questions_package["run_id"],
                page_url=page_url,
                model_profile_id=model_profile_id,
                credential_provider=credential_provider,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                submit_confirmation_callback=submit_confirmation_callback,
            )
    except InteractionRunnerError:
        raise
    except Exception as error:
        safe_adapter_error = bool(safe_error_types and isinstance(error, safe_error_types))
        attempted = bool(getattr(error, "attempted", False)) if safe_adapter_error else True
        reason = (
            str(error)
            if safe_adapter_error
            else "interaction adapter stopped unexpectedly; check the website page before retrying."
        )
        if attempted:
            reason = "interaction adapter stopped after a possible submission; check the website page before retrying."
        error_code = getattr(error, "code", None) if safe_adapter_error else None
        if not isinstance(error_code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", error_code):
            error_code = "adapter_error"
        items = _fallback_items(questions_package, reason, attempted)
        logger.error(
            "interaction adapter failed site_id=%s run_id=%s category=%s error_type=%s",
            questions_package["site_id"],
            questions_package["run_id"],
            error_code,
            type(error).__name__,
        )

    if not _result_items_match_questions(items, questions_package):
        items = _fallback_items(
            questions_package,
            "interaction adapter returned an incomplete result; check the website page before retrying.",
            attempted=True,
        )
        logger.error(
            "interaction adapter returned an invalid result set site_id=%s run_id=%s",
            questions_package["site_id"],
            questions_package["run_id"],
        )

    result = {
        "site_id": questions_package["site_id"],
        "adapter_version": questions_package["adapter_version"],
        "schema_version": questions_package["schema_version"],
        "run_id": questions_package["run_id"],
        "started_at": started_at,
        "completed_at": _iso_now(),
        "items": items,
    }
    _validate_schema_file(result, schemas["execution_result"], "execution_result.json")
    written_path = _write_result(package_dir, result)
    counts = {
        status: sum(item["status"] == status for item in items)
        for status in ("confirmed", "failed", "unconfirmed", "skipped", "filled_unsubmitted")
    }
    logger.info(
        "interaction completed site_id=%s run_id=%s confirmed=%d failed=%d unconfirmed=%d skipped=%d filled_unsubmitted=%d",
        questions_package["site_id"],
        questions_package["run_id"],
        counts["confirmed"],
        counts["failed"],
        counts["unconfirmed"],
        counts["skipped"],
        counts["filled_unsubmitted"],
    )
    _report_progress(
        progress_callback,
        "interaction_result_written",
        question_count=len(items),
    )
    return written_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Interact with a website package and write execution_result.json. "
            "Normal submission requires the AutoLearn WebUI confirmation flow."
        )
    )
    parser.add_argument("package", help="run_id under runs/ or a path to a run directory")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing execution_result.json and rerun the package",
    )
    parser.add_argument(
        "--fill-only",
        action="store_true",
        help="debug in a visible browser: fill and verify controls without invoking submission; keep the window open until closed",
    )
    args = parser.parse_args(argv)
    try:
        ensure_user_data_migrated()
        output_path = interact_package(
            args.package,
            overwrite=args.overwrite,
            fill_only=args.fill_only,
        )
    except (InteractionRunnerError, DataMigrationError) as error:
        parser.exit(1, f"交互失败：{error}\n")
    print(f"交互结果：{output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
