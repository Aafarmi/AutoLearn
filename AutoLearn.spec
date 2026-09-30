# -*- mode: python ; coding: utf-8 -*-
"""AutoLearn PyInstaller 打包配置（onedir，P9；v0.3.0）。

产出 ``dist/AutoLearn/`` 文件夹，含 ``AutoLearn.exe`` 与 ``_internal/`` 运行时。

关键点（改这里必须同步理解）：
- **入口是 ``launcher.py``**，单进程起靶场(8899/8900) + UI(8800) + pywebview 原生窗口。
- **资源用绝对路径显式收集**，不依赖 ``__file__``：``ui/static`` 三文件、
  ``mock_site`` 全量（html/js/json/css/md）、``adapters`` 的媒体锚点 yaml
  （v0.2.0 起只有 ``selectors_media.yaml`` —— 题目侧的 ``selectors.yaml``
  随 DOM 通道一起删除，此处**不要再把它列回 datas**：源文件不存在时
  PyInstaller 会直接报错）、``scripts/serve_mock.py``（launcher 按路径加载它）。
- **动态导入要 collect**：uvicorn 的 protocol/loop/lifespan 是
  ``import_from_string`` 运行时加载；keyring 的 backend 靠 entry_points 发现；
  playwright 的 driver（node.exe + package）靠官方 hook 收集 data。
- **复用系统浏览器**（channel=msedge/chrome），不捆绑 Chromium —— 这是既定策略。
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# --------------------------------------------------------------------------- #
# 静态资源（源码树 → 包内相对路径）
# --------------------------------------------------------------------------- #
datas = [
    ("ui/static", "ui/static"),
    ("mock_site", "mock_site"),
    ("prompts", "prompts"),
    ("skills", "skills"),
    ("adapters/mock_exam/selectors_media.yaml", "adapters/mock_exam"),
    # 桌面窗口图标（webview.start(icon=...) 与 EXE icon 共用）
    ("assets/autolearn.ico", "assets"),
]

# certifi 的 CA bundle（httpx 发 HTTPS 请求需要，不会进静态分析）
datas += collect_data_files("certifi")

# playwright 官方 hook 会把 driver（node.exe + package，约 103MB）收集进来。
# 显式再 collect 一次 data 兜底（幂等，去重后无害）。
datas += collect_data_files("playwright")

# --------------------------------------------------------------------------- #
# 动态导入模块
# --------------------------------------------------------------------------- #
hiddenimports = []
# uvicorn：protocol（h11/httptools/zttp）、loop、lifespan、websocket 全部运行时 import
hiddenimports += collect_submodules("uvicorn")
# keyring：backend 走 entry_points，静态分析看不到，全量收集子模块
hiddenimports += collect_submodules("keyring")
hiddenimports += collect_submodules("keyring.backends")
# pydantic / fastapi 的插件式组件
hiddenimports += collect_submodules("pydantic_settings")
# 桌面窗口（P9 桌面化）：pywebview 的 winforms 后端经 guilib 动态选择，
# pythonnet 的 clr 由 hook-clr / hook-clr_loader 收集 DLL，这里显式补模块
hiddenimports += ["webview", "webview.platforms.winforms", "clr"]
# uvicorn[standard] 的可选加速协议（若环境有则保留，没有不影响 fallback）
hiddenimports += ["uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
                  "uvicorn.loops.auto", "uvicorn.lifespan.on", "uvicorn.lifespan.off"]

a = Analysis(
    ["launcher.py"],
    pathex=["scripts"],  # 让 PyInstaller 解析 launcher 里的 `import serve_mock`
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 不打包测试与开发工具，减小体积
        "pytest",
        "_pytest",
        "mypy",
        "ruff",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoLearn",
    debug=False,
    icon="assets/autolearn.ico",
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 保留控制台窗口：打印服务地址与日志，关窗即停
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AutoLearn",
)
