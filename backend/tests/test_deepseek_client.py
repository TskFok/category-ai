import json

import httpx
import pytest

from app.core.config import Settings
from app.infrastructure.llm.deepseek import DeepSeekClient, LLMClientError


def test_deepseek_retries_blank_json_and_429():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        if calls["n"] == 2:
            # HTTP 200 with blank JSON content (Section 8.1 edge case)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "   "}}], "usage": {}},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"mentions": [{"surface_text": "韩立"}]})}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
            },
        )

    transport = httpx.MockTransport(handler)
    settings = Settings(
        deepseek_api_key="test-key",
        deepseek_mock_mode=False,
        llm_max_retries=3,
    )
    with httpx.Client(transport=transport) as http_client:
        client = DeepSeekClient(settings, http_client=http_client)
        resp = client.chat_json(system_prompt="sys", user_prompt="usr", tier="extract")

    assert calls["n"] == 3
    assert resp.data["mentions"][0]["surface_text"] == "韩立"
    assert resp.input_tokens == 1000
    assert resp.output_tokens == 200
    assert resp.estimated_cost > 0


def test_deepseek_fails_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": ""}}]},
        )

    transport = httpx.MockTransport(handler)
    settings = Settings(
        deepseek_api_key="test-key",
        deepseek_mock_mode=False,
        llm_max_retries=1,
    )
    with httpx.Client(transport=transport) as http_client:
        client = DeepSeekClient(settings, http_client=http_client)
        with pytest.raises(LLMClientError) as exc_info:
            client.chat_json(system_prompt="sys", user_prompt="usr")
    assert exc_info.value.error_type == "EMPTY_JSON_CONTENT"
