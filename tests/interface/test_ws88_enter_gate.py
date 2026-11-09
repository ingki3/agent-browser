"""WS-88 (#248): Enter-on-field 판정 — type_text(press_enter)·press_key(Enter)·개행 입력이 같은 판정을 쓴다.

실제 BrowserMCPServer(헤드리스 Chromium) + 로컬 HTTP fixture 만 쓴다(공개 사이트 없음). 서버는 받은 요청(메서드·경로)을
기록해 '제출이 실제로 일어났는가'를 본다.

Enter 전용 census(리뷰 B7):
* 위험군(RISK) — POST 로그인·결제, 메서드 없는 JS 로그인 폼(p1), GET+formmethod=post(p2), hidden op=delete_account(p3),
  민감 칸 각 종류(B2), 폼 안 다른 출처 iframe, form= 속성 연결(NN6), activedescendant 위험 항목(NN2), 페이지 world 위조
  (격리/페이지 불일치), 입력 중 폼·칸 변조(p8·p9, B4), 입력 중 문서 교체(B5) — **통과 0**.
* 정상군(NORMAL) — 폼 밖 combobox·searchbox, method=get 검색 폼, 지도 자동완성 fixture — **승인 0**, 남는 거부는
  KNOWN_HIGH 로 기록(NN1).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401

HEAD = "<!doctype html><meta charset=utf-8><title>{t}</title>"
OUT = "<p id=out>대기</p>"
#: 키 핸들러: Enter 가 그 칸에 닿았으면 #out 에 표시(폼 밖 칸의 '효과' 확인).
ENTER_MARK = ("<script>document.getElementById('t').addEventListener('keydown', (e) => {"
              "if (e.key === 'Enter') document.getElementById('out').textContent = 'enter:' + e.target.value; });"
              "</script>")
COMBO = ("<div role=listbox id=lb><div role=option id=o1>서울역</div><div role=option id=o2>부산역</div></div>")


def _page(title: str, body: str, mark: bool = True) -> str:
    return HEAD.format(t=title) + body + OUT + (ENTER_MARK if mark else "")


#: 위험군: (경로, 페이지, 제출로 보는 요청 경로 접두). 대상 칸은 언제나 id=t, 이름은 aria-label.
RISK: Dict[str, Tuple[str, str]] = {
    "/r_post_login": (_page("로그인", "<form method=post action=/login><input id=t name=email aria-label='이메일'>"
                                     "<input type=password name=pw aria-label='암호'><button>들어가기</button></form>"),
                      "/login"),
    "/r_post_pay": (_page("결제", "<form method=post action=/order/pay><input id=t name=qty aria-label='수량'>"
                                 "<button>진행</button></form>"), "/order"),
    # p1: method 속성 없는 JS 로그인 폼(React 류) — form.method 는 'get' 으로 읽히지만 명시가 아니다.
    "/r_js_login": (_page("JS", "<form id=f><input id=t name=email aria-label='이메일 주소'></form>"
                               "<script>document.getElementById('f').addEventListener('submit', (e) => {"
                               "e.preventDefault(); fetch('/api/login', {method: 'POST'}); });</script>"), "/api"),
    # p2: GET 폼이지만 기본 제출 버튼이 formmethod=post formaction=/pay.
    "/r_formmethod": (_page("검색", "<form method=get action=/search><input id=t name=q aria-label='검색어'>"
                                   "<button formmethod=post formaction=/billing>검색</button></form>"), "/billing"),
    # p3: GET 폼 hidden op=delete_account.
    "/r_hidden_op": (_page("계정", "<form method=get action=/acct><input type=hidden name=op value=delete_account>"
                                  "<input id=t name=q aria-label='검색어'></form>"), "/acct"),
    # GET 폼 action 쿼리 키워드(path_only 우회 금지).
    "/r_action_query": (_page("검색", "<form method=get action='/go?do=withdraw'><input id=t name=q aria-label='검색어'>"
                                     "</form>"), "/go"),
    # B2 민감 칸 — 폼 밖이어도 HIGH.
    "/r_password": (_page("암호", "<input id=t type=password aria-label='암호'>"), ""),
    "/r_cc": (_page("카드", "<input id=t autocomplete=cc-number aria-label='번호'>"), ""),
    "/r_otp": (_page("코드", "<input id=t autocomplete=one-time-code aria-label='코드'>"), ""),
    "/r_curpw": (_page("pw", "<input id=t autocomplete=current-password aria-label='값'>"), ""),
    "/r_name_card": (_page("카드", "<input id=t aria-label='카드번호'>"), ""),
    "/r_name_auth": (_page("인증", "<input id=t placeholder='인증번호 6자리'>"), ""),
    "/r_name_pw": (_page("비번", "<input id=t aria-label='비밀번호'>"), ""),
    "/r_name_login": (_page("로그인", "<input id=t aria-label='로그인 아이디'>"), ""),
    "/r_get_with_pw": (_page("검색", "<form method=get action=/s><input id=t name=q aria-label='검색어'>"
                                    "<input type=password name=p aria-label='암호'></form>"), "/s"),
    # 폼 안 다른 출처 iframe(결제 위젯) — GET 이어도 HIGH.
    "/r_iframe": (_page("위젯", "<form method=get action=/s><input id=t name=q aria-label='검색어'>"
                               "<iframe src='{other}/widget' width=50 height=20></iframe></form>"), "/s"),
    # NN6: form= 속성으로 POST 폼에 연결된 칸(closest('form') 없음).
    "/r_form_attr": (_page("연결", "<form id=f method=post action=/order/pay></form>"
                                  "<input id=t form=f aria-label='수량'>"), "/order"),
    # NN2: 폼 밖 combobox 의 활성 항목이 위험 동작.
    "/r_activedesc": (_page("명령", "<input id=t role=combobox aria-label='명령' aria-controls=lb2 "
                                   "aria-activedescendant=d1><div role=listbox id=lb2>"
                                   "<div role=option id=d1>계정 삭제</div></div>"), ""),
    # 격리/페이지 불일치: 페이지 world 가 form getter 를 지운다 — 격리 world 는 POST 폼 소속을 본다.
    "/r_spoof": (_page("위조", "<script>Object.defineProperty(HTMLInputElement.prototype, 'form', "
                              "{get() { return null; }});</script>"
                              "<form method=post action=/order/pay><input id=t aria-label='수량'></form>"), "/order"),
    # B4 p8: 입력 중 폼을 POST /pay 로 바꾼다.
    "/r_mutate_form": (_page("변조", "<form method=get action=/search><input id=t name=q aria-label='검색어' "
                                    "oninput=\"this.form.method='post'; this.form.action='/billing'\"></form>"),
                       "/billing"),
    # B4 p9: 입력 중 칸이 password 로 바뀐다.
    "/r_mutate_pw": (_page("변조", "<form method=get action=/search><input id=t name=q aria-label='검색어' "
                                  "oninput=\"this.type='password'\"></form>"), "/search"),
    # B5: 입력 중 문서 교체(같은 id 칸, POST 결제 폼).
    "/r_swap": (_page("교체", "<form method=get action=/search><input id=t name=q aria-label='검색어' "
                             "oninput=\"if (!window.__gone) { window.__gone = 1; location.replace('/r_swapped'); }\">"
                             "</form>"), "/order"),
}
SWAPPED = _page("교체됨", "<form method=post action=/order/pay><input id=t name=q aria-label='검색어'></form>")
#: R1 (검증 B3): 폼 밖 평범한 두 칸 — SecretStore 값을 친 칸(t)과 평범한 칸(t2).
SECRET_PAGE = _page("검색", "<input id=t name=keyword aria-label='검색어 입력'><input id=t2 aria-label='메모 입력'>")

#: 정상군: (경로, 페이지, 효과 확인 방식 'mark'(#out) 또는 요청 경로 접두).
NORMAL: Dict[str, Tuple[str, str]] = {
    "/n_combo": (_page("지도", "<input id=t role=combobox aria-label='출발지 입력' aria-autocomplete=list "
                              "aria-controls=lb>" + COMBO), "mark"),
    "/n_combo_ad": (_page("지도", "<input id=t role=combobox aria-label='도착지 입력' aria-controls=lb "
                                 "aria-activedescendant=o2>" + COMBO), "mark"),
    "/n_searchbox": (_page("검색", "<input id=t type=search aria-label='검색'>"), "mark"),
    "/n_textbox": (_page("검색", "<input id=t name=keyword aria-label='검색어 입력'>"), "mark"),
    "/n_get_search": (_page("검색", "<form method=get action=/results><input id=t name=q aria-label='검색어'>"
                                   "<button class=btn-search>검색</button></form>", mark=False), "/results"),
    "/n_get_shop": (_page("쇼핑", "<form method=GET action=/results><input type=hidden name=cat value=all>"
                                 "<input id=t name=query aria-label='상품 검색'><button type=submit>찾기</button></form>",
                          mark=False), "/results"),
    # NN8: 다른 출처 GET 목적지 — 허용, gate_basis 에 목적지 출처를 싣는다.
    "/n_get_cross": (_page("검색", "<form method=get action='{other}/results'><input id=t name=q aria-label='검색어'>"
                                  "</form>", mark=False), "other:/results"),
    # 지도 자동완성 fixture(네이버지도 길찾기 모양): 폼 밖 두 칸 + 목록.
    "/n_map": (_page("길찾기", "<div class=route><input id=t role=combobox aria-label='출발지 입력' "
                              "aria-controls=lb aria-expanded=true><input id=t2 role=combobox aria-label='도착지 입력'>"
                              "</div>" + COMBO), "mark"),
}
#: 이름만으로 HIGH 인 정상 칸(NN1 — 이번 범위에서 바꾸지 않는 알려진 과잉 거부).
KNOWN_HIGH: Dict[str, Tuple[str, str]] = {
    "/k_order_search": (_page("주문", "<input id=t type=search aria-label='주문 검색'>"), "mark"),
}


class _Site:
    def __init__(self) -> None:
        self.requests: List[Tuple[str, str]] = []
        self.base = ""
        self.other = ""


@pytest.fixture
def site() -> Iterator[_Site]:
    state = _Site()
    pages: Dict[str, str] = {k: v[0] for k, v in {**RISK, **NORMAL, **KNOWN_HIGH}.items()}
    pages["/r_swapped"] = SWAPPED
    pages["/x_secret"] = SECRET_PAGE

    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            path = self.path.split("?", 1)[0]
            state.requests.append((self.command, self.path))
            if path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
            else:
                html = pages.get(path, HEAD.format(t="결과") + "<h1>결과</h1>")
                body = html.replace("{other}", state.other).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._reply()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._reply()

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    state.base = f"http://127.0.0.1:{port}"
    state.other = f"http://localhost:{port}"  # 다른 출처(호스트 다름)
    try:
        yield state
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(server: BrowserMCPServer, action: ActionType, args: Dict[str, Any]):
    return await server.call_tool(tool_name(action), args)


async def _target(server: BrowserMCPServer, name: str) -> Tuple[str, int]:
    obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    o = obs.data["observation"]
    for e in o["elements"]:
        if e["name"] == name:
            return e["element_id"], o["snapshot_epoch"]
    raise AssertionError(f"요소 없음: {name} — {[e['name'] for e in o['elements']]}")


def _name_of(html: str) -> str:
    import re

    tag = re.search(r"<input id=t[^>]*>", html).group(0)
    m = re.search(r"aria-label='([^']*)'", tag) or re.search(r"placeholder='([^']*)'", tag)
    return m.group(1)


def _submitted(site: _Site, prefix: str, start: int) -> bool:
    if not prefix:
        return False
    return any(path.startswith(prefix) for _m, path in site.requests[start:])


async def _run(server: BrowserMCPServer, site: _Site, path: str, html: str, mode: str) -> Dict[str, Any]:
    """한 fixture 에서 Enter 한 번: mode='type_enter'(type_text press_enter) | 'press_key'(입력 뒤 press_key Enter)."""
    nav = await _call(server, ActionType.NAVIGATE, {"url": site.base + path})
    assert nav.success, nav.error_message
    eid, epoch = await _target(server, _name_of(html))
    start = len(site.requests)
    if mode == "type_enter":
        r = await _call(server, ActionType.TYPE_TEXT,
                        {"element_id": eid, "epoch": epoch, "text": "abc", "press_enter": True})
    else:
        typed = await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "abc"})
        if not typed.success:
            return {"result": typed, "start": start, "out": None, "typed_failed": True}
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    import asyncio

    await asyncio.sleep(0.15)  # 비동기 fetch·이동 요청이 서버에 닿을 시간
    try:
        out = await server._page.text_content("#out", timeout=500)  # noqa: SLF001
    except Exception:  # noqa: BLE001 — 문서가 옮겨 갔다
        out = None
    return {"result": r, "start": start, "out": out}


def _passed(rec: Dict[str, Any]) -> bool:
    return bool(rec["result"].success)


# ------------------------------------------------------------------ 위험군: 통과 0


@requires_chromium
@pytest.mark.parametrize("mode", ["type_enter", "press_key"])
async def test_risk_census_zero_pass(site, mode):
    rows = []
    async with BrowserMCPServer() as server:
        for path, (html, prefix) in RISK.items():
            if mode == "press_key" and path in ("/r_swap",):
                continue  # 문서 교체는 입력 한 호출 안의 결속 시험(press_key 는 입력 뒤 새 문서에서 다시 판정)
            rec = await _run(server, site, path, html, mode)
            if rec.get("typed_failed"):
                rows.append((path, "typed_failed", rec["result"].error_code))
                continue
            r = rec["result"]
            leaked = _submitted(site, prefix, rec["start"]) if prefix not in ("/s", "/search") else \
                _submitted(site, prefix + "?", rec["start"])
            rows.append((path, "PASS" if r.success else "blocked", r.error_code, leaked,
                         (r.data or {}).get("typed_without_enter")))
    passed = [row for row in rows if row[1] == "PASS" or (len(row) > 3 and row[3])]
    print(json.dumps([[str(c) for c in row] for row in rows], ensure_ascii=False))
    assert passed == [], passed


#: R1 (검증 B1): 기본 제출자 형제 — 명시 GET 폼(/results)이지만 **HTML 규칙상 기본 제출자**(폼 소유 제출 버튼 중 문서 순서
#: 첫째 — button·input submit·input image, form= 로 바깥에서 연결된 것 포함)가 formmethod=post formaction=/pay 다.
#: 세 경로 모두 승인 없이 POST /pay 가 나가면 안 된다(기준 9b9bbf0 은 전부 HIGH). form.elements 는 image 를 빼므로 그것으로
#: 찾으면 image·바깥 image 형제가 새어 나갔다.
_SUB_POST = "formmethod=post formaction=/pay"
_SIB_FIELD = "<input id=t name=q aria-label='검색어'>"
SUBMITTER_SIBLINGS: Dict[str, str] = {
    "/sb_button": f"<form id=f method=get action=/results>{_SIB_FIELD}<button {_SUB_POST}>검색</button></form>",
    "/sb_submit": f"<form id=f method=get action=/results>{_SIB_FIELD}<input type=submit {_SUB_POST} value='검색'></form>",
    "/sb_image": f"<form id=f method=get action=/results>{_SIB_FIELD}<input type=image {_SUB_POST} alt='검색'></form>",
    # image 가 앞, 평범한 button 이 뒤 — 기본 제출자는 image(문서 순서 첫째).
    "/sb_image_first": (f"<form id=f method=get action=/results>{_SIB_FIELD}<input type=image {_SUB_POST} alt='검색'>"
                        "<button>찾기</button></form>"),
    "/sb_ext_button": f"<form id=f method=get action=/results>{_SIB_FIELD}</form><button form=f {_SUB_POST}>검색</button>",
    "/sb_ext_image": (f"<form id=f method=get action=/results>{_SIB_FIELD}</form>"
                      f"<input type=image form=f {_SUB_POST} alt='검색'>"),
    # 바깥 제출자가 폼보다 **앞**(문서 순서 첫째) — 안쪽 평범한 버튼보다 우선.
    "/sb_ext_before": (f"<input type=image form=f {_SUB_POST} alt='검색'>"
                       f"<form id=f method=get action=/results>{_SIB_FIELD}<button>찾기</button></form>"),
    # 비활성 첫 제출자: 암묵 제출 없음(뒤 버튼으로 넘어가지 않음) — 그 formmethod 를 그대로 반영(HIGH).
    "/sb_disabled_first": (f"<form id=f method=get action=/results>{_SIB_FIELD}<button disabled {_SUB_POST}>검색</button>"
                           "<button>찾기</button></form>"),
}
#: 대조(과차단 금지): 기본 제출자가 평범한 GET 이고 POST 형제는 그 **뒤** — 명시 GET 예외(검색형 제출) 그대로.
SUBMITTER_GET_OK: Dict[str, str] = {
    "/sb_button_then_image": (f"<form id=f method=get action=/results>{_SIB_FIELD}<button>찾기</button>"
                              f"<input type=image {_SUB_POST} alt='검색'></form>"),
    "/sb_none": f"<form id=f method=get action=/results>{_SIB_FIELD}</form>",
}


@pytest.fixture
def sib_site() -> Iterator[_Site]:
    state = _Site()
    pages = {k: _page("검색", v, mark=False) for k, v in {**SUBMITTER_SIBLINGS, **SUBMITTER_GET_OK}.items()}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            state.requests.append((self.command, self.path))
            path = self.path.split("?", 1)[0]
            body = (b"User-agent: *\nAllow: /\n" if path == "/robots.txt"
                    else pages.get(path, HEAD.format(t="결과") + "<h1>결과</h1>").encode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._reply()

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply()

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state.base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield state
    finally:
        srv.shutdown()
        srv.server_close()


async def _sib_enter(server: BrowserMCPServer, site: _Site, path: str, mode: str) -> Tuple[Any, List[Tuple[str, str]]]:
    import asyncio

    nav = await _call(server, ActionType.NAVIGATE, {"url": site.base + path})
    assert nav.success, nav.error_message
    eid, epoch = await _target(server, "검색어")
    start = len(site.requests)
    if mode == "press_key":
        typed = await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "abc"})
        assert typed.success, (path, typed.error_message)
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    else:
        r = await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch,
                                                       "text": "abc\n" if mode == "newline" else "abc",
                                                       "press_enter": mode == "type_enter"})
    await asyncio.sleep(0.15)
    return r, site.requests[start:]


@requires_chromium
@pytest.mark.parametrize("mode", ["type_enter", "press_key", "newline"])
async def test_default_submitter_siblings_never_post_unapproved(sib_site, mode):
    rows = []
    async with BrowserMCPServer() as server:
        for path in SUBMITTER_SIBLINGS:
            r, reqs = await _sib_enter(server, sib_site, path, mode)
            rows.append((path, r.error_code, [m for m, _p in reqs if m == "POST"]))
    print(json.dumps([[str(c) for c in row] for row in rows], ensure_ascii=False))
    leaked = [row for row in rows if row[2] or row[1] is not ErrorCode.HITL_UNATTENDED_BLOCKED]
    assert leaked == [], leaked


@requires_chromium
@pytest.mark.parametrize("mode", ["type_enter", "press_key"])
async def test_default_submitter_get_controls_still_pass(sib_site, mode):
    async with BrowserMCPServer() as server:
        for path in SUBMITTER_GET_OK:
            r, reqs = await _sib_enter(server, sib_site, path, mode)
            assert r.success, (path, r.error_code, r.error_message)
            assert [m for m, p in reqs if p.startswith("/results?q=abc")] == ["GET"], (path, reqs)
            assert not any(m == "POST" for m, _p in reqs), (path, reqs)


@requires_chromium
async def test_press_key_enter_after_secret_is_sensitive_like_type_text(site):
    """R1 (검증 B3): SecretStore 키로 실제 값을 넣은 칸에서 press_key Enter 도 type_text(press_enter)·개행과 같은 민감 칸
    판정(HIGH, sensitive=secret_value) — 칸 이름이 평범해도. 다른 평범한 칸으로 옮겨 친 Enter 는 그대로 통과(과차단 금지)."""
    from security.secrets import SecretStore

    async with BrowserMCPServer() as server:
        server._dispatcher.ctx.secrets = SecretStore({"WS88_SYN_KEY": "SyntheticCedarValue"})  # noqa: SLF001 — 합성 값
        await _call(server, ActionType.NAVIGATE, {"url": site.base + "/x_secret"})
        eid, epoch = await _target(server, "검색어 입력")
        a = await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "WS88_SYN_KEY",
                                                       "press_enter": True})
        eid, epoch = await _target(server, "검색어 입력")
        typed = await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "WS88_SYN_KEY"})
        b = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        out_b = await server._page.text_content("#out")  # noqa: SLF001
        eid2, epoch2 = await _target(server, "메모 입력")
        await _call(server, ActionType.TYPE_TEXT, {"element_id": eid2, "epoch": epoch2, "text": "abc"})
        c = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert typed.success and typed.data.get("secret_resolved") is True, (typed.error_message, typed.data)
    for r in (a, b):
        assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.action, r.error_code, r.error_message)
        assert r.data["gate_basis"].get("sensitive") == "secret_value", (r.action, r.data.get("gate_basis"))
    assert out_b == "대기"  # Enter 가 그 칸에 닿지 않았다
    assert c.success, (c.error_code, c.error_message)


#: R2 (재검증 C1): 문서 단위 Enter 표시 — SPA 로 교체된 노드에도 효과를 본다(칸 리스너는 cloneNode 로 옮겨지지 않는다).
_DOC_MARK_JS = ("() => { document.addEventListener('keydown', (e) => { if (e.key === 'Enter') "
                "document.getElementById('out').textContent = 'enter:' + e.target.value; }); }")


async def _secret_enter(server: BrowserMCPServer, name: str, text: str, **kw: Any) -> Tuple[Any, Any, Any]:
    """칸(name)에 type_text(text, kw) 뒤 press_key Enter — (입력 결과, Enter 결과, #out)."""
    await server._page.evaluate("() => { document.getElementById('out').textContent = '대기'; }")  # noqa: SLF001
    eid, epoch = await _target(server, name)
    typed = await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": text, **kw})
    r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    return typed, r, await server._page.text_content("#out")  # noqa: SLF001


async def _secret_nav(server: BrowserMCPServer, site: _Site) -> None:
    assert (await _call(server, ActionType.NAVIGATE, {"url": site.base + "/x_secret"})).success
    await server._page.evaluate(_DOC_MARK_JS)  # noqa: SLF001


def _blocked_secret(r: Any) -> bool:
    return r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED and \
        (r.data or {}).get("gate_basis", {}).get("sensitive") == "secret_value"


@requires_chromium
async def test_concealed_mark_cleared_by_plain_replacement_only(site):
    """R2 (재검증 C1): 가림 표식은 '지금 그 칸에 비밀 값이 있다' — 같은 칸을 **통째로 바꾸는** 평범한 입력(clear 후 입력,
    성공, 값 확인)이면 해제(기준처럼 허용·Enter 효과). 덧붙임(clear_before=false)·입력 실패·값 불일치는 유지(HIGH)."""
    from security.secrets import SecretStore

    async with BrowserMCPServer() as server:
        server._dispatcher.ctx.secrets = SecretStore({"WS88_SYN_KEY": "SyntheticCedarValue"})  # noqa: SLF001 — 합성 값
        await _secret_nav(server, site)
        typed, pos, _ = await _secret_enter(server, "검색어 입력", "WS88_SYN_KEY")
        assert typed.data.get("secret_resolved") is True and _blocked_secret(pos), (pos.error_code, pos.data)
        # c: 같은 칸 clear(빈 입력) 뒤 평범한 abc — 기준(9b9bbf0)처럼 허용, Enter 효과.
        _, _, _ = await _secret_enter(server, "검색어 입력", "")
        typed, c, out_c = await _secret_enter(server, "검색어 입력", "abc")
        assert typed.success and c.success, (c.error_code, c.error_message, (c.data or {}).get("gate_basis"))
        assert out_c == "enter:abc"
        # c': 비밀 값 위에 곧바로 평범한 대체 입력(clear_before 기본) — 허용.
        await _secret_enter(server, "검색어 입력", "WS88_SYN_KEY")
        _, c2, out_c2 = await _secret_enter(server, "검색어 입력", "abc")
        assert c2.success and out_c2 == "enter:abc", (c2.error_code, c2.error_message)
        # 덧붙임: 비밀 입력 뒤 clear 없이 abc — 비밀이 칸에 남는다(HIGH).
        await _secret_enter(server, "검색어 입력", "WS88_SYN_KEY")
        # (덧붙임 type_text 자체는 기존 사후조건(기대값 = 친 글자)으로 실패 보고 — 기준과 같음. 글자는 칸에 들어갔다.)
        _, app, out_app = await _secret_enter(server, "검색어 입력", "abc", clear_before=False)
        assert _blocked_secret(app), (app.error_code, (app.data or {}).get("gate_basis"))
        assert await server._page.input_value("#t") == "SyntheticCedarValueabc"  # noqa: SLF001 — 합성 값
        assert out_app == "대기"
        # 덧붙임 뒤 평범한 대체 입력은 다시 해제(표식 = 현재 값).
        _, again, _ = await _secret_enter(server, "검색어 입력", "abc")
        assert again.success, (again.error_code, again.error_message)
        # 입력 실패: 비밀 입력 뒤 칸이 readonly — clear 가 실패하면 비밀이 남는다(HIGH).
        await _secret_enter(server, "검색어 입력", "WS88_SYN_KEY")
        await server._page.evaluate("() => { document.getElementById('t').readOnly = true; }")  # noqa: SLF001
        typed, fail, out_fail = await _secret_enter(server, "검색어 입력", "abc")
        assert not typed.success and _blocked_secret(fail), (typed.error_code, fail.error_code)
        assert out_fail == "대기"
        # 값 확인 불가(입력 결과가 친 값과 다름 — maxlength): 유지(HIGH).
        await _secret_nav(server, site)
        await _secret_enter(server, "검색어 입력", "WS88_SYN_KEY")
        await server._page.evaluate("() => { document.getElementById('t').maxLength = 2; }")  # noqa: SLF001
        _, short, _ = await _secret_enter(server, "검색어 입력", "abc")
        assert _blocked_secret(short), (short.error_code, (short.data or {}).get("gate_basis"))
        # a: 비밀 칸은 그대로, 다른 평범한 칸 Enter — 허용(다른 칸으로 번지지 않음).
        await _secret_nav(server, site)
        await _secret_enter(server, "검색어 입력", "WS88_SYN_KEY")
        _, a, out_a = await _secret_enter(server, "메모 입력", "abc")
        assert a.success and out_a == "enter:abc", (a.error_code, a.error_message)
        # d: SPA 로 같은 칸 노드 교체 뒤 평범한 입력 — 허용.
        await server._page.evaluate(  # noqa: SLF001
            "() => { const t = document.getElementById('t'); t.replaceWith(t.cloneNode(true)); }")
        _, d, out_d = await _secret_enter(server, "검색어 입력", "abc")
        assert d.success and out_d == "enter:abc", (d.error_code, d.error_message)
        # e: 비밀 입력 뒤 새 문서 — 평범한 입력 허용.
        await _secret_enter(server, "검색어 입력", "WS88_SYN_KEY")
        await _secret_nav(server, site)
        _, e, out_e = await _secret_enter(server, "검색어 입력", "abc")
        assert e.success and out_e == "enter:abc", (e.error_code, e.error_message)


@requires_chromium
async def test_mutation_during_input_refused_with_typed_without_enter(site):
    """B4: 입력 전 LOW(명시 GET) → 입력 중 POST·password 로 바뀜 → Enter 를 보내지 않고 거부(typed_without_enter)."""
    async with BrowserMCPServer() as server:
        for path in ("/r_mutate_form", "/r_mutate_pw"):
            html, prefix = RISK[path]
            rec = await _run(server, site, path, html, "type_enter")
            r = rec["result"]
            assert not r.success
            assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (path, r.error_code, r.error_message)
            assert r.data.get("typed_without_enter") is True, (path, r.data)
            assert not _submitted(site, "/billing", rec["start"])
            assert not _submitted(site, "/search?", rec["start"])
            value = await server._page.evaluate("() => document.getElementById('t').value")  # noqa: SLF001
            assert value == "abc"  # 입력은 됐다 — Enter 만 안 보냈다


@requires_chromium
async def test_document_swap_during_input_refused(site):
    """B5: press_enter 도 게이트 판정 문서에 묶인다 — 입력 중 문서가 바뀌면 새 문서의 POST 폼으로 Enter 를 보내지 않는다."""
    async with BrowserMCPServer() as server:
        html, _prefix = RISK["/r_swap"]
        rec = await _run(server, site, "/r_swap", html, "type_enter")
        r = rec["result"]
    assert not r.success
    assert not _submitted(site, "/order", rec["start"])


# ------------------------------------------------------------------ 정상군: 승인 0


@requires_chromium
@pytest.mark.parametrize("mode", ["type_enter", "press_key"])
async def test_normal_census_no_approval(site, mode):
    rows = []
    async with BrowserMCPServer() as server:
        for path, (html, effect) in {**NORMAL, **KNOWN_HIGH}.items():
            rec = await _run(server, site, path, html, mode)
            r = rec["result"]
            if effect == "mark":
                effective = (rec["out"] or "").startswith("enter:")
            elif effect.startswith("other:"):
                effective = _submitted(site, effect[len("other:"):], rec["start"])
            else:
                effective = _submitted(site, effect, rec["start"])
            rows.append((path, bool(r.success), r.error_code, effective))
    print(json.dumps([[str(c) for c in row] for row in rows], ensure_ascii=False))
    refused = sorted(row[0] for row in rows if not row[1])
    assert refused == sorted(KNOWN_HIGH), rows  # 남는 거부 = 알려진 HIGH(NN1) 1건
    assert all(row[3] for row in rows if row[1]), rows  # 통과한 Enter 는 실제로 그 칸·그 폼에 닿았다


@requires_chromium
async def test_enter_paths_agree_on_get_form(site):
    """B3/B6: 명시 GET 검색 폼 — type_text(press_enter)·press_key(Enter)·개행 입력이 같은 판정(통과)."""
    html, _ = NORMAL["/n_get_search"]
    async with BrowserMCPServer() as server:
        a = await _run(server, site, "/n_get_search", html, "type_enter")
        b = await _run(server, site, "/n_get_search", html, "press_key")
        await _call(server, ActionType.NAVIGATE, {"url": site.base + "/n_get_search"})
        eid, epoch = await _target(server, "검색어")
        start = len(site.requests)
        c = await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "abc\n"})
    assert a["result"].success and b["result"].success, (a["result"].error_message, b["result"].error_message)
    # 개행 입력: 게이트는 통과(승인 없음)하고 Enter 가 폼을 실제로 제출한다 — 값 사후조건(이동 뒤 칸 없음)은 이 시험 밖.
    assert c.error_code is not ErrorCode.HITL_UNATTENDED_BLOCKED, c.error_message
    assert _submitted(site, "/results?q=abc", start)


