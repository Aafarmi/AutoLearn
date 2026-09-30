"""开局判定：把视觉组的**观测**裁决成一份 :class:`~core.models.RunPlan`（2026-09-30 加）。

这一份文件回答的都是**程序侧**的问题，不是模型侧的问题
------------------------------------------------------
用户对上一版的判词是「下一题处理仍然存在严重问题」，并给出方法：
**程序开始运行时需要有一套判断逻辑，一旦判定好后后续的全部按照这套逻辑。**

于是分工被切干净了：

============================  ==================================================
视觉组（模型）                 只做观测：这一屏有哪些控件、有没有题号答题卡、
                               进度文字写的是什么、提交按钮管多大范围
本模块（程序）                 **裁决**：用哪一条推进逻辑、推进落点在哪、
                               什么时候提交、什么时候算做完
============================  ==================================================

裁决只做一次（``Orchestrator._plan_run`` 在开局调用），之后整条运行
**只按裁决结果执行**：不再每题重新问模型「下一题在哪」，也没有「点击试不通就
改滚动、滚动试不通改滑动」的换招阶梯 —— 那正是「一次跳过十几道题」的成因。

裁决的顺序是固定的（:data:`core.advance_library.PLAN_PREFERENCE`）
----------------------------------------------------------------
1. **答题卡（``CARD``）**：有题号网格 → 点「下一题号」那一格。
   真实作业页（一屏一题、44 题）最常见就是这种：**没有**「下一题」按钮，
   切题只能点答题卡。旧版只说得出一句 ``click`` 而给不出落点，于是做到一半就停。
2. **固定控件（``CLICK``）**：画面上有「下一题 / 下一页 / 继续」→ 点它。
3. **向下滚动（``SCROLL``）**：都没有、页面还能滚 → 滚一步、读一屏、
   确认新题进来了才算到位（它自带「不许跳题」的判据）。
4. **滑动（``SWIPE``）**：连滚动都不行（整屏一张卡、不可滚动）→ 手势翻页。

**任何一步推不动都不换招**：如实停下，请视觉组确认「是不是全部完成了」。

几何为什么在开局一次算好
------------------------
答题卡的每一格都能由「网格 + 题号」推出来（:func:`grid_geometry` /
:func:`card_cell_box`）。开局拿两个观测点校准一次（当前题号格，最好还有下一题号格），
之后第 N 题的落点就是纯算术 —— 不需要每一步再截一张图、再问一次模型。
推算不出来（缺 ``cols`` / ``rows``、格子在可见区域之外）时**返回 ``None``**：
宁可停下，也绝不拿一个编出来的坐标去点用户的页面。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.advance_library import PLAN_PREFERENCE, strategy_for
from core.enums import AdvanceMethod, CompletionState, SubmitScope
from core.models import PageCard, RunPlan

if TYPE_CHECKING:  # pragma: no cover
    from core.models import ReadBatch

__all__ = [
    "CARD_GRID_TOLERANCE",
    "all_done",
    "card_cell_box",
    "derive_plan",
    "grid_geometry",
    "plan_summary",
]

logger = logging.getLogger(__name__)

NormBox = tuple[float, float, float, float]

#: 推算出来的格子允许超出「答题卡可见区域」多少（比例），超出即判为**算不出来**。
#:
#: 为什么要有它：答题卡可能是**内部可滚动的**，题号离当前很远时那一格并不在画面上。
#: 那时按算术算出来的坐标会落在卡片外面，点下去就是一次真实的误点
#: —— 宁可返回 ``None``（这一格推不出来）让程序停下。
CARD_GRID_TOLERANCE = 0.10


def _box_ok(box: object) -> bool:
    """归一化框是否合法（越界 / 倒挂一律判为非法，**不修**）。"""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    try:
        values = [float(value) for value in box]
    except (TypeError, ValueError):
        return False
    if any(value < 0.0 or value > 1.0 for value in values):
        return False
    return values[2] > 0.0 and values[3] > 0.0


def _center(box: NormBox) -> tuple[float, float]:
    return (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)


def _nearest_cell(card: PageCard, point: tuple[float, float]) -> int | None:
    """在 ``card`` 的网格里找「离 ``point`` 最近的那一格」的下标（0 基，行优先）。"""
    cols, rows = int(card.cols), int(card.rows)
    if cols <= 0 or rows <= 0:
        return None
    step_x, step_y = card.box[2] / cols, card.box[3] / rows
    best: tuple[float, int] | None = None
    for index in range(cols * rows):
        row, col = divmod(index, cols)
        cx = card.box[0] + (col + 0.5) * step_x
        cy = card.box[1] + (row + 0.5) * step_y
        distance = (cx - point[0]) ** 2 + (cy - point[1]) ** 2
        if best is None or distance < best[0]:
            best = (distance, index)
    return best[1] if best is not None else None


def grid_geometry(card: PageCard) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """答题卡网格 → ``((原点中心), (步距))``，两样都是**归一化**量。

    原点 = **题号最小的那一格（网格第 0 格）的中心**，步距 = 往右 / 往下走一格。

    校准只有一处：``card.current_box``（模型指着「当前题号」那一格）。
    用它在网格里找最近的一格，然后把整个网格平移，让那一格与模型给的位置对齐 ——
    模型给的答题卡外框往往包含标题与留白，直接按外框等分会有系统偏差，
    而「当前题号格」是它**明确指出来**的一个锚点，比外框可靠。

    拿不到 ``cols`` / ``rows`` / ``box`` 就返回 ``None``（**算不出来就不猜**）。
    """
    cols, rows = int(card.cols or 0), int(card.rows or 0)
    if cols <= 0 or rows <= 0 or not _box_ok(card.box):
        return None
    step_x, step_y = card.box[2] / cols, card.box[3] / rows
    if step_x <= 0.0 or step_y <= 0.0:  # pragma: no cover - 由 _box_ok 挡住
        return None
    origin_x, origin_y = card.box[0] + step_x / 2.0, card.box[1] + step_y / 2.0
    if card.current_box is not None and _box_ok(card.current_box):
        anchor = _nearest_cell(card, _center(card.current_box))
        if anchor is not None:
            row, col = divmod(anchor, cols)
            origin_x += _center(card.current_box)[0] - (card.box[0] + (col + 0.5) * step_x)
            origin_y += _center(card.current_box)[1] - (card.box[1] + (row + 0.5) * step_y)
    return (origin_x, origin_y), (step_x, step_y)


def _anchor_number(plan: RunPlan) -> int:
    """网格第 0 格（左上）对应的**题号**。

    由「当前题号 + 它在网格里的下标」反推：``anchor = current - matched_index``。
    拿不到校准锚点时按 1（题号从 1 开始、行优先填格，是最常见的形态）。
    """
    current = plan.current if isinstance(plan.current, int) and plan.current > 0 else 1
    card = plan.card
    if card is None or card.current_box is None or not _box_ok(card.current_box):
        return current
    matched = _nearest_cell(card, _center(card.current_box))
    if matched is None:
        return current
    return current - matched


def card_cell_box(plan: RunPlan, number: int) -> NormBox | None:
    """题号 ``number`` 那一格的归一化框；**推算不出来返回 ``None``**。

    三条「算不出来」都要如实返回 ``None``（调用方据此停下，不许硬点）：

    1. 没有网格几何（缺 ``cols`` / ``rows`` / 外框 / 步距）；
    2. 题号落在网格之外（``number`` 比第 0 格还小、或超出 ``cols × rows``）；
    3. 算出来的格子**超出了答题卡可见区域**（内部可滚动的卡片会这样）——
       那一格并不在画面上，点它等于点空白甚至点到别处。
    """
    if plan.card is None or plan.card_step is None or plan.card_origin is None:
        return None
    card = plan.card
    cols, rows = int(card.cols or 0), int(card.rows or 0)
    if cols <= 0 or rows <= 0:
        return None
    index = int(number) - plan.card_anchor
    if index < 0 or index >= cols * rows:
        return None
    step_x, step_y = plan.card_step
    origin_x, origin_y = plan.card_origin
    # ⚠️ 网格**按行换行**：步距要按 (行, 列) 分开算。
    # 直接 ``origin + index * step`` 在行末会算到卡片外面去（第一版就是这个 bug，
    # 由冒烟测试里第 17 题落在 x=0.66 而卡片只到 0.2 暴露出来）。
    row, col = divmod(index, cols)
    center_x = origin_x + col * step_x
    center_y = origin_y + row * step_y
    width, height = step_x * 0.8, step_y * 0.8
    box: NormBox = (center_x - width / 2.0, center_y - height / 2.0, width, height)
    if not _box_ok(box):
        return None
    pad_x = card.box[2] * CARD_GRID_TOLERANCE
    pad_y = card.box[3] * CARD_GRID_TOLERANCE
    inside = (
        card.box[0] - pad_x <= box[0]
        and card.box[1] - pad_y <= box[1]
        and box[0] + box[2] <= card.box[0] + card.box[2] + pad_x
        and box[1] + box[3] <= card.box[1] + card.box[3] + pad_y
    )
    return box if inside else None


def _card_plan(plan: RunPlan, page_card: PageCard) -> bool:
    """把观测到的答题卡落进方案。**算不出落点就不采用 CARD**（返回 False）。

    「不采用」是**整体回退**：连 ``plan.card`` 一起清干净，免得留下一份
    「方法不是 CARD、却带着一副答题卡几何」的奇怪状态给下游用。

    锚点优先用**模型直接指出的下一题号格**（``card.next_box``）：它是对
    「下一次该点哪里」的正面陈述，比「当前题号格 + 算术」更直接。用它的好处是
    **题号观测可以缺失**（``current`` 为 ``None``）而落点照样准 ——
    这时题号算术从「第 1 题之后」起算，只是不再做题号校验。
    """
    geometry = grid_geometry(page_card)
    if geometry is None:
        return False
    origin, step = geometry
    plan.card = page_card
    plan.card_origin = origin
    plan.card_step = step
    plan.card_anchor = _anchor_number(plan)
    # 「下一次要点的题号」：起点是 current（观测不到时按 1）+ 1。
    target = plan.card_start_number + 1

    if page_card.next_box is not None and _box_ok(page_card.next_box):
        matched = _nearest_cell(page_card, _center(page_card.next_box))
        if matched is not None:
            # 把「题号 → 格子」的对应关系钉在模型指出的那一格上：
            # 让 card_target(target) 的落点**恰好**是 next_box。
            row, col = divmod(matched, int(page_card.cols))
            plan.card_anchor = target - matched
            plan.card_origin = (
                _center(page_card.next_box)[0] - col * step[0],
                _center(page_card.next_box)[1] - row * step[1],
            )
    if card_cell_box(plan, target) is None:
        # 下一题的格子推算不出来（缺列行数 / 落在可见区域之外）→ 整条 CARD 不可用。
        plan.card = None
        plan.card_origin = None
        plan.card_step = None
        return False
    return True


def derive_plan(batch: ReadBatch, *, batch_size: int = 0) -> RunPlan:
    """**开局裁决**：视觉组的观测 + 本次读到的题数 → 唯一一份运行方案。

    这是「程序开始运行时的那一套判断逻辑」。它一定会给出一份**可执行**的方案：
    观测缺字段时按 :data:`~core.advance_library.PLAN_PREFERENCE` 的后备顺序
    （滚动 → 滑动）裁决，而不是把「不知道」留到运行期变成换招。

    为什么提交范围也在这里定：提交是**不可逆**的动作，而它一旦在运行期
    「看这次读到什么再决定」，就会出现「这次读到的题数变了 → 判成整卷/每题变了」
    这种漂移。开局定死之后，整卷页面不会再因为「这一屏没有提交按钮」而暂停
    （2026-09-29 真机 `logs/10ca5ee8c89e` 就是停在这儿）。
    """
    page = batch.page
    plan = RunPlan(
        total=page.total if page else None,
        current=page.current if page else None,
        raw=(batch.raw or "")[:500],
    )

    # —— 推进方式：按固定优先级裁决，选中即定死 ——
    if page is not None and page.card is not None and _card_plan(plan, page.card):
        plan.method = AdvanceMethod.CARD
        plan.reason = (
            f"画面里有题号答题卡（{page.card.cols}×{page.card.rows} 格），点题号切题"
        )
    if (
        plan.method is AdvanceMethod.UNKNOWN
        and page is not None
        and page.next_control is not None
        and _box_ok(page.next_control.box)
    ):
        plan.method = AdvanceMethod.CLICK
        plan.control_box = page.next_control.box
        plan.control_label = page.next_control.label
        label = page.next_control.label or "（无文字）"
        plan.reason = f"画面里有推进控件「{label}」，点它进下一题"
    if plan.method is AdvanceMethod.UNKNOWN:
        # 既没有题号卡、也没有控件：只能滚 / 滑。**滚动优先** —— 它自带
        # 「确认新题真的进来了」的判据，滑错了也不会静默跳题。
        scrollable = page is None or page.scrolling is not False
        plan.method = AdvanceMethod.SCROLL if scrollable else AdvanceMethod.SWIPE
        tail = (
            "，按「向下滚动 + 确认新题进来了」推进"
            if scrollable
            else "，且页面不可滚动 → 只能滑动翻页"
        )
        plan.reason = "画面上既没有推进控件、也没有题号答题卡" + tail
    if plan.method not in PLAN_PREFERENCE:  # pragma: no cover - 上面已覆盖全部可执行取值
        plan.method = AdvanceMethod.SCROLL

    # —— 提交范围：开局定死（判错的代价不对称，所以默认往「整卷」靠）——
    observed = page.submit.scope if page is not None and page.submit is not None else None
    if page is not None and page.submit is not None:
        plan.submit_box = page.submit.box
    if observed is not None:
        plan.submit_scope = observed
    elif page is not None and page.card is not None:
        # 有题号答题卡 = 多题共处一个卷面 = 不存在「每题提交」。
        plan.submit_scope = SubmitScope.PAPER
    elif (plan.total or 0) >= 2 or batch_size >= 2:
        plan.submit_scope = SubmitScope.PAPER
    else:
        plan.submit_scope = SubmitScope.QUESTION

    if page is not None and page.reason:
        plan.reason = f"{plan.reason}；模型依据：{page.reason}"
    logger.debug(
        "开局裁决：method=%s total=%s current=%s scope=%s",
        plan.method.value,
        plan.total,
        plan.current,
        plan.submit_scope.value if plan.submit_scope else None,
    )
    return plan


def all_done(page_view: object | None) -> bool:
    """观测是否**明确**说明整卷已经全部答完（收尾闸门的唯一放行条件）。

    看不出来（``UNKNOWN``）**不算做完** —— 这是全流程唯一防跳题的闸门，
    宁可停下来问人。
    """
    completed = getattr(page_view, "completed", None)
    if isinstance(completed, CompletionState):
        return completed is CompletionState.ALL_DONE
    return str(completed or "").strip().lower() == CompletionState.ALL_DONE.value


def plan_summary(plan: RunPlan) -> str:
    """一句话描述这份方案（进事件流与界面，让用户看得懂「它打算怎么走」）。"""
    strategy = strategy_for(plan.method)
    parts = [f"推进方式：{strategy.label}"]
    if plan.knows_total:
        parts.append(f"总题数：{plan.total}")
    scope = plan.submit_scope
    if scope is SubmitScope.PAPER:
        parts.append("提交范围：整卷（推迟到全部做完）")
    elif scope is SubmitScope.QUESTION:
        parts.append("提交范围：每题")
    else:  # pragma: no cover - derive_plan 一定会给出取值
        parts.append("提交范围：未知")
    return "；".join(parts)
