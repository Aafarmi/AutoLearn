"""Backend-only access to local site and model credentials.

Credential values are returned only to backend stages. Callers must never
include them in UI events, logs, task records, or exception messages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.paths import data_path
from core.settings import ModelProfileError, ModelProfileStore


DEFAULT_CONFIG_PATH = data_path("config.json")

_SITE_COOKIE_FIELDS = {
    "chaoxing": ("chaoxing", "cookie"),
    "cjyuketang": ("cjyuketang", "cookie"),
    # 课程站点（智慧树）与做题站点共用同一份 config.json，只是 section 名不同。
    "zhihuishu": ("zhihuishu", "cookie"),
}


class CredentialProviderError(RuntimeError):
    """Safe local credential lookup failure."""


class LocalCredentialProvider:
    """Resolve credentials from the existing local configuration on demand."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH

    def get_site_cookies(self, site_id: str) -> dict[str, str]:
        """Return a fresh in-memory Cookie mapping for an allowed adapter."""
        fields = _SITE_COOKIE_FIELDS.get(site_id)
        if fields is None:
            raise CredentialProviderError("site credentials are unavailable")
        section_name, cookie_field = fields
        try:
            value = json.loads(self._config_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise CredentialProviderError("local site credentials are unavailable") from None
        if not isinstance(value, dict):
            raise CredentialProviderError("local site credentials are invalid")
        section = value.get(section_name)
        if not isinstance(section, dict):
            raise CredentialProviderError("local site credentials are unavailable")
        header = section.get(cookie_field, "")
        if not isinstance(header, str):
            raise CredentialProviderError("local site credentials are invalid")

        cookies: dict[str, str] = {}
        for part in header.split(";"):
            name, separator, cookie_value = part.strip().partition("=")
            if not separator or not name:
                if part.strip():
                    raise CredentialProviderError("local site credentials are invalid")
                continue
            if (
                any(character.isspace() or character in "=," for character in name)
                or any(character in cookie_value for character in "\r\n;")
            ):
                raise CredentialProviderError("local site credentials are invalid")
            previous = cookies.get(name)
            if previous is not None and previous != cookie_value:
                raise CredentialProviderError("local site credentials are ambiguous")
            cookies[name] = cookie_value
        return cookies

    def get_model_settings(self, model_profile_id: str) -> dict[str, Any]:
        """Return secret-bearing profile settings for backend use only."""
        try:
            return ModelProfileStore(self._config_path).resolve_settings(model_profile_id)
        except (ModelProfileError, OSError, ValueError):
            raise CredentialProviderError("selected model credentials are unavailable") from None
