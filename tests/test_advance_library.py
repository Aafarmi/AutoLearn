"""推进方式库（``core/advance_library.py``）的契约守门。

「怎么进入下一题」这件事只允许有固定几种做法，而方式库就是**可选项的唯一定义点**。
这几条用例把两件事钉死：库与 :class:`AdvanceMethod` **一一对应**（漏一个 = 模型说得
出、程序做不了），以及「认不出来的方式」**绝不抛异常**（模型用词偏差不该打挂任务）。
"""

from __future__ import annotations

from core.advance_library import (
    ADVANCE_LIBRARY,
    DEFAULT_METHOD_ORDER,
    PLAN_PREFERENCE,
    library_methods,
    strategy_for,
)
from core.enums import AdvanceMethod


def test_library_covers_every_advance_method() -> None:
    """方式库必须覆盖 ``AdvanceMethod`` 的**全部取值**，不多不少。"""
    assert set(ADVANCE_LIBRARY) == set(AdvanceMethod)


def test_library_methods_matches_the_enum() -> None:
    assert sorted(library_methods()) == sorted(m.value for m in AdvanceMethod)


def test_unknown_strategy_is_the_default_ladder_not_an_error() -> None:
    """认不出来一律回落 ``UNKNOWN``，**不抛异常** —— 它是被「模型说了什么」驱动的。"""
    assert strategy_for("click").method is AdvanceMethod.CLICK
    assert strategy_for("card").method is AdvanceMethod.CARD
    assert strategy_for("scroll").method is AdvanceMethod.SCROLL
    assert strategy_for("swipe").method is AdvanceMethod.SWIPE
    assert strategy_for("unknown").method is AdvanceMethod.UNKNOWN
    assert strategy_for("某种没见过的说法").method is AdvanceMethod.UNKNOWN
    assert strategy_for(None).method is AdvanceMethod.UNKNOWN


def test_plan_preference_is_card_first_and_has_no_unknown() -> None:
    """裁决优先级是**固定的**：答题卡 → 固定控件 → 滚动 → 滑动。

    顺序对应「落点有多确定」，不是「哪个更省事」：

    * ``CARD`` 的落点是**算出来的**（网格 + 题号），可验证；
    * ``CLICK`` 的落点是**模型给的**，一次观测定死；
    * ``SCROLL`` 自带「新题真的进来了没有」的判据，最稳但最慢；
    * ``SWIPE`` 是页面不可滚动时的最后一招。

    ``UNKNOWN`` **不在**这个序列里 —— 它不是一个可执行方式，
    留在里面就等于给「换招阶梯」留了个后门（2026-09-30 整条删掉）。
    """
    assert PLAN_PREFERENCE == (
        AdvanceMethod.CARD,
        AdvanceMethod.CLICK,
        AdvanceMethod.SCROLL,
        AdvanceMethod.SWIPE,
    )
    assert AdvanceMethod.UNKNOWN not in PLAN_PREFERENCE
    # 兼容别名：老代码按 ``DEFAULT_METHOD_ORDER`` 取它，语义**不再是**「阶梯」。
    assert DEFAULT_METHOD_ORDER == PLAN_PREFERENCE


def test_needs_coords_only_for_click() -> None:
    """只有「点控件」需要模型给坐标；滚动 / 滑动不是「点某个位置」，不白花调用。"""
    assert strategy_for("click").needs_coords is True
    assert strategy_for("scroll").needs_coords is False
    assert strategy_for("swipe").needs_coords is False
