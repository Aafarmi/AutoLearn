"""长江雨课堂 Cookie Manager 配置。"""

from __future__ import annotations

from typing import Any


SITE_ID = "cjyuketang"
CONFIG_SECTION = "cjyuketang"
URL_FIELD = "url"
COOKIE_FIELD = "cookie"
COOKIE_VALUE_TYPE = str
PROFILE_NAME = "cjyuketang"
ALLOWED_COOKIE_DOMAINS = ("yuketang.cn",)


def is_authenticated(page: Any) -> bool:
    """只将包含雨课堂题目结构的页面视为已登录的目标页。"""
    try:
        items = page.locator(".exercise-item")
        count = items.count()
        return count > 0 and items.locator(".item-type").count() == count
    except Exception:
        return False


def serialize_cookies(cookies: list[dict[str, Any]]) -> str:
    """以 Cookie 请求头格式写回本地配置，不输出值。"""
    values_by_name: dict[str, str] = {}
    for cookie in cookies:
        name = cookie["name"]
        value = cookie["value"]
        if (
            not name
            or any(character.isspace() or character in "=;," for character in name)
            or any(character in value for character in "\r\n;")
        ):
            raise ValueError("Cookie cannot be represented as a Cookie header")
        previous = values_by_name.get(name)
        if previous is not None and previous != value:
            raise ValueError("Cookie names are ambiguous for the configured page")
        values_by_name[name] = value
    if not values_by_name:
        raise ValueError("No cookies are available")
    return "; ".join(f"{name}={value}" for name, value in values_by_name.items())
