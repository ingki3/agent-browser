"""WS-97 (#280·#293) — 늦은 클릭 효과 3초 창 + 주 프레임 귀속 + 실패 문구 + 기본 locale ko-KR.

리뷰(.hermes/plans/ws97-evidence/review.md) fixture ⓪~⑧(fxserver.py) 를 저장소 시험 서버로 옮긴 것. 실제
BrowserMCPServer.call_tool 경로, 로컬 http.server(127.0.0.1)만 쓴다. 공개 사이트 접속 없음.

규칙(리뷰 B1~B6):
* 효과가 0 일 때만 LATE_EFFECT_WINDOW_MS(3000) 동안 150ms 주기로 다시 본다(정상 클릭 대기 0).
* 창 안 약한 신호(text_changed·dom_delta)는 주 프레임 기록기 귀속을 통과할 때만 효과. 창이 효과 없이 끝나면
  최종 판정도 약한 신호만으로는 성공하지 않는다. 즉시 판정(창 진입 전)에는 귀속을 적용하지 않는다(#294).
* 실패 문구는 click 두 생산자(dispatcher·server_signals)가 같은 함수를 쓴다. E_TIMEOUT·retry_safe false 유지.
* serve 기본 locale ko-KR(navigator.language·Accept-Language), --locale none 이면 기존 동작(en-US, 머리 없음).
"""

from __future__ import annotations

import asyncio
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import pytest

from actions import dispatcher as dmod
from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, envelope_dict

from test_run_cli import requires_chromium  # noqa: F401

pytestmark = requires_chromium

_LANG = ("<p id=lang></p><script>document.getElementById('lang').textContent="
         "navigator.language+'|'+new Intl.DateTimeFormat().resolvedOptions().locale</script>")

