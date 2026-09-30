"""适配器（M0-3 / M0-7，v0.2.0 精简）。

v0.2.0 变化
-----------
原先本模块的核心是**题目锚点**：五锚点 ``AnchorSet``（题干 / 选项 / 提交 /
结果 / 题型）加一堆扩展选择器（``question_root`` / ``option_text`` / ``frame`` …），
用来让「DOM 通道」把页面结构读成一道题。

程序改成**只使用模型**读页面之后，题目侧不再解析站点文档结构，
那一整套锚点、坑变体（``variants``）与就绪判据（``readiness``）全部删除 ——
连同 ``selectors.yaml`` 一起。模型看的是截图，站点差异不再需要配置。

**留下的是媒体锚点组**：网课任务要判断「在播 / 暂停 / 放到第几秒 / 第几集」，
这些量在页面上只有 ``<video>`` 属性一个可靠来源，因此
``selectors_media.yaml`` 与 :class:`MediaAnchorSet` 原样保留，并成为本模块的主体。

分工原则
--------
- 站点差异 → ``adapters/<site>/selectors_media.yaml``；
- 换一个网站不用改的 → 不该在这里。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Locator, Page

__all__ = [
    "BaseAdapter",
    "InterruptDetection",
    "MediaAnchorSet",
    "MediaAssertions",
]


class MediaAnchorSet(BaseModel):
    """媒体锚点组（M0-3 / M5）。**本模块现在唯一的选择器契约。**"""

    model_config = ConfigDict(extra="forbid")

    video: str
    episode_list: str
    next: str
    interrupt: str
    play_button: str
    progress: str


class MediaAssertions(BaseModel):
    """任务书 M5「进度断言量化」的四条判据。"""

    model_config = ConfigDict(extra="forbid")

    progress_window_s: float = 3.0
    progress_min_delta_s: float = 1.0
    paused_max_delta_s: float = 0.2
    resume_tolerance_s: float = 2.0
    ended_epsilon_s: float = 0.35


class InterruptDetection(BaseModel):
    """弹题探测参数（M0-7）。

    弹题**不是媒体态**（弹窗覆盖不会让 ``paused`` 变真），只能靠文档变更探测，
    故这里给的是 ``MutationObserver`` 与定时轮询两路的节奏。
    """

    model_config = ConfigDict(extra="forbid")

    observer_interval_ms: int = 120
    poll_interval_ms: int = 80


class BaseAdapter(ABC):
    """站点适配器（媒体侧）。题目侧不读文档结构，因此没有题目锚点。"""

    #: 站点标识，与 ``adapters/<site>/`` 目录同名
    site: str = ""

    def __init__(self, media_config: dict[str, Any] | None = None) -> None:
        self.media_config = media_config or {}

        media_anchors = self.media_config.get("media_anchors") or {}
        self.media_anchors = MediaAnchorSet(
            **{key: media_anchors[key] for key in MediaAnchorSet.model_fields}
        )

        #: 媒体扩展选择器（``episode_item`` / ``interrupt_panel`` …）
        self.media_selectors: dict[str, str] = dict(self.media_config.get("selectors") or {})

        #: 属性名约定（值是**属性名**，不是选择器）
        self.media_fields: dict[str, Any] = dict(self.media_config.get("fields") or {})

        self.media_assertions = MediaAssertions(**(self.media_config.get("media_assertions") or {}))
        self.interrupt_detection = InterruptDetection(
            **(self.media_config.get("interrupt_detection") or {})
        )

    # ------------------------------------------------------------------ 装载

    @classmethod
    def from_yaml(cls, media_path: Path) -> Self:
        """从 ``selectors_media.yaml`` 装配适配器。"""
        media_config = yaml.safe_load(Path(media_path).read_text(encoding="utf-8")) or {}
        return cls(media_config)

    # ------------------------------------------------------------------ 判定

    @abstractmethod
    async def matches(self, page: Page) -> bool:
        """当前页面是否属于本站点（只用于挑适配器，不参与读题）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ 取址

    def media_locator(self, page: Page, anchor: str, **kwargs: Any) -> Locator:
        """按媒体锚点名取 :class:`Locator`。"""
        scope = kwargs.pop("scope", None)
        selector = kwargs.pop("selector", None)
        if selector is None:
            value = getattr(self.media_anchors, anchor, None)
            if isinstance(value, str):
                selector = value
            elif anchor in self.media_selectors:
                selector = self.media_selectors[anchor]
            else:
                raise KeyError(f"未定义的媒体锚点：{anchor}")
        root: Any = page if scope is None else scope
        return root.locator(selector)

    # ------------------------------------------------------------------ 小工具

    def field(self, name: str, *, media: bool = False) -> str | None:
        """取一个属性名约定。未登记返回 ``None``（调用方自行给默认值）。

        ``media`` 入参保留（恒为真）是为了让既有调用点不必逐个改签名。
        """
        del media  # v0.2.0 起只剩媒体属性约定
        value = self.media_fields.get(name)
        return str(value) if value is not None else None
