"""WS-85 (#171 #218 #163): 판정 읽기를 격리 world 로도 — 페이지가 getter·prototype 을 바꿔 판정을 속이는 경로.

결합 규칙(리뷰 B1): #171 challenge = 격리 결과, 없으면 페이지 결과(OR). #218 민감 칸 = 페이지 OR 격리(입력 직후
격리 world activeElement — 실패 시 '민감 처리' 금지, B4). #163 클릭·selector·개행 = 두 판정을 각각 _gate_rank 로 분류해
높은 쪽(같으면 페이지), 판독할 수 있어야 하는데 실패하고 페이지 판정 < HIGH 면 승인 경로(B2). 실제 BrowserMCPServer
(헤드리스 Chromium) + 로컬 HTTP 만, 비밀값은 합성. 실제 효과(제출·눌림)는 페이지 world 를 거치지 않는 CDP 격리 판독으로
단정한다(_truth).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401

PAGES: Dict[str, str] = {}


def _serve(host: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            body = PAGES.get(path, "<!doctype html><meta charset=utf-8><title>x</title><p>x</p>")
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer((host, 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def site():
    srv = _serve("127.0.0.1")
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(server: BrowserMCPServer, action: ActionType, args: Dict[str, Any]):
    return await server.call_tool(tool_name(action), args)


def _page(server: BrowserMCPServer):
    return server._dispatcher.ctx.page  # noqa: SLF001


async def _truth(server: BrowserMCPServer, expression: str) -> Any:
    """실제 값 — 페이지 world getter 를 거치지 않는 CDP 격리 world 판독(위조가 닿지 않는다)."""
    return await server._dispatcher.isolated_top_evaluate(expression)  # noqa: SLF001


async def _status(server: BrowserMCPServer) -> str:
    await _page(server).wait_for_timeout(150)
    return str(await _truth(server, "document.getElementById('out').textContent"))


def _script(js: str) -> str:
    return f"<script>{js}</script>" if js else ""


HEAD = "<!doctype html><meta charset=utf-8><title>g</title><body style=margin:0><p id=out>대기</p>"
ON_SUBMIT = "onsubmit=\"document.getElementById('out').textContent='제출됨';return false\""
ON_CLICK = "onclick=\"document.getElementById('out').textContent='눌림'\""
SECRET = "SynthPw-7x9Q-ws85"

# ------------------------------------------------------------------ #171 challenge·handoff

T_CHAL = ("Object.defineProperty(HTMLElement.prototype,'innerText',{get(){return '환영합니다'},configurable:true});"
          "Object.defineProperty(Document.prototype,'title',{get(){return '홈'},configurable:true});"
          "Object.defineProperty(Node.prototype,'textContent',{get(){return '환영합니다'},configurable:true});")
CHAL_PAGE = ("<!doctype html><meta charset=utf-8><title>보안 확인</title><body><p>보안 확인을 완료해 주세요</p>")


@requires_chromium
async def test_171_hidden_challenge_detected_with_handoff(site):
    PAGES["/c0"] = CHAL_PAGE
    PAGES["/c1"] = CHAL_PAGE + _script(T_CHAL)
    async with BrowserMCPServer() as server:
        base = await _call(server, ActionType.NAVIGATE, {"url": site + "/c0"})
        assert base.data.get("challenge") is not None  # 전제: 위조 전에는 잡힌다
        # 전제(규칙 4): 페이지 world 프로브는 실제로 속는다.
        from browser.challenge import detect_challenge

        hidden = await _call(server, ActionType.NAVIGATE, {"url": site + "/c1"})
        fooled = await detect_challenge(_page(server))
    assert fooled.detected is False
    assert hidden.data.get("challenge") == base.data["challenge"]
    assert hidden.data.get("handoff") is not None
    assert hidden.data.get("handoff") == base.data.get("handoff")


@requires_chromium
async def test_171_normal_page_has_no_challenge(site):
    PAGES["/n"] = HEAD + "<h1>환영합니다</h1><p>평범한 본문</p>"
    async with BrowserMCPServer() as server:
        r = await _call(server, ActionType.NAVIGATE, {"url": site + "/n"})
    assert r.success and r.data.get("challenge") is None and "handoff" not in r.data


# ------------------------------------------------------------------ #218 민감 칸

T_TYPE = "Object.defineProperty(HTMLInputElement.prototype,'type',{get(){return 'text'},configurable:true});"


def _eid(obs, pred) -> str:
    for e in obs.data["observation"]["elements"]:
        if pred(e):
            return e["element_id"]
    raise AssertionError(obs.data["observation"]["elements"])


@requires_chromium
async def test_218_spoofed_password_type_text_hides_secret(site):
    PAGES["/p"] = HEAD + "<input id=p type=password aria-label='pw'>" + _script(T_TYPE)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/p"})
        assert await _page(server).evaluate("document.getElementById('p').type") == "text"  # 전제: 위조됨
        assert await _truth(server, "document.getElementById('p').type") == "password"
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.TYPE_TEXT, {
            "element_id": _eid(obs, lambda e: "pw" in (e.get("name") or "")), "text": SECRET,
            "epoch": obs.snapshot_epoch})
        dump = json.dumps(r.model_dump(mode="json"), ensure_ascii=False)
        typed = await _truth(server, "document.getElementById('p').value")
    assert typed == SECRET  # 실제로 입력됐다
    assert SECRET not in dump, dump


@requires_chromium
async def test_218_spoofed_password_press_key_is_sensitive(site):
    PAGES["/p"] = HEAD + "<input id=p type=password aria-label='pw'>" + _script(T_TYPE)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/p"})
        await _page(server).focus("#p")
        k = await _call(server, ActionType.PRESS_KEY, {"key": "z"})
        sensitive = server._dispatcher.last_key_sensitive  # noqa: SLF001
    assert k.success, k.error_message
    assert sensitive is True


@requires_chromium
async def test_218_plain_input_not_registered_as_secret(site):
    """과잉 0(B4): 일반 입력칸(검색어)은 비밀로 등록하지 않는다 — 결과에 값이 그대로 남는다."""
    PAGES["/s"] = HEAD + "<input id=q aria-label='검색어'>"
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/s"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.TYPE_TEXT, {
            "element_id": _eid(obs, lambda e: "검색어" in (e.get("name") or "")), "text": "plainword-ws85",
            "epoch": obs.snapshot_epoch})
        await _page(server).focus("#q")
        k = await _call(server, ActionType.PRESS_KEY, {"key": "a"})
        sensitive = server._dispatcher.last_key_sensitive  # noqa: SLF001
        from security.secret_registry import mask_registered

        masked = mask_registered("plainword-ws85")
    assert r.success and k.success
    assert sensitive is False
    assert masked == "plainword-ws85", masked


# ------------------------------------------------------------------ #163 클릭·selector·개행 (B1~B3)

T_FORM = ("Object.defineProperty(HTMLButtonElement.prototype,'form',{get(){return null},configurable:true});"
          "Object.defineProperty(HTMLInputElement.prototype,'form',{get(){return null},configurable:true});")
T_ARIA = ("(()=>{const g=Element.prototype.getAttribute;Element.prototype.getAttribute=function(n){"
          "if(n==='aria-label'||n==='class'||n==='id')return null;return g.call(this,n)}})();")
T_TEXT = ("Object.defineProperty(HTMLElement.prototype,'innerText',{get(){return '보기'},configurable:true});"
          "Object.defineProperty(Node.prototype,'textContent',{get(){return '보기'},configurable:true});")
_BOX = "position:absolute;left:20px;top:60px;width:120px;height:32px"


def _by_name(name: str):
    return lambda e: (e.get("name") or "") == name


async def _click_eid(server, name: str):
    obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    return await _call(server, ActionType.CLICK, {"element_id": _eid(obs, _by_name(name)),
                                                  "epoch": obs.snapshot_epoch})


def _blocked(r) -> bool:
    return r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED


@requires_chromium
async def test_163_element_id_form_destination_spoof_blocked(site):
    PAGES["/a"] = (HEAD + f"<form action='/account/delete' method=post {ON_SUBMIT}><button id=b>계속</button></form>"
                   + _script(T_FORM))
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/a"})
        page_info = await server._dispatcher.describe_element_for_gate(  # noqa: SLF001
            _eid(await _call(server, ActionType.OBSERVE_PAGE, {}), _by_name("계속")))
        assert not [s for s in page_info["info"]["signals"] if s[0] == "form_action"]  # 전제: 페이지 world 는 속는다
        r = await _click_eid(server, "계속")
        status = await _status(server)
    assert _blocked(r), (r.success, r.error_message)
    assert r.data["gate_basis"]["source"] == "form_action"
    assert status == "대기"


@requires_chromium
async def test_163_selector_aria_spoof_blocked(site):
    PAGES["/b"] = HEAD + f"<button id=b aria-label='계정 삭제' {ON_CLICK}>X</button>" + _script(T_ARIA)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b"})
        r = await _call(server, ActionType.CLICK, {"selector": "#b"})
        status = await _status(server)
    assert _blocked(r), (r.success, r.error_message)
    assert status == "대기"


@requires_chromium
async def test_163_text_getter_spoof_blocked_element_id_and_selector(site):
    """WS-86 리뷰 probe_d: '결제하기' 버튼의 innerText·textContent getter 를 '보기' 로 — element_id·selector 2/6 경로가 눌렸다."""
    PAGES["/t"] = HEAD + f"<button id=b {ON_CLICK}>결제하기</button>" + _script(T_TEXT)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/t"})
        r1 = await _call(server, ActionType.CLICK, {"selector": "#b"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        eid = next(e["element_id"] for e in obs.data["observation"]["elements"]
                   if (server._engine.get_handle(e["element_id"]).css_path or "").endswith("#b"))  # noqa: SLF001
        r2 = await _call(server, ActionType.CLICK, {"element_id": eid, "epoch": obs.snapshot_epoch})
        status = await _status(server)
    assert _blocked(r1) and _blocked(r2), (r1.error_message, r2.error_message)
    assert status == "대기"


@requires_chromium
async def test_163_newline_submit_spoof_blocked(site):
    PAGES["/c"] = HEAD + f"<form action='/s' {ON_SUBMIT}><input id=q aria-label='메모'></form>" + _script(T_FORM)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/c"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.TYPE_TEXT, {"element_id": _eid(obs, lambda e: "메모" in (e.get("name") or "")),
                                                       "text": "hi\n", "epoch": obs.snapshot_epoch})
        status = await _status(server)
    assert _blocked(r), (r.success, r.error_message)
    assert r.data["gate_basis"]["source"] == "form_submit"
    assert status == "대기"


_DUP_SHADOW = (
    "window.dup=function(){const h=document.createElement('div');h.style.cssText='" + _BOX + "';"
    "document.body.insertBefore(h,document.body.firstChild);const r=h.attachShadow({mode:'open'});"
    "r.innerHTML=\"<button id=b style='position:absolute;left:0;top:0;width:120px;height:32px;margin:0'>계속</button>\";};"
)


@requires_chromium
async def test_163_forced_binding_failure_goes_to_approval_not_page_world(site):
    """리뷰 F4·B2: 관찰 뒤 열린 shadow 에 같은 상자의 복제 button#b — 결속 불가. 페이지 world(위조 LOW)로 내려가지 않고 승인."""
    PAGES["/d"] = (HEAD + f"<form action='/account/delete' method=post {ON_SUBMIT}>"
                   f"<button id=b style='{_BOX};margin:0'>계속</button></form>" + _script(T_FORM + _DUP_SHADOW))
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/d"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        eid = _eid(obs, _by_name("계속"))
        await _page(server).evaluate("window.dup()")
        iso = await server._dispatcher.isolated_element_info_for_gate(eid)  # noqa: SLF001
        r = await _call(server, ActionType.CLICK, {"element_id": eid, "epoch": obs.snapshot_epoch})
        status = await _status(server)
    assert "unresolved" in iso, iso
    assert _blocked(r), (r.success, r.error_message)
    assert r.data["gate_basis"]["source"] in ("unresolved", "form_action"), r.data["gate_basis"]
    assert status == "대기"