@requires_chromium
async def test_enter_paths_agree_on_post_form(site):
    html, _ = RISK["/r_post_pay"]
    async with BrowserMCPServer() as server:
        a = await _run(server, site, "/r_post_pay", html, "type_enter")
        b = await _run(server, site, "/r_post_pay", html, "press_key")
        await _call(server, ActionType.NAVIGATE, {"url": site.base + "/r_post_pay"})
        eid, epoch = await _target(server, "수량")
        c = await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "abc\n"})
    for r in (a["result"], b["result"], c):
        assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.action, r.error_message)
        assert "폼 제출" in (r.error_message or "")


@requires_chromium
async def test_cross_origin_get_destination_in_gate_basis(site):
    """NN8: 다른 출처 GET 목적지는 허용하되 gate_basis 에 목적지 출처를 싣는다."""
    html, _ = NORMAL["/n_get_cross"]
    async with BrowserMCPServer() as server:
        rec = await _run(server, site, "/n_get_cross", html, "type_enter")
    r = rec["result"]
    assert r.success, r.error_message
    assert r.data["gate_basis"]["enter_action_origin"] == site.other


# ------------------------------------------------------------------ batch·jev 경로(NN7)


@requires_chromium
async def test_batch_type_enter_uses_same_gate(site, tmp_path):
    async with BrowserMCPServer(handoff_root=tmp_path / "s", recipes=False) as s:
        await _call(s, ActionType.NAVIGATE, {"url": site.base + "/n_combo"})
        ok = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "type_text", "target": {"role": "combobox", "name": "출발지 입력"},
             "args": {"text": "서울", "press_enter": True}}]})
        out_ok = await s._page.text_content("#out")  # noqa: SLF001
        await _call(s, ActionType.NAVIGATE, {"url": site.base + "/r_post_pay"})
        start = len(site.requests)
        bad = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "type_text", "target": {"role": "textbox", "name": "수량"},
             "args": {"text": "1", "press_enter": True}}]})
    assert ok["success"] is True, ok
    assert out_ok == "enter:서울"
    assert bad["success"] is False, bad
    assert not _submitted(site, "/order", start)


