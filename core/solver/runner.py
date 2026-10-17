"""Read a question package, request structured answers, validate, and save them."""

from __future__ import annotations

import argparse
import base64
import importlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import socket
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from core.common.validation import (
    SchemaDefinitionError,
    ValidationIssue,
    validate_json_schema,
)
from core.data_migration import DataMigrationError, ensure_user_data_migrated
from core.paths import data_path, resource_path
from core.settings import ModelProfileError, ModelProfileStore, profile_changed_after


LOG_PATH = data_path("logs", "solver.log")
MAX_API_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SINGLE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 60 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "max context",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "request too large",
    "maximum input",
)


class SolverError(RuntimeError):
    """An expected, user-actionable solver failure."""

    def __init__(self, message: str, category: str = "solver") -> None:
        super().__init__(message)
        self.category = category


class SolverCancelled(SolverError):
    """Raised at a solver safe point after a cooperative cancel request."""

    def __init__(self) -> None:
        super().__init__("solver cancelled", "cancelled")


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SolverCancelled()


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


class ModelProfileConfirmationRequired(SolverError):
    """A task must explicitly approve a profile changed since task creation."""

    def __init__(self, profile_name: str, current_version: int) -> None:
        self.profile_name = profile_name
        self.current_version = current_version
        super().__init__(
            f"模型档案“{profile_name}”在任务创建后已更新到 v{current_version}；"
            "请先确认是否使用当前配置。",
            "model_profile_confirmation",
        )


class ContextLimitError(SolverError):
    def __init__(self) -> None:
        super().__init__(
            "request exceeded the model context or payload limit",
            "context_limit",
        )


class ModelOutputError(SolverError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message, category)


class ModelAPIError(SolverError):
    def __init__(
        self,
        status: int | None,
        provider_error_type: str | None = None,
        provider_error_code: str | None = None,
    ) -> None:
        self.status = status
        self.provider_error_type = provider_error_type
        self.provider_error_code = provider_error_code
        provider_detail = provider_error_code or provider_error_type
        detail_suffix = f" (provider error: {provider_detail})" if provider_detail else ""
        if status is None:
            message = "could not connect to the configured model endpoint"
            category = "network"
        elif status == 401:
            message = (
                "model service rejected the API key (HTTP 401); verify model.api_key "
                "in config.json and confirm it belongs to the configured API service"
                f"{detail_suffix}"
            )
            category = "authentication"
        elif status == 403:
            message = (
                "model service denied API access (HTTP 403); check the account plan, "
                "model permissions, or endpoint access"
                f"{detail_suffix}"
            )
            category = "permission_denied"
        elif status == 404:
            message = "model endpoint or model was not found; check model.base_url and model.model"
            category = "not_found"
        elif status == 413:
            message = "model service rejected the request as too large"
            category = "payload_too_large"
        elif status == 429:
            message = "model service rate or usage limit was reached"
            category = "rate_limited"
        elif status == 400:
            message = "model service rejected the request; check model, endpoint, and request compatibility"
            category = "bad_request"
        elif status >= 500:
            message = f"model service returned HTTP {status}"
            category = "upstream"
        else:
            message = f"model service returned HTTP {status}"
            category = "http"
        super().__init__(message, category)


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    endpoint_url: str
    api_key: str = field(repr=False)
    model: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: float
    api_style: str


@dataclass(frozen=True)
class ModelResponse:
    text: str
    truncated: bool
    prompt_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ImageAttachment:
    asset_path: str
    references: tuple[str, ...]
    media_type: str
    encoded_data: str

    @property
    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.encoded_data}"


def _get_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("autolearn.solver")
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
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    return logger


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        raise SolverError(f"{label} was not found: {path.name}", "file_missing") from None
    except json.JSONDecodeError as error:
        raise SolverError(
            f"{label} is invalid JSON at line {error.lineno}, column {error.colno}",
            "invalid_json",
        ) from None
    except OSError:
        raise SolverError(f"{label} could not be read: {path.name}", "file_read") from None

    if not isinstance(value, dict):
        raise SolverError(f"{label} must contain a JSON object", "invalid_shape")
    return value


def _resolve_package_path(package_argument: str | Path) -> Path:
    supplied = Path(package_argument).expanduser()
    candidates: list[Path] = []
    if supplied.is_absolute():
        candidates.append(supplied)
    else:
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

    raise SolverError(
        "run directory was not found; pass a run_id under runs/ or a directory path",
        "run_missing",
    )


