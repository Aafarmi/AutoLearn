"""求解器（M2-3 / T0-3 / T0-4）。

自指选项（T0-4）：检测保留，但它**不再改变呈现顺序**
----------------------------------------------------
``SELF_REF_PATTERN`` / ``is_self_referential`` / ``question_has_self_ref`` /
``allows_shuffle`` 仍然保留（都在 ``__all__`` 里，也仍有测试），描述的是一类
**真实风险**：「以上都对」「前者」「A 和 C」这类选项的含义锚定在选项的排列位置上，
一旦被重排，「以上」指向的集合就变了 —— 模型会算出一道语义上根本不存在的题。

但**呈现顺序不再由它们决定**：``_build_batch`` 一律按页面顺序。两条理由：

1. **解题组必须拿到视觉组翻译出来的那一份固定格式本身。** 模型回的字母是
   「点哪个选项」的操作指令（见 ``prompts/20-解题组.md``），所以它看到的标号
   必须**逐字等于页面标号**。打乱一次，标号与页面的对应关系就错位一次：
   模型答 ``D``、程序按位置映射落回 ``C`` —— 这正是真机日志
   ``logs/10ca5ee8c89e/.../solve.json`` 里的现场（视觉组给的非连续标号
   ``A, B, D`` 更会把位置字母彻底打乱）。
2. **不复算时打乱没有任何收益。** 抗位置偏置靠的是「同一题看到不同的排列、
   再比一致性」，而默认路径只有**一次**采样：打乱只是换了个视角，
   没有任何东西可以对照，却要让标号冒一次错位的风险。复算时也不打乱 ——
   那就得维护一套「呈现 ↔ 页面」双向映射，而映射一旦漏一环，
   错误的呈现方式**从输出上看不出来**。宁可少一层映射。

于是自指检测降级为**风险标注**（``allows_shuffle`` 仍可被评审 / 留痕调用），
温度也不再分档：一律 ``DEFAULT_TEMPERATURE``。

「回复不可用」与「模型明确空作答」
----------------------------------
两者在下游是**完全不同的动作**（见 :data:`SAMPLE_RETRY_MAX`）：

- **不可用**（没 content / 解析不出 / 标号越界 / Provider 报错）= **这次请求失败**，
  与「这道题没有答案」无关 → 有界重发；
- **显式空作答**（``chosen_labels`` 字段存在且为空数组）= **模型的结论** →
  不重发，直接标 ``⚠复核`` 停下问人。

把它们混成一个空的 ``chosen_labels``，故障表现就是「解题组解不了这道题」，
而真实原因只是那一次回复坏了 —— 而且因为开复算时第二个样本能顺手救回来，
这个故障**只在不复算时暴露**。

Tier 路由已删除（2026-09-29）
--------------------------------
原先 ``agreement < 0.8`` 会自动「升级到 Tier2 复算」，带图 / Canvas / 截断的题
**直接**走 Tier2。那套隐式升级整体删除 —— 现在只有一种求解模式：

- 默认**只取第一次答案**（``RunConfig.recalculate=False``）；
- 用户在建任务时显式勾选「复算」→ 按 ``RunConfig.sample_n`` 次取样，
  再按**内容**投票，多数解为最终答案。

「要不要多花钱」由用户在创建任务时决定，而不是由一致率触发的隐式升级决定。
唯一保留的自动动作是：复算出来的一致率低于 ``guards.agreement_accept`` 时
标 ``⚠复核`` 并必停等人 —— 那是「结果有分歧」的如实上报，不是模式切换。

``confidence`` 的两种口径
-------------------------
- **复算**（有效样本 > 1）→ 投票多数占比（「多个样本有多一致」）；
- **不复算**（单样本）→ 模型自报的把握（``prompts/20-解题组.md`` 的输出契约里
  新增的 ``confidence`` 字段）。单样本的一致率恒为 1，那个 1.0 不携带任何信息；
  模型没报时回到一致率（= 1.0），**不因为「没报」而新增暂停**。
  自报值低于 ``guards.confidence_review_min`` → 标 ``⚠复核``。

「必停」在这里的落地形式
------------------------
Solver 是纯函数式的求解环节，**不自己暂停进程**：它把
``Answer.review_flag`` 置真、并往事件总线发 ``solve.review_required``，
由编排层（P7）据此挂起任务并落截图。求解层擅自 ``await`` 一个人工确认会
把编排层的状态机切成两半，续跑语义就没了。
"""

