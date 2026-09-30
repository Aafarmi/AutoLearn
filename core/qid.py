"""题目标识（T0-1）。

算法：``sha1(NFKC归一化题干 + "|" + "|".join(sorted(归一化选项)))[:16]``

三条不可动摇的性质
------------------
1. **不含答案**——同一道题无论作答如何，``qid`` 恒定。
2. **选项排序后参与哈希**——页面把选项打乱，``qid`` 不变，缓存与去重才成立。
3. **NFKC 归一化**——全角/半角、兼容标点、连字等统一后再入哈希。
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence

__all__ = ["make_qid", "make_stem_hash", "normalize_text"]

_QID_LEN = 16
_SEP = "|"


def normalize_text(s: str) -> str:
    """归一化文本：NFKC → 去首尾空白 → 内部连续空白折叠为单个半角空格。

    NFKC 负责统一全角半角与兼容标点；空白折叠是刻意补上的——从 HTML
    里抽出来的题干常带 ``\\n`` 与缩进，不折叠会让同一道题算出不同 ``qid``。
    """
    return " ".join(unicodedata.normalize("NFKC", s).split())


def make_qid(stem: str, option_texts: Sequence[str]) -> str:
    """由题干与选项正文构造题目 ID。

    ``option_texts`` 会被逐项归一化后**排序**，因此打乱选项顺序不会改变结果。
    """
    norm_stem = normalize_text(stem)
    norm_options = sorted(normalize_text(t) for t in option_texts)
    payload = norm_stem + _SEP + _SEP.join(norm_options)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:_QID_LEN]


def make_stem_hash(stem: str) -> str:
    """题干正文指纹，用于 M3-7「执行前重校验」。

    只对题干本体取哈希，不含选项——选项被判重排序过，不构成身份。
    """
    payload = normalize_text(stem)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:_QID_LEN]
