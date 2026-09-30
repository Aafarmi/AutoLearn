"""全局禁用项守卫：``networkidle`` 与 ``full_page=True``。

这两个字符串是本项目风险 #6。它们不报错、不崩溃，只是让等待变得**隐式不稳定**
（``networkidle`` 在长轮询/SSE 页面上永远不会到）和让截图成本塌方。等出问题
的时候已经离现场很远了，所以用测试卡死。

只扫**真实代码字符串**，不扫注释与 docstring —— 本仓库的文档里大量引用了这两个
名字来说明「为什么禁止」。用 AST 跳过 docstring 后就不会自伤。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BANNED = ("networkidle", "full_page=True")

SCAN_DIRS = ("core", "perception", "solve", "act", "adapters", "ui", "scripts")


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """收集所有 docstring 的节点 id。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _python_files() -> list[Path]:
    root = Path(__file__).resolve().parent.parent
    files: list[Path] = []
    for name in SCAN_DIRS:
        directory = root / name
        if directory.exists():
            files.extend(sorted(directory.rglob("*.py")))
    return files


def test_scan_targets_exist() -> None:
    files = _python_files()
    assert len(files) > 20, f"扫描面太小了（{len(files)} 个文件），守卫会形同虚设"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_banned_wait_strings_in_code(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        for banned in BANNED:
            assert banned not in node.value, (
                f"{path}:{node.lineno} 出现被禁用的 {banned!r}"
                "（networkidle 在长轮询页面上永不满足；full_page 让截图成本塌方）"
            )
