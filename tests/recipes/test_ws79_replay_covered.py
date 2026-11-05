"""WS-79 (#205): 레시피 재생의 click 단계도 같은 디스패처 경로(call_tool) — 덮임 처리·covered_by 가 그대로 실린다.

기록은 덮개 없이, 재생은 같은 페이지에 가운데만 덮는 띠(다른 점으로 성공) / 전부 덮는 띠(covered_by 로 멈춤)를 띄운다.
덮개는 상호작용 요소가 아니라(div) 골격이 같다. '눌렸나' 는 서버 요청 기록(/clicked·/cover)으로 본다.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from actions.covered_click import COVERED_MESSAGE
from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, tool_name

#: 덮개 클릭 기록 — onclick 속성은 레시피 골격(상호작용 요소)을 바꾸므로 스크립트로 단다. 덮개는 늘 같은 DOM 이고 위치만
#: 바뀐다(기록 때는 버튼 밖 — 골격이 같아야 재생이 그 페이지로 받아들인다).
LISTEN = "<script>document.getElementById('bar').addEventListener('click',()=>fetch('/cover'))</script>"
BAR = {
    "none": "left:400px;top:300px;width:100px;height:60px",
    "mid": "left:150px;top:90px;width:100px;height:60px",
    "full": "left:0;top:80px;width:100%;height:80px",
}


def _page(bar: str) -> bytes:
    return ("<!doctype html><html><head><meta charset=utf-8><title>상품</title></head><body style=margin:0>"
            "<main><button onclick=\"fetch('/clicked');document.body.append('눌림')\" style=\"position:absolute;left:50px;"
            "top:100px;width:300px;height:40px\">목록 보기</button><div id=bar style=\"position:fixed;background:#c33;"
            "z-index:10;" + BAR[bar] + "\">Cookie</div>" + LISTEN + "</main></body></html>").encode()


@pytest.fixture
def site():
    hits: List[str] = []
    state: Dict[str, Any] = {"bar": "none"}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            body = b"User-agent: *\nAllow: /\n" if self.path == "/robots.txt" else _page(state["bar"])
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
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits, state
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(srv: BrowserMCPServer, action: ActionType, **args: Any):
    return await srv.call_tool(tool_name(action), args)


async def _settle(srv: BrowserMCPServer) -> None:
    await srv._dispatcher.ctx.page.wait_for_timeout(300)  # noqa: SLF001 — onclick 의 fetch 가 서버에 닿을 시간


@pytest.mark.asyncio
async def test_replay_click_step_uses_covered_click_path(site):
    base, hits, state = site
    async with BrowserMCPServer(headless=True, profile="ws79") as srv:
        assert (await _call(srv, ActionType.NAVIGATE, url=base + "/shop")).success
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        eid = next(e["element_id"] for e in obs.data["observation"]["elements"] if e["name"] == "목록 보기")
        res = await _call(srv, ActionType.CLICK, element_id=eid, epoch=obs.snapshot_epoch)
        assert res.success, res.error_message
        saved = await srv.call_server_tool("browser_recipe", {"op": "save", "name": "목록", "last_n": 1})
        assert saved["success"], saved
        rid = saved["data"]["recipe"]["id"]

        state["bar"] = "mid"
        assert (await _call(srv, ActionType.NAVIGATE, url=base + "/shop")).success
        hits.clear()
        ok = await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid})
        await _settle(srv)
        ok_hits = list(hits)

        state["bar"] = "full"
        assert (await _call(srv, ActionType.NAVIGATE, url=base + "/shop")).success
        hits.clear()
        bad = await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid})
        await _settle(srv)
        bad_hits = list(hits)
    assert ok["success"], ok
    assert "/clicked" in ok_hits and "/cover" not in ok_hits, ok_hits
    assert not bad["success"]
    assert bad["data"]["recipe"]["reason"] == "action_failed", bad["data"]["recipe"]
    step = bad["data"]["step_result"]
    assert step["error_code"] == "E_ELEMENT_NOT_INTERACTABLE" and step["error_message"] == COVERED_MESSAGE
    assert step["data"]["covered_by"]["layer"] == "fixed" and step["data"]["covered_by"]["modal"] is False
    assert "/clicked" not in bad_hits and "/cover" not in bad_hits, bad_hits