def _safe_site_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise SolverError("questions.json contains an invalid site_id", "invalid_site")
    return value


def _read_adapter_files(
    site_id: str,
) -> tuple[Path, dict[str, Any], dict[str, Any], str, Any, Any]:
    adapters_root = resource_path("core", "adapters").resolve()
    adapter_dir = (adapters_root / site_id).resolve()
    if adapters_root not in adapter_dir.parents:
        raise SolverError("adapter path is outside the adapters directory", "invalid_site")
    if not adapter_dir.is_dir():
        raise SolverError(f"no adapter is registered for site_id {site_id}", "unknown_adapter")

    questions_schema = _read_json(
        adapter_dir / "schemas" / "questions.schema.json",
        "questions schema",
    )
    answers_schema = _read_json(
        adapter_dir / "schemas" / "answers.schema.json",
        "answers schema",
    )
    readme_path = adapter_dir / "README.md"
    try:
        adapter_readme = readme_path.read_text(encoding="utf-8")
    except OSError:
        raise SolverError("adapter README.md could not be read", "file_read") from None

    try:
        adapter_solver = importlib.import_module(f"core.adapters.{site_id}.solver")
    except ImportError:
        raise SolverError(
            f"adapter {site_id} has no solver cross-reference validator",
            "adapter_solver_missing",
        ) from None

    validator = getattr(adapter_solver, "validate_answers_against_questions", None)
    if not callable(validator):
        raise SolverError(
            f"adapter {site_id} has no answer cross-reference validator",
            "adapter_solver_missing",
        )
    question_validator = getattr(adapter_solver, "validate_question_package", None)
    if not callable(question_validator):
        raise SolverError(
            f"adapter {site_id} has no question cross-reference validator",
            "adapter_solver_missing",
        )
    return (
        adapter_dir,
        questions_schema,
        answers_schema,
        adapter_readme,
        validator,
        question_validator,
    )


def model_config_from_section(model_section: Any) -> ModelConfig:
    """Validate one model settings mapping into a ModelConfig.

    Shared by the solver and the settings-page connection test so both apply the
    same endpoint, API key and timeout rules.
    """
    if not isinstance(model_section, dict):
        raise SolverError("config.json must contain a model object", "config")

    endpoint_url = model_section.get("base_url")
    api_key = model_section.get("api_key", "")
    model_name = model_section.get("model")
    provider = model_section.get("provider", "")
    temperature = model_section.get("temperature", 0.2)
    max_output_tokens = model_section.get("max_output_tokens")
    timeout_seconds = model_section.get("timeout_seconds")

    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        raise SolverError("fill in model.base_url with the complete API URL", "config")
    endpoint_url = endpoint_url.strip()
    try:
        parsed_url = urllib.parse.urlsplit(endpoint_url)
        host = parsed_url.hostname or ""
        _ = parsed_url.port
    except ValueError:
        raise SolverError("model.base_url is not a valid absolute URL", "config") from None

    if parsed_url.scheme not in {"https", "http"} or not parsed_url.netloc:
        raise SolverError("model.base_url must be a complete HTTP or HTTPS URL", "config")
    if parsed_url.fragment:
        raise SolverError("model.base_url must not contain a URL fragment", "config")

    is_loopback = host.lower() == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if parsed_url.scheme != "https" and not (
        parsed_url.scheme == "http" and is_loopback
    ):
        raise SolverError(
            "model.base_url must use HTTPS unless it points to a local loopback service",
            "config",
        )

    normalized_path = parsed_url.path.rstrip("/").lower()
    if normalized_path.endswith("/chat/completions"):
        api_style = "chat_completions"
    elif normalized_path.endswith("/responses"):
        api_style = "responses"
    elif normalized_path.endswith("/messages"):
        api_style = "anthropic_messages"
    else:
        raise SolverError(
            "model.base_url must be a complete Chat Completions, Responses, or Messages endpoint URL",
            "config",
        )

    if not isinstance(api_key, str):
        raise SolverError("model.api_key must be a string", "config")
    if not isinstance(model_name, str) or not model_name.strip():
        raise SolverError("fill in model.model in config.json", "config")
    if not isinstance(provider, str):
        raise SolverError("model.provider must be a string", "config")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise SolverError("model.temperature must be a number", "config")
    if temperature < 0 or temperature > 2:
        raise SolverError("model.temperature must be between 0 and 2", "config")
    if api_style == "anthropic_messages" and temperature > 1:
        raise SolverError(
            "model.temperature must be between 0 and 1 for the Messages endpoint",
            "config",
        )
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens < 1
    ):
        raise SolverError("model.max_output_tokens must be a positive integer", "config")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise SolverError("model.timeout_seconds must be a positive number", "config")

    return ModelConfig(
        provider=provider,
        endpoint_url=endpoint_url,
        api_key=api_key,
        model=model_name.strip(),
        temperature=float(temperature),
        max_output_tokens=max_output_tokens,
        timeout_seconds=float(timeout_seconds),
        api_style=api_style,
    )


