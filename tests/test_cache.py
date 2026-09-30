"""M2-4 精确缓存：``qid → Answer``，不做语义近似。"""

from __future__ import annotations

import pytest

from core.enums import SolvePath
from core.models import Answer
from solve.cache import SolveCache


def make_answer(
    qid: str, *, labels: list[str] | None = None, path: SolvePath = SolvePath.MOCK
) -> Answer:
    return Answer(
        qid=qid,
        chosen_labels=labels or ["A"],
        chosen_texts=["甲"],
        confidence=1.0,
        solve_path=path,
        review_flag=False,
    )


def test_miss_returns_none_not_a_guess() -> None:
    assert SolveCache().get("nope") is None


def test_put_then_get_roundtrip() -> None:
    cache = SolveCache()
    answer = make_answer("q1")
    cache.put(answer)

    assert cache.get("q1") is answer
    assert cache.size() == 1


def test_same_qid_overwrites() -> None:
    """复算结果永远以最新一次为准，缓存不该囤积自相矛盾的答案。"""
    cache = SolveCache()
    cache.put(make_answer("q1", labels=["A"]))
    cache.put(make_answer("q1", labels=["B"]))

    assert cache.size() == 1
    cached = cache.get("q1")
    assert cached is not None
    assert cached.chosen_labels == ["B"]


def test_cache_is_exact_not_fuzzy() -> None:
    """只认精确 ``qid``：相邻的 key 绝不能命中。"""
    cache = SolveCache()
    cache.put(make_answer("deadbeefdeadbeef"))

    assert cache.get("deadbeefdeadbeee") is None
    assert cache.get("deadbeefdeadbeef") is not None


def test_contains_and_clear() -> None:
    cache = SolveCache()
    cache.put(make_answer("q1"))

    assert "q1" in cache
    assert "q2" not in cache
    assert 123 not in cache

    cache.clear()
    assert cache.size() == 0


def test_review_answers_are_not_special_cased_by_cache() -> None:
    """缓存层不做业务判断（要不要缓存由 Solver 决定），但必须原样返还复核标记。"""
    cache = SolveCache()
    cache.put(make_answer("q1").model_copy(update={"review_flag": True}))
    cached = cache.get("q1")
    assert cached is not None
    assert cached.review_flag is True


@pytest.mark.parametrize("qid", ["a", "b", "0" * 16])
def test_arbitrary_qids_are_stored_verbatim(qid: str) -> None:
    cache = SolveCache()
    cache.put(make_answer(qid))
    assert cache.get(qid) is not None
