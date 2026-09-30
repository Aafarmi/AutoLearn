"""通道仲裁（M1-3，v0.2.0 精简）。

v0.2.0 起程序**只使用模型**读页面，题目通道只剩视觉一条，「各通道一致 /
不一致 / 信谁」这套裁决已经没有对象 —— 原先的四分支（一致用 DOM、
不一致用 DOM + 人工复核、DOM 失败走网络、双失败暂停留档）整体删除。

留下的只有一件事，而它恰恰是原来最容易被写错的那条：

    **读不到就必须停。**

视觉通道「给你一张图」不等于「读到了题」—— 图要交给模型读，模型读不出来
时后续链路必须停下来留档，绝不能静默跳过一道题。所以：

============================  ==========================================
裁出图（``_VISION_OK``）       交出截图 + ``needs_vision=True``，等模型读
裁不出图                        ``review_required`` + ``paused_for_dump``
============================  ==========================================

:class:`DecisionTrace` 定义在 :mod:`core.models`（``PerceptionResult`` 要引用它，
放这里会成 ``arbiter → models → arbiter`` 环），此处原样 re-export。
"""

from __future__ import annotations

from core.config import RunConfig
from core.enums import ProbeName
from core.models import DecisionTrace, PerceptionResult

__all__ = ["DecisionTrace", "arbitrate", "decide_channel"]

_VISION = ProbeName.VISION

#: 视觉通道「裁图成功」的标记。判据用标记而不是「有没有 warnings」——
#: 截图失败也会带 ``vision:crop_failed``，用关键字会把它误判成可用。
_VISION_OK = "vision:crop_ok"


def _dedup(items: list[str]) -> list[str]:
    """保序去重，避免同一告警被记两遍。"""
    return list(dict.fromkeys(item for item in items if item))


def decide_channel(cfg: RunConfig, available: list[ProbeName]) -> ProbeName:
    """在可用通道里选第一个。

    v0.2.0 只有一条通道，这个函数实际只回答「视觉通道在不在可用集合里」。
    全都不可用时返回视觉 —— 由调用方负责把「不可用」显式暴露出来，
    **不得在此静默兜底成「读到了」**。
    """
    del cfg  # 顺序不再取决于配置；入参保留是为了与契约签名一致
    if _VISION in set(available):
        return _VISION
    return _VISION


def arbitrate(results: list[PerceptionResult], cfg: RunConfig) -> PerceptionResult:
    """把探针结果裁决成一次感知产出。``results`` 顺序应与探针链一致。"""
    del cfg  # 单通道下判据与配置无关；保留入参是为了与契约签名一致

    vision = next(
        (result for result in results if result.channel_used is _VISION),
        results[-1] if results else None,
    )

    base_warnings: list[str] = []
    for result in results:
        base_warnings.extend(result.warnings)

    #: 「带图」的判据：模型读不出题时还能重新裁图；裁不出图就是真失败。
    vision_available = vision is not None and (
        vision.screenshot_ref is not None or _VISION_OK in vision.warnings
    )
    screenshot_ref = vision.screenshot_ref if vision is not None else None

    if vision_available:
        return PerceptionResult(
            question=None,
            video_state=None,
            channel_used=_VISION,
            warnings=_dedup(base_warnings),
            screenshot_ref=screenshot_ref,
            trace=DecisionTrace(
                chosen=_VISION,
                reason="vision_only",
                needs_vision=True,
            ),
        )

    # 裁不出图 = 这次真的什么都没读到。**暂停留档，绝不静默跳过。**
    base_warnings.extend(["vision:crop_failed", "vision:paused_for_dump"])
    return PerceptionResult(
        question=None,
        video_state=None,
        channel_used=_VISION,
        warnings=_dedup(base_warnings),
        screenshot_ref=screenshot_ref,
        review_required=True,
        trace=DecisionTrace(
            chosen=_VISION,
            reason="vision_failed",
            conflicts=["no_channel_produced_question"],
            needs_vision=True,
            paused_for_dump=True,
        ),
    )
