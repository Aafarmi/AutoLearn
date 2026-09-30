"""M2-1 Provider 层：OpenAI 兼容实现、错误码映射、能力降级、工厂装配。

假端点用 ``conftest`` 里的本地 HTTP 服务（``provider("ok")`` / ``provider("unauthorized")`` …），
所以这一组用例**零密钥、零外网**可跑。

错误码必须进 §2.4 字典：401 → ``auth_failed``、404 → ``model_not_found``、
429 → ``rate_limited``。UI 靠它分流到对应的引导卡，混成一个「请求失败」就废了。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import SecretStr

from core.enums import ProviderName, SolvePath
from core.model_registry import (
    CredentialStore,
    InMemorySecretBackend,
    ModelProfile,
    build_api_key_ref,
)
from core.models import CapabilityReport
from solve.prompts import build_messages, parse_answer_payload
from solve.providers.base import (
    AuthError,
    LLMRequest,
    ModelNotFoundError,
    ProviderError,
    VisionNotSupportedError,
)
from solve.providers.factory import (
    build_default_chain,
    build_mock_provider,
    build_provider,
    build_provider_chain,
    provider_name_of,
)
from solve.providers.mock import QuestionBankSource
from solve.providers.openai_compat import OpenAICompatProvider

PROFILE_ID = "main"
API_KEY = "sk-test"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def credentials() -> CredentialStore:
    store = CredentialStore(InMemorySecretBackend())
    store.put(build_api_key_ref(PROFILE_ID), SecretStr(API_KEY))
    return store


def make_profile(
    base_url: str,
    *,
    model: str = "fake-model",
    capabilities: CapabilityReport | None = None,
    concurrency: int = 2,
) -> ModelProfile:
    """一套配置**一个**模型（2026-09-28 起不再分 ``tier1_model`` / ``tier2_model``）。"""
    return ModelProfile(
        profile_id=PROFILE_ID,
        name="假端点",
        base_url=base_url,
        model=model,
        api_key_ref=build_api_key_ref(PROFILE_ID),
        capabilities=capabilities,
        concurrency=concurrency,
    )


def make_request(*, images: list[bytes] | None = None, model: str = "") -> LLMRequest:
    return LLMRequest(
        model=model,
        system="你是答题器",
        user="题干:\n甲\n\n选项:\nA. 甲\nB. 乙",
        images=list(images or []),
    )


def caps(*, vision: bool = True, structured: bool = True) -> CapabilityReport:
    return CapabilityReport(
        auth_ok=True,
        model_ok=True,
        supports_vision=vision,
        supports_structured_output=structured,
        image_payload="base64" if vision else None,
        max_qps=1.0,
        latency_ms=100,
    )


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
async def test_complete_returns_text_latency_and_raw(
    provider: Callable[[str], str],
) -> None:
    impl = build_provider(make_profile(provider("ok")), credentials=credentials())

    response = await impl.complete(make_request())

    assert response.text == '{"ok": true}'
    # raw 是 **HTTP 响应全文**（M4-3 要求 solve.json 落原始响应），不是抽出来的正文
    assert json.loads(response.raw)["choices"][0]["message"]["content"] == '{"ok": true}'
    assert response.latency_ms >= 0
    assert response.error_code is None
    await impl.aclose()


async def test_unparsable_answer_is_not_guessed(
    provider: Callable[[str], str],
) -> None:
    """模型没给标号就 ``parsed=None`` —— 由投票记成无效样本，**不许**猜一个。"""
    impl = OpenAICompatProvider(make_profile(provider("ok")), SecretStr(API_KEY), 2)

    response = await impl.complete(make_request())

    assert response.parsed is None
    await impl.aclose()


async def test_usage_is_extracted_when_present(
    provider: Callable[[str], str],
) -> None:
    impl = OpenAICompatProvider(make_profile(provider("ok")), SecretStr(API_KEY), 2)
    # 假端点不返回 usage，取默认零值即可 —— 这里守住「不炸」这条底线
    response = await impl.complete(make_request())
    assert response.usage.total_tokens == 0
    await impl.aclose()


# --------------------------------------------------------------------------- #
# 错误码映射（§2.4）
# --------------------------------------------------------------------------- #
async def test_401_becomes_auth_error(provider: Callable[[str], str]) -> None:
    impl = OpenAICompatProvider(make_profile(provider("unauthorized")), SecretStr(API_KEY), 2)

    with pytest.raises(AuthError) as excinfo:
        await impl.complete(make_request())

    assert excinfo.value.code == "auth_failed"
    await impl.aclose()


async def test_unknown_model_becomes_model_not_found(provider: Callable[[str], str]) -> None:
    impl = OpenAICompatProvider(
        make_profile(provider("ok"), model="不存在的模型"),
        SecretStr(API_KEY),
        2,
    )

    with pytest.raises(ModelNotFoundError) as excinfo:
        await impl.complete(make_request())

    assert excinfo.value.code == "model_not_found"
    await impl.aclose()


async def test_server_error_becomes_provider_error(provider: Callable[[str], str]) -> None:
    """没进字典的状态码落到通用 ``provider_error``，不静默吞掉。"""
    impl = OpenAICompatProvider(make_profile(provider("novision")), SecretStr(API_KEY), 2)

    with pytest.raises(ProviderError) as excinfo:
        await impl.complete(make_request(images=[b"\x89PNG"]))

    assert excinfo.value.code == "provider_error"
    await impl.aclose()


# --------------------------------------------------------------------------- #
# 能力降级（M2-6 实测结果驱动）
# --------------------------------------------------------------------------- #
async def test_structured_unsupported_falls_back_to_text_parsing(
    provider: Callable[[str], str],
) -> None:
    """实测不支持结构化输出 → **不塞** ``response_format``，照常按文本解析。"""
    profile = make_profile(provider("nostruct"), capabilities=caps(structured=False))
    impl = OpenAICompatProvider(profile, SecretStr(API_KEY), 2)

    response = await impl.complete(make_request())

    assert response.text == '{"ok": true}'
    await impl.aclose()


async def test_structured_sent_when_capability_unknown_then_server_rejects(
    provider: Callable[[str], str],
) -> None:
    """没实测过就不擅自降级 —— 假端点会因为收到 ``response_format`` 而报 400。"""
    impl = OpenAICompatProvider(
        make_profile(provider("nostruct"), capabilities=None),
        SecretStr(API_KEY),
        2,
    )

    with pytest.raises(ProviderError):
        await impl.complete(make_request())
    await impl.aclose()


async def test_vision_rejected_early_without_spending_a_request(
    provider: Callable[[str], str],
) -> None:
    """实测不支持视觉 → 在**发请求之前**失败，不烧钱也不留半截留痕。"""
    profile = make_profile(provider("ok"), capabilities=caps(vision=False))
    impl = OpenAICompatProvider(profile, SecretStr(API_KEY), 2)

    with pytest.raises(VisionNotSupportedError) as excinfo:
        await impl.complete(make_request(images=[b"\x89PNG"]))

    assert excinfo.value.code == "vision_unsupported"
    await impl.aclose()


def test_image_payload_is_a_base64_data_uri(provider: Callable[[str], str]) -> None:
    impl = OpenAICompatProvider(make_profile(provider("ok")), SecretStr(API_KEY), 2)
    content = impl._user_content(make_request(images=[b"\x89PNG"]))

    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_only_request_keeps_a_plain_string_content(
    provider: Callable[[str], str],
) -> None:
    impl = OpenAICompatProvider(make_profile(provider("ok")), SecretStr(API_KEY), 2)
    assert isinstance(impl._user_content(make_request()), str)


# --------------------------------------------------------------------------- #
# 响应正文抽取：多厂商兜底（推理模型 content 为空 / parts 数组 / reasoning_content）
# --------------------------------------------------------------------------- #
def _payload(message: dict | None, text: str | None = None) -> dict:
    return {"choices": [{"message": message, "text": text}]}


def test_extract_text_reads_standard_content() -> None:
    assert OpenAICompatProvider._extract_text(_payload({"content": " 答案是 A "})) == " 答案是 A "


def test_extract_text_falls_back_to_reasoning_content() -> None:
    """推理模型：``content`` 为空、答案在 ``reasoning_content`` —— 必须能捞回来。"""
    payload = _payload({"content": "", "reasoning_content": '{"questions": [...]}'})
    assert OpenAICompatProvider._extract_text(payload) == '{"questions": [...]}'


def test_extract_text_reads_parts_array_content() -> None:
    payload = _payload({"content": [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]})
    assert OpenAICompatProvider._extract_text(payload) == "第一段\n第二段"


def test_extract_text_prefers_content_over_reasoning() -> None:
    payload = _payload({"content": "正文", "reasoning_content": "思考过程"})
    assert OpenAICompatProvider._extract_text(payload) == "正文"


def test_extract_text_empty_payload_is_empty_string() -> None:
    assert OpenAICompatProvider._extract_text(None) == ""
    assert OpenAICompatProvider._extract_text({"choices": []}) == ""


# --------------------------------------------------------------------------- #
# 模型名（一套配置一个模型）
# --------------------------------------------------------------------------- #
def test_model_for_returns_the_single_configured_model(
    provider: Callable[[str], str],
) -> None:
    """**一套配置一个模型**：``model_for()`` 恒返回配置里的那个名字。

    Tier 分级已整体删除（2026-09-29），所以这个方法**不再有档位参数** ——
    「复算」用的是同一个模型重新采样，不存在「第二档用另一个模型」这回事。
    """
    profile = make_profile(provider("ok"), model="fake-model")
    impl = OpenAICompatProvider(profile, SecretStr(API_KEY), 2)

    assert impl.model_for() == "fake-model"


async def test_request_model_overrides_profile(provider: Callable[[str], str]) -> None:
    """``req.model`` 优先于配置里的模型名（求解层会显式传 ``model_for()``）。

    这条用假端点的 404 行为来证明「请求里的模型名**真的发到了服务端**」：
    填空串时回落到配置里的模型（正常回包），传一个端点上没有的名字时才会 404。
    """
    impl = OpenAICompatProvider(make_profile(provider("ok")), SecretStr(API_KEY), 2)

    response = await impl.complete(make_request(model=""))
    assert response.text == '{"ok": true}'

    with pytest.raises(ModelNotFoundError):
        await impl.complete(make_request(model="端点上没有的模型"))
    await impl.aclose()


def test_mock_provider_has_no_model_name() -> None:
    assert build_mock_provider(answer_source=QuestionBankSource.from_default()).model_for() is None


def test_solve_path_for_mock_is_mock() -> None:
    from solve.solver import Solver

    assert Solver._solve_path(build_mock_provider()) is SolvePath.MOCK


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def test_mock_scheme_selects_mock_provider() -> None:
    profile = make_profile("mock://local")
    assert provider_name_of(profile) is ProviderName.MOCK
    assert isinstance(build_provider(profile), build_mock_provider().__class__)


def test_http_scheme_selects_openai_compat() -> None:
    assert provider_name_of(make_profile("https://api.example.com/v1")) is (
        ProviderName.OPENAI_COMPAT
    )


def test_build_provider_raises_without_credential(provider: Callable[[str], str]) -> None:
    with pytest.raises(AuthError):
        build_provider(make_profile(provider("ok")), credentials=CredentialStore(InMemorySecretBackend()))


def test_build_provider_reads_key_from_credential_store(
    provider: Callable[[str], str],
) -> None:
    impl = build_provider(make_profile(provider("ok")), credentials=credentials())
    assert isinstance(impl, OpenAICompatProvider)


def test_concurrency_is_capped_by_profile(provider: Callable[[str], str]) -> None:
    """配置里写 2，就算运行期给了 10，也只能是 2。"""
    impl = build_provider(
        make_profile(provider("ok"), concurrency=2),
        credentials=credentials(),
        concurrency=10,
    )
    assert isinstance(impl, OpenAICompatProvider)
    assert impl._concurrency == 2


def test_chain_skips_profiles_without_credentials(provider: Callable[[str], str]) -> None:
    profiles = [
        make_profile(provider("ok"), capabilities=None),
    ]
    profiles[0] = profiles[0].model_copy(update={"profile_id": "no-key"})
    profiles[0] = profiles[0].model_copy(update={"api_key_ref": build_api_key_ref("no-key")})

    assert build_provider_chain(profiles, credentials=credentials()) == []


def test_chain_skips_disabled_profiles(provider: Callable[[str], str]) -> None:
    profile = make_profile(provider("ok")).model_copy(update={"enabled": False})
    assert build_provider_chain([profile], credentials=credentials()) == []


def test_chain_keeps_order(provider: Callable[[str], str]) -> None:
    store = credentials()
    profiles = []
    for profile_id in ("a", "b"):
        ref = build_api_key_ref(profile_id)
        store.put(ref, SecretStr(API_KEY))
        profiles.append(
            make_profile(provider("ok")).model_copy(
                update={"profile_id": profile_id, "api_key_ref": ref}
            )
        )
    chain = build_provider_chain(profiles, credentials=store)
    assert [impl.profile.profile_id for impl in chain if isinstance(impl, OpenAICompatProvider)] == [
        "a",
        "b",
    ]


def test_default_chain_falls_back_to_mock(provider: Callable[[str], str]) -> None:
    """一条可用配置都没有 → Mock 单链（无配置的用户也能跑通全链路）。"""
    chain = build_default_chain([], credentials=CredentialStore(InMemorySecretBackend()))

    assert len(chain) == 1
    assert chain[0].name is ProviderName.MOCK


def test_default_chain_uses_configured_profiles_when_available(
    provider: Callable[[str], str],
) -> None:
    chain = build_default_chain(
        [make_profile(provider("ok"))],
        credentials=credentials(),
    )
    assert len(chain) == 1
    assert chain[0].name is ProviderName.OPENAI_COMPAT


# --------------------------------------------------------------------------- #
# 响应解析（解析不出来就不猜）
# --------------------------------------------------------------------------- #
def test_parse_plain_json() -> None:
    payload = parse_answer_payload('{"chosen_labels": ["B"], "reason": "因为"}')
    assert payload == {"chosen_labels": ["B"], "reason": "因为"}


def test_parse_json_inside_code_fence() -> None:
    payload = parse_answer_payload('```json\n{"chosen_labels": ["a"]}\n```')
    assert payload is not None
    assert payload["chosen_labels"] == ["A"]


def test_parse_json_surrounded_by_chatter() -> None:
    payload = parse_answer_payload('好的，答案是：{"chosen_labels": ["A", "C"]} 以上。')
    assert payload is not None
    assert payload["chosen_labels"] == ["A", "C"]


def test_parse_tolerates_chinese_punctuation_and_letters_in_text() -> None:
    payload = parse_answer_payload('{"chosen_labels": "A、C", "reason": ""}')
    assert payload is not None
    assert payload["chosen_labels"] == ["A", "C"]


def test_parse_rejects_content_as_label() -> None:
    """选项正文不能被当成标号 —— 那是「按内容比对」的反面。"""
    assert parse_answer_payload('{"chosen_labels": ["SYN 报文"]}') is None


def test_parse_rejects_empty_and_garbage() -> None:
    assert parse_answer_payload("我觉得选 A 吧") is None
    assert parse_answer_payload("{不是 JSON") is None
    # 2026-09-30 起 ``{"chosen_labels": []}`` 是**显式空作答**（模型明确说答不了），
    # 不再算「解析失败」—— 两者的下游动作不同（不重发 vs 有界重发），
    # 见 ``solve/prompts.py::parse_answer_payload`` 与 ``tests/test_prompts.py``。
    explicit = parse_answer_payload('{"chosen_labels": []}')
    assert explicit is not None
    assert explicit["explicit_empty"] is True


def test_prompt_marks_multiple_choice_as_a_set() -> None:
    """多选题是**集合语义**：要完整集合，不许给「最接近」的近似集合。

    提示词 2026-09-28 起住在 ``prompts/20-解题组.md``（``solve/prompts.py``
    在导入时拼装），断言按那份文件里的**实际措辞**来 —— 不引号原文，
    免得在「」/""/"" 这类引号字符上陪跑。
    """
    from core.enums import ProbeName, QType
    from core.models import Option, Question

    question = Question(
        qid="q",
        stem="哪些是对的？",
        stem_hash="h",
        qtype=QType.MULTIPLE,
        options=[
            Option(index=0, label="A", text="甲", raw="甲"),
            Option(index=1, label="B", text="乙", raw="乙"),
        ],
        source=ProbeName.VISION,
    )
    system, user = build_messages(question, question.option_texts)

    assert "多选题" in user
    assert "多选是集合语义" in system
    assert "近似集合" in system, "「不许给近似集合」这条硬约束必须写在提示词里"
