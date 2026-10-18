"""WS-51 R4 — 서버 도구 출구(§1.2 b)·MCP SDK 예외 경로(§1.2 d) (브라우저 없음).

* R3-B7: `approved` 같은 값이 등록돼도 control_status·approval_wait 의 제어 필드(status·approval_id·holder·
  server_id·request_id·since)는 원래 값과 같고, 승인 대기·승인 뒤 재호출이 동작한다.
* P9: backend·봉투 직렬화가 예외를 내면 MCP SDK 가 isError 텍스트로 모델에 보낸다 — 그 문구에 원문 0.

값은 합성 값. 공개 사이트 없음.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from contracts import ActionType
from interface import mcp_server
from interface.mcp_server import tool_name
from security import secret_registry

from test_ws29_mcp_handoff import _blocked, _server
from ws29_helpers import srv_approve

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "security"))
from ws51_r4_leak import leaks_in_text  # noqa: E402

SECRET = "SYNTHr4ServerVal"


@pytest.fixture(autouse=True)
def _fresh_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def srv(tmp_path: Path):
    s = _server(tmp_path)
    yield s
    s.hub.close()


@pytest.mark.parametrize("value", ["approved", "agent", "pending", "navigate", "tab-1"])
async def test_b7_control_status_and_approval_wait_keep_control_fields(srv, value):
    """`?token=<값>` 을 본 뒤에도(등록 제외가 깨진 경우까지 막으려 레지스트리에 직접 넣는다) 제어 필드는 원래 값."""
    secret_registry.register_url(f"http://127.0.0.1:1/seen?token={value}")
    secret_registry.REGISTRY.add_values([value])
    raw_status = srv.hub.status()
    out = await srv.call_server_tool("browser_control_status", {})
    ctrl = out["data"]["control"]
    for key in ("holder", "server_id", "request_id", "since", "requested"):
        assert ctrl[key] == raw_status[key], (key, ctrl[key], raw_status[key])
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    waiter = asyncio.ensure_future(srv.call_server_tool(
        "browser_approval_wait", {"approval_id": aid, "timeout_s": 5}))
    await asyncio.sleep(0.1)
    await srv_approve(srv, aid)
    ap = (await waiter)["data"]["approval"]
    assert ap["approval_id"] == aid and ap["status"] == "approved", ap
    assert ap["expires_at"] == srv.hub.approval_status(aid)["expires_at"]
    ok = await _blocked(srv, approval_id=aid)  # 승인 뒤 재호출 동작
    assert ok.success, ok
    assert ok.data["approval"] == {"approval_id": aid, "status": "used", "outcome": "succeeded"}


async def test_server_tool_free_text_is_masked(srv):
    """서버 도구 응답의 자유 문장(control_request reason)은 가린다 — 제어 키라도 형식이 다르면 정제."""
    srv._human_can_see = lambda: True
    secret_registry.register_url(f"http://127.0.0.1:1/seen?token={SECRET}")
    out = await srv.call_server_tool("browser_control_request", {"reason": f"세션 {SECRET} 확인 필요"})
    assert out["success"], out
    status = await srv.call_server_tool("browser_control_status", {})
    assert SECRET not in json.dumps(status, ensure_ascii=False)
    assert "[REDACTED]" in status["data"]["control"]["reason"]


# ------------------------------------------------------------------ P9 MCP SDK 예외 경로


async def _call_via_sdk(server: Any, name: str, args: dict) -> Any:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    result: Any = None
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: server.run(server_streams[0], server_streams[1],
                                             server.create_initialization_options()))
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                result = await session.call_tool(name, args)
            tg.cancel_scope.cancel()
    return result


def _texts(result: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


@pytest.mark.parametrize("where", ["envelope_json", "call_tool", "call_server_tool"])
async def test_p9_sdk_error_text_masked(where, monkeypatch):
    """envelope 직렬화(·backend) 예외 주입 → SDK 가 모델에 보내는 isError 텍스트에 원문 0(등록 값·주소 토큰)."""
    secret_registry.register_url(f"http://127.0.0.1:1/seen?token={SECRET}")
    message = f"Circular reference detected at {SECRET} /a?token=SYNTHurlTok999 \"{SECRET}\""
    server, backend = mcp_server.create_server()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError(message)

    async def aboom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError(message)

    from contracts import ActionResult

    async def fake_call_tool(name: str, arguments: dict) -> ActionResult:
        return ActionResult(action=ActionType.OBSERVE_PAGE, success=True, snapshot_epoch=0,
                            current_url="about:blank", tab_id="tab-1", retry_safe=True)

    if where == "envelope_json":
        monkeypatch.setattr(backend, "call_tool", fake_call_tool)
        monkeypatch.setattr(mcp_server, "envelope_json", boom)
        name = tool_name(ActionType.OBSERVE_PAGE)
    elif where == "call_tool":
        monkeypatch.setattr(backend, "call_tool", aboom)
        name = tool_name(ActionType.OBSERVE_PAGE)
    else:
        monkeypatch.setattr(backend, "call_server_tool", aboom)
        name = "browser_control_status"
    try:  # mcp 1.x: isError 결과 텍스트 · 2.x: JSON-RPC 오류(MCPError) 문구 — 둘 다 모델에 가는 문구
        result = await _call_via_sdk(server, name, {})
        text = _texts(result)
        assert result.isError, text
    except BaseException as exc:  # noqa: BLE001 - anyio 묶음 예외(ExceptionGroup) 안의 MCPError
        errors = list(getattr(exc, "exceptions", [exc]))
        while any(hasattr(e, "exceptions") for e in errors):
            errors = [x for e in errors for x in getattr(e, "exceptions", [e])]
        mcp_errors = [e for e in errors if type(e).__name__ in ("MCPError", "McpError")]
        assert mcp_errors, repr(exc)
        text = "\n".join(str(e) for e in mcp_errors)
    assert "도구 처리 중 내부 오류" in text and "ValueError" in text
    assert leaks_in_text(text, SECRET) == [] and "SYNTHurlTok999" not in text, text
