"""Jev 판단기 + qwen 폴백 (2026-09-24 agent_eval 실측으로 고른 구성).

Jev에게 좁은 질문을 차례로 묻는다(행동 → 대상 → 값). 다음 경우에만 폴백 모델
(qwen/qwen3.8-27b)에게 넘긴다: 행동 확신 < 0.5, '끝' 확신 < 0.7, 직전 실패,
후보 요소 없음, Jev가 값을 만들 수 없음. 폴백이 한 번 넘겨받으면 다음 1스텝도 이어서.

네트워크를 호출하지 않는다 — Decisions/Chat 모두 가짜로 바꾼다.
"""

from __future__ import annotations

import json

import pytest

from agent.jev_decider import (
    FALLBACK_STICKY_STEPS,
    JevDecider,
    quoted_values,
    screen_order,
)
from contracts import BBox, ObservedElement, ObserveResult
from llm.decisions import DecisionAnswer


def _el(eid, role, name, y, x=10, value=None, interactable=True):
    return ObservedElement(element_id=eid, role=role, name=name, value=value,
                           bbox=BBox(x=x, y=y, width=50, height=20),
                           interactable=interactable, score=1.0)


def _obs(*els, url="https://x.test/", title="t"):
    return ObserveResult(title=title, url=url, snapshot_epoch=1, elements=list(els),
                         axtree_summary="", token_count=0)


