"""WS-95b R1(#278 검증 B1·NB2) — 선언 밖 인자 거부는 준비 부작용보다 **앞**.

* 선언 밖 키가 있는 호출은 브라우저 시작·작업 공간 준비(새 탭)·on-demand 창 관문/전환·직접 조작 사이트 미리 창(sticky,
  기존 문서 재이동)보다 먼저, 부작용 없는 순수 검증으로 거부한다.
* 순서(기존 그대로): 조작권(사람) 거부 > 계약 입력 검증 오류 > 선언 밖 키 거부 — 선언 밖 키가 있으면 셋 다 준비 전에.
  선언 밖 키가 없으면 앞당긴 검사는 아무것도 하지 않는다(기존 경로).
* NB2: 반향 키가 많아도(150개) 허용 키 목록이 잘리지 않는다(반향 이름 최대 5개 + '외 N개').

브라우저 시험은 로컬 루프백 HTTP 서버(합성 페이지)만 쓴다. 공개 사이트 없음.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, tool_name
from security.manual_sites import ManualSites

from test_run_cli import requires_chromium
from test_ws29_mcp_handoff import _server

REPRESENTATIVE: Dict[str, Dict[str, Any]] = {
    "browser_scroll": {"direction": "down"},
    "browser_navigate": {"url": "http://127.0.0.1:9/new"},
    "browser_click": {"selector": "button"},
    "browser_observe_page": {},
}


def _spy(calls: List[str], label: str) -> Any:
    async def spy(*a: Any, **k: Any) -> Any:
        calls.append(label)
        return None

    return spy


# ======================================================================= 브라우저 없음


@pytest.mark.parametrize("tool", list(REPRESENTATIVE))
async def test_refusal_does_not_start_browser(tmp_path, monkeypatch, tool):
    srv = BrowserMCPServer(handoff_root=tmp_path / "servers", recipes=False)
    try:
        calls: List[str] = []
        monkeypatch.setattr(srv, "start", _spy(calls, "start"))
        monkeypatch.setattr(srv, "_ensure_workspace", _spy(calls, "ensure_workspace"))
        r = await srv.call_tool(tool, dict(REPRESENTATIVE[tool], bogus_key=1))
        assert r.error_code is ErrorCode.FEATURE_NOT_IMPLEMENTED, (r.error_code, r.error_message)
        assert calls == []
        assert srv._started is False and srv._core is None  # noqa: SLF001
    finally:
        srv.hub.close()


@pytest.mark.parametrize("tool", list(REPRESENTATIVE))
async def test_refusal_skips_workspace_prep(tmp_path, monkeypatch, tool):
    srv = _server(tmp_path, visible=False)
    try:
        calls: List[str] = []
        monkeypatch.setattr(srv, "_ensure_workspace", _spy(calls, "ensure_workspace"))
        r = await srv.call_tool(tool, dict(REPRESENTATIVE[tool], workspace="fresh", bogus_key=1))
        assert r.error_code is ErrorCode.FEATURE_NOT_IMPLEMENTED, (r.error_code, r.error_message)
        assert calls == []
    finally:
        srv.hub.close()


def _on_demand(tmp_path) -> BrowserMCPServer:
    srv = BrowserMCPServer(handoff_root=tmp_path / "servers", manual_sites=ManualSites.of(("srail.kr",)),
                           recipes=False, browser_mode="on-demand")
    srv._started = True  # noqa: SLF001
    return srv


@pytest.mark.parametrize("tool,args", [
    ("browser_navigate", {"url": "https://srail.kr/refused"}),
    ("browser_tab_control", {"command": "create", "url": "https://srail.kr/refused"}),
    ("browser_scroll", {"direction": "down"}),
    ("browser_observe_page", {}),
])
async def test_on_demand_refusal_no_window_gate_or_switch(tmp_path, monkeypatch, tool, args):
    """직접 조작 사이트 목록 + on-demand: 거부 전에 창 관문·headless→headed 전환·sticky 가 일어나지 않는다."""
    srv = _on_demand(tmp_path)
    try:
        w = srv._window  # noqa: SLF001
        calls: List[str] = []

        async def fake_switch(headed: bool, reason: str, **_: Any) -> Dict[str, Any]:
            calls.append(f"switch:{reason}")
            w.state = "headed"
            return {"ok": True}

        monkeypatch.setattr(srv, "_switch_window", fake_switch)
        monkeypatch.setattr(srv, "_on_demand_enter", _spy(calls, "on_demand_enter"))
        monkeypatch.setattr(srv, "_ensure_workspace", _spy(calls, "ensure_workspace"))
        monkeypatch.setattr("interface.login_cli.display_available", lambda: True)
        r = await srv.call_tool(tool, dict(args, bogus_key=1))
        assert r.error_code is ErrorCode.FEATURE_NOT_IMPLEMENTED, (r.error_code, r.error_message)
        assert calls == []
        assert w.state == "headless" and not w.sticky
    finally:
        srv.hub.close()


async def test_contract_error_wins_and_is_also_before_prep(tmp_path, monkeypatch):
    """계약 오류 ⊕ 선언 밖 키: 계약 오류(ws39 '입력 검증 실패')가 먼저 — 그리고 그것도 브라우저 시작 전."""
    srv = BrowserMCPServer(handoff_root=tmp_path / "servers", recipes=False)
    try:
        calls: List[str] = []
        monkeypatch.setattr(srv, "start", _spy(calls, "start"))
        r = await srv.call_tool(tool_name(ActionType.HOVER), {"selector": "button", "bogus_key": 1})
        assert r.error_code is ErrorCode.ELEMENT_NOT_FOUND, (r.error_code, r.error_message)
        assert "입력 검증 실패" in (r.error_message or "")
        r = await srv.call_tool(tool_name(ActionType.SCROLL), {"direction": "sideways", "bogus_key": 1})
        assert r.error_code is not ErrorCode.FEATURE_NOT_IMPLEMENTED, (r.error_code, r.error_message)
        assert "입력 검증 실패" in (r.error_message or "")
        assert calls == []
    finally:
        srv.hub.close()


async def test_contract_error_without_extra_keeps_old_path(tmp_path, monkeypatch):
    """선언 밖 키가 없으면 앞당긴 검사는 아무것도 하지 않는다 — 계약 오류는 기존 자리(준비 뒤)에서 그대로."""
    srv = _server(tmp_path, visible=False)
    try:
        calls: List[str] = []
        real = srv._ensure_workspace  # noqa: SLF001

        async def spy() -> None:
            calls.append("ensure_workspace")
            await real()

        monkeypatch.setattr(srv, "_ensure_workspace", spy)
        r = await srv.call_tool(tool_name(ActionType.SCROLL), {"direction": "sideways"})
        assert r.error_code is not ErrorCode.FEATURE_NOT_IMPLEMENTED and "입력 검증 실패" in (r.error_message or "")
        assert calls == ["ensure_workspace"]
    finally:
        srv.hub.close()


async def test_human_control_still_first_and_before_prep(tmp_path, monkeypatch):
    """기존 순서(test_ws29 control_check_runs_before_input_validation): 조작권 거부가 계약·선언 밖 검사보다 먼저 —
    그리고 선언 밖 키가 있으면 그것도 작업 공간 준비 전에."""
    from interface.handoff import write_command

    srv = _server(tmp_path, visible=False)
    try:
        srv.open_handoff()
        write_command(srv.hub.root, srv.hub.server_id, "take")
        calls: List[str] = []
        monkeypatch.setattr(srv, "_ensure_workspace", _spy(calls, "ensure_workspace"))
        for args in ({"direction": "down", "bogus_key": 1}, {"direction": "sideways", "bogus_key": 1}):
            r = await srv.call_tool(tool_name(ActionType.SCROLL), dict(args, workspace="fresh"))
            assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.error_code, r.error_message)
        assert calls == []
        assert srv._dispatcher.calls == []  # noqa: SLF001
    finally:
        srv.hub.close()


# ======================================================================= NB2 허용 키 목록 보존


def test_many_echoed_keys_keep_allowed_list():
    from interface.server_common import undeclared_args_message
    from interface.server_schema import SERVER_TOOLS, declared_args

    keys = [("k" + "".join(chr(97 + int(d)) for d in f"{i:03d}") + "_" * 28)[:32] for i in range(150)]
    assert len(set(keys)) == 150
    names = [tool_name(a) for a in ActionType] + list(SERVER_TOOLS)
    for name in names:
        allowed = declared_args(name) or ()
        msg = undeclared_args_message(keys, allowed)
        assert msg.startswith("선언되지 않은 인자 150개: ["), msg
        assert "외 145개" in msg, msg
        tail = msg.split("허용:", 1)[1]
        for key in allowed:
            assert key in tail, (name, key, msg)
        assert msg.endswith("선언된 인자만으로 다시 호출하세요."), msg


def test_few_keys_message_unchanged():
    from interface.server_common import undeclared_args_message

    assert undeclared_args_message(["b", "a"], ("x",)) == (
        "선언되지 않은 인자 2개: [a, b] — 허용: [x]. 실행하지 않았습니다 — 선언된 인자만으로 다시 호출하세요.")


# ======================================================================= 실제 Chromium(루프백)


class _Ledger(BaseHTTPRequestHandler):
    requests: List[str] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests.append(self.path)
        body = (b"User-agent: *\nAllow: /\n" if self.path == "/robots.txt"
                else b"<!doctype html><title>synthetic</title><button>View</button>")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: Any) -> None:
        pass


@pytest.fixture
def ledger_site():
    handler = type("H", (_Ledger,), {"requests": []})
    http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", handler.requests
    finally:
        http.shutdown()
        http.server_close()


@requires_chromium
async def test_real_browser_new_workspace_refusal_no_tab_no_request(tmp_path, ledger_site):
    base, requests = ledger_site
    async with BrowserMCPServer(headless=True, nav_settle=False, handoff_root=tmp_path / "servers",
                                recipes=False) as s:
        r = await s.call_tool("browser_navigate", {"url": base + "/", "wait_until": "load"})
        assert r.success, r.error_message
        ctx = s._page.context  # noqa: SLF001
        for i, (tool, args) in enumerate(REPRESENTATIVE.items()):
            args = dict(args, url=base + "/new") if tool == "browser_navigate" else dict(args)
            pages, seen = len(ctx.pages), list(requests)
            r = await s.call_tool(tool, dict(args, workspace=f"fresh-{i}", bogus_key=1))
            assert r.error_code is ErrorCode.FEATURE_NOT_IMPLEMENTED, (tool, r.error_code, r.error_message)
            assert len(ctx.pages) == pages, f"refused {tool} created a tab"
            assert list(requests) == seen, (tool, seen, list(requests))


@requires_chromium
async def test_real_browser_refusal_before_first_start(tmp_path, ledger_site):
    base, requests = ledger_site
    s = BrowserMCPServer(headless=True, nav_settle=False, handoff_root=tmp_path / "servers", recipes=False)
    try:  # 첫 호출이 브라우저를 띄운다(async with 는 입구에서 띄우므로 쓰지 않는다)
        r = await s.call_tool("browser_navigate", {"url": base + "/x", "bogus_key": 1})
        assert r.error_code is ErrorCode.FEATURE_NOT_IMPLEMENTED, (r.error_code, r.error_message)
        assert s._started is False and s._core is None  # noqa: SLF001
        assert requests == []
        r = await s.call_tool("browser_navigate", {"url": base + "/x", "wait_until": "load"})
        assert r.success, r.error_message
        assert s._started is True and "/x" in requests  # noqa: SLF001
    finally:
        await s.close()
