"""M1-3 仲裁单测（纯函数，不需要浏览器）。

v0.2.0 起程序**只使用模型（视觉）**读页面，题目通道只剩视觉一条 ——
原先那套「各通道一致 / 不一致 / DOM 失败走网络 / 双失败」的四分支裁决
已经没有对象，只剩下面两件事：

============================  ==========================================
裁出图                        交出截图 + ``needs_vision``，等模型读
裁不出图                      **暂停留档，绝不静默跳过**
============================ ==========================================

第二条是本文件真正要盯住的东西：它最容易被写成「读不到就跳过这一题」。
"""

from __future__ import annotations

from core.arbiter import arbitrate, decide_channel
from core.config import ProbeName, RunConfig
from core.models import PerceptionResult

CFG = RunConfig()


def _crop_ok(ref: str | None = "logs/run/item__vision.png") -> PerceptionResult:
    """一次成功的裁图：只出了图，题面还没读（读题归 Tier2 模型）。"""
    return PerceptionResult(
        question=None,
        channel_used=ProbeName.VISION,
        warnings=["vision:crop_ok"],
        screenshot_ref=ref,
    )


def _crop_failed(reason: str = "vision:crop_failed") -> PerceptionResult:
    return PerceptionResult(question=None, channel_used=ProbeName.VISION, warnings=[reason])


# ------------------------------------------------------------------ 拿到图


def test_crop_ok_hands_over_the_screenshot_and_asks_for_vision() -> None:
    result = arbitrate([_crop_ok()], CFG)
    assert result.channel_used is ProbeName.VISION
    assert result.question is None, "探针只出图，题面由 Tier2 读"
    assert result.screenshot_ref == "logs/run/item__vision.png"
    assert result.trace is not None
    assert result.trace.reason == "vision_only"
    assert result.trace.needs_vision is True
    assert result.review_required is False
    assert result.trace.paused_for_dump is False


def test_crop_ok_without_screenshot_ref_still_counts_as_available() -> None:
    """没落盘（``screenshot_dir`` 为空）不等于没截到图。

    判据是 ``vision:crop_ok`` 标记而不是「有没有 screenshot_ref」——
    否则「只把 PNG 留在内存」的调用路径会被误判成读不到。
    """
    result = arbitrate([_crop_ok(ref=None)], CFG)
    assert result.trace is not None
    assert result.trace.reason == "vision_only"


def test_crop_failed_marker_is_not_mistaken_for_available() -> None:
    """反面守卫：带 ``vision:crop_failed`` 的结果不能被当成「有图」。

    这里踩过一次坑：早期用「warnings 里有没有 vision: 前缀」当判据，
    于是裁图失败也被算成可用，最后当成「读到了但题面空」混过去。
    """
    result = arbitrate([_crop_failed()], CFG)
    assert result.review_required is True
    assert result.trace is not None and result.trace.paused_for_dump is True


# ------------------------------------------------------------------ 没拿到图


def test_crop_failed_pauses_and_dumps_never_silent() -> None:
    """裁不出图 = 这次真的什么都没读到 → **暂停留档**。

    这一条最容易被写成静默跳过，而「静默跳过一道题」正是本项目
    反复强调不能出现的行为。
    """
    result = arbitrate([_crop_failed()], CFG)
    assert result.question is None
    assert result.channel_used is ProbeName.VISION
    assert result.review_required is True
    assert result.trace is not None
    assert result.trace.reason == "vision_failed"
    assert result.trace.paused_for_dump is True
    assert result.trace.conflicts == ["no_channel_produced_question"]
    assert any("paused_for_dump" in warning for warning in result.warnings)


def test_empty_results_still_pauses() -> None:
    """探针链空（探针没装配）也必须停，不能当成「没有题目要做」。"""
    result = arbitrate([], CFG)
    assert result.trace is not None and result.trace.paused_for_dump is True


# ------------------------------------------------------------------ 选通道


def test_decide_channel_always_vision() -> None:
    """只有一条通道：无论传入什么集合，答案都是视觉。"""
    assert decide_channel(CFG, [ProbeName.VISION]) is ProbeName.VISION
    assert decide_channel(CFG, []) is ProbeName.VISION
    assert decide_channel(CFG, [ProbeName.MEDIA]) is ProbeName.VISION
