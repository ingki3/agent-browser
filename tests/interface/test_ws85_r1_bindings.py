"""WS-85 R1: 격리 판독·게이트 판정을 **입력한 그 노드·그 문서**에 묶는다(검증 B1·B2·B3).

B1 — 격리 민감 판독(입력 직후 activeElement)은 입력한 그 노드일 때만 OR 에 더한다: 입력 완료 이벤트·Tab 이 포커스를 다른
비밀번호 칸으로 옮겨도 검색어를 비밀로 등록하지 않는다(기준 594f087 동작). 정상 비밀번호 칸(위조 type 포함)은 계속 민감.
실제 BrowserMCPServer(헤드리스 Chromium) + 로컬 HTTP 만, 비밀값은 합성.
"""

from __future__ import annotations

import json

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer
from security.secret_registry import mask_registered

from test_run_cli import requires_chromium  # noqa: F401
from test_ws85_isolated_gates import HEAD, PAGES, T_TYPE, _call, _eid, _page, _script, _truth, site  # noqa: F401

PW = "SYNTHuntouched85r1"


def _focus_moves(query: str, spoof: bool) -> str:
    js = ("document.getElementById('q').addEventListener('input',()=>{if(document.getElementById('q').value==="
          + json.dumps(query) + ")document.getElementById('p').focus()});")
    return js + (T_TYPE if spoof else "")


@requires_chromium
@pytest.mark.parametrize("spoof", [False, True])
async def test_b1_search_query_not_secret_when_input_event_focuses_password(site, spoof):
    query = f"ordinaryquery-{spoof}-ws85r1"
    PAGES["/b1"] = (HEAD + f"<input id=q type=search aria-label=검색어><input id=p type=password value={PW}>"
                    + _script(_focus_moves(query, spoof)))
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b1"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.TYPE_TEXT, {"element_id": _eid(obs, lambda e: e.get("name") == "검색어"),
                                                       "text": query, "epoch": obs.snapshot_epoch})
        actual = await _truth(server, "[document.getElementById('q').value, document.getElementById('p').value,"
                                      " document.activeElement.id]")
    assert actual == [query, PW, "p"], actual  # 전제: 검색칸에만 입력됐고 포커스는 비밀번호 칸으로 옮겨졌다
    assert r.success, r.error_message
    assert mask_registered(query) == query
    assert query in json.dumps(r.model_dump(mode="json"), ensure_ascii=False)


@requires_chromium
async def test_b1_tab_to_spoofed_password_is_not_sensitive(site):
    """마무리 보고 한계(2): 일반 칸에서 Tab 으로 위조 비밀번호 칸에 옮겨 가도 키 뒤 격리 판독을 더하지 않는다."""
    PAGES["/b1t"] = (HEAD + f"<input id=q aria-label=검색어 value=plainTabword><input id=p type=password value={PW}t>"
                     + _script(T_TYPE))
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b1t"})
        await _page(server).focus("#q")
        k = await _call(server, ActionType.PRESS_KEY, {"key": "Tab"})
        sensitive = server._dispatcher.last_key_sensitive  # noqa: SLF001
        focused = await _truth(server, "document.activeElement.id")
    assert k.success, k.error_message
    assert focused == "p"  # 전제: 포커스가 위조 비밀번호 칸으로 옮겨졌다
    assert sensitive is False
    assert mask_registered(f"{PW}t") == f"{PW}t"


@requires_chromium
async def test_b1_keydown_moves_focus_to_password_not_sensitive(site):
    """press_key: 키 이벤트가 포커스를 비밀번호 칸으로 옮겨도 키 전 일반 칸 입력은 비밀이 아니다."""
    PAGES["/b1k"] = (HEAD + f"<input id=q aria-label=검색어 value=plainkey><input id=p type=password value={PW}k>"
                     + _script("document.getElementById('q').addEventListener('keyup',()=>"
                               "document.getElementById('p').focus());" + T_TYPE))
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b1k"})
        await _page(server).focus("#q")
        k = await _call(server, ActionType.PRESS_KEY, {"key": "x"})
        sensitive = server._dispatcher.last_key_sensitive  # noqa: SLF001
        actual = await _truth(server, "[document.getElementById('q').value, document.activeElement.id]")
    assert k.success, k.error_message
    assert actual[1] == "p", actual
    assert sensitive is False
    assert mask_registered(f"{PW}k") == f"{PW}k"