PAGES: Dict[str, str] = {
    # ⓪ 정상 클릭(즉시 DOM 변경) / ⓪b 정상 target=_blank 링크
    "f0": '<button onclick="document.getElementById(\'m\').textContent=\'완료\'">Go</button><p id=m></p>',
    "f0b": '<a target=_blank href="/t">Go</a>',
    # ① 1.5초 뒤 DOM 변경 / ② 1.5초 뒤 window.open
    "f1": '<button onclick="setTimeout(()=>{document.getElementById(\'m\').textContent=\'조회 완료\'},1500)">Lookup</button>'
          '<p id=m></p>',
    "f2": '<button onclick="setTimeout(()=>window.open(\'/t\'),1500)">Open</button>',
    # ③a form POST 2.8초 → 303 / ③b fetch 2.8초 → location / ③c fetch 2.8초 → pushState+렌더 / ③d fetch 1.9초 → location
    "f3a": '<form method=post action="/slow303?d=2800"><input name=x value=1><button>Book</button></form>',
    "f3b": '<button onclick="fetch(\'/api?d=2800\',{method:\'POST\'}).then(()=>location.href=\'/done\')">Book</button>',
    "f3c": '<button onclick="fetch(\'/api?d=2800\',{method:\'POST\'}).then(()=>{history.pushState({},\'\','
           '\'/my/bookings/1?popup=bookingCompletion\');document.body.innerHTML=\'<h1>예약 완료</h1>\'})">Book</button>',
    "f3d": '<button onclick="fetch(\'/api?d=1900\',{method:\'POST\'}).then(()=>location.href=\'/done\')">Book</button>',
    # ④a _blank 새 탭 첫 응답 2.5초 / ④b _blank 막고 fetch 1초 뒤 window.open
    "f4a": '<a target=_blank href="/slowpage?d=2500">예약하기</a>',
    "f4b": '<a target=_blank href="/t" onclick="event.preventDefault();fetch(\'/api?d=1000\')'
           '.then(()=>window.open(\'/t\',\'_blank\'))">예약하기</a>',
    # ⑤ 무반응 / ⑥ 1초 시계 + 무반응 / ⑥b 시계 + ① / ⑥c 250ms 광고 + 무반응
    "f5": "<button>Noop</button>",
    "f6": "<p id=c>0</p><button>Noop</button><script>let n=0;setInterval(()=>{document.getElementById('c')"
          ".textContent=String(++n)},1000)</script>",
    "f6b": "<p id=c>0</p><button onclick=\"setTimeout(()=>{document.getElementById('m').textContent='조회 완료'},1500)\">"
           "Lookup</button><p id=m></p><script>let n=0;setInterval(()=>{document.getElementById('c')"
           ".textContent=String(++n)},1000)</script>",
    "f6c": "<p id=c>0</p><button>Noop</button><script>let n=0;setInterval(()=>{document.getElementById('c')"
           ".textContent='ad'+(++n)},250)</script>",
    # ⑥d(결정적 ⑥): 1초 시계 + 시계만 다시 맞추는 버튼 — 클릭 뒤 첫 틱이 700ms 뒤(즉시 판정 뒤)라 반드시 창에 들어가고,
    # 창 안 변화는 기준선에서도 바뀌던 그 시계 노드뿐(자발 변화)이다.
    "f6d": "<p id=c>0</p><button onclick=\"clearInterval(t);setTimeout(()=>{tick();t=setInterval(tick,1000)},700)\">"
           "Noop</button><script>let n=0;const tick=()=>{document.getElementById('c').textContent=String(++n)};"
           "let t=setInterval(tick,1000)</script>",
    # press_key 가 바꾼 노드(#m)를 click 의 1.5초 뒤 효과가 다시 바꿈(WS-78 왕복 예산으로 press_key 뒤 arm 생략 — 낡은 기준선)
    "fkey": "<input id=q aria-label=검색어><button onclick=\"setTimeout(()=>{m.textContent='조회 완료'},1500)\">Lookup</button>"
            "<p id=m></p><script>const m=document.getElementById('m');let k=0;document.getElementById('q')"
            ".addEventListener('keydown',()=>{m.textContent='입력 '+(++k)})</script>",
    # ⑧ POST 3초 → 204(문서 없음), 3.3초 뒤 JS 이동
    "f8": '<form method=post action="/slow204?d=3000" onsubmit="setTimeout(()=>location.href=\'/done\',3300)">'
          "<button>Book</button></form>",
    # B5①: open shadow 안 버튼이 1.5초 뒤 shadow 안 글자를 바꿈 + 가벼운 1초 시계(자발 변화)
    "fshadow": "<p id=c>0</p><div id=host></div><script>const r=document.getElementById('host')"
               ".attachShadow({mode:'open'});r.innerHTML='<button>Shadow</button><p id=sm></p>';"
               "r.querySelector('button').onclick=()=>setTimeout(()=>{r.getElementById('sm').textContent="
               "'그림자 완료'},1500);let n=0;setInterval(()=>{document.getElementById('c').textContent=String(++n)},1000)"
               "</script>",
    # 관계 탭 오류 문서(server_signals 문구 생산자)
    "opener": "<h1>OPENER</h1><button onclick=\"window.open('/pop','_blank','width=400,height=300')\">sized popup</button>",
    "pop": "<h1>POPUP</h1><button onclick=\"opener.location.href='/loop'\">loop opener</button>",
    # B5②: load 50ms 뒤 응답 없는 문서로 이동
    "to_never": "<h1>FIRST</h1><script>addEventListener('load',()=>setTimeout(()=>{location.href='/hangdoc'},50))"
                "</script>",
    "t": "<h1>새 탭 대상</h1>",
    "done": "<h1>예약이 확정되었습니다</h1>",
    "lang": "<h1>언어</h1>",
}

