"""开局判定（``core/run_plan.py``）：把视觉组的观测裁决成**唯一一份**运行方案。

这一层为什么值得逐条钉死
----------------------
用户对上一版的判词是「下一题处理仍然存在严重问题」，给出的方法是「程序开始运行时
需要有一套判断逻辑，一旦判定好后后续的全部按照这套逻辑」。那套判断逻辑就是本模块：
它把「这一屏长什么样」的**观测**裁决成一种推进方式、一份落点几何、一个提交范围。

判错的代价是不对称的，所以每条判据都对应一个已经发生过的事故：

* 该点答题卡却判成滚动 —— 真实作业页**没有**「下一题」按钮，做到一半就推不动；
* 答题卡格子按 ``origin + index * step`` 算 —— 第 6 题落到卡片外面，点下去是
  **一次真实的误点**（第一版就是这个 bug，由「第 17 题落在 x=0.66」暴露）；
* 该整卷提交却判成每题提交 —— 做完第 1 题就去交卷（2026-09-28 真机
  ``region_mad=0.06``，按钮当时被禁用，点了毫无反应）；
* 不知道总数却当成「到齐了」—— 后面所有题静默不作答。

本文件是**纯单测**：不起浏览器、不发网络、不读盘。裁决只吃一份
:class:`~core.models.PageView`，所以这些判据在任何环境里都跑得动。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.advance_library import PLAN_PREFERENCE
from core.enums import AdvanceMethod, CompletionState, SubmitScope
from core.models import PageCard, PageView, RunPlan
from core.run_plan import (
    CARD_GRID_TOLERANCE,
    all_done,
    card_cell_box,
    derive_plan,
    grid_geometry,
    plan_summary,
)
from tests.orchestrator_helpers import SUBMIT_BOX, make_page_view, make_question, make_read_batch

NormBox = tuple[float, float, float, float]

#: 手算用的答题卡：5 列 × 2 行，格宽 0.1 / 格高 0.2，整块从 ``(0.1, 0.1)`` 起。
CARD_BOX: NormBox = (0.1, 0.1, 0.5, 0.4)
CARD_COLS = 5
CARD_ROWS = 2

#: 「下一题 / 下一页」控件的框。中心 ``(0.89, 0.955)``。
NEXT_BOX: NormBox = (0.82, 0.93, 0.14, 0.05)


# --------------------------------------------------------------------------- #
# 手算几何：期望值全部由这里算出来，不在测试正文里重复一遍公式
# --------------------------------------------------------------------------- #
def _card(
    *,
    box: NormBox = CARD_BOX,
    cols: int = CARD_COLS,
    rows: int = CARD_ROWS,
    current_box: NormBox | None = None,
    next_box: NormBox | None = None,
) -> PageCard:
    return PageCard(box=box, cols=cols, rows=rows, current_box=current_box, next_box=next_box)


def _cell_center(
    index: int,
    *,
    box: NormBox = CARD_BOX,
    cols: int = CARD_COLS,
    rows: int = CARD_ROWS,
) -> tuple[float, float]:
    """第 ``index`` 格的**中心**（0 基、行优先）。"""
    row, col = divmod(index, cols)
    step_x, step_y = box[2] / cols, box[3] / rows
    return (box[0] + (col + 0.5) * step_x, box[1] + (row + 0.5) * step_y)


def _cell_box(
    index: int,
    *,
    box: NormBox = CARD_BOX,
    cols: int = CARD_COLS,
    rows: int = CARD_ROWS,
) -> NormBox:
    """第 ``index`` 格的落点框（与 :func:`core.run_plan.card_cell_box` 同一形状）。"""
    center_x, center_y = _cell_center(index, box=box, cols=cols, rows=rows)
    width, height = box[2] / cols * 0.8, box[3] / rows * 0.8
    return (center_x - width / 2.0, center_y - height / 2.0, width, height)


def _center(box: NormBox) -> tuple[float, float]:
    return (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)


def _inside(box: NormBox, outer: NormBox) -> bool:
    return (
        outer[0] <= box[0]
        and outer[1] <= box[1]
        and box[0] + box[2] <= outer[0] + outer[2]
        and box[1] + box[3] <= outer[1] + outer[3]
    )


def _derive(page: PageView | None, *, batch_size: int = 1, questions: int = 1) -> RunPlan:
    """走**真的**裁决：喂一份观测 + 这次读到的题数。"""
    batch = make_read_batch(*(make_question(index) for index in range(1, questions + 1)), page=page)
    return derive_plan(batch, batch_size=batch_size)


# --------------------------------------------------------------------------- #
# 1. 几何：网格原点、步距、按行换行
# --------------------------------------------------------------------------- #
def test_grid_geometry_calibrates_on_the_current_cell() -> None:
    """网格原点按 ``current_box``（模型明确指出的当前题号格）校准。

    为什么不能只用外框等分：模型给的答题卡外框常常含标题与留白，直接等分会有系统偏差，
    几十格之后偏差就够点到隔壁题。而「当前题号格」是它明确指出的一个锚点。
    """
    geometry = grid_geometry(_card(current_box=_cell_box(0)))

    assert geometry is not None
    origin, step = geometry
    assert step == pytest.approx((CARD_BOX[2] / CARD_COLS, CARD_BOX[3] / CARD_ROWS))
    assert origin == pytest.approx(_cell_center(0))


def test_grid_geometry_gives_up_without_a_usable_grid() -> None:
    """缺列数 / 行数 / 可用外框 → **返回 None**（算不出来就不猜）。

    猜出来的网格会让每一格都偏移，而偏移的表现是「点错了题」—— 停下来的代价小得多。
    """
    for card in (_card(cols=0), _card(rows=0), _card(box=(0.1, 0.1, 0.0, 0.4))):
        assert grid_geometry(card) is None


def test_card_grid_wraps_to_the_next_row() -> None:
    """答题卡**按行换行**：5 列时第 6 题在第 2 行第 1 列。

    这条钉死第一版的 bug：``origin + index * step`` 把第 6 格算到
    ``0.15 + 5 × 0.1 = 0.65``，而卡片只到 ``x = 0.6`` —— 落在卡片外面，
    点下去就是一次真实的误点（真机上由「第 17 题落在 x=0.66」暴露）。
    """
    plan = _derive(make_page_view(card=_card(current_box=_cell_box(0)), current=1))
    assert plan.method is AdvanceMethod.CARD

    target = plan.card_target(6)
    assert target is not None, "第 6 格必须算得出来（它就在第二行）"
    assert target == pytest.approx(_cell_box(5))
    assert _center(target) == pytest.approx(_cell_center(5))
    assert _center(target)[0] == pytest.approx(CARD_BOX[0] + 0.5 * CARD_BOX[2] / CARD_COLS), (
        "第 6 题必须回到**第 1 列**"
    )
    assert _center(target)[0] != pytest.approx(_cell_center(0)[0] + 5 * CARD_BOX[2] / CARD_COLS), (
        "旧公式（origin + index × step）算出的 0.65 是错的"
    )
    assert _inside(target, CARD_BOX), "落点必须在卡片内"


def test_card_anchor_is_current_minus_the_matched_cell() -> None:
    """网格第 0 格对应的题号 = ``current - 匹配到的格子下标``（题号不一定从 1 开始）。

    真实卷面上「第 1 格」未必是第 1 题（可能从上次的断点接着显示），
    所以锚点必须由两个观测点反推，不能写死 1。
    """
    plan = _derive(make_page_view(card=_card(current_box=_cell_box(2)), current=8), batch_size=0)

    assert plan.method is AdvanceMethod.CARD
    assert plan.card_anchor == 6, "当前第 8 题落在第 3 格 → 第 0 格是第 6 题"
    assert plan.card_target(6) == pytest.approx(_cell_box(0))
    assert plan.card_target(9) == pytest.approx(_cell_box(3)), "第 9 题 = 第 4 格"


def test_target_of_the_current_number_matches_the_observed_cell() -> None:
    """``card_target(current)`` 必须与模型指出的当前格基本重合。

    这是「几何算得对不对」的自检：算出来的当前格与观测对不上，说明校准没生效，
    后面每一格都会带着同样的偏差。
    """
    card = _card(current_box=_cell_box(0))
    plan = _derive(make_page_view(card=card, current=1))

    assert plan.method is AdvanceMethod.CARD
    assert plan.card_target(1) == pytest.approx(_cell_box(0))


def test_next_box_pins_the_target_even_without_the_current_number() -> None:
    """模型直接指出「下一题号格」时，**题号观测可以缺失**，落点照样钉在那一格上。

    为什么要有这条：真实卷面常常看不到「第几题」（题干画在 canvas 上、题库不给编号），
    旧实现于是整套几何都拿不到 —— 答题卡明明就在眼前却判成滚动推进。
    现在改成：``next_box`` 是模型对「下一步点哪儿」的**正面陈述**，用它定锚点，
    于是 ``card_target(起始题号 + 1)`` 的落点必须**正好**落在它上面（误差远小于半格）。
    """
    next_box = _cell_box(4)
    plan = _derive(make_page_view(card=_card(current_box=None, next_box=next_box)))

    assert plan.method is AdvanceMethod.CARD
    assert plan.numbers_known is False, "没观测到题号 → 不许说题号可信"
    assert plan.card_start_number == 1, "题号缺失时按「从第 1 题起」算"
    assert plan.card_anchor == 2 - 4, "锚点 = (起始题号 + 1) − next_box 命中的格子下标"

    target = plan.card_target(2)
    assert target is not None
    assert target == pytest.approx(next_box), "下一题的落点框就是模型指出的那一格"
    assert _center(target) == pytest.approx(_center(next_box), abs=1e-6)


def test_next_box_outside_the_card_still_falls_back_as_a_whole() -> None:
    """``next_box`` 指到卡片外面（模型看错了）→ 仍要过「落点算得出来吗」那一关 → 整体回退。

    这是 ``next_box`` 新路的**安全阀**：既然锚点改成模型给的，就必须假设它会错。
    错的时候不能让 CARD 带着一份「锚点指向卡片外」的几何继续走 ——
    拿它算出来的坐标落在卡片外面，点下去就是一次真实误点。
    """
    # 中心 (0.9, 0.9)：远在卡片 (0.1, 0.1, 0.5, 0.4) 之外
    card = _card(current_box=None, next_box=(0.86, 0.82, 0.08, 0.16))

    plan = _derive(make_page_view(card=card))

    assert plan.method is AdvanceMethod.SCROLL, "CARD 不成立 → 退回滚动"
    assert plan.card is None and plan.card_step is None and plan.card_origin is None
    assert plan.card_target(2) is None


@pytest.mark.parametrize(
    ("current", "trusted", "start"),
    [
        pytest.param(None, False, 1, id="no-observation"),
        pytest.param(0, False, 1, id="zero"),
        pytest.param(-3, False, 1, id="negative"),
        pytest.param(1, True, 1, id="from-the-first-question"),
        pytest.param(16, True, 16, id="resumed-from-16"),
    ],
)
def test_number_trust_and_card_start_number(
    current: int | None, trusted: bool, start: int
) -> None:
    """``numbers_known`` 与 ``card_start_number`` 是同一条契约的两面。

    为什么 ``0`` 与负数也算「不知道」：题号从 1 起，``current=0`` 只可能是模型没填或
    填错；把它当真会让 ``card_target`` 算出一个负号题号的格子 —— 点在答题卡之外。
    ``card_start_number`` 单独开一个属性，是为了不让每个调用方各写一遍
    「不是 ``None`` 且大于 0」—— 漏掉一半（只判 ``None``）就会放过 ``0``。
    """
    plan = RunPlan(current=current)

    assert plan.numbers_known is trusted
    assert plan.card_start_number == start


def test_tolerance_boundary_is_exactly_ten_percent_of_the_card() -> None:
    """``CARD_GRID_TOLERANCE`` 的边界：**正好**在容差内 → 有值；稍微超出 → ``None``。

    为什么要有容差：模型给的外框与网格边界总有系统偏差，一刀切会把可用的格子丢掉。
    为什么容差必须小：它一旦松到接近半格，算出来的坐标就可能落在卡片之外 ——
    那是一次真实误点。所以边界值本身要钉死，而不是「大概差不多」。
    """
    card = PageCard(box=(0.2, 0.2, 0.2, 0.2), cols=2, rows=2)
    step = (0.1, 0.1)
    # 第 3 题 = 第 2 行第 1 列，它的左边界 = 原点 x - 半格宽。
    # 让这个左边界**正好**等于「卡片左边界 - 容差」，就是边界上的那一格。
    edge = card.box[0] - card.box[2] * CARD_GRID_TOLERANCE
    origin_x = edge + step[0] * 0.8 / 2.0

    inside = RunPlan(card=card, card_step=step, card_origin=(origin_x, 0.25), card_anchor=1)
    assert card_cell_box(inside, 3) is not None, "正好在容差内必须算得出来"

    outside = inside.model_copy(update={"card_origin": (origin_x - 0.0001, 0.25)})
    assert card_cell_box(outside, 3) is None, "超出容差一点点也必须如实返回 None"


# --------------------------------------------------------------------------- #
# 2. 裁决表：CARD → CLICK → SCROLL → SWIPE
# --------------------------------------------------------------------------- #
def test_click_is_chosen_when_only_a_control_is_observed() -> None:
    """只有「下一题」控件 → CLICK，且 ``control_box`` **原样**是观测到的那个框。

    原样传递是关键：归一化框 → 像素的换算只应在执行层做一次；编排层自己再算一遍
    就会与执行层漂移，表现是「点得偏了半个屏」。
    """
    plan = _derive(make_page_view(next_box=NEXT_BOX, next_label="下一页"))

    assert plan.method is AdvanceMethod.CLICK
    assert plan.control_box == pytest.approx(NEXT_BOX)
    assert plan.control_label == "下一页"
    assert plan.card is None


@pytest.mark.parametrize(
    "card",
    [
        pytest.param(_card(cols=0), id="cols=0"),
        pytest.param(_card(rows=0), id="rows=0"),
        pytest.param(_card(box=(0.1, 0.1, 0.0, 0.4)), id="box-without-area"),
    ],
)
def test_broken_card_is_not_adopted_and_leaves_no_half_geometry(card: PageCard) -> None:
    """``cols=0`` / ``rows=0`` / 没有可用外框 → **不采用 CARD**，并且**整体回退**。

    「整体回退」是刻意的：只把 ``method`` 改掉、却留下 ``card`` / ``card_step``，
    下游就会拿到一份「方法不是 CARD、却带着答题卡几何」的奇怪状态 ——
    这种半套几何最容易在下一次改动里被误用。
    """
    plan = _derive(make_page_view(card=card, current=1))

    assert plan.method is AdvanceMethod.SCROLL, "没有别的观测 → 退回滚动"
    assert plan.card is None and plan.card_step is None and plan.card_origin is None
    assert plan.card_target(2) is None


def test_broken_card_falls_through_to_the_click_control() -> None:
    """答题卡用不了、但画面上有控件 → 按 :data:`PLAN_PREFERENCE` 退到 CLICK。

    优先级是「可选项的唯一定义点」，不是随手写的 if 顺序：这里钉住
    「CARD 不成立时**接着往下取**」，而不是直接跳到滚动。
    """
    plan = _derive(make_page_view(card=_card(cols=0), next_box=NEXT_BOX, current=1))

    assert plan.method is AdvanceMethod.CLICK
    assert plan.card is None
    assert plan.control_box == pytest.approx(NEXT_BOX)


def test_card_wins_over_a_click_control() -> None:
    """两个观测都在 → 取 CARD（优先级里的第一个）。

    有题号答题卡的页面上，「下一题」控件常常是灰的 / 装饰性的；点题号才是真正能切题的
    那一个。优先级固定下来，等于把「哪种更可靠」这个判断从模型手里拿回程序。
    """
    plan = _derive(make_page_view(card=_card(current_box=_cell_box(0)), next_box=NEXT_BOX, current=1))

    assert plan.method is AdvanceMethod.CARD
    assert plan.method is PLAN_PREFERENCE[0]
    assert plan.control_box is None, "方案里只留被选中的那一种落点"


def test_card_cell_outside_the_visible_area_is_not_adopted() -> None:
    """「下一题号那一格」算出来已经**超出答题卡可见区域** → 不采用 CARD。

    答题卡内部可能是可滚动的：题号离当前很远时那一格并不在画面上，按算术算出来的
    坐标落在卡片外面 —— 点它就是点空白、甚至点到别的控件。容差只有
    :data:`CARD_GRID_TOLERANCE`（10%），这里手算一个刚好越界的情形。

    几何：2 列 × 2 行、格宽 0.1。模型把「当前格」（第 2 格）指得**偏左 0.04** →
    整个网格跟着左移，于是「下一格」（第 3 格，第 2 行第 1 列）的左边界落到
    ``0.21 - 0.04 = 0.17``，比「卡片左边界 - 容差」``0.2 - 0.02 = 0.18`` 还靠左。
    """
    card = PageCard(box=(0.2, 0.2, 0.2, 0.2), cols=2, rows=2, current_box=(0.29, 0.23, 0.04, 0.04))
    plan = _derive(make_page_view(card=card, current=1))

    assert plan.method is AdvanceMethod.SCROLL, "算不出落点就不许采用 CARD"
    assert plan.card is None
    assert plan.card_target(2) is None


@pytest.mark.parametrize(
    ("scrolling", "expected"),
    [
        pytest.param(None, AdvanceMethod.SCROLL, id="unknown-scrollability"),
        pytest.param(True, AdvanceMethod.SCROLL, id="scrollable"),
        pytest.param(False, AdvanceMethod.SWIPE, id="not-scrollable"),
    ],
)
def test_scroll_is_preferred_over_swipe(scrolling: bool | None, expected: AdvanceMethod) -> None:
    """没有控件、没有答题卡 → 能滚就滚；只有确认「不可滚动」才滑动。

    滚动优先不是因为更简单，而是它**自带判据**（滚一步读一屏，新题真进来了才算到位）；
    滑动是最后手段，因为它一次就翻过一整屏，判错了代价最大。
    """
    plan = _derive(make_page_view(scrolling=scrolling))

    assert plan.method is expected
    assert plan.method in PLAN_PREFERENCE


def test_no_observation_still_yields_an_executable_plan() -> None:
    """``page is None``（模型没回观测块）→ **仍然**给一份可执行方案，绝不 UNKNOWN。

    「不知道这一屏长什么样」不能在运行期变成「四招都试一遍」——那正是
    「一次跳过十几道题」的成因。给一个确定的、自带校验的方式（滚动）比留个未知状态安全。
    """
    plan = _derive(None, batch_size=1)

    assert plan.method is AdvanceMethod.SCROLL
    assert plan.method in PLAN_PREFERENCE
    assert plan.total is None and plan.knows_total is False

    wide = _derive(None, batch_size=3)
    assert wide.submit_scope is SubmitScope.PAPER, "一屏多题是整卷的结构性证据"


def test_derive_plan_never_returns_unknown() -> None:
    """任何观测组合下裁决都会给出可执行方式（CARD / CLICK / SCROLL / SWIPE）。

    ``UNKNOWN`` 只允许出现在「还没裁决」的那一刻。运行期拿到 ``UNKNOWN``
    等于没有逻辑可用，而「没有逻辑就动手」在这个项目里是不可接受的。
    """
    pages: list[PageView | None] = [
        None,
        make_page_view(),
        make_page_view(scrolling=False),
        make_page_view(card=_card(cols=0)),
        make_page_view(next_box=NEXT_BOX),
        make_page_view(card=_card(current_box=_cell_box(0)), current=1, next_box=NEXT_BOX),
        make_page_view(card=_card(current_box=_cell_box(0)), current=1, scrolling=False),
    ]
    for page in pages:
        plan = _derive(page)
        assert plan.method is not AdvanceMethod.UNKNOWN, f"观测 {page!r} 裁决出了 UNKNOWN"
        assert plan.method in PLAN_PREFERENCE


# --------------------------------------------------------------------------- #
# 3. 提交范围：开局定死，默认往「整卷」靠
# --------------------------------------------------------------------------- #
def test_submit_scope_observed_by_the_model_wins() -> None:
    """模型说了提交范围就听它的 —— 这是**唯一**能区分「本题提交 / 整卷提交」的观测。

    按结构证据改判会出人命：整卷页面被判成「每题提交」= 做完第 1 题就交卷，
    而提交是本项目唯一不可逆的动作。
    """
    for scope in (SubmitScope.PAPER, SubmitScope.QUESTION):
        page = make_page_view(card=_card(current_box=_cell_box(0)), current=1, submit_scope=scope)
        plan = _derive(page)

        assert plan.submit_scope is scope, f"观测说 {scope} 时不该按结构证据改判"


def test_submit_scope_falls_back_to_the_card_evidence() -> None:
    """观测没说范围，但画面上有答题卡 → 整卷。

    有题号答题卡 = 多道题共处一个卷面 = 不存在「每题提交」这回事。
    """
    plan = _derive(make_page_view(card=_card(current_box=_cell_box(0)), current=1))

    assert plan.submit_scope is SubmitScope.PAPER


def test_submit_scope_falls_back_to_the_total_and_the_batch_size() -> None:
    """没有观测时按结构证据判：总数 ≥ 2 或一屏 ≥ 2 题 → 整卷；否则每题。

    默认往「整卷」靠是刻意的：判成每题的代价是**做完第一题就交卷**（不可逆），
    判成整卷最坏只是推迟到收尾（可见、可救）。
    """
    two_total = _derive(make_page_view(total=2))
    assert two_total.submit_scope is SubmitScope.PAPER

    two_batch = _derive(make_page_view(total=1), batch_size=2)
    assert two_batch.submit_scope is SubmitScope.PAPER

    single = _derive(make_page_view(total=1), batch_size=1)
    assert single.submit_scope is SubmitScope.QUESTION


def test_submit_box_comes_from_the_observation() -> None:
    """提交框原样进方案（收尾整卷提交要用它）；没观测到就是 ``None``，不猜。"""
    plan = _derive(make_page_view(submit_box=SUBMIT_BOX, submit_scope=SubmitScope.PAPER), batch_size=0)

    assert plan.submit_box == pytest.approx(SUBMIT_BOX)
    assert plan.submit_scope is SubmitScope.PAPER

    blank = _derive(make_page_view(submit_box=None, submit_scope=None), batch_size=0)
    assert blank.submit_box is None


# --------------------------------------------------------------------------- #
# 4. 收工判据与摘要
# --------------------------------------------------------------------------- #
def test_all_done_requires_an_explicit_all_done() -> None:
    """收尾闸门唯一的放行条件是**明确说「全部做完」**；「看不出来」不算做完。

    把 UNKNOWN 当成做完 = 后面所有题都不再作答，而且界面上看不出来 ——
    两侧代价不对称，所以默认方向必须是「不放行」。
    """
    assert all_done(make_page_view(completed=CompletionState.ALL_DONE)) is True
    assert all_done(make_page_view(completed=CompletionState.NOT_DONE)) is False
    assert all_done(make_page_view(completed=CompletionState.UNKNOWN)) is False
    assert all_done(SimpleNamespace(completed=None)) is False, "字段缺失 = 没观测到"
    assert all_done(None) is False, "连观测都没有 → 未确认"


@pytest.mark.parametrize("total", [None, 0, -1, -20])
def test_unknown_total_never_counts_as_reached(total: int | None) -> None:
    """不知道总数（``None`` / ``0`` / 负数）→ ``knows_total`` 为假，``reached_total`` **永远**为假。

    拿「不知道」当「做完了」= 静默跳掉后面所有题，这是本项目最贵的一种错。
    """
    plan = RunPlan(total=total)

    assert plan.knows_total is False
    for done in (0, 1, 3, 99, 10_000):
        assert plan.reached_total(done) is False, f"total={total!r} done={done}"


def test_reached_total_boundaries() -> None:
    """总数可用时的边界：刚好到、超了都算到齐；差一道就不算。"""
    plan = RunPlan(total=3)

    assert plan.knows_total is True
    assert plan.reached_total(0) is False
    assert plan.reached_total(2) is False
    assert plan.reached_total(3) is True
    assert plan.reached_total(4) is True, "超了更不能当成没做完"


def test_summary_names_the_method_the_total_and_the_scope() -> None:
    """摘要进事件流与界面：用户要能一眼看懂「它打算怎么走」。

    三样都不能少 —— 少了推进方式就看不出它凭什么这么推；少了提交范围就不知道
    什么时候会交卷（而提交不可逆）。
    """
    card_plan = _derive(make_page_view(card=_card(current_box=_cell_box(0)), current=1, total=20))
    summary = plan_summary(card_plan)
    assert "点答题卡题号" in summary
    assert "总题数：20" in summary
    assert "整卷" in summary

    click_plan = _derive(make_page_view(next_box=NEXT_BOX, submit_scope=SubmitScope.QUESTION))
    click_summary = plan_summary(click_plan)
    assert "点控件" in click_summary
    assert "每题" in click_summary
    assert "总题数" not in click_summary, "不知道总数时不该出现「总题数」（那是猜的）"

    swipe_plan = _derive(make_page_view(scrolling=False))
    assert "滑动翻页" in plan_summary(swipe_plan)

    scroll_plan = _derive(make_page_view())
    assert "向下滚动" in plan_summary(scroll_plan)
