"""WS-64 — 직접 조작할 사이트: 사람 CLI(manual-sites·handoff done)·서버 단위(미리 창·available·도구 목록·settle).

브라우저 없음. 목록 파일은 tmp 로 바꿔 끼운다(홈 파일을 건드리지 않는다).
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode
from interface import cli, handoff, manual_sites_cli
from interface.handoff import HandoffHub, write_command
from interface.mcp_server import BrowserMCPServer
from security import manual_sites as ms
from security.manual_sites import ManualSites


@pytest.fixture
def listfile(tmp_path, monkeypatch):
    path = tmp_path / "ab" / "manual_sites.json"
    monkeypatch.setenv(ms.MANUAL_SITES_ENV, str(path))
    return path


class _Stdin(io.StringIO):
    def __init__(self, text: str, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# ======================================================================= manual-sites CLI


def test_cli_add_list_noninteractive_and_file_0600(listfile, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Stdin("", tty=False))
    assert cli.main(["manual-sites", "add", "https://etk.srail.kr/"]) == 0  # add 는 비대화형도 받는다
    assert "etk.srail.kr" in capsys.readouterr().out
    assert stat.S_IMODE(os.stat(listfile).st_mode) == 0o600
    assert cli.main(["manual-sites", "list", "--json"]) == 0
    assert [r["site"] for r in json.loads(capsys.readouterr().out)["sites"]] == ["etk.srail.kr"]


@pytest.mark.parametrize("bad", ["*", "*.kr", "co.kr", "*.co.kr", "srail.kr/path", "ftp://srail.kr"])
def test_cli_add_rejects_public_suffix_and_wildcards(listfile, bad, capsys):
    assert cli.main(["manual-sites", "add", bad]) == 2
    assert not listfile.exists()


def test_cli_remove_requires_tty_and_retyped_site(listfile, capsys, monkeypatch):
    ms.add_site("srail.kr", listfile)
    monkeypatch.setattr("sys.stdin", _Stdin("srail.kr\n", tty=False))
    assert cli.main(["manual-sites", "remove", "srail.kr"]) == 2  # 에이전트·스크립트(비대화형)는 못 지운다
    assert [r["site"] for r in ms.read_entries(listfile)] == ["srail.kr"]
    monkeypatch.setattr("sys.stdin", _Stdin("other.kr\n", tty=True))
    assert cli.main(["manual-sites", "remove", "srail.kr"]) == 1  # 다르게 입력
    assert ms.read_entries(listfile)
    monkeypatch.setattr("sys.stdin", _Stdin("https://SRAIL.kr\n", tty=True))
    assert cli.main(["manual-sites", "remove", "srail.kr"]) == 0
    assert ms.read_entries(listfile) == []


def test_serve_flag_validated_and_store_passed(listfile, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--manual-site", "*.kr"])
    assert exc.value.code == 2
    ms.add_site("srail.kr", listfile)
    seen: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        seen.update(kw)

    from interface import mcp_server

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    assert cli.main(["serve", "--manual-site", "127.0.0.1:8080"]) == 0
    store = seen["manual_sites"]
    assert set(store.current().texts()) == {"127.0.0.1:8080", "srail.kr"}


# ======================================================================= handoff done(사람 CLI)


def test_manual_sites_file_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv(ms.MANUAL_SITES_ENV, raising=False)
    assert ms.manual_sites_file() == ms.MANUAL_SITES_FILE
    monkeypatch.setenv(ms.MANUAL_SITES_ENV, str(tmp_path / "x.json"))
    assert ms.manual_sites_file() == tmp_path / "x.json"


def test_hub_done_op_changes_no_state(tmp_path):
    hub = HandoffHub(tmp_path / "servers", browser_mode="human")
    hub.open()
    try:
        hub.request("x", secret_wanted=True)
        before = (hub.holder, hub.requested, hub.secret_wanted, hub.version, hub.last_event)
        write_command(hub.root, hub.server_id, "done")
        assert hub.poll() == ["handoff_done"]
        assert (hub.holder, hub.requested, hub.secret_wanted, hub.version, hub.last_event) == before
        assert "done" in handoff._OPS
    finally:
        hub.close()


def test_cli_handoff_done_sends_command(monkeypatch):
    sent: List[Any] = []
    monkeypatch.setattr(handoff, "cli_control", lambda op, sid, as_json=False: sent.append((op, sid)) or 0)
    assert cli.main(["handoff", "done", "--server", "123-abc"]) == 0
    assert sent == [("done", "123-abc")]


# ======================================================================= 에이전트는 목록을 바꿀 수 없다


def test_no_mcp_tool_can_change_the_list():
    from interface.mcp_server import build_all_tools, build_server_tools

    specs = build_all_tools() + build_server_tools()
    blob = json.dumps(specs, ensure_ascii=False).lower()
    assert "manual" not in blob and "직접 조작할 사이트" not in blob
    assert len(build_server_tools()) == len(build_server_tools())  # 서버 도구 수는 그대로(토큰 예산 시험이 고정)


# ======================================================================= 서버 단위(브라우저 없음)


def _bare(tmp_path, sites=("srail.kr",), **kw) -> BrowserMCPServer:
    return BrowserMCPServer(handoff_root=tmp_path / "servers", manual_sites=ManualSites.of(sites), recipes=False, **kw)


async def test_pre_switch_only_before_navigate_to_listed_site(tmp_path, monkeypatch):
    srv = _bare(tmp_path, browser_mode="on-demand")
    srv._started = True
    w = srv._window
    calls: List[Any] = []

    async def fake_switch(headed: bool, reason: str, **_: Any) -> Dict[str, Any]:
        calls.append((headed, reason))
        w.state = "headed"
        return {"ok": True}

    monkeypatch.setattr(srv, "_switch_window", fake_switch)
    monkeypatch.setattr("interface.login_cli.display_available", lambda: True)
    await srv._manual_pre_switch(ActionType.NAVIGATE, {"url": "https://example.org/"})
    await srv._manual_pre_switch(ActionType.CLICK, {"url": "https://srail.kr/"})
    await srv._manual_pre_switch(ActionType.TAB_CONTROL, {"command": "switch", "url": "https://srail.kr/"})
    assert calls == []
    await srv._manual_pre_switch(ActionType.NAVIGATE, {"url": "http://SRAIL.kr/main"})
    assert calls == [(True, "manual_site")]
    assert w.sticky is True and w.notice["reason"] == "manual_site" and "사라졌습니다" in w.notice["hint"]
    await srv._manual_pre_switch(ActionType.TAB_CONTROL, {"command": "create", "url": "https://srail.kr/"})
    assert calls == [(True, "manual_site")]  # 이미 창 — 다시 전환하지 않는다(sticky)


async def test_pre_switch_skipped_without_display_or_empty_list(tmp_path, monkeypatch):
    srv = _bare(tmp_path, browser_mode="on-demand")
    srv._started = True
    calls: List[Any] = []

    async def fake_switch(*a: Any, **k: Any) -> Dict[str, Any]:
        calls.append(a)
        return {"ok": True}

    monkeypatch.setattr(srv, "_switch_window", fake_switch)
    monkeypatch.setattr("interface.login_cli.display_available", lambda: False)
    await srv._manual_pre_switch(ActionType.NAVIGATE, {"url": "https://srail.kr/"})
    empty = _bare(tmp_path, sites=(), browser_mode="on-demand")
    empty._started = True
    monkeypatch.setattr(empty, "_switch_window", fake_switch)
    monkeypatch.setattr("interface.login_cli.display_available", lambda: True)
    await empty._manual_pre_switch(ActionType.NAVIGATE, {"url": "https://srail.kr/"})
    assert calls == [] and empty._window.state == "headless"


def test_available_follows_window_state(tmp_path):
    assert _bare(tmp_path)._manual_available() is False  # 고정 headless
    assert _bare(tmp_path, browser_mode="human")._manual_available() is True
    od = _bare(tmp_path, browser_mode="on-demand")
    assert od._manual_available() is False  # 리다이렉트·클릭으로 도착(창 없음) → available=false
    od._window.state = "headed"
    assert od._manual_available() is True


def test_settle_handoff_keeps_manual_site():
    from interface.server_signals import settle_handoff

    data = {"robots": {"disallowed": True}, **ms.handoff_data(ActionType.CLICK, available=True, handoff_id="mh-1")}
    settle_handoff(data)
    assert data["handoff"]["reason"] == "manual_site"
    other = {"robots": {"disallowed": True}, "handoff": {"reason": "captcha"}}
    settle_handoff(other)
    assert "handoff" not in other  # WS-63 동작 그대로


def test_control_vocabulary_registers_new_fixed_words():
    from security.public_url import CONTROL_VOCABULARY

    assert "manual_site" in CONTROL_VOCABULARY["reason"]
    assert "browser_control_wait" in CONTROL_VOCABULARY["tool"]
    assert set(ms.OUTCOMES) <= CONTROL_VOCABULARY["changed"]
    assert "cancelled" not in CONTROL_VOCABULARY["changed"]  # 사용자 취소는 done/expired 로 귀결
    assert "done" in CONTROL_VOCABULARY["op"]


def test_handoff_tool_schemas_unchanged_budget():
    """인계 도구 스키마는 바꾸지 않는다(browser_control_wait 재사용 — 토큰 예산 시험 test_ws29_mcp_handoff 가 고정)."""
    from interface.server_schema import SERVER_TOOLS

    assert "browser_control_wait" in SERVER_TOOLS
    assert not any("handoff_wait" in name for name in SERVER_TOOLS)
