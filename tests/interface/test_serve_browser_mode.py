"""serve --browser {headless,human,user-chrome} (WS-27).

CLI 파싱·조합 오류 → run_stdio → create_server → BrowserMCPServer → BrowserCore 로
값이 끝까지 전달되는지, 시작 로그가 stdout(MCP 프로토콜 전용)이 아니라 stderr 로
가는지 고정한다. 브라우저는 띄우지 않는다(가짜 BrowserCore / 가짜 stdio).

실 Chrome·실 창 통합 테스트는 AB_USER_CHROME_TEST=1 / AB_HUMAN_TEST=1 일 때만 돈다.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from contracts import ActionType
from interface import cli, mcp_server
from interface.mcp_server import BrowserMCPServer, tool_name

USUAL = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


# ---------------------------------------------------------------- CLI 파싱


def _parse(*argv: str):
    return cli._build_parser().parse_args(["serve", *argv])


def test_serve_browser_default_headless():
    args = _parse()
    assert args.browser == "headless"
    assert args.chrome_profile is None
    assert args.keep_open is False


@pytest.mark.parametrize("mode", ["headless", "human", "user-chrome"])
def test_serve_browser_choices(mode):
    assert _parse("--browser", mode).browser == mode


def test_serve_browser_invalid_choice_errors():
    with pytest.raises(SystemExit) as ei:
        _parse("--browser", "stealth")
    assert ei.value.code == 2


@pytest.mark.parametrize("extra", [
    ["--chrome-profile", "/tmp/x"],
    ["--keep-open"],
    ["--browser", "human", "--keep-open"],
    ["--browser", "headless", "--chrome-profile", "/tmp/x"],
])
def test_serve_user_chrome_only_options_rejected(extra, monkeypatch, capsys):
    called: List[Any] = []
    monkeypatch.setattr(cli, "_cmd_serve", lambda a: called.append(a) or 0)
    with pytest.raises(SystemExit) as ei:
        cli.main(["serve", *extra])
    assert ei.value.code == 2
    assert called == []
    assert "user-chrome" in capsys.readouterr().err


def test_serve_combination_error_message(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_cmd_serve", lambda a: pytest.fail("serve 가 불리면 안 됨"))
    with pytest.raises(SystemExit):
        cli.main(["serve", "--keep-open"])
    err = capsys.readouterr().err
    assert "user-chrome" in err


def test_serve_usual_profile_rejected_before_start(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_cmd_serve", lambda a: pytest.fail("serve 가 불리면 안 됨"))
    with pytest.raises(SystemExit) as ei:
        cli.main(["serve", "--browser", "user-chrome", "--chrome-profile", str(USUAL)])
    assert ei.value.code == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "평소 Chrome 프로필" in out.err


def test_cli_passes_browser_options_to_run_stdio(monkeypatch, tmp_path):
    seen: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    prof = tmp_path / "prof"
    rc = cli.main([
        "serve", "--browser", "user-chrome", "--chrome-profile", str(prof), "--keep-open",
    ])
    assert rc == 0
    assert seen["browser_mode"] == "user-chrome"
    assert Path(seen["chrome_profile"]) == prof
    assert seen["keep_open"] is True


def test_cli_default_passes_headless(monkeypatch):
    seen: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    assert cli.main(["serve"]) == 0
    assert seen["browser_mode"] == "headless"
    assert seen["chrome_profile"] is None
    assert seen["keep_open"] is False


# ---------------------------------------------------------------- 서버까지 전달


def test_create_server_passes_browser_options(tmp_path):
    _, backend = mcp_server.create_server(
        browser_mode="user-chrome", chrome_profile=tmp_path / "p", keep_open=True
    )
    assert backend.browser_mode == "user-chrome"
    assert backend.chrome_profile == tmp_path / "p"
    assert backend.keep_open is True


def test_create_server_default_headless():
    _, backend = mcp_server.create_server()
    assert backend.browser_mode == "headless"
    assert backend.keep_open is False


class _RecordingCore:
    """BrowserMCPServer.start 가 만든 BrowserCore 인자를 기록하고 즉시 실패한다."""

    seen: List[Dict[str, Any]] = []

    def __init__(self, **kw: Any) -> None:
        _RecordingCore.seen.append(kw)

    async def start(self) -> Any:
        raise RuntimeError("stop-here")


@pytest.mark.parametrize("mode", ["headless", "human", "user-chrome"])
async def test_server_start_builds_core_with_mode(monkeypatch, tmp_path, mode):
    import browser

    _RecordingCore.seen = []
    monkeypatch.setattr(browser, "BrowserCore", _RecordingCore)
    kw: Dict[str, Any] = {"browser_mode": mode}
    if mode == "user-chrome":
        kw.update(chrome_profile=tmp_path / "p", keep_open=True)
    srv = BrowserMCPServer(**kw)
    with pytest.raises(RuntimeError, match="stop-here"):
        await srv.start()
    got = _RecordingCore.seen[-1]
    assert got["browser_mode"] == mode
    assert got["headless"] is True  # human 은 코어가 headless=False 로 바꾼다
    assert got.get("chrome_profile") == kw.get("chrome_profile")
    assert got.get("keep_open", False) == kw.get("keep_open", False)


async def test_server_start_failure_closes_core(monkeypatch):
    """시작 도중(컨텍스트 준비 등) 실패하면 띄운 브라우저를 닫는다 — user-chrome 고아 방지."""
    import browser

    state: Dict[str, Any] = {"closed": 0}

    class _FailingCore:
        def __init__(self, **kw: Any) -> None:
            pass

        async def start(self) -> Any:
            return self

        async def new_context(self, name: str) -> Any:
            raise RuntimeError("context-fail")

        async def close(self) -> None:
            state["closed"] += 1

    monkeypatch.setattr(browser, "BrowserCore", _FailingCore)
    srv = BrowserMCPServer(browser_mode="user-chrome")
    with pytest.raises(RuntimeError, match="context-fail"):
        await srv.start()
    assert state["closed"] == 1
    assert srv.started is False


async def test_run_stdio_passes_options_and_logs_to_stderr_only(monkeypatch, capsys, tmp_path):
    """시작 로그는 stderr 1줄 — stdout 에는 아무것도 쓰지 않는다(MCP 프로토콜 전용)."""
    import mcp.server.stdio as stdio_mod

    created: Dict[str, Any] = {}

    class _FakeServer:
        def create_initialization_options(self) -> Any:
            return None

        async def run(self, *a: Any) -> None:
            return None

    class _FakeBackend:
        closed = False

        async def close(self) -> None:
            _FakeBackend.closed = True

    def fake_create_server(**kw: Any):
        created.update(kw)
        return _FakeServer(), _FakeBackend()

    @contextlib.asynccontextmanager
    async def fake_stdio():
        yield (None, None)

    monkeypatch.setattr(mcp_server, "create_server", fake_create_server)
    monkeypatch.setattr(stdio_mod, "stdio_server", fake_stdio)
    await mcp_server.run_stdio(
        browser_mode="user-chrome", chrome_profile=tmp_path / "p", keep_open=False
    )
    out = capsys.readouterr()
    assert out.out == ""
    lines = [ln for ln in out.err.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    assert "browser=user-chrome" in lines[0]
    assert created["browser_mode"] == "user-chrome"
    assert created["chrome_profile"] == tmp_path / "p"
    assert created["keep_open"] is False
    assert _FakeBackend.closed is True


def test_serve_process_stdout_clean_on_eof(tmp_path):
    """실제 `agent-browser serve` 프로세스: stdin EOF 로 끝날 때 stdout 이 비어 있고
    시작 로그는 stderr 로 간다. 브라우저는 첫 툴 호출 전까지 띄우지 않는다."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "interface.cli", "serve", "--browser", "human"],
        input=b"", capture_output=True, timeout=60, env=env,
    )
    assert proc.stdout == b"", proc.stdout[:200]
    assert b"browser=human" in proc.stderr


