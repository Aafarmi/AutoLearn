"""Provider 抽象与错误体系（M2-1）。

统一走 OpenAI 兼容协议，换厂商只改 ``base_url`` + 模型名。
错误码必须来自规划书 §2.4 字典，进字典才准用。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.enums import ProviderName

__all__ = [
    "AuthError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "ModelNotFoundError",
    "ProviderError",
    "ProviderName",
    "RateLimitError",
    "StructuredNotSupportedError",
    "TokenUsage",
    "VisionNotSupportedError",
]


class TokenUsage(BaseModel):
    """token 用量（M1-4a 的量化指标之一；原先是三通道对比，v0.2.0 只剩模型这一侧）。"""

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMRequest(BaseModel):
    """一次模型请求。``images`` 非空即视觉请求。"""

    model_config = ConfigDict(extra="forbid")

    model: str
    system: str
    user: str
    images: list[bytes] = Field(default_factory=list)
    temperature: float = 0.2
    force_json: bool = True
    max_tokens: int = 2048


class LLMResponse(BaseModel):
    """一次模型响应。``raw`` 必须原样落进 ``solve.json``。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    parsed: dict[str, Any] | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
    raw: str = ""
    error_code: str | None = None


class ProviderError(Exception):
    """Provider 层异常基类。``code`` 必须来自 §2.4 错误码字典。"""

    code: str = "provider_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(message or self.code)


class AuthError(ProviderError):
    """401 / 403。UI 标红 + 引导卡 ``GUIDE_AUTH_FAILED``。"""

    code = "auth_failed"


class RateLimitError(ProviderError):
    """429。UI 降速提示 + 重试倒计时。"""

    code = "rate_limited"


class ModelNotFoundError(ProviderError):
    """404 / 模型名无效。"""

    code = "model_not_found"


class VisionNotSupportedError(ProviderError):
    """模型不支持图片。「模型优先」置灰。"""

    code = "vision_unsupported"


class StructuredNotSupportedError(ProviderError):
    """不支持 JSON Schema。降级为文本解析。"""

    code = "structured_unsupported"


class LLMProvider(ABC):
    """模型 Provider 抽象。一个实例复用一条 ``httpx.AsyncClient`` 连接池。"""

    name: ProviderName

    @abstractmethod
    async def complete(self, req: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    @abstractmethod
    async def aclose(self) -> None:
        raise NotImplementedError

    def model_for(self) -> str | None:
        """这套配置实际使用的模型名。

        Tier 分级（Tier1 / Tier2）已整体删除：**一套配置一个模型**，
        请求里只有「最终用哪个模型名」。求解层只管问「这次用谁」。

        返回 ``None`` 表示本 Provider 不分模型（如 MockProvider），
        此时求解层会照常发请求，由 Provider 自行决定用什么。
        """
        return None
