"""WS-56 E2E — 실제 Chromium(headless) + 로컬 페이지 + MCP SDK 클라이언트(elicitation 지원).

* accept → 실제로 눌린다(서버 기록 1건), data.approval.via="elicitation"
* 사람이 보는 동안 버튼 이름이 바뀌면 → E_TOCTOU_MISMATCH, 눌리지 않음
* 사람이 보는 동안 같은 이름의 새 버튼으로 바뀌면(요소 교체) → E_TOCTOU_MISMATCH, 눌리지 않음
* decline → 눌리지 않음(headless 는 증표 없음 — 기존 안내)

로컬 http(127.0.0.1)·임시 상태 폴더만. 공개 사이트 없음.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

import anyio
import pytest

from contracts import ActionType, ErrorCode
from interface import handoff
from interface.mcp_server import create_server, tool_name
from test_run_cli import requires_chromium

PAGE = """<!doctype html><meta charset=utf-8><title>가게</title>
<p id=out>대기</p>
<div id=box><button id=pay onclick="fetch('/clicked?b=pay');document.getElementById('out').textContent='눌림'">결제하기</button></div>
"""


@pytest.fixture
def site():
    hits: List[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/clicked"):
                hits.append(self.path)
                body = "ok"
            else:
                body = PAGE
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
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    finally:
        srv.shutdown()
        srv.server_close()


async def _run(base: str, answer: str, during: Optional[Callable[[Any], Any]] = None) -> Dict[str, Any]:
    from mcp import types as t
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    server, backend = create_server(recipes=False)
    out: Dict[str, Any] = {"messages": []}

    async def elicit(ctx: Any, params: Any) -> Any:
        out["messages"].append(params.message)
        if during is not None:
            await during(backend)
        return t.ElicitResult(action=answer, content={} if answer == "accept" else None)

    try:
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(ss[0], ss[1], server.create_initialization_options()))
                async with ClientSession(*cs, elicitation_callback=elicit) as session:
                    await session.initialize()
                    await session.call_tool(tool_name(ActionType.NAVIGATE), {"url": base + "/"})
                    obs = await backend.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
                    observation = obs.data["observation"]
                    eid = next(e["element_id"] for e in observation["elements"] if e["name"] == "결제하기")
                    res = await session.call_tool(tool_name(ActionType.CLICK),
                                                  {"element_id": eid, "epoch": observation["snapshot_epoch"]})
                    out["env"] = json.loads(res.content[0].text)
                    await backend._dispatcher.ctx.page.wait_for_timeout(300)
                    out["text"] = await backend._dispatcher.ctx.page.text_content("#out")
                tg.cancel_scope.cancel()
    finally:
        await backend.close()
        backend.hub.close()
    return out


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))


@requires_chromium
async def test_accept_clicks_for_real(site):
    base, hits = site
    out = await _run(base, "accept")
    env = out["env"]
    assert env["success"] is True, env
    assert env["data"]["approval"]["via"] == "elicitation"
    assert out["text"] == "눌림" and hits == ["/clicked?b=pay"]
    assert len(out["messages"]) == 1 and "결제하기" in out["messages"][0]


@requires_chromium
async def test_renamed_while_waiting_is_toctou(site):
    base, hits = site

    async def rename(backend: Any) -> None:
        await backend._dispatcher.ctx.page.evaluate("document.getElementById('pay').textContent='결제 취소'")

    out = await _run(base, "accept", rename)
    assert out["env"]["error_code"] == ErrorCode.TOCTOU_MISMATCH.value, out["env"]
    assert out["text"] == "대기" and hits == []


@requires_chromium
async def test_replaced_while_waiting_is_toctou(site):
    """같은 이름의 다른 요소로 바뀌면(관찰한 노드가 문서에서 사라짐) 연결 확인이 잡는다."""
    base, hits = site

    async def replace(backend: Any) -> None:
        await backend._dispatcher.ctx.page.evaluate("""() => {
            const b = document.createElement('button'); b.id = 'other'; b.textContent = '결제하기';
            b.onclick = () => fetch('/clicked?b=other');
            document.getElementById('box').replaceChildren(b);
        }""")

    out = await _run(base, "accept", replace)
    assert out["env"]["error_code"] == ErrorCode.TOCTOU_MISMATCH.value, out["env"]
    assert out["text"] == "대기" and hits == []


@requires_chromium
async def test_decline_does_not_click(site):
    base, hits = site
    out = await _run(base, "decline")
    env = out["env"]
    assert env["success"] is False and env["data"]["elicitation"]["answer"] == "declined"
    assert env["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value
    assert out["text"] == "대기" and hits == []
