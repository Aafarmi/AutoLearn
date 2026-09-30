"""执行层（M3-2 / M3-3 / M3-4，v0.2.0 起题目侧只按坐标点）。

题目侧：只剩一条路 —— 模型给坐标，我们点坐标
--------------------------------------------
v0.2.0 起题目侧不再解析页面结构，也就没有「元素级阶梯」可爬：能拿到的东西只有
模型给的归一化包围框。于是整条路径缩成一步：

    归一化框 ──×图像尺寸──▶ 视口 CSS 像素 ──▶ ``page.mouse.click(中心)``

**坐标换算只乘一次**（见 :func:`act.screen.norm_box_center`）：截图一律
``scale="css"``，图像像素 == 视口 CSS 像素，所以不再除 ``devicePixelRatio`` ——
那一层正是「在缩放显示器上点偏一半、而且看起来像偶发失败」的根源。

校验也不再回读页面属性，而是**点前后比对选项区域像素**
（:meth:`act.verifier.Verifier.measure_region_change`，判成
``changed`` / ``weak`` / ``none`` 三态）。「区域变了」证明不了变的就是它，
但这是画面里唯一读得到的证据；配着「按状态分派处置」与「没变就停」，
宁可停在「没验成」，也不把误点当成功。

**2026-09-29 的修复（判定表按落点分）**：落在墨迹（``ink_centroid``，
即这道题自己的文字 / 标号）上的那一下，``changed`` / ``weak`` / ``none``
**一律收工** —— 点在自己的内容上却一个像素都没动，最可能的解释是它
**本来就已经选中**（再点一次在多选 / 复选上会把勾取消，那正是用户报的
「胡乱操作」）；只有几何兜底（``box_center`` / ``left_half`` / ``upper_half``）
没变时才换下一个候选点。完整判定表见
:meth:`Actuator.select_option` 的 docstring。

「已经达成期望状态就不动手」这条纪律还剩两种形态
----------------------------------------------
1. **媒体侧照旧**：:meth:`Actuator._drive` 在每次尝试前先读一次 ``<video>``
   属性，已达成（在播 / 已暂停）就跳过 —— 播放键是 toggle，重复点会把视频点回去。
   跳过时 ``ActionResult.readback`` 以 ``skipped:`` 开头，留痕里一眼看得出。
2. **题目侧改成像素三态版**：多选重复点击会把勾**取消**，所以点击之后先按三态
   判一次；``changed`` / ``weak`` 都算「着了」，直接收工，绝不再点第二下。

媒体动作阶梯**顺序与元素点击相反**（M3-3）
------------------------------------------
``page.evaluate("video.play()")`` 是**不可信手势**，会被浏览器抛
``NotAllowedError``。所以媒体动作必须：

1. 先真实点击播放器区域（可信手势）；
2. 再试空格键（可信键盘手势）；
3. 最后才轮到 ``evaluate``。

顺序写反 → 播放**永远**失败。这是本项目排第 4 的风险项。

``seek`` 是唯一的例外：靶场的进度条是纯文本 ``<span>``，没有可点的 scrubber，
点击路径不可能落到指定秒数，因此 :data:`SEEK_LEVELS` 把脚本路径（``currentTime = s``）
排在第一位 —— 它不是"手势"，但它是唯一能精确到秒的机制。

``pause`` 是第二个例外（P8）：弹题弹窗是全屏遮罩，正好盖住播放按钮，
点击路径在「挂起视频」这个最关键的时刻**必然不可用**，因此
:data:`PAUSE_LEVELS` 把键盘（可信手势）与脚本（``pause()`` 不要手势）提到前面。

``swipe`` 不是阶梯动作（P12）
-----------------------------
「下一题」在有些页面上**根本没有可点的按钮**：整屏是一张答题卡，
翻页靠滑动手势。所以 :meth:`Actuator.swipe` 不走阶梯 —— 它没有可定位的
目标元素，只有「往哪个方向、划多远」。它落 ``ActLevel.L6_VISION_XY``：
六级里的最后一档本来就是「不靠页面结构、直接用坐标驱动输入」。

两种输入事件（由 ``guards.advance_swipe_mode`` 选，默认 ``mouse``）：

- ``mouse``：鼠标按下 → 中间点连续移动 → 抬起。Chromium 会把鼠标拖拽合成
  pointer 事件，现代手势库（PointerEvent 路线）都认它。**这是默认值**。
- ``touch``：CDP ``Input.dispatchTouchEvent``。只认 ``TouchEvent`` 的老式
  移动端页面才需要；用之前会开触摸模拟（否则页面在特性检测阶段就把触摸分支
  关掉了，事件发下去也没人听）。

中间点（:data:`SWIPE_STEPS`）是必需的：``down → move → up`` 一步到位会被
手势库判成**点击**。

P6 落地的四条纪律（v0.2.0 修订）
--------------------------------
1. **能不动手就不动手** —— 媒体侧读状态（见上），题目侧点击后先按三态比像素。
2. **动作没发出去就别重放同一个动作。** 坐标点击抛异常说明输入事件根本没发出去，
   重放同一个坐标没有意义（旧阶梯上这一支叫「上移一级」，而现在题目侧只剩一级），
   因此直接截图留档。只有「点下去了但画面没变」才按 T0-5 **换点**重试
   ≤ ``click_replay_max`` 次（间隔 ``click_replay_gap_ms`` 随机）——
   2026-09-29 起还有一个更强的约束：落在墨迹上的那一下就**没有第二次**
   （见 :meth:`Actuator.select_option` 的判定表）。
3. **提交不重试、不重放。** :meth:`Actuator.submit` 只点一次，
   既不走阶梯也不走重放 —— 提交是本项目唯一的不可逆动作。
4. **失败 → 截图 + ``failed`` + 发 ``pause`` 信号。**
   真正的「暂停」由编排层执行（硬约束：必停不自己 ``await``），
   执行层只把 ``ok=False`` / ``screenshot_ref`` / ``pause: true`` 交出去，
   **绝不静默跳过**。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from act.screen import (
    CandidatePoint,
    ImageSize,
    NormBox,
    candidate_points,
    norm_box_center,
    short,
    shot_viewport,
)
from act.verifier import (
    REGION_CHANGE_EXPECTED,
    SEEK_TOLERANCE_S,
    RegionChange,
    Verifier,
)
from core.enums import ActionKind, ActLevel, ErrorCode, QType
from core.events import Event
from core.models import ActionResult
from core.ratelimit import sleep_gap

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Locator, Page

    from adapters.base import BaseAdapter
    from core.config import RunConfig
    from core.trace import EventBus, RunLogger

__all__ = [
    "ACTION_TIMEOUT_MS",
    "LEVELS",
    "MEDIA_LEVELS",
    "PAUSE_LEVELS",
    "SEEK_LEVELS",
    "SUBMIT_TIMEOUT_MS",
    "SWIPE_DIRECTIONS",
    "SWIPE_STEPS",
    "ActLevel",
    "ActionKind",
    "Actuator",
    "ImageSize",
    "NormBox",
    "norm_box_center",
]

logger = logging.getLogger(__name__)

#: 元素动作每一级阶梯的等待预算。**不是**「重试次数」——
#: 一级只发一次输入事件，等不到可点就判失败、上移一级。
#: v0.2.0 起只剩媒体控件走元素路径（题目侧是坐标点击）。
ACTION_TIMEOUT_MS = 3000

#: 提交动作的等待预算。超时 → ``submit_timeout`` + 暂停等人（M3-6）。
#:
#: 旧实现里这条线由 ``locator.click(timeout=…)`` 自己画（等按钮「可点」）；
#: 坐标点击没有可等待的可点性判定，所以由 :meth:`Actuator.submit` 用
#: ``asyncio.wait_for`` 自己兜住 —— 页面卡死时同样必须**限时退出**并留档，
#: 而不是让整条运行挂在这里。
SUBMIT_TIMEOUT_MS = 10000

#: 媒体动作逐级确认的轮询预算（媒体态从"点了"到"真的动了"有个几十~几百 ms 的空窗）
MEDIA_CONFIRM_TIMEOUT_S = 2.0
MEDIA_CONFIRM_POLL_S = 0.1

#: 元素级阶梯（M3-2），**v0.2.0 起只剩媒体路径在用**。
#:
#: 原先它是题目侧的六级主阶梯；题目改成「模型给坐标」之后：
#:
#: - 元素级那四级没有了对象（视觉给的是坐标，不是选择器）；
#: - 视觉坐标那一级（``L6_VISION_XY``）也**从媒体阶梯里摘掉了**：
#:   题目侧的坐标现在由调用方显式传 ``box`` / ``size`` 进来
#:   （见 :meth:`Actuator.select_option`），不再有「回调去问坐标」这层间接；
#:   而媒体控件是**媒体锚点**寻址的（``selectors_media.yaml``），
#:   没有任何东西会产出「某个媒体控件的视觉坐标」。
#:   留着一级没人能供货的阶梯，只会让每次媒体动作白烧一次必然失败，
#:   而且最后报出来的是那一级自己造的错，与真实故障无关。
#:
#: 只剩 :meth:`Actuator.next_episode` 还按「先点按钮、点不动再换招」的顺序走，
#: 于是名字保留、用途收窄为**媒体下一集**。**顺序即降级顺序，不得重排。**
LEVELS: tuple[ActLevel, ...] = (
    ActLevel.L1_LOCATOR,
    ActLevel.L2_FORCE,
    ActLevel.L3_SCROLL,
    ActLevel.L4_FOCUS_KEYS,
    ActLevel.L5_BBOX,
)

#: 媒体动作阶梯（M3-3）。**刻意与 LEVELS 排序不同**。
#:
#: 映射关系与理由（P6 实现时照此，不要凭直觉改）：
#:
#: ===================  ==================================================
#: ``L1_LOCATOR``       真实点击播放器区域 —— 最可信手势，排第一
#: ``L4_FOCUS_KEYS``    焦点落到播放器后按空格 —— 可信键盘手势
#: ``L3_SCROLL``        滚动到播放器再点一次 —— 解决被遮挡
#: ``L5_BBOX``          坐标级点击（``bounding_box`` 给的已是 CSS 像素）
#: ``L2_FORCE``         ``evaluate("video.play()")`` —— **不可信手势，只能垫底**
#: ===================  ==================================================
#:
#: 前四级都是浏览器认可的**真实输入事件**；L2_FORCE 绕过输入路径直接用脚本
#: 调 ``play()``，会被自动播放策略抛 ``NotAllowedError``。它排在最后不是
#: 「更强」，而是「最后实在没辙才用」。
MEDIA_LEVELS: tuple[ActLevel, ...] = (
    ActLevel.L1_LOCATOR,
    ActLevel.L4_FOCUS_KEYS,
    ActLevel.L3_SCROLL,
    ActLevel.L5_BBOX,
    ActLevel.L2_FORCE,
)

#: seek 专用阶梯（P6 增量，见 ``README.md`` §P6）。
#: 脚本路径第一（唯一能精确落点），键盘（→ 默认跳 5s）其次，点击路径垫底。
SEEK_LEVELS: tuple[ActLevel, ...] = (
    ActLevel.L2_FORCE,
    ActLevel.L4_FOCUS_KEYS,
    ActLevel.L3_SCROLL,
    ActLevel.L5_BBOX,
    ActLevel.L1_LOCATOR,
)

#: 暂停专用阶梯（P8 增量，见 ``README.md`` §P8）。
#:
#: **为什么不能直接复用 MEDIA_LEVELS**：网课的弹题弹窗是全屏遮罩
#: （``.cs-interrupt { position: fixed; inset: 0; z-index: 50 }``），它盖住的正是
#: 播放按钮。真机实测（见验收报告 §六）：
#:
#: - ``L1_LOCATOR`` 的 ``locator.click()`` 在遮罩下**必然超时**（3s），
#:   而视频**仍在播放** —— 于是「挂起期间必须显式暂停媒体」（M5-2）根本达不成，
#:   「恢复位置连续（偏差 ≤2s）」的断言必然失败；
#: - ``L4_FOCUS_KEYS``（焦点 + 空格）与 ``L2_FORCE``（``video.pause()``）
#:   都不受遮罩影响。
#:
#: 所以暂停把这两条提到前面：**键盘仍是可信手势**（沿用 M3-3「可信手势优先」），
#: 脚本紧随其后 —— ``pause()`` 不像 ``play()`` 那样会被自动播放策略拒绝，
#: 它不要求用户手势。点击路径挪到最后（遮罩场景下它只是浪费 3s 超时）。
#: 播放仍走 :data:`MEDIA_LEVELS` 不变（``play()`` 必须由可信手势发起）。
PAUSE_LEVELS: tuple[ActLevel, ...] = (
    ActLevel.L4_FOCUS_KEYS,
    ActLevel.L2_FORCE,
    ActLevel.L3_SCROLL,
    ActLevel.L5_BBOX,
    ActLevel.L1_LOCATOR,
)

#: 需要「点完要等一会儿才见效」的媒体动作（确认时给一段轮询预算）
_MEDIA_KINDS: frozenset[ActionKind] = frozenset(
    {
        ActionKind.PLAY_MEDIA,
        ActionKind.PAUSE_MEDIA,
        ActionKind.SEEK_MEDIA,
        ActionKind.NEXT_EPISODE,
    }
)

#: 合法滑动方向（P12）。语义是**手指的移动方向**：
#: ``left`` = 手指从右往左划（轮播 / 答题卡的「下一页」），
#: ``up`` = 手指从下往上划（信息流式答题）。
SWIPE_DIRECTIONS: frozenset[str] = frozenset({"left", "right", "up", "down"})

#: 一次滑动拆成几步中间移动。**必须有中间点** —— 一步到位的
#: ``down → move → up`` 在多数手势库里会被判成「点击」而不是「滑动」。
SWIPE_STEPS = 8

#: ``touch`` 模式下触摸点的半径（CDP ``Input.dispatchTouchEvent`` 必填字段）
_TOUCH_RADIUS = 8.0

# 这里原先还有一个「去问视觉坐标」的回调（由编排层注入）与消费它的那一级。
# v0.2.0 把题目侧的坐标**改成显式入参**（``box`` / ``size`` 直接传进
# :meth:`Actuator.select_option`），媒体控件又是媒体锚点寻址的，
# 于是那个回调再没有任何调用方 —— 已删除。它留下的教训写进 ``LEVELS`` 的注释：
# **不要留一级没有人能供货的阶梯**。


class Actuator:
    """所有会改变页面状态的动作都从这里走。"""

    def __init__(
        self,
        page: Page,
        cfg: RunConfig,
        *,
        run_logger: RunLogger | None = None,
        item_id: str | None = None,
        bus: EventBus | None = None,
        action_timeout_ms: int = ACTION_TIMEOUT_MS,
    ) -> None:
        self.page = page
        self.cfg = cfg
        self.run_logger = run_logger
        self.item_id = item_id
        self.bus = bus
        self._action_timeout_ms = action_timeout_ms
        #: 动作前的滚动位置，动作结束后复位（降级过程可能把页面滚跑了）
        self._scroll_before: tuple[float, float] | None = None
        self._verifier: Verifier | None = None

    # ------------------------------------------------------------------ 题目

    async def select_option(
        self,
        box: NormBox,
        size: ImageSize,
        qtype: QType,
        *,
        target_label: str | None = None,
    ) -> ActionResult:
        """按坐标选中一个选项（单选 / 多选同一条路径）。

        点哪儿由 :func:`act.screen.candidate_points` 决定 —— **框内内容质心优先**，
        几何中心只是个兜底。这一条是 2026-09-28 事故（``logs/a090315220c6``）的直接修复：
        当时模型给的框比可点内容大得多（超星作业页的选项，文字只占框左侧一小段），
        而执行层点的是**框的几何中心**，于是 4 次尝试全落在行尾空白上，
        ``region_mad=0.00`` —— 页面上除计时器外一个像素都没动，题目以
        ``action_failed`` 停下。**问题不在"点得不够准"，在"点的地方根本不是选项"。**

        判定表（2026-09-29 修复「点对了却被判成没点中 → 执行层继续乱点」）
        ------------------------------------------------------------------
        判据由单阈值布尔改成三态（:meth:`act.verifier.Verifier.measure_region_change`），
        并且**只有落在墨迹上的那一下才允许收工**：

        ====================================  ==================================================
        ``ink_centroid`` 点后 ``changed``(≥2.0)   成功收工
        ``ink_centroid`` 点后 ``weak``(0.05~2.0)  成功收工，``readback`` 写 ``weak_changed:<mad>``
        ``ink_centroid`` 点后 ``none``(<0.05)     **绝不重点**：``ok=True``，写 ``no_change_on_ink``
        几何兜底点后 ``none``                      换下一个候选点（至多 ``click_replay_max`` 次）
        所有候选都试完且全是 ``none``              失败（``_exhausted`` + 暂停），绝不静默报成功
        ====================================  ==================================================

        **为什么 ``ink_centroid`` 上 ``none`` 也算成功**：这一点落在**这道题自己的
        文字 / 标号**上（不是行尾空白）。「什么都没变」最可能的解释是**它本来就已经
        选中了** —— 续跑、断点续跑、或用户自己点过；那种情况下再点一次像素仍然
        **一点都不会变**（``mad=0.00``），而多选 / 复选上那一下会把刚选上的勾
        **取消**。也就是说「重复点」正是用户报的「胡乱操作」本身，收益为负。
        另一种可能是该站点的选中态本来就是不变样式（纯图片选项），同样不该再点。
        代价是「确实没点中」也会被记成功 —— 这个风险由下游兜：选择是否正确由
        解题组与提交后的结果面板回答（见 MEMORY §1.4），不归执行层判。

        **为什么几何兜底仍然允许换点**：``box_center`` / ``left_half`` /
        ``upper_half`` 是为 2026-09-28「整个框都是空白、4 次全点行尾空白」那次事故
        留的兜底 —— 那一下**根本没落在内容上**，换点才有意义。换点之前先按原样
        重测一次：已经是 ``changed`` / ``weak`` 就说明上一次其实点上了（只是当时
        还在过渡动画里），直接收工，**绝不点第二下**。
        （实践中几何候选去重后通常只剩 ``box_center`` + ``left_half``：
        ``upper_half`` 与几何中心恒重合，见 ``tests/test_region_change.py``
        里那条「纯白框只有 2 个位置不同的候选点」的用例。）
        """
        label = target_label or f"{_describe_box(box, size)}:qtype={qtype.value}"
        started = time.perf_counter()
        attempts = 0
        last_error: str | None = None
        self._scroll_before = await self._snapshot_scroll()
        try:
            try:
                baseline = await shot_viewport(self.page)
            except Exception as exc:
                # 连基准帧都拿不到：这次判不了，按「动作失败」留档（重放没有意义）
                return await self._exhausted(
                    ActionKind.SELECT_OPTION,
                    label,
                    ActLevel.L6_VISION_XY,
                    f"基准截图失败：{short(exc)}",
                    attempts,
                    started,
                )

            candidates = candidate_points(baseline, box, size)
            # 候选点预算是**换点**的上限，不是「把同一个坐标重复点几次」：
            # 候选枚举完就停 —— 重复一个错的坐标再多次也不会变对。
            plan = candidates[: self.cfg.guards.click_replay_max + 1]
            verifier = self._verifier_of()

            for attempt, (point_x, point_y, aim) in enumerate(plan):
                attempts = attempt + 1
                if attempt:
                    # 换点之前先比一次像素：多选重复点击会把勾**取消**，所以
                    # 「上一次其实点上了、只是当时还在过渡动画里」必须认出来并收工 ——
                    # 这一条就是旧实现「已选中就不动手」在像素时代的形态。
                    pre = await verifier.measure_region_change(box, size, baseline)
                    if pre.state != "none":
                        return self._done(
                            ActionKind.SELECT_OPTION,
                            label,
                            ActLevel.L6_VISION_XY,
                            f"already_changed:region_mad={pre.mad:.2f} state={pre.state}",
                            started,
                        )
                point = (point_x, point_y)
                try:
                    await self._click_point(point)
                except Exception as exc:
                    # 坐标点击抛异常 = 输入事件根本没发出去，换点也没有意义
                    last_error = f"{type(exc).__name__}: {short(exc)}"
                    break

                change = await verifier.measure_region_change(box, size, baseline)
                if change.state != "none":
                    return self._done(
                        ActionKind.SELECT_OPTION,
                        label,
                        ActLevel.L6_VISION_XY,
                        _select_readback(aim, change),
                        started,
                    )
                if aim == "ink_centroid" and change.samples:
                    # 点在这道题自己的内容上、画面一个像素都没动 → 最可能是**本来就已选中**。
                    # 再点一下（或换框里的别的点）在多选 / 复选上只会把勾取消，就此收工。
                    logger.info(
                        "select_option：墨迹上点击后区域无变化（mad=%.2f），"
                        "按「已选中 / 选中态不变样式」收工",
                        change.mad,
                    )
                    return self._done(
                        ActionKind.SELECT_OPTION,
                        label,
                        ActLevel.L6_VISION_XY,
                        f"no_change_on_ink:region_mad={change.mad:.2f} aim={aim}",
                        started,
                    )

                last_error = (
                    f"{ErrorCode.READBACK_MISMATCH.value}: "
                    f"region_mad={change.mad:.2f} state={change.state} "
                    f"samples={change.samples} aim={aim}"
                )
                self._emit(
                    Event.ACT_READBACK_MISMATCH,
                    {
                        "item_id": self.item_id,
                        "kind": ActionKind.SELECT_OPTION.value,
                        "level": ActLevel.L6_VISION_XY.value,
                        "expected": REGION_CHANGE_EXPECTED,
                        "actual": (
                            f"region_mad={change.mad:.2f} state={change.state} "
                            f"samples={change.samples}"
                        ),
                        # 落点与它的来由都进事件：下次再出这种事，不用靠猜是哪儿的锅。
                        "point": [round(point[0]), round(point[1])],
                        "aim": aim,
                    },
                )
                if aim == "ink_centroid":
                    # 墨迹上判不了（``samples == 0``：取帧失败 / 尺寸对不上）。
                    # 这种情况**不许**当成「确实没变」去报成功，也**不该**再往框里
                    # 别的点乱试（那正是要修的行为）→ 如实失败，交给调用方暂停。
                    break
                if attempt < len(plan) - 1:
                    await sleep_gap(self.cfg.guards.click_replay_gap_ms)

            return await self._exhausted(
                ActionKind.SELECT_OPTION,
                label,
                ActLevel.L6_VISION_XY,
                last_error,
                attempts,
                started,
            )
        finally:
            await self._restore_scroll()

    async def click(
        self,
        box: NormBox,
        size: ImageSize,
        *,
        kind: ActionKind = ActionKind.CLICK,
        target_label: str | None = None,
    ) -> ActionResult:
        """通用坐标点击：下一题 / 关弹窗 / 页面上任何没有「目标状态」的东西。

        刻意**不做校验**：点完页面该变成什么样是编排层的事，硬编一个期望值
        只会制造假失败。于是它也只点一次 —— 点不动就截图 + 发 ``pause`` 信号，
        **不静默跳过**（旧实现里它走六级阶梯，现在只剩坐标这一级）。
        """
        label = target_label or _describe_box(box, size)
        started = time.perf_counter()
        self._scroll_before = await self._snapshot_scroll()
        aim = "box_center"
        try:
            try:
                point_x, point_y, aim = await self._aim(box, size)
                await self._click_point((point_x, point_y))
            except Exception as exc:
                return await self._exhausted(
                    kind,
                    label,
                    ActLevel.L6_VISION_XY,
                    f"{type(exc).__name__}: {short(exc)}",
                    1,
                    started,
                )
        finally:
            await self._restore_scroll()
        return self._done(kind, label, ActLevel.L6_VISION_XY, f"aim={aim}", started)

    async def submit(
        self,
        box: NormBox,
        size: ImageSize,
        *,
        target_label: str | None = None,
    ) -> ActionResult:
        """提交。**不重试、不重放**，点不动就暂停等人（M3-6）。

        只发一次坐标点击：不走阶梯，也不走 T0-5 的重放。提交是本项目唯一的
        不可逆动作 —— 失败可能是真失败，也可能是慢，两种都得让人来看；
        自动重放会把「其实已经提交成功」的那一题再点一遍。
        """
        label = target_label or _describe_box(box, size)
        started = time.perf_counter()
        self._scroll_before = await self._snapshot_scroll()
        try:
            # ``wait_for`` 是**限时退出**用的：坐标点击没有「等元素可点」那一步，
            # 页面卡死时若不兜住，整条运行会挂在这里而不是走暂停等人那条路。
            point_x, point_y, _aim = await self._aim(box, size)
            await asyncio.wait_for(
                self._click_point((point_x, point_y)), timeout=SUBMIT_TIMEOUT_MS / 1000.0
            )
        except Exception as exc:
            ref = await self._capture("submit_failed")
            message = f"{ErrorCode.SUBMIT_TIMEOUT.value}: {exc}"
            self._emit(
                Event.ACT_SUBMIT_TIMEOUT,
                {
                    "item_id": self.item_id,
                    "error_code": ErrorCode.SUBMIT_TIMEOUT.value,
                    "error": short(exc),
                    "screenshot_ref": ref,
                    # 由编排层执行「暂停等人」
                    "pause": True,
                },
            )
            return ActionResult(
                kind=ActionKind.SUBMIT,
                target=label,
                level_used=ActLevel.L6_VISION_XY,
                ok=False,
                # 提交超时**必须复核**：点没点进去、提交成没成功都还不知道。
                readback_ok=False,
                error=message,
                elapsed_ms=_ms(started),
                screenshot_ref=ref,
            )
        finally:
            await self._restore_scroll()

        return ActionResult(
            kind=ActionKind.SUBMIT,
            target=label,
            level_used=ActLevel.L6_VISION_XY,
            ok=True,
            # 提交这条路径刻意不做回读（见 :meth:`submit` 的 docstring）——
            # 「没有可校验的回读」按语义就是 ``True``，而不是「验成了」。
            readback_ok=True,
            elapsed_ms=_ms(started),
        )

    async def _click_point(self, point: tuple[float, float]) -> None:
        """在视口 CSS 像素坐标上点一下。

        **不做任何钳制**：坐标怎么来就怎么点 —— 越界坐标会被浏览器丢掉，
        而那正是「点了没反应」的一种真实成因，钳回视口内只会把它掩盖掉。
        """
        await self.page.mouse.click(point[0], point[1])

    async def _aim(self, box: NormBox, size: ImageSize) -> CandidatePoint:
        """决定点哪儿：框内**内容质心**优先，取不到就回退几何中心。

        ``click`` / ``submit`` 手上没有现成的基准帧，这里**现截一张** ——
        多花一张截图换「点得中」，对提交这种不可逆动作尤其划算：
        提交失败只能停下等人，代价远高于一张图。

        截图失败不算致命：回退几何中心，行为与改动前一致。
        """
        try:
            before = await shot_viewport(self.page)
        except Exception as exc:
            logger.debug("定位失败（取不到基准帧），回退几何中心：%s", exc)
            center = norm_box_center(box, size)
            return (center[0], center[1], "box_center")
        return candidate_points(before, box, size)[0]

    async def _click_box(self, box: NormBox, size: ImageSize) -> tuple[float, float]:
        """点归一化框的**几何中心**，返回实际点到的那一点（CSS 像素，仅用于留痕）。

        保留它是给「就是要中点」的调用方与既有测试用的；
        要「点中内容」请走 :meth:`_aim`。
        """
        point = norm_box_center(box, size)
        await self._click_point(point)
        return point

    # ------------------------------------------------------------------ 媒体

    async def play_media(self, adapter: BaseAdapter) -> ActionResult:
        """开始播放。走 :data:`MEDIA_LEVELS`（可信手势优先，``evaluate`` 垫底）。"""
        return await self._drive_media(
            ActionKind.PLAY_MEDIA,
            adapter,
            levels=MEDIA_LEVELS,
            confirm=lambda: self._verifier_of().verify_media_flag(adapter, paused=False),
        )

    async def pause_media(self, adapter: BaseAdapter) -> ActionResult:
        """暂停播放。走 :data:`PAUSE_LEVELS`（键盘/脚本优先）—— 见该常量的理由：
        弹题遮罩会挡住播放按钮，纯点击路径在挂起场景下不可用。"""
        return await self._drive_media(
            ActionKind.PAUSE_MEDIA,
            adapter,
            levels=PAUSE_LEVELS,
            confirm=lambda: self._verifier_of().verify_media_flag(adapter, paused=True),
        )

    async def seek_media(self, adapter: BaseAdapter, seconds: float) -> ActionResult:
        """跳到指定秒数。走 :data:`SEEK_LEVELS`（脚本路径第一，见模块 docstring）。"""
        return await self._drive_media(
            ActionKind.SEEK_MEDIA,
            adapter,
            levels=SEEK_LEVELS,
            confirm=lambda: self._verifier_of().verify_media_flag(
                adapter, current_time=seconds, tolerance=SEEK_TOLERANCE_S
            ),
            seconds=seconds,
            target_label=f"media:seek={seconds:g}s",
        )

    async def next_episode(self, adapter: BaseAdapter) -> ActionResult:
        """切到下一集，并回读分集索引**严格 +1**（M5-3）。"""
        state = await self._verifier_of().read_media(adapter)
        if state is None:
            ref = await self._capture("media_missing")
            return ActionResult(
                kind=ActionKind.NEXT_EPISODE,
                target="media:next",
                level_used=ActLevel.L1_LOCATOR,
                ok=False,
                # 连 ``<video>`` 都读不到 → 分集有没有推进无从得知，必须复核。
                readback_ok=False,
                error=f"{ErrorCode.MEDIA_ACTION_FAILED.value}: media:not_available",
                screenshot_ref=ref,
            )
        return await self._drive_media(
            ActionKind.NEXT_EPISODE,
            adapter,
            levels=LEVELS,
            confirm=lambda: self._verifier_of().verify_episode_advance(
                self.page, adapter, state.episode_index
            ),
            target_label="media:next",
        )

    # ------------------------------------------------------------------ 手势

    async def swipe(
        self,
        direction: str = "left",
        *,
        mode: str | None = None,
        distance_ratio: float | None = None,
        duration_ms: int | None = None,
    ) -> ActionResult:
        """滑动手势（P12）：**没有按钮可点时的「下一题」**。

        ``direction`` 是**手指的移动方向**（``left`` = 从右往左划）。
        落在视口中线附近、留出 ``distance_ratio`` 个视口宽/高的行程，
        两端都留在视口内 —— 手势库普遍按「起点在元素上」做命中判定，
        从屏幕边缘起手有相当比例会被当成系统手势而丢弃。

        返回的 ``ok`` **只表示输入事件发出去了**，不表示页面真的翻页了。
        「翻没翻」由编排层比对页面指纹（:meth:`Orchestrator._advance_by_swipe`）——
        执行层在这里判不了，也不该假装判得了。
        """
        cfg_guards = self.cfg.guards
        configured = getattr(cfg_guards, "advance_swipe_mode", "mouse")
        chosen_mode = str(mode if mode is not None else configured).lower()
        ratio = float(
            distance_ratio
            if distance_ratio is not None
            else getattr(cfg_guards, "advance_swipe_distance", 0.6)
        )
        span_ms = int(
            duration_ms
            if duration_ms is not None
            else getattr(cfg_guards, "advance_swipe_duration_ms", 240)
        )
        label = f"gesture:swipe={direction}"
        started = time.perf_counter()
        if direction not in SWIPE_DIRECTIONS:
            return self._gesture_failed(label, f"unknown_direction:{direction}", started)

        try:
            width, height = await self._viewport_size()
            start, end = _swipe_path(direction, width, height, ratio)
            if chosen_mode == "touch":
                await self._touch_swipe(start, end, span_ms)
            else:
                await self._mouse_swipe(start, end, span_ms)
        except Exception as exc:
            logger.debug("滑动手势失败（%s）：%s", direction, exc)
            return self._gesture_failed(label, f"{type(exc).__name__}: {short(exc)}", started)

        self._emit(
            Event.ACT_LEVEL_USED,
            {
                "item_id": self.item_id,
                "kind": ActionKind.SWIPE.value,
                "level": ActLevel.L6_VISION_XY.value,
                # 手势元数据进留痕：出问题时第一个要看的就是「往哪个方向划了多远」
                "direction": direction,
                "mode": chosen_mode,
                "distance_ratio": round(ratio, 3),
                "ok": True,
            },
        )
        return ActionResult(
            kind=ActionKind.SWIPE,
            target=label,
            level_used=ActLevel.L6_VISION_XY,
            ok=True,
            readback=f"gesture:{chosen_mode}:{direction}",
            # 手势的 ``readback`` 只是「往哪划了多远」的元数据，**不是校验结论**：
            # 翻没翻页由编排层比对页面指纹判（见 :meth:`swipe`）。没有可校验的回读
            # → ``True``（详情页不该为一次正常滑动报「回读不一致」）。
            readback_ok=True,
            elapsed_ms=_ms(started),
        )

    async def _mouse_swipe(
        self, start: tuple[float, float], end: tuple[float, float], span_ms: int
    ) -> None:
        """鼠标拖拽手势（可信输入事件，Chromium 会合成 pointer 事件）。"""
        await self.page.mouse.move(start[0], start[1])
        await self.page.mouse.down()
        try:
            for point in _interpolate(start, end, SWIPE_STEPS):
                await self.page.mouse.move(point[0], point[1])
                await asyncio.sleep(max(0.0, span_ms / 1000.0) / SWIPE_STEPS)
        finally:
            # 抬起必须发生 —— 卡在按下状态的鼠标会让后续每一次点击都变成拖拽
            await self.page.mouse.up()

    async def _touch_swipe(
        self, start: tuple[float, float], end: tuple[float, float], span_ms: int
    ) -> None:
        """CDP 触摸手势。**先开触摸模拟**，否则页面在特性检测阶段就把触摸分支关掉了。"""
        context = getattr(self.page, "context", None)
        if context is None:
            raise RuntimeError("touch 手势需要 page.context 才能建 CDP 会话")
        session = await context.new_cdp_session(self.page)
        try:
            await session.send(
                "Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1}
            )
            await session.send(
                "Input.dispatchTouchEvent",
                {"type": "touchStart", "touchPoints": [_touch_point(start)]},
            )
            for point in _interpolate(start, end, SWIPE_STEPS):
                await session.send(
                    "Input.dispatchTouchEvent",
                    {"type": "touchMove", "touchPoints": [_touch_point(point)]},
                )
                await asyncio.sleep(max(0.0, span_ms / 1000.0) / SWIPE_STEPS)
            await session.send(
                "Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []}
            )
        finally:
            with suppress(Exception):
                await session.detach()

    def _gesture_failed(self, label: str, error: str, started: float) -> ActionResult:
        """手势本身没发出去（视口读不到 / CDP 会话建不起来）。**不静默吞掉。**"""
        self._emit(
            Event.ACT_LEVEL_USED,
            {
                "item_id": self.item_id,
                "kind": ActionKind.SWIPE.value,
                "level": ActLevel.L6_VISION_XY.value,
                "ok": False,
                "readback_ok": False,
                "error": error,
            },
        )
        return ActionResult(
            kind=ActionKind.SWIPE,
            target=label,
            level_used=ActLevel.L6_VISION_XY,
            ok=False,
            # 手势根本没发出去 → 页面纹丝没动，必须复核（绝不静默吞掉）。
            readback_ok=False,
            error=f"{ErrorCode.ACTION_LADDER_EXHAUSTED.value}: {error}",
            elapsed_ms=_ms(started),
        )

    # ------------------------------------------------------------------ 阶梯驱动

    async def _drive(
        self,
        kind: ActionKind,
        target: Locator,
        *,
        levels: tuple[ActLevel, ...],
        confirm: Callable[[], Awaitable[Any]] | None,
        adapter: BaseAdapter | None = None,
        seconds: float | None = None,
        target_label: str | None = None,
    ) -> ActionResult:
        """通用阶梯驱动：**逐级尝试 → 逐级回读 → 重放 → 升级 → 到顶留档**。

        v0.2.0 起只剩**媒体控件**走这条（题目侧是裸坐标，没有可定位的目标元素）。
        """
        label = target_label or _describe(target)
        started = time.perf_counter()
        attempts = 0
        last_error: str | None = None
        try:
            self._scroll_before = await self._snapshot_scroll()
            for level in levels:
                for _ in range(self.cfg.guards.click_replay_max + 1):
                    attempts += 1
                    # 纪律一：临点前一刻读。已达成期望状态就不动手（幂等 + TOCTOU）
                    if confirm is not None:
                        pre = await confirm()
                        if pre.ok:
                            return self._done(kind, label, level, f"skipped:{pre.actual}", started)
                    outcome = await self._attempt(
                        level, target, kind=kind, adapter=adapter, seconds=seconds
                    )
                    if not outcome.ok:
                        # 动作根本没发出去：重放同一级没有意义，直接上移一级
                        last_error = outcome.error or ErrorCode.ACTION_LADDER_EXHAUSTED.value
                        break
                    if confirm is None:
                        return self._done(kind, label, level, None, started)
                    verdict = await self._confirm(confirm, kind=kind)
                    if verdict.ok:
                        return self._done(kind, label, level, verdict.actual, started)
                    last_error = f"{ErrorCode.READBACK_MISMATCH.value}: {verdict.actual}"
                    self._emit(
                        Event.ACT_READBACK_MISMATCH,
                        {
                            "item_id": self.item_id,
                            "kind": kind.value,
                            "level": level.value,
                            "expected": verdict.expected,
                            "actual": verdict.actual,
                        },
                    )
                    await sleep_gap(self.cfg.guards.click_replay_gap_ms)
                # 本级重放用尽 → 上移一级
            return await self._exhausted(kind, label, levels[-1], last_error, attempts, started)
        finally:
            await self._restore_scroll()

    async def _drive_media(
        self,
        kind: ActionKind,
        adapter: BaseAdapter,
        *,
        levels: tuple[ActLevel, ...],
        confirm: Callable[[], Awaitable[Any]],
        seconds: float | None = None,
        target_label: str | None = None,
    ) -> ActionResult:
        """媒体动作的阶梯驱动。点击 / 焦点 / 坐标三级都落在同一件控件上：

        - 播放 / 暂停 → 播放按钮（``media_anchors.play_button``）
        - 下一集 → 下一集按钮（``media_anchors.next``）
        - seek → 视频元素本身（靶场没有可点的 scrubber，见模块 docstring）
        """
        if kind is ActionKind.NEXT_EPISODE:
            anchor = "next"
        elif kind is ActionKind.SEEK_MEDIA:
            anchor = "video"
        else:
            anchor = "play_button"
        target = adapter.media_locator(self.page, anchor)
        return await self._drive(
            kind,
            target,
            levels=levels,
            confirm=confirm,
            adapter=adapter,
            seconds=seconds,
            target_label=target_label,
        )

    async def _confirm(self, confirm: Callable[[], Awaitable[Any]], *, kind: ActionKind) -> Any:
        """回读确认。媒体动作给一段轮询预算（"点了"到"真的动了"有几百 ms 空窗）；

        非媒体动作只读一次 —— T0-5 的重放机制本身就是给页面刷新留的时间。
        （v0.2.0 起这条路径只服务媒体控件，非媒体分支留着是为了
        ``_drive`` 仍是一个可独立测试的通用阶梯。）
        """
        verdict = await confirm()
        if verdict.ok or kind not in _MEDIA_KINDS:
            return verdict
        deadline = time.monotonic() + MEDIA_CONFIRM_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(MEDIA_CONFIRM_POLL_S)
            verdict = await confirm()
            if verdict.ok:
                return verdict
        return verdict

    async def _attempt(
        self,
        level: ActLevel,
        target: Locator,
        *,
        kind: ActionKind = ActionKind.CLICK,
        adapter: BaseAdapter | None = None,
        seconds: float | None = None,
    ) -> ActionResult:
        """在**某一级**上执行一次动作。返回 ``ok`` 表示输入事件是否真的发出去了，
        **不代表状态已经变对**（那是回读的事）。"""
        started = time.perf_counter()
        try:
            if kind in _MEDIA_KINDS and level is ActLevel.L2_FORCE:
                await self._script_media(kind, adapter, seconds)
            elif kind in _MEDIA_KINDS and level is ActLevel.L4_FOCUS_KEYS:
                await self._media_focus_keys(target, kind)
            else:
                await self._element_attempt(level, target, timeout_ms=self._action_timeout_ms)
        except Exception as exc:
            logger.debug("阶梯 %s 在 %s 上失败：%s", level.value, kind.value, exc)
            return ActionResult(
                kind=kind,
                target=_describe(target),
                level_used=level,
                ok=False,
                # 输入事件都没发出去 → 这一次没有任何可复核的回读。
                readback_ok=False,
                error=f"{type(exc).__name__}: {short(exc)}",
                elapsed_ms=_ms(started),
            )
        return ActionResult(
            kind=kind,
            target=_describe(target),
            level_used=level,
            ok=True,
            # 注意这里是**中间结果**：``ok`` 只说明输入事件发出去了，回读结论
            # 由调用方 :meth:`_confirm` 判定，最终结论在 :meth:`_done` /
            # :meth:`_exhausted` 里落盘（这一层的结果不会进 ``action.json``）。
            elapsed_ms=_ms(started),
        )

    async def _element_attempt(self, level: ActLevel, target: Locator, *, timeout_ms: int) -> None:
        """元素级四档 + ``bounding_box`` 一档的裸机制。**不做回读、不做重放**（只剩媒体在用）。"""
        if level is ActLevel.L1_LOCATOR:
            await target.click(timeout=timeout_ms)
        elif level is ActLevel.L2_FORCE:
            await target.click(force=True, timeout=timeout_ms)
        elif level is ActLevel.L3_SCROLL:
            await target.scroll_into_view_if_needed(timeout=timeout_ms)
            await target.click(timeout=timeout_ms)
        elif level is ActLevel.L4_FOCUS_KEYS:
            await target.focus(timeout=timeout_ms)
            await self.page.keyboard.press(await self._activation_key(target))
        elif level is ActLevel.L5_BBOX:
            await self._click_bbox(target)
        else:  # pragma: no cover - 枚举穷尽（L6 是题目侧的坐标级，不走元素阶梯）
            raise ValueError(f"未知阶梯：{level}")

    async def _click_bbox(self, target: Locator) -> None:
        """L5：用元素自身的 ``bounding_box`` 坐标点击。

        .. note::
           ``bounding_box()`` 返回的**已经是 CSS 像素**，这里**不要**再除
           ``devicePixelRatio`` —— 再除一次就是「正好偏一半」。
        """
        box = await target.bounding_box(timeout=self._action_timeout_ms)
        if not box:
            raise RuntimeError("bounding_box 为空：元素不可见或已脱离文档")
        await self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    async def _script_media(
        self, kind: ActionKind, adapter: BaseAdapter | None, seconds: float | None
    ) -> None:
        """L2：脚本路径。``play()`` 会抛 ``NotAllowedError``，所以它只能垫底。"""
        if adapter is None:
            raise RuntimeError("媒体脚本路径需要 adapter（视频选择器来自 YAML）")
        selector = adapter.media_anchors.video
        if kind is ActionKind.PLAY_MEDIA:
            # 故意把 play() 的 Promise 交回给 Playwright：被策略拒绝时它会 reject，
            # 于是"播放失败"变成一次可捕获的阶梯失败，而不是一个静默的未处理 Promise。
            await self.page.evaluate(
                """(sel) => {
                    const v = document.querySelector(sel);
                    if (!v) throw new Error('video_not_found');
                    return v.play();
                }""",
                selector,
            )
        elif kind is ActionKind.PAUSE_MEDIA:
            await self.page.evaluate(
                """(sel) => {
                    const v = document.querySelector(sel);
                    if (!v) throw new Error('video_not_found');
                    v.pause();
                    return v.paused;
                }""",
                selector,
            )
        elif kind is ActionKind.SEEK_MEDIA:
            target_time = float(seconds or 0.0)
            await self.page.evaluate(
                """(args) => {
                    const v = document.querySelector(args.sel);
                    if (!v) throw new Error('video_not_found');
                    v.currentTime = args.t;
                    return v.currentTime;
                }""",
                {"sel": selector, "t": target_time},
            )
        else:  # pragma: no cover - 调用点已限定
            raise ValueError(f"{kind} 没有脚本路径")

    async def _media_focus_keys(self, target: Locator, kind: ActionKind) -> None:
        """L4：键盘手势。播放 / 暂停按空格，seek 按方向键。"""
        if kind is ActionKind.SEEK_MEDIA:
            await target.focus(timeout=self._action_timeout_ms)
            await self.page.keyboard.press("ArrowRight")
            return
        await target.focus(timeout=self._action_timeout_ms)
        await self.page.keyboard.press("Space")

    # ------------------------------------------------------------------ 收尾

    def _done(
        self, kind: ActionKind, label: str, level: ActLevel, readback: str | None, started: float
    ) -> ActionResult:
        """成功收尾的**唯一出口**：``ok=True`` 且 ``readback_ok=True``。

        为什么这里恒为 ``True``：能走到 ``_done`` 的每一条路径都已经按
        「这次动作有没有验成」判过了 —— ``changed`` / ``weak_changed:`` /
        ``no_change_on_ink:``（本来就已选中，见
        :meth:`select_option` 的判定表）/ ``already_changed:`` /
        ``skipped:``（主动不动手）/ 压根没有回读可校验（``aim=…``、手势元数据）。
        界面的「回读不一致」不该在这里报。
        """
        self._emit(
            Event.ACT_LEVEL_USED,
            {
                "item_id": self.item_id,
                "kind": kind.value,
                "level": level.value,
                "readback": readback,
                "ok": True,
                # 结构化结论也进事件流：排障时不必去解析 readback 文案。
                "readback_ok": True,
                "skipped": bool(readback and readback.startswith("skipped:")),
            },
        )
        return ActionResult(
            kind=kind,
            target=label,
            level_used=level,
            ok=True,
            readback=readback,
            # **显式**填：这是 act 层对「无需复核」的承诺，不能靠默认值。
            readback_ok=True,
            elapsed_ms=_ms(started),
        )

    async def _exhausted(
        self,
        kind: ActionKind,
        label: str,
        level: ActLevel,
        last_error: str | None,
        attempts: int,
        started: float,
    ) -> ActionResult:
        """动作做到顶仍没验成：**截图 + 记 failed + 发 pause 信号**，绝不静默跳过。

        ``readback_ok`` 在这里**必须显式写 False**：字段默认是 ``True``，
        漏填就会让「重放耗尽」这条真失败在详情页显示成「一致」——
        比原来的误报更坏（误报只是吵，漏报是把失败说成成功）。
        """
        ref = await self._capture("error")
        message = last_error or ErrorCode.ACTION_LADDER_EXHAUSTED.value
        error = f"{ErrorCode.ACTION_LADDER_EXHAUSTED.value}: {message}"
        self._emit(
            Event.ACT_LEVEL_USED,
            {
                "item_id": self.item_id,
                "kind": kind.value,
                "level": level.value,
                "ok": False,
                "readback_ok": False,
                "attempts": attempts,
                "error": message,
                "screenshot_ref": ref,
                # 编排层据此执行「暂停 + 记 failed」（必停不自己 await）
                "pause": True,
            },
        )
        return ActionResult(
            kind=kind,
            target=label,
            level_used=level,
            ok=False,
            readback_ok=False,
            error=error,
            elapsed_ms=_ms(started),
            screenshot_ref=ref,
        )

    async def _restore_scroll(self) -> None:
        """降级过程中若滚动过页面，动作结束后复位。"""
        snapshot = self._scroll_before
        self._scroll_before = None
        if snapshot is None:
            return
        try:
            await self.page.evaluate(
                "(pos) => window.scrollTo(pos[0], pos[1])", [snapshot[0], snapshot[1]]
            )
        except Exception as exc:  # pragma: no cover - 页面已导航 / 关闭
            logger.debug("滚动复位失败：%s", exc)

    # ------------------------------------------------------------------ 工具

    def _verifier_of(self) -> Verifier:
        if self._verifier is None:
            self._verifier = Verifier(
                self.page,
                self.cfg,
                run_logger=self.run_logger,
                item_id=self.item_id,
                bus=self.bus,
            )
        return self._verifier

    async def _snapshot_scroll(self) -> tuple[float, float] | None:
        try:
            value: Any = await self.page.evaluate(
                "() => [window.scrollX || 0, window.scrollY || 0]"
            )
            items = list(value)
        except Exception as exc:  # pragma: no cover - 页面尚未就绪
            logger.debug("滚动位置读取失败：%s", exc)
            return None
        if len(items) < 2:
            return None
        return (float(items[0]), float(items[1]))

    async def _viewport_size(self) -> tuple[float, float]:
        """视口尺寸（CSS 像素）。

        **不能只看 ``page.viewport_size``**：通过 CDP 附加来的页面这个属性常常是
        ``None``（P11 实测），而手势没有视口就没法算行程。所以回退到页内读。
        """
        size = getattr(self.page, "viewport_size", None)
        if isinstance(size, dict) and size.get("width") and size.get("height"):
            return float(size["width"]), float(size["height"])
        try:
            value = await self.page.evaluate(
                "() => [window.innerWidth || 0, window.innerHeight || 0]"
            )
            width, height = float(value[0]), float(value[1])
        except Exception as exc:
            raise RuntimeError(f"读不到视口尺寸，无法做滑动手势: {short(exc)}") from exc
        if width <= 0 or height <= 0:
            raise RuntimeError("读到的视口尺寸为 0，无法做滑动手势")
        return width, height

    async def _activation_key(self, target: Locator) -> str:
        """L4 用哪个键激活：原生 ``<input>`` 认空格，按钮 / 链接认回车。"""
        try:
            tag = await target.first.evaluate(
                "(el) => (el.matches('input') ? 'input' : (el.tagName || '').toLowerCase())"
            )
        except Exception:  # pragma: no cover - 元素已消失
            return "Enter"
        return "Space" if str(tag) == "input" else "Enter"

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

    def _media_unavailable(self, name: str, reason: str, started: float) -> ActionResult:
        error = f"{ErrorCode.MEDIA_ACTION_FAILED.value}: {name}:{reason}"
        self._emit(
            Event.ACT_LEVEL_USED,
            {
                "item_id": self.item_id,
                "kind": ActionKind.PLAY_MEDIA.value,
                "ok": False,
                "readback_ok": False,
                "error": error,
                "pause": True,
            },
        )
        return ActionResult(
            kind=ActionKind.PLAY_MEDIA,
            target=f"media:{name}",
            level_used=ActLevel.L1_LOCATOR,
            ok=False,
            # 媒体状态读不到 → 在不在播无从确认，必须复核。
            readback_ok=False,
            error=error,
            elapsed_ms=_ms(started),
        )

    def _emit(self, event: str, payload: dict[str, object]) -> None:
        if self.bus is not None:
            self.bus.emit(event, payload)


def _select_readback(aim: str, change: RegionChange) -> str:
    """选项选中成功的 ``readback`` 文案（**人类可读 + 带 mad 与状态**）。

    ``weak``（0.05~2.0：1px 描边、小圆点这类轻变化）单独用一个前缀，
    因为「画面只是轻轻动了一下」这件事在留痕里必须一眼可辨 ——
    它是 2026-09-29「点对了却被判成没点中」那次修复的核心一档。
    """
    if change.state == "weak":
        return f"weak_changed:{change.mad:.2f} state=weak aim={aim}"
    return f"region_mad={change.mad:.2f} state={change.state} aim={aim}"


def _describe_box(box: NormBox, size: ImageSize) -> str:
    """坐标动作的可读目标标识（**仅用于留痕与事件展示**）。"""
    try:
        center = norm_box_center(box, size)
    except ValueError:
        return f"vision:box={box}"
    return f"vision:xy=({center[0]:.0f},{center[1]:.0f})"


def _describe(target: object) -> str:
    """给媒体动作结果一个可读的目标标识（**仅用于留痕与事件展示**）。"""
    selector = getattr(target, "_selector", None)
    if isinstance(selector, str) and selector:
        return selector
    return short(target)


def _swipe_path(
    direction: str,
    width: float,
    height: float,
    distance_ratio: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """按方向算出 ``(起点, 终点)``。

    从视口中线起手、两端各留一半行程 —— 这样起点与终点都在视口内，
    不会被当成「从屏幕边缘起手」的系统手势丢掉。
    """
    cx, cy = width / 2.0, height / 2.0
    if direction in {"left", "right"}:
        dx = max(1.0, width * distance_ratio / 2.0)
        if direction == "left":
            return (cx + dx, cy), (cx - dx, cy)
        return (cx - dx, cy), (cx + dx, cy)
    dy = max(1.0, height * distance_ratio / 2.0)
    if direction == "up":
        return (cx, cy + dy), (cx, cy - dy)
    return (cx, cy - dy), (cx, cy + dy)


def _interpolate(
    start: tuple[float, float], end: tuple[float, float], steps: int
) -> list[tuple[float, float]]:
    """起点到终点的中间点（**不含起点**，含终点）。"""
    count = max(1, steps)
    return [
        (
            start[0] + (end[0] - start[0]) * (index / count),
            start[1] + (end[1] - start[1]) * (index / count),
        )
        for index in range(1, count + 1)
    ]


def _touch_point(point: tuple[float, float]) -> dict[str, float]:
    """CDP 触摸点的载荷（``radiusX``/``radiusY`` 是必填字段）。"""
    return {
        "x": float(point[0]),
        "y": float(point[1]),
        "radiusX": _TOUCH_RADIUS,
        "radiusY": _TOUCH_RADIUS,
        "force": 1.0,
    }


def _ms(started: float) -> int:
    """从 ``time.perf_counter()`` 起点算出的毫秒耗时（写进 ``ActionResult.elapsed_ms``）。"""
    return int((time.perf_counter() - started) * 1000)
