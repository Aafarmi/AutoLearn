"""测试辅助：靶场 URL 构造、题库读取、页面就绪等待。

题库读取只用于**测试判分**（拿期望的坑位分布与题干做对照），
生产链路里的通道一律不得读地面真值。
"""

from __future__ import annotations

import json
import urllib.request
from contextlib import suppress
from typing import Any

from playwright.async_api import Page

#: 绕开环境代理的 opener。
#:
#: 开发机上常有 ``HTTP_PROXY``/``HTTPS_PROXY``，而 ``urlopen`` 默认把
#: ``127.0.0.1`` 的请求也交给代理 —— 取题库会失败。**必须绕开**：
#: 靶场是本机服务，永远不该走代理。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 靶场题目序号 → 坑（靶场 static/traps.md §4）
Q_BASE = 1
Q_SPA = 21
Q_LAZY = 24
Q_CANVAS = 27
Q_IFRAME = 30
Q_CLS = 33
Q_MODAL = 36
Q_XHR = 39
Q_COMPOSITE_SPA_CLS = 40
Q_COMPOSITE_IFRAME_SPA = 46
Q_COMPOSITE_ALL = 50  # iframe + canvas + xhr 三坑叠加


def quiz_url(mock_base: str, seq: int | str | None = None, seed: int | None = None) -> str:
    params: list[str] = []
    if seq is not None:
        params.append(f"seq={seq}")
    if seed is not None:
        params.append(f"seed={seed}")
    return f"{mock_base}/quiz.html" + ("?" + "&".join(params) if params else "")


def course_url(mock_base: str, **params: Any) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    return f"{mock_base}/course.html" + (f"?{query}" if query else "")


def fetch_questions(mock_base: str) -> list[dict]:
    """取靶场题库（测试判分用）。"""
    with _NO_PROXY_OPENER.open(f"{mock_base}/static/questions.json", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))["questions"]


def fetch_course(mock_base: str) -> dict:
    with _NO_PROXY_OPENER.open(f"{mock_base}/static/course.json", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def open_quiz(page: Page, url: str, *, timeout_ms: int = 15000) -> None:
    """打开题目页并等到第一题「挂上了」（不保证内容就绪 —— 那正是被测点）。"""
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)


async def wait_question_attached(page: Page, timeout_ms: int = 8000) -> bool:
    """等题目根出现（主文档或 frame 内）。SPA 题需要的等待量。"""
    selector = '[data-quiz="question"], [data-quiz="frame"]'
    try:
        await page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def click_next(page: Page, *, timeout_ms: int = 8000, scroll_steps: int = 8) -> None:
    """点「下一题」并等下一题挂上。

    iframe 题的按钮在跨域 frame 内（点它靠 postMessage 回传给主文档）。

    **必须能处理「下一题要滚动才出现」**（坑 ``next_after_scroll``）：
    那种按钮初始 ``display:none``，而 Playwright 的 ``click()`` 只会把**可见**元素
    滚进视口 —— 对 ``display:none`` 等多久都不会变可见。所以先**有界地向下滚**，
    让页面自己的 JS 把它放出来（真实站点上完全同一件事）。

    点完把滚动位置复位：每个题都从页面顶部开始，用例才是确定的 ——
    否则上一题留下的滚动位置会让下一题的条件随机化，而 ``next_after_scroll``
    恰恰是靠滚动位置生效的。
    """
    button = await next_button(page)
    if not await _is_visible(button):
        await _scroll_until_visible(page, button, scroll_steps)
    await button.click(timeout=timeout_ms)
    await page.wait_for_timeout(120)
    await _scroll_to_top(page)


async def _is_visible(locator) -> bool:
    try:
        return await locator.is_visible()
    except Exception:
        return False


async def _scroll_until_visible(page: Page, locator, steps: int) -> None:
    """有界地向下滚，直到目标可见。**有界**是硬要求 —— 否则就是死循环。"""
    for _ in range(max(0, steps)):
        if await _is_visible(locator):
            return
        try:
            await page.evaluate("(dy) => window.scrollBy(0, dy)", 700)
        except Exception:
            return
        await page.wait_for_timeout(60)


async def _scroll_to_top(page: Page) -> None:
    with suppress(Exception):
        await page.evaluate("() => window.scrollTo(0, 0)")


async def next_button(page: Page):
    """「下一题」按钮：主文档没有就进 frame 找。"""
    main = page.locator('[data-quiz="next"]')
    if await main.count() > 0:
        return main.first
    return page.frame_locator('[data-quiz="frame"]').locator('[data-quiz="next"]').first


async def dismiss_modal_if_any(page: Page, timeout_ms: int = 4000) -> None:
    """等遮罩自己消失（坑 modal 会在 1.2~1.8s 后自动移除）。"""
    from contextlib import suppress

    with suppress(Exception):
        await page.wait_for_selector('[data-quiz="modal"]', state="detached", timeout=timeout_ms)


def fast_adapter(adapter, ready_timeout_ms: int = 200):
    """做一份「就绪等待极短」的适配器副本。

    v0.2.0：适配器只剩媒体侧，题目侧的 ``readiness``（就绪判据）已随 DOM
    通道一起删除，所以这里只能改媒体配置 —— 保留函数是为了不让调用点全改。
    """
    del ready_timeout_ms
    return type(adapter)(adapter.media_config)


async def frame_locator_of(page: Page):
    """跨域 frame 的 FrameLocator。"""
    return page.frame_locator('[data-quiz="frame"]')


def qid_of(question) -> str:
    return question.qid


def trap_counts(questions: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in questions:
        for trap in item.get("traps") or []:
            counts[trap] = counts.get(trap, 0) + 1
    return counts
