"""感知层公共契约（M0-4，v0.2.0 精简）。

两条通道实现同一接口，由 ``perception/pipeline.py`` 调度：

============  ==========================================================
``VisionProbe`` 截当前视口，交给模型读题（**唯一的题目通道**）
``MediaProbe``  读 ``<video>`` 三态，**不在题目探针链里**（见 ``run_video``）
============  ==========================================================

v0.2.0 删除了 ``DomProbe`` 与 ``NetProbe``：页面只经模型的眼睛读，
不再解析文档结构、也不再被动抓 XHR 响应。

约定
----
- 探针**只读**，不做任何写入动作（点击 / 输入 / 提交一律归 ``act/``）；
- ``probe()`` **永不抛异常向上冒**：读不到就返回 ``question=None`` + ``warnings``，
  由仲裁层决定「暂停留档 / 继续」。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from core.enums import ProbeName
from core.models import PerceptionResult

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

    from adapters.base import BaseAdapter
    from perception.pipeline import PerceptionContext

__all__ = [
    "BaseProbe",
    "MediaNotAvailableError",
    "PerceptionNotReadyError",
]


class PerceptionNotReadyError(RuntimeError):
    """就绪断言未通过。**不静默继续** —— 由调用方决定复核 / 暂停。"""


class MediaNotAvailableError(RuntimeError):
    """媒体元素不存在或不可读。"""


class BaseProbe(ABC):
    """探针抽象基类。"""

    #: 探针标识，决定它在 ``active_probe_chain()`` 里的位置
    name: ProbeName

    def __init__(self) -> None:
        #: 已挂过监听的页面（``id(page)``），保证 ``attach()`` 幂等
        self._attached_pages: set[int] = set()

    async def attach(self, page: Page) -> None:
        """挂载页面级监听（事件订阅、定时轮询等）。

        默认实现只登记页面；``MediaProbe`` 会覆写。
        **必须幂等**：同一页面重复挂只会生效一次。
        """
        self._attached_pages.add(id(page))

    async def is_available(self, page: Page, adapter: BaseAdapter) -> bool:
        """该通道在当前页面上是否可用（只做判定，不改动页面）。"""
        return True

    @abstractmethod
    async def probe(
        self,
        page: Page,
        adapter: BaseAdapter,
        ctx: PerceptionContext,
    ) -> PerceptionResult:
        """读一次页面，产出结构化结果。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _failed(
        name: ProbeName,
        reason: str,
        *extra: str,
    ) -> PerceptionResult:
        """读不到时的统一返回：``question`` 为空 + 明确原因，**绝不抛异常**。"""
        return PerceptionResult(
            question=None,
            video_state=None,
            channel_used=name,
            warnings=[reason, *extra],
        )
