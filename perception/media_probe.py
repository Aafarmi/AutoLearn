"""媒体感知探针（M0-7 / M5-3 / M5-4）。

两条铁律
--------
1. **媒体态必须读 ``paused`` / ``ended`` / ``currentTime`` / ``duration``**，
   绝不能用元素可见性代替（任务书 §2.1）。
2. **弹题不是媒体态**——弹窗遮罩不会让 ``paused`` 变真。所以
   :func:`wait_for_interrupt` 走 ``MutationObserver + 定时轮询`` 双保险，
   而不是监听媒体事件。

媒体锚点一律来自 ``adapters/<site>/selectors_media.yaml``，本模块零硬编码 DOM。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from core.enums import ProbeName
from core.models import Episode, PerceptionResult, VideoState
from perception.base import BaseProbe, MediaNotAvailableError

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

    from adapters.base import BaseAdapter
    from perception.pipeline import PerceptionContext

__all__ = [
    "ENDED_GRACE_S",
    "EPISODE_OUTCOME_ENDED",
    "EPISODE_OUTCOME_INTERRUPT",
    "INTERRUPT_POLL_S",
    "MediaProbe",
    "read_episode_catalog",
    "read_episode_index",
    "read_video_state",
    "wait_for_ended",
    "wait_for_episode_outcome",
    "wait_for_interrupt",
    "wait_for_playback",
]

logger = logging.getLogger(__name__)

#: 弹题轮询间隔的兜底默认值（YAML ``interrupt_detection.poll_interval_ms`` 优先）
INTERRUPT_POLL_S = 0.25

#: 一集的两条结局（M5-2 / M5-4）。**弹题先被检出**：
#: 同刻到达时处理完弹题再按 ``ended`` 收尾（该集视为已完成、不恢复播放）。
EPISODE_OUTCOME_INTERRUPT = "interrupt"
EPISODE_OUTCOME_ENDED = "ended"

#: 「接近片尾」到真正的 ``ended`` 之间的宽限窗（秒）。
#:
#: ``currentTime >= duration − ε`` 会比 ``ended`` **早 ε 触发**（靶场默认 0.35s）。
#: 若在那一帧就判「播完」，`?interrupt_at=end` 这种「弹题与 ended 同刻到达」的
#: 场景会把弹题**抢跑掉** —— 弹窗随后出现却再没人处理（P8 真机实测踩到）。
#: 因此「接近片尾」只作为**待定**，先给弹题一个露头的机会：
#: 弹题出现 → 返回 ``interrupt``；``ended`` 落地 → 返回 ``ended``；窗口用尽 → 返回 ``ended``。
ENDED_GRACE_S = 1.0

#: 页面侧状态的挂载键
_PAGE_STATE_KEY = "__al_media"

# 页面侧：装监听（幂等）。媒体事件 + 弹题的 MutationObserver + 定时轮询。
_ATTACH_JS = """
(spec) => {
  if (window[spec.key]) return window[spec.key];
  const state = {
    attached_at: Date.now(),
    events: [],              // 只留关键事件：play / pause / ended
    ended_count: 0,
    play_count: 0,
    pause_count: 0,
    interrupt_seen: false,
    interrupt_at: null,
    interrupt_gone_at: null,
    interrupt_count: 0,
    last_seen_at: null,
  };
  window[spec.key] = state;

  const video = document.querySelector(spec.video);
  if (video) {
    video.addEventListener('ended', () => { state.ended_count++; state.events.push('ended'); });
    video.addEventListener('play',  () => { state.play_count++;  state.events.push('play'); });
    video.addEventListener('pause', () => { state.pause_count++; state.events.push('pause'); });
  }

  // 弹题探测：MutationObserver 负责「不漏」，定时轮询负责「不瞎」
  const check = () => {
    const el = document.querySelector(spec.interrupt);
    const present = !!el;
    if (present) {
      state.last_seen_at = Date.now();
      if (!state.interrupt_seen) {
        state.interrupt_seen = true;
        state.interrupt_at = Date.now();
        state.interrupt_count++;
      }
      state.interrupt_gone_at = null;
    } else if (state.interrupt_seen && state.interrupt_gone_at === null) {
      state.interrupt_gone_at = Date.now();
    }
    return present;
  };

  try {
    const obs = new MutationObserver(check);
    obs.observe(document.documentElement, { childList: true, subtree: true });
    state.observer = true;
  } catch (err) {
    state.observer = false;
  }
  setInterval(check, spec.interval_ms);
  check();
  return state;
}
"""


async def read_video_state(page: Page, adapter: BaseAdapter) -> VideoState:
    """读一帧媒体态。

    媒体属性缺失（``NaN`` duration 等）一律归零，避免把 ``Infinity`` 传进断言。
    媒体元素不存在时抛 :class:`MediaNotAvailableError`，**绝不返回一个假状态**。
    """
    selector = adapter.media_anchors.video
    data = await page.evaluate(
        """(sel) => {
            const v = document.querySelector(sel);
            if (!v) return null;
            const dur = Number(v.duration);
            return {
                paused: !!v.paused,
                ended: !!v.ended,
                current_time: Number(v.currentTime) || 0,
                duration: isFinite(dur) && dur > 0 ? dur : 0,
                src: v.currentSrc || v.getAttribute('src') || null,
            };
        }""",
        selector,
    )
    if data is None:
        raise MediaNotAvailableError(f"未找到媒体元素：{selector}")

    current, total = await read_episode_index(page, adapter)
    return VideoState(
        paused=bool(data["paused"]),
        ended=bool(data["ended"]),
        current_time=float(data["current_time"]),
        duration=float(data["duration"]),
        episode_index=current,
        episode_total=total,
        src=data["src"],
    )


async def read_episode_index(page: Page, adapter: BaseAdapter) -> tuple[int, int]:
    """返回 ``(当前集索引, 总集数)``，索引从 1 开始；读不到时索引为 0。

    优先信 ``body[data-current-episode]``（靶场自己维护），再退到 ``data-active`` 项。
    """
    item_selector = adapter.media_selectors.get("episode_item")
    if not item_selector:
        return 0, 0
    total = await page.locator(item_selector).count()

    body_attr = adapter.field("body_episode_attr", media=True)
    if body_attr:
        raw = await page.evaluate(
            "(name) => (document.body ? document.body.getAttribute(name) : null)", body_attr
        )
        if raw and str(raw).lstrip("-").isdigit():
            return int(raw), total

    active_attr = adapter.field("episode_active_attr", media=True) or "data-active"
    active_true = adapter.field("active_true", media=True) or "true"
    index_attr = adapter.field("episode_index_attr", media=True) or "data-episode-index"
    active = page.locator(f'{item_selector}[{active_attr}="{active_true}"]')
    if await active.count() > 0:
        raw = await active.first.get_attribute(index_attr)
        if raw and str(raw).lstrip("-").isdigit():
            return int(raw), total
    return 0, total


async def read_episode_catalog(page: Page, adapter: BaseAdapter) -> list[Episode]:
    """读整张分集表（M5-1）。**只读 DOM**，选择器与属性名全部来自 YAML。

    读不出 ``episode_index`` 或 ``vid`` 的条目直接跳过（不报错）——
    分集表里混进装饰性元素时，宁可少一条也不要凭空造一条没有身份的条目。
    返回按 ``episode_index`` 升序。
    """
    item_selector = adapter.media_selectors.get("episode_item")
    if not item_selector:
        return []
    attrs = {
        "index": adapter.field("episode_index_attr", media=True) or "data-episode-index",
        "vid": adapter.field("episode_vid_attr", media=True) or "data-vid",
        "title": adapter.field("episode_title_attr", media=True) or "data-title",
        "duration": adapter.field("episode_duration_attr", media=True) or "data-duration",
    }
    rows = await page.locator(item_selector).evaluate_all(
        """(els, keys) => els.map(el => ({
            index: el.getAttribute(keys.index),
            vid: el.getAttribute(keys.vid),
            title: el.getAttribute(keys.title),
            duration: el.getAttribute(keys.duration),
        }))""",
        attrs,
    )

    episodes: list[Episode] = []
    for row in rows:
        index = _as_int(row.get("index"))
        vid = row.get("vid")
        if index is None or not vid:
            continue
        episodes.append(
            Episode(
                episode_index=index,
                title=str(row.get("title") or ""),
                vid=str(vid),
                duration=float(_as_int(row.get("duration")) or 0),
            )
        )
    episodes.sort(key=lambda item: item.episode_index)
    return episodes


async def wait_for_playback(
    page: Page,
    adapter: BaseAdapter,
    window_s: float = 3.0,
    min_delta: float = 1.0,
) -> bool:
    """播放态推进断言：采样窗口内 ``ΔcurrentTime ≥ min_delta``（任务书 M5）。"""
    before = await read_video_state(page, adapter)
    await asyncio.sleep(window_s)
    after = await read_video_state(page, adapter)
    return (after.current_time - before.current_time) >= min_delta


async def wait_for_ended(
    page: Page,
    adapter: BaseAdapter,
    timeout_s: float,
    poll_s: float = 0.5,
) -> bool:
    """等播放结束。**事件 + 轮询兜底**双路判定。

    - 事件路：页面侧监听器记到 ``ended_count > 0``；
    - 兜底路：``currentTime >= duration − ε`` —— ``ended`` 事件丢失时仍要能推进
      下一集（M5-4 的明确要求）。
    """
    epsilon = adapter.media_assertions.ended_epsilon_s
    deadline = time.monotonic() + timeout_s
    while True:
        state = await read_video_state(page, adapter)
        if state.ended:
            return True
        if state.duration > 0 and state.current_time >= state.duration - epsilon:
            return True
        fired = await page.evaluate(
            "(key) => !!(window[key] && window[key].ended_count > 0)", _PAGE_STATE_KEY
        )
        if fired:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)


async def wait_for_episode_outcome(
    page: Page,
    adapter: BaseAdapter,
    timeout_s: float,
    *,
    poll_s: float | None = None,
) -> str | None:
    """等一集出现两种结局之一：**弹题打断** 或 **播放结束**（M5-2 / M5-4）。

    判定顺序即优先级：**每轮先查弹题**。`?interrupt_at=end` 那种「弹题与 ``ended``
    同刻到达」的场景里，两条同时成立 —— 先返回 ``interrupt`` 让编排层去处理弹题，
    处理完再回读 ``ended`` 收尾（该集视为已完成、**不恢复播放**）。若反过来先返回
    ``ended``，弹窗会带着没答的题留在页面上，还会被「下一集」顺手关掉。

    ``ended`` 事件丢失时靠 ``currentTime ≥ duration − ε`` 与 ``video.ended``
    兜底（**不依赖页面侧的事件计数器**，见 ``tests/test_media_probe.py``）。

    返回 ``EPISODE_OUTCOME_INTERRUPT`` / ``EPISODE_OUTCOME_ENDED`` / ``None``（超时）。
    """
    poll = poll_s or adapter.interrupt_detection.poll_interval_ms / 1000.0 or INTERRUPT_POLL_S
    interrupt_selector = adapter.media_anchors.interrupt
    epsilon = adapter.media_assertions.ended_epsilon_s
    deadline = time.monotonic() + timeout_s
    near_end_at: float | None = None
    while True:
        # 弹题优先：弹窗出现时 `paused` 仍是 false，不能靠媒体态判
        if await page.locator(interrupt_selector).count() > 0:
            return EPISODE_OUTCOME_INTERRUPT
        try:
            state = await read_video_state(page, adapter)
        except MediaNotAvailableError:
            return None
        if state.ended:
            return EPISODE_OUTCOME_ENDED
        if state.duration > 0 and state.current_time >= state.duration - epsilon:
            # 先判「待定」，给同刻到达的弹题一个露头的机会（见 ENDED_GRACE_S）
            if near_end_at is None:
                near_end_at = time.monotonic()
            elif time.monotonic() - near_end_at >= ENDED_GRACE_S:
                return EPISODE_OUTCOME_ENDED
        else:
            near_end_at = None
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll)


async def wait_for_interrupt(
    page: Page,
    adapter: BaseAdapter,
    timeout_s: float,
    *,
    reset: bool = True,
) -> bool:
    """等弹题出现。要求弹题出现 1s 内触发，且无弹题场景连续 60s 零误报（M0 验收）。

    弹题不是媒体态，所以**不读 ``paused``**；靠页面侧 MutationObserver + 定时轮询
    维护的 ``interrupt_seen`` 标志判定，并在每轮直接查一次 DOM 兜底。

    ``reset=True``（默认）时不认上一集遗留的标志：先清位再等；
    但若此刻弹题**正开着**则立即返回 ``True``。
    """
    selector = adapter.media_anchors.interrupt
    poll_s = adapter.interrupt_detection.poll_interval_ms / 1000.0 or INTERRUPT_POLL_S
    key = _PAGE_STATE_KEY

    if await page.locator(selector).count() > 0:
        return True

    if reset:
        await page.evaluate(
            """(key) => { const s = window[key]; if (s) {
                    s.interrupt_seen = false;
                    s.interrupt_at = null;
                    s.interrupt_gone_at = null; } }""",
            key,
        )

    deadline = time.monotonic() + timeout_s
    while True:
        if await page.evaluate("(key) => !!(window[key] && window[key].interrupt_seen)", key):
            return True
        if await page.locator(selector).count() > 0:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)


def _as_int(value: object) -> int | None:
    """把 DOM 属性读来的字符串转成整数；非数字一律 ``None``（不抛）。"""
    if value is None:
        return None
    text = str(value).strip().lstrip("+")
    if not text.lstrip("-").isdigit():
        return None
    try:
        return int(text)
    except ValueError:  # pragma: no cover - isdigit 已挡住
        return None


class MediaProbe(BaseProbe):
    """媒体感知探针。

    **不在题目探针链里**，由 ``PerceptionPipeline.run_video()`` 直接调度。
    必须先 ``bind(adapter)`` 才能 ``attach()`` —— 选择器只来自 YAML，本模块零硬编码。
    """

    name = ProbeName.MEDIA

    def __init__(self) -> None:
        super().__init__()
        self._bound = False
        self._video_selector = ""
        self._interrupt_selector = ""
        self._interval_ms = 120

    def bind(self, adapter: BaseAdapter) -> None:
        """把媒体锚点交给探针（``attach`` 前必须调用一次）。"""
        self._video_selector = adapter.media_anchors.video
        self._interrupt_selector = adapter.media_anchors.interrupt
        self._interval_ms = adapter.interrupt_detection.observer_interval_ms
        self._bound = True

    async def attach(
        self,
        page: Page,
        adapter: BaseAdapter | None = None,
    ) -> None:
        """注册 ``timeupdate`` / ``ended`` / ``pause`` 监听 + 弹题探测，幂等。"""
        if adapter is not None:
            self.bind(adapter)
        if not self._bound:
            raise RuntimeError(
                "MediaProbe 未绑定适配器：请先 bind(adapter) 或 attach(page, adapter)"
            )
        if id(page) in self._attached_pages:
            return
        await super().attach(page)
        await page.evaluate(
            _ATTACH_JS,
            {
                "key": _PAGE_STATE_KEY,
                "video": self._video_selector,
                "interrupt": self._interrupt_selector,
                "interval_ms": self._interval_ms,
            },
        )

    async def is_available(self, page: Page, adapter: BaseAdapter) -> bool:
        return await page.locator(adapter.media_anchors.video).count() > 0

    async def probe(
        self,
        page: Page,
        adapter: BaseAdapter,
        ctx: PerceptionContext,
    ) -> PerceptionResult:
        try:
            state = await read_video_state(page, adapter)
        except MediaNotAvailableError as exc:
            return self._failed(self.name, "media:not_available", str(exc))
        return PerceptionResult(
            question=None,
            video_state=state,
            channel_used=self.name,
            warnings=["media:attributes_only"],
        )

    async def page_events(self, page: Page) -> dict:
        """取页面侧记录的事件计数（供 M4 留痕与测试使用）。"""
        return await page.evaluate("(key) => window[key] || {}", _PAGE_STATE_KEY)
