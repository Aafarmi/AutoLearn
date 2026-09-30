"""感知流水线（M0-4，v0.2.0 精简）。

两条流程，**互不干扰**：

题目流程
    :meth:`PerceptionPipeline.run` 跑**唯一**的视觉通道（截当前视口），
    把结果交给 :func:`core.arbiter.arbitrate`：
    「拿到了图」和「图里是什么」是两件事，后者归 Tier2 视觉模型的读题步骤
    （见 ``solve/reader.py``）。
视频流程
    :meth:`PerceptionPipeline.run_video` 只走 ``MediaProbe``。网课场景里弹题是
    嵌套中断，题目链会在中断时被单独调用一次。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from core.arbiter import arbitrate
from core.config import RunConfig, active_probe_chain
from core.enums import ProbeName
from core.models import PerceptionResult, VideoState
from perception.base import BaseProbe, MediaNotAvailableError
from perception.media_probe import MediaProbe

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

    from adapters.base import BaseAdapter

__all__ = ["PerceptionContext", "PerceptionPipeline"]

logger = logging.getLogger(__name__)


class PerceptionContext(BaseModel):
    """一次感知调用的上下文。由编排层构造并透传给探针。

    字段全部有默认值，**便于单测直接构造**；编排层仍应显式传
    ``item_id`` / ``run_id`` / ``cfg``（留痕与限速都依赖它们）。
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    item_id: str = ""
    run_id: str = ""
    cfg: RunConfig = Field(default_factory=RunConfig)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    log_root: Path = Path("logs")
    warnings: list[str] = Field(default_factory=list)
    #: 上一次感知到的媒体状态，供「进度是否推进」类判断做对照
    last_video_state: VideoState | None = None

    #: 单条通道的等待预算（秒）。探针不得各自硬编码超时
    timeout_s: float = 8.0
    #: 落图目录；为空则只把 PNG 留在内存（``screenshot_ref`` 为 None）
    screenshot_dir: Path | None = None


class PerceptionPipeline:
    """调度探针。题目链只有视觉一条，媒体探针独立调度。"""

    def __init__(self, probes: list[BaseProbe], cfg: RunConfig) -> None:
        self._cfg = cfg
        self._probes: dict[ProbeName, BaseProbe] = {probe.name: probe for probe in probes}
        # 媒体探针不在题目链里，但流水线要负责它（没给就补一个）
        if ProbeName.MEDIA not in self._probes:
            self._probes[ProbeName.MEDIA] = MediaProbe()

    def chain(self) -> list[ProbeName]:
        """当前生效的题目探针顺序（v0.2.0 恒为 ``[vision]``）。

        只在**已装配的探针**里取 —— 未注入的通道不应被展开进链路。
        """
        return [name for name in active_probe_chain(self._cfg) if name in self._probes]

    def probe(self, name: ProbeName) -> BaseProbe | None:
        """取一个已装配的探针。"""
        return self._probes.get(name)

    # ------------------------------------------------------------------ 题目

    async def run(
        self,
        page: Page,
        adapter: BaseAdapter,
        ctx: PerceptionContext,
    ) -> PerceptionResult:
        """跑一遍题目感知链并仲裁。

        走「``is_available`` 门控 → ``probe``」，任何异常都在此收口成一条
        ``*:probe_error`` 结果 —— 探针异常**不得**中断整条链。
        """
        results: list[PerceptionResult] = []

        for name in self.chain():
            probe = self._probes[name]
            await probe.attach(page)
            if not await probe.is_available(page, adapter):
                results.append(BaseProbe._failed(name, f"{name.value}:unavailable"))
                continue
            try:
                results.append(await probe.probe(page, adapter, ctx))
            except Exception as exc:  # 探针异常必须收口，不能冒到编排层
                logger.warning("探针 %s 抛出异常：%s", name.value, exc)
                results.append(BaseProbe._failed(name, f"{name.value}:probe_error", str(exc)))

        return arbitrate(results, self._cfg)

    # ------------------------------------------------------------------ 视频

    async def run_video(
        self,
        page: Page,
        adapter: BaseAdapter,
        ctx: PerceptionContext,
    ) -> VideoState:
        """读一帧媒体态。媒体元素缺失时抛 :class:`MediaNotAvailableError`。"""
        probe = self._probes[ProbeName.MEDIA]
        if isinstance(probe, MediaProbe):
            probe.bind(adapter)
        await probe.attach(page)
        result = await probe.probe(page, adapter, ctx)
        if result.video_state is None:
            raise MediaNotAvailableError(";".join(result.warnings) or "media:unavailable")
        return result.video_state

