"""P6 执行层测试辅助：内存替身 + 合成截图 + 靶场几何。

为什么要有这一层
----------------
真浏览器用例（走 conftest 的 ``page`` / ``adapter`` 夹具）在靶场没起时会 ``skip``，
而执行层里**最该天天跑**的那几条 —— 坐标换算、重放次数、到顶留档、
媒体阶梯顺序 —— 全都与浏览器无关。把它们写成纯内存用例，
才不会因为「今天没起靶场」而默默不跑。

替身只实现被真正用到的那几个方法（``count`` / ``first`` / ``click`` /
``focus`` / ``bounding_box`` / ``evaluate`` / ``screenshot``），
**不是 Playwright 的模拟器**，方法签名以调用点为准，用不到的一概不写。

v0.2.0：题目侧改成按坐标点 + 像素差分，所以这里多了两样东西 ——
**可按帧出图的 ``FakePage``**（模拟「点前 / 点后」两帧截图）与
**合成 PNG**（``synthetic_png``，让差分的输入完全确定）。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import time
from typing import Any

from PIL import Image, ImageDraw
from playwright.async_api import Page

from tests.helpers import wait_question_attached

__all__ = [
    "FakeKeyboard",
    "FakeLocator",
    "FakeMouse",
    "FakePage",
    "RecordingBus",
    "correct_option_input",
    "correct_option_region",
    "correct_option_row",
    "element_region",
    "option_input",
    "quiz_scope",
    "synthetic_png",
    "viewport_size",
    "wait_options",
]


# --------------------------------------------------------------------------- #
# 内存替身
# --------------------------------------------------------------------------- #
class FakeMouse:
    """鼠标替身。``click_error`` 用来演「输入事件根本没发出去」。"""

    def __init__(self, *, click_error: Exception | None = None) -> None:
        self.clicks: list[tuple[float, float]] = []
        self.click_error = click_error

    async def click(self, x: float, y: float, **kwargs: Any) -> None:
        if self.click_error is not None:
            raise self.click_error
        self.clicks.append((float(x), float(y)))


class FakeKeyboard:
    def __init__(self) -> None:
        self.presses: list[str] = []

    async def press(self, key: str, **kwargs: Any) -> None:
        self.presses.append(str(key))


class RecordingBus:
    """只记不发的 ``EventBus`` 替身（断言事件用）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def payloads(self, event: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.events if name == event]


class FakeLocator:
    """最小 Locator 替身（媒体阶梯的 ``_attempt`` 在用）。

    ``on_click`` 用来模拟「点完页面变了」这类副作用。
    """

    def __init__(
        self,
        *,
        selector: str = "fake",
        state: dict[str, str | None] | None = None,
        click_error: Exception | None = None,
        box: dict[str, float] | None = None,
        tag: str = "button",
        count: int = 1,
        text: str = "",
        attrs: dict[str, str] | None = None,
        on_click: Any = None,
    ) -> None:
        self._selector = selector
        self.state: dict[str, str | None] = dict(state or {})
        self.click_error = click_error
        self.box = box
        self._tag = tag
        self._count = count
        self._text = text
        self._attrs = dict(attrs or {})
        self._on_click = on_click
        #: 等可见时抛的异常（模拟「元素一直不出现」的路径）
        self.wait_error: Exception | None = None
        self.clicks = 0
        self.forced_clicks = 0
        self.focuses = 0
        self.scrolls = 0
        self.bbox_calls = 0
        self.waits = 0

    async def count(self) -> int:
        return self._count

    async def wait_for(self, *, state: str = "visible", timeout: float | None = None) -> None:
        """``Locator.wait_for``。等不到就抛 —— 调用方据此判超时。"""
        self.waits += 1
        if self.wait_error is not None:
            raise self.wait_error

    @property
    def first(self) -> FakeLocator:
        return self

    async def click(self, **kwargs: Any) -> None:
        if self.click_error is not None:
            raise self.click_error
        self.clicks += 1
        if kwargs.get("force"):
            self.forced_clicks += 1
        if self._on_click is not None:
            self._on_click()

    async def focus(self, **kwargs: Any) -> None:
        self.focuses += 1

    async def scroll_into_view_if_needed(self, **kwargs: Any) -> None:
        self.scrolls += 1

    async def bounding_box(self, **kwargs: Any) -> dict[str, float] | None:
        self.bbox_calls += 1
        return self.box

    async def is_visible(self) -> bool:
        return self._count > 0

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "matches('input')" in script:  # act.actuator._activation_key
            return self._tag
        return None


