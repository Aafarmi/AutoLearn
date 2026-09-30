"""视觉感知通道（M1-2，v0.2.0 起是**唯一**的读题方式）。

本层**只负责把当前画面截成图**，不做识别 —— 识别归 Tier2 视觉模型
（且必须由用户自行添加「支持视觉」的模型配置后才可用，见任务书 §3.2）。

v0.2.0 的变化
-------------
原先这里只是「DOM 读不出来时的兜底」，前面还挂着一整条 DOM / 网络通道。
现在页面**只经模型的眼睛读**，于是：

- 不再按题目锚点裁切 —— 锚点是站点文档结构的约定，程序已经不解析它了；
- 一律截**当前视口**（``scale="css"``，图像像素 == 视口 CSS 像素），
  模型给的归一化框乘上图像尺寸就能直接喂给 ``page.mouse.click``；
- 坐标换算只剩一层，``devicePixelRatio`` 那一层被刻意省掉 ——
  它正是「在缩放显示器上必然偏一半」的根源。

截图纪律（全局硬约束，有守门单测卡住）
--------------------------------------
1. **一律视口截图，禁用全页截图**（``full_page`` 在本仓库里连参数名都不出现）；
2. 超时重试是**必需**而不是保险：真实站点（学习通、网课平台那类）页面上有
   持续动画、大图、第三方脚本，第一次 ``screenshot`` 常常直接挂到超时，
   而紧接着的第二次请求 1 秒内就返回。只截一次会把「本次刚好忙」
   误判成「视觉不可用」。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from core.enums import ProbeName
from core.models import PerceptionResult
from perception.base import BaseProbe

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

    from adapters.base import BaseAdapter
    from perception.pipeline import PerceptionContext

__all__ = ["VisionProbe"]

logger = logging.getLogger(__name__)

#: 整屏截图的单次超时与重试次数。
_VIEWPORT_TIMEOUT_MS = 12_000
_VIEWPORT_ATTEMPTS = 3


class VisionProbe(BaseProbe):
    """视觉通道探针。产出当前视口截图（PNG 字节），交给 Tier2 读题。"""

    name = ProbeName.VISION

    async def is_available(self, page: Page, adapter: BaseAdapter) -> bool:
        """判据只有「有 body 可截」——不看任何题目锚点。

        这一条踩过坑：早期实现去查题目锚点来决定「题目在哪」，锚点不在就弃权。
        可那套锚点是自建靶场的约定，真实站点一个都没有 —— 于是视觉通道在真实
        网页上**永远不可用**，把「唯一能读真实页面的通道」挡在门外。
        v0.2.0 已经没有别的通道可退，这个判据更不能再收紧。
        """
        del adapter
        try:
            return await page.locator("body").count() > 0
        except Exception:
            return False

    # ------------------------------------------------------------------ 截图

    async def crop_question(self, page: Page) -> bytes:
        """截当前视口，返回 PNG 字节。

        名字保留 ``crop_question`` 是为了让调用点不必改；实际语义已经是
        「把整个视口交给模型，让它自己找题目」。
        """
        return await self.shot_viewport(page)

    @staticmethod
    async def shot_viewport(page: Page) -> bytes:
        """整屏视口截图（带重试）。

        ``scale="css"`` 是刻意的：图像像素 == 视口 CSS 像素，于是模型给的
        归一化框乘上图像尺寸，就能直接喂给 ``page.mouse.click`` ——
        省掉 ``devicePixelRatio`` 那一层换算（那是「在某个显示缩放下必然偏一半」的根源）。
        """
        last: Exception | None = None
        for attempt in range(_VIEWPORT_ATTEMPTS):
            try:
                return await page.screenshot(type="png", scale="css", timeout=_VIEWPORT_TIMEOUT_MS)
            except Exception as exc:  # 超时是常态，不是异常路径
                last = exc
                logger.info(
                    "整屏截图第 %d/%d 次失败，重试：%s", attempt + 1, _VIEWPORT_ATTEMPTS, exc
                )
        raise RuntimeError(f"vision:viewport_shot_failed: {last}")

    # ------------------------------------------------------------------ 探针

    async def probe(
        self,
        page: Page,
        adapter: BaseAdapter,
        ctx: PerceptionContext,
    ) -> PerceptionResult:
        del adapter
        try:
            png = await self.crop_question(page)
        except Exception as exc:
            return self._failed(self.name, "vision:crop_failed", str(exc))

        ref: str | None = None
        if ctx.screenshot_dir is not None:
            directory = Path(ctx.screenshot_dir)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{ctx.item_id or 'item'}__vision.png"
            target.write_bytes(png)
            ref = str(target)

        #: 只保留两个**仍然有意义**的标记：截到图（``crop_ok``）与图像字节数。
        #: 旧标记 ``vision:crop_only`` / ``vision:viewport`` / ``vision:requires_model_config``
        #: 描述的是「DOM 通道 / 需自行配模型」那些已删除的功能，一并清除。
        warnings = ["vision:crop_ok", f"vision:bytes={len(png)}"]

        return PerceptionResult(
            question=None,
            video_state=None,
            channel_used=self.name,
            warnings=warnings,
            screenshot_ref=ref,
        )
