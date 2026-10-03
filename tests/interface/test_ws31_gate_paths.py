"""WS-31: 좌표 클릭 대상 해석 + 이름 밖 문맥 신호가 모든 경로에서 같은 판정을 내는지.

실제 BrowserMCPServer(헤드리스 Chromium) + 로컬 HTTP 서버만 쓴다(실사이트 없음). 무인 기본.

* 공백(2) 재현 마크업(아이콘 클래스·::before·img alt·title·zero-width·aria≠보임·폼 action·
  formaction·class 토큰·svg title)을 element_id·selector·좌표·포커스 Space/Enter 다섯 경로로
  눌러 모두 차단(효과 없음)인지, 정상 마크업은 모두 통과(효과 있음)인지.
* 좌표 클릭: 같은 출처 iframe·open shadow 를 따라 내려가 해석, 다른 출처 iframe·closed shadow·
  해석 예외는 fail-closed, 캔버스(상호작용 조상 없음·폼 밖·신호 없음)는 저위험 통과 + gate_basis.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple

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
    srv = _serve("127.0.0.1")
    try:
        yield f"http://localhost:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(server: BrowserMCPServer, action: ActionType, args: Dict[str, Any]):
    return await server.call_tool(tool_name(action), args)


def _page(server: BrowserMCPServer):
    return server._dispatcher.ctx.page  # noqa: SLF001


_PRESS = "onclick=\"document.getElementById('out').textContent='눌림'\""
_HEAD = "<!doctype html><meta charset=utf-8><title>g</title><body style=margin:0><p id=out>대기</p>"
_FORM_SUBMIT = "onsubmit=\"document.getElementById('out').textContent='눌림';return false\""

#: 공백(2) 재현 — 이름에 위험 단어가 없거나 이름이 무해한 위험 버튼.
GAP_KINDS: Dict[str, str] = {
    "icon_class": f"<button id=t {_PRESS}><i class=\"fa fa-trash\" style=\"display:inline-block;width:20px;height:20px\"></i></button>",
    "pseudo_before": f"<style>#t::before{{content:'결제'}}</style><button id=t {_PRESS}></button>",
    "img_alt": f"<button id=t {_PRESS}><img alt=\"결제\" width=20 height=20></button>",
    "title_only": f"<button id=t title=\"결제\" {_PRESS}>&#x1F4B3;</button>",
    "zero_width": f"<button id=t {_PRESS}>결&#8203;제</button>",
    "aria_vs_text": f"<button id=t aria-label=\"확인\" {_PRESS}>결제</button>",
    "form_action": f"<form action=\"/checkout\" {_FORM_SUBMIT}><button id=t>다음</button></form>",
    "formaction": f"<form action=\"/search\" {_FORM_SUBMIT}><button id=t formaction=\"/pay\">다음</button></form>",
    "class_token": f"<button id=t class=\"btn-pay\" {_PRESS}>다음</button>",
    "svg_title": f"<button id=t {_PRESS}><svg width=20 height=20><title>삭제</title><rect width=20 height=20></rect></svg></button>",
}

#: 정상 — 다섯 경로 모두 통과해야 한다(과차단 없음).
BENIGN_KINDS: Dict[str, str] = {
    "plain": f"<button id=t {_PRESS}>보기</button>",
    "icon_search": f"<button id=t {_PRESS}><i class=\"fa fa-search\" style=\"display:inline-block;width:20px;height:20px\"></i></button>",
    "title_help": f"<button id=t title=\"도움말\" {_PRESS}>?</button>",
    "btn_primary": f"<button id=t class=\"btn btn-primary\" {_PRESS}>다음</button>",
}

PATHS = ("element_id", "selector", "coordinates", "focus_space", "focus_enter")


async def _observe(server: BrowserMCPServer) -> Tuple[str, int]:
    """#t 의 element_id 와 현재 epoch (관찰 이름이 비어도 css_path 로 찾는다)."""
    obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    observation = obs.data["observation"]
    engine = server._engine  # noqa: SLF001
    for e in observation["elements"]:
        handle = engine.get_handle(e["element_id"])
        if handle is not None and (handle.css_path or "").endswith("#t"):
            return e["element_id"], observation["snapshot_epoch"]
    raise AssertionError(f"#t 없음: {[(e['name'], e['role']) for e in observation['elements']]}")


