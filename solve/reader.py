"""视觉「读题」：让模型看一张图，**把题目读出来**（不是答题）。

v0.2.0 起这是**唯一**的读题方式
-------------------------------
程序不再解析页面文档结构，页面上「题目是什么」只能由模型看图回答。
于是本模块从「读不出来时的兜底」变成主路：截图 → 模型 → 结构化题目 + 几何。

读出来的东西分两路走，这是本模块唯一需要交代的接口事实：

    ReadResult ──转成 Question──▶ 求解 / 投票 / 缓存链路（不含几何）
               └─选项与按钮的归一化框─▶ 执行层按坐标点击（act/actuator.py）

所以 **``Question`` 刻意不含几何**（它是全链路共用的契约），几何单独交给
``ReadResult`` 保存；执行层拿到的框是**相对整张截图的归一化坐标**，
乘一次图像尺寸就能点（见 :mod:`act.screen`）。原先「DOM 读题 / 视觉读题
两条路共用一套 Question」那段对照已经作废：现在只有这一条路。

提示词纪律
----------
**提示词正文不在这里**：它们住在仓库根 ``prompts/*.md``（唯一真源），由
:mod:`solve.prompt_files` 在导入时读一次并拼装。本模块只负责
「**怎么问、怎么解析、怎么降级**」。三条纪律由那几份 Markdown 承载：

1. **只读不答**。明确禁止它给答案 —— 模型一旦顺手把答案写在题干里，
   后面的求解就变成了「抄自己」，而且会污染 qid（qid 由题干哈希而来）。
2. **要求坐标归一化到 ``0..1``**。图可能被模型侧缩放过，像素坐标会漂；
   归一化后由调用方按「图实际尺寸」一次性换算，换算点只有一个。
3. **找不到就留空，不要编**。宁可少一个选项，也不能让模型把不存在的按钮补出来 ——
   执行层会照着坐标去点，编出来的坐标就是一次真实的误点。
4. **推进目标（``page.advance_skill_id`` / ``page.swipe``）每一步都要按当前画面重给**
   （2026-09-30 加）。它答的是「这一步怎么进下一题」，不是「开局时怎么进」——
   开局算好的答题卡几何会漂移，沿用它就是「推进后读到的题与实际对不上」的成因。
   解析层对这两个字段只做严格校验（未注册 ID 落 ``None``、非法幅度整块丢弃），
   **不修、不猜、不延续上一屏**。

本模块对外提供**一件事**（2026-09-30 收口）：``read_questions``。

视觉组每次只回答**同一个输出契约**（题目 + 页面观测，见 ``prompts/10-视觉组.md``）。
上一版为了「找下一题控件 / 起始标定 / 收尾确认」另开了三套提示词与三个请求，
结果是同一个页面被问了三遍、而且每次的说法都可能不一样 ——
那正是「下一题处理仍然存在严重问题」的成因。现在：

* 只有**一份** system prompt（共享契约 + 视觉组专职规则）；
* 只有**一种**回复 schema（``page`` 观测块 + ``questions`` 数组）；
* 「该怎么推进 / 什么时候提交 / 是不是做完了」一律由程序按
  :func:`core.run_plan.derive_plan` 从观测里裁决。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from contextlib import suppress
from typing import Any

from core.enums import CompletionState, ProbeName, QType, SubmitScope
from core.models import (
    PageCard,
    PageControl,
    PageSubmit,
    PageSwipe,
    PageView,
    ReadBatch,
    ReadOption,
    ReadResult,
)
from solve.prompt_files import vision_system_prompt
from solve.providers.base import LLMProvider, LLMRequest, ProviderError, RateLimitError
from solve.skill_library import (
    ADVANCE_SWIPE_SKILL_ID,
    SKILL_SPECS,
    get_advance_skill,
    get_skill,
)

__all__ = [
    "READ_SYSTEM_PROMPT",
    "READ_TEMPERATURE",
    "build_read_messages",
    "gate_read_result",
    "parse_page_view",
    "parse_read_batch",
    "parse_read_payload",
    "read_question",
    "read_questions",
    "skill_for_reported_type",
]

logger = logging.getLogger(__name__)

READ_TEMPERATURE = 0.0

#: 读题 completion 预算，包含推理模型的 reasoning tokens 与最终 JSON。
#: 现场故障样本的 usage 为 2,128 completion tokens（其中 1,489 reasoning），
#: finish_reason=stop，说明没有触及 token 截断；解析失败是 JSON 字符串里的裸换行与
#: 未转义引号造成的。保留 16k 上限为长题面/推理余量，不靠继续加 token 修复格式错误。
READ_MAX_TOKENS = 16384
#: 视觉读取遇到瞬时限流时，对同一 provider 的短重试次数（不含首次请求）。
READ_RATE_LIMIT_RETRIES = 2
READ_RATE_LIMIT_RETRY_DELAY_S = 1.0

#: 视觉组的 system prompt。**只有这一份**（2026-09-30 起）。
#:
#: 为什么不在本文件里留内联副本：提示词是**产品行为的一部分**，改一句话就换一套行为；
#: 留在 ``.py`` 里意味着每次调提示词都要动代码、过 lint 与类型检查，而且没法让人直接审阅
#: 那份文本。搬到 Markdown 之后，**审阅的就是喂给模型的那一份**。
#:
#: 读不到会**大声失败**（``PromptFileMissingError``）—— 空提示词不会报错，
#: 它只是让模型自由发挥，而现场看起来一切正常：最贵的一种失败。
#:
#: 改动提示词后**必须重启服务**：这里是导入时读一次，``prompts/`` 已计入 ``ui.code_rev()``，
#: ``scripts/check_server.py`` 能判出「改了没生效」。
READ_SYSTEM_PROMPT = vision_system_prompt()

_LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
#: 从可能夹带散文的回复里抠出 JSON 对象
_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

#: 题型别名 → 项目识别的题型。未知题型不推测。
#:
#: 已有的单选、多选映射保持兼容；本次新增判断题 ``true_false``。
#: 具体的解题方法由视觉组选中的 ``skill_id`` 决定，并由注册表再次校验。
#: 未知 / 不支持的题型会随整道题留档并跳过，不得擅自降为单选。
_QTYPE_ALIASES: dict[str, QType] = {
    "single": QType.SINGLE,
    "single_choice": QType.SINGLE,
    "singlechoice": QType.SINGLE,
    "radio": QType.SINGLE,
    "one": QType.SINGLE,
    "single_select": QType.SINGLE,
    "choice": QType.SINGLE,
    "single_select_question": QType.SINGLE,
    "单选": QType.SINGLE,
    "单选题": QType.SINGLE,
    "单项选择": QType.SINGLE,
    "单项选择题": QType.SINGLE,
    "multiple": QType.MULTIPLE,
    "multi": QType.MULTIPLE,
    "multi_choice": QType.MULTIPLE,
    "multiple_choice": QType.MULTIPLE,
    "multiplechoice": QType.MULTIPLE,
    "multichoice": QType.MULTIPLE,
    "checkbox": QType.MULTIPLE,
    "multi_select": QType.MULTIPLE,
    "true_false": QType.TRUE_FALSE,
    "truefalse": QType.TRUE_FALSE,
    "judge": QType.TRUE_FALSE,
    "judgment": QType.TRUE_FALSE,
    "boolean": QType.TRUE_FALSE,
    "判断": QType.TRUE_FALSE,
    "判断题": QType.TRUE_FALSE,
    "true/false": QType.TRUE_FALSE,
    "true_or_false": QType.TRUE_FALSE,
    "boolean_question": QType.TRUE_FALSE,
}


def build_read_messages(image_png: bytes) -> tuple[str, list[dict[str, Any]]]:
    """构造读题请求的内容块（与 provider 的 ``images`` 分开传，这里只给文本部分）。

    用户消息刻意点明「**所有完整题目**」：长页面上常见一屏两三道题，
    只抄最上面一道会让下面两道各付一次截图 + 一次模型调用。
    """
    return READ_SYSTEM_PROMPT, [
        {
            "type": "text",
            "text": "把这一屏里所有**完整的**题目如实抄录成 JSON（被切掉的题不要抄）。",
        }
    ]


def _repair_json_strings(text: str) -> str:
    """修复模型常见的字符串格式错误：裸换行与未转义的内部双引号。

    视觉模型会把 HTML/CSS、代码块原样写进 JSON 字符串，常见结果是
    ``class="hidden"`` 和真实换行直接破坏 JSON。只在 JSON 字符串内部改写：
    控制字符转义；只有后面紧跟 JSON 分隔符的双引号才视为字符串结束符。
    """
    output: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if not in_string:
            output.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            output.append(char)
            escaped = False
            continue
        if char == "\\":
            output.append(char)
            escaped = True
            continue
        if char == '"':
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead == len(text) or text[lookahead] in ",:}]":
                output.append(char)
                in_string = False
            else:
                output.append('\\"')
            continue
        if char == "\n":
            output.append("\\n")
        elif char == "\r":
            output.append("\\r")
        elif char == "\t":
            output.append("\\t")
        elif ord(char) < 0x20:
            output.append(f"\\u{ord(char):04x}")
        else:
            output.append(char)
    return "".join(output)


def _extract_json(text: str) -> dict[str, Any] | None:
    """从回复里取 JSON，兼容代码围栏及视觉模型常见的坏转义。"""
    stripped = (text or "").strip()
    if not stripped:
        return None
    candidates: list[str] = []
    fenced = _FENCED.search(stripped)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(stripped)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        for source in (candidate, _repair_json_strings(candidate)):
            try:
                payload = json.loads(source)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
    return None


#: 滑动方向别名 → 项目规范方向（口径见 :class:`core.models.PageSwipe.direction`）。
#:
#: 为什么 ``down`` 也归到 ``up``：模型说的「往下滚」与手势「向上滑」是**同一个动作** ——
#: ``up`` 的定义就是「向下滚动看下一题」。同理中文的「向下 / 向上」都指这一步。
#: 真正**不能**收的是 ``right``（往回翻）和任何含糊说法：方向反了是往做过的题走，
#: 那比停下来问人贵得多，所以认不出就是 ``None``（整块 swipe 丢弃）。
_SWIPE_DIRECTIONS: dict[str, str] = {
    "up": "up",
    "down": "up",
    "上": "up",
    "下": "up",
    "向上": "up",
    "向下": "up",
    "left": "left",
    "左": "left",
    "向左": "left",
    "leftward": "left",
}

#: ``swipe.reason`` 只进留痕，限长即可（防止模型把整段思考塞进来）。
_MAX_SWIPE_REASON = 200


def _norm_box(raw: Any) -> tuple[float, float, float, float] | None:
    """校验并规整一个归一化包围框。**非法就丢，不修**。

    宁可少一个坐标（其实是没有可点的目标），也不能把一个越界或倒挂的框
    交给执行层 —— 那会变成一次真实的误点。
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    if not all(0.0 <= v <= 1.0 for v in (x, y, w, h)):
        return None
    if w <= 0 or h <= 0:
        return None
    if x + w > 1.001 or y + h > 1.001:
        return None
    return (x, y, w, h)


