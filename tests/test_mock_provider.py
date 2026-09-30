"""M2-1 MockProvider：读靶场真值、按注入错误率制造错答。

它的存在意义只有一个：**证明投票逻辑本身是对的**。
它直接读地面真值，所以其结果**不得作为 M2 闸门依据**（规划书 P4 坑位第一条）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.enums import ProbeName, ProviderName, QType
from core.models import Option, Question
from core.qid import make_qid, normalize_text
from solve.prompts import build_messages, parse_presented_options
from solve.providers.base import LLMRequest
from solve.providers.mock import MockProvider, QuestionBankSource

BANK_PATH = Path(__file__).resolve().parents[1] / "mock_site" / "static" / "questions.json"

PAYLOAD = {
    "questions": [
        {
            "index": 1,
            "id": "t1",
            "qtype": "single",
            "stem": "TCP 建立连接时，客户端发送的第一个报文段是？",
            "options": ["SYN 报文", "ACK 报文", "FIN 报文", "RST 报文"],
            "answer": [0],
        },
        {
            "index": 2,
            "id": "t2",
            "qtype": "multiple",
            "stem": "下列哪些情况会导致数据库索引失效？",
            "options": ["对索引列使用函数", "隐式类型转换", "使用 LIKE 前缀匹配", "直接比较索引列"],
            "answer": [0, 1, 2],
        },
    ]
}


def make_question(
    *texts: str,
    stem: str = "TCP 建立连接时，客户端发送的第一个报文段是？",
    qtype: QType = QType.SINGLE,
) -> Question:
    return Question(
        qid=make_qid(stem, list(texts)),
        stem=stem,
        stem_hash="stemhash",
        qtype=qtype,
        options=[
            Option(index=i, label=chr(ord("A") + i), text=text, raw=text)
            for i, text in enumerate(texts)
        ],
        source=ProbeName.VISION,
        skill_id="single_choice" if qtype is QType.SINGLE else None,
    )


def request_for(question: Question, presented: list[str]) -> LLMRequest:
    system, user = build_messages(question, presented)
    return LLMRequest(model="", system=system, user=user)


# --------------------------------------------------------------------------- #
# 真值源
# --------------------------------------------------------------------------- #
def test_bank_source_indexes_truth_by_qid() -> None:
    source = QuestionBankSource.from_payload(PAYLOAD)
    qid = make_qid(PAYLOAD["questions"][0]["stem"], PAYLOAD["questions"][0]["options"])

    assert source.lookup(qid) == ["SYN 报文"]
    assert source.lookup("不存在") is None
    assert source.size() == 2


def test_bank_source_qid_is_option_order_invariant() -> None:
    """题库里把选项顺序调换（答案下标跟着重映射），索引依然成立 —— 这正是 ``qid`` 的设计目的（T0-1）。"""
    base = PAYLOAD["questions"][0]
    shuffled = {
        "questions": [
            {
                **base,
                "options": list(reversed(base["options"])),
                "answer": [len(base["options"]) - 1 - index for index in base["answer"]],
            }
        ]
    }
    original = QuestionBankSource.from_payload(PAYLOAD)
    reversed_source = QuestionBankSource.from_payload(shuffled)

    qid = make_qid(base["stem"], base["options"])
    assert original.lookup(qid) == ["SYN 报文"]
    assert reversed_source.lookup(qid) == ["SYN 报文"], "选项换序后 qid 不变，真值也应不变"


def test_bank_source_tolerates_broken_entries() -> None:
    source = QuestionBankSource.from_payload(
        {"questions": [{"stem": "", "options": [], "answer": [0]}, {"stem": "x"}]}
    )
    assert source.size() == 0


def test_default_bank_loads_the_mock_site() -> None:
    if not BANK_PATH.exists():  # pragma: no cover - 靶场缺失时不算失败
        pytest.skip("靶场题库不存在")
    payload = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    source = QuestionBankSource.from_default()
    canvas_count = sum(1 for entry in payload["questions"] if entry.get("canvas_stem"))

    assert source.size() == len(payload["questions"]) + canvas_count


# --------------------------------------------------------------------------- #
# 作答
# --------------------------------------------------------------------------- #
async def test_mock_answers_presented_labels_not_original_labels() -> None:
    """Mock 必须回答**本次呈现**的标号，否则它验证不了「按内容比对」。"""
    provider = MockProvider(QuestionBankSource.from_payload(PAYLOAD))
    question = make_question("SYN 报文", "ACK 报文", "FIN 报文", "RST 报文")
    presented = ["RST 报文", "SYN 报文", "ACK 报文", "FIN 报文"]  # 打乱后

    response = await provider.complete(request_for(question, presented))

    assert response.parsed is not None
    assert response.parsed["chosen_labels"] == ["B"], "SYN 报文 在本次呈现里是 B"
    assert parse_presented_options(provider.calls[0].user)[1][0] == "B"


async def test_mock_multiple_choice_returns_all_correct_labels() -> None:
    provider = MockProvider(QuestionBankSource.from_payload(PAYLOAD))
    question = make_question(
        "对索引列使用函数",
        "隐式类型转换",
        "使用 LIKE 前缀匹配",
        "直接比较索引列",
        stem=PAYLOAD["questions"][1]["stem"],
        qtype=QType.MULTIPLE,
    )
    presented = ["直接比较索引列", "对索引列使用函数", "隐式类型转换", "使用 LIKE 前缀匹配"]

    response = await provider.complete(request_for(question, presented))

    assert response.parsed is not None
    assert response.parsed["chosen_labels"] == ["B", "C", "D"]


async def test_zero_error_rate_is_always_correct() -> None:
    provider = MockProvider(QuestionBankSource.from_payload(PAYLOAD), error_rate=0.0)
    question = make_question("SYN 报文", "ACK 报文", "FIN 报文", "RST 报文")

    for _ in range(20):
        response = await provider.complete(request_for(question, question.option_texts))
        assert response.parsed is not None
        assert response.parsed["chosen_labels"] == ["A"]


async def test_certain_error_rate_is_always_wrong() -> None:
    provider = MockProvider(QuestionBankSource.from_payload(PAYLOAD), error_rate=1.0)
    question = make_question("SYN 报文", "ACK 报文", "FIN 报文", "RST 报文")

    for _ in range(20):
        response = await provider.complete(request_for(question, question.option_texts))
        assert response.parsed is not None
        assert response.parsed["chosen_labels"] != ["A"]


async def test_unknown_question_reports_error_code_instead_of_guessing() -> None:
    provider = MockProvider(QuestionBankSource.from_payload(PAYLOAD))
    question = make_question("甲", "乙", stem="题库里没有这道题")

    response = await provider.complete(request_for(question, question.option_texts))

    assert response.error_code == "mock_unknown_question"
    assert response.parsed is not None
    assert response.parsed["chosen_labels"] == []


def test_error_rate_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        MockProvider(QuestionBankSource.from_payload(PAYLOAD), error_rate=1.5)


async def test_seed_makes_error_injection_reproducible() -> None:
    source = QuestionBankSource.from_payload(PAYLOAD)

    def run(seed: int) -> list[list[str]]:
        provider = MockProvider(source, error_rate=0.5, seed=seed)
        return [provider._maybe_corrupt(["A"], [("A", "SYN 报文"), ("B", "ACK 报文")]) for _ in range(5)]

    assert run(7) == run(7)


async def test_provider_reports_itself_as_mock() -> None:
    assert MockProvider(QuestionBankSource.from_payload(PAYLOAD)).name is ProviderName.MOCK


async def test_aclose_is_a_noop() -> None:
    provider = MockProvider(QuestionBankSource.from_payload(PAYLOAD))
    await provider.aclose()


def test_prompt_never_leaks_ground_truth() -> None:
    """提示词里不许出现地面真值字段名，也不许标出「正确答案」。"""
    question = make_question("SYN 报文", "ACK 报文", "FIN 报文", "RST 报文")
    _, user = build_messages(question, question.option_texts)

    for banned in ("data-answer", "data-answer-texts", "正确答案", "answer:"):
        assert banned not in user


def test_bank_payload_from_disk_matches_question_format() -> None:
    """靶场题库字段没变的话，每题都要能被索引到（含 canvas 题的镜像题干）。"""
    if not BANK_PATH.exists():  # pragma: no cover
        pytest.skip("靶场题库不存在")
    payload = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    questions = payload["questions"]
    source = QuestionBankSource.from_payload(payload)

    canvas_count = sum(1 for entry in questions if entry.get("canvas_stem"))
    assert source.size() == len(questions) + canvas_count
    assert all(normalize_text(entry["stem"]) for entry in questions)


def test_canvas_question_is_indexed_by_the_mirrored_stem() -> None:
    """canvas 题画在画布上的题干与镜像出来的 ``canvas_stem`` 是同一句话 —— 两个 qid 都要能查到真值。

    这是批处理跑第 27 题时暴露出来的：页面把 ``canvas_stem`` 镜像进
    ``data-quiz-stem-text``，于是页面上真实存在的 ``qid`` 是按镜像算的那个。
    """
    entry = {
        "stem": "下图给出了一棵二叉树的形态，它的中序遍历结果是？",
        "canvas_stem": "二叉树：根 5，左 3，右 8。中序遍历结果？",
        "options": ["1 3 4 5 8", "5 3 1 4 8"],
        "answer": [0],
    }
    source = QuestionBankSource.from_payload({"questions": [entry]})

    authored = make_qid(entry["stem"], entry["options"])
    mirrored = make_qid(entry["canvas_stem"], entry["options"])

    assert source.lookup(authored) == ["1 3 4 5 8"]
    assert source.lookup(mirrored) == ["1 3 4 5 8"], "镜像题干也要能查到"
    assert source.size() == 2, "两条索引指向同一个答案，不构成歧义"
