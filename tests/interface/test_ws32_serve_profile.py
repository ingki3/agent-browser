"""WS-32: serve --profile NAME — 서버·CLI 단위(브라우저 없음).

* 서버: 잠금(서버 id), BrowserCore 에 영속 폴더 전달, 시작 실패·종료 때 잠금 반납, 동시 사용 거부,
  control_status 의 profile, headless control_request 안내 문구, user-chrome 동시 지정 거부.
* CLI: serve 인자 검사(이름·user-chrome 충돌), 사용 중이면 exit 2 + 서버 id, profile list/remove.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from browser import serve_profile as sp
from interface import cli, mcp_server
from interface.mcp_server import BrowserMCPServer


class _RecordingCore:
    seen: List[Dict[str, Any]] = []

    def __init__(self, **kw: Any) -> None:
        _RecordingCore.seen.append(kw)

    async def start(self) -> Any:
        raise RuntimeError("stop-here")


@pytest.fixture
def recording_core(monkeypatch):
    import browser

    _RecordingCore.seen = []
    monkeypatch.setattr(browser, "BrowserCore", _RecordingCore)
    return _RecordingCore


async def test_profile_passes_persistent_dir_and_holds_lock(recording_core, tmp_path,
                                                            _isolated_profile_root):
    srv = BrowserMCPServer(profile="t1", handoff_root=tmp_path / "servers")
    with pytest.raises(RuntimeError, match="stop-here"):
        await srv.start()
    kw = recording_core.seen[-1]
    assert kw["persistent_profile"] == _isolated_profile_root / "serve-t1"
    assert kw["egress"] is not None  # 영속 모드도 같은 검증 프록시
    # 시작 실패면 잠금도 반납
    assert sp.holder("t1") is None


async def test_no_profile_keeps_old_behaviour(recording_core, tmp_path, _isolated_profile_root):
    srv = BrowserMCPServer(handoff_root=tmp_path / "servers")
    with pytest.raises(RuntimeError, match="stop-here"):
        await srv.start()
    assert recording_core.seen[-1].get("persistent_profile") is None
    assert not _isolated_profile_root.exists()  # 프로필 폴더를 만들지 않는다


async def test_second_server_same_profile_refused(tmp_path):
    a = BrowserMCPServer(profile="t1", handoff_root=tmp_path / "servers")
    b = BrowserMCPServer(profile="t1", handoff_root=tmp_path / "servers")
    a.acquire_profile()
    try:
        with pytest.raises(sp.ProfileInUseError) as ei:
            await b.start()
        assert a.hub.server_id in str(ei.value)
        assert b._egress_runtime is None and b._core is None  # 아무것도 띄우지 않았다
    finally:
        await a.close()
    assert sp.holder("t1") is None  # close 가 잠금 반납
    b.acquire_profile()
    b.release_profile()


def test_profile_with_user_chrome_rejected():
    with pytest.raises(ValueError, match="user-chrome"):
        BrowserMCPServer(profile="t1", browser_mode="user-chrome")


def test_bad_profile_name_rejected():
    with pytest.raises(sp.ProfileError):
        BrowserMCPServer(profile="../x")


async def test_control_status_reports_profile_without_path(tmp_path, _isolated_profile_root):
    srv = BrowserMCPServer(profile="t1", handoff_root=tmp_path / "servers")
    try:
        st = await srv.call_server_tool("browser_control_status", {})
        assert st["data"]["profile"] == {"name": "t1", "persistent": True}
        assert str(_isolated_profile_root) not in json.dumps(st, ensure_ascii=False)
    finally:
        await srv.close()


async def test_control_status_without_profile(tmp_path):
    srv = BrowserMCPServer(handoff_root=tmp_path / "servers")
    try:
        st = await srv.call_server_tool("browser_control_status", {})
        assert "profile" not in st["data"]
    finally:
        await srv.close()


async def test_headless_control_request_mentions_profile_login(tmp_path):
    srv = BrowserMCPServer(profile="t1", handoff_root=tmp_path / "servers")
    try:
        r = await srv.call_server_tool("browser_control_request",
                                       {"reason": "로그인", "secret_wanted": True})
        assert r["success"] is False
        assert "--browser human --profile t1" in r["error_message"]
        assert "headless" in r["error_message"]
        assert r["data"]["profile"] == {"name": "t1", "persistent": True}
    finally:
        await srv.close()


async def test_headless_control_request_without_profile_unchanged(tmp_path):
    srv = BrowserMCPServer(handoff_root=tmp_path / "servers")
    try:
        r = await srv.call_server_tool("browser_control_request", {"reason": "로그인"})
        assert "--profile" not in r["error_message"]
    finally:
        await srv.close()


# ------------------------------------------------------------------ serve CLI


def _serve_kwargs(monkeypatch, argv: List[str]) -> Dict[str, Any]:
    got: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        got.update(kw)

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    assert cli.main(["serve", *argv]) == 0
    return got


def test_serve_profile_flag_passed(monkeypatch):
    assert _serve_kwargs(monkeypatch, ["--profile", "t1"])["profile"] == "t1"
    assert _serve_kwargs(monkeypatch, [])["profile"] is None


@pytest.mark.parametrize("bad", ["../x", "A", "x" * 33, "a/b"])
def test_serve_bad_profile_name_exit2(monkeypatch, capsys, bad):
    monkeypatch.setattr(mcp_server, "run_stdio", lambda **kw: pytest.fail("서버를 띄우면 안 됨"))
    with pytest.raises(SystemExit) as ei:
        cli.main(["serve", "--profile", bad])
    assert ei.value.code == 2
    assert "프로필 이름" in capsys.readouterr().err


def test_serve_profile_with_user_chrome_exit2(monkeypatch, capsys):
    monkeypatch.setattr(mcp_server, "run_stdio", lambda **kw: pytest.fail("서버를 띄우면 안 됨"))
    with pytest.raises(SystemExit) as ei:
        cli.main(["serve", "--profile", "t1", "--browser", "user-chrome"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "--profile" in err and "user-chrome" in err and "--chrome-profile" in err


def test_serve_profile_in_use_exit2_with_server_id(monkeypatch, capsys):
    """run_stdio 가 시작 전에 잠금을 잡는다 — 이미 쓰는 중이면 MCP 를 열지 않고 exit 2."""
    import mcp.server.stdio as stdio_mod

    @contextlib.asynccontextmanager
    async def no_stdio():
        pytest.fail("사용 중인 프로필로 MCP 를 열면 안 됨")
        yield (None, None)

    monkeypatch.setattr(stdio_mod, "stdio_server", no_stdio)
    held = sp.acquire("t1", server_id="4242-abcdef")
    try:
        assert cli.main(["serve", "--profile", "t1"]) == 2
    finally:
        held.release()
    err = capsys.readouterr().err
    assert "4242-abcdef" in err and "t1" in err


def test_run_stdio_releases_lock_on_normal_exit(monkeypatch):
    import mcp.server.stdio as stdio_mod

    @contextlib.asynccontextmanager
    async def fake_stdio():
        yield (None, None)

    class _S:
        def create_initialization_options(self) -> Any:
            return None

        async def run(self, *a: Any) -> None:
            assert sp.holder("t1") is not None  # 서빙 중에는 잠겨 있다
            return None

    real_create = mcp_server.create_server

    def fake_create_server(**kw: Any):
        _server, backend = real_create(**kw)
        return _S(), backend

    monkeypatch.setattr(mcp_server, "create_server", fake_create_server)
    monkeypatch.setattr(stdio_mod, "stdio_server", fake_stdio)
    asyncio.run(mcp_server.run_stdio(profile="t1"))
    assert sp.holder("t1") is None


# ------------------------------------------------------------------ profile CLI


def test_profile_list_text_and_json(capsys, _isolated_profile_root):
    sp.prepare(sp.profile_dir("t1"))
    lock = sp.acquire("t2", server_id="77-aa")
    try:
        assert cli.main(["profile", "list"]) == 0
        out = capsys.readouterr().out
        assert "t1" in out and "t2" in out and "77-aa" in out
        assert cli.main(["profile", "list", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)
    finally:
        lock.release()
    assert {r["name"] for r in rows} == {"t1", "t2"}
    assert str(_isolated_profile_root) not in out


def test_profile_list_empty(capsys):
    assert cli.main(["profile", "list"]) == 0
    assert "없습니다" in capsys.readouterr().out


def test_profile_remove_yes(capsys, _isolated_profile_root):
    sp.prepare(sp.profile_dir("t1"))
    assert cli.main(["profile", "remove", "t1", "--yes"]) == 0
    assert not (_isolated_profile_root / "serve-t1").exists()


def test_profile_remove_in_use_refused(capsys, _isolated_profile_root):
    sp.prepare(sp.profile_dir("t1"))
    lock = sp.acquire("t1", server_id="88-bb")
    try:
        assert cli.main(["profile", "remove", "t1", "--yes"]) == 2
    finally:
        lock.release()
    assert "88-bb" in capsys.readouterr().err
    assert (_isolated_profile_root / "serve-t1").exists()


def test_profile_remove_without_yes_non_tty_refused(monkeypatch, capsys, _isolated_profile_root):
    sp.prepare(sp.profile_dir("t1"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["profile", "remove", "t1"]) == 2
    assert (_isolated_profile_root / "serve-t1").exists()


@pytest.mark.parametrize("answer,code,kept", [("y\n", 0, False), ("n\n", 1, True)])
def test_profile_remove_prompt(monkeypatch, capsys, _isolated_profile_root, answer, code, kept):
    from interface import profile_cli

    sp.prepare(sp.profile_dir("t1"))
    monkeypatch.setattr(profile_cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO(answer))
    assert cli.main(["profile", "remove", "t1"]) == code
    assert (_isolated_profile_root / "serve-t1").exists() is kept


def test_profile_remove_bad_name(capsys):
    assert cli.main(["profile", "remove", "../x", "--yes"]) == 2


def test_profile_remove_missing(capsys):
    assert cli.main(["profile", "remove", "nope", "--yes"]) == 2