async def _press(server: BrowserMCPServer, url: str, path: str):
    await _call(server, ActionType.NAVIGATE, {"url": url})
    page = _page(server)
    if path == "element_id":
        eid, epoch = await _observe(server)
        r = await _call(server, ActionType.CLICK, {"element_id": eid, "epoch": epoch})
    elif path == "selector":
        r = await _call(server, ActionType.CLICK, {"selector": "#t"})
    elif path == "coordinates":
        _, epoch = await _observe(server)
        box = await page.locator("#t").bounding_box()
        x, y = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        r = await _call(server, ActionType.CLICK, {"x": x, "y": y, "epoch": epoch})
    else:
        await page.focus("#t")
        r = await _call(server, ActionType.PRESS_KEY,
                        {"key": "Space" if path == "focus_space" else "Enter"})
    await page.wait_for_timeout(50)
    pressed = (await page.text_content("#out")) == "눌림"
    return r, pressed


@requires_chromium
@pytest.mark.parametrize("kind", sorted(GAP_KINDS))
async def test_gap_markup_blocked_on_every_path(site, kind):
    PAGES["/g"] = _HEAD + GAP_KINDS[kind]
    verdicts = {}
    async with BrowserMCPServer() as server:
        for path in PATHS:
            r, pressed = await _press(server, site + "/g", path)
            verdicts[path] = (r.error_code, pressed, (r.data or {}).get("gate_basis"))
    for path, (code, pressed, basis) in verdicts.items():
        assert code is ErrorCode.HITL_UNATTENDED_BLOCKED, (kind, path, verdicts)
        assert not pressed, f"차단됐는데 눌림: {kind} {path}"
        assert isinstance(basis, dict) and basis.get("source"), (kind, path, basis)


@requires_chromium
@pytest.mark.parametrize("kind", sorted(BENIGN_KINDS))
async def test_benign_markup_passes_on_every_path(site, kind):
    PAGES["/b"] = _HEAD + BENIGN_KINDS[kind]
    verdicts = {}
    async with BrowserMCPServer() as server:
        for path in PATHS:
            r, pressed = await _press(server, site + "/b", path)
            verdicts[path] = (r.success, r.error_code, r.error_message, pressed)
    for path, (ok, code, msg, pressed) in verdicts.items():
        assert ok and pressed, (kind, path, code, msg)


@requires_chromium
async def test_blocked_message_names_source_and_basis(site):
    PAGES["/g"] = _HEAD + GAP_KINDS["form_action"]
    async with BrowserMCPServer() as server:
        r, _ = await _press(server, site + "/g", "selector")
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    basis = r.data["gate_basis"]
    assert basis["source"] == "form_action" and basis["matched_keyword"] == "checkout"
    assert basis["name"] == "다음"
    assert "form_action" in r.error_message


# ------------------------------------------------------------------ 좌표 해석

async def _coord_click(server: BrowserMCPServer, url: str, x: int, y: int):
    await _call(server, ActionType.NAVIGATE, {"url": url})
    obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    epoch = obs.data["observation"]["snapshot_epoch"]
    return await _call(server, ActionType.CLICK, {"x": x, "y": y, "epoch": epoch})


_FRAME_STYLE = "style=\"position:absolute;left:0;top:100px;width:300px;height:100px;border:0\""


@requires_chromium
@pytest.mark.parametrize("label,blocked", [("결제", True), ("보기", False)])
async def test_coordinates_descend_same_origin_iframe(site, label, blocked):
    PAGES["/inner"] = (
        "<!doctype html><meta charset=utf-8><body style=margin:0>"
        f"<button id=ib style=\"width:200px;height:60px\" onclick=\"parent.document."
        f"getElementById('out').textContent='눌림'\">{label}</button>"
    )
    PAGES["/top"] = _HEAD + f"<iframe src=\"/inner\" {_FRAME_STYLE}></iframe>"
    async with BrowserMCPServer() as server:
        r = await _coord_click(server, site + "/top", 50, 120)
        await _page(server).wait_for_timeout(50)
        pressed = (await _page(server).text_content("#out")) == "눌림"
    if blocked:
        assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
        assert not pressed
        assert r.data["pre_approve_hint"] == "click:결제"
    else:
        assert r.success and pressed, (r.error_code, r.error_message)


@requires_chromium
async def test_coordinates_cross_origin_iframe_fails_closed(site, other_origin):
    PAGES["/inner"] = (
        "<!doctype html><meta charset=utf-8><body style=margin:0>"
        "<button style=\"width:200px;height:60px\">보기</button>"
    )
    PAGES["/top"] = _HEAD + f"<iframe src=\"{other_origin}/inner\" {_FRAME_STYLE}></iframe>"
    async with BrowserMCPServer() as server:
        r = await _coord_click(server, site + "/top", 50, 120)
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert r.data["gate_basis"]["source"] == "unresolved"
    assert "다른 출처" in r.error_message


@requires_chromium
@pytest.mark.parametrize("label,blocked", [("삭제", True), ("보기", False)])
async def test_coordinates_descend_open_shadow(site, label, blocked):
    PAGES["/s"] = _HEAD + (
        "<div id=h style=\"width:200px;height:60px\"></div><script>"
        "const r=document.getElementById('h').attachShadow({mode:'open'});"
        f"r.innerHTML='<button style=\"width:200px;height:60px\">{label}</button>';"
        "r.querySelector('button').onclick=()=>{document.getElementById('out').textContent='눌림'};"
        "</script>"
    )
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/s"})
        box = await _page(server).locator("#h").bounding_box()
        r = await _coord_click(server, site + "/s", int(box["x"] + 50), int(box["y"] + 20))
        await _page(server).wait_for_timeout(50)
        pressed = (await _page(server).text_content("#out")) == "눌림"
    if blocked:
        assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
        assert not pressed
    else:
        assert r.success and pressed, (r.error_code, r.error_message)


@requires_chromium
async def test_coordinates_closed_shadow_fails_closed(site):
    PAGES["/s"] = _HEAD + (
        "<div id=h style=\"width:200px;height:60px\"></div><script>"
        "document.getElementById('h').attachShadow({mode:'closed'}).innerHTML="
        "'<button style=\"width:200px;height:60px\">보기</button>';</script>"
    )
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/s"})
        box = await _page(server).locator("#h").bounding_box()
        r = await _coord_click(server, site + "/s", int(box["x"] + 50), int(box["y"] + 20))
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert r.data["gate_basis"]["source"] == "unresolved"
    assert "closed shadow" in r.error_message


_CANVAS = (
    "<canvas id=cv width=300 height=100 style=\"display:block\"></canvas><script>"
    "document.getElementById('cv').addEventListener('click',()=>{"
    "document.getElementById('out').textContent='눌림'});</script>"
)


@requires_chromium
async def test_coordinates_canvas_passes_with_basis(site):
    PAGES["/c"] = _HEAD + _CANVAS
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/c"})
        box = await _page(server).locator("#cv").bounding_box()
        r = await _coord_click(server, site + "/c", int(box["x"] + 50), int(box["y"] + 50))
        pressed = (await _page(server).text_content("#out")) == "눌림"
    assert r.success and pressed, (r.error_code, r.error_message)
    basis = r.data["gate_basis"]
    assert basis["coordinate_target"] == "non_interactive"
    assert basis["tag"] == "CANVAS"
    assert basis["matched_keyword"] is None


