"""AgentLoop 차단·캡차 감지 → 사람 인계 (WS-22).

- 차단/캡차 화면에서 on_challenge가 없으면 LLM을 한 번도 부르지 않고 끝낸다
  (실측: 네이버·쿠팡 차단 화면에서 scroll/read_text를 7~9번 헛돌다 포기했다).
- on_challenge가 True(사람이 해결)면 다시 보고, 정상이면 같은 목표로 계속한다.
- 여전히 차단이면 끝낸다. 정상 페이지에서는 on_challenge를 부르지 않는다.
"""

from __future__ import annotations

import pytest

from agent import loop as loop_mod
from agent.loop import AgentLoop
from actions import ActionDispatcher, DispatchContext
from contracts import ErrorCode
from llm import LLMConfig
from llm.client import LLMResponse
from perception import PerceptionEngine


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

BLOCKED = """<!doctype html><meta charset=utf-8><title>네이버쇼핑</title>
<h2>쇼핑 서비스 접속이 일시적으로 제한되었습니다.</h2><a href='#x'>고객센터</a>"""

NORMAL = """<!doctype html><meta charset=utf-8><title>검색</title>
<input aria-label='검색어' id=q><button>검색</button><p>상품 목록</p>"""


def _cfg(decider="llm"):
    return LLMConfig(api_key="sk-or-v1-" + "k" * 40, model="z-ai/glm-5.3-flash",
                     base_url="https://openrouter.ai/api/v1", decider=decider,
                     fallback_model="qwen/qwen3.8-27b")


class FakeChat:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def close(self):
        return None

    async def complete(self, messages, **kw):
        self.calls += 1
        return LLMResponse(content=self.bodies.pop(0), model="m",
                           prompt_tokens=10, completion_tokens=5, cost_usd=0.001)


class FakeDecisions:
    def __init__(self):
        self.asked = []

    async def start(self):
        return self

    async def close(self):
        return None

    async def ask(self, state, name, question):
        self.asked.append(name)
        raise AssertionError("차단 화면에서 Jev를 부르면 안 된다")


async def _run(monkeypatch, *, html, chat, on_challenge=None, cfg=None, decisions=None):
    from playwright.async_api import async_playwright

    monkeypatch.setattr(loop_mod, "OpenRouterClient", lambda *a, **k: chat)
    if decisions is not None:
        monkeypatch.setattr(loop_mod, "DecisionsClient", lambda *a, **k: decisions)
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.set_content(html)
        engine = PerceptionEngine()
        disp = ActionDispatcher(DispatchContext(page=page, engine=engine,
                                                cdp=await ctx.new_cdp_session(page)))
        hook = on_challenge(page) if on_challenge else None
        run = await AgentLoop(page=page, engine=engine, dispatcher=disp, config=cfg or _cfg(),
                              max_steps=4, on_challenge=hook).run("상품을 찾아 줘")
        await b.close()
    return run


@requires_chromium
async def test_blocked_page_without_handler_stops_without_llm(monkeypatch):
    chat = FakeChat([])
    run = await _run(monkeypatch, html=BLOCKED, chat=chat)
    assert chat.calls == 0, "차단 화면에서 LLM을 부르면 안 된다(헛돌기)"
    assert not run.completed
    assert run.terminal_reason.startswith(ErrorCode.CAPTCHA_DETECTED.value), run.terminal_reason
    assert "blocked" in run.terminal_reason
    assert run.challenge == "blocked"
    assert run.steps == []
    assert run.to_dict()["challenge"] == "blocked"


@requires_chromium
async def test_blocked_page_with_jev_never_asks_jev(monkeypatch):
    chat, dec = FakeChat([]), FakeDecisions()
    run = await _run(monkeypatch, html=BLOCKED, chat=chat, cfg=_cfg("jev"), decisions=dec)
    assert chat.calls == 0 and dec.asked == []
    assert run.terminal_reason.startswith(ErrorCode.CAPTCHA_DETECTED.value)


