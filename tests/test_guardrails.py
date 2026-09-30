"""守门单测：把「不碰地面真值」「探针零硬编码」「感知不依赖求解」钉死在 CI 里。

v0.2.0 起程序只使用模型读页面，题目侧不再解析文档结构，所以守门项也跟着变：

    - 感知 / 适配层不得读 ``data-answer``（地面真值只允许 MockProvider 读）；
    - 视觉通道与媒体探针内零硬编码靶场选择器（媒体选择器走 YAML）；
    - 感知层不得依赖求解层（M0 验收「零模型调用」在结构上成立）。

这些是「一旦被误用就会静默劣化」的写法，所以用源码级检查而非人工评审。

> ``networkidle`` 与 ``full_page`` 两条禁令由 ``tests/test_no_banned_waits.py``
> 负责（扫描面更宽），此处不重复实现。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 探针与适配层：这里绝不允许出现靶场选择器字面量
CHANNEL_DIRS = ("perception", "adapters")


def _iter_py(*dirs: str) -> list[Path]:
    files: list[Path] = []
    for name in dirs:
        base = ROOT / name
        if base.exists():
            files.extend(sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts))
    return files


def _strip_docstrings(source: str) -> str:
    """去掉模块/类/函数 docstring，避免注释里提到某个词就被误判。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
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
            first.value.value = ""
    return ast.unparse(tree)


# --------------------------------------------------------------------------- #
# 禁令一：通道不得读地面真值
# --------------------------------------------------------------------------- #
def test_channels_never_read_ground_truth() -> None:
    """感知 / 适配层不得出现 `data-answer`（地面真值只允许 MockProvider 读）。"""
    offenders = []
    for path in _iter_py(*CHANNEL_DIRS):
        text = _strip_docstrings(path.read_text(encoding="utf-8"))
        if "data-answer" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"通道读取了地面真值：{offenders}"


def test_channels_have_no_hardcoded_mock_selectors() -> None:
    """``vision_probe`` / ``media_probe`` 内零硬编码靶场选择器。

    媒体锚点必须走 YAML；探针里出现 ``[data-quiz`` / ``[data-media`` / ``.qz-`` 即违规。
    """
    probe_files = ["perception/vision_probe.py", "perception/media_probe.py"]
    tokens = ("[data-quiz", "[data-media", ".qz-")
    offenders: list[str] = []
    for name in probe_files:
        path = ROOT / name
        text = _strip_docstrings(path.read_text(encoding="utf-8"))
        for token in tokens:
            if token in text:
                offenders.append(f"{name} :: {token}")
    assert not offenders, f"探针里硬编码了靶场选择器：{offenders}"


def test_no_question_dom_channel_files_exist() -> None:
    """**回归守卫**：DOM / 网络题目通道不许回来。

    ``dom_probe`` 与 ``net_probe`` 是「解析页面文档结构读题」这条通道的实现；
    v0.2.0 已整体删除。它们一旦重现，说明有人把双通道又接了回来，
    而界面上已经没有「DOM 优先」这个开关了 —— 那会变成一条**谁都关不掉**的隐蔽路径。
    """
    for gone in ("perception/dom_probe.py", "perception/net_probe.py"):
        assert not (ROOT / gone).exists(), f"{gone} 已被 v0.2.0 删除，不该重新出现"


def test_adapter_yaml_only_has_media_anchors() -> None:
    """题目锚点 YAML 已删除；媒体锚点 YAML 是唯一的选择器来源。"""
    assert not (ROOT / "adapters/mock_exam/selectors.yaml").exists()
    assert (ROOT / "adapters/mock_exam/selectors_media.yaml").is_file()


def test_perception_never_imports_solver() -> None:
    """感知层不得依赖求解层 —— 保证 M0 验收「零模型调用」在结构上成立。"""
    offenders = []
    for path in _iter_py("perception", "adapters"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"solve", "openai", "anthropic"}:
                        offenders.append(f"{path.name} -> {alias.name}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] in {"solve", "openai", "anthropic"}
            ):
                offenders.append(f"{path.name} -> {node.module}")
    assert not offenders, f"感知层依赖了求解层：{offenders}"
