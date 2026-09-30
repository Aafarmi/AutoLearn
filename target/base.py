"""目标采集层（P11）：把「电脑上正在运行的目标」变成可读句柄。

为什么单独一层
--------------
改造前，「题目从哪来」这件事散落在两处，合起来的效果是**软件只会操作自己开的
靶场**，永远看不到用户电脑上已经在跑的东西：

``core/orchestrator.py::default_page_factory``
    自己 ``launch()`` 一个浏览器，再 ``goto()`` 到一个写死的地址；
``ui/assembly.py::default_start_url``
    按 ``task_sequence`` 在「题目靶场页 / 网课靶场页」之间二选一。

这正是 2026-09-27 用户指出的根本偏差：预期是「抓取电脑上**正在运行**的程序或
网页」，实际做出来的是「自己起一个靶场自己玩」。本层补的就是缺掉的那截。

为什么不重写感知层
------------------
结论：**下游一行都不用改，只要目标仍然以 Playwright 的 ``Page`` 交付**。

题目探针签名是 ``(page, adapter, ctx)``，执行器是 ``Actuator(page, ...)``，
校验器同理。所以本层对外只做一件事 —— 产出一个 :class:`TargetHandle`，
它的 ``page`` 字段就是原来那个 ``Page``。

v0.2.0 起读题只走模型（视觉）一条通道，所以目标类型**不再影响通道选择** ——
它只决定「怎么拿到画面」：浏览器页走 CDP 附加（有 ``Page``），
原生窗口走 Win32 截图（只有 ``ScreenSurface``，没有 ``Page``）。
后者暂时还接不上读题链路，见 ``core/targets`` 与编排层的启动护栏。

生命周期与红线
--------------
:meth:`TargetSource.open` 刻意返回**异步上下文管理器**而不是直接给出句柄：
释放必须与附加严格配对。尤其是浏览器目标，收尾时只能断开 CDP 连接，
**绝不能关掉用户的标签页或浏览器** —— 用户可能正在上面干活。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, computed_field

from core.enums import ProbeName, TargetKind
from core.targets import channels_for

__all__ = [
    "ScreenSurface",
    "TargetHandle",
    "TargetInfo",
    "TargetKind",
    "TargetSource",
    "TargetUnavailableError",
]


class TargetUnavailableError(RuntimeError):
    """目标不存在 / 已关闭 / 附加失败。

    与「模型读不到题目」刻意区分开：读不到可能是页面形态问题（重读一次或
    让人工接手能救），而目标不可用是**根本没接上**，只能让人去处理。路由层据此
    返回带 ``error_code`` 的 400/409，而不是笼统的 500。
    """


class TargetInfo(BaseModel):
    """一个**候选项**的自述。只装能跨 HTTP 传的纯数据，不含任何句柄。

    ``target_id`` 由采集源定义、对采集源有意义；调用方只当它是不透明字符串。
    """

    model_config = ConfigDict(extra="forbid")

    target_id: str
    kind: TargetKind
    title: str = ""
    url: str | None = None
    #: 目标所属的宿主（浏览器标识 ``chrome`` / ``msedge``，或进程名）。
    #: 列表里做区分用 —— 同时开着 Chrome 和 Edge 时不至于分不清。
    app: str | None = None
    #: 是否当前前台。只作提示，**不做自动跟随**：P11 由用户勾选决定抓哪个。
    active: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def channels(self) -> list[ProbeName]:
        """该目标可用的读题通道。

        v0.2.0 起恒为 ``["vision"]``。字段保留是因为界面仍在列表里显示它 ——
        「这个目标怎么读」对用户是有用信息，且将来若真的恢复第二条通道，
        消费方不必改。
        """
        return sorted(channels_for(self.kind), key=lambda name: name.value)


@dataclass(slots=True)
class ScreenSurface:
    """桌面目标的「可看可点」面。

    原生窗口没有 ``Page``、也没有 ``Locator`` —— 唯一能做的两件事就是**截图**和
    **在坐标上点一下**。所以桌面目标的读与写都收口在这个小对象上，
    上层（探针 / 执行器）只依赖它，不依赖任何 Windows API。

    坐标换算刻意只用一个 ``scale`` 加一个 ``origin``：
    ``screen = origin + image * scale``。窗口截图可能是物理像素、也可能被系统
    缩放（Windows 显示缩放 125% / 150%），把换算收在一处，
    比让调用方各自乘除安全得多 —— 那种写法必然会在某个缩放下「正好偏一半」。
    """

    #: 截一张当前画面（PNG 字节）
    capture: Callable[[], Awaitable[bytes]]
    #: 在**屏幕坐标**上左键点一下
    click: Callable[[float, float], Awaitable[None]]
    #: 图像像素 → 屏幕像素的缩放比
    scale: float = 1.0
    #: 窗口左上角在屏幕上的位置
    origin: tuple[int, int] = (0, 0)
    #: 最近一次截图的方法（``printwindow`` / ``screen_grab``），用于排障
    grab_method: str = ""

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """图像坐标 → 屏幕坐标。模型给的是图像上的位置，点击要的是屏幕位置。"""
        return (
            round(self.origin[0] + x * self.scale),
            round(self.origin[1] + y * self.scale),
        )


@dataclass(slots=True)
class TargetHandle:
    """已附加的目标。**句柄的生命周期 = 一次运行的生命周期**。

    用 dataclass 而不是 pydantic，与 ``RunDeps`` 同理：这里装的是 Playwright
    页面这类活对象，过一遍校验层只会带来复制与转换的意外。
    """

    info: TargetInfo
    #: 浏览器目标的 Playwright 页面。桌面窗口为 ``None``
    page: Any | None = None
    #: 桌面目标的可看可点面。浏览器目标为 ``None``
    surface: ScreenSurface | None = None
    #: 收尾回调。**只用来断开自己建立的东西**（CDP 连接等），
    #: 绝不允许放进「关闭用户目标」的语义。
    cleanup: Callable[[], Awaitable[None]] | None = None

    @property
    def kind(self) -> TargetKind:
        return self.info.kind

    @property
    def channels(self) -> frozenset[ProbeName]:
        return channels_for(self.kind)

    def supports(self, name: ProbeName) -> bool:
        """该目标能否用这条通道。编排层在跑探针链前用它做一次护栏检查。"""
        return name in self.channels

    async def aclose(self) -> None:
        """断开自己建立的连接。幂等：重复调用不会重复收尾。"""
        cleanup, self.cleanup = self.cleanup, None
        if cleanup is not None:
            await cleanup()


class TargetSource(ABC):
    """一类目标的采集源。"""

    #: 本采集源产出的目标类型。**子类必须声明**。
    kind: ClassVar[TargetKind]

    @abstractmethod
    async def list_targets(self) -> list[TargetInfo]:
        """列出当前可附加的目标。

        **必须无副作用**：UI 上用户点一下「抓取」就会调它，反复调用不得改变
        任何东西（不得顺带启动浏览器、不得占用页面）。
        """

    @abstractmethod
    def open(self, target_id: str) -> AbstractAsyncContextManager[TargetHandle]:
        """附加到指定目标。

        目标不存在或附加不上时抛 :class:`TargetUnavailableError`。
        返回的不是句柄而是上下文管理器，是为了让**释放与附加严格配对** ——
        参见模块头的红线说明。
        """

    async def aclose(self) -> None:
        """释放采集源自身持有的资源。默认无事可做。"""
        return

    @property
    def available(self) -> bool:
        """采集源当前是否可用（如调试端口是否活着）。默认恒可用。"""
        return True
