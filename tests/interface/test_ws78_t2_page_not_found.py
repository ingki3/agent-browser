"""WS-78 T2(#139) — 404/410 없는 주소를 '접근 막힘'으로 오해하지 않게 data.page_not_found 고정 신호.

실 Chromium(headless) + 로컬 http(127.0.0.1 만). 리뷰 .hermes/plans/ws78-evidence/review.md B5.

* 판정용 상태(judge_status_for — 현재 URL == 응답 URL)가 404/410 이고 challenge 가 없을 때만.
* CHALLENGE_CHECK_ACTIONS 전체(같은 부착 지점) + browser_batch 응답 관찰.
* 410 문서에서 pushState 로 옮긴 SPA 화면에는 없음. 200·403·429 에는 없음.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browser.serve_profile import PROFILE_ROOT_ENV
from interface.handoff import STATE_ROOT_ENV
from interface.mcp_server import BrowserMCPServer, envelope_json
from interface.server_schema import OBSERVE_INSTRUCTIONS, PAGE_NOT_FOUND_HINT

from test_run_cli import requires_chromium

_NF = ("<!doctype html><title>Not Found</title><h1>404 page</h1><p>" + "x" * 50 + "</p>"
       "<button onclick=\"history.pushState(null,'','/spa-ok')\">go spa</button>")


def _route(path: str):
    if path == "/robots.txt":
        return 200, "User-agent: *\nAllow: /\n"
    if path.startswith("/missing"):
        return 404, _NF
    if path == "/gone":
        return 410, _NF
    if path == "/forbidden":
        return 403, "<!doctype html><title>403</title>Forbidden"
    if path == "/rate":
        return 429, "<!doctype html><title>429</title>Too Many Requests"
    return 200, "<!doctype html><title>home</title><a href=/missing2>broken link</a><input aria-label=q>"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path / "servers"))
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(tmp_path / "profiles"))


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            code, body = _route(self.path.split("?")[0])
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _nf(status):
    return {"status": status, "hint": PAGE_NOT_FOUND_HINT}


@requires_chromium
async def test_404_410_signals_and_pushstate_clears(site):
    async with BrowserMCPServer(headless=True, recipes=False, nav_settle=True) as s:
        r = await s.call_tool("browser_navigate", {"url": site + "/missing"})
        assert r.data.get("page_not_found") == _nf(404) and r.data.get("challenge") is None
        r = await s.call_tool("browser_navigate", {"url": site + "/gone"})
        assert r.data.get("page_not_found") == _nf(410)
        o = await s.call_tool("browser_observe_page", {})
        assert o.data.get("page_not_found") == _nf(410)
        btn = next(e for e in o.data["observation"]["elements"] if e.get("role") == "button")
        c = await s.call_tool("browser_click", {"element_id": btn["element_id"], "epoch": o.snapshot_epoch})
        assert c.current_url.endswith("/spa-ok")
        assert c.data.get("last_http_status") == 410  # 측정 조건: 옛 문서 상태가 그대로 따라온다
        assert "page_not_found" not in c.data, c.data
        assert "page_not_found" not in (await s.call_tool("browser_observe_page", {})).data


@requires_chromium
async def test_click_tab_and_go_back(site):
    async with BrowserMCPServer(headless=True, recipes=False, nav_settle=True) as s:
        home = await s.call_tool("browser_navigate", {"url": site + "/"})
        assert "page_not_found" not in home.data  # 200 은 신호 없음
        o = await s.call_tool("browser_observe_page", {})
        link = next(e for e in o.data["observation"]["elements"] if e.get("role") == "link")
        c = await s.call_tool("browser_click", {"element_id": link["element_id"], "epoch": o.snapshot_epoch})
        assert c.data.get("page_not_found") == _nf(404), c.data
        back = await s.call_tool("browser_go_back", {})
        assert "page_not_found" not in back.data
        t = await s.call_tool("browser_tab_control", {"command": "create", "url": site + "/missing3"})
        assert t.data.get("page_not_found") == _nf(404), t.data


@requires_chromium
async def test_block_statuses_have_no_signal(site):
    async with BrowserMCPServer(headless=True, recipes=False, nav_settle=True) as s:
        for path in ("/forbidden", "/rate"):
            r = await s.call_tool("browser_navigate", {"url": site + path})
            assert "page_not_found" not in r.data, (path, r.data)


@requires_chromium
async def test_batch_observation_carries_signal(site):
    async with BrowserMCPServer(headless=True, recipes=False, nav_settle=True) as s:
        await s.call_tool("browser_navigate", {"url": site + "/missing"})
        out = await s.call_server_tool("browser_batch", {"steps": [{"action": "scroll", "args": {"direction": "down"}}]})
        assert out["data"].get("page_not_found") == _nf(404), json.dumps(out, ensure_ascii=False)[:800]


@requires_chromium
async def test_ok_page_wire_unchanged_shape(site):
    """200 응답에는 새 키가 없다(봉투 바이트 동일성은 ws48 골든이 지킴)."""
    async with BrowserMCPServer(headless=True, recipes=False, nav_settle=True) as s:
        r = await s.call_tool("browser_navigate", {"url": site + "/"})
        assert "page_not_found" not in envelope_json(r)


def test_instructions_mention_page_not_found():
    assert "page_not_found" in OBSERVE_INSTRUCTIONS