@requires_chromium
async def test_handler_resolves_then_loop_continues(monkeypatch):
    chat = FakeChat(['{"action":"finish","reason":"상품 목록이 보인다"}'])
    seen = []

    def make(page):
        async def on_challenge(ch):
            seen.append(ch)
            await page.set_content(NORMAL)   # 사람이 해결한 셈
            return True
        return on_challenge

    run = await _run(monkeypatch, html=BLOCKED, chat=chat, on_challenge=make)
    assert len(seen) == 1 and seen[0].kind.value == "blocked" and seen[0].vendor == "naver"
    assert run.completed, run.terminal_reason
    assert chat.calls == 1
    assert run.challenge == "blocked", "인계가 있었다는 기록은 남는다"


@requires_chromium
async def test_handler_true_but_still_blocked_stops(monkeypatch):
    chat = FakeChat([])
    calls = []

    def make(page):
        async def on_challenge(ch):
            calls.append(ch)
            return True
        return on_challenge

    run = await _run(monkeypatch, html=BLOCKED, chat=chat, on_challenge=make)
    assert len(calls) == 1
    assert chat.calls == 0 and not run.completed
    assert run.terminal_reason.startswith(ErrorCode.CAPTCHA_DETECTED.value)


@requires_chromium
async def test_handler_false_stops(monkeypatch):
    chat = FakeChat([])

    def make(page):
        async def on_challenge(ch):
            return False
        return on_challenge

    run = await _run(monkeypatch, html=BLOCKED, chat=chat, on_challenge=make)
    assert chat.calls == 0 and not run.completed
    assert run.terminal_reason.startswith(ErrorCode.CAPTCHA_DETECTED.value)


@requires_chromium
async def test_block_status_seen_during_run_stops(monkeypatch):
    """스텝 중 이동한 문서가 418 + 짧은 본문이면(문구 없음) 상태코드로 잡는다."""
    from playwright.async_api import async_playwright

    chat = FakeChat(['{"action":"navigate","url":"http://shop.test/list","reason":"목록"}'])
    monkeypatch.setattr(loop_mod, "OpenRouterClient", lambda *a, **k: chat)
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context()
        await ctx.route("http://shop.test/**", lambda r: r.fulfill(
            status=418, content_type="text/html; charset=utf-8",
            body="<!doctype html><meta charset=utf-8><p>요청을 처리할 수 없습니다.</p>"))
        page = await ctx.new_page()
        await page.set_content(NORMAL)
        engine = PerceptionEngine()
        disp = ActionDispatcher(DispatchContext(page=page, engine=engine,
                                                cdp=await ctx.new_cdp_session(page)))
        run = await AgentLoop(page=page, engine=engine, dispatcher=disp, config=_cfg(),
                              max_steps=4).run("상품을 찾아 줘")
        await b.close()
    assert chat.calls == 1, "이동 뒤 차단 화면에서는 더 부르지 않는다"
    assert run.challenge == "blocked" and "418" in run.terminal_reason, run.terminal_reason


@requires_chromium
async def test_normal_page_never_calls_handler(monkeypatch):
    chat = FakeChat(['{"action":"finish","reason":"보인다"}'])
    calls = []

    def make(page):
        async def on_challenge(ch):
            calls.append(ch)
            return True
        return on_challenge

    run = await _run(monkeypatch, html=NORMAL, chat=chat, on_challenge=make)
    assert calls == [] and run.completed and run.challenge is None
    assert run.to_dict()["challenge"] is None


# ---------------------------------------------------------------- 강제 계속 (R2-1)

# 짧은 정상 공지인데 차단 문구가 그대로 든 페이지 — 판정기는 느슨하게 하지 않으므로
# BLOCKED 로 잡힌다(오탐). 사람이 "강제 계속" 을 지시하면 진행해야 한다.
FALSE_POSITIVE = """<!doctype html><meta charset=utf-8><title>공지</title>
<p>점검 시간에는 접속이 일시적으로 제한될 수 있으며 곧 복구됩니다.</p>
<input aria-label='검색어' id=q><button>검색</button><p>상품 목록</p>"""


