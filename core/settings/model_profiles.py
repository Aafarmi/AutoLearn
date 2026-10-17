"""Versioned model profiles persisted alongside the legacy local config."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping
import uuid

from core.paths import data_path

DEFAULT_CONFIG_PATH = data_path("config.json")
_LEGACY_PROFILE_NAMESPACE = uuid.UUID("4a44707c-a516-4c39-8519-e1d2c96b182f")
_PROFILE_ID_NAMESPACE = uuid.UUID("2d575d53-3cf5-45d5-9a51-12cbecae3116")
_SETTINGS_FIELDS = (
    "provider",
    "base_url",
    "api_key",
    "model",
    "temperature",
    "max_output_tokens",
    "timeout_seconds",
)
_DEFAULT_SETTINGS: dict[str, Any] = {
    "provider": "",
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": 0.2,
    "max_output_tokens": 4096,
    "timeout_seconds": 60,
}


class ModelProfileError(RuntimeError):
    """Safe model-profile error that never includes configuration values."""


class ProfileUpdateConfirmationRequired(ModelProfileError):
    """Raised when a task references a profile changed after task creation."""

    def __init__(self, profile_name: str, current_version: int) -> None:
        self.profile_name = profile_name
        self.current_version = current_version
        super().__init__(
            f"模型档案“{profile_name}”在任务创建后已更新到 v{current_version}；"
            "请先确认是否使用当前配置。"
        )


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    name: str
    version: int
    created_at: str | None
    updated_at: str | None
    settings: dict[str, Any] = field(repr=False, compare=False)

    @property
    def api_key_configured(self) -> bool:
        value = self.settings.get("api_key")
        return isinstance(value, str) and bool(value)


@dataclass(frozen=True)
class _ConfigSnapshot:
    config: dict[str, Any] = field(repr=False)
    raw: bytes | None = field(repr=False)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_snapshot(path: Path) -> _ConfigSnapshot:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _ConfigSnapshot({}, None)
    except OSError:
        raise ModelProfileError("无法读取本地配置；文件未修改。") from None
    if len(raw) > 4 * 1024 * 1024:
        raise ModelProfileError("本地配置文件超出允许大小；文件未修改。")
    try:
        config = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ModelProfileError("本地配置不是有效的 UTF-8 JSON；文件未修改。") from None
    if not isinstance(config, dict):
        raise ModelProfileError("本地配置顶层必须是对象；文件未修改。")
    return _ConfigSnapshot(config, raw)


def _write_snapshot(path: Path, snapshot: _ConfigSnapshot, config: dict[str, Any]) -> None:
    try:
        if snapshot.raw is None:
            if path.exists():
                raise ModelProfileError("配置文件已被其他程序修改；本次未覆盖。")
        elif path.read_bytes() != snapshot.raw:
            raise ModelProfileError("配置文件已被其他程序修改；本次未覆盖。")
    except ModelProfileError:
        raise
    except OSError:
        raise ModelProfileError("无法确认配置文件未被并发修改；本次未写入。") from None

    serialized = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".config.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        if snapshot.raw is not None:
            try:
                os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
            except OSError:
                pass
        os.replace(temporary_path, path)
    except ModelProfileError:
        raise
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ModelProfileError("无法安全保存本地配置；原配置保持不变。") from None


def _validate_profile_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ModelProfileError("模型档案 ID 无效。")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ModelProfileError("模型档案 ID 无效。") from None
    if canonical != value.lower():
        raise ModelProfileError("模型档案 ID 无效。")
    return canonical


def _validate_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ModelProfileError("模型档案名称必须是文字。")
    name = " ".join(value.split())
    if not name or len(name) > 80:
        raise ModelProfileError("模型档案名称需为 1 至 80 个字符。")
    return name


def _validate_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelProfileError("模型档案配置格式无效。")
    settings = dict(_DEFAULT_SETTINGS)
    settings.update(value)
    for field_name in ("provider", "base_url", "api_key", "model"):
        field_value = settings.get(field_name)
        if not isinstance(field_value, str):
            raise ModelProfileError("模型档案文字字段格式无效。")
        if len(field_value) > 8192:
            raise ModelProfileError("模型档案文字字段过长。")
    temperature = settings.get("temperature")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
        or not 0 <= float(temperature) <= 2
    ):
        raise ModelProfileError("temperature 必须介于 0 和 2 之间。")
    max_tokens = settings.get("max_output_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 10_000_000:
        raise ModelProfileError("max_output_tokens 必须是正整数。")
    timeout = settings.get("timeout_seconds")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not 0 < float(timeout) <= 3600
    ):
        raise ModelProfileError("timeout_seconds 必须大于 0 且不超过 3600。")
    settings["temperature"] = float(temperature)
    settings["timeout_seconds"] = float(timeout)
    return settings


def _from_config(config: Mapping[str, Any]) -> tuple[list[ModelProfile], str | None]:
    raw_profiles = config.get("model_profiles")
    profiles: list[ModelProfile] = []
    if "model_profiles" not in config:
        for legacy_key, name in (("model", "默认模型"), ("model1", "备用模型")):
            legacy = config.get(legacy_key)
            if legacy is None:
                continue
            settings = _validate_settings(legacy)
            profile_id = str(uuid.uuid5(_LEGACY_PROFILE_NAMESPACE, legacy_key))
            profiles.append(
                ModelProfile(
                    profile_id=profile_id,
                    name=name,
                    version=1,
                    created_at=None,
                    updated_at=None,
                    settings=settings,
                )
            )
        default_id = profiles[0].profile_id if profiles else None
        return profiles, default_id

    if not isinstance(raw_profiles, list):
        raise ModelProfileError("model_profiles 必须是数组；配置未修改。")
    seen_ids: set[str] = set()
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, dict):
            raise ModelProfileError("模型档案记录格式无效；配置未修改。")
        profile_id = _validate_profile_id(raw_profile.get("id"))
        if profile_id in seen_ids:
            raise ModelProfileError("模型档案 ID 重复；配置未修改。")
        seen_ids.add(profile_id)
        name = _validate_name(raw_profile.get("name"))
        version = raw_profile.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ModelProfileError("模型档案版本无效；配置未修改。")
        created_at = raw_profile.get("created_at")
        updated_at = raw_profile.get("updated_at")
        for timestamp in (created_at, updated_at):
            if timestamp is not None:
                if not isinstance(timestamp, str):
                    raise ModelProfileError("模型档案时间字段无效；配置未修改。")
                try:
                    _parse_timestamp(timestamp)
                except (TypeError, ValueError):
                    raise ModelProfileError("模型档案时间字段无效；配置未修改。") from None
        if version > 1 and updated_at is None:
            raise ModelProfileError("已修改的模型档案缺少版本时间；配置未修改。")
        settings = _validate_settings(raw_profile.get("settings"))
        profiles.append(
            ModelProfile(
                profile_id=profile_id,
                name=name,
                version=version,
                created_at=created_at,
                updated_at=updated_at,
                settings=settings,
            )
        )

    default_id = config.get("default_model_profile_id")
    if default_id is not None:
        default_id = _validate_profile_id(default_id)
        if default_id not in seen_ids:
            raise ModelProfileError("默认模型档案不存在；配置未修改。")
    elif profiles:
        default_id = profiles[0].profile_id
    return profiles, default_id


def _profile_to_json(profile: ModelProfile) -> dict[str, Any]:
    return {
        "id": profile.profile_id,
        "name": profile.name,
        "version": profile.version,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "settings": dict(profile.settings),
    }


def _merge_legacy_model(config: dict[str, Any], profile: ModelProfile) -> None:
    current = config.get("model")
    legacy = dict(current) if isinstance(current, dict) else {}
    legacy.update(profile.settings)
    config["model"] = legacy


class ModelProfileStore:
    """Read and update model profiles without exposing secrets in summaries."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH

    def _read(self) -> tuple[_ConfigSnapshot, list[ModelProfile], str | None]:
        snapshot = _read_snapshot(self.config_path)
        profiles, default_id = _from_config(snapshot.config)
        return snapshot, profiles, default_id

    def list_profiles(self) -> tuple[ModelProfile, ...]:
        _snapshot, profiles, _default_id = self._read()
        return tuple(profiles)

    def default_profile_id(self) -> str | None:
        _snapshot, _profiles, default_id = self._read()
        return default_id

    def get_profile(self, profile_id: str) -> ModelProfile:
        profile_id = _validate_profile_id(profile_id)
        _snapshot, profiles, _default_id = self._read()
        for profile in profiles:
            if profile.profile_id == profile_id:
                return profile
        raise ModelProfileError("指定的模型档案不存在。")

    def save_profile(
        self,
        profile_id: str | None,
        *,
        name: str,
        settings: Mapping[str, Any],
        api_key: str | None = None,
    ) -> ModelProfile:
        snapshot, profiles, default_id = self._read()
        name = _validate_name(name)
        profile = None
        if profile_id is not None:
            profile_id = _validate_profile_id(profile_id)
            profile = next((item for item in profiles if item.profile_id == profile_id), None)
            if profile is None:
                raise ModelProfileError("指定的模型档案不存在。")

        if profile is None:
            profile_id = str(uuid.uuid4())
            base_settings = dict(_DEFAULT_SETTINGS)
            previous_version = 0
            created_at = _timestamp()
        else:
            base_settings = dict(profile.settings)
            previous_version = profile.version
            created_at = profile.created_at

        for field_name in _SETTINGS_FIELDS:
            if field_name in settings:
                candidate = settings[field_name]
                if field_name == "api_key" and candidate is None:
                    continue
                base_settings[field_name] = candidate
        if api_key is not None:
            base_settings["api_key"] = api_key
        normalized_settings = _validate_settings(base_settings)

        now = _timestamp()
        changed = (
            profile is None
            or name != profile.name
            or normalized_settings != profile.settings
        )
        new_profile = ModelProfile(
            profile_id=profile_id,
            name=name,
            version=previous_version + 1 if profile is not None and changed else max(1, previous_version),
            created_at=created_at or now,
            updated_at=now if changed else (profile.updated_at if profile else now),
            settings=normalized_settings,
        )
        by_id = {item.profile_id: item for item in profiles}
        by_id[new_profile.profile_id] = new_profile
        ordered_profiles = list(by_id.values())
        if default_id is None:
            default_id = new_profile.profile_id

        config = dict(snapshot.config)
        config["model_profiles"] = [_profile_to_json(item) for item in ordered_profiles]
        config["default_model_profile_id"] = default_id
        if default_id == new_profile.profile_id:
            _merge_legacy_model(config, new_profile)
        _write_snapshot(self.config_path, snapshot, config)
        return new_profile

    def create_profile(self, *, name: str = "新模型") -> ModelProfile:
        return self.save_profile(None, name=name, settings={})

    def set_default_profile(self, profile_id: str) -> ModelProfile:
        profile_id = _validate_profile_id(profile_id)
        snapshot, profiles, _old_default_id = self._read()
        selected = next((item for item in profiles if item.profile_id == profile_id), None)
        if selected is None:
            raise ModelProfileError("指定的模型档案不存在。")
        config = dict(snapshot.config)
        config["model_profiles"] = [_profile_to_json(item) for item in profiles]
        config["default_model_profile_id"] = selected.profile_id
        _merge_legacy_model(config, selected)
        _write_snapshot(self.config_path, snapshot, config)
        return selected

    def delete_profile(self, profile_id: str) -> str | None:
        profile_id = _validate_profile_id(profile_id)
        snapshot, profiles, default_id = self._read()
        if not any(item.profile_id == profile_id for item in profiles):
            raise ModelProfileError("指定的模型档案不存在。")
        remaining = [item for item in profiles if item.profile_id != profile_id]
        if default_id == profile_id:
            default_id = remaining[0].profile_id if remaining else None

        config = dict(snapshot.config)
        config["model_profiles"] = [_profile_to_json(item) for item in remaining]
        config["default_model_profile_id"] = default_id
        config.pop("model1", None)
        if default_id is None:
            config.pop("model", None)
        else:
            selected = next(item for item in remaining if item.profile_id == default_id)
            _merge_legacy_model(config, selected)
        _write_snapshot(self.config_path, snapshot, config)
        return default_id

    def resolve_settings(self, profile_id: str) -> dict[str, Any]:
        """Return settings for trusted local backend use; never put in UI events."""
        return dict(self.get_profile(profile_id).settings)


def profile_changed_after(profile: ModelProfile, task_created_at: str) -> bool:
    """Whether a profile was created or edited after its task was created.

    Legacy profiles are imported at version 1 without a synthetic modification
    time so migration alone never triggers an unexpected confirmation.
    """
    if not isinstance(task_created_at, str):
        raise ValueError("task_created_at must be a timestamp")
    changed_at = profile.updated_at or profile.created_at
    if changed_at is None:
        return False
    try:
        return _parse_timestamp(changed_at) > _parse_timestamp(task_created_at)
    except (TypeError, ValueError):
        raise ValueError("task/profile timestamp is invalid") from None


def require_profile_confirmation(
    profile: ModelProfile,
    task_created_at: str,
    *,
    confirmed: bool,
) -> None:
    if profile_changed_after(profile, task_created_at) and not confirmed:
        raise ProfileUpdateConfirmationRequired(profile.name, profile.version)
