"""루프 수준: Enter 다음 스텝의 관찰이 결과 페이지인지 (WS-25 h).

실측(G마켓, v1.5.0): press_key Enter 직후 루프가 아직 떠나는 중인 홈 화면을 관찰하고
판단했다. 가짜 LLM 이 Enter 다음 스텝에서 받는 프롬프트의 페이지 제목이 결과 페이지여야 한다.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent import loop as loop_mod
from agent.loop import AgentLoop
from browser.challenge import Challenge
from actions import ActionDispatcher, DispatchContext
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

HOME = ("<!doctype html><meta charset=utf-8><title>쇼핑 홈</title>"
        "<form action='/result'><input id=q name=q aria-label='검색어' autofocus></form>")
RESULT = "<!doctype html><meta charset=utf-8><title>검색 결과</title><p>상품 1 10,000원</p>"


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/result"):
                # 1.5초 — 감지 창(0.3초) 뒤 관찰이 문서 교체와 겹치지 않고 홈을 그대로 보게
                # 해서, 대기가 없으면 루프의 관찰 실패 재시도(_settle)로 가려지지 않게 한다.
                time.sleep(1.5)
                body = RESULT
            else:
                body = HOME
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


class RecordingChat:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.prompts = []

    async def __aenter__(self):
        return self

    async def close(self):
        return None

    async def complete(self, messages, **kw):
        self.prompts.append("\n".join(str(m.get("content", "")) for m in messages))
        return LLMResponse(content=self.bodies.pop(0), model="m",
                           prompt_tokens=10, completion_tokens=5, cost_usd=0.001)


@requires_chromium
async def test_step_after_enter_observes_result_page(server, monkeypatch):
    from playwright.async_api import async_playwright

    chat = RecordingChat([
        '{"action":"press_key","key":"Enter","reason":"검색 실행"}',
        '{"action":"finish","reason":"결과가 보인다"}',
    ])
    monkeypatch.setattr(loop_mod, "OpenRouterClient", lambda *a, **k: chat)

    # 차단 감지 probe(page.evaluate)는 이동 중이면 새 문서까지 붙잡혀 우연히 기다려
    # 주기도 한다(측정: 0.002초 또는 1.5초, 경쟁). 그 우연에 기대지 않도록 이 테스트는
    # probe 를 비워 관찰 시점만 본다 — 대기는 디스패처가 해야 한다.
    async def _no_challenge(page, *, last_status=None):
        return Challenge()

    monkeypatch.setattr(loop_mod, "detect_challenge", _no_challenge)

    # 관찰이 떠나는 중인 문서를 건드려 실패하면 루프는 _settle 로 기다렸다 다시 본다.
    # 그 안전망이 결과를 가리지 않도록, 이번 흐름에서는 한 번도 불리지 않아야 한다.
    settles = []
    orig_settle = AgentLoop._settle

    async def _spy_settle(self):
        settles.append(1)
        await orig_settle(self)

    monkeypatch.setattr(AgentLoop, "_settle", _spy_settle)
    cfg = LLMConfig(api_key="sk-or-v1-" + "k" * 40, model="z-ai/glm-5.3-flash",
                    base_url="https://openrouter.ai/api/v1", decider="llm",
                    fallback_model="qwen/qwen3.8-27b")
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.goto(server + "/")
        await page.focus("#q")
        await page.keyboard.type("키보드")
        engine = PerceptionEngine()
        disp = ActionDispatcher(DispatchContext(page=page, engine=engine))
        await AgentLoop(page=page, engine=engine, dispatcher=disp, config=cfg,
                        max_steps=3).run("키보드를 검색해 줘")
        await b.close()
    assert len(chat.prompts) >= 2, chat.prompts
    assert "페이지 제목: 쇼핑 홈" in chat.prompts[0]
    assert "페이지 제목: 검색 결과" in chat.prompts[1], chat.prompts[1][:600]
    assert settles == [], "Enter 뒤 관찰이 이동 중인 문서를 봤다(관찰 실패 → _settle)"