def _load_model_config(
    path: Path,
    model_profile_id: str | None = None,
    credential_provider: Any | None = None,
) -> ModelConfig:
    config = _read_json(path, "config.json")
    if model_profile_id is None:
        model_section = config.get("model")
    else:
        try:
            model_section = (
                credential_provider.get_model_settings(model_profile_id)
                if credential_provider is not None
                else ModelProfileStore(path).resolve_settings(model_profile_id)
            )
        except Exception:
            raise SolverError("selected model profile is unavailable or invalid", "config") from None
    return model_config_from_section(model_section)


def _schema_issues(instance: Any, schema: dict[str, Any], label: str) -> list[ValidationIssue]:
    try:
        return validate_json_schema(instance, schema)
    except SchemaDefinitionError:
        raise SolverError(
            f"{label} uses a JSON Schema feature this solver does not support",
            "schema_feature",
        ) from None


def _format_issues(issues: list[ValidationIssue]) -> str:
    return "; ".join(str(issue) for issue in issues[:5])


def _validate_question_package(
    package: dict[str, Any], schema: dict[str, Any]
) -> list[dict[str, Any]]:
    issues = _schema_issues(package, schema, "questions schema")
    if issues:
        raise SolverError(
            "questions.json does not match its adapter schema: "
            + _format_issues(issues),
            "questions_schema",
        )
    questions = package.get("questions")
    if not isinstance(questions, list):
        raise SolverError("questions.json is missing its questions array", "invalid_shape")
    return questions


