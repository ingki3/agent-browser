"""AgentLoop + Jev 판단기 연결 (decider="jev").

- Jev가 확신하면 채팅 모델을 부르지 않는다.
- 넘길 신호면 폴백 모델(qwen3.8-27b, 생각 짧게 + JSON 강제)을 부른다.
- 폴백이 실패하면: Jev 답이 있으면 그걸 쓰고, 없으면 그 스텝은 포기로 기록.
- 로컬(OpenRouter가 아닌) base_url에서는 Jev를 쓰지 않는다 — 로그인 작업은
  페이지 내용이 클라우드로 가면 안 된다.
- 이력 요약에 입력값이 남는다(Jev가 이미 넣은 값을 알아야 반복하지 않는다).
"""

from __future__ import annotations

import pytest

from agent import loop as loop_mod
from agent.loop import AgentLoop, StepOutcome
from agent.policy import Decision
from actions import ActionDispatcher, DispatchContext
from llm import LLMConfig, LLMError
from llm.client import LLMResponse
from llm.decisions import DecisionAnswer
from perception import PerceptionEngine

PAGE = """<!doctype html><meta charset=utf-8><title>검색</title>
<input aria-label='검색어' id=q><button onclick="document.title='결과';document.body.insertAdjacentHTML('beforeend','<p id=r>검색 결과</p>')">검색</button>
<a href='#a'>도움말</a>"""


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")


def _cfg(base="https://openrouter.ai/api/v1", decider="jev"):
    return LLMConfig(api_key="sk-or-v1-" + "k" * 40, model="z-ai/glm-5.3-flash",
                     base_url=base, decider=decider, fallback_model="qwen/qwen3.8-27b")


class FakeChat:
    def __init__(self, bodies, fail=False):
        self.bodies = list(bodies)
        self.calls = []
        self.fail = fail

    async def __aenter__(self):
        return self

    async def close(self):
        return None

    async def complete(self, messages, **kw):
        self.calls.append(kw)
        if self.fail:
            raise LLMError("재시도 0회 후 실패: 타임아웃 (60.0s)")
        body = self.bodies.pop(0)
        if callable(body):
            body = body(messages[1]["content"])
        return LLMResponse(content=body, model=kw.get("model") or "m",
                           prompt_tokens=10, completion_tokens=5, cost_usd=0.001)


class FakeDecisions:
    """스크립트: 스텝마다 (action, conf) — click/type 대상은 이름으로 찾는다."""

    def __init__(self, script):
        self.script = list(script)
        self.asked = []
        self.started = self.closed = False

    async def start(self):
        self.started = True
        return self

    async def close(self):
        self.closed = True

    async def ask(self, state, name, question):
        self.asked.append(name)
        if name == "action":
            act, conf = self.script.pop(0)
            return DecisionAnswer(choice=act, confidence=conf, latency_ms=200, cost_usd=0.00001)
        if name == "target":
            # type_text → 입력칸 '검색어', click → 버튼 '검색'
            want = "검색어" if "입력칸" in question["instructions"] else "검색"
            for k, v in question["criteria"].items():
                if f"'{want}'" in v:
                    return DecisionAnswer(choice=k, confidence=0.9, latency_ms=200)
            return DecisionAnswer(choice=next(iter(question["criteria"])), confidence=0.9)
        raise AssertionError(name)


async def _run(monkeypatch, *, cfg, chat, decisions, goal="검색창에 'python'을 입력하고 검색", steps=5):
    from playwright.async_api import async_playwright

    monkeypatch.setattr(loop_mod, "OpenRouterClient", lambda *a, **k: chat)
    monkeypatch.setattr(loop_mod, "DecisionsClient", lambda *a, **k: decisions)
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.set_content(PAGE)
        engine = PerceptionEngine()
        disp = ActionDispatcher(DispatchContext(page=page, engine=engine, cdp=await ctx.new_cdp_session(page)))
        run = await AgentLoop(page=page, engine=engine, dispatcher=disp, config=cfg,
                              max_steps=steps).run(goal)
        title = await page.title()
        value = await page.input_value("#q")
        await b.close()
    return run, title, value


