"""WS-55 R2 E2E — 민감 칸 판정 시점(clear 전) 고정. 실 Chromium(headless) + 로컬 http(127.0.0.1 만), 합성 값.

검증 B1-R1(.hermes/verify/ws55-r1/report.md): 판정이 `fill("")`(clear) **뒤**라, clear 가 일으킨 첫 input 이벤트에서
페이지가 password 칸의 type 을 text 로 바꾸거나 같은 id 의 text 노드로 교체하면 등록·가림이 모두 우회됐다.

* 판정은 그 입력 액션의 첫 부작용(clear·focus·첫 input 이벤트) **전**에 한 번 — 결과는 액션 끝까지 유지(sticky).
* 보수 규칙(OR): 시작 전·clear 후·입력 후 어느 시점에라도 민감이면 등록 — clear·포커스·입력으로 **새로** password 가
  된 칸도 등록한다.
* 대조군: 일반 검색칸의 평범한 값은 계속 보존(코디네이터 결정 A).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browser.serve_profile import PROFILE_ROOT_ENV
from interface.handoff import STATE_ROOT_ENV
from interface.mcp_server import BrowserMCPServer, envelope_json
from security import secret_registry

from test_run_cli import requires_chromium

_REFLECT = ("document.title=this.value; "
            "history.replaceState(null,'','/land?sortKey='+encodeURIComponent(this.value))")
_PAGES = {
    # 값이 있던 password 칸 — clear 의 input 이벤트가 type 을 text 로 바꾸고 값을 주소·제목에 반사한다.
    "/flip": ("<!doctype html><title>clean</title><label>Password<input id=entry type=password value=old "
              f"oninput=\"this.type='text'; {_REFLECT}\"></label>"),
    # 값이 있던 password 칸 — clear 의 input 이벤트가 같은 id 의 text 노드로 교체, 새 노드가 입력값을 반사한다.
    "/replace": ("<!doctype html><title>clean</title><label>Entry<input id=entry type=password value=old></label>"
                 "<script>entry.oninput=function(){if(this.value===''){const n=document.createElement('input');"
                 "n.id='entry';n.type='text';n.setAttribute('oninput'," + json.dumps(_REFLECT) + ");"
                 "this.replaceWith(n);window.didReplace=true}}</script>"),
    # 포커스(clear)로 text → password 가 되는 칸(OR 규칙 — clear 후 판정).
    "/onfocus": ("<!doctype html><title>clean</title><label>Entry<input id=entry "
                 f"onfocus=\"this.type='password'\" oninput=\"{_REFLECT}\"></label>"),
    # 입력 중(첫 문자) text → password 가 되는 칸(OR 규칙 — 입력 후 판정; clear_before=false 로 clear 없음).
    "/oninput": ("<!doctype html><title>clean</title><label>Entry<input id=entry "
                 f"oninput=\"this.type='password'; {_REFLECT}\"></label>"),
    # clear 의 input 이벤트(값 '')에서만 password, 첫 문자 입력부터 다시 text — clear 후 판정만이 잡는다(시작 전·입력
    # 후에는 text). 이 칸이 clear 후 판정을 고유하게 측정한다(AGENTS 규칙 2).
    "/onclear": ("<!doctype html><title>clean</title><label>Entry<input id=entry value=old "
                 "oninput=\"if(this.value===''){this.type='password';window.wasPassword=true}"
                 f"else{{this.type='text'; {_REFLECT}}}\"></label>"),
    # 대조군 — 일반 검색칸.
    "/plain": f"<!doctype html><title>clean</title><label>Search<input id=entry oninput=\"{_REFLECT}\"></label>",
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path / "servers"))
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _PAGES.get(self.path.split("?")[0], _PAGES["/plain"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _outside_content(wire: str) -> str:
    """페이지 내용 subtree(CONTENT_PATHS — 관찰 요소·요약, 원문 보존 계약)를 뺀 wire. 검증 B1-R1 도 이 범위만 셌다."""
    obj = json.loads(wire)
    obs = obj.get("data", {}).get("observation") or {}
    obs.pop("elements", None)
    obs.pop("axtree_summary", None)
    return json.dumps(obj, ensure_ascii=False)


async def _open(s, site, path):
    assert (await s.call_tool("browser_navigate", {"url": site + path})).success


async def _type(s, text, **extra):
    obs = await s.call_tool("browser_observe_page", {})
    entry = next(e for e in obs.data["observation"]["elements"] if e.get("role") == "textbox")
    return await s.call_tool("browser_type_text", {"element_id": entry["element_id"], "epoch": obs.snapshot_epoch,
                                                   "text": text, **extra})


async def _assert_hidden(s, typed, value):
    typed_wire = envelope_json(typed)
    assert value not in typed_wire, typed_wire
    obs_wire = _outside_content(envelope_json(await s.call_tool("browser_observe_page", {})))
    assert value not in obs_wire, obs_wire
    assert "sortKey=[REDACTED]" in obs_wire
    assert len(secret_registry.REGISTRY) > 0, "민감 칸에 친 값이 등록되지 않았다"


@requires_chromium
async def test_clear_flips_password_to_text_still_registered(site):
    """B1-R1 재현 1 — clear 의 input 이벤트가 type 을 text 로 바꿔도 시작 전 판정(password)이 유지된다."""
    value = "SYNTHedgeSecret55"
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("type_text:*",)) as s:
        await _open(s, site, "/flip")
        entry = s._page.locator("#entry")
        assert await entry.get_attribute("type") == "password"  # 측정 조건: 시작 시 실제 password·기존 값
        assert await entry.input_value() == "old"
        typed = await _type(s, value)
        assert typed.success, typed
        assert await entry.input_value() == value
        assert await entry.get_attribute("type") == "text"  # 페이지가 실제로 바꿨다
        assert "sortKey=" + value in s._page.url  # 페이지가 실제로 반사했다
        await _assert_hidden(s, typed, value)


@requires_chromium
async def test_clear_replaces_password_node_still_registered(site):
    """B1-R1 재현 2 — clear 의 input 이벤트가 password 노드를 같은 id 의 text 노드로 교체해도 등록된다."""
    value = "SYNTHedgeSecret55"
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("type_text:*",)) as s:
        await _open(s, site, "/replace")
        entry = s._page.locator("#entry")
        assert await entry.get_attribute("type") == "password"
        assert await entry.input_value() == "old"
        typed = await _type(s, value)
        assert typed.success, typed
        assert await s._page.evaluate("window.didReplace") is True  # 실제 노드 교체
        assert await entry.get_attribute("type") == "text"
        assert await entry.input_value() == value
        assert "sortKey=" + value in s._page.url
        await _assert_hidden(s, typed, value)


@requires_chromium
async def test_becomes_password_on_focus_registered(site):
    """OR 규칙 — 시작 전에는 text, clear(포커스) 뒤 password 가 된 칸도 등록한다."""
    value = "SYNTHfocusSecret55"
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("type_text:*",)) as s:
        await _open(s, site, "/onfocus")
        assert await s._page.locator("#entry").get_attribute("type") in (None, "text")
        typed = await _type(s, value)
        assert typed.success, typed
        assert await s._page.locator("#entry").get_attribute("type") == "password"
        assert "sortKey=" + value in s._page.url
        await _assert_hidden(s, typed, value)


@requires_chromium
async def test_password_only_between_clear_and_input_registered(site):
    """OR 규칙 — 시작 전·입력 후에는 text 지만 clear 직후 password 였던 칸도 등록한다(clear 후 판정 고유 측정)."""
    value = "SYNTHclearSecret55"
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("type_text:*",)) as s:
        await _open(s, site, "/onclear")
        entry = s._page.locator("#entry")
        assert await entry.get_attribute("type") in (None, "text")
        typed = await _type(s, value)
        assert typed.success, typed
        assert await s._page.evaluate("window.wasPassword") is True  # clear 직후 실제 password 였다
        assert await entry.get_attribute("type") == "text"  # 입력 후엔 다시 text
        assert "sortKey=" + value in s._page.url
        await _assert_hidden(s, typed, value)


@requires_chromium
async def test_becomes_password_during_input_registered(site):
    """OR 규칙 — clear 없이(clear_before=false) 첫 문자 입력에서 password 가 된 칸도 입력 후 판정으로 등록한다."""
    value = "SYNTHinputSecret55"
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("type_text:*",)) as s:
        await _open(s, site, "/oninput")
        assert await s._page.locator("#entry").get_attribute("type") in (None, "text")
        typed = await _type(s, value, clear_before=False)
        assert typed.success, typed
        assert await s._page.locator("#entry").get_attribute("type") == "password"
        assert "sortKey=" + value in s._page.url
        await _assert_hidden(s, typed, value)


@requires_chromium
async def test_plain_field_value_still_preserved(site):
    """대조군(결정 A) — 민감하지 않은 일반 칸의 값은 등록하지 않는다(과잉 가림 방지)."""
    value = "SYNTHplainQuery55"
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("type_text:*",)) as s:
        await _open(s, site, "/plain")
        typed = await _type(s, value)
        assert typed.success, typed
        assert len(secret_registry.REGISTRY) == 0
        obs = json.loads(envelope_json(await s.call_tool("browser_observe_page", {})))
        assert obs["current_url"].endswith("/land?sortKey=" + value)


@requires_chromium
async def test_dispatcher_sticky_flag_stays_off_caller_params():
    """민감 판정 표시(_conceal_value)는 디스패처 사본에만 — 호출자 params(트레이스)는 그대로, 신호엔 값 대신 <secret>."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from contracts import ActionType
    from perception import PerceptionEngine

    value = "SYNTHdispSecret55"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content("<label>Password<input id=entry type=password value=old "
                               "oninput=\"this.type='text'\"></label>")
        engine = PerceptionEngine()
        disp = ActionDispatcher(DispatchContext(page=page, engine=engine, cdp=await context.new_cdp_session(page)))
        obs = await engine.observe_page(page=page, prune_top_n=20)
        entry = next(e for e in obs.elements if e.role == "textbox")
        params = {"element_id": entry.element_id, "epoch": obs.snapshot_epoch, "text": value}
        snapshot = dict(params)
        result = await disp.dispatch(ActionType.TYPE_TEXT, params)
        flipped = await page.locator("#entry").get_attribute("type")
        await browser.close()
    assert result.success, result
    assert flipped == "text"  # 측정 조건: 페이지가 실제로 type 을 바꿨다
    assert params == snapshot  # 호출자 dict 에 내부 표시가 새지 않는다
    assert value not in json.dumps(result.data, ensure_ascii=False)
    assert "value_applied: <secret>" in result.data.get("signals", [])
    assert len(secret_registry.REGISTRY) > 0
