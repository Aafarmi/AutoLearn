"""Zhihuishu authentication settings for the standalone cookie manager.

The module mirrors the attribute contract that ``core.auth.cookie_manager``
expects from ``core.adapters.<site>.auth`` (see ``_load_adapter``): seven
required names plus the optional ``CAPTURE_START_URL`` / ``CAPTURE_HINT``.
It performs no I/O, imports no browser machinery and never writes config.json;
the cookie manager owns persistence.
"""

from __future__ import annotations

from typing import Any


SITE_ID = "zhihuishu"
CONFIG_SECTION = "zhihuishu"
URL_FIELD = "url"
COOKIE_FIELD = "cookie"
COOKIE_VALUE_TYPE = str
PROFILE_NAME = "zhihuishu"
ALLOWED_COOKIE_DOMAINS = ("zhihuishu.com",)
CAPTURE_START_URL = "https://www.zhihuishu.com/"
CAPTURE_HINT = (
    "请使用智慧树 App 扫描页面中的二维码登录。"
)

#: Login cookie required by the upstream ``Fucker.cookies`` setter (§4.6 #9).
LOGIN_COOKIE_NAME = "CASLOGC"


def _page_cookies(page: Any) -> list[Any]:
    """Return the browser context cookie list, or an empty list on any failure."""
    try:
        cookies = page.context.cookies()
    except Exception:
        return []
    return cookies if isinstance(cookies, list) else []


def is_authenticated(page: Any) -> bool:
    """Detect the Zhihuishu login state from the browser context cookies.

    The upstream crawler needs ``CASLOGC`` with a non-empty value, so the
    presence of that cookie is the only accepted signal. A missing page,
    a broken context or a non-cookie payload yields ``False`` rather than
    an exception: callers poll this while the user is still logging in.
    """
    for cookie in _page_cookies(page):
        if not isinstance(cookie, dict):
            continue
        if cookie.get("name") != LOGIN_COOKIE_NAME:
            continue
        value = cookie.get("value")
        if isinstance(value, str) and value.strip():
            return True
    return False


def serialize_cookies(cookies: list[dict[str, Any]]) -> str:
    """Serialize cookies into the ``Cookie`` header value stored for the site.

    Mirrors the Chaoxing adapter: ``name=value`` pairs joined by ``"; "``,
    duplicates with conflicting values rejected, and any cookie that cannot
    survive a round-trip through the header format rejected with ``ValueError``.
    """
    values_by_name: dict[str, str] = {}
    for cookie in cookies:
        name = cookie["name"]
        value = cookie["value"]
        if (
            not name
            or any(character.isspace() or character in "=;," for character in name)
            or not isinstance(value, str)
            or ";" in value
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError("Cookie cannot be represented as a Cookie header")
        previous = values_by_name.get(name)
        if previous is not None and previous != value:
            raise ValueError("Cookie names are ambiguous for the configured page")
        values_by_name[name] = value
    return "; ".join(f"{name}={value}" for name, value in values_by_name.items())
