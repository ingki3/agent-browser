"""WS-91 (#253·#246): type_text(press_enter)·press_key Enter 꼬리 — nav_settle=False 대기·거짓 실패 + Enter 뒤 상태.

#253: nav_settle=False 에서 Enter press 가 Playwright noWaitAfter=false 로 새 문서 커밋까지(~415ms) 막히고, 사후 판독이
새 문서를 읽어 거짓 E_TIMEOUT 이었다. F1 no_wait_after(끔 모드만) · F2 Enter 뒤 메인 문서 요청 리스너 + 공유 마감(0.3s) ·
F3 서버 차단 판정(_attach_challenge)도 같은 마감 · B3 마감 안 요청 실패·오류 문서면 기준 실패 그대로.
#246: data.after_enter = {value, field, listbox, options, document} — 고정 어휘·숫자만, 민감 칸이면 통째 생략.

실제 BrowserMCPServer(헤드리스 Chromium) + 로컬 HTTP fixture 만(공개 사이트 없음), 값은 합성 값.
주입은 멈춤(5s)만 — 같은 지연 주입은 동시/순차를 못 가른다(WS-89 교훈).
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

import pytest

from actions import dispatcher as dmod
from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, envelope_json, tool_name

from test_run_cli import requires_chromium  # noqa: F401

_HANG_S = 5.0
_BOUND_S = 0.35
HEAD = "<!doctype html><meta charset=utf-8><title>{t}</title>"
_OPTS = "".join(f"<li role=option>운중 {i}</li>" for i in range(7))
_RES = "".join(f"<li role=option>결과 {i}</li>" for i in range(6))
#: 실제 네이버지도 길찾기 캡처 구조(.hermes/plans/ws91-evidence/naver-capture/timeline.md): 자동 포커스 combobox,
#: aria-controls·owns 없음, 입력 시 연결 없는 자동완성 listbox(option 7)·aria-expanded=true, Enter 는 자동완성을 닫고
#: (aria-expanded=false·listbox 0) 값을 그대로 두며, 1s 뒤 검색 결과 listbox(option 6). URL 그대로.
NAVER = (HEAD.format(t="길찾기") + "<input id=input_search_r_6_ class=input_search role=combobox "
         "aria-label='출발지 입력' aria-expanded=false aria-activedescendant='' autofocus>"
         "<div id=ac></div><div id=res></div><script>"
         "const t=document.getElementById('input_search_r_6_'),ac=document.getElementById('ac'),"
         "res=document.getElementById('res');"
         "t.addEventListener('input',()=>{ac.innerHTML=\"<div class=scroll_area><ul role=listbox>" + _OPTS
         + "</ul></div>\";t.setAttribute('aria-expanded','true');});"
         "t.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();ac.innerHTML='';"
         "t.setAttribute('aria-expanded','false');setTimeout(()=>{res.innerHTML=\"<ul role=listbox>" + _RES
         + "</ul>\";},1000);}});</script>")
_LB = ("<div role=listbox id=lb hidden></div><script>const t=document.getElementById('t'),"
       "lb=document.getElementById('lb');function show(){lb.innerHTML=['A역','B역','C역'].map((x,i)=>"
       "`<div role=option id=o${i}>${t.value} ${x}</div>`).join('');lb.hidden=false;"
       "t.setAttribute('aria-expanded','true');} t.addEventListener('input', show);</script>")
_COMBO = "<input id=t role=combobox aria-label='출발지 입력' aria-autocomplete=list aria-controls=lb aria-expanded=false>"


def _enter_js(body: str) -> str:
    return ("<script>document.getElementById('t').addEventListener('keydown',e=>{if(e.key==='Enter'){"
            "e.preventDefault();" + body + "}});</script>")


PAGES: Dict[str, str] = {
    "/a": HEAD.format(t="홈") + "<form method=get action='/result'><input id=t name=q aria-label='검색어'>"
          "<input type=hidden name=d value=400></form>",
    "/naver": NAVER,
    # (c) Enter 가 값을 비운다(동기), 연결 목록은 열린 채
    "/c": HEAD.format(t="지도") + _COMBO + _LB + _enter_js("t.value='';"),
    # (c2) 값 그대로, 연결 목록 열린 채(3개)
    "/c2": HEAD.format(t="지도") + _COMBO + _LB,
    # (c3) SPA 재렌더로 칸 노드 교체(같은 css, 빈 값), 목록 닫힘
    "/c3": HEAD.format(t="지도") + "<div id=wrap>" + _COMBO + "</div>" + _LB + _enter_js(
        "document.getElementById('wrap').innerHTML=\"" + _COMBO.replace("'", "\\'") + "\";lb.hidden=true;"),
    "/pw": HEAD.format(t="암호") + "<input id=t type=password aria-label='암호'>",
    "/plain": HEAD.format(t="메모") + "<input id=t aria-label='메모'>",
    # WS-88 B4 p8: 입력 중 GET 폼이 POST 결제로 바뀐다
    "/mutate": HEAD.format(t="변조") + "<form method=get action=/search><input id=t name=q aria-label='검색어' "
               "oninput=\"this.form.method='post'; this.form.action='/billing'\"></form>",
    # POST 결제 폼(승인 경로 — elicitation) — 결과 400ms 늦게
    "/post": HEAD.format(t="결제") + "<form method=post action='/order/pay?d=400'><input id=t name=qty "
             "aria-label='수량'><button>진행</button></form>",
}


class _Site:
    base = ""
    closed = ""
    posts: List[str] = []


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def site():
    st = _Site()
    st.posts = []

    class H(BaseHTTPRequestHandler):
        def _reply(self, body: str) -> None:
            b = body.encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            d = int((parse_qs(u.query).get("d") or ["0"])[0])
            if u.path == "/robots.txt":
                return self._reply("User-agent: *\nAllow: /\n")
            if u.path == "/result":
                time.sleep(d / 1000)
                return self._reply(HEAD.format(t="결과") + "<p>결과 1</p>")
            if u.path == "/closed":
                return self._reply(HEAD.format(t="홈") + f"<form method=get action='{st.closed}/x'>"
                                   "<input id=t name=q aria-label='검색어'></form>")
            return self._reply(PAGES.get(u.path, HEAD.format(t="x")))

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            st.posts.append(u.path)
            time.sleep(int((parse_qs(u.query).get("d") or ["0"])[0]) / 1000)
            self._reply(HEAD.format(t="완료") + "<p>완료</p>")

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    st.base = f"http://127.0.0.1:{srv.server_address[1]}"
    st.closed = f"http://127.0.0.1:{_closed_port()}"
    try:
        yield st
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ 계측·주입(Playwright press·판독 멈춤)


class _Probe:
    """Enter 시각·press 인자 기록, '이동 대기 흉내'(no_wait_after 없으면 5s), Enter 뒤 판독 멈춤(5s) 주입."""

    def __init__(self) -> None:
        self.enter: List[Tuple[float, Dict[str, Any]]] = []
        self.imitate = False
        self.hang: Dict[str, bool] = {"capture": False, "iso": False, "detect": False}
        self.armed = False

    def reset(self) -> None:
        self.enter.clear()
        self.armed = False

    @property
    def t_enter(self) -> float:
        assert self.enter, "Enter 를 보내지 않음"
        return self.enter[-1][0]


@pytest.fixture
def probe(monkeypatch):
    from playwright.async_api import ElementHandle, Keyboard, Locator

    from browser import challenge as challenge_mod
    from interface import mcp_server as ms

    p = _Probe()

    def wrap_press(orig):
        async def press(self, key, **kw):
            if key == "Enter":
                p.enter.append((time.perf_counter(), dict(kw)))
            out = await orig(self, key, **kw)
            if key == "Enter":
                p.armed = True
                if p.imitate and not kw.get("no_wait_after"):
                    await asyncio.sleep(_HANG_S)  # Playwright 의 '이동 시작까지 대기'(noWaitAfter=false) 흉내
            return out
        return press

    for cls in (ElementHandle, Locator):
        monkeypatch.setattr(cls, "press", wrap_press(cls.press))
    kb_press = Keyboard.press

    async def kb(self, key, **kw):
        if key == "Enter":
            p.enter.append((time.perf_counter(), dict(kw)))
        out = await kb_press(self, key, **kw)
        if key == "Enter":
            p.armed = True
        return out

    monkeypatch.setattr(Keyboard, "press", kb)
    cap = dmod.ActionDispatcher._capture_state

    async def capture(self, *a, **k):
        if p.armed and p.hang["capture"]:
            await asyncio.sleep(_HANG_S)
        return await cap(self, *a, **k)

    monkeypatch.setattr(dmod.ActionDispatcher, "_capture_state", capture)
    iso = ms.BrowserMCPServer._isolated_challenge

    async def iso_ch(self, *a, **k):
        if p.armed and p.hang["iso"]:
            await asyncio.sleep(_HANG_S)
        return await iso(self, *a, **k)

    monkeypatch.setattr(ms.BrowserMCPServer, "_isolated_challenge", iso_ch)
    det = challenge_mod.detect_challenge

    async def detect(*a, **k):
        if p.armed and p.hang["detect"]:
            await asyncio.sleep(_HANG_S)
        return await det(*a, **k)

    monkeypatch.setattr(challenge_mod, "detect_challenge", detect)
    return p


async def _call(s: BrowserMCPServer, action: ActionType, args: Dict[str, Any]) -> Any:
    return await s.call_tool(tool_name(action), args)


async def _target(s: BrowserMCPServer, url: str, name: str) -> Tuple[str, int]:
    nav = await _call(s, ActionType.NAVIGATE, {"url": url})
    assert nav.success, nav.error_message
    obs = await _call(s, ActionType.OBSERVE_PAGE, {})
    o = obs.data["observation"]
    for e in o["elements"]:
        if e["name"] == name:
            return e["element_id"], o["snapshot_epoch"]
    raise AssertionError([e["name"] for e in o["elements"]])


async def _type_enter(s: BrowserMCPServer, p: _Probe, url: str, name: str, text: str = "운중") -> Tuple[Any, float]:
    eid, epoch = await _target(s, url, name)
    p.reset()
    r = await _call(s, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": text, "press_enter": True})
    return r, time.perf_counter() - p.t_enter


async def _press_enter(s: BrowserMCPServer, p: _Probe, url: str, name: str) -> Tuple[Any, float]:
    eid, epoch = await _target(s, url, name)
    typed = await _call(s, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "운중"})
    assert typed.success, typed.error_message
    p.reset()
    r = await _call(s, ActionType.PRESS_KEY, {"key": "Enter"})
    return r, time.perf_counter() - p.t_enter


def _why(r: Any, el: float) -> str:
    return f"{el:.3f}s code={r.error_code} msg={r.error_message} latency_ms={r.data.get('latency_ms')} data={r.data}"


# ------------------------------------------------------------------ #253: (a) GET 폼 → 400ms 늦은 결과 문서


@requires_chromium
async def test_type_enter_get_form_nav_settle_off_success_fast(site, probe):
    """기준: E_TIMEOUT('기대값 != 실제 None')·Enter→반환 ~425ms. 대상: 성공(navigation_started)·≤0.35s(call_tool 경계)."""
    async with BrowserMCPServer(nav_settle=False) as s:
        await _type_enter(s, probe, site.base + "/a", "검색어")  # 준비 비용 제외
        r, el = await _type_enter(s, probe, site.base + "/a", "검색어")
    assert r.success, _why(r, el)
    assert "navigation_started" in r.data.get("signals", []) and r.reobserve_required, _why(r, el)
    assert el <= _BOUND_S, _why(r, el)
    assert probe.enter[-1][1].get("no_wait_after") is True  # F1
    assert r.data.get("after_enter", {}).get("document") == "navigating", r.data


@requires_chromium
@pytest.mark.parametrize("hang", ["imitate", "capture", "iso", "detect"])
async def test_type_enter_nav_settle_off_hangs_bounded(site, probe, hang):
    """F1(press 이동 대기 흉내)·F2(사후 판독 멈춤)·F3(서버 차단 판정 두 판독 멈춤) — 어느 하나가 5s 멈춰도 ≤0.35s."""
    async with BrowserMCPServer(nav_settle=False) as s:
        await _type_enter(s, probe, site.base + "/a", "검색어")
        if hang == "imitate":
            probe.imitate = True
        else:
            probe.hang[hang] = True
        r, el = await _type_enter(s, probe, site.base + "/a", "검색어")
    assert r.success, _why(r, el)
    assert el <= _BOUND_S, _why(r, el)


@requires_chromium
@pytest.mark.parametrize("hang", ["none", "iso", "detect"])
async def test_press_key_enter_server_path_bounded(site, probe, hang):
    """#244 서버 경로: press_key Enter nav_settle=False — 서버 차단 판정도 키 뒤 마감 안(기준 p50 418.6ms·멈춤 5s)."""
    async with BrowserMCPServer(nav_settle=False) as s:
        await _press_enter(s, probe, site.base + "/a", "검색어")
        if hang != "none":
            probe.hang[hang] = True
        r, el = await _press_enter(s, probe, site.base + "/a", "검색어")
    assert r.success, _why(r, el)
    assert el <= _BOUND_S, _why(r, el)
    doc = r.data.get("after_enter", {}).get("document")
    # 판독이 문서 요청보다 먼저 끝나면(멈춤 없음) same 일 수 있다 — 문서화된 한계(다음 관찰로 확인). 마감까지 기다린 경우는 이동.
    assert doc in (("same", "navigating", "navigated") if hang == "none" else ("navigating", "navigated")), r.data


