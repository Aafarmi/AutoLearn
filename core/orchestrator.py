"""编排循环（M4-1 / M4-2 / M4-3 / M5-1 ~ M5-5）。

异步队列 + SQLite 断点续跑，**严格按 T0-2 状态机**，不得在编排层绕过
:func:`core.states.require_transition`。

两个场景，一套循环
------------------
``cfg.task_sequence`` 决定跑哪个场景（见 :func:`core.tasks.build_task_sequence`）：

- 不含 ``video`` → **刷题场景**（``_drain_quiz``）：题目驱动，读到哪题认领哪题；
- 含 ``video`` → **网课场景**（``_drain_video``）：分集驱动，按目录逐集播完，
  播放中被弹题打断时**压栈挂起 → 处理弹题 → 弹栈恢复**（M5-2 的嵌套中断）。
  题目在网课场景里不作为独立队列出现，只以「弹题」形式嵌套进来。

续跑红线
--------
进入 ``submitted`` 的任务只做**结果回读**，绝不重新点击、绝不重新提交。

「题目驱动」而不是「列表驱动」
------------------------------
靶场（以及真实站点）在开跑前**不告诉你一共有多少题**，``qid`` 也只有读过页面
才知道。所以主循环是「读当前这道题 → 按 ``qid`` 认领 / 新建条目 → 处理 →
点下一题」，而不是「先建 50 条再逐条跑」。续跑因此天然成立：

- 页面从第 1 题开始，循环照样从头读；
- 读到的 ``qid`` 若在库里已是终态，就**只前进、不重做**。

必停的落地
----------
执行层（P6）不自己 ``await`` 人工确认，只发 ``pause: true`` 与 ``ok=False``；
真正的挂起在这里：``_pause_for()`` 清掉运行闸，``resume()`` 放行。
**编排层是唯一会让一次运行停下来的地方。**

状态只进不退
------------
题目九态里 ``pending`` 没有任何入边（T0-2：状态一经离开便不可回退重做）。
所以续跑时向前推进走 :meth:`Orchestrator._ensure_state`（只在合法时才迁移、
已经在更靠后的状态就不回头），只有「已选好 → 已提交 → 已校验」这段危险路径
才用严格的 :meth:`Orchestrator._transition`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from core import db
from core.advance_library import strategy_for
from core.config import RunConfig
from core.enums import (
    ActionKind,
    AdvanceMethod,
    AdvanceSkill,
    ErrorCode,
    MediaState,
    ProbeName,
    QuestionState,
    TaskType,
)
from core.events import Event
from core.models import (
    Answer,
    CompletionCheck,
    PageCard,
    PageSwipe,
    PageView,
    PerceptionResult,
    Question,
    ReadBatch,
    ReadResult,
    RunPlan,
    SubmitScope,
    TaskItem,
)
from core.ratelimit import sleep_click_gap, sleep_submit_gap
from core.run_plan import all_done, derive_plan, plan_summary
from core.states import can_transition, is_terminal, require_transition, resume_entry
from core.tasks import SuspendFrame, TaskStack, build_task_sequence
from core.trace import run_dir
from solve.reader import (
    gate_read_result,
    read_questions,
    stem_fingerprint,
    to_question,
)

__all__ = ["Orchestrator", "RunContext", "RunDeps", "default_page_factory"]

logger = logging.getLogger(__name__)


def _vision_error_hint(error: str | None) -> str:
    """将读题错误码映射成具体、可执行的用户提示。"""
    if error == "rate_limited":
        return "视觉模型触发服务商限流；已自动短暂重试，请检查服务商额度或稍后重试"
    if error == "read_parse_failed":
        return "模型有回复但结构无法解析；请查看 vision_read_raw.txt 或检查题目输出格式"
    if error == "provider_unavailable":
        return "没有可用的视觉模型 provider；请检查模型配置和密钥"
    if error in {"vision_unsupported", "vision_not_supported"}:
        return "当前模型不支持图片输入；请改用支持视觉的模型"
    if error == "crop_failed":
        return "页面截图失败；请确认页面已加载后重试"
    if error == "no_frame":
        return "未获取到页面截图；请确认目标页面有效后重试"
    return "视觉读题失败；请查看 vision_read 事件中的错误码及原始响应"


#: 等人决策时的轮询间隔（秒）。
#:
#: 刻意**不订阅内存事件**：人工决策是通过 HTTP 写进 SQLite 的，
#: 轮询数据库对「界面在另一个进程」同样成立，订阅内存事件只在同进程管用。
_DECISION_POLL_S = 0.2

#: 判定「选项已经点过」的动作类型。
#:
#: 只认 ``select_option``：v0.2.0 里选选项**只有**「按模型给的坐标点」这一条路，
#: 产出的 kind 就是它。「下一题」的 ``click`` 不算 —— 它由
#: :meth:`Orchestrator._record_level_stat` 单独统计，不写 ``action.json``。
_APPLY_KINDS = frozenset({ActionKind.SELECT_OPTION.value})

#: 观看一集的超时余量（秒）。真实时长未知时用它兜底。
_MEDIA_WATCH_MARGIN_S = 60.0

#: 媒体位置的有效精度：小于它的存档位置视为「这一集还没真正开播」。
_POSITION_EPS_S = 0.5

#: 页面指纹可判定的最低正文长度（P12）。
#: 短于此值说明这一页没有可读文本（题干画在 canvas 上就是这种），
#: 那时刻的指纹都长一样 —— 拿它判「翻没翻页」必然误判，所以判为**不可判定**。
_MIN_SIGNATURE_CHARS = 8

#: 模型指出的「下一题号格」与「当前题号格」允许相差的**网格下标**上限。
#:
#: 一格 = 一个题号，所以下标差 > 1 就意味着**一次跨过至少一道题**。
#: 用下标差（而不是几何距离）是因为答题卡按行换行：行末那一步几何上横跨整个卡片宽度，
#: 却仍然是「下一格」。超出即**不采信**，退回开局算好的有界落点 ——
#: 跳题在本项目是静默事故，宁可走旧路。
_CARD_ADJACENT_MAX_INDEX_DELTA = 1


#: 「整屏」归一化框。提交结果的截图差分取它：提交后面板 / 翻页可能出现在
#: **任何位置**，只盯按钮那一小块会漏判（那一侧的表现是「明明交上去了却判超时」）。
#: 代价是它对动画敏感 —— 所以差分只用来证明「画面变了」，不作为「选对了」的证据。
_FULL_VIEWPORT = (0.0, 0.0, 1.0, 1.0)


def _page_brief(batch: ReadBatch | None) -> dict[str, Any] | None:
    """把 ``page`` 观测压成一小段可读留痕（进 ``events.jsonl`` 的 ``vision_read`` 日志行）。

    为什么留痕要留它：推进方式与提交范围都是**照它裁决**的。事后想回答
    「它当时凭什么点答题卡 / 凭什么认定这是整卷」，只有这一行能给出答案；
    光有 ``advance.calibrated`` 那个结论看不出观测到底长什么样。
    """
    page = batch.page if batch is not None else None
    if page is None:
        return None
    return {
        "progress": page.progress,
        "total": page.total,
        "current": page.current,
        "next_control": (page.next_control.label if page.next_control else None),
        "card": (
            f"{page.card.cols}x{page.card.rows}" if page.card is not None else None
        ),
        "submit_scope": page.submit.scope.value if page.submit and page.submit.scope else None,
        "completed": page.completed.value,
        "scrolling": page.scrolling,
    }


def _first_int(text: str | None) -> int | None:
    """从 ``"3."`` / ``"第 3 题"`` 这类题号里抠出**第一个**整数；抠不出返回 ``None``。

    只服务于「留痕告警」（滚动跨了几道题），**不参与任何判定** —— 所以宁可返回
    ``None`` 也不猜：题号形态千奇百怪（``"3."`` / ``"第 3 题"`` / ``"（3）"``），
    猜错只会让人去核对一串并不存在的跳跃。
    """
    if not text:
        return None
    digits = ""
    for char in text:
        if char.isdigit():
            digits += char
        elif digits:
            break
    return int(digits) if digits else None


def _norm_box_ok(box: object) -> bool:
    """归一化框是否在合法范围内（越界 / 倒挂 / 零面积一律判非法，**不修**）。

    与 ``core.run_plan._box_ok`` 同口径，但这里判的是**模型刚给的**框：
    一个越界的框递到执行层就是一次真实的误点，所以宁可退回开局几何。
    """
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    try:
        values = [float(value) for value in box]
    except (TypeError, ValueError):
        return False
    if any(value < 0.0 or value > 1.0 for value in values):
        return False
    return values[2] > 0.0 and values[3] > 0.0


def _box_overlap_ratio(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> float:
    """两个框的交集面积 ÷ 较小那个的面积（``0..1``）。

    用途只有一个：认出「模型把当前格填成了下一格」。画面上看不见下一格时，
    模型有相当比例会退回填当前格 —— 照着点等于原地不动，随后被判成
    ``wrong_question``。重叠比例是最直接、最不依赖语义的判据。
    """
    ax, ay, aw, ah = (float(value) for value in first)
    bx, by, bw, bh = (float(value) for value in second)
    overlap_x = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_y = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    smaller = min(aw * ah, bw * bh)
    if smaller <= 0.0:
        return 0.0
    return (overlap_x * overlap_y) / smaller


def _nearest_cell_index(
    card: PageCard, box: tuple[float, float, float, float]
) -> int | None:
    """``box`` 的中心落在答题卡网格的**第几格**（0 基、行优先）。算不出返回 ``None``。"""
    cols, rows = int(card.cols or 0), int(card.rows or 0)
    if cols <= 0 or rows <= 0 or not _norm_box_ok(card.box):
        return None
    step_x, step_y = card.box[2] / cols, card.box[3] / rows
    if step_x <= 0.0 or step_y <= 0.0:
        return None
    center_x = box[0] + box[2] / 2.0
    center_y = box[1] + box[3] / 2.0
    best: tuple[float, int] | None = None
    for index in range(cols * rows):
        row, col = divmod(index, cols)
        cell_x = card.box[0] + (col + 0.5) * step_x
        cell_y = card.box[1] + (row + 0.5) * step_y
        distance = (cell_x - center_x) ** 2 + (cell_y - center_y) ** 2
        if best is None or distance < best[0]:
            best = (distance, index)
    return best[1] if best is not None else None


def _card_index_gap(
    card: PageCard,
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> int | None:
    """``first`` 与 ``second`` 在网格上**相差几格**（下标差的绝对值）。算不出返回 ``None``。

    为什么用**下标差**而不是几何距离：答题卡是**按行换行**的（第 5 格后面是第 6 格，
    落在下一行第一列）。用欧氏距离量的话，行末那一步会横跨整个卡片宽度
    （5 列时相差 4 格宽），而真正的「下一格」反而被判成「离得太远」；
    同时斜对角（距离 √2）又会被误当成相邻。下标差没有这两个问题。

    为什么要有这道护栏：答题卡里**一格 = 一个题号**。模型指出的「下一格」若与
    「当前格」差着好几格，点下去就是**一次跨过好几道题** —— 而跳题在本项目是
    静默事故（2026-09-28 真机滚动模式下题号从 3 直接跳到 16）。

    刻意**不用**题号相减：题号可能不连续（题库编号常见 1,2,3,5…），
    而网格下标是几何量，与编号规则无关。
    """
    first_index = _nearest_cell_index(card, first)
    second_index = _nearest_cell_index(card, second)
    if first_index is None or second_index is None:
        return None
    return abs(first_index - second_index)


def _clamp_swipe_ratio(value: float) -> float:
    """把视觉组给的滑动幅度夹进执行层能安全消化的区间。

    上限 0.9 是刻意的：``Actuator.swipe`` 从视口中线起手，幅度 1.0 会让起点 / 终点
    落在视口边缘上，而**从屏幕边缘起手的手势有相当比例会被系统丢弃**。
    下限 0.05 保证手势真的产生位移（0 幅度等于原地不动，却会被记成「滑过了」）。

    ``nan`` / ``inf`` 一律走安全默认值（**不是**夹成 0.9）：模型吐出 ``Infinity`` 时
    按「它给了个很大的数」处理，等于用**最大**幅度去滑 —— 那正是最容易滑过头的值。
    """
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return 0.6
    if not math.isfinite(ratio):
        return 0.6
    return max(0.05, min(0.9, ratio))


class RunContext(BaseModel):
    """一次运行的上下文。落进 ``run`` 表。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    cfg: RunConfig
    started_at: datetime
    warnings: list[str] = Field(default_factory=list)


@dataclass
class RunDeps:
    """编排层的装配包。**全部可选**，缺什么就退化成「不做那件事」。

    这样单测可以只注入假 ``actuator_factory`` / ``verifier_factory`` / ``conn``，
    完全不碰浏览器；生产装配在 ``ui/routes/run.py`` 里一次性补齐。

    用 dataclass 而不是 pydantic：这里装的是 Playwright 页面、SQLite 连接、
    事件总线这类**活对象**，过一遍校验层只会带来复制与类型转换的意外。
    """

    #: 已就绪的页面（测试注入用）；为空则用默认工厂开一个
    page: Any | None = None
    adapter: Any | None = None
    pipeline: Any | None = None
    solver: Any | None = None
    cache: Any | None = None
    run_logger: Any | None = None
    bus: Any | None = None
    conn: Any | None = None

    #: 起始 URL 与浏览器通道（复用系统浏览器，不捆 Chromium）
    start_url: str = "http://127.0.0.1:8899/quiz.html"
    browser_channel: str = "msedge"

    #: 目标采集源与目标 ID（P11）。
    #:
    #: 两者都给了才会**附加到用户正在用的目标**；否则退回 ``start_url`` 那条
    #: 自启靶场的老路。保留老路是因为它至今仍是**唯一的回归基础设施** ——
    #: 675 个测试全部走它（见 ``target/mock_source.py`` 的说明）。
    target_source: Any | None = None
    target_id: str | None = None

    #: 条目工厂：给一条 ``TaskItem`` 造执行器 / 校验器
    actuator_factory: Callable[[TaskItem], Any] | None = None
    verifier_factory: Callable[[TaskItem], Any] | None = None

    #: **视觉识别模型**的 Provider 链（用户单独选的那一套）。
    #:
    #: 为空则退回 ``solver.providers``（判题那一套）—— 保住 P4~P12 的既有行为：
    #: 那时只有「一套模型」，读图和判题共用。两档分开的意义见
    #: :attr:`core.config.RunConfig.vision_profile_id`。
    vision_providers: Any | None = None

    #: 限速钩子：可注入以便单测断言「确实节流了」而不真的睡 5 秒
    click_gap: Callable[[], Awaitable[None]] | None = None
    submit_gap: Callable[[], Awaitable[None]] | None = None

    #: 感知上下文里的单通道预算（秒）
    probe_timeout_s: float = 8.0

    #: 观看一集的超时（秒）。``None`` = 按时长自动推算（时长 + 余量）。
    #: 单测注入小值即可让「既没播完也没弹题」的暂停分支瞬间到达。
    media_watch_timeout_s: float | None = None

    #: 「下一集」最多连点多少次（续跑时用来快进到目标集）。
    #: 只是防呆上限 —— 正常分集数远小于它。
    episode_advance_max: int = 200

    #: 运行期累计的告警（写回 ``RunContext.warnings``）
    warnings: list[str] = field(default_factory=list)