_SHADOW_BENIGN = (
    "<div id=host></div><nav><a href='/home' " + ON_CLICK.replace("눌림", "홈") + ">홈</a></nav>"
    "<script>const r=document.getElementById('host').attachShadow({mode:'open'});"
    "r.innerHTML=\"<button onclick=\\\"document.getElementById('out').textContent='그림자'\\\">그림자 버튼</button>"
    "<a href='/x'>그림자 링크</a>\";</script>"
)


@requires_chromium
async def test_163_no_new_approval_for_normal_and_open_shadow_targets(site):
    """과잉 0(B3·F5): 평범한 버튼·열린 shadow 버튼(role+name)·shadow 와 겹치는 가벼운 DOM 링크 — 격리 판독이 결속되어 승인 없음."""
    PAGES["/e"] = HEAD + f"<input type=button id=v value='보기' {ON_CLICK}>" + _SHADOW_BENIGN
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/e"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        reads = {}
        for e in obs.data["observation"]["elements"]:
            reads[e["name"]] = await server._dispatcher.isolated_element_info_for_gate(e["element_id"])  # noqa: SLF001
        r1 = await _click_eid(server, "보기")
        s1 = await _status(server)
        r2 = await _click_eid(server, "그림자 버튼")
        s2 = await _status(server)
        r3 = await _call(server, ActionType.CLICK, {"selector": "#v"})
    failed = {k: v for k, v in reads.items() if "info" not in v}
    assert not failed, failed
    assert {"보기", "그림자 버튼", "그림자 링크", "홈"} <= set(reads)
    assert r1.success and s1 == "눌림", r1.error_message
    assert r2.success and s2 == "그림자", r2.error_message
    assert r3.success, r3.error_message


