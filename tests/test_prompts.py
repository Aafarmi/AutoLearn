"""解题组消息组装与响应解析（p6.0 的标号口径 + ``confidence`` 契约）。

这一层守的是**「模型看到什么、我们怎么读回来」**，两件事都属于
「错了也不报错」的那一类：

1. **标号口径**：渲染给模型的标号必须是**页面标号**。错一次，下游就拿着
   错标号去点页面上的另一个选项，而输出上完全看不出来。视觉组还可能给出
   **非连续标号**（某个选项的框被丢弃时是 ``A, B, D``）—— 位置字母在那种题上
   必定错位，所以这里专门钉一条。
2. **``confidence``**：它是「要不要停下问人」的输入（低于
   ``guards.confidence_review_min`` 必停）。解析不出就等于门限消失；
   把「模型没报」误当成「报了 0」则会让每一题都停 —— 两个方向都要钉住。
3. **「不可用」与「明确空作答」**必须分得开：前者触发有界重发，
   后者是模型的结论（不重发）。
"""

from __future__ import annotations

import pytest

from core.enums import ProbeName, QType
from core.models import Option, Question
from solve.prompts import (
    CONFIDENCE_KEY,
    EXPLICIT_EMPTY_KEY,
    PROMPT_VERSION,
    build_messages,
    option_pairs,
    parse_answer_payload,
    parse_confidence,
    parse_presented_options,
    render_options,
)


def make_question(
    texts: list[str],
    *,
    labels: list[str] | None = None,
    qtype: QType = QType.SINGLE,
) -> Question:
    resolved = labels if labels is not None else [chr(ord("A") + i) for i in range(len(texts))]
    return Question(
        qid="0123456789abcdef",
        stem="下列说法正确的是？",
        stem_hash="stem-hash",
        qtype=qtype,
        options=[
            Option(index=i, label=resolved[i], text=text, raw=text)
            for i, text in enumerate(texts)
        ],
        source=ProbeName.VISION,
        skill_id={QType.SINGLE: "single_choice", QType.TRUE_FALSE: "true_false"}.get(qtype),
    )


# --------------------------------------------------------------------------- #
# 版本
# --------------------------------------------------------------------------- #
def test_prompt_version_is_p7() -> None:
    """技能选择与技能正文进入用户消息，旧格式留痕必须区分。"""
    assert PROMPT_VERSION == "p7.0"


# --------------------------------------------------------------------------- #
# 标号口径：页面标号，不是位置字母
# --------------------------------------------------------------------------- #
def test_options_are_rendered_with_page_labels() -> None:
    question = make_question(["甲", "乙", "丙"], labels=["A", "B", "D"])
    _, user = build_messages(question, option_pairs(question, question.option_texts))

    assert "A. 甲" in user
    assert "B. 乙" in user
    assert "D. 丙" in user
    assert "\nC. " not in user, "页面上没有 C，就不能渲染出 C"


def test_solver_user_message_includes_selected_skill_text() -> None:
    from solve.skill_library import skill_prompt

    question = make_question(["正確", "錯誤"], qtype=QType.TRUE_FALSE)
    question = question.model_copy(update={"skill_id": "true_false"})
    _, user = build_messages(
        question,
        [("A", "正確"), ("B", "錯誤")],
        skill_text=skill_prompt("true_false"),
    )

    assert "题型: 判断题" in user
    assert "技能ID: true_false" in user
    assert "陈述成立" in user
    assert "不得假定" in user


def test_presented_options_round_trip_keeps_page_labels() -> None:
    """``parse_presented_options`` 是 MockProvider 的眼睛，必须读回同一份标号。"""
    question = make_question(["甲", "乙", "丙"], labels=["A", "B", "D"])
    _, user = build_messages(question, [("A", "甲"), ("B", "乙"), ("D", "丙")])

    assert parse_presented_options(user) == [("A", "甲"), ("B", "乙"), ("D", "丙")]


def test_render_options_never_renumbers() -> None:
    """给什么标号就渲染什么标号 —— 它不做「按位置重排」这件事。"""
    assert render_options([("A", "甲"), ("D", "丙")]) == ["A. 甲", "D. 丙"]


def test_bare_texts_are_paired_with_page_labels() -> None:
    """历史调用方传裸正文时，标号取自 ``question.options[i].label``。"""
    question = make_question(["甲", "乙", "丙"], labels=["A", "B", "D"])
    _, user = build_messages(question, ["甲", "乙", "丙"])

    assert "D. 丙" in user
    assert "\nC. " not in user


