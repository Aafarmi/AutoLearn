"""AutoLearn 源码分发包打包脚本。

产出 ``dist/autolearn-v<version>.zip``：一个带版本号、可分发、解压即用的源码包。
含运行时必需的全部代码 / 靶场 / 启动器 / 依赖清单 / 核心文档，排除开发与运行时产物。

用法::

    .venv/Scripts/python scripts/package.py            # 版本号取 pyproject.toml
    .venv/Scripts/python scripts/package.py --version v0.3.0   # 显式覆盖

排除原则（对齐 P9 打包策略）：
- 复用系统浏览器、不打包 Chromium、不引 PyInstaller；
- 不打包 .venv / 测试 / 运行时状态（state/、logs/）/ 工作记忆（.workbuddy/）；
- 验收报告与量化产物属开发留档，不入分发包。
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 分发包顶层目录名（不含版本号，版本号运行时拼上）
PACKAGE_NAME = "autolearn"

#: 要整体包含的目录（递归；内部仍会剔除 __pycache__ 等）
INCLUDE_DIRS = [
    "core",
    "perception",
    "solve",
    "act",
    "adapters",
    "ui",
    "mock_site",
    "scripts",
]

#: 要整体包含的单个文件
INCLUDE_FILES = [
    "run.bat",
    "requirements.txt",
    "README.md",
    ".env.example",
    "pyproject.toml",
]

#: docs/ 下要带上的核心文档（验收报告 / 量化产物属留档，不带）
INCLUDE_DOCS = [
    "AutoLearn-项目任务书.md",
    "AutoLearn-实施规划书.md",
    "AutoLearn-实施规划书-v2.0-进度版.md",
    "README.md.md",
]

#: 任意层级都要剔除的名字（目录或文件）
EXCLUDE_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest-tmp",
    "pt_pytest",
    ".venv",
    ".workbuddy",
    ".git",
    "logs",
    "state",
    "tests",
    "dist",
    "package.py",  # 打包脚本自身不入包
    "requirements-dev.txt",
    ".gitignore",
}


def read_version() -> str:
    """从 pyproject.toml 读 ``version = "..."``（PEP 440，不带 ``v``）。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        raise SystemExit("未在 pyproject.toml 找到 version 字段")
    return match.group(1)


def display_version(version: str) -> str:
    """对外展示的版本号（带 ``v`` 前缀，如 ``v0.1.0``）。"""
    return version if version.startswith("v") else f"v{version}"


def should_include(path: Path) -> bool:
    """相对路径判定：true = 入包。"""
    parts = path.parts
    if any(part in EXCLUDE_NAMES for part in parts):
        return False
    return path.suffix not in {".pyc", ".pyo"}


def iter_files() -> list[tuple[Path, Path]]:
    """返回 ``(源绝对路径, 包内相对路径)`` 列表。"""
    pairs: list[tuple[Path, Path]] = []

    for name in INCLUDE_DIRS:
        src = ROOT / name
        if not src.is_dir():
            raise SystemExit(f"缺少目录：{src}")
        for path in sorted(src.rglob("*")):
            if path.is_dir():
                continue
            if not should_include(path.relative_to(ROOT)):
                continue
            pairs.append((path, path.relative_to(ROOT)))

    for name in INCLUDE_FILES:
        path = ROOT / name
        if not path.is_file():
            raise SystemExit(f"缺少文件：{path}")
        pairs.append((path, path.relative_to(ROOT)))

    docs_dir = ROOT / "docs"
    for name in INCLUDE_DOCS:
        path = docs_dir / name
        if not path.is_file():
            raise SystemExit(f"缺少文档：{path}")
        pairs.append((path, path.relative_to(ROOT)))

    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description="打包 AutoLearn 源码分发包")
    parser.add_argument("--version", default=None, help="覆盖版本号（默认读 pyproject.toml）")
    args = parser.parse_args()

    version = args.version or read_version()
    display = display_version(version)
    top = f"{PACKAGE_NAME}-{display}"
    out_dir = ROOT / "dist"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{top}.zip"

    pairs = iter_files()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, rel in pairs:
            zf.write(src, arcname=f"{top}/{rel.as_posix()}")

    # 解包后 state/ 由程序按需创建，但显式给一个占位，避免首次运行目录缺失的困惑
    total = len(pairs)
    print(f"打包完成：{out_path}")
    print(f"  版本 {display} · {total} 个文件 · 顶层目录 {top}/")
    print(f"  大小 {out_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
