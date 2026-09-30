"""WS-30 항목 2·6 + 추가 불일치(A·B): MCP→디스패처 경계와 HITL 게이트 일관성.

실제 BrowserMCPServer(헤드리스 Chromium) + 로컬 MockServer/HTTP 서버만 쓴다.

* 2  download_file: 계약 키 `trigger_element_id` 가 디스패처까지 전달돼 실제 파일이 저장된다.
* A  click(selector): 계약이 허용하는 selector 클릭이 실제로 동작하고, 게이트는 selector 문자열이
     아니라 **페이지에서 읽은 대상 이름**으로 판정한다(우회 구멍 없음). 0개/여러 개면 fail-closed.
* B  switch_frame(to_main): 계약에 없는 to_main 을 경계에서 특례로 받아 메인 문서로 복귀한다.
* 6  press_key(Enter): 폼 안 입력칸에 포커스가 있으면 type_text(press_enter=True) 와 같은
     '폼 제출' 게이트를 탄다. 사전 승인 `press_key:*` 로 풀린다.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401

PAGES: Dict[str, str] = {
    "/form": """<!doctype html><meta charset=utf-8><title>검색</title>
<form action="/results" method="get"><input id=q name=q aria-label="검색어"></form>
<input id=loose aria-label="메모">
<button id=nothing>아무것도</button>""",
    "/results": """<!doctype html><meta charset=utf-8><title>결과</title><h1>검색 결과</h1>""",
    "/pay": """<!doctype html><meta charset=utf-8><title>결제</title><p id=out>대기</p>
