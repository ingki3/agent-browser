"""WS-97 (#280·#293) — 늦은 클릭 효과 3초 창(v3: 강한 근거만) + 실패 문구 + 기본 locale ko-KR.

리뷰(.hermes/plans/ws97-evidence/review.md, v3/review.md) fixture 와 검증 반례(.hermes/verify/ws97/reproduce.py 케이스)를
저장소 시험 서버로 옮긴 것. 실제 BrowserMCPServer.call_tool 경로, 로컬 http.server(127.0.0.1)만 쓴다. 공개 사이트 없음.

v3 규칙:
* 효과가 0 일 때만 LATE_EFFECT_WINDOW_MS(3000) 동안 150ms 주기로 다시 본다(정상 클릭 대기 0, 진입 조건은 기준 그대로).
* 창 안 근거는 ① 정상 문서 커밋(오류 문서·about:·같은 주소·reload 제외) ② 이 클릭이 연 새 탭(popup·Page.windowOpen,
  자발 popup 이력이 있는 탭 제외) ③ 창 시작에 잡은 대상 노드의 aria-expanded·aria-pressed·checked·value(class 제외)
  ④ 같은 문서 주소 변화 + history.length 증가(pushState)뿐. 글자·노드·다이얼로그·관계 탭·다른 탭·포커스·class 는 아니다.
* 창이 근거 없이 끝나면 반드시 실패(E_TIMEOUT·retry_safe false·고정 문구), 창 안에서 붙은 dialogs·tabs_changed 는 뗀다.
* 창 + 창 안 이동 대기 ≤ 3초, 서버 차단 판정은 창 시작부터 3.5초까지(넘기면 challenge null + reobserve_required).
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

#: 검증 반례 공통 페이지(independent_probe.py 의 audit): 무반응 Lookup + 자발 타이머(schedule 로 클릭 뒤 700ms 에 시작).
_AUDIT_JS = (
    "let n=0,timer;window.clicks=0;window.ticks=0;"
    "const tick=()=>{window.ticks++;let p=document.getElementById('clock');"
    " if(window.mode==='rotate'){p=document.createElement('p');p.id='clock';document.getElementById('slot').replaceChildren(p)}"
    " if(window.mode==='forge'&&window.__abTopRec)window.__abTopRec.times.clear();"
    " p.textContent='ad '+(++n);};"
    "window.mode='reuse';timer=setInterval(tick,500);"
    "window.schedule=(mode,delay)=>{clearInterval(timer);window.mode=mode;"
    "timer=setTimeout(()=>{tick();timer=setInterval(tick,1000)},delay)};"
    "window.stop=()=>clearInterval(timer);"
    "document.getElementById('noop').addEventListener('click',()=>{window.clicks++});"
)
_AUDIT = ("<input id=q aria-label=Query><button id=noop>Lookup</button>"
          "<button id=open onclick=\"window.open('/other')\">Open</button><div id=slot><p id=clock>ad 0</p></div>"
          "<p id=out></p><script>" + _AUDIT_JS + "</script>")

PAGES: Dict[str, str] = {
    # ⓪ 정상 클릭(즉시 DOM 변경) / ⓪b 정상 target=_blank 링크
    "f0": '<button onclick="document.getElementById(\'m\').textContent=\'완료\'">Go</button><p id=m></p>',
    "f0b": '<a target=_blank href="/t">Go</a>',
    # ① 1.5초 뒤 글자만 / ② 1.5초 뒤 window.open
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
    # ④c/④d 새 탭 첫 응답 5초(19:05 모형) — popup 이벤트는 5초 뒤, opener 세션 Page.windowOpen 은 곧바로
    "f4c": '<a target=_blank href="/slowpage?d=5000">예약하기</a>',
    "f4d": '<a href="#" onclick="event.preventDefault();window.open(\'/slowpage?d=5000\',\'_blank\')">예약하기</a>',
    # ⑤ 무반응 / ⑥b 시계 + ①
    "f5": "<button>Noop</button>",
    "f6b": "<p id=c>0</p><button onclick=\"setTimeout(()=>{document.getElementById('m').textContent='조회 완료'},1500)\">"
           "Lookup</button><p id=m></p><script>let n=0;setInterval(()=>{document.getElementById('c')"
           ".textContent=String(++n)},1000)</script>",
    # ⑧ POST 3초 → 204(문서 없음), 3.3초 뒤 JS 이동
    "f8": '<form method=post action="/slow204?d=3000" onsubmit="setTimeout(()=>location.href=\'/done\',3300)">'
          "<button>Book</button></form>",
    # 진짜 늦은 aria-expanded(1.2초) / 진짜 늦은 checked(사용자 정의 체크 — 1.2초 뒤)
    "aria": "<button id=noop aria-expanded=false onclick=\"setTimeout(()=>this.setAttribute('aria-expanded','true'),"
            "1200)\">Lookup</button>",
    # 검증 반례(independent_probe audit 계열) + key-late
    "audit": _AUDIT,
    "poison-immediate": _AUDIT + "<script>setInterval(()=>{const r=window.__abTopRec;if(r&&!r.__hooked){const old=r.push;"
                                 "r.push=(...a)=>{out.textContent='RECORDER SIDE EFFECT';return old(...a)};"
                                 "r.__hooked=1}},30)</script>",
    "other": "<h1>OTHER</h1><script>window.scheduleNav=(d)=>setTimeout(()=>location.href='/done',d)</script>",
    "late": "<input id=q aria-label=Query><button id=noop onclick=\"setTimeout(()=>out.textContent='Finished',1200)\">"
            "Lookup</button><p id=out></p>",
    # iframe 시계 + 무반응(sibling_probe)
    "frame-outer": '<iframe id=fr src="/frame-inner" width=600 height=400></iframe>',
    "frame-inner": "<button id=noop onclick=\"window.clicks++\">Lookup</button><p id=c>0</p><script>window.clicks=0;"
                   "let n=0;const tick=()=>c.textContent=String(++n);let t=setInterval(tick,500);"
                   "window.schedule=()=>{clearInterval(t);t=setTimeout(()=>{tick();t=setInterval(tick,1000)},700)}</script>",
    # 늦은 오류 문서(302 순환 → chrome-error) / 응답 6초 문서 / 자기 새로고침
    "imm-alert": "<button onclick=\"alert('바로')\">Lookup</button>",
    "late-error": "<button onclick=\"setTimeout(()=>location.href='/loop',700)\">Lookup</button>",
    "late-pending": "<button onclick=\"setTimeout(()=>location.href='/slowpage?d=6000',700)\">Lookup</button>",
    "self-reload": "<button id=noop>Lookup</button><script>window.later=(d)=>setTimeout(()=>location.reload(),d)</script>",
    # v3 리뷰 새 반례(new_probe): 타이머 광고 popup / 버튼 자기 class 토글 / 노드 교체 / replaceState / 해시 타이머
    "popt": "<button id=noop>Lookup</button><script>window.ad=(d)=>setTimeout(()=>window.open('/t'),d)</script>",
    "cls": "<button id=noop class=btn>Lookup</button><script>window.later=(d)=>setTimeout(()=>{"
           "noop.classList.toggle('pulse');setInterval(()=>noop.classList.toggle('pulse'),500)},d)</script>",
    "swap": "<div id=slot><button id=noop class=a>Lookup</button></div><script>let n=0;const sw=()=>{"
            "const b=document.createElement('button');b.id='noop';b.className=(++n%2)?'b':'a';"
            "b.setAttribute('aria-pressed',String(n%2===1));b.textContent='Lookup';slot.replaceChildren(b)};"
            "window.later=(d)=>setTimeout(()=>{sw();setInterval(sw,1000)},d)</script>",
    "rst": "<button id=noop>Lookup</button><script>let n=0;window.later=(d)=>setTimeout(()=>{const f=()=>"
           "history.replaceState(null,'','?slide='+(++n));f();setInterval(f,1000)},d)</script>",
    "hash": "<button id=noop>Lookup</button><script>let n=0;window.later=(d)=>setTimeout(()=>{const f=()=>"
            "{location.hash='slide'+(++n)};f();setInterval(f,1000)},d)</script>",
    # 관계 탭 오류 문서(server_signals 문구 생산자)
    "opener": "<h1>OPENER</h1><button onclick=\"window.open('/pop','_blank','width=400,height=300')\">sized popup</button>",
    "pop": "<h1>POPUP</h1><button onclick=\"opener.location.href='/loop'\">loop opener</button>",
    "t": "<h1>새 탭 대상</h1>",
    "done": "<h1>예약이 확정되었습니다</h1>",
    "lang": "<h1>언어</h1>",
}

NAMES = {"f0": "Go", "f0b": "Go", "f1": "Lookup", "f2": "Open", "f3a": "Book", "f3b": "Book", "f3c": "Book",
         "f3d": "Book", "f4a": "예약하기", "f4b": "예약하기", "f4c": "예약하기", "f4d": "예약하기", "f5": "Noop",
         "f6b": "Lookup", "f8": "Book", "aria": "Lookup", "imm-alert": "Lookup", "late-error": "Lookup", "late-pending": "Lookup"}


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


async def _timed_click(s: BrowserMCPServer, target: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.perf_counter()
    r = await call(s, "click", target)
    r["lat_ms"] = (time.perf_counter() - t0) * 1000
    r["window"] = s._dispatcher.__dict__.get("_late_outcome")
    return r


async def run_fx(s: BrowserMCPServer, base: str, fx: str, think: float = 0.5) -> Dict[str, Any]:
    """fixture 를 열고 관찰 → 생각 시간 → click. 반환: 봉투 + lat_ms + window(창 결과, 미진입이면 None)."""
    await _reset_tabs(s)
    nav = await call(s, "navigate", {"url": f"{base}/{fx}", "wait_until": "load"})
    assert nav["success"], nav
    target = await _element(s, NAMES[fx])
    await asyncio.sleep(think)
    return await _timed_click(s, target)


def _expect_late_failure(r: Dict[str, Any]) -> None:
    assert r["success"] is False, r
    assert r["error_code"] == "E_TIMEOUT", r
    assert r["retry_safe"] is False and r["reobserve_required"] is True, r
    assert r["error_message"] == dmod.click_unsatisfied_message(document_aborted=False), r["error_message"]
    data = r.get("data") or {}
    assert "late_effect" not in data, r  # 새 data 키 없음
    # 창 안에서 붙은 다이얼로그·관계 탭·popup 은 근거도 아니고 실패 봉투에도 남기지 않는다(창 진입 전 기준 봉투와 같게).
    assert not any(k in data for k in ("dialogs", "tabs_changed", "opened_tab_ids", "popup_opened")), data
    assert not any(not str(x).startswith("focus_moved") for x in data.get("signals") or []), data


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


# ------------------------------------------------------------------ 강한 근거: 창 안에서 잡힘


@pytest.mark.parametrize("fx", ["f2", "f3b", "f3c", "f3d", "f4a", "f4b", "f8", "aria"])
async def test_late_strong_effect_within_window_is_success(site, fx):
    async with _server() as s:
        r = await run_fx(s, site["base"], fx)
    assert r["success"] is True, (r.get("error_code"), r.get("error_message"), r.get("data"))
    assert r["window"] == "effect", r["window"]
    signals = r["data"]["signals"]
    if fx in ("f2", "f4a", "f4b"):
        assert any(x.startswith("new_tab") for x in signals), signals
    if fx in ("f2", "f4b"):
        assert r["data"].get("opened_tab_ids"), r["data"]  # 기존 키(첫 응답이 빠른 탭 — popup 이벤트가 창 안에 옴)
    if fx == "f3c":
        assert any(x.startswith("url_changed") for x in signals), signals  # pushState(history.length 증가)
    if fx == "aria":
        assert "element_attr_changed" in signals, signals


@pytest.mark.parametrize("fx", ["f4c", "f4d"])
async def test_slow_first_response_new_tab_via_window_open(site, fx):
    """B/04(19:05 모형): 새 탭 첫 응답 5초 — popup 이벤트는 창 뒤에 오지만 opener 세션 Page.windowOpen 이 곧바로 와서 성공.
    탭 id 는 아직 없을 수 있다(opened_tab_ids 없음) — 새 키 없이 new_tab 신호만, tab_control list 로 확인."""
    async with _server() as s:
        r = await run_fx(s, site["base"], fx)
        assert r["success"] is True, (r.get("error_code"), r.get("error_message"), r.get("data"))
        assert r["window"] == "effect"
        assert any(x.startswith("new_tab") for x in r["data"]["signals"]), r["data"]
        assert r["lat_ms"] < 3000, r["lat_ms"]  # 5초 응답을 기다리지 않았다
        await asyncio.sleep(5.5)
        tabs = (await call(s, "tab_control", {"command": "list"}))["data"]["tabs"]
        assert len(tabs) == 2, tabs


async def test_form_post_303_unchanged_success(site):
    """③a: 클릭이 커밋까지 기다리는 기존 경로 그대로 성공(창과 무관)."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "f3a", think=0.3)
    assert r["success"] is True, r
    assert r["window"] is None
    assert r["data"].get("nav_committed") is True, r


