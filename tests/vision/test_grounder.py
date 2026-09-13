"""VLM 그라운더 테스트 (Stage 4 Task 5).

네트워크를 호출하지 않는다. `complete()`만 노출하는 가짜 클라이언트로
전송 메시지와 응답 해석을 검증한다.
"""

from __future__ import annotations

import base64
import json

import pytest

from contracts import BBox, thresholds
from llm import BudgetGuard
from llm.client import LLMResponse
from security.prompt_isolation import VISION_UNTRUSTED_NOTICE
from vision import SomCandidate
from vision.grounder import GroundingResult, ground

PNG = b"\x89PNG\r\n\x1a\nfake"


def _cand(tag: str, name: str = "") -> SomCandidate:
    return SomCandidate(
        tag=tag,
        selector_path=f"#{tag.lower()}",
        bbox=BBox(x=10, y=10, width=20, height=20),
        role="button",
        name=name,
    )


class FakeClient:
    """`OpenRouterClient.complete` 시그니처만 흉내낸다."""

    def __init__(self, content: str, budget: BudgetGuard | None = None) -> None:
        self.content = content
        self.budget = budget or BudgetGuard()
        self.calls: list[dict] = []

    async def complete(self, messages, *, model=None, temperature=0.0, max_tokens=1024, response_format=None):
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        return LLMResponse(
            content=self.content,
            model=model or "fake",
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=0.001,
        )


# --- 태그 모드 ----------------------------------------------------------------


async def test_tag_mode_returns_chosen_tag():
    client = FakeClient(json.dumps({"tag": "A3", "reason": "장바구니 아이콘"}))
    result = await ground(client, PNG, [_cand("A1"), _cand("A2"), _cand("A3")], "장바구니 열기")
    assert isinstance(result, GroundingResult)
    assert result.tag == "A3"
    assert result.point is None
    assert result.reason == "장바구니 아이콘"
    assert result.tokens == 120
    assert result.latency_ms >= 0


async def test_hallucinated_tag_is_rejected():
    client = FakeClient(json.dumps({"tag": "Z9", "reason": "x"}))
    result = await ground(client, PNG, [_cand("A1")], "goal")
    assert result.tag is None
    assert result.point is None


async def test_tag_mode_message_shape():
    client = FakeClient(json.dumps({"tag": "A1", "reason": "r"}))
    await ground(client, PNG, [_cand("A1", name="검색")], "검색하기", failure_context="click @e2 FAIL")
    call = client.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["max_tokens"] == 256
    assert call["temperature"] == 0
    system = call["messages"][0]
    assert system["role"] == "system"
    assert VISION_UNTRUSTED_NOTICE in system["content"]
    user = call["messages"][1]
    parts = user["content"]
    texts = [p["text"] for p in parts if p["type"] == "text"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 1
    expected = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
    assert images[0]["image_url"]["url"] == expected
    joined = "\n".join(texts)
    assert "A1" in joined and "button" in joined and "검색" in joined
    assert "검색하기" in joined
    assert "click @e2 FAIL" in joined


async def test_image_tokens_prerecorded_into_budget():
    budget = BudgetGuard()
    client = FakeClient(json.dumps({"tag": "A1", "reason": "r"}), budget=budget)
    await ground(client, PNG, [_cand("A1")], "goal")
    assert budget.used_tokens >= thresholds.SOM_IMAGE_TOKENS_PER_CAPTURE


async def test_unparseable_response_gives_empty_result():
    client = FakeClient("not json at all")
    result = await ground(client, PNG, [_cand("A1")], "goal")
    assert result.tag is None and result.point is None
    assert result.reason


# --- 좌표 모드 (옵션 B) ---------------------------------------------------------


async def test_coordinate_mode_when_no_candidates():
    client = FakeClient(json.dumps({"x": 640, "y": 360, "reason": "canvas"}))
    result = await ground(client, PNG, [], "장바구니 클릭")
    assert result.tag is None
    assert result.point == (640, 360)
    texts = "\n".join(
        p["text"] for p in client.calls[0]["messages"][1]["content"] if p["type"] == "text"
    )
    assert str(thresholds.VIEWPORT_WIDTH) in texts and str(thresholds.VIEWPORT_HEIGHT) in texts


@pytest.mark.parametrize("x,y", [(-1, 10), (1280, 10), (10, 720), (10, -5)])
async def test_out_of_viewport_point_is_rejected(x, y):
    client = FakeClient(json.dumps({"x": x, "y": y, "reason": "r"}))
    result = await ground(client, PNG, [], "goal")
    assert result.point is None


async def test_coordinate_mode_ignores_tag_field():
    client = FakeClient(json.dumps({"tag": "A1", "reason": "r"}))
    result = await ground(client, PNG, [], "goal")
    assert result.tag is None and result.point is None
