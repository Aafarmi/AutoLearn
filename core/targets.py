"""目标常量（P11，v0.2.0 精简）。

原先这里是一张「目标类型 → 可用感知通道」的能力矩阵，存在的理由只有一个：
让「DOM 优先 / 模型优先」这个选择**不在真空里**做 —— 原生桌面窗口没有 DOM，
在界面上摆一个「DOM 优先」只会让用户配出一个必然失败的运行。

v0.2.0 起程序只使用模型读页面，题目通道只剩视觉一条，「通道选择」这个概念
本身没有了，能力矩阵随之删除。目标类型仍然保留，但它现在只影响
**怎么拿到画面**（浏览器页走 CDP 附加 / 原生窗口走 Win32 截图），
不再影响「信哪条通道」。

本模块是**纯逻辑、零 I/O、零第三方依赖**，因此可以被 ``core.config`` 直接引用
（``core/config.py`` 不能反向依赖要拉 httpx / playwright 的 ``target/`` 包）。
真正的采集实现住在 ``target/`` 包里。
"""

from __future__ import annotations

from core.enums import ProbeName, TargetKind

__all__ = [
    "DEFAULT_DEBUG_PORT",
    "QUESTION_CHANNEL",
    "channels_for",
]

#: 浏览器调试端口默认值。选 9222 是因为它是 Chrome 社区约定俗成的那个
#: （教程与别的工具大多默认它），用户自己手动带参数启动时最可能撞上，
#: 撞上就能直接用。
#:
#: 放在这里而不是 ``target/browsers.py``，是为了让 ``core.config`` 能引用它
#: 而不必反向依赖 ``target``（后者要拉 httpx / playwright）。
DEFAULT_DEBUG_PORT = 9222

#: 读题用的**唯一**通道（v0.2.0）。留成常量而不是散落的字面量，
#: 是为了让「只有一条通道」这件事在一处可见、可被测试钉住。
QUESTION_CHANNEL = ProbeName.VISION


def channels_for(kind: TargetKind) -> frozenset[ProbeName]:
    """该目标类型可用的题目通道。

    v0.2.0 起两条目标类型都是 :data:`QUESTION_CHANNEL` ——
    保留这个函数是为了让调用方（校验层 / 界面）不必自己写死一条通道，
    将来若真的重新引入第二条，改动点仍然只有这里。
    """
    del kind  # 通道不再随目标类型变化；入参保留是为了调用点语义清晰
    return frozenset({QUESTION_CHANNEL})
