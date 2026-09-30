"""T0-4 自指选项白名单：命中即禁止打乱选项。

判错方向是不对称的：
- 误判为自指 → 少打乱一些题，只损失一点抗位置偏置能力；
- 漏判自指 → 把「以上都对」打乱后直接产出错答，且无法事后察觉。

所以这里同时测「该命中的都命中」和「不该命中的别误伤太多」。
"""

from __future__ import annotations

import pytest

from core.enums import ProbeName, QType
from core.models import Option, Question
from solve.solver import (
    SELF_REF_PATTERN,
    allows_shuffle,
    is_self_referential,
    question_has_self_ref,
)

#: T0-4 明确要求覆盖的写法，逐条对应
SELF_REF_TEXTS = [
    "以上都对",
    "以上都不对",
    "上述说法正确",
    "以下选项",
    "下列哪一项",
    "都对",
    "都正确",
    "都错",
    "均对",
    "均正确",
    "均错",
    "全部",
    "不全",
    "前者",
    "后者",
    "(A)和(B)",
    "(A)和",
    "A和C",
    "A、C",
]

PLAIN_TEXTS = [
    "SYN 报文不携带数据",
    "三次握手可以防止历史连接",
    "TCP 提供可靠传输",
    "服务端先收到 SYN",
]


@pytest.mark.parametrize("text", SELF_REF_TEXTS)
def test_self_referential_detected(text: str) -> None:
    assert is_self_referential(text), f"{text!r} 应被判为自指"


@pytest.mark.parametrize("text", PLAIN_TEXTS)
def test_plain_option_not_flagged(text: str) -> None:
    assert not is_self_referential(text), f"{text!r} 不该被判为自指"


def test_pattern_is_exposed_for_review() -> None:
    """白名单正则是**评审对象**，必须能被直接拿到。"""
    assert isinstance(SELF_REF_PATTERN, type(SELF_REF_PATTERN))
    assert "前者" in SELF_REF_PATTERN.pattern


def _question(option_texts: list[str]) -> Question:
    return Question(
        qid="deadbeefdeadbeef",
        stem="下列说法正确的是：",
        stem_hash="cafebabecafebabe",
        qtype=QType.SINGLE,
        options=[
            Option(index=i, label=chr(ord("A") + i), text=t, raw=t)
            for i, t in enumerate(option_texts)
        ],
        source=ProbeName.VISION,
        channel_trace=["vision"],
    )


def test_question_with_self_ref_option_is_flagged_for_no_shuffle() -> None:
    """P0 验收：含自指选项的题**不被乱序**。"""
    q = _question(["甲说法", "乙说法", "以上都对"])
    assert question_has_self_ref(q)
    assert allows_shuffle(q) is False


def test_self_ref_anywhere_in_options_disables_shuffle() -> None:
    """自指选项出现在任意位置（不只最后一条）都要命中。"""
    for pos in range(4):
        texts = ["甲", "乙", "丙", "丁"]
        texts[pos] = "前者"
        assert allows_shuffle(_question(texts)) is False, f"位置 {pos} 漏判"


def test_clean_question_stays_shufflable() -> None:
    q = _question(["甲说法", "乙说法", "丙说法", "丁说法"])
    assert question_has_self_ref(q) is False
    assert allows_shuffle(q) is True


def test_empty_question_is_shufflable() -> None:
    """没有选项时不构成自指，别让边界把整题锁死。"""
    assert allows_shuffle(_question([])) is True
