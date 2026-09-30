"""M2-2 一致性投票：**按内容比对，不按字母**。

最核心的一条：同一份内容在两次不同的打乱里落到不同字母上，
必须被判成**同一票**。做不到这一点，投票投的就是「页面有没有串位」，
而不是「模型答得对不对」。
"""

from __future__ import annotations

from core.enums import QType
from core.models import VoteResult
from solve.providers.base import LLMResponse
from solve.voting import VotingEngine

ENGINE = VotingEngine()


def sample(
    labels: list[str],
    presented: list[str],
    original: list[str] | None = None,
    *,
    latency_ms: int = 10,
    error_code: str | None = None,
) -> LLMResponse:
    """构造一个带「本次呈现顺序 + 原始顺序」的样本。"""
    return LLMResponse(
        text="{}",
        parsed={
            "chosen_labels": labels,
            "option_texts": presented,
            "original_texts": original if original is not None else presented,
        },
        latency_ms=latency_ms,
        raw="raw-response",
        error_code=error_code,
    )


# --------------------------------------------------------------------------- #
# 序号映射
# --------------------------------------------------------------------------- #
def test_index_map_pairs_by_content() -> None:
    mapping = ENGINE.build_index_map(["丙", "甲", "乙"], ["甲", "乙", "丙"])
    assert mapping == {0: 2, 1: 0, 2: 1}


def test_index_map_does_not_reuse_same_original_index() -> None:
    """同一份正文出现两次时，两次必须映射到**不同**的原始位置。"""
    mapping = ENGINE.build_index_map(["甲", "甲"], ["甲", "甲"])
    assert mapping == {0: 0, 1: 1}


def test_index_map_skips_unknown_text() -> None:
    assert ENGINE.build_index_map(["丁"], ["甲", "乙"]) == {}


# --------------------------------------------------------------------------- #
# 打乱不改变票面
# --------------------------------------------------------------------------- #
def test_shuffled_same_content_is_one_vote() -> None:
    """「打乱后同一内容应判为一致」—— M2-2 的验收用例。"""
    first = sample(["B"], ["乙", "甲"], ["甲", "乙"])  # 呈现：乙在前，B = 甲
    shuffled = sample(["B"], ["乙", "甲"], ["甲", "乙"])  # 同样打乱，同样选甲
    second = sample(["A"], ["甲", "乙"], ["甲", "乙"])  # 呈现：甲在前，A = 甲
    other = sample(["B"], ["甲", "乙"], ["甲", "乙"])  # 这次 B = 乙

    result = ENGINE.vote([first, shuffled, second, other], QType.SINGLE)

    assert result.n_samples == 4
    assert len(result.distribution) == 2, "两种内容，两个票面"
    assert result.distribution["甲"] == 3, "三次选到甲，字母各不相同"
    assert result.distribution["乙"] == 1
    assert result.chosen_labels == ["A"], "原始序号空间：甲 是第 1 项 → A"
    assert result.majority_ratio == 0.75


def test_same_letter_different_content_is_not_one_vote() -> None:
    """反例：字母相同但内容不同，绝不能并成一票。"""
    first = sample(["A"], ["甲", "乙"], ["甲", "乙"])
    second = sample(["A"], ["乙", "甲"], ["甲", "乙"])  # A 在这里是「乙」

    result = ENGINE.vote([first, second], QType.SINGLE)

    assert len(result.distribution) == 2
    assert result.majority_ratio == 0.5


def test_original_texts_missing_falls_back_to_presented_order() -> None:
    """没给原始顺序时按「呈现即原始」处理，方便直接构造样本。"""
    response = LLMResponse(text="", parsed={"chosen_labels": ["B"], "option_texts": ["甲", "乙"]})
    result = ENGINE.vote([response], QType.SINGLE)
    assert result.chosen_labels == ["B"]
    assert result.distribution == {"乙": 1}


# --------------------------------------------------------------------------- #
# 多选题：集合语义
# --------------------------------------------------------------------------- #
def test_multiple_choice_uses_set_semantics() -> None:
    first = sample(["A", "C"], ["甲", "乙", "丙", "丁"])
    second = sample(["C", "A"], ["甲", "乙", "丙", "丁"])  # 顺序不同，集合相同

    result = ENGINE.vote([first, second], QType.MULTIPLE)

    assert len(result.distribution) == 1, "集合相等就是同一票"
    assert result.majority_ratio == 1.0
    assert result.chosen_labels == ["A", "C"]


def test_multiple_choice_partial_overlap_is_a_different_vote() -> None:
    first = sample(["A", "C"], ["甲", "乙", "丙", "丁"])
    second = sample(["A", "B"], ["甲", "乙", "丙", "丁"])

    result = ENGINE.vote([first, second], QType.MULTIPLE)

    assert len(result.distribution) == 2
    assert result.majority_ratio == 0.5


def test_multiple_choice_labels_sorted_by_original_index() -> None:
    result = ENGINE.vote(
        [sample(["D", "A"], ["甲", "乙", "丙", "丁"])],
        QType.MULTIPLE,
    )
    assert result.chosen_labels == ["A", "D"]


# --------------------------------------------------------------------------- #
# 无效样本与占比口径
# --------------------------------------------------------------------------- #
def test_unparsable_sample_is_excluded_from_denominator() -> None:
    """分母只数**有效**样本 —— 否则 Provider 掉线会把一致率稀释成假高分。"""
    good = [sample(["A"], ["甲", "乙"]) for _ in range(3)]
    bad = [sample([], ["甲", "乙"], error_code="rate_limited") for _ in range(7)]

    result = ENGINE.vote([*good, *bad], QType.SINGLE)

    assert result.n_samples == 3
    assert result.majority_ratio == 1.0
    assert len(result.samples) == 10, "采样明细要留全部样本，含失败的"
    assert {record.error_code for record in result.samples[3:]} == {"rate_limited"}


def test_sample_without_labels_is_invalid() -> None:
    response = LLMResponse(text="废话", parsed=None)
    result = ENGINE.vote([response], QType.SINGLE)
    assert result.n_samples == 0
    assert result.chosen_labels == []
    assert result.samples[0].error_code == "unparsable_sample"


def test_out_of_range_label_is_rejected() -> None:
    result = ENGINE.vote([sample(["F"], ["甲", "乙"])], QType.SINGLE)
    assert result.n_samples == 0
    assert result.chosen_labels == []


def test_majority_ratio_is_share_of_top_vote() -> None:
    votes = [sample(["A"], ["甲", "乙"]) for _ in range(4)]
    votes.append(sample(["B"], ["甲", "乙"]))

    result = ENGINE.vote(votes, QType.SINGLE)

    assert result.majority_ratio == 0.8
    assert VotingEngine.majority_ratio(result) == 0.8


def test_majority_ratio_of_empty_result_is_zero() -> None:
    assert VotingEngine.majority_ratio(VoteResult(chosen_labels=[], majority_ratio=0.0)) == 0.0


def test_sample_records_keep_latency_and_raw() -> None:
    result = ENGINE.vote([sample(["A"], ["甲", "乙"], latency_ms=42)], QType.SINGLE)
    record = result.samples[0]
    assert record.latency_ms == 42
    assert record.raw == "raw-response"
    assert record.chosen_texts == ["甲"]