NAMES = {"f0": "Go", "f0b": "Go", "f1": "Lookup", "f2": "Open", "f3a": "Book", "f3b": "Book", "f3c": "Book",
         "f3d": "Book", "f4a": "예약하기", "f4b": "예약하기", "f5": "Noop", "f6": "Noop", "f6b": "Lookup", "f6c": "Noop",
         "f6d": "Noop", "f8": "Book", "fshadow": "Shadow"}


def _html(body: str) -> bytes:
    return ("<!doctype html><html><head><meta charset=utf-8><title>fx</title></head><body>" + body + _LANG
            + "</body></html>").encode()


@pytest.fixture
def site():
    seen: List[Optional[str]] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # noqa: ANN401
            pass

        def _send(self, code: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if body:
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _route(self) -> None:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            d = int((q.get("d") or ["0"])[0]) / 1000
            name = u.path.strip("/") or "f5"
            if name != "robots.txt":  # robots 는 서버(파이썬)가 직접 읽는다 — 브라우저 요청만 센다
                seen.append(self.headers.get("Accept-Language"))
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            try:
                if name == "slow303":
                    time.sleep(d)
                    return self._send(303, headers={"Location": "/done"})
                if name == "slow204":
                    time.sleep(d)
                    return self._send(204)
                if name == "api":
                    time.sleep(d)
                    return self._send(200, b"{}")
                if name == "slowpage":
                    time.sleep(d)
                    return self._send(200, _html("<h1>slow</h1>"))
                if name == "loop":
                    return self._send(302, headers={"Location": "/loop"})
                if name == "hangdoc":
                    time.sleep(6)
                    return self._send(200, _html("<h1>late</h1>"))
                if name == "robots.txt":
                    return self._send(200, b"User-agent: *\nAllow: /\n")
                if name.startswith("my/"):
                    return self._send(200, _html("<h1>예약 완료</h1>"))
                if name in PAGES:
                    return self._send(200, _html(PAGES[name]))
                return self._send(404, b"nf")
            except (BrokenPipeError, ConnectionResetError):
                return None

        do_GET = _route
        do_POST = _route

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "seen": seen}
    finally:
        srv.shutdown()
        srv.server_close()


def _server(**kw: Any) -> BrowserMCPServer:
    return BrowserMCPServer(recipes=False, headless_retry=False, **kw)