@pytest.mark.parametrize("nav_settle", [True, False])
async def test_capture_error_in_window_keeps_watching(site, monkeypatch, nav_settle):
    """B2: 창 안 판독 예외(문서 교체 중 'Execution context was destroyed')에서 창을 끝내지 않는다 — 다음 주기에 다시 봐서
    1.2초 뒤 aria-expanded(③)를 잡는다. 실제 경합은 드물어 예외를 주입해 고정한다."""
    state = {"raised": 0}
    orig = dmod.ActionDispatcher._late_probe

    class Replacing:
        async def evaluate(self, *_a: Any) -> Any:
            state["raised"] += 1
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")

    async def probe(self, doc: Any, pin: Any, css: str, timeout: float):  # noqa: ANN202
        if css and state["raised"] < 2:
            return await orig(self, Replacing(), pin, css, timeout)  # 실제 판독 함수가 예외를 받는다
        return await orig(self, doc, pin, css, timeout)

    monkeypatch.setattr(dmod.ActionDispatcher, "_late_probe", probe)
    async with _server(nav_settle=nav_settle) as s:
        r = await run_fx(s, site["base"], "aria", think=0.3)
    assert state["raised"] == 2
    assert r["success"] is True, (r.get("error_code"), r.get("error_message"), r.get("data"))