<button id=pay class=btn onclick="document.getElementById('out').textContent='결제됨'">결제하기</button>
<button id=view class=btn onclick="document.getElementById('out').textContent='보기'">상세 보기</button>""",
}


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = PAGES.get(self.path.split("?", 1)[0], "<p>404</p>")
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
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def mock_server():
    from harness import MockServer

    with MockServer() as srv:
        yield srv


async def _call(server: BrowserMCPServer, action: ActionType, args: Dict[str, Any]):
    return await server.call_tool(tool_name(action), args)


async def _element(server: BrowserMCPServer, name: str):
    obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    observation = obs.data["observation"]
    for e in observation["elements"]:
        if e["name"] == name:
            return e["element_id"], observation["snapshot_epoch"]
    raise AssertionError(f"요소 없음: {name} in {[e['name'] for e in observation['elements']]}")


# ------------------------------------------------------------------ 2. download_file


@requires_chromium
async def test_download_file_saves_csv_with_contract_key(mock_server, tmp_path):
    async with BrowserMCPServer(pre_approved_actions=("download_file:CSV 내려받기",)) as server:
        await _call(server, ActionType.NAVIGATE, {"url": mock_server.site_url("s04_download")})
        eid, epoch = await _element(server, "CSV 내려받기")
        r = await _call(
            server,
            ActionType.DOWNLOAD_FILE,
            {"trigger_element_id": eid, "epoch": epoch, "save_dir": str(tmp_path)},
        )
    assert r.success, (r.error_code, r.error_message)
    saved = Path(r.downloaded_path)
    assert saved.parent == tmp_path
    assert saved.read_text(encoding="utf-8").startswith("id,name,amount")


@requires_chromium
async def test_download_file_still_gated(mock_server, tmp_path):
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": mock_server.site_url("s04_download")})
        eid, epoch = await _element(server, "CSV 내려받기")
        r = await _call(
            server,
            ActionType.DOWNLOAD_FILE,
            {"trigger_element_id": eid, "epoch": epoch, "save_dir": str(tmp_path)},
        )
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert r.data["pre_approve_hint"] == "download_file:CSV 내려받기"
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ A. click(selector)


@requires_chromium
async def test_click_selector_low_risk_works(site):
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/pay"})
        r = await _call(server, ActionType.CLICK, {"selector": "#view"})
        out = await server._dispatcher.ctx.page.text_content("#out")  # noqa: SLF001
    assert r.success, (r.error_code, r.error_message)
    assert out == "보기"


@requires_chromium
async def test_click_selector_same_verdict_as_element_id(site):
    """'결제하기' 버튼을 id 셀렉터로 눌러도 element_id 로 누를 때와 같이 차단된다(우회 없음)."""
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/pay"})
        eid, epoch = await _element(server, "결제하기")
        by_id = await _call(server, ActionType.CLICK, {"element_id": eid, "epoch": epoch})
        by_sel = await _call(server, ActionType.CLICK, {"selector": "#pay"})
        out = await server._dispatcher.ctx.page.text_content("#out")  # noqa: SLF001
    assert by_id.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert by_sel.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert by_sel.data["pre_approve_hint"] == by_id.data["pre_approve_hint"] == "click:결제하기"
    assert out == "대기", "차단됐는데 클릭이 실행됨"


@requires_chromium
async def test_click_selector_ambiguous_or_missing_fails_closed(site):
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/pay"})
        many = await _call(server, ActionType.CLICK, {"selector": ".btn"})
        none = await _call(server, ActionType.CLICK, {"selector": "#nope"})
        out = await server._dispatcher.ctx.page.text_content("#out")  # noqa: SLF001
    assert many.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert none.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert out == "대기"


@requires_chromium
async def test_click_selector_pre_approved_by_read_name(site):
    async with BrowserMCPServer(pre_approved_actions=("click:결제하기",)) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/pay"})
        r = await _call(server, ActionType.CLICK, {"selector": "#pay"})
        amb = await _call(server, ActionType.CLICK, {"selector": ".btn"})
        out = await server._dispatcher.ctx.page.text_content("#out")  # noqa: SLF001
    assert r.success, (r.error_code, r.error_message)
    assert out == "결제됨"
    # 이름 승인은 모호한 selector 를 열지 않는다.
    assert amb.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED


@requires_chromium
async def test_click_selector_ambiguous_with_wildcard_is_not_found(site):
    """click:* 로 게이트를 열어도 모호한 selector 는 실행하지 않는다(디스패처 단)."""
    async with BrowserMCPServer(pre_approved_actions=("click:*",)) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/pay"})
        many = await _call(server, ActionType.CLICK, {"selector": ".btn"})
        out = await server._dispatcher.ctx.page.text_content("#out")  # noqa: SLF001
    assert many.error_code is ErrorCode.ELEMENT_NOT_FOUND
    assert "2" in (many.error_message or "")
    assert out == "대기"


# ------------------------------------------------------------------ B. switch_frame(to_main)


@requires_chromium
async def test_to_main_from_nested_frame_via_mcp(mock_server):
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": mock_server.site_url("s05_iframe")})
        a = await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#outer"})
        b = await _call(server, ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
        back = await _call(server, ActionType.SWITCH_FRAME, {"to_main": True})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        both = await _call(
            server, ActionType.SWITCH_FRAME, {"to_main": True, "frame_selector": "#outer"}
        )
    assert a.success and b.success
    assert back.success, (back.error_code, back.error_message)
    assert back.data["frame_depth"] == 0
    assert obs.data["observation"]["url"] == mock_server.site_url("s05_iframe")
    assert obs.data["observation"]["title"] == "중첩 프레임"
    assert not both.success
    assert "to_main" in (both.error_message or "")


def test_switch_frame_description_mentions_to_main_and_order():
    from interface.mcp_server import build_tool_schema

    desc = build_tool_schema(ActionType.SWITCH_FRAME)["description"]
    assert "to_main" in desc
    assert "현재 프레임" in desc and "최상위" in desc


# ------------------------------------------------------------------ 6. press_key(Enter) 게이트


@requires_chromium
async def test_enter_paths_same_verdict(site):
    """type_text(press_enter) 와 type_text 뒤 press_key(Enter) 가 같은 판정(차단)."""
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/form"})
        eid, epoch = await _element(server, "검색어")
        a = await _call(server, ActionType.TYPE_TEXT,
                        {"element_id": eid, "epoch": epoch, "text": "노트북", "press_enter": True})
        typed = await _call(server, ActionType.TYPE_TEXT,
                            {"element_id": eid, "epoch": epoch, "text": "노트북"})
        b = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        c = await _call(server, ActionType.PRESS_KEY, {"key": "NumpadEnter"})
        d = await _call(server, ActionType.PRESS_KEY, {"key": "return"})
        url = server._dispatcher.ctx.page.url  # noqa: SLF001
    assert typed.success
    for r in (a, b, c, d):
        assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.action, r.error_message)
        assert "폼 제출" in (r.error_message or "")
    assert b.data["pre_approve_hint"] == "press_key:*"
    assert url.endswith("/form"), f"차단됐는데 제출됨: {url}"


@requires_chromium
async def test_type_text_newline_is_form_submit(site):
    """text 안의 줄바꿈도 Enter 와 같다 — 입력칸(textarea 아님)이면 폼 제출로 본다."""
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/form"})
        eid, epoch = await _element(server, "검색어")
        r = await _call(server, ActionType.TYPE_TEXT,
                        {"element_id": eid, "epoch": epoch, "text": "노트북\n"})
        url = server._dispatcher.ctx.page.url  # noqa: SLF001
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert url.endswith("/form")


@requires_chromium
async def test_press_key_enter_pre_approved_submits(site):
    async with BrowserMCPServer(pre_approved_actions=("press_key:*",)) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/form"})
        eid, epoch = await _element(server, "검색어")
        await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "노트북"})
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert r.success, (r.error_code, r.error_message)
    assert "/results" in r.current_url


@requires_chromium
async def test_press_key_enter_outside_form_and_other_keys_pass(site):
    """폼 밖 입력칸의 Enter, 폼 안 입력칸의 Tab 은 제출이 아니다(과차단 금지)."""
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/form"})
        eid, epoch = await _element(server, "메모")
        await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "x"})
        enter_loose = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        eid, epoch = await _element(server, "검색어")
        await _call(server, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "y"})
        tab = await _call(server, ActionType.PRESS_KEY, {"key": "Tab"})
    assert enter_loose.success, (enter_loose.error_code, enter_loose.error_message)
    assert tab.success, (tab.error_code, tab.error_message)


@requires_chromium
async def test_press_key_enter_on_focused_high_risk_button_is_gated(site):
    """포커스된 '결제하기' 버튼에서 Enter = 그 버튼 클릭 — 이름 게이트를 탄다."""
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/pay"})
        await server._dispatcher.ctx.page.focus("#pay")  # noqa: SLF001
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        out = await server._dispatcher.ctx.page.text_content("#out")  # noqa: SLF001
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert out == "대기"
