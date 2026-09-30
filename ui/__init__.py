"""AutoLearn Web UI（P5）。

FastAPI 装配入口 + SSE 事件流 + 零构建单页。前端三文件固定为
``static/index.html`` / ``static/app.js`` / ``static/style.css``。

启动::

    python -m uvicorn ui.server:create_app --factory --port 8800
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["APP_VERSION", "BOOT_REV", "REPO_ROOT", "code_rev"]

#: 对外暴露的版本号，``GET /api/health`` 与 OpenAPI 文档都读它
APP_VERSION = "0.3.0"

#: 仓库根（本文件在 ``<root>/ui/__init__.py``）
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 参与指纹的目录。**只放「会影响运行行为」的东西**，不含 ``tests`` / ``build`` /
#: ``dist`` / ``docs`` —— 改测试、写文档不该让人以为要重启服务。
#:
#: ``prompts`` 在里面，而且它是唯一按 ``.md`` 计入的目录：提示词是**产品行为的一部分**，
#: 改一句话就换一套行为。它不进来就会漏掉最容易骗人的那种陈旧 ——
#: 提示词改了、服务没重启，而 ``check_server.py`` 报「✅ 一致」。
_REV_DIRS = (
    "core",
    "ui",
    "target",
    "act",
    "perception",
    "solve",
    "adapters",
    "scripts",
    "prompts",
    "skills",
)
_REV_SUFFIXES = (".py", ".js", ".html", ".css", ".md")


def code_rev() -> str:
    """当前**磁盘上**代码的短指纹。

    存在的唯一理由：**判断正在跑的服务是不是旧进程**。

    这个坑真实发生过 —— 改完代码、界面却没变化，用户以为功能没做好，
    实际是浏览器/控制台连着一个改动之前启动的旧 uvicorn 进程
    （``run.bat`` 起的窗口常常被忘在后台）。
    旧进程的接口定义与新代码不一致，症状会非常离奇：
    「带参数的接口不认参数」「扫描永远返回空」。

    指纹取「文件相对路径 + 修改时间 + 大小」的哈希，不读文件内容 ——
    文件多的时候读内容太慢，而 mtime+size 对「代码有没有变」足够敏感。
    代价是：只改文件内容、mtime 恰好不变（极罕见）时指纹不变。
    """
    digest = hashlib.sha1()
    entries: list[tuple[str, float, int]] = []
    for name in _REV_DIRS:
        directory = REPO_ROOT / name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix not in _REV_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover - 文件刚被删
                continue
            entries.append((str(path.relative_to(REPO_ROOT)), stat.st_mtime, stat.st_size))
    for relative, mtime, size in sorted(entries):
        digest.update(f"{relative}:{mtime:.3f}:{size}\n".encode())
    return digest.hexdigest()[:12]


#: **本进程启动时**（更准确地说：本模块被导入时）的代码指纹。
#:
#: 为什么单独立一个：``code_rev()`` 是**请求时现读磁盘**算的，所以在一个活着的
#: 进程里它永远等于「磁盘现在的样子」—— 也就是说它**查不出最要紧的那种陈旧**：
#: 「改完代码、没重启服务」。实测踩到过：``scripts/check_server.py`` 对一个
#: 改动前启动的进程报「✅ 一致」，而那个进程里的 ``core/`` 还是老代码。
#:
#: 有了它，判断就变成「**进程加载的代码** vs **磁盘现在的代码**」——
#: 这才是「我改的东西生效了吗」这个问题真正需要的比较。
BOOT_REV = code_rev()