@requires_chromium
async def test_jev_press_enter_maps_to_gated_press_key(site):
    """jev_decider 의 press_enter 는 press_key Enter 로 실행된다 — 그 판정이 같은 Enter-on-field 판정이다."""
    # 변환 자체는 test_jev_decider(test_press_enter_and_scroll_map_to_loop_actions)가 고정한다 — 여기서는 변환된 결정을
    # 서버로 보내 두 fixture 판정을 본다.
    decision = {"action": "press_key", "key": "Enter"}
    async with BrowserMCPServer() as server:
        rec_ok = await _run(server, site, "/n_combo", NORMAL["/n_combo"][0], "press_key")
        nav = await _call(server, ActionType.NAVIGATE, {"url": site.base + "/r_post_login"})
        assert nav.success
        eid, epoch = await _target(server, "이메일")
        await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "a@b.c"})
        bad = await server.call_tool(tool_name(ActionType.PRESS_KEY), {"key": decision["key"]})
    assert rec_ok["result"].success and rec_ok["out"] == "enter:abc"
    assert bad.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED


# ------------------------------------------------------------------ 판정 함수 단위(브라우저 없음)

from interface.server_hitl import _enter_field_verdict, _enter_sensitive, _secret_text  # noqa: E402


def _field(**kw: Any) -> Dict[str, Any]:
    info = {"tag": "INPUT", "type": "text", "name": "", "in_form": False, "signals": [], "enter": {}}
    info.update(kw)
    return info