from __future__ import annotations

import random
import re
from contextlib import suppress
from typing import Any, NamedTuple

from core.config import RunConfig
from core.enums import ProviderName, SolvePath
from core.events import Event
from core.models import Answer, Question, VoteResult
from core.qid import normalize_text
from core.ratelimit import ConcurrencyGate
from core.trace import EventBus
from solve.cache import SolveCache
from solve.prompts import CONFIDENCE_KEY, OptionPair, build_messages
from solve.providers.base import LLMProvider, LLMRequest, LLMResponse, ProviderError
from solve.providers.factory import build_mock_provider
from solve.skill_library import skill_for_question, skill_prompt
from solve.voting import KEY_OPTION_LABELS, KEY_OPTION_TEXTS, KEY_ORIGINAL_TEXTS, VotingEngine

__all__ = [
    "DEFAULT_TEMPERATURE",
    "LOW_TEMPERATURE",
    "SAMPLE_RETRY_MAX",
    "SELF_REF_PATTERN",
    "SOLVE_MAX_TOKENS",
    "Solver",
    "allows_shuffle",
    "is_self_referential",
    "question_has_self_ref",
]

#: 自指选项白名单正则（T0-4）。
#:
#: 逐条对应 T0-4 要求的覆盖面：
#:
#: =====================  ====================================================
#: ``以上|以下|上述``     指示代词类（``下述``/``上列``/``下列`` 为同族扩展）
#: ``都[对正确错]``       ``都对`` / ``都正确`` / ``都错`` / ``都不对``
#: ``均[对正确错]``       ``均对`` / ``均正确`` / ``均错`` / ``均不对``
#: ``全[部不对]``         ``全部`` / ``不全`` / ``全都对`` / ``全不正确``
#: ``前者|后者``          序数指代
#: ``(A)和`` / ``A和C``   跨选项引用
#: =====================  ====================================================
SELF_REF_PATTERN: re.Pattern[str] = re.compile(
    r"""
    (?:以上|以下|上述|下述|下列|上列)                     # 指示代词类
    |都(?:对|正确|错|不对|不正确)
    |均(?:对|正确|错|不对|不正确)
    |全(?:部|都)?(?:对|正确|错|不对|不正确)
    |^全[部不对]                                          # 「全部」「全不」「全对」
    |不全                                                 # 「不全」「不全对」
    |前者|后者
    |[（(][A-Za-z][)）]                                   # 括号标号引用：(A) / (A)和(C)
    |(?<![A-Za-z0-9])[A-Za-z]\s*[和与、,，及]\s*[A-Za-z](?![A-Za-z0-9])
                                                          # 裸标号引用：A和C / A、C
    """,
    re.VERBOSE,
)

#: 常规采样温度。多次采样要的是**分布**，温度太低会采出一堆一样的样本。
DEFAULT_TEMPERATURE = 0.2

#: 自指题的低温度：**保留但不再被使用**。
#:
#: 自指检测现在只作风险标注（``allows_shuffle``），呈现顺序不再因它改变，
#: 温度也就统一成 :data:`DEFAULT_TEMPERATURE`（见模块文档第 2 条）。
#: 留着这个常量是为了不悄悄删掉一个被文档与留痕引用过的公开名。
LOW_TEMPERATURE = 0.0

