"""目标采集层（P11）。

把「电脑上正在运行的目标」变成可读句柄，交给现有的感知 / 执行 / 校验层。
对外只有四样东西：契约（``base``）、浏览器目标（``browsers``）、
桌面窗口目标（``windows``）、测试用靶场（``mock_source``）。

能力矩阵住在 ``core.targets``（纯逻辑，供 ``core.config`` 引用），
本包只放**需要 I/O 的实现** —— 这个拆分是为了不让 ``core.config``
反向依赖 httpx / playwright / ctypes。
"""

from __future__ import annotations

from target.base import (
    ScreenSurface,
    TargetHandle,
    TargetInfo,
    TargetKind,
    TargetSource,
    TargetUnavailableError,
)
from target.browsers import DEFAULT_DEBUG_PORT, BrowserTargetSource
from target.mock_source import MOCK_TARGET_ID, MockTargetSource
from target.windows import DesktopWindowSource

__all__ = [
    "DEFAULT_DEBUG_PORT",
    "MOCK_TARGET_ID",
    "BrowserTargetSource",
    "DesktopWindowSource",
    "MockTargetSource",
    "ScreenSurface",
    "TargetHandle",
    "TargetInfo",
    "TargetKind",
    "TargetSource",
    "TargetUnavailableError",
]