def _get_form(**kw: Any) -> Dict[str, Any]:
    form = {"method_attr": "get", "action_attr": "/search", "controls": [{"tag": "INPUT", "type": "text",
                                                                          "name_attr": "q", "id": "t"}],
            "controls_total": 1, "frames": 0, "submitter": None, "submitter_undetermined": False,
            "action_origin": "http://a", "doc_origin": "http://a"}
    form.update(kw)
    return form


@pytest.mark.parametrize("name,sensitive", [
    ("카드번호", True), ("카드 번호 입력", True), ("인증번호 6자리", True), ("비밀번호", True), ("로그인 아이디", True),
    ("아이디", True), ("Password", True), ("Card number", True), ("Security code", True), ("OTP", True),
    ("아이디어 검색", False), ("출발지 입력", False), ("검색어", False), ("상품 검색", False), ("Search", False),
])
def test_sensitive_name_table(name, sensitive):
    assert bool(_enter_sensitive(_field(name=name))) is sensitive


@pytest.mark.parametrize("ac", ["cc-number", "cc-csc", "one-time-code", "current-password", "new-password",
                                "section-x cc-exp"])
def test_sensitive_autocomplete(ac):
    assert _enter_sensitive(_field(enter={"autocomplete": ac})) == "autocomplete"


