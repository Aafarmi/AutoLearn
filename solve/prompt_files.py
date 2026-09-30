"""提示词文件加载（**提示词格式的唯一真源**）。

提示词住在仓库根目录的 Markdown 文件里，不再内联在 Python 字符串里：

============  ================================================================
``00-共享契约.md``  **两个模型都读**：分工、中间数据结构、LaTeX 口径、
                   标号与 qid、不确定时的统一处置、共同禁止事项
``10-视觉组.md``    只给视觉组读：抄录契约 / 题型 / 几何 / 画面边界 / 复核协议
``20-解题组.md``    只给解题组读：输入形态 / 输出契约 / 空作答规则 / 解题纪律
============  ================================================================

为什么搬到文件里
----------------
提示词是**产品行为的一部分** —— 改一句话就换一套行为。写在 ``.py`` 里意味着
每次调提示词都要动代码、都要过 lint 与类型检查，而且没法让人直接审阅那份文本。
搬到 Markdown 之后，**审阅的就是喂给模型的那一份**，拼装规则只有这一处。

四条纪律
--------
1. **缺文件就大声失败**，绝不退化成空提示词。空提示词不会报错，
   它只是让模型自由发挥 —— 那是最贵的一种失败（看起来一切正常）。
2. **拼装顺序固定**：共享契约在前、专职规则在后。后写的更具体，压得住前者。
3. **在导入时读一次**。改完提示词要**重启服务**；``prompts/`` 已计入
   ``ui.code_rev()``，所以 ``scripts/check_server.py`` 能判出「改了没生效」。
4. 行尾统一按 UTF-8 读，读不了就报错，不用 ``errors="replace"`` 吞掉
   —— 提示词里出现替换字符会让模型照着一堆 ``?`` 执行。

⚠️ 改这些文件请用编辑器（或本仓库的编辑工具），**不要**用 PowerShell 的
``Get-Content | Set-Content`` —— 它会把 UTF-8 中文转成乱码。这条本项目踩过不止一次。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = [
    "PROMPT_DIR_NAME",
    "SHARED_PROMPT_FILE",
    "SOLVER_PROMPT_FILE",
    "VISION_PROMPT_FILE",
    "PromptFileMissingError",
    "load_prompt",
    "prompt_path",
    "prompts_dir",
    "solver_system_prompt",
    "training_system_prompt",
    "vision_skill_catalog",
    "vision_system_prompt",
]

PROMPT_DIR_NAME = "prompts"
SHARED_PROMPT_FILE = "00-共享契约.md"
VISION_PROMPT_FILE = "10-视觉组.md"
SOLVER_PROMPT_FILE = "20-解题组.md"
#: **下一题方式库**（2026-09-29 加）。它是「怎么进入下一题」的唯一真源，
#: 训练模式会往它的经验区追加实战要点。
LIBRARY_PROMPT_FILE = "33-推进方式库.md"
#: **训练总结**（2026-09-29 加）。训练模式专用的输出契约。
TRAINING_PROMPT_FILE = "34-训练总结.md"

#: 共享契约与专职规则之间的分隔。
#: 给模型一个明确的分界，免得它把两段规则读成一整段、分不清哪条更具体。
_SEPARATOR = "\n\n---\n\n"


class PromptFileMissingError(RuntimeError):
    """提示词文件缺失或为空。

    **故意大声失败**：不做兜底、不给默认提示词。理由见模块头第 1 条 ——
    一个空提示词不会让任何东西报错，只会让模型自己发挥，而现场看起来很正常。
    """


def _candidates() -> list[Path]:
    """按优先级列出 ``prompts/`` 可能的位置。

    源码态用 ``__file__`` 推仓库根。冻结态的分支**保留着**（``pyinstaller`` 打包
    这条线已整体删除，见 README「启动」一节）——万一将来重新引入打包，
    这段路径兜底不用再写一遍。
    """
    found: list[Path] = []
    if getattr(sys, "frozen", False):  # pragma: no cover - 打包态
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            found.append(Path(meipass) / PROMPT_DIR_NAME)
        exe_dir = Path(sys.executable).resolve().parent
        found.append(exe_dir / PROMPT_DIR_NAME)
        found.append(exe_dir / "_internal" / PROMPT_DIR_NAME)
    # 源码态：本文件在 ``<root>/solve/prompt_files.py``
    found.append(Path(__file__).resolve().parents[1] / PROMPT_DIR_NAME)
    found.append(Path.cwd() / PROMPT_DIR_NAME)
    return found


def prompts_dir() -> Path:
    """找到 ``prompts/`` 目录。找不到抛 :class:`PromptFileMissingError`。

    刻意**不缓存**：它只是几次 ``is_dir()``，而缓存会让「换目录」这件事
    在测试里变成需要清缓存才能生效的隐式状态。
    """
    candidates = _candidates()
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    tried = "\n".join(f"  - {path}" for path in candidates)
    raise PromptFileMissingError(
        f"找不到提示词目录 {PROMPT_DIR_NAME}/（缺提示词不许静默降级为「没有规则」）。\n"
        f"已尝试：\n{tried}"
    )


def load_prompt(name: str) -> str:
    """读一份提示词文件（UTF-8）。缺失、读不了、或内容为空都抛异常。"""
    path = prompt_path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptFileMissingError(f"提示词文件缺失：{path}") from exc
    except UnicodeDecodeError as exc:  # pragma: no cover - 文件被存成别的编码
        raise PromptFileMissingError(
            f"提示词文件不是 UTF-8：{path}（{exc}）。请用 UTF-8 保存，"
            "不要用 PowerShell 的 Get-Content | Set-Content 转存。"
        ) from exc
    if not text.strip():
        raise PromptFileMissingError(f"提示词文件为空：{path}")
    return text.strip()


def prompt_path(name: str) -> Path:
    """一份提示词文件在磁盘上的**绝对路径**。

    训练模式要**原地改写**方式库与经验区，所以它需要的是路径而不是正文 ——
    这里是「提示词文件在哪」的唯一定义点，别在调用方自己拼 ``prompts/``。
    """
    return prompts_dir() / name


def _compose(*names: str) -> str:
    return _SEPARATOR.join(load_prompt(name) for name in names)


def vision_system_prompt() -> str:
    """视觉组的 system prompt = 共享契约 + 专职规则 + 注册技能选择目录。"""
    from solve.skill_library import vision_skill_catalog

    return _SEPARATOR.join(
        (_compose(SHARED_PROMPT_FILE, VISION_PROMPT_FILE), vision_skill_catalog())
    )


def vision_skill_catalog() -> str:
    """导出注册技能清单，供提示词契约测试核对唯一映射。"""
    from solve.skill_library import vision_skill_catalog as catalog

    return catalog()


def solver_system_prompt() -> str:
    """解题组的 system prompt = 共享契约 + 解题组专职规则。"""
    return _compose(SHARED_PROMPT_FILE, SOLVER_PROMPT_FILE)


def training_system_prompt() -> str:
    """**训练总结**的 system prompt = 共享契约 + 训练总结规则。

    刻意**不**与读题共用一个 system prompt：训练模式问的是
    「这次运行有什么可复用的经验」，与「这一屏里有什么题」是两个输出契约。
    """
    return _compose(SHARED_PROMPT_FILE, TRAINING_PROMPT_FILE)