def _advance_skill_of(raw: Any) -> str | None:
    """把模型给的 ``advance_skill_id`` 归一化。**只认注册表里的 ID，认不出就是 ``None``。**

    为什么在这里就把未注册 ID 抹掉（而不是留给下游判断）：推进方式是**执行动作**，
    一个不认识的 ID 在下游只有两种命运 —— 被当成某个相近技能（可能点错地方），
    或者让编排层自己猜。两种都比「退回开局裁决的那一套」差。所以未注册一律落 ``None``，
    并在留痕里点名它，让「模型发明了 ID」这件事能被看见。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        logger.warning("读题：advance_skill_id 不是字符串：%r，已忽略", raw)
        return None
    key = raw.strip().lower()
    if not key:
        return None
    spec = get_advance_skill(key)
    if spec is None:
        logger.warning("读题：未注册的 advance_skill_id=%r，已忽略（按开局裁决推进）", raw)
        return None
    return spec.skill_id


def _swipe_direction_of(raw: Any) -> str | None:
    """把模型给的方向归一化。**只认 up / left 两个规范值**（``down`` 见别名表）。"""
    if not isinstance(raw, str):
        return None
    return _SWIPE_DIRECTIONS.get(raw.strip().lower())


def _swipe_of(raw: Any) -> PageSwipe | None:
    """解析并**严格校验** ``page.swipe``。任何一项不合格 → **整块** ``None``。

    为什么不逐项兜底（例如幅度非法就换成默认 0.6）：幅度是「滑多远」的唯一依据，
    猜一个值就是一次可能滑过头的真实手势 —— 而滑过头会**静默跳过好几道题**，
    事后只能从题号跳变里看出来。宁可这一步推不动（停下来问人），也不替模型填数。

    校验口径（与 :class:`core.models.PageSwipe` 的约定一致）：

    * ``direction`` 归一到 ``up`` / ``left``，认不出就整块丢弃；
    * ``amplitude`` 必须能转成 ``float``（数字或数字字符串），且 ``0 < x <= 1``；
      ``bool`` 刻意不收 —— ``True`` 会被 ``float()`` 悄悄变成 ``1.0``（整屏），
      那是模型给了个垃圾值却被当成「滑一整屏」。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning("读题：swipe 不是对象：%r，已忽略", raw)
        return None
    direction = _swipe_direction_of(raw.get("direction"))
    raw_amplitude = raw.get("amplitude")
    amplitude: float | None = None
    if not isinstance(raw_amplitude, bool) and raw_amplitude is not None:
        try:
            amplitude = float(raw_amplitude)
        except (TypeError, ValueError):
            amplitude = None
    if direction is None or amplitude is None or not 0.0 < amplitude <= 1.0:
        logger.warning(
            "读题：swipe 非法（direction=%r amplitude=%r），整块丢弃，不修不猜",
            raw.get("direction"),
            raw_amplitude,
        )
        return None
    reason = raw.get("reason")
    return PageSwipe(
        direction=direction,
        amplitude=amplitude,
        reason=str(reason).strip()[:_MAX_SWIPE_REASON] if reason else "",
    )


