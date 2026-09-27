"""MCP 탭 정리 (WS-26b): 탭 생성 명령·팝업 등록·탭별 문서 상태·프레임/탭 전환 판정 기준.

검증 보고(/tmp/ws26-verify.md) NB-1·3·4·5·6 을 고정한다. 로컬 HTTP 서버만 쓴다.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionResult, ActionType
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401

NORMAL = """<!doctype html><meta charset=utf-8><title>쇼핑</title>
<p>상품 목록 사과 3000원</p>"""
BLOCK = "<!doctype html><meta charset=utf-8><title>막힘</title><p>요청이 거부되었습니다.</p>"
NAVER = """<!doctype html><meta charset=utf-8><title>네이버</title>
<h2>보안 확인을 완료해 주세요</h2><p>아래 문제에 답해 주세요.</p>"""
NAVER_FRAME = NAVER + "<iframe id=inner src='/' width=300 height=100></iframe>"
PLAIN_FRAME = NORMAL + "<iframe id=inner src='/' width=300 height=100></iframe>"
POPUP = NORMAL + """<a id=pop href='/blocked' target=_blank>새 창</a>
<button id=win onclick="window.open('/blocked')">창 열기</button>
<a id=okpop href='/' target=_blank>정상 새 창</a>"""


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status, body = 200, NORMAL
            if self.path.startswith("/blocked"):
                status, body = 403, BLOCK
            elif self.path.startswith("/naverframe"):
                body = NAVER_FRAME
            elif self.path.startswith("/plainframe"):
                body = PLAIN_FRAME
            elif self.path.startswith("/popup"):
                body = POPUP
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(server: BrowserMCPServer, action: ActionType, args: Dict[str, Any]) -> ActionResult:
    return await server.call_tool(tool_name(action), args)


async def _nav(server: BrowserMCPServer, url: str) -> ActionResult:
    return await _call(server, ActionType.NAVIGATE, {"url": url})


async def _tabs(server: BrowserMCPServer) -> ActionResult:
    return await _call(server, ActionType.TAB_CONTROL, {"command": "list"})


async def _click_name(server: BrowserMCPServer, name: str) -> ActionResult:
    obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    observation = obs.data["observation"]
    el = next(e for e in observation["elements"] if e["name"] == name)
    r = await _call(
        server, ActionType.CLICK,
        {"element_id": el["element_id"], "epoch": observation["snapshot_epoch"]},
    )
    # 팝업 첫 문서 응답·등록 이벤트가 처리되도록 잠시 둔다.
    await server._page.wait_for_timeout(200)
    return r


# ------------------------------------------------------------------ 1. create


@requires_chromium
async def test_mcp_tab_create_opens_and_activates_new_tab(site):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/")
        r = await _call(server, ActionType.TAB_CONTROL, {"command": "create", "url": site + "/blocked"})
        assert r.success is True, r.error_message
        new_id = r.data["tab_id"]
        listed = await _tabs(server)
    assert new_id in [t["tab_id"] for t in listed.data["tabs"]]
    assert listed.data["count"] == 2
    assert listed.data["active"] == new_id
    assert r.tab_id == new_id
    # 새 탭 기준 판정: 403 짧은 본문 → blocked
    assert r.data["challenge"]["kind"] == "blocked"
    assert r.data["last_http_status"] == 403


# ------------------------------------------------------------------ 2. 팝업 등록


@requires_chromium
@pytest.mark.parametrize("name", ["새 창", "창 열기"])  # target=_blank, window.open
async def test_popup_is_listed_active_unchanged_and_switchable(site, name):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/popup")
        before = await _tabs(server)
        origin_id = before.data["active"]
        click = await _click_name(server, name)
        listed = await _tabs(server)
        ids = [t["tab_id"] for t in listed.data["tabs"]]
        assert listed.data["count"] == 2, listed.data
        assert listed.data["active"] == origin_id  # 팝업이 활성 탭을 바꾸지 않는다
        popup_id = next(i for i in ids if i != origin_id)
        assert click.data["opened_tab_ids"] == [popup_id]
        assert "signals" in click.data and "element_id" in click.data  # 기존 키 유지

        sw = await _call(server, ActionType.TAB_CONTROL, {"command": "switch", "tab_id": popup_id})
        assert sw.success is True, sw.error_message
        assert sw.tab_id == popup_id
        # 팝업 탭(403 짧은 본문)으로 옮긴 뒤 판정 → blocked (양성 유지)
        assert sw.data["challenge"]["kind"] == "blocked"
        assert sw.data["last_http_status"] == 403
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        assert obs.data["challenge"]["kind"] == "blocked"


@requires_chromium
async def test_closed_popup_leaves_tab_list(site):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/popup")
        await _click_name(server, "새 창")
        listed = await _tabs(server)
        popup = next(t for t in server._core.tabs() if t.tab_id != listed.data["active"])
        await popup.page.close()
        await server._page.wait_for_timeout(50)
        after = await _tabs(server)
    assert after.data["count"] == 1
    assert popup.tab_id not in [t["tab_id"] for t in after.data["tabs"]]


@requires_chromium
async def test_tab_close_command_via_mcp(site):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/")
        created = await _call(server, ActionType.TAB_CONTROL, {"command": "create", "url": site + "/"})
        new_id = created.data["tab_id"]
        r = await _call(server, ActionType.TAB_CONTROL, {"command": "close", "tab_id": new_id})
        assert r.success is True, r.error_message
        listed = await _tabs(server)
    assert listed.data["count"] == 1
    assert new_id not in [t["tab_id"] for t in listed.data["tabs"]]


# ------------------------------------------------------------------ 3. 탭별 문서 상태 (검증 §4 재현 2건)


@requires_chromium
async def test_popup_403_does_not_mark_original_tab_blocked(site):
    """(i) 팝업이 403 을 받아도 원래 탭(짧은 정상 본문)은 blocked 가 아니다."""
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/popup")
        await _click_name(server, "새 창")
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    assert obs.data["challenge"] is None
    assert obs.data["last_http_status"] == 200  # 원래 탭의 값


@requires_chromium
async def test_spa_url_change_after_403_is_not_blocked(site):
    """(ii) 403 문서 뒤 pushState 로 URL 을 바꾸고 짧은 정상 본문 → blocked 아님."""
    async with BrowserMCPServer() as server:
        first = await _nav(server, site + "/blocked")
        assert first.data["challenge"]["kind"] == "blocked"
        await server._page.evaluate(
            "history.pushState({}, '', '/app/home');"
            "document.title='홈'; document.body.innerHTML='<p>환영합니다</p>';"
        )
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    assert obs.data["challenge"] is None
    # 이 탭의 마지막 문서 응답은 여전히 403 이다(판정에만 쓰지 않는다).
    assert obs.data["last_http_status"] == 403


@requires_chromium
async def test_same_tab_403_short_body_still_blocked_on_observe(site):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/")
        await _nav(server, site + "/blocked")
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    assert obs.data["challenge"]["kind"] == "blocked"
    assert obs.data["last_http_status"] == 403


# ------------------------------------------------------------------ 4. 프레임·탭 전환 판정 기준


@requires_chromium
async def test_top_level_captcha_detected_inside_normal_iframe(site):
    """최상위=캡차, 정상 iframe 으로 들어간 뒤 observe → captcha (root_page 우선)."""
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/naverframe")
        sw = await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
        assert sw.success is True, sw.error_message
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    assert obs.data["challenge"]["kind"] == "captcha"
    assert obs.data["challenge"]["vendor"] == "naver"


@requires_chromium
async def test_tab_switch_from_inside_frame_judges_new_tab(site):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/")
        created = await _call(server, ActionType.TAB_CONTROL, {"command": "create", "url": site + "/"})
        other = created.data["tab_id"]
        first = server._core.tabs()[0].tab_id
        await _call(server, ActionType.TAB_CONTROL, {"command": "switch", "tab_id": first})
        await _nav(server, site + "/naverframe")
        await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
        assert server._dispatcher.ctx.root_page is not None

        sw = await _call(server, ActionType.TAB_CONTROL, {"command": "switch", "tab_id": other})
        assert server._dispatcher.ctx.root_page is None
        assert sw.data["challenge"] is None  # 옛 탭(캡차)이 아니라 새 탭 기준
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        assert obs.data["challenge"] is None


@requires_chromium
async def test_tab_create_from_inside_frame_judges_new_tab(site):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/naverframe")
        await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
        created = await _call(server, ActionType.TAB_CONTROL, {"command": "create", "url": site + "/"})
        assert server._dispatcher.ctx.root_page is None
    assert created.data["challenge"] is None


@requires_chromium
async def test_tab_close_from_inside_frame_resets_root_page(site):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/")
        created = await _call(server, ActionType.TAB_CONTROL, {"command": "create", "url": site + "/naverframe"})
        await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
        r = await _call(server, ActionType.TAB_CONTROL, {"command": "close", "tab_id": created.data["tab_id"]})
        assert r.success is True, r.error_message
        assert server._dispatcher.ctx.root_page is None
    assert r.data["challenge"] is None