@requires_chromium
async def test_b1_spoofed_password_typed_with_focus_kept_still_sensitive(site):
    """정상 비밀번호 칸(위조 type): 입력한 그 노드에 포커스가 남아 있으면 계속 민감 — 노드 일치 검사가 OR 을 끄지 않는다."""
    secret = "SYNTHr1typed85"
    PAGES["/b1p"] = HEAD + "<input id=q aria-label=검색어><input id=p type=password aria-label=pw>" + _script(T_TYPE)
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b1p"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.TYPE_TEXT, {"element_id": _eid(obs, lambda e: "pw" in (e.get("name") or "")),
                                                       "text": secret, "epoch": obs.snapshot_epoch})
        typed = await _truth(server, "document.getElementById('p').value")
    assert typed == secret
    assert secret not in json.dumps(r.model_dump(mode="json"), ensure_ascii=False)
    assert mask_registered(secret) != secret


# ------------------------------------------------------------------ B2·B3 게이트 판정 문서·노드 = 실제 입력 문서·노드
#
# 판정과 실행 사이의 경합(검증 run_timer·run_detach_sweep 의 5~11ms 타이머·지연 getter)을 결정적으로 만든다: 게이트
# 판독 함수 하나를 1회만 감싸 (a) 그 판독이 '지금 DOM 에 없음'을 보게 하거나 (b) 판독 직후 페이지 스크립트로 같은 css 의
# 다른 노드로 바꾸거나 (c) 판독 직후 새 문서로 이동시킨다. 효과 판정은 로컬 서버의 /effect 요청 수와 격리 world 판독.

import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from typing import Any, Dict, List  # noqa: E402

from contracts import ErrorCode  # noqa: E402

FX_PAGES: Dict[str, str] = {}
FX_HITS: List[str] = []
_SUBMIT = "onsubmit=\"document.getElementById('out').textContent='submitted';fetch('/effect');return false\""
_DELETE_FORM = f"<form action='/account/delete' method=post {_SUBMIT}>"
_NEW = (HEAD + _DELETE_FORM + "<input id=q aria-label=메모 autofocus><button id=b style='position:absolute;left:20px;"
        "top:60px;width:120px;height:32px'>계속</button></form>")