def test_message_echoes_the_full_answer_contract() -> None:
    """用户消息末尾回显的契约必须与提示词一致（含 ``confidence``）。"""
    question = make_question(["甲", "乙"])
    _, user = build_messages(question, question.option_texts)

    assert "confidence" in user
    assert "chosen_labels" in user
    assert "reason" in user


# --------------------------------------------------------------------------- #
# confidence 解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.8, 0.8),
        (1, 1.0),
        ("0.8", 0.8),
        ("80%", 0.8),
        (" 92 % ", 0.92),
        ("0", 0.0),
    ],
)
def test_confidence_accepts_numbers_strings_and_percent(raw: object, expected: float) -> None:
    assert parse_confidence(raw) == expected


def test_out_of_range_confidence_is_clamped_not_dropped() -> None:
    """越界夹到 ``[0, 1]``：写法夸张不等于「模型没报」。"""
    assert parse_confidence(1.5) == 1.0
    assert parse_confidence(-0.2) == 0.0
    assert parse_confidence("150%") == 1.0


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "不知道", "大概八成", True, False, [0.5], {"v": 1}, float("nan")],
)
def test_unparsable_confidence_is_none(raw: object) -> None:
    """解析不出来 = **模型没报**（调用方据此保持旧语义，不新增暂停）。"""
    assert parse_confidence(raw) is None


def test_answer_payload_carries_confidence_when_reported() -> None:
    payload = parse_answer_payload(
        '{"chosen_labels": ["A"], "confidence": "80%", "reason": "因为"}'
    )

    assert payload is not None
    assert payload["chosen_labels"] == ["A"]
    assert payload[CONFIDENCE_KEY] == 0.8
    assert payload["reason"] == "因为"


def test_answer_payload_without_confidence_has_no_confidence_key() -> None:
    """缺失**不写这个键** —— 取到 ``None`` 就是「没报」，与「报了 0」不同。"""
    payload = parse_answer_payload('{"chosen_labels": ["A"], "reason": "因为"}')

    assert payload is not None
    assert payload.get(CONFIDENCE_KEY) is None
    assert CONFIDENCE_KEY not in payload


def test_confidence_zero_is_kept_as_zero() -> None:
    payload = parse_answer_payload('{"chosen_labels": ["A"], "confidence": 0}')

    assert payload is not None
    assert payload[CONFIDENCE_KEY] == 0.0


# --------------------------------------------------------------------------- #
# 「不可用」与「明确空作答」
# --------------------------------------------------------------------------- #
def test_explicit_empty_answer_is_recognisable() -> None:
    """显式空作答是**模型的结论** —— 可识别，因此下游能不重发。"""
    payload = parse_answer_payload(
        '{"chosen_labels": [], "confidence": 0, "reason": "题面缺失：选项D的公式未能读出"}'
    )

    assert payload is not None
    assert payload["chosen_labels"] == []
    assert payload[EXPLICIT_EMPTY_KEY] is True
    assert payload[CONFIDENCE_KEY] == 0.0


def test_only_an_empty_array_counts_as_explicit_empty() -> None:
    """字段缺失 / ``null`` / 空串都不算显式空作答 —— 那是「这条回复不可用」。"""
    assert parse_answer_payload('{"chosen_labels": []}') is not None
    assert parse_answer_payload('{"chosen_labels": null, "reason": "读不出来"}') is None
    assert parse_answer_payload('{"chosen_labels": "", "reason": "读不出来"}') is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "我觉得选 A 吧",
        "{不是 JSON",
        '{"reason": "只有理由，没有标号"}',
        '{"chosen_labels": ["SYN 报文"], "reason": "把正文当标号"}',
        '{"chosen_labels": ["", " "]}',
    ],
)
def test_unusable_replies_still_return_none(text: str) -> None:
    """不可用 = ``None``（重发路径）；绝不能猜一个答案出来。"""
    assert parse_answer_payload(text) is None


def test_missing_labels_field_is_unusable_not_empty_answer() -> None:
    """字段**缺失** ≠ 空作答：前者是「这条回复不能用」，后者是「模型的结论」。"""
    assert parse_answer_payload('{"chosen_labels": ["A"]}') is not None
    assert parse_answer_payload('{"reason": "没给标号"}') is None
