"""P2 媒体感知验收（真跑网课靶场）。

逐条对应规划书 P2 / M0-7 验收口径：
    - 媒体探针能正确读出播放 / 暂停 / 结束三态
    - `wait_for_interrupt()` 在弹题出现 1s 内触发，且**不误报**
    - 弹题不是媒体态：弹窗出现时 `paused` 仍然是 false
"""

from __future__ import annotations

import asyncio
import time

import pytest

from perception.media_probe import (
    MediaProbe,
    read_episode_index,
    read_video_state,
    wait_for_ended,
    wait_for_interrupt,
    wait_for_playback,
)
from perception.pipeline import PerceptionContext
from tests.helpers import course_url

PLAY = '[data-media="play-button"]'
VIDEO = '[data-media="video"]'
INTERRUPT = '[data-media="interrupt"]'


async def _boot_course(page, mock_base, adapter, probe: MediaProbe, **params) -> None:
    """打开网课页并装上媒体监听（监听必须在导航后、播放前挂好）。"""
    await page.goto(course_url(mock_base, **params), wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_selector(VIDEO, state="attached", timeout=8000)
    # 靶场的 boot 是异步的：先 fetch course.json，再 loadEpisode(1)。
    # 只等 <video> 挂上会读到「还没装源」的那一帧（src 为空、duration=NaN、
    # 分集索引为 0），表现为偶发失败 —— 所以必须等第 1 集真的挂上。
    await page.wait_for_function(
        "() => document.body.getAttribute('data-current-episode') === '1'", timeout=8000
    )
    probe.bind(adapter)
    await probe.attach(page)


async def _play(page) -> None:
    await page.locator(PLAY).click()
    await page.wait_for_timeout(250)


# --------------------------------------------------------------------------- #
# 三态
# --------------------------------------------------------------------------- #


async def test_initial_state_is_paused(page, mock_base, adapter, course_meta):
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe)

    state = await read_video_state(page, adapter)
    assert state.paused is True
    assert state.ended is False
    assert state.current_time == pytest.approx(0.0, abs=0.5)
    assert state.duration > 0
    assert state.episode_index == 1
    assert state.episode_total == len(course_meta["episodes"])
    assert state.src


async def test_playing_state_and_progress(page, mock_base, adapter):
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe, dur=20)
    await _play(page)

    state = await read_video_state(page, adapter)
    assert state.paused is False
    assert state.ended is False

    assert await wait_for_playback(page, adapter, window_s=2.0, min_delta=0.5) is True


async def test_paused_state_is_static(page, mock_base, adapter):
    """暂停态静止：采样窗口内 ΔcurrentTime ≤ 0.2s。"""
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe, dur=20)
    await _play(page)
    await page.wait_for_timeout(600)
    await page.locator(PLAY).click()  # 再点一次即暂停
    await page.wait_for_timeout(200)

    state = await read_video_state(page, adapter)
    assert state.paused is True
    assert await wait_for_playback(page, adapter, window_s=2.0, min_delta=0.5) is False


async def test_ended_state(page, mock_base, adapter):
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe, dur=3)
    await _play(page)

    assert await wait_for_ended(page, adapter, timeout_s=20) is True
    # `wait_for_ended` 可能先被「currentTime 逼近 duration」兜底判中，
    # 给 `ended` 事件一点时间落地，再断言终态。
    await page.wait_for_timeout(600)
    state = await read_video_state(page, adapter)
    assert state.ended is True
    assert state.paused is True
    assert state.duration > 0
    assert state.current_time >= state.duration - 1.0


async def test_ended_detected_by_polling_fallback(page, mock_base, adapter):
    """事件丢失时轮询兜底仍能判定结束（M5-4 要求）。

    这里把页面侧的事件计数抹掉，模拟「`ended` 事件丢失」，
    `wait_for_ended` 必须靠 `currentTime >= duration - ε` 兜底判出来。
    """
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe, dur=3)
    await _play(page)
    await page.evaluate(
        "(key) => { if (window[key]) { window[key].ended_count = 0; } }", "__al_media"
    )

    assert await wait_for_ended(page, adapter, timeout_s=20) is True


# --------------------------------------------------------------------------- #
# 弹题探测
# --------------------------------------------------------------------------- #


async def test_interrupt_does_not_touch_media_paused(page, mock_base, adapter):
    """关键约定：弹题弹窗**绝不暂停视频** —— 弹题不是媒体态。"""
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe, dur=20, interrupt_at=1)
    await _play(page)

    assert await wait_for_interrupt(page, adapter, timeout_s=6) is True
    assert await page.locator(INTERRUPT).count() == 1

    state = await read_video_state(page, adapter)
    assert state.paused is False, "弹题出现时视频必须仍在播放（暂停是系统的责任）"


