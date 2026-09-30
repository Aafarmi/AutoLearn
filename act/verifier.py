"""校验层（M3-5 / M5 进度断言，v0.2.0 精简）。

题目侧只剩一条判据：**点前后比对选项区域像素**
（:meth:`Verifier.verify_region_changed`）。原先那两个读页面结构的断言 ——
一个回读选项的选中态（``checked`` / ``aria-checked`` / class），
一个等结果面板出现（靠站点选择器）—— 已经删除：v0.2.0 起程序不再解析
站点文档结构，它们没有可读的对象了（连同那一层回读适配模块一起下线）。

.. note::
   「区域变了」只能证明**画面上有东西变了**，证明不了变的就是我们要的那一项。
   所以这条判据配着重放，以及「没变就停下留档」：宁可停在「没验成」，
   也不能把一次误点当成成功放过去。真正「选的是不是正确选项」由解题组
   与提交后的结果面板回答，不归这一层。

.. note::
   **2026-09-29：这条判据从布尔升级成三态**（:class:`RegionChange` /
   :func:`region_change_state`）。原先只有一条线（``mad >= 2.0`` 才算变），
   而真实站点上「已选中」常常只值 0.1~1.5 —— 于是**点对了却被判成没点中**，
   执行层回同一个框里再点几个候选点，多选 / 复选上那是把勾取消（用户报的
   「胡乱操作」）。现在 ``weak``（0.05~2.0）也算「画面响应了」，
   处置由 :meth:`act.actuator.Actuator.select_option` 的判定表决定。

媒体侧照旧
----------
媒体态（``paused`` / ``ended`` / ``currentTime`` / ``duration``）是 ``<video>``
自己的属性，不属于「题目通道」，所以四条断言全部保留，量化口径也不动：

========================  ==================================================
播放态推进                ``window_s`` 内 ``ΔcurrentTime ≥ min_delta``（默认 3s / 1.0s）
暂停态静止                ``window_s`` 内 ``ΔcurrentTime ≤ max_delta``（默认 3s / 0.2s）
恢复位置连续              ``|currentTime − 挂起时| ≤ RESUME_MAX_DRIFT_S``
                          且 ``currentTime < duration``
分集索引变化              点击下一集后索引严格 ``+1``
========================  ==================================================

两类断言别混用（P6 落地补充）
----------------------------
- **时间窗断言**（:meth:`verify_playing` / :meth:`verify_paused`）：要等满 3s，
  证据强但慢。用于「这一步是不是真的成了」这种**一次性质检**（P8 网课流程）。
- **瞬时断言**（:meth:`verify_media_flag`）：读一帧，只对 ``paused`` / ``ended`` /
  ``currentTime`` 做等值比较。用于**动作阶梯的逐级确认** ——
  六级阶梯每级都等 3s 是 18s，实战里不可接受。

媒体态一律读 ``paused`` / ``ended`` / ``currentTime`` / ``duration``，
**不得用元素可见性代替**（任务书 §2.1）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from act.screen import (
    ImageSize,
    NormBox,
    region_mean_abs_diff,
    short,
    shot_viewport,
)
from core.enums import VerifyKind
from core.models import VerifyResult, VideoState
from perception.base import MediaNotAvailableError
from perception.media_probe import read_episode_index, read_video_state

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

    from adapters.base import BaseAdapter
    from core.config import RunConfig
    from core.trace import EventBus, RunLogger

__all__ = [
    "PAUSE_MAX_DELTA_S",
    "PAUSE_WINDOW_S",
    "PLAY_MIN_DELTA_S",
    "PLAY_WINDOW_S",
    "REGION_CHANGE_EXPECTED",
    "REGION_CHANGE_MIN_MAD",
    "REGION_CHANGE_WEAK_MAD",
    "RESUME_MAX_DRIFT_S",
    "SEEK_TOLERANCE_S",
    "RegionChange",
    "Verifier",
    "VerifyKind",
    "region_change_state",
]

logger = logging.getLogger(__name__)

#: 播放态推进断言：采样窗口 3s 内 ΔcurrentTime ≥ 1.0s
PLAY_WINDOW_S = 3.0
PLAY_MIN_DELTA_S = 1.0

#: 暂停态静止断言：采样窗口 3s 内 ΔcurrentTime ≤ 0.2s
PAUSE_WINDOW_S = 3.0
PAUSE_MAX_DELTA_S = 0.2

#: 恢复位置连续断言：与挂起时的偏差 ≤ 2s
RESUME_MAX_DRIFT_S = 2.0

#: seek 达成判定的容差（WAV 合成源 + Range 请求，落点有几十毫秒抖动）
SEEK_TOLERANCE_S = 1.0

#: 判定「选项区域**明显**变了」的平均绝对差阈值（灰度 0~255）。
#:
#: 为什么偏偏是 2.0（数字来自靶场真机实测，见 :meth:`Verifier.verify_region_changed`）：
#:
#: - **不能取 0**：点下去到画出来之间有渲染空窗（靶场选中态是个 120ms 的 CSS
#:   过渡）。点完立刻截图只差 ``1.35``，过渡结束才是 ``14.3`` —— 阈值贴着 0
#:   就等于把「还没画出来」判成「已经变了」，校验形同虚设。
#: - **不能取太大**：真实站点上选中态有时只是画一条 1px 描边或一个小圆点，
#:   整块区域的平均差本就不高。阈值定高了会把**真的选中**判成没变，
#:   接着重放又去点一下 —— 多选题上那一高清空的是刚选好的勾。
#:   （2026-09-29 起这一档由 :data:`REGION_CHANGE_WEAK_MAD` 单独接住：
#:   2.0 只用来区分「明显变了」与「轻轻动了一下」，不再是「变没变」的生死线。）
#: - **静态噪声实测是 0.00**（PNG 无损、区域无动画）：同一块区域连拍两帧逐像素
#:   相等，所以这 2.0 全是留给「真的有东西变了」的余量，不用去垫编解码噪声。
REGION_CHANGE_MIN_MAD = 2.0

#: 「确实有东西动过」的**地板**阈值（灰度 0~255）。2026-09-29 新增，用来把
#: 「变了没有」从布尔升级成**三态**（见 :func:`region_change_state`）。
#:
#: 为什么是 0.05（依据与 ``REGION_CHANGE_MIN_MAD`` 同源）：PNG **无损**截图下，
#: 同一静态区域连拍两帧逐像素实测差是 **0.00**（见上面那条注释的实测记录）。
#: 既然噪声地板是 0.00，任何非零的量级差异都只能来自「画面真的被改过」，
#: 0.05 已经是可靠信号，而且**远低于** 2.0。
#:
#: 为什么单靠 2.0 不够（本条阈值存在的理由）：真实站点上「已选中」的视觉变化
#: 常常很轻 —— 1px 描边、一个小圆点、一点点底色差，整块区域的平均绝对差
#: 只有 0.1~1.5。把它判成「没变」的后果不是「多等一会儿」，而是执行层
#: 在**同一个框里**再点 1~3 个候选点：多选 / 复选上那是把刚选上的勾**取消**，
#: 框略偏时还会点到别处 —— 这就是用户报的「点对了却被判成没点中，于是乱点」。
REGION_CHANGE_WEAK_MAD = 0.05

#: 三态判定里「期望」那一栏的**唯一文案**（进 ``VerifyResult.expected`` 与
#: ``ACT_READBACK_MISMATCH`` 事件）。它说的是 ``changed`` 那条线 ——
#: 判据放宽到 ``weak`` 之后这条文案也**不改**：它只在「区域真的没动」时
#: 被读到（那时 mad < 0.05，与 ``≥2.0`` 的落差正是要报告的落差）。
REGION_CHANGE_EXPECTED = f"region_mad≥{REGION_CHANGE_MIN_MAD:g}"

#: 差分判定的重拍节奏：过渡动画结束前多拍几帧，取其中最大的一次差值。
#:
#: 2026-09-29：预算从 4 帧 × 80ms ≈ 320ms 提到 **6 帧 × 120ms ≈ 720ms**。
#: 真实站点的过渡动画常在 300~600ms，320ms 的窗口会在动画画完之前就收工，
#: 于是「点对了」被判成「没点中」—— 这是上面那条误判链的次要成因。
#: 失败路径上这点等待换的是「不误判」，划算。
_REGION_SETTLE_ATTEMPTS = 6
_REGION_SETTLE_GAP_S = 0.12

#: 媒体态读取失败时的重试节奏（元素刚 `load()` 完 `duration` 可能还是 NaN）
_MEDIA_READ_ATTEMPTS = 3
_MEDIA_READ_GAP_S = 0.15


def region_change_state(mad: float) -> str:
    """平均绝对差 → 三态标签（``"changed"`` / ``"weak"`` / ``"none"``）。

    口径（三态判定的**唯一定义点**，别在调用方重新划线）：

    ==========================  ============
    ``mad >= 2.0``              ``changed``  明显变了（靶场选中态实测 14.3）
    ``0.05 <= mad < 2.0``       ``weak``     确实有东西动过，只是很轻（1px 描边 / 小圆点）
    ``mad < 0.05``              ``none``     一个像素都没动（无损截图下噪声地板是 0.00）
    ==========================  ============
    """
    if mad >= REGION_CHANGE_MIN_MAD:
        return "changed"
    if mad >= REGION_CHANGE_WEAK_MAD:
        return "weak"
    return "none"


@dataclass(frozen=True)
class RegionChange:
    """一次「区域变了没有」的测量结果 —— **三态，不是布尔**。

    为什么不用布尔：``REGION_CHANGE_MIN_MAD``（2.0）是「明显变了」的门槛，
    而真实站点上「已选中」常常只值 0.1~1.5。布尔判定会把这一档压成「没变」，
    下游的处置却是「换个候选点再点一下」—— 在多选 / 复选上那是把勾取消。
    三态把这一档单独交出来，让**处置**（见
    :meth:`act.actuator.Actuator.select_option`）按状态分派，而不是按 true/false。
    """

    #: 本次判定取到的最大的平均绝对差（灰度 0~255，取数帧里的最大值）
    mad: float
    #: ``"changed"`` / ``"weak"`` / ``"none"``，口径见 :func:`region_change_state`
    state: str
    #: 拍到**并量成差值**的帧数；``0`` = 一帧都没量成（截图失败 / 尺寸对不上）。
    #: 注意 ``samples == 0`` 与 ``state == "none"`` 不是一回事：前者是「判不了」，
    #: 后者是「判了，确实没动」。调用方**不许**把「判不了」当成「确实没变」。
    samples: int


class Verifier:
    """所有「做完了没有」的判断都在这里，返回 ``VerifyResult`` 而非裸 ``bool``。"""

    def __init__(
        self,
        page: Page,
        cfg: RunConfig,
        *,
        run_logger: RunLogger | None = None,
        item_id: str | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.page = page
        self.cfg = cfg
        # 「失败一律暂停 + 截图 + 留档」的落地点；不注入时只返回结论、不落盘。
        self.run_logger = run_logger
        self.item_id = item_id
        #: 目前没有校验事件要发（失败留痕走 ``screenshot_ref`` + 执行层的
        #: ``pause`` 信号）。保留入参是为了让执行层用同一套构造参数装配两者，
        #: 不必为「谁会发事件」记两套签名。
        self.bus = bus

    # ------------------------------------------------------------------ 题目侧

    async def measure_region_change(
        self,
        box: NormBox,
        size: ImageSize,
        before: bytes,
    ) -> RegionChange:
        """重拍若干帧取**最大差**，判成三态（``changed`` / ``weak`` / ``none``）。

        ``before`` 是点击之前那一帧的 PNG（由 :class:`act.actuator.Actuator`
        截好交进来），这里负责截「现在」这一帧并比对区域内的平均绝对差。

        为什么要重拍几帧而不是拍一张就下结论：点到画上有渲染空窗。
        靶场实测 —— 立刻拍 ``region_mad=1.35``（过渡刚开始），过渡结束 ``14.3``；
        单拍一张会把「正在变」判成「没变」，于是执行层重放又点一下，
        而多选题上那一下会把刚选好的勾**取消**。所以取数帧里的**最大**差值。

        判据（阈值与三态口径见 :data:`REGION_CHANGE_WEAK_MAD` /
        :func:`region_change_state`）：``changed`` 与 ``weak`` 都表示**画面确实
        响应了这次点击**，只是强弱不同；只有 ``none`` 才是「一个像素都没动」。
        调用方怎么处置这三态**不在这里决定** —— 见
        :meth:`act.actuator.Actuator.select_option` 的判定表。
        """
        change, _error = await self._measure_region_change(box, size, before)
        return change

    async def verify_region_changed(
        self,
        box: NormBox,
        size: ImageSize,
        before: bytes,
    ) -> VerifyResult:
        """选项区域**点前 / 点后像素差分**（题目侧唯一判据）。

        语义（2026-09-29 起，签名与返回类型不变）：``ok = state in {"changed",
        "weak"}`` —— 2026-09-29 之前的实现只认 ``mad >= 2.0``，真实站点上
        「已选中」那点轻变化（0.1~1.5）会被判成「没点中」，执行层于是回同一个框里
        再点几个候选点（多选 / 复选上那是把勾取消）。三态与阈值依据见
        :data:`REGION_CHANGE_WEAK_MAD`；``mad`` 与 ``state`` 都写进 ``actual``，
        留痕里一眼看得出是「明显变了」还是「轻轻动了一下」。

        差分做不成（图解不开 / 尺寸与给坐标时那一帧对不上）时 ``ok=False``
        且 ``actual`` 说明原因 —— 与「区域没变」一样都不算验成，
        但两者在留痕里必须分得清：一个是页面事实，一个是这次取帧出了问题。
        """
        change, error = await self._measure_region_change(box, size, before)
        if error is not None:
            ref = await self._capture("region_unverified")
            return VerifyResult(
                ok=False,
                kind=VerifyKind.SCREENSHOT_DIFF,
                expected=REGION_CHANGE_EXPECTED,
                actual=error,
                screenshot_ref=ref,
            )

        verdict_ok = change.state in {"changed", "weak"}
        # 没变 = 判据没成立，按「失败自带证据」留一张图（执行层随后还会再截一张
        # 到阶段的 ``error.png``；两张用途不同：这张是**判据当时**的那一帧）。
        ref = None if verdict_ok else await self._capture("region_unchanged")
        return VerifyResult(
            ok=verdict_ok,
            kind=VerifyKind.SCREENSHOT_DIFF,
            expected=REGION_CHANGE_EXPECTED,
            actual=f"region_mad={change.mad:.2f} state={change.state}",
            screenshot_ref=ref,
        )

    async def _measure_region_change(
        self,
        box: NormBox,
        size: ImageSize,
        before: bytes,
    ) -> tuple[RegionChange, str | None]:
        """三态测量的**唯一实现**，顺带把「为什么量不成」交回给调用方。

        :meth:`measure_region_change` 与 :meth:`verify_region_changed` 共用这一段
        （两份循环迟早会在「拍几次」上分叉）。错误文本走这条私有回程是因为
        :class:`RegionChange` 只有三个字段 —— 拿它做处置的调用方（执行层）
        不该被「这次取帧是怎么回事」的细节绑架，而校验结果必须把两者分开：
        「页面事实（没变）」与「这次取帧出了问题（判不了）」在留痕里必须分得清。
        """
        best: float | None = None
        samples = 0
        last_error: str | None = None
        for attempt in range(_REGION_SETTLE_ATTEMPTS):
            try:
                after = await shot_viewport(self.page)
            except Exception as exc:
                last_error = f"截图失败：{short(exc)}"
                break
            mad = region_mean_abs_diff(before, after, box, size)
            if mad is None:
                last_error = "区域读不出来（不是 PNG，或尺寸与给坐标时那一帧不一致）"
                break
            samples += 1
            best = mad if best is None else max(best, mad)
            if mad >= REGION_CHANGE_MIN_MAD:
                break
            if attempt < _REGION_SETTLE_ATTEMPTS - 1:
                await asyncio.sleep(_REGION_SETTLE_GAP_S)

        if best is None:
            return RegionChange(mad=0.0, state="none", samples=samples), (
                last_error or "region_unreadable"
            )
        return RegionChange(mad=best, state=region_change_state(best), samples=samples), None

    # ------------------------------------------------------------------ 媒体读

    async def read_media(self, adapter: BaseAdapter) -> VideoState | None:
        """读一帧媒体态（读不到返回 ``None``，**不抛**）。

        执行层的幂等守卫（已在播就别再点、已暂停就别再按）与各条断言共用它。
        """
        for attempt in range(_MEDIA_READ_ATTEMPTS):
            try:
                return await read_video_state(self.page, adapter)
            except MediaNotAvailableError:
                return None
            except Exception as exc:  # 页面正在 seek / 导航，属性会短暂缺失
                logger.debug("read_media 第 %d 次失败：%s", attempt + 1, exc)
                await asyncio.sleep(_MEDIA_READ_GAP_S)
        return None

    # ------------------------------------------------------------------ 媒体断言

    async def verify_playing(
        self,
        page: Page,
        adapter: BaseAdapter,
        window_s: float = PLAY_WINDOW_S,
        min_delta: float = PLAY_MIN_DELTA_S,
    ) -> VerifyResult:
        """播放态推进：``window_s`` 内 ``ΔcurrentTime ≥ min_delta``（M5 量化断言）。"""
        before = await self.read_media(adapter)
        if before is None:
            return self._media_missing(VerifyKind.MEDIA_PROGRESS)
        await asyncio.sleep(window_s)
        after = await self.read_media(adapter)
        if after is None:
            return self._media_missing(VerifyKind.MEDIA_PROGRESS)

        delta = after.current_time - before.current_time
        return VerifyResult(
            ok=delta >= min_delta,
            kind=VerifyKind.MEDIA_PROGRESS,
            expected=f"Δ≥{min_delta:.1f}s/{window_s:.1f}s",
            actual=f"Δ={delta:.2f}s (t={after.current_time:.2f}s)",
        )

    async def verify_paused(
        self,
        page: Page,
        adapter: BaseAdapter,
        window_s: float = PAUSE_WINDOW_S,
        max_delta: float = PAUSE_MAX_DELTA_S,
    ) -> VerifyResult:
        """暂停态静止：``window_s`` 内 ``ΔcurrentTime ≤ max_delta``（M5 量化断言）。"""
        before = await self.read_media(adapter)
        if before is None:
            return self._media_missing(VerifyKind.MEDIA_PAUSED)
        await asyncio.sleep(window_s)
        after = await self.read_media(adapter)
        if after is None:
            return self._media_missing(VerifyKind.MEDIA_PAUSED)

        delta = abs(after.current_time - before.current_time)
        return VerifyResult(
            ok=delta <= max_delta and after.paused,
            kind=VerifyKind.MEDIA_PAUSED,
            expected=f"Δ≤{max_delta:.1f}s/{window_s:.1f}s 且 paused=true",
            actual=f"Δ={delta:.2f}s paused={after.paused}",
        )

    async def verify_resume_continuous(
        self,
        page: Page,
        adapter: BaseAdapter,
        suspend_time: float,
    ) -> VerifyResult:
        """恢复位置连续：``|currentTime − 挂起时| ≤ RESUME_MAX_DRIFT_S`` 且未播完。"""
        state = await self.read_media(adapter)
        if state is None:
            return self._media_missing(VerifyKind.MEDIA_RESUME)

        drift = abs(state.current_time - suspend_time)
        within = state.current_time < state.duration if state.duration > 0 else True
        return VerifyResult(
            ok=drift <= RESUME_MAX_DRIFT_S and within,
            kind=VerifyKind.MEDIA_RESUME,
            expected=f"|Δt|≤{RESUME_MAX_DRIFT_S:.1f}s 且 t<duration",
            actual=f"Δt={drift:.2f}s t={state.current_time:.2f}s duration={state.duration:.2f}s",
        )

    async def verify_episode_advance(
        self,
        page: Page,
        adapter: BaseAdapter,
        before_index: int,
    ) -> VerifyResult:
        """分集索引变化：点击下一集后索引严格 ``+1``。"""
        try:
            current, total = await read_episode_index(self.page, adapter)
        except Exception as exc:  # pragma: no cover - 列表被替换
            logger.debug("分集索引读取失败：%s", exc)
            return self._media_missing(VerifyKind.EPISODE_INDEX)

        return VerifyResult(
            ok=current == before_index + 1,
            kind=VerifyKind.EPISODE_INDEX,
            expected=f"index={before_index + 1}",
            actual=f"index={current} (total={total})",
        )

    async def verify_media_flag(
        self,
        adapter: BaseAdapter,
        *,
        paused: bool | None = None,
        ended: bool | None = None,
        current_time: float | None = None,
        tolerance: float = SEEK_TOLERANCE_S,
    ) -> VerifyResult:
        """**瞬时**媒体态断言：读一帧，比对 ``paused`` / ``ended`` / ``currentTime``。

        与 :meth:`verify_playing` / :meth:`verify_paused` 的区别是它**不等时间窗** ——
        动作阶梯每升一级都等 3s 是不可接受的，所以逐级确认用这个；
        而「这一集是不是真的从头播到尾了」这种一次性质检用时间窗那两个。

        ``paused=False`` 是 M5-3 明写的「点击播放 → 回读 paused === false」；
        ``current_time`` 用于 seek 的落点判定（容差 ``tolerance``）。
        """
        state = await self.read_media(adapter)
        if state is None:
            return self._media_missing(VerifyKind.MEDIA_PROGRESS)

        ok = True
        parts: list[str] = [f"paused={state.paused}", f"ended={state.ended}"]
        expected: list[str] = []
        if paused is not None:
            ok = ok and state.paused is paused
            expected.append(f"paused={paused}")
        if ended is not None:
            ok = ok and state.ended is ended
            expected.append(f"ended={ended}")
        if current_time is not None:
            drift = abs(state.current_time - current_time)
            ok = ok and drift <= tolerance
            expected.append(f"t={current_time:.2f}±{tolerance:.1f}")
            parts.append(f"Δt={drift:.2f}s")

        return VerifyResult(
            ok=ok,
            kind=VerifyKind.MEDIA_PROGRESS,
            expected="; ".join(expected) or "media_flag",
            actual="; ".join(parts),
        )

    # ------------------------------------------------------------------ 处置

    def should_escalate(self, result: VerifyResult) -> bool:
        """校验失败是否应当上移一级动作阶梯。

        T0-5：回读不一致 → 先**原地重放** ``click_replay_max`` 次（间隔随机），
        仍不一致才上移一级。所以本方法只回答「这一级算不算数」——
        重放决策在 :class:`act.actuator.Actuator` 的阶梯驱动里，不在这里。
        """
        return not result.ok

    # ------------------------------------------------------------------ 内部

    def _media_missing(self, kind: VerifyKind) -> VerifyResult:
        return VerifyResult(
            ok=False,
            kind=kind,
            expected="media_available",
            actual="media:not_available",
        )

    async def _capture(self, stage: str) -> str | None:
        """视口截图留档（**禁用 ``full_page``**，见 ``tests/test_no_banned_waits.py``）。"""
        if self.run_logger is None or not self.item_id:
            return None
        try:
            data = await shot_viewport(self.page)
        except Exception as exc:  # pragma: no cover - 页面已关闭
            logger.debug("截图失败：%s", exc)
            return None
        return self.run_logger.save_screenshot(self.item_id, stage, data)