def _qtype_key(raw: Any) -> str:
    """把多厂商偶尔返回的 enum-ish 类型标识归一成可匹配的 key。"""
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("name", raw.get("label", "")))
    key = str(raw or "").strip().lower()
    key = re.sub(r"[\s-]+", "_", key)
    if "." in key:
        key = key.rsplit(".", 1)[-1]
    return key


def _qtype_of(raw: Any) -> tuple[QType, str | None]:
    """``(类型, 不支持的原始取值)``；未知 / 缺失题型不猜单选。"""
    key = _qtype_key(raw)
    mapped = _QTYPE_ALIASES.get(key)
    if mapped is not None:
        return mapped, None
    return QType.SINGLE, (key or "missing")


def skill_for_reported_type(raw_qtype: Any) -> tuple[str | None, str | None]:
    """按视觉组报告的题型使用本地 registry 解析技能，不依赖模型复述 ID。

    视觉模型会把 `single_choice` 省成 null，或输出变体字段；项目仅注册了
    single / true_false 两种技能，所以从识别出的 qtype 确定映射仍是封闭且安全的。
    明确不支持的题型保持 None，绝不回退为单选。
    """
    qtype, unsupported = _qtype_of(raw_qtype)
    if unsupported is not None:
        return None, "unsupported_question_type"
    spec = next((entry for entry in SKILL_SPECS if entry.qtype is qtype), None)
    if spec is None:
        return None, "no_matching_skill"
    return spec.skill_id, None


