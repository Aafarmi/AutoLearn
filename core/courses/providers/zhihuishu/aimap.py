"""把本地模型档案设置映射为 fuckZHS 所需的 AI 配置。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ...errors import CourseUnsupported

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


def build(profile_settings: Mapping[str, Any]) -> dict[str, Any]:
    """构造一次运行使用的上游配置；调用者不得将结果持久化到 job.json。"""
    if not isinstance(profile_settings, Mapping):
        raise CourseUnsupported("模型档案配置无效。")

    endpoint = profile_settings.get("base_url")
    if not isinstance(endpoint, str):
        raise CourseUnsupported("模型档案接口地址无效。")
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        raise CourseUnsupported("模型档案接口地址无效。") from None

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.path.endswith(_CHAT_COMPLETIONS_SUFFIX)
    ):
        raise CourseUnsupported("模型档案仅支持 /chat/completions 接口。")

    api_key = profile_settings.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CourseUnsupported("该模型档案没有配置 API 密钥。")

    base_path = parsed.path[: -len(_CHAT_COMPLETIONS_SUFFIX)].rstrip("/")
    if base_path.endswith("/v1"):
        base_path = base_path[:-3]
    api_base = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))

    return {
        "enabled": True,
        "use_zhidao_ai": False,
        "openai": {
            "api_base": api_base,
            "api_key": api_key,
            "model_name": profile_settings.get("model", ""),
        },
        "ppt_processing": {
            "provide_to_ai": False,
            "moonShot": {"base_url": "", "api_key": ""},
        },
        "use_stream": True,
    }

