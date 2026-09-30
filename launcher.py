"""AutoLearn 桌面启动器（P9 打包入口）。

一键启动完整闭环：UI(8800) + pywebview 原生桌面窗口。

设计要点
--------
- **单进程、零业务代码改动**：UI 用 ``uvicorn.run`` 编程式起在后台线程，
  主线程留给 pywebview 的事件循环。复用 ``ui.server:create_app``，不改它。
- **桌面窗口替代浏览器**：``webview.create_window`` 加载 ``http://127.0.0.1:8800``，
  渲染走系统 Edge WebView2（Win10/11 随 Edge 预装），不再弹浏览器标签页。
  窗口关闭即 ``webview.start`` 返回 → 主线程结束 → 后台线程随进程退出。
- **不启动靶场**：靶场（``scripts/serve_mock.py``）是**测试专用**的回归基础设施，
  产品路径是「附加到你正在用的页面」，不需要、也不该由启动器拉起靶场。
  要跑靶场自检时，手动 ``scripts/serve_mock.py`` 即可。
- **资源定位统一走 ``bundle_root()``**：源码模式指向仓库根；PyInstaller 打包后
  指向 ``sys._MEIPASS``（onedir 的 ``_internal``）。
- **工作目录对齐**：``state/``、``logs/`` 都是相对路径，运行前 ``chdir`` 到
  exe 所在目录，保证数据库 / 留痕落在 exe 旁边而不是系统临时目录。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

UI_HOST = "127.0.0.1"
UI_PORT = 8800
UI_READY_TIMEOUT_S = 20.0

# WebView2 运行参数（须在环境创建前设置）。两个开关各有实证：
# - CalculateNativeWinOcclusion：「原生窗口遮挡检测」会把被判定遮挡的窗口渲染
#   进程挂起（网络层能拉到 HTML 但渲染器不解析），后台/自动化会话里高发。
# - no-sandbox：本机存在注入 GUI 进程的第三方 DLL（WPS Office 的 qingnse64.dll、
#   WorkBuddy 沙箱 tsbx.dll 等，崩溃 dump 模块表实证），与 Chromium 沙箱子进程
#   机制冲突 → msedge.dll 在首次真实网络导航时确定性 CHECK 崩溃（0x80000003，
#   同地址复现），窗口永久空白。WebView2 只渲染本机 127.0.0.1 的自有 UI、
#   不加载任意外部内容，关闭沙箱的风险可接受；洁癖方案是把本应用加入
#   安全软件/WPS 的注入排除名单后去掉此参数。
os.environ.setdefault(
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "--disable-features=CalculateNativeWinOcclusion --no-sandbox",
)


def bundle_root() -> Path:
    """资源根目录：打包后为 ``_MEIPASS``，源码模式为仓库根。"""
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _run_ui() -> None:
    """UI 服务线程：uvicorn 起在后台线程，主线程留给桌面窗口的事件循环。"""
    import uvicorn

    from ui.server import create_app

    # 非 main 线程里 uvicorn 自动跳过信号处理注册，无需额外参数
    uvicorn.run(create_app(), host=UI_HOST, port=UI_PORT, log_level="info")


def _wait_ui_ready(timeout: float = UI_READY_TIMEOUT_S) -> bool:
    """轮询 UI 端口，就绪后再开窗口（否则 WebView2 会拿到连接拒绝而白屏）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((UI_HOST, UI_PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def _open_desktop_window() -> None:
    """pywebview 原生窗口：复用系统 WebView2，关窗瞬间硬退（见 closed 回调）。"""
    import webview

    icon = bundle_root() / "assets" / "autolearn.ico"
    window = webview.create_window(
        "AutoLearn 控制台",
        f"http://{UI_HOST}:{UI_PORT}",
        width=1360,
        height=860,
        min_size=(960, 640),
        background_color="#f4f6f8",  # 与前端 --bg 同色，避免启动白闪
    )
    # 窗口关闭的瞬间就硬退：CLR/WinForms 收尾要 10s+（taskkill 与
    # CloseMainWindow 实测），等 webview.start() 返回再退就会「关窗后
    # 进程残留十几秒」。FormClosed → events.closed 同步触发本回调，
    # 在慢速收尾开始之前终结进程；daemon 线程随进程终止。
    window.events.closed += lambda: os._exit(0)
    try:
        # icon 官方文档只声明 GTK/QT 支持，但 winforms 后端同样读取
        # _state['icon']（见 webview/platforms/winforms.py），Windows 上生效。
        webview.start(icon=str(icon) if icon.is_file() else None)
    except Exception as exc:
        print(f"\n[AutoLearn] 桌面窗口启动失败：{exc!r}")
        print("[AutoLearn] 常见原因：系统缺少 Edge WebView2 Runtime。")
        print("[AutoLearn] 安装后重试：https://developer.microsoft.com/microsoft-edge/webview2/")
        raise
    # 兜底：closed 事件因故未触发（如编程式关闭路径差异）时也保证退出。
    os._exit(0)


def _preflight_ui_port() -> bool:
    """启动前确认 UI 端口是空的。

    「改了代码界面却没变化」的头号原因是**上一个控制台进程还活着**：
    界面连的是旧服务，接口定义与磁盘上的代码早就不一致了。此时如果还照样
    起一个新进程，它要么绑定失败、要么被用户忽略，然后继续对着旧服务调试 ——
    这个坑实测踩到过（表现为「带参数的接口不认参数」「扫描永远返回空」）。
    所以宁可在这里明确拒绝启动，也不留一个「看起来正常」的假象。
    """
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((UI_HOST, UI_PORT)) != 0:
            return True
    print(f"[AutoLearn] 端口 {UI_PORT} 已被占用 —— 很可能上一个控制台还在运行。")
    print("[AutoLearn] 那个进程跑的是**旧代码**，继续调试只会看到旧行为。")
    print("[AutoLearn] 请先关闭它（任务管理器结束该 python 进程），再重新启动。")
    print("[AutoLearn] 想确认它在跑什么：python scripts/check_server.py")
    return False


def main() -> int:
    # 1. 工作目录对齐 exe 所在目录（state/、logs/ 相对路径落位）
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).parent)

    print("=" * 56)
    print("  AutoLearn  视觉模型读页面（v0.3.0，只使用模型）")
    print("=" * 56)
    print(f"  控制台 UI      : http://{UI_HOST}:{UI_PORT}")
    print("  桌面窗口关闭即停止全部服务")
    print()

    # 2. 端口预检：不放过「对着旧进程调试」这种情况
    if not _preflight_ui_port():
        return 1

    # 3. UI 服务后台线程
    threading.Thread(target=_run_ui, name="autolearn-ui", daemon=True).start()

    # 4. 等 UI 就绪后开桌面窗口（阻塞，关窗退出）
    if not _wait_ui_ready():
        print("[AutoLearn] 控制台 UI 未能在限定时间内就绪，退出。")
        return 1

    _open_desktop_window()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
