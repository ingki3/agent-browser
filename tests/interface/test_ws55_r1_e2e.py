"""WS-55 R1 E2E — 실 Chromium(headless) + 로컬 http. 검증 FAIL(.hermes/verify/ws55/report.md) 재현 7건을 저장소 테스트로 고정.

* B1: SecretStore 로 해석한 실제 값(`browser_type_text(text='X-VERIFY55')`)을 페이지가 허용 목록 키 주소
  (`history.replaceState(…'/land?sortKey='+값)`)·제목에 반사해도 다음 관찰의 최종 wire 에 원문이 없다. 일반 검색칸에
  에이전트가 직접 친 평범한 값은 에이전트가 이미 아는 값이라 **보존**(코디네이터 결정 A — 검증 재현의 plain 기대를 뒤집음).
  비밀번호 칸에 친 값은 **입력 시점** 판정으로 등록 — 입력 직후 페이지가 `type=password` 를 `text` 로 바꿔도 가린다.
* B2: 경로 matrix(`/p;sortKey=<hex40>`)가 navigate·observe·extract·tab_control·302 리다이렉트·연결 거부 오류 응답에서
  원문을 남기지 않는다.
* B3: `?pagekey=` 소문자 붙여쓰기도 가린다.
* 정상: 허용 키의 리다이렉트·선등록 값 방어.

값은 합성 값. 공개 사이트 없음(127.0.0.1 만).
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browser.serve_profile import PROFILE_ROOT_ENV
from interface.handoff import STATE_ROOT_ENV
from interface.mcp_server import BrowserMCPServer, envelope_json
from security import secret_registry
from security.secrets import SecretStore

from test_run_cli import requires_chromium

HEX40 = "0123456789abcdef0123456789abcdef01234567"
_REFLECT = ("document.title=this.value; "
            "history.replaceState(null,'','/land?sortKey='+encodeURIComponent(this.value))")
_PAGES = {
    "/form": f"<!doctype html><title>clean</title><label>Search<input id=entry oninput=\"{_REFLECT}\"></label>"
             "<p>public body</p>",
    # 입력 직후 type=password → text 로 바꾸는 페이지(입력 시점 판정이 흔들리지 않는지).
    "/pw": f"<!doctype html><title>login</title><label>Password<input id=pw type=password "
           f"oninput=\"this.type='text'; {_REFLECT}\"></label>",
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
            if self.path == "/matrix-redirect":
                self.send_response(302)
                self.send_header("Location", "/land;sortKey=" + HEX40)
                self.end_headers()
                return
            if self.path.startswith("/redirect?"):
                self.send_response(302)
                self.send_header("Location", "/land?" + self.path.split("?", 1)[1])
                self.end_headers()
                return
            page = _PAGES.get(self.path.split("?")[0].split(";")[0], _PAGES["/form"])
            body = page.encode()
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
    """페이지 내용 subtree(CONTENT_PATHS — 관찰 요소·요약, 원문 보존 계약)를 뺀 wire. 검증 B1 도 이 범위만 셌다."""
    obj = json.loads(wire)
    obs = obj.get("data", {}).get("observation") or {}
    obs.pop("elements", None)
    obs.pop("axtree_summary", None)
    return json.dumps(obj, ensure_ascii=False)


async def _type_into(s, site, path, element_text):
    assert (await s.call_tool("browser_navigate", {"url": site + path})).success
    obs = await s.call_tool("browser_observe_page", {})
    entry = next(e for e in obs.data["observation"]["elements"] if e.get("role") == "textbox")
    return await s.call_tool("browser_type_text", {"element_id": entry["element_id"], "epoch": obs.snapshot_epoch,
                                                   "text": element_text})


@requires_chromium
async def test_matrix_secret_reaches_real_tools(site):
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        nav = await s.call_tool("browser_navigate", {"url": site + "/p;sortKey=" + HEX40})
        assert nav.success, nav
        wires = {"navigate": envelope_json(nav)}
        for name, args in [("browser_observe_page", {}), ("browser_extract", {"selector": "p"}),
                           ("browser_tab_control", {"command": "list"})]:
            result = await s.call_tool(name, args)
            assert result.success, result
            wires[name] = envelope_json(result)
        leaked = [name for name, wire in wires.items() if HEX40 in wire]
        assert leaked == [], leaked
        assert ";sortKey=[REDACTED]" in json.loads(wires["navigate"])["current_url"]
        assert len(secret_registry.REGISTRY) >= 1


@requires_chromium
async def test_secretstore_type_text_registered_before_allowlisted_url(site):
    """검증 B1(base 통과 → WS-55 HEAD 실패였던 SecretStore 건)."""
    value = "SYNTHtypedSecret55"
    store = SecretStore({"X-VERIFY55": value})
    async with BrowserMCPServer(headless=True, nav_settle=False, secrets=store,
                                pre_approved_actions=("type_text:*",)) as s:
        typed = await _type_into(s, site, "/form", "X-VERIFY55")
        assert typed.success, typed
        assert await s._page.locator("#entry").input_value() == value
        assert typed.data.get("secret_resolved") is True
        assert "sortKey=" + value in s._page.url  # 페이지가 실제로 반사했다(측정 조건)
        wire = _outside_content(envelope_json(await s.call_tool("browser_observe_page", {})))
        assert value not in wire, wire
        assert "sortKey=[REDACTED]" in wire
        assert value not in envelope_json(typed)


@requires_chromium
async def test_plain_type_text_value_preserved(site):
    """검증 B1 의 plain 건 — 기대를 뒤집어 고정(코디네이터 결정 A): 일반 칸에 에이전트가 친 평범한 값은 에이전트가 이미
    자기 호출 인자로 아는 값이라 등록하지 않는다(검색어가 제목·주소에서 가려지는 과잉 가림 방지)."""
    value = "SYNTHtypedSecret55"
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("type_text:*",)) as s:
        typed = await _type_into(s, site, "/form", value)
        assert typed.success, typed
        assert len(secret_registry.REGISTRY) == 0
        obs = json.loads(envelope_json(await s.call_tool("browser_observe_page", {})))
        assert obs["current_url"].endswith("/land?sortKey=" + value)
        assert obs["data"]["observation"]["title"] == value


@requires_chromium
async def test_password_field_value_registered_at_input_time(site):
    """비밀번호 칸 — 입력 시점 판정. 페이지가 입력 직후 type 을 text 로 바꾸고 값을 주소·제목에 반사해도 가린다."""
    value = "SYNTHpwField55"
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("type_text:*",)) as s:
        typed = await _type_into(s, site, "/pw", value)
        assert typed.success, typed
        assert await s._page.locator("#pw").get_attribute("type") == "text"  # 페이지가 실제로 바꿨다(측정 조건)
        assert "sortKey=" + value in s._page.url
        wire = _outside_content(envelope_json(await s.call_tool("browser_observe_page", {})))
        assert value not in wire, wire
        assert "sortKey=[REDACTED]" in wire
        assert value not in envelope_json(typed)


@requires_chromium
async def test_lowercase_pagekey_cursor_hidden(site):
    value = "SYNTHcursor55"
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        nav = await s.call_tool("browser_navigate", {"url": site + "/p?pagekey=" + value})
        assert nav.success
        assert value not in envelope_json(nav)


@requires_chromium
async def test_allowlisted_secret_redirect_and_registry_work(site):
    value = "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3"
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        nav = await s.call_tool("browser_navigate", {"url": site + "/redirect?sortKey=" + value})
        assert nav.success
        assert "/land?" in s._page.url
        assert value not in envelope_json(nav)
        assert len(secret_registry.REGISTRY) > 0
        assert value not in envelope_json(await s.call_tool("browser_observe_page", {}))
        short = "SYNTHknownSecret55"
        assert (await s.call_tool("browser_navigate", {"url": site + "/p?token=" + short})).success
        alias = await s.call_tool("browser_navigate", {"url": site + "/p?sortKey=" + short})
        assert alias.success
        assert short not in envelope_json(alias)


@requires_chromium
async def test_matrix_redirect_and_error(site):
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        redirected = await s.call_tool("browser_navigate", {"url": site + "/matrix-redirect"})
        assert redirected.success
        assert "/land;" in s._page.url
        redirect_wire = envelope_json(redirected)
        secret_registry.clear()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        failed = await s.call_tool("browser_navigate",
                                   {"url": f"http://127.0.0.1:{port}/p;sortKey={HEX40}", "timeout_ms": 2000})
        assert not failed.success
        error_wire = envelope_json(failed)
        assert HEX40 not in redirect_wire, redirect_wire
        assert HEX40 not in error_wire, error_wire
