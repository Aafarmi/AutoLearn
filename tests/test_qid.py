"""T0-1 题目标识：``qid`` 必须做到「选项打乱后不变」。"""

from __future__ import annotations

import inspect
import random
import re

from core.qid import make_qid, make_stem_hash, normalize_text

HEX16 = re.compile(r"^[0-9a-f]{16}$")

STEM = "以下关于 TCP 三次握手的说法，正确的是："
OPTIONS = ["SYN 报文不携带数据", "ACK 报文一定携带数据", "第三次握手可以携带数据", "SYN-ACK 由客户端发出"]


def test_shuffled_options_qid_unchanged() -> None:
    """M0/T0 验收：把选项打乱后 ``qid`` 必须一模一样。"""
    base = make_qid(STEM, OPTIONS)
    for seed in range(25):
        shuffled = OPTIONS[:]
        random.Random(seed).shuffle(shuffled)
        assert make_qid(STEM, shuffled) == base, f"seed={seed} 打乱后 qid 变了"


def test_qid_is_argument_order_agnostic() -> None:
    """入参顺序无关：先传打乱再传原始，结果仍相同。"""
    assert make_qid(STEM, OPTIONS[::-1]) == make_qid(STEM, OPTIONS)


def test_qid_independent_of_answer() -> None:
    """``qid`` 不含答案 —— 结构性保证：签名里根本没有答案参数。"""
    params = list(inspect.signature(make_qid).parameters)
    assert params == ["stem", "option_texts"], "make_qid 签名里出现了非题干/选项的参数"


def test_nfkc_normalization_unifies_width_and_punctuation() -> None:
    """全角题干/选项与半角写法必须得到同一个 ``qid``。"""
    assert normalize_text("ＡＢＣ（）") == "ABC()"
    assert make_qid("下列哪项（Ａ）正确", ["甲", "乙"]) == make_qid("下列哪项(A)正确", ["甲", "乙"])


def test_whitespace_collapse_is_stable() -> None:
    """从 HTML 抽出来的题干常带换行与缩进，折叠后应等价。"""
    assert make_qid("题干  带\n\n多余\t空白", ["甲", "乙"]) == make_qid(
        "题干 带 多余 空白", ["甲", "乙"]
    )


def test_stem_hash_depends_only_on_stem() -> None:
    """M3-7：``stem_hash`` 只对题干取指纹，选项变化不影响它。"""
    assert make_stem_hash(STEM) == make_stem_hash(f"  {STEM}  ")
    assert make_stem_hash(STEM) != make_stem_hash("换个题干")


def test_ids_are_16_hex() -> None:
    assert HEX16.match(make_qid(STEM, OPTIONS))
    assert HEX16.match(make_stem_hash(STEM))


def test_distinct_stems_or_options_give_distinct_ids() -> None:
    """碰撞会让缓存串题，必须避免。"""
    base = make_qid(STEM, OPTIONS)
    assert make_qid(STEM + "。", OPTIONS) != base
    assert make_qid(STEM, [*OPTIONS[:-1], "换掉一个选项"]) != base
    assert make_qid(STEM, [*OPTIONS, "多一个选项"]) != base