_CUSTOM = (
    "<my-btn id=c role=button tabindex=0 " + ON_CLICK + ">구매 내역 보기</my-btn>"
    "<script>customElements.define('my-btn',class extends HTMLElement{get value(){return '구매확정'}"
    "get type(){return 'submit'}});</script>"
)


@requires_chromium
async def test_163_custom_element_page_defined_getters_keep_page_verdict(site):
    """F3: 사용자 정의 요소의 page 정의 value/type 은 격리 world 에서 보이지 않는다 — 판정은 높은 쪽(같으면 페이지) 그대로."""
    PAGES["/f"] = HEAD + _CUSTOM
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        eid = _eid(obs, _by_name("구매 내역 보기"))
        page_info = (await server._dispatcher.describe_element_for_gate(eid))["info"]  # noqa: SLF001
        iso_info = (await server._dispatcher.isolated_element_info_for_gate(eid))["info"]  # noqa: SLF001
        r = await _call(server, ActionType.CLICK, {"element_id": eid, "epoch": obs.snapshot_epoch})
    assert page_info["value"] == "구매확정" and iso_info["value"] == ""  # F3 그대로 재현
    from security import ActionContext
    from security.hitl import assess_risk

    base = assess_risk(ActionContext(action=ActionType.CLICK, element_name="구매 내역 보기",
                                     signals=tuple(tuple(s) for s in page_info["signals"])))
    assert (r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED) == (base.risk.value == "high"), (r.error_message, base)


