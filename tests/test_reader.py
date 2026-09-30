"""视觉「读题」契约单测（P11 增量）。

分两层：

- **纯解析层**（大部分用例）：模型输出 → ``ReadResult``。不起浏览器、不发网络，
  所以永远不会 skip。判分只看「遇到脏输出会不会把脏数据放进下游」。
- **一次端到端**：走本地假 OpenAI 端点，验 ``read_question`` 真的把链路串起来了。

这一层的存在理由：读题是**执行层的上游**。执行层会照着读出来的坐标去点，
所以「宁可少一个框，也不能放进一个错框」—— 越界、倒挂、缺框都必须被挡在这里。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import SecretStr

from core.enums import ProviderName, QType
from core.model_registry import CredentialStore, ModelProfile
from solve.providers.base import LLMProvider, LLMRequest, LLMResponse, RateLimitError
from solve.providers.factory import build_provider_chain
from solve.reader import (
    READ_MAX_TOKENS,
    READ_SYSTEM_PROMPT,
    _extract_json,
    gate_read_result,
    parse_page_view,
    parse_read_batch,
    parse_read_payload,
    read_question,
    skill_for_reported_type,
    stem_fingerprint,
    to_question,
)

#: 一份合法的最小读题结果（2026-09-30 起的固定格式：``page`` 观测 + ``questions``）。
GOOD = {
    "page": {
        "progress": "2/10",
        "total": 10,
        "current": 2,
        "next_control": {"box": [0.82, 0.93, 0.14, 0.05], "label": "下一题"},
        "submit": {"box": [0.89, 0.01, 0.11, 0.03], "scope": "question"},
        "completed": "not_done",
        "scrolling": True,
        "reason": "还在做",
    },
    "questions": [
        {
            "index": 1,
            "num_text": "2.",
            "stem": "设 f(x) 为随机变量的概率密度，则其必满足的性质是",
            "qtype": "single",
            "skill_id": "single_choice",
            "options": [
                {"label": "A", "text": "单调不减函数", "box": [0.05, 0.36, 0.56, 0.03]},
                {"label": "B", "text": "连续函数", "box": [0.05, 0.41, 0.56, 0.03]},
            ],
        }
    ],
    "more_below": False,
    "note": "",
}


def _payload(**overrides: object) -> str:
    """改**题面**字段的辅助（``GOOD`` 里的第一道题）。"""
    merged = dict(GOOD["questions"][0])
    merged.update(overrides)
    body = dict(GOOD)
    body["questions"] = [merged]
    return json.dumps(body, ensure_ascii=False)


def _raw_payload(**overrides: object) -> str:
    """改**顶层**字段的辅助（``page`` / ``more_below`` / ``note``）。"""
    merged = dict(GOOD)
    merged.update(overrides)
    return json.dumps(merged, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 解析：干净输入
# --------------------------------------------------------------------------- #
def test_parses_clean_payload() -> None:
    result = parse_read_payload(_payload())
    assert result is not None
    assert result.stem.startswith("设 f(x)")
    assert [o.label for o in result.options] == ["A", "B"]
    assert result.num_text == "2."
    assert result.skill_id == "single_choice"
    assert result.skill_error is None
    # 提交框 / 下一题框**不再挂在题上**：它们是页面级观测（``page``），
    # 归编排层裁决用。见下面的 ``parse_page_view`` 那一节。
    assert not hasattr(result, "submit_box")


def test_tolerates_markdown_fence_and_chatter() -> None:
    """模型经常无视「不要 markdown 代码块」，还爱在前后加一句话。

    这不是宽容，是**必需**：真实模型的输出就是这样。原样拒绝等于把可用结果丢掉。
    """
    wrapped = f"好的，以下是结果：\n```json\n{_payload()}\n```\n希望有帮助！"
    assert parse_read_payload(wrapped) is not None


def test_tolerates_leading_and_trailing_prose() -> None:
    assert parse_read_payload(f"我看到了题目。{_payload()} 以上。") is not None


# --------------------------------------------------------------------------- #
# 解析：脏输入必须被挡住（执行层会照着框去点）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_box",
    [
        [1.5, 0.0, 0.2, 0.1],  # x 越界
        [0.0, -0.1, 0.2, 0.1],  # y 为负
        [0.0, 0.0, 0.0, 0.1],  # 宽为 0
        [0.0, 0.0, 0.2, -0.1],  # 高为负
        [0.9, 0.0, 0.3, 0.1],  # 右边越界
        [0.0, 0.0, 0.1],  # 少一个分量
        ["a", "b", "c", "d"],  # 不是数字
        None,
    ],
)
def test_invalid_box_is_dropped_not_repaired(bad_box: object) -> None:
    """非法框**丢弃**，绝不修补。

    猜一个「大概的位置」出来，就是把一次误点交给执行层 ——
    宁可少一个选项（执行层会说「这个选项点不到」），也不能点错地方。
    """
    payload = _payload(
        options=[
            {"label": "A", "text": "好选项", "box": [0.05, 0.36, 0.56, 0.03]},
            {"label": "B", "text": "坏框选项", "box": bad_box},
        ]
    )
    result = parse_read_payload(payload)
    assert result is not None
    assert [o.label for o in result.options] == ["A"], "坏框的选项必须被丢掉"


def test_all_boxes_broken_yields_no_result() -> None:
    payload = _payload(options=[{"label": "A", "text": "x", "box": [2.0, 0, 1, 1]}])
    assert parse_read_payload(payload) is None


@pytest.mark.parametrize("bad", ["", "我不知道", "{}", '{"stem": ""}', "null", "[1,2]"])
def test_unusable_output_returns_none(bad: str) -> None:
    assert parse_read_payload(bad) is None


def test_missing_label_is_filled_by_position() -> None:
    payload = _payload(options=[{"text": "没给标号", "box": [0.05, 0.36, 0.9, 0.03]}])
    result = parse_read_payload(payload)
    assert result is not None
    assert result.options[0].label == "A"


def test_unsupported_qtype_is_recorded_not_coerced() -> None:
    """填空 / 简答保留原始题型，并明确为无技能匹配。"""
    result = parse_read_payload(_payload(qtype="text", skill_id=None))
    assert result is not None
    assert result.unsupported_qtype == "text"
    assert result.skill_error == "unsupported_question_type"


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("single", "single_choice"), ("single_choice", "single_choice"), ("judge", "true_false")],
)
def test_skill_mapping_helper_uses_local_registry(reported: str, expected: str) -> None:
    skill_id, error = skill_for_reported_type(reported)
    assert skill_id == expected
    assert error is None


def test_skill_mapping_helper_rejects_unsupported_types() -> None:
    assert skill_for_reported_type("multiple") == (None, "no_matching_skill")
    assert skill_for_reported_type(None) == (None, "unsupported_question_type")


def test_true_false_type_requires_matching_registered_skill() -> None:
    result = parse_read_payload(_payload(qtype="true_false", skill_id="true_false"))
    assert result is not None
    assert result.qtype is QType.TRUE_FALSE
    assert result.skill_id == "true_false"
    assert result.reported_skill_id == "true_false"
    assert result.skill_error is None


def test_supported_qtype_infers_missing_skill_id_from_registry() -> None:
    result = parse_read_payload(_payload(qtype="single", skill_id=None))
    assert result is not None
    assert result.qtype is QType.SINGLE
    assert result.skill_id == "single_choice"
    assert result.reported_skill_id is None
    assert result.skill_error is None


def test_skill_id_enum_variant_is_normalized_from_qtype() -> None:
    result = parse_read_payload(_payload(qtype="QuestionType.SINGLE", skill_id=None))
    assert result is not None
    assert result.skill_id == "single_choice"
    assert result.skill_error is None


def test_missing_or_unknown_qtype_never_infers_single_skill() -> None:
    missing = parse_read_payload(_payload(qtype="unknown", skill_id=None))
    assert missing is not None
    assert missing.skill_id is None
    assert missing.skill_error == "unsupported_question_type"


def test_skill_type_mismatch_is_rejected() -> None:
    result = parse_read_payload(_payload(qtype="true_false", skill_id="single_choice"))
    assert result is not None
    assert result.skill_error == "skill_type_mismatch"


def test_multiple_choice_is_recognized_but_has_no_skill() -> None:
    result = parse_read_payload(_payload(qtype="multiple", skill_id=None))
    assert result is not None
    assert result.qtype is QType.MULTIPLE
    assert result.skill_id is None
    assert result.skill_error == "no_matching_skill"


# --------------------------------------------------------------------------- #
# 转 Question：接进既有求解链路
# --------------------------------------------------------------------------- #
def test_to_question_keeps_qid_answer_free() -> None:
    """qid 只由题干决定 —— 跨通道一致，缓存与「这题读了两遍」才成立。"""
    result = parse_read_payload(_payload())
    assert result is not None
    first = to_question(result)
    second = to_question(result)

    assert first.qid == second.qid
    assert first.stem_hash == stem_fingerprint(result.stem)
    assert first.source.value == "vision"
    assert "vision:read_from_image" in first.channel_trace
    # 选项正文不许出现在 qid 里（qid 不含答案，T0-1）
    assert result.options[0].text not in first.qid


def test_to_question_preserves_option_order() -> None:
    result = parse_read_payload(_payload())
    assert result is not None
    question = to_question(result)
    assert [o.label for o in question.options] == ["A", "B"]
    assert [o.index for o in question.options] == [0, 1]


def test_read_prompt_forbids_answering() -> None:
    """提示词必须明确「只抄录、不答题」。

    模型一旦顺手把答案写进题干，后面的求解就变成抄自己，
    而且会污染 qid（qid 由题干哈希而来）。

    2026-09-28 起 ``READ_SYSTEM_PROMPT`` 是从 ``prompts/00-共享契约.md`` +
    ``prompts/10-视觉组.md`` 加载的（拼装规则见 ``solve/prompt_files.py``），
    所以这里按**那两份文件里的实际措辞**断言：既不写死旧句子，也不放宽成
    永远为真的空断言 —— 断言的必须是**真的在讲这件事**的那句话。
    """
    assert "归一化" in READ_SYSTEM_PROMPT, "坐标归一化口径必须在读题提示词里"
    assert "不判断哪个选项对" in READ_SYSTEM_PROMPT, "「不作答」这条必须写明"
    assert "不给答案" in READ_SYSTEM_PROMPT
    assert "如实抄录" in READ_SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# 门禁：题面残缺 / 模型拿不准 → **不许下传**（2026-09-28 加）
# --------------------------------------------------------------------------- #
def test_clean_result_passes_the_gate() -> None:
    result = parse_read_payload(_payload())
    assert result is not None
    assert gate_read_result(result) == (True, None)


def test_clipped_blocks_the_gate() -> None:
    """模型说某处被画面切掉/没读出 → 拦。

    这是本环节唯一能发现"抄漏了"的信号 —— 抄漏的题面看起来完全正常。
    """
    result = parse_read_payload(_payload(clipped=["option:D"]))
    assert result is not None
    assert gate_read_result(result) == (False, "vision_incomplete")


def test_uncertain_blocks_the_gate() -> None:
    """模型自己点名"拿不准" → 拦，并给**另一个**原因码。

    两个原因码必须分开：``vision_incomplete``（题面确实缺）与
    ``vision_uncertain``（题面全但没把握）对应的下一步动作不同 ——
    前者要重新截一张更全的图，后者要放大复核那个字形。
    """
    result = parse_read_payload(_payload(uncertain=["stem.formula.1"]))
    assert result is not None
    assert gate_read_result(result) == (False, "vision_uncertain")


def test_gate_prefers_incomplete_over_uncertain() -> None:
    """两个都非空 → 报 ``vision_incomplete``：残缺比"拿不准"更硬。"""
    result = parse_read_payload(_payload(clipped=["stem"], uncertain=["option:A"]))
    assert result is not None
    assert gate_read_result(result) == (False, "vision_incomplete")


def test_more_below_alone_does_not_block() -> None:
    """``more_below`` **不进判据**。

    它只表示"下方**可能**还有内容"，而那是"多题同页"的**正常形态** ——
    单凭它拦会让每一页长题干都停下来。它进留痕，不进判据。
    """
    result = parse_read_payload(_raw_payload(more_below=True))
    assert result is not None
    batch = parse_read_batch(_raw_payload(more_below=True))
    assert batch is not None and batch.more_below is True
    assert gate_read_result(result) == (True, None)


def test_no_submit_box_alone_does_not_block() -> None:
    """这一屏看不到提交按钮**不归门禁管**：选项本身是读对了的。

    2026-09-30 起它更不该拦 —— 提交时机由开局那份**方案**定（整卷页面本来
    就不需要每一屏都有提交框），门禁拦它只会把正常页面整页丢给人工。
    """
    body = dict(GOOD)
    body["page"] = {**GOOD["page"], "submit": {"scope": "question"}}
    result = parse_read_payload(json.dumps(body, ensure_ascii=False))
    assert result is not None
    assert gate_read_result(result) == (True, None)


# --- 质量字段的解析纪律（放松会静默失效，收紧会误拦）------------------------- #
def test_quality_lists_tolerate_string_form() -> None:
    """模型常把数组写成逗号串 —— 两种都得认，否则门禁会**静默失效**。"""
    result = parse_read_payload(_payload(clipped="stem, option:D"))
    assert result is not None
    assert result.clipped == ["stem", "option:D"]


def test_quality_lists_are_deduped_and_capped() -> None:
    result = parse_read_payload(_payload(uncertain=["stem", "stem", "", "x" * 200]))
    assert result is not None
    assert result.uncertain[0] == "stem"
    assert len(result.uncertain) == 2, "重复项要去掉"
    assert len(result.uncertain[1]) == 60, "超长条目要截断（留痕别被散文撑爆）"


@pytest.mark.parametrize("junk", [123, {"a": 1}, True, None])
def test_garbage_quality_values_do_not_block(junk: object) -> None:
    """**不是清单形态**的值当空处理 —— 宁可漏报。

    漏报的后果是"行为与加门禁之前一致"（照旧解题）；
    误报的后果是每一题都停下来问人，门禁会被用户直接关掉，等于没有。
    """
    result = parse_read_payload(_payload(clipped=junk))
    assert result is not None
    assert result.clipped == []
    assert gate_read_result(result) == (True, None)


def test_non_string_quality_items_still_block() -> None:
    """非字符串条目要**照拦** —— 只问清单空不空，不替模型论证形式对不对。

    ``clipped`` 的语义是「模型在说：这里有问题」。它的**形式**畸形（例如写成
    ``[1, 2]`` 而不是 ``["stem"]``）不该让这个信号消失：能自己承认有问题的模型
    给出的信号，正是最不该被忽略的那种。所以这里只做 ``str()`` 归一化，
    不做"看起来不像定位符就丢掉"的过滤。
    """
    result = parse_read_payload(_payload(clipped=[1, 2]))
    assert result is not None
    assert result.clipped == ["1", "2"]
    assert gate_read_result(result) == (False, "vision_incomplete")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("false", False), ("true", True), (0, False), (1, True), (None, False)],
)
def test_more_below_accepts_string_booleans(raw: object, expected: bool) -> None:
    """``bool("false") is True`` 这个坑一旦踩到，会把「还有内容」判成「没有了」。"""
    batch = parse_read_batch(_raw_payload(more_below=raw))
    assert batch is not None
    assert batch.more_below is expected


# --------------------------------------------------------------------------- #
# 端到端：真的把链路串起来（本地假端点，零外网）
# --------------------------------------------------------------------------- #
async def test_read_question_end_to_end(provider: Callable[[str], str]) -> None:
    providers = _chain([("p1", provider("read"))])
    result, error = await read_question(b"\x89PNG\r\n\x1a\nfake", providers=providers)

    assert error is None
    assert result is not None
    assert len(result.options) == 4


async def test_read_question_reports_error_when_model_cannot_read(
    provider: Callable[[str], str],
) -> None:
    """模型答非所问 → ``(None, 错误码)``，不抛异常、也不假装成功。"""
    providers = _chain([("p2", provider("ok"))])
    result, error = await read_question(b"\x89PNG\r\n\x1a\nfake", providers=providers)

    assert result is None
    assert error == "read_parse_failed"


async def test_read_question_falls_back_along_the_chain(
    provider: Callable[[str], str],
) -> None:
    """降级链：第一个不支持图像，第二个能读 —— 必须走到第二个。

    与求解层同款策略：一次能力不足不该毁掉整道题。
    """
    providers = _chain([("p3", provider("novision")), ("p4", provider("read"))])
    result, error = await read_question(b"\x89PNG\r\n\x1a\nfake", providers=providers)

    assert error is None
    assert result is not None


async def test_read_questions_retries_transient_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from solve import reader

    class FlakyVision(LLMProvider):
        name = ProviderName.OPENAI_COMPAT

        def __init__(self) -> None:
            self.calls = 0

        def model_for(self) -> str:
            return "vision-test"

        async def complete(self, req: LLMRequest) -> LLMResponse:
            del req
            self.calls += 1
            if self.calls == 1:
                raise RateLimitError("temporary 429")
            return LLMResponse(text=json.dumps(GOOD, ensure_ascii=False))

        async def aclose(self) -> None:
            return None

    provider = FlakyVision()
    delays: list[float] = []

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(reader.asyncio, "sleep", no_wait)
    batch, error = await reader.read_questions(b"fake-png", providers=[provider])

    assert error is None
    assert batch is not None and batch.questions
    assert provider.calls == 2
    assert delays == [reader.READ_RATE_LIMIT_RETRY_DELAY_S]


async def test_end_screen_batch_has_no_questions_but_carries_the_observation(
    provider: Callable[[str], str],
) -> None:
    """收尾那一屏：**一道题都没有，但观测说整卷做完了** → 仍然要给出一批。

    这是收尾闸门的唯一输入。解析层若因为「没有题」就返回 ``None``，
    程序就再也无从知道「做完了没有」，只能停在 ``advance_failed``。
    """
    from solve.reader import read_questions

    providers = _chain([("e1", provider("end_screen"))])
    batch, error = await read_questions(b"\x89PNG\r\n\x1a\nfake", providers=providers)

    assert error is None
    assert batch is not None
    assert batch.questions == []
    assert batch.page is not None
    assert batch.page.completed.value == "all_done"
    assert batch.page.submit is not None
    assert batch.page.submit.box is not None


def _chain(entries: list[tuple[str, str]]) -> list[object]:
    """按降级链构造假 provider。

    ``build_provider_chain`` **会跳过取不到密钥的配置**，所以这里必须把密钥
    放进同一个 CredentialStore —— 否则链路是空的，测试会以「provider_unavailable」
    这种看不出原因的方式失败（踩过）。
    """
    store = CredentialStore()
    profiles = []
    for profile_id, base_url in entries:
        profile = _profile(profile_id, base_url, model="fake-model")
        store.put(profile.api_key_ref, SecretStr("sk-fake"))
        profiles.append(profile)
    return list(build_provider_chain(profiles, credentials=store))


def _profile(profile_id: str, base_url: str, *, model: str) -> ModelProfile:
    """一套配置**一个**模型（2026-09-28 起不再分 tier1 / tier2）。"""
    return ModelProfile(
        profile_id=profile_id,
        name=profile_id,
        base_url=base_url,
        model=model,
        # 假端点不校验密钥，但 profile 需要它来定位凭据条目
        api_key_ref=f"autolearn/{profile_id}",
    )


# --------------------------------------------------------------------------- #
# ``page`` 观测块（2026-09-30）：它是「程序那一套判断逻辑」的**唯一输入**
#
# 解析纪律只有一条：**只认结构，不认散文；缺字段就留空**。
# 解析层任何一处「顺手填个默认值」，都会让开局裁决在错误的观测上做决定 ——
# 而那个决定的后果是「点错地方」或「提前收工」。
# --------------------------------------------------------------------------- #
def test_page_view_parses_the_full_observation() -> None:
    page = parse_page_view(GOOD)
    assert page is not None
    assert page.progress == "2/10"
    assert page.total == 10
    assert page.current == 2
    assert page.next_control is not None
    assert page.next_control.box == (0.82, 0.93, 0.14, 0.05)
    assert page.next_control.label == "下一题"
    assert page.submit is not None
    assert page.submit.box == (0.89, 0.01, 0.11, 0.03)
    assert page.submit.scope.value == "question"
    assert page.completed.value == "not_done"
    assert page.scrolling is True


def test_page_view_absent_is_none_not_empty() -> None:
    """没有 ``page`` 块 = **这次没拿到观测**，不是「观测到一片空白」。

    两者在下游完全不是一回事：``None`` 会让裁决退回后备顺序，
    空观测则会被当成「画面上什么都没有」而误判成滑动翻页。
    """
    assert parse_page_view({"questions": []}) is None
    assert parse_page_view({"page": "有题"}) is None
    assert parse_page_view({"page": []}) is None


def test_page_view_keeps_missing_fields_missing() -> None:
    """字段缺失一律留空，**绝不猜**。"""
    page = parse_page_view({"page": {}})
    assert page is not None
    assert page.total is None and page.current is None
    assert page.next_control is None and page.card is None and page.submit is None
    assert page.scrolling is None
    # 看不出「做完了没有」必须是 UNKNOWN，hasattr 不能是 ALL_DONE：
    # 把它当成「做完了」会直接收工，后面所有题都不再作答。
    assert page.completed.value == "unknown"


def test_page_view_drops_invalid_control_box() -> None:
    """越界 / 倒挂的控件框 → 当成「没有可用控件」，**绝不用它去点**。"""
    page = parse_page_view({"page": {"next_control": {"box": [1.4, 0, 0.3, 0.1]}}})
    assert page is not None
    assert page.next_control is None


def test_page_view_keeps_control_without_label() -> None:
    """没有文字标签的控件仍然可用（图标按钮就是这样），只是留痕里没有名字。"""
    page = parse_page_view({"page": {"next_control": {"box": [0.8, 0.9, 0.1, 0.05]}}})
    assert page is not None
    assert page.next_control is not None
    assert page.next_control.label is None


def test_page_view_parses_the_card_grid() -> None:
    page = parse_page_view(
        {
            "page": {
                "card": {
                    "box": [0.0, 0.1, 0.2, 0.6],
                    "cols": 5,
                    "rows": 9,
                    "current_box": [0.01, 0.31, 0.03, 0.05],
                    "next_box": [0.05, 0.31, 0.03, 0.05],
                }
            }
        }
    )
    assert page is not None
    assert page.card is not None
    assert (page.card.cols, page.card.rows) == (5, 9)
    assert page.card.current_box is not None
    assert page.card.next_box is not None


@pytest.mark.parametrize("bad", [0, -3, "五", None, True])
def test_page_view_card_counts_must_be_positive_ints(bad: object) -> None:
    """``cols`` / ``rows`` 不是正整数 = **看不出网格**（0 格网格算不出来落点）。"""
    page = parse_page_view(
        {"page": {"card": {"box": [0.0, 0.1, 0.2, 0.6], "cols": bad, "rows": 9}}}
    )
    assert page is not None
    assert page.card is not None
    assert page.card.cols == 0, "看不出列数就是 0，由裁决层据此判定「算不出来」"


def test_page_view_without_card_box_is_no_card() -> None:
    page = parse_page_view({"page": {"card": {"cols": 5, "rows": 9}}})
    assert page is not None
    assert page.card is None


def test_page_view_current_greater_than_total_drops_current() -> None:
    """两个数互相矛盾（当前题号 > 总题数）→ 信总数、丢掉当前题号。

    保留一个自相矛盾的题号，会让答题卡锚点算歪，进而整条推进都点偏。
    """
    page = parse_page_view({"page": {"total": 10, "current": 44}})
    assert page is not None
    assert page.total == 10
    assert page.current is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("all_done", "all_done"),
        ("已完成", "all_done"),
        ("finished", "all_done"),
        (True, "all_done"),
        ("not_done", "not_done"),
        (False, "not_done"),
        ("可能吧", "unknown"),
        (None, "unknown"),
        (123, "unknown"),
    ],
)
def test_page_view_completion_only_trusts_explicit_wording(raw: object, expected: str) -> None:
    """**只有明确说「全部做完」才算做完**；其余一律 UNKNOWN。

    方向是刻意不对称的：把「不知道」当 ``ALL_DONE`` 会直接收工，
    把「不知道」当 ``NOT_DONE`` 只会多推一步（推不动又会停下）。
    """
    page = parse_page_view({"page": {"completed": raw}})
    assert page is not None
    assert page.completed.value == expected


def test_page_view_scrolling_only_accepts_real_booleans() -> None:
    """``scrolling`` 只认真布尔：字符串 ``"false"`` 是**没有信息**，不是「不能滚」。"""
    assert parse_page_view({"page": {"scrolling": False}}).scrolling is False  # type: ignore[union-attr]
    assert parse_page_view({"page": {"scrolling": "false"}}).scrolling is None  # type: ignore[union-attr]


def test_reader_budget_is_large_enough_for_reasoning_and_json() -> None:
    # DeepSeek-style calls count reasoning tokens inside the completion budget;
    # keep the existing 16k headroom rather than reducing it based on concise output.
    assert READ_MAX_TOKENS >= 16_384


def test_extract_json_repairs_multiline_html_and_css_strings() -> None:
    malformed = '''{
      "questions": [{
        "stem": "HTML: <div class="hidden-box">示例</div>
.hidden-box {
 display: none;
}",
        "options": []
      }]
    }'''

    payload = _extract_json(malformed)

    assert payload is not None
    assert 'class="hidden-box"' in payload["questions"][0]["stem"]
    assert "\\n.hidden-box {" not in payload["questions"][0]["stem"]
    assert "\n.hidden-box {" in payload["questions"][0]["stem"]
    batch = parse_read_batch(malformed)
    assert batch is not None and batch.questions[0].stem == payload["questions"][0]["stem"]


def test_batch_without_page_still_parses_questions() -> None:
    """没有 ``page`` 块（模型只回了题）→ 题目照样收下。

    观测缺失只影响「怎么推进 / 什么时候提交」，不该把读到的题一并丢掉。
    """
    batch = parse_read_batch(json.dumps({"questions": GOOD["questions"]}, ensure_ascii=False))
    assert batch is not None
    assert batch.page is None
    assert len(batch.questions) == 1


def test_batch_without_questions_and_without_page_is_a_failure() -> None:
    """既没有题、也没有观测 = 这次读图没成。"""
    assert parse_read_batch('{"questions": []}') is None


def test_batch_keeps_the_end_screen_shape() -> None:
    """收尾屏：``questions: []`` + ``page.completed: all_done`` → 必须给出观测。"""
    batch = parse_read_batch(
        json.dumps({"page": {"completed": "all_done"}, "questions": []}, ensure_ascii=False)
    )
    assert batch is not None
    assert batch.questions == []
    assert batch.page is not None


def test_read_prompt_names_the_observation_block() -> None:
    """提示词必须写明「除了题目，还要报页面观测」。

    这一块是**程序侧判断逻辑的唯一输入**：模型不报它，程序就只能凭猜推进。
    （提示词住在 ``prompts/10-视觉组.md``，按文件里的实际措辞断言。）
    """
    assert '"page"' in READ_SYSTEM_PROMPT, "必须给出 page 观测块"
    assert "next_control" in READ_SYSTEM_PROMPT, "必须给出推进控件字段"
    assert "答题卡" in READ_SYSTEM_PROMPT, "必须给出答题卡字段"
    assert "completed" in READ_SYSTEM_PROMPT, "必须给出「做完了没有」字段"
    assert "报告「有什么」" in READ_SYSTEM_PROMPT or "不要报告「该怎么办」" in READ_SYSTEM_PROMPT, (
        "必须写明视觉组只做观测、不做裁决"
    )
