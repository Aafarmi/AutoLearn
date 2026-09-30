"""一致性投票（M2-2）。

计票规则
--------
- 比对**选项内容**，不比对字母；
- 多选题按 ``frozenset`` 整体集合记票，不做逐项独立计票；
- 用**多数票占比**（``majority_ratio``）而非全体一致。

为什么仍然按内容比对
--------------------
p6.0 起选项**不再打乱**，「字母会串位」这个成因没有了 —— 但按内容比对仍然是
**更强的判据**，而且这条判据是免费的：

- 字母可能错：模型抄错一个字母、把页面标号非连续（``A, B, D``）的那道题按位置
  数成 ``C``、或者干脆回了一个页面上不存在的字母。这些都会在内容这一层露馅；
- 内容不能错：真正要过的是「点下去的那个选项是不是我要选的那个」，
  而那由**正文**决定；
- 历史留痕（打乱时代）也按同一套口径归并得回来。

所以计票口径不变：先定位标号 → 取正文 → 按正文归并。

标号怎么定位（p6.0）
--------------------
样本 ``parsed`` 里的 ``option_labels`` 是**页面标号**（来自
``question.options[i].label``，可能不连续）。定位必须走它，**不能按
``ord(label) - ord('A')`` 现算位置** —— 视觉组丢掉某个选项的框时标号是
``A, B, D``，按位置算会把 ``D`` 当成第 4 项（越界）或错项。没有
``option_labels`` 时（直接构造样本的单测）才退回位置字母，与历史行为一致。

样本要带的几样东西
------------------
计票需要知道「这一次呈现的是什么」，所以每个样本的 ``parsed`` 里可以带：

=====================================  ==========================================
``parsed["chosen_labels"]``            模型回的字母，位于**本次呈现**的空间
``parsed["option_labels"]``            本次呈现的标号（页面标号）
``parsed["option_texts"]``             本次呈现的选项正文，下标 ↔ 标号
``parsed["original_texts"]``           页面顺序的选项正文，用于把内容映射回原序号
=====================================  ==========================================

后三项由 :class:`~solve.solver.Solver` 在收到响应后注入 —— **Provider 不必知道**
页面顺序，它只管把模型看到的那份标号答出来。缺 ``original_texts`` 时按
「呈现顺序即原始顺序」处理，这样单测可以直接构造样本而不用搭 Solver。

输出的标号语义
--------------
:attr:`SampleOutcome.labels` 与 :attr:`VoteResult.chosen_labels` 是**原始序号空间
的字母**（第 0 项 = ``A``），与「哪个样本投中了这一票」无关，因此是确定的。
页面真实标号由 Solver 用 ``question.options[i].label`` 翻译，投票层不产出页面标号
（它只用页面标号**定位**，见上）。
"""

from __future__ import annotations

from collections import Counter
from typing import NamedTuple

from core.enums import QType
from core.models import SampleRecord, VoteResult
from solve.prompts import EXPLICIT_EMPTY_KEY
from solve.providers.base import LLMResponse

__all__ = [
    "EXPLICIT_EMPTY_CODE",
    "KEY_CHOSEN_LABELS",
    "KEY_OPTION_LABELS",
    "KEY_OPTION_TEXTS",
    "KEY_ORIGINAL_TEXTS",
    "SampleOutcome",
    "VotingEngine",
]

#: 样本 ``parsed`` 里的键，见模块文档
KEY_CHOSEN_LABELS = "chosen_labels"
KEY_OPTION_LABELS = "option_labels"
KEY_OPTION_TEXTS = "option_texts"
KEY_ORIGINAL_TEXTS = "original_texts"

#: 显式空作答在 ``SampleRecord.error_code`` 里的记号。
#:
#: 它不是 Provider 错误，而是**模型的结论**（「题面不足以下结论」）——
#: 记成这个码是为了让 ``solve.json`` 的采样明细一眼分得清
#: 「请求失败」与「模型说答不了」。与载荷键同值，避免两套字面量漂移。
EXPLICIT_EMPTY_CODE = EXPLICIT_EMPTY_KEY

#: 多选票面（distribution 的键）里各项之间的分隔符
_KEY_SEP = " ｜ "


class SampleOutcome(NamedTuple):
    """一个样本的解析结论。

    ``error`` 非空 = 该样本**不可用**（调用方按错误码决定要不要重发）；
    ``explicit_empty`` 为真 = 模型**明确**空作答（这是结论，不该重发）。
    """

    labels: list[str]
    texts: list[str]
    error: str | None
    explicit_empty: bool


