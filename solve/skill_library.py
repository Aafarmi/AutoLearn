"""技能库：题型技能（怎么解一道题）+ 推进技能（怎么进下一题）。

技能 ID 与支持的题型映射集中在这里；技能正文独立存放于仓库根 ``skills/``。
对不上题型、技能不存在、或题型尚无技能时必须返回 ``None``，调用方不得猜测。

**两张注册表是分开的**（2026-09-30 加推进技能）：

* **题型技能**按 ``qtype`` 一一对应，回答「这道题怎么解」，由解题组加载；
* **推进技能**回答「**这一步**怎么进下一题」（点控件 / 滑动 / 点答题卡题号），
  由视觉组**每一步**按当前画面选，不绑任何题型。

为什么必须分开：推进技能一旦混进题型表，模型会把「选了滑动」读成「这道题属于某类题」，
而误判题型的代价是整题解错；反过来把题型 ID 填进 ``advance_skill_id`` 则会让执行层
拿到一个它不认识的推进目标。两张表各自封闭，ID 不许互相借用（有单测卡着）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.enums import QType

__all__ = [
    "ADVANCE_SKILL_SPECS",
    "ADVANCE_SWIPE_SKILL_ID",
    "SKILLS_DIR_NAME",
    "SKILL_SPECS",
    "AdvanceSkillSpec",
    "SkillSpec",
    "advance_skill_catalog",
    "advance_skill_prompt",
    "get_advance_skill",
    "get_skill",
    "skill_for_question",
    "skill_prompt",
    "vision_skill_catalog",
]

SKILLS_DIR_NAME = "skills"

#: ``advance_swipe`` 的 ID 单独提出来给解析层用：它有一条**额外载荷要求**
#: （必须同时给出 ``swipe`` 的方向与幅度），解析层要按 ID 判断，
#: 不能让这个字面量散落在解析代码里。
ADVANCE_SWIPE_SKILL_ID = "advance_swipe"


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """可供解题组使用的一项正式技能。"""

    skill_id: str
    qtype: QType
    filename: str
    vision_hint: str
    label: str


# 唯一注册点。没有注册在这里的题型不下传给解题组。
SKILL_SPECS: tuple[SkillSpec, ...] = (
    SkillSpec(
        skill_id="single_choice",
        qtype=QType.SINGLE,
        filename="single_choice.md",
        label="单项选择题",
        vision_hint="明确只有一个正确答案的选择题；qtype=single，skill_id=single_choice。",
    ),
    SkillSpec(
        skill_id="true_false",
        qtype=QType.TRUE_FALSE,
        filename="true_false.md",
        label="判断题",
        vision_hint="判断陈述正误（正确/错误、对/错、是/否）；qtype=true_false，skill_id=true_false。",
    ),
)

_BY_ID = {spec.skill_id: spec for spec in SKILL_SPECS}


@dataclass(frozen=True, slots=True)
class AdvanceSkillSpec:
    """可供视觉组选择的一项**推进技能**。

    ``needs_swipe`` 声明这条技能是否**必须带载荷**：``advance_swipe`` 只能靠
    「方向 + 幅度」执行，光有一个 ID 落不了地 —— 解析层据此把缺载荷的选择降级为
    「没选」，免得执行层拿到一句「滑一下」却没有任何依据（上一版滑过头跳题正是这么来的）。
    """

    skill_id: str
    filename: str
    label: str
    vision_hint: str

    @property
    def needs_swipe(self) -> bool:
        """是否需要 ``page.swipe`` 载荷。目前只有滑动技能需要。"""
        return self.skill_id == ADVANCE_SWIPE_SKILL_ID


#: **推进技能**的唯一注册点（与题型技能分开）。没有注册在这里的 ID 一律当「没选」。
ADVANCE_SKILL_SPECS: tuple[AdvanceSkillSpec, ...] = (
    AdvanceSkillSpec(
        skill_id="advance_click",
        filename="advance_click.md",
        label="点控件进下一题",
        vision_hint=(
            "画面上有一个固定的「下一题 / 下一页 / 继续 / 保存并下一题」控件，"
            "点它就能进下一题：`advance_skill_id=\"advance_click\"`，"
            "控件框照旧写在 `page.next_control.box`（按**本屏**重新量）。"
        ),
    ),
    AdvanceSkillSpec(
        skill_id=ADVANCE_SWIPE_SKILL_ID,
        filename="advance_swipe.md",
        label="滑动进下一题",
        vision_hint=(
            "题目在同一屏里排成一行 / 一列，没有可点的推进控件，只能把它滑走："
            "`advance_skill_id=\"advance_swipe\"`，**并且必须**给出 `page.swipe`"
            "（`direction` 取 `up` / `left`，`amplitude` 是 0~1 的比例，另附一句 `reason`）。"
        ),
    ),
    AdvanceSkillSpec(
        skill_id="advance_card",
        filename="advance_card.md",
        label="点答题卡题号进下一题",
        vision_hint=(
            "题号答题卡（一片由题号数字组成的网格）在画面上："
            "`advance_skill_id=\"advance_card\"`，网格几何照旧写在 `page.card`"
            "（`box` / `cols` / `rows` / `current_box` / `next_box`，按本屏重新量）。"
        ),
    ),
)

_ADVANCE_BY_ID = {spec.skill_id: spec for spec in ADVANCE_SKILL_SPECS}


def get_advance_skill(skill_id: str | None) -> AdvanceSkillSpec | None:
    """按视觉组报告的 ID 查推进技能；未知、空、非字符串一律无匹配。"""
    if not isinstance(skill_id, str):
        return None
    return _ADVANCE_BY_ID.get(skill_id.strip().lower())


def get_skill(skill_id: str | None) -> SkillSpec | None:
    """按模型选择的 ID 查注册项；未知或空 ID 一律无匹配。"""
    if not isinstance(skill_id, str):
        return None
    return _BY_ID.get(skill_id.strip().lower())


def skill_for_question(qtype: QType, skill_id: str | None) -> SkillSpec | None:
    """只有技能 ID 与判定题型**同时匹配**才放行。"""
    spec = get_skill(skill_id)
    return spec if spec is not None and spec.qtype is qtype else None


def _skills_dir() -> Path:
    """定位技能正文目录；兼容源码、PyInstaller 与工作目录启动。"""
    candidates = [
        Path(__file__).resolve().parents[1] / SKILLS_DIR_NAME,
        Path.cwd() / SKILLS_DIR_NAME,
    ]
    if getattr(sys, "frozen", False):  # pragma: no cover - 打包态
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.insert(0, Path(meipass) / SKILLS_DIR_NAME)
        candidates.insert(1, Path(sys.executable).resolve().parent / SKILLS_DIR_NAME)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("技能库目录 skills/ 缺失；不允许无技能规则地继续解题")


def _read_skill_body(filename: str) -> str:
    """读一份技能正文。文件缺失、读不了、或为空都**大声失败**，不静默降级。

    两个注册表共用这一段读取逻辑：技能正文有没有、读不读得出来，与它是题型技能
    还是推进技能无关，判据只该有一处。
    """
    path = _skills_dir() / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise FileNotFoundError(f"技能文件不可用：{path}") from exc
    if not text:
        raise ValueError(f"技能文件为空：{path}")
    return text


@lru_cache(maxsize=len(SKILL_SPECS))
def skill_prompt(skill_id: str) -> str:
    """读取指定题型技能正文。注册技能文件缺失或为空时大声失败，不静默降级。"""
    spec = get_skill(skill_id)
    if spec is None:
        raise ValueError(f"未知技能 ID：{skill_id!r}")
    return _read_skill_body(spec.filename)


@lru_cache(maxsize=len(ADVANCE_SKILL_SPECS))
def advance_skill_prompt(skill_id: str) -> str:
    """读取指定**推进技能**正文。未知 ID 或文件缺失/为空同样大声失败。

    与 :func:`skill_prompt` 分开缓存：两张注册表的条目各自独立，
    混在一个缓存里会让「改一份文件」的失效范围变得说不清。
    """
    spec = get_advance_skill(skill_id)
    if spec is None:
        raise ValueError(f"未知推进技能 ID：{skill_id!r}")
    return _read_skill_body(spec.filename)


def _question_skill_catalog() -> str:
    """题型技能目录：视觉组只做分类，不解题。"""
    lines = [
        "## 技能库选择（只做分类，不解题）",
        "只需准确标注 qtype；程序会从本地注册表自动选择唯一匹配的技能 ID。",
        "视觉模型可省略 skill_id；不支持的题型由程序保存并跳过，不要强行映射。",
    ]
    lines.extend(f"- `{spec.skill_id}`：{spec.vision_hint}" for spec in SKILL_SPECS)
    lines.append("- 其他题型（例如多选、填空、简答）：`skill_id: null`，不得套用相近技能。")
    return "\n".join(lines)


def advance_skill_catalog() -> str:
    """**推进技能**目录（拼进视觉组 system prompt）。

    单独成段而不是并进题型表：两者回答的是不同问题（这道题怎么解 / 这一步怎么走）。
    段首写死两条纪律 —— **每一步重新判断**、**没有控件时按画面估幅度** ——
    它们正是「读回来的题与实际对不上」那条事故链的修复点。
    """
    lines = [
        "## 推进技能选择（**每一步**都要重新判断）",
        "推进技能回答「这一步怎么进下一题」，与题型技能是两回事，不要混用 ID。",
        "**每一步都要按当前这一屏重新选、重新量坐标**：换屏之后上一屏的控件框、"
        "答题卡格子与滑动幅度都可能已经不准 —— 沿用旧值正是「推进后与实际不符」的成因。",
        "看不到「下一题」控件、题目又在同一屏里排着时，选 `advance_swipe` 并给出幅度依据。",
        "选不出来（这一屏看不到任何推进入口）就**省略** `advance_skill_id`，程序退回开局裁决。",
    ]
    lines.extend(f"- `{spec.skill_id}`：{spec.vision_hint}" for spec in ADVANCE_SKILL_SPECS)
    lines.append("不要发明 ID，也不要用题型技能 ID（single_choice 等）冒充推进技能 ID。")
    return "\n".join(lines)


def vision_skill_catalog() -> str:
    """视觉组可选技能清单 = **题型技能目录 + 推进技能目录**。

    两段必须在**同一份** system prompt 里给出：视觉组每次只回答一个 JSON，
    分两次请求会让「这一屏有什么」出现两套说法（那正是上一版被删掉的理由，
    见 :mod:`solve.reader` 模块头）。
    """
    return "\n\n".join((_question_skill_catalog(), advance_skill_catalog()))

