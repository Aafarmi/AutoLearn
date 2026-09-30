"""固定的「推进方式库」（2026-09-29 加）。

为什么要有它
------------
「怎么进入下一题」这件事，在本项目里**只允许有固定几种做法** ——
点控件 / 向下滚动 / 滑动翻页 / 看不出来。原因不是洁癖，而是跳题的代价：

    每步临时想办法 → 这次点按钮、下次去滚动 → 一次滚过头 → 静默跳过好几道题

2026-09-28 的真机事故就是这么来的（`scroll` 模式下照样去找按钮，找不到就继续滚，
一路滚到 0.8 屏 × 6，题号从 3 直接跳到 16）。修法是把「用哪种方式」**在任务开始
就定死**，中途不许换招硬试 —— 而「定死」需要一个**可选项清单**，那就是本模块。

本模块与提示词的关系
--------------------
* `prompts/33-推进方式库.md` 是**给人看、也给人改的**那份清单（唯一真源）；
  训练模式（`solve/training.py`）会往它的 ``AUTOTRAIN`` 区追加实战经验。
* 本模块是**程序侧的镜像**：把清单落成可执行的分派表，让编排层不必写
  ``if method is CLICK: ... elif ...`` 这种散在各处的分支。

两边必须一致：:data:`ADVANCE_LIBRARY` 覆盖 :class:`~core.enums.AdvanceMethod`
的**全部取值**，有守门单测卡着（漏一个 = 模型说得出、程序做不了）。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.enums import AdvanceMethod

__all__ = [
    "ADVANCE_LIBRARY",
    "DEFAULT_METHOD_ORDER",
    "PLAN_PREFERENCE",
    "AdvanceStrategy",
    "library_methods",
    "library_prompt_text",
    "strategy_for",
]

#: **开局裁决的优先级**（2026-09-30 起它不再是运行期的「尝试阶梯」）。
#:
#: 旧版把 ``DEFAULT_METHOD_ORDER`` 当运行时降级阶梯用：标定失败就拿它一步一步试
#: （点击 → 滚动 → 滑动）—— 那正是用户口中的「换招硬试」。现在它只被
#: :func:`core.run_plan.derive_plan` 在**开局**读一次，用来把观测裁决成
#: **唯一**一种方式：
#:
#: 1. 有题号答题卡 → ``CARD``（直接跳到第 N 题，最确定）；
#: 2. 有「下一题」控件 → ``CLICK``；
#: 3. 都没有、页面还能滚动 → ``SCROLL``（它自带「确认新题进来了」的判据，不会跳题）；
#: 4. 都不能 → ``SWIPE``（手势，最容易一次滑过头，排最后）。
PLAN_PREFERENCE: tuple[AdvanceMethod, ...] = (
    AdvanceMethod.CARD,
    AdvanceMethod.CLICK,
    AdvanceMethod.SCROLL,
    AdvanceMethod.SWIPE,
)

#: 兼容别名：老代码 / 老测试仍按 ``DEFAULT_METHOD_ORDER`` 导入。
#: 语义已变为「开局裁决优先级」，**不再**是运行期阶梯。
DEFAULT_METHOD_ORDER: tuple[AdvanceMethod, ...] = PLAN_PREFERENCE


@dataclass(frozen=True)
class AdvanceStrategy:
    """方式库里的一条方式。

    ``needs_coords`` 是**执行层依赖**的声明：``click`` 需要模型给出坐标，
    ``scroll`` / ``swipe`` 不需要（它们不是「点某个位置」）。
    编排层据它决定要不要为了这一步再截一张图问模型 —— 不需要的方式
    绝不白花一次模型调用。
    """

    method: AdvanceMethod
    label: str
    summary: str
    needs_coords: bool

    @property
    def key(self) -> str:
        """库里的稳定标识（= 枚举值）。"""
        return self.method.value


#: **固定方式库**。取值一个都不能少，也不能多（有守门单测）。
ADVANCE_LIBRARY: dict[AdvanceMethod, AdvanceStrategy] = {
    AdvanceMethod.CLICK: AdvanceStrategy(
        method=AdvanceMethod.CLICK,
        label="点控件",
        summary="点画面上那个固定的「下一题 / 下一页 / 继续」控件（开局判定的坐标）。",
        needs_coords=True,
    ),
    AdvanceMethod.CARD: AdvanceStrategy(
        method=AdvanceMethod.CARD,
        label="点答题卡题号",
        summary="点题号答题卡里「下一题号」那一格；格子几何在开局一次算好，之后按题号推算。",
        needs_coords=True,
    ),
    AdvanceMethod.SCROLL: AdvanceStrategy(
        method=AdvanceMethod.SCROLL,
        label="向下滚动",
        summary="下一题就在下方：滚一步 → 读一屏 → 确认新题进来了才算到位。",
        needs_coords=False,
    ),
    AdvanceMethod.SWIPE: AdvanceStrategy(
        method=AdvanceMethod.SWIPE,
        label="滑动翻页",
        summary="没有可点的控件与题号卡，靠手势翻页（整屏一张答题卡那类）。",
        needs_coords=False,
    ),
    AdvanceMethod.UNKNOWN: AdvanceStrategy(
        method=AdvanceMethod.UNKNOWN,
        label="未裁决",
        summary="只在开局裁决之前出现；裁决一定会给出一个可执行的方式。",
        needs_coords=False,
    ),
}


def strategy_for(method: AdvanceMethod | str | None) -> AdvanceStrategy:
    """取一条方式。认不出来一律给 :attr:`AdvanceMethod.UNKNOWN` 那条。

    **不抛异常**是刻意的：它是被「模型到底说了什么」驱动的调用点，
    抛出去就等于让一个用词偏差把整个任务打挂。
    """
    try:
        key = method if isinstance(method, AdvanceMethod) else AdvanceMethod(str(method))
    except ValueError:
        key = AdvanceMethod.UNKNOWN
    return ADVANCE_LIBRARY[key]


def library_methods() -> list[str]:
    """库里全部方式的 ``key``（与 :class:`AdvanceMethod` 一一对应）。"""
    return [method.value for method in AdvanceMethod]


def library_prompt_text() -> str:
    """读 ``prompts/33-推进方式库.md``。**只给训练模式与文档生成用**。

    它**不进模型请求**：读图的系统提示词是 ``10-视觉组.md`` + ``00-共享契约.md``，
    而「该选哪一条方式」由程序侧的 :func:`core.run_plan.derive_plan` 按
    :data:`PLAN_PREFERENCE` 裁决 —— 模型不参与选方式，所以这份库也就没有
    「作为提示词喂给模型」的用途了。这里读它只是为了在训练模式里
    把「当前的经验库长什么样」交给总结用的模型。
    """
    from solve.prompt_files import LIBRARY_PROMPT_FILE, load_prompt

    return load_prompt(LIBRARY_PROMPT_FILE)