@pytest.fixture
def fx():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            FX_HITS.append(path)
            data = FX_PAGES.get(path, "<!doctype html><title>x</title><p>x</p>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FX_HITS.clear()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _once(server: BrowserMCPServer, name: str, before: Any = None, after: Any = None, fake: Any = None) -> Dict[str, int]:
    """디스패처 판독 함수 name 을 첫 호출 1회만 감싼다 — before/after(페이지 조작 코루틴), fake(가짜 반환값)."""
    d = server._dispatcher  # noqa: SLF001
    real = getattr(d, name)
    seen = {"n": 0}

    async def wrapped(*args: Any) -> Any:
        seen["n"] += 1
        if seen["n"] > 1:
            return await real(*args)
        if before is not None:
            await before()
        out = fake if fake is not None else await real(*args)
        if after is not None:
            await after()
        return out

    setattr(d, name, wrapped)
    return seen


def _effects() -> int:
    return FX_HITS.count("/effect")


async def _status_fx(server: BrowserMCPServer) -> str:
    await _page(server).wait_for_timeout(200)
    return str(await _truth(server, "document.getElementById('out').textContent"))


_BENIGN = HEAD + "<div id=w><button id=b style='position:absolute;left:20px;top:60px;width:120px;height:32px' " \
    "onclick=\"document.getElementById('out').textContent='pressed'\">계속</button></div>"
#: 같은 자리·같은 css(#b)·같은 이름인데 삭제 폼의 제출 버튼으로 바꾼다(같은 css 복제 — 검증 B3).
_SWAP_TO_DELETE = ("document.getElementById('w').innerHTML=" + json.dumps(
    _DELETE_FORM + "<button id=b style='position:absolute;left:20px;top:60px;width:120px;height:32px'>계속</button></form>"))
_SWAP_TO_BENIGN = ("document.getElementById('w').innerHTML=" + json.dumps(
    "<button id=b style='position:absolute;left:20px;top:60px;width:120px;height:32px' "
    "onclick=\"document.getElementById('out').textContent='pressed2'\">계속</button>"))


@requires_chromium
async def test_b3_missing_at_gate_rejudges_real_node(fx):
    """게이트 판독 순간 대상이 DOM 에 없었다(missing — 이름만으로 LOW) → 실행 직전 다시 붙은 노드(삭제 폼 제출)를 다시
    판정해 실행하지 않는다. 기준·R0: 이름 판정만으로 제출(51/150)."""
    FX_PAGES["/a"] = HEAD + _DELETE_FORM + "<button id=b>계속</button></form>"
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": fx + "/a"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        eid = _eid(obs, lambda e: e.get("name") == "계속")
        _once(server, "describe_element_for_gate", fake={"missing": True})
        _once(server, "isolated_element_info_for_gate", fake={"missing": True})
        r = await _call(server, ActionType.CLICK, {"element_id": eid, "epoch": obs.snapshot_epoch})
        status = await _status_fx(server)
    assert status == "대기" and _effects() == 0, r.model_dump(mode="json")
    assert r.success is False and r.error_code is ErrorCode.TOCTOU_MISMATCH and r.reobserve_required, r.error_message


@requires_chromium
@pytest.mark.parametrize("route", ["element_id", "selector"])
async def test_b3_same_css_swap_after_gate_is_rejudged(fx, route):
    """게이트가 판정한 노드(일반 버튼) 뒤 같은 css·이름의 삭제 폼 제출 버튼으로 바뀜 → 실행 노드가 판정 노드가 아니라 다시
    판정(폼 제출 HIGH) → 실행하지 않는다."""
    FX_PAGES["/s"] = _BENIGN
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": fx + "/s"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        page = _page(server)

        async def swap() -> None:
            await page.evaluate(_SWAP_TO_DELETE)

        if route == "element_id":
            _once(server, "isolated_element_info_for_gate", after=swap)
            args = {"element_id": _eid(obs, lambda e: e.get("name") == "계속"), "epoch": obs.snapshot_epoch}
        else:
            _once(server, "isolated_selector_info_for_gate", after=swap)
            args = {"selector": "#b"}
        r = await _call(server, ActionType.CLICK, args)
        status = await _status_fx(server)
    assert status == "대기" and _effects() == 0, r.model_dump(mode="json")
    assert r.success is False and r.error_code is ErrorCode.TOCTOU_MISMATCH and r.reobserve_required, r.error_message


@requires_chromium
async def test_b3_benign_rerender_after_gate_still_clicks(fx):
    """과잉 0: 판정 뒤 같은 버튼이 다시 그려졌을 뿐이면(일반 버튼 → 일반 버튼) 다시 판정해 그 새 노드를 누른다."""
    FX_PAGES["/r"] = _BENIGN
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": fx + "/r"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        page = _page(server)

        async def swap() -> None:
            await page.evaluate(_SWAP_TO_BENIGN)

        _once(server, "isolated_element_info_for_gate", after=swap)
        r = await _call(server, ActionType.CLICK, {"element_id": _eid(obs, lambda e: e.get("name") == "계속"),
                                                   "epoch": obs.snapshot_epoch})
        status = await _status_fx(server)
    assert r.success, r.error_message
    assert status == "pressed2"


_OLD = (HEAD + "<input id=q aria-label=메모 style='position:absolute;left:20px;top:20px'>"
        "<div><button id=b type=button style='position:absolute;left:20px;top:60px;width:120px;height:32px'>계속</button>"
        "</div>")


@requires_chromium
@pytest.mark.parametrize("route", ["element_id", "selector", "point", "newline", "enter"])
async def test_b2_document_swap_after_gate_sends_nothing(fx, route):
    """게이트 판독 직후 문서가 새 문서(같은 css·자리의 삭제 폼, 자동 포커스 입력칸)로 바뀜 → 어느 경로도 새 문서에 입력하지
    않는다(TOCTOU + 재관찰). 기준·R0: element_id·selector·개행이 새 문서 폼을 제출(8/40)."""
    FX_PAGES["/old"] = _OLD
    FX_PAGES["/new"] = _NEW
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": fx + "/old"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        page = _page(server)

        async def move() -> None:
            await page.evaluate("location.href='/new'")
            await page.wait_for_url("**/new")
            await page.wait_for_load_state("domcontentloaded")

        if route == "element_id":
            _once(server, "isolated_element_info_for_gate", after=move)
            a, args = ActionType.CLICK, {"element_id": _eid(obs, lambda e: e.get("name") == "계속"),
                                         "epoch": obs.snapshot_epoch}
        elif route == "selector":
            _once(server, "isolated_selector_info_for_gate", after=move)
            a, args = ActionType.CLICK, {"selector": "#b"}
        elif route == "point":
            _once(server, "isolated_point_target", after=move)
            a, args = ActionType.CLICK, {"x": 80, "y": 76, "epoch": obs.snapshot_epoch}
        elif route == "newline":
            _once(server, "isolated_element_info_for_gate", after=move)
            a, args = ActionType.TYPE_TEXT, {"element_id": _eid(obs, lambda e: e.get("name") == "메모"),
                                             "epoch": obs.snapshot_epoch, "text": "hi\n"}
        else:
            await page.focus("#q")
            _once(server, "isolated_focus_target", after=move)
            a, args = ActionType.PRESS_KEY, {"key": "Enter"}
        r = await _call(server, a, args)
        status = await _status_fx(server)
        doc_q = await _truth(server, "[location.pathname, document.getElementById('q').value]")
    out = r.model_dump(mode="json")
    assert doc_q[0] == "/new", doc_q  # 전제: 문서가 바뀌었다
    assert status == "대기" and _effects() == 0 and doc_q[1] == "", (out, doc_q)
    assert r.success is False and r.error_code is ErrorCode.TOCTOU_MISMATCH and r.reobserve_required, out


_NEW_BENIGN = (HEAD + "<input id=q aria-label=메모 style='position:absolute;left:20px;top:20px'>"
               "<div><button id=b type=button onclick=\"fetch('/effect')\" style='position:absolute;left:20px;top:60px;"
               "width:120px;height:32px'>계속</button></div>")


@requires_chromium
@pytest.mark.parametrize("route", ["element_id", "selector", "newline"])
async def test_b2_document_swap_to_benign_target_still_refused(fx, route):
    """새 문서의 같은 css 대상이 위험하지 않아도(다시 판정하면 LOW) 게이트가 판정한 문서가 아니면 입력하지 않는다 — 노드 재판정이
    아니라 문서 대조가 막는 경로(문서 식별 대조 사보타주가 여기서 잡힌다)."""
    FX_PAGES["/old"] = _OLD
    FX_PAGES["/new"] = _NEW_BENIGN
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": fx + "/old"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        page = _page(server)

        async def move() -> None:
            await page.evaluate("location.href='/new'")
            await page.wait_for_url("**/new")
            await page.wait_for_load_state("domcontentloaded")

        if route == "selector":
            _once(server, "isolated_selector_info_for_gate", after=move)
            a, args = ActionType.CLICK, {"selector": "#b"}
        else:
            _once(server, "isolated_element_info_for_gate", after=move)
            if route == "element_id":
                a, args = ActionType.CLICK, {"element_id": _eid(obs, lambda e: e.get("name") == "계속"),
                                             "epoch": obs.snapshot_epoch}
            else:
                a, args = ActionType.TYPE_TEXT, {"element_id": _eid(obs, lambda e: e.get("name") == "메모"),
                                                 "epoch": obs.snapshot_epoch, "text": "hi\n"}
        r = await _call(server, a, args)
        await page.wait_for_timeout(200)
        doc_q = await _truth(server, "[location.pathname, document.getElementById('q').value]")
    out = r.model_dump(mode="json")
    assert doc_q[0] == "/new", doc_q
    assert _effects() == 0 and doc_q[1] == "", (out, doc_q)
    assert r.success is False and r.error_code is ErrorCode.TOCTOU_MISMATCH and r.reobserve_required, out


_SPA = (HEAD + "<input id=q aria-label=메모 style='position:absolute;left:20px;top:20px'>"
        "<div><button id=b type=button onclick=\"document.getElementById('out').textContent='pressed';"
        "fetch('/effect')\" style='position:absolute;left:20px;top:60px;width:120px;height:32px'>계속</button></div>")


@requires_chromium
@pytest.mark.parametrize("route", ["element_id", "selector", "point", "newline", "enter", "plain"])
async def test_b2_same_document_pushstate_is_not_refused(fx, route):
    """거부 0: 같은 문서 안 SPA 주소 변경(pushState — loaderId 불변)·같은 문서 정상 입력은 묶음이 거부하지 않는다."""
    FX_PAGES["/spa"] = _SPA
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": fx + "/spa"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        page = _page(server)

        async def push() -> None:
            await page.evaluate("history.pushState({}, '', '/spa2')")

        if route == "element_id":
            _once(server, "isolated_element_info_for_gate", after=push)
            a, args = ActionType.CLICK, {"element_id": _eid(obs, lambda e: e.get("name") == "계속"),
                                         "epoch": obs.snapshot_epoch}
        elif route == "selector":
            _once(server, "isolated_selector_info_for_gate", after=push)
            a, args = ActionType.CLICK, {"selector": "#b"}
        elif route == "point":
            _once(server, "isolated_point_target", after=push)
            a, args = ActionType.CLICK, {"x": 80, "y": 76, "epoch": obs.snapshot_epoch}
        elif route == "newline":
            _once(server, "isolated_element_info_for_gate", after=push)
            a, args = ActionType.TYPE_TEXT, {"element_id": _eid(obs, lambda e: e.get("name") == "메모"),
                                             "epoch": obs.snapshot_epoch, "text": "hi\nthere"}
        elif route == "enter":
            await page.focus("#q")
            _once(server, "isolated_focus_target", after=push)
            a, args = ActionType.PRESS_KEY, {"key": "Enter"}
        else:  # 묶음 없는 평범한 같은 문서 클릭(판독 감싸지 않음)
            a, args = ActionType.CLICK, {"element_id": _eid(obs, lambda e: e.get("name") == "계속"),
                                         "epoch": obs.snapshot_epoch}
        r = await _call(server, a, args)
        await page.wait_for_timeout(200)
        q = await _truth(server, "document.getElementById('q').value")
    assert r.error_code is not ErrorCode.TOCTOU_MISMATCH, r.model_dump(mode="json")
    if route in ("element_id", "selector", "point", "plain"):
        assert r.success, r.error_message
        assert _effects() == 1
    elif route == "newline":
        assert q == "hithere", q  # 한 줄 입력칸: 줄바꿈은 Enter(값에 남지 않음) — 앞뒤 조각 모두 그 칸에
    else:
        assert r.success, r.error_message
