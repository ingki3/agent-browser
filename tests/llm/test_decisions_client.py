"""Jev Decisions API 클라이언트 (typesafe/jev — 보기 고르기 전용 모델).

네트워크를 호출하지 않는다. httpx.MockTransport로 응답을 흉내 낸다.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from llm import BudgetGuard, LLMConfig, LLMError
from llm.decisions import DecisionsClient, decisions_url

KEY = "sk-or-v1-" + "d" * 40


def _cfg(**kw) -> LLMConfig:
    base = dict(api_key=KEY, model="m", base_url="https://openrouter.ai/api/v1")
    base.update(kw)
    return LLMConfig(**base)


def _answer(name: str, choice: str, probs: dict, confidence: float = 0.9) -> dict:
    return {"model": "typesafe/jev-1.13", "answers": {name: {
        "type": "choice", "choice": choice, "probabilities": probs, "confidence": confidence}},
        "usage": {"input_tokens": 300, "output_tokens": 20, "cost": 0.0000126}}


def _client(handler, *, budget=None, timeout_s=5.0) -> DecisionsClient:
    c = DecisionsClient(_cfg(), budget or BudgetGuard(), timeout_s=timeout_s)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout_s)
    return c


def test_url_is_derived_from_base_url():
    assert decisions_url("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/alpha/decisions"
    assert decisions_url("https://openrouter.ai/api/v1/") == "https://openrouter.ai/api/alpha/decisions"


async def test_choice_request_shape_and_parsed_answer():
    seen = {}

    async def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_answer("action", "click", {"click": 0.9, "finish": 0.1}, 0.85))

    budget = BudgetGuard()
    c = _client(handler, budget=budget)
    ans = await c.ask({"request": "g"}, "action",
                      {"type": "choice", "instructions": "i", "criteria": {"click": "a", "finish": "b"}})
    await c.close()
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == "~typesafe/jev-latest"
    assert seen["body"]["state"] == {"request": "g"}
    assert list(seen["body"]["questions"]) == ["action"]
    assert ans.choice == "click" and ans.confidence == 0.85
    assert ans.probabilities == {"click": 0.9, "finish": 0.1}
    assert budget.used_usd == pytest.approx(0.0000126) and budget.calls == 1


async def test_noul_answer_is_parsed_as_probability():
    async def handler(request):
        return httpx.Response(200, json={"answers": {"done": {"type": "noul", "noul": 0.3}},
                                         "usage": {"input_tokens": 10, "cost": 0.0}})

    c = _client(handler)
    ans = await c.ask({}, "done", {"type": "noul", "instructions": "i", "true": "y", "false": "n"})
    await c.close()
    assert ans.choice is None and ans.noul == 0.3


async def test_http_error_raises_with_body_but_not_key():
    async def handler(request):
        return httpx.Response(400, json={"error": {"message": "Choice question must have at least one choice"}})

    c = _client(handler)
    with pytest.raises(LLMError) as exc:
        await c.ask({}, "target", {"type": "choice", "instructions": "i", "criteria": {}})
    await c.close()
    assert "400" in str(exc.value) and "at least one choice" in str(exc.value)
    assert KEY not in str(exc.value)


async def test_total_timeout():
    async def handler(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json=_answer("a", "x", {"x": 1}))

    c = _client(handler, timeout_s=0.3)
    with pytest.raises(LLMError, match="타임아웃"):
        await c.ask({}, "a", {"type": "choice", "instructions": "i", "criteria": {"x": "x"}})
    await c.close()


async def test_budget_is_checked_before_call():
    from llm import BudgetExceeded

    calls = []

    async def handler(request):
        calls.append(1)
        return httpx.Response(200, json=_answer("a", "x", {"x": 1}))

    budget = BudgetGuard(max_usd=0.0)
    budget.used_usd = 0.01
    c = _client(handler, budget=budget)
    with pytest.raises(BudgetExceeded):
        await c.ask({}, "a", {"type": "choice", "instructions": "i", "criteria": {"x": "x"}})
    await c.close()
    assert calls == []