def test_serve_process_usual_profile_exits_without_stdout():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "interface.cli", "serve", "--browser", "user-chrome",
         "--chrome-profile", str(USUAL)],
        input=b"", capture_output=True, timeout=60, env=env,
    )
    assert proc.returncode == 2
    assert proc.stdout == b""
    assert "평소 Chrome 프로필".encode() in proc.stderr


# ---------------------------------------------------------------- 실 브라우저 (opt-in)


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = ("<!doctype html><meta charset=utf-8><title>가게</title>"
                    "<p>상품 목록 사과 3000원</p><button>담기</button>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def _user_chrome_ready() -> bool:
    from browser.user_chrome import find_chrome

    return os.environ.get("AB_USER_CHROME_TEST") == "1" and find_chrome() is not None


def _human_ready() -> bool:
    if os.environ.get("AB_HUMAN_TEST") != "1":
        return False
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    return True


async def _nav_observe(srv: BrowserMCPServer, url: str):
    nav = await srv.call_tool(tool_name(ActionType.NAVIGATE), {"url": url})
    obs = await srv.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
    return nav, obs


@pytest.mark.skipif(not _user_chrome_ready(),
                    reason="AB_USER_CHROME_TEST=1 + 설치된 Chrome 필요(창이 뜸)")
async def test_real_user_chrome_serve_backend(tmp_path, local_site):
    srv = BrowserMCPServer(browser_mode="user-chrome", chrome_profile=tmp_path / "prof")
    await srv.start()
    pid = srv._core._user_chrome.process.pid  # noqa: SLF001
    try:
        assert _alive(pid)
        assert srv._core.tab_count == 1  # noqa: SLF001 — 첫 about:blank 채택, 빈 탭 없음
        nav, obs = await _nav_observe(srv, local_site)
        print(f"[integration] nav={nav.success} obs={obs.success} "
              f"challenge={obs.data.get('challenge')} status={obs.data.get('last_http_status')}")
        assert nav.success and obs.success
        assert obs.data["challenge"] is None
        assert obs.data["last_http_status"] == 200
        webdriver = await srv._page.evaluate("navigator.webdriver")  # noqa: SLF001
        print(f"[integration] navigator.webdriver={webdriver!r}")
        assert webdriver is False
    finally:
        await srv.close()
    await asyncio.sleep(0.5)
    assert not _alive(pid), "우리가 띄운 Chrome 이 남아 있음"


@pytest.mark.skipif(not _human_ready(), reason="AB_HUMAN_TEST=1 + 화면 필요(창이 뜸)")
async def test_real_human_serve_backend(local_site):
    srv = BrowserMCPServer(browser_mode="human")
    await srv.start()
    try:
        nav, obs = await _nav_observe(srv, local_site)
        viewport = srv._page.viewport_size  # noqa: SLF001
        print(f"[integration-human] nav={nav.success} obs={obs.success} viewport={viewport} "
              f"status={obs.data.get('last_http_status')}")
        assert nav.success and obs.success
        assert obs.data["challenge"] is None
        assert obs.data["last_http_status"] == 200
        assert viewport is None  # no_viewport
        assert srv._core.headless is False  # noqa: SLF001
    finally:
        await srv.close()