@requires_chromium
async def test_closed_port_get_submit_keeps_base_failure(site, probe):
    """B3: 닫힌 포트로 GET 제출(요청 실패 → 오류 문서) nav_settle=False — 기준 실패 계열(E_TIMEOUT) 그대로, 거짓 성공 없음."""
    async with BrowserMCPServer(nav_settle=False) as s:
        for _ in range(2):
            r, el = await _type_enter(s, probe, site.base + "/closed", "검색어")
            assert not r.success, _why(r, el)
            assert r.error_code is ErrorCode.TIMEOUT, _why(r, el)
            assert "사후조건 미충족" in (r.error_message or ""), _why(r, el)


@requires_chromium
async def test_nav_settle_on_unchanged_press_waits(site, probe):
    """nav_settle=True: press 는 그대로(no_wait_after 없음)·마감 없음·리스너 없음, 결과는 기준대로 navigated 성공."""
    async with BrowserMCPServer() as s:
        r, el = await _type_enter(s, probe, site.base + "/a", "검색어")
        d = s._dispatcher  # noqa: SLF001
        assert getattr(d, "last_input_deadline", None) is None
        assert d.__dict__.get("_enter_watch") is None
    assert r.success, _why(r, el)
    assert "no_wait_after" not in probe.enter[-1][1]
    assert any(str(x).startswith("navigated:") for x in r.data.get("signals", [])), r.data
    assert r.data.get("after_enter", {}).get("document") == "navigated", r.data


