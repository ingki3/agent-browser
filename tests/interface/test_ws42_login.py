"""WS-42: human login commands, local cookies only, no real profiles/sites."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from browser import serve_profile
from interface.cli import main, _build_parser
from interface.handoff import HandoffHub, LoginJob
from interface.mcp_server import BrowserMCPServer


def test_login_parser():
    args = _build_parser().parse_args(["login", "https://example.test", "--profile", "mock"])
    assert args.timeout == 600
    assert args.profile == "mock"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/hosts", "https://", "https://u:p@example.test"])
def test_bad_url(url, capsys):
    assert main(["login", url, "--profile", "mock"]) != 0
    assert not serve_profile.profile_dir("mock").exists()
    assert "http" in capsys.readouterr().err


def test_no_display(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert main(["login", "https://example.test", "--profile", "mock"]) != 0
    assert "맥 앞" in capsys.readouterr().err


def test_partial_stdin_cannot_block_timeout(monkeypatch):
    from interface import login_cli

    reader, writer = os.pipe()

    class Pipe:
        def fileno(self):
            return reader

        def readline(self):
            raise AssertionError("partial piped input would block readline and prevent the timeout")

    monkeypatch.setattr(sys, "stdin", Pipe())
    try:
        os.write(writer, b"partial")
        assert not login_cli._enter_ready()
        os.write(writer, b"\n")
        assert login_cli._enter_ready()
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize("busy", [False, True])
def test_sites_only_metadata(busy, capsys):
    path = serve_profile.prepare(serve_profile.profile_dir("mock")) / "Default" / "Network"
    path.mkdir(parents=True)
    with sqlite3.connect(path / "Cookies") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE cookies(host_key TEXT, expires_utc INTEGER, name TEXT, value TEXT)")
        db.executemany("INSERT INTO cookies VALUES(?,?,?,?)", [
            (".a.example.co.kr", 13400000000000000, "cookie_name_marker", "cookie_value_marker"),
            (".b.example.co.kr", 0, "session_name_marker", "session_value_marker"),
            ("127.0.0.1", 0, "other_name_marker", "other_value_marker"),
        ])
        db.commit()
        lock = serve_profile.acquire("mock", server_id="test") if busy else None
        try:
            assert main(["profile", "sites", "mock", "--json"]) == 0
            out = capsys.readouterr()
            rows = json.loads(out.out)
            assert "로그인" in rows["notice"]
            by = {r["domain"]: r for r in rows["sites"]}
            assert by["example.co.kr"]["session_only"] is False
            assert by["example.co.kr"]["expires_at"]
            assert by["127.0.0.1"]["session_only"] is True
            assert by["127.0.0.1"]["expires_at"] is None
            for marker in ("cookie_name_marker", "cookie_value_marker", "session_name_marker", "session_value_marker"):
                assert marker not in out.out + out.err
        finally:
            if lock:
                lock.release()


async def test_login_hint_control_request():
    s = BrowserMCPServer(profile="mock")
    s._page = SimpleNamespace(url="https://example.test/path?token=not-for-output")
    try:
        out = await s.call_server_tool("browser_control_request", {"reason": "로그인 필요"})
        assert out["data"]["login_hint"] == "맥에서: agent-browser login https://example.test --profile mock"
        assert "not-for-output" not in json.dumps(out)
        out = await s.call_server_tool("browser_control_request", {"reason": ""})
        assert out["data"]["login_hint"]
        s._page = SimpleNamespace(url="http://[::1]:1234/path")
        out = await s.call_server_tool("browser_control_request", {"reason": "로그인 필요"})
        assert "http://[::1]:1234 --profile mock" in out["data"]["login_hint"]
        s.profile = None
        out = await s.call_server_tool("browser_control_request", {"reason": "로그인 필요"})
        assert "--profile 로 서버를 띄워야" in out["data"]["login_hint"]
    finally:
        await s.close()


@pytest.mark.parametrize("ending", ["enter", "timeout", "cancel"])
async def test_server_login_always_finishes(monkeypatch, ending):
    from interface import login_cli
    calls = []

    def send(root, sid, op, **fields):
        calls.append(op)
        return "nonce"

    monkeypatch.setattr(login_cli.handoff, "write_command", send)
    monkeypatch.setattr(login_cli.handoff, "wait_ack", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(login_cli.handoff, "read_control", lambda *a: {"holder": "human", "login": {"status": "ready"}})

    async def wait(*args):
        if ending == "timeout":
            raise asyncio.TimeoutError
        if ending == "cancel":
            raise asyncio.CancelledError

    monkeypatch.setattr(login_cli, "wait_for_done", wait)
    if ending == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await login_cli.server_login({"server_id": "test"}, "https://example.test", 2)
    else:
        assert await login_cli.server_login({"server_id": "test"}, "https://example.test", 2) == (1 if ending == "timeout" else 0)
    assert calls == ["login", "login_finish"]


def test_finish_cancels_pending_login():
    hub = HandoffHub()
    hub.open()
    try:
        assert hub._apply({"op": "login", "url": "https://example.test", "login_id": "a", "timeout": 1}, "nonce")[0] is None
        assert hub._apply({"op": "login_finish", "login_id": "a"})[0]
        assert hub.login is None
        assert hub.holder == "agent"
    finally:
        hub.close()


def test_finish_before_login_cannot_leave_orphan_window():
    # Hub polls random nonce filenames in sorted order, so Ctrl-C can arrive first.
    from interface.handoff import _write_private

    hub = HandoffHub()
    hub.open()
    try:
        _write_private(hub.dir / "cmd-aaaa.json", {"op": "login_finish", "login_id": "cancelled", "nonce": "aaaa", "server_id": hub.server_id})
        _write_private(hub.dir / "cmd-zzzz.json", {"op": "login", "url": "https://example.test", "login_id": "cancelled", "timeout": 600, "nonce": "zzzz", "server_id": hub.server_id})
        hub.poll()
        assert hub.login is None
        assert hub.holder == "agent"
        assert json.loads((hub.dir / "ack-zzzz.json").read_text())["ok"] is False
    finally:
        hub.close()


@pytest.mark.parametrize("url", ["file:///etc/hosts", "javascript:alert(1)"])
def test_hub_rejects_bad_scheme(url):
    hub = HandoffHub()
    assert hub._apply({"op": "login", "url": url, "login_id": "mock", "timeout": 1})[0] is False
    assert hub.login is None


def test_regular_release_finishes_login():
    hub = HandoffHub()
    hub.login = LoginJob(url="https://example.test", login_id="mock", status="ready", nonce="mock", deadline=1)
    hub.holder = "human"
    assert hub._apply({"op": "release"})[0]
    assert hub.login is None
    assert hub.holder == "agent"


async def test_direct_start_failure_releases(monkeypatch):
    from browser.core import BrowserCore
    from interface.login_cli import direct_login

    async def fail(*args):
        raise RuntimeError("mock startup failure")

    monkeypatch.setattr(BrowserCore, "start", fail)
    with pytest.raises(RuntimeError):
        await direct_login("mock", "https://example.test", 1)
    assert serve_profile.holder("mock") is None


async def test_login_gate_blocks_new_human_server_calls(monkeypatch):
    from contracts import ActionType, ErrorCode

    s = BrowserMCPServer(browser_mode="human", headless=False)
    entered = asyncio.Event()

    async def call(*args):
        entered.set()
        return s._error_result(ActionType.NAVIGATE, ErrorCode.TIMEOUT, "mock")

    monkeypatch.setattr(s, "_call_tool", call)
    s._hold_gate()
    pending = asyncio.create_task(s.call_tool("browser_navigate", {"url": "http://localhost"}))
    try:
        await asyncio.sleep(.05)
        assert not entered.is_set(), "login startup must serialize calls even without an on-demand window"
        s._release_gate()
        await pending
        assert entered.is_set()
    finally:
        s._release_gate()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await s.close()


async def test_hub_sites_metadata_bound(monkeypatch):
    from interface import handoff
    s = BrowserMCPServer(profile="mock")

    async def cookies():
        return [{"domain": f"site{i}.test", "name": "name_marker", "value": "value_marker", "expires": -1}
                for i in range(1000)]

    s._core = SimpleNamespace(context_for=lambda name: SimpleNamespace(cookies=cookies))
    s.open_handoff()
    try:
        nonce = handoff.write_command(s.hub.root, s.hub.server_id, "cookie_sites")
        await s._poll_handoff()
        path = s.hub.dir / f"ack-{nonce}.json"
        body = path.read_text()
        assert len(body.encode()) < 16000
        assert "name_marker" not in body and "value_marker" not in body
        out = json.loads(body)
        assert out["ok"] and out["truncated"]
        assert out["sites"]
        assert set(out["sites"][0]) == {"domain", "expires_at", "session_only"}
        assert path.stat().st_mode & 0o777 == 0o600
        # run_stdio reserves the profile before any browser/first tool exists.
        s._core = None
        db_path = serve_profile.prepare(serve_profile.profile_dir("mock")) / "Default"
        db_path.mkdir()
        with sqlite3.connect(db_path / "Cookies") as db:
            db.execute("CREATE TABLE cookies(host_key TEXT, expires_utc INTEGER)")
            db.execute("INSERT INTO cookies VALUES('fresh.example.test', 0)")
        nonce = handoff.write_command(s.hub.root, s.hub.server_id, "cookie_sites")
        await s._poll_handoff()
        out = json.loads((s.hub.dir / f"ack-{nonce}.json").read_text())
        assert out["sites"][0]["domain"] == "example.test"
    finally:
        s._core = None
        await s.close()
