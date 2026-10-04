"""WS-31 R1: 문맥 신호 수집·규칙 보정이 실제 경로(element_id·selector·좌표)에서 같은 판정을 내는지.

실제 BrowserMCPServer(헤드리스 Chromium) + 로컬 HTTP 서버만 쓴다. 무인 기본·사전승인 없음.

* NB-1 (i) 빈 `<i>` 40개 패딩 뒤의 ::before 글자·img alt·fa-trash·svg title(검증 s19/s20/s22/s23/s34)
  — 신호를 가진 자손만 고르게 바꿔 차단. (ii) aria-label 긴 꼬리(이름 200자 컷 넘김) — aria 원천이
  있어야 막힌다(검증자 M6 'aria 끔' 뮤테이션을 죽인다).
* NB-2 Cf 문자(U+200F·U+2064·U+061C)·CGJ(U+034F) 끼운 '결제'(검증 s31~s33·e08).
* NB-6 과차단 8종(검증 x06·x07·x11·x13·x20·x22·x25·e04)은 통과, 같은 모양의 위험 대조군은 차단.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401

PAGES: Dict[str, str] = {}


def _serve() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = PAGES.get(self.path.split("?", 1)[0], "<!doctype html><meta charset=utf-8><p>x</p>")
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def site():
    srv = _serve()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(server: BrowserMCPServer, action: ActionType, args: Dict[str, Any]):
    return await server.call_tool(tool_name(action), args)


_HEAD = "<!doctype html><meta charset=utf-8><title>g</title><body style=margin:0><p id=out>대기</p>"
_OUT = "document.getElementById('out').textContent='눌림'"
_PRESS = f"onclick=\"{_OUT}\""
_S = "style=\"display:inline-block;width:160px;height:50px\""
_A = f"onclick=\"event.preventDefault();{_OUT}\" {_S}"
_FORM = f"onsubmit=\"{_OUT};return false\""
_PAD = "<i></i>" * 40

#: 위험 — 모든 경로에서 차단돼야 한다.
RISKY: Dict[str, str] = {
    # NB-1 (i) 빈 요소 패딩
    "s19_pseudo_deep": f"<style>.a::after{{content:'›'}} #deep::before{{content:'결제'}}</style><button id=t {_PRESS} {_S}>{_PAD}<span id=deep></span></button>",
    "s20_pseudo_child21": f"<style>#deep::before{{content:'결제'}}</style><button id=t {_PRESS} {_S}>{'<i></i>' * 20}<span id=deep></span></button>",
    "s22_img_alt_deep": f"<button id=t {_PRESS} {_S}>{_PAD}<img alt=결제 width=10 height=10></button>",
    "s23_icon_deep": f"<button id=t {_PRESS} {_S}>{_PAD}<i class=\"fa fa-trash\"></i></button>",
    "s34_svg_title_deep": f"<button id=t {_PRESS} {_S}>{_PAD}<svg width=16 height=16><title>삭제</title></svg></button>",
    "pad_400_alt": f"<button id=t {_PRESS} {_S}>{'<i></i>' * 400}<img alt=결제 width=10 height=10></button>",
    "pad_classed_60_icon": f"<button id=t {_PRESS} {_S}>{'<i class=x></i>' * 60}<i class=\"bi bi-trash\"></i></button>",
    # NB-1 (ii) aria 긴 꼬리 — 이름(200자)엔 '결제'가 없고 aria 원천에만 있다
    "aria_long_tail": f"<button id=t aria-label=\"{'가' * 240} 결제\" {_PRESS} {_S}>다음</button>",
    # NB-2 서식 문자
    "cf_rlm": f"<button id=t {_PRESS} {_S}>결\u200f제</button>",
    "cf_invisible_plus": f"<button id=t {_PRESS} {_S}>결\u2064제</button>",
    "cf_alm": f"<button id=t {_PRESS} {_S}>결\u061c제</button>",
    "cgj": f"<button id=t {_PRESS} {_S}>결\u034f제</button>",
    # NB-6 대조군(완화가 구멍이 되지 않는지)
    "link_delete_href": f"<a id=t href=\"/account/delete\" {_A}>항목</a>",
    "link_named_pay": f"<a id=t href=\"/help\" {_A}>결제하기</a>",
    "link_aria_pay": f"<a id=t href=\"/help\" aria-label=\"결제\" {_A}><i class=\"fa fa-info\"></i></a>",
    "link_child_alt": f"<a id=t href=\"/help\" {_A}><img alt=삭제 width=10 height=10></a>",
    "link_js_trash": f"<a id=t href=\"javascript:void(0)\" {_A}><i class=\"fa fa-trash\"></i></a>",
    "link_hash_trash": f"<a id=t href=\"#\" {_A}><i class=\"fa fa-trash\"></i></a>",
    # 링크 **안의** 별도 컨트롤(role=button span)은 링크 완화 대상이 아니다 — 대상 자신이 a[href] 일 때만.
    "button_span_in_link_trash": f"<a href=\"/x\" {_A}><span id=t role=button {_PRESS} {_S}><i class=\"fa fa-trash\"></i></span></a>",
    "role_link_div_trash": f"<div id=t role=link tabindex=0 {_PRESS} {_S}><i class=\"fa fa-trash\"></i></div>",
    "button_trash_icon": f"<button id=t {_PRESS} {_S}><i class=\"fa fa-trash\"></i></button>",
    "button_admin_name": f"<button id=t {_PRESS} {_S}>admin</button>",
    "form_admin_grant": f"<form action=\"/admin/grant\" method=post {_FORM}><button id=t {_S}>다음</button></form>",
    "get_form_checkout": f"<form action=\"/checkout\" {_FORM}><button id=t {_S}>다음</button></form>",
    "get_form_search_pay_name": f"<form action=\"/search?type=order\" {_FORM}><button id=t {_S}>결제</button></form>",
    "post_form_query_order": f"<form action=\"/search?type=order\" method=post {_FORM}><button id=t {_S}>검색</button></form>",
    "icon_btn_title_pay": f"<button id=t title=\"결제\" {_PRESS} {_S}>&#x1F4B3;</button>",
}

#: 정상(검증 §3 과차단 8종 + e04) — 모든 경로에서 통과(효과 있음)해야 한다.
BENIGN: Dict[str, str] = {
    "x06_trash_view_link": f"<a id=t href=\"/trash\" {_A}><i class=\"fa fa-trash\"></i> 휴지통 보기</a>",
    "x07_search_form_query": f"<form action=\"/search?type=order\" {_FORM}><input name=q><button id=t {_S}>검색</button></form>",
    "x11_admin_link": f"<a id=t href=\"/admin\" {_A}>대시보드</a>",
    "x13_save_title_mentions_pay": f"<button id=t title=\"결제는 나중에 할 수 있습니다\" {_PRESS} {_S}>저장</button>",
    "x20_cart_icon_link": f"<a id=t href=\"/cart\" aria-label=\"장바구니\" {_A}><i class=\"fa fa-shopping-cart\"></i></a>",
    "x22_grant_scholarship": f"<a id=t href=\"/scholarships/grant-2025\" {_A}>장학금 안내</a>",
    "x25_testid_order_history": f"<a id=t data-testid=\"nav-order-history\" href=\"/history\" {_A}>이력</a>",
    "e04_cart_icon_button": f"<button id=t aria-label=\"장바구니 보기\" {_PRESS} {_S}><i class=\"fa fa-shopping-cart\"></i></button>",
    "emoji_zwj_name": f"<button id=t {_PRESS} {_S}>\U0001F468\u200D\U0001F469\u200D\U0001F467 가족 보기</button>",
    "pua_icon_font_pseudo": f"<style>.ic::before{{content:'\\e900'}}</style><button id=t {_PRESS} {_S}>{_PAD}<i class=ic></i>메뉴</button>",
}

PATHS = ("element_id", "selector", "coordinates")


async def _press(server: BrowserMCPServer, url: str, path: str):
    await _call(server, ActionType.NAVIGATE, {"url": url})
    page = server._dispatcher.ctx.page  # noqa: SLF001
    obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    observation = obs.data["observation"]
    epoch = observation["snapshot_epoch"]
    if path == "element_id":
        eid = None
        for e in observation["elements"]:
            handle = server._engine.get_handle(e["element_id"])  # noqa: SLF001
            if handle is not None and (handle.css_path or "").endswith("#t"):
                eid = e["element_id"]
        assert eid, [(e["name"], e["role"]) for e in observation["elements"]]
        r = await _call(server, ActionType.CLICK, {"element_id": eid, "epoch": epoch})
    elif path == "selector":
        r = await _call(server, ActionType.CLICK, {"selector": "#t"})
    else:
        box = await page.locator("#t").bounding_box()
        x, y = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        r = await _call(server, ActionType.CLICK, {"x": x, "y": y, "epoch": epoch})
    await page.wait_for_timeout(50)
    pressed = (await page.text_content("#out")) == "눌림"
    return r, pressed


@requires_chromium
async def test_risky_blocked_on_every_path(site):
    failures = []
    async with BrowserMCPServer() as server:
        for kind, markup in RISKY.items():
            PAGES[f"/r_{kind}"] = _HEAD + markup
            for path in PATHS:
                r, pressed = await _press(server, f"{site}/r_{kind}", path)
                if r.error_code is not ErrorCode.HITL_UNATTENDED_BLOCKED or pressed:
                    failures.append((kind, path, r.error_code, pressed, (r.data or {}).get("gate_basis")))
    assert not failures, failures


@requires_chromium
async def test_benign_passes_on_every_path(site):
    failures = []
    async with BrowserMCPServer() as server:
        for kind, markup in BENIGN.items():
            PAGES[f"/b_{kind}"] = _HEAD + markup
            for path in PATHS:
                r, pressed = await _press(server, f"{site}/b_{kind}", path)
                if not (r.success and pressed):
                    failures.append((kind, path, r.error_code, (r.data or {}).get("gate_basis")))
    assert not failures, failures


@requires_chromium
async def test_aria_long_tail_reports_aria_source(site):
    """이름은 200자에서 잘려 '결제'가 없다 — 차단 근거가 aria 원천이어야 한다(M6 를 죽이는 고정점)."""
    PAGES["/aria"] = _HEAD + RISKY["aria_long_tail"]
    async with BrowserMCPServer() as server:
        r, pressed = await _press(server, site + "/aria", "selector")
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED and not pressed
    basis = r.data["gate_basis"]
    assert "결제" not in basis["name"]
    assert basis["source"] == "aria" and basis["matched_keyword"] == "결제"


@requires_chromium
async def test_signal_payload_stays_small_for_huge_descendants(site):
    """자손 5,000개(클래스 다 다름)여도 신호는 원천별 하나로 모이고 상한을 지킨다."""
    PAGES["/big"] = _HEAD + (
        f"<button id=t {_PRESS} style=\"width:120px;height:40px;overflow:hidden\">보기"
        + "".join(f"<i class=ic{i}></i>" for i in range(5000)) + "</button>"
    )
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/big"})
        target = await server._dispatcher.describe_selector_target("#t")  # noqa: SLF001
    signals = target["info"]["signals"]
    sources = [s for s, _ in signals]
    assert sources.count("class") == 1
    assert len(dict(signals)["class"].split(" \u00a6 ")) == 400
