"""OpenAI 兼容 Provider（M2-1）。

换厂商只改 ``base_url`` + 模型名，本模块代码不动。

三条纪律
--------
1. **连接池复用**：一个实例一条 ``httpx.AsyncClient``，不每次请求新建；
2. **并发用信号量包住整个请求生命周期**（建连 → 读 body → 释放），
   ``asyncio.Semaphore`` 只包住 ``post`` 会漏掉响应体读取那段时间；
3. **HTTP 状态码必须翻译成 §2.4 错误码字典里的异常**，不许把 401 当普通失败吞掉 ——
   UI 要靠它分流到「鉴权失败」引导卡，Prompter 靠它决定是否换下一条降级链。

能力降级
--------
``profile.capabilities`` 是 ``[测试连接]`` 的实测结果（M2-6，不接受手填）：

- 实测不支持结构化输出 → **不塞** ``response_format``，仍照常解析文本；
- 实测不支持视觉却来了图片 → 抛 :class:`VisionNotSupportedError`（早失败，不烧钱）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx
from pydantic import SecretStr

from core.enums import ProviderName
from core.model_registry import ModelProfile
from solve.prompts import parse_answer_payload
from solve.providers.base import (
    AuthError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    TokenUsage,
    VisionNotSupportedError,
)

__all__ = ["OpenAICompatProvider"]


def _status_error(status: int, body: str) -> ProviderError:
    """HTTP 状态码 → 错误码字典里的异常（§2.4）。"""
    detail = body[:200]
    if status in (401, 403):
        return AuthError(f"鉴权失败（HTTP {status}）：{detail}")
    if status == 404:
        return ModelNotFoundError(f"模型名或端点不存在（HTTP 404）：{detail}")
    if status == 429:
        return RateLimitError(f"触发限流（HTTP 429）：{detail}")
    return ProviderError(f"Provider 返回 HTTP {status}：{detail}")


def _data_uri(image: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


class OpenAICompatProvider(LLMProvider):
    """面向 ``/v1/chat/completions`` 的通用实现。"""

    name = ProviderName.OPENAI_COMPAT

    def __init__(
        self,
        profile: ModelProfile,
        api_key: SecretStr,
        concurrency: int,
    ) -> None:
        self.profile = profile
        self._api_key = api_key
        self._concurrency = max(1, concurrency)
        self._client: httpx.AsyncClient | None = None
        # 延迟到首次请求才创建：Semaphore 绑定事件循环，构造期还没有循环
        self._semaphore: asyncio.Semaphore | None = None

    # -- 模型名 ------------------------------------------------------------ #
    def model_for(self) -> str | None:
        """返回这套配置使用的模型名。

        **一套配置只有**一个**模型**（2026-09-28 定，2026-09-29 彻底去掉档位参数）——
        Tier 分级已删除，「复算」用的是同一个模型重新采样。
        """
        return self.profile.model

    # -- 请求 -------------------------------------------------------------- #
    async def complete(self, req: LLMRequest) -> LLMResponse:
        if req.images and not self._vision_allowed():
            raise VisionNotSupportedError(
                f"模型 {self.profile.model} 未通过视觉能力实测，收到图片请求"
            )
        model = req.model or self.profile.model
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": self._user_content(req)},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.force_json and self._structured_allowed():
            body["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        semaphore = self._gate()
        async with semaphore:
            response = await self._http().post("chat/completions", json=body)
            text_body = response.text
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            raise _status_error(response.status_code, text_body)

        payload = self._decode(text_body)
        text = self._extract_text(payload)
        return LLMResponse(
            text=text,
            parsed=parse_answer_payload(text),
            usage=self._extract_usage(payload),
            latency_ms=latency_ms,
            raw=text_body,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- 内部 -------------------------------------------------------------- #
    def _gate(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._concurrency)
        return self._semaphore

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.profile.base_url.rstrip("/") + "/",
                headers={
                    "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(self.profile.timeout_s),
            )
        return self._client

    def _vision_allowed(self) -> bool:
        caps = self.profile.capabilities
        # 没实测过就不拦（用户可能刚配好还没点「测试连接」）；实测明确不支持才早失败
        return caps is None or caps.supports_vision

    def _structured_allowed(self) -> bool:
        caps = self.profile.capabilities
        return caps is None or caps.supports_structured_output

    def _user_content(self, req: LLMRequest) -> Any:
        """纯文本请求给字符串；带图请求给 OpenAI 的 content parts 数组。"""
        if not req.images:
            return req.user
        parts: list[dict[str, Any]] = [{"type": "text", "text": req.user}]
        parts += [
            {"type": "image_url", "image_url": {"url": _data_uri(image)}} for image in req.images
        ]
        return parts

    @staticmethod
    def _decode(text_body: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(text_body)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _extract_text(payload: dict[str, Any] | None) -> str:
        """从 OpenAI 兼容响应里抠出正文，**多厂商兜底**。

        各厂商/各模型「答案放在哪」不一样，实测踩到过：

        * 常规：``choices[0].message.content`` 是字符串；
        * **推理类模型**（DeepSeek R1 / v4 系）：思考过程与最终答案分字段，
          有的把最终 JSON 放进 ``message.reasoning_content``，``content`` 反而是空；
        * 极少数：``content`` 是 ``[{type:"text", text:...}]`` 的 parts 数组；
        * 老式补全：``choices[0].text``。

        顺序是「可信度从高到低」：**content 优先**（标准字段），为空再退
        ``reasoning_content``，再退 parts 数组，最后才看 ``text``。
        任何一个兜底救回来的正文，都好过「读题失败 + 空原文」—— 那会让排查
        连模型到底回了什么都没地方看。
        """
        if not payload:
            return ""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""

        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
            # 推理模型的最终答案可能在这里（content 为空时）
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                return reasoning
            # content 被拆成 parts 数组的厂商
            if isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if isinstance(part, dict):
                        txt = part.get("text")
                        if isinstance(txt, str):
                            parts.append(txt)
                if parts:
                    return "\n".join(parts)

        # 老式补全格式
        text = first.get("text")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _extract_usage(payload: dict[str, Any] | None) -> TokenUsage:
        if not payload:
            return TokenUsage()
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return TokenUsage()
        return TokenUsage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )
