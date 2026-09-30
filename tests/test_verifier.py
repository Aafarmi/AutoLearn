"""P6 / M3-5 + M5 进度断言：校验层验收。

两类断言各测各的：

- **题目侧**（``verify_region_changed``）：点前后选项区域的像素差分 —— 题目侧唯一判据；
- **媒体侧**（``verify_playing`` / ``verify_paused`` / ``verify_media_flag`` / …）：
  时间窗断言与瞬时断言，判据数字全部来自 ``selectors_media.yaml`` 的 ``media_assertions`` 段。

像素差分那一组用**合成 PNG**（``synthetic_png``）而不是真截图：阈值边界要的是
完全确定的输入 —— 真截图的噪声与视图差异只会把「2.0 / 0.05 这两条线划得对不对」
测成玄学。2026-09-29 起判据是**三态**（``changed`` / ``weak`` / ``none``），
所以这一组同时钉住「哪一档算过」与「哪一档算没变」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from act import verifier as verifier_module
from act.screen import norm_box_center, region_bounds
from act.verifier import (
    PAUSE_MAX_DELTA_S,
    PLAY_MIN_DELTA_S,
    REGION_CHANGE_MIN_MAD,
    REGION_CHANGE_WEAK_MAD,
    RESUME_MAX_DRIFT_S,
    Verifier,
)
from core.config import RunConfig
from core.enums import VerifyKind
from core.models import VerifyResult
from core.trace import RunLogger
from tests.act_helpers import FakePage, RecordingBus, synthetic_png
from tests.helpers import course_url

#: 合成截图的尺寸与「选项框」：全组共用，判据边界才是确定的。
SIZE: tuple[int, int] = (200, 200)
BOX: tuple[float, float, float, float] = (0.2, 0.2, 0.4, 0.2)


@pytest.fixture(autouse=True)
def _zero_region_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """差分判定的**等待**压到 0：被测点是「重拍几次」，不是「睡多久」。"""
    monkeypatch.setattr(verifier_module, "_REGION_SETTLE_GAP_S", 0.0)


def _frame(paused: bool, current_time: float, duration: float = 8.0) -> dict[str, object]:
    return {
        "paused": paused,
        "ended": current_time >= duration,
        "current_time": current_time,
        "duration": duration,
        "src": "x",
    }


# --------------------------------------------------------------------------- #
# 题目侧：选项区域像素差分
# --------------------------------------------------------------------------- #
async def test_region_change_above_the_threshold_passes() -> None:
    page = FakePage(screenshot_frames=[synthetic_png(SIZE, color=255), synthetic_png(SIZE, color=200)])
    verdict = await Verifier(page, RunConfig()).verify_region_changed(
        BOX, SIZE, synthetic_png(SIZE, color=255)
    )

    assert verdict.ok is True
    assert verdict.kind is VerifyKind.SCREENSHOT_DIFF
    assert verdict.expected == f"region_mad≥{REGION_CHANGE_MIN_MAD:g}"
    assert _mad_of(verdict) >= REGION_CHANGE_MIN_MAD
    assert "state=changed" in verdict.actual, "留痕要说清是哪一档，别只给个数字"


async def test_identical_region_is_not_a_change() -> None:
    """点下去画面上什么都没变 = 这一下没落到选项上，按 T0-5 重放。"""
    same = synthetic_png(SIZE, color=255)
    page = FakePage(screenshot_frames=[same, same])

    verdict = await Verifier(page, RunConfig()).verify_region_changed(BOX, SIZE, same)

    assert verdict.ok is False
    assert _mad_of(verdict) == 0.0
    assert "state=none" in verdict.actual


async def test_a_light_change_is_weak_and_still_passes() -> None:
    """2026-09-29：差一点点（这里 1.0）算 ``weak`` —— **过判**，但不等于 changed。

    旧口径（单阈值 2.0）把它判成「没变」，执行层于是回同一个框里再点 1~3 个候选点；
    真实站点上「已选中」常常就值 0.1~1.5（1px 描边 / 一个小圆点），
    多选 / 复选上多点那一下是把刚选上的勾**取消** —— 用户报的「胡乱操作」。
    """
    before = synthetic_png(SIZE, color=255)
    barely = synthetic_png(SIZE, color=254)
    page = FakePage(screenshot_frames=[barely])

    verdict = await Verifier(page, RunConfig()).verify_region_changed(BOX, SIZE, before)

    assert verdict.ok is True, "weak 也是「画面确实响应了」，不该判失败"
    assert _mad_of(verdict) == 1.0
    assert _mad_of(verdict) < REGION_CHANGE_MIN_MAD
    assert "state=weak" in verdict.actual


async def test_a_change_below_the_weak_floor_is_still_not_a_change() -> None:
    """下界也没被放松：mad < 0.05（无损截图下的「一个像素都没动」）仍判 ``none``。

    这里造的是「差 50 灰度、但只落在 2×2 像素上」：mad ≈ 0.047 —— **确实有东西动过**，
    但小到量不出来，不足以证明「选项被选中了」，所以不许当成功放过去。
    """
    patch = (0.25, 0.3, 0.01, 0.01)
    before = synthetic_png(SIZE, color=255, patch=patch, patch_color=255)
    after = synthetic_png(SIZE, color=255, patch=patch, patch_color=205)
    verifier = Verifier(FakePage(screenshot_frames=[after]), RunConfig())

    # 精度只看**未取整**的那个数：``actual`` 里的 mad 是两位小数，
    # 0.047 在文案里就是 0.05，拿它比边界会自欺欺人。
    change = await verifier.measure_region_change(BOX, SIZE, before)
    assert 0.0 < change.mad < REGION_CHANGE_WEAK_MAD
    assert change.state == "none"

    verdict = await verifier.verify_region_changed(BOX, SIZE, before)
    assert verdict.ok is False
    assert "state=none" in verdict.actual


async def test_change_outside_the_option_region_is_ignored() -> None:
    """区域外的变化与这题无关：整页闪一下不该被当成「选项选中了」。"""
    patch = (0.0, 0.0, 0.1, 0.1)
    before = synthetic_png(SIZE, patch=patch, patch_color=0)
    page = FakePage(screenshot_frames=[synthetic_png(SIZE, patch=patch, patch_color=128)])

    verdict = await Verifier(page, RunConfig()).verify_region_changed(BOX, SIZE, before)

    assert verdict.ok is False


async def test_change_just_outside_the_box_is_still_seen() -> None:
    """框**外扩**了一圈：选中态常常画在框边上（单选框 / 描边 / 圆角底色）。

    这里的变化发生在框上沿之外 2~4px（框在 y=80~120，条带在 y=76~80），
    不做外扩就会测出 0。
    """
    strip = (0.0, 0.38, 1.0, 0.02)  # y = 76~80px：落在外扩环里、框本体之外
    before = synthetic_png(SIZE, patch=strip, patch_color=255)
    page = FakePage(screenshot_frames=[synthetic_png(SIZE, patch=strip, patch_color=0)])

    verdict = await Verifier(page, RunConfig()).verify_region_changed(BOX, SIZE, before)

    assert verdict.ok is True
    assert _mad_of(verdict) > REGION_CHANGE_MIN_MAD


def test_region_bounds_pads_outwards_and_clamps_to_the_image() -> None:
    """外扩量 = 1% 边长（下限 4px）；越界一律夹回图像内，绝不挪到图外取景。"""
    assert region_bounds((0.4, 0.4, 0.2, 0.2), SIZE) == (76, 76, 124, 124)
    assert region_bounds((0.0, 0.0, 0.05, 0.05), SIZE) == (0, 0, 14, 14)
    assert region_bounds((0.99, 0.99, 0.01, 0.01), SIZE) == (194, 194, 200, 200)


def test_norm_box_center_is_shared_with_the_actuator() -> None:
    """换算只有一个定义点：执行层点哪儿、校验器裁哪儿必须同一个算法。"""
    assert norm_box_center(BOX, SIZE) == pytest.approx((80.0, 60.0))


async def test_frame_with_the_wrong_size_is_not_verified() -> None:
    """尺寸对不上说明拿到的不是给坐标时那一帧，**判不了**，不能猜。"""
    page = FakePage(screenshot_frames=[synthetic_png(SIZE, color=0)])

    verdict = await Verifier(page, RunConfig()).verify_region_changed(
        BOX, SIZE, synthetic_png((100, 100), color=255)
    )

    assert verdict.ok is False
    assert "尺寸" in verdict.actual


async def test_screenshot_failure_is_reported_not_raised() -> None:
    """取不到帧 → ``ok=False`` 且说明原因；绝不抛异常把动作流程炸掉。"""
    page = FakePage(screenshot_error=RuntimeError("page closed"))

    verdict = await Verifier(page, RunConfig()).verify_region_changed(
        BOX, SIZE, synthetic_png(SIZE, color=255)
    )

    assert verdict.ok is False
    assert "截图失败" in verdict.actual


async def test_it_polls_until_the_transition_settles() -> None:
    """过渡动画中途那一帧不算数 —— 接着拍，直到看见真的变化。"""
    before = synthetic_png(SIZE, color=255)
    page = FakePage(
        screenshot_frames=[
            synthetic_png(SIZE, color=254),  # 过渡刚开始：1.0，不够
            synthetic_png(SIZE, color=0),  # 过渡结束：255
        ]
    )

    verdict = await Verifier(page, RunConfig()).verify_region_changed(BOX, SIZE, before)

    assert verdict.ok is True
    assert page.screenshot_calls == 2, "第一帧不够就要再拍"


async def test_failed_verdict_carries_its_own_evidence(tmp_path: Path) -> None:
    """「失败自带证据」：判据没成立时留一张当时的图，而不是靠调用方记得去截。"""
    same = synthetic_png(SIZE, color=255)
    page = FakePage(screenshot_frames=[same])
    verifier = Verifier(
        page,
        RunConfig(),
        run_logger=RunLogger("run-p6", tmp_path),
        item_id="item-1",
    )

    verdict = await verifier.verify_region_changed(BOX, SIZE, same)

    assert verdict.ok is False
    assert verdict.screenshot_ref is not None
    assert Path(verdict.screenshot_ref).name == "region_unchanged.png"
    assert Path(verdict.screenshot_ref).is_file()


def _mad_of(verdict: VerifyResult) -> float:
    """从 ``region_mad=12.34 state=weak`` 里把数字抠出来（断言判据本身，不看措辞）。

    2026-09-29 起 ``actual`` 多了 ``state=`` 一栏，所以按空格切词找前缀，
    而不是「按第一个 ``=`` 切两半」—— 后者会把 ``state=weak`` 一起吃进 ``float()``。
    """
    for field in verdict.actual.split():
        if field.startswith("region_mad="):
            return float(field.partition("=")[2])
    raise AssertionError(f"actual 里没有 region_mad=：{verdict.actual!r}")


# --------------------------------------------------------------------------- #
# 媒体侧的处置
# --------------------------------------------------------------------------- #
def test_should_escalate_is_just_not_ok() -> None:
    verifier = Verifier(FakePage(), RunConfig())
    assert verifier.should_escalate(
        VerifyResult(ok=False, kind=VerifyKind.SCREENSHOT_DIFF, expected="x", actual="y")
    )
    assert not verifier.should_escalate(
        VerifyResult(ok=True, kind=VerifyKind.SCREENSHOT_DIFF, expected="x", actual="x")
    )


# --------------------------------------------------------------------------- #
# 时间窗断言（窗口压到 0s，判据不变）
# --------------------------------------------------------------------------- #
async def test_verify_playing_passes_when_time_advances(adapter) -> None:
    page = FakePage(video_frames=[_frame(False, 1.0), _frame(False, 3.0)])
    verdict = await Verifier(page, RunConfig()).verify_playing(page, adapter, window_s=0.0)
    assert verdict.ok is True
    assert verdict.kind is VerifyKind.MEDIA_PROGRESS
    assert f"Δ≥{PLAY_MIN_DELTA_S}" in verdict.expected


async def test_verify_playing_fails_when_stuck(adapter) -> None:
    page = FakePage(video_frames=[_frame(False, 1.0), _frame(False, 1.5)])
    verdict = await Verifier(page, RunConfig()).verify_playing(page, adapter, window_s=0.0)
    assert verdict.ok is False
    assert "Δ=0.50s" in verdict.actual


async def test_verify_paused_requires_both_stillness_and_paused_flag(adapter) -> None:
    still = FakePage(video_frames=[_frame(True, 2.0), _frame(True, 2.05)])
    assert (await Verifier(still, RunConfig()).verify_paused(still, adapter, window_s=0.0)).ok

    moving = FakePage(video_frames=[_frame(True, 2.0), _frame(True, 5.0)])
    verdict = await Verifier(moving, RunConfig()).verify_paused(moving, adapter, window_s=0.0)
    assert verdict.ok is False
    assert f"Δ≤{PAUSE_MAX_DELTA_S}" in verdict.expected


async def test_verify_playing_reports_missing_media(adapter) -> None:
    page = FakePage()
    page.evaluate = _no_media  # type: ignore[method-assign]
    verdict = await Verifier(page, RunConfig()).verify_playing(page, adapter, window_s=0.0)
    assert verdict.ok is False
    assert verdict.actual == "media:not_available"


async def _no_media(script: str, arg: object = None) -> object:
    if "currentSrc" in script:
        return None  # read_video_state 的「元素不存在」分支
    return None


async def test_verify_resume_continuous_bounds(adapter) -> None:
    ok_page = FakePage(video_frames=[_frame(True, 30.0, duration=60.0)])
    verdict = await Verifier(ok_page, RunConfig()).verify_resume_continuous(ok_page, adapter, 29.0)
    assert verdict.ok is True

    drift_page = FakePage(video_frames=[_frame(True, 40.0, duration=60.0)])
    verdict = await Verifier(drift_page, RunConfig()).verify_resume_continuous(
        drift_page, adapter, 29.0
    )
    assert verdict.ok is False
    assert f"|Δt|≤{RESUME_MAX_DRIFT_S}" in verdict.expected


async def test_verify_episode_advance_requires_exactly_plus_one(adapter) -> None:
    verifier = Verifier(FakePage(body_attrs={"data-current-episode": "2"}), RunConfig())
    assert (await verifier.verify_episode_advance(None, adapter, 1)).ok  # type: ignore[arg-type]
    assert not (await verifier.verify_episode_advance(None, adapter, 3)).ok  # type: ignore[arg-type]
    assert (await verifier.verify_episode_advance(None, adapter, 3)).actual.startswith("index=2")


# --------------------------------------------------------------------------- #
# 瞬时断言
# --------------------------------------------------------------------------- #
async def test_verify_media_flag_variants(adapter) -> None:
    verifier = Verifier(FakePage(video_frames=[_frame(False, 4.0)]), RunConfig())

    assert (await verifier.verify_media_flag(adapter, paused=False)).ok
    assert not (await verifier.verify_media_flag(adapter, paused=True)).ok
    assert (await verifier.verify_media_flag(adapter, ended=False)).ok
    assert (await verifier.verify_media_flag(adapter, current_time=4.2, tolerance=0.5)).ok
    assert not (await verifier.verify_media_flag(adapter, current_time=7.0, tolerance=0.5)).ok


async def test_verify_media_flag_without_media_fails(adapter) -> None:
    page = FakePage()
    page.evaluate = _no_media  # type: ignore[method-assign]
    verdict = await Verifier(page, RunConfig()).verify_media_flag(adapter, paused=False)
    assert verdict.ok is False
    assert verdict.actual == "media:not_available"
    assert verdict.kind is VerifyKind.MEDIA_PROGRESS, "读不到媒体是媒体侧的事，不是截图差分"


async def test_media_flag_reports_pause_and_no_event_noise(adapter, tmp_path: Path) -> None:
    """瞬时断言不落图、不发事件 —— 逐级确认每级都截一张会让留痕炸掉。

    （失败留档由执行层的阶梯到顶那一步统一做，见 ``test_actuator.py``。）
    """
    bus = RecordingBus()
    page = FakePage(video_frames=[_frame(False, 4.0)])
    verifier = Verifier(
        page,
        RunConfig(),
        run_logger=RunLogger("run-p6", tmp_path),
        item_id="item-1",
        bus=bus,
    )

    verdict = await verifier.verify_media_flag(adapter, paused=False)

    assert verdict.ok is True
    assert page.screenshot_calls == 0
    assert bus.events == []


# --------------------------------------------------------------------------- #
# 真浏览器：时间窗断言在网课靶场上真的成立
# --------------------------------------------------------------------------- #
async def test_verify_playing_and_paused_on_real_course(page, mock_base, adapter) -> None:
    """``?dur=8`` 的合成视频：播放态推进达标、暂停态静止达标。

    这两条是 M5 的量化断言，P6 先把 Verifier 落地，P8 直接消费。
    """
    await page.goto(course_url(mock_base, dur=8), wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_selector('[data-media="video"]', state="attached", timeout=8000)
    await page.wait_for_function(
        "() => document.body.getAttribute('data-current-episode') === '1'", timeout=8000
    )

    verifier = Verifier(page, RunConfig())
    await page.locator('[data-media="play-button"]').click()

    playing = await verifier.verify_playing(page, adapter)
    assert playing.ok is True, f"播放态推进不达标：{playing.actual}"

    await page.locator('[data-media="play-button"]').click()
    paused = await verifier.verify_paused(page, adapter)
    assert paused.ok is True, f"暂停态静止不达标：{paused.actual}"


# --------------------------------------------------------------------------- #
# 真浏览器：像素差分在真页面上真的能判
# --------------------------------------------------------------------------- #
async def test_region_diff_sees_a_real_selection(
    page, mock_base, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真靶场上：点一下选项 → 区域差分必须过线；不点 → 必须是 0。

    合成图钉的是阈值，这条钉的是「阈值对真页面也成立」。
    这里把轮询间隔**还原成生产值**：真页面的选中态有 120ms 过渡，
    间隔压到 0 会让四帧全部落在过渡开始之前（那正是重放机制要兜的情况）。
    """
    from tests.act_helpers import quiz_scope, wait_options

    monkeypatch.setattr(verifier_module, "_REGION_SETTLE_GAP_S", 0.08)
    await page.goto(f"{mock_base}/quiz.html?seq=1", wait_until="domcontentloaded", timeout=15000)
    scope = await quiz_scope(page)
    assert await wait_options(scope) >= 2

    size = await _viewport(page)
    row = scope.locator('[data-quiz="option"]').first
    bbox = await row.bounding_box()
    assert bbox is not None
    box = (
        bbox["x"] / size[0],
        bbox["y"] / size[1],
        bbox["width"] / size[0],
        bbox["height"] / size[1],
    )

    verifier = Verifier(page, RunConfig())
    before = await page.screenshot(type="png", scale="css")

    # 1) 没点之前：区域必须一模一样
    assert (await verifier.verify_region_changed(box, size, before)).ok is False

    # 2) 点中中心：区域必须变（真页面上的选中态是底色 + 描边一起变）
    await page.mouse.click(bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
    verdict = await verifier.verify_region_changed(box, size, before)
    assert verdict.ok is True, f"真页面上没判出变化：{verdict.actual}"


async def _viewport(page) -> tuple[int, int]:
    from tests.act_helpers import viewport_size

    return await viewport_size(page)
