"""解题组的消息组装与响应解析（M2-1 / M2-3）。

本模块是**「怎么把题问出去、怎么把答案收回来」的唯一真源**：
Provider 与 Solver 都不许自己拼串。

标号口径（p6.0 起）
-------------------
渲染给模型的标号**一律是页面标号**（``question.options[i].label``），
呈现顺序**一律是页面顺序**（见 ``Solver._build_batch``）。理由：

1. 模型回的字母是**操作指令**（下游按它去点页面上的选项），所以它看到的标号
   必须逐字等于页面标号 —— 错位一次就点错一次，而且从输出上看不出来；
2. 视觉组可能给出**非连续标号**（某个选项的框被丢弃时是 ``A, B, D``）：
   拿「第几个」当标号在那种题上必定错，所以标号只能从
   ``question.options[i].label`` 来，不能按位置现算。

消息模板本身也是 ``MockProvider`` 的输入契约 —— 它按同一格式把「模型看到的那份
选项」解析回来，才能算出正确内容的那个**页面标号**。改这里的排版 = 改 Mock 的行为。

提示词住在哪
------------
**system prompt 不在本文件里**，它来自 ``prompts/``：

- ``prompts/00-共享契约.md``  —— 两个模型都读（分工 / 数据结构 / LaTeX 口径 / 标号与 qid）
- ``prompts/20-解题组.md``    —— 只给解题组读（输入形态 / 输出契约 / 空作答规则）

:func:`solve.prompt_files.solver_system_prompt` 把两者拼起来，在**导入时读一次**。
改提示词请改那两个 Markdown 文件，不要再把规则内联回这里 —— 拼装规则只有一处。
``ui.code_rev()`` 已把 ``prompts/`` 计入指纹，所以「改了提示词没重启」这件事
会被 ``scripts/check_server.py`` 判出来。

四条硬约束（规划书 §P4 / 任务书 §M2-1）
---------------------------------------
1. **只输出 JSON**，字段固定为
   ``{"chosen_labels": ["A"], "confidence": 0.92, "reason": "..."}``；
2. 选项按**页面顺序**给出、标号是**页面标号**，模型只回**字母标号**，不回内容；
3. **不泄漏地面真值**（``data-answer`` / ``data-answer-texts``）；
4. 多选是**集合语义**，不允许「最接近」式模糊表述；题干被截断或带图时显式告知。

关于「题目ID」那一行
--------------------
``qid`` 是题干与选项正文的 sha1 前缀（T0-1），**不含任何答案信息**，
所以把它写进提示词不构成泄漏。写上它是为了让 ``MockProvider`` 在题干被截断时
仍能确定地取到真值 —— 否则截断题在 Mock 下无法复现。
它同时是**编排层的对账键**（把「读到的题」与「解的题」绑在一起），
但解题组**不需要在回复里回报它** —— 题面与 qid 同源，编排层自己对得上。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from typing import Any

from core.enums import QType
from core.models import Question
from core.qid import normalize_text
from solve.prompt_files import solver_system_prompt

__all__ = [
    "CHOSEN_LABELS_KEY",
    "CONFIDENCE_KEY",
    "EXPLICIT_EMPTY_KEY",
    "LATEX_NOTE",
    "NO_MOCK_ANSWER",
    "PROMPT_VERSION",
    "REASON_KEY",
    "SYSTEM_PROMPT",
    "OptionPair",
    "build_messages",
    "label_for_index",
    "option_pairs",
    "parse_answer_payload",
    "parse_confidence",
    "parse_presented_options",
    "render_options",
]

#: 提示词版本。改文案/排版必须改这个号，便于日志里区分历史作答。
#:
#: ``p4.1 → p5.0``：system prompt 改为从 ``prompts/`` 拼装（共享契约 + 解题组专职规则），
#: 并在用户消息里显式给出公式说明 —— 消息形态变了，所以是**次版本号**级别的改动。
#:
#: ``p5.0 → p6.0``：两处**消息形态**一起变了，老留痕不能再按新格式解读：
#:
#: 1. 选项标号从「本次呈现的位置字母」改成**页面标号**（p5.0 的 ``D`` 是
#:    「打乱后第 4 项」，p6.0 的 ``D`` 是「页面上印着 D 的那一项」，语义不同）；
#: 2. 输出契约新增 ``confidence``（模型自报的把握），空的照旧不写。
#:
#: ``p6.0 → p7.0``：新增题型技能 ID 与正文，求解上下文形态变化。
#: 因为跨版本解读会**读错答案**（不是少一个字段），所以升**主版本号**。
PROMPT_VERSION = "p7.0"

CHOSEN_LABELS_KEY = "chosen_labels"
CONFIDENCE_KEY = "confidence"
REASON_KEY = "reason"
#: 显式空作答的标记键：``chosen_labels`` **字段存在且为空数组**时置真。
#: 它与「回复不可用」是两件事 —— 见 :func:`parse_answer_payload`。
EXPLICIT_EMPTY_KEY = "explicit_empty"

#: 未知题目时 Mock 给出的占位标号集合（空集表示「无法作答」）。
NO_MOCK_ANSWER: list[str] = []

#: 解题组的 system prompt = ``prompts/00-共享契约.md`` + ``prompts/20-解题组.md``。
#:
#: **刻意不内联。** 上一版内联在这里，缺了三件要紧的事：没有说 ``$...$`` 是 LaTeX、
#: 没有给「题面缺失就空作答」这条出口、也没有提醒模型「这道题的题面是上游抄来的，
#: 本身可能是错的」。三件都在 Markdown 里补齐了。
SYSTEM_PROMPT = solver_system_prompt()

#: 用户消息里的公式说明。
#:
#: 为什么写在**消息里**而不是只写在 system prompt 里：视觉组抄出来的题干带 ``$...$``，
#: 模型必须在读到题面的那一刻就知道「这段不是普通文本」。system prompt 离得远，
#: 而这一行就贴在题干上面。
#:
#: 门禁会把 ``clipped`` / ``uncertain`` 的题**挡在解题组之外**，所以正常情况下
#: 题面里不会出现 ``$?$``；万一漏过来（例如换了调用方），解题组那一侧的规则
#: （``prompts/20-解题组.md`` 第 8 条）仍然要求它空作答。
LATEX_NOTE = (
    "公式说明：题干与选项里的 $...$ 是 LaTeX 数学公式，请按数学含义理解，"
    "不要当成普通文本，也不要改写它。"
)

#: 题干被截断时追加的告知（约束 4）
TRUNCATION_NOTE = (
    "注意：本题题干**可能不完整**（页面存在截断/懒加载）。请基于可见部分作答，"
    "并在 reason 中注明信息不完整。"
)

#: 用户消息末尾的作答契约回显（与 ``prompts/20-解题组.md`` 一字不差地同构）。
ANSWER_SHAPE_HINT = (
    '请只输出 JSON：{"chosen_labels": ["A"], "confidence": 0.92, "reason": "..."}'
)

#: 「选项 = (标号, 正文)」对。标号是**页面标号**，正文是 NFKC 归一化后的选项正文。
OptionPair = tuple[str, str]


def label_for_index(index: int) -> str:
    """位置序号 → 字母（``0 → A``）。超过 26 项返回空串，由调用方兜底。

    .. warning::
       这是**位置字母**，只作最后兜底（调用方给不出页面标号时）。
       页面标号一律取 ``question.options[i].label`` —— 视觉组给的非连续标号
       （``A, B, D``）在位置字母下会整个错位（见模块文档）。
    """
    if index < 0 or index > 25:
        return ""
    return chr(ord("A") + index)


def render_options(options: Sequence[OptionPair]) -> list[str]:
    """把 ``(标号, 正文)`` 渲染成 ``["A. 甲", "B. 乙"]``。

    标号**照抄传进来的那一个**：它已经是页面标号，本函数不再按位置重排。
    """
    return [f"{label}. {text}" for label, text in options]


def option_pairs(question: Question, presented: Sequence[str | OptionPair]) -> list[OptionPair]:
    """把「本次呈现」归一化成 ``(标号, 正文)`` 列表。

    两种输入都收：

    - ``(标号, 正文)`` 对：直接用它。**求解层走这条** —— 标号与正文成对传递，
      中途没有「按位置重算标号」的环节，也就没有错位的机会；
    - 裸正文：按顺序配上 ``question.options[i].label``（历史调用方与单测的写法）。
      配不到（比题目选项还长）时才退回 :func:`label_for_index`。
    """
    pairs: list[OptionPair] = []
    for index, entry in enumerate(presented):
        if isinstance(entry, tuple):
            label, text = entry
            pairs.append((str(label), str(text)))
            continue
        text = str(entry)
        label = (
            question.options[index].label
            if index < len(question.options)
            else label_for_index(index)
        )
        pairs.append((label, text))
    return pairs


def build_messages(
    question: Question,
    presented: Sequence[str | OptionPair],
    *,
    truncated: bool = False,
    skill_text: str | None = None,
) -> tuple[str, str]:
    """构造 ``(system, user)``。

    ``presented`` 是**本次采样实际呈现**的选项：求解层给 ``(标号, 正文)`` 对，
    且顺序恒等于页面顺序。渲染出的标号就是页面标号（见模块文档中的两条理由）。
    ``skill_text`` 必须来自注册表；本函数不根据题面猜技能。
    """
    qtype_cn = {
        QType.SINGLE: "单选题",
        QType.MULTIPLE: "多选题",
        QType.TRUE_FALSE: "判断题",
    }[question.qtype]
    if skill_text is None:
        # 兼容诊断脚本与少数非生产调用者；生产 Solver 强制显式加载注册技能。
        skill_text = "未加载题型技能；如无法确定技能规则，必须空作答，不得猜测。"
    lines = [
        f"题目ID: {question.qid}",
        f"题型: {qtype_cn}",
        f"技能ID: {question.skill_id or '未指定'}",
        "",
        "【题型技能】",
        skill_text.strip(),
        "",
        LATEX_NOTE,
        "",
        "题干:",
        question.stem,
        "",
        "选项:",
        *render_options(option_pairs(question, presented)),
    ]
    if truncated:
        lines += ["", TRUNCATION_NOTE]
    lines += ["", ANSWER_SHAPE_HINT]
    return SYSTEM_PROMPT, "\n".join(lines)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_LABEL_LINE = re.compile(r"^\s*([A-Za-z])\s*[.、．:：)）]\s*(.*)$")
_LABEL_ONLY = re.compile(r"^\s*[（(]?\s*([A-Za-z])\s*[)）]?\s*[.、．]?\s*$")
_PERCENT = re.compile(r"^(-?\d+(?:\.\d+)?)\s*%$")


def _extract_json_object(text: str) -> str | None:
    """从模型输出里抠出最外层 JSON 对象（容忍代码块与前后废话）。"""
    stripped = text.strip()
    fenced = _FENCE.search(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for offset, char in enumerate(stripped[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : offset + 1]
    return None


def _normalize_labels(raw: Any) -> list[str]:
    """把模型给的标号归一化成 ``["A", "C"]``。

    容忍 ``"A"`` / ``"a."`` / ``"(A)"`` / ``"A、C"`` / ``["A","C"]`` 等写法，
    但**只收单字母标号** —— 多字符内容一律丢弃，避免把选项正文当成标号。
    """
    items: list[str] = []
    if isinstance(raw, str):
        items = [part for part in re.split(r"[,，、;；\s]+", raw) if part]
    elif isinstance(raw, list | tuple):
        items = [str(entry) for entry in raw]
    else:
        return []
    labels: list[str] = []
    for item in items:
        match = _LABEL_ONLY.match(item)
        candidate = match.group(1) if match else item.strip()
        if len(candidate) != 1 or not candidate.isascii() or not candidate.isalpha():
            continue
        label = candidate.upper()
        if label not in labels:
            labels.append(label)
    return labels


def parse_confidence(raw: Any) -> float | None:
    """把模型自报的 ``confidence`` 归一化成 ``[0, 1]``；解析不出来返回 ``None``。

    - ``0.92`` / ``"0.92"`` / ``"92%"`` 都认；
    - **越界夹到** ``[0, 1]``，而不是丢弃：模型报了「很有把握」，
      只因为写法夸张（``1.2`` / ``120%``）就当作「没报」会白丢一个真实信号。
      夹取的方向是安全的 —— 夹到 1.0 只是「不额外触发复核」，
      夹到 0.0 只会多停一次；
    - ``bool`` 不算数字（JSON 里的 ``true`` 不是 ``1.0``）；
    - 缺失 / 非数字 / ``NaN`` / ``None`` → ``None``（= **模型没报**）。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        match = _PERCENT.match(text)
        if match:
            value = float(match.group(1)) / 100.0
        else:
            try:
                value = float(text)
            except ValueError:
                return None
    else:
        return None
    if math.isnan(value):
        return None
    return min(1.0, max(0.0, value))


