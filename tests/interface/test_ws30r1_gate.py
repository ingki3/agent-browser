"""WS-30 R1: HITL 게이트가 요소를 누르는 모든 경로에서 같은 판정을 내리는지.

독립 검증(FAIL)에서 찾은 구멍을 테스트로 옮겼다. 실제 BrowserMCPServer(헤드리스 Chromium) +
로컬 HTTP 서버만 쓴다(실사이트 없음).

* BLOCKING-1  같은 요소를 element_id·selector·포커스 Space·포커스 Enter 로 눌렀을 때 판정이 같다.
              원인: 게이트가 읽는 이름(_TARGET_INFO_JS)이 관찰 엔진 이름 규칙과 달랐다
              (`<input type=submit value=결제>` 는 관찰 이름 '결제', 게이트 이름 '').
* BLOCKING-2  Chromium 에서 Enter 로 폼이 제출되는 포커스 대상(실측 표)은 모두 '폼 제출' 게이트를 탄다.
              `<select>` 가 빠져 있었다. 모르는 태그(사용자 정의 요소)가 폼 안이면 fail-closed.
* BLOCKING-3  switch_frame 뒤 press_key: 키는 최상위 키보드로 가므로 판정도 최상위 기준 포커스로 한다.
* V2          다른 출처 프레임 안 포커스 — 판정 불가면 fail-closed.
* V3          포커스를 못 읽으면 fail-closed(Enter·Space).
* V8          폼 안 submit 버튼에 포커스 + Enter = 폼 제출(이름이 무해해도).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

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


@pytest.fixture
def other_origin():
    """같은 기계의 다른 출처(host 이름이 달라 출처가 다르다)."""
    srv = _serve("127.0.0.1")
    try:
        yield f"http://localhost:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(server: BrowserMCPServer, action: ActionType, args: Dict[str, Any]):
    return await server.call_tool(tool_name(action), args)


async def _element(server: BrowserMCPServer, name: str):
    obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    observation = obs.data["observation"]
    for e in observation["elements"]:
        if e["name"] == name:
            return e["element_id"], observation["snapshot_epoch"]
    raise AssertionError(f"요소 없음: {name} in {[e['name'] for e in observation['elements']]}")


def _page(server: BrowserMCPServer):
    return server._dispatcher.ctx.page  # noqa: SLF001


def _top(server: BrowserMCPServer):
    ctx = server._dispatcher.ctx  # noqa: SLF001
    return ctx.root_page or ctx.page


# ------------------------------------------------------------ BLOCKING-1 네 경로 같은 판정

_ONCLICK = "onclick=\"document.getElementById('out').textContent='눌림'\""

#: 요소 종류별 마크업(폼 밖 — 폼 제출이 아니라 이름 판정만 비교한다). {N} = 요소 이름.
ELEMENT_KINDS = {
    "button_text": "<button id=t " + _ONCLICK + ">{N}</button>",
    "input_submit_value": "<input id=t type=submit value=\"{N}\" " + _ONCLICK + ">",
    "input_button_value": "<input id=t type=button value=\"{N}\" " + _ONCLICK + ">",
    "input_reset_value": "<input id=t type=reset value=\"{N}\" " + _ONCLICK + ">",
    "aria_label": "<button id=t aria-label=\"{N}\" " + _ONCLICK + ">&#x1F4B3;</button>",
    "aria_labelledby": "<span id=lbl>{N}</span><button id=t aria-labelledby=lbl "
    + _ONCLICK + ">&rarr;</button>",
    "input_image_alt": "<input id=t type=image alt=\"{N}\" style=\"width:60px;height:30px\" "
    + _ONCLICK + ">",
}


async def _four_paths(server: BrowserMCPServer, url: str, name: str) -> Dict[str, Any]:
    """element_id·selector·포커스 Space·포커스 Enter 로 같은 요소를 누른다. 경로별 (차단?, 눌림?)."""
    out: Dict[str, Any] = {}
    for path in ("element_id", "selector", "focus_space", "focus_enter"):
        await _call(server, ActionType.NAVIGATE, {"url": url})
        if path == "element_id":
            eid, epoch = await _element(server, name)
            r = await _call(server, ActionType.CLICK, {"element_id": eid, "epoch": epoch})
        elif path == "selector":
            r = await _call(server, ActionType.CLICK, {"selector": "#t"})
        else:
            await _page(server).focus("#t")
            key = "Space" if path == "focus_space" else "Enter"
            r = await _call(server, ActionType.PRESS_KEY, {"key": key})
        pressed = (await _page(server).text_content("#out")) == "눌림"
        blocked = r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
        out[path] = (blocked, pressed)
    return out


@requires_chromium
@pytest.mark.parametrize("kind", sorted(ELEMENT_KINDS))
@pytest.mark.parametrize("name,expect_blocked", [("결제", True), ("보기", False)])
async def test_four_paths_same_verdict(site, kind, name, expect_blocked):
    PAGES["/kind"] = (
        "<!doctype html><meta charset=utf-8><title>k</title><p id=out>대기</p>"
        + ELEMENT_KINDS[kind].replace("{N}", name)
    )
    async with BrowserMCPServer() as server:
        verdicts = await _four_paths(server, site + "/kind", name)
    for path, (blocked, pressed) in verdicts.items():
        assert blocked is expect_blocked, (kind, name, path, verdicts)
        if blocked:
            assert not pressed, f"차단됐는데 눌림: {kind} {path}"
        else:
            assert pressed, f"통과했는데 효과 없음: {kind} {path}"


# ------------------------------------------------------------ BLOCKING-2 Enter 제출 대상 전수

#: Chromium 실측(/tmp/ws30r1/enter_survey.py): 제출 버튼이 있는 폼 안에서 Enter 가 폼을 제출하는가.
ENTER_TARGETS = {
    # 제출됨
    "input_text": ("<input id=t type=text>", True),
    "input_search": ("<input id=t type=search>", True),
    "input_email": ("<input id=t type=email>", True),
    "input_number": ("<input id=t type=number>", True),
    "input_password": ("<input id=t type=password>", True),
    "input_tel": ("<input id=t type=tel>", True),
    "input_url": ("<input id=t type=url>", True),
    "input_date": ("<input id=t type=date>", True),
    # WS-30b NB-R1-1(X12): 날짜·시간 계열 전부와 type 없음·모르는 type(브라우저는 text 로 다룸).
    "input_time": ("<input id=t type=time>", True),
    "input_datetime_local": ("<input id=t type=datetime-local>", True),
    "input_month": ("<input id=t type=month>", True),
    "input_week": ("<input id=t type=week>", True),
    "input_no_type": ("<input id=t name=q>", True),
    "input_unknown_type": ("<input id=t type=foo>", True),
    "input_checkbox": ("<input id=t type=checkbox>", True),
    "input_radio": ("<input id=t type=radio name=r>", True),
    "input_range": ("<input id=t type=range>", True),
    "select": ("<select id=t name=s><option>a</option><option>b</option></select>", True),
    "button_default": ("<button id=t>다음</button>", True),
    "input_image": ("<input id=t type=image alt=다음 style=\"width:40px;height:20px\">", True),
    # 제출 안 됨
    "input_color": ("<input id=t type=color>", False),
    "input_file": ("<input id=t type=file>", False),
    "button_button": ("<button id=t type=button>다음</button>", False),
    "textarea": ("<textarea id=t></textarea>", False),
    "contenteditable": ("<div id=t contenteditable=true>x</div>", False),
}


@requires_chromium
@pytest.mark.parametrize("kind", sorted(ENTER_TARGETS))
async def test_enter_on_every_submitting_target_is_gated(site, kind):
    markup, submits = ENTER_TARGETS[kind]
    PAGES["/f"] = (
        "<!doctype html><meta charset=utf-8><title>f</title>"
        f"<form action=\"/checkout\">{markup}<input type=submit value=\"다음 단계\"></form>"
    )
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#t")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _page(server).wait_for_timeout(200)
        url = _page(server).url
    if submits:
        assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (kind, r.error_message)
        assert "폼 제출" in (r.error_message or "")
        assert "/checkout" not in url, f"차단됐는데 제출됨: {kind}"
    else:
        assert r.success, (kind, r.error_code, r.error_message)
        assert "/checkout" not in url


#: WS-30b NB-R1-1: `form=` 속성으로 폼 밖에서 폼에 연결된 요소 — Chromium 은 Enter 로 그 폼을 제출한다.
FORM_ATTR_TARGETS = {
    "input_text": "<input id=t name=q form=f>",
    "button": "<button id=t form=f>다음</button>",
    "input_submit": "<input id=t type=submit form=f value=\"다음\">",
}


@requires_chromium
@pytest.mark.parametrize("kind", sorted(FORM_ATTR_TARGETS))
async def test_enter_on_form_attribute_outside_form_is_gated(site, kind):
    PAGES["/fa"] = (
        "<!doctype html><meta charset=utf-8><title>fa</title>"
        "<form id=f action=\"/checkout\"><input name=a><input type=submit value=go></form>"
        + FORM_ATTR_TARGETS[kind]
    )
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/fa"})
        await _page(server).focus("#t")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _page(server).wait_for_timeout(200)
        url = _page(server).url
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (kind, r.error_message)
    assert "/checkout" not in url, f"차단됐는데 제출됨: {kind}"


@requires_chromium
async def test_enter_on_select_pre_approved_submits(site):
    PAGES["/f"] = (
        "<!doctype html><meta charset=utf-8><title>f</title><form action=\"/checkout\">"
        "<select id=t name=s><option>a</option></select><input type=submit value=go></form>"
    )
    async with BrowserMCPServer(pre_approved_actions=("press_key:*",)) as server:
        # select 의 Enter 가 폼을 암묵 제출하는지는 플랫폼마다 다르다(macOS Chromium 은
        # 제출, CI 의 Linux Chromium 은 제출하지 않음). 같은 브라우저에서 게이트를 거치지
        # 않은 기준 동작을 먼저 재고, 사전 승인된 press_key 가 그와 같은지 본다.
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        page = _page(server)
        await page.focus("#t")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(300)
        browser_submits = "/checkout" in page.url

        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#t")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _page(server).wait_for_timeout(300)
        url = _page(server).url
    # 사전 승인이면 게이트는 통과해야 한다(차단 오류 없음).
    assert r.error_code is not ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert ("/checkout" in url) == browser_submits, (browser_submits, url, r.error_code)
    if browser_submits:
        assert r.success, (r.error_code, r.error_message)


@requires_chromium
async def test_unknown_tag_in_form_fails_closed(site):
    """폼 안의 사용자 정의 요소(폼 연계 요소일 수 있음)는 판정 불가 → 차단."""
    PAGES["/f"] = (
        "<!doctype html><meta charset=utf-8><title>f</title><form action=\"/checkout\">"
        "<x-pay id=t tabindex=0>위젯</x-pay><input type=submit value=go></form>"
    )
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#t")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message


# ------------------------------------------------------------ V8 폼 안 submit 버튼 포커스

@requires_chromium
@pytest.mark.parametrize("key", ["Enter", "Space"])
async def test_focused_submit_button_in_form_is_form_submit(site, key):
    """이름이 무해('다음')해도 폼 안 submit 버튼을 키로 누르면 폼 제출이다."""
    PAGES["/f"] = (
        "<!doctype html><meta charset=utf-8><title>f</title><form action=\"/checkout\">"
        "<input name=q value=x><button id=t>다음</button></form>"
    )
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#t")
        r = await _call(server, ActionType.PRESS_KEY, {"key": key})
        await _page(server).wait_for_timeout(200)
        url = _page(server).url
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert "폼 제출" in (r.error_message or "")
    assert "/checkout" not in url


# ------------------------------------------------------------ BLOCKING-3 switch_frame 뒤 press_key

def _frame_pages(frame_origin: str) -> None:
    PAGES["/top"] = (
        "<!doctype html><meta charset=utf-8><title>top</title>"
        "<form action=\"/checkout\"><input id=q name=q aria-label=\"최상위 검색\"></form>"
        f"<iframe id=sf src=\"{frame_origin}/inner\" width=300 height=120></iframe>"
    )
    PAGES["/inner"] = (
        "<!doctype html><meta charset=utf-8><title>inner</title>"
        "<form action=\"/fdone\"><input id=fq name=fq aria-label=\"프레임 검색\"></form>"
        "<input id=loose aria-label=\"프레임 메모\">"
    )


@requires_chromium
async def test_switch_frame_then_enter_on_top_form_is_gated(site):
    """포커스는 최상위 폼 입력칸, 컨텍스트는 프레임 — 키는 최상위로 가므로 차단돼야 한다."""
    _frame_pages(site)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        eid, epoch = await _element(server, "최상위 검색")
        await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "x"})
        assert (await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#sf"})).success
        top = _top(server)
        assert await top.evaluate("document.activeElement.id") == "q"
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await top.wait_for_timeout(200)
        url = top.url
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert "/checkout" not in url


async def _focus_frame_input(server: BrowserMCPServer, name: str) -> None:
    assert (await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#sf"})).success
    eid, epoch = await _element(server, name)
    typed = await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "y"})
    assert typed.success, (typed.error_code, typed.error_message)


@requires_chromium
async def test_switch_frame_then_enter_on_frame_form_is_gated(site):
    _frame_pages(site)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        await _focus_frame_input(server, "프레임 검색")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _top(server).wait_for_timeout(200)
        frame_url = _page(server).url
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert "/fdone" not in frame_url


@requires_chromium
async def test_switch_frame_enter_pre_approved_both_pass(site):
    _frame_pages(site)
    async with BrowserMCPServer(pre_approved_actions=("press_key:*",)) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        await _focus_frame_input(server, "프레임 검색")
        in_frame = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _top(server).wait_for_timeout(200)
        frame_url = _page(server).url
        await _call(server, ActionType.SWITCH_FRAME, {"to_main": True})
        eid, epoch = await _element(server, "최상위 검색")
        await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "x"})
        await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#sf"})
        top_form = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _top(server).wait_for_timeout(300)
        top_url = _top(server).url
    assert in_frame.success, (in_frame.error_code, in_frame.error_message)
    assert "/fdone" in frame_url
    assert top_form.success, (top_form.error_code, top_form.error_message)
    assert "/checkout" in top_url


@requires_chromium
async def test_switch_frame_enter_outside_form_not_overblocked(site):
    _frame_pages(site)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        await _focus_frame_input(server, "프레임 메모")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert r.success, (r.error_code, r.error_message)


# ------------------------------------------------------------ V2 다른 출처 프레임

@requires_chromium
async def test_cross_origin_frame_form_enter_is_gated(site, other_origin):
    _frame_pages(other_origin)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        await _focus_frame_input(server, "프레임 검색")
        # 최상위 문서에서 보면 포커스는 다른 출처 iframe 안이다(내용을 읽을 수 없다).
        opaque = await _top(server).evaluate(
            "(() => { try { return !document.activeElement.contentDocument; }"
            " catch (e) { return true; } })()"
        )
        assert opaque is True
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _top(server).wait_for_timeout(200)
        frame_url = _page(server).url
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert "/fdone" not in frame_url


@requires_chromium
async def test_forged_has_focus_in_two_frames_fails_closed(site, other_origin):
    """WS-30b NB-R1-1(X6): 두 다른 출처 프레임이 모두 `document.hasFocus()` 를 true 라고 속이면
    포커스 사슬이 한 줄이 아니다 → 판정 불가(차단). 가장 깊은 프레임만 믿으면 폼 밖 요소로 읽혀
    통과했다(포커스는 실제로 다른 프레임의 폼 입력칸)."""
    PAGES["/two"] = (
        "<!doctype html><meta charset=utf-8><title>two</title>"
        f"<iframe id=sf src=\"{other_origin}/inner\" width=300 height=120></iframe>"
        "<iframe id=mid src=\"/mid\" width=300 height=120></iframe>"
    )
    PAGES["/mid"] = (
        "<!doctype html><meta charset=utf-8><p>mid</p>"
        f"<iframe id=deep src=\"{other_origin}/inner\" width=280 height=100></iframe>"
    )
    PAGES["/inner"] = (
        "<!doctype html><meta charset=utf-8><title>inner</title>"
        "<form action=\"/fdone\"><input id=fq name=fq aria-label=\"프레임 검색\"></form>"
        "<input id=loose aria-label=\"프레임 메모\">"
    )
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/two"})
        page = _page(server)
        # 두 inner 프레임이 모두 뜬 뒤에 위조한다(전제 확인 — 안 뜬 채 위조하면 갈래가 안 생긴다).
        await page.frame_locator("#sf").locator("#fq").wait_for()
        await page.frame_locator("#mid").frame_locator("#deep").locator("#fq").wait_for()
        inner = [f for f in page.frames if "/inner" in f.url]
        assert len(inner) == 2, [f.url for f in page.frames]
        await page.frame_locator("#sf").locator("#fq").focus()
        for f in inner:
            await f.evaluate("document.hasFocus = () => true")
        assert all([await f.evaluate("document.hasFocus()") for f in inner])
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await page.wait_for_timeout(300)
        submitted = any("/fdone" in f.url for f in page.frames)
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert not submitted


@requires_chromium
async def test_cross_origin_frame_non_form_enter_passes(site, other_origin):
    """다른 출처 프레임이어도 실제 포커스(폼 밖 입력칸)를 읽어 판정한다 — 과차단 없음."""
    _frame_pages(other_origin)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        await _focus_frame_input(server, "프레임 메모")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert r.success, (r.error_code, r.error_message)


@requires_chromium
@pytest.mark.parametrize("key", ["Enter", "Space"])
async def test_opaque_focus_fails_closed(site, key, monkeypatch):
    """포커스 대상이 다른 출처라 끝내 읽지 못하면(opaque) 판정 불가 → 차단."""
    PAGES["/plain"] = "<!doctype html><meta charset=utf-8><title>p</title><input id=q>"
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/plain"})

        async def opaque() -> Optional[Dict[str, Any]]:
            return {"opaque": True, "tag": "IFRAME"}

        monkeypatch.setattr(server._dispatcher, "focused_target", opaque)  # noqa: SLF001
        r = await _call(server, ActionType.PRESS_KEY, {"key": key})
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message


# ------------------------------------------------------------ V3 포커스 못 읽음

@requires_chromium
@pytest.mark.parametrize("key", ["Enter", "Space"])
async def test_unreadable_focus_fails_closed(site, key, monkeypatch):
    PAGES["/plain"] = "<!doctype html><meta charset=utf-8><title>p</title><input id=q>"
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/plain"})

        async def unreadable() -> Optional[Dict[str, Any]]:
            return None

        monkeypatch.setattr(server._dispatcher, "focused_target", unreadable)  # noqa: SLF001
        r = await _call(server, ActionType.PRESS_KEY, {"key": key})
        tab = await _call(server, ActionType.PRESS_KEY, {"key": "Tab"})
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert tab.success, "Enter/Space 가 아닌 키는 포커스 판정 대상이 아니다"
