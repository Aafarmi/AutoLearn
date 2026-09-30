"""精确缓存（M2-4）。

基于 ``qid`` 的精确命中；**语义缓存本阶段不启用**。
命中直接返回既有 :class:`~core.models.Answer`，并计入采样明细
（``solve_path=CACHE``）。

为什么只做精确缓存
------------------
``qid`` 由「归一化题干 + 排序后的选项正文」算出（T0-1），**不含答案**，
所以同一道题无论页面怎么打乱选项都命中同一条缓存。语义缓存要在向量空间里
判「差不多是同一道题」，一旦判错返回的就是**别人的答案**，而这类错误在
留痕里看不出来。M2 阶段不冒这个险。

进程内字典，不落盘：缓存是**省钱**手段，不是事实来源。进程重启后重新算一遍
即可，真相始终在 SQLite 的任务状态里。
"""

from __future__ import annotations

from core.models import Answer

__all__ = ["SolveCache"]


class SolveCache:
    """``qid → Answer`` 的精确映射。命中率是 M2 的成本指标之一。"""

    def __init__(self) -> None:
        self._store: dict[str, Answer] = {}

    def get(self, qid: str) -> Answer | None:
        """精确命中则返回既有作答；未命中返回 ``None``（**不要**返回近似结果）。"""
        return self._store.get(qid)

    def put(self, answer: Answer) -> None:
        """写入。同 ``qid`` 后写覆盖先写 —— 复算结果永远以最新一次为准。"""
        self._store[answer.qid] = answer

    def size(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        """清空。测试与「换模型重跑」用。"""
        self._store.clear()

    def __contains__(self, qid: object) -> bool:
        return isinstance(qid, str) and qid in self._store