class FakePage:
    """最小 Page 替身。

    ``video_frames`` 是按调用顺序出栈的媒体态脚本（用完后一直返回最后一帧），
    用来把「3s 窗口内 ΔcurrentTime」这类断言测成确定性的。

    ``screenshot_frames`` 同理，是**逐帧出图**的截图脚本（用完后重复最后一帧）：
    像素差分要的正是「点前一帧、点后一帧」这种序列。
    """

    def __init__(
        self,
        *,
        dpr: float = 1.0,
        video_frames: list[dict[str, Any]] | None = None,
        body_attrs: dict[str, str] | None = None,
        locator: FakeLocator | None = None,
        selector_error: Exception | None = None,
        screenshot_error: Exception | None = None,
        screenshot_frames: list[bytes] | None = None,
        click_error: Exception | None = None,
        viewport_size: dict[str, int] | None = None,
    ) -> None:
        self.mouse = FakeMouse(click_error=click_error)
        self.keyboard = FakeKeyboard()
        self.dpr = dpr
        self.body_attrs = dict(body_attrs or {})
        self.viewport_size = viewport_size
        self.screenshot_calls = 0
        self.screenshots: list[bytes] = []
        self._frames = list(video_frames or [])
        self._frame_index = 0
        self._shots = list(screenshot_frames or [])
        self._shot_index = 0
        self._locator = locator if locator is not None else FakeLocator()
        self._selector_error = selector_error
        self._screenshot_error = screenshot_error
        # 「等某个元素出现」这类调用挂在 locator 上（``page.wait_for_selector`` 只留兜底）
        self._locator.wait_error = selector_error

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "currentSrc" in script:  # perception.media_probe.read_video_state
            return self._next_frame()
        if "document.body" in script:  # read_episode_index 的 body 属性
            return self.body_attrs.get(str(arg))
        if "devicePixelRatio" in script:
            return self.dpr
        if "scrollX" in script:
            return [0, 0]
        if "scrollTo" in script:
            return None
        return True  # 媒体脚本路径（play / pause / seek）

    def _next_frame(self) -> dict[str, Any]:
        if not self._frames:
            return {"paused": True, "ended": False, "current_time": 0.0, "duration": 8.0, "src": "x"}
        frame = self._frames[min(self._frame_index, len(self._frames) - 1)]
        self._frame_index += 1
        return frame

    def locator(self, selector: str, **kwargs: Any) -> FakeLocator:
        return self._locator

    async def wait_for_selector(self, selector: str, **kwargs: Any) -> None:
        if self._selector_error is not None:
            raise self._selector_error
        return

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.screenshot_calls += 1
        if self._screenshot_error is not None:
            raise self._screenshot_error
        if not self._shots:
            return b"\x89PNG\r\n\x1a\nfake"
        frame = self._shots[min(self._shot_index, len(self._shots) - 1)]
        self._shot_index += 1
        self.screenshots.append(frame)
        return frame


# --------------------------------------------------------------------------- #
# 合成截图（像素差分的输入）
# --------------------------------------------------------------------------- #
def synthetic_png(
    size: tuple[int, int],
    *,
    color: int = 255,
    patch: tuple[float, float, float, float] | None = None,
    patch_color: int = 0,
) -> bytes:
    """造一张灰度合成 PNG：整张 ``color``，可再往 ``patch``（归一化框）涂一块。

    像素差分要的是**完全确定的输入** —— 真截图的噪声与视图差异会把
    「阈值判定」测成玄学。所以阈值边界用合成图钉，真页面只负责验
    「这条链路在真浏览器上确实合得上」（见 ``test_actuator.py`` 的 e2e 用例）。
    """
    width, height = int(size[0]), int(size[1])
    image = Image.new("L", (width, height), color=int(color))
    if patch is not None:
        left = round(patch[0] * width)
        top = round(patch[1] * height)
        right = round((patch[0] + patch[2]) * width)
        bottom = round((patch[1] + patch[3]) * height)
        ImageDraw.Draw(image).rectangle([left, top, max(left, right - 1), max(top, bottom - 1)],
                                        fill=int(patch_color))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 靶场定位器（真浏览器用；靶场不可达时上游夹具会 skip）
