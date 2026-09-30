"""P6 / M3-2 … M3-7 执行层验收（v0.2.0：题目侧按坐标点 + 像素差分）。

逐条对应规划书 P6 验收口径（内存用例标 ``[unit]``，真浏览器用例标 ``[e2e]``）：

===============================================  ==========================================
坐标换算只乘一次（不除 devicePixelRatio）        ``test_norm_box_center_*`` / ``test_*l6*``
点后区域没变 → 换候选点 ≤3 次 → 到顶 failed       ``test_select_option_*``
「墨迹上没变就不再点」→ 收工（三态判定表）        ``tests/test_region_change.py``
失败题留截图 + 暂停信号，不静默跳过               ``test_*_pauses_*``
「已经变了就不动手」（多选重复点击会取消勾）       ``test_select_option_does_not_click_again_*``
提交不重试、不重放，超时暂停                       ``test_submit_*``
媒体动作阶梯顺序与元素点击相反                     ``test_media_ladder_*`` / ``test_play_media_e2e``
坐标点击在真靶场上真的选中 / 推进 / 提交           ``test_*_on_a_real_*``
===============================================  ==========================================
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from act import verifier as verifier_module
from act.actuator import (
    LEVELS,
    MEDIA_LEVELS,
    PAUSE_LEVELS,
    SEEK_LEVELS,
    SWIPE_STEPS,
    Actuator,
    _swipe_path,
    norm_box_center,
)
from act.screen import candidate_points
from act.verifier import Verifier
from adapters.mock_exam.adapter import load_adapter
from core.config import GuardThresholds, RunConfig
from core.enums import ActionKind, ActLevel, ErrorCode, QType, VerifyKind
from core.events import Event
from core.models import ActionResult, VerifyResult
from core.trace import RunLogger
from perception.media_probe import read_video_state
from tests.act_helpers import (
    FakeLocator,
    FakePage,
    RecordingBus,
    correct_option_region,
    element_region,
    option_input,
    quiz_scope,
    synthetic_png,
    wait_options,
)
from tests.helpers import course_url, open_quiz, quiz_url

#: 快跑配置：重放关掉、间隔归零。阶梯逻辑本身用这些数字测，**不动生产默认值**。
FAST = RunConfig(guards=GuardThresholds(click_replay_max=0, click_replay_gap_ms=(0, 0)))
#: 重放 2 次的配置，用来数「一次首点 + 几次重放」
REPLAY2 = RunConfig(guards=GuardThresholds(click_replay_max=2, click_replay_gap_ms=(0, 0)))
#: 带可辨识间隔的配置：验证重放间隔**真的取自配置**（而不是写死的 sleep）
GAPPED = RunConfig(guards=GuardThresholds(click_replay_max=2, click_replay_gap_ms=(7, 9)))

#: 合成截图的尺寸与「选项框」：差分用例统一用它，判据边界才是确定的。
SHOT_SIZE: tuple[int, int] = (200, 200)
BOX: tuple[float, float, float, float] = (0.2, 0.2, 0.4, 0.2)

#: 点前 / 点后 / 没变 三张合成图
BEFORE = synthetic_png(SHOT_SIZE, color=255)
AFTER = synthetic_png(SHOT_SIZE, color=0)
SAME = synthetic_png(SHOT_SIZE, color=255)


@pytest.fixture(autouse=True)
def _zero_region_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """差分判定的**等待**压到 0：被测点是「重拍几次」，不是「睡多久」。"""
    monkeypatch.setattr(verifier_module, "_REGION_SETTLE_GAP_S", 0.0)


def _assert_clicked(page: FakePage, x: float, y: float) -> None:
    """断言「点在哪个像素上」。浮点相乘必然带尾巴（0.3×200 = 60.00000000000001），
    所以比的是值而不是字面量。"""
    assert len(page.mouse.clicks) == 1, page.mouse.clicks
    assert page.mouse.clicks[0] == pytest.approx((x, y))


def _patch_attempt(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    monkeypatch.setattr(Actuator, "_attempt", fn)


def _attempt_stub(
    *, ok_levels: tuple[ActLevel, ...] = (), record: list[ActLevel] | None = None
) -> Any:
    """造一个「只在指定级别成功」的 ``_attempt`` 替身。"""

    async def stub(
        self: Actuator,
        level: ActLevel,
        target: Any,
        *,
        kind: ActionKind = ActionKind.CLICK,
        adapter: Any = None,
        seconds: float | None = None,
    ) -> ActionResult:
        if record is not None:
            record.append(level)
        ok = level in ok_levels
        return ActionResult(
            kind=kind,
            target="stub",
            level_used=level,
            ok=ok,
            error=None if ok else "stub:level_failed",
        )

    return stub


# --------------------------------------------------------------------------- #
# 阶梯常量：顺序即契约（v0.2.0 起只剩媒体路径）
# --------------------------------------------------------------------------- #
def test_levels_order_is_frozen() -> None:
    """``LEVELS`` 现在只服务媒体「下一集」，但**顺序仍是冻结契约**。"""
    assert LEVELS == (
        ActLevel.L1_LOCATOR,
        ActLevel.L2_FORCE,
        ActLevel.L3_SCROLL,
        ActLevel.L4_FOCUS_KEYS,
        ActLevel.L5_BBOX,
    )


def test_media_ladders_have_no_vision_rung() -> None:
    """媒体阶梯里**不留** ``L6_VISION_XY``：那一级已经没人能供货。

    视觉坐标那一级在 v0.2.0 只属于题目侧，而且题目侧是**显式入参**
    （``box`` / ``size``）而不是「回调去问坐标」；媒体控件又只由媒体锚点寻址。
    留着一级必然失败，只会让每次媒体动作白烧一次，并把失败原因指向一个
    与真实故障无关的地方。这条用例就是防止它被「顺手补回来」。
    """
    for ladder in (LEVELS, MEDIA_LEVELS, SEEK_LEVELS, PAUSE_LEVELS):
        assert ActLevel.L6_VISION_XY not in ladder, ladder
    assert ActLevel.L6_VISION_XY.value == "l6_vision_xy", "级别本身仍属契约（题目侧在报它）"


def test_media_ladder_puts_script_path_last() -> None:
    """风险 #4：``evaluate("video.play()")`` 是不可信手势，只能垫底。写反 → 永远 NotAllowedError。"""
    assert MEDIA_LEVELS[0] is ActLevel.L1_LOCATOR, "可信手势必须排第一"
    assert MEDIA_LEVELS[-1] is ActLevel.L2_FORCE, "脚本路径必须垫底"
    assert MEDIA_LEVELS[-2] is ActLevel.L5_BBOX
    assert set(MEDIA_LEVELS) == set(LEVELS), "媒体阶梯必须是同一套级别的重排"