def _resolve_asset(package_dir: Path, asset_path: str) -> Path:
    if not isinstance(asset_path, str) or not asset_path:
        raise SolverError("question contains an empty asset path", "asset_path")
    if "\\" in asset_path:
        raise SolverError("asset paths must use forward slashes", "asset_path")

    relative = PurePosixPath(asset_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise SolverError("question contains an unsafe asset path", "asset_path")

    package_root = package_dir.resolve()
    resolved = (package_root / Path(*relative.parts)).resolve()
    if package_root not in resolved.parents:
        raise SolverError("asset path resolves outside the run directory", "asset_path")
    if not resolved.is_file():
        raise SolverError(f"question asset is missing: {asset_path}", "asset_missing")
    return resolved


def _content_references(question: dict[str, Any]) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    question_id = question.get("question_id", "unknown")
    stem = question.get("stem", {})
    if isinstance(stem, dict):
        references.extend(
            (path, f"question {question_id} stem")
            for path in stem.get("asset_paths", [])
        )

    for option in question.get("options", []):
        if not isinstance(option, dict):
            continue
        content = option.get("content", {})
        if not isinstance(content, dict):
            continue
        label = option.get("label") or option.get("option_id") or "option"
        references.extend(
            (path, f"question {question_id} option {label}")
            for path in content.get("asset_paths", [])
        )
    return references


def _validate_all_assets(package_dir: Path, questions: list[dict[str, Any]]) -> int:
    paths: set[str] = set()
    for question in questions:
        for asset_path, _ in _content_references(question):
            paths.add(asset_path)
    for asset_path in sorted(paths):
        _resolve_asset(package_dir, asset_path)
    return len(paths)


def _build_attachments(
    package_dir: Path, questions: list[dict[str, Any]]
) -> list[ImageAttachment]:
    references_by_path: dict[str, set[str]] = {}
    for question in questions:
        for asset_path, label in _content_references(question):
            references_by_path.setdefault(asset_path, set()).add(label)

    attachments: list[ImageAttachment] = []
    total_bytes = 0
    for asset_path in sorted(references_by_path):
        path = _resolve_asset(package_dir, asset_path)
        size = path.stat().st_size
        if size > MAX_SINGLE_IMAGE_BYTES:
            raise ContextLimitError()
        total_bytes += size
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ContextLimitError()

        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type not in SUPPORTED_IMAGE_TYPES:
            raise SolverError(
                f"unsupported image type for attachment: {asset_path}",
                "asset_type",
            )
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            raise SolverError(
                f"image attachment could not be read: {asset_path}",
                "asset_read",
            ) from None

        attachments.append(
            ImageAttachment(
                asset_path=asset_path,
                references=tuple(sorted(references_by_path[asset_path])),
                media_type=mime_type,
                encoded_data=encoded,
            )
        )
    return attachments


def _read_optional_package_readme(package_dir: Path) -> str:
    path = package_dir / "README.md"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise SolverError("run README.md could not be read", "file_read") from None
    return text[:30_000]


def _build_user_text(
    package: dict[str, Any],
    questions: list[dict[str, Any]],
    adapter_readme: str,
    package_readme: str,
    answers_schema: dict[str, Any],
) -> str:
    sections = [
        "Adapter documentation (use it to interpret adapter fields and answer types):",
        adapter_readme[:30_000],
    ]
    if package_readme:
        sections.extend(
            [
                "",
                "Run package README (reference material):",
                package_readme,
            ]
        )
    sections.extend(
        [
            "",
            "Authoritative answer JSON Schema:",
            json.dumps(answers_schema, ensure_ascii=False, indent=2),
            "",
            "Questions for this request:",
            json.dumps(
                {
                    "site_id": package["site_id"],
                    "adapter_version": package["adapter_version"],
                    "schema_version": package["schema_version"],
                    "questions": questions,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "",
            "Image attachments follow as separate image inputs. Their labels identify the question and stem or option they belong to.",
        ]
    )
    return "\n".join(sections)


def _build_request_payload(
    settings: ModelConfig,
    system_prompt: str,
    user_text: str,
    attachments: list[ImageAttachment],
) -> dict[str, Any]:
    if settings.api_style == "chat_completions":
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for attachment in attachments:
            labels = ", ".join(attachment.references)
            user_content.append(
                {
                    "type": "text",
                    "text": f"Image reference {attachment.asset_path} ({labels})",
                }
            )
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": attachment.data_url,
                        "detail": "auto",
                    },
                }
            )
        return {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": settings.temperature,
            "max_tokens": settings.max_output_tokens,
        }

    if settings.api_style == "responses":
        user_content = [{"type": "input_text", "text": user_text}]
        for attachment in attachments:
            labels = ", ".join(attachment.references)
            user_content.append(
                {
                    "type": "input_text",
                    "text": f"Image reference {attachment.asset_path} ({labels})",
                }
            )
            user_content.append(
                {
                    "type": "input_image",
                    "image_url": attachment.data_url,
                }
            )
        return {
            "model": settings.model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": settings.temperature,
            "max_output_tokens": settings.max_output_tokens,
        }

    user_content = [{"type": "text", "text": user_text}]
    for attachment in attachments:
        labels = ", ".join(attachment.references)
        user_content.append(
            {
                "type": "text",
                "text": f"Image reference {attachment.asset_path} ({labels})",
            }
        )
        user_content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": attachment.media_type,
                    "data": attachment.encoded_data,
                },
            }
        )
    return {
        "model": settings.model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
        "temperature": settings.temperature,
        "max_tokens": settings.max_output_tokens,
    }


def _is_context_limit_error(status: int, body: str) -> bool:
    if status == 413:
        return True
    normalized = body.lower()
    return any(marker in normalized for marker in CONTEXT_ERROR_MARKERS)