def parse_answer_payload(text: str) -> dict[str, Any] | None:
    """把模型输出解析成作答载荷。**两种「没有答案」必须分清**：

    =========================================================  ==============================
    ``{"chosen_labels": [], "explicit_empty": True}``          模型**明确**说「无法作答」
    ``None``                                                  这条回复**不可用**
    =========================================================  ==============================

    前者是**模型的结论**（题面不足以下结论），下游不重发、直接按空作答停下问人；
    后者是**请求失败**（没 JSON / JSON 坏了 / ``chosen_labels`` 字段缺失 /
    字段里的东西根本不是标号），下游可以重发（有界，见 ``Solver.SAMPLE_RETRY_MAX``）。

    这两件事被混成同一个 ``None`` 时，一次网络抖动与一次「题面缺失」在日志里
    长得一模一样，重发逻辑就无从下手。

    ``confidence`` **只在解析得出的时候**才写进返回值：调用方用 ``.get()`` 取，
    取到 ``None`` 就是「模型没报」。这与「报了 0」是两件不同的事，
    所以不能用 ``0.0`` 代替缺失值。
    """
    blob = _extract_json_object(text)
    if blob is None:
        return None
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    raw_labels = payload.get(CHOSEN_LABELS_KEY)
    # 「显式空作答」= 字段**存在**且是空数组。字段缺失/写成 null 都不算 ——
    # 那是「这条回复不可用」，不是「模型说答不了」。
    explicit_empty = (
        CHOSEN_LABELS_KEY in payload and isinstance(raw_labels, list) and not raw_labels
    )
    labels = _normalize_labels(raw_labels)
    if not labels and not explicit_empty:
        return None
    reason = payload.get(REASON_KEY)
    parsed: dict[str, Any] = {
        CHOSEN_LABELS_KEY: labels,
        REASON_KEY: reason if isinstance(reason, str) else "",
    }
    confidence = parse_confidence(payload.get(CONFIDENCE_KEY))
    if confidence is not None:
        parsed[CONFIDENCE_KEY] = confidence
    if explicit_empty:
        parsed[EXPLICIT_EMPTY_KEY] = True
    return parsed


def parse_presented_options(user_text: str) -> list[tuple[str, str]]:
    """把发给模型的选项块解析回 ``[(标号, 正文), ...]``。

    标号是**页面标号**（可能不连续，如 ``A, B, D``），顺序就是消息里的顺序。
    只给 ``MockProvider`` 与测试用：它必须知道自己**实际呈现了哪一份**，
    才能按内容把正确项翻译成「模型看到的那个标号」——
    而在 p6.0 的口径下，模型看到的标号就是页面标号。
    """
    _, _, tail = user_text.partition("选项:")
    if not tail:
        return []
    body = tail.split("\n\n", 1)[0]
    parsed: list[tuple[str, str]] = []
    for line in body.splitlines():
        match = _LABEL_LINE.match(line)
        if not match:
            continue
        label = match.group(1).upper()
        text = normalize_text(match.group(2))
        if text:
            parsed.append((label, text))
    return parsed