#: 单个质量条目允许的最大长度。模型偶尔会把整句解释塞进 ``clipped`` 数组，
#: 那既不是定位信息、又会让留痕里塞满散文 —— 截断而不是丢弃（至少还看得出有东西）。
_MAX_QUALITY_ITEM = 60

#: 质量字段最多收多少条。防止模型把整页元素都列进来把留痕撑爆。
_MAX_QUALITY_ITEMS = 20


def _str_list(raw: Any) -> list[str]:
    """把模型给的「残缺/拿不准」清单规整成 ``list[str]``。

    容忍 ``["stem", "option:D"]`` 与 ``"stem,option:D"`` 两种写法，
    去重、去空、限长、限量。**非列表的垃圾一律当空** —— 这一组字段宁可漏报
    （门禁放行，行为与加门禁之前一致），也不要因为一个畸形值把整题误拦。
    """
    items: list[str] = []
    if isinstance(raw, str):
        items = [part for part in re.split(r"[,，、;；\s]+", raw) if part]
    elif isinstance(raw, list | tuple):
        items = [str(entry) for entry in raw]
    else:
        return []

    cleaned: list[str] = []
    for item in items:
        text = item.strip().strip("\"'")
        if not text:
            continue
        text = text[:_MAX_QUALITY_ITEM]
        if text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= _MAX_QUALITY_ITEMS:
            break
    return cleaned