async def call(s: BrowserMCPServer, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return envelope_dict(await s.call_tool("browser_" + name, dict(args or {})))


async def _element(s: BrowserMCPServer, name: str) -> Dict[str, Any]:
    o = await call(s, "observe_page", {})
    ob = o["data"]["observation"]
    el = next(e for e in ob["elements"] if (e.get("name") or "").strip() == name)
    return {"element_id": el["element_id"], "epoch": ob["snapshot_epoch"]}


async def _reset_tabs(s: BrowserMCPServer) -> None:
    tl = (await call(s, "tab_control", {"command": "list"})).get("data") or {}
    tabs = tl.get("tabs") or []
    for t in tabs[1:]:
        await call(s, "tab_control", {"command": "close", "tab_id": t["tab_id"]})
    if tabs:
        await call(s, "tab_control", {"command": "switch", "tab_id": tabs[0]["tab_id"]})


async def run_fx(s: BrowserMCPServer, base: str, fx: str, think: float = 2.2) -> Dict[str, Any]:
    """fixture 를 열고 관찰 → 생각 시간(기준선) → click. 반환: 봉투 + lat_ms."""
    await _reset_tabs(s)
    nav = await call(s, "navigate", {"url": f"{base}/{fx}", "wait_until": "load"})
    assert nav["success"], nav
    target = await _element(s, NAMES[fx])
    await asyncio.sleep(think)
    t0 = time.perf_counter()
    r = await call(s, "click", target)
    r["lat_ms"] = (time.perf_counter() - t0) * 1000
    return r


def _expect_late_failure(r: Dict[str, Any]) -> None:
    assert r["success"] is False, r
    assert r["error_code"] == "E_TIMEOUT", r
    assert r["retry_safe"] is False and r["reobserve_required"] is True, r
    assert r["error_message"] == dmod.click_unsatisfied_message(nav_aborted=False), r["error_message"]
    assert "late_effect" not in (r.get("data") or {}), r  # 새 data 키 없음


# ------------------------------------------------------------------ 정상 클릭: 창 미진입, 지연 불변


@pytest.mark.parametrize("fx", ["f0", "f0b"])
async def test_normal_click_succeeds_without_entering_window(site, fx, monkeypatch):
    entered: List[int] = []
    orig = dmod.ActionDispatcher._late_effect_window

    async def spy(self, *a: Any, **k: Any):  # noqa: ANN202
        entered.append(1)
        return await orig(self, *a, **k)

    monkeypatch.setattr(dmod.ActionDispatcher, "_late_effect_window", spy)
    async with _server() as s:
        lat3000 = []
        for _ in range(2):
            r = await run_fx(s, site["base"], fx, think=0.3)
            assert r["success"] is True, r
            lat3000.append(r["lat_ms"])
        assert entered == []
        monkeypatch.setattr(dmod, "LATE_EFFECT_WINDOW_MS", 0)
        lat0 = []
        for _ in range(2):
            r = await run_fx(s, site["base"], fx, think=0.3)
            assert r["success"] is True, r
            lat0.append(r["lat_ms"])
    assert abs(min(lat3000) - min(lat0)) < 100, (lat3000, lat0)


# ------------------------------------------------------------------ 늦은 효과: 창 안에서 잡힘


@pytest.mark.parametrize("fx", ["f1", "f2", "f3b", "f3c", "f3d", "f4a", "f4b", "f8"])
async def test_late_effect_within_window_is_success(site, fx):
    async with _server() as s:
        r = await run_fx(s, site["base"], fx, think=0.5)
    assert r["success"] is True, (r.get("error_code"), r.get("error_message"), r.get("data"))
    assert r.get("error_code") is None


@pytest.mark.parametrize("nav_settle", [True, False])
async def test_capture_error_in_window_keeps_watching(site, monkeypatch, nav_settle):
    """B2: 창 안 상태 캡처 예외(문서 교체 중 'Execution context was destroyed')에서 창을 끝내지 않는다 — 다음 주기에
    다시 봐서 1.5초 뒤 효과(①)를 잡는다. 실제 경합(③d)은 창 안 _NavWatch 가 먼저 잡아 드물어서 예외를 주입해 고정한다."""
    state = {"in_window": False, "raised": 0}
    orig_window = dmod.ActionDispatcher._late_effect_window
    orig_capture = dmod.ActionDispatcher._capture_state

    async def window(self, *a: Any, **k: Any):  # noqa: ANN202
        state["in_window"] = True
        try:
            return await orig_window(self, *a, **k)
        finally:
            state["in_window"] = False

    async def capture(self, *a: Any, **k: Any):  # noqa: ANN202
        if state["in_window"] and state["raised"] < 2:
            state["raised"] += 1
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
        return await orig_capture(self, *a, **k)

    monkeypatch.setattr(dmod.ActionDispatcher, "_late_effect_window", window)
    monkeypatch.setattr(dmod.ActionDispatcher, "_capture_state", capture)
    async with _server(nav_settle=nav_settle) as s:
        r = await run_fx(s, site["base"], "f1", think=0.3)
    assert state["raised"] == 2
    assert r["success"] is True, (r.get("error_code"), r.get("error_message"), r.get("data"))


async def test_late_effect_on_node_changed_by_press_key_is_success(site):
    """press_key 뒤에는 주 프레임 기록기를 다시 붙이지 않는다(WS-78 R3 왕복 예산). 그 키가 바꾼 노드를 다음 click 의 늦은
    효과가 다시 바꾸면, 낡은 기준선으로 '자발'이라 버리지 않고 귀속 판정 불가(B5①) → 기존 판정(효과 인정)."""
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["base"] + "/fkey", "wait_until": "load"})
        assert nav["success"], nav
        await s._page.locator("#q").focus()
        k = await call(s, "press_key", {"key": "a"})
        assert k["success"], k
        assert await s._page.locator("#m").inner_text() == "입력 1"
        assert s._dispatcher._main_rec_stale is True  # arm 생략 = 낡은 기준선으로 적힘
        target = await _element(s, "Lookup")
        await asyncio.sleep(1.0)
        r = await call(s, "click", target)
        stale_after_click = s._dispatcher._main_rec_stale
    assert r["success"] is True, (r.get("error_code"), r.get("error_message"), r.get("data"))
    assert "text_changed" in r["data"]["signals"], r
    assert stale_after_click is False  # 성공한 click 뒤 다시 붙임 = 새 기준선