class VotingEngine:
    """把多次采样收敛成一个多数解。"""

    # -- 序号映射 ---------------------------------------------------------- #
    def build_index_map(self, shuffled: list[str], original: list[str]) -> dict[int, int]:
        """建立「呈现序号 → 原始序号」映射。

        按内容逐一配对，**已用掉的原始下标不再复用** —— 否则同一份正文出现两次时
        （靶场确有这种题）两次都会映射到同一个原始位置。
        配不上的呈现项不出现在返回值里，由调用方按无效处理。
        """
        mapping: dict[int, int] = {}
        used: set[int] = set()
        for shuffled_index, text in enumerate(shuffled):
            for original_index, candidate in enumerate(original):
                if original_index in used or candidate != text:
                    continue
                mapping[shuffled_index] = original_index
                used.add(original_index)
                break
        return mapping

    # -- 计票 -------------------------------------------------------------- #
    def vote(self, samples: list[LLMResponse], qtype: QType) -> VoteResult:
        """按内容记票。多选题整体集合相等才算同一票。

        解析不出标号、标号越界、显式空作答的样本记为**无效样本**：进
        :attr:`VoteResult.samples`（带 ``error_code``）但不参与多数票占比的分母 ——
        分母只数有效样本，否则 Provider 掉线会把「一致率」稀释成一个假的高分。
        """
        counts: Counter[str] = Counter()
        labels_of: dict[str, list[str]] = {}
        records: list[SampleRecord] = []
        valid = 0

        for index, response in enumerate(samples):
            outcome = self.resolve(response)
            if outcome.error is None:
                valid += 1
                key = self._key_of(outcome.texts, qtype)
                counts[key] += 1
                labels_of.setdefault(key, outcome.labels)
            records.append(
                SampleRecord(
                    sample_index=index,
                    chosen_labels=outcome.labels,
                    chosen_texts=outcome.texts,
                    latency_ms=response.latency_ms,
                    error_code=outcome.error or response.error_code,
                    raw=response.raw or None,
                )
            )

        if not counts:
            return VoteResult(
                chosen_labels=[],
                majority_ratio=0.0,
                distribution={},
                n_samples=valid,
                samples=records,
            )

        winner, top = counts.most_common(1)[0]
        return VoteResult(
            chosen_labels=labels_of[winner],
            majority_ratio=(top / valid) if valid else 0.0,
            distribution=dict(counts),
            n_samples=valid,
            samples=records,
        )

    @staticmethod
    def majority_ratio(vote_result: VoteResult) -> float:
        """多数票占比 = 最高票数 / 有效样本数。"""
        if vote_result.n_samples <= 0 or not vote_result.distribution:
            return 0.0
        return max(vote_result.distribution.values()) / vote_result.n_samples

    def resolve(self, response: LLMResponse) -> SampleOutcome:
        """把一个样本解析成「原始序号空间的标号 + 正文」+ 处置结论。

        公开出来是为了让求解层能在**投票之前**就知道这个样本能不能用：
        「回复不可用」要触发有界重发，「模型明确空作答」不能触发（见
        ``solve/solver.py`` 的 ``SAMPLE_RETRY_MAX``）。

        判定顺序有讲究：**Provider 错误码优先** —— 一条带错误码的响应即使
        顺带带了个 ``chosen_labels`` 也不算可用样本。
        """
        parsed = response.parsed or {}
        if response.error_code:
            return SampleOutcome([], [], response.error_code, explicit_empty=False)
        if parsed.get(EXPLICIT_EMPTY_KEY):
            return SampleOutcome([], [], EXPLICIT_EMPTY_CODE, explicit_empty=True)

        chosen = [str(label).upper() for label in (parsed.get(KEY_CHOSEN_LABELS) or [])]
        if not chosen:
            return SampleOutcome([], [], "unparsable_sample", explicit_empty=False)

        original = [str(text) for text in (parsed.get(KEY_ORIGINAL_TEXTS) or [])]
        presented = [str(text) for text in (parsed.get(KEY_OPTION_TEXTS) or [])]
        labels = [str(label).upper() for label in (parsed.get(KEY_OPTION_LABELS) or [])]
        if not original:
            # 没给原始顺序：按「呈现即原始」处理，便于直接构造样本做单测
            original = list(presented)
        if not original:
            return SampleOutcome([], [], "missing_option_texts", explicit_empty=False)
        if not labels:
            # 没有页面标号：退回位置字母（历史样本与直接构造的单测走这条）
            labels = [chr(ord("A") + index) for index in range(len(presented or original))]

        mapping = self.build_index_map(presented, original) if presented else {}
        pairs: list[tuple[int, str]] = []
        for label in chosen:
            if label not in labels:
                # 页面上没有这个标号：越界，不算有效样本（**不猜它是不是第几项**）
                continue
            position = labels.index(label)
            if position < 0 or position >= len(presented or original):
                continue
            original_index = mapping.get(position, position) if presented else position
            if original_index >= len(original):
                continue
            pairs.append((original_index, original[original_index]))

        if not pairs:
            return SampleOutcome([], [], "label_out_of_range", explicit_empty=False)

        pairs.sort()
        return SampleOutcome(
            [chr(ord("A") + original_index) for original_index, _ in pairs],
            [text for _, text in pairs],
            None,
            explicit_empty=False,
        )

    @staticmethod
    def _key_of(texts: list[str], qtype: QType) -> str:
        """由**内容**生成票面键。多选按集合（``frozenset``）折叠，顺序不影响。"""
        if qtype is QType.MULTIPLE:
            return _KEY_SEP.join(sorted(set(texts)))
        return texts[0] if texts else ""