def test_sensitive_identifier_tokens_and_controls():
    assert _enter_sensitive(_field(signals=[["id", "userPw"]])) == "field_name"
    assert _enter_sensitive(_field(signals=[["name_attr", "login_id"]])) == "field_name"
    form = _get_form(controls=[{"tag": "INPUT", "type": "password", "name_attr": "x", "id": ""}], controls_total=1)
    assert _enter_sensitive(_field(in_form=True, enter={"form": form})) == "password"


def test_get_exception_rules():
    ok = _enter_field_verdict(_field(in_form=True, name="검색어", enter={"form": _get_form()}), True)
    assert ok[0] is False and ok[4]["enter_target"] == "form_get"
    # 격리 확인이 없으면 낮추지 않는다(NN9).
    assert _enter_field_verdict(_field(in_form=True, enter={"form": _get_form()}), False)[0] is True
    # 메서드 없음(p1)·POST·formmethod=post(p2)·iframe·컨트롤 상한 초과·formmethod=dialog → 폼 제출.
    for form in (_get_form(method_attr=None), _get_form(method_attr="post"),
                 _get_form(submitter={"tag": "BUTTON", "type": "submit", "formmethod": "post", "formaction": "/pay",
                                      "name": "검색", "signals": []}),
                 _get_form(frames=1), _get_form(frames=-1), _get_form(controls_total=201),
                 _get_form(submitter={"tag": "BUTTON", "type": "submit", "formmethod": "dialog", "formaction": None,
                                      "name": "검색", "signals": []})):
        v = _enter_field_verdict(_field(in_form=True, enter={"form": form}), True)
        assert v[0] is True and "enter_target" not in v[4], form