@requires_chromium
async def test_navigate_challenge_unbounded_when_no_enter(site, probe):
    """NAVIGATE 등 다른 행동은 마감 None — 앞 Enter 의 마감이 남아도 차단 판정을 자르지 않는다(바이트 동일)."""
    async with BrowserMCPServer(nav_settle=False) as s:
        await _type_enter(s, probe, site.base + "/a", "검색어")
        await asyncio.sleep(0.5)
        assert (await _call(s, ActionType.NAVIGATE, {"url": site.base + "/plain"})).success
        assert s._dispatcher.last_input_deadline is None  # noqa: SLF001


# ------------------------------------------------------------------ #246: data.after_enter


_NAVER_WANT = {"value": "kept", "field": "same_node", "listbox": "closed", "options": 0, "document": "same"}


@requires_chromium
@pytest.mark.parametrize("nav_settle", [True, False])
@pytest.mark.parametrize("path,want_success,want", [
    ("/naver", True, _NAVER_WANT),
    ("/c2", True, {"value": "kept", "field": "same_node", "listbox": "open", "options": 3, "document": "same"}),
    ("/c3", None, {"value": "unknown", "field": "replaced", "listbox": "none", "options": 0, "document": "same"}),
    ("/c", None, {"value": "cleared", "field": "same_node", "listbox": "open", "options": 3, "document": "same"}),
])
async def test_after_enter_fixtures(site, probe, nav_settle, path, want_success, want):
    """실제 캡처 구조(네이버) — 값 그대로·자동완성 닫힘·문서 그대로가 data 로 보인다. 값 비움·목록 열림·노드 교체."""
    async with BrowserMCPServer(nav_settle=nav_settle) as s:
        r, el = await _type_enter(s, probe, site.base + path, "출발지 입력")
    if want_success is not None:
        assert r.success is want_success, _why(r, el)
    assert r.data.get("after_enter") == want, _why(r, el)
    if path == "/c" and nav_settle:
        # 기준: 값 비움은 nav_settle=True 에서 실패(E_TIMEOUT 실제 '') — 판정 불변, 실패 결과에도 after_enter.
        assert not r.success and r.error_code is ErrorCode.TIMEOUT, _why(r, el)


