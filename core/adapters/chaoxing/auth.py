"""Chaoxing authentication settings for the standalone cookie manager."""

from __future__ import annotations

from typing import Any


SITE_ID = "chaoxing"
CONFIG_SECTION = "chaoxing"
URL_FIELD = "url"
COOKIE_FIELD = "cookie"
COOKIE_VALUE_TYPE = str
PROFILE_NAME = "chaoxing"
ALLOWED_COOKIE_DOMAINS = ("chaoxing.com",)


def is_authenticated(page: Any) -> bool:
    """Recognize the assignment form shape expected by the Chaoxing crawler."""
    try:
        form = page.locator("form#submitForm")
        if form.count() != 1:
            return False
        return (
            form.locator(".questionLi").count() > 0
            or form.locator('div[id^="question"]').count() > 0
        )
    except Exception:
        return False


def serialize_cookies(cookies: list[dict[str, Any]]) -> str:
    """Serialize cookies for the existing Chaoxing config header field."""
    values_by_name: dict[str, str] = {}
    for cookie in cookies:
        name = cookie["name"]
        value = cookie["value"]
        if (
            not name
            or any(character.isspace() or character in "=;," for character in name)
            or ";" in value
        ):
            raise ValueError("Cookie cannot be represented as a Cookie header")
        previous = values_by_name.get(name)
        if previous is not None and previous != value:
            raise ValueError("Cookie names are ambiguous for the configured page")
        values_by_name[name] = value
    return "; ".join(f"{name}={value}" for name, value in values_by_name.items())
