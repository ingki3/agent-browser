"""WS-34: serve --browser on-demand — 창 없이 도는 단위 테스트(핵심 논리 고정).

실 브라우저·실 창 E2E 는 test_ws34_on_demand_e2e.py. 여기서는 CLI 인자, 모드별 사람 창 판단,
등록 가능 도메인, 복원 대상 URL, sticky 판정 상태기계, 사람 CLI 상태 표시, 전환 직렬화 관문을
창 없이 고정한다(Linux CI 에서도 돈다).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from interface import cli, handoff, on_demand
from interface.handoff import HandoffHub
from interface.mcp_server import SERVER_TOOLS, BrowserMCPServer
from interface.on_demand import WindowState, registrable_domain, restorable_url


def _parse(*argv: str):
    return cli._build_parser().parse_args(["serve", *argv])


# ---------------------------------------------------------------- CLI


def test_serve_accepts_on_demand():
    assert _parse("--browser", "on-demand").browser == "on-demand"
    assert _parse("--browser", "on-demand", "--profile", "work").profile == "work"


@pytest.mark.parametrize("extra", [["--keep-open"], ["--chrome-profile", "/x"]])
def test_on_demand_rejects_user_chrome_only_options(extra):
    with pytest.raises(SystemExit) as ei:
        cli.main(["serve", "--browser", "on-demand", *extra])
    assert ei.value.code == 2


def test_on_demand_passes_through_to_run_stdio(monkeypatch):
    seen = {}

    async def fake_run_stdio(**kw):
        seen.update(kw)

    monkeypatch.setattr("interface.mcp_server.run_stdio", fake_run_stdio)
    assert cli.main(["serve", "--browser", "on-demand"]) == 0
    assert seen["browser_mode"] == "on-demand"
    assert seen["profile"] is None


def test_server_rejects_on_demand_with_user_chrome_profile_combo():
    # on-demand 는 user-chrome 과 별개 방식 — 한 값만 고를 수 있다(인자 검사 = choices).
    with pytest.raises(SystemExit):
        _parse("--browser", "on-demand,user-chrome")


# ---------------------------------------------------------------- 서버 판단


def test_human_can_see_on_demand_but_not_headless():
    assert BrowserMCPServer(browser_mode="on-demand")._human_can_see() is True
    assert BrowserMCPServer(browser_mode="headless")._human_can_see() is False
    assert BrowserMCPServer(browser_mode="human")._human_can_see() is True


def test_on_demand_without_profile_uses_ephemeral_profile(_isolated_profile_root):
    from browser import serve_profile as sp

    s = BrowserMCPServer(browser_mode="on-demand")
    path = s.acquire_profile()
    assert path is not None and path.parent == _isolated_profile_root
    assert path.name.startswith(sp.EPHEMERAL_PREFIX)
    assert path.is_dir()
    assert sp.list_profiles() == []  # 이름 붙은 프로필 목록에는 안 보인다
    s.release_profile()
    assert not path.exists()  # 서버 종료 = 임시 프로필 삭제


def test_on_demand_with_named_profile_keeps_folder(_isolated_profile_root):
    s = BrowserMCPServer(browser_mode="on-demand", profile="work")
    path = s.acquire_profile()
    assert path.name == "serve-work"
    s.release_profile()
    assert path.exists()  # 이름 붙은 프로필은 지우지 않는다(로그인 유지)


def test_headless_without_profile_has_no_profile_folder():
    assert BrowserMCPServer(browser_mode="headless").acquire_profile() is None


def test_on_demand_start_cleans_leftover_ephemeral(_isolated_profile_root):
    from browser import serve_profile as sp

    dead = sp.acquire_ephemeral(server_id="1-dead")
    dead.release()
    s = BrowserMCPServer(browser_mode="on-demand")
    path = s.acquire_profile()
    try:
        assert not dead.path.exists()
        assert path.exists()
    finally:
        s.release_profile()


async def test_headless_control_request_still_refused_without_browser():
    s = BrowserMCPServer(browser_mode="headless")
    try:
        r = await s.call_server_tool("browser_control_request", {"reason": "캡차"})
        assert r["success"] is False
        assert "headless" in r["error_message"]
        assert "on-demand" in r["error_message"]  # 운영자 안내에 새 방식이 보인다
    finally:
        s.hub.close()


def test_control_request_description_mentions_on_demand():
    assert "on-demand" in SERVER_TOOLS["browser_control_request"]["description"]


# ---------------------------------------------------------------- 도메인·URL


@pytest.mark.parametrize("url,expected", [
    ("https://www.shop.example.com/x?y=1", "example.com"),
    ("https://example.com", "example.com"),
    ("http://127.0.0.1:8123/shop", "127.0.0.1"),
    ("http://localhost:3000/", "localhost"),
    ("https://a.b.co.kr/p", "b.co.kr"),
    ("https://m.naver.com", "naver.com"),
    ("http://[::1]:80/", "::1"),
    ("about:blank", ""),
    ("", ""),
    ("not a url", ""),
])
def test_registrable_domain(url, expected):
    assert registrable_domain(url) == expected


@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a", True),
    ("http://127.0.0.1:1/x", True),
    ("about:blank", False),
    ("chrome://settings", False),
    ("chrome-error://chromewebdata/", False),
    ("data:text/html,hi", False),
    ("file:///etc/passwd", False),
    ("javascript:alert(1)", False),
    ("", False),
])
def test_restorable_url(url, ok):
    assert restorable_url(url) is ok


# ---------------------------------------------------------------- sticky 상태기계


def test_sticky_pending_only_for_solved_domain_after_headless_return():
    w = WindowState()
    ch = {"kind": "captcha", "vendor": "cloudflare", "reason": "x"}
    # 창에서 해결한 적 없음 → 알림 없음
    assert w.note_challenge("https://shop.example.com/a", ch) is None
    w.state = "headed"
    w.note_solved(["https://www.example.com/item"])
    # 아직 창이 열린 동안의 감지는 sticky 대상이 아니다(사람이 해결 중)
    assert w.note_challenge("https://example.com/b", ch) is None
    w.state = "headless"
    # 다른 사이트 → 알림 없음
    assert w.note_challenge("https://other.org/", ch) is None
    pending = w.note_challenge("https://m.example.com/c", ch)
    assert pending is not None and pending["domain"] == "example.com"
    assert w.info()["sticky_pending"]["domain"] == "example.com"
    assert w.sticky is False
    # 다음 조작권 요청 때 sticky 로 확정
    assert w.take_sticky_for_request() is True
    assert w.sticky is True and "example.com" in w.sticky_reason
    assert w.info()["sticky"] is True and "sticky_pending" not in w.info()
    assert w.keep_window_after_release() is True


def test_not_sticky_closes_after_release():
    w = WindowState()
    assert w.take_sticky_for_request() is False
    assert w.keep_window_after_release() is False


def test_window_info_shape():
    w = WindowState()
    info = w.info()
    assert info["mode"] == "on-demand"
    assert info["state"] == "headless"
    assert info["sticky"] is False


# ---------------------------------------------------------------- 사람 CLI 상태 표시


def test_hub_status_carries_window_only_when_set(tmp_path):
    hub = HandoffHub(root=tmp_path / "s")
    assert "window" not in hub.status()
    hub.set_window({"mode": "on-demand", "state": "headless", "sticky": False})
    assert hub.status()["window"]["state"] == "headless"


def test_cli_control_status_shows_window(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "s"))
    hub = HandoffHub(root=tmp_path / "s", browser_mode="on-demand").open()
    try:
        hub.set_window({"mode": "on-demand", "state": "headed", "sticky": True,
                        "sticky_reason": "example.com 에서 캡차 재감지"})
        assert handoff.cli_control("status", hub.server_id) == 0
        out = capsys.readouterr().out
        assert "window=headed" in out and "sticky=True" in out
        assert handoff.cli_control("status", hub.server_id, as_json=True) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["window"]["sticky"] is True
    finally:
        hub.close()


def test_hub_cancel_request(tmp_path):
    hub = HandoffHub(root=tmp_path / "s")
    hub.request("캡차")
    v = hub.version
    assert hub.cancel_request() is True
    assert hub.status()["requested"] is False and hub.version > v
    assert hub.last_event == "request_expired"
    assert hub.cancel_request() is False


def test_hub_release_by_server(tmp_path):
    hub = HandoffHub(root=tmp_path / "s")
    hub.request("비밀번호", secret_wanted=True)
    hub.holder = handoff.HOLDER_HUMAN
    hub.release_by_server()
    st = hub.status()
    assert st["holder"] == "agent" and not st["requested"] and not st["secret_wanted"]
    assert hub.last_event == "released"


# ---------------------------------------------------------------- 직렬화 관문


async def test_gate_blocks_new_calls_until_switch_ends(monkeypatch):
    s = BrowserMCPServer(browser_mode="on-demand")
    s._hold_gate()
    waiter = asyncio.ensure_future(s._await_gate(timeout=5))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    s._release_gate()
    assert await waiter is True


async def test_gate_times_out_with_false():
    s = BrowserMCPServer(browser_mode="on-demand")
    s._hold_gate()
    assert await s._await_gate(timeout=0.05) is False
    s._release_gate()
    assert await s._await_gate(timeout=0.05) is True


async def test_drain_waits_for_inflight_calls():
    s = BrowserMCPServer(browser_mode="on-demand")
    s._inflight = 1

    async def finish():
        await asyncio.sleep(0.05)
        s._inflight = 0

    asyncio.ensure_future(finish())
    assert await s._drain_calls(timeout=2) is True
    s._inflight = 1
    assert await s._drain_calls(timeout=0.05) is False


async def test_tool_call_during_switch_times_out_with_clear_error(monkeypatch):
    s = BrowserMCPServer(browser_mode="on-demand")
    s._started = True  # 브라우저 없이: 관문에서 끝나야 한다
    monkeypatch.setattr(on_demand, "SWITCH_WAIT_S", 0.05)
    s._hold_gate()
    r = await s.call_tool("browser_observe_page", {})
    s._release_gate()
    assert r.success is False
    assert r.error_code.value == "E_TIMEOUT"
    assert r.data["window"]["mode"] == "on-demand"
    assert "전환" in r.error_message
    s.hub.close()
