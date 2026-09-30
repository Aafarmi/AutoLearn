"""桌面窗口采集：抓**正在运行的应用程序**（P11 桌面侧）。

浏览器目标靠 CDP 拿到可截可点的 ``Page``，原生程序什么都没有 ——
能做的只有两件事：**截一张窗口画面**，以及**在某个坐标上点一下**。
所以本模块的产出是一个 :class:`~target.base.ScreenSurface`（截图 + 点击），
而不是页面。

读题方式的事实
--------------
v0.2.0 起程序只使用模型读页面，**浏览器页与原生窗口走的是同一条视觉通道**：

    两边都是「截一张画面 → 交给模型读题 → 按模型给的坐标点」。
    原生窗口的差别只在于截图来源是 Win32 而不是 Playwright。

所以「用视觉模型识别题目」在这里不是兜底，是主路径。

为什么全部用 ``ctypes``
-----------------------
``win32gui`` / ``pyautogui`` / ``mss`` 都要新增依赖，而本项目对依赖是收紧的
（见 MEMORY：技术栈不得随意扩张）。枚举窗口、PrintWindow 截图、SendInput 点击
这三件事用 ``ctypes`` 直接调 user32/gdi32 都能做到，且少一层依赖就少一处
随依赖漂移而失效的风险。

DPI 缩放：最容易被忽略的一条
-----------------------------
Windows 显示缩放（125% / 150%）下，「窗口像素」与「屏幕坐标」不是 1:1。
本模块在**导入时就**把进程设成 per-monitor DPI aware，这样
``GetWindowRect`` 与截图拿到的都是同一套物理像素；即便如此，
换算仍然集中收在 :meth:`ScreenSurface.to_screen` 一处，不让调用方各自乘除 ——
那种写法必然会在某个缩放下「正好偏一半」。
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from ctypes import wintypes
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from core.enums import TargetKind
from target.base import (
    ScreenSurface,
    TargetHandle,
    TargetInfo,
    TargetSource,
    TargetUnavailableError,
)

__all__ = [
    "DesktopWindowSource",
    "WindowRef",
    "capture_window",
    "focus_window",
    "list_windows",
    "screen_size",
    "send_click",
]

logger = logging.getLogger(__name__)

#: 目标 ID 前缀。带上它，日志与配置里一眼能看出这是窗口而不是标签页。
_WIN_ID_PREFIX = "win:"

#: 过滤掉太小的窗口：托盘气泡、输入法候选框、各种浮层都不是「一个应用」。
_MIN_W, _MIN_H = 320, 240

#: 桌面外壳与系统浮层。它们的标题要么为空要么是系统文案，抓了没意义。
_SHELL_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow",
    "ApplicationFrameWindow",  # UWP 宿主，子窗口才是真身；这里先不纠结
    "SysShadow",
    "ForegroundStaging",
    "TaskListThumbnailWnd",
}

#: Win32 三个库的句柄。非 Windows 上为 ``None``，此时本模块的功能不可用，
#: 但**导入本身必须成功** —— 否则 ``target/__init__`` 会在别的平台上炸掉，
#: 连带把只想要浏览器目标的那条路也堵死。
_user32: Any = None
_gdi32: Any = None
_kernel32: Any = None

if os.name == "nt":  # pragma: no cover - 非 Windows 上本模块不可用
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _declare_prototypes() -> None:
    """声明 Win32 函数的参数与返回类型。

    **这一步不能省。** ctypes 默认把 Python int 当 C ``int``（32 位）处理，
    而 ``HWND`` / ``HANDLE`` / ``HDC`` 在 64 位 Windows 上都是指针宽度 ——
    不声明的话句柄会被**静默截断**，表现为「枚举出来的窗口点进去都是无效句柄」
    或截图返回空，且完全不报错。
    """
    if _user32 is None or _gdi32 is None or _kernel32 is None:  # pragma: no cover
        return
    from ctypes import POINTER, c_int, c_void_p

    u, g, k = _user32, _gdi32, _kernel32

    u.IsWindow.argtypes = [wintypes.HWND]
    u.IsWindow.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsIconic.restype = wintypes.BOOL
    u.GetForegroundWindow.restype = wintypes.HWND
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, c_int]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = c_int
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, c_int]
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, c_int]
    u.GetWindowRect.argtypes = [wintypes.HWND, POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, POINTER(wintypes.DWORD)]
    u.GetWindowDC.argtypes = [wintypes.HWND]
    u.GetWindowDC.restype = wintypes.HDC
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    u.PrintWindow.restype = wintypes.BOOL
    u.SetCursorPos.argtypes = [c_int, c_int]
    u.mouse_event.argtypes = [wintypes.DWORD] * 5
    u.GetSystemMetrics.argtypes = [c_int]
    u.SetProcessDpiAwarenessContext.argtypes = [c_void_p]
    u.SetProcessDpiAwarenessContext.restype = wintypes.BOOL

    g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g.CreateCompatibleDC.restype = wintypes.HDC
    g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, c_int, c_int]
    g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g.SelectObject.restype = wintypes.HGDIOBJ
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.DeleteDC.argtypes = [wintypes.HDC]
    g.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        c_void_p,
        c_void_p,
        wintypes.UINT,
    ]
    g.GetDIBits.restype = c_int

    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        POINTER(wintypes.DWORD),
    ]
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]


#: 导入即生效（比 DPI 更早，因为 DPI 那句本身也要用已声明原型的 user32）。
_declare_prototypes()


def _ensure_dpi_aware() -> str:
    """尽力把本进程设成 per-monitor DPI aware。返回实际达成的级别（供排障）。

    不设的后果：在 125% / 150% 缩放下，``GetWindowRect`` 给的是逻辑像素，
    而 PrintWindow 截出来的是物理像素 —— 两者混用会让点击坐标系统性偏移。
    """
    if _user32 is None:
        return "unsupported"
    # -4 = PER_MONITOR_AWARE_V2（Win10 1703+）
    with suppress(Exception):
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per_monitor_v2"
    # 退回旧接口：2 = PROCESS_PER_MONITOR_DPI_AWARE
    with suppress(Exception):
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per_monitor"
    with suppress(Exception):
        if _user32.SetProcessDPIAware():
            return "system_aware"
    return "none"


#: 导入即生效。放在模块级是刻意的：DPI 感知必须在**任何**窗口操作之前设好，
#: 晚设会留下已经按旧 DPI 缓存的窗口尺寸。
DPI_AWARENESS = _ensure_dpi_aware()


@dataclass(frozen=True, slots=True)
class WindowRef:
    """一个可抓的顶层窗口。"""

    hwnd: int
    title: str
    #: 窗口在屏幕上的矩形（物理像素）
    rect: tuple[int, int, int, int]
    pid: int
    process: str

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    @property
    def target_id(self) -> str:
        return f"{_WIN_ID_PREFIX}{self.hwnd}"


def _process_name(pid: int) -> str:
    """取进程名（不含路径）。失败返回 ``pid:<n>``，不抛异常。"""
    if _kernel32 is None or not pid:
        return f"pid:{pid}"
    # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return f"pid:{pid}"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return f"pid:{pid}"
    finally:
        with suppress(Exception):
            _kernel32.CloseHandle(handle)


def _iter_windows() -> list[WindowRef]:
    if _user32 is None:
        return []

    refs: list[WindowRef] = []
    own_pid = os.getpid()

    # ctypes 的 WINFUNCTYPE 是**工厂函数**，实例化出的才是回调类型；
    # 实例本身用局部小写名，避免与「常量」混淆。
    window_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True
        # 最小化的窗口截不到画面，列出来只会让人选中一个抓不到的目标
        if _user32.IsIconic(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True

        cls_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls_buf, 256)
        if cls_buf.value in _SHELL_CLASSES:
            return True

        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        if rect.right - rect.left < _MIN_W or rect.bottom - rect.top < _MIN_H:
            return True

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # 排除自己：把 AutoLearn 控制台当成「待抓应用」纯属自找麻烦
        if int(pid.value) == own_pid:
            return True

        refs.append(
            WindowRef(
                hwnd=int(hwnd),
                title=title,
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                pid=int(pid.value),
                process=_process_name(int(pid.value)),
            )
        )
        return True

    _user32.EnumWindows(window_proc(_callback), 0)
    return refs


def list_windows() -> list[WindowRef]:
    """列出当前可见、有标题、尺寸够大的顶层窗口。"""
    refs = _iter_windows()
    refs.sort(key=lambda r: r.title.lower())
    return refs


def screen_size() -> tuple[int, int]:
    """虚拟屏幕尺寸（含多显示器）。取不到时返回 ``(0, 0)``。"""
    if _user32 is None:
        return (0, 0)
    # SM_XVIRTUALSCREEN=76, SM_YVIRTUALSCREEN=77, SM_CXVIRTUALSCREEN=78, SM_CYVIRTUALSCREEN=79
    return (
        int(_user32.GetSystemMetrics(78)),
        int(_user32.GetSystemMetrics(79)),
    )


# --------------------------------------------------------------------------- #
# 截图
# --------------------------------------------------------------------------- #
#: PrintWindow 的这个标志让 DWM 合成的窗口（Chrome / Electron / WPF）
#: 也能截到真实内容；不带它，很多现代窗口只能拿到一片黑。
_PW_RENDERFULLCONTENT = 0x00000002

#: GetDIBits 的 DIB_RGB_COLORS
_DIB_RGB_COLORS = 0


def _grab_printwindow(hwnd: int, width: int, height: int) -> bytes | None:
    """用 PrintWindow 截窗口画面。**即使窗口被遮挡或不在前台也能截到**。"""
    if _user32 is None or _gdi32 is None:
        return None
    hdc_win = _user32.GetWindowDC(hwnd)
    if not hdc_win:
        return None
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_win)
    bitmap = _gdi32.CreateCompatibleBitmap(hdc_win, width, height)
    if not hdc_mem or not bitmap:
        with suppress(Exception):
            _gdi32.DeleteDC(hdc_mem)
            _user32.ReleaseDC(hwnd, hdc_win)
        return None
    old = _gdi32.SelectObject(hdc_mem, bitmap)
    try:
        # 先按带标志的新行为试；有些老旧窗口不认它，退回不带标志的老行为。
        fresh = _user32.PrintWindow(hwnd, hdc_mem, _PW_RENDERFULLCONTENT)
        if not fresh and not _user32.PrintWindow(hwnd, hdc_mem, 0):
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        info = BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.biWidth = width
        # 负高度 = 自上而下，省掉一次行序翻转
        info.biHeight = -height
        info.biPlanes = 1
        info.biBitCount = 32
        info.biCompression = 0

        stride = width * 4
        buffer = ctypes.create_string_buffer(stride * height)
        got = _gdi32.GetDIBits(
            hdc_mem, bitmap, 0, height, buffer, ctypes.byref(info), _DIB_RGB_COLORS
        )
        if got == 0:
            return None
        return _bgrx_to_png(buffer.raw, width, height)
    finally:
        with suppress(Exception):
            _gdi32.SelectObject(hdc_mem, old)
            _gdi32.DeleteObject(bitmap)
            _gdi32.DeleteDC(hdc_mem)
            _user32.ReleaseDC(hwnd, hdc_win)


def _grab_screen(rect: tuple[int, int, int, int]) -> bytes | None:
    """退路：直接从屏幕截取窗口矩形。

    需要窗口**没有被遮挡**，因此只能当退路 —— 主路径是 PrintWindow。
    """
    try:
        from PIL import ImageGrab
    except Exception:  # pragma: no cover - pillow 是既有依赖，理论上不会缺
        return None
    with suppress(Exception):
        image = ImageGrab.grab(bbox=rect, all_screens=True)
        return _pil_to_png(image)
    return None


def _bgrx_to_png(raw: bytes, width: int, height: int) -> bytes | None:
    """BGRA 缓冲 → PNG 字节。用 PIL 编码，保证与仓库既有的 pillow 依赖一致。"""
    try:
        from PIL import Image
    except Exception:  # pragma: no cover
        return None
    with suppress(Exception):
        # PrintWindow 给的是 BGRX（第四字节无用），按 raw 解码再转 RGB
        image = Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1)
        return _pil_to_png(image.convert("RGB"))
    return None


def _pil_to_png(image: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def capture_window(hwnd: int) -> tuple[bytes, str]:
    """截窗口画面，返回 ``(png_bytes, 方法名)``。

    方法名会一路带到留痕里：``printwindow`` 说明拿到的是窗口真实内容，
    ``screen_grab`` 说明只能看到屏幕上露出来的部分（窗口被遮挡时可能不完整）——
    这两者在排查「模型为什么读错」时是完全不同的线索。
    """
    if _user32 is None or not _user32.IsWindow(hwnd):
        raise TargetUnavailableError(f"窗口句柄 {hwnd} 不存在（可能已被关闭）")
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise TargetUnavailableError(f"读不到窗口 {hwnd} 的尺寸，可能已被关闭")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise TargetUnavailableError(
            f"窗口 {hwnd} 的尺寸非法（{width}x{height}）—— 窗口可能已最小化"
        )

    png = _grab_printwindow(hwnd, width, height)
    if png:
        return png, "printwindow"
    png = _grab_screen((rect.left, rect.top, rect.right, rect.bottom))
    if png:
        return png, "screen_grab"
    raise TargetUnavailableError(
        f"窗口 {hwnd} 截图失败：PrintWindow 与屏幕截取都拿不到画面。"
        "可能是受保护窗口（如带 DRM 的播放器），或窗口已最小化。"
    )


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_SW_RESTORE = 9
_SW_SHOW = 5


def focus_window(hwnd: int) -> bool:
    """把窗口提到前台。**点击前必须做**，否则点在别的窗口上。

    返回是否成功。Windows 对「后台进程抢前台」有节流，失败是常态而非异常，
    所以返回布尔值让调用方决定要不要继续（点了不开总比点错窗口好）。
    """
    if _user32 is None:
        return False
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
    else:
        _user32.ShowWindow(hwnd, _SW_SHOW)
    with suppress(Exception):
        _user32.SetForegroundWindow(hwnd)
    return int(_user32.GetForegroundWindow()) == int(hwnd)


def send_click(x: float, y: float) -> None:
    """在**屏幕坐标**上左键点一下。"""
    if _user32 is None:
        raise TargetUnavailableError("当前系统不支持桌面窗口点击（仅 Windows）")
    _user32.SetCursorPos(round(x), round(y))
    _user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    _user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


# --------------------------------------------------------------------------- #
# 采集源
# --------------------------------------------------------------------------- #
class DesktopWindowSource(TargetSource):
    """把正在运行的原生程序窗口当成可抓目标。

    ``list_targets`` 是**幂等只读**的：反复枚举窗口不改变任何东西
    （这正是 UI 上「抓取窗口」按钮可以随便点的前提）。
    """

    kind = TargetKind.DESKTOP_WINDOW

    def __init__(self, *, exclude_hwnds: frozenset[int] = frozenset()) -> None:
        self.exclude_hwnds = exclude_hwnds

    async def list_targets(self) -> list[TargetInfo]:
        refs = [r for r in list_windows() if r.hwnd not in self.exclude_hwnds]
        return [
            TargetInfo(
                target_id=ref.target_id,
                kind=self.kind,
                title=ref.title,
                url=None,
                # 「所属应用」对桌面目标就是进程名 —— 同时开着好几个客户端时靠它区分
                app=ref.process,
            )
            for ref in refs
        ]

    @asynccontextmanager
    async def open(self, target_id: str) -> AsyncIterator[TargetHandle]:
        hwnd = _parse_hwnd(target_id)
        if hwnd is None or _user32 is None or not _user32.IsWindow(hwnd):
            raise TargetUnavailableError(
                f"窗口目标 {target_id} 不存在（可能已被关闭）。请重新点一次「抓取窗口」。"
            )

        # 换算参数由「每次截图」刷新（用户随时可能移动窗口），所以这里的
        # surface 必须能被 capture 闭包改到 —— 用一个晚绑定的容器绕开
        # 「surface 要引用 capture、capture 又要改 surface」的先后顺序问题。
        holder: dict[str, ScreenSurface] = {}

        async def _capture() -> bytes:
            return await _capture_frame(hwnd, holder.get("surface"))

        ref = _find_ref(hwnd)
        surface = ScreenSurface(
            capture=_capture,
            click=lambda x, y: _click_async(hwnd, x, y),
            origin=(ref.rect[0], ref.rect[1]) if ref else (0, 0),
        )
        holder["surface"] = surface

        yield TargetHandle(
            info=TargetInfo(
                target_id=target_id,
                kind=self.kind,
                title=ref.title if ref else "",
                # 「所属应用」对桌面目标就是进程名 —— 同时开着好几个客户端时靠它区分
                app=ref.process if ref else "",
            ),
            page=None,
            surface=surface,
        )


def _parse_hwnd(target_id: str) -> int | None:
    if not target_id.startswith(_WIN_ID_PREFIX):
        return None
    with suppress(ValueError):
        return int(target_id[len(_WIN_ID_PREFIX) :])
    return None


def _find_ref(hwnd: int) -> WindowRef | None:
    for ref in list_windows():
        if ref.hwnd == hwnd:
            return ref
    return None


async def _capture_frame(hwnd: int, surface: ScreenSurface | None) -> bytes:
    """截一帧，并把「图像↔屏幕」的换算参数刷到 surface 上。

    换算参数**每次截图都刷新**：用户随时可能移动窗口或改显示缩放。
    缓存一次、然后在用户拖了窗口之后继续用旧值，点出来就会偏。
    """
    png, method = await asyncio.to_thread(capture_window, hwnd)
    if surface is None:
        return png
    ref = await asyncio.to_thread(_find_ref, hwnd)
    if ref is None:
        return png
    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        img_w, _img_h = image.size
    surface.origin = (ref.rect[0], ref.rect[1])
    # 图像像素 / 窗口像素。per-monitor DPI aware 下通常相等，但某些窗口会
    # 返回不同尺寸的位图，所以按实际比例算，不假定 1:1 ——
    # 假定 1:1 的后果是点击在缩放显示器上系统性偏移。
    surface.scale = (ref.width / img_w) if img_w else 1.0
    surface.grab_method = method
    return png


async def _click_async(hwnd: int, x: float, y: float) -> None:
    """先提窗口到前台，再点。不这么做就点到别的窗口上了。"""
    if not await asyncio.to_thread(focus_window, hwnd):
        raise TargetUnavailableError(
            f"无法把窗口 {hwnd} 提到前台，点击会落到别的窗口上，已放弃本次点击。"
            "请先手动点一下那个窗口再重试。"
        )
    await asyncio.to_thread(send_click, x, y)