def test_normalize_handoff_accepts_bool_and_force():
    from agent.loop import HandoffOutcome, normalize_handoff

    assert normalize_handoff(True) is HandoffOutcome.RESOLVED
    assert normalize_handoff(False) is HandoffOutcome.UNRESOLVED
    assert normalize_handoff(None) is HandoffOutcome.UNRESOLVED
    assert normalize_handoff("force") is HandoffOutcome.FORCE
    assert normalize_handoff(HandoffOutcome.FORCE) is HandoffOutcome.FORCE
    assert normalize_handoff(HandoffOutcome.RESOLVED) is HandoffOutcome.RESOLVED


@requires_chromium
async def test_false_positive_force_continue_calls_llm_and_completes(monkeypatch):
    chat = FakeChat(['{"action":"finish","reason":"상품 목록이 보인다"}'])
    seen = []

    def make(page):
        async def on_challenge(ch):
            seen.append(ch)
            return "force"   # 사람: 오탐이니 그냥 계속
        return on_challenge

    run = await _run(monkeypatch, html=FALSE_POSITIVE, chat=chat, on_challenge=make)
    assert len(seen) == 1 and seen[0].kind.value == "blocked"
    assert chat.calls == 1, "강제 계속이면 LLM 을 불러야 한다"
    assert run.completed, run.terminal_reason
    assert run.challenge == "blocked", "인계가 있었다는 기록은 남는다"


@requires_chromium
async def test_force_continue_same_challenge_not_handed_off_again(monkeypatch):
    chat = FakeChat(['{"action":"read_text","reason":"읽는다"}',
                     '{"action":"read_text","reason":"또 읽는다"}',
                     '{"action":"finish","reason":"상품 목록이 보인다"}'])
    seen = []

    def make(page):
        async def on_challenge(ch):
            seen.append(ch)
            return "force"
        return on_challenge

    run = await _run(monkeypatch, html=FALSE_POSITIVE, chat=chat, on_challenge=make)
    assert len(seen) == 1, f"같은 판정은 다시 인계하지 않는다: {len(seen)}회"
    assert chat.calls == 3 and run.completed, run.terminal_reason


@requires_chromium
async def test_force_continue_then_different_challenge_hands_off_again(monkeypatch):
    from playwright.async_api import async_playwright

    chat = FakeChat(['{"action":"navigate","url":"http://shop.test/check","reason":"이동"}'])
    monkeypatch.setattr(loop_mod, "OpenRouterClient", lambda *a, **k: chat)
    seen = []

    async def on_challenge(ch):
        seen.append(ch)
        return "force" if len(seen) == 1 else False

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context()
        await ctx.route("http://shop.test/**", lambda r: r.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body="<!doctype html><meta charset=utf-8><title>Just a moment...</title><p>잠시만</p>"))
        page = await ctx.new_page()
        await page.set_content(FALSE_POSITIVE)
        engine = PerceptionEngine()
        disp = ActionDispatcher(DispatchContext(page=page, engine=engine,
                                                cdp=await ctx.new_cdp_session(page)))
        run = await AgentLoop(page=page, engine=engine, dispatcher=disp, config=_cfg(),
                              max_steps=4, on_challenge=on_challenge).run("상품을 찾아 줘")
        await b.close()
    assert [c.kind.value for c in seen] == ["blocked", "captcha"], seen
    assert chat.calls == 1 and not run.completed
    assert run.terminal_reason.startswith(ErrorCode.CAPTCHA_DETECTED.value), run.terminal_reason
    assert run.challenge == "captcha"


@requires_chromium
async def test_resolved_signal_on_false_positive_still_rechecks_and_stops(monkeypatch):
    """빈 Enter/빈 파일(=해결했다)은 여전히 재확인한다 — 오탐 화면이면 그대로 멈춘다."""
    chat = FakeChat([])
    calls = []

    def make(page):
        async def on_challenge(ch):
            calls.append(ch)
            return True
        return on_challenge

    run = await _run(monkeypatch, html=FALSE_POSITIVE, chat=chat, on_challenge=make)
    assert len(calls) == 1 and chat.calls == 0 and not run.completed
    assert run.terminal_reason.startswith(ErrorCode.CAPTCHA_DETECTED.value)