def test_get_exception_needs_determined_default_submitter():
    """R1 (검증 B1): 기본 제출자를 정하지 못했거나(해석 실패·모호) 그 사실이 없으면 GET 예외 없음(HIGH)."""
    for form in (_get_form(submitter_undetermined=True), {k: v for k, v in _get_form().items()
                                                         if k != "submitter_undetermined"}):
        v = _enter_field_verdict(_field(in_form=True, enter={"form": form}), True)
        assert v[0] is True and "enter_target" not in v[4], form


def test_concealed_field_mark_is_sensitive():
    """R1 (검증 B3): 격리 world 가 가림 표식(SecretStore·민감 입력으로 친 칸)을 본 칸 — 세 경로 공통 민감 칸."""
    assert _enter_sensitive(_field(enter={"concealed": True})) == "secret_value"
    v = _enter_field_verdict(_field(name="검색어 입력", enter={"concealed": True}), True)
    assert v[0] is True and v[4] == {"enter_target": "sensitive_field", "sensitive": "secret_value"}
    assert _enter_sensitive(_field(enter={"concealed": False})) == ""


def test_get_signals_include_hidden_and_query():
    from security.hitl import assess_risk
    from security import ActionContext, RiskLevel

    def risk(form: Dict[str, Any]) -> Any:
        v = _enter_field_verdict(_field(in_form=True, name="검색어", enter={"form": form}), True)
        return assess_risk(ActionContext(action=ActionType.TYPE_TEXT, element_name=v[1], submits_form=v[0],
                                         unresolved_target=v[2], signals=v[3])).risk

    hidden = [{"tag": "INPUT", "type": "hidden", "name_attr": "op", "id": "", "value": "delete_account"}]
    assert risk(_get_form(controls=hidden, controls_total=1)) is RiskLevel.HIGH
    assert risk(_get_form(action_attr="/acct?op=withdraw")) is RiskLevel.HIGH
    assert risk(_get_form(submitter={"tag": "BUTTON", "type": "submit", "formmethod": None, "formaction": None,
                                     "name": "결제하기", "signals": []})) is RiskLevel.HIGH
    # 무작위 토큰 값(CSRF)의 부분 문자열 'pay' 는 위험이 아니다(식별자 토큰 규칙).
    token = [{"tag": "INPUT", "type": "hidden", "name_attr": "t", "id": "", "value": "xPaYz9"}]
    assert risk(_get_form(controls=token, controls_total=1)) is RiskLevel.LOW
    assert risk(_get_form()) is RiskLevel.LOW


