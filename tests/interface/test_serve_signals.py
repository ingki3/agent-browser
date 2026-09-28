"""serve 종료 신호 처리 (WS-27 R1, NB-1).

SIGTERM·SIGHUP·SIGINT 를 받으면 정상 종료 경로(backend.close → keep_open 이 아니면 우리가 띄운
Chrome 종료)를 거쳐 128+신호 번호로 끝난다. 정리는 상한 시간(SHUTDOWN_CLOSE_TIMEOUT_S)이 있고,
두 번째 신호는 정리를 기다리지 않고 바로 끝낸다. stdout(MCP 전용)에는 아무것도 쓰지 않는다.

신호 처리 로직은 브라우저 없이 실제 serve 서브프로세스로 검증한다(가짜 backend.close 주입).
실 Chrome 시험은 AB_USER_CHROME_TEST=1 일 때만 돈다(임시 프로필).
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX 신호 전용"
)

SIGNALS = [signal.SIGTERM, signal.SIGHUP, signal.SIGINT]

# 가짜 close 를 주입해 실제 cli.main(["serve"]) 를 돌리는 드라이버.
# cfg: cap(정리 상한, 초), grace(감시 여유, 초), close_async_s / close_sync_s(가짜 close 지연).
_DRIVER = textwrap.dedent(
    """
    import asyncio, json, sys, time
    cfg = json.loads(sys.argv[1])
    from interface import cli, mcp_server

    if "cap" in cfg:
        mcp_server.SHUTDOWN_CLOSE_TIMEOUT_S = cfg["cap"]
    if "grace" in cfg:
        mcp_server.SHUTDOWN_WATCHDOG_GRACE_S = cfg["grace"]

    async def fake_close(self):
        print("FAKE_CLOSE_START", file=sys.stderr, flush=True)
        if cfg.get("close_async_s"):
            await asyncio.sleep(cfg["close_async_s"])
        if cfg.get("close_sync_s"):
            time.sleep(cfg["close_sync_s"])  # 이벤트 루프를 막는 동기 정리(UserChrome.close 같은)
        print("FAKE_CLOSE_END", file=sys.stderr, flush=True)

    mcp_server.BrowserMCPServer.close = fake_close
    sys.exit(cli.main(["serve"]))
    """
)


def _env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    return env


def _spawn(args: List[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, *args],
        stdin=subprocess.PIPE,  # 열어 둔다 — MCP 클라이언트가 붙어 있는 상태
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )


def _wait_stderr(proc: subprocess.Popen, needle: bytes, timeout: float = 30.0) -> bytes:
    """stderr 에 needle 이 나올 때까지 읽는다(시작 준비 확인용)."""
    buf = b""
    deadline = time.monotonic() + timeout
    fd = proc.stderr.fileno()
    while needle not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            proc.kill()
            raise AssertionError(f"stderr 에 {needle!r} 없음: {buf[-400:]!r}")
        r, _, _ = select.select([fd], [], [], left)
        if r:
            chunk = os.read(fd, 4096)
            if not chunk:
                raise AssertionError(f"프로세스가 먼저 끝남: {buf[-400:]!r}")
            buf += chunk
    return buf


def _finish(proc: subprocess.Popen, head: bytes, timeout: float) -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(f"{timeout}s 안에 끝나지 않음")
    elapsed = time.monotonic() - t0
    out = proc.stdout.read()
    err = head + proc.stderr.read()
    return {"rc": rc, "elapsed": elapsed, "stdout": out, "stderr": err.decode("utf-8", "replace")}


def _start_driver(cfg: Dict[str, Any]) -> tuple:
    proc = _spawn(["-c", _DRIVER, json.dumps(cfg)])
    head = _wait_stderr(proc, b"agent-browser serve:")
    return proc, head


# ---------------------------------------------------------------- 실제 serve (브라우저 미기동)


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_serve_process_signal_runs_cleanup_and_exits(sig):
    proc = _spawn(["-m", "interface.cli", "serve"])
    head = _wait_stderr(proc, b"agent-browser serve:")
    time.sleep(0.3)
    proc.send_signal(sig)
    res = _finish(proc, head, timeout=15)
    assert res["rc"] == 128 + int(sig), res
    assert res["stdout"] == b""
    assert f"신호 {sig.name}" in res["stderr"], res["stderr"]
    assert "정리 완료" in res["stderr"], res["stderr"]


# ---------------------------------------------------------------- 가짜 backend.close


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_signal_calls_backend_close_once(sig):
    proc, head = _start_driver({})
    time.sleep(0.3)
    proc.send_signal(sig)
    res = _finish(proc, head, timeout=15)
    assert res["rc"] == 128 + int(sig), res
    assert res["stdout"] == b""
    assert res["stderr"].count("FAKE_CLOSE_START") == 1, res["stderr"]
    assert res["stderr"].count("FAKE_CLOSE_END") == 1, res["stderr"]


def test_slow_async_close_bounded_by_cap():
    """close 가 끝나지 않아도 상한(cap) 뒤에는 끝난다 — 무한 대기 금지."""
    proc, head = _start_driver({"cap": 1.0, "grace": 30.0, "close_async_s": 60})
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    res = _finish(proc, head, timeout=10)
    assert res["rc"] == 128 + int(signal.SIGTERM)
    assert res["elapsed"] < 4.0, res["elapsed"]
    assert "FAKE_CLOSE_START" in res["stderr"]
    assert "FAKE_CLOSE_END" not in res["stderr"]
    assert "상한" in res["stderr"], res["stderr"]
    assert res["stdout"] == b""


def test_blocking_sync_close_bounded_by_watchdog():
    """정리가 이벤트 루프를 막아도(동기 대기) 감시 스레드가 cap+grace 뒤에 끝낸다."""
    proc, head = _start_driver({"cap": 1.0, "grace": 1.0, "close_sync_s": 60})
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    res = _finish(proc, head, timeout=10)
    assert res["rc"] == 128 + int(signal.SIGTERM)
    assert res["elapsed"] < 5.0, res["elapsed"]
    assert "FAKE_CLOSE_END" not in res["stderr"]
    assert res["stdout"] == b""


@pytest.mark.parametrize("blocking", [False, True], ids=["async", "sync"])
def test_second_signal_exits_immediately(blocking):
    key = "close_sync_s" if blocking else "close_async_s"
    proc, head = _start_driver({"cap": 30.0, "grace": 30.0, key: 60})
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    head += _wait_stderr(proc, b"FAKE_CLOSE_START", timeout=10)
    t0 = time.monotonic()
    proc.send_signal(signal.SIGINT)
    res = _finish(proc, head, timeout=10)
    assert time.monotonic() - t0 < 3.0
    assert res["rc"] == 128 + int(signal.SIGINT), res
    assert "FAKE_CLOSE_END" not in res["stderr"]
    assert res["stdout"] == b""


def test_stdin_eof_still_closes_once_and_exits_zero():
    """기존 정상 종료(stdin EOF) 경로는 그대로 — close 1회, 종료 코드 0."""
    proc = _spawn(["-c", _DRIVER, json.dumps({})])
    head = _wait_stderr(proc, b"agent-browser serve:")
    proc.stdin.close()
    res = _finish(proc, head, timeout=15)
    assert res["rc"] == 0, res
    assert res["stderr"].count("FAKE_CLOSE_START") == 1
    assert res["stdout"] == b""


# ---------------------------------------------------------------- 정리 상한 뒤 마지막 수단(단위)


class _HangingBackend:
    def __init__(self, keep_open: bool) -> None:
        class _UC:
            calls = 0

            def close(self, timeout: float = 10.0) -> None:
                _UC.calls += 1

        class _Core:
            pass

        self.uc = _UC()
        self._core = _Core()
        self._core._user_chrome = self.uc  # type: ignore[attr-defined]
        self._core.keep_open = keep_open  # type: ignore[attr-defined]

    async def close(self) -> None:
        import asyncio

        await asyncio.sleep(60)


@pytest.mark.parametrize("keep_open,expected", [(False, 1), (True, 0)])
async def test_bounded_close_timeout_falls_back_to_owned_chrome(monkeypatch, keep_open, expected):
    """close 가 상한을 넘으면 우리가 띄운 Chrome 을 직접 닫는다(keep_open 이면 두고)."""
    from interface import mcp_server

    monkeypatch.setattr(mcp_server, "SHUTDOWN_CLOSE_TIMEOUT_S", 0.2)
    backend = _HangingBackend(keep_open)
    t0 = time.monotonic()
    ok = await mcp_server._bounded_close(backend)  # noqa: SLF001
    assert ok is False
    assert time.monotonic() - t0 < 2.0
    assert type(backend.uc).calls == expected


# ---------------------------------------------------------------- 실 Chrome (opt-in)


def _user_chrome_ready() -> bool:
    from browser.user_chrome import find_chrome

    return os.environ.get("AB_USER_CHROME_TEST") == "1" and find_chrome() is not None


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<!doctype html><meta charset=utf-8><title>t</title><p>hello</p>"
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


def _chrome_pids(profile: Path) -> List[int]:
    """임시 프로필 경로로 우리가 띄운 Chrome 주 프로세스만 고른다(헬퍼 제외)."""
    path = str(profile.resolve()).replace(".", "[.]")
    out = subprocess.run(
        ["pgrep", "-f", "--",
         f"Google Chrome --remote-debugging-port=[0-9]+ --user-data-dir={path} "],
        capture_output=True, text=True,
    ).stdout.split()
    return [int(p) for p in out]


def _rpc(proc: subprocess.Popen, msg: Dict[str, Any]) -> None:
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen, req_id: int, timeout: float = 60.0) -> Dict[str, Any]:
    fd = proc.stdout.fileno()
    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            msg = json.loads(line)  # stdout 은 JSON-RPC 뿐이어야 한다
            if msg.get("id") == req_id:
                return msg
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"응답 {req_id} 대기 초과")
        r, _, _ = select.select([fd], [], [], left)
        if r:
            chunk = os.read(fd, 65536)
            if not chunk:
                raise AssertionError("서버 stdout 닫힘")
            buf += chunk


def _serve_and_navigate(profile: Path, url: str, *extra: str) -> subprocess.Popen:
    import mcp.types as mt

    proc = _spawn(["-m", "interface.cli", "serve", "--browser", "user-chrome",
                   "--allow-domain", "127.0.0.1", "--chrome-profile", str(profile), *extra])
    _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": mt.LATEST_PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}}})
    assert "result" in _read_response(proc, 1)
    _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "browser_navigate", "arguments": {"url": url}}})
    res = _read_response(proc, 2, timeout=90)
    text = res["result"]["content"][0]["text"]
    assert json.loads(text)["success"] is True, text
    return proc


def _wait_gone(pids: List[int], timeout: float) -> List[int]:
    deadline = time.monotonic() + timeout
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [p for p in alive if _alive(p)]
        time.sleep(0.2)
    return alive


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


@pytest.mark.skipif(not _user_chrome_ready(),
                    reason="AB_USER_CHROME_TEST=1 + 설치된 Chrome 필요(창이 뜸)")
def test_real_user_chrome_sigterm_closes_our_chrome(tmp_path, local_site):
    profile = tmp_path / "prof"
    proc = _serve_and_navigate(profile, local_site)
    pids: List[int] = []
    try:
        pids = _chrome_pids(profile)
        assert pids, "우리 Chrome 을 찾지 못함"
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=20)
        left = _wait_gone(pids, 10)
        print(f"[integration-sigterm] rc={rc} chrome={pids} left={left}")
        assert rc == 128 + int(signal.SIGTERM)
        assert left == [], "SIGTERM 뒤 우리 Chrome 이 남음"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for p in _chrome_pids(profile):
            os.killpg(p, signal.SIGTERM)


@pytest.mark.skipif(not _user_chrome_ready(),
                    reason="AB_USER_CHROME_TEST=1 + 설치된 Chrome 필요(창이 뜸)")
def test_real_user_chrome_sigterm_keep_open_leaves_chrome(tmp_path, local_site):
    profile = tmp_path / "prof"
    proc = _serve_and_navigate(profile, local_site, "--keep-open")
    pids: List[int] = []
    try:
        pids = _chrome_pids(profile)
        assert pids
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=20)
        time.sleep(1.0)
        alive = [p for p in pids if _alive(p)]
        print(f"[integration-keep-open] rc={rc} chrome={pids} alive={alive}")
        assert rc == 128 + int(signal.SIGTERM)
        assert alive == pids, "--keep-open 인데 Chrome 이 닫힘"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for p in pids:
            try:
                os.killpg(p, signal.SIGTERM)  # 직접 정리(start_new_session → 그룹 = Chrome)
            except ProcessLookupError:
                pass
        left = _wait_gone(pids, 10)
        assert left == [], f"직접 정리 실패: {left}"