def test_seek_ladder_puts_script_path_first() -> None:
    """靶场没有可点的 scrubber，只有脚本能精确落点 —— seek 是唯一例外。"""
    assert SEEK_LEVELS[0] is ActLevel.L2_FORCE


def test_pause_ladder_avoids_covered_clicks() -> None:
    """弹题遮罩（``position: fixed; inset: 0``）盖住播放按钮，纯点击路径在挂起场景下必然超时。

    暂停因此把键盘（可信手势、不受遮罩影响）与脚本（``pause()`` 不要求用户手势）
    提到前面，点击路径垫底。播放仍走 ``MEDIA_LEVELS`` 不变 —— ``play()`` 必须由
    可信手势发起，这条不能动。
    """
    assert PAUSE_LEVELS[0] is ActLevel.L4_FOCUS_KEYS, "键盘是可信手势的兜底"
    assert PAUSE_LEVELS[1] is ActLevel.L2_FORCE, "脚本紧随其后"
    assert PAUSE_LEVELS[-1] is ActLevel.L1_LOCATOR, "会被遮罩挡住的那一级必须垫底"
    assert PAUSE_LEVELS.index(ActLevel.L4_FOCUS_KEYS) < PAUSE_LEVELS.index(ActLevel.L1_LOCATOR)
    assert set(PAUSE_LEVELS) == set(LEVELS), "暂停阶梯必须是同一套级别的重排"
    assert MEDIA_LEVELS[0] is ActLevel.L1_LOCATOR, "播放的阶梯不受本条影响"


# --------------------------------------------------------------------------- #
# 坐标换算 [unit]：只乘一次，不除 devicePixelRatio
# --------------------------------------------------------------------------- #
def test_norm_box_center_multiplies_by_image_size_only() -> None:
    """``scale="css"`` 下图像像素 == 视口 CSS 像素，所以**只乘一次**。

    中心 = (x + w/2, y + h/2) 再乘图像尺寸：``(0.25, 0.5, 0.5, 0.1)``、``1000×500``
    → ``(500, 275)``。若这里再除一次 DPR，缩放显示器上就会整体偏一半。
    """
    assert norm_box_center((0.25, 0.5, 0.5, 0.1), (1000, 500)) == pytest.approx((500.0, 275.0))


def test_norm_box_center_rejects_a_zero_sized_shot() -> None:
    """拿不到尺寸就**停下来** —— 点 (0, 0) 是一次真实的误点。"""
    with pytest.raises(ValueError):
        norm_box_center(BOX, (0, 0))