@requires_chromium
async def test_after_enter_press_key_naver(site, probe):
    """press_key Enter 도 같은 helper — 네이버 구조에서 값 그대로·같은 칸·자동완성 닫힘·문서 그대로."""
    async with BrowserMCPServer(nav_settle=False) as s:
        r, el = await _press_enter(s, probe, site.base + "/naver", "출발지 입력")
    assert r.success, _why(r, el)
    assert r.data.get("after_enter") == {**_NAVER_WANT, "field": "same_node"}, r.data


@requires_chromium
async def test_after_enter_absent_for_sensitive_fields(site, probe, caplog):
    """민감 칸(password·SecretStore 값)은 after_enter 통째 생략 + 응답·로그에 합성 비밀 원문 없음."""
    from security.secrets import SecretStore

    secret = "SynthWs91CedarValue"
    caplog.set_level("DEBUG")
    outs: List[Any] = []
    async with BrowserMCPServer(nav_settle=False) as s:
        d = s._dispatcher  # noqa: SLF001
        d.ctx.secrets = SecretStore({"WS91_SYN_KEY": secret})
        for path, name, text in (("/pw", "암호", secret), ("/plain", "메모", "WS91_SYN_KEY")):
            eid, epoch = await _target(s, site.base + path, name)
            # 서버(게이트) 경로 — 민감 칸 Enter 는 막힌다(after_enter 없음)
            outs.append(await _call(s, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": text,
                                                             "press_enter": True}))
            eid, epoch = await _target(s, site.base + path, name)
            # 디스패처 직접(게이트 없음) — Enter 를 실제로 보낸다
            outs.append(await d.dispatch(ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": text,
                                                              "press_enter": True}))
        listed = await s.call_server_tool("browser_recipe", {"op": "list"})
    assert secret not in json.dumps(listed, ensure_ascii=False)  # 레시피에도 원문 없음
    assert probe.enter, "Enter 를 보낸 경로가 없음"
    for r in outs:
        assert "after_enter" not in r.data, r.data
        env = envelope_json(r)
        assert secret not in env
    assert secret not in caplog.text