def _flag_of(raw: Any) -> bool:
    """把模型给的布尔值规整成 ``bool``。

    ``"false"`` / ``"true"`` / ``0`` / ``1`` 都得认 —— 模型经常把布尔写成字符串，
    而 ``bool("false") is True`` 这种坑一旦踩到，就会把「还有内容」判成「没有了」。
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "yes", "1", "y"}
    if isinstance(raw, int | float):
        return bool(raw)
    return False


def _submit_scope_of(raw: Any) -> SubmitScope | None:
    """把模型给的 ``submit_scope`` 归一化。**只认明确的词，认不出就是 ``None``。**

    为什么**宁可返回 ``None`` 也不猜**：编排层对 ``None`` 有结构性兜底
    （一屏多题 → 按整卷处理，提交推迟到收尾），而对 ``QUESTION`` 的解释是
    「每做完一题就点一次提交按钮」—— 在整卷页面上那就是**做完第 1 题交卷**。
    把不确定解析成果断动作，是这条链上代价最大的错误，所以拿不准就交回上层。

    只接受 ``question`` / ``paper`` 两个规范值（以及中文的「本题」「整卷」）。
    ``"submit"`` 这类含糊说法**故意不收** —— 它既可能指本题也可能指整卷。
    """
    if not isinstance(raw, str):
        return None
    token = raw.strip().lower()
    if token in {"paper", "whole_paper", "all", "整卷", "整份", "交卷"}:
        return SubmitScope.PAPER
    if token in {"question", "item", "one", "本题", "单题"}:
        return SubmitScope.QUESTION
    return None


def _scan_question_objects(text: str) -> list[dict[str, Any]]:
    """从回复里**尽量多**地抠出题目对象 —— 哪怕整份 JSON 是截断的。

    为什么需要它（2026-09-28 实测故障）：提示词改成「一屏多题」后单次回复变长，
    token 预算一紧就会被**截断**；而截断的 JSON 用 ``json.loads`` 是**整份失败**的 ——
    明明前面几道题抄得完整，却一起丢掉，界面上只看到一句 ``perception_failed``。

    做法是逐个对象扫描：从第一个 ``{`` 起 ``raw_decode``，成功一个就接着找下一个，
    遇到截断就停下、把已经拿到的交出去。**只保留「像题目」的对象**
    （含 ``stem`` 或 ``options`` 键），否则选项、``formula`` 这些嵌套小对象也会被捞上来。
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    index = 0
    length = len(text)
    while index < length:
        start = text.find("{", index)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            # 这一处不是合法对象（多半正好是被截断的那一个）→ 挪一格再找
            index = start + 1
            continue
        if isinstance(obj, dict) and ("stem" in obj or "options" in obj):
            found.append(obj)
        index = max(end, start + 1)
    return found


def parse_read_batch(text: str) -> ReadBatch | None:
    """把模型回复解析成 :class:`ReadBatch`（**一屏可能有多道题**）。

    三种形态都认 —— 提示词与模型之间的偏差不该让整次读题白费：

    - **新格式**：``{"page": {...}, "questions": [{...}, {...}], "more_below": ..., "note": ...}``
    - **旧格式**：整份就是一个题目对象（等价于只读出一道题）
    - **裸数组 / 被截断的回复**：交给 :func:`_scan_question_objects` 逐个抢救，
      完整几道就交几道，总比「一道都没有」强

    单道题解析失败**只跳过它**：一屏三题里坏了一道，没有理由把另外两道也扔掉。

    **一道题都没有、但有 ``page`` 观测时仍然返回一批**（``questions == []``）：
    收尾那一屏（「已全部答完」的提示页）本来就是没有题的，而那一屏的
    ``page.completed`` 正是收工判据 —— 在解析层就因为「没有题」而返回 ``None``，
    等于把收尾闸门的输入扔掉了。只有**既没有题、也没有观测**才算「这次读题没成」。
    """
    payload = _extract_json(text)
    items: list[dict[str, Any]]
    more_below = False
    note = ""
    page: PageView | None = None
    if payload is not None:
        page = parse_page_view(payload)
        raw_questions = payload.get("questions")
        if isinstance(raw_questions, list):
            items = [item for item in raw_questions if isinstance(item, dict)]
        else:
            # 回复里只有裸的题目对象：它同时也是「page」的候选载体（旧契约），
            # 但旧契约里没有 page 块，所以这里不额外解析。
            items = [payload] if page is None else []
        more_below = _flag_of(payload.get("more_below"))
        note = str(payload.get("note") or "").strip()[:200]
    else:
        items = _scan_question_objects(text)
        more_below = bool(re.search(r'"more_below"\s*:\s*true', text))

    questions = [parsed for item in items if (parsed := _parse_one(item)) is not None]
    if not questions and page is None:
        return None

    return ReadBatch(
        questions=questions,
        page=page,
        more_below=more_below,
        note=note,
        raw=(text or "").strip()[:2000],
    )


def _parse_one(item: dict[str, Any]) -> ReadResult | None:
    """解析**单道题**的对象。解析不出来返回 ``None``。

    **不抛异常**：读题失败是一件正常的事（模型答非所问、图太糊），
    交给调用方决定怎么记账，比在深处炸掉好。
    """
    stem = str(item.get("stem") or "").strip()
    if not stem:
        return None

    raw_options = item.get("options")
    if not isinstance(raw_options, list):
        return None

    raw_qtype = item.get("qtype")
    qtype, unsupported = _qtype_of(raw_qtype)
    options: list[ReadOption] = []
    for index, entry in enumerate(raw_options):
        if not isinstance(entry, dict):
            continue
        text_value = str(entry.get("text") or "").strip()
        if not text_value:
            continue
        box = _norm_box(entry.get("box"))
        if box is None:
            # 没有可点区域的选项留着也没用（执行层点不到），而且会让选项数与
            # 页面上真实存在的选项对不上 —— 解题组拿到的题面就此与页面对不齐。
            # 丢弃并告警。
            logger.warning("读题：选项 %s 缺可用的 box，已丢弃", entry.get("label"))
            continue
        label = str(entry.get("label") or "").strip() or _LABELS[index]
        options.append(ReadOption(label=label, text=text_value, box=box))

    if not options and unsupported is None:
        # 已支持题型缺少可点选项时是残缺读题，不可创建无证据的可执行任务。
        return None

    raw_skill_id = item.get("skill_id")
    raw_skill_key = str(raw_skill_id).strip().lower() if raw_skill_id is not None else None
    spec = get_skill(raw_skill_key)
    skill_id: str | None = raw_skill_key
    skill_error: str | None = None
    if unsupported is not None:
        skill_id = None
        skill_error = "unsupported_question_type"
    elif spec is not None and spec.qtype is qtype:
        skill_id = spec.skill_id
    elif spec is not None:
        skill_error = "skill_type_mismatch"
        skill_id = None
    else:
        # skill_id 是模型重复输出的冗余字段；以识别出的 qtype 通过本地 registry
        # 补全唯一合法映射。只有明示的非空错误 ID 才记录为 mismatch。
        inferred_id, inference_error = skill_for_reported_type(raw_qtype)
        if raw_skill_key is not None:
            skill_error = "unknown_skill_id"
            skill_id = None
        elif inference_error == "unsupported_question_type" and unsupported is None:
            # 题型已识别（如 multiple），但技能库没有对应实现。
            skill_error = "no_matching_skill"
            skill_id = None
        else:
            skill_id = inferred_id
            skill_error = inference_error

    note = str(item.get("note") or "").strip()
    num_text = item.get("num_text")
    return ReadResult(
        stem=stem,
        qtype=qtype,
        options=options,
        skill_id=skill_id,
        skill_error=skill_error,
        reported_skill_id=raw_skill_key,
        reported_qtype=str(raw_qtype).strip()[:60] if raw_qtype is not None else None,
        unsupported_qtype=unsupported,
        clipped=_str_list(item.get("clipped")),
        uncertain=_str_list(item.get("uncertain")),
        more_below=_flag_of(item.get("more_below")),
        note=note[:200],
        index=_positive_int(item.get("index")) or 0,
        num_text=str(num_text).strip()[:40] if num_text else None,
    )


def parse_read_payload(text: str) -> ReadResult | None:
    """**兼容入口**：按旧语义返回**第一道**题。解析不出来返回 ``None``。

    保留它是因为「读题 = 一道题」的调用方还有不少（诊断脚本、门禁单测）。
    新代码请用 :func:`parse_read_batch` —— 一屏多题时只取第一道会白白丢掉其余题目。
    """
    batch = parse_read_batch(text)
    if batch is None or not batch.questions:
        return None
    return batch.questions[0]


def gate_read_result(result: ReadResult) -> tuple[bool, str | None]:
    """**门禁**：这份读题结果能不能下传给解题组。返回 ``(放行?, 拦截原因码)``。

    这是「读题错了也不报错」这条链路唯一的出口，判据只有一条：

        **模型自己说这份题面不完整 / 它拿不准 → 不许下传。**

    为什么拦得这么狠（**非空就拦**，不做轻重分级）：

    - 读题是全链路唯一**错了也不报错**的环节。少一个负号、漏一个指数、
      被视口截掉半行 —— 输出看起来完全正常，下游会算出一个**看起来正常的错答案**，
      再照着坐标点到用户的真实页面上。事后从日志里根本看不出发生过什么。
    - 能自己承认"这里我没把握"的模型，**正是最不该被忽略的那种信号**。
      放行等于告诉它：说了也没用。
    - 拦截的代价只是**停下来问人**（``paused_for_dump`` 那条既有路径），
      而放行的代价是一次静默的错误作答。两侧代价不对称，所以宁可拦。

    复核过之后仍然不确定的，同样拦 —— 那说明这道题在**当前画面**上就是读不出来，
    需要人给一张更清楚的图，或者换个位置重截。

    不拦的情况（刻意）：
    - ``more_below``：它只表示"下方**可能**还有内容"，是"多题同页"的正常形态，
      单凭它拦会让每一页长题干都停下来。它进留痕，不进判据。
    - 「画面上没有提交按钮」：**不归门禁管**（2026-09-30 起）。提交时机由开局裁决的
      :class:`~core.models.RunPlan` 决定，与「这一屏抄得全不全」是两件事。
      旧版在这里顺手拦了一下，真机上的表现是「整卷页面做到一半被暂停」。
    """
    if result.clipped:
        return False, "vision_incomplete"
    if result.uncertain:
        return False, "vision_uncertain"
    return True, None


def parse_page_view(payload: dict[str, Any]) -> PageView | None:
    """解析回复里的 ``page`` 观测块（2026-09-30 加）。没有这一块返回 ``None``。

    这是「程序那一套判断逻辑」的**唯一输入**，所以解析纪律是
    **只认结构，不认散文**：

    * 每个字段都能缺（缺 = ``None`` / ``UNKNOWN``），**绝不填默认猜测值** ——
      「不知道」必须一路传到 :func:`core.run_plan.derive_plan`，
      由它按固定顺序裁决，而不是在解析层就悄悄变成「有」或「没有」；
    * ``box`` 一律走 :func:`_norm_box`（越界 / 倒挂 / 零面积都判非法 → ``None``）：
      一个非法框在下游就是一次真实误点的坐标；
    * ``card`` 的 ``cols`` / ``rows`` 只在**正整数**时才收（0 与负数等于「看不出网格」）。
    """
    raw = payload.get("page")
    if not isinstance(raw, dict):
        return None

    control: PageControl | None = None
    raw_control = raw.get("next_control")
    if isinstance(raw_control, dict):
        box = _norm_box(raw_control.get("box"))
        if box is not None:
            label = raw_control.get("label")
            control = PageControl(
                box=box, label=str(label).strip()[:40] or None if label is not None else None
            )

    card: PageCard | None = None
    raw_card = raw.get("card")
    if isinstance(raw_card, dict):
        card_box = _norm_box(raw_card.get("box"))
        if card_box is not None:
            cols = _positive_int(raw_card.get("cols")) or 0
            rows = _positive_int(raw_card.get("rows")) or 0
            card = PageCard(
                box=card_box,
                cols=min(cols, 64),
                rows=min(rows, 200),
                current_box=_norm_box(raw_card.get("current_box")),
                next_box=_norm_box(raw_card.get("next_box")),
            )

    submit: PageSubmit | None = None
    raw_submit = raw.get("submit")
    if isinstance(raw_submit, dict):
        submit_box = _norm_box(raw_submit.get("box"))
        scope = _submit_scope_of(raw_submit.get("scope"))
        if submit_box is not None or scope is not None:
            submit = PageSubmit(box=submit_box, scope=scope)

    total = _positive_int(raw.get("total"))
    current = _positive_int(raw.get("current"))
    if current is not None and total is not None and current > total:
        # 两个数互相矛盾 → 信总数、丢掉当前题号（与旧标定同一条口径）。
        current = None

    progress = raw.get("progress")
    reason = raw.get("reason")
    scrolling = raw.get("scrolling")
    # —— 推进目标（2026-09-30 加）：这一步该怎么进下一题 ——
    # 它是**本屏**的观测，不是开局结论：几何漂移后仍沿用旧落点，正是
    # 「推进后读到的题与实际对不上」的成因，所以每一步都由模型重新给、这里重新校验。
    advance_skill_id = _advance_skill_of(raw.get("advance_skill_id"))
    swipe = _swipe_of(raw.get("swipe"))
    if advance_skill_id == ADVANCE_SWIPE_SKILL_ID and swipe is None:
        # 滑动技能的**唯一载荷**就是方向 + 幅度：没有合法载荷时这个 ID 落不了地，
        # 交下去只会让执行层「按某种方式滑一下」——那正是上一版一跳十几题的来源。
        # 所以连带把技能选择也降级为「没选」，让程序退回开局裁决。
        logger.warning("读题：advance_swipe 缺合法 swipe 载荷，已降级为未选推进技能")
        advance_skill_id = None
    return PageView(
        progress=str(progress).strip()[:40] or None if progress is not None else None,
        total=total,
        current=current,
        next_control=control,
        card=card,
        submit=submit,
        completed=_completion_of(raw.get("completed")),
        scrolling=scrolling if isinstance(scrolling, bool) else None,
        advance_skill_id=advance_skill_id,
        swipe=swipe,
        reason=str(reason).strip()[:200] if reason else "",
    )


def _completion_of(raw: Any) -> CompletionState:
    """把 ``completed`` 归一化。**只认明确说「全部做完」的说法**，其余一律 ``UNKNOWN``。

    为什么默认方向是「不知道」而不是「没做完」：这两者的下游动作不同 ——
    ``NOT_DONE`` 会让程序继续推进（在那个页面上推不动又会停下来，代价可控），
    ``UNKNOWN`` 只影响留痕与提示。而把「不知道」当成 ``ALL_DONE`` 会**直接收工**，
    后面所有题都不再作答，所以那一边绝对不能靠默认值滑过去。
    """
    if isinstance(raw, bool):
        return CompletionState.ALL_DONE if raw else CompletionState.NOT_DONE
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in {"all_done", "done", "completed", "finished", "全部完成", "已完成"}:
            return CompletionState.ALL_DONE
        if token in {"not_done", "unfinished", "还有题", "未完成"}:
            return CompletionState.NOT_DONE
    return CompletionState.UNKNOWN


def _positive_int(value: Any) -> int | None:
    """``True`` 也是 ``int`` —— 必须先挡掉布尔，否则 ``total: true`` 会变成 1。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def stem_fingerprint(stem: str) -> str:
    """由题干算 ``qid`` 的前缀。

    与 ``Question.qid`` 的既有口径保持一致：**不含答案**，只由题干决定。
    同一道题读两遍（重截一次图 / 换了模型重读）会得到同一个 qid ——
    这是缓存命中与「这题读了两遍」判定能成立的前提。
    """
    normalized = re.sub(r"\s+", "", stem or "")
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


async def read_questions(
    image_png: bytes,
    *,
    providers: list[LLMProvider],
    force_json: bool = True,
    raw_sink: Any | None = None,
) -> tuple[ReadBatch | None, str | None]:
    """让模型读**一张图**，返回这一屏里的**全部**题目：``(ReadBatch | None, 错误码)``。

    按**降级链**逐个尝试，全部失败返回 ``(None, 最后一个错误码)`` ——
    与求解层同款策略：一次抖动不该毁掉整道题，但也不能假装成功。

    ``raw_sink`` 是可选回调：解析失败时把**原始响应**（``response.raw``，
    即服务端原文，不是抽出来的正文）交出去，供调用方落盘。它的存在是因为
    抽出来的 ``response.text`` 可能是**空串** —— 那时「模型回了什么」就只剩
    这份原文能回答，不落盘就是又一次「读题失败、无证据可查」。

    一屏多题是这个函数的**正常形态**（长页面上常见），调用方不要假设只有一道。
    """
    last_error: str | None = "provider_unavailable"
    system, content = build_read_messages(image_png)
    for provider in providers:
        request = LLMRequest(
            model=provider.model_for() or "",
            system=system,
            user=json.dumps(content, ensure_ascii=False),
            images=[image_png],
            temperature=READ_TEMPERATURE,
            max_tokens=READ_MAX_TOKENS,
            force_json=force_json,
        )
        rate_attempt = 0
        while True:
            try:
                response = await provider.complete(request)
            except ProviderError as exc:
                last_error = exc.code
                if isinstance(exc, RateLimitError) and rate_attempt < READ_RATE_LIMIT_RETRIES:
                    rate_attempt += 1
                    logger.warning(
                        "视觉读题触发限流，%s 秒后重试（%s/%s）",
                        READ_RATE_LIMIT_RETRY_DELAY_S,
                        rate_attempt,
                        READ_RATE_LIMIT_RETRIES,
                    )
                    await asyncio.sleep(READ_RATE_LIMIT_RETRY_DELAY_S)
                    continue
                break
            parsed = parse_read_batch(response.text)
            if parsed is not None:
                return parsed, None
            # 解析不出来时把**原始响应**交出去留痕 + 打进日志 —— 这是唯一能看出
            # 「模型到底回了什么」的地方，而排查读题失败最需要的正是它。
            # （2026-09-28 的 `perception_failed` 事故里，原始回复一点痕迹都没留，
            #   只能靠事后猜，代价是整整一轮排查。）
            # 2026-09-29：这里原先只打 ``response.text``（抽出来的正文），而推理模型
            # 可能把它留空、答案放在别的字段 → 日志里只看到一行空标题。改成打
            # ``response.raw``（服务端原文），空正文也能看到真实结构。
            raw = (response.raw or response.text or "").strip()
            if raw_sink is not None:
                with suppress(Exception):
                    raw_sink(raw)
            logger.warning("读题回复无法解析（原始响应前 600 字）：%s", raw[:600])
            last_error = "read_parse_failed"
            break
        if last_error in {"rate_limited", "provider_unavailable"}:
            logger.error(
                "视觉 provider %s 最终失败：%s；尝试下一 provider",
                provider.model_for() or type(provider).__name__,
                last_error,
            )
    return None, last_error


async def read_question(
    image_png: bytes,
    *,
    providers: list[LLMProvider],
    force_json: bool = True,
) -> tuple[ReadResult | None, str | None]:
    """**兼容入口**：只取这一屏的**第一道**题。

    保留它是因为「读题 = 一道题」的调用方还有不少（诊断脚本、既有单测）。
    走的是同一条 :func:`read_questions`，所以**不会多发一次请求**；
    但一屏多题时它会**丢掉其余题目** —— 编排层请直接用 :func:`read_questions`。
    """
    batch, error = await read_questions(image_png, providers=providers, force_json=force_json)
    if batch is None:
        return None, error
    if not batch.questions:
        return None, "read_empty"
    return batch.questions[0], None


def to_question(result: ReadResult, *, qid_prefix: str = "v") -> Any:
    """``ReadResult`` → ``Question``，接进既有求解链路。

    几何**不进** ``Question``（理由见 ``core/models.ReadResult`` 的说明），
    而是由调用方另行保存，供执行层按坐标点击。
    """
    from core.models import Option, Question

    options = [
        Option(index=index, label=opt.label, text=opt.text, raw=opt.text)
        for index, opt in enumerate(result.options)
    ]
    fingerprint = stem_fingerprint(result.stem)
    spec = get_skill(result.skill_id)
    skill_error = result.skill_error
    if skill_error is None and (spec is None or spec.qtype is not result.qtype):
        skill_error = "unsupported_question_type" if result.unsupported_qtype else (
            "no_matching_skill" if spec is None else "skill_type_mismatch"
        )
    reported = result.reported_qtype or result.unsupported_qtype
    return Question(
        qid=f"{qid_prefix}{fingerprint[:15]}",
        stem=result.stem,
        stem_hash=fingerprint,
        qtype=result.qtype,
        options=options,
        source=ProbeName.VISION,
        skill_id=spec.skill_id if spec is not None and spec.qtype is result.qtype else None,
        skill_error=skill_error,
        reported_skill_id=result.reported_skill_id,
        reported_qtype=reported,
        channel_trace=["vision:read_from_image", f"vision:options={len(options)}"],
    )
