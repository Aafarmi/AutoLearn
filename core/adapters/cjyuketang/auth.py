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

# 任务 URL 保持不变，只把「抓 Cookie 时浏览器首先打开的页面」定到雨课堂首页：
# 直接打开考试/作业深链经常在未登录时被拦截或落到异常页，先到首页登录再手动进入更稳。
CAPTURE_START_URL = "https://changjiang.yuketang.cn/web"
CAPTURE_HINT = (
    "浏览器会先打开雨课堂首页。请在该浏览器里完成登录，然后手动点击进入任务里的作业 / 考试页面；"
    "识别到题目页面后会自动保存 Cookie，请不要关闭浏览器直到本页提示成功。"
)


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