# ------------------------------------------------------------------ 이웃


@requires_chromium
async def test_ws88_mutation_refused_nav_settle_off(site, probe):
    """NN4: 입력 중 폼이 POST 로 바뀌면 nav_settle=False 에서도 Enter 를 보내지 않는다(typed_without_enter, 제출 0)."""
    site.posts.clear()
    async with BrowserMCPServer(nav_settle=False) as s:
        r, _ = await _type_enter_noenter_ok(s, probe, site.base + "/mutate", "검색어")
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.error_code, r.error_message)
    assert r.data.get("typed_without_enter") is True
    assert "after_enter" not in r.data
    assert not probe.enter
    await asyncio.sleep(0.2)
    assert site.posts == []


async def _type_enter_noenter_ok(s: BrowserMCPServer, p: _Probe, url: str, name: str) -> Tuple[Any, None]:
    eid, epoch = await _target(s, url, name)
    p.reset()
    r = await _call(s, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "abc", "press_enter": True})
    return r, None


@requires_chromium
async def test_elicitation_path_press_uses_no_wait_after(site, probe):
    """NN4: 승인(elicitation) 경로 press 도 같은 줄(F1) — POST 결제 폼, 결과 400ms 늦게, nav_settle=False."""
    import anyio
    from mcp import types as t
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from interface.mcp_server import create_server

    server, backend = create_server(recipes=False, nav_settle=False)
    out: Dict[str, Any] = {}

    async def elicit(ctx: Any, params: Any) -> Any:
        return t.ElicitResult(action="accept", content={})

    try:
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(ss[0], ss[1], server.create_initialization_options()))
                async with ClientSession(*cs, elicitation_callback=elicit) as session:
                    await session.initialize()
                    await session.call_tool(tool_name(ActionType.NAVIGATE), {"url": site.base + "/post"})
                    obs = await backend.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
                    eid = next(e["element_id"] for e in obs.data["observation"]["elements"] if e["name"] == "수량")
                    probe.reset()
                    res = await session.call_tool(tool_name(ActionType.TYPE_TEXT), {
                        "element_id": eid, "epoch": obs.data["observation"]["snapshot_epoch"], "text": "2",
                        "press_enter": True})
                    out["el"] = time.perf_counter() - probe.t_enter
                    out["env"] = json.loads(res.content[0].text)
                tg.cancel_scope.cancel()
    finally:
        await backend.close()
        backend.hub.close()
    env = out["env"]
    assert env["data"].get("approval", {}).get("via") == "elicitation", env
    assert probe.enter[-1][1].get("no_wait_after") is True, probe.enter
    assert env["success"] is True, env
    assert out["el"] <= _BOUND_S, (out["el"], env)


