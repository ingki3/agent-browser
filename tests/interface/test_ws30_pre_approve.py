"""serve --pre-approve 가 HITL 게이트까지 관통하는지 (WS-30 항목 1) + 차단 안내 (항목 5).

결함(WS-30 비교 시험): `_cmd_serve` 가 `args.pre_approve` 를 `run_stdio` 에 넘기지 않고,
`run_stdio` 시그니처에도 그 인자가 없어 CLI 플래그가 조용히 무시됐다.

* CLI → run_stdio kwargs (브라우저 없음)
* run_stdio → create_server kwargs (가짜 stdio)
* **실제 serve 프로세스**에 MCP stdio 로 붙어, 사전 승인한 이름의 고위험 클릭만 통과하고
  다른 이름은 여전히 차단되는지, `click:*` 와일드카드는 둘 다 통과하는지
로컬 HTTP 서버만 쓴다(외부 요청 없음).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode, ExecutionMode
from interface import cli, mcp_server
from interface.mcp_server import BrowserMCPServer

from test_run_cli import requires_chromium  # noqa: F401 - Chromium 유무 판정 재사용

SRC = Path(__file__).resolve().parents[2] / "src"

PAGE = """<!doctype html><meta charset=utf-8><title>결제 시험</title>
<p id=out>대기</p>
<button id=a onclick="document.getElementById('out').textContent='A 눌림'">결제 A</button>
<button id=b onclick="document.getElementById('out').textContent='B 눌림'">결제 B</button>
<button id=c onclick="document.getElementById('out').textContent='C 눌림'">보기</button>
"""


# ---------------------------------------------------------------- CLI → run_stdio


@pytest.mark.parametrize(
    "argv,expected",
    [
        ([], ()),
        (["--pre-approve", "click:결제 A"], ("click:결제 A",)),
        (["--pre-approve", "click:*", "--pre-approve", "type_text:*"], ("click:*", "type_text:*")),
    ],
)
def test_cli_passes_pre_approve_to_run_stdio(monkeypatch, argv, expected):
    seen: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    assert cli.main(["serve", *argv]) == 0
    assert tuple(seen["pre_approved_actions"]) == expected


def test_cli_passes_interactive_mode_to_run_stdio(monkeypatch):
    seen: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    assert cli.main(["serve", "--mode", "interactive"]) == 0
    assert seen["mode"] is ExecutionMode.INTERACTIVE


async def test_run_stdio_passes_pre_approve_to_create_server(monkeypatch):
    import mcp.server.stdio as stdio_mod

    created: Dict[str, Any] = {}

    class _FakeServer:
        def create_initialization_options(self) -> Any:
            return None

        async def run(self, *a: Any) -> None:
            return None

    class _FakeBackend:
        async def close(self) -> None:
            return None

    def fake_create_server(**kw: Any):
        created.update(kw)
        return _FakeServer(), _FakeBackend()

    @contextlib.asynccontextmanager
    async def fake_stdio():
        yield (None, None)

    monkeypatch.setattr(mcp_server, "create_server", fake_create_server)
    monkeypatch.setattr(stdio_mod, "stdio_server", fake_stdio)
    await mcp_server.run_stdio(pre_approved_actions=("click:결제 A",))
    assert tuple(created["pre_approved_actions"]) == ("click:결제 A",)
    created.clear()
    await mcp_server.run_stdio()
    assert tuple(created["pre_approved_actions"]) == ()


def test_create_server_hands_pre_approve_to_backend():
    _, backend = mcp_server.create_server(pre_approved_actions=("click:*",))
    assert backend.pre_approved_actions == ("click:*",)


# ---------------------------------------------------------------- 실제 serve 프로세스


@pytest.fixture
def pay_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = PAGE.encode("utf-8")
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
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()


async def _serve_session(url: str, extra: List[str], names: List[str]) -> Dict[str, Dict]:
    """실제 `agent-browser serve` 를 띄워 MCP stdio 로 이동·관찰·클릭한다."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "interface.cli", "serve", "--browser", "headless", *extra],
        env=env,
    )

    async def call(session: Any, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        res = await session.call_tool(name, args)
        return json.loads(res.content[0].text)

    out: Dict[str, Dict] = {}
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            nav = await call(session, "browser_navigate", {"url": url})
            assert nav["success"], nav
            obs = await call(session, "browser_observe_page", {})
            observation = obs["data"]["observation"]
            by_name = {e["name"]: e["element_id"] for e in observation["elements"]}
            for name in names:
                out[name] = await call(
                    session,
                    "browser_click",
                    {"element_id": by_name[name], "epoch": observation["snapshot_epoch"]},
                )
    return out


@requires_chromium
async def test_real_serve_pre_approve_specific_name(pay_site):
    out = await _serve_session(pay_site, ["--pre-approve", "click:결제 A"], ["결제 A", "결제 B"])
    a, b = out["결제 A"], out["결제 B"]
    assert a["success"], a
    assert a["error_code"] is None
    # 다른 이름은 여전히 차단된다.
    assert not b["success"]
    assert b["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value


@requires_chromium
async def test_real_serve_pre_approve_wildcard(pay_site):
    out = await _serve_session(pay_site, ["--pre-approve", "click:*"], ["결제 A", "결제 B"])
    assert out["결제 A"]["success"], out["결제 A"]
    assert out["결제 B"]["success"], out["결제 B"]


@requires_chromium
async def test_real_serve_without_pre_approve_blocks_with_hint(pay_site):
    """사전 승인이 없으면 차단 + 사람용 해결 경로(항목 5)."""
    out = await _serve_session(pay_site, [], ["결제 A", "보기"])
    blocked = out["결제 A"]
    assert blocked["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value
    assert blocked["data"]["pre_approve_hint"] == "click:결제 A"
    assert '--pre-approve "click:결제 A"' in blocked["error_message"]
    assert "--mode interactive" in blocked["error_message"]
    # 저위험 클릭은 그대로 통과한다.
    assert out["보기"]["success"], out["보기"]


# ---------------------------------------------------------------- 차단 안내 (항목 5, 브라우저 없음)


class _Handle:
    def __init__(self, name: str) -> None:
        self.name = name


class _Engine:
    epoch = 0

    def get_handle(self, element_id: str) -> Any:
        return _Handle({"@e1": "계정 삭제"}.get(element_id, ""))


class _Page:
    url = "http://127.0.0.1/x"


def _gate_server(**kw: Any) -> BrowserMCPServer:
    from security import HITLGate

    srv = BrowserMCPServer(**kw)
    srv._engine = _Engine()
    srv._page = _Page()
    srv._hitl = HITLGate(mode=srv.mode, pre_approved_actions=srv.pre_approved_actions)
    return srv


@pytest.mark.parametrize(
    "action,params,hint",
    [
        (ActionType.CLICK, {"element_id": "@e1", "epoch": 0}, "click:계정 삭제"),
        (ActionType.UPLOAD_FILE, {"element_id": "@e2", "epoch": 0, "file_paths": ["a"]},
         "upload_file:*"),
        (ActionType.TYPE_TEXT, {"element_id": "@e2", "epoch": 0, "text": "q",
                                "press_enter": True}, "type_text:*"),
    ],
)
async def test_blocked_message_has_operator_hint(action, params, hint):
    srv = _gate_server()
    blocked = await srv._check_hitl(action, params)
    assert blocked is not None
    assert blocked.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert blocked.data["pre_approve_hint"] == hint
    msg = blocked.error_message
    assert f'--pre-approve "{hint}"' in msg
    assert "--mode interactive" in msg
    assert "사용자에게" in msg and "멈추" in msg
    # 기존 판정 근거도 그대로 남는다.
    assert "무인 모드에서 사전 승인되지 않은 고위험 액션" in msg


async def test_hint_really_unlocks():
    """안내한 hint 를 그대로 --pre-approve 로 주면 같은 액션이 통과한다."""
    srv = _gate_server()
    blocked = await srv._check_hitl(ActionType.CLICK, {"element_id": "@e1", "epoch": 0})
    hint = blocked.data["pre_approve_hint"]
    srv2 = _gate_server(pre_approved_actions=(hint,))
    assert await srv2._check_hitl(ActionType.CLICK, {"element_id": "@e1", "epoch": 0}) is None


async def test_blocked_message_teaches_no_bypass():
    """해결 경로는 운영자용 두 가지뿐 — press_key 등 우회를 알려 주지 않는다."""
    srv = _gate_server()
    blocked = await srv._check_hitl(
        ActionType.TYPE_TEXT,
        {"element_id": "@e2", "epoch": 0, "text": "q", "press_enter": True},
    )
    msg = blocked.error_message
    assert "press_key" not in msg
    assert "press_enter" not in msg
    assert "evaluate" not in msg