class FakeDecisions:
    """질문 이름별로 미리 정한 답을 준다. 받은 질문을 기록한다."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    async def ask(self, state, name, question):
        self.asked.append((name, state, question))
        a = self.answers[name]
        if callable(a):
            a = a(state, question)
        if isinstance(a, Exception):
            raise a
        choice, conf = a
        return DecisionAnswer(choice=choice, confidence=conf, latency_ms=200.0, cost_usd=0.00001)


def test_quoted_values_keeps_order_and_dedupes():
    assert quoted_values("사용자명에 'tomsmith', 비밀번호에 'Pw!'를 입력, 'tomsmith'") == ["tomsmith", "Pw!"]
    assert quoted_values('검색창에 "python" 입력') == ["python"]
    assert quoted_values("따옴표 없음") == []


def test_screen_order_is_top_to_bottom_then_left_to_right():
    els = [_el("@e1", "link", "b", 300), _el("@e2", "link", "a", 12), _el("@e3", "link", "c", 12, x=200)]
    assert [e.element_id for e in screen_order(els)] == ["@e2", "@e3", "@e1"]


async def test_click_asks_action_then_target_among_clickables_only():
    obs = _obs(_el("@e1", "textbox", "검색", 10), _el("@e2", "link", "new", 30), _el("@e3", "link", "ask", 30, x=100))
    fake = FakeDecisions({"action": ("click", 0.95), "target": ("@e3", 0.9)})
    res = await JevDecider(fake).decide("'ask' 링크를 클릭", obs, history=[])
    assert res.decision == {"action": "click", "element_id": "@e3", "reason": "jev click 0.95"}
    assert res.defer_reason == ""
    names = [n for n, *_ in fake.asked]
    assert names == ["action", "target"]
    target_q = fake.asked[1][2]
    assert set(target_q["criteria"]) == {"@e2", "@e3"}, "click 후보는 링크·버튼만(입력칸 제외)"


async def test_single_candidate_is_not_asked():
    obs = _obs(_el("@e1", "textbox", "검색", 10), _el("@e2", "link", "x", 30))
    fake = FakeDecisions({"action": ("type_text", 0.99)})
    res = await JevDecider(fake).decide("검색창에 'python' 입력", obs, history=[])
    assert res.decision["element_id"] == "@e1" and res.decision["text"] == "python"
    assert [n for n, *_ in fake.asked] == ["action"], "후보·값이 하나면 묻지 않는다"


async def test_value_question_when_several_quoted_values():
    obs = _obs(_el("@e1", "textbox", "Username", 10), _el("@e2", "textbox", "Password", 40))
    fake = FakeDecisions({"action": ("type_text", 0.99), "target": ("@e2", 0.99), "value": ("t1", 0.9)})
    res = await JevDecider(fake).decide("사용자명에 'tom', 비밀번호에 'Pw!'", obs,
                                        history=["type_text @e1 -> OK ('tom' 입력)"])
    assert res.decision == {"action": "type_text", "element_id": "@e2", "text": "Pw!", "reason": "jev type_text 0.99"}
    value_state = fake.asked[2][1]
    assert value_state["done_so_far"] == ["type_text @e1 -> OK ('tom' 입력)"]


async def test_press_enter_and_scroll_map_to_loop_actions():
    obs = _obs(_el("@e1", "link", "x", 10))
    r1 = await JevDecider(FakeDecisions({"action": ("press_enter", 0.9)})).decide("g", obs, history=[])
    assert r1.decision == {"action": "press_key", "key": "Enter", "reason": "jev press_enter 0.90"}
    r2 = await JevDecider(FakeDecisions({"action": ("scroll", 0.9)})).decide("g", obs, history=[])
    assert r2.decision["action"] == "scroll" and r2.decision["direction"] == "down"


@pytest.mark.parametrize("answers, history, obs_els, reason", [
    ({"action": ("click", 0.49), "target": ("@e1", 0.9)}, [], True, "행동 확신 낮음"),
    ({"action": ("finish", 0.69)}, ["click @e1 -> OK"], True, "완료 확신 낮음"),
    ({"action": ("click", 0.99), "target": ("@e1", 0.9)}, ["click @e1 -> FAIL E_TIMEOUT"], True, "직전 액션 실패"),
    ({"action": ("click", 0.99)}, [], False, "후보 요소 없음"),
    ({"action": ("type_text", 0.99), "target": ("@e1", 0.9)}, [], True, "입력값 없음"),
])
async def test_defer_reasons(answers, history, obs_els, reason):
    obs = _obs(_el("@e1", "link", "x", 10), _el("@e2", "textbox", "y", 30)) if obs_els else _obs()
    res = await JevDecider(FakeDecisions(answers)).decide("따옴표 없는 목표", obs, history=history)
    assert reason in res.defer_reason


async def test_confident_finish_is_not_deferred():
    res = await JevDecider(FakeDecisions({"action": ("finish", 0.95)})).decide(
        "g", _obs(_el("@e1", "link", "x", 10)), history=["click @e1 -> OK"])
    assert res.defer_reason == "" and res.decision["action"] == "finish"


async def test_jev_error_defers_without_decision():
    from llm import LLMError

    res = await JevDecider(FakeDecisions({"action": LLMError("Decisions 타임아웃")})).decide(
        "g", _obs(_el("@e1", "link", "x", 10)), history=[])
    assert res.decision is None and "Jev 오류" in res.defer_reason


async def test_sticky_fallback_for_next_step_only():
    d = JevDecider(FakeDecisions({"action": ("click", 0.99), "target": ("@e1", 0.99)}))
    obs = _obs(_el("@e1", "link", "x", 10), _el("@e2", "link", "y", 30))
    d.fallback_used("행동 확신 낮음 0.40")
    r1 = await d.decide("g", obs, history=["click @e1 -> OK"])
    assert "직전에 폴백" in r1.defer_reason
    d.fallback_used(r1.defer_reason)          # 이어받은 판단은 다시 연장하지 않는다
    r2 = await d.decide("g", obs, history=["click @e1 -> OK"])
    assert r2.defer_reason == ""
    assert FALLBACK_STICKY_STEPS == 1


async def test_state_carries_goal_history_page_text_and_screen_order():
    obs = _obs(_el("@e1", "link", "아래", 300), _el("@e2", "link", "위", 10))
    fake = FakeDecisions({"action": ("read_text", 0.9)})
    await JevDecider(fake).decide("목표", obs, history=["a", "b"], page_text="보이는 글자")
    state = fake.asked[0][1]
    assert state["request"] == "목표" and state["done_so_far"] == ["a", "b"]
    assert state["page"]["visible_text"] == "보이는 글자"
    assert [x.split(" ")[0] for x in state["page"]["elements_in_screen_order"]] == ["@e2", "@e1"]
    json.dumps(state, ensure_ascii=False)  # 직렬화 가능해야 한다