@requires_chromium
async def test_recipe_records_and_replays_enter_submit(site, probe):
    """NN7: nav_settle=False Enter 제출 성공(navigation_started)이 레시피로 기록되고 재생 1회 성공."""
    async with BrowserMCPServer(headless=True, profile="ws91", nav_settle=False) as s:
        r, el = await _type_enter(s, probe, site.base + "/a", "검색어")
        assert r.success, _why(r, el)
        await asyncio.sleep(0.6)
        listed = await s.call_server_tool("browser_recipe", {"op": "list"})
        auto = [x for x in listed["data"]["recipes"] if x.get("auto")]
        assert auto, listed  # 기준(실패 결과)은 기록 0
        rid, names = auto[0]["id"], auto[0]["params"]
    async with BrowserMCPServer(headless=True, profile="ws91", nav_settle=False) as s2:
        out = await s2.call_server_tool("browser_recipe", {"op": "run", "id": rid, "params": {n: "판교" for n in names}})
        assert out["success"], out
        await asyncio.sleep(0.6)
        assert "/result?q=" in s2._page.url, s2._page.url  # noqa: SLF001


# ------------------------------------------------------------------ 순수 helper


@pytest.mark.parametrize("before,read,doc,css,want", [
    ("a", {"s": True}, "same", None, None),
    ("a", None, "same", None, {"value": "unknown", "field": "unknown", "listbox": "none", "options": 0,
                               "document": "same"}),
    ("a", None, "navigating", None, {"value": "unknown", "field": "unknown", "listbox": "none", "options": 0,
                                     "document": "navigating"}),
    ("a", {"c": True, "v": "a"}, "navigated", None, {"value": "unknown", "field": "gone", "listbox": "none",
                                                      "options": 0, "document": "navigated"}),
    ("a", {"c": False, "v": ""}, "same", True, {"value": "unknown", "field": "replaced", "listbox": "none",
                                                 "options": 0, "document": "same"}),
    ("a", {"c": False, "v": ""}, "same", False, {"value": "unknown", "field": "gone", "listbox": "none",
                                                  "options": 0, "document": "same"}),
    ("a", {"c": True, "v": "b", "ex": None, "linked": 0}, "same", None,
     {"value": "changed", "field": "same_node", "listbox": "none", "options": 0, "document": "same"}),
    ("a", {"c": True, "v": "", "ex": "true", "linked": 0}, "same", None,
     {"value": "cleared", "field": "same_node", "listbox": "open", "options": 0, "document": "same"}),
    ("a", {"c": True, "v": "a", "ex": "true", "linked": 1, "shown": True, "opts": 999}, "same", None,
     {"value": "kept", "field": "same_node", "listbox": "open", "options": 50, "document": "same"}),
    ("a", {"c": True, "v": "a", "ex": "false", "linked": 1, "shown": True, "opts": 3}, "same", None,
     {"value": "kept", "field": "same_node", "listbox": "closed", "options": 0, "document": "same"}),
])
def test_after_enter_view_vocabulary(before, read, doc, css, want):
    assert dmod.after_enter_view(before, read, doc, css) == want


def test_after_enter_view_never_carries_values():
    view = dmod.after_enter_view("SynthBefore", {"c": True, "v": "SynthAfterPlace", "linked": 0}, "same")
    assert "Synth" not in json.dumps(view)
    assert set(view) == {"value", "field", "listbox", "options", "document"}