@requires_chromium
async def test_confident_jev_runs_without_chat_model(monkeypatch):
    chat = FakeChat([])
    dec = FakeDecisions([("type_text", 0.99), ("click", 0.95), ("finish", 0.9)])
    run, title, value = await _run(monkeypatch, cfg=_cfg(), chat=chat, decisions=dec)
    assert run.completed, run.terminal_reason
    assert value == "python" and title == "결과"
    assert chat.calls == [], "Jev가 확신하면 채팅 모델을 부르지 않는다"
    assert [s.decided_by for s in run.steps] == ["jev", "jev", "jev"]
    assert dec.started and dec.closed
    assert "('python' 입력)" in run.steps[0].summary(), "이력에 입력값이 남아야 한다"


@requires_chromium
async def test_low_confidence_defers_to_fallback_with_its_settings_then_sticks(monkeypatch):
    import re

    def type_into_box(prompt):
        eid = re.search(r'\[(@e\d+)\] textbox', prompt).group(1)
        return '{"action":"type_text","element_id":"%s","text":"python","reason":"q"}' % eid

    chat = FakeChat([type_into_box, '{"action":"finish","reason":"done"}'])
    dec = FakeDecisions([("click", 0.30), ("click", 0.99), ("finish", 0.95)])
    run, _t, value = await _run(monkeypatch, cfg=_cfg(), chat=chat, decisions=dec, steps=4)
    assert run.steps[0].decided_by == "fallback" and "행동 확신 낮음" in run.steps[0].defer_reason
    assert run.steps[1].decided_by == "fallback" and "직전에 폴백" in run.steps[1].defer_reason
    kw = chat.calls[0]
    assert kw["model"] == "qwen/qwen3.8-27b"
    assert kw["reasoning"] == {"effort": "low"}
    assert kw["response_format"] == {"type": "json_object"}
    assert value == "python"


@requires_chromium
async def test_fallback_failure_uses_jev_answer(monkeypatch):
    chat = FakeChat([], fail=True)
    dec = FakeDecisions([("finish", 0.6)])        # 완료 확신 낮음 → 폴백 → 실패 → Jev 답(finish)
    run, _t, _v = await _run(monkeypatch, cfg=_cfg(), chat=chat, decisions=dec, steps=2)
    assert run.steps[0].decided_by == "jev"
    assert "폴백 실패" in run.steps[0].note
    assert run.completed


@requires_chromium
async def test_local_base_url_never_uses_jev(monkeypatch):
    chat = FakeChat(['{"action":"finish","reason":"x"}'])
    dec = FakeDecisions([("click", 0.99)])
    run, _t, _v = await _run(monkeypatch, cfg=_cfg(base="http://127.0.0.1:8091/v1"),
                             chat=chat, decisions=dec, steps=2)
    assert dec.asked == [] and not dec.started, "로컬 모델 작업에서 페이지 내용이 Jev로 가면 안 된다"
    assert run.steps[0].decided_by == "llm"
    assert "model" not in chat.calls[0] or chat.calls[0]["model"] is None


@requires_chromium
async def test_default_decider_is_llm(monkeypatch):
    chat = FakeChat(['{"action":"finish","reason":"x"}'])
    dec = FakeDecisions([])
    run, _t, _v = await _run(monkeypatch, cfg=_cfg(decider="llm"), chat=chat, decisions=dec, steps=2)
    assert dec.asked == [] and run.steps[0].decided_by == "llm"


def test_summary_shows_typed_value_and_key():
    s = StepOutcome(step=1, decision=Decision(action="type_text", element_id="@e1", text="buy milk"),
                    judged_success=True)
    assert s.summary() == "type_text @e1 -> OK ('buy milk' 입력)"
    k = StepOutcome(step=2, decision=Decision(action="press_key", key="Enter"), judged_success=True)
    assert k.summary() == "press_key -> OK (키 Enter)"


def test_config_reads_decider_and_fallback_from_env(tmp_path, monkeypatch):
    from llm import load_config

    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-or-v1-" + "x" * 40 + "\nAGENT_DECIDER=jev\n", encoding="utf-8")
    monkeypatch.delenv("AGENT_DECIDER", raising=False)
    monkeypatch.delenv("AGENT_FALLBACK_MODEL", raising=False)
    cfg = load_config(env)
    assert cfg.decider == "jev" and cfg.fallback_model == "qwen/qwen3.8-27b"
    assert load_config(tmp_path / "none.env").decider == "llm"