async def test_select_option_clicks_the_box_center_in_css_pixels() -> None:
    """DPR=2 的屏上也**不换算**：点的就是乘出来的 CSS 像素。"""
    page = FakePage(dpr=2.0, screenshot_frames=[BEFORE, AFTER])

    result = await Actuator(page, FAST).select_option(BOX, SHOT_SIZE, QType.SINGLE)

    assert result.ok is True
    _assert_clicked(page, 80.0, 60.0)  # 只乘图像尺寸，不除 devicePixelRatio


# --------------------------------------------------------------------------- #
# select_option：坐标 + 像素差分 [unit]
# --------------------------------------------------------------------------- #
async def test_select_option_reports_level6_and_the_region_diff() -> None:
    bus = RecordingBus()
    page = FakePage(screenshot_frames=[BEFORE, AFTER])
    result = await Actuator(page, FAST, bus=bus).select_option(
        BOX, SHOT_SIZE, QType.SINGLE, target_label="q1:A"
    )

    assert result.ok is True
    assert result.kind is ActionKind.SELECT_OPTION
    assert result.target == "q1:A"
    assert result.level_used is ActLevel.L6_VISION_XY
    assert "region_mad=" in (result.readback or ""), "回读要说清「差多少」，别只说 true"
    _assert_clicked(page, 80.0, 60.0)

    payload = bus.payloads(Event.ACT_LEVEL_USED)[-1]
    assert payload["kind"] == ActionKind.SELECT_OPTION.value
    assert payload["level"] == ActLevel.L6_VISION_XY.value
    assert payload["ok"] is True


async def test_select_option_unchanged_region_switches_points_then_pauses_loudly(
    tmp_path: Path,
) -> None:
    """框里没有内容、点哪儿都不变 → **换候选点**（1 次首点 + 剩下几个候选点）→ 到顶：
    截图 + ``pause``。

    换点而不是重复同一个坐标：重复一个错的坐标再多次也不会变对
    （2026-09-28「4 次全点行尾空白」那次事故的教训）。
    换点次数由**真的有几个位置不同的候选点**决定，不写死 —— 纯白框给出
    ``["box_center", "left_half"]``（``upper_half`` 与几何中心重合，去重时被吃掉）。
    """
    bus = RecordingBus()
    run_logger = RunLogger("run-p6", tmp_path)
    page = FakePage(screenshot_frames=[BEFORE, SAME])
    actuator = Actuator(page, REPLAY2, run_logger=run_logger, item_id="item-1", bus=bus)
    expected = min(len(candidate_points(BEFORE, BOX, SHOT_SIZE)), REPLAY2.guards.click_replay_max + 1)

    result = await actuator.select_option(BOX, SHOT_SIZE, QType.SINGLE)

    assert result.ok is False
    assert result.level_used is ActLevel.L6_VISION_XY
    assert len(page.mouse.clicks) == expected, "1 次首点 + 每次换一个位置不同的候选点"
    assert len(set(page.mouse.clicks)) == expected, "换点必须真的换位置"
    assert (result.error or "").startswith(ErrorCode.ACTION_LADDER_EXHAUSTED.value)
    assert ErrorCode.READBACK_MISMATCH.value in (result.error or "")

    assert result.screenshot_ref is not None
    assert Path(result.screenshot_ref).name == "error.png"
    assert Path(result.screenshot_ref).is_file()

    payload = bus.payloads(Event.ACT_LEVEL_USED)[-1]
    assert payload["ok"] is False
    assert payload["attempts"] == expected
    assert payload["pause"] is True, "编排层靠这个信号执行「暂停 + 记 failed」"
    # 每次不一致都要在 SSE 上可见（不是只在最后报一次）
    assert len(bus.payloads(Event.ACT_READBACK_MISMATCH)) == expected


async def test_select_option_replay_gap_comes_from_the_config(monkeypatch) -> None:
    """T0-5 的换点间隔必须走 ``cfg.guards.click_replay_gap_ms``（带抖动），不许写死。"""
    sleeps: list[tuple[int, int]] = []

    async def spy(gap: tuple[int, int]) -> None:
        sleeps.append(gap)

    monkeypatch.setattr("act.actuator.sleep_gap", spy)
    page = FakePage(screenshot_frames=[BEFORE, SAME])
    expected = min(len(candidate_points(BEFORE, BOX, SHOT_SIZE)), GAPPED.guards.click_replay_max + 1)

    result = await Actuator(page, GAPPED).select_option(BOX, SHOT_SIZE, QType.SINGLE)

    assert result.ok is False
    assert sleeps == [(7, 9)] * (expected - 1), "每次换点之间隔一次；最后一次不再等"


