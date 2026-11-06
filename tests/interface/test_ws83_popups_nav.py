"""WS-83 (#162·#138) — 팝업 닫힘 복귀·이름 창 재사용·opener 변경·팝업 신호·다른 탭 차단 오귀속·JS 이동 차단 분류.

리뷰(.hermes/plans/ws83-evidence/review.md) F2·F3·F4·F6·F7·F8 probe 를 저장소 시험으로 옮긴 것. 실제
BrowserMCPServer.call_tool 경로, 로컬 http.server 하나(localhost 허용 / 127.0.0.1 비허용)만 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

import pytest

from browser.workspace import workspace_scope
from interface.mcp_server import BrowserMCPServer, envelope_dict

from test_run_cli import requires_chromium  # noqa: F401

OPENER = """<title>Opener</title><h1>OPENER</h1>
<input id="f" aria-label="date field" value="">
<button onclick="window.open('/pop','_blank','width=400,height=300')">sized popup</button>
<a href="/pop" target="_blank">blank link</a>
<button onclick="window.open('/pop?a','cal','width=300,height=300')">named A</button>
<button onclick="window.open('/pop?b','cal','width=300,height=300')">named B</button>
<a href="{denied}" target="_blank">denied blank link</a>
<button onclick="window.open('{denied}','cal','width=300,height=300')">named denied</button>
<button onclick="window.open('{doc}/loop','cal')">named loop</button>
<button onclick="window.open('{dead}','cal')">named dead</button>
<button id="noop">plain noop</button>"""
POP = """<title>Pop</title><h1>POPUP</h1>
<button onclick="opener.document.getElementById('f').value='2026-12-01';window.close()">fill and close</button>
<button onclick="opener.location.href='/changed'">change opener</button>
<button onclick="setTimeout(()=>window.close(),400)">close later</button>
<button onclick="window.open('/pop','_blank','width=200,height=200')">nested popup</button>
<button onclick="opener.location.href='/changed';window.close()">change opener and close</button>
<button onclick="window.close()">just close</button>
<button onclick="opener.location.href='{denied}'">denied opener</button>
<button onclick="opener.location.href='{denied}';window.close()">denied opener and close</button>
<button onclick="opener.location.href='{doc}/loop'">loop opener</button>
<button onclick="opener.location.href='{doc}/loop';window.close()">loop opener and close</button>
<button onclick="opener.location.href='{dead}'">dead opener</button>
<button onclick="opener.location.href='{dead}';window.close()">dead opener and close</button>
<button id="noop">pop noop</button>"""

STATE: Dict[str, Any] = {"trigger": threading.Event(), "hits": []}


def _page(path: str, urls: Dict[str, str]) -> bytes:
    denied = urls["denied"]
    doc = urls["doc"]
    if path == "/robots.txt":
        return b"User-agent: *\nAllow: /\n"
    if path == "/opener":
        return OPENER.format(denied=denied, doc=doc, dead=urls["dead"]).encode()
    if path == "/pop":
        return POP.replace("{denied}", denied).replace("{doc}", doc).replace("{dead}", urls["dead"]).encode()
    if path == "/changed":
        return b"<title>Changed</title><h1>CHANGED_BY_POPUP</h1>"
    if path == "/bg":
        return (b"<h1>BACKGROUND</h1><script>let t=setInterval(async()=>{if(await(await fetch('/trigger')).text()"
                b"==='go'){clearInterval(t);location.href='/redirect'}},25)</script>")
    if path == "/bgnav":
        # 배경 탭이 스스로 계속 이동(자발 이동, 60ms 마다) — 효과 없는 click 의 효과로 세면 안 된다.
        return b"<h1>BGNAV</h1><script>setTimeout(()=>{location.href='/bgnav?'+Date.now()},60)</script>"
    if path == "/trigger":
        return b"go" if STATE["trigger"].is_set() else b"wait"
    if path == "/slow":
        STATE["trigger"].set()
        time.sleep(.7)
        return b"<h1>ACTIVE_HEALTHY</h1><p>HTML_COMPLETE</p>"
    if path == "/js":
        return ("<script>location.href=" + json.dumps(denied) + "</script><h1>JS_SOURCE</h1>").encode()
    if path == "/meta":
        return ('<meta http-equiv="refresh" content="0;url=' + denied + '"><h1>META_SOURCE</h1>').encode()
    if path == "/js-bounce":
        return ("<script>location.href=" + json.dumps(doc + "/redirect") + "</script><h1>JS</h1>").encode()
    if path == "/js-local":
        return ("<script>location.href=" + json.dumps(doc + "/good") + "</script><h1>JS</h1>").encode()
    if path == "/meta-local":
        return ('<meta http-equiv="refresh" content="0;url=' + doc + '/good"><h1>META</h1>').encode()
    if path == "/img-then-bad":
        # 하위 자원(이미지)만 막힌 뒤 문서가 egress 와 무관한 이유(리다이렉트 무한 반복)로 오류 문서가 된다.
        return ('<img src="' + denied + '/img.png"><script>location.href=' + json.dumps(doc + "/loop")
                + '</script><h1>IMG_THEN_BAD</h1>').encode()
    if path == "/stream-img":
        return ('<title>Healthy</title><h1>HEALTHY</h1><img src="' + denied + '/img.png"><p>HTML_COMPLETE</p>').encode()
    return b"<title>Good</title><h1>GOOD_DOCUMENT</h1><p>HTML_COMPLETE</p>"


@pytest.fixture
def site():
    urls: Dict[str, str] = {}
    STATE["trigger"].clear()
    STATE["hits"].clear()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            STATE["hits"].append(self.path)
            path = self.path.split("?", 1)[0]
            try:
                if path == "/loop":
                    self.send_response(302)
                    self.send_header("Location", urls["doc"] + "/loop")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", urls["denied"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = _page(path, urls)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()  # 닫힌 허용 호스트 포트 — 프록시가 connect_failed 문서를 만든다(R2)
    urls.update(doc=f"http://localhost:{port}", denied=f"http://127.0.0.1:{port}/denied",
                dead=f"http://localhost:{dead_port}/unreachable")
    try:
        yield urls
    finally:
        srv.shutdown()
        srv.server_close()
    assert not [h for h in STATE["hits"] if h.startswith("/denied")], STATE["hits"]


def _server(**kw: Any) -> BrowserMCPServer:
    return BrowserMCPServer(recipes=False, headless_retry=False, allowed_domains=("localhost",), **kw)


async def call(s: BrowserMCPServer, name: str, args: Optional[Dict[str, Any]] = None,
               ws: Optional[str] = None) -> Dict[str, Any]:
    args = dict(args or {})
    if ws is not None:
        args["workspace"] = ws
    return envelope_dict(await s.call_tool("browser_" + name, args))


async def ids(s: BrowserMCPServer, ws: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    o = await call(s, "observe_page", {}, ws)
    ob = o["data"]["observation"]
    return {e["name"]: {"element_id": e["element_id"], "epoch": ob["snapshot_epoch"]} for e in ob["elements"]}


async def click(s: BrowserMCPServer, name: str, ws: Optional[str] = None) -> Dict[str, Any]:
    m = await ids(s, ws)
    return await call(s, "click", m[name], ws)


async def tabs(s: BrowserMCPServer, ws: Optional[str] = None) -> Dict[str, Any]:
    return (await call(s, "tab_control", {"command": "list"}, ws))["data"]


async def open_popup_and_switch(s: BrowserMCPServer, ws: Optional[str] = None) -> str:
    r = await click(s, "sized popup", ws)
    assert r["success"], r
    pop = r["data"]["opened_tab_ids"][-1]
    sw = await call(s, "tab_control", {"command": "switch", "tab_id": pop}, ws)
    assert sw["success"], sw
    return pop


def _field_value(o: Dict[str, Any]) -> Any:
    return [e.get("value") for e in o["data"]["observation"]["elements"] if e.get("name") == "date field"]


# ------------------------------------------------------------------ B4: 팝업이 스스로 닫힘 → opener 복귀


@requires_chromium
@pytest.mark.parametrize("ws", [None, "w2"])
async def test_popup_self_close_returns_to_opener_and_click_succeeds(site, ws):
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["doc"] + "/opener"}, ws)
        opener = nav["tab_id"]
        pop = await open_popup_and_switch(s, ws)
        r = await click(s, "fill and close", ws)
        assert r["success"] is True, r
        assert r.get("error_code") is None
        assert r["reobserve_required"] is True
        assert r["data"]["tab_closed"] == {"closed_tab_id": pop, "active_tab_id": opener}
        assert r["tab_id"] == opener
        assert (await tabs(s, ws))["active"] == opener
        o = await call(s, "observe_page", {}, ws)
        assert o["success"] and o["tab_id"] == opener, o
        assert _field_value(o) == ["2026-12-01"]
        n = await call(s, "navigate", {"url": site["doc"] + "/good"}, ws)
        assert n["success"] and n["tab_id"] == opener, n


@requires_chromium
async def test_popup_close_returns_to_its_opener_not_the_first_tab(site):
    async with _server() as s:
        await call(s, "navigate", {"url": site["doc"] + "/opener"})
        created = await call(s, "tab_control", {"command": "create", "url": site["doc"] + "/opener"})
        second = created["data"]["tab_id"]
        pop = await open_popup_and_switch(s)
        r = await click(s, "fill and close")
        assert r["success"] is True, (r.get("error_code"), r.get("error_message"), r["data"])
        assert r["data"]["tab_closed"] == {"closed_tab_id": pop, "active_tab_id": second}
        assert (await tabs(s))["active"] == second
        o = await call(s, "observe_page", {})
        assert o["tab_id"] == second and _field_value(o) == ["2026-12-01"]


@requires_chromium
@pytest.mark.parametrize("ws", [None, "w2"])
async def test_popup_closing_after_the_call_is_recovered_before_the_next_call(site, ws):
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["doc"] + "/opener"}, ws)
        opener = nav["tab_id"]
        pop = await open_popup_and_switch(s, ws)
        await click(s, "close later", ws)
        await asyncio.sleep(0.8)
        o = await call(s, "observe_page", {}, ws)
        assert o["success"] is True, o
        assert o["tab_id"] == opener
        assert o["data"]["tab_closed"] == {"closed_tab_id": pop, "active_tab_id": opener}
        again = await call(s, "observe_page", {}, ws)
        assert "tab_closed" not in again["data"]  # 한 번만 알린다


@requires_chromium
async def test_popup_chain_middle_closed_returns_to_grandparent(site):
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["doc"] + "/opener"})
        root = nav["tab_id"]
        middle = await open_popup_and_switch(s)
        r = await click(s, "nested popup")
        leaf = r["data"]["opened_tab_ids"][-1]
        # 가운데 팝업을 (사이트가) 닫는다 — 활성이 아니므로 활성은 그대로.
        page = s._core.get_tab(middle).page
        await page.evaluate("window.close()")
        await asyncio.sleep(0.3)
        await call(s, "tab_control", {"command": "switch", "tab_id": leaf})
        r = await click(s, "just close")
        # 잎 팝업의 opener(가운데)는 이미 닫혔다 → 사슬을 따라 할아버지로.
        assert r["data"]["tab_closed"] == {"closed_tab_id": leaf, "active_tab_id": root}, r
        assert r["success"] is True


@requires_chromium
async def test_opener_closed_first_then_popup_closes_uses_existing_rule(site):
    async with _server() as s:
        await call(s, "navigate", {"url": site["doc"] + "/opener"})
        created = await call(s, "tab_control", {"command": "create", "url": site["doc"] + "/opener"})
        second = created["data"]["tab_id"]
        pop = await open_popup_and_switch(s)
        await s._core.get_tab(second).page.close()
        await asyncio.sleep(0.2)
        r = await click(s, "just close")
        first = (await tabs(s))["tabs"][0]["tab_id"]
        assert r["data"]["tab_closed"] == {"closed_tab_id": pop, "active_tab_id": first}, r


@requires_chromium
async def test_popup_closed_in_another_workspace_leaves_my_active_tab(site):
    async with _server() as s:
        mine = await call(s, "navigate", {"url": site["doc"] + "/opener"})
        await call(s, "navigate", {"url": site["doc"] + "/opener"}, "w2")
        await open_popup_and_switch(s, "w2")
        r = await click(s, "fill and close", "w2")
        assert r["success"] and "tab_closed" in r["data"]
        o = await call(s, "observe_page", {})
        assert o["success"] and o["tab_id"] == mine["tab_id"] and "tab_closed" not in o["data"]
        assert _field_value(o) == [""]  # 남의 팝업은 내 칸을 채우지 않았다


@requires_chromium
async def test_popup_changes_opener_then_closes_reports_both(site):
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["doc"] + "/opener"})
        pop = await open_popup_and_switch(s)
        r = await click(s, "change opener and close")
        assert r["success"] is True, r
        assert r["data"]["tab_closed"] == {"closed_tab_id": pop, "active_tab_id": nav["tab_id"]}
        await asyncio.sleep(0.3)
        o = await call(s, "observe_page", {})
        assert o["current_url"].endswith("/changed"), o


# ------------------------------------------------------------------ D3/B6: opener 관계 탭의 변화


@requires_chromium
async def test_named_window_reuse_is_a_click_effect(site):
    async with _server() as s:
        await call(s, "navigate", {"url": site["doc"] + "/opener"})
        a = await click(s, "named A")
        pop = a["data"]["opened_tab_ids"][0]
        b = await click(s, "named B")
        assert b["success"] is True, b
        assert b["data"]["tabs_changed"] == [{"tab_id": pop, "change": "navigated"}]
        assert "opened_tab_ids" not in b["data"]
        assert (await tabs(s))["active"] == a["tab_id"]


@requires_chromium
async def test_popup_changing_opener_is_a_click_effect(site):
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["doc"] + "/opener"})
        await open_popup_and_switch(s)
        r = await click(s, "change opener")
        assert r["success"] is True, r
        assert r["data"]["tabs_changed"] == [{"tab_id": nav["tab_id"], "change": "navigated"}]
        dumped = json.dumps(r["data"]["tabs_changed"])
        assert "/changed" not in dumped and "localhost" not in dumped


@requires_chromium
async def test_unrelated_background_tab_navigation_is_not_my_click_effect(site):
    async with _server() as s:
        await call(s, "navigate", {"url": site["doc"] + "/bgnav"})
        await call(s, "tab_control", {"command": "create", "url": site["doc"] + "/opener"})
        m = await ids(s)
        r = await call(s, "click", m["plain noop"])
        assert r["success"] is False and r["error_code"] == "E_TIMEOUT", r
        assert "tabs_changed" not in r["data"]


# ------------------------------------------------------------------ D1/B5: 팝업 신호(전환 없음)


@requires_chromium
async def test_popup_opened_signal_without_switching(site):
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["doc"] + "/opener"})
        r = await click(s, "blank link")
        assert r["success"] is True
        pop = r["data"]["opened_tab_ids"]
        assert len(pop) == 1
        assert [p["tab_id"] for p in r["data"]["popup_opened"]] == pop
        assert r["data"]["popup_opened"][0]["opener_tab_id"] == nav["tab_id"]
        assert set(r["data"]["popup_opened"][0]) == {"tab_id", "opener_tab_id", "url"}
        assert r["tab_id"] == nav["tab_id"]
        assert (await tabs(s))["active"] == nav["tab_id"]


# ------------------------------------------------------------------ B2: #138(1) 다른 탭 차단 오귀속


@requires_chromium
async def test_background_tab_block_is_not_my_navigate_failure(site):
    for _ in range(2):
        STATE["trigger"].clear()
        async with _server() as s:
            assert (await call(s, "navigate", {"url": site["doc"] + "/bg"}))["success"]
            assert (await call(s, "tab_control", {"command": "create", "url": site["doc"] + "/good"}))["success"]
            r = await call(s, "navigate", {"url": site["doc"] + "/slow", "wait_until": "load"})
            assert r["success"] is True, r
            assert "egress" not in r["data"]


# ------------------------------------------------------------------ B1: #138(2) JS 이동·meta refresh 차단 분류


@requires_chromium
@pytest.mark.parametrize("wait", ["load", "domcontentloaded"])
@pytest.mark.parametrize("path", ["/js", "/meta"])
async def test_script_navigation_block_is_classified_like_redirect(site, path, wait):
    async with _server() as s:
        r = await call(s, "navigate", {"url": site["doc"] + path, "wait_until": wait, "timeout_ms": 5000})
        assert r["success"] is False, r
        assert r["error_code"] == "E_INVALID_URL", r
        assert r["data"]["egress"]["host"] == "127.0.0.1"
        assert r["data"]["egress"]["code"] == "egress_blocked"
        assert r["data"]["nav_failure"]["kind"] == "blocked", r


@requires_chromium
async def test_navigation_controls_keep_their_results(site):
    async with _server() as s:
        for path in ("/good", "/js-local", "/meta-local", "/stream-img", "/good"):
            r = await call(s, "navigate", {"url": site["doc"] + path, "wait_until": "load", "timeout_ms": 5000})
            assert r["success"] is True, (path, r)
            assert "egress" not in r["data"], (path, r)
        for path in ("/redirect", "/js-bounce"):
            r = await call(s, "navigate", {"url": site["doc"] + path, "wait_until": "load", "timeout_ms": 5000})
            assert r["success"] is False and r["error_code"] == "E_INVALID_URL", (path, r)
            assert r["data"]["egress"]["host"] == "127.0.0.1"


@requires_chromium
async def test_subresource_block_is_not_a_document_block_even_if_the_document_fails(site):
    """B1 사보타주 표지: 하위 자원 차단 기록 + egress 와 무관한 오류 문서 → egress 분류가 아니다(main_document 조건)."""
    async with _server() as s:
        r = await call(s, "navigate", {"url": site["doc"] + "/img-then-bad", "wait_until": "load", "timeout_ms": 5000})
        assert r["success"] is False, r
        assert r["error_code"] != "E_INVALID_URL", r
        assert "egress" not in r["data"], r


# ------------------------------------------------------------------ B3: 팝업 첫 문서 차단


@requires_chromium
async def test_blocked_popup_first_document_is_reported_on_the_click(site):
    async with _server() as s:
        await call(s, "navigate", {"url": site["doc"] + "/opener"})
        r = await click(s, "denied blank link")
        assert r["success"] is True, r
        assert r["data"]["opened_tab_ids"]
        assert r["data"]["egress"]["host"] == "127.0.0.1", r


# ------------------------------------------------------------------ 적대 사례: batch·동시 호출


@requires_chromium
async def test_batch_stops_after_popup_closes_itself(site):
    async with _server() as s:
        await call(s, "navigate", {"url": site["doc"] + "/opener"})
        await open_popup_and_switch(s)
        out = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "click", "target": {"role": "button", "name": "fill and close"}},
            {"action": "click", "target": {"role": "button", "name": "pop noop"}},
        ]})
        batch = out["data"]["batch"]
        assert batch["stopped_at"] is not None and batch["ran"] <= 1, out
        o = await call(s, "observe_page", {})
        assert o["success"] and _field_value(o) == ["2026-12-01"]


@requires_chromium
async def test_concurrent_call_while_popup_closes_is_not_stuck(site):
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["doc"] + "/opener"})
        await open_popup_and_switch(s)
        m = await ids(s)
        r1, r2 = await asyncio.gather(call(s, "click", m["fill and close"]), call(s, "observe_page", {}))
        assert r1["success"] is True, r1
        o = await call(s, "observe_page", {})
        assert o["success"] and o["tab_id"] == nav["tab_id"]


# ------------------------------------------------------------------ R1 B1: 관계 탭 차단은 성공으로 승격하지 않는다


@requires_chromium
@pytest.mark.parametrize("button", ["denied opener and close", "denied opener"])
async def test_popup_sending_opener_to_denied_host_is_not_a_click_success(site, button):
    """팝업이 opener 를 비허용 주소로 보낸 뒤 닫힘(또는 유지) — 닫힘 복귀·관계 탭 효과로 성공 승격 금지, E_INVALID_URL + egress."""
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["doc"] + "/opener"})
        pop = await open_popup_and_switch(s)
        r = await click(s, button)
        assert r["success"] is False, r
        assert r["error_code"] == "E_INVALID_URL", r
        assert r["data"]["egress"]["host"] == "127.0.0.1" and r["data"]["egress"]["code"] == "egress_blocked", r
        assert r["retry_safe"] is False and r["reobserve_required"] is True, r
        assert r["data"]["tabs_changed"] == [{"tab_id": nav["tab_id"], "change": "navigated"}], r
        if button.endswith("close"):
            assert r["data"]["tab_closed"] == {"closed_tab_id": pop, "active_tab_id": nav["tab_id"]}, r
            assert r["tab_id"] == nav["tab_id"]


@requires_chromium
async def test_named_window_reuse_to_denied_host_is_not_a_click_success(site):
    """형제 경로: 이름 있는 창 재사용이 그 창을 비허용 주소로 보냄 → E_INVALID_URL + egress(관계 탭 효과로 성공 승격 금지)."""
    async with _server() as s:
        await call(s, "navigate", {"url": site["doc"] + "/opener"})
        a = await click(s, "named A")
        pop = a["data"]["opened_tab_ids"][0]
        r = await click(s, "named denied")
        assert r["success"] is False and r["error_code"] == "E_INVALID_URL", r
        assert r["data"]["egress"]["host"] == "127.0.0.1", r
        assert r["data"]["tabs_changed"] == [{"tab_id": pop, "change": "navigated"}], r
        assert (await tabs(s))["active"] == a["tab_id"]


# ------------------------------------------------------------------ R1 NB2: batch 는 관계 탭이 바뀐 단계 뒤 멈춘다


@requires_chromium
async def test_batch_stops_after_click_changes_a_related_tab(site):
    async with _server() as s:
        await call(s, "navigate", {"url": site["doc"] + "/opener"})
        a = await click(s, "named A")
        pop = a["data"]["opened_tab_ids"][0]
        out = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "click", "target": {"role": "button", "name": "named B"}},
            {"action": "click", "target": {"role": "button", "name": "plain noop"}},
        ]})
        batch = out["data"]["batch"]
        assert batch["stopped_at"] == 0 and batch["ran"] == 1 and batch["reason"] == "navigated", out
        step = out["data"]["step_result"]
        assert step["success"] is True, step
        assert step["data"]["tabs_changed"] == [{"tab_id": pop, "change": "navigated"}], step


# ------------------------------------------------------------------ R2 (R1-B1): 차단 기록 없는 관계 탭 오류 문서


_LOOP_FAILURE = {"kind": "other", "net_error": "ERR_TOO_MANY_REDIRECTS"}


@requires_chromium
@pytest.mark.parametrize("target", ["loop", "dead"])
@pytest.mark.parametrize("route", ["opener", "opener and close", "named"])
async def test_related_tab_error_document_is_not_a_click_success(site, target, route):
    """관계 탭(opener 이동·닫힘 복귀·이름 창 재사용)이 egress 차단 없이 오류 문서(리다이렉트 고리 chrome-error·연결 거부
    프록시 connect_failed 문서)로 끝나면 그 효과로 성공 승격 금지 — 기준(6f435cc)과 같은 계열(E_TIMEOUT·닫힘 E_PAGE_CRASHED)."""
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["doc"] + "/opener"})
        if route == "named":
            a = await click(s, "named A")
            rel = a["data"]["opened_tab_ids"][0]
            r = await click(s, f"named {target}")
        else:
            pop = await open_popup_and_switch(s)
            rel = nav["tab_id"]
            r = await click(s, f"{target} {route}")
        assert r["success"] is False, r
        assert r["retry_safe"] is False and r["reobserve_required"] is True, r
        assert r["data"]["tabs_changed"] == [{"tab_id": rel, "change": "navigated"}], r
        assert r["data"]["nav_failure"] == (_LOOP_FAILURE if target == "loop" else {"kind": "connect"}), r
        if target == "dead":
            assert r["data"]["egress"]["code"] == "connect_failed", r
        else:
            assert "egress" not in r["data"], r
        if route == "opener and close":
            assert r["error_code"] == "E_PAGE_CRASHED", r
            assert r["data"]["tab_closed"] == {"closed_tab_id": pop, "active_tab_id": nav["tab_id"]}, r
            assert r["tab_id"] == nav["tab_id"]
            o = await call(s, "observe_page", {})  # 닫힌 탭에 갇히지 않는다
            assert o["success"] is True and o["tab_id"] == nav["tab_id"], o
        else:
            assert r["error_code"] == "E_TIMEOUT", r
            assert "tab_closed" not in r["data"], r


@requires_chromium
@pytest.mark.parametrize("button", ["change opener", "change opener and close", "named B"])
async def test_related_tab_healthy_navigation_stays_a_click_success(site, button):
    """대조: 같은 관계 경로의 허용·정상 문서 이동은 성공 그대로(오류 문서 판정이 정상 문서를 오류로 보지 않는다)."""
    async with _server() as s:
        await call(s, "navigate", {"url": site["doc"] + "/opener"})
        if button == "named B":
            await click(s, "named A")
        else:
            await open_popup_and_switch(s)
        r = await click(s, button)
        assert r["success"] is True and r.get("error_code") is None, r
        assert "nav_failure" not in r["data"] and "egress" not in r["data"], r