@requires_chromium
async def test_coordinates_canvas_inside_form_fails_closed(site):
    PAGES["/c"] = _HEAD + "<form action=\"/search\">" + _CANVAS + "</form>"
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/c"})
        box = await _page(server).locator("#cv").bounding_box()
        r = await _coord_click(server, site + "/c", int(box["x"] + 50), int(box["y"] + 50))
        pressed = (await _page(server).text_content("#out")) == "눌림"
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert not pressed


@requires_chromium
async def test_coordinates_canvas_with_risky_id_blocked(site):
    PAGES["/c"] = _HEAD + _CANVAS.replace("id=cv ", "id=cv data-testid=\"checkout-canvas\" ")
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/c"})
        box = await _page(server).locator("#cv").bounding_box()
        r = await _coord_click(server, site + "/c", int(box["x"] + 50), int(box["y"] + 50))
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert r.data["gate_basis"]["source"] == "testid"


@requires_chromium
async def test_coordinates_on_child_resolve_to_interactive_ancestor(site):
    """버튼 안 span 을 눌러도 버튼(상호작용 조상)의 이름으로 판정한다."""
    PAGES["/a"] = _HEAD + (
        f"<button id=t {_PRESS} style=\"padding:20px\"><span id=sp>결제 진행</span></button>"
    )
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/a"})
        box = await _page(server).locator("#sp").bounding_box()
        r = await _coord_click(server, site + "/a", int(box["x"] + 3), int(box["y"] + 3))
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert r.data["pre_approve_hint"] == "click:결제 진행"
    assert r.data["gate_basis"]["coordinate_target"] == "interactive"


@requires_chromium
async def test_coordinates_pre_approved_by_resolved_name(site):
    PAGES["/a"] = _HEAD + f"<button id=t {_PRESS} style=\"width:120px;height:40px\">결제</button>"
    async with BrowserMCPServer(pre_approved_actions=("click:결제",)) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/a"})
        box = await _page(server).locator("#t").bounding_box()
        r = await _coord_click(server, site + "/a", int(box["x"] + 10), int(box["y"] + 10))
        pressed = (await _page(server).text_content("#out")) == "눌림"
    assert r.success and pressed, (r.error_code, r.error_message)


@requires_chromium
async def test_coordinates_resolution_exception_fails_closed(site, monkeypatch):
    PAGES["/a"] = _HEAD + f"<button id=t {_PRESS} style=\"width:120px;height:40px\">보기</button>"
    async with BrowserMCPServer() as server:

        async def boom(*_a, **_k):
            raise RuntimeError("cdp gone")

        monkeypatch.setattr(server._dispatcher, "_gate_cdp_for", boom)  # noqa: SLF001
        r = await _coord_click(server, site + "/a", 10, 50)
        pressed = (await _page(server).text_content("#out")) == "눌림"
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, r.error_message
    assert not pressed
    assert r.data["gate_basis"]["source"] == "unresolved"


@requires_chromium
async def test_coordinates_stale_epoch_or_offscreen_keeps_dispatcher_error(site):
    """디스패처가 어차피 거부하는 좌표(epoch 불일치·뷰포트 밖)는 그 오류를 그대로 받는다."""
    PAGES["/a"] = _HEAD + "<button id=t>결제</button>"
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/a"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        epoch = obs.data["observation"]["snapshot_epoch"]
        stale = await _call(server, ActionType.CLICK, {"x": 5, "y": 30, "epoch": epoch + 7})
        off = await _call(server, ActionType.CLICK, {"x": 99999, "y": 99999, "epoch": epoch})
    assert stale.error_code is ErrorCode.TOCTOU_MISMATCH
    assert off.error_code is ErrorCode.ELEMENT_NOT_FOUND
