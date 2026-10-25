from __future__ import annotations

import pytest

from core.courses.errors import CourseUnsupported
from core.courses.providers.zhihuishu.aimap import build


@pytest.mark.parametrize(
    ("endpoint", "expected_base"),
    [
        ("https://api.openai.com/v1/chat/completions", "https://api.openai.com"),
        (
            "https://x.com/openai/v1/chat/completions",
            "https://x.com/openai",
        ),
        (
            "https://x.com/openai/chat/completions",
            "https://x.com/openai",
        ),
        ("https://x.com/v1/responses", None),
        ("http://127.0.0.1:1234/v1/chat/completions", "http://127.0.0.1:1234"),
    ],
)
def test_build_maps_supported_chat_completions_urls(
    endpoint: str, expected_base: str | None
) -> None:
    settings = {
        "base_url": endpoint,
        "api_key": "secret-test-key",
        "model": "gpt-test",
    }
    if expected_base is None:
        with pytest.raises(CourseUnsupported):
            build(settings)
        return

    result = build(settings)
    assert result == {
        "enabled": True,
        "use_zhidao_ai": False,
        "openai": {
            "api_base": expected_base,
            "api_key": "secret-test-key",
            "model_name": "gpt-test",
        },
        "ppt_processing": {
            "provide_to_ai": False,
            "moonShot": {"base_url": "", "api_key": ""},
        },
        "use_stream": True,
    }


def test_build_rejects_empty_api_key_without_echoing_secrets() -> None:
    with pytest.raises(CourseUnsupported, match="该模型档案没有配置 API 密钥"):
        build(
            {
                "base_url": "https://api.openai.com/v1/chat/completions",
                "api_key": "  ",
                "model": "gpt-test",
            }
        )
