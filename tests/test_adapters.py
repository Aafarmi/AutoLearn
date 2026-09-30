"""M0-3 适配器单测（v0.2.0 精简）。

题目侧不再解析站点文档结构，所以适配器**只剩媒体锚点组**：
``selectors_media.yaml`` 驱动的媒体锚点、属性名约定与进度断言。
本文件负责证明「换站点只改 YAML」这条约定仍然成立，不依赖浏览器。
"""

from __future__ import annotations

import pytest

from adapters.base import MediaAnchorSet
from adapters.mock_exam.adapter import MockExamAdapter


def test_media_anchor_group_is_complete(adapter: MockExamAdapter) -> None:
    """六个媒体锚点一个都不能少 —— 少了哪个，对应的媒体动作就没法定位。"""
    assert set(MediaAnchorSet.model_fields) == {
        "video",
        "episode_list",
        "next",
        "interrupt",
        "play_button",
        "progress",
    }
    for field in MediaAnchorSet.model_fields:
        assert getattr(adapter.media_anchors, field)


def test_question_anchors_are_gone(adapter: MockExamAdapter) -> None:
    """**回归守卫**：题目锚点不许回来。

    v0.2.0 起程序只使用模型读页面，题目几何由模型从截图里给出。
    一旦 ``anchors`` / ``selectors`` / ``readiness`` / ``variants`` 重新出现，
    就说明有人把「解析页面结构」这条读题通道又接了回来。
    """
    for gone in ("anchors", "selectors", "readiness", "variants", "qtype_map", "locator"):
        assert not hasattr(adapter, gone), f"适配器上不该再有 {gone}"


def test_adapter_is_driven_by_media_yaml(adapter: MockExamAdapter) -> None:
    """改 YAML 就能改媒体读法 —— 换站点不需要动代码。"""
    assert adapter.site == "mock_exam"
    assert adapter.media_selectors.get("episode_item")
    assert adapter.field("body_site_attr") == "data-site"
    assert adapter.field("episode_vid_attr") == "data-vid"
    assert adapter.media_assertions.progress_min_delta_s == pytest.approx(1.0)
    assert adapter.media_assertions.paused_max_delta_s == pytest.approx(0.2)
    assert adapter.media_assertions.resume_tolerance_s == pytest.approx(2.0)


def test_unknown_media_anchor_raises(adapter: MockExamAdapter) -> None:
    with pytest.raises(KeyError):
        adapter.media_locator(object(), "no-such-anchor")


def test_media_locator_accepts_scope_and_selector(adapter: MockExamAdapter) -> None:
    """``media_locator()`` 支持 scope 与直接指定选择器。"""
    calls: list[str] = []

    class _FakeLocator:
        def locator(self, selector: str):
            calls.append(selector)
            return self

    fake = _FakeLocator()
    adapter.media_locator(fake, "video")
    adapter.media_locator(fake, "video", scope=fake)
    adapter.media_locator(fake, "ignored", selector=".x")

    assert calls[0] == '[data-media="video"]'
    # scope 生效时用的是 scope 的 locator，但选择器仍然是锚点解析出来的
    assert calls[1] == '[data-media="video"]'
    assert calls[2] == ".x"
