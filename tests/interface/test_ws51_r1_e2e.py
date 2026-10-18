"""WS-51 R1 E2E — 독립 검증 BLOCKING 의 실제 출구(실 Chromium + 로컬 페이지, headless 만).

* B1/B2: 혼합 대소문자 키(`GaTeToKeN`)·`;` 구분자(`page=2;gateToken=`) 주소의 navigate/observe 응답.
* B3: 로컬 robots 본문의 상대 경로 규칙(`Disallow: /guard?gateToken=…`) → `data.robots.rule`,
  `tab_control create about:blank#access_token=…` 의 `current_url`·`data.url`.
* NB-4: 서버 도구 출구(call_server_tool)의 가림을 **창 없이** 확인 — 레시피 재생 중단 응답에 실리는 현재 관찰
  주소(Linux CI 의 DISPLAY 없는 환경에서도 출구 가림 제거 사보타주가 실패해야 한다).

값은 일부러 짧은 합성 값이다(JWT·긴 값이면 값 규칙이 키 규칙의 구멍을 덮는다). 공개 사이트 없음.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List
from urllib.parse import urlsplit

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, envelope_json, tool_name

from test_run_cli import requires_chromium

SHORT = "SYNTH_leak_7"

PAGE = "<!doctype html><meta charset=utf-8><title>lab</title><button>Go</button>"


@pytest.fixture
def lab():
    hits: List[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            if urlsplit(self.path).path == "/robots.txt":
                body, ctype = f"User-agent: *\nDisallow: /guard?gateToken={SHORT}\n".encode(), "text/plain"
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield type("Lab", (), {"base": f"http://127.0.0.1:{srv.server_address[1]}", "hits": hits})
    finally:
        srv.shutdown()
        srv.server_close()


async def _wire(s: BrowserMCPServer, action: ActionType, **args) -> str:
    return envelope_json(await s.call_tool(tool_name(action), args))


@requires_chromium
@pytest.mark.parametrize("tail", [f"?GaTeToKeN={SHORT}", f"?%54%6f%4b%65%4e={SHORT}",
                                  f"?page=2;gateToken={SHORT}", f"#page=2;token={SHORT}"])
async def test_mixed_case_and_semicolon_urls_never_reach_the_agent(lab, tail):
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        nav = await _wire(s, ActionType.NAVIGATE, url=f"{lab.base}/lab{tail}")
        obs = await _wire(s, ActionType.OBSERVE_PAGE)
        assert json.loads(nav)["success"] and json.loads(obs)["success"]
        assert SHORT in s._page.url  # 실제 이동·내부 상태는 원문
        for text in (nav, obs):
            assert SHORT not in text, text[:400]
        assert "[REDACTED]" in json.loads(obs)["data"]["observation"]["url"]


@requires_chromium
async def test_relative_robots_rule_is_masked(lab):
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        texts = [await _wire(s, ActionType.NAVIGATE, url=f"{lab.base}/guard?gateToken={SHORT}")]
        for _ in range(20):
            if "robots" in json.loads(texts[-1]).get("data", {}):
                break
            await asyncio.sleep(0.1)
            texts.append(await _wire(s, ActionType.OBSERVE_PAGE))
        robots = json.loads(texts[-1])["data"].get("robots")
        assert robots and robots.get("disallowed"), texts[-1][:600]  # 원문 주소로 판정 → 금지 규칙에 걸림
        assert robots["rule"].startswith("Disallow: /guard?gateToken=[REDACTED]"), robots
        for text in texts:
            assert SHORT not in text, text[:600]


@requires_chromium
async def test_about_blank_fragment_tab_is_created_but_masked(lab):
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        created = await _wire(s, ActionType.TAB_CONTROL, command="create", url=f"about:blank#access_token={SHORT}")
        data = json.loads(created)
        assert data["success"], created  # 탭 생성 자체는 유지
        assert SHORT not in created, created
        assert data["data"]["url"] == "about:blank#access_token=[REDACTED]"
        listed = await _wire(s, ActionType.TAB_CONTROL, command="list")
        assert SHORT not in listed
        assert any(SHORT in t.page.url for t in s._core.tabs())  # 실제 탭은 원문


# ------------------------------------------------------------------ NB-4 서버 도구 출구(headless)


@requires_chromium
async def test_server_tool_exit_masks_urls_headless():
    """레시피 재생 성공 응답의 `data.current_url`(재생 뒤 페이지 원문 주소) — call_server_tool 출구.

    params 로 받은 토큰을 주소에 넣어 이동하는 레시피를 창 없이 재생한다. 출구의 public_payload 를 빼면
    (사보타주 S5) 이 테스트가 DISPLAY 없이도 실패한다.
    """
    from harness.recipe_mock import RecipeSite

    # 레시피는 비밀 계열 키(gateToken 등)를 params 로 받지 않는다 — 값 규칙(긴 대·소문자·숫자 혼합)으로 가려지는
    # 평범한 키(q)를 쓴다.
    recorded, replayed = "SYNTHrecordedValue0123456789abcXYZ", "SYNTHreplayedValue0123456789abcXYZ"
    with RecipeSite() as site:
        async with BrowserMCPServer(headless=True) as srv:
            nav = await srv.call_tool(tool_name(ActionType.NAVIGATE), {"url": site.url(f"/list?q={recorded}")})
            assert nav.success, nav.error_message
            saved = await srv.call_server_tool("browser_recipe", {"op": "save", "name": "대기열 목록", "last_n": 1,
                                                                  "params": {"gate": recorded}})
            assert saved["success"], saved
            rid = saved["data"]["recipe"]["id"]
            assert (await srv.call_tool(tool_name(ActionType.NAVIGATE), {"url": site.url("/search?q=x")})).success

            raw = await srv._server_tool_payload("browser_recipe", {"op": "run", "id": rid, "params": {"gate": replayed}})
            assert raw["success"], raw
            assert replayed in json.dumps(raw, ensure_ascii=False)  # 조건 성립: 가리기 전 응답엔 원문이 있다
            assert replayed in srv._page.url  # 재생은 원문 주소로 이동
            out = await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid, "params": {"gate": replayed}})
            assert out["success"], out
            text = json.dumps(out, ensure_ascii=False)
            assert replayed not in text and recorded not in text, text[:600]
            assert "q=[REDACTED]" in out["data"]["current_url"]
