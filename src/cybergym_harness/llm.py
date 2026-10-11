from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .metrics import UsageRecord, estimate_usage


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class ChatTurn:
    message: dict[str, Any]
    usage: UsageRecord
    model: str


class LLMAdapter:
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ChatTurn:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class DeepSeekAdapter(LLMAdapter):
    def __init__(self, settings: Settings):
        settings.validate_runtime()
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ChatTurn:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "thinking": {"type": self.settings.thinking},
        }
        if self.settings.thinking == "disabled":
            payload["temperature"] = self.settings.temperature
        started = time.monotonic()
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError(f"DeepSeek request timed out after {self.settings.llm_timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"DeepSeek transport error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            detail = response.text[:1000]
            raise LLMError(f"DeepSeek HTTP {response.status_code}: {detail}")
        try:
            body = response.json()
            message = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("DeepSeek returned an invalid chat completion") from exc
        latency = time.monotonic() - started
        usage_body = body.get("usage")
        if isinstance(usage_body, dict):
            prompt = int(usage_body.get("prompt_tokens") or 0)
            completion = int(usage_body.get("completion_tokens") or 0)
            details = usage_body.get("prompt_tokens_details") or {}
            cached = int(
                usage_body.get("prompt_cache_hit_tokens")
                or details.get("prompt_cache_hit_tokens")
                or details.get("cached_tokens")
                or 0
            )
            cache_miss = int(
                usage_body.get("prompt_cache_miss_tokens")
                or details.get("prompt_cache_miss_tokens")
                or max(prompt - cached, 0)
            )
            usage = UsageRecord(
                input_tokens=prompt,
                output_tokens=completion,
                cached_tokens=cached,
                cache_miss_tokens=cache_miss,
                total_tokens=int(usage_body.get("total_tokens") or prompt + completion),
                source="api_actual",
                latency_seconds=latency,
            )
        else:
            usage = estimate_usage(payload, message)
            usage.latency_seconds = latency
        clean_message = {
            key: value
            for key, value in message.items()
            if key in {"role", "content", "reasoning_content", "tool_calls"} and value is not None
        }
        clean_message.setdefault("role", "assistant")
        return ChatTurn(message=clean_message, usage=usage, model=str(body.get("model") or self.settings.model))

    async def close(self) -> None:
        await self._client.aclose()


class MockLLMAdapter(LLMAdapter):
    """Deterministic, zero-network adapter used by free integration tests."""

    def __init__(self, model: str = "mock-deepseek"):
        self.model = model
        self.calls = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ChatTurn:
        await asyncio.sleep(0)
        self.calls += 1
        payload = b"first-does-not-crash" if self.calls == 1 else b"mock-crash-poc"
        call_id = f"mock-call-{self.calls}"
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "submit_poc",
                    "arguments": json.dumps({
                        "poc_base64": base64.b64encode(payload).decode("ascii"),
                        "explanation": f"mock attempt {self.calls}",
                    }),
                },
            }],
        }
        usage = UsageRecord(
            input_tokens=100 if self.calls == 1 else 80,
            output_tokens=20 if self.calls == 1 else 15,
            cached_tokens=0 if self.calls == 1 else 10,
            cache_miss_tokens=100 if self.calls == 1 else 70,
            total_tokens=120 if self.calls == 1 else 95,
            source="mock",
            latency_seconds=0.001,
        )
        return ChatTurn(message, usage, self.model)


def make_llm(settings: Settings) -> LLMAdapter:
    if settings.llm_mode == "mock":
        return MockLLMAdapter(settings.model)
    return DeepSeekAdapter(settings)