#: 单条回复**不可用**时的额外重发次数上限（**不含第一次**）。
#:
#: 为什么要有它：默认路径（不复算）只有一次采样，而「没 content / 解析不出 /
#: 标号越界 / Provider 报错」都是**这一次请求失败**，不是「这道题没有答案」。
#: 没有重发时，一次网络抖动或一次截断就直接变成 ``chosen_labels=[]``，
#: 编排层按 ``empty_answer`` 停下 —— 用户看到的是「模型解不了这道题」，
#: 真实原因却只是那一次回复坏了。开复算时第二个样本能顺手救回来，
#: 所以这个故障**只在不复算时暴露**。
#:
#: 为什么是 2：坏回复的常见成因（截断、空 content、限流抖动）重发一次就好，
#: 第二次是给「连着坏两次」留的余量；再多就说明是配置 / Provider 级别的故障，
#: 那种情况应该停下来报错，而不是把请求打成一串。
#:
#: **重发不算复算**：不提高 ``sample_n``，``solve.vote`` 的 ``recalculated``
#: 仍表示「是否按多次有效样本投票」（重发只影响 ``samples`` 明细里的条数）。
SAMPLE_RETRY_MAX = 2

#: 判题的 token 预算。给推理模型留出思维链的余量，避免「思考写完、最终 JSON 空」。
#: 判题组只吃文字（一题一答），4096 足够；2026-09-29 前这里没设（用默认 2048），
#: 推理模型对一道多选项题都可能把预算烧光 → 判题恒为空。
SOLVE_MAX_TOKENS = 4096

#: 「无有效样本」的复核理由（保持 p5.0 的措辞，编排层与留痕按它认故障）。
NO_VALID_SAMPLE_REASON = "无有效样本（Provider 全部失败）"
#: 显式空作答的复核理由：**必须与上面那条区分开** ——
#: 一条是「我们没拿到可用回复」，另一条是「模型说题面不足以下结论」。
EXPLICIT_EMPTY_REASON = "模型明确空作答（题面不足以下结论）"
#: 复算后一致率不足的复核理由（措辞冻结，测试与留痕按它断言）。
LOW_AGREEMENT_REASON = "复算后一致率低于门限"
#: 单样本自报置信度低于 ``guards.confidence_review_min`` 的复核理由。
LOW_CONFIDENCE_REASON = "自报置信度低于门限"


def is_self_referential(text: str) -> bool:
    """选项正文是否自指（即其含义取决于选项的排列位置）。

    命中的是**一类真实风险**，但请注意：p6.0 起它**不再用来决定呈现顺序**
    （一律按页面顺序，见模块文档）。
    """
    return SELF_REF_PATTERN.search(normalize_text(text)) is not None


def question_has_self_ref(question: Question) -> bool:
    """整道题里只要有一个自指选项，就值得标一次风险。"""
    return any(is_self_referential(opt.text) for opt in question.options)


def allows_shuffle(question: Question) -> bool:
    """该题是否**允许**打乱选项顺序（T0-4 的策略出口 / 风险标注）。

    .. important::
       **它不再影响任何呈现顺序。** p6.0 起 ``Solver._build_batch`` 一律按页面
       顺序，理由见模块文档（标号必须逐字等于页面标号；不复算时打乱无收益）。
       这个函数保留下来，是因为「哪一类题不能重排」是个需要能被评审、
       能被留痕、能被将来某个真正的多次采样实现复用的判断 —— 但今天的答案
       是「谁都不重排」，所以它不构成一条控制流。

    返回 ``False`` 表示这道题含自指选项；返回 ``True`` 也不代表会被打乱。
    """
    return not question_has_self_ref(question)


class _PassOutcome(NamedTuple):
    """一次 :meth:`Solver._run_pass` 的结论。"""

    #: 投票明细（按**内容**归并）
    vote: VoteResult
    #: 最后一次请求实际用的 Provider（写进 ``Answer.model_name``）
    provider: LLMProvider
    #: 是否**只有**显式空作答（模型明确说答不了）—— 与「无有效样本」区分
    explicit_empty: bool
    #: 单样本时模型自报的 ``confidence``（没报 = ``None``；复算时忽略）
    self_confidence: float | None


