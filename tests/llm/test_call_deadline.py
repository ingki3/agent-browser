"""OpenRouter 호출 1회의 전체 시간 상한 (WS-7).

실측(tier2_som live 50런, 2026-09-23) — 러너가 51분째 CPU 0%로 OpenRouter
연결 하나를 연 채 멈췄다. httpx의 `timeout=60`은 **바이트 사이 간격** 상한이지
요청 전체 상한이 아니다(connect/read/write/pool 각각). 서버가 조금씩이라도
계속 보내면 끝나지 않는다 — 로컬에서 timeout=1.0으로 0.35초 간격 응답을
받아 보니 4.2초 걸려 '정상' 완료됐다(artifacts/diag_httpx_timeout.py).
루프의 태스크당 10분 상한도 스텝 사이에서만 확인하므로 이 멈춤을 못 막는다.

네트워크를 호출하지 않는다. httpx.MockTransport로 응답 시점을 조절한다.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from llm import BudgetGuard, LLMConfig, LLMError
from llm.client import OpenRouterClient

_OK = {
    "choices": [{"message": {"content": "{\"a\": 1}"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
    "model": "m",
}


def _config(**kw) -> LLMConfig:
    base = dict(api_key="sk-test-not-a-placeholder-0123456789", model="m",
                timeout_s=0.5, max_retries=0)
    base.update(kw)
    return LLMConfig(**base)


class _TrickleStream(httpx.AsyncByteStream):
    """본문을 한 바이트씩 `gap`초 간격으로 흘린다 — 간격 상한에는 안 걸린다."""

    def __init__(self, body: bytes, gap: float) -> None:
        self._body = body
        self._gap = gap

    async def __aiter__(self):
        for ch in self._body:
            await asyncio.sleep(self._gap)
            yield bytes([ch])


def _client_with(handler, **cfg) -> OpenRouterClient:
    client = OpenRouterClient(_config(**cfg), BudgetGuard())
    client._client = httpx.AsyncClient(
        base_url="https://openrouter.test/api/v1",
        transport=httpx.MockTransport(handler),
        timeout=client.config.timeout_s,
    )
    return client


async def test_trickling_response_is_cut_at_total_timeout():
    """조금씩 오는 응답도 timeout_s 안에 끝나지 않으면 실패다."""
    body = json.dumps(_OK).encode()

    async def handler(request):
        # 바이트 간격 0.05초 × ~150바이트 ≈ 7초 — 간격 상한 0.5초에는 안 걸린다
        return httpx.Response(200, stream=_TrickleStream(body, 0.05))

    client = _client_with(handler, timeout_s=0.5)
    started = time.perf_counter()
    with pytest.raises(LLMError, match="타임아웃"):
        await client.complete([{"role": "user", "content": "x"}])
    elapsed = time.perf_counter() - started
    await client.close()
    # 상한 0.5초 + 여유. 전체 상한이 없으면 ~7초 걸린다.
    assert elapsed < 2.0, f"{elapsed:.1f}초 — 전체 상한이 걸리지 않았다"


async def test_total_timeout_is_retried_then_fails():
    """전체 상한 초과는 기존 httpx 타임아웃과 같이 재시도 대상이다."""
    calls = []
    body = json.dumps(_OK).encode()

    async def handler(request):
        calls.append(1)
        return httpx.Response(200, stream=_TrickleStream(body, 0.05))

    client = _client_with(handler, timeout_s=0.3, max_retries=1)
    # 재시도 대기(2**0=1초)를 테스트에서 빼기 위해 sleep을 즉시 반환시킨다
    orig_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **kw):
        return await orig_sleep(0 if delay >= 1 else delay, *a, **kw)

    asyncio.sleep, saved = fast_sleep, asyncio.sleep
    try:
        with pytest.raises(LLMError, match="재시도 1회 후 실패: 타임아웃"):
            await client.complete([{"role": "user", "content": "x"}])
    finally:
        asyncio.sleep = saved
        await client.close()
    assert len(calls) == 2


async def test_fast_response_is_unaffected():
    async def handler(request):
        return httpx.Response(200, json=_OK)

    client = _client_with(handler, timeout_s=0.5)
    res = await client.complete([{"role": "user", "content": "x"}])
    await client.close()
    assert res.content == "{\"a\": 1}"


async def test_timeout_message_does_not_leak_key():
    body = json.dumps(_OK).encode()

    async def handler(request):
        return httpx.Response(200, stream=_TrickleStream(body, 0.05))

    client = _client_with(handler, timeout_s=0.2)
    with pytest.raises(LLMError) as info:
        await client.complete([{"role": "user", "content": "x"}])
    await client.close()
    assert "sk-test" not in str(info.value)