async def test_select_option_does_not_click_again_once_the_region_changed() -> None:
    """换点前先看一眼：上一次其实点上了就别再点 —— 多选重复点击会把勾**取消**。

    帧序列按新的重拍预算造：首点后的那一轮差分要**拍满** ``_REGION_SETTLE_ATTEMPTS``
    帧才肯说「没变」（过渡动画可能比旧窗口长），下一轮才轮到「变了」那一帧。
    """
    frames = [BEFORE, *[SAME] * verifier_module._REGION_SETTLE_ATTEMPTS, AFTER]
    page = FakePage(screenshot_frames=frames)

    result = await Actuator(page, REPLAY2).select_option(BOX, SHOT_SIZE, QType.MULTIPLE)

    assert result.ok is True
    assert len(page.mouse.clicks) == 1, "已经变了就绝不再点第二下"
    assert (result.readback or "").startswith("already_changed:")


async def test_select_option_click_failure_is_reported_without_replay() -> None:
    """坐标点击抛异常 = 输入事件没发出去：重放同一个坐标没有意义，直接留档。"""
    bus = RecordingBus()
    page = FakePage(
        screenshot_frames=[BEFORE],
        click_error=RuntimeError("mouse 被拖拽状态卡住了"),
    )
    result = await Actuator(page, REPLAY2, bus=bus).select_option(BOX, SHOT_SIZE, QType.SINGLE)

    assert result.ok is False
    assert page.mouse.clicks == []
    assert "RuntimeError" in (result.error or "")
    payload = bus.payloads(Event.ACT_LEVEL_USED)[-1]
    assert payload["pause"] is True
    assert payload["attempts"] == 1, "只试一次：这一支不该重放"


async def test_select_option_survives_screenshot_failure() -> None:
    """连基准帧都拿不到时：动作失败但**程序不能崩**，截图引用为空也照常报。"""
    page = FakePage(screenshot_error=RuntimeError("page closed"))
    result = await Actuator(page, FAST).select_option(BOX, SHOT_SIZE, QType.SINGLE)

    assert result.ok is False
    assert result.screenshot_ref is None
    assert (result.error or "").startswith(ErrorCode.ACTION_LADDER_EXHAUSTED.value)


async def test_select_option_restores_the_scroll_position() -> None:
    """坐标动作不该把页面滚跑；万一滚了，动作结束后必须复位。"""
    calls: list[str] = []

    class _RecordingPage(FakePage):
        async def evaluate(self, script: str, arg: Any = None) -> Any:
            calls.append(script)
            return await super().evaluate(script, arg)

    page = _RecordingPage(screenshot_frames=[BEFORE, AFTER])
    await Actuator(page, FAST).select_option(BOX, SHOT_SIZE, QType.SINGLE)

    assert any("scrollX" in script for script in calls), "动作前要记下滚动位置"
    assert any("scrollTo" in script for script in calls), "动作后要复位"


# --------------------------------------------------------------------------- #
# click：通用坐标点击 [unit]
# --------------------------------------------------------------------------- #
async def test_click_by_coordinates_is_a_single_l6_click() -> None:
    """通用点击**只点一次、不做结果校验**，但会为了让落点落在内容上先截一张。

    2026-09-28 起：点哪儿由「框内内容质心」决定（``act.screen.candidate_points``），
    所以多了一次截图 —— 这是刻意的取舍。旧实现点的是框的**几何中心**，
    在真实站点上会落到行尾空白里，四次点击全部无效（事故留痕见 ``logs/a090315220c6``）。
    一张图换「点得中」，对推进这种一步定生死的动作划算。

    ``readback`` 里放的是**这一次用了哪个候选落点**（定位信息），不是校验结论 ——
    本方法仍然没有判据、也仍然只点一次。
    """
    bus = RecordingBus()
    page = FakePage()
    result = await Actuator(page, FAST, bus=bus).click(
        (0.5, 0.5, 0.2, 0.2), (1000, 500), target_label="next"
    )

    assert result.ok is True
    assert result.kind is ActionKind.CLICK
    assert result.target == "next"
    assert result.level_used is ActLevel.L6_VISION_XY
    # FakePage 截不出真图 → 回退几何中心，所以落点是框中心、来由是 box_center。
    assert result.readback == "aim=box_center"
    _assert_clicked(page, 600.0, 300.0)
    assert page.screenshot_calls == 1, "为了定位落点会截一张基准帧"

    payload = bus.payloads(Event.ACT_LEVEL_USED)[-1]
    assert payload["kind"] == ActionKind.CLICK.value
    assert payload["ok"] is True