async def test_interrupt_is_detected_within_one_second(page, mock_base, adapter):
    """弹题出现 1s 内触发探测。

    用一个独立的观察者在 Python 侧记录「弹题首次出现在页面上」的时刻，
    再与 `wait_for_interrupt` 返回的时刻相比，得到真实探测延迟。
    """
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe, dur=20, interrupt_at=1)
    await _play(page)

    appeared_at: list[float] = []

    async def watcher() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if await page.locator(INTERRUPT).count() > 0:
                appeared_at.append(time.monotonic())
                return
            await asyncio.sleep(0.03)

    watcher_task = asyncio.create_task(watcher())
    try:
        fired = await wait_for_interrupt(page, adapter, timeout_s=10)
        returned_at = time.monotonic()
    finally:
        watcher_task.cancel()

    assert fired is True
    assert appeared_at, "观察者没有看到弹题"
    latency = returned_at - appeared_at[0]
    assert latency < 1.0, f"探测延迟 {latency:.3f}s 超过 1s"


async def test_interrupt_wait_does_not_use_stale_flag(page, mock_base, adapter):
    """`reset=True` 时不许拿「上一次已经出现过的弹题」充数，否则会误报。

    步骤：弹题出现 → 关掉它（页面侧标志仍是 true）→ 暂停播放（不会再有新弹题）
    → 此时等待必须返回 False，且页面侧确实记录过 1 次，证明 False 不是「什么都没发生」。
    """
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe, dur=20, interrupt_at=1)
    await _play(page)
    assert await wait_for_interrupt(page, adapter, timeout_s=6) is True

    # 关掉弹题（提交后靶场会在 400ms 内移除弹窗）
    await page.locator(f'{INTERRUPT} [data-quiz="input"]').first.click()
    await page.locator(f'{INTERRUPT} [data-quiz="submit"]').first.click()
    await page.wait_for_selector(INTERRUPT, state="detached", timeout=4000)

    # 暂停播放：后续不会再产生新弹题
    await page.locator(VIDEO).evaluate("v => v.pause()")
    await page.wait_for_timeout(200)
    assert (await read_video_state(page, adapter)).paused is True

    events = await probe.page_events(page)
    assert events.get("interrupt_count", 0) >= 1, "页面侧应确实记录过弹题"

    assert await wait_for_interrupt(page, adapter, timeout_s=2) is False


async def test_interrupt_false_positive_free_window(page, mock_base, adapter):
    """不误报：无弹题场景连续 10s 零触发（对应验收里的 60s 窗口，见 slow 用例）。"""
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe, dur=30)
    await _play(page)

    started = time.monotonic()
    fired = await wait_for_interrupt(page, adapter, timeout_s=10)
    elapsed = time.monotonic() - started
    assert fired is False
    assert elapsed >= 9.5
    assert await page.locator(INTERRUPT).count() == 0


@pytest.mark.slow
async def test_interrupt_no_false_positive_for_sixty_seconds(page, mock_base, adapter):
    """P2 验收原话：无弹题场景**连续 60s 零触发**。"""
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe, dur=60)
    await _play(page)

    fired = await wait_for_interrupt(page, adapter, timeout_s=60)
    assert fired is False
    assert await page.locator(INTERRUPT).count() == 0


# --------------------------------------------------------------------------- #
# 分集索引
# --------------------------------------------------------------------------- #


async def test_episode_index_and_total(page, mock_base, adapter, course_meta):
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe)

    current, total = await read_episode_index(page, adapter)
    assert current == 1
    assert total == len(course_meta["episodes"])

    await page.locator('[data-media="next"]').click()
    await page.wait_for_timeout(300)
    current, total = await read_episode_index(page, adapter)
    assert current == 2, "点击下一集后索引必须严格 +1"


async def test_episode_anchors_carry_vid_and_duration(page, mock_base, adapter, course_meta):
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe)

    items = page.locator('[data-media="episode"]')
    assert await items.count() == len(course_meta["episodes"])
    vids = await items.evaluate_all("els => els.map(e => e.getAttribute('data-vid'))")
    assert all(vids)
    durations = await items.evaluate_all("els => els.map(e => e.getAttribute('data-duration'))")
    assert all(int(d) > 0 for d in durations)


# --------------------------------------------------------------------------- #
# 探针口径
# --------------------------------------------------------------------------- #


async def test_media_probe_returns_video_state(page, mock_base, adapter):
    probe = MediaProbe()
    await _boot_course(page, mock_base, adapter, probe)
    result = await probe.probe(page, adapter, PerceptionContext())

    assert result.video_state is not None
    assert result.question is None
    assert result.channel_used.value == "media"


async def test_media_probe_without_adapter_binding_is_explicit(page, mock_base):
    """没绑适配器就 attach → 明确报错，不静默降级。"""
    probe = MediaProbe()
    await page.set_content("<html><body></body></html>")
    with pytest.raises(RuntimeError):
        await probe.attach(page)