def test_button_and_body_keep_old_table():
    # 폼 밖 '로그인' 버튼에서 Enter = 그 버튼 클릭 — 민감 칸 판정은 입력칸만(클릭과 같은 판정).
    assert _enter_field_verdict({"tag": "BUTTON", "type": "submit", "name": "로그인", "in_form": False,
                                 "signals": []}, True)[0] is False
    # body 포커스: 페이지 글자(innerText)로 판정하지 않는다.
    body = {"tag": "BODY", "type": "", "name": "", "in_form": False, "signals": [["text", "결제 삭제"]]}
    assert _enter_field_verdict(body, True)[:4] == (False, "", "", ())


def test_field_text_content_not_judged_but_activedescendant_is():
    v = _enter_field_verdict({"tag": "TEXTAREA", "type": "textarea", "name": "메시지", "in_form": False,
                              "signals": [["text", "결제 해 주세요"]], "enter": {}}, True)
    assert v[3] == ()
    v = _enter_field_verdict(_field(name="명령", enter={"active_option": {"name": "계정 삭제", "signals": []}}), True)
    assert ("detail", "계정 삭제") in v[3]


class _Store:
    def __init__(self, keys: Any) -> None:
        self.keys = keys

    def resolve(self, text: str) -> Any:
        if self.keys is None:
            raise RuntimeError("store down")
        return type("R", (), {"resolved": text in self.keys})()