# --------------------------------------------------------------------------- #
async def quiz_scope(page: Page, timeout_ms: int = 8000) -> Any:
    """题目所在的查找根：主文档有题目就用 ``page``，否则切进跨域 frame。"""
    await wait_question_attached(page, timeout_ms)
    if await page.locator('[data-quiz="question"]').count() > 0:
        return page
    if await page.locator('[data-quiz="frame"]').count() > 0:
        return page.frame_locator('[data-quiz="frame"]')
    return page


async def wait_options(scope: Any, timeout_ms: int = 8000) -> int:
    """等选项被填充出来。

    懒加载坑要先把选项区滚进视口才会填充，所以这里一边滚一边等，
    而不是直接断言 —— 断言失败会变成「偶发失败」，最难查。
    """
    rows = scope.locator('[data-quiz="option"]')
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if await rows.count() >= 2:
            break
        with contextlib.suppress(Exception):
            await scope.locator('[data-quiz="options"]').first.scroll_into_view_if_needed(timeout=1000)
        await asyncio.sleep(0.08)
    return await rows.count()


async def option_input(scope: Any, index: int = 0) -> Any:
    """取第 ``index`` 个选项的 ``<input>``（判「真的选中了」就看它的 ``checked``）。"""
    return scope.locator('[data-quiz="option"]').nth(index).locator('[data-quiz="input"]')


async def correct_option_row(scope: Any) -> Any:
    """按地面真值取「正确选项」那一整行。

    .. warning::
       **只允许测试判分用。** 生产链路读 ``data-answer`` 会让 M2 闸门数字全假
       （由 ``tests/test_guardrails.py`` 卡住）。
    """
    root = scope.locator('[data-quiz="question"]').first
    answer = (await root.get_attribute("data-answer")) or ""
    label = answer.split(",")[0].strip()
    return scope.locator(f'[data-quiz="option"][data-label="{label}"]').first


async def correct_option_input(scope: Any) -> Any:
    """按地面真值取「正确选项」的 ``<input>``（判分与真浏览器用例用）。"""
    row = await correct_option_row(scope)
    return row.locator('[data-quiz="input"]')


async def correct_option_region(
    page: Page, scope: Any
) -> tuple[tuple[float, float, float, float], tuple[int, int]]:
    """正确选项行的 ``(归一化框, 截图像素尺寸)`` —— 给坐标点击的 e2e 用例用。"""
    return await element_region(page, await correct_option_row(scope))


async def viewport_size(page: Page) -> tuple[int, int]:
    """截图像素尺寸（``scale="css"`` 下等于视口 CSS 像素）。

    CDP 附加来的页面 ``page.viewport_size`` 常是 ``None``，所以回退到页内读 ——
    与 ``Actuator._viewport_size`` 同一条口径。
    """
    raw = getattr(page, "viewport_size", None)
    if not isinstance(raw, dict):
        raw = await page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
    return int(raw["width"]), int(raw["height"])


async def element_region(page: Page, locator: Any) -> tuple[tuple[float, float, float, float], tuple[int, int]]:
    """元素 → ``(归一化框, 截图像素尺寸)``。

    **这是测试里唯一的「作弊」点**：生产里的框来自视觉模型，测试里只能从页面
    几何反推。它验的是「执行层照坐标点、按像素判」这件事，不是模型读得准不准。
    """
    bbox = await locator.bounding_box()
    assert bbox is not None, "元素没有 bounding_box，量不出区域"
    size = await viewport_size(page)
    box = (
        float(bbox["x"]) / size[0],
        float(bbox["y"]) / size[1],
        float(bbox["width"]) / size[0],
        float(bbox["height"]) / size[1],
    )
    return box, size