# ------------------------------------------------------------------ 수용한 손실: 글자만 바뀌는 늦은 효과 → 새 문구


async def test_late_text_only_effect_fails_with_new_message(site):
    """①: 1.5초 뒤 글자만 — 창 안 근거가 아니다(fail-closed). E_TIMEOUT + '화면 글자만 바뀌었을 수 있음' 문구."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "f1")
    assert r["window"] == "none"
    _expect_late_failure(r)
    assert "화면 글자만 바뀌었을 수 있음" in r["error_message"]


async def test_key_late_text_effect_fails(site):
    """key-late: press_key 뒤 click 의 1.2초 뒤 글자 변화 — 근거 아님(검증 key-late, 수용한 손실)."""
    async with _server() as s:
        nav = await call(s, "navigate", {"url": site["base"] + "/late", "wait_until": "load"})
        assert nav["success"], nav
        await s._page.locator("#q").focus()
        k = await call(s, "press_key", {"key": "a"})
        assert k["success"], k
        target = await _element(s, "Lookup")
        await asyncio.sleep(0.5)
        r = await _timed_click(s, target)
        assert await s._page.locator("#out").inner_text() == "Finished"  # 효과는 실제로 있었다
    _expect_late_failure(r)


async def test_clock_page_late_text_fails_when_window_entered(site):
    """⑥b 창 경로: 시계 페이지의 1.5초 뒤 글자 — 창에 들어갔으면 실패(즉시 판정이 시계 틱을 잡았으면 기준 그대로 성공)."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "f6b")
    if r["window"] is not None:
        _expect_late_failure(r)
    else:
        assert r["success"] is True, r