@requires_chromium
async def test_163_same_origin_frame_element_id_spoof_blocked(site):
    PAGES["/inner"] = ("<!doctype html><meta charset=utf-8><body style=margin:0><p id=out>대기</p>"
                       f"<form action='/account/delete' method=post {ON_SUBMIT}><button id=b>계속</button></form>"
                       + _script(T_FORM))
    PAGES["/top"] = HEAD + "<iframe id=fr name=fr src='/inner' style='width:400px;height:200px;border:0'></iframe>"
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        await _page(server).wait_for_timeout(200)
        sw = await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#fr"})
        assert sw.success, sw.error_message
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.CLICK, {"element_id": _eid(obs, _by_name("계속")),
                                                   "epoch": obs.snapshot_epoch})
        status = await _page(server).evaluate("document.getElementById('out').textContent")
    assert _blocked(r), (r.success, r.error_message)
    assert status == "대기"


# ------------------------------------------------------------------ #163 승인 결속 의미(_MEANING_JS, B2·B3)

_SWAP = ("window.swap=function(){const f=document.getElementById('f');"
         "const old=Object.getOwnPropertyDescriptor(HTMLFormElement.prototype,'action').get.call(f);"
         "f.setAttribute('action','/account/delete-all?confirm=1');"
         "Object.defineProperty(HTMLFormElement.prototype,'action',{get(){return old},configurable:true});};")
_MEAN_PAGE = (HEAD + f"<form id=f action='/account/delete' method=post {ON_SUBMIT}><input id=q aria-label='메모'>"
              "<button id=b>계속</button></form>" + _script(_SWAP))


async def _pinned(server, params):
    got = await server._dispatcher.pin_target(params)  # noqa: SLF001
    assert "pin" in got, got
    return got["pin"]


@requires_chromium
@pytest.mark.parametrize("target", ["element_id", "selector", "point", "focus"])
async def test_163_meaning_swap_after_pin_is_refused(site, target):
    PAGES["/m"] = _MEAN_PAGE
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/m"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        if target == "element_id":
            params = {"element_id": _eid(obs, _by_name("계속")), "epoch": obs.snapshot_epoch}
        elif target == "selector":
            params = {"selector": "#b"}
        elif target == "point":
            box = await _page(server).locator("#b").bounding_box()
            params = {"x": int(box["x"] + box["width"] / 2), "y": int(box["y"] + box["height"] / 2),
                      "epoch": obs.snapshot_epoch}
        else:
            await _page(server).focus("#q")
            params = {"key": "Enter"}
        pin = await _pinned(server, params)
        same = await server._dispatcher.pinned_target_check(pin, params)  # noqa: SLF001
        await _page(server).evaluate("window.swap()")
        # 전제: 페이지 world 의미 판독은 속는다(옛 목적지).
        page_now = await pin.node.evaluate("(el) => (el.form || el.closest('form')).action")
        swapped = await server._dispatcher.pinned_target_check(pin, params)  # noqa: SLF001
        truth = await _truth(server, "document.getElementById('f').action")
    assert same == {"fresh": True, "detail": ""}, same  # 과잉 0: 바뀌지 않았으면 그대로 통과
    assert page_now.endswith("/account/delete") and truth.endswith("delete-all?confirm=1")
    assert swapped["fresh"] is False and "action" in swapped["detail"], swapped