class Solver:
    """题目求解：取样 → 投票 →（可选）复算。**只有一种模式。**"""

    def __init__(
        self,
        providers: list[LLMProvider],
        cache: SolveCache,
        cfg: RunConfig,
        *,
        bus: EventBus | None = None,
        rng: random.Random | None = None,
        gate: ConcurrencyGate | None = None,
    ) -> None:
        self.providers = providers
        self.cache = cache
        self.cfg = cfg
        self._bus = bus
        self._rng = rng if rng is not None else random.Random()
        self._voting = VotingEngine()
        #: 请求级并发闸（M4-2）。由编排层注入 —— 求解层不该自己去读全局配置，
        #: 也不该自己 new 一个信号量（那样每个 Solver 一份配额，限流形同虚设）。
        self._gate = gate
        #: 最近一次 :meth:`solve` 的投票明细（P7 增量）。
        #:
        #: 编排层写 ``solve.json`` 时要「每题记录采样明细」，而 :class:`Answer`
        #: 里没有这个字段（也不该有 —— 作答是结论，明细是证据）。这里留一份
        #: 供编排层取用；求解循环是串行的，不会串题。
        self.last_vote: VoteResult | None = None

    def last_vote_result(self) -> VoteResult | None:
        """最近一次求解的投票明细（``None`` 表示还没解过）。"""
        return self.last_vote

    async def aclose(self) -> None:
        """释放全部 Provider 的连接池。编排层在一次运行收尾时调用。"""
        for provider in self.providers:
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                with suppress(Exception):
                    await closer()

    # -- 主流程 ------------------------------------------------------------ #
    async def solve(
        self,
        question: Question,
        *,
        truncated: bool = False,
    ) -> Answer:
        """求解一道题。**只有一种模式**：取样 →（开了复算才）投票。

        **判题只吃文字**：题干与选项是视觉组抄出来的文本，判题组不再看原图
        （推理模型对着图思考会把 token 预算烧光、最终答案留空）。
        :param truncated: 题干被截断的显式标记（读题层未写进 ``channel_trace`` 时用）。
        """
        spec = skill_for_question(question.qtype, question.skill_id)
        if spec is None or question.skill_error is not None:
            raise ValueError(
                f"题目没有匹配的解题技能：qtype={question.qtype.value}, "
                f"skill_id={question.skill_id!r}, reason={question.skill_error or 'skill_mismatch'}"
            )
        skill_text = skill_prompt(spec.skill_id)
        cached = self.cache.get(question.qid)
        if cached is not None:
            return self._finish(cached.model_copy(update={"solve_path": SolvePath.CACHE}))

        providers = self.providers or [build_mock_provider()]
        # **不复算就以第一次答案为准**：这里把「第一次」落成一次真实的取样 ——
        # batch 只有一个元素，投票也只会有一个样本，不存在「先采样再挑一个」。
        n = self._sample_count()
        batch = self._build_batch(question, n)
        outcome = await self._run_pass(
            question, batch, providers, truncated=truncated, skill_text=skill_text
        )
        final = outcome.vote
        used = outcome.provider
        self._emit(
            Event.SOLVE_VOTE,
            {
                "qid": question.qid,
                # 只由**配置**决定：重发不改变它（「有没有按多次有效样本投票」）。
                "recalculated": n > 1,
                "n_samples": final.n_samples,
                "majority_ratio": round(final.majority_ratio, 4),
                "distribution": final.distribution,
            },
        )

        review_reason = self._review_reason_of(n, final, outcome)
        self.last_vote = final

        page_labels, chosen_texts = self._to_page_labels(question, final.chosen_labels)
        answer = Answer(
            qid=question.qid,
            chosen_labels=page_labels,
            chosen_texts=chosen_texts,
            confidence=self._confidence_of(n, final, outcome),
            solve_path=self._solve_path(used),
            review_flag=False,
            model_name=used.model_for() or used.name.value,
            samples=final.n_samples,
            stem_hash=question.stem_hash,
        )
        if review_reason is not None:
            answer = self.mark_review(answer, review_reason)
        elif final.chosen_labels:
            # 只缓存「干净」的作答：带复核标记的答案缓存下来会在下一轮被当成确定结论
            self.cache.put(answer)

        self._emit(
            Event.SOLVE_DONE,
            {
                "qid": question.qid,
                "chosen_labels": answer.chosen_labels,
                "confidence": round(answer.confidence, 4),
                "solve_path": answer.solve_path.value,
                "review_flag": answer.review_flag,
                "samples": answer.samples,
            },
        )
        return answer

    def _sample_count(self) -> int:
        """本次实际取样几次。

        ``recalculate=False``（默认）→ **1**（以第一次答案为准）；
        ``recalculate=True`` → ``cfg.sample_n``（校验器已保证 ≥2）。

        注意它**不把重发算进去**：重发是「同一次采样再试一遍」，
        不是多了一个样本（见 :data:`SAMPLE_RETRY_MAX`）。
        """
        if not bool(getattr(self.cfg, "recalculate", False)):
            return 1
        return max(1, int(self.cfg.sample_n))

    def build_sampling_batch(self, q: Question) -> list[list[OptionPair]]:
        """生成本次需要的选项批次（不复算时只有一批）。

        每一项是 ``(页面标号, 正文)``；顺序**恒等于页面顺序**（见模块文档）。
        """
        return self._build_batch(q, self._sample_count())

    def mark_review(self, answer: Answer, reason: str) -> Answer:
        """标 ``⚠复核``。复算结果分歧过大、或需要人看一眼时必须调用。

        理由随事件走（``solve.review_required``），不塞进 :class:`Answer` ——
        答案模型里没有「为什么不确定」这个字段，硬塞会污染契约。
        """
        self._emit(Event.SOLVE_REVIEW_REQUIRED, {"qid": answer.qid, "reason": reason})
        return answer.model_copy(update={"review_flag": True})

    # -- 置信度与复核口径 --------------------------------------------------- #
    def _confidence_of(self, n: int, final: VoteResult, outcome: _PassOutcome) -> float:
        """这次作答的 ``confidence`` 取哪个数。

        * **显式空作答** → ``0.0``（模型的结论就是「答不了」）；
        * **复算**（``n > 1``）→ 投票多数占比（不变）。它衡量「多个样本有多一致」，
          是唯一有统计意义的量；此时模型自报值**不参与**；
        * **不复算**（``n == 1``）且模型报了 → **就用它**。单样本的一致率恒为 1，
          那个 1.0 不含信息；模型自报的把握才是这一轮唯一的新信息；
        * **模型没报**（``None``）→ 回到一致率（单样本有效即 1.0，无有效样本即 0.0），
          语义与 p5.0 完全一致，**不因为「没报」而新增任何暂停**。
        """
        if outcome.explicit_empty:
            return 0.0
        if n > 1:
            return final.majority_ratio
        if outcome.self_confidence is not None:
            return outcome.self_confidence
        return final.majority_ratio

    def _review_reason_of(self, n: int, final: VoteResult, outcome: _PassOutcome) -> str | None:
        """要不要标 ``⚠复核``，以及理由。``None`` = 干净作答。"""
        if not final.chosen_labels:
            # 「模型说答不了」与「我们没拿到可用回复」是两种故障，理由必须分开
            return EXPLICIT_EMPTY_REASON if outcome.explicit_empty else NO_VALID_SAMPLE_REASON
        if n > 1 and final.majority_ratio < self.cfg.guards.agreement_accept:
            # 只有**真的复算过**之后「一致率」才有意义 —— 单样本的一致率恒为 1。
            return LOW_AGREEMENT_REASON
        if (
            n == 1
            and outcome.self_confidence is not None
            and outcome.self_confidence < self.cfg.guards.confidence_review_min
        ):
            # 题面完整、样本可用，但模型自己说没把握 —— 这种「安静的错答案」
            # 正是必须停下来让人看一眼的那一类。
            return LOW_CONFIDENCE_REASON
        return None

    # -- 采样 -------------------------------------------------------------- #
    def _build_batch(self, q: Question, n: int) -> list[list[OptionPair]]:
        """生成取样批次：**每批都是页面顺序**，一次也不打乱。

        每项是 ``(页面标号, 正文)`` 对 —— 标号与正文成对传递，
        中途没有「按位置重算标号」的环节，也就没有错位的机会。
        自指检测（``allows_shuffle``）仍然存在，但它不改变这里的顺序，
        理由见模块文档。
        """
        count = max(1, int(n))
        pairs: list[OptionPair] = [(opt.label, opt.text) for opt in q.options]
        return [list(pairs) for _ in range(count)]

    async def _run_pass(
        self,
        question: Question,
        batch: list[list[OptionPair]],
        providers: list[LLMProvider],
        *,
        truncated: bool = False,
        skill_text: str,
    ) -> _PassOutcome:
        """按批次逐次取样（每次都有界重发），然后按**内容**投票。

        重发判据来自 :meth:`VotingEngine.resolve`（与投票层同一套判据，
        不存在「求解层以为可用、投票层判无效」的缝）：

        * 样本**可用** → 收工，进下一个批次项；
        * 样本是**显式空作答** → 收工，不重发（模型的结论，重发只会拿到同一句）；
        * 样本**不可用** → 还有重发预算就再发一次，用尽则把这条不可用的样本
          留在明细里，由投票层记它的 ``error_code``。

        每一次尝试都是一个独立的样本，都会进 ``VoteResult.samples``。
        """
        samples: list[LLMResponse] = []
        used = providers[0]
        explicit_seen = False
        self_confidence: float | None = None

        for presented in batch:
            retries_left = SAMPLE_RETRY_MAX
            while True:
                response, provider = await self._complete_with_chain(
                    providers,
                    question,
                    presented,
                    temperature=DEFAULT_TEMPERATURE,
                    truncated=truncated,
                    skill_text=skill_text,
                )
                used = provider
                samples.append(response)
                outcome = self._voting.resolve(response)
                if outcome.explicit_empty:
                    explicit_seen = True
                    break
                if outcome.error is None:
                    reported = (response.parsed or {}).get(CONFIDENCE_KEY)
                    if self_confidence is None and isinstance(reported, float):
                        self_confidence = reported
                    break
                if retries_left <= 0:
                    break
                retries_left -= 1

        vote = self._voting.vote(samples, question.qtype)
        return _PassOutcome(
            vote=vote,
            provider=used,
            # 只有「一个有效样本都没有」时，显式空作答才是**整题的结论**；
            # 复算里出现一两个空作答、其余样本有答案时，照常投票。
            explicit_empty=explicit_seen and not vote.chosen_labels,
            self_confidence=self_confidence,
        )

    async def _complete_with_chain(
        self,
        providers: list[LLMProvider],
        question: Question,
        presented: list[OptionPair],
        *,
        temperature: float,
        truncated: bool,
        skill_text: str,
    ) -> tuple[LLMResponse, LLMProvider]:
        """按降级链请求一次。全部失败也**返回**一条带错误码的响应，不抛异常。

        抛出去会让一次网络抖动毁掉整题；返回错误码则由重发 / 投票处置它，
        剩下的样本照样能收敛。
        """
        system, user = build_messages(
            question, presented, truncated=truncated, skill_text=skill_text
        )
        last_error = "provider_unavailable"
        provider = providers[0]
        for candidate in providers:
            provider = candidate
            request = LLMRequest(
                model=candidate.model_for() or "",
                system=system,
                user=user,
                temperature=temperature,
                max_tokens=SOLVE_MAX_TOKENS,
            )
            try:
                response = await self._complete(candidate, request)
            except ProviderError as exc:
                last_error = exc.code
                continue
            response.parsed = self._annotate(response, presented, question)
            return response, candidate
        return LLMResponse(text="", parsed=None, error_code=last_error), provider

    async def _complete(self, provider: LLMProvider, request: LLMRequest) -> LLMResponse:
        """发一次请求，**整条请求生命周期都在并发闸内**（M4-2）。

        并发闸只包住 ``post`` 是不够的 —— 响应体读取也在占用这条连接。
        没注入闸门时按无限制处理（单测与「不跑模型的 Mock 链路」不需要限流）。

        **这里有一道硬闸：解题请求不许带图**（用户 2026-09-30 的原话是
        「解题组模型严禁拿到非文本输入」）。构造请求的那一处（:meth:`_run_pass`）
        从来不填 ``images``，所以这道闸平时不会响 —— 它的作用是**让将来某次
        「顺手把截图也发过去」在第一时间炸出来**，而不是悄悄多花掉几千 token
        并把推理模型的 content 挤空（真机上踩过：发图之后模型不回答，只烧 token）。
        看页面是视觉组的事（``solve/reader.py``），解题组只看文本。
        """
        if request.images:
            raise RuntimeError(
                "解题请求不许带图：解题组只吃文本（v0.2.0 硬约束，见 solve/solver.py 本方法）"
            )
        if self._gate is None:
            return await provider.complete(request)
        async with self._gate:
            return await provider.complete(request)

    @staticmethod
    def _annotate(
        response: LLMResponse,
        presented: list[OptionPair],
        question: Question,
    ) -> dict[str, Any] | None:
        """把「本次呈现的标号 + 正文」和「页面顺序的正文」注入样本。

        - ``option_labels`` / ``option_texts``：本次呈现的标号与正文。标号是
          **页面标号**，投票层用它**定位**（不能按 ``ord`` 现算位置 ——
          视觉组可能给 ``A, B, D`` 这种非连续标号）；
        - ``original_texts``：页面顺序的正文，投票层按**内容**归并的口径。

        Provider 只需要回答「模型看到的那份标号里，选的是哪几个字母」，
        页面顺序由这里补 —— 这样 Provider 实现不必知道呈现映射。
        """
        if response.parsed is None:
            return None
        annotated = dict(response.parsed)
        annotated[KEY_OPTION_LABELS] = [label for label, _ in presented]
        annotated[KEY_OPTION_TEXTS] = [text for _, text in presented]
        annotated[KEY_ORIGINAL_TEXTS] = question.option_texts
        return annotated

    # -- 输出转换 ---------------------------------------------------------- #
    @staticmethod
    def _to_page_labels(
        question: Question,
        labels: list[str],
    ) -> tuple[list[str], list[str]]:
        """投票层的「原始序号字母」→ **页面真实标号** + 正文。

        投票层工作在「原始序号空间」（第 0 项 = ``A``，见 ``solve/voting.py``），
        所以这里按序号取 ``question.options[index].label`` —— 页面标号因此
        可以是不连续的（``A, B, D``），不会被位置字母顶替。
        """
        page_labels: list[str] = []
        texts: list[str] = []
        for label in labels:
            index = ord(label) - ord("A")
            if index < 0 or index >= len(question.options):
                continue
            option = question.options[index]
            page_labels.append(option.label)
            texts.append(option.text)
        return page_labels, texts

    @staticmethod
    def _solve_path(provider: LLMProvider) -> SolvePath:
        """这次作答走的是哪条路径。**只有一种求解模式**（Mock 是自检用的旁路）。"""
        if provider.name is ProviderName.MOCK:
            return SolvePath.MOCK
        return SolvePath.SINGLE

    def _finish(self, answer: Answer) -> Answer:
        self._emit(
            Event.SOLVE_DONE,
            {
                "qid": answer.qid,
                "chosen_labels": answer.chosen_labels,
                "confidence": round(answer.confidence, 4),
                "solve_path": answer.solve_path.value,
                "review_flag": answer.review_flag,
                "samples": answer.samples,
                "cached": True,
            },
        )
        return answer

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._bus is not None:
            self._bus.emit(event, payload)