async def test_no_effect_click_fails_with_fixed_message(site):
    """⑤ 무반응: 창 끝까지 효과 없음 → E_TIMEOUT + 고정 문구 + retry_safe false."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "f5")
    _expect_late_failure(r)
    assert r["lat_ms"] >= dmod.LATE_EFFECT_WINDOW_MS - 50, r["lat_ms"]


# ------------------------------------------------------------------ 검증·리뷰 반례: 기준과 같은 실패


async def _audit_case(s: BrowserMCPServer, base: str, case: str) -> Dict[str, Any]:
    """independent_probe.py 의 한 케이스 — 클릭 뒤 700ms 에 시작하는 자발 변화 + 무반응 Lookup."""
    use_selector = case.endswith("-selector")
    case = case.removesuffix("-selector")
    route = "poison-immediate" if case == "poison-immediate" else "audit"
    assert (await call(s, "navigate", {"url": f"{base}/{route}", "wait_until": "load"}))["success"]
    main = s._page
    other = None
    if case == "related":
        opened = await call(s, "click", await _element(s, "Open"))
        other = s._core.get_tab(opened["data"]["opened_tab_ids"][-1]).page
    target = await _element(s, "Lookup")
    if use_selector:
        target = {"selector": "#noop"}
    await asyncio.sleep(0.8)
    if case.startswith("key-"):
        await main.locator("#q").focus()
        k = await call(s, "press_key", {"key": "a"})
        assert k["success"], k
    if case in ("timer-rotate", "forge-timer", "key-clock", "poison-immediate"):
        mode = {"timer-rotate": "rotate", "forge-timer": "forge"}.get(case, "reuse")
        await main.evaluate("([m,d])=>window.schedule(m,d)", [mode, 700])
    else:
        await main.evaluate("()=>window.stop()")
    if other is not None:
        await other.evaluate("(d)=>window.scheduleNav(d)", 700)
    if case == "alert":
        await main.evaluate("()=>{setTimeout(()=>alert('UNRELATED TIMER'),700)}")
    r = await _timed_click(s, target)
    r["truth"] = await main.evaluate("()=>({clicks: window.clicks, ticks: window.ticks})")
    if other is not None:
        r["other_url"] = other.url
    return r


@pytest.mark.parametrize("case", ["timer-rotate", "timer-rotate-selector", "related", "alert", "forge-timer",
                                  "poison-immediate", "key-clock"])
async def test_verification_counterexamples_fail_like_base(site, case):
    """검증 FAIL 반례(reproduce.py): 무반응 클릭 + 창 안 자발 변화(광고 노드 교체·관계 탭 이동·무관 alert·시계) — 기준
    (268ebf2)처럼 실패. 창 안에서 붙은 dialogs·tabs_changed 도 없다."""
    async with _server() as s:
        r = await _audit_case(s, site["base"], case)
    assert r["truth"]["clicks"] == 1, r["truth"]  # 클릭은 전달됐다(업무 효과 없음)
    if case in ("timer-rotate", "timer-rotate-selector", "forge-timer", "key-clock", "poison-immediate"):
        assert r["truth"]["ticks"] >= 2, r["truth"]  # 창 안에서 자발 변화가 실제로 있었다
    if case == "related":
        assert r["other_url"].endswith("/done"), r["other_url"]  # 관계 탭은 실제로 이동했다
    assert r["window"] == "none", r["window"]
    _expect_late_failure(r)


async def test_frame_clock_counterexample_fails(site):
    """sibling_probe: iframe 안 같은 노드 시계 + 무반응 — 프레임 컨텍스트 클릭도 실패."""
    async with _server() as s:
        assert (await call(s, "navigate", {"url": site["base"] + "/frame-outer"}))["success"]
        sw = await call(s, "switch_frame", {"frame_selector": "#fr"})
        assert sw["success"], sw
        target = await _element(s, "Lookup")
        frame = s._dispatcher.ctx.page
        await asyncio.sleep(0.8)
        await frame.evaluate("()=>window.schedule()")
        r = await _timed_click(s, target)
        truth = await frame.evaluate("()=>({clicks: window.clicks, clock: document.getElementById('c').textContent})")
    assert truth["clicks"] == 1, truth
    assert r["window"] == "none"
    _expect_late_failure(r)


async def test_late_error_document_is_not_an_effect(site):
    """B3: 클릭 700ms 뒤 302 순환 → chrome-error 문서 커밋 — 근거 아님(기준처럼 E_TIMEOUT)."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "late-error")
        url = s._page.url
    assert url.startswith("chrome-error://"), url  # 오류 문서가 실제로 커밋됐다
    assert r["window"] == "none"
    _expect_late_failure(r)


