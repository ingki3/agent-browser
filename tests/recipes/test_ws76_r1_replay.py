"""WS-76 R1 (#204 검증 B2·B3): 레시피 재생도 단건 클릭과 같은 판정 (실제 Chromium + 로컬 HTTP).

* `스팸으로 보내기`(B3 활용형)·`Block "Alice"`·동작 버튼 `Trash` — 사전 승인으로 기록, 승인 없는 서버(같은 프로필)에서
  재생하면 approval_required, 서버 요청 기록에 /done 이 없다(눌리지 않음).
* `휴지통 비우기 취소`(B2)·검색어 지우기 `비우기`(B1, 입력칸 옆) — 재생이 끝까지 간다(기준 그대로).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, tool_name

#: 폼 제출이 아닌 type=button(폼 제출 판정이 아니라 이름 판정만 재는지 — 제출 버튼이면 이름과 무관하게 HIGH 다).
_BUTTON = "<button type='button' onclick=\"location.href='/done'\">{name}</button>"
PAGES: Dict[str, str] = {
    "spam_send": _BUTTON.format(name="스팸으로 보내기"),
    "block_quoted": _BUTTON.format(name="Block &quot;Alice&quot;"),
    "trash_button": _BUTTON.format(name="Trash"),
    "empty_cancel": _BUTTON.format(name="휴지통 비우기 취소"),
    "clear_search": ("<label>검색<input value='words'></label>"
                     + _BUTTON.format(name="비우기")),
}
NAMES = {"spam_send": "스팸으로 보내기", "block_quoted": 'Block "Alice"', "trash_button": "Trash",
         "empty_cancel": "휴지통 비우기 취소", "clear_search": "비우기"}


@pytest.fixture
def site():
    hits: List[str] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            key = self.path.strip("/").split("?")[0]
            if self.path == "/robots.txt":
                body = "User-agent: *\nAllow: /\n"
            else:
                body = ("<!doctype html><html><head><meta charset=utf-8><title>메일</title></head><body><main>"
                        + PAGES.get(key, "<p>done</p>") + "</main></body></html>")
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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


async def _record(srv: BrowserMCPServer, url: str, name: str) -> str:
    assert (await _call(srv, ActionType.NAVIGATE, url=url)).success
    obs = await _call(srv, ActionType.OBSERVE_PAGE)
    eid = next(e["element_id"] for e in obs.data["observation"]["elements"] if e["name"] == name)
    res = await _call(srv, ActionType.CLICK, element_id=eid, epoch=obs.snapshot_epoch)
    assert res.success, res.error_message
    saved = await srv.call_server_tool("browser_recipe", {"op": "save", "name": name, "last_n": 1})
    assert saved["success"], saved
    return saved["data"]["recipe"]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spam_send", "block_quoted", "trash_button"])
async def test_replay_stops_at_r1_risky(site, kind):
    base, hits = site
    profile = "ws76r1" + kind.replace("_", "")
    async with BrowserMCPServer(headless=True, pre_approved_actions=("click:*",), profile=profile) as srv:
        rid = await _record(srv, f"{base}/{kind}", NAMES[kind])
    assert any(h.startswith("/done") for h in hits)  # 기록 때는 사전 승인으로 눌림
    async with BrowserMCPServer(headless=True, profile=profile) as srv2:
        await _call(srv2, ActionType.NAVIGATE, url=f"{base}/{kind}")
        hits.clear()
        out = await srv2.call_server_tool("browser_recipe", {"op": "run", "id": rid})
    assert not out["success"], out
    assert out["data"]["recipe"]["reason"] == "approval_required", out["data"]["recipe"]
    assert out["data"]["step_result"]["error_code"] == "E_HITL_UNATTENDED_BLOCKED"
    assert not any(h.startswith("/done") for h in hits), hits


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["empty_cancel", "clear_search"])
async def test_replay_r1_controls_run(site, kind):
    base, hits = site
    profile = "ws76r1" + kind.replace("_", "")
    async with BrowserMCPServer(headless=True, profile=profile) as srv:
        rid = await _record(srv, f"{base}/{kind}", NAMES[kind])
        await _call(srv, ActionType.NAVIGATE, url=f"{base}/{kind}")
        hits.clear()
        out = await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid})
    assert out["success"], out
    assert any(h.startswith("/done") for h in hits), hits
