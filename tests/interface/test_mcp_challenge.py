"""MCP 결과에 차단·캡차 신호 싣기 (WS-26).

MCP 클라이언트(부르는 에이전트)가 캡차·차단 화면을 평범한 페이지로 받지 않도록,
대상 액션 결과의 data 에 `challenge`·`last_http_status` 를 싣는다. 판정은 run 경로와
같은 browser.challenge.detect_challenge(classify) 를 쓴다 — 캡차를 풀지 않는다.
로컬 HTTP 서버만 쓴다(실사이트 접속 없음).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionResult, ActionType, ErrorCode, ExecutionMode
from interface import mcp_server
from interface.mcp_server import CHALLENGE_CHECK_ACTIONS, BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401 - Chromium 유무 판정 재사용

NORMAL = """<!doctype html><meta charset=utf-8><title>쇼핑</title>
<input aria-label='검색어' id=q><button id=go>검색</button><p>상품 목록 사과 3000원</p>
<button id=pay-button>결제하기</button>"""
BLOCK = "<!doctype html><meta charset=utf-8><title>막힘</title><p>요청이 거부되었습니다.</p>"
NAVER = """<!doctype html><meta charset=utf-8><title>네이버</title>
<h2>보안 확인을 완료해 주세요</h2><p>아래 문제에 답해 주세요.</p>"""
RECAPTCHA = """<!doctype html><meta charset=utf-8><title>확인</title><p>로봇이 아닙니다</p>
<iframe src='/recaptcha/api2/anchor?k=x' width=304 height=78></iframe>"""
WIDGET = "<!doctype html><meta charset=utf-8><p>위젯</p>"
POPUP = NORMAL + "<a id=pop href='/blocked' target=_blank>새 창</a>"


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status, body = 200, NORMAL
            if self.path.startswith("/blocked"):
                status, body = 403, BLOCK
            elif self.path.startswith("/naver"):
                body = NAVER
            elif self.path.startswith("/captcha"):
                body = RECAPTCHA
            elif self.path.startswith("/recaptcha/"):
                body = WIDGET
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


async def _nav(server: BrowserMCPServer, url: str) -> ActionResult:
    return await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": url})


# ------------------------------------------------------------------ 실제 브라우저


@requires_chromium
async def test_navigate_403_short_body_is_blocked(site):
    async with BrowserMCPServer() as server:
        r = await _nav(server, site + "/blocked")
    assert r.data["last_http_status"] == 403
    assert r.data["challenge"]["kind"] == "blocked"
    assert r.data["challenge"]["vendor"] == "generic"
    assert r.data["challenge"]["reason"]


@requires_chromium
async def test_naver_security_check_phrase_is_captcha(site):
    async with BrowserMCPServer() as server:
        r = await _nav(server, site + "/naver")
    assert r.data["challenge"] == {
        "kind": "captcha", "vendor": "naver", "reason": "네이버 보안 확인 화면",
    }
    assert r.data["last_http_status"] == 200


@requires_chromium
async def test_visible_recaptcha_iframe_is_captcha(site):
    async with BrowserMCPServer() as server:
        r = await _nav(server, site + "/captcha")
    assert r.data["challenge"]["kind"] == "captcha"
    assert r.data["challenge"]["vendor"] == "recaptcha"


@requires_chromium
async def test_normal_page_has_null_challenge_and_200(site):
    async with BrowserMCPServer() as server:
        r = await _nav(server, site + "/")
    assert r.success is True
    assert "challenge" in r.data and r.data["challenge"] is None
    assert r.data["last_http_status"] == 200
    # 기존 디스패처 키는 그대로(병합).
    assert r.data["url"] == site + "/"


@requires_chromium
async def test_observe_keeps_observation_and_adds_signal(site):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/")
        r = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
    assert r.success is True
    assert "observation" in r.data
    assert r.data["challenge"] is None
    assert r.data["last_http_status"] == 200


@requires_chromium
async def test_click_opening_new_tab_updates_last_status(site):
    async with BrowserMCPServer() as server:
        first = await _nav(server, site + "/popup")
        assert first.data["last_http_status"] == 200
        obs = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        observation = obs.data["observation"]
        link = next(e for e in observation["elements"] if e["name"] == "새 창")
        r = await server.call_tool(
            tool_name(ActionType.CLICK),
            {"element_id": link["element_id"], "epoch": observation["snapshot_epoch"]},
        )
    assert r.success is True, r.error_message
    assert r.data["last_http_status"] == 403
    assert "challenge" in r.data


@requires_chromium
async def test_non_target_actions_have_no_challenge_key(site):
    async with BrowserMCPServer() as server:
        await _nav(server, site + "/blocked")
        shot = await server.call_tool(tool_name(ActionType.TAKE_SCREENSHOT), {})
        scroll = await server.call_tool(tool_name(ActionType.SCROLL), {"direction": "down"})
    assert "challenge" not in shot.data and "last_http_status" not in shot.data
    assert "challenge" not in scroll.data and "last_http_status" not in scroll.data


@requires_chromium
async def test_hitl_blocked_result_has_no_challenge_key(site):
    async with BrowserMCPServer(mode=ExecutionMode.UNATTENDED) as server:
        await _nav(server, site + "/blocked")
        r = await server.call_tool(tool_name(ActionType.CLICK), {"selector": "#pay-button"})
    assert r.success is False
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert "challenge" not in r.data and "last_http_status" not in r.data


# ------------------------------------------------------------------ 가짜 디스패처(브라우저 없음)


class _RaisingPage:
    url = "http://127.0.0.1/x"

    async def evaluate(self, *_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("Execution context was destroyed")


class _FakeDispatcher:
    def __init__(self, page: Any, result: ActionResult) -> None:
        self.ctx = type("Ctx", (), {"page": page})()
        self._result = result

    async def dispatch(self, action: ActionType, params: Dict[str, Any]) -> ActionResult:
        return self._result.model_copy(deep=True)


def _fake_server(result: ActionResult, status: Any = 403) -> BrowserMCPServer:
    from security import HITLGate

    server = BrowserMCPServer()
    server._started = True
    server._page = _RaisingPage()
    server._dispatcher = _FakeDispatcher(server._page, result)
    server._hitl = HITLGate(mode=ExecutionMode.UNATTENDED)
    server._http_status = {"last_http_status": status}
    return server


def _res(action: ActionType, success: bool = True, **data: Any) -> ActionResult:
    return ActionResult(
        success=success, action=action, current_url="http://127.0.0.1/x",
        snapshot_epoch=3, tab_id="tab-1", retry_safe=True,
        error_code=None if success else ErrorCode.NAVIGATE_TIMEOUT,
        error_message=None if success else "timeout",
        data=data,
    )


async def test_evaluate_failure_keeps_result_and_null_challenge():
    server = _fake_server(_res(ActionType.NAVIGATE, url="http://127.0.0.1/x"), status=200)
    r = await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": "http://127.0.0.1/x"})
    assert r.success is True
    assert r.snapshot_epoch == 3 and r.tab_id == "tab-1"
    assert r.data["url"] == "http://127.0.0.1/x"
    assert r.data["challenge"] is None
    assert r.data["last_http_status"] == 200


async def test_detector_exception_does_not_break_result(monkeypatch):
    import browser.challenge as challenge_mod

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("detector bug")

    monkeypatch.setattr(challenge_mod, "detect_challenge", _boom)
    server = _fake_server(_res(ActionType.RELOAD, url="http://127.0.0.1/x"))
    r = await server.call_tool(tool_name(ActionType.RELOAD), {})
    assert r.success is True
    assert r.data["url"] == "http://127.0.0.1/x"
    assert r.data["challenge"] is None
    assert r.data["last_http_status"] == 403


async def test_failed_result_also_carries_signal(monkeypatch):
    import browser.challenge as challenge_mod

    seen: Dict[str, Any] = {}

    async def _fake(page: Any, *, last_status: Any = None) -> Any:
        seen["last_status"] = last_status
        return challenge_mod.classify("", "짧은 본문", 5, "", last_status)

    monkeypatch.setattr(challenge_mod, "detect_challenge", _fake)
    server = _fake_server(_res(ActionType.NAVIGATE, success=False, detail="x"))
    r = await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": "http://127.0.0.1/x"})
    assert r.success is False and r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["detail"] == "x"
    assert seen["last_status"] == 403  # 추적값을 판정기에 넘긴다
    assert r.data["challenge"]["kind"] == "blocked"
    assert r.data["last_http_status"] == 403


async def test_validation_error_result_has_no_challenge_key():
    server = _fake_server(_res(ActionType.NAVIGATE))
    r = await server.call_tool(tool_name(ActionType.NAVIGATE), {})
    assert r.success is False
    assert "challenge" not in r.data


# ------------------------------------------------------------------ 공용 추적기


def test_run_cli_uses_shared_document_status_tracker():
    from browser import doc_status
    from interface import run_cli

    assert run_cli._track_main_document_status is doc_status.track_main_document_status
    assert run_cli._is_main_document_response is doc_status.is_main_document_response


# ------------------------------------------------------------------ 목록·설명


def test_challenge_check_actions_list_is_exact():
    assert CHALLENGE_CHECK_ACTIONS == frozenset({
        ActionType.NAVIGATE, ActionType.GO_BACK, ActionType.RELOAD, ActionType.CLICK,
        ActionType.PRESS_KEY, ActionType.TYPE_TEXT, ActionType.SELECT_OPTION,
        ActionType.CHECK_BOX, ActionType.OBSERVE_PAGE, ActionType.TAB_CONTROL,
        ActionType.WAIT_FOR,
    })


def test_target_tool_descriptions_mention_challenge():
    for spec in mcp_server.build_all_tools():
        action = mcp_server.action_from_tool(spec["name"])
        has = "data.challenge" in spec["description"]
        assert has is (action in CHALLENGE_CHECK_ACTIONS), spec["name"]


def test_input_schemas_unchanged_by_challenge_note():
    from contracts import ACTION_INPUT_MAP

    for action, model in ACTION_INPUT_MAP.items():
        assert mcp_server.build_tool_schema(action)["inputSchema"] == model.model_json_schema()