async def test_click_failure_pauses_with_screenshot(tmp_path: Path) -> None:
    bus = RecordingBus()
    page = FakePage(click_error=RuntimeError("坐标在视口之外"))
    actuator = Actuator(
        page,
        FAST,
        run_logger=RunLogger("run-p6", tmp_path),
        item_id="item-1",
        bus=bus,
    )

    result = await actuator.click((0.5, 0.5, 0.1, 0.1), (100, 100), target_label="next")

    assert result.ok is False
    assert page.mouse.clicks == []
    assert result.screenshot_ref is not None
    assert Path(result.screenshot_ref).name == "error.png"
    assert bus.payloads(Event.ACT_LEVEL_USED)[-1]["pause"] is True


# --------------------------------------------------------------------------- #
# submit：不重试、不重放 [unit]
# --------------------------------------------------------------------------- #
async def test_submit_clicks_once_and_reports_l6() -> None:
    page = FakePage()
    result = await Actuator(page, FAST).submit(BOX, SHOT_SIZE, target_label="submit:q1")

    assert result.ok is True
    assert result.kind is ActionKind.SUBMIT
    assert result.target == "submit:q1"
    assert result.level_used is ActLevel.L6_VISION_XY
    _assert_clicked(page, 80.0, 60.0)  # 提交只点一次


async def test_submit_failure_pauses_without_any_retry(tmp_path: Path) -> None:
    bus = RecordingBus()
    page = FakePage(click_error=TimeoutError("提交按钮点不动"))
    actuator = Actuator(
        page,
        FAST,
        run_logger=RunLogger("run-p6", tmp_path),
        item_id="item-1",
        bus=bus,
    )

    result = await actuator.submit(BOX, SHOT_SIZE, target_label="submit:q1")

    assert result.ok is False
    assert page.mouse.clicks == [], "超时后**绝对不能**再点一次"
    assert ErrorCode.SUBMIT_TIMEOUT.value in (result.error or "")
    assert result.screenshot_ref is not None
    assert Path(result.screenshot_ref).name == "submit_failed.png"

    payload = bus.payloads(Event.ACT_SUBMIT_TIMEOUT)[-1]
    assert payload["pause"] is True, "超时暂停等人，不许自动重放"


async def test_submit_times_out_instead_of_hanging(monkeypatch) -> None:
    """页面卡死时提交必须**限时退出**（坐标点击没有「等元素可点」那一步，得自己兜）。"""

    class _HangingMouse:
        def __init__(self) -> None:
            self.calls = 0

        async def click(self, x: float, y: float, **kwargs: Any) -> None:
            self.calls += 1
            await asyncio.sleep(5)

    page = FakePage()
    page.mouse = _HangingMouse()  # type: ignore[assignment]
    monkeypatch.setattr("act.actuator.SUBMIT_TIMEOUT_MS", 20)

    result = await Actuator(page, FAST).submit(BOX, SHOT_SIZE)

    assert result.ok is False
    assert ErrorCode.SUBMIT_TIMEOUT.value in (result.error or "")
    assert result.elapsed_ms < 1000, "不能真的挂满 5 秒"


# --------------------------------------------------------------------------- #
# 坐标级 [unit]：L5 用 bounding_box（已是 CSS 像素），题目侧的点位显式入参
# --------------------------------------------------------------------------- #
async def test_level5_uses_css_pixels_without_dpr_conversion() -> None:
    page = FakePage(dpr=2.0)
    locator = FakeLocator(box={"x": 100.0, "y": 50.0, "width": 40.0, "height": 20.0})

    result = await Actuator(page, FAST)._attempt(ActLevel.L5_BBOX, locator)

    assert result.ok is True
    assert page.mouse.clicks == [(120.0, 60.0)], (
        "bounding_box 已经是 CSS 像素，再除一次 devicePixelRatio 就会「正好偏一半」"
    )


async def test_level5_fails_when_element_has_no_box() -> None:
    page = FakePage()
    result = await Actuator(page, FAST)._attempt(ActLevel.L5_BBOX, FakeLocator(box=None))
    assert result.ok is False
    assert "bounding_box" in (result.error or "")
    assert page.mouse.clicks == []


# --------------------------------------------------------------------------- #
# 媒体：幂等守卫 + 逐级确认的等待预算 [unit]
# --------------------------------------------------------------------------- #
def _video_frame(*, paused: bool, current_time: float = 0.0, duration: float = 8.0) -> dict[str, Any]:
    return {
        "paused": paused,
        "ended": False,
        "current_time": current_time,
        "duration": duration,
        "src": "x",
    }


