"""服务自检：确认控制台连的是**当前代码**，而不是一个改动之前启动的旧进程。

为什么需要这个脚本
------------------
改完代码、界面却毫无变化 —— 第一反应通常是「功能没做好」，但真实原因常常是
**浏览器/控制台连着一个改动之前启动的旧 uvicorn 进程**。
``run.bat`` 与 ``launcher.py`` 都会离开一个后台窗口，很容易被忘在那里；
而旧进程的接口定义与新代码不一致时，症状会非常离奇：

- 「带参数的接口不认参数」（旧代码根本没有那个 query 参数）
- 「扫描永远返回空」（旧代码走的是另一条分支）

这两条本仓库都实测踩到过。所以把判断做成一条命令，而不是靠人回忆「我重启过没有」。

比的是 ``boot_rev``，不是 ``code_rev``（**这条踩过，值得记住**）
-------------------------------------------------------------
``code_rev()`` 是**请求时现读磁盘**算的，所以在活着的进程里它永远等于磁盘现状 ——
拿它对比等于**恒真**，查不出最要紧的那一种陈旧：「改完代码、没重启服务」。
实测：一个改动前启动的进程，本脚本曾报「✅ 一致」。

现在比的是 ``boot_rev``（``ui/__init__.py`` 在**导入时**记下的指纹）：
「进程加载的代码」vs「磁盘现在的代码」。老进程没有这个字段 → 直接判为陈旧。

用法::

    .venv/Scripts/python scripts/check_server.py
    .venv/Scripts/python scripts/check_server.py --url http://127.0.0.1:8801

退出码：一致 → 0；陈旧 / 连不上 / 没有服务 → 1。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui import code_rev  # noqa: E402

#: 回环地址不走环境代理（开发机常有 HTTP_PROXY，会把 127.0.0.1 也交给代理）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_URL = "http://127.0.0.1:8800"


def _fetch_health(base: str) -> dict | None:
    try:
        with _OPENER.open(f"{base}/api/health", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def _port_owner_hint(port: int) -> str:
    """尽力指出谁占着端口。取不到就返回一句可操作的通用提示。"""
    try:
        import subprocess

        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            errors="replace",  # 系统是 GBK，按 utf-8 硬解会抛
        ).stdout
    except Exception:  # pragma: no cover
        return ""
    for line in (out or "").splitlines():
        if f":{port} " in line and "LISTENING" in line:
            pid = line.split()[-1]
            return f"占用进程 PID = {pid}（可在任务管理器里结束它）"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="确认控制台连的是当前代码")
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    base = args.url.rstrip("/")
    port = urlparse(base).port or 80
    disk_rev = code_rev()

    print("AutoLearn 服务自检")
    print(f"  服务地址      : {base}")
    print(f"  磁盘代码指纹  : {disk_rev}")

    health = _fetch_health(base)
    if health is None:
        alive = socket.socket().connect_ex(("127.0.0.1", port)) == 0
        print(f"  /api/health   : 取不到（端口{'被占用但不是 AutoLearn' if alive else '没有服务'})")
        if alive:
            print(f"  {_port_owner_hint(port)}")
        print("\n结论：**没有在跑的服务**，或它不响应 /api/health。")
        print("      启动方式见项目根 README 或 run.bat。")
        return 1

    boot_rev = health.get("boot_rev")
    loaded_rev = str(boot_rev or "(该进程不支持 boot_rev，必定是改动前启动的)")
    print(f"  进程加载的指纹: {loaded_rev}")
    print(f"  服务 PID      : {health.get('pid')}")
    print(f"  服务启动于    : {health.get('started_at')}")

    if boot_rev is None:
        print("\n结论：❌ **控制台连的是旧进程** —— 它连 ``boot_rev`` 都不认识。")
        print("      你刚才改的东西**没有生效**，界面与运行行为都会是老行为。")
        print(f"      处置：先结束那个进程（{_port_owner_hint(port) or '见下方'}），")
        print("            再重新启动服务（关闭 run.bat 开出的旧窗口后重跑 run.bat）。")
        return 1

    if str(boot_rev) != disk_rev:
        print("\n结论：❌ **控制台连的是旧进程** —— 服务里的代码已经过期。")
        print("      你刚才改的东西**没有生效**，界面上的行为会是老行为。")
        print(f"      处置：先结束那个进程（{_port_owner_hint(port) or '见下方'}），")
        print("            再重新启动服务（关闭 run.bat 开出的旧窗口后重跑 run.bat）。")
        return 1

    print("\n结论：✅ 服务加载的就是磁盘上的这份代码，可以放心测。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
