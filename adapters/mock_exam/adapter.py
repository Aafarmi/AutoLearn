"""靶场适配器（M0-3）。

``site = "mock_exam"``。媒体锚点全部来自同目录的 ``selectors_media.yaml``，
本文件里**不得出现任何选择器字面量** —— 换靶场只改 YAML。

题目侧不再需要锚点：程序只使用模型读页面，模型看的是截图。
"""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import Page

from adapters.base import BaseAdapter

__all__ = ["SELECTORS_MEDIA_YAML", "MockExamAdapter", "load_adapter"]

HERE = Path(__file__).resolve().parent

#: 媒体锚点 YAML（本模块唯一的选择器来源）
SELECTORS_MEDIA_YAML = HERE / "selectors_media.yaml"


class MockExamAdapter(BaseAdapter):
    """``mock_site/`` 两个靶场页（``quiz.html`` / ``course.html``）的适配器。"""

    site = "mock_exam"

    @classmethod
    def load(cls, media: Path | None = None) -> MockExamAdapter:
        """装载默认靶场适配器；可显式传入媒体 YAML 路径。"""
        return cls.from_yaml(media or SELECTORS_MEDIA_YAML)

    async def matches(self, page: Page) -> bool:
        """``body[data-site]`` 是否等于本适配器的 ``site``。

        属性名从 YAML 取（``fields.body_site_attr``），代码不写死 ``data-site``。
        """
        attr = self.field("body_site_attr") or "data-site"
        try:
            value = await page.evaluate(
                "(name) => (document.body ? document.body.getAttribute(name) : null)",
                attr,
            )
        except Exception:  # pragma: no cover - 页面尚未有 body / 已关闭
            return False
        return value == self.site


def load_adapter() -> MockExamAdapter:
    """默认装载靶场适配器（脚本、测试都走这里，避免各处自拼路径）。"""
    return MockExamAdapter.load()
