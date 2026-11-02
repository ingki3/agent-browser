"""WS-78 T1(#142) — press_key 로 한 글자씩 친 비밀번호도 등록·가림, 레시피에 키 글자 0.

실 Chromium(headless) + 로컬 http(127.0.0.1 만), 합성 값. 리뷰 .hermes/plans/ws78-evidence/review.md B1~B4·NB1.

* 키 **전** 포커스 노드로 민감 판정(sticky·OR), 키 **뒤** 그 노드의 값을 통째로 등록(접두 유지).
* 레시피: 민감 칸으로 간 press_key 단계는 비밀 단계 — 자동 저장·이름에 키 글자가 남지 않는다.
* 값을 못 읽으면(문서 이동) 키 전 값 등록 + data.sensitive_input 고정 신호.
* 대조군: 일반 검색칸 press_key 는 등록 없음·응답 data 는 {"key": …} 그대로.
"""

from __future__ import annotations

import asyncio
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
    "/login": f"<!doctype html><title>clean</title><label>Password<input id=entry type=password oninput=\"{_REFLECT}\"></label>",
    # 첫 입력 이벤트에서 password → text 로 바뀌는 칸(키 전 판정·sticky 가 잡는다).
    "/flip": ("<!doctype html><title>clean</title><label>Password<input id=entry type=password "
              f"oninput=\"this.type='text'; {_REFLECT}\"></label>"),
    # Tab 을 누르면 페이지가 값에 꼬리를 붙이고(키 뒤에야 생기는 값) 포커스가 다음 칸으로 — blur 때 그 값을 반사한다.
    "/tab": ("<!doctype html><title>clean</title><label>Password<input id=entry type=password "
             "onkeydown=\"if(event.key==='Tab'){this.value+='SYNTHtabTail78'}\" "
             f"onblur=\"{_REFLECT}\"></label><label>Next<input id=next></label>"),
    # Control+V 를 붙여넣기처럼 흉내 — 키 하나로 긴 값이 들어오고 반사된다.
    "/paste": ("<!doctype html><title>clean</title><label>Password<input id=entry type=password "
               "onkeydown=\"if(event.ctrlKey&&event.key==='v'){this.value='SYNTHpastedSecret78';"
               "document.title=this.value;history.replaceState(null,'','/land?sortKey='+this.value)}\"></label>"),
    # 대조군 — 일반 검색칸.
    "/plain": f"<!doctype html><title>clean</title><label>Search<input id=entry oninput=\"{_REFLECT}\"></label>",
}
VALUE = "SYNTHkeySecret78x"


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
            body = ("User-agent: *\nAllow: /\n" if self.path == "/robots.txt"
                    else _PAGES.get(self.path.split("?")[0], _PAGES["/plain"])).encode()
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


async def _focus_entry(s, site, path):
    assert (await s.call_tool("browser_navigate", {"url": site + path})).success
    obs = await s.call_tool("browser_observe_page", {})
    el = next(e for e in obs.data["observation"]["elements"]
              if e.get("role") == "textbox" and "next" not in str(e.get("name") or "").lower())
    # 칸 클릭은 '포커스만 이동'이라 사후조건 실패로 보고될 수 있다 — 측정 조건은 실제 포커스로 확인한다.
    await s.call_tool("browser_click", {"element_id": el["element_id"], "epoch": obs.snapshot_epoch})
    assert await s._page.evaluate("document.activeElement && document.activeElement.id") == "entry"


async def _type_keys(s, text):
    last = None
    wires = []
    for ch in text:
        last = await s.call_tool("browser_press_key", {"key": ch})
        assert last.success, last
        wires.append(envelope_json(last))
    return last, wires


def _recipe_dump(s) -> str:
    rec = s._recipes
    saved = rec.store.list() if rec is not None else []
    traj = rec.trajectory.entries if rec is not None else []
    return json.dumps({"saved": saved, "trajectory": list(traj)}, ensure_ascii=False, default=str)


def _no_key_sequence(dump: str, value: str) -> None:
    # 키 순서(연속 4글자 이상)가 이름("S 키→Y 키…")이나 단계 key 로 남지 않는다.
    assert value not in dump
    assert "→".join(f"{c} 키" for c in value[:4]) not in dump, dump
    for c in value:
        assert f'"key": "{c}"' not in dump, dump