async def test_clock_page_after_click_rearm_is_not_stale(site):
    """대조: press_key 가 아닌 액션(navigate) 뒤에는 기준선이 새로 붙어 낡지 않다 — ⑥d 귀속이 그대로 동작한다."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "f6d")
        assert s._dispatcher.__dict__.get("_main_rec_stale") is False
    _expect_late_failure(r)


async def test_form_post_303_unchanged_success(site):
    """③a: 클릭이 커밋까지 기다리는 기존 경로 그대로 성공(창과 무관)."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "f3a", think=0.3)
    assert r["success"] is True, r
    assert r["data"].get("nav_committed") is True, r


async def test_clock_page_with_real_late_effect_is_success(site):
    """⑥b: 자발 변화(1초 시계)가 있어도 액션 창에서만 바뀐 노드(진짜 늦은 효과)는 귀속을 통과한다."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "f6b")
    assert r["success"] is True, (r.get("error_code"), r.get("error_message"), r.get("data"))


async def test_open_shadow_late_effect_is_attributed(site):
    """B5①: open shadow 안의 1.5초 뒤 효과 — 기록기가 shadow root 도 보므로 시계(자발)와 구분돼 성공."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "fshadow")
    assert r["success"] is True, (r.get("error_code"), r.get("error_message"), r.get("data"))


# ------------------------------------------------------------------ 무반응: 창 끝까지 효과 없음 → 새 문구


async def test_no_effect_click_fails_with_fixed_message(site):
    """⑤ 무반응: 창 끝까지 효과 없음 → E_TIMEOUT + 고정 문구 + retry_safe false."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "f5")
    _expect_late_failure(r)
    assert r["lat_ms"] >= dmod.LATE_EFFECT_WINDOW_MS - 50, r["lat_ms"]


async def test_spontaneous_change_in_window_is_not_an_effect(site):
    """⑥d(결정적 ⑥): 창 안 변화가 자발 변화(시계)뿐 — 창 안 귀속과 최종 판정 귀속(B1-③)으로 E_TIMEOUT."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "f6d")
    _expect_late_failure(r)


@pytest.mark.parametrize("fx", ["f6", "f6c"])
async def test_spontaneous_pages_fail_when_window_entered(site, fx, monkeypatch):
    """⑥·⑥c: 창에 들어갔으면 E_TIMEOUT + 새 문구. 즉시 판정에서 이미 자발 변화가 잡혀 창에 들어가지 않았으면 기준 판정
    그대로(즉시 성공) — 즉시 판정 귀속은 #294 로 분리(코디네이터 결정 A)."""
    entered: List[int] = []
    orig = dmod.ActionDispatcher._late_effect_window

    async def spy(self, *a: Any, **k: Any):  # noqa: ANN202
        entered.append(1)
        return await orig(self, *a, **k)

    monkeypatch.setattr(dmod.ActionDispatcher, "_late_effect_window", spy)
    async with _server() as s:
        r = await run_fx(s, site["base"], fx)
    if entered:
        _expect_late_failure(r)
    else:
        assert r["success"] is True, r
        assert any(str(x).startswith(("text_changed", "dom_delta")) for x in r["data"]["signals"]), r


