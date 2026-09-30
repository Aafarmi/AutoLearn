"""MockProvider（M2-1）。

不调任何模型，直接拿**靶场题库**当「正确」答案，再按可配置错误率注入错误样本。

.. warning::
   它读的是地面真值，**结果不得作为 M2 闸门依据**。它只用来验证投票逻辑本身
   是否正确（例如注入 40% 错误率后，投票应把多数票收敛回真值）。

真值从哪来
----------
``mock_site/static/questions.json``（:class:`QuestionBankSource`）。
靶场页面把同一份真值挂在题目根的 ``data-answer`` / ``data-answer-texts`` 上，
但那是**给测试判分用的**（见 ``tests/act_helpers.py``）：本 Provider 拿不到页面，
也不该去读它 —— 读页面就等于让「通道」自己知道答案，M2 的数字会全假。

用户未添加任何模型配置时，系统自动落到本 Provider，全链路仍可跑通。

它为什么必须回答「模型看到的那个标号」
--------------------------------------
Mock 拿到的提示词里，选项带着**页面标号**（p6.0 起不再打乱，看到的顺序就是页面
顺序；标号可能不连续，例如某个选项的框被丢弃时是 ``A, B, D``）。它若直接回
题干下标或位置字母，投票就永远一致 —— 那样投的其实是「Mock 没串位」，
而不是「投票按内容比对」。

所以它严格按 :func:`~solve.prompts.parse_presented_options` 解析出模型看到的
标号与正文，再把真值**按内容**翻译成对应的那个**页面标号**。注入错误率后，
多数票能否收敛回真值，就成了对 :class:`~solve.voting.VotingEngine` 的真实检验，
而且顺带钉住一条：非连续标号（``A, B, D``）下 Mock 也不会答出一个页面上
不存在的字母。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Protocol

from core.enums import ProviderName
from core.qid import make_qid, normalize_text
from solve.prompts import (
    CHOSEN_LABELS_KEY,
    NO_MOCK_ANSWER,
    REASON_KEY,
    parse_presented_options,
)
from solve.providers.base import LLMProvider, LLMRequest, LLMResponse

__all__ = ["AnswerSource", "MockProvider", "QuestionBankSource"]

#: 靶场题库默认落位（仓库根 ``mock_site/static/questions.json``）
DEFAULT_BANK_PATH = Path(__file__).resolve().parents[2] / "mock_site" / "static" / "questions.json"

_OPTION_KEY = "option_texts"
_LABELS_KEY = "option_labels"


class AnswerSource(Protocol):
    """地面真值来源（靶场题库 / 测试自造的 ``qid → 正确项正文`` 索引）。"""

    def lookup(self, qid: str) -> list[str] | None:
        """返回该题正确选项的**正文列表**；未知题目返回 ``None``。"""
        ...


class QuestionBankSource:
    """从 ``questions.json`` 建 ``qid → 正确项正文`` 的索引。

    题库里存的是 ``answer``（选项**下标**），而 Mock 需要正文 —— 于是这里用
    :func:`~core.qid.make_qid` 反算 ``qid`` 建表。顺带白拿一个性质：
    ``qid`` 对选项顺序不敏感，所以打乱题库里的选项顺序，索引依然成立。

    为什么 canvas 题要**按两个题干各建一条**
    ----------------------------------------
    靶场对 canvas 题把题干正文画在画布上，同时把一份文字版（``canvas_stem``）
    镜像进页面属性（``mock_site/static/quiz_shared.js``：
    ``'data-quiz-stem-text': question.canvas_stem || question.stem``）。
    而 v0.2.0 的读题走**视觉**：模型看到的是画在画布上的那份正文，于是页面上
    真实存在的 ``qid`` 是按 ``canvas_stem`` 算的那个；只索引 ``stem``
    会让 canvas 题在 Mock 下变成「未知题目」—— 批处理跑第 27 题时就是这样停下的。
    两条都建，读哪一份都能查到真值。

    .. warning::
       两条索引指向**同一个答案**，不构成歧义。不要为了「兼容」去做模糊匹配 ——
       返回一道别的题的答案，比承认认不出危险得多。
    """

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._truth: dict[str, list[str]] = {}
        for entry in entries:
            options = [normalize_text(str(text)) for text in (entry.get("options") or [])]
            if not options:
                continue
            indexes = entry.get("answer")
            if not isinstance(indexes, list):
                continue
            texts = [
                options[index]
                for index in indexes
                if isinstance(index, int) and 0 <= index < len(options)
            ]
            if not texts:
                continue
            # canvas 题：页面上真实存在的 qid 是按 canvas_stem 算的（见类文档）
            for stem in (entry.get("stem"), entry.get("canvas_stem")):
                if stem:
                    self._truth[make_qid(str(stem), options)] = texts

    @classmethod
    def from_path(cls, path: Path) -> QuestionBankSource:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Any) -> QuestionBankSource:
        if isinstance(payload, dict):
            entries = payload.get("questions") or []
        elif isinstance(payload, list):
            entries = payload
        else:
            entries = []
        return cls([entry for entry in entries if isinstance(entry, dict)])

    @classmethod
    def from_default(cls) -> QuestionBankSource:
        """读仓库内靶场题库。文件缺失时返回**空索引**（Mock 会明确报未知题目）。"""
        if not DEFAULT_BANK_PATH.exists():
            return cls([])
        return cls.from_path(DEFAULT_BANK_PATH)

    def lookup(self, qid: str) -> list[str] | None:
        return self._truth.get(qid)

    def size(self) -> int:
        return len(self._truth)


class MockProvider(LLMProvider):
    """按注入错误率返回真值或随机错答。"""

    name = ProviderName.MOCK

    def __init__(
        self,
        answer_source: AnswerSource,
        error_rate: float = 0.0,
        *,
        seed: int | None = None,
    ) -> None:
        if not 0.0 <= error_rate <= 1.0:
            raise ValueError(f"error_rate 必须在 0~1 之间，得到 {error_rate}")
        self._source = answer_source
        self._error_rate = error_rate
        self._rng = random.Random(seed)
        self.calls: list[LLMRequest] = []

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """不联网、不计费，只把靶场真值翻译成「本次呈现」的标号。"""
        self.calls.append(req)
        presented = parse_presented_options(req.user)
        qid = self._qid_of(req.user)
        truth = self._source.lookup(qid) if qid else None

        if truth is None:
            return LLMResponse(
                text='{"chosen_labels": [], "reason": "未知题目"}',
                parsed={CHOSEN_LABELS_KEY: NO_MOCK_ANSWER, REASON_KEY: "未知题目"},
                raw="mock:unknown_question",
                error_code="mock_unknown_question",
            )

        correct = [label for label, text in presented if text in set(truth)]
        labels = self._maybe_corrupt(correct, presented)
        parsed: dict[str, Any] = {
            CHOSEN_LABELS_KEY: labels,
            REASON_KEY: f"mock 真值（错误率 {self._error_rate:g}）",
            # 模型看到的标号 = 页面标号（p6.0），一并写清楚，
            # 这样直接调 ``complete`` 的测试也拿得到一个完整样本。
            _LABELS_KEY: [label for label, _ in presented],
            _OPTION_KEY: [text for _, text in presented],
        }
        return LLMResponse(
            text=json.dumps(
                {CHOSEN_LABELS_KEY: labels, REASON_KEY: "mock"},
                ensure_ascii=False,
            ),
            parsed=parsed,
            raw=f"mock:qid={qid}",
        )

    async def aclose(self) -> None:
        """MockProvider 无连接，no-op 即可。"""
        return

    # -- 内部 -------------------------------------------------------------- #
    @staticmethod
    def _qid_of(user_text: str) -> str | None:
        """从提示词里取 ``题目ID:``。取不到则按「题干 + 选项」现算一次。"""
        for line in user_text.splitlines():
            if line.startswith("题目ID:"):
                value = line.split(":", 1)[1].strip()
                return value or None
        return None

    def _maybe_corrupt(
        self,
        correct: list[str],
        presented: list[tuple[str, str]],
    ) -> list[str]:
        """按 ``error_rate`` 决定这次的作答。"""
        if not correct or self._error_rate <= 0.0:
            return list(correct)
        if self._rng.random() >= self._error_rate:
            return list(correct)
        wrong = [label for label, _ in presented if label not in correct]
        if not wrong:
            # 全对（例如只有一项正确却又是唯一选项）时无法注入错误
            return list(correct)
        return [self._rng.choice(wrong)]