@requires_chromium
async def test_single_press_key_password_hidden_everywhere(site):
    """(1)(3) 단건 press_key 로 17자 — 마지막 응답·관찰·current_url 원문 0, 등록>0, 레시피에 키 순서 0."""
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        await _focus_entry(s, site, "/login")
        last, _ = await _type_keys(s, VALUE)
        assert "sortKey=" + VALUE in s._page.url  # 측정 조건: 페이지가 실제로 반사했다
        assert VALUE not in envelope_json(last)
        obs_wire = envelope_json(await s.call_tool("browser_observe_page", {}))
        assert VALUE not in obs_wire, obs_wire
        assert "sortKey=[REDACTED]" in obs_wire
        assert len(secret_registry.REGISTRY) > 0
        _no_key_sequence(_recipe_dump(s), VALUE)
        assert s._recipes.segment.stuck is True  # 비밀 단계에서 구간이 끊겼다


@requires_chromium
async def test_batch_press_key_password_hidden(site):
    """(2) browser_batch press_key 단계도 같은 디스패처 경로 — wire·관찰 원문 0, 레시피 키 순서 0."""
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        await _focus_entry(s, site, "/login")
        part = VALUE[:10]  # 묶음 상한 10단계
        obs = await s.call_tool("browser_observe_page", {})
        name = next(e for e in obs.data["observation"]["elements"] if e.get("role") == "textbox")["name"]
        steps = [{"action": "press_key", "target": {"role": "textbox", "name": name}, "args": {"key": c}}
                 for c in part]
        out = await s.call_server_tool("browser_batch", {"steps": steps})
        assert out.get("success") is True, out
        assert "sortKey=" + part in s._page.url
        wire = json.dumps(out, ensure_ascii=False)
        assert part not in wire, wire
        obs_wire = envelope_json(await s.call_tool("browser_observe_page", {}))
        assert part not in obs_wire
        assert len(secret_registry.REGISTRY) > 0
        _no_key_sequence(_recipe_dump(s), part)


@requires_chromium
async def test_type_flip_keeps_pre_key_judgement(site):
    """(4) 입력 이벤트가 type 을 password→text 로 바꿔도 키 전 판정·sticky 로 계속 등록한다."""
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        await _focus_entry(s, site, "/flip")
        last, _ = await _type_keys(s, VALUE)
        assert await s._page.locator("#entry").get_attribute("type") == "text"  # 페이지가 실제로 바꿨다
        assert "sortKey=" + VALUE in s._page.url
        assert VALUE not in envelope_json(last)
        assert VALUE not in envelope_json(await s.call_tool("browser_observe_page", {}))
        _no_key_sequence(_recipe_dump(s), VALUE)


@requires_chromium
async def test_tab_registers_previous_field_value(site):
    """(5) Tab 으로 포커스가 옮겨도 키 전 그 노드의 키 뒤 값(꼬리 포함)을 등록한다."""
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        await _focus_entry(s, site, "/tab")
        # 키 전 값은 등록되지 않는 짧은 값 — 키 뒤 **그 노드**의 값을 읽어야만 완성값이 등록된다.
        await s._page.locator("#entry").evaluate("e => { e.value = 'Zq'; }")
        tab = await s.call_tool("browser_press_key", {"key": "Tab"})
        assert tab.success, tab
        full = "ZqSYNTHtabTail78"
        assert "sortKey=" + full in s._page.url
        assert await s._page.evaluate("document.activeElement.id") == "next"
        assert full not in envelope_json(tab)
        assert full not in envelope_json(await s.call_tool("browser_observe_page", {}))


@requires_chromium
async def test_paste_key_registers_value(site):
    """(6) Control+V 같은 키 하나로 들어온 긴 값도 키 뒤 값 읽기로 등록한다."""
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        await _focus_entry(s, site, "/paste")
        r = await s.call_tool("browser_press_key", {"key": "Control+v"})
        assert r.success, r
        assert "SYNTHpastedSecret78" in s._page.url
        assert "SYNTHpastedSecret78" not in envelope_json(r)
        assert "SYNTHpastedSecret78" not in envelope_json(await s.call_tool("browser_observe_page", {}))


