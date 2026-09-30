"""P3 视觉通道验收（真跑靶场）。

v0.2.0 起这是**唯一**的读题通道，它只负责把当前画面截出来。
本文件钉住三件事：

1. 一律**视口截图**：图像像素 == 视口 CSS 像素（``scale="css"``），
   于是模型给的归一化框乘上图像尺寸就能直接喂给 ``page.mouse.click``；
2. **全局禁 ``full_page=True``**（截整页会让坐标与视口脱钩）；
3. 没有靶场锚点也必须出手 —— 真实站点上一个锚点都没有。
"""

from __future__ import annotations

import io

from PIL import Image

from perception.pipeline import PerceptionContext
from perception.vision_probe import VisionProbe
from tests.helpers import Q_BASE, open_quiz, quiz_url

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
VIEWPORT = (1280, 720)


async def test_crop_covers_the_whole_viewport(page, mock_base, adapter) -> None:
    """截的就是当前视口 —— 不再按题目锚点裁切。"""
    await open_quiz(page, quiz_url(mock_base, Q_BASE))

    png = await VisionProbe().crop_question(page)

    assert png.startswith(PNG_MAGIC)
    image = Image.open(io.BytesIO(png))
    assert (image.width, image.height) == VIEWPORT, (
        "scale='css' 下图像像素必须等于视口 CSS 像素，否则坐标换算要再乘一次 DPR"
    )


async def test_crop_works_without_any_page_anchor(page, mock_base, adapter) -> None:
    """**真实站点形态**：没有任何靶场锚点，照样必须截得出来。

    这条行为是 P11 修正的：原先没有题目锚点就抛 ``crop_failed``，
    而 ``is_available`` 又去查同一个锚点 —— 于是真实网页上视觉通道**永远不出手**。
    v0.2.0 已经没有别的通道可退，这条更不能再收紧。
    """
    await page.set_content("<html><body><h1>第 1 题</h1><p>没有锚点的题</p></body></html>")

    png = await VisionProbe().crop_question(page)

    assert png.startswith(PNG_MAGIC)
    image = Image.open(io.BytesIO(png))
    assert (image.width, image.height) == VIEWPORT


async def test_probe_saves_crop_and_declares_model_requirement(
    page, mock_base, adapter, tmp_path
) -> None:
    """截图落盘 + 明确声明「读题需要用户自行添加的视觉模型」。"""
    await open_quiz(page, quiz_url(mock_base, Q_BASE))
    ctx = PerceptionContext(item_id="item-1", screenshot_dir=tmp_path)
    result = await VisionProbe().probe(page, adapter, ctx)

    assert result.question is None, "本层只出图，识别归 Tier2"
    assert result.channel_used.value == "vision"
    assert "vision:crop_ok" in result.warnings
    assert result.screenshot_ref is not None

    saved = tmp_path / "item-1__vision.png"
    assert saved.exists()
    assert saved.read_bytes().startswith(PNG_MAGIC)


async def test_crop_failure_is_reported_not_swallowed(adapter) -> None:
    """截不出图必须如实报 ``crop_failed``，由仲裁层停下 —— 不许静默变成「没题」。"""

    class _BrokenPage:
        async def screenshot(self, **kwargs):
            raise RuntimeError("boom")

    result = await VisionProbe().probe(_BrokenPage(), adapter, PerceptionContext())

    assert result.question is None
    assert any("vision:crop_failed" in warning for warning in result.warnings)


async def test_is_available_does_not_depend_on_anchors(page, mock_base, adapter) -> None:
    """**回归守卫**：``is_available`` 不许去查题目锚点。

    判据只能是「有可截的画面」。
    """
    probe = VisionProbe()
    await page.set_content("<html><body><p>一个没有 data-quiz 锚点的页面</p></body></html>")
    assert await probe.is_available(page, adapter) is True, "无锚点不等于视觉不可用"
    await open_quiz(page, quiz_url(mock_base, Q_BASE))
    assert await probe.is_available(page, adapter) is True