async def test_media_action_is_skipped_when_the_flag_is_already_right(monkeypatch) -> None:
    """已经在期望状态就不动手 —— 播放/暂停键都是 toggle，重复点会把它点回去。"""
    record: list[ActLevel] = []
    _patch_attempt(monkeypatch, _attempt_stub(ok_levels=tuple(LEVELS), record=record))
    page = FakePage(video_frames=[_video_frame(paused=True)])

    result = await Actuator(page, FAST).pause_media(load_adapter())

    assert result.ok is True
    assert record == [], "已暂停就不该再碰播放按钮"
    assert (result.readback or "").startswith("skipped:")


async def test_media_confirmation_polls_before_giving_up(monkeypatch) -> None:
    """「点了」到「真的动了」有几百 ms 空窗：确认要给一段轮询预算，而不是拍一帧定生死。"""
    record: list[ActLevel] = []
    _patch_attempt(monkeypatch, _attempt_stub(ok_levels=(ActLevel.L1_LOCATOR,), record=record))
    calls = {"n": 0}

    async def flaky_flag(self: Verifier, adapter: Any, **kwargs: Any) -> VerifyResult:
        calls["n"] += 1
        return VerifyResult(
            ok=calls["n"] >= 3,
            kind=VerifyKind.MEDIA_PROGRESS,
            expected="paused=False",
            actual=f"try{calls['n']}",
        )

    monkeypatch.setattr(Verifier, "verify_media_flag", flaky_flag)

    result = await Actuator(FakePage(), FAST).play_media(load_adapter())

    assert result.ok is True
    assert result.level_used is ActLevel.L1_LOCATOR
    assert record == [ActLevel.L1_LOCATOR], "L1 成了就不该继续往下降"
    assert calls["n"] == 3, "前两次没成要接着等，而不是立刻判失败"


# --------------------------------------------------------------------------- #
# 真浏览器：[e2e] 坐标点击在真靶场上真的管用
# --------------------------------------------------------------------------- #
async def test_select_option_by_coordinates_on_a_real_question(page, mock_base) -> None:
    """视觉模型给框、执行层照坐标点、点后看像素 —— 这条链路在真页面上合得上。"""
    actuator = Actuator(page, RunConfig())
    await open_quiz(page, quiz_url(mock_base, seq=1))
    scope = await quiz_scope(page)
    assert await wait_options(scope) >= 2

    box, size = await element_region(page, scope.locator('[data-quiz="option"]').first)
    result = await actuator.select_option(box, size, QType.SINGLE, target_label="q1:A")

    assert result.ok is True, result.error
    assert result.level_used is ActLevel.L6_VISION_XY
    assert "region_mad=" in (result.readback or "")
    assert await (await option_input(scope, 0)).evaluate("(el) => el.checked") is True


async def test_click_by_coordinates_advances_to_the_next_question(page, mock_base) -> None:
    """「下一题」也没有锚点路径了：点模型给的坐标，然后由编排层确认页面翻了。

    序号取 ``1,4``：靶场题库只收录坑位题（不是连续的 1..50），凑两道真实的题即可。
    """
    await open_quiz(page, quiz_url(mock_base, seq="1,4"))
    scope = await quiz_scope(page)
    assert await wait_options(scope) >= 2
    sequence = await page.evaluate("() => (window.__MOCK__ || {}).sequence || []")
    assert len(sequence) >= 2, f"这组序号凑不出两道题：{sequence}"
    assert await _progress_index(page) == "1"

    box, size = await element_region(page, scope.locator('[data-quiz="next"]').first)
    result = await Actuator(page, RunConfig()).click(box, size, target_label="next")

    assert result.ok is True, result.error
    assert result.kind is ActionKind.CLICK
    assert await _wait_progress_index(page, "2") == "2", "点了下一题，进度要真的往后走"


async def _progress_index(page: Any) -> str | None:
    locator = page.locator('[data-quiz="progress-index"]')
    if await locator.count() == 0:  # pragma: no cover - 靶场结构变化
        return None
    return (await locator.first.inner_text()).strip()


