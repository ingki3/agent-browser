"""WS-76 (#204): 레시피 재생도 휴지통·스팸·차단·비우기 단계에서 HITL 로 멈춘다 (실제 Chromium + 로컬 HTTP).

기록 때는 사전 승인(click:휴지통 비우기)으로 통과, 사전 승인 없는 서버(같은 프로필)에서 재생하면 approval_required,
서버 요청 기록에 /emptied 가 없어야 한다(눌리지 않음). 같은 틀의 폴더 열기('휴지통')는 재생이 끝까지 간다.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, tool_name

PAGE = ("<!doctype html><html><head><meta charset=utf-8><title>메일</title></head><body><main>"
        "<h1>휴지통</h1><form action='/emptied'><button type='submit'>휴지통 비우기</button></form>"
        "<a href='/folder'>휴지통</a></main></body></html>")


@pytest.fixture
def site():
    hits: List[str] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            body = ("User-agent: *\nAllow: /\n" if self.path == "/robots.txt" else PAGE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(srv: BrowserMCPServer, action: ActionType, **args: Any):
    return await srv.call_tool(tool_name(action), args)


async def _record(srv: BrowserMCPServer, base: str, name: str) -> str:
    assert (await _call(srv, ActionType.NAVIGATE, url=base + "/mail")).success
    obs = await _call(srv, ActionType.OBSERVE_PAGE)
    eid = next(e["element_id"] for e in obs.data["observation"]["elements"] if e["name"] == name)
    res = await _call(srv, ActionType.CLICK, element_id=eid, epoch=obs.snapshot_epoch)
    assert res.success, res.error_message
    saved = await srv.call_server_tool("browser_recipe", {"op": "save", "name": name, "last_n": 1})
    assert saved["success"], saved
    return saved["data"]["recipe"]["id"]


@pytest.mark.asyncio
async def test_replay_stops_at_empty_trash(site):
    base, hits = site
    async with BrowserMCPServer(headless=True, pre_approved_actions=("click:휴지통 비우기",),
                                profile="ws76") as srv:
        rid = await _record(srv, base, "휴지통 비우기")
    assert any(h.startswith("/emptied") for h in hits)  # 기록 때는 사전 승인으로 눌림
    async with BrowserMCPServer(headless=True, profile="ws76") as srv2:
        await _call(srv2, ActionType.NAVIGATE, url=base + "/mail")
        hits.clear()
        out = await srv2.call_server_tool("browser_recipe", {"op": "run", "id": rid})
    assert not out["success"]
    assert out["data"]["recipe"]["reason"] == "approval_required", out["data"]["recipe"]
    assert out["data"]["step_result"]["error_code"] == "E_HITL_UNATTENDED_BLOCKED"
    assert not any(h.startswith("/emptied") for h in hits), hits


@pytest.mark.asyncio
async def test_replay_folder_link_runs(site):
    base, hits = site
    async with BrowserMCPServer(headless=True, profile="ws76f") as srv:
        rid = await _record(srv, base, "휴지통")
        await _call(srv, ActionType.NAVIGATE, url=base + "/mail")
        hits.clear()
        out = await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid})
    assert out["success"], out
    assert "/folder" in hits
