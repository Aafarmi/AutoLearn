"""``select_option`` 的**三态判定表** —— 2026-09-29「点对了却判错 → 乱点」的回归测试。

背景（用户原话）：「动作回读不一致判断逻辑存在问题，存在点击正确却判断错误导致胡乱操作。」
根因链：

1. 真实站点上「已选中」的视觉变化常常很轻（1px 描边、一个小圆点、一点底色差，
   整块区域的平均绝对差只有 0.1~1.5）；
2. 旧判据是**单阈值布尔**（``mad >= 2.0`` 才算「变了」）→ 这一档被判成**没点中**；
3. 执行层于是回到**同一个框里**再点 1~3 个候选点（``box_center`` / ``left_half`` /
   ``upper_half``）—— 多选 / 复选上那是把刚选上的勾**取消**，这就是「胡乱操作」；
4. 更坏的一种：这道题**本来就已经选中**（续跑 / 断点续跑 / 用户自己点过），
   再点一次像素**一点都没变**（``mad=0.00``）→ 判失败 → 连点 4 次后停下，
   而它其实是对的。

本文件钉住的判定表（完整理由见 ``Actuator.select_option`` 的 docstring）：

====================================  ==================================================
墨迹上 ``changed``(≥2.0)               成功收工
墨迹上 ``weak``(0.05~2.0)              成功收工，``readback`` 以 ``weak_changed:`` 开头
墨迹上 ``none``(<0.05)                 **只点一次**：``ok=True`` + ``no_change_on_ink``
几何兜底 ``none``                      换下一个候选点，至多 ``click_replay_max + 1`` 次
所有候选都 ``none``                    ``ok=False`` + 暂停 + 留档（**绝不静默报成功**）
====================================  ==================================================

纯逻辑，**不启浏览器**：假截图 + 合成 PNG（复用 ``tests/act_helpers.py`` 的替身）。
阈值边界用合成图钉死 —— 真截图的噪声会把「0.05 / 2.0 这两条线划得对不对」测成玄学。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from act import verifier as verifier_module
from act.actuator import Actuator
from act.screen import candidate_points
from act.verifier import (
    REGION_CHANGE_MIN_MAD,
    REGION_CHANGE_WEAK_MAD,
    RegionChange,
    Verifier,
    region_change_state,
)
from core.config import GuardThresholds, RunConfig
from core.enums import ActionKind, ActLevel, ErrorCode, QType
from core.events import Event
from core.trace import RunLogger
from tests.act_helpers import FakePage, RecordingBus, synthetic_png

#: 合成截图的尺寸与「选项框」：与 ``test_actuator.py`` / ``test_verifier.py`` 同一组，
#: 判据边界才是确定的（框裁出来是 88×48 = 4224 像素）。
SIZE: tuple[int, int] = (200, 200)
BOX: tuple[float, float, float, float] = (0.2, 0.2, 0.4, 0.2)

#: 框内**有墨迹**（文字 / 标号）的一帧：首选落点因此是 ``ink_centroid``。
INK_PATCH = (0.2, 0.2, 0.4, 0.2)
WITH_INK = synthetic_png(SIZE, color=255, patch=INK_PATCH, patch_color=0)
#: 整块纯白：框里**没有可辨认的内容** → 只剩几何兜底候选点。
BLANK = synthetic_png(SIZE, color=255)
#: 明确的强变化（整块由白变黑，mad = 255）
STRONG = synthetic_png(SIZE, color=0)

#: 快跑配置：间隔归零。``click_replay_max`` 按用例需要给（这些数字只影响「最多换几个点」）。
FAST = RunConfig(guards=GuardThresholds(click_replay_max=0, click_replay_gap_ms=(0, 0)))
RETRY2 = RunConfig(guards=GuardThresholds(click_replay_max=2, click_replay_gap_ms=(0, 0)))
RETRY5 = RunConfig(guards=GuardThresholds(click_replay_max=5, click_replay_gap_ms=(0, 0)))


@pytest.fixture(autouse=True)
def _zero_region_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """差分判定的**等待**压到 0：被测点是「怎么判、怎么处置」，不是「睡多久」。"""
    monkeypatch.setattr(verifier_module, "_REGION_SETTLE_GAP_S", 0.0)


# --------------------------------------------------------------------------- #
# 三态本身：阈值边界（纯函数）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("mad", "expected"),
    [
        (0.0, "none"),
        (0.049, "none"),
        (REGION_CHANGE_WEAK_MAD, "weak"),  # 边界本身算 weak（含下界）
        (1.0, "weak"),  # 真实站点上最典型的那一档
        (REGION_CHANGE_MIN_MAD - 0.001, "weak"),
        (REGION_CHANGE_MIN_MAD, "changed"),  # 边界本身算 changed（含下界）
        (14.3, "changed"),  # 靶场选中态实测值
        (255.0, "changed"),
    ],
)
def test_region_change_state_boundaries(mad: float, expected: str) -> None:
    assert region_change_state(mad) == expected


def test_region_change_is_a_frozen_exported_dataclass() -> None:
    """它进 ``__all__``（题目侧判据的对外口径），而且是 frozen —— 结果不该被改写。"""
    assert "RegionChange" in verifier_module.__all__
    assert "REGION_CHANGE_WEAK_MAD" in verifier_module.__all__
    change = RegionChange(mad=0.31, state="weak", samples=3)
    assert (change.mad, change.state, change.samples) == (0.31, "weak", 3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        change.mad = 1.0  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 三态本身：真像素（合成图 → 确定的 mad）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("after_color", "expected_state", "expected_samples"),
    [
        (255, "none", verifier_module._REGION_SETTLE_ATTEMPTS),  # 一帧都没动 → 拍满预算
        (254, "weak", verifier_module._REGION_SETTLE_ATTEMPTS),  # 轻变化 → 也要拍满（想看到更强的）
        (0, "changed", 1),  # 强变化 → 拍到就收工
    ],
)
async def test_measured_state_follows_the_pixel_difference(
    after_color: int, expected_state: str, expected_samples: int
) -> None:
    before = synthetic_png(SIZE, color=255)
    page = FakePage(screenshot_frames=[synthetic_png(SIZE, color=after_color)])

    change = await Verifier(page, RunConfig()).measure_region_change(BOX, SIZE, before)

    assert isinstance(change, RegionChange)
    assert change.state == expected_state
    assert change.mad == pytest.approx(255 - after_color)
    assert change.samples == expected_samples


async def test_measure_takes_the_biggest_difference_of_several_frames() -> None:
    """过渡动画中途那一帧不作数：接着拍，取数帧里的**最大**差值。"""
    before = synthetic_png(SIZE, color=255)
    # 第一帧只有 1.0（weak），第二帧才是 255（changed）
    page = FakePage(screenshot_frames=[synthetic_png(SIZE, color=254), STRONG])

    change = await Verifier(page, RunConfig()).measure_region_change(BOX, SIZE, before)

    assert change.mad == pytest.approx(255.0)
    assert change.state == "changed"
    assert change.samples == 2, "看到强变化就收工，不再空拍剩下的预算"

    # 边界上的最大值也算数：253 单帧只有 2.0（正好是 changed 的下界）
    page = FakePage(screenshot_frames=[synthetic_png(SIZE, color=254), synthetic_png(SIZE, color=253)])
    change = await Verifier(page, RunConfig()).measure_region_change(BOX, SIZE, before)
    assert change.mad == pytest.approx(2.0)
    assert change.state == "changed"


async def test_unmeasurable_frames_report_zero_samples_not_a_silent_none() -> None:
    """``samples == 0`` 是「**判不了**」，不是「确实没变」—— 不许当成成功放过去。"""
    before = synthetic_png(SIZE, color=255)
    page = FakePage(screenshot_frames=[synthetic_png((100, 100), color=255)])  # 尺寸对不上

    change = await Verifier(page, RunConfig()).measure_region_change(BOX, SIZE, before)

    assert change.samples == 0
    assert change.state == "none"

    verdict = await Verifier(page, RunConfig()).verify_region_changed(BOX, SIZE, before)
    assert verdict.ok is False
    assert "尺寸" in verdict.actual, "判不了要如实说明原因，而不是报成「没变」"


# --------------------------------------------------------------------------- #
# 处置：墨迹上的那一下 —— changed / weak / none 都**收工**
# --------------------------------------------------------------------------- #
async def test_ink_click_with_no_change_clicks_exactly_once() -> None:
    """墨迹上点一下、一个像素都没动 → **只点一次**、``ok=True``、``no_change_on_ink``。

    这是「本来就已选中」（续跑 / 断点续跑 / 用户自己点过）的样子：再点一次像素
    仍然一点不变，而多选 / 复选上那一下会把刚选上的勾**取消**。
    配置特意给 ``click_replay_max=2``：旧逻辑在这里会点满 3 次（用户报的「乱点」）。
    """
    bus = RecordingBus()
    page = FakePage(screenshot_frames=[WITH_INK, WITH_INK])

    result = await Actuator(page, RETRY2, bus=bus).select_option(BOX, SIZE, QType.MULTIPLE)

    assert result.ok is True, result.error
    assert result.kind is ActionKind.SELECT_OPTION
    assert result.level_used is ActLevel.L6_VISION_XY
    assert len(page.mouse.clicks) == 1, f"墨迹上不该有第二次点击：{page.mouse.clicks}"
    readback = result.readback or ""
    assert "no_change_on_ink" in readback
    assert "region_mad=0.00" in readback, "留痕要带 mad，别只说一句话"
    # 它落在框内内容上（这次是整块墨迹：x ∈ [40,120), y ∈ [40,80)）
    x, y = page.mouse.clicks[0]
    assert 40 <= x <= 120 and 40 <= y <= 80, f"没点在墨迹上：({x}, {y})"

    assert bus.payloads(Event.ACT_READBACK_MISMATCH) == [], (
        "「本来就已选中」不是回读不一致，不该按失败留痕"
    )
    payload = bus.payloads(Event.ACT_LEVEL_USED)[-1]
    assert payload["ok"] is True
    assert payload["level"] == ActLevel.L6_VISION_XY.value


async def test_ink_click_with_a_weak_change_is_a_success() -> None:
    """轻变化（mad = 1.0：1px 描边 / 小圆点那一档）**也要收工**，不再换点。

    这一条正是修复的核心：旧判据把 1.0 判成「没点中」，于是回同一个框里再点，
    多选 / 复选上把刚选上的勾取消。
    """
    before = synthetic_png(SIZE, color=255, patch=INK_PATCH, patch_color=0)
    after = synthetic_png(SIZE, color=254, patch=INK_PATCH, patch_color=0)
    page = FakePage(screenshot_frames=[before, after])
    bus = RecordingBus()

    result = await Actuator(page, RETRY2, bus=bus).select_option(BOX, SIZE, QType.SINGLE)

    assert result.ok is True, result.error
    assert len(page.mouse.clicks) == 1, f"weak 也是「着了」，不许再点：{page.mouse.clicks}"
    readback = result.readback or ""
    assert readback.startswith("weak_changed:"), readback
    assert "state=weak" in readback
    x, y = page.mouse.clicks[0]
    assert 40 <= x <= 120 and 40 <= y <= 80, f"首选落点应当是墨迹质心：({x}, {y})"


async def test_ink_click_with_a_real_change_is_a_success() -> None:
    """明显变化（mad = 255）→ 成功收工，且留痕里带 mad 与 state。"""
    page = FakePage(screenshot_frames=[WITH_INK, STRONG])

    result = await Actuator(page, FAST, bus=RecordingBus()).select_option(
        BOX, SIZE, QType.SINGLE
    )

    assert result.ok is True, result.error
    assert len(page.mouse.clicks) == 1
    readback = result.readback or ""
    assert "region_mad=" in readback
    assert "state=changed" in readback
    assert "aim=ink_centroid" in readback


async def test_a_weak_change_wins_even_on_a_geometric_candidate() -> None:
    """框里没有墨迹时也一样：只要有响应（changed / weak）就收工，不许继续换点。"""
    page = FakePage(screenshot_frames=[BLANK, synthetic_png(SIZE, color=254)])

    result = await Actuator(page, RETRY2).select_option(BOX, SIZE, QType.SINGLE)

    assert result.ok is True, result.error
    assert len(page.mouse.clicks) == 1
    assert (result.readback or "").startswith("weak_changed:")


# --------------------------------------------------------------------------- #
# 处置：几何兜底 —— 换点，但有界；全 none 必须**大声失败**
# --------------------------------------------------------------------------- #
def _geometric_candidates() -> list[tuple[float, float, str]]:
    """框里没有墨迹时的候选点（纯几何兜底）。期望的换点次数由它推导，不写死。"""
    return candidate_points(BLANK, BOX, SIZE)


def test_the_blank_box_has_fewer_distinct_points_than_the_retry_budget() -> None:
    """纯白框只有 **2 个位置不同**的候选点 —— 所以「换点」的次数上限其实由它决定。

    ⚠️ 本次未修的观察（``act/screen.py`` 的历史缺陷）：``candidate_points`` 里的
    ``upper_half`` 在几何上**恒等于** ``box_center``
    （``y + h/4 + (h/2)/2 == y + h/2``），去重后永远被吃掉 ——
    也就是说「偏上」那一路兜底从来没生效过。修它会改变候选点集合，
    会动到不在本次允许改动范围内的 ``tests/test_aim_point.py``，所以这里只把
    现状钉住，免得把「换点次数」写成 3 这种与实现无关的数字。
    """
    assert [reason for _, _, reason in _geometric_candidates()] == ["box_center", "left_half"]


async def test_geometric_fallback_switches_points_and_fails_loudly(tmp_path: Path) -> None:
    """框里没有墨迹、点哪儿都不变 → 换点至多 ``click_replay_max + 1`` 次，然后如实失败。

    换点（而不是重复同一个坐标）是为 2026-09-28「整个框都是空白、4 次全点行尾空白」
    那次事故留的兜底：那一下**根本没落在内容上**，换点才有意义。
    """
    bus = RecordingBus()
    page = FakePage(screenshot_frames=[BLANK, BLANK])
    run_logger = RunLogger("run-region", tmp_path)
    actuator = Actuator(page, RETRY2, run_logger=run_logger, item_id="item-1", bus=bus)
    expected = min(len(_geometric_candidates()), RETRY2.guards.click_replay_max + 1)
    assert expected >= 2, "至少要换过一次点，这条用例才有意义"

    result = await actuator.select_option(BOX, SIZE, QType.SINGLE)

    assert result.ok is False, "全都没变就绝不能静默报成功"
    assert ErrorCode.READBACK_MISMATCH.value in (result.error or "")
    assert result.level_used is ActLevel.L6_VISION_XY
    clicks = page.mouse.clicks
    assert len(clicks) == expected, f"1 次首点 + 换点 {expected - 1} 次：{clicks}"
    assert len(clicks) <= RETRY2.guards.click_replay_max + 1, "换点次数不许超过预算"
    assert len(set(clicks)) == expected, f"换点必须真的换位置：{clicks}"

    assert result.screenshot_ref is not None
    assert Path(result.screenshot_ref).name == "error.png"
    assert Path(result.screenshot_ref).is_file()

    payload = bus.payloads(Event.ACT_LEVEL_USED)[-1]
    assert payload["ok"] is False
    assert payload["attempts"] == expected
    assert payload["pause"] is True, "编排层靠这个信号执行「暂停 + 记 failed」"
    assert len(bus.payloads(Event.ACT_READBACK_MISMATCH)) == expected, (
        "每次不一致都要在事件流上可见"
    )


async def test_candidate_switching_stops_when_the_candidates_run_out() -> None:
    """候选点枚举完就停：**不重复点同一个坐标**（``click_replay_max`` 只是上限）。"""
    page = FakePage(screenshot_frames=[BLANK, BLANK])
    expected = len(_geometric_candidates())

    result = await Actuator(page, RETRY5).select_option(BOX, SIZE, QType.SINGLE)

    assert result.ok is False
    clicks = page.mouse.clicks
    assert len(clicks) == expected, f"候选枚举完就停（预算是 {RETRY5.guards.click_replay_max + 1}）：{clicks}"
    assert len(clicks) < RETRY5.guards.click_replay_max + 1, "这条用例要的是「预算没用完」"
    assert len(set(clicks)) == expected


async def test_a_change_seen_before_the_next_point_stops_the_switching() -> None:
    """换点之前先重测一次：上一击其实点上了（只是当时还在过渡里）→ 收工，不点第二下。"""
    frames = [BLANK, *[BLANK] * verifier_module._REGION_SETTLE_ATTEMPTS, STRONG]
    page = FakePage(screenshot_frames=frames)

    result = await Actuator(page, RETRY2).select_option(BOX, SIZE, QType.MULTIPLE)

    assert result.ok is True, result.error
    assert len(page.mouse.clicks) == 1, f"已经变了就绝不再点第二下：{page.mouse.clicks}"
    assert (result.readback or "").startswith("already_changed:"), result.readback
    assert "state=changed" in (result.readback or "")