class Orchestrator:
    """任务主循环。"""

    def __init__(self, ctx: RunContext, *, deps: RunDeps | None = None) -> None:
        self.ctx = ctx
        self.deps = deps if deps is not None else RunDeps()
        self.stack = TaskStack()
        #: 本次运行涉及的任务（新建或从库里恢复）
        self.items: list[TaskItem] = []

        self._page: Any | None = self.deps.page
        #: 当前附加到的目标句柄（P11）。自启靶场路径下为 ``None`` ——
        #: 那条路没有「外部目标」这个概念。
        self._target: Any | None = None
        #: 视觉**读题**的产出（按 ``qid`` 索引）：题干之外还带几何，
        #: 供执行层按坐标点击。v0.2.0 起这是**唯一**的读题来源 ——
        #: 题目侧只有视觉一条路，所以每个做过作答的 ``qid`` 都该在这里有一份。
        self._vision_reads: dict[str, tuple[ReadResult, tuple[int, int]]] = {}
        #: 视觉读题**被门禁拦下**时的具体原因（``vision_incomplete`` /
        #: ``vision_uncertain``）。主循环用它当暂停理由 —— 否则界面上只会看到
        #: 笼统的 ``perception_failed``，而那个词把排查方向指向"没读到"，
        #: 与真实情况（**读到了，但题面残缺/拿不准**）正好相反。
        self._vision_gate_reason: str | None = None
        #: 最近一次视觉读图的底层失败码（rate_limited / read_parse_failed 等），供诊断事件使用。
        self._last_vision_error: str | None = None
        #: **同一屏**读到、还没做的其余题目（视觉组一次读一屏，见 ``prompts/10-视觉组.md``）。
        #:
        #: 它们的几何已经在 :attr:`_vision_reads` 里备好，所以取用时**不需要再截一次图、
        #: 再调一次模型** —— 这就是「减少消耗」的落点：长页面一屏两三道题时，
        #: 从前每道题各付一次截图 + 一次调用，现在一次调用管一屏。
        #:
        #: 什么时候必须清空：**画面一旦变了，这批几何就不再属于当前画面**。
        #: 清空点只有一处 —— ``_advance`` 的入口（推进必然改画面，失败也可能已经滚动）。
        self._pending_reads: list[Question] = []
        #: **最近一次读图拿到的整批观测**（``ReadBatch``，含 ``page`` 块）与那张图的尺寸。
        #:
        #: 为什么留的是「整批」而不是「第一道题」：``page`` 是**页面级**的事实
        #: （进度、控件、答题卡、提交按钮），不属于某一道题。开局裁决
        #: （:meth:`_plan_run`）与收尾确认都要看它。
        self._last_batch: ReadBatch | None = None
        self._last_size: tuple[int, int] | None = None
        self._gate = asyncio.Event()
        self._gate.set()
        self._stopped = False
        self._paused_by: str | None = None
        self._visited: set[str] = set()
        self._current: TaskItem | None = None
        #: **开局裁决出的运行方案**（``core/run_plan.py``）。``None`` = 还没裁决
        #: 或开局那次读图没成 —— 两条路都按「没有方案就不动手」处理
        #: （推进退回收尾确认，绝不瞎试）。
        #:
        #: ⚠️ 它是**整条运行唯一的推进与提交逻辑**：裁决一次，之后每一步都照它执行。
        #: 运行期不再问模型「下一题在哪」，也没有「点击不行改滚动」的换招阶梯。
        self._plan: RunPlan | None = None
        #: 方案是否已经**尝试过**裁决（不管成没成）。它守住「只裁决一次」。
        self._planned = False
        #: 裁决时那张图的尺寸（归一化框 → 像素的换算基准）。
        #:
        #: 方案里的控件框 / 答题卡步距都来自**那一次**截图的坐标空间，
        #: 所以点它时必须配**那一次的尺寸** —— 拿别的尺寸换算会整体偏移。
        self._plan_size: tuple[int, int] | None = None
        #: 答题卡模式下「当前题号」的推进链（由裁决结果初始化，每成功一步 +1）。
        #:
        #: 为什么用**序号链**而不是每一步重新读页面：点题号是「跳到第 N 题」，
        #: 第 N 题的落点是开局算好的纯算术 —— 每一步都重新判断，
        #: 就又有「这次跳到别处」的可能。链是确定的，**校验**（读回来的题号）
        #: 负责在偏了的时候立刻停下（见 :attr:`_expected_num`）。
        self._card_number: int = 1
        #: 推进之后**期望**读到的题号。非 ``None`` 时主循环会拿读回来的
        #: ``num_text`` 与它比对，不一致就按 ``advance_failed`` 停下。
        #:
        #: 这是防「静默跳题」的最后一道：坐标算歪、答题卡内部滚过、页面重排，
        #: 都会表现为「点完读到的是另一道题」—— 那比停下来问人严重得多。
        self._expected_num: int | None = None
        #: **推进之前**那道题的题号（读回来仍是它 = 这一下推进没生效）。
        #:
        #: 为什么要单独记：真机日志 ``logs/9abc4ed8ef60`` 里，点了答题卡 27 号格之后
        #: 重新读图仍读到 **26 题**（同一个 qid），当时的诊断只有一句
        #: ``wrong_question expected=27 got=26`` —— 那句话把人引向「坐标算错了」，
        #: 而真实情况是「页面根本没动」。两者要采取的动作完全不同，所以必须分开。
        self._num_before_advance: int | None = None
        #: 本屏**从队列里消费掉**的题目数（同屏多题时第 2、3 题走这条路）。
        #:
        #: 为什么要单独计数：这些题**不经过答题卡推进**，所以 :attr:`_card_number`
        #: 链不会跟着走；而「最近一次读图」给的 ``card.next_box`` 是**相对那一屏的
        #: 首题**算的（读图时 current=26，故它指 #27）。等 #27 也做完之后，
        #: 那个指针就**落后于我们的实际位置了** —— 照着点就是又点刚做完那道题。
        #: 这个计数既用来把新鲜的答题卡指针判为陈旧，也用来给题号修正设一个**有界**窗口。
        self._queued_consumed: int = 0
        #: 收尾确认那一次读图给出的**提交按钮框与那张图的尺寸**（整卷收尾用）。
        #:
        #: 为什么要有它：开局那一屏常常看不到「交卷」（它在页脚，或者要滚到底），
        #: 而收尾确认本来就要读一屏 —— 顺手把它记下来，收尾提交就不必再问一次。
        #: 尺寸必须与框**同源**：拿方案那次截图的尺寸去换算收尾这屏的框，
        #: 点出来的位置会整体偏移（真机上就是「点歪了」）。
        self._end_submit: tuple[tuple[float, float, float, float], tuple[int, int]] | None = None
        #: **最近一次**观测到的提交按钮框与那次截图的尺寸（每题提交用）。
        #:
        #: 提交框一共有三个来源（优先级见 :meth:`_submit_and_confirm`）：
        #: 开局方案、最近一次读图（这个）、收尾那一屏。为什么要有它：
        #: 「每题提交」需要的正是**当前这道题所在那一屏**的提交按钮，
        #: 而开局那一屏的框可能要等好几题之后才出现 —— 少了这一路，
        #: 关掉开局判定（``guards.advance_calibrate=False``）时每题提交会因为
        #: 「没有框」而暂停（实测踩到）。
        self._last_submit: tuple[tuple[float, float, float, float], tuple[int, int]] | None = None
        #: 收尾确认的缓存，键为 ``(qid, 触发点)``。
        #: 缓存是防绕圈用的：同一个触发点在同一道题上只问一次。
        self._completion_checks: dict[tuple[str, str], CompletionCheck] = {}
        #: 视觉组**确认过「已全部完成」**（任一触发点）。
        #:
        #: 它是整卷提交的前提：只有真正被确认过完成，才允许去点「交卷」。
        #: 不拿「主循环没有暂停」当前提 —— ``_advance`` 在拿不到 actuator 时也会
        #: 直接返回 ``False``（既没确认也没暂停），那条路不该被解释成「做完了」。
        self._completion_confirmed = False
        #: 本次运行**一屏读到过的最多题数**。它只是「一屏多题」的结构性证据，
        #: 供裁决前的兜底使用（正式判据在 :meth:`_submit_scope_of_run`）。
        self._batch_max = 0
        #: 最后一道**真正点过选项**的题（存 ``item_id``，**不是** ``qid``）。
        #:
        #: 收尾提交要用它的 ``submit_box``。存 item_id 是因为 :meth:`_find_item`
        #: 按 item_id 索引 —— 拿 qid 去查只会**静默查不到**，整卷提交就永远不发生
        #: （不报错、不留痕，只有「什么都没提交」这一个现象）。
        self._last_applied_item_id: str | None = None
        #: 整卷提交是否已经做过（**绝不重放** —— 提交是本项目唯一的不可逆动作）。
        self._paper_submitted = False
        #: 本次运行的**分集目录**（``vid → Episode``），网课场景专用
        self._episodes: dict[str, Any] = {}

    # ------------------------------------------------------------------ 生命周期

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def paused(self) -> bool:
        """是否处于暂停（含「等人确认」这种暂停）。"""
        return not self._gate.is_set()

    @property
    def paused_by(self) -> str | None:
        """暂停原因；``None`` 表示没暂停。"""
        return self._paused_by

    async def run(self) -> None:
        """跑完整个任务序列。可被 :meth:`stop` 提前结束。"""
        self._open_run()
        self._restore()
        try:
            if self._page is not None:
                await self._drain(self._page)
            else:
                async with self._page_session() as page:
                    self._page = page
                    await self._drain(page)
        finally:
            # 先收尾落库，**再**释放连接 —— 反过来的话 finish_run 会写到一条
            # 已关闭的连接上，运行状态永远停在 running（P7 实测踩到过）。
            self._close_run(self._final_status())
            await self._aclose()

    def _final_status(self) -> str:
        """收尾状态。**「暂停等人」不能记成 finished** —— 那会让界面以为跑完了。"""
        if self._stopped:
            return "stopped"
        if self._paused_by is not None:
            return f"paused:{self._paused_by}"
        return "finished"

    async def pause(self) -> None:
        """暂停：循环会在下一个条目边界停下。**不打断正在进行的动作**。

        不打断是刻意的 —— 在「点了选项还没提交」的位置硬切，会留下一个状态
        未知的页面，比多跑完一道题危险得多。
        """
        self._paused_by = self._paused_by or "manual"
        self._gate.clear()
        self._emit(Event.RUN_PAUSED, {"run_id": self.ctx.run_id, "reason": self._paused_by})

    async def resume(self) -> None:
        """恢复。放行闸门，**下一轮循环会重新读库核对真实状态**。"""
        self._paused_by = None
        self._gate.set()
        self._emit(Event.RUN_RESUMED, {"run_id": self.ctx.run_id})

    async def stop(self) -> None:
        """停止。释放所有等待者，循环在最近的检查点退出。"""
        self._stopped = True
        self._paused_by = None
        self._gate.set()

    # ------------------------------------------------------------------ 主循环

    async def _drain(self, page: Any) -> None:
        if not self._has_perception():
            raise RuntimeError("编排层缺少感知依赖（pipeline / adapter），无法启动")
        if TaskType.VIDEO in self.ctx.cfg.task_sequence:
            await self._drain_video(page)
        else:
            await self._drain_quiz(page)

    async def _drain_quiz(self, page: Any) -> None:
        # **开局判定**：让视觉组看一眼，程序据此裁决出唯一一份运行方案
        # （推进方式 + 落点几何 + 提交范围 + 总题数）。
        # 放在最前面是刻意的 —— 后面每一步推进都按它执行，判晚了就白推一轮。
        # 这一次读图的产出**同时**是首屏的题（不再多花一次模型调用）。
        await self._plan_run(page)
        position = 0
        while not self._stopped:
            await self._gate.wait()
            if self._stopped:
                break

            perception = await self._perceive(page, position)
            question = perception.question
            if question is None:
                # 流水线在 v0.2.0 里**只出图、不识别**（``arbiter`` 的产出恒为
                # ``question=None`` + 一张截图），所以「题面是什么」一律在这里
                # 交给视觉模型读出来 —— 这是正常路径，不是兜底。
                question = self._take_queued_question()
                if question is None:
                    question = await self._read_question_by_vision()
                if question is not None:
                    # 把读题结果**并入感知结果**，而不是只放在这个局部变量里。
                    # 下游（``_step_quiz`` / 留痕 / 截图目录）一律只认
                    # ``perception.question`` —— 分成两条路传，必然在某处漏掉一条。
                    # 实测踩到：读题成功（日志里 ``vision_read=ok``、题干与选项都对），
                    # 任务却在下一行立刻被判 ``failed``，暂停理由还是
                    # ``perception_failed`` —— 看上去像"没读到"，其实是读到没传下去。
                    perception = perception.model_copy(update={"question": question})
            if question is None:
                # 双失败（或视觉读题被门禁拦下）：这里负责「真的停下来」。
                #
                # 门禁拦下时用**具体**原因。笼统的 ``perception_failed`` 会把排查
                # 方向指向"没读到"，而真实情况是**读到了、但题面残缺或模型拿不准**
                # —— 两者的下一步动作完全不同（一个是查通道，一个是核对画面）。
                self._pause_for(self._vision_gate_reason or "perception_failed")
                break

            question = await self._reconcile_advance_read(page, question)
            if question is None:
                # 推进之后读到的**不是**方案里那一道（题号对不上）。
                # 继续做下去就等于**静默跳题**：跳过的那几道不会被任何人发现。
                break

            if question.qid in self._visited:
                # 同一道题被读了两遍 —— 「下一题」没生效。再点下去只会无限循环。
                self._pause_for("question_did_not_advance")
                break
            self._visited.add(question.qid)

            item = self._claim_item(question, position)
            item.attempts += 1
            self._persist_item(item)
            self._current = item

            if question.skill_error is not None or question.skill_id is None:
                self._skip_unsupported_question(
                    item,
                    question,
                    question.skill_error or "no_matching_skill",
                    self._vision_reads.get(question.qid),
                )
            elif is_terminal(QuestionState(item.state)):
                # 续跑：这题已经了结（含 submitted → verified），**只前进不重做**
                self._emit(
                    Event.TASK_UPDATED,
                    {
                        "item_id": item.item_id,
                        "state": str(item.state),
                        "resumed": True,
                        "redone": False,
                    },
                )
            else:
                await self._step_quiz(item, perception=perception)

            position += 1
            if self._stopped or self.paused:
                break
            # **同屏还有题没做 → 先做完，再推进。**
            #
            # 几何是**相对视口**的（归一化到当前画面），页面一滚就全部作废 ——
            # 所以「复用同屏几何」与「做完一题就推进」不能共存。原先少了这一句：
            # 每轮末尾都会走 `_advance`，而它的入口**清空队列**，于是同屏读到的
            # 第 2、3 题下一轮必然被丢掉，「一屏多题省调用」从未真正生效 ——
            # 表现为日志里 `batch:2 queued:1` 看着很省，实际下一题又从头读了一次图。
            if self._pending_reads:
                continue
            if not await self._advance(page, position):
                break

        self._current = None
        # —— 整卷收尾提交（2026-09-28 加）——
        #
        # 放在主循环**之后**而不是每题末尾：真实作业页上「交卷」是全页唯一的按钮，
        # 一按就结束整场。原先每题都点一次它 —— 做完第 1 题就去交卷，
        # 留痕 ``region_mad=0.06``（按钮那时被禁用，点了毫无反应）。
        # 这里只负责「调用时机」，判据全在 :meth:`_submit_paper_once` 里。
        await self._submit_paper_once(page)

    async def _advance(self, page: Any, position: int | None = None) -> bool:
        """推进到下一题。返回 ``False`` 表示**这次运行到此为止**。

        **只按开局裁决的那一种方式推进，一次都不换招**（2026-09-30 收口）：

        ===================  ==========================================================
        ``method=card``      点答题卡里「下一个题号」那一格（开局算好的几何 + 题号推算）
        ``method=click``     点开局观测到的那一个固定控件（「下一题 / 下一页 / 继续」）
        ``method=scroll``    向下滚动，滚一步读一屏，**确认新题真的进来了**才算到位
        ``method=swipe``     滑动手势（页面不可滚动时的最后一招）
        ===================  ==========================================================

        为什么把「换招」整条删掉：用户实测判定这一块有严重问题。旧实现在
        ``scroll`` 模式下照样会先去点按钮、找不着就继续往下滚，一路滚到上限
        （0.8 屏 × 6 = **4.8 屏**），真机题号从 **3 直接跳到 16**；而
        ``unknown`` 时那套「点击 → 滚动 → 滑动」的默认阶梯更是每一步都在换逻辑。
        现在**推不动就停下**：请视觉组确认一次「是不是全部完成了」，
        确认不了按 ``advance_failed`` 停下等人。

        怎么判断「做完了」——两处硬闸门，都不靠猜：

        1. 已完成数达到裁决出的题目总数 → 先让视觉组确认一次；
        2. 这一种方式推不动 → 再让视觉组确认一次。

        确认「已全部完成」才干净收工；**确认不了就按 ``advance_failed`` 停下**，
        绝不静默跳过后面所有题。
        """
        # **一推进，画面就会变** —— 同一屏读到的其余题目，几何从此不再属于当前画面。
        # 放在**入口**清（而不是每种推进成功之后）是刻意的：推进失败也可能已经滚动
        # 或切过页，那些几何同样作废；漏清一次就是拿旧坐标去点新画面。
        self._pending_reads.clear()

        actuator = self._actuator_for(self._current)
        if actuator is None:
            return False

        # 闸门 1：已达裁决出的总数。**先确认再做别的** —— 这时多推一步就可能
        # 跨过卷末（有些站点会在最后一题之后把页面跳到成绩页）。
        # 模型说还没做完时**不返回**：总数可能读错了，继续按方案推。
        if self._plan_reached(position) and await self._confirm_completion(
            page, "reached_total"
        ):
            return False

        plan = self._plan
        if plan is None:
            # 没有方案 = 开局那次读图没成。**没有逻辑就不动手**（一个坐标都不点）。
            self._emit(
                Event.LOG_LINE,
                {"advance": "no_plan", "hint": "开局未能裁决推进方式，按收尾确认处理"},
            )
            return await self._settle_end(page)

        if plan.method in {AdvanceMethod.CLICK, AdvanceMethod.CARD} and (
            not self._visual_fallback_enabled()
        ):
            # 兜底关掉：按坐标点击整体禁用（用户在守卫里显式要求的）→ 不点，进收尾。
            self._emit(Event.LOG_LINE, {"advance": "fallback_off", "method": plan.method.value})
            return await self._settle_end(page)

        # **从固定的推进方式库取这一种方式**（``core/advance_library.py``）。
        # 库是「可选项的唯一定义点」：库里没有的做法，程序就不做。
        strategy = strategy_for(plan.method)
        self._emit(
            Event.LOG_LINE,
            {
                "advance_library": strategy.key,
                "label": strategy.label,
                "needs_coords": strategy.needs_coords,
                "planned": True,
            },
        )
        # 记下「推进前是哪一题」：下一轮读回来仍是它 = 这一下没生效（见
        # :meth:`_expected_number_ok`）。必须在动手之前取，动手之后就读不到了。
        self._num_before_advance = self._current_number()
        before = await self._page_signature(page)
        if await self._run_advance_strategy(plan, page, actuator, before):
            await self._settle_after_advance(page)
            # 推进真的发生了 → 题号链与页面重新对齐，「同屏消费」的欠账清零。
            self._queued_consumed = 0
            self._emit(Event.LOG_LINE, {"advance": strategy.key, "planned": True})
            return True

        # 方案里那一招走不通 —— **不换别的招硬试**，直接进收尾确认。
        self._emit(
            Event.LOG_LINE,
            {
                "advance": "stuck",
                "method": plan.method.value,
                "hint": "开局裁决的这一种推进方式没推成，按方案不换招；转收尾确认",
            },
        )
        return await self._settle_end(page)

    async def _run_advance_strategy(
        self,
        plan: RunPlan,
        page: Any,
        actuator: Any,
        before: str | None,
    ) -> bool:
        """按**方案里定的那一种方式**推进一步。返回是否真的推进了。"""
        if plan.method is AdvanceMethod.CARD:
            return await self._advance_by_card(plan, page, actuator, before)
        if plan.method is AdvanceMethod.CLICK:
            return await self._advance_by_control(plan, page, actuator, before)
        if plan.method is AdvanceMethod.SCROLL:
            return await self._advance_by_scroll(page, actuator, before)
        if plan.method is AdvanceMethod.SWIPE:
            return await self._advance_by_swipe(page, actuator, before)
        # ``UNKNOWN`` 不该走到这里（裁决一定会给出可执行的方式）。
        self._emit(Event.LOG_LINE, {"advance": "undecided", "method": plan.method.value})
        return False

    def _current_number(self) -> int | None:
        """当前这道题在页面上印的题号（读不出返回 ``None``）。"""
        current = self._current
        if current is None or not current.qid:
            return None
        return _first_int(self._num_text_of(current.qid))

    async def _settle_after_advance(self, page: Any) -> None:
        """推进动作之后、下一次截图之前的**无条件沉淀**。

        为什么非有不可：页面切题是异步的（进度条与题号先变、题面随后才换），
        动作一返回就截屏，截到的往往是**换到一半的旧画面** —— 那会把一次
        **成功的**推进读成「推进没生效」，真机日志 ``logs/9abc4ed8ef60``
        就是这么停下等人的。

        与 ``_page_changed`` 的分工：那一处靠**页面指纹**判「变了没有」，
        指纹取不到时（题干画在 canvas、``evaluate`` 受限）会整条跳过；
        这一处是无条件兜底，保证「动作之后绝不零等待截屏」。
        预算是 ``guards.advance_settle_ms``，可配（设 0 即关掉）。
        """
        del page  # 只按时间沉淀，不读页面 —— 读得到指纹时 `_page_changed` 已经等过了
        settle_ms = max(0, int(getattr(self.ctx.cfg.guards, "advance_settle_ms", 350)))
        if settle_ms <= 0:
            return
        await asyncio.sleep(settle_ms / 1000.0)

    def _plan_reached(self, position: int | None) -> bool:
        """已完成数是否已达到裁决出的题目总数。

        ``position`` 是**下一题的序号（0 基）**，所以「已完成数」= ``position``。
        不知道总数时**永远返回 False** —— 不能拿「不知道」当「做完了」。
        """
        plan = self._plan
        if plan is None or not plan.knows_total:
            return False
        return plan.reached_total(max(0, position or 0))

    def _fresh_page_view(self) -> PageView | None:
        """**最近一次读图**给出的页面观测。

        它是推进落点的**新鲜来源**：开局那一屏算好的几何只对那一屏成立 ——
        页面一滚、答题卡内部一滚、浏览器一缩放，旧框就全部作废。
        真机日志 ``logs/9abc4ed8ef60`` 里 ``advance_card`` 连着两次点偏，
        根因就是拿旧几何去点新画面。``None`` = 这一次没拿到观测（退回方案几何）。
        """
        batch = self._last_batch
        return batch.page if batch is not None else None

    def _fresh_advance_target(
        self, kind: str
    ) -> tuple[tuple[float, float, float, float], tuple[int, int]] | None:
        """从最近一次观测里取这一步的落点。``kind`` 取 ``"control"`` / ``"card"``。

        两条纪律：

        * ``control``：模型这一屏指出的「下一题」控件框。**每次读图都会重新给**，
          所以它比开局那一次的框新。
        * ``card``：模型这一屏指出的「下一题号那一格」。它必须与模型**同屏**给出的
          ``current_box`` **相邻**（相差不超过 ``_CARD_ADJACENT_MAX_CELLS`` 格）：
          答题卡一格就是一个题号，隔好几格就是**一次跨过好几道题**（静默跳题）。
          两种不采信的情形：

          1. 与 ``current_box`` **明显重叠** —— 画面上看不见下一格时模型常把「当前格」
             填进来，照着点等于原地不动，再被判成 ``wrong_question``
             （用户看到的「推进之后与实际不符」）；
          2. 离 ``current_box`` 太远，或模型**根本没给** ``current_box``
             —— 相邻性无从校验，宁可退回**有界的**开局算术落点，也不拿一个
             可能越过好几题的坐标去点用户的页面。

        框先过一遍范围校验（越界 / 倒挂一律丢弃）：一个非法框在下游就是一次真实误点。
        """
        view = self._fresh_page_view()
        size = self._last_size
        if view is None or size is None:
            return None
        if kind == "control":
            box = view.next_control.box if view.next_control is not None else None
        else:
            card = view.card
            box = card.next_box if card is not None else None
            if box is not None and card is not None:
                if self._queued_consumed > 0:
                    # 这一屏已经被我们**做掉了不止一道题**：观测里的 ``next_box``
                    # 是照「那一屏的头一道题」算的，此刻已落后于我们的实际位置 ——
                    # 照着点就是又点刚做完那道题（页面不动 → 判成推进没生效）。
                    # 独立验证者用现场形状复现过这条旁路。
                    self._emit(
                        Event.LOG_LINE,
                        {
                            "advance_card": "next_box_stale_screen",
                            "consumed": self._queued_consumed,
                            "hint": "本屏已从队列消费过题目 → 这一屏给的「下一格」已落后，"
                            "退回按实际题号算出的落点",
                        },
                    )
                    return None
                if card.current_box is None:
                    self._emit(
                        Event.LOG_LINE,
                        {
                            "advance_card": "next_box_unverifiable",
                            "hint": "模型没给「当前题号格」→ 无法确认「下一格」是否相邻，"
                            "退回开局算好的有界落点",
                        },
                    )
                    return None
                if _box_overlap_ratio(box, card.current_box) > 0.5:
                    self._emit(
                        Event.LOG_LINE,
                        {
                            "advance_card": "next_box_suspect",
                            "hint": "「下一格」与「当前格」几乎重叠（看不清下一格时模型会这样填）"
                            "→ 不采信，退回开局算好的落点",
                        },
                    )
                    return None
                gap = _card_index_gap(card, box, card.current_box)
                if gap is None or gap > _CARD_ADJACENT_MAX_INDEX_DELTA:
                    # ``gap is None`` = 网格信息不全（缺 cols / rows / 外框），
                    # 相邻性**无从校验** → 与「太远」同处置：不采信。
                    self._emit(
                        Event.LOG_LINE,
                        {
                            "advance_card": (
                                "next_box_unverifiable"
                                if gap is None
                                else "next_box_too_far"
                            ),
                            "cells": gap,
                            "hint": "「下一格」与「当前格」相差不止一格（或网格信息不全、"
                            "无从校验）—— 点它等于一次跨过好几道题，退回开局算好的落点",
                        },
                    )
                    return None
        if box is None or not _norm_box_ok(box):
            return None
        return box, size

    async def _advance_by_control(
        self, plan: RunPlan, page: Any, actuator: Any, before: str | None
    ) -> bool:
        """点「下一题 / 下一页 / 继续」控件。

        落点优先取**本次读图刚给出的框**（:meth:`_fresh_advance_target`），
        取不到才退回开局裁决时记下的那一个 —— 开局几何只在那一屏成立，
        而真实页面的控件会随滚动 / 布局变化漂移。
        """
        fresh = self._fresh_advance_target("control")
        if fresh is not None:
            box, size = fresh
            source = "read"
        else:
            planned = plan.control_box
            planned_size = self._plan_size
            if planned is None or planned_size is None:
                self._emit(Event.LOG_LINE, {"advance_click": "no_geometry"})
                return False
            box, size = planned, planned_size
            source = "plan"
        return await self._click_box(page, actuator, box, size, source=source, before=before)

    def _card_advance_target(self, plan: RunPlan) -> int:
        """这一步要跳到第几题（**必须用方案自己的题号基底**）。

        **题号可信**（``plan.numbers_known``：开局观测到了「当前第几题」）时，
        以「刚刚做完那道题」的题号 +1 为准，而不是只看 :attr:`_card_number` 链。

        为什么：一屏多题时，同屏的第 2、3 题由 :meth:`_take_queued_question`
        **直接从队列消费** —— 那几步不经过本方法，链就不会 +1。于是下一题的
        目标又指回**刚做完的那一道**，点下去页面不动，随后被判成「推进没生效」。
        真机 ``logs/9abc4ed8ef60`` 正是如此：一屏读到 26 / 27 两道，
        两道都做完之后点答题卡的 **27 号格**（＝刚才那道），页面纹丝不动。

        **题号不可信**时**不能**这么做：那时方案的题号基底是「从 1 之后起算」的
        合成编号（``card_start_number`` 退回 1，``card_anchor`` 是照模型指出的
        ``next_box`` 反推的），拿页面上的真实题号去喂 ``card_target`` 会解析到
        完全不同的格子。这种情况仍走 :attr:`_card_number` 链 —— 它在这个基底下
        是自洽的（每次成功 +1）。

        修正**有界**：只允许在 ``[链目标, 链目标 + 同屏已消费的题数]`` 这个窗口里
        往前修正。窗口的右边就是「我们确实比链多做了几道」的事实依据；超出窗口
        说明读到的题号本身不可信（例如把 26 读成 30），那时**退回链目标**——
        链是每成功一步 +1 的，它自己**永远不会跳过题**，宁可少走一步（会被
        ``_expected_number_ok`` 抓到并停下），也绝不静默跨过中间几道。
        """
        chain_target = self._card_number + 1
        if not plan.numbers_known:
            return chain_target
        just_done = self._current_number()
        if just_done is None:
            return chain_target
        candidate = just_done + 1
        lower = chain_target
        upper = chain_target + max(0, self._queued_consumed)
        if lower <= candidate <= upper:
            return candidate
        logger.debug(
            "答题卡题号修正被拒：candidate=%s 不在 [%s, %s]（chain=%s queued=%s）",
            candidate,
            lower,
            upper,
            self._card_number,
            self._queued_consumed,
        )
        return lower

    async def _advance_by_card(
        self, plan: RunPlan, page: Any, actuator: Any, before: str | None
    ) -> bool:
        """点**答题卡**里下一个题号那一格（真实作业页最常见的那种切题方式）。

        落点是**优先用本次读图刚指出的那一格**，取不到才退回开局算术：

        * 目标题号 = :meth:`_card_advance_target`（**刚做完那道题的题号 + 1**，
          不是 :attr:`_card_number` 链 —— 同屏其余题走队列，链不会跟着走）；
        * 算不出那一格的落点（超出可见区域 / 缺列行数）→ **不点**，返回 ``False``
          交给收尾确认 —— 绝不拿一个编出来的坐标去点；
        * 点完**不在这里回读**：读题由主循环照常做，而主循环会用
          :attr:`_expected_num` 校验「读回来的题号对不对」，对不上就停下。
        """
        target = self._card_advance_target(plan)
        size = self._plan_size
        # 落点优先用**本次读图刚指出的那一格**（模型看得见画面，比开局算术新）；
        # 取不到（与「当前格」重叠、离得太远、或模型没说当前格在哪）才退回开局算术。
        fresh = self._fresh_advance_target("card")
        box = fresh[0] if fresh is not None else plan.card_target(target)
        if fresh is not None:
            size = fresh[1]
        if size is None or box is None:
            self._emit(
                Event.LOG_LINE,
                {
                    "advance_card": "no_target",
                    "number": target,
                    "hint": "答题卡里这一格的落点推不出来（超出可见区域或缺列行数）",
                },
            )
            return False
        if not await self._click_box(page, actuator, box, size, source="card", before=before):
            return False
        self._card_number = target
        # 下一轮读到的题**应当**是这个题号；对不上就停下（防静默跳题）。
        # 只有**题号可信**时才校验：观测没给出当前题号时，这个 target 是
        # 「从第 1 题之后起算」的序号，拿它去比对真实题号会把正常推进判成跳题。
        self._expected_num = target if plan.numbers_known else None
        self._emit(
            Event.LOG_LINE,
            {
                "advance_card": "clicked",
                "number": target,
                "box": [round(value, 4) for value in box],
            },
        )
        return True

    async def _reconcile_advance_read(self, page: Any, question: Question) -> Question | None:
        """推进之后那次读图的**旧画面复核**（2026-10-01 加）。

        真机故障：点了答题卡 27 号格，重新读图仍读到 26 题 —— 页面切题是异步的，
        读题紧跟着动作截屏，截到的正是**换到一半的旧画面**。旧实现把它直接判成
        ``wrong_question`` 停下等人，而页面上其实什么都没坏。

        判据只有一条：**读回来的题号（数字）与预期不符**。不符就先沉淀、再补读一屏，
        然后交给 :meth:`_expected_number_ok` 定论。补读**只读不点** ——
        重试点击才是「一次跨过两道题」的真正来源，这里一次都不点。

        ⚠️ 补读条件**刻意不做成「读到的正好是上一题」**（第一版就是那样写的）：
        旧画面可能比「上一题」还旧一帧（截屏抢跑时读到的是更早那一屏），
        现场 ``expected=27 / 上一题=27 / got=26`` 就属于这种，
        按「等于上一题」筛选会整条漏掉、等于没修。判据放宽到「任何数字不符」之后，
        真正的落点错误也只是多花**一次**读图，随后照样停下（判据没有放松）。

        返回 ``None`` 表示已经停下（调用方跳出主循环）。
        """
        expected = self._expected_num
        missed = self._num_before_advance
        got = _first_int(self._num_text_of(question.qid))
        if expected is None or got is None or got == expected:
            return question if self._expected_number_ok(question) else None

        # 读到的题号与预期不符：先沉淀再补读一屏（只读不点），别急着报错。
        await self._settle_after_advance(page)
        self._emit(
            Event.LOG_LINE,
            {
                "advance": "reread_stale",
                "expected": expected,
                "got": got,
                "before": missed,
                "hint": "推进后读到的题号与预期不符：疑似截屏抢在切题完成之前，"
                "补读一屏复核（只读不点）",
            },
        )
        again = await self._read_question_by_vision()
        if again is not None:
            again_num = _first_int(self._num_text_of(again.qid))
            if again_num is not None and again_num != got:
                # 补读到了**别的**题 → 上一次是旧画面，按这一次继续（判据照常跑）。
                self._expected_num = expected
                return again if self._expected_number_ok(again) else None
            if again_num == expected:
                # 补读正好是预期那一题（got 与 expected 之间只差一帧）。
                self._expected_num = expected
                return again if self._expected_number_ok(again) else None
        return question if self._expected_number_ok(question) else None

    def _expected_number_ok(self, question: Question) -> bool:
        """推进之后读到的题号是否符合方案里的预期。不符合就停下并返回 ``False``。

        三种情况分开处置（真机日志 ``logs/9abc4ed8ef60`` 就是被第一种误报的）：

        ====================  ==========================================================
        ``got == expected``   正常。清掉预期，继续做这一题
        ``got`` 读不出        不判。拿「没读到」当「跳题了」会把正常页面全拦下来
        ``got == 推进前那道`` **这一下推进没生效**（页面根本没动）—— 与「落到别的题」
                              不是一回事：前者要重试/等人点一下，后者是坐标算错了。
                              事件用 ``advance=no_effect``，理由写清「推进未生效」
        ``got`` 是别的数字    真的落到了别的题上 → ``advance=wrong_question``（防静默跳题）
        ====================  ==========================================================
        """
        expected = self._expected_num
        self._expected_num = None
        missed = self._num_before_advance
        self._num_before_advance = None
        if expected is None:
            return True
        got = _first_int(self._num_text_of(question.qid))
        if got is None or got == expected:
            return True
        if missed is not None and got == missed:
            # 「点了没反应」而不是「点错地方」：页面还停在推进前那道题上。
            # 这个区分至关重要 —— 旧实现把它一律报成 wrong_question，
            # 用户看到「expected 29 got 28」只能去怀疑坐标，而真相是这一步没生效。
            self._emit(
                Event.LOG_LINE,
                {
                    "advance": "no_effect",
                    "expected": expected,
                    "got": got,
                    "qid": question.qid,
                    "hint": "推进动作没有生效：页面仍停在推进前那道题 → "
                    "请手动点一次「下一题」或题号后恢复；若是滑动推进，可能是幅度不够",
                },
            )
            self._pause_for(ErrorCode.ADVANCE_FAILED.value)
            return False
        self._emit(
            Event.LOG_LINE,
            {
                "advance": "wrong_question",
                "expected": expected,
                "got": got,
                "qid": question.qid,
                "hint": "推进之后读到的题号与方案不符 → 可能算错落点，停下等人",
            },
        )
        self._pause_for(ErrorCode.ADVANCE_FAILED.value)
        return False

    async def _advance_by_scroll(self, page: Any, actuator: Any, before: str | None) -> bool:
        """向下滚动推进：**滚一步 → 读一屏 → 确认「下一题」真的进来了**。

        ``actuator`` / ``before`` 收下不用：滚动这条路**不点任何坐标**，也不靠页面
        指纹判断（判据是「这一屏有没有新题」）。保留它们只为三个推进方法签名一致，
        读调用点时不必记「哪个方法少一个参数」。

        与旧实现的差别只有一点，但它是致命的：旧版（``_scroll_then_look``）把滚动
        当作**找推进控件的手段** —— 滚完就去找按钮点，找不到就继续滚，一路滚到
        ``advance_scroll_max_steps``（0.8 屏 × 6 = **4.8 屏**）。2026-09-28 真机实测：
        题号从 **3 直接跳到 16**，中间 13 道题全部被跳过。

        现在的判据是「**这一屏有没有出现没做过的题**」：

        * 有 → 到位。那批题**直接收下**（几何入缓存、其余入队），主循环下一轮
          不必再读一次图 —— 所以这里多花的那次读图**不是额外开销**；
        * 没有（还是做过的那道）→ **滚少了** → 再滚一步，最多 ``max_steps`` 步；
        * 滚不动（到底 / 没有滚动条）→ 如实返回 ``False``，交给收尾确认。

        **2026-09-29 两处加固**（用户反馈「滚动过少或过多、跳过题目」）：

        1. **步长降到半步**（``advance_scroll_step_ratio`` 0.8 → 0.5，上限放宽到 12 步）。
           整屏滚动最容易把「上一屏底部还没露全」的题整格跨过去 ——
           半步滚动保证夹缝里的题一定会先被某一屏完整读到一次。
        2. **滚过头时往回补看一步**（``advance_scroll_recover_max``，默认 2 次）。
           判据是读到的题**题号跨度 > 1**（``_scroll_gap``）。它只在两端题号都是
           数字时才成立，且补看有界；补看不成仍回到「继续往下滚」的主线，
           绝不因为一次跳跃就丢弃或改写已做过的题。
        """
        del actuator, before  # 见上：滚动不看坐标，也不比指纹
        guard = self.ctx.cfg.guards
        steps = int(getattr(guard, "advance_scroll_max_steps", 12) or 0)
        if steps <= 0:
            return False
        ratio = float(getattr(guard, "advance_scroll_step_ratio", 0.5) or 0.5)
        recover_max = max(0, int(getattr(guard, "advance_scroll_recover_max", 2) or 0))
        recovered = 0
        for attempt in range(1, steps + 1):
            if not await self._scroll_once(page, ratio):
                # 滚不动了（到顶 / 到底 / 这页根本没有滚动条）：画面没变，
                # 再看一眼也只是白花一次模型调用。
                self._emit(Event.LOG_LINE, {"advance_scroll": "stuck", "attempt": attempt})
                return False
            status = await self._scroll_reveal_next(attempt)
            if status == "arrived":
                return True
            if status == "overshoot" and recovered < recover_max:
                # **滚过头了**：这一屏直接跳到了更靠后的题（题号跨度 > 1），
                # 说明中间有题被整屏跨过去了。往回滚一步再读，尽量把它们捞回来。
                #
                # 界限必须硬：只回滚 ``advance_scroll_recover_max`` 次，
                # 且只在**两端题号都是数字**时才可能判出 "overshoot"（见 `_scroll_gap`）。
                # 真实题库编号本来就可能不连续，所以这一条只用来「补一次观察」，
                # 绝不据此自动丢弃或改写已经做过的题。
                recovered += 1
                self._emit(
                    Event.LOG_LINE,
                    {
                        "advance_scroll": "recover_back",
                        "attempt": attempt,
                        "recovered": recovered,
                        "hint": "题号跨度 > 1，往回滚一步补看中间是否漏题",
                    },
                )
                if await self._scroll_once(page, -ratio) and (
                    await self._scroll_reveal_next(attempt) == "arrived"
                ):
                    if recovered:
                        self._emit(
                            Event.LOG_LINE,
                            {"advance_scroll": "recovered", "attempt": attempt},
                        )
                    return True
                # 补看不成 → 回到「继续往下滚」这条主线上（已经回滚过的那一步
                # 由下一轮的前向滚动补回来）。
                continue
            # "same"（还是做过的题）= 滚少了 → 再滚一步
        self._emit(Event.LOG_LINE, {"advance_scroll": "exhausted", "steps": steps})
        return False

    async def _scroll_reveal_next(self, attempt: int) -> str:
        """滚过之后读一屏，回答「下一题进来了没有」。

        返回三种结果之一（**不再是有/无二选一**，因为「滚多了」和「滚少了」
        需要完全相反的动作）：

        ==============  ==========================================================
        ``arrived``     新题进来了 —— 那一批题**已经入队 + 几何已缓存**，到位
        ``same``        这一屏还是**做过的题** → 滚少了，应当再滚一步
        ``overshoot``   进来的题**跳过了中间题号**（跨度 > 1）→ 可能滚多了
        ``empty``       这一屏读不出东西（切了一半 / 空白 / 模型没答好）
        ==============  ==========================================================

        为什么在这里读图、而不是交给主循环：**滚动量对不对，只有读了才知道**。
        主循环那次读图发生在推进**之后**，那时已经没法再滚 —— 而「滚少了」恰恰
        需要「再滚一点」。

        ``_read_question_by_vision`` 会把这一屏的题**入队 + 缓存几何**。读到的是
        **做过的题**时必须把这一批**整批丢掉**（连同它入队的其余题），否则主循环
        下一轮会拿起一道做过的题、然后以 ``question_did_not_advance`` 停下 ——
        那会把一次「滚少了」误报成「下一题按钮没生效」，排查方向整个跑偏。
        """
        mark = len(self._pending_reads)
        question = await self._read_question_by_vision()
        if question is None:
            # 这一屏读不出东西（切了一半 / 空白 / 模型没答好）→ 继续往下滚，
            # 顺手把这次可能入队的东西丢掉。
            del self._pending_reads[mark:]
            return "empty"
        if question.qid in self._visited:
            del self._pending_reads[mark:]
            self._emit(
                Event.LOG_LINE,
                {
                    "advance_scroll": "short",
                    "attempt": attempt,
                    "num_text": self._num_text_of(question.qid),
                    "hint": "滚少了：这一屏还是做过的题，继续往下滚",
                },
            )
            return "same"
        # **这一道也要入队。** `_read_question_by_vision` 只把同屏的**其余**题入队
        # （第一道是"返回给调用方"的），而滚动推进不消费返回值 —— 不补这一下，
        # 滚出来的第一道题就会被主循环当成「队列为空」而丢掉、再读一次图。
        self._pending_reads.insert(mark, question)
        gap = self._scroll_gap(question.qid)
        # ``gap > 1`` = 这一屏的题号跳了。**只告警留痕**：真实题库编号本来就可能不连续，
        # 所以它不能当自动回滚的依据。但它是「滚过头」的唯一客观信号，
        # 调用方据此决定要不要往回补看一步（有界）。
        if gap is not None and gap > 1:
            self._emit(
                Event.LOG_LINE,
                {
                    "advance_scroll": "jumped",
                    "attempt": attempt,
                    "num_text": self._num_text_of(question.qid),
                    "gap": gap,
                    "hint": "题号跨度 > 1：可能滚过头，也可能题库编号不连续",
                },
            )
            return "overshoot"
        self._emit(
            Event.LOG_LINE,
            {
                "advance_scroll": "arrived",
                "attempt": attempt,
                "num_text": self._num_text_of(question.qid),
                "skipped": gap,
            },
        )
        return "arrived"

    def _num_text_of(self, qid: str) -> str | None:
        """某道题在页面上**印的**题号（来自读题缓存）；没有就 ``None``。"""
        cached = self._vision_reads.get(qid)
        return cached[0].num_text if cached else None

    def _scroll_gap(self, new_qid: str) -> int | None:
        """从当前题到新读到的题，**题号跨了几道**。任一端读不出题号就是 ``None``。

        只用于**留痕告警**：``> 1`` 说明这一屏的题号跳了（滚多了，或者题库编号本就
        不连续）。真实题库里编号跳跃很常见，所以它**不能当自动回滚的依据** ——
        只能告诉人「这里发生过一次跳跃，去核一下有没有漏题」。
        """
        current = self._current
        before = self._num_text_of(current.qid) if current and current.qid else None
        after = self._num_text_of(new_qid)
        first, second = _first_int(before), _first_int(after)
        if first is None or second is None:
            return None
        return second - first

    async def _scroll_once(self, page: Any, ratio: float) -> bool:
        """滚一步（滚轮优先，退化到脚本滚动）。返回页面是否**朝预期方向**动了。

        ``ratio`` 带符号：正值向下、**负值向上**（「滚过头了往回补看一步」用它）。
        量值 = 视口高的 ``|ratio|``，最小 80px —— 太小的滚动在很多页面上会被
        平滑滚动吃掉，读位置时看着像「没动」。
        """
        try:
            size = await page.evaluate("() => ({h: innerHeight, y: scrollY})")
        except Exception as exc:
            logger.debug("读取滚动位置失败：%s", exc)
            return False
        if not isinstance(size, dict):
            return False
        height = float(size.get("h") or 0) or 800.0
        before = float(size.get("y") or 0)
        magnitude = max(80.0, height * min(1.0, abs(ratio) or 0.5))
        delta = magnitude if ratio >= 0 else -magnitude
        try:
            await page.mouse.wheel(0, delta)
        except Exception as exc:
            logger.debug("滚轮事件失败，退化到脚本滚动：%s", exc)
            try:
                await page.evaluate("(dy) => window.scrollBy(0, dy)", delta)
            except Exception as exc2:
                logger.debug("脚本滚动也失败：%s", exc2)
                return False
        await asyncio.sleep(0.12)
        try:
            after = await page.evaluate("() => scrollY")
        except Exception as exc:
            logger.debug("读取滚动位置失败：%s", exc)
            return False
        if not isinstance(after, (int, float)):
            return False
        moved = float(after) - before
        return moved > 1.0 if ratio >= 0 else moved < -1.0

    def _visual_fallback_enabled(self) -> bool:
        """找不到推进控件时，允不允许「看图找坐标 / 滑动手势」这两条兜底。

        v0.2.0 起判据只剩配置。原先还要看「这道题是不是 DOM 锚点读出来的」
        —— 有锚点的站点上「没有按钮」就等于干净跑完；那条锚点路已经删除，
        现在**唯一**的读题方式就是视觉，所以这里退化成一道纯粹的开关。
        """
        return bool(getattr(self.ctx.cfg.guards, "advance_visual_fallback", True))

    # -- 起始标定 / 收尾确认（2026-09-28） ---------------------------------- #

    def _vision_providers(self) -> list[Any]:
        """读图用的 provider 链：优先**视觉组**，为空则回落解题链。

        为什么必须回落而不是直接放弃：绝大多数用户只配一套模型
        （「视觉组留空 = 与解题组共用」），若不回落，这些人就永远标定不了、
        也永远确认不了收尾 —— 功能对最常见的用法直接失效。
        """
        vision = list(getattr(self.deps, "vision_providers", None) or [])
        if vision:
            return vision
        solver = self.deps.solver
        return list(getattr(solver, "providers", None) or []) if solver else []

    async def _plan_run(self, page: Any) -> None:
        """**开局判定**：读一屏 → 把视觉组的观测裁决成唯一一份运行方案。

        这是「程序开始运行时需要有一套判断逻辑，一旦判定好后后续的全部按照这套逻辑」
        的落点。裁决只有一次（:attr:`_planned` 守住），结果放进 :attr:`_plan`，
        之后**每一步推进与提交都只照它执行**。

        三个刻意的取舍：

        1. **只读一次图，两件事一起办。** 这一次读图既产出方案（``batch.page``），
           也产出首屏的题（``batch.questions``，已入队＋缓存几何）——
           不再像旧版那样「先标定一次、再读题一次」白花一次调用。
        2. **失败允许**：没有视觉 provider / 截图失败 / 模型答不出来 → ``_plan`` 保持
           ``None``。不在这里暂停 —— 主循环会用同一套读题逻辑给出确切原因
           （``vision_read_failed`` 等），用户看到的理由更准。
        3. **绝不因为方案没成而终止任务**：没有方案时推进按「不动手」处理。
        """
        if self._planned:
            return
        self._planned = True
        if not bool(getattr(self.ctx.cfg.guards, "advance_calibrate", True)):
            return
        if not self._vision_providers():
            self._emit(
                Event.LOG_LINE,
                {
                    "advance_calibrate": "skipped",
                    "reason": "没有可用的视觉模型（视觉组为空且解题链也没有 provider）",
                },
            )
            return
        # 这一次读图的产出被 `_read_question_by_vision` 记进 `_last_batch`。
        # 它的**返回值**是首屏的第一道题（``accepted[0]``），而 `_read_question_by_vision`
        # 只把 ``accepted[1:]`` 入队 —— 所以这里必须把它**放回队首**：
        # 主循环先取队列（``_take_queued_question``），没人接这个返回值，
        # 于是「一屏多题」时首屏的第 1 题会被**静默跳过**（读到了、几何也缓存了，
        # 却从头到尾没有作答）。放回队首还让这一屏的题按**页面顺序**作答。
        first = await self._read_question_by_vision()
        if first is not None:
            self._pending_reads.insert(0, first)
        batch = self._last_batch
        if batch is None:
            self._emit(
                Event.LOG_LINE,
                {
                    "advance_calibrate": "failed",
                    "reason": "开局读图未成，没有可裁决的观测",
                    "read_error": self._last_vision_error or self._vision_gate_reason or "unknown",
                    "hint": "查看 vision_read 事件；rate_limited 会重试，持续失败请检查额度",
                },
            )
            return
        plan = derive_plan(batch, batch_size=len(batch.questions))
        self._plan = plan
        self._plan_size = self._last_size
        self._card_number = plan.card_start_number
        self._emit(
            Event.ADVANCE_CALIBRATED,
            {
                "run_id": self.ctx.run_id,
                "method": plan.method.value,
                "total": plan.total,
                "current": plan.current,
                "scope": plan.submit_scope.value if plan.submit_scope else None,
                "summary": plan_summary(plan),
                "reason": plan.reason,
            },
        )

    async def _confirm_completion(self, page: Any, trigger: str) -> bool:
        """收尾闸门：读一屏，问「是不是全部做完了」。**只有 ``all_done`` 才允许收工。**

        2026-09-30 起走的是**同一份固定格式**（不再是一套单独的「收尾确认」提示词）：
        读一屏 → 看 ``page.completed``。三种情况的处置：

        ==================  ==========================================================
        ``all_done``        确认完成 → 允许收工（整卷还据此去点「交卷」）
        ``not_done``        还有题没做 → 不许收工，停下等人
        ``unknown`` /
        问不成              一律按**未确认**处理（没有 provider / 截图失败 / 解析失败）
        ==================  ==========================================================

        三条纪律，每条都对应一种真实的错法：

        1. **问不成 = 未确认**（返回 ``False``）：这是整个流程里唯一防跳题的闸门。
        2. **同一题同一触发点只问一次**：``reached_total`` 与 ``stuck``
           各自最多一次，避免在「推不动 → 问 → 说没完成 → 再推 → 再问」之间绕圈。
        3. **理由必须进事件流**：用户要能看懂「它凭什么说做完了」。

        顺带记下这一屏的**提交按钮框与那张图的尺寸**（:attr:`_end_submit`）：收尾本来就要读一屏，
        而「交卷」往往只在这一屏才露出来（开局那屏它在页脚之外）。
        """
        if not bool(getattr(self.ctx.cfg.guards, "advance_confirm_completion", True)):
            return False
        item = self._current
        # ``qid`` 理论上非空，但**不能拿理论上**去换一个 KeyError：
        # 归一化成空串，退化成「整个运行共用一次缓存」，而不是崩。
        key = ((item.qid if item is not None else None) or "", trigger)
        cached = self._completion_checks.get(key)
        if cached is not None:
            # 缓存命中也要记账：整卷提交看的是「有没有被确认过完成」，
            # 而不是「这一次是不是刚问出来的」。
            self._completion_confirmed = self._completion_confirmed or cached.completed
            return cached.completed
        providers = self._vision_providers()
        if not providers:
            self._emit(
                Event.ADVANCE_COMPLETION_CHECK,
                {
                    "run_id": self.ctx.run_id,
                    "trigger": trigger,
                    "completed": False,
                    "reason": "没有可用的视觉模型，无法确认是否全部完成",
                },
            )
            return False
        try:
            frame = await self._vision_frame()
        except Exception as exc:
            logger.warning("收尾确认：截图失败 %s", exc)
            frame = None
        if frame is None:
            self._emit(
                Event.ADVANCE_COMPLETION_CHECK,
                {
                    "run_id": self.ctx.run_id,
                    "trigger": trigger,
                    "completed": False,
                    "reason": "收尾确认截图失败",
                },
            )
            return False
        png, size = frame
        # 解析失败时的原始回复同样**必须落盘**：收尾确认说「没做完」而人看不出为什么，
        # 第一手证据就是模型到底回了什么（与读题那条路一致，2026-09-30）。
        raw_bucket: list[str] = []
        batch, error = await read_questions(
            png, providers=providers, raw_sink=raw_bucket.append
        )
        page_view = batch.page if batch is not None else None
        if batch is None or page_view is None:
            if raw_bucket and self.deps.run_logger is not None:
                with suppress(Exception):
                    directory = run_dir(self.deps.run_logger.root, self.deps.run_logger.run_id)
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / "vision_confirm_raw.txt").write_text(
                        raw_bucket[-1], encoding="utf-8"
                    )
            self._emit(
                Event.ADVANCE_COMPLETION_CHECK,
                {
                    "run_id": self.ctx.run_id,
                    "trigger": trigger,
                    "completed": False,
                    "reason": f"确认请求失败：{error or 'no_page_observation'}",
                },
            )
            return False
        if page_view.submit is not None and page_view.submit.box is not None:
            self._end_submit = (page_view.submit.box, size)
        check = CompletionCheck(
            completed=all_done(page_view),
            reason=page_view.reason or batch.note or "",
            raw=(batch.raw or "")[:500],
        )
        self._completion_checks[key] = check
        if check.completed:
            self._completion_confirmed = True
        self._emit(
            Event.ADVANCE_COMPLETION_CHECK,
            {
                "run_id": self.ctx.run_id,
                "trigger": trigger,
                "completed": check.completed,
                "observed": page_view.completed.value,
                "progress": page_view.progress,
                "reason": check.reason,
            },
        )
        return check.completed

    async def _screenshot(self, page: Any) -> bytes | None:
        """截图。失败返回 ``None`` —— 调用方按「没确认」处理，不抛异常。"""
        try:
            return await page.screenshot()
        except Exception as exc:
            logger.debug("推进判定截图失败：%s", exc)
            return None

    async def _settle_end(self, page: Any) -> bool:
        """方案里那一种推进方式推不动了：让视觉组确认一次，再决定收工还是停下。

        - 确认「已全部完成」→ 返回 ``False``：主循环**干净收工**（``finished``）；
        - 否则 → 按 ``advance_failed`` 停下（**绝不静默跳过**），并给出可操作的提示。

        为什么**不换招再试**：换招是「一次跳过十几道题」的成因（滚动模式下先点按钮、
        点不着再继续滚）。而且换招之后做过的题与跳过的题混在一起，
        事后从日志里根本分不清漏了哪几道。推不动就停下，人一眼就能看出停在第几题。

        这里**不再有「问人是不是最后一题」这条路** —— 用户实测判定那套逻辑有严重
        问题，已整套推倒；现在判定权在视觉组的观测 + 程序的固定裁决上，
        判不出来就停下等人**手动处理页面**（而不是回答一个是非题）。
        """
        if await self._confirm_completion(page, "stuck"):
            self._emit(
                Event.LOG_LINE,
                {"advance": "completed", "reason": "视觉组观测到整卷已完成"},
            )
            return False
        method = self._plan.method.value if self._plan is not None else "no_plan"
        self._emit(
            Event.LOG_LINE,
            {
                "advance": "failed",
                "reason": f"方案里的推进方式（{method}）推不动，且视觉组未能确认「已全部完成」",
                "hint": (
                    "可试：① 手动点一次「下一题 / 题号」再恢复；"
                    "② 确认那个控件/答题卡在画面上（滚进视野）后重跑；"
                    "③ 若这确实是最后一题，确认视觉组模型能看清画面"
                ),
            },
        )
        self._pause_for(ErrorCode.ADVANCE_FAILED.value)
        return False

    async def _click_box(
        self,
        page: Any,
        actuator: Any,
        box: tuple[float, float, float, float],
        size: tuple[int, int],
        *,
        source: str,
        before: str | None,
    ) -> bool:
        """按归一化框点一下，并用页面指纹判断「到底有没有翻页」。

        坐标 → 视口像素的换算在**执行层**（``Actuator.click(box, size)``）里做，
        编排层只管把模型给的框原样递过去 —— 换算点只有一处，不会两处漂移。

        ``before`` 为 ``None``（指纹取不到）时**只点一次就交卷** —— 交给主循环的
        ``question_did_not_advance`` 护栏去判。不回读的情况下连点两次，
        一旦第一次其实成功了，就会一次跳过两道题（静默跳题）。
        """
        result = await actuator.click(
            box, size, kind=ActionKind.CLICK, target_label=f"next:{source}"
        )
        self._record_level_stat(self._current, result)
        if not result.ok:
            # 这一下没发出去（页面正在导航 / 帧被销毁）：交给调用方按「推不动」处理，
            # **不在这里换别的坐标再点** —— 换招正是「一次跳过好几道题」的成因。
            return False
        if before is None:
            return True
        return await self._page_changed(page, before)

    async def _advance_by_swipe(
        self, page: Any, actuator: Any, before: str | None
    ) -> bool:
        """滑动手势翻页。**一次只滑一下，滑出变化就停。**

        方向按 ``guards.advance_swipe_directions`` 逐个试（默认先左后上）——
        只有**证明上一下没生效**才试下一个方向：连滑是「一次跳过好几道题」的成因，
        而静默跳题是本项目的硬禁区。
        """
        directions = tuple(getattr(self.ctx.cfg.guards, "advance_swipe_directions", ()) or ())
        # **视觉组选中的滑动技能优先**：它看得见「这一屏的题占多高、下一题在哪个方向」，
        # 所以由它给方向与幅度；配置里的方向序只是它没给观测时的兜底。
        # 滑多了就是静默跳题（2026-09-28 题号 3 → 16），所以幅度必须由看见画面的人回答。
        vision_swipe = self._vision_swipe()
        ratio: float | None = None
        if vision_swipe is not None:
            directions = (vision_swipe.direction,)
            ratio = _clamp_swipe_ratio(vision_swipe.amplitude)
            self._emit(
                Event.LOG_LINE,
                {
                    "advance_swipe": "planned",
                    "direction": vision_swipe.direction,
                    "amplitude": round(ratio, 3),
                    "hint": "按视觉组观测的幅度滑动（不是配置里的固定距离）",
                },
            )
        if not directions:
            self._emit(Event.LOG_LINE, {"advance": "swipe_disabled"})
            return False
        swipe = getattr(actuator, "swipe", None)
        if swipe is None:
            self._emit(Event.LOG_LINE, {"advance": "swipe_unsupported"})
            return False
        for direction in directions:
            result = await swipe(direction, distance_ratio=ratio)
            self._record_level_stat(self._current, result)
            if not result.ok:
                # 手势机制本身不可用（视口读不到 / CDP 会话建不起来）——
                # 换方向也还是同一套机制，再试没意义。
                self._emit(
                    Event.LOG_LINE,
                    {"advance_swipe": direction, "ok": False, "error": result.error},
                )
                return False
            if before is None:
                # 指纹取不到 → 判不了「翻没翻」，只滑这一次
                return True
            if await self._page_changed(page, before):
                self._emit(Event.LOG_LINE, {"advance_swipe": direction, "ok": True})
                return True
            self._emit(
                Event.LOG_LINE,
                {"advance_swipe": direction, "ok": False, "note": "no_change"},
            )
        return False

    def _vision_swipe(self) -> PageSwipe | None:
        """本次读图里视觉组给出的滑动观测（``advance_swipe`` 技能的落点信息）。

        只在**这一屏真的选了滑动技能**时采信：模型报了一个幅度却把
        ``advance_skill_id`` 填成别的（或没填）时，说明它自己也没把握 ——
        那时退回配置里的方向序更安全，绝不拿一个来路不明的幅度去滑。
        """
        view = self._fresh_page_view()
        if view is None or view.swipe is None:
            return None
        if view.advance_skill_id != AdvanceSkill.SWIPE.value:
            logger.debug(
                "滑动观测被忽略：advance_skill_id=%r 不是 %s",
                view.advance_skill_id,
                AdvanceSkill.SWIPE.value,
            )
            return None
        return view.swipe

    async def _page_changed(self, page: Any, before: str) -> bool:
        """等页面指纹变成别的（翻页有过渡动画，读早了会误判成没动）。"""
        guards = self.ctx.cfg.guards
        timeout_s = max(0, int(getattr(guards, "advance_change_timeout_ms", 1500))) / 1000.0
        poll_s = max(0.01, int(getattr(guards, "advance_change_poll_ms", 150)) / 1000.0)
        deadline = time.monotonic() + timeout_s
        while True:
            await asyncio.sleep(poll_s)
            current = await self._page_signature(page)
            if current is not None and current != before:
                return True
            if time.monotonic() >= deadline:
                return False

    async def _page_signature(self, page: Any) -> str | None:
        """页面指纹：``URL + 整页可见文本的哈希``。

        用来回答一个问题：**「下一题」到底有没有生效**。
        取不到**可判定**的内容（例如题干画在 canvas 上、内文为空）时返回 ``None``，
        调用方据此退化为「只做一次动作」—— 绝不拿「没变」下结论，
        那会让我们再滑一次、一次滑掉两道题，而**跳题是静默的**。

        v0.2.0 起不再按题目锚点取「题目区」的局部文本：锚点是站点文档结构的约定，
        程序已经不解析它了。整页文本对「翻没翻页」同样有效，而且不依赖任何站点配置。
        """
        script = """() => {
            const root = document.body;
            const text = (root && root.innerText) || '';
            return [String(location.href), text.replace(/\\s+/g, ' ').trim().slice(0, 4000)];
        }"""
        try:
            value = await page.evaluate(script)
            url, text = str(value[0] or ""), str(value[1] or "")
        except Exception as exc:
            logger.debug("页面指纹读取失败：%s", exc)
            return None
        if len(text) < _MIN_SIGNATURE_CHARS:
            return None
        return f"{url}\x00{stem_fingerprint(text)}"

    # ------------------------------------------------------------------ 单条任务

    async def _step_quiz(
        self,
        item: TaskItem,
        *,
        perception: PerceptionResult | None = None,
    ) -> None:
        """推进一条题目任务：感知 → 求解 → 决策 → 执行 → 提交 → 校验。

        ``perception`` 由主循环透传（页面已经读过一次，不必再读）；单独调用
        （不给 ``perception``）时会自己读一次，便于定向排查。
        """
        page = self._page
        if page is None:
            raise RuntimeError("没有可用页面，无法执行题目任务")

        state = self._state_of(item)

        # —— 危险态：只回读结果，绝不重新点击（M4 红线）
        #
        # v0.2.0 的「回读」只能靠截图差分，而差分要一张**点之前**的图当基准 ——
        # 它活在提交那个进程的内存里。所以续跑遇到 ``submitted`` 时判不了结果，
        # 如实保持 ``submitted``（结果未知）并继续前进，绝不伪造成 ``verified``。
        if state is QuestionState.SUBMITTED:
            await self._confirm_submit(item)
            return

        # —— 停在等人确认：先等决策，再决定要不要继续
        if state is QuestionState.PENDING_CONFIRM:
            if not await self._await_human(item):
                return
            state = self._state_of(item)
            if state is QuestionState.SKIPPED:
                return

        # —— 断点恰好落在「选项已选好、还没提交」：补提交即可（**不重新点选项**）
        if state is QuestionState.APPLIED and self._options_clicked(item):
            if self._submit_scope_of_run() is SubmitScope.PAPER:
                # **整卷不需要补提交**：选项早已落在页面上，而「交卷」是
                # 全部题做完之后的收尾动作。在这里补一次 = 提前交卷。
                self._last_applied_item_id = item.item_id
                return
            await self._submit_and_confirm(item)
            return

        perception = perception or await self._perceive(page, item.item_id)
        question = perception.question
        if question is None:
            self._ensure_state(item, QuestionState.FAILED)
            self._pause_for("perception_failed")
            return

        self._write_perception(item, perception)
        # 几何与提交范围一并落盘（``perception.json`` 里没有它们）。
        self._write_vision_read(item)
        await self._capture(page, item, "before")
        self._ensure_state(item, QuestionState.PERCEIVED)

        answer = await self._solve(item, question, perception)
        if answer is None:
            self._ensure_state(item, QuestionState.FAILED)
            self._pause_for("solve_failed")
            return
        self._ensure_state(item, QuestionState.SOLVED)

        # 半自动：每题都等人点头；⚠复核：**无论哪种模式都必停**（T0-3）
        if answer.review_flag or not self._auto_apply():
            self._ensure_state(item, QuestionState.PENDING_CONFIRM)
            self._emit(
                Event.TASK_NEEDS_CONFIRM,
                {
                    "item_id": item.item_id,
                    "qid": question.qid,
                    "chosen_labels": answer.chosen_labels,
                    "confidence": answer.confidence,
                    "review_flag": answer.review_flag,
                    "reason": "review_flag" if answer.review_flag else "manual_mode",
                },
            )
            if not await self._await_human(item):
                return

        if not answer.chosen_labels:
            # 空作答**绝不能走到提交**：人确认了「继续」，但我们手上没有任何
            # 可选的选项（例如 Provider 全部失败）。提交一个空答案比停下来糟得多。
            self._ensure_state(item, QuestionState.FAILED)
            self._pause_for("empty_answer")
            return

        await self._apply(item, question, answer)
        if QuestionState(item.state) is not QuestionState.APPLIED:
            return
        # 记下「最后一道真的点过选项的题」：整卷提交要用它的 ``submit_box``。
        # 只有 APPLIED 了才记 —— 点在空白上没选中的题，不配当收尾提交的凭据。
        self._last_applied_item_id = item.item_id
        if self._submit_scope_of_run() is SubmitScope.PAPER:
            # **整卷：提交不属于「这一题」。**
            # 这个按钮是「交卷」，点下去就结束整场 —— 在这里点，等于做完第 1 题
            # 就交卷（2026-09-28 真机故障）。留痕说明它被推到了收尾。
            self._emit(
                Event.LOG_LINE,
                {
                    "submit": "deferred",
                    "scope": SubmitScope.PAPER.value,
                    "item_id": item.item_id,
                    "hint": "整卷交卷：推迟到全部题目做完的收尾阶段",
                },
            )
            return
        await self._submit_and_confirm(item)

    # ------------------------------------------------------------------ 网课场景（M5）

    async def _drain_video(self, page: Any) -> None:
        """网课主循环：按分集目录逐集播完，弹题以**嵌套中断**处理（M5-1 / M5-5）。

        与刷题循环一样是「只前进、不回头」：已 ``ended`` 的分集直接跳过
        （**不重播已完成集**），未完成的从落盘位置续上。
        """
        adapter = self.deps.adapter
        from perception.media_probe import read_episode_catalog

        if adapter is None:
            self._pause_for("no_actuator")
            return
        catalog = await read_episode_catalog(page, adapter)
        if not catalog:
            self._pause_for("media_unavailable")
            return

        self._episodes = {episode.vid: episode for episode in catalog}
        sequence = build_task_sequence(self.ctx.cfg, run_id=self.ctx.run_id, episodes=catalog)
        self.items = self._merge_video_sequence(self.items, sequence)

        # 续跑：栈里还留着「弹题处理到一半」的上下文 → 先按栈顶把它重建出来
        if not self.stack.is_empty():
            self._resume_suspended()

        for item in sequence:
            await self._gate.wait()
            if self._stopped:
                break
            if self._media_state_of(item) is MediaState.ENDED:
                # 续跑：这一集上次已经播完，只前进不重播
                self._emit(
                    Event.TASK_UPDATED,
                    {
                        "item_id": item.item_id,
                        "state": str(item.state),
                        "resumed": True,
                        "redone": False,
                    },
                )
                continue

            item.attempts += 1
            self._persist_item(item)
            self._current = item
            if not await self._goto_episode(page, adapter, item, self._episode_index_of(item)):
                break
            await self._step_video(item)
            if self._stopped or self.paused:
                break

        self._current = None

    async def _step_video(self, item: TaskItem) -> None:
        """推进一集：播放 → 回读 ``paused`` → 进度断言 → 观看 → 记 ``ended``。

        四条纪律（对应任务书 M5-3 / M5-4 / M5-2）：

        1. 播放后**回读** ``paused === false``，不信「点了就算播了」；
        2. 播放态推进断言（3s 内 ``ΔcurrentTime ≥ 1.0s``）不过就暂停留档；
        3. 观看期间弹题与 ``ended`` 两条路同时看着，**弹题优先被检出**；
        4. 断点续跑时先 ``seek`` 回落盘位置（**paused 态重建**），再播。
        """
        page = self._page
        adapter = self.deps.adapter
        actuator = self._actuator_for(item)
        if page is None or adapter is None or actuator is None:
            self._pause_for("no_actuator")
            return

        state = await self._read_media(page, adapter)
        if state is None:
            self._pause_for("media_unavailable")
            return
        self._write_video_perception(item, state)
        await self._capture(page, item, "before")

        # 断点续跑：把落盘位置带回来（M5-5）。先 seek 成 paused 态，**不自动续播**。
        saved = self._saved_position(item)
        if saved is not None and saved > _POSITION_EPS_S:
            if state.duration > 0 and saved >= state.duration - _POSITION_EPS_S:
                self._finish_episode(item, state)  # 已到片尾，等价于播完
                return
            seek = await actuator.seek_media(adapter, saved)
            self._record_action(item, seek)
            if not seek.ok:
                self._set_media_state(item, MediaState.PAUSED)
                self._pause_for("media_failed")
                return
            self._set_media_state(item, MediaState.PAUSED)

        self._set_media_state(item, MediaState.PLAYING)
        result = await actuator.play_media(adapter)
        self._record_action(item, result)
        if not result.ok:
            self._set_media_state(item, MediaState.PAUSED)
            self._pause_for("media_failed")
            return

        verifier = self._verifier_for(item)
        if verifier is None:
            self._pause_for("no_verifier")
            return
        playing = await verifier.verify_media_flag(adapter, paused=False)
        self._write_verify(item, playing)
        if not playing.ok:
            self._set_media_state(item, MediaState.PAUSED)
            self._pause_for("media_failed")
            return

        progress = await verifier.verify_playing(page, adapter)
        self._write_verify(item, progress)
        if not progress.ok:
            self._set_media_state(item, MediaState.PAUSED)
            self._pause_for("media_stalled")
            return

        await self._watch_episode(page, adapter, item)

    async def _watch_episode(self, page: Any, adapter: Any, item: TaskItem) -> None:
        """看完一集：反复等「弹题 / 播完」，弹题处理完就继续等。"""
        from perception.media_probe import EPISODE_OUTCOME_ENDED, wait_for_episode_outcome

        poll_s = adapter.interrupt_detection.poll_interval_ms / 1000.0
        while not self._stopped:
            budget = self.deps.media_watch_timeout_s
            if budget is None:
                budget = await self._watch_budget(page, adapter)
            outcome = await wait_for_episode_outcome(page, adapter, budget, poll_s=poll_s)
            if outcome is None:
                # 既没播完也没弹题：不静默续等，停下来让人看一眼
                self._set_media_state(item, MediaState.PAUSED)
                self._pause_for("media_timeout")
                return
            if outcome == EPISODE_OUTCOME_ENDED:
                self._finish_episode(item, await self._read_media(page, adapter))
                return

            frame = await self._on_interrupt(item)
            if self._stopped or self.paused:
                return
            state = await self._read_media(page, adapter)
            if state is not None and self._episode_finished(adapter, state):
                # `?interrupt_at=end`：弹题与 ended 同刻 —— 弹题处理完，该集视为已完成，
                # **不恢复播放**，直接推进下一集（M5-2 的优先级规定）
                self._finish_episode(item, state)
                return
            if not await self._resume_after_interrupt(page, adapter, item, frame):
                return

    async def _on_interrupt(self, item: TaskItem) -> SuspendFrame | None:
        """M5-2 压栈：弹题到来 → **显式暂停媒体** → 压栈 → 处理弹题 → 弹栈。

        弹题弹窗**不会**暂停视频（这是靶场与真实站点共同的约定），所以「暂停」
        必须由系统自己做 —— 少了这一步，挂起期间 ``currentTime`` 会继续往前走，
        「恢复位置连续（偏差 ≤2s）」的断言必然失败。

        返回本次压入（或续跑时复用）的挂起帧，供调用方做恢复位置校验。
        """
        page = self._page
        adapter = self.deps.adapter
        actuator = self._actuator_for(item)
        self._emit(Event.MEDIA_INTERRUPT_DETECTED, {"item_id": item.item_id, "vid": item.vid})

        # 1) 显式暂停（幂等：已经暂停就不动手，见执行层「已达成期望状态就不动手」）
        if actuator is not None and adapter is not None:
            paused = await actuator.pause_media(adapter)
            self._record_action(item, paused)
            if not paused.ok:
                self._set_media_state(item, MediaState.PAUSED)
                self._pause_for("media_pause_failed")
                return None
        self._set_media_state(item, MediaState.INTERRUPTED)

        # 2) 挂起时的媒体态 → 落盘（跨进程续跑要靠它把视频以 paused 重建）
        suspend_state = await self._read_media(page, adapter)
        if suspend_state is None:
            self._pause_for("media_unavailable")
            return None
        self._save_position(item, suspend_state.current_time)

        # 3) 感知弹题 → 按 qid 认领子任务 → 压栈
        #
        # **弹题一出现，之前那一屏的同屏几何就全部作废**（画面被盖住了）。
        # 不清的话，父任务那一屏留下的队列会在弹窗还盖着的时候被取用 ——
        # 那是拿旧坐标往一层遮罩上点，正是「胡乱操作」的一种形态。
        self._pending_reads.clear()
        perception = await self._perceive(page, item.item_id)
        question = perception.question
        if question is None:
            # v0.2.0：流水线**只出图**，所以弹题也要交给模型读 ——
            # 与主循环走的是同一条路（``_read_question_by_vision``）。
            # 少了这一步，弹题在真实站点上永远读不出来，每次中断都停在这里，
            # 整条网课链路等于作废。
            question = await self._read_question_by_vision()
            if question is not None:
                # 与主循环同一条纪律：读题结果必须**并入** perception，
                # 否则下游（``_step_quiz`` / 留痕）看到的仍是 ``question=None``。
                perception = perception.model_copy(update={"question": question})
        if question is None:
            self._pause_for(self._vision_gate_reason or "interrupt_perception_failed")
            return None
        # 弹题这一屏**只取这一道**：同屏其余题属于「弹窗还开着」的那个画面
        # （遮盖底下那一屏的题，或者同一份回复里的其余条目）—— 留着它们，
        # 就会在弹窗关掉之后被拿去点。丢弃是安全方向：真要作答，
        # 等遮罩消失后重新读一屏即可（多花一次调用，换掉一次误点）。
        self._pending_reads.clear()
        child = self._claim_item(question, item.item_id)
        child.attempts += 1
        self._persist_item(child)
        if is_terminal(self._state_of(child)):
            # 这一题**已经了结**（续跑时重新出现的弹窗）：不重做。
            # 也不能当没看见 —— 真机上此时弹窗仍盖着播放按钮，继续恢复播放会
            # 一路降级到超时。正确处置是停下来让人看一眼（「禁止静默跳过」是硬约束）。
            self._pause_for("interrupt_already_answered")
            return None

        top = self.stack.peek()
        if top is not None and top.parent_item_id == item.item_id:
            # 续跑：栈顶正是重启前留下的同一段上下文 —— 复用（先弹再压），
            # 不叠出两层指向同一父任务的帧，否则栈会越跑越深、弹栈也弹不干净
            self.stack.pop()
        frame = SuspendFrame(
            parent_item_id=item.item_id,
            child_item_id=child.item_id,
            media_state_at_suspend=suspend_state,
            reason="quiz_interrupt",
        )
        self.stack.push(frame)
        item.suspended = True
        self._persist_item(item)
        self._emit(
            Event.STACK_PUSHED,
            {
                "run_id": self.ctx.run_id,
                "frame_id": frame.frame_id,
                "parent_item_id": item.item_id,
                "child_item_id": child.item_id,
                "depth": self.stack.depth,
            },
        )

        # 4) 处理弹题：复用题目主循环的单条路径 —— 半自动确认、⚠复核必停、
        #    `submitted` 只回读不重提交，全都照旧生效
        if not is_terminal(QuestionState(child.state)):
            await self._step_quiz(child, perception=perception)
        if self._stopped or self.paused:
            return frame

        # 5) 弹栈：嵌套中断结束
        popped = self.stack.pop() or frame
        item.suspended = False
        self._persist_item(item)
        self._emit(
            Event.STACK_POPPED,
            {
                "run_id": self.ctx.run_id,
                "frame_id": popped.frame_id,
                "parent_item_id": item.item_id,
                "depth": self.stack.depth,
            },
        )
        return popped

    async def _resume_after_interrupt(
        self,
        page: Any,
        adapter: Any,
        item: TaskItem,
        frame: SuspendFrame | None,
    ) -> bool:
        """弹题处理完，恢复播放。返回 ``False`` 表示已暂停 / 停止。"""
        actuator = self._actuator_for(item)
        if actuator is None:
            self._pause_for("no_actuator")
            return False

        if frame is not None:
            verifier = self._verifier_for(item)
            if verifier is None:
                self._pause_for("no_verifier")
                return False
            drift = await verifier.verify_resume_continuous(
                page, adapter, frame.media_state_at_suspend.current_time
            )
            self._write_verify(item, drift)
            if not drift.ok:
                # 位置越界（比如挂起期间没真的暂停、视频自己跑了）：不硬续播
                self._set_media_state(item, MediaState.PAUSED)
                self._pause_for("resume_drift")
                return False

        self._set_media_state(item, MediaState.RESUMED)
        result = await actuator.play_media(adapter)
        self._record_action(item, result)
        if not result.ok:
            self._set_media_state(item, MediaState.PAUSED)
            self._pause_for("media_failed")
            return False
        self._set_media_state(item, MediaState.PLAYING)
        return True

    def _resume_suspended(self) -> None:
        """重启后续跑：把挂起帧里的视频**以 paused 态重建**、位置回填（M5-2）。

        **不自动续播** —— 只把状态摆正（PAUSED + 落盘位置），真正的播放交给
        正常的观看流程（它会重新触发弹题，子任务按 qid 复用库里那条）。
        """
        self._prune_stale_frames()
        for frame in self.stack.snapshot():
            parent = self._find_item(frame.parent_item_id)
            if parent is None:
                continue
            self._save_position(parent, frame.media_state_at_suspend.current_time)
            self._set_media_state(parent, MediaState.PAUSED)
            parent.suspended = True
            self._persist_item(parent)
        logger.info(
            "%s：恢复了 %d 层挂起栈（栈顶优先）",
            ErrorCode.STACK_RESTORED.value,
            self.stack.depth,
        )

    def _prune_stale_frames(self) -> None:
        """丢弃**已经没意义**的挂起帧。

        帧只在「弹题处理到一半时进程退出」时才有价值。父分集已经播完、
        或子任务已经了结，都说明这段上下文早就结束了 —— 留着它会让栈深虚高
        （真机实测出现过 ``stack.pushed depth=2`` 这种不该有的深度），
        还会让「视频以 paused 重建」把一段早已结束的位置当成断点回填。
        """
        while not self.stack.is_empty():
            frame = self.stack.peek()
            if frame is None:  # pragma: no cover - is_empty 已排除
                return
            parent = self._find_item(frame.parent_item_id)
            child = self._find_item(frame.child_item_id)
            if parent is not None and child is not None and self._frame_is_live(parent, child):
                return
            dropped = self.stack.pop()
            logger.info(
                "丢弃过期的挂起帧 %s（父分集已结束 / 子任务已了结 / 条目已不在）",
                dropped.frame_id if dropped is not None else "?",
            )

    def _frame_is_live(self, parent: TaskItem, child: TaskItem) -> bool:
        """帧还有没有意义：父分集没播完，且子任务没了结。"""
        if self._media_state_of(parent) is MediaState.ENDED:
            return False
        try:
            return not is_terminal(QuestionState(child.state))
        except ValueError:  # 子条目不是题目态（数据异常）—— 留着更危险
            return False

    async def _goto_episode(
        self, page: Any, adapter: Any, item: TaskItem, target: int
    ) -> bool:
        """把页面切到第 ``target`` 集。**只点「下一集」，绝不回头重播**。

        续跑时页面总是从第 1 集加载，所以这里靠连点快进到目标集；
        每次点击都由 :meth:`Actuator.next_episode` 回读「索引严格 +1」。
        """
        if target <= 0:
            return True
        from perception.media_probe import read_episode_index

        actuator = self._actuator_for(item)
        if actuator is None:
            self._pause_for("no_actuator")
            return False

        try:
            current, _total = await read_episode_index(page, adapter)
        except Exception as exc:  # 页面正在换源
            logger.debug("分集索引读取失败：%s", exc)
            current = 0
        if current <= 0:
            # 刚 load 完、body 属性还没写上的那一帧：就当在第 1 集
            current = 1

        guard = 0
        while current < target:
            guard += 1
            if guard > self.deps.episode_advance_max:
                self._pause_for("episode_advance_failed")
                return False
            result = await actuator.next_episode(adapter)
            self._record_action(item, result)
            if not result.ok:
                self._pause_for("episode_advance_failed")
                return False
            await self._click_gap()
            current += 1
        return True

    def _finish_episode(self, item: TaskItem, state: Any) -> None:
        """一集播完：位置落盘 + 记 ``ended``。

        位置记**片尾**（``duration``）而不是停下来的那一帧 ——
        ``media_position`` 是按 ``(vid, 集数)`` 存的**课程级进度**（表结构 P0 冻结，
        没有 run_id 列），下次开播靠它判断「这一集看完了没」。
        记成中间值会让「已完成」这个事实下次读不出来。
        """
        if state is not None:
            tail = state.duration if state.duration > 0 else state.current_time
            self._save_position(item, tail)
        self._set_media_state(item, MediaState.ENDED)
        self._emit(
            Event.TASK_UPDATED,
            {"item_id": item.item_id, "state": str(item.state), "ended": True},
        )

    @staticmethod
    def _episode_finished(adapter: Any, state: Any) -> bool:
        """这一集算不算播完（``ended`` 或 ``currentTime`` 已经贴着片尾）。"""
        if state.ended:
            return True
        epsilon = adapter.media_assertions.ended_epsilon_s
        return state.duration > 0 and state.current_time >= state.duration - epsilon

    async def _watch_budget(self, page: Any, adapter: Any) -> float:
        """观看预算：已知时长就「时长 + 余量」，未知就按余量兜底。"""
        state = await self._read_media(page, adapter)
        duration = state.duration if state is not None else 0.0
        return max(duration, 1.0) + _MEDIA_WATCH_MARGIN_S

    async def _read_media(self, page: Any, adapter: Any) -> Any | None:
        """读一帧媒体态；读不到返回 ``None``（**不抛**）。"""
        from perception.base import MediaNotAvailableError
        from perception.media_probe import read_video_state

        if page is None or adapter is None:
            return None
        try:
            return await read_video_state(page, adapter)
        except MediaNotAvailableError:
            return None
        except Exception as exc:  # 页面正在 seek / 换源，属性会短暂缺失
            logger.debug("媒体态读取失败：%s", exc)
            return None

    def _write_video_perception(self, item: TaskItem, state: Any) -> None:
        """分集任务也要写 ``perception.json`` —— 界面正是靠它显示「第几集」。"""
        if self.deps.run_logger is None or state is None:
            return
        self.deps.run_logger.save_json(
            item.item_id,
            "perception",
            {
                "item_id": item.item_id,
                "vid": item.vid,
                "channel_used": ProbeName.MEDIA.value,
                "video_state": state.model_dump(mode="json"),
                "warnings": ["media:attributes_only"],
            },
        )

    def _set_media_state(self, item: TaskItem, target: MediaState) -> None:
        """推进媒体态。媒体态没有转移表（六态不是状态机），故只记不留痕地切。

        同态调用只落库、**不发事件** —— 「重建为 paused → seek 后仍是 paused」
        这类重复上报会让界面显示 ``paused → paused``，看着像出错了。
        """
        previous = item.state
        item.state = target
        item.updated_at = datetime.now(UTC)
        self._persist_item(item)
        if str(previous) == str(target):
            return
        self._emit(
            Event.MEDIA_STATE_CHANGED,
            {
                "item_id": item.item_id,
                "vid": item.vid,
                "from": str(previous),
                "to": str(target),
            },
        )

    def _media_state_of(self, item: TaskItem) -> MediaState:
        """以**库里的状态**为准（人工/续跑都可能改过），并同步回内存对象。"""
        state = self._read_media_state(item.item_id)
        if state is not None:
            item.state = state
        try:
            return MediaState(item.state)
        except ValueError:
            return MediaState.IDLE

    def _read_media_state(self, item_id: str) -> MediaState | None:
        conn: sqlite3.Connection | None = self.deps.conn
        if conn is None:
            return None
        row = conn.execute("SELECT state FROM task_item WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        try:
            return MediaState(row["state"])
        except ValueError:
            return None

    def _find_item(self, item_id: str) -> TaskItem | None:
        for item in self.items:
            if item.item_id == item_id:
                return item
        return None

    def _episode_index_of(self, item: TaskItem) -> int:
        episode = self._episodes.get(item.vid or "")
        return int(getattr(episode, "episode_index", 0) or 0)

    def _save_position(self, item: TaskItem, position: float) -> None:
        conn: sqlite3.Connection | None = self.deps.conn
        if conn is None or not item.vid:
            return
        with suppress(Exception):
            db.save_media_position(
                conn,
                vid=item.vid,
                episode_index=self._episode_index_of(item),
                last_position=float(position),
            )

    def _saved_position(self, item: TaskItem) -> float | None:
        conn: sqlite3.Connection | None = self.deps.conn
        if conn is None or not item.vid:
            return None
        try:
            return db.load_media_position(conn, item.vid, self._episode_index_of(item))
        except Exception as exc:  # pragma: no cover - 库损坏
            logger.warning("读取媒体断点失败（vid=%s）：%s", item.vid, exc)
            return None

    @staticmethod
    def _merge_video_sequence(restored: list[TaskItem], sequence: list[TaskItem]) -> list[TaskItem]:
        """把「恢复出来的条目」与「按目录新排的序列」合并。

        视频条目按 ``vid`` 认领（续跑保留上次的状态），已恢复的其它条目
        （比如弹题子任务）一律保留，否则弹题的身份会在重启后丢失。
        """
        by_vid = {item.vid: item for item in restored if item.vid}
        merged: list[TaskItem] = []
        seen: set[str] = set()
        for item in sequence:
            chosen = by_vid.get(item.vid) if item.vid else None
            chosen = chosen or item
            merged.append(chosen)
            seen.add(chosen.item_id)
        for item in restored:
            if item.item_id not in seen:
                merged.append(item)
        return merged

    async def _perceive(self, page: Any, position: int | str) -> PerceptionResult:
        from perception.pipeline import PerceptionContext

        ctx = PerceptionContext(
            item_id=str(position),
            run_id=self.ctx.run_id,
            cfg=self.ctx.cfg,
            log_root=self._log_root(),
            timeout_s=self.deps.probe_timeout_s,
        )
        pipeline = self.deps.pipeline
        adapter = self.deps.adapter
        if pipeline is None or adapter is None:
            raise RuntimeError("编排层缺少感知依赖（pipeline / adapter）")
        result = await pipeline.run(page, adapter, ctx)
        self._emit(
            Event.PERCEPTION_DONE,
            {
                "run_id": self.ctx.run_id,
                "channel": str(result.channel_used),
            },
        )
        return result

    async def _solve(
        self,
        item: TaskItem,
        question: Question,
        perception: PerceptionResult,
    ) -> Answer | None:
        solver = self.deps.solver
        if solver is None:
            return None
        del perception  # 判题只吃题面，不再引用感知结果
        # 判题组**只吃文字**：视觉组已经把题干/选项抄成文本（含 LaTeX），
        # 判题组不需要再看原图。之前这里会现截整屏再发一次 —— 推理模型会把
        # max_tokens 全烧在「看图的思维链」上、把最终 JSON 的 content 留空，
        # 于是判题恒为空（``empty_answer`` 暂停），这就是「原来能跑、现在不能跑」的根因。
        answer = await solver.solve(
            question,
            truncated=any("truncat" in marker for marker in question.channel_trace),
        )
        self._write_solve(item, answer)
        if self.deps.conn is not None:
            with suppress(Exception):
                db.save_answer(self.deps.conn, answer)
        return answer

    async def _vision_frame(self) -> tuple[bytes, tuple[int, int]] | None:
        """现截一张**视口**图，返回 ``(png, (宽, 高))``。取不到返回 ``None``。

        尺寸必须由**图本身**给出，不能用 ``page.viewport_size`` ——
        通过 CDP 附加来的页面这个属性常常是 ``None``（实测），而坐标换算全靠它。

        v0.2.0 里视觉探针的 ``crop_question(page)`` 只返回 PNG 字节
        （不再有「锚点裁切 / 整屏」两种模式）：题目侧只有这一种画面，
        坐标原点天然与 ``mouse.click`` 的视口坐标同源。
        """
        pipeline = self.deps.pipeline
        if pipeline is None or self._page is None:
            return None
        probe = pipeline.probe(ProbeName.VISION)
        crop = getattr(probe, "crop_question", None)
        if crop is None:
            return None
        png = await crop(self._page)
        with Image.open(BytesIO(png)) as image:
            size = (image.width, image.height)
        return png, size

    async def _read_question_by_vision(self) -> Question | None:
        """让视觉模型**读图**，把题目读出来（不是答题）。**v0.2.0 起这是唯一的读题路。**

        契约在 ``solve.reader``：模型看一张整屏视口图，输出（同一份固定格式的）
        ``page`` 观测块 + 题干、选项文本与各元素的**归一化包围框**。
        读出来的题转成 ``Question`` 后走**完全相同的**求解 / 投票 / 缓存链路，
        几何另存在 :attr:`_vision_reads` 里供执行层按坐标点击；观测块留在
        :attr:`_last_batch` 里供开局裁决与收尾确认使用。

        ⚠️ **这里不再有「找不到提交按钮就滚到顶部重读一次」那条恢复路径**
        （2026-09-30 删）。它有两个后果，都是真机上实测到的：

        1. 滚回顶部会把刚刚滚到的位置整个毁掉 —— 读回来的是**更早的题**；
        2. 「这一屏没有提交按钮」在整卷页面上是**正常形态**，于是每一屏都触发一次
           回滚重读，日志里只剩 ``region_mad`` 与 ``no_submit_box``，没人看得出
           题目其实一直在原地打转。

        提交时机现在由开局裁决的方案决定（整卷页面根本不需要每屏都有提交按钮），
        所以这条恢复路径既没有用、又有害。

        为什么这条是唯一的路：真实站点没有靶场那套 ``data-quiz`` 锚点，
        解析页面结构的读法在它们身上必然落空 —— 而「看得懂页面」本来就只有模型能给。
        """
        # 每轮读题都先清掉上一次的门禁原因，免得陈旧的拦截理由泄漏到这一次。
        self._vision_gate_reason = None
        self._last_vision_error = None
        self._last_batch = None
        self._last_size = None
        solver = self.deps.solver
        providers = list(getattr(self.deps, "vision_providers", None) or [])
        if not providers:
            # 没单独选视觉模型 → 与判题共用一套（P4~P12 的既有行为）
            providers = list(getattr(solver, "providers", None) or []) if solver else []
        if not providers:
            # 一个可用的视觉模型都没有 —— 这是**配置问题**，也是这条链路唯一
            # 真正的死路：不读图就没有题面，没有题面就一道题都做不了。
            # 理由写具体（``vision_read_failed``）而不是让它退化成
            # ``perception_failed`` —— 后者会把人引去查「通道读不到」，
            # 而这里要做的事是「去设置里选一个视觉模型」。
            self._vision_gate_reason = "vision_read_failed"
            self._emit(
                Event.LOG_LINE,
                {
                    "vision_read": "skipped",
                    "reason": "no_provider",
                    "hint": "没有可用的视觉模型：本版本只能靠看图读题，请先在设置里选好模型",
                },
            )
            return None
        self._last_vision_error = None
        try:
            frame = await self._vision_frame()
        except Exception as exc:
            self._last_vision_error = "crop_failed"
            logger.warning("视觉读题：截图失败 %s", exc)
            self._vision_gate_reason = "vision_read_failed"
            self._emit(Event.LOG_LINE, {"vision_read": "failed", "error": "crop_failed"})
            return None
        if frame is None:
            self._last_vision_error = "no_frame"
            self._vision_gate_reason = "vision_read_failed"
            self._emit(Event.LOG_LINE, {"vision_read": "failed", "error": "no_frame"})
            return None
        png, size = frame
        # 解析失败时的原始响应**必须落盘**：抽出来的正文可能是空串（推理模型把
        # 答案放在 reasoning_content 等别的字段），那时「模型回了什么」只剩这份原文
        # 能回答。落成一个 run 级文件，不再重蹈「读题失败、无证据可查」的覆辙。
        raw_bucket: list[str] = []
        batch, error = await read_questions(png, providers=providers, raw_sink=raw_bucket.append)
        self._last_vision_error = error
        if batch is None:
            if raw_bucket and self.deps.run_logger is not None:
                with suppress(Exception):
                    directory = run_dir(self.deps.run_logger.root, self.deps.run_logger.run_id)
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / "vision_read_raw.txt").write_text(
                        raw_bucket[-1], encoding="utf-8"
                    )
            # **把"读题失败"与"没读到内容"区分开**。原来的兜底理由 ``perception_failed``
            # 会把排查方向指向"通道读不到"，而真实情况往往是"读到了、但模型回复用不了"
            # —— 两者的下一步动作完全不同（一个查页面，一个查模型回复）。
            #
            # 理由保持**单个码**、不带冒号：它就是 ``run.paused`` 的 ``reason``，
            # 会被界面直接显示、也可能被拿去查引导卡，拼接出来的复合串两处都不认。
            # 具体错误码放在事件里（``error`` 字段），日志与界面上都看得到。
            self._vision_gate_reason = "vision_read_failed"
            self._emit(
                Event.LOG_LINE,
                {
                    "vision_read": "failed",
                    "error": error or "unknown",
                    "hint": _vision_error_hint(error),
                },
            )
            return None
        # **观测先留下**：``page`` 块是页面级的事实，和「这一屏有没有读到题」无关。
        # 开局裁决（:meth:`_plan_run`）与收尾确认都要用它 —— 旧版把它记在
        # 「至少有一道题过门禁」之后，于是「只有进度/答题卡、没有完整题目」的一屏
        # 会把观测整份丢掉，表现为「读到了页面，却给不出运行方案」。
        self._last_batch = batch
        self._last_size = size
        self._note_last_submit(batch, size)
        if not batch.questions:
            # 一屏里一道完整题目都没有（整屏被切、或模型没看出题目）。
            # **这不等于「没有题了」** —— 那是推进与收尾该回答的问题，这里只如实报告。
            self._emit(
                Event.LOG_LINE,
                {"vision_read": "empty", "note": batch.note, "more_below": batch.more_below},
            )
            self._vision_gate_reason = self._vision_gate_reason or "vision_read_empty"
            return None
        # 逐题过门禁：**坏的那道不挡住好的那道**。
        # 被切掉半截的题按提示词本该不进 `questions`；进来却自认残缺
        # （``clipped`` / ``uncertain`` 非空）的，跳过它继续看下一道 ——
        # 一屏三题里坏了一道，没有理由让另外两道也跟着停下。全都不过才按原因暂停问人。
        accepted: list[tuple[Question, ReadResult]] = []
        blocked_reason: str | None = None
        for item in batch.questions:
            question = to_question(item)
            if question.skill_error is not None or question.skill_id is None:
                # 不支持/无匹配技能是明确的跳过结果，不是读题失败；进入任务队列，
                # 由主循环持久化为 SKIPPED 后继续推进，绝不调用解题组或执行器。
                accepted.append((question, item))
                continue
            if not self._vision_result_allowed(item):
                blocked_reason = blocked_reason or self._vision_gate_reason
                continue
            accepted.append((question, item))

        if not accepted:
            self._vision_gate_reason = (
                blocked_reason or self._vision_gate_reason or "vision_read_empty"
            )
            # 原始回复进留痕：``region_mad=0.00`` 那次事故里，最关键的证据
            # （模型到底给了多大的框）就是因为没留痕，只能靠截图反推。
            self._emit(
                Event.LOG_LINE,
                {
                    "vision_read": "rejected_all",
                    "reason": self._vision_gate_reason,
                    "raw": batch.raw[:400],
                },
            )
            return None

        question, first = accepted[0]
        # 提交范围与「一屏几道题」都从这里记 —— 它们是整卷提交的判据来源。
        self._note_read_scope(first, len(accepted))
        # 这一批观测已经在解析成功时就留下了（见上面 ``_last_batch``）——
        # 留的是**整批**而不是第一道题，因为「这一屏长什么样」是页面级的事实。
        # 几何**全部**先记好（含入队的那些）—— 取用时不必重读，也就不会再花一次调用。
        for queued_question, item in accepted:
            self._vision_reads[queued_question.qid] = (item, size)
        for queued_question, _item in accepted[1:]:
            self._pending_reads.append(queued_question)

        self._emit(
            Event.LOG_LINE,
            {
                "vision_read": "ok",
                "qid": question.qid,
                "qtype": question.qtype.value,
                "reported_qtype": first.reported_qtype,
                "skill_id": question.skill_id,
                "reported_skill_id": first.reported_skill_id,
                "skill_error": question.skill_error,
                "options": len(first.options),
                "num_text": first.num_text,
                # 一屏读到几道、其中几道留给后面用 —— 「省了几次调用」看这两个数。
                "batch": len(accepted),
                "queued": len(accepted) - 1,
                "more_below": batch.more_below,
                # 页面观测留在日志里：推进方式与提交范围都是照它裁决的，
                # 事后要能一眼看出「它当时看到的页面是什么样」。
                "page": _page_brief(batch),
            },
        )
        return question

    def _take_queued_question(self) -> Question | None:
        """取一道**同屏读到、还没做**的题；队列空则返回 ``None``。

        走这条路的题**不截图、不调模型** —— 它的几何是上一次读图时一起算好的，
        只要画面没动就依然有效（清空点见 :attr:`_pending_reads` 的说明）。
        这是「一屏多题」真正省下消耗的地方。
        """
        if not self._pending_reads:
            return None
        question = self._pending_reads.pop(0)
        # 这一道**没有经过推进**就被做掉了 —— 记账，供推进时的题号修正与
        # 「新鲜的答题卡指针是否已过期」判断使用（见 :attr:`_queued_consumed`）。
        self._queued_consumed += 1
        self._emit(
            Event.LOG_LINE,
            {
                "vision_read": "reused",
                "qid": question.qid,
                "num_text": None,
                "remaining": len(self._pending_reads),
                "hint": "同屏已读到的题，直接复用几何，不再截图与调用模型",
            },
        )
        return question

    def _vision_result_allowed(self, result: ReadResult) -> bool:
        """门禁：题面残缺 / 模型拿不准 → **不下传**。返回是否放行。

        判据本身在 :func:`solve.reader.gate_read_result`（纯函数、可单测）；
        这里只负责**记账与留痕**：拦下时把原因记进 :attr:`_vision_gate_reason`
        （主循环拿它当 ``_pause_for`` 的理由），并发一条可审计的日志。

        为什么拦（而不是"把残缺的题交给解题组让它自己小心"）：读题是本链路唯一
        **错了也不报错**的环节。残缺的题面看起来完全正常，拿它去解题只会得到
        一个"看起来正常的错答案"，再按坐标点到用户的真实页面上。
        """
        allowed, blocked = gate_read_result(result)
        if allowed:
            return True
        self._emit(
            Event.LOG_LINE,
            {
                "vision_read": "gated",
                "reason": blocked,
                "clipped": result.clipped,
                "uncertain": result.uncertain,
                "more_below": result.more_below,
                "note": result.note,
                "hint": "读题结果残缺/拿不准 → 按纪律不下传；核对画面后重跑",
            },
        )
        self._vision_gate_reason = blocked
        return False

    async def _apply(self, item: TaskItem, question: Question, answer: Answer) -> None:
        """作答入口：**v0.2.0 起只此一条路**（按模型给的坐标点选项）。

        原先这里分派两条：DOM 锚点取 ``Locator`` → ``apply_answer``，或按模型给的
        归一化框点坐标。DOM 通道整体删除后只剩后者，所以这一层退化成一次委托 ——
        留着它是为了让「作答 = 按坐标点」这件事与主循环的调用点在同一处可见。
        """
        await self._apply_by_geometry(item, question, answer)

    async def _apply_by_geometry(
        self, item: TaskItem, question: Question, answer: Answer
    ) -> None:
        """按**坐标**点选项（``ActLevel.L6_VISION_XY``，v0.2.0 唯一的作答路径）。

        为什么不做「执行前重校验题干」：题干与几何来自**同一次读图**，
        拿它校验自己没有意义。替代护栏是「页面没被换掉」——
        每点一个选项之前比对 URL，一旦不一致就**一次都不再点**。

        选中是否生效由**执行层**用截图差分判（``VerifyKind.SCREENSHOT_DIFF``）。
        注意它只能证明「点下去之后那块像素变了」，**不能**证明「选中的就是我想要
        的那一项」—— 所以读题端的门禁（``gate_read_result``）才是这条链路的真正防线。
        """
        cached = self._vision_reads.get(question.qid)
        page = self._page
        actuator = self._actuator_for(item)
        if cached is None or page is None or actuator is None:
            # 没有几何 = 没有任何可点的坐标。**不猜**（猜出来的坐标就是一次真实误点）。
            self._emit(Event.LOG_LINE, {"vision_apply": "no_geometry", "qid": question.qid})
            self._ensure_state(item, QuestionState.FAILED)
            self._pause_for("vision_geometry_missing")
            return
        read, size = cached

        url_before = getattr(page, "url", None)
        boxes = {opt.label: opt.box for opt in read.options}
        for label in answer.chosen_labels:
            box = boxes.get(label)
            if box is None:
                self._emit(
                    Event.LOG_LINE,
                    {"vision_apply": "no_box", "qid": question.qid, "label": label},
                )
                self._ensure_state(item, QuestionState.FAILED)
                self._pause_for("vision_box_missing")
                return
            if getattr(page, "url", url_before) != url_before:
                # 页面在下手之前被换掉了：这份几何已经不属于当前画面。
                self._pause_for("page_changed_mid_action")
                return
            result = await actuator.select_option(
                box,
                size,
                question.qtype,
                target_label=f"answer:{question.qid}:{label}",
            )
            self._record_action(item, result)
            if not result.ok:
                self._ensure_state(item, QuestionState.FAILED)
                self._pause_for("action_failed")
                return
            await self._click_gap()

        await self._capture(page, item, "after")
        self._ensure_state(item, QuestionState.APPLIED)

    # ------------------------------------------------------------------ 提交时机（2026-09-28 加）

    def _submit_scope_of_run(self) -> SubmitScope:
        """本次运行该**按哪种范围**提交 —— 取自**开局裁决的方案**。

        2026-09-30 起它不再随每次读题漂移：范围在开局定死一次（:attr:`_plan`），
        之后整条运行都按它走。旧版按「最近一次读题说了什么」判，于是
        ①同一份卷子在不同屏上可能判出不同范围；②整卷页面因为某一屏看不到「交卷」
        而被判成「每题提交」—— 真机上的表现就是「做到一半停下等人点提交」。

        没有方案（开局读图失败）时才走结构性兜底：一屏读到过 ≥2 道题 → 整卷，
        否则 → ``QUESTION``（**保持靶场的既有行为**：一屏一题、每题一个提交按钮）。
        """
        plan = self._plan
        if plan is not None and plan.submit_scope is not None:
            return plan.submit_scope
        if self._batch_max >= 2:
            return SubmitScope.PAPER
        return SubmitScope.QUESTION

    def _note_read_scope(self, read: ReadResult, batch_size: int) -> None:
        """记录「一屏最多读到过几道题」（裁决前的结构性兜底证据）。"""
        del read  # 提交范围不再来自单次读题，见 :meth:`_submit_scope_of_run`
        if batch_size > self._batch_max:
            self._batch_max = batch_size

    def _note_last_submit(self, batch: ReadBatch, size: tuple[int, int] | None) -> None:
        """记下**最近一次**观测到的提交按钮框（配那一次截图的尺寸）。

        框与尺寸必须同源：拿这一屏的框配上一屏的尺寸，点出来的位置会整体偏移。
        观测里没有提交按钮时**什么都不做**（保留上一次的）—— 页面下方还没滚到
        「交卷」并不代表上一次看到的那个框失效了。
        """
        page_view = batch.page
        if page_view is None or page_view.submit is None or size is None:
            return
        if page_view.submit.box is None:
            return
        self._last_submit = (page_view.submit.box, size)

    async def _submit_paper_once(self, page: Any) -> None:
        """整卷收尾：**全部题目做完之后提交一次**。

        四道前提，缺一不做 —— 宁可少交一次，也不能在没做完的时候交：

        ==========================  ============================================
        视觉组确认过「全部完成」       :attr:`_completion_confirmed`。**这是主闸门**，
                                    它排除了「推进失败停下」「读题失败停下」
                                    这些「主循环退出了但并没做完」的情形
        这一轮真的点过选项           :attr:`_last_applied_item_id`。一道都没作答，
                                    既没有「交什么」这回事，也拿不到 ``submit_box``
        范围是整卷                   :meth:`_submit_scope_of_run`
        还没交过                     :attr:`_paper_submitted` —— 提交**绝不重放**
        ==========================  ============================================

        提交动作交给 :meth:`_submit_and_confirm` —— 那条路已经有「点一次、
        截图差分回读、失败就暂停等人」的完整处置，不在这里另写一份。
        """
        if self._submit_scope_of_run() is not SubmitScope.PAPER:
            return
        if self._paper_submitted:
            return
        if not self._completion_confirmed or self._last_applied_item_id is None:
            self._emit(
                Event.LOG_LINE,
                {
                    "submit": "skipped",
                    "scope": SubmitScope.PAPER.value,
                    "reason": (
                        "未确认全部完成"
                        if not self._completion_confirmed
                        else "这一轮没有任何已作答的题"
                    ),
                    "hint": "整卷提交只在「视觉组确认做完」之后执行；也可以自己在页面上交卷",
                },
            )
            return
        item = self._find_item(self._last_applied_item_id)
        if item is None:
            return
        self._paper_submitted = True
        self._emit(
            Event.LOG_LINE,
            {
                "submit": "paper",
                "scope": SubmitScope.PAPER.value,
                "item_id": item.item_id,
                "hint": "全部题目已完成，执行整卷交卷",
            },
        )
        await self._submit_and_confirm(item)
        if self.paused:
            # 提交失败/超时：这一卷**结果未知**，其余题保持原状更安全 ——
            # 「未确认」绝不伪造成「已确认」（与整条链路的纪律一致）。
            return
        self._settle_paper_batch()

    def _settle_paper_batch(self) -> None:
        """整卷提交**确认生效**之后，把这一轮其余已作答的题一起推进到 ``verified``。

        为什么必须做：整卷只有**一次**提交动作，而结果回读只挂在「最后作答的那道题」
        （:meth:`_submit_and_confirm` 的 ``item``）上，其余题会一直停在 ``applied``。
        而 ``applied`` **不是终态** —— 续跑时 ``is_terminal("applied")`` 为假，
        那些题会被**重做一遍**：多选题上重新点一遍选项，就是把刚选上的勾**取消**
        （用户报的「点击正确却判断错误导致胡乱操作」的一种形态）。

        为什么分两步写（``applied`` → ``submitted`` → ``verified``）：状态机里
        ``applied`` **没有**到 ``verified`` 的边（``verified`` 只能由结果回读触发，
        见 ``core/states.py``）。这两步都合法，语义也如实：答案随整卷交上去了
        （``submitted``），提交被回读确认了（``verified``）。

        只在**没暂停**时调用：提交失败时这一卷结果未知，其余题保持原状才是诚实的。
        """
        settled = 0
        for other in self.items:
            if other.item_id == self._last_applied_item_id:
                continue  # 它已经由回读自己推进了终态
            if QuestionState(other.state) is not QuestionState.APPLIED:
                continue
            self._write_state(other, QuestionState.SUBMITTED)
            self._write_state(other, QuestionState.VERIFIED)
            settled += 1
        if settled:
            self._emit(
                Event.LOG_LINE,
                {
                    "submit": "paper_batch_settled",
                    "scope": SubmitScope.PAPER.value,
                    "items": settled,
                    "hint": "整卷已提交确认，这一轮其余已作答的题一并标记为已验证",
                },
            )

    async def _submit_and_confirm(self, item: TaskItem) -> None:
        """提交 + 结果回读。**提交不重试、不重放**（M3-6）。

        顺序是**提交 → 回读结果 → 提交级限速**，不是「提交 → 限速 → 回读」：
        结果证据的窗口可能很短（网课的弹题面板在提交后约 400ms 就被移除），
        中间插一个 5~15s 的 ``sleep_submit_gap`` 会把窗口直接睡过去，
        症状是「明明提交成功了，却一路判 ``submit_timeout``」——P8 真机实测踩到。
        限速移到回读之后，两次提交之间的间距仍然 ≥ ``submit_gap_s``。

        v0.2.0 起只有一条路：按模型给的坐标点提交，再用**截图差分**回读结果。
        原先那条「取 ``selectors.submit`` 的 ``Locator`` 再 ``verify_submit``」
        的 DOM 分支已随 DOM 通道整体删除。

        **提交按钮的框有三个来源，按优先级取**（2026-09-30 起）：

        1. 开局裁决的方案里那个框（``RunPlan.submit_box``，配开局那张图的尺寸）；
        2. **最近一次读图**看到的框（:attr:`_last_submit`，配那一屏的尺寸）——
           「每题提交」要的正是当前这道题所在那一屏的按钮；
        3. 收尾确认那一屏读到的框（:attr:`_end_submit`，配收尾那张图的尺寸）——
           「交卷」往往只在收尾那一屏才露出来。

        每题范围把 2 排在 3 前面（当前这题那一屏的按钮才作数），整卷范围把 3 排在
        2 前面（收尾那一屏的「交卷」才是要点的那个）。**框与尺寸永远同源**：
        拿 A 屏的框配 B 屏的尺寸，点出来的位置会整体偏移 —— 真机上就是「点歪了」。

        三个都没有时按范围区分处置，共同点是**都不猜坐标**：

        * 整卷（``PAPER``）：全部题已经做完，人只需点一下「交卷」——
          留下明确提示后暂停（不是卡住，是缺最后一下人工动作）；
        * 每题（``QUESTION``）：这一题提交不了 → 答案可能没被记录，必须停下。
        """
        cached = self._vision_reads.get(item.qid or "")
        page = self._page
        actuator = self._actuator_for(item)
        if page is None or actuator is None:
            self._emit(
                Event.LOG_LINE, {"vision_submit": "no_geometry", "item_id": item.item_id}
            )
            self._ensure_state(item, QuestionState.FAILED)
            self._pause_for("vision_geometry_missing")
            return
        paper = self._submit_scope_of_run() is SubmitScope.PAPER
        plan_pair = (
            (self._plan.submit_box, self._plan_size)
            if self._plan is not None and self._plan.submit_box is not None
            else None
        )
        ordered = (
            [plan_pair, self._end_submit, self._last_submit]
            if paper
            else [plan_pair, self._last_submit, self._end_submit]
        )
        box: tuple[float, float, float, float] | None = None
        size: tuple[int, int] | None = None
        for candidate in ordered:
            if candidate is not None and candidate[0] is not None and candidate[1] is not None:
                box, size = candidate
                break
        if box is None or size is None:
            # 画面上找不到提交按钮。**不猜** —— 猜错就是一次真实的误点。
            # 状态留在 ``applied``：选项是点对了的，人点一下按钮就完事。
            self._emit(
                Event.LOG_LINE,
                {
                    "vision_submit": "no_box",
                    "item_id": item.item_id,
                    "scope": SubmitScope.PAPER.value if paper else SubmitScope.QUESTION.value,
                    "hint": (
                        "整卷范围：题目已全部作答，请手动点一次「交卷 / 提交作业」"
                        if paper
                        else "本题范围：画面上找不到提交按钮，核对画面后重跑"
                    ),
                },
            )
            self._pause_for("vision_no_submit_box")
            return
        if cached is None:
            self._emit(
                Event.LOG_LINE, {"vision_submit": "no_read", "item_id": item.item_id}
            )

        if QuestionState(item.state) is not QuestionState.SUBMITTED:
            self._transition(item, QuestionState.SUBMITTED)

        # 差分基准：**点提交之前**的画面。判据只有「画面变了没有」，
        # 所以取整屏 —— 提交后面板可能出现在任何位置，甚至整页翻走。
        before = await self._screenshot(page)
        result = await actuator.submit(box, size, target_label=f"submit:{item.item_id}")
        self._record_action(item, result)
        if not result.ok:
            self._transition(item, QuestionState.FAILED)
            self._pause_for("submit_failed")
            return

        await self._confirm_submit(item, box=_FULL_VIEWPORT, size=size, before=before)
        await self._capture(page, item, "after_submit")
        if not self.paused:
            await self._submit_gap()

    async def _confirm_submit(
        self,
        item: TaskItem,
        *,
        box: tuple[float, float, float, float] | None = None,
        size: tuple[int, int] | None = None,
        before: bytes | None = None,
    ) -> None:
        """提交结果回读：**截图差分**（v0.2.0 唯一的题目侧判据）。只回读，不重放。

        传了 ``before``（提交前那张图）才判得了：画面变了 = 这一下生效了 →
        ``verified``；没变 = 没生效 → ``failed`` + ``submit_timeout`` 停下等人。
        误判成「生效」的那一侧还有一道兜底：主循环若再读到**同一道题**（qid 相同），
        会以 ``question_did_not_advance`` 停下，不会静默跳过。

        ``before`` 为空（续跑：``submitted`` 是上一轮进程留下的）时**判不了** ——
        差分基准活在提交那个进程的内存里。此时只记一条留痕、保持 ``submitted``：
        与 M4 的续跑红线一致（危险态只回读、绝不重新点击），
        绝不把「结果未知」伪造成 ``verified``。
        """
        verifier = self._verifier_for(item)
        if verifier is None or box is None or size is None or before is None:
            self._emit(
                Event.LOG_LINE,
                {
                    "submit_readback": "skipped",
                    "item_id": item.item_id,
                    "reason": "no_diff_baseline",
                },
            )
            return
        result = await verifier.verify_region_changed(box, size, before)
        self._write_verify(item, result)
        if result.ok:
            self._transition(item, QuestionState.VERIFIED)
            return
        self._transition(item, QuestionState.FAILED)
        self._pause_for("submit_timeout")

    # ------------------------------------------------------------------ 人工决策

    async def _await_human(self, item: TaskItem) -> bool:
        """挂起等人确认。返回 ``True`` 表示已确认（可以继续执行/提交）。

        人工决策是通过 HTTP 写进 SQLite 的（``POST /api/tasks/{id}/confirm``），
        所以这里以**库里的状态**为准。

        拿到决策时**必须把运行闸放回去**：否则这一个条目做完之后，主循环会在
        下一个边界看到「还在暂停」而退出，半自动跑批就变成「确认一次、停一次」，
        用户得反复按「恢复」。
        """
        self._pause_for("needs_confirm")
        while not self._stopped:
            await asyncio.sleep(_DECISION_POLL_S)
            if self._stopped:
                break
            state = self._state_of(item)
            if state is not QuestionState.PENDING_CONFIRM:
                decision = "confirm" if state is QuestionState.APPLIED else "reject"
                self._emit(Event.RUN_RESUMED, {"run_id": self.ctx.run_id, "decision": decision})
                if self._paused_by == "needs_confirm":
                    # 决策即放行（与 ``resume()`` 同义）。只认自己造成的暂停，
                    # 免得把用户手动按下的「暂停」一起解掉。
                    self._paused_by = None
                    self._gate.set()
                return state is QuestionState.APPLIED
            if self._gate.is_set():
                # 用户按了「恢复」但没做决策：不当作同意一切，继续等。
                # 同时把暂停原因**重新标回** —— 否则界面会显示「运行中」，
                # 而它其实还在等人确认。
                self._paused_by = "needs_confirm"
                self._gate.clear()
        return False

    # ------------------------------------------------------------------ 状态机与落库

    def _transition(self, item: TaskItem, target: QuestionState) -> None:
        """**严格**迁移（危险路径专用）。同态调用是 no-op。"""
        current = QuestionState(item.state)
        if current is target:
            return
        require_transition(current, target)  # 非法即抛，不得绕过
        self._write_state(item, target)

    def _ensure_state(self, item: TaskItem, target: QuestionState) -> None:
        """**只进不退**地推进到 ``target``。

        ``pending`` 没有入边（T0-2），所以续跑时既不能回退、也不该硬迁 ——
        已经在更靠后的状态就直接放行，能迁才迁。
        """
        current = QuestionState(item.state)
        if current is target or not can_transition(current, target):
            return
        self._write_state(item, target)

    def _write_state(self, item: TaskItem, target: QuestionState) -> None:
        previous = QuestionState(item.state)
        item.state = target
        item.updated_at = datetime.now(UTC)
        self._persist_item(item)
        self._emit(
            Event.TASK_STATE_CHANGED,
            {"item_id": item.item_id, "from": previous.value, "to": target.value},
        )

    def _state_of(self, item: TaskItem) -> QuestionState:
        """以**库里的状态**为准（人工决策写在库里），并同步回内存对象。"""
        state = self._read_state(item.item_id)
        if state is not None:
            item.state = state
        return QuestionState(item.state)

    def _read_state(self, item_id: str) -> QuestionState | None:
        conn: sqlite3.Connection | None = self.deps.conn
        if conn is None:
            return None
        row = conn.execute("SELECT state FROM task_item WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        try:
            return QuestionState(row["state"])
        except ValueError:
            return None

    def _claim_item(self, question: Question, position: int | str) -> TaskItem:
        """按 ``qid`` 认领已有条目（续跑）或新建一条。

        ``item_id`` 是**运行内**标识（``<run_id>-<qid>``），不是 ``qid`` 本身。
        原因：``task_item.item_id`` 是**全局主键**，而 ``qid`` 是题目的身份 ——
        同一批题重跑一次，若沿用 ``qid`` 当主键，upsert 会命中上一次运行留下的
        行（并刻意不改 ``run_id``），于是「新运行一条任务都没有」，而且旧状态
        会把新运行整批当成「已完成」跳过去。题干身份仍在 ``qid`` 列，续跑按
        ``qid`` 认领，两件事不混。
        """
        for item in self.items:
            if item.qid == question.qid:
                return item
        item = TaskItem(
            item_id=f"{self.ctx.run_id}-{question.qid}",
            type=TaskType.QUIZ,
            qid=question.qid,
            state=resume_entry(QuestionState.PENDING),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.items.append(item)
        self._persist_item(item)
        self._emit(
            Event.TASK_CREATED,
            {"item_id": item.item_id, "qid": question.qid, "position": position},
        )
        return item

    def _persist(self) -> None:
        """把运行元信息与**全部**任务落库（契约方法）。

        常规推进是逐条 ``_persist_item``；这个入口用于「一次性刷全量」——
        例如人工确认改了库之后，编排层想把内存态整体对齐一次。
        """
        self._open_run()
        for item in self.items:
            self._persist_item(item)

    def _persist_item(self, item: TaskItem) -> None:
        """写一条任务状态。

        **失败必须留痕**：吞掉异常等于「跑完了但库里没有」，续跑时会当成
        从没做过而重跑一遍 —— 那正是 M4 最不想要的失败形态。
        """
        if self.deps.conn is None:
            return
        try:
            db.upsert_task_item(self.deps.conn, self.ctx.run_id, item)
        except Exception as exc:
            logger.warning("任务状态落库失败（item=%s）：%s", item.item_id, exc)
            self.deps.warnings.append(f"persist_failed: {item.item_id}: {exc}")

    def _open_run(self) -> None:
        if self.deps.conn is None:
            return
        try:
            db.upsert_run(
                self.deps.conn,
                run_id=self.ctx.run_id,
                started_at=self.ctx.started_at,
                config=self.ctx.cfg,
                status="running",
            )
        except Exception as exc:
            logger.warning("运行元信息落库失败：%s", exc)

    def _close_run(self, status: str) -> None:
        if self.deps.conn is None:
            return
        try:
            db.finish_run(self.deps.conn, self.ctx.run_id, status)
        except Exception as exc:
            logger.warning("运行收尾落库失败：%s", exc)

    def _restore(self) -> None:
        """从 SQLite 恢复队列与任务栈（``submitted`` 条目原地保留）。

        任务栈**先挂上持久化后端再恢复**：``restore()`` 自己会 ``_flush()`` 一次
        把读到的帧原样写回；顺序反了就会拿空栈去覆盖库里的帧（"恢复即丢失"）。
        """
        conn = self.deps.conn
        if conn is None:
            return
        try:
            self.items = db.load_task_items(conn, self.ctx.run_id)
            self.stack.attach_store(db.make_task_stack_store(conn, self.ctx.run_id))
            self.stack.restore(db.load_suspend_frames(conn, self.ctx.run_id))
        except Exception as exc:  # 库损坏不该让运行起不来，但必须留痕
            logger.warning("恢复运行状态失败：%s", exc)
            self.deps.warnings.append(f"restore_failed: {exc}")
            self._emit(Event.RUN_ERROR, {"run_id": self.ctx.run_id, "message": str(exc)})
            self.items = []

    # ------------------------------------------------------------------ 留痕

    def _write_perception(self, item: TaskItem, perception: PerceptionResult) -> None:
        if self.deps.run_logger is not None:
            self.deps.run_logger.save_json(item.item_id, "perception", perception)

    def _skip_unsupported_question(
        self,
        item: TaskItem,
        question: Question,
        reason: str,
        cached: tuple[ReadResult, tuple[int, int]] | None,
    ) -> None:
        """持久化无匹配技能的题目为 SKIPPED，不求解、不点击，并留下原始证据。"""
        state = QuestionState(item.state)
        if state is QuestionState.SKIPPED:
            return
        if state not in {QuestionState.PENDING, QuestionState.PERCEIVED}:
            # 终态续跑照常处理；不允许无技能判定覆写已提交等既有进度。
            if is_terminal(state):
                self._emit(
                    Event.TASK_UPDATED,
                    {
                        "item_id": item.item_id,
                        "state": state.value,
                        "resumed": True,
                        "redone": False,
                    },
                )
                return
            self._pause_for("unsupported_question_unexpected_state")
            return
        self._ensure_state(item, QuestionState.PERCEIVED)
        item_id = item.item_id
        record: dict[str, Any] = {
            "qid": question.qid,
            "stem": question.stem,
            "options": [
                {"label": option.label, "text": option.text}
                for option in question.options
            ],
            "reported_qtype": question.reported_qtype or question.qtype.value,
            "qtype": question.qtype.value,
            "skill_id": question.skill_id,
            "reported_skill_id": question.reported_skill_id,
            "skill_error": question.skill_error,
            "reason": reason,
            "disposition": "skipped_no_matching_skill",
            "action_taken": False,
        }
        logger_ = self.deps.run_logger
        if logger_ is not None:
            logger_.save_json(item_id, "skipped", record)
            if cached is not None:
                read, size = cached
                logger_.save_json(
                    item_id,
                    "vision_read",
                    {"size": list(size), "read": read.model_dump(mode="json")},
                )
        self._ensure_state(item, QuestionState.SKIPPED)
        self._emit(
            Event.LOG_LINE,
            {"vision_read": "skipped_no_skill", **record, "item_id": item_id},
        )
        self._emit(
            Event.TASK_UPDATED,
            {"item_id": item_id, "state": QuestionState.SKIPPED.value, "reason": reason},
        )

    def _write_vision_read(self, item: TaskItem) -> None:
        """把视觉读题的**原始产出**（含每个 ``box``）落到留痕目录。

        为什么非落不可：两次真机事故（``region_mad=0.00`` 点在行尾空白、
        ``region_mad=0.06`` 点了被禁用的交卷按钮）里，最关键的证据都是
        「模型给的框到底在哪、有多大」—— 而 ``perception.json`` 里**没有几何**
        （``PerceptionResult`` 刻意不含它），于是每一次都只能靠截图反推几何、
        绕一大圈。这一份是「坐标为什么是这个值」的直接出处。

        ``page`` 观测块（进度 / 控件 / 答题卡 / 提交按钮）不在这里 —— 它是**页面级**
        的事实，落在 ``run.json`` 与事件流里（``advance.calibrated``）；这一份只讲
        「这道题的每个框在哪」，所以判读时不必在一堆页面数据里找坐标。
        """
        logger_ = self.deps.run_logger
        if logger_ is None:
            return
        cached = self._vision_reads.get(item.qid or "")
        if cached is None:
            return
        read, size = cached
        logger_.save_json(
            item.item_id,
            "vision_read",
            {"size": list(size), "read": read.model_dump(mode="json")},
        )

    def _write_solve(self, item: TaskItem, answer: Answer) -> None:
        """写 ``solve.json`` 与模型原始响应全文（M4-3：**全文必须落盘**）。"""
        logger_ = self.deps.run_logger
        if logger_ is None:
            return
        vote = self._last_vote()
        samples = [record.model_dump(mode="json") for record in (vote.samples if vote else [])]
        logger_.save_json(
            item.item_id,
            "solve",
            {
                "qid": answer.qid,
                "chosen_labels": answer.chosen_labels,
                "chosen_texts": answer.chosen_texts,
                "confidence": answer.confidence,
                "solve_path": str(answer.solve_path),
                "review_flag": answer.review_flag,
                "model_name": answer.model_name,
                "samples": samples,
                "vote_distribution": dict(vote.distribution) if vote else {},
            },
        )
        raws = [record.raw for record in (vote.samples if vote else []) if record.raw]
        if raws:
            logger_.save_model_raw(item.item_id, "\n\n".join(raws))

    def _last_vote(self) -> Any | None:
        """取最近一次投票明细（由 Solver 记录，见 ``solve/solver.py``）。"""
        solver = self.deps.solver
        getter = getattr(solver, "last_vote", None)
        return getter() if callable(getter) else getter

    def _write_verify(self, item: TaskItem, result: Any) -> None:
        if self.deps.run_logger is not None:
            self.deps.run_logger.save_json(item.item_id, "verify", result)

    def _record_action(self, item: TaskItem, result: Any) -> None:
        """把动作结果落进 ``action.json`` 与降级统计表。"""
        if result is None:
            return
        if self.deps.run_logger is not None:
            self.deps.run_logger.save_json(item.item_id, "action", result)
        self._record_level_stat(item, result)

    def _record_level_stat(self, item: TaskItem | None, result: Any) -> None:
        """只记降级统计，**不覆盖** ``action.json``。

        「下一题」这类动作也要进热力图，但它的结果不能盖掉该条目最后一条
        真实动作记录（``_options_clicked`` 就是靠那条判断断点落在哪一步）。
        """
        conn = self.deps.conn
        if conn is None or item is None or result is None:
            return
        with suppress(Exception):
            db.record_level_stat(
                conn,
                item_id=item.item_id,
                kind=str(result.kind),
                level_used=str(result.level_used),
                ok=bool(result.ok),
                elapsed_ms=int(getattr(result, "elapsed_ms", 0) or 0),
            )

    def _options_clicked(self, item: TaskItem) -> bool:
        """``action.json`` 的最后一个动作是不是「选选项」。

        断点落在「已选好、还没提交」时，靠这份证据决定「补提交」还是「重新选」。
        没有留痕时返回 ``False``（重新选一遍）—— 重选比漏选安全：
        漏选会一路点到提交，交给用户的是一道空着的题。
        """
        logger_ = self.deps.run_logger
        if logger_ is None:
            return False
        try:
            path = logger_.item_dir(item.item_id) / "action.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and str(payload.get("kind")) in _APPLY_KINDS

    async def _capture(self, page: Any, item: TaskItem, stage: str) -> None:
        """抓一张 before/after 截图（**视口截图，禁 ``full_page``**）。"""
        logger_ = self.deps.run_logger
        if logger_ is None or page is None:
            return
        try:
            data = await page.screenshot()
        except Exception as exc:  # 页面已崩 / 帧正在销毁
            logger.warning("截图失败（%s）：%s", stage, exc)
            return
        if isinstance(data, bytes):
            logger_.save_screenshot(item.item_id, stage, data)

    # ------------------------------------------------------------------ 工具

    def _has_perception(self) -> bool:
        return self.deps.pipeline is not None and self.deps.adapter is not None

    def _auto_apply(self) -> bool:
        return bool(getattr(self.ctx.cfg, "auto_apply", False))

    def _log_root(self) -> Path:
        root = getattr(self.deps.run_logger, "root", None)
        return Path(root) if root is not None else Path("logs")

    def _actuator_for(self, item: TaskItem | None) -> Any | None:
        if item is None:
            return None
        if self.deps.actuator_factory is not None:
            return self.deps.actuator_factory(item)
        if self._page is None:
            return None
        from act.actuator import Actuator

        return Actuator(
            self._page,
            self.ctx.cfg,
            run_logger=self.deps.run_logger,
            item_id=item.item_id,
            bus=self.deps.bus,
        )

    def _verifier_for(self, item: TaskItem) -> Any | None:
        if self.deps.verifier_factory is not None:
            return self.deps.verifier_factory(item)
        if self._page is None:
            return None
        from act.verifier import Verifier

        return Verifier(
            self._page,
            self.ctx.cfg,
            run_logger=self.deps.run_logger,
            item_id=item.item_id,
            bus=self.deps.bus,
        )

    async def _click_gap(self) -> None:
        if self.deps.click_gap is not None:
            await self.deps.click_gap()
            return
        await sleep_click_gap(self.ctx.cfg)

    async def _submit_gap(self) -> None:
        if self.deps.submit_gap is not None:
            await self.deps.submit_gap()
            return
        await sleep_submit_gap(self.ctx.cfg)

    def _pause_for(self, reason: str) -> None:
        """挂起运行并说明原因。事件带 ``pause: true``，界面据此提示。"""
        self._paused_by = reason
        self._gate.clear()
        self._emit(
            Event.RUN_PAUSED,
            {"run_id": self.ctx.run_id, "reason": reason, "pause": True},
        )

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.deps.bus is not None:
            self.deps.bus.emit(event, payload)
        # 顺手落一份**按行**的事件流（``logs/<run_id>/events.jsonl``）。
        # 这是「这次运行到底发生了什么」的第一手记录；推进动作原先一点留痕都没有
        # （2026-09-28 排查「滚过头」只能靠读题结果里的题号反推，绕了一大圈）。
        run_logger = self.deps.run_logger
        if run_logger is not None:
            run_logger.append_event(event, payload)

    # -- 目标与浏览器生命周期 ----------------------------------------------- #
    @asynccontextmanager
    async def _page_session(self) -> Any:
        """开一个可操作的页面。

        **优先附加到用户正在用的目标**（P11），没有目标才退回自启靶场。
        这个 `if` 就是方向改动的落点：改造前这里只有下面那条
        ``default_page_factory`` 分支 —— 不管用户想抓什么，我们都自己起一个
        浏览器去靶场，于是永远看不到电脑上真正在跑的东西。
        """
        source = self.deps.target_source
        if source is not None and self.deps.target_id:
            async with source.open(self.deps.target_id) as handle:
                self._target = handle
                if handle.page is None:
                    raise NotImplementedError(
                        f"目标类型 {handle.kind} 尚无可用句柄："
                        "原生桌面窗口只有视觉通道，留待二期实现"
                    )
                yield handle.page
            self._target = None
            return

        async with default_page_factory(
            self.ctx.cfg, channel=self.deps.browser_channel, url=self.deps.start_url
        ) as page:
            yield page

    async def _aclose(self) -> None:
        """释放依赖。**只关自己开的东西**（注入的 page / solver 归注入方）。"""
        if self.deps.page is None and self.deps.conn is not None:
            with suppress(Exception):
                self.deps.conn.close()
        for name in ("solver", "pipeline", "adapter", "target_source"):
            target = getattr(self.deps, name, None)
            closer = getattr(target, "aclose", None)
            if closer is not None:
                with suppress(Exception):
                    await closer()


# --------------------------------------------------------------------------- #
# 默认页面工厂
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def default_page_factory(
    cfg: RunConfig,
    *,
    channel: str = "msedge",
    url: str = "http://127.0.0.1:8899/quiz.html",
) -> Any:
    """起一个页面并导航到 ``url``。

    复用系统浏览器（``channel``），**不下载自带 chromium**；``storage_state``
    存在时带上（真实站点的登录态），不存在就匿名跑。
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=channel,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        state_path = Path(cfg.storage_state_path)
        context = await browser.new_context(
            storage_state=str(state_path) if state_path.exists() else None
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            yield page
        finally:
            with suppress(Exception):
                await context.close()
            with suppress(Exception):
                await browser.close()

