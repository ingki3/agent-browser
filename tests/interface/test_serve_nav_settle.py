"""serve --nav-settle {on,off} (WS-28).

CLI 파싱 → run_stdio → create_server → BrowserMCPServer → DispatchContext 로 값이 끝까지
전달되는지, run 경로는 기본값(on)을 그대로 쓰는지 고정한다. 대부분 브라우저 없이 확인하고,
MCP 경로 지연 비교만 로컬 서버 + 헤드리스 Chromium 을 쓴다(외부 요청 없음).
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from interface import cli, mcp_server, run_cli
from interface.mcp_server import BrowserMCPServer, tool_name
from contracts import ActionType

from test_run_cli import _cfg, requires_chromium  # noqa: F401 - 헬퍼 재사용


def _parse(*argv: str):
    return cli._build_parser().parse_args(["serve", *argv])


# ---------------------------------------------------------------- CLI 파싱


def test_serve_nav_settle_default_on():
    assert _parse().nav_settle == "on"


@pytest.mark.parametrize("val", ["on", "off"])
def test_serve_nav_settle_choices(val):
    assert _parse("--nav-settle", val).nav_settle == val


def test_serve_nav_settle_invalid_exits_2():
    with pytest.raises(SystemExit) as ei:
        _parse("--nav-settle", "maybe")
    assert ei.value.code == 2


def test_serve_nav_settle_help_mentions_cost_and_wait_for(capsys):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["serve", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--nav-settle" in out
    assert "0.2" in out
    assert "wait_for" in out


@pytest.mark.parametrize("argv,expected", [([], True), (["--nav-settle", "on"], True),
                                           (["--nav-settle", "off"], False)])
def test_cli_passes_nav_settle_to_run_stdio(monkeypatch, argv, expected):
    seen: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    assert cli.main(["serve", *argv]) == 0
    assert seen["nav_settle"] is expected


# ---------------------------------------------------------------- 서버까지 전달


def test_create_server_passes_nav_settle():
    _, backend = mcp_server.create_server(nav_settle=False)
    assert backend.nav_settle is False
    _, backend = mcp_server.create_server()
    assert backend.nav_settle is True


def test_backend_default_nav_settle_true():
    assert BrowserMCPServer().nav_settle is True


async def test_run_stdio_passes_nav_settle(monkeypatch):
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
    await mcp_server.run_stdio(nav_settle=False)
    assert created["nav_settle"] is False
    created.clear()
    await mcp_server.run_stdio()
    assert created["nav_settle"] is True


class _StopCore:
    """_init_session 이 DispatchContext 를 만들 때까지 가짜로 진행한다."""

    def __init__(self, **kw: Any) -> None:
        pass

    async def start(self) -> Any:
        return self

    async def new_context(self, name: str) -> Any:
        return None

    def context_for(self, name: str) -> Any:
        return None

    async def new_tab(self, name: str) -> Any:
        class _Tab:
            tab_id = "tab-1"
            page = object()

        return _Tab()

    async def new_cdp_session(self, tab_id: str) -> Any:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.parametrize("flag", [True, False])
async def test_backend_builds_dispatch_context_with_nav_settle(monkeypatch, flag):
    import actions
    import browser
    from browser import doc_status

    seen: List[Any] = []

    class _Stop(Exception):
        pass

    class _RecordingDispatcher:
        def __init__(self, ctx: Any) -> None:
            seen.append(ctx)
            raise _Stop

    class _NoStatus:
        def attach(self, ctx: Any) -> None:
            return None

    monkeypatch.setattr(browser, "BrowserCore", _StopCore)
    monkeypatch.setattr(actions, "ActionDispatcher", _RecordingDispatcher)
    monkeypatch.setattr(doc_status, "PageDocumentStatus", _NoStatus)
    srv = BrowserMCPServer(nav_settle=flag)
    with pytest.raises(_Stop):
        await srv.start()
    assert len(seen) == 1
    assert seen[0].nav_settle is flag


# ---------------------------------------------------------------- run 경로 불변


def test_run_path_uses_default_nav_settle_on(monkeypatch):
    """run 은 DispatchContext 기본값(on)으로 만든다 — run_cli 코드는 바꾸지 않는다."""
    import actions
    import agent

    seen: List[Any] = []

    class Stop(Exception):
        pass

    real = actions.ActionDispatcher

    class Rec(real):  # type: ignore[misc, valid-type]
        def __init__(self, ctx: Any) -> None:
            seen.append(ctx)
            super().__init__(ctx)

    def fake_loop(**kw):
        raise Stop

    class Page:
        url = "about:blank"

        async def goto(self, *a, **k):
            return None

        async def wait_for_timeout(self, ms):
            return None

    class Ctx:
        async def new_cdp_session(self, page):
            return object()

        async def close(self):
            return None

    class Br:
        async def close(self):
            return None

    async def fake_open(pw, args, record, **_):
        return Br(), Ctx(), Page(), None

    async def body(page):
        return ""

    monkeypatch.setattr(actions, "ActionDispatcher", Rec)
    monkeypatch.setattr(agent, "AgentLoop", fake_loop)
    monkeypatch.setattr(run_cli, "_load_config", _cfg)
    monkeypatch.setattr(run_cli, "_open_browser", fake_open)
    monkeypatch.setattr(run_cli, "_read_body_text", body)
    args = cli._build_parser().parse_args(["run", "--url", "http://x/", "--goal", "g"])
    asyncio.run(run_cli.run_goal(args))
    assert len(seen) == 1
    assert seen[0].nav_settle is True


# ---------------------------------------------------------------- MCP 경로 지연(로컬)

_PAGE = ("<!doctype html><meta charset=utf-8><title>홈</title>"
         "<button onclick=\"document.getElementById('o').textContent='눌림'+Date.now()\">더하기</button>"
         "<p id=o></p>")


@pytest.fixture
def local_page():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_PAGE.encode())

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()


async def _mcp_click_p50(url: str, nav_settle: bool, n: int = 20) -> float:
    ms: List[float] = []
    async with BrowserMCPServer(nav_settle=nav_settle) as server:
        r = await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": url})
        assert r.success, r.error_message
        obs = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        observation = obs.data["observation"]
        eid = next(e["element_id"] for e in observation["elements"] if e["name"] == "더하기")
        epoch = observation["snapshot_epoch"]
        for _ in range(n):
            t0 = time.perf_counter()
            r = await server.call_tool(tool_name(ActionType.CLICK),
                                       {"element_id": eid, "epoch": epoch})
            ms.append((time.perf_counter() - t0) * 1000)
            assert r.success, r.error_message
            assert "nav_wait_ms" not in r.data
    return statistics.median(ms)


@requires_chromium
async def test_mcp_no_nav_click_p50_on_vs_off(local_page):
    from actions import dispatcher as dmod

    on = await _mcp_click_p50(local_page, True)
    off = await _mcp_click_p50(local_page, False)
    print(f"MCP 이동 없는 click p50 on={on:.1f}ms off={off:.1f}ms")
    assert on >= dmod.NAV_DETECT_MS * 0.9, on
    assert off <= on - dmod.NAV_DETECT_MS * 0.5, (on, off)