async def _later_case(s: BrowserMCPServer, base: str, page: str, fn: str = "later") -> Dict[str, Any]:
    assert (await call(s, "navigate", {"url": f"{base}/{page}", "wait_until": "load"}))["success"]
    target = await _element(s, "Lookup")
    await asyncio.sleep(0.5)
    await s._page.evaluate(f"(d)=>window.{fn}(d)", 700)
    return await _timed_click(s, target)


@pytest.mark.parametrize("page", ["cls", "swap", "rst", "hash", "self-reload"])
async def test_spontaneous_strongish_changes_are_not_effects(site, page):
    """v3 리뷰 새 반례: 클릭 뒤 700ms 부터 버튼 자기 class 토글(B/04)·aria 가 다른 새 노드로 교체(노드 신원)·replaceState
    주소 갱신(history.length 그대로)·해시 갱신(조각만)·자기 새로고침(같은 주소 재요청) — 모두 근거가 아니다."""
    async with _server() as s:
        r = await _later_case(s, site["base"], page)
        if page == "self-reload":
            await asyncio.sleep(0.2)
    assert r["window"] == "none", (r["window"], r.get("data"))
    _expect_late_failure(r)


async def test_timer_popup_on_tab_with_spontaneous_popups_is_not_an_effect(site):
    """B/01: 액션 밖에서 popup 을 연 적이 있는 탭(광고 타이머)의 창 안 popup 은 근거가 아니다."""
    async with _server() as s:
        assert (await call(s, "navigate", {"url": site["base"] + "/popt", "wait_until": "load"}))["success"]
        target = await _element(s, "Lookup")
        await s._page.evaluate("(d)=>window.ad(d)", 50)  # 액션 밖 자발 popup
        for _ in range(40):
            if len(s._page.context.pages) >= 2:
                break
            await asyncio.sleep(0.1)
        assert len(s._page.context.pages) >= 2, "자발 popup 이 열리지 않음(전제)"
        await asyncio.sleep(0.3)
        await s._page.evaluate("(d)=>window.ad(d)", 700)  # 클릭과 무관한 다음 광고 popup — 창 안에 온다
        r = await _timed_click(s, target)
        pages = len(s._page.context.pages)
    assert pages >= 3, pages  # 창 안 popup 이 실제로 열렸다
    assert r["window"] == "none", (r["window"], r.get("data"))
    _expect_late_failure(r)


