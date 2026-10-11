from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from cybergym_harness.llm import DeepSeekAdapter


@pytest.mark.asyncio
async def test_deepseek_usage_and_cache_fields_are_preserved(settings):
    configured = replace(
        settings,
        api_key="test-key",
        llm_mode="deepseek",
        thinking="enabled",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={
            "model": "deepseek-flash",
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 40,
            },
        })

    adapter = DeepSeekAdapter(configured)
    await adapter._client.aclose()
    adapter._client = httpx.AsyncClient(
        base_url=configured.base_url,
        headers={"Authorization": "Bearer test-key"},
        transport=httpx.MockTransport(handler),
    )
    try:
        turn = await adapter.complete(
            [{"role": "user", "content": "test"}], [], 100
        )
    finally:
        await adapter.close()
    assert turn.usage.input_tokens == 120
    assert turn.usage.output_tokens == 30
    assert turn.usage.cached_tokens == 80
    assert turn.usage.cache_miss_tokens == 40
    assert turn.usage.source == "api_actual"