@requires_chromium
async def test_plain_field_press_key_unchanged(site):
    """(8) 대조군 — 일반 검색칸은 등록 없음, data 는 {"key"} 뿐(+공통 latency_ms), 값 보존."""
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        await _focus_entry(s, site, "/plain")
        last, _ = await _type_keys(s, "SYNTHplain78")
        assert len(secret_registry.REGISTRY) == 0
        # 서버 공통 신호(challenge·last_http_status·latency_ms) 말고 새 키 없음 — 민감 신호도 없다.
        assert set(last.data) == {"key", "latency_ms", "challenge", "last_http_status"}, last.data
        assert "sortKey=SYNTHplain78" in envelope_json(last)


@requires_chromium
async def test_append_type_text_registers_whole_field(site):
    """NB1 — type_text clear_before=false 로 password 에 덧붙이면 칸 전체 값(앞 값+친 글자)을 등록한다."""
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("type_text:*",)) as s:
        assert (await s.call_tool("browser_navigate", {"url": site + "/login"})).success
        await s._page.locator("#entry").evaluate("e => { e.value = 'SYNTHhead'; }")
        obs = await s.call_tool("browser_observe_page", {})
        el = next(e for e in obs.data["observation"]["elements"] if e.get("role") == "textbox")
        r = await s.call_tool("browser_type_text", {"element_id": el["element_id"], "epoch": obs.snapshot_epoch,
                                                    "text": "Q7", "clear_before": False})
        # 자격증명 칸 덧붙이기는 기존 사후조건(필드 값 == 친 값)으로 실패 보고될 수 있다 — 입력 자체는 됐다.
        full = await s._page.locator("#entry").input_value()
        assert len(full) == len("SYNTHhead") + 2 and "SYNTHhead" in full  # 측정 조건: 앞 값에 덧붙었다
        assert "sortKey=" + full in s._page.url
        assert full not in envelope_json(r)
        wire = envelope_json(await s.call_tool("browser_observe_page", {}))
        assert full not in wire, wire


class _GoneNode:
    """키 뒤 평가가 실패하는(문서 이동) 노드."""

    async def evaluate(self, *a, **k):
        raise RuntimeError("Execution context was destroyed")

    async def dispose(self):
        return None


async def test_unread_after_key_fail_closed():
    """(7) 민감 칸 키 뒤 값을 못 읽으면 키 전 값을 등록하고 data 에 고정 신호(값 없음)."""
    from actions.dispatcher import SENSITIVE_VALUE_UNREAD, ActionDispatcher

    disp = ActionDispatcher.__new__(ActionDispatcher)
    data: dict = {"key": "Enter"}
    await disp._key_field_after(_GoneNode(), True, "SYNTHbeforeEnter78", data)
    assert data["sensitive_input"] == {"value_read": False, "hint": SENSITIVE_VALUE_UNREAD}
    assert "SYNTHbeforeEnter78" not in json.dumps(data, ensure_ascii=False)
    assert disp.last_key_sensitive is True
    assert secret_registry.REGISTRY.mask("x SYNTHbeforeEnter78 y") != "x SYNTHbeforeEnter78 y"


class _HangNode:
    """Enter 제출 직후처럼 평가가 새 문서 도착까지 멈추는 노드(실측: 응답 3초 지연 폼에서 5회 중 1회)."""

    async def evaluate(self, *a, **k):
        await asyncio.sleep(30)

    async def dispose(self):
        await asyncio.sleep(30)


@pytest.mark.parametrize("sensitive", [True, False])
async def test_hanging_read_is_bounded(sensitive):
    """키 뒤 읽기·핸들 해제가 멈춰도 상한(_KEY_FIELD_TIMEOUT_S) 안에 끝난다 — 민감이면 fail-closed 신호, 일반 칸은 그대로."""
    import time

    from actions.dispatcher import _KEY_FIELD_TIMEOUT_S, ActionDispatcher

    disp = ActionDispatcher.__new__(ActionDispatcher)
    data: dict = {"key": "Enter"}
    t0 = time.perf_counter()
    await disp._key_field_after(_HangNode(), sensitive, "SYNTHhangBefore78", data)
    assert time.perf_counter() - t0 < _KEY_FIELD_TIMEOUT_S + 0.5
    if sensitive:
        assert data["sensitive_input"]["value_read"] is False
    else:
        assert data == {"key": "Enter"} and len(secret_registry.REGISTRY) == 0