def test_message_is_fixed_text():
    base = dmod.click_unsatisfied_message(nav_aborted=False)
    aborted = dmod.click_unsatisfied_message(nav_aborted=True)
    assert base == ("사후조건 미충족(클릭 뒤 3초 동안 URL/DOM/텍스트/속성/값/탭 변화가 보이지 않음 — Silent Failure 의심). "
                    "click 은 재시도 시 부작용이 중복될 수 있어 중단합니다 — 다시 누르기 전에 observe_page·tab_control "
                    "list 로 결과를 확인하세요.")
    assert aborted == ("사후조건 미충족(문서 요청은 보냈지만 새 문서가 열리지 않음 — 응답 204·중단). "
                       "click 은 재시도 시 부작용이 중복될 수 있어 중단합니다 — 다시 누르기 전에 observe_page·tab_control "
                       "list 로 결과를 확인하세요.")


async def test_nav_aborted_click_uses_aborted_message(site, monkeypatch):
    """⑧ 에서 뒤따른 JS 이동이 창 밖이면(창을 줄여 재현) 문서 요청 204 중단 판 문구 — '어디에도 변화 없음' 아님."""
    monkeypatch.setattr(dmod, "LATE_EFFECT_WINDOW_MS", 150)
    async with _server() as s:
        r = await run_fx(s, site["base"], "f8", think=0.3)
    assert r["success"] is False and r["error_code"] == "E_TIMEOUT", r
    assert r["retry_safe"] is False and r["reobserve_required"] is True, r
    assert r["data"].get("nav_aborted"), r
    assert r["error_message"] == dmod.click_unsatisfied_message(nav_aborted=True), r


async def test_both_message_producers_are_the_same(site):
    """B4: dispatcher(_retry_unsatisfied)와 server_signals(관계 탭 오류 문서) 두 click 문구 생산자가 같다."""
    async with _server() as s:
        direct = await run_fx(s, site["base"], "f5", think=0.3)
        await _reset_tabs(s)
        await call(s, "navigate", {"url": site["base"] + "/opener"})
        opened = await call(s, "click", await _element(s, "sized popup"))
        pop = opened["data"]["opened_tab_ids"][-1]
        await call(s, "tab_control", {"command": "switch", "tab_id": pop})
        rel = await call(s, "click", await _element(s, "loop opener"))
    assert direct["success"] is False and rel["success"] is False, (direct, rel)
    assert rel["error_code"] == direct["error_code"] == "E_TIMEOUT"
    assert rel["data"].get("tabs_changed"), rel
    assert rel["error_message"] == direct["error_message"] == dmod.click_unsatisfied_message(nav_aborted=False)


# ------------------------------------------------------------------ B5②: 응답 없는 문서로 가는 navigate 시간 기준


async def test_navigate_to_never_answering_document_keeps_time_bound(site):
    """B5②: 액션 뒤 주 프레임 기록기 arm 이 응답 없는 문서로 가는 navigate 를 붙잡지 않는다(상한 100ms).

    디스패처 단독 경로로 잰다(WS-92 시험과 같은 기준 — 서버 경로는 기준(268ebf2)에서도 응답 없는 문서 요청이 끝날
    때까지 ~6초라 이 기준의 대상이 아니다)."""
    from test_ws92_client_redirect import _direct

    r, _, elapsed, _ = await _direct(site["base"] + "/to_never", timeout_ms=1500)
    assert r.success is False and r.error_code.value == "E_NAVIGATE_TIMEOUT", (r.error_code, r.data)
    assert elapsed < 1.5 + 0.5, elapsed


# ------------------------------------------------------------------ #293 locale


