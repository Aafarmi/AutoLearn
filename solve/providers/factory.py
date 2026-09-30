"""Provider 工厂（M2-1）。

职责边界
--------
工厂只做「配置 → 可用 Provider 实例」这一件事，**不做重试、不做投票、不选档位**。
密钥从 :class:`~core.model_registry.CredentialStore` 取（环境变量 > keyring）。

降级链的两条约定
----------------
1. ``build_provider_chain`` **跳过没有密钥的配置**，而不是抛错 —— 用户刚删掉 keyring
   条目时，剩下的配置仍应继续服务；
2. ``build_default_chain`` 在「一条可用配置都没有」时返回 **MockProvider 单链**，
   让无配置的用户也能跑通全链路（任务书 §3.2：交付后由用户自行添加配置）。

``base_url`` 以 ``mock://`` 开头的配置按 Mock 处理：这样「不联网跑一遍」不必
改代码路径，测试与演示都能用同一套装配逻辑。
"""

from __future__ import annotations

from core.enums import ProviderName
from core.model_registry import CredentialStore, ModelProfile
from solve.providers.base import AuthError, LLMProvider, ProviderError
from solve.providers.mock import AnswerSource, MockProvider, QuestionBankSource
from solve.providers.openai_compat import OpenAICompatProvider

__all__ = [
    "MOCK_SCHEME",
    "build_default_chain",
    "build_mock_provider",
    "build_provider",
    "build_provider_chain",
    "provider_name_of",
]

#: 以该协议头声明的配置走 MockProvider
MOCK_SCHEME = "mock://"


def provider_name_of(profile: ModelProfile) -> ProviderName:
    """由配置推导 Provider 实现。当前全部走 OpenAI 兼容协议（``mock://`` 除外）。"""
    if profile.base_url.startswith(MOCK_SCHEME):
        return ProviderName.MOCK
    return ProviderName.OPENAI_COMPAT


def build_provider(
    profile: ModelProfile,
    *,
    credentials: CredentialStore | None = None,
    answer_source: AnswerSource | None = None,
    concurrency: int | None = None,
    error_rate: float = 0.0,
    seed: int | None = None,
) -> LLMProvider:
    """按配置构造单个 Provider；密钥从 ``CredentialStore`` 取。

    :raises ~solve.providers.base.AuthError: 该配置**不是** Mock，且取不到密钥。
    :raises ~solve.providers.base.ProviderError: 取不到密钥（Mock 除外）。
    """
    if provider_name_of(profile) is ProviderName.MOCK:
        return build_mock_provider(
            answer_source=answer_source,
            error_rate=error_rate,
            seed=seed,
        )

    store = credentials if credentials is not None else CredentialStore()
    api_key = store.get(profile.api_key_ref)
    if api_key is None:
        raise AuthError(
            f"配置 {profile.profile_id} 取不到密钥（api_key_ref={profile.api_key_ref}）"
        )

    limit = concurrency if concurrency is not None else profile.concurrency
    return OpenAICompatProvider(profile, api_key, max(1, min(limit, profile.concurrency)))


def build_provider_chain(
    profiles: list[ModelProfile],
    *,
    credentials: CredentialStore | None = None,
    answer_source: AnswerSource | None = None,
    concurrency: int | None = None,
) -> list[LLMProvider]:
    """按降级链顺序批量构造。**取不到密钥的配置跳过**，不中断整条链。"""
    chain: list[LLMProvider] = []
    for profile in profiles:
        if not profile.enabled:
            continue
        try:
            chain.append(
                build_provider(
                    profile,
                    credentials=credentials,
                    answer_source=answer_source,
                    concurrency=concurrency,
                )
            )
        except (ProviderError, ValueError):
            # 单条配置不可用（多为缺密钥）不应拖垮整条降级链
            continue
    return chain


def build_mock_provider(
    *,
    answer_source: AnswerSource | None = None,
    error_rate: float = 0.0,
    seed: int | None = None,
) -> MockProvider:
    """构造 MockProvider。``answer_source`` 缺省读仓库内靶场题库。"""
    return MockProvider(
        answer_source if answer_source is not None else QuestionBankSource.from_default(),
        error_rate=error_rate,
        seed=seed,
    )


def build_default_chain(
    profiles: list[ModelProfile] | None = None,
    *,
    credentials: CredentialStore | None = None,
    answer_source: AnswerSource | None = None,
    concurrency: int | None = None,
    error_rate: float = 0.0,
) -> list[LLMProvider]:
    """用户已配模型 → 用配置链；**一条都没有 → Mock 单链**（无配置也能跑通）。"""
    chain = build_provider_chain(
        profiles or [],
        credentials=credentials,
        answer_source=answer_source,
        concurrency=concurrency,
    )
    if chain:
        return chain
    return [build_mock_provider(answer_source=answer_source, error_rate=error_rate)]