def test_secret_text_rule():
    assert _secret_text({"text": "NAVER_PW"}, _Store({"NAVER_PW"})) is True
    assert _secret_text({"text": "KTX"}, _Store({"NAVER_PW"})) is False  # 키 모양이지만 저장소에 없음
    assert _secret_text({"text": "KTX"}, None) is False  # 저장소 없음 = 치환 없음
    assert _secret_text({"text": "NAVER_PW"}, _Store(None)) is True  # 묻지 못함 = fail-closed
    assert _secret_text({"text": "노트북"}, _Store({"NAVER_PW"})) is False


async def test_press_enter_isolated_read_failure_is_form_submit():
    """격리 판독이 실패하면(판독기는 있음) press_enter 는 기존 '폼 제출' HIGH — 페이지 world 만으로 낮추지 않는다."""
    from interface.server_hitl import HitlMixin

    class _Disp:
        ctx = type("C", (), {"secrets": None})()
        fail = True

        async def describe_element(self, element_id: str) -> Dict[str, Any]:
            return _field(name="출발지 입력")

        async def isolated_element_info_for_gate(self, element_id: str, enter: bool = False) -> Dict[str, Any]:
            assert enter is True
            if self.fail:
                raise RuntimeError("world gone")
            return {"info": _field(name="출발지 입력"), "node": ("f", 1)}

    class _Srv(HitlMixin):
        _dispatcher = _Disp()
        _page = type("P", (), {"url": "http://127.0.0.1/"})()

    srv = _Srv()
    args = {"element_id": "@e1", "text": "x", "press_enter": True}
    v = await srv._enter_field_target(args, "@e1")
    assert v[0] is True and "enter_target" not in v[4]
    srv._dispatcher.fail = False  # 격리 판독 성공 — 폼 밖 combobox 는 낮춘다
    v = await srv._enter_field_target(args, "@e1")
    assert v[0] is False and v[4]["enter_target"] == "no_form"
    srv._dispatcher.ctx.secrets = _Store({"NAVER_PW"})
    v = await srv._enter_field_target(dict(args, text="NAVER_PW"), "@e1")
    assert v[0] is True and v[4]["sensitive"] == "secret_value"


async def test_press_enter_without_isolated_reader_is_form_submit():
    """판독기 없는 가짜 디스패처(격리 판독 없음)도 press_enter 를 낮추지 않는다(기존 '폼 제출')."""
    from interface.server_hitl import HitlMixin

    class _Disp:
        ctx = type("C", (), {"secrets": None})()

        async def describe_element(self, element_id: str) -> Dict[str, Any]:
            return _field(name="출발지 입력")

    class _Srv(HitlMixin):
        _dispatcher = _Disp()
        _page = type("P", (), {"url": "http://127.0.0.1/"})()

    v = await _Srv()._enter_field_target({"element_id": "@e1", "text": "x", "press_enter": True}, "@e1")
    assert v[0] is True and "enter_target" not in v[4]