async def _read_lang(s: BrowserMCPServer, site: Dict[str, Any]) -> Dict[str, Any]:
    site["seen"].clear()
    nav = await call(s, "navigate", {"url": site["base"] + "/lang", "wait_until": "load"})
    assert nav["success"], nav
    x = await call(s, "extract", {"selector": "#lang"})
    text = str(x.get("data") or {})
    return {"text": text, "accept": list(site["seen"])}


def _assert_ko(v: Dict[str, Any]) -> None:
    assert "ko-KR|ko-KR" in v["text"], v
    assert v["accept"] and all(a == "ko-KR" for a in v["accept"]), v


async def test_default_serve_locale_is_ko_kr(site):
    async with _server() as s:
        _assert_ko(await _read_lang(s, site))


async def test_persistent_profile_locale_is_ko_kr(site):
    async with _server(profile="ws97-locale") as s:
        _assert_ko(await _read_lang(s, site))


async def test_on_demand_reopen_keeps_locale(site, tmp_path):
    async with _server(browser_mode="on-demand", handoff_root=tmp_path / "servers") as s:
        _assert_ko(await _read_lang(s, site))
        out = await s._switch_window(False, "ws97_test", force=True)
        assert out["ok"] and out["reopened"], out
        _assert_ko(await _read_lang(s, site))


async def test_locale_none_keeps_previous_behaviour(site):
    async with _server(locale=None) as s:
        v = await _read_lang(s, site)
    assert "en-US|en-US" in v["text"], v
    assert v["accept"] and all(a is None for a in v["accept"]), v


@pytest.mark.parametrize("bad", ["", "ko_KR", "korean language", "ko-", "x" * 40, "ko;q=1"])
def test_invalid_locale_rejected(bad):
    from browser.core import BrowserCore, parse_locale

    with pytest.raises(ValueError):
        parse_locale(bad)
    with pytest.raises(ValueError):
        BrowserCore(locale=bad)
    with pytest.raises(ValueError):
        BrowserMCPServer(locale=bad)


def test_parse_locale_values():
    from browser.core import DEFAULT_LOCALE, parse_locale

    assert DEFAULT_LOCALE == "ko-KR"
    assert parse_locale("ko-KR") == "ko-KR"
    assert parse_locale("en-US") == "en-US"
    assert parse_locale("zh-Hant-TW") == "zh-Hant-TW"
    assert parse_locale("none") is None
    assert parse_locale("NONE") is None


def test_serve_cli_locale_flag(monkeypatch):
    from interface import cli

    p = cli._build_parser()
    assert p.parse_args(["serve"]).locale == "ko-KR"
    assert p.parse_args(["serve", "--locale", "en-US"]).locale == "en-US"
    assert p.parse_args(["serve", "--locale", "none"]).locale is None
    with pytest.raises(SystemExit):
        p.parse_args(["serve", "--locale", "ko_KR"])
    seen: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        seen.update(kw)

    import interface.mcp_server as ms

    monkeypatch.setattr(ms, "run_stdio", fake_run_stdio)
    assert cli.main(["serve", "--locale", "none"]) == 0
    assert seen["locale"] is None
    seen.clear()
    assert cli.main(["serve"]) == 0
    assert seen.get("locale", "ko-KR") == "ko-KR"


def test_context_options_same_locale_regardless_of_human_like():
    from browser.core import BrowserCore

    assert BrowserCore()._context_options() == {"viewport": {"width": 1280, "height": 720}, "locale": "ko-KR"}
    assert BrowserCore(human_like=True)._context_options() == {"no_viewport": True, "locale": "ko-KR"}
    assert BrowserCore(locale="en-GB", human_like=True)._context_options() == {"no_viewport": True,
                                                                               "locale": "en-GB"}
    assert BrowserCore(locale=None)._context_options() == {"viewport": {"width": 1280, "height": 720}}


def test_contract_action_enum_untouched():
    assert ActionType.CLICK.value == "click"