async def test_late_popup_without_spontaneous_history_is_success(site):
    """대조(B/01): 같은 페이지라도 액션 밖 popup 이력이 없으면 창 안 popup 은 근거(②와 같은 경로)."""
    async with _server() as s:
        r = await _later_case(s, site["base"], "popt", fn="ad")
    assert r["success"] is True, (r.get("error_code"), r.get("data"))
    assert r["window"] == "effect"


# ------------------------------------------------------------------ 응답 상한 (B/02)


async def test_late_pending_document_public_latency_is_bounded(site):
    """창 안에서 시작한 문서가 6초 동안 응답하지 않아도 public click 응답 ≤ 3.6초(창 + 이동 대기 ≤ 3초, 서버 차단 판정은
    창 시작부터 3.5초까지 — 잘렸으면 challenge null + reobserve_required)."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "late-pending", think=0.3)
    assert r["window"] == "none"
    # 문서 대기 키(nav_timed_out 등)는 남을 수 있어 _expect_late_failure 대신 같은 항목을 직접 본다.
    assert r["success"] is False and r["error_code"] == "E_TIMEOUT", r
    assert r["retry_safe"] is False and r["reobserve_required"] is True, r
    assert r["error_message"] == dmod.click_unsatisfied_message(document_aborted=False)
    assert r["data"].get("challenge") is None, r["data"]
    assert r["lat_ms"] <= 3600, r["lat_ms"]


# ------------------------------------------------------------------ 즉시 판정·창 진입 조건은 기준 그대로


async def test_immediate_dialog_is_still_an_effect_before_window(site):
    """창 진입 전 즉시 판정(기준 그대로): 클릭 핸들러가 곧바로 띄운 alert 는 지금처럼 효과(dialog_opened) — 창에 들어가지
    않고 data.dialogs 도 그대로 실린다(기준 즉시 결과의 키)."""
    async with _server() as s:
        r = await run_fx(s, site["base"], "imm-alert", think=0.3)
    assert r["success"] is True, r
    assert r["window"] is None
    assert r["data"].get("dialogs"), r["data"]
    assert any(x.startswith("dialog_opened") for x in r["data"]["signals"]), r["data"]


def test_window_entry_condition_is_base_bytes():
    """창 진입 조건(효과 0 일 때만)과 즉시 판정 코드는 기준(268ebf2)과 같은 줄이다 — 창 호출부만 바뀌었다."""
    import inspect

    src = inspect.getsource(dmod.ActionDispatcher._observe_element_effect)
    head, _, _ = src.partition("await self._late_effect_window(")
    assert "if not popups and action is ActionType.CLICK and not self._nav_info.get(\"nav_committed\") \\\n" \
           "                and not self.__dict__.get(\"_related_navs\"):" in head
    assert "if not verify_post_condition(before, after).satisfied and not (\n" \
           "                self._effect_beyond_target(root_before, root_after, weak_ok=True)\n" \
           "            ):" in head
    assert "_main_rec" not in inspect.getsource(dmod.ActionDispatcher)  # 주 프레임 기록기 없음(v3)
    assert "if (n && n.nodeType === 11 && n.host)" not in dmod._TOP_RECORDER_ARM_JS  # 기록기 JS 는 기준 그대로


# ------------------------------------------------------------------ 문구


def test_message_is_fixed_text():
    base = dmod.click_unsatisfied_message(document_aborted=False)
    aborted = dmod.click_unsatisfied_message(document_aborted=True)
    assert base == ("사후조건 미충족(클릭 뒤 3초 동안 확인 가능한 효과(이동·새 탭·요소 상태)가 보이지 않음 — 화면 글자만 "
                    "바뀌었을 수 있음). click 은 재시도 시 부작용이 중복될 수 있어 중단합니다 — 다시 누르기 전에 "
                    "observe_page·tab_control list 로 결과를 확인하세요.")
    assert aborted == ("사후조건 미충족(문서 요청은 보냈지만 새 문서가 열리지 않음 — 응답 204·중단). "
                       "click 은 재시도 시 부작용이 중복될 수 있어 중단합니다 — 다시 누르기 전에 observe_page·tab_control "
                       "list 로 결과를 확인하세요.")


async def test_nav_aborted_click_uses_aborted_message(site, monkeypatch):
    """⑧ 에서 뒤따른 JS 이동이 창 밖이면(창을 줄여 재현) 문서 요청 204 중단 판 문구."""
    monkeypatch.setattr(dmod, "LATE_EFFECT_WINDOW_MS", 150)
    async with _server() as s:
        r = await run_fx(s, site["base"], "f8", think=0.3)
    assert r["success"] is False and r["error_code"] == "E_TIMEOUT", r
    assert r["retry_safe"] is False and r["reobserve_required"] is True, r
    assert r["data"].get("nav_aborted"), r
    assert r["error_message"] == dmod.click_unsatisfied_message(document_aborted=True), r


async def test_both_message_producers_are_the_same(site):
    """B4: dispatcher(_retry_unsatisfied)와 server_signals(관계 탭 오류 문서) 두 click 문구 생산자가 같은 함수."""
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
    assert rel["data"].get("tabs_changed"), rel  # 즉시(창 진입 전) 관계 탭 이동 — 기준 봉투 그대로
    assert rel["error_message"] == direct["error_message"] == dmod.click_unsatisfied_message(document_aborted=False)


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


@pytest.mark.parametrize("bad", ["", "ko_KR", "korean language", "ko-", "x" * 40, "ko;q=1", "ko-KR\n", "en-US\n"])
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
    with pytest.raises(SystemExit):
        p.parse_args(["serve", "--locale", "ko-KR\n"])
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
