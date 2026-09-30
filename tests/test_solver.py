"""M2-3 求解器：单一模式、可选复算、投票收敛、缓存、复核标记、降级链。

Tier 分级已删除（2026-09-29）
-----------------------------
原先的 ``tier1`` / ``tier2`` 两档与「一致率不足就自动升级复算」那套隐式路由
整体删除：现在**只有一种求解模式**，多出来的行为只有一个显式开关 ——

* ``RunConfig.recalculate == False``（默认）→ **以第一次答案为准**，只调一次模型；
* ``RunConfig.recalculate == True`` → 按 ``sample_n`` 次取样后按**内容**投票，
  一致率低于 ``guards.agreement_accept`` 时标 ``⚠复核`` 必停。

p6.0 的三条新口径（本文件逐条钉住）
-----------------------------------
1. **呈现顺序 = 页面顺序，标号 = 页面标号**（含 ``A, B, D`` 这种非连续标号）。
   自指检测仍在，但不再改变呈现顺序、也不再降温度；
2. **单样本的 ``confidence`` 用模型自报值**，低于 ``guards.confidence_review_min``
   → 标 ``⚠复核``；模型没报则保持旧语义（一致率），**不新增暂停**；
3. **回复不可用 → 有界重发**（``SAMPLE_RETRY_MAX`` 次额外尝试），
   而**显式空作答**是模型的结论 —— 不重发，两类故障的理由也必须分得开。

两个「必须有」的用例保持不变：
- **自指题不打乱**（T0-4）：打乱会把「以上都对」指向别的集合；
- **注入错误率后投票仍收敛回真值**（M2-2）：这才证明投票逻辑本身正确，
  而不是证明 Mock 没串位。
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.config import GuardThresholds, RunConfig
from core.enums import ProbeName, ProviderName, QType, SolvePath
from core.events import Event
from core.models import Answer, Option, Question, VoteResult
from core.qid import make_qid
from core.trace import EventBus, EventStream
from solve.cache import SolveCache
from solve.prompts import build_messages, parse_answer_payload, parse_presented_options
from solve.providers.base import (
    AuthError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
)
from solve.providers.mock import MockProvider, QuestionBankSource
from solve.solver import DEFAULT_TEMPERATURE, SAMPLE_RETRY_MAX, Solver, allows_shuffle

BANK_PATH = Path(__file__).resolve().parents[1] / "mock_site" / "static" / "questions.json"

STEM = "TCP 建立连接时，客户端发送的第一个报文段是？"
OPTIONS = ["SYN 报文", "ACK 报文", "FIN 报文", "RST 报文"]

#: 回应签名：``(req, 本次是第几次调用) -> 标号列表``；抛异常表示这次请求失败
Responder = Callable[[LLMRequest, int], list[str]]


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def make_question(
    texts: list[str] | None = None,
    *,
    stem: str = STEM,
    qtype: QType = QType.SINGLE,
    labels: list[str] | None = None,
    trace: list[str] | None = None,
    source: ProbeName = ProbeName.VISION,
    skill_id: str | None = None,
    skill_error: str | None = None,
) -> Question:
    """造一道题。

    ``labels`` 用来造**非连续标号**（视觉组丢掉某一项时就是 ``A, B, D``）——
    位置字母在那道题上一定错位，所以它是最该被钉住的形状。
    """
    options = texts if texts is not None else list(OPTIONS)
    resolved = labels if labels is not None else [chr(ord("A") + i) for i in range(len(options))]
    return Question(
        qid=make_qid(stem, options),
        stem=stem,
        stem_hash="stem-hash",
        qtype=qtype,
        options=[
            Option(index=i, label=resolved[i], text=text, raw=text)
            for i, text in enumerate(options)
        ],
        source=source,
        skill_id=skill_id or {
            QType.SINGLE: "single_choice",
            QType.TRUE_FALSE: "true_false",
        }.get(qtype, "unavailable"),
        skill_error=skill_error,
        channel_trace=trace or [],
    )


def recalc_config(n: int = 5) -> RunConfig:
    """**开启复算**的配置 —— 需要多重采样/投票的用例都用它。"""
    return RunConfig(recalculate=True, sample_n=n)


def pick(req: LLMRequest, truth: list[str]) -> list[str]:
    """本次呈现里，内容属于 ``truth`` 的那些标号（标号即页面标号）。"""
    return [label for label, text in parse_presented_options(req.user) if text in set(truth)]


def always(truth: list[str]) -> Responder:
    """永远按内容选 ``truth``（与顺序无关）。"""
    return lambda req, _index: pick(req, truth)


def cycle(*truths: list[str]) -> Responder:
    """轮流按内容选，用来人为压低一致率。"""
    return lambda req, index: pick(req, truths[index % len(truths)])


class FakeProvider(LLMProvider):
    """按脚本作答的 Provider，用来把复算、重发与降级链钉死。"""

    name = ProviderName.OPENAI_COMPAT

    def __init__(
        self,
        responder: Responder,
        *,
        name: ProviderName | None = None,
        confidence: float | None = None,
    ) -> None:
        self.responder = responder
        self.confidence = confidence
        self.calls: list[LLMRequest] = []
        if name is not None:
            self.name = name

    def model_for(self) -> str | None:
        return "fake-model"

    async def complete(self, req: LLMRequest) -> LLMResponse:
        index = len(self.calls)
        self.calls.append(req)
        labels = self.responder(req, index)
        presented = parse_presented_options(req.user)
        texts = [text for label, text in presented if label in labels]
        text_payload: dict[str, Any] = {"chosen_labels": labels, "reason": "fake"}
        parsed: dict[str, Any] = {"chosen_labels": labels, "chosen_texts": texts}
        if self.confidence is not None:
            # 真 Provider 走 ``parse_answer_payload``，自报值经同一条路进来
            text_payload["confidence"] = self.confidence
            parsed["confidence"] = self.confidence
        return LLMResponse(
            text=json.dumps(text_payload, ensure_ascii=False),
            parsed=parsed,
            latency_ms=1,
            raw="fake-raw",
        )

    async def aclose(self) -> None:
        return


class FlakyProvider(LLMProvider):
    """前 ``unusable_times`` 次回一条**不可用**的回复（没有 content、解析不出）。

    这正是真机上的「模型这次没吐出东西」：``text`` 为空 → 解析不出 →
    有效样本为 0。它必须与「模型明确说答不了」区分开。
    """

    name = ProviderName.OPENAI_COMPAT

    def __init__(
        self,
        truth: list[str],
        *,
        unusable_times: int = 1,
        confidence: float | None = None,
    ) -> None:
        self._truth = truth
        self._unusable_times = unusable_times
        self.confidence = confidence
        self.calls: list[LLMRequest] = []

    def model_for(self) -> str | None:
        return "flaky-model"

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        if len(self.calls) <= self._unusable_times:
            return LLMResponse(text="", parsed=None, raw="flaky-empty")
        labels = pick(req, self._truth)
        presented = parse_presented_options(req.user)
        texts = [text for label, text in presented if label in labels]
        parsed: dict[str, Any] = {"chosen_labels": labels, "chosen_texts": texts}
        text_payload: dict[str, Any] = {"chosen_labels": labels, "reason": "flaky"}
        if self.confidence is not None:
            parsed["confidence"] = self.confidence
            text_payload["confidence"] = self.confidence
        return LLMResponse(
            text=json.dumps(text_payload, ensure_ascii=False),
            parsed=parsed,
            latency_ms=1,
            raw="flaky-raw",
        )

    async def aclose(self) -> None:
        return


#: 模型**明确**空作答的回复（题面不足以下结论），走真解析器进来
EXPLICIT_EMPTY_REPLY = '{"chosen_labels": [], "confidence": 0, "reason": "题面缺失：选项D的公式未能读出"}'


class ExplicitEmptyProvider(LLMProvider):
    """永远回显式空作答 —— 这是模型的**结论**，不是一次坏回复。"""

    name = ProviderName.OPENAI_COMPAT

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []

    def model_for(self) -> str | None:
        return "empty-model"

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        return LLMResponse(
            text=EXPLICIT_EMPTY_REPLY,
            parsed=parse_answer_payload(EXPLICIT_EMPTY_REPLY),
            latency_ms=1,
            raw=EXPLICIT_EMPTY_REPLY,
        )

    async def aclose(self) -> None:
        return


def make_solver(
    providers: list[LLMProvider],
    *,
    cfg: RunConfig | None = None,
    cache: SolveCache | None = None,
    bus: EventBus | None = None,
    seed: int = 0,
) -> Solver:
    return Solver(
        providers,
        cache if cache is not None else SolveCache(),
        cfg if cfg is not None else RunConfig(),
        bus=bus,
        rng=random.Random(seed),
    )


async def drain(stream: EventStream) -> list[tuple[str, dict]]:
    """取出订阅队列里已投递的事件，然后注销订阅。

    ``emit()`` 是同步投递，用例里又没起消费者任务，所以断言前直接读队列即可。
    """
    events: list[tuple[str, dict]] = []
    while not stream._queue.empty():  # 测试直读内部队列，避免为一个断言起消费任务
        events.append(stream._queue.get_nowait())
    await stream.aclose()
    return events


def names_of(events: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in events]


def payloads_of(events: list[tuple[str, dict]], name: str) -> list[dict]:
    return [payload for event, payload in events if event == name]


def page_pairs(question: Question) -> list[tuple[str, str]]:
    return list(zip(question.option_labels, question.option_texts, strict=True))


# --------------------------------------------------------------------------- #
# 采样批次：**恒按页面顺序**，自指检测只作风险标注（T0-4）
# --------------------------------------------------------------------------- #
def test_self_ref_question_is_never_shuffled() -> None:
    question = make_question(["甲说法", "乙说法", "以上都对"])
    solver = make_solver([FakeProvider(always(["甲说法"]))], cfg=recalc_config(5))

    assert allows_shuffle(question) is False, "自指检测保留（它描述的是一类真实风险）"
    batches = solver.build_sampling_batch(question)

    assert len(batches) == 5
    expected = page_pairs(question)
    assert all(batch == expected for batch in batches), "命中自指 → 整批不打乱"


def test_clean_question_is_not_shuffled_either() -> None:
    """p6.0：**谁都不打乱** —— 标号必须逐字等于页面标号，否则点错选项。"""
    question = make_question()
    solver = make_solver([FakeProvider(always(["SYN 报文"]))], cfg=recalc_config(5))

    assert allows_shuffle(question) is True, "「允许打乱」只是风险判断，不再是一条控制流"
    batches = solver.build_sampling_batch(question)

    assert len(batches) == 5
    assert len({tuple(batch) for batch in batches}) == 1, "多次采样看到的是同一份呈现顺序"
    expected = page_pairs(question)
    assert all(batch == expected for batch in batches)


def test_batch_pairs_page_labels_with_their_texts() -> None:
    """非连续标号（A, B, D）：批次里第三项是 ``("D", ...)``，不是 ``("C", ...)``。"""
    question = make_question(["SYN 报文", "ACK 报文", "FIN 报文"], labels=["A", "B", "D"])
    solver = make_solver([FakeProvider(always(["FIN 报文"]))], cfg=recalc_config(3))

    batches = solver.build_sampling_batch(question)

    assert batches[0] == [
        ("A", "SYN 报文"),
        ("B", "ACK 报文"),
        ("D", "FIN 报文"),
    ]


async def test_model_sees_page_labels() -> None:
    """渲染给模型的标号 = 页面标号：非连续标号下**不许**出现位置字母 C。"""
    question = make_question(["SYN 报文", "ACK 报文", "FIN 报文"], labels=["A", "B", "D"])
    provider = FakeProvider(always(["FIN 报文"]))
    solver = make_solver([provider])

    answer = await solver.solve(question)

    user = provider.calls[0].user
    assert "A. SYN 报文" in user
    assert "B. ACK 报文" in user
    assert "D. FIN 报文" in user
    assert "\nC. " not in user, "页面上没有 C，就不能让模型看到 C"
    assert answer.chosen_labels == ["D"], "作答里必须是页面标号 D，而不是位置字母 C"
    assert answer.chosen_texts == ["FIN 报文"]


async def test_a_solve_request_with_images_blows_up_loudly() -> None:
    """**解题请求不许带图** —— 真带了要当场炸，而不是悄悄发出去。

    用户的原话是「解题组模型严禁拿到非文本输入」。构造请求的那一处从来不填
    ``images``，所以这条约束平时看不出来；这里直接拿一个带了图的请求去撞那道硬闸，
    保证将来某次「顺手把截图也发过去」会在第一时间暴露 —— 真机上发图之后
    推理模型会不回答、只烧 token（content 留空），那种故障从输出上完全看不出来。
    """
    solver = make_solver([FakeProvider(always(["SYN 报文"]))])
    request = LLMRequest(
        model="fake",
        system="sys",
        user="user",
        images=[b"\x89PNG-fake"],
    )

    import pytest

    with pytest.raises(RuntimeError, match="不许带图"):
        await solver._complete(solver.providers[0], request)


async def test_self_ref_question_samples_at_default_temperature() -> None:
    """自指题不再降到 0 度：呈现顺序与温度都不再因自指改变。"""
    question = make_question(["甲说法", "乙说法", "以上都对"])
    provider = FakeProvider(always(["甲说法"]))
    solver = make_solver([provider], cfg=recalc_config(3))

    await solver.solve(question)

    assert provider.calls, "应当真的发了请求"
    assert {req.temperature for req in provider.calls} == {DEFAULT_TEMPERATURE}


async def test_shufflable_question_samples_at_default_temperature() -> None:
    provider = FakeProvider(always(["SYN 报文"]))
    solver = make_solver([provider])

    await solver.solve(make_question())

    assert {req.temperature for req in provider.calls} == {DEFAULT_TEMPERATURE}


def test_default_asks_the_model_once() -> None:
    """默认**不复算** → 只取一次样，第一次答案即最终答案（用户要求的行为）。"""
    solver = make_solver([FakeProvider(always(["SYN 报文"]))])

    assert solver.cfg.recalculate is False
    assert solver.cfg.sample_n == 1
    assert len(solver.build_sampling_batch(make_question())) == 1


def test_recalculate_can_be_turned_on_with_a_count() -> None:
    """复算是**能力**，不是隐式升级：显式打开并给次数，就真的发那么多次。"""
    solver = make_solver([FakeProvider(always(["SYN 报文"]))], cfg=recalc_config(5))

    assert solver.cfg.recalculate is True
    assert len(solver.build_sampling_batch(make_question())) == 5


def test_recalculate_requires_at_least_two_samples() -> None:
    """「复算 1 次」等于不复算 —— 配置阶段就该被拒，而不是跑完才发现白设置。"""
    import pytest

    with pytest.raises(ValueError):
        RunConfig(recalculate=True, sample_n=1)


def test_batches_are_copies_not_views_of_the_question() -> None:
    """批次必须是副本：求解过程不得就地改动 Question 的选项顺序。"""
    question = make_question()
    solver = make_solver([FakeProvider(always(["SYN 报文"]))])

    batches = solver.build_sampling_batch(question)
    batches[0].reverse()

    assert question.option_texts == OPTIONS


# --------------------------------------------------------------------------- #
# 投票收敛（MockProvider 注入错误率）
# --------------------------------------------------------------------------- #
async def test_mock_vote_converges_under_injected_error_rate() -> None:
    """注入 40% 错误率，复算 21 次 —— 多数票仍应收敛回真值（投票逻辑的验收点）。"""
    question = make_question()
    provider = MockProvider(QuestionBankSource.from_default(), error_rate=0.4, seed=20260925)
    solver = make_solver([provider], cfg=recalc_config(21))

    answer = await solver.solve(question)

    assert answer.chosen_texts == ["SYN 报文"], "40% 的错答不该盖过真值"
    assert answer.chosen_labels == ["A"]
    assert answer.confidence > 0.5
    assert answer.solve_path is SolvePath.MOCK
    assert answer.samples == 21
    assert answer.stem_hash == question.stem_hash


async def test_recalculation_keeps_the_majority_answer_and_cache_state_is_honest() -> None:
    """复算后多数票仍指向真值；**是否进缓存必须与「有没有标复核」一致**。

    2026-09-29 起这条铁律更严了：复算过之后一致率低于门限就标 ``⚠复核``
    （旧的 Tier 实现只在「升级过」的那条分支里查一致率，直接进 Tier2 的题
    反倒不查 —— 那正是「看起来正常的错答案」最容易溜过去的地方）。
    所以这里断言的是**不变式**，而不是写死一个布尔值。
    """
    question = make_question()
    provider = MockProvider(QuestionBankSource.from_default(), error_rate=0.1, seed=7)
    cache = SolveCache()
    solver = make_solver([provider], cfg=recalc_config(15), cache=cache)

    answer = await solver.solve(question)

    assert answer.chosen_texts == ["SYN 报文"], "注入的错答不该盖过真值"
    assert answer.confidence > 0.5
    assert answer.solve_path is SolvePath.MOCK
    assert (cache.size() == 1) is (answer.review_flag is False), (
        "带复核标记的答案绝不能进缓存（下一轮会被当成确定结论）"
    )


async def test_mock_vote_is_content_based_not_letter_based() -> None:
    """把题干选项顺序整体调换，答案的**内容**必须不变。"""
    original = make_question()
    reordered = make_question(["RST 报文", "FIN 报文", "ACK 报文", "SYN 报文"])

    first = await make_solver(
        [MockProvider(QuestionBankSource.from_default(), seed=1)]
    ).solve(original)
    second = await make_solver(
        [MockProvider(QuestionBankSource.from_default(), seed=1)]
    ).solve(reordered)

    assert first.chosen_texts == ["SYN 报文"]
    assert second.chosen_texts == ["SYN 报文"]
    assert first.chosen_labels != second.chosen_labels, "页面标号变了，但内容一样"


#: 一道**只有三个选项**的题（视觉组丢掉某个选项的框时，标号就会是 A、B、D）
DISCONTINUOUS_PAYLOAD: dict[str, Any] = {
    "questions": [
        {
            "index": 7,
            "qtype": "single",
            "stem": "下列哪一项是质数？",
            "options": ["4", "6", "7"],
            "answer": [2],
        }
    ]
}


async def test_mock_answers_the_page_label_on_a_discontinuous_question() -> None:
    """非连续标号（A, B, D）：Mock 必须回页面上**真实存在**的 ``D``。

    这是真机故障（模型回 D、程序按位置落回 C）的最小复现形状：
    第三项的页面标号是 ``D``，位置字母是 ``C``，两者必须由页面标号一锤定音。
    """
    provider = MockProvider(QuestionBankSource.from_payload(DISCONTINUOUS_PAYLOAD))
    question = make_question(["4", "6", "7"], stem="下列哪一项是质数？", labels=["A", "B", "D"])
    solver = make_solver([provider])

    answer = await solver.solve(question)

    assert answer.chosen_texts == ["7"]
    assert answer.chosen_labels == ["D"], "页面标号是 D；位置字母会把它写成 C"


async def test_all_invalid_samples_marks_review_without_raising() -> None:
    """Provider 全挂也必须给出带复核标记的作答，**不能抛异常**。"""

    def broken(_req: LLMRequest, _index: int) -> list[str]:
        raise ProviderError("boom", code="provider_error")

    provider = FakeProvider(broken)
    answer = await make_solver([provider]).solve(make_question())

    assert answer.review_flag is True
    assert answer.chosen_labels == []
    assert answer.samples == 0
    assert answer.confidence == 0.0
    assert len(provider.calls) == SAMPLE_RETRY_MAX + 1, "重发到上限才停"


# --------------------------------------------------------------------------- #
# 复算：只有开了才投票，只有真投过票才谈「一致率」
# --------------------------------------------------------------------------- #
async def test_recalculate_off_uses_the_first_sample_even_when_it_looks_weird() -> None:
    """**不复算就以第一次答案为准** —— 哪怕它看起来「不一致」也不复算。"""
    provider = FakeProvider(cycle(["SYN 报文"], ["ACK 报文"], ["FIN 报文"]))
    solver = make_solver([provider])

    answer = await solver.solve(make_question())

    assert len(provider.calls) == 1, "不复算只调一次模型"
    assert answer.samples == 1
    assert answer.review_flag is False, "单样本的一致率恒为 1，不该触发复核"
    assert answer.chosen_texts == ["SYN 报文"]


async def test_recalculate_on_votes_across_samples() -> None:
    """开复算后，多数票（而不是第一次采样）才是最终答案。"""
    provider = FakeProvider(cycle(["ACK 报文"], ["SYN 报文"], ["SYN 报文"]))
    solver = make_solver([provider], cfg=recalc_config(3))

    answer = await solver.solve(make_question())

    assert len(provider.calls) == 3
    assert answer.samples == 3
    assert answer.chosen_texts == ["SYN 报文"], "2/3 的多数票胜出，而不是第一次的 ACK"


async def test_recalculated_disagreement_marks_review_and_skips_cache() -> None:
    """复算后一致率低于门限 → ``⚠复核`` 必停，且**不得进缓存**。"""
    provider = FakeProvider(cycle(["ACK 报文"], ["FIN 报文"]))
    cache = SolveCache()
    solver = make_solver([provider], cfg=recalc_config(4), cache=cache)

    answer = await solver.solve(make_question())

    assert answer.review_flag is True
    assert cache.size() == 0, "带复核标记的答案缓存下来会在下一轮被当成确定结论"


async def test_clean_recalculation_is_clean() -> None:
    """复算且一致率达标 → 干净采用，不标复核，可以进缓存。"""
    provider = FakeProvider(always(["SYN 报文"]))
    cache = SolveCache()
    solver = make_solver([provider], cfg=recalc_config(3), cache=cache)

    answer = await solver.solve(make_question())

    assert answer.review_flag is False
    assert answer.confidence == 1.0
    assert cache.size() == 1


async def test_clean_answer_is_cached_and_cache_hit_marks_path() -> None:
    provider = FakeProvider(always(["SYN 报文"]))
    cache = SolveCache()
    solver = make_solver([provider], cache=cache)
    question = make_question()

    first = await solver.solve(question)
    calls_after_first = len(provider.calls)
    second = await solver.solve(question)

    assert cache.size() == 1
    assert len(provider.calls) == calls_after_first, "缓存命中不得再调模型"
    assert second.solve_path is SolvePath.CACHE
    assert second.chosen_labels == first.chosen_labels
    assert second.confidence == first.confidence


async def test_solve_is_text_only() -> None:
    """判题组**只吃文字**，不再把截图发回模型（推理模型会烧光预算、答案留空）。"""
    provider = FakeProvider(always(["SYN 报文"]))
    solver = make_solver([provider])

    answer = await solver.solve(make_question())

    assert answer.solve_path is SolvePath.SINGLE
    assert all(req.images == [] for req in provider.calls), "判题请求不带任何图片"


async def test_judgment_skill_is_forwarded_without_images() -> None:
    provider = FakeProvider(always(["正确"]))
    solver = make_solver([provider])
    question = make_question(["正确", "错误"], qtype=QType.TRUE_FALSE, skill_id="true_false")

    await solver.solve(question)

    assert provider.calls
    assert all("技能ID: true_false" in req.user for req in provider.calls)
    assert all("先根据题干所给条件判断陈述真假" in req.user for req in provider.calls)
    assert all(req.images == [] for req in provider.calls)


async def test_solver_refuses_missing_or_mismatched_skill() -> None:
    solver = make_solver([])
    question = make_question(["甲", "乙"], qtype=QType.TRUE_FALSE, skill_id="single_choice")

    with pytest.raises(ValueError, match="没有匹配的解题技能"):
        await solver.solve(question)


async def test_truncated_flag_is_forwarded_into_the_prompt() -> None:
    provider = FakeProvider(always(["SYN 报文"]))
    solver = make_solver([provider])

    await solver.solve(make_question(), truncated=True)

    assert all("可能不完整" in req.user for req in provider.calls)


# --------------------------------------------------------------------------- #
# C. 回复不可用 → 有界重发；显式空作答 → 不重发
# --------------------------------------------------------------------------- #
async def test_unusable_reply_is_resent_and_still_answers() -> None:
    """不复算时第一次回复不可用 → **重发**，重发成功仍能给出答案。"""
    provider = FlakyProvider(["SYN 报文"], unusable_times=1)
    solver = make_solver([provider])

    answer = await solver.solve(make_question())

    assert len(provider.calls) == 2, "1 次原始请求 + 1 次重发"
    assert answer.chosen_labels == ["A"]
    assert answer.chosen_texts == ["SYN 报文"]
    assert answer.review_flag is False
    assert answer.samples == 1, "重发**不算复算**：有效样本仍是 1"


async def test_retry_is_bounded_and_then_stops_honestly() -> None:
    """重发用尽仍不可用 → 保持现在的诚实停下，且**请求数有界**。"""
    provider = FlakyProvider(["SYN 报文"], unusable_times=SAMPLE_RETRY_MAX + 1)
    solver = make_solver([provider])

    answer = await solver.solve(make_question())

    assert len(provider.calls) == SAMPLE_RETRY_MAX + 1, "1 次 + 最多 SAMPLE_RETRY_MAX 次重发"
    assert answer.chosen_labels == []
    assert answer.review_flag is True
    assert answer.confidence == 0.0
    assert answer.samples == 0


async def test_retry_records_every_attempt_with_its_own_error_code() -> None:
    """每次尝试都要进 ``VoteResult.samples``，并带自己的 ``error_code``。"""
    provider = FlakyProvider(["SYN 报文"], unusable_times=SAMPLE_RETRY_MAX + 1)
    solver = make_solver([provider])
    question = make_question()

    await solver.solve(question)

    vote = solver.last_vote
    assert vote is not None
    assert len(vote.samples) == SAMPLE_RETRY_MAX + 1
    assert [record.sample_index for record in vote.samples] == [0, 1, 2]
    assert {record.error_code for record in vote.samples} == {"unparsable_sample"}


async def test_retry_does_not_turn_into_recalculation() -> None:
    """``recalculated`` 只表示「是否按多次有效样本投票」，重发不改变它。"""
    bus = EventBus()
    stream = bus.subscribe()
    provider = FlakyProvider(["SYN 报文"], unusable_times=1)
    solver = make_solver([provider], bus=bus)

    await solver.solve(make_question())

    payloads = payloads_of(await drain(stream), Event.SOLVE_VOTE)
    assert payloads
    assert payloads[0]["recalculated"] is False
    assert payloads[0]["n_samples"] == 1
    assert set(payloads[0]) == {
        "qid",
        "recalculated",
        "n_samples",
        "majority_ratio",
        "distribution",
    }, "事件 payload 的键不许增删（编排层与测试按它断言）"


async def test_out_of_range_label_counts_as_unusable_and_is_resent() -> None:
    """标号越界也算「回复不可用」—— 重发，而不是当成模型的答案。"""
    provider = FakeProvider(always(["Z"]))  # 页面上没有 Z
    solver = make_solver([provider])

    answer = await solver.solve(make_question())

    assert len(provider.calls) == SAMPLE_RETRY_MAX + 1
    assert answer.chosen_labels == []
    assert answer.review_flag is True


async def test_explicit_empty_answer_is_not_resent() -> None:
    """显式空作答是**模型的结论**：不重发，直接停下等人。"""
    provider = ExplicitEmptyProvider()
    solver = make_solver([provider])

    answer = await solver.solve(make_question())

    assert len(provider.calls) == 1, "结论不是请求失败，重发没有意义"
    assert answer.chosen_labels == []
    assert answer.review_flag is True
    assert answer.confidence == 0.0


async def test_explicit_empty_and_no_valid_sample_have_distinct_reasons() -> None:
    """两种「没有答案」的理由必须分得开，否则日志里长得一模一样。"""
    empty_bus = EventBus()
    empty_stream = empty_bus.subscribe()
    await make_solver([ExplicitEmptyProvider()], bus=empty_bus).solve(make_question())
    empty_reasons = payloads_of(
        await drain(empty_stream), Event.SOLVE_REVIEW_REQUIRED
    )

    broken_bus = EventBus()
    broken_stream = broken_bus.subscribe()

    def broken(_req: LLMRequest, _index: int) -> list[str]:
        raise ProviderError("boom", code="provider_error")

    await make_solver([FakeProvider(broken)], bus=broken_bus).solve(make_question())
    broken_reasons = payloads_of(
        await drain(broken_stream), Event.SOLVE_REVIEW_REQUIRED
    )

    assert empty_reasons and broken_reasons
    assert empty_reasons[0]["reason"] != broken_reasons[0]["reason"]
    assert "空作答" in empty_reasons[0]["reason"]
    assert "无有效样本" in broken_reasons[0]["reason"]


# --------------------------------------------------------------------------- #
# B. 自报 confidence
# --------------------------------------------------------------------------- #
def test_confidence_review_min_default_is_0_5() -> None:
    """新旋钮的默认值就是契约（``core/config.py`` 顶部清单同步写明了它）。"""
    assert GuardThresholds().confidence_review_min == 0.5


async def test_single_sample_uses_the_self_reported_confidence() -> None:
    """不复算（单样本）→ ``Answer.confidence`` 就是模型自报值。"""
    provider = FakeProvider(always(["SYN 报文"]), confidence=0.92)
    solver = make_solver([provider])

    answer = await solver.solve(make_question())

    assert answer.confidence == 0.92
    assert answer.review_flag is False


async def test_low_self_reported_confidence_marks_review_and_skips_cache() -> None:
    """低于 ``guards.confidence_review_min`` 的自报值 → ``⚠复核`` 必停。"""
    bus = EventBus()
    stream = bus.subscribe()
    cache = SolveCache()
    provider = FakeProvider(always(["SYN 报文"]), confidence=0.2)
    solver = make_solver([provider], cache=cache, bus=bus)

    answer = await solver.solve(make_question())

    assert answer.confidence == 0.2
    assert answer.review_flag is True
    assert cache.size() == 0, "自报没把握的答案不许进缓存"
    payloads = payloads_of(await drain(stream), Event.SOLVE_REVIEW_REQUIRED)
    assert payloads and payloads[0]["reason"] == "自报置信度低于门限"


async def test_confidence_threshold_is_configurable() -> None:
    """门槛本身是具名旋钮：调高它，原本能过的作答就该停下。"""
    provider = FakeProvider(always(["SYN 报文"]), confidence=0.8)
    cfg = RunConfig(guards=GuardThresholds(confidence_review_min=0.9))
    solver = make_solver([provider], cfg=cfg)

    answer = await solver.solve(make_question())

    assert answer.confidence == 0.8
    assert answer.review_flag is True


async def test_missing_confidence_keeps_the_old_semantics() -> None:
    """模型没报 confidence → 回到一致率（单样本即 1.0），**不新增暂停**。"""
    provider = FakeProvider(always(["SYN 报文"]))
    solver = make_solver([provider])

    answer = await solver.solve(make_question())

    assert answer.confidence == 1.0
    assert answer.review_flag is False


async def test_recalculation_ignores_the_self_reported_confidence() -> None:
    """复算时 confidence 仍是多数票占比 —— 自报值不参与那个口径。"""
    provider = FakeProvider(always(["SYN 报文"]), confidence=0.1)
    solver = make_solver([provider], cfg=recalc_config(3))

    answer = await solver.solve(make_question())

    assert answer.confidence == 1.0
    assert answer.review_flag is False


# --------------------------------------------------------------------------- #
# 降级链
# --------------------------------------------------------------------------- #
async def test_failing_provider_falls_back_to_the_next_one() -> None:
    def broken(_req: LLMRequest, _index: int) -> list[str]:
        raise AuthError("no key")

    good = FakeProvider(always(["SYN 报文"]))
    solver = make_solver([FakeProvider(broken), good])

    answer = await solver.solve(make_question())

    assert answer.chosen_texts == ["SYN 报文"]
    assert answer.review_flag is False
    assert good.calls, "降级链的第二条应当被用上"


async def test_second_provider_used_when_first_is_rate_limited() -> None:
    def limited(_req: LLMRequest, _index: int) -> list[str]:
        raise ProviderError("429", code="rate_limited")

    fallback = FakeProvider(always(["SYN 报文"]))
    answer = await make_solver([FakeProvider(limited), fallback]).solve(make_question())

    assert answer.chosen_texts == ["SYN 报文"]
    assert answer.review_flag is False


async def test_error_names_are_recorded_in_sample_details() -> None:
    def limited(_req: LLMRequest, _index: int) -> list[str]:
        raise ProviderError("429", code="rate_limited")

    solver = make_solver([FakeProvider(limited)])
    question = make_question()
    from solve.skill_library import skill_prompt

    outcome = await solver._run_pass(
        question,
        solver.build_sampling_batch(question),
        solver.providers,
        skill_text=skill_prompt("single_choice"),
    )

    assert outcome.vote.n_samples == 0
    assert {record.error_code for record in outcome.vote.samples} == {"rate_limited"}
    assert len(outcome.vote.samples) == SAMPLE_RETRY_MAX + 1, "每次尝试都留痕"


# --------------------------------------------------------------------------- #
# 事件与标号
# --------------------------------------------------------------------------- #
async def test_solve_emits_vote_and_done_events() -> None:
    bus = EventBus()
    stream = bus.subscribe()
    solver = make_solver([FakeProvider(always(["SYN 报文"]))], bus=bus)

    await solver.solve(make_question())

    names = names_of(await drain(stream))
    assert Event.SOLVE_VOTE in names
    assert Event.SOLVE_DONE in names
    assert "solve.escalated" not in names, "Tier 升级事件已删除，不许再发"


async def test_vote_event_says_whether_it_recalculated() -> None:
    """票面必须能看出「这次到底复算过没有」—— 那是「花了多少钱」的唯一凭据。"""
    bus = EventBus()
    stream = bus.subscribe()
    solver = make_solver([FakeProvider(always(["SYN 报文"]))], cfg=recalc_config(4), bus=bus)

    await solver.solve(make_question())

    payloads = payloads_of(await drain(stream), Event.SOLVE_VOTE)
    assert payloads and payloads[0]["recalculated"] is True
    assert payloads[0]["n_samples"] == 4


async def test_review_event_carries_a_reason() -> None:
    bus = EventBus()
    stream = bus.subscribe()
    solver = make_solver(
        [FakeProvider(cycle(["ACK 报文"], ["FIN 报文"]))],
        cfg=recalc_config(4),
        bus=bus,
    )

    await solver.solve(make_question())

    payloads = payloads_of(await drain(stream), Event.SOLVE_REVIEW_REQUIRED)
    assert payloads
    assert payloads[0]["reason"] == "复算后一致率低于门限"


async def test_cached_solve_emits_done_event_with_cached_flag() -> None:
    bus = EventBus()
    stream = bus.subscribe()
    solver = make_solver([FakeProvider(always(["SYN 报文"]))], bus=bus)
    question = make_question()

    await solver.solve(question)
    await drain(stream)
    stream = bus.subscribe()
    await solver.solve(question)

    payloads = payloads_of(await drain(stream), Event.SOLVE_DONE)
    assert payloads and payloads[0]["cached"] is True


def test_answer_labels_are_page_labels() -> None:
    """投票层用「原始序号字母」，作答里必须是页面真实标号 + 正文。"""
    labels, texts = Solver._to_page_labels(make_question(), ["A", "C"])
    assert labels == ["A", "C"]
    assert texts == ["SYN 报文", "FIN 报文"]


def test_to_page_labels_uses_the_page_label_not_the_position() -> None:
    """非连续标号：第三项（序号 2）的页面标号是 ``D``，不是位置字母 ``C``。"""
    question = make_question(["SYN 报文", "ACK 报文", "FIN 报文"], labels=["A", "B", "D"])
    labels, texts = Solver._to_page_labels(question, ["C"])
    assert labels == ["D"]
    assert texts == ["FIN 报文"]


def test_to_page_labels_ignores_out_of_range() -> None:
    labels, texts = Solver._to_page_labels(make_question(["甲", "乙"]), ["C"])
    assert labels == []
    assert texts == []


def test_mark_review_keeps_everything_else() -> None:
    solver = make_solver([])
    answer = Answer(
        qid="q1",
        chosen_labels=["A"],
        chosen_texts=["甲"],
        confidence=0.9,
        solve_path=SolvePath.SINGLE,
        review_flag=False,
    )

    marked = solver.mark_review(answer, "test")

    assert marked.review_flag is True
    assert marked.chosen_labels == ["A"]
    assert marked.confidence == 0.9


def test_prompt_states_question_type_and_labels() -> None:
    question = make_question(["甲", "乙"], qtype=QType.MULTIPLE)
    _, user = build_messages(question, page_pairs(question))

    assert "多选题" in user
    assert "A. 甲" in user
    assert "B. 乙" in user
    assert question.qid in user


def test_bank_file_is_readable_for_mock_default() -> None:
    if not BANK_PATH.exists():  # pragma: no cover - 靶场缺失时不算失败
        import pytest

        pytest.skip("靶场题库不存在")
    payload = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    questions = payload["questions"]
    canvas_count = sum(1 for entry in questions if entry.get("canvas_stem"))
    # 每题一条索引，canvas 题额外多一条镜像题干（见 QuestionBankSource 文档）
    assert QuestionBankSource.from_default().size() == len(questions) + canvas_count


def test_provider_chain_iteration_is_deterministic() -> None:
    """降级链顺序即配置顺序，不能被集合/字典打乱。"""
    first = FakeProvider(always(["SYN 报文"]), name=ProviderName.MOCK)
    second = FakeProvider(always(["ACK 报文"]))
    solver = make_solver([first, second])

    assert list(solver.providers) == [first, second]


def test_empty_option_question_still_produces_batches() -> None:
    solver = make_solver([FakeProvider(always([]))])
    batches = solver.build_sampling_batch(make_question([]))
    assert len(batches) == 1
    assert all(batch == [] for batch in batches)


def test_vote_result_is_exposed_for_logging() -> None:
    """``last_vote`` 仍要给编排层取用（写 ``solve.json`` 的采样明细靠它）。"""
    solver = make_solver([])
    assert solver.last_vote is None
    assert isinstance(VoteResult(chosen_labels=[], majority_ratio=0.0), VoteResult)