def _provider_error_fields(body: str) -> tuple[str | None, str | None]:
    """Extract only safe machine-readable error identifiers from provider JSON."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, dict):
        return None, None

    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None

    def safe_identifier(value: Any) -> str | None:
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            return value
        return None

    return safe_identifier(error.get("type")), safe_identifier(error.get("code"))


def _post_json(settings: ModelConfig, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "curl/8.21.0",
    }
    if settings.api_style == "anthropic_messages":
        if settings.api_key:
            headers["x-api-key"] = settings.api_key
        headers["anthropic-version"] = "2023-06-01"
    elif settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"

    request = urllib.request.Request(
        settings.endpoint_url,
        data=encoded,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.timeout_seconds,
        ) as response:
            raw_body = response.read(MAX_API_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        try:
            body = error.read(MAX_API_RESPONSE_BYTES).decode("utf-8", errors="replace")
        except OSError:
            body = ""
        if _is_context_limit_error(error.code, body):
            raise ContextLimitError() from None
        error_type, error_code = _provider_error_fields(body)
        raise ModelAPIError(error.code, error_type, error_code) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
        raise ModelAPIError(None) from None

    if len(raw_body) > MAX_API_RESPONSE_BYTES:
        raise ModelOutputError(
            "response_too_large",
            "model response exceeded the response size limit",
        )
    try:
        value = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ModelOutputError(
            "invalid_api_response",
            "model endpoint returned invalid JSON",
        ) from None
    if not isinstance(value, dict):
        raise ModelOutputError(
            "invalid_api_response",
            "model endpoint returned an unexpected response",
        )
    return value


def _extract_chat_text(response: dict[str, Any]) -> tuple[str, bool]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelOutputError("empty_response", "model endpoint returned no answer")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ModelOutputError("empty_response", "model endpoint returned no answer")
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    else:
        text = ""
    if not text.strip():
        raise ModelOutputError("empty_response", "model endpoint returned no answer text")
    return text, choice.get("finish_reason") == "length"


def _extract_responses_text(response: dict[str, Any]) -> tuple[str, bool]:
    text_value = response.get("output_text")
    if isinstance(text_value, str) and text_value.strip():
        text = text_value
    else:
        parts: list[str] = []
        output_items = response.get("output", [])
        if not isinstance(output_items, list):
            output_items = []
        for output_item in output_items:
            if not isinstance(output_item, dict):
                continue
            for content_item in output_item.get("content", []):
                if (
                    isinstance(content_item, dict)
                    and content_item.get("type") in {"output_text", "text"}
                    and isinstance(content_item.get("text"), str)
                ):
                    parts.append(content_item["text"])
        text = "".join(parts)
    if not text.strip():
        raise ModelOutputError("empty_response", "model endpoint returned no answer text")

    incomplete = response.get("status") == "incomplete"
    details = response.get("incomplete_details")
    if isinstance(details, dict) and details.get("reason") in {
        "max_output_tokens",
        "max_tokens",
    }:
        incomplete = True
    return text, incomplete


def _extract_anthropic_text(response: dict[str, Any]) -> tuple[str, bool]:
    content = response.get("content")
    if not isinstance(content, list):
        raise ModelOutputError("empty_response", "model endpoint returned no answer")
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    if not text.strip():
        raise ModelOutputError("empty_response", "model endpoint returned no answer text")
    return text, response.get("stop_reason") == "max_tokens"


PROBE_MAX_OUTPUT_TOKENS = 16
PROBE_PROMPT = "Reply with the single word: pong"
PROBE_TIMEOUT_CAP_SECONDS = 30.0


def _probe_payload(settings: ModelConfig) -> dict[str, Any]:
    """Smallest request each supported API style accepts for a liveness check."""
    if settings.api_style == "chat_completions":
        return {
            "model": settings.model,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": PROBE_MAX_OUTPUT_TOKENS,
        }
    if settings.api_style == "responses":
        return {
            "model": settings.model,
            "input": PROBE_PROMPT,
            "max_output_tokens": PROBE_MAX_OUTPUT_TOKENS,
        }
    return {
        "model": settings.model,
        "max_tokens": PROBE_MAX_OUTPUT_TOKENS,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
    }


def _probe_reply(settings: ModelConfig, response: dict[str, Any]) -> str:
    """Best-effort reply text; an empty answer must not fail the health check."""
    extractors = {
        "chat_completions": _extract_chat_text,
        "responses": _extract_responses_text,
        "anthropic_messages": _extract_anthropic_text,
    }
    extractor = extractors.get(settings.api_style)
    if extractor is None:
        return ""
    try:
        text, _truncated = extractor(response)
    except Exception:
        return ""
    return text.strip()[:120]


def probe_model_endpoint(
    model_section: Any,
    *,
    timeout_cap: float = PROBE_TIMEOUT_CAP_SECONDS,
) -> dict[str, Any]:
    """Verify a model endpoint with one minimal request before it is saved.

    The returned mapping is safe for the UI: it carries the host, the measured
    latency, the detected API style and a short reply. It never contains the
    query string, request headers, the API key, or the response body.
    """
    settings = model_config_from_section(model_section)
    timeout_seconds = settings.timeout_seconds
    if timeout_cap and timeout_cap > 0:
        timeout_seconds = min(timeout_seconds, timeout_cap)
    probe_settings = ModelConfig(
        provider=settings.provider,
        endpoint_url=settings.endpoint_url,
        api_key=settings.api_key,
        model=settings.model,
        temperature=0.0,
        max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
        timeout_seconds=float(timeout_seconds),
        api_style=settings.api_style,
    )

    started = time.monotonic()
    response = _post_json(probe_settings, _probe_payload(probe_settings))
    latency_ms = int(round((time.monotonic() - started) * 1000))
    host = urllib.parse.urlsplit(probe_settings.endpoint_url).hostname or ""
    return {
        "latency_ms": latency_ms,
        "endpoint_host": host,
        "api_style": probe_settings.api_style,
        "reply": _probe_reply(probe_settings, response),
    }


def _call_model(
    settings: ModelConfig,
    system_prompt: str,
    user_text: str,
    attachments: list[ImageAttachment],
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
    question_count: int | None = None,
) -> ModelResponse:
    _check_cancel(cancel_event)
    _report_progress(
        progress_callback,
        "model_request_started",
        question_count=question_count,
        asset_count=len(attachments),
    )
    payload = _build_request_payload(
        settings,
        system_prompt,
        user_text,
        attachments,
    )
    response = _post_json(settings, payload)
    _check_cancel(cancel_event)
    _report_progress(
        progress_callback,
        "model_response_received",
        question_count=question_count,
        asset_count=len(attachments),
    )

    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
    if not isinstance(prompt_tokens, int):
        prompt_tokens = 0
    if not isinstance(output_tokens, int):
        output_tokens = 0

    if settings.api_style == "chat_completions":
        text, truncated = _extract_chat_text(response)
    elif settings.api_style == "responses":
        text, truncated = _extract_responses_text(response)
    else:
        text, truncated = _extract_anthropic_text(response)
    return ModelResponse(
        text=text,
        truncated=truncated,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
    )


def _parse_model_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence_marker = chr(96) * 3
    if stripped.startswith(fence_marker) and stripped.endswith(fence_marker):
        stripped = stripped[len(fence_marker) : -len(fence_marker)].strip()
        first_line, separator, remainder = stripped.partition("\n")
        if separator and first_line.strip().lower() == "json":
            stripped = remainder.strip()

    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        value = None
        for index, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "answers" in candidate:
                value = candidate
                break
    if not isinstance(value, dict) or not isinstance(value.get("answers"), list):
        raise ModelOutputError(
            "invalid_json",
            "model response was not a JSON object containing an answers array",
        )
    return value


def _validate_answer_batch(
    questions: list[dict[str, Any]],
    answers: list[dict[str, Any]],
    package: dict[str, Any],
    answers_schema: dict[str, Any],
    cross_validator: Any,
) -> None:
    answer_package = {
        "site_id": package["site_id"],
        "adapter_version": package["adapter_version"],
        "schema_version": package["schema_version"],
        "run_id": package["run_id"],
        "answers": answers,
    }
    issues = _schema_issues(answer_package, answers_schema, "answers schema")
    if issues:
        raise ModelOutputError(
            "answer_schema",
            "model response does not match the answer schema: "
            + _format_issues(issues),
        )

    cross_issues = cross_validator(questions, answers)
    if cross_issues:
        raise ModelOutputError(
            "answer_reference",
            "model response does not match the question IDs or answer slots: "
            + "; ".join(cross_issues[:5]),
        )


def _solve_batch(
    batch: list[dict[str, Any]],
    package: dict[str, Any],
    package_dir: Path,
    adapter_readme: str,
    package_readme: str,
    answers_schema: dict[str, Any],
    cross_validator: Any,
    model_settings: ModelConfig,
    system_prompt: str,
    logger: logging.Logger,
    token_usage: dict[str, int],
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> list[dict[str, Any]]:
    try:
        _check_cancel(cancel_event)
        attachments = _build_attachments(package_dir, batch)
        user_text = _build_user_text(
            package,
            batch,
            adapter_readme,
            package_readme,
            answers_schema,
        )
        response = _call_model(
            model_settings,
            system_prompt,
            user_text,
            attachments,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            question_count=len(batch),
        )
        token_usage["prompt_tokens"] += response.prompt_tokens
        token_usage["output_tokens"] += response.output_tokens

        if response.truncated:
            raise ModelOutputError(
                "output_limit",
                "model response reached the configured output token limit",
            )

        parsed = _parse_model_json(response.text)
        answers = parsed["answers"]
        _validate_answer_batch(
            batch,
            answers,
            package,
            answers_schema,
            cross_validator,
        )
        logger.info(
            "model batch completed question_count=%d image_count=%d prompt_tokens=%d output_tokens=%d",
            len(batch),
            len(attachments),
            response.prompt_tokens,
            response.output_tokens,
        )
        return answers
    except ContextLimitError:
        if len(batch) == 1:
            raise SolverError(
                "a single question and its images exceed the model context or payload limit",
                "context_limit",
            ) from None
        category = "context_limit"
    except ModelOutputError as error:
        if len(batch) == 1:
            raise SolverError(
                f"model could not return a valid answer for one question ({error.category})",
                error.category,
            ) from None
        category = error.category

    midpoint = len(batch) // 2
    logger.warning(
        "splitting model batch question_count=%d reason=%s",
        len(batch),
        category,
    )
    left_answers = _solve_batch(
        batch[:midpoint],
        package,
        package_dir,
        adapter_readme,
        package_readme,
        answers_schema,
        cross_validator,
        model_settings,
        system_prompt,
        logger,
        token_usage,
        cancel_event,
        progress_callback,
    )
    right_answers = _solve_batch(
        batch[midpoint:],
        package,
        package_dir,
        adapter_readme,
        package_readme,
        answers_schema,
        cross_validator,
        model_settings,
        system_prompt,
        logger,
        token_usage,
        cancel_event,
        progress_callback,
    )
    return left_answers + right_answers


def _load_system_prompt() -> str:
    path = resource_path("core", "solver", "prompt.md")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        raise SolverError("solver prompt.md could not be read", "file_read") from None


def _write_answers(package_dir: Path, answer_package: dict[str, Any]) -> Path:
    output_path = package_dir / "answers.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=package_dir,
            prefix=".answers.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(answer_package, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_path, output_path)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise SolverError("answers.json could not be written", "file_write") from None
    return output_path


def solve_package(
    package_argument: str | Path,
    *,
    overwrite: bool = False,
    task_id: str | None = None,
    run_id: str | None = None,
    page_url: str | None = None,
    model_profile_id: str | None = None,
    task_created_at: str | None = None,
    confirmed_profile_version: int | None = None,
    credential_provider: Any | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> Path:
    """Solve a run directory and atomically write its answers.json."""
    if task_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task_id):
        raise SolverError("task_id is invalid", "config")
    if page_url is not None and not isinstance(page_url, str):
        raise SolverError("task URL context is invalid", "config")
    _check_cancel(cancel_event)
    logger = _get_logger()
    package_dir = _resolve_package_path(package_argument)
    questions_path = package_dir / "questions.json"
    output_path = package_dir / "answers.json"
    if output_path.exists() and not overwrite:
        raise SolverError(
            "answers.json already exists; pass --overwrite to replace it",
            "output_exists",
        )

    package = _read_json(questions_path, "questions.json")
    site_id = _safe_site_id(package.get("site_id"))
    (
        _adapter_dir,
        questions_schema,
        answers_schema,
        adapter_readme,
        cross_validator,
        question_validator,
    ) = _read_adapter_files(site_id)
    questions = _validate_question_package(package, questions_schema)
    question_issues = question_validator(questions)
    if question_issues:
        raise SolverError(
            "questions.json contains invalid or duplicate question, option, or slot IDs: "
            + "; ".join(question_issues[:5]),
            "question_reference",
        )
    if not isinstance(package.get("run_id"), str) or not package["run_id"]:
        raise SolverError("questions.json is missing run_id", "invalid_metadata")
    if run_id is not None and package["run_id"] != run_id:
        raise SolverError("task run_id does not match the question package", "invalid_metadata")

    image_count = _validate_all_assets(package_dir, questions)
    package_readme = _read_optional_package_readme(package_dir)
    config_path = data_path("config.json")
    if model_profile_id is not None:
        if not isinstance(model_profile_id, str):
            raise SolverError("model_profile_id is invalid", "config")
        if not isinstance(task_created_at, str):
            raise SolverError("task creation context is required for a model profile", "config")
        if confirmed_profile_version is not None and (
            isinstance(confirmed_profile_version, bool)
            or not isinstance(confirmed_profile_version, int)
            or confirmed_profile_version < 1
        ):
            raise SolverError("confirmed_profile_version is invalid", "config")
        try:
            selected_profile = ModelProfileStore(config_path).get_profile(model_profile_id)
            changed_after_creation = profile_changed_after(selected_profile, task_created_at)
        except (ModelProfileError, ValueError):
            raise SolverError("selected model profile or task context is invalid", "config") from None
        if (
            changed_after_creation
            and confirmed_profile_version != selected_profile.version
        ):
            raise ModelProfileConfirmationRequired(
                selected_profile.name,
                selected_profile.version,
            )
    model_settings = _load_model_config(config_path, model_profile_id, credential_provider)
    system_prompt = _load_system_prompt()
    _check_cancel(cancel_event)

    logger.info(
        "solver started site_id=%s run_id=%s question_count=%d image_count=%d",
        site_id,
        package["run_id"],
        len(questions),
        image_count,
    )
    _report_progress(
        progress_callback,
        "validated_package",
        question_count=len(questions),
        asset_count=image_count,
    )
    token_usage = {"prompt_tokens": 0, "output_tokens": 0}
    answers = _solve_batch(
        questions,
        package,
        package_dir,
        adapter_readme,
        package_readme,
        answers_schema,
        cross_validator,
        model_settings,
        system_prompt,
        logger,
        token_usage,
        cancel_event,
        progress_callback,
    )

    answer_package = {
        "site_id": package["site_id"],
        "adapter_version": package["adapter_version"],
        "schema_version": package["schema_version"],
        "run_id": package["run_id"],
        "answers": answers,
    }
    final_issues = _schema_issues(answer_package, answers_schema, "answers schema")
    if final_issues:
        raise SolverError(
            "assembled answers.json failed schema validation: "
            + _format_issues(final_issues),
            "answers_schema",
        )
    cross_issues = cross_validator(questions, answers)
    if cross_issues:
        raise SolverError(
            "assembled answers.json has invalid question references: "
            + "; ".join(cross_issues[:5]),
            "answer_reference",
        )

    if output_path.exists() and not overwrite:
        raise SolverError(
            "answers.json appeared while solving; it was left unchanged",
            "output_exists",
        )
    _check_cancel(cancel_event)
    written_path = _write_answers(package_dir, answer_package)
    logger.info(
        "solver completed site_id=%s run_id=%s answer_count=%d prompt_tokens=%d output_tokens=%d",
        site_id,
        package["run_id"],
        len(answers),
        token_usage["prompt_tokens"],
        token_usage["output_tokens"],
    )
    _report_progress(
        progress_callback,
        "answers_written",
        question_count=len(answers),
        asset_count=image_count,
    )
    return written_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Solve a run package and write its validated answers.json."
    )
    parser.add_argument(
        "package",
        help="run_id under runs/ or a path to a run directory",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing answers.json after a successful solve",
    )
    args = parser.parse_args(argv)

    try:
        ensure_user_data_migrated()
    except DataMigrationError as error:
        parser.exit(1, f"用户数据迁移失败：{error}\n")

    logger = _get_logger()
    try:
        output_path = solve_package(args.package, overwrite=args.overwrite)
    except SolverError as error:
        status = error.status if isinstance(error, ModelAPIError) else "-"
        provider_error_type = (
            error.provider_error_type if isinstance(error, ModelAPIError) else None
        )
        provider_error_code = (
            error.provider_error_code if isinstance(error, ModelAPIError) else None
        )
        logger.error(
            "solver failed category=%s http_status=%s provider_error_type=%s provider_error_code=%s",
            error.category,
            status,
            provider_error_type or "-",
            provider_error_code or "-",
        )
        print(f"解题失败：{error}", file=sys.stderr)
        print(f"日志：{LOG_PATH}", file=sys.stderr)
        return 1
    except Exception as error:
        logger.error(
            "solver failed unexpectedly error_type=%s",
            type(error).__name__,
        )
        print(f"解题失败：内部错误；请查看日志 {LOG_PATH}", file=sys.stderr)
        return 2

    print(f"解题完成：{output_path}")
    print(f"日志：{LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