async def _wait_progress_index(page: Any, expected: str, timeout_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    current = ""
    while time.monotonic() < deadline:
        current = (await _progress_index(page)) or ""
        if current == expected:
            return current
        await page.wait_for_timeout(80)
    return current


async def test_submit_by_coordinates_reaches_the_result_panel(page, mock_base) -> None:
    """提交只点一次；点完页面必须给出结果面板（P6 第一验收口径的收尾段）。"""
    actuator = Actuator(page, RunConfig())
    await open_quiz(page, quiz_url(mock_base, seq=1))
    scope = await quiz_scope(page)
    assert await wait_options(scope) >= 2

    option_box, size = await correct_option_region(page, scope)
    assert (await actuator.select_option(option_box, size, QType.SINGLE)).ok is True

    submit_box, submit_size = await element_region(
        page, scope.locator('[data-quiz="submit"]').first
    )
    submitted = await actuator.submit(submit_box, submit_size, target_label="submit:q1")
    assert submitted.ok is True, submitted.error

    await page.wait_for_selector('[data-quiz="result"]', state="visible", timeout=5000)
    text = await scope.locator('[data-quiz="result"]').first.inner_text()
    assert "正确" in text, f"结果面板应报正确，实际：{text}"


async def test_play_media_uses_trusted_gesture_not_script(page, mock_base, adapter) -> None:
    """媒体阶梯写反就会永远抛 ``NotAllowedError``（风险 #4）。

    这里要看到的是：真实点击（L1）就够了 —— ``level_used`` 必须是 L1，
    而不是垫底的 ``evaluate("video.play()")``（L2）。
    """
    await page.goto(course_url(mock_base, dur=8), wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_selector('[data-media="video"]', state="attached", timeout=8000)
    await page.wait_for_function(
        "() => document.body.getAttribute('data-current-episode') === '1'", timeout=8000
    )

    actuator = Actuator(page, RunConfig())
    result = await actuator.play_media(adapter)

    assert result.ok is True, f"播放失败：{result.error}"
    assert result.level_used is ActLevel.L1_LOCATOR, "可信手势优先，别一上来就用 evaluate"
    assert (await read_video_state(page, adapter)).paused is False

    paused = await actuator.pause_media(adapter)
    assert paused.ok is True, f"暂停失败：{paused.error}"
    assert (await read_video_state(page, adapter)).paused is True


async def test_pause_media_is_idempotent(page, mock_base, adapter) -> None:
    """已经暂停就别再按播放键 —— 那是 toggle，会把视频点开。"""
    await page.goto(course_url(mock_base, dur=8), wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_selector('[data-media="video"]', state="attached", timeout=8000)

    result = await Actuator(page, RunConfig()).pause_media(adapter)

    assert result.ok is True
    assert (result.readback or "").startswith("skipped:"), "已暂停 → 不动手"
    assert "paused=True" in (result.readback or "")
    assert (await read_video_state(page, adapter)).paused is True


async def test_next_episode_advances_index(page, mock_base, adapter) -> None:
    """M5-3 的「点击下一集 → 回读分集索引 +1」在 P6 已可用。"""
    await page.goto(course_url(mock_base, dur=8), wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_selector('[data-media="video"]', state="attached", timeout=8000)
    # 靶场 boot 是异步的（先 fetch course.json 再 loadEpisode），必须等第 1 集真正挂上
    await page.wait_for_function(
        "() => document.body.getAttribute('data-current-episode') === '1'", timeout=8000
    )

    result = await Actuator(page, RunConfig()).next_episode(adapter)

    assert result.ok is True, f"next_episode 失败：{result.error}"
    assert result.readback and "index=2" in result.readback


# --------------------------------------------------------------------------- #
# 滑动手势（P12）[unit] + [e2e]
# --------------------------------------------------------------------------- #
class _GestureMouse:
    def __init__(self) -> None:
        self.events: list[tuple[str, float, float]] = []

    async def move(self, x: float, y: float, **kwargs: Any) -> None:
        self.events.append(("move", float(x), float(y)))

    async def down(self, **kwargs: Any) -> None:
        self.events.append(("down", 0.0, 0.0))

    async def up(self, **kwargs: Any) -> None:
        self.events.append(("up", 0.0, 0.0))

    def path(self) -> list[tuple[float, float]]:
        return [(x, y) for name, x, y in self.events if name == "move"]


class _GesturePage:
    """只提供滑动手势用到的那几个口子（``viewport_size`` 故意缺失）。"""

    def __init__(self, *, inner: tuple[int, int] | None = (1000, 600)) -> None:
        self.mouse = _GestureMouse()
        self._inner = inner

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "innerWidth" in script:
            if self._inner is None:
                raise RuntimeError("页面已关闭")
            return list(self._inner)
        return None


def test_swipe_path_keeps_both_ends_inside_the_viewport() -> None:
    """两端都留在视口内：从屏幕边缘起手会被当成系统手势丢掉。"""
    for direction, expected in (
        ("left", ((800.0, 300.0), (200.0, 300.0))),
        ("right", ((200.0, 300.0), (800.0, 300.0))),
        ("up", ((500.0, 480.0), (500.0, 120.0))),
        ("down", ((500.0, 120.0), (500.0, 480.0))),
    ):
        assert _swipe_path(direction, 1000.0, 600.0, 0.6) == expected, direction


async def test_swipe_falls_back_to_inner_width_when_viewport_size_is_missing() -> None:
    """CDP 附加来的页面 ``page.viewport_size`` 常是 ``None`` —— 必须回退读页内尺寸。"""
    page = _GesturePage()
    actuator = Actuator(page, RunConfig())  # type: ignore[arg-type]

    result = await actuator.swipe("left")

    assert result.ok is True, result.error
    assert result.kind is ActionKind.SWIPE
    assert result.level_used is ActLevel.L6_VISION_XY, "手势是坐标级动作"
    path = page.mouse.path()
    assert path[0] == pytest.approx((800.0, 300.0)), "先移动到起点"
    assert path[-1] == pytest.approx((200.0, 300.0))
    # 起点 + 位移点：**必须有中间点**，只发 down/up 会被手势库判成点击
    assert len(path) == SWIPE_STEPS + 1


async def test_swipe_reports_failure_when_the_viewport_is_unknown() -> None:
    """读不到视口 → ``ok=False`` 且错误可查，**不抛异常**（留给编排层决定怎么办）。"""
    actuator = Actuator(_GesturePage(inner=None), RunConfig())  # type: ignore[arg-type]

    result = await actuator.swipe("up")

    assert result.ok is False
    assert result.error and "无法做滑动手势" in result.error


async def test_swipe_rejects_an_unknown_direction() -> None:
    actuator = Actuator(_GesturePage(), RunConfig())  # type: ignore[arg-type]

    result = await actuator.swipe("diagonal")

    assert result.ok is False
    assert result.error and "unknown_direction" in result.error


async def test_swipe_uses_button_mode_from_config() -> None:
    """``advance_swipe_mode=touch`` 要真的走触摸路径（没有 CDP 会话就如实报错）。"""
    cfg = RunConfig(guards=GuardThresholds(advance_swipe_mode="touch"))
    actuator = Actuator(_GesturePage(), cfg)  # type: ignore[arg-type]

    result = await actuator.swipe("left")

    assert result.ok is False
    assert result.error and "touch 手势需要" in result.error


async def test_swipe_drag_is_seen_by_pointer_handlers(page) -> None:
    """[e2e] 真浏览器：滑动手势必须能让 ``pointer`` 事件处理器看见一串位移。

    这条是「滑动下一题」能不能成的**机制**证明：只发 ``down/up`` 不挪位置，
    手势库收到的就是一次点击，页面根本不会翻。
    """
    await page.set_content(
        """
        <div id="card" style="width:100%;height:600px"></div>
        <script>
          window.__log = [];
          const card = document.getElementById('card');
          ['pointerdown', 'pointermove', 'pointerup'].forEach(function (type) {
            card.addEventListener(type, function (event) {
              window.__log.push(type + ':' + Math.round(event.clientX));
            });
          });
        </script>
        """
    )

    result = await Actuator(page, RunConfig()).swipe("left")

    assert result.ok is True, result.error
    log = await page.evaluate("() => window.__log")
    kinds = [entry.split(":")[0] for entry in log]
    assert kinds[-1] == "pointerup", f"末尾必须是抬手：{kinds}"
    down_at = kinds.index("pointerdown")
    # 按下**之后**的位移才是拖动本身；按下之前那一次是「把鼠标挪到起点」
    dragged = [int(entry.split(":")[1]) for entry in log[down_at:] if entry.startswith("pointermove")]
    assert len(dragged) >= SWIPE_STEPS, f"拖动位移点太少：{dragged}"
    assert dragged[-1] < dragged[0], "左滑必须是从右往左"


async def test_touch_swipe_reaches_touch_event_handlers(page) -> None:
    """[e2e] 只认 ``TouchEvent`` 的老式移动端页面：``touch`` 模式要真的送到。"""
    await page.set_content(
        """
        <div id="card" style="width:100%;height:600px"></div>
        <script>
          window.__touch = [];
          const card = document.getElementById('card');
          ['touchstart', 'touchmove', 'touchend'].forEach(function (type) {
            card.addEventListener(type, function () { window.__touch.push(type); });
          });
        </script>
        """
    )

    result = await Actuator(page, RunConfig()).swipe("up", mode="touch")

    assert result.ok is True, result.error
    log = await page.evaluate("() => window.__touch")
    assert log[0] == "touchstart" and log[-1] == "touchend", f"实际 {log}"
    assert log.count("touchmove") >= SWIPE_STEPS, f"位移点太少：{log}"
