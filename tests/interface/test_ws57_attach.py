"""WS-57 `serve --profile NAME --attach` — 같은 프로필의 두 번째 serve 가 잠금 오류 대신 첫 serve 에 붙는다.

단위: 소켓 디렉터리 0700·소켓 0600·심볼릭 링크 거부·상대 uid 확인·악수·중계 종료 코드.
E2E: 실제 하위 프로세스 serve 두 개(대화·감시 모사)가 같은 프로필로 동시에 작업 — 서로의 활성 탭·epoch·탭 닫기·
navigate 영향 0, 로그인(쿠키) 공유, 첫 serve 종료 시 붙은 쪽은 명확한 오류로 끝남. 로컬 http·임시 프로필만.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

import pytest

from interface import attach

from test_run_cli import requires_chromium


@pytest.fixture
def short_root(monkeypatch):
    """유닉스 소켓 경로 상한(104바이트) 때문에 짧은 임시 프로필 루트(시스템 임시 폴더 아래)."""
    from browser.serve_profile import PROFILE_ROOT_ENV

    root = Path(tempfile.mkdtemp(prefix="ab57"))
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(root))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _profile_dir(root: Path, name: str = "p") -> Path:
    d = root / f"serve-{name}"
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


# ------------------------------------------------------------------ 단위: 디렉터리·소켓 파일


def test_socket_dir_is_private_and_symlinks_are_refused(short_root):
    prof = _profile_dir(short_root)
    d = attach.prepare_socket_dir(prof)
    assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700
    # 넓은 권한은 좁힌다(내 디렉터리)
    os.chmod(d, 0o755)
    attach.prepare_socket_dir(prof)
    assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700
    # 심볼릭 링크 거부
    shutil.rmtree(d)
    elsewhere = short_root / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    os.symlink(elsewhere, d)
    if hasattr(os, "lchmod"):
        os.lchmod(d, 0o700)  # 링크 자체 권한을 좁혀 '권한 검사'가 아니라 '심볼릭 링크 검사'로 걸리게
    with pytest.raises(attach.AttachError):
        attach.prepare_socket_dir(prof)
    with pytest.raises(attach.AttachError):
        attach.connect(prof, None)


def test_profile_dir_with_wide_permissions_is_refused(short_root):
    prof = _profile_dir(short_root)
    os.chmod(prof, 0o755)
    with pytest.raises(attach.AttachError):
        attach.prepare_socket_dir(prof)


async def _echo_listener(prof: Path, seen: list) -> attach.AttachListener:
    async def serve(reader, writer, workspace):
        seen.append(workspace)
        while True:
            line = await reader.readline()
            if not line:
                return
            writer.write(line)
            await writer.drain()

    listener = attach.AttachListener(prof, serve, server_id="t-1")
    await listener.start()
    return listener


async def test_listener_socket_is_0600_and_replaces_only_a_stale_socket(short_root):
    prof = _profile_dir(short_root)
    path = attach.socket_path(prof)
    attach.prepare_socket_dir(prof)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))  # 죽은 serve 가 남긴 소켓 파일
    stale.close()
    seen: list = []
    listener = await _echo_listener(prof, seen)
    try:
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
        assert stat.S_ISSOCK(os.lstat(path).st_mode)
    finally:
        await listener.close()
    assert not path.exists()
    # 소켓 자리에 심볼릭 링크가 있으면 지우지 않고 거부
    os.symlink(short_root / "x", path)
    with pytest.raises(attach.AttachError):
        await attach.AttachListener(prof, lambda *a: None).start()


async def test_long_profile_path_is_refused_cleanly(short_root):
    prof = _profile_dir(short_root, "a" * 32)
    deep = prof
    for i in range(4):
        deep = deep / ("d" * 20)
    deep.mkdir(parents=True, mode=0o700)
    with pytest.raises(attach.AttachError):
        await attach.AttachListener(deep, lambda *a: None).start()


# ------------------------------------------------------------------ 단위: 상대 확인·악수·중계


def test_peer_uid_is_the_kernel_reported_uid():
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert attach.peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


def _in_thread(fn, *args):
    out: Dict[str, Any] = {}

    def run():
        try:
            out["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc
    t = threading.Thread(target=run)
    t.start()
    return t, out


async def _join(t, timeout=10.0):
    deadline = time.monotonic() + timeout
    while t.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not t.is_alive()


async def test_handshake_assigns_workspaces(short_root):
    prof = _profile_dir(short_root)
    seen: list = []
    listener = await _echo_listener(prof, seen)
    try:
        t, out = _in_thread(attach.connect, prof, "watch")
        await _join(t)
        sock, reply = out["value"]
        assert reply == {"ok": True, "workspace": "watch", "server_id": "t-1"}
        sock.close()
        t, out = _in_thread(attach.connect, prof, None)
        await _join(t)
        out["value"][0].close()
        assert out["value"][1]["workspace"] == "attach-1"
        t, out = _in_thread(attach.connect, prof, "Bad Name")
        await _join(t)
        assert isinstance(out.get("error"), attach.AttachError)
        await asyncio.sleep(0.05)
        assert seen == ["watch", "attach-1"]
    finally:
        await listener.close()


async def test_other_user_peer_is_refused(short_root, monkeypatch):
    prof = _profile_dir(short_root)
    seen: list = []
    listener = await _echo_listener(prof, seen)
    real = attach.peer_uid
    try:
        # 첫 serve 쪽이 보는 상대 uid 만 바꾼다(클라이언트 쪽 확인은 진짜 값).
        calls = {"n": 0}

        def fake(sock):
            calls["n"] += 1
            return os.getuid() + 1 if threading.current_thread() is threading.main_thread() else real(sock)
        monkeypatch.setattr(attach, "peer_uid", fake)
        t, out = _in_thread(attach.connect, prof, "watch")
        await _join(t)
        assert isinstance(out.get("error"), attach.AttachError), out
        assert seen == [] and calls["n"] >= 1
        # 상대를 확인할 수 없어도(None) 거부
        monkeypatch.setattr(attach, "peer_uid", lambda sock: None if threading.current_thread()
                            is threading.main_thread() else real(sock))
        t, out = _in_thread(attach.connect, prof, "watch")
        await _join(t)
        assert isinstance(out.get("error"), attach.AttachError)
        assert seen == []
    finally:
        await listener.close()


@pytest.mark.parametrize("where", ["sendall", "recv"])
async def test_peer_reset_during_handshake_is_attach_error(short_root, monkeypatch, where):
    """첫 serve 가 거부하며 끊을 때 Linux 는 EOF 대신 ECONNRESET·EPIPE 를 준다(CI 실측) — 원시 OSError 가 아니라
    AttachError 여야 부르는 쪽이 원래 잠금 오류를 알린다. macOS 에서도 재현되게 소켓 메서드를 바꿔 끼운다."""
    prof = _profile_dir(short_root)
    seen: list = []
    listener = await _echo_listener(prof, seen)
    real_socket = socket.socket

    class ResettingSocket(real_socket):
        def sendall(self, *a, **k):  # noqa: D401
            if where == "sendall":
                raise BrokenPipeError(32, "Broken pipe")
            return super().sendall(*a, **k)

        def recv(self, *a, **k):
            if where == "recv":
                raise ConnectionResetError(104, "Connection reset by peer")
            return super().recv(*a, **k)

    monkeypatch.setattr(attach.socket, "socket", ResettingSocket)
    try:
        t, out = _in_thread(attach.connect, prof, "watch")
        await _join(t)
        assert isinstance(out.get("error"), attach.AttachError), out
    finally:
        monkeypatch.setattr(attach.socket, "socket", real_socket)
        await listener.close()


async def test_relay_exit_codes(short_root):
    """클라이언트(stdin)가 먼저 끝나면 0, 첫 serve 가 먼저 끝나면 stderr 한 줄 + 3."""
    import io

    prof = _profile_dir(short_root)
    seen: list = []
    listener = await _echo_listener(prof, seen)
    try:
        # (1) stdin EOF 먼저 → 0, 그 전에 보낸 줄은 그대로 돌아온다
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        t, out = _in_thread(attach.connect, prof, "watch")
        await _join(t)
        sock = out["value"][0]
        err = io.StringIO()
        t, res = _in_thread(lambda: attach.relay(sock, stdin_fd=r_in, stdout_fd=w_out, stderr=err))
        os.write(w_in, b'{"x":1}\n')
        os.close(w_in)
        await _join(t)
        os.close(w_out)
        assert os.read(r_out, 100) == b'{"x":1}\n'
        assert res["value"] == 0 and err.getvalue() == ""
        os.close(r_in)
        os.close(r_out)
        # (2) 첫 serve 가 먼저 끝남 → 3 + 안내
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        t, out = _in_thread(attach.connect, prof, "watch")
        await _join(t)
        sock = out["value"][0]
        err = io.StringIO()
        t, res = _in_thread(lambda: attach.relay(sock, stdin_fd=r_in, stdout_fd=w_out, stderr=err))
        await asyncio.sleep(0.1)
        await listener.close()
        await _join(t)
        assert res["value"] == attach.EXIT_PRIMARY_GONE
        assert "첫 serve 가 끝나" in err.getvalue()
        for fd in (r_in, w_in, r_out, w_out):
            os.close(fd)
    finally:
        await listener.close()


def test_cli_attach_requires_profile_and_valid_workspace(capsys):
    from interface.cli import main

    with pytest.raises(SystemExit) as e:
        main(["serve", "--attach"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        main(["serve", "--workspace", "../x"])
    assert e.value.code == 2


# ------------------------------------------------------------------ E2E: 하위 프로세스 serve 두 개


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str, headers: Dict[str, str] = None) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            cookies = self.headers.get("Cookie") or ""
            if self.path.startswith("/login"):
                self._send("<title>로그인됨</title>", {"Set-Cookie": "auth=synthetic; Max-Age=3600; Path=/"})
            elif self.path.startswith("/whoami"):
                who = "로그인" if "auth=synthetic" in cookies else "손님"
                self._send(f"<title>{who}</title><p id=who>{who}</p>")
            else:
                name = self.path.strip("/") or "home"
                self._send(f"<title>{name}</title><button>{name} 버튼</button>")

        def log_message(self, *a):  # noqa: D401
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


def _params(*extra: str):
    from mcp.client.stdio import StdioServerParameters

    args = ["-m", "interface.cli", "serve", "--profile", "p", "--attach", *extra]
    return StdioServerParameters(command=sys.executable, args=args, env=dict(os.environ))


class _Client:
    def __init__(self, session):
        self.session = session

    async def __call__(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        res = await self.session.call_tool(name, args)
        return json.loads(res.content[0].text)


async def _wait_for_socket(root: Path, timeout: float = 30.0) -> None:
    path = attach.socket_path(root / "serve-p")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    pytest.fail("첫 serve 가 붙기 소켓을 열지 않음")


@requires_chromium
async def test_e2e_two_serves_share_one_browser_without_touching_each_other(short_root, site):
    from contextlib import AsyncExitStack

    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    async with AsyncExitStack() as stack:
        err1 = stack.enter_context(tempfile.TemporaryFile(mode="w+"))
        err2 = stack.enter_context(tempfile.TemporaryFile(mode="w+"))
        r1, w1 = await stack.enter_async_context(stdio_client(_params(), errlog=err1))
        chat_s = await stack.enter_async_context(ClientSession(r1, w1))
        await chat_s.initialize()
        await _wait_for_socket(short_root)
        r2, w2 = await stack.enter_async_context(stdio_client(_params("--workspace", "watch"), errlog=err2))
        watch_s = await stack.enter_async_context(ClientSession(r2, w2))
        await watch_s.initialize()
        chat, watch = _Client(chat_s), _Client(watch_s)

        a, b = await asyncio.gather(chat("browser_navigate", {"url": site.url + "/chat"}),
                                    watch("browser_navigate", {"url": site.url + "/watch"}))
        assert a["success"] and b["success"], (a, b)
        assert a["tab_id"] != b["tab_id"]
        oa = await chat("browser_observe_page", {})
        epoch_a = oa.get("snapshot_epoch", 0)
        for path in ("/w1", "/w2"):
            assert (await watch("browser_navigate", {"url": site.url + path}))["success"]
        oa2 = await chat("browser_observe_page", {})
        assert oa2["current_url"].endswith("/chat") and oa2.get("snapshot_epoch", 0) == epoch_a
        lb = await watch("browser_tab_control", {"command": "list"})
        assert [t["tab_id"] for t in lb["data"]["tabs"]] == [b["tab_id"]]
        assert lb["data"]["other_workspace_tabs"] == 1
        r = await watch("browser_tab_control", {"command": "close", "tab_id": a["tab_id"]})
        assert r["error_code"] == "E_TAB_NOT_FOUND"
        assert (await watch("browser_tab_control", {"command": "close", "tab_id": b["tab_id"]}))["success"]
        oa3 = await chat("browser_observe_page", {})
        assert oa3["success"] and oa3["current_url"].endswith("/chat") and oa3["tab_id"] == a["tab_id"]
        # 로그인 공유: 대화가 받은 쿠키를 감시가 쓴다
        assert (await chat("browser_navigate", {"url": site.url + "/login"}))["success"]
        assert (await watch("browser_navigate", {"url": site.url + "/whoami"}))["success"]
        ex = await watch("browser_extract", {"selector": "#who"})
        assert "로그인" in json.dumps(ex["data"], ensure_ascii=False)
        # 두 연결이 같은 서버(브라우저 하나)
        st1 = await chat_s.call_tool("browser_control_status", {})
        st2 = await watch_s.call_tool("browser_control_status", {})
        sid1 = json.loads(st1.content[0].text)["data"]["control"]["server_id"]
        sid2 = json.loads(st2.content[0].text)["data"]["control"]["server_id"]
        assert sid1 == sid2



@requires_chromium
async def test_e2e_attached_serve_ends_with_clear_error_when_primary_exits(short_root, site):
    import subprocess as sp

    from contextlib import AsyncExitStack

    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    relay = None
    try:
        async with AsyncExitStack() as stack:
            err1 = stack.enter_context(tempfile.TemporaryFile(mode="w+"))
            r1, w1 = await stack.enter_async_context(stdio_client(_params(), errlog=err1))
            chat_s = await stack.enter_async_context(ClientSession(r1, w1))
            await chat_s.initialize()
            await _wait_for_socket(short_root)
            # 붙는 serve 는 직접 띄워 종료 코드·stderr 를 본다(stdin 은 열어 둔다).
            relay = sp.Popen([sys.executable, "-m", "interface.cli", "serve", "--profile", "p", "--attach",
                              "--workspace", "watch"], stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE,
                             env=dict(os.environ))
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
            relay.stdin.write((json.dumps(init) + "\n").encode())
            relay.stdin.flush()
            line = await asyncio.get_running_loop().run_in_executor(None, relay.stdout.readline)
            assert json.loads(line)["id"] == 1  # 붙은 연결로 첫 serve 가 답했다
        # 대화 쪽 stdio 가 끝남 → 첫 serve 종료 → 붙은 serve 는 3 + 안내
        code = await asyncio.get_running_loop().run_in_executor(None, lambda: relay.wait(timeout=30))
        stderr = relay.stderr.read().decode("utf-8", "replace")
        assert code == attach.EXIT_PRIMARY_GONE, (code, stderr)
        assert "첫 serve 가 끝나" in stderr
    finally:
        if relay is not None and relay.poll() is None:
            relay.kill()
