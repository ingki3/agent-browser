"""WS-50 꺼진 브라우저 자동 복구 · 마지막 탭 닫기 · 창 닫힘 오판 (실 Chromium + 로컬 Mock 사이트).

실사용(2026-10-14, on-demand 서버): 에이전트가 tab_control close 로 마지막 탭을 닫자 영속 컨텍스트(브라우저)가
꺼졌고, 이후 1시간 동안 navigate·observe·tab_control create 가 모두 실패했다(창 전환 경로가 우연히 다시 띄울 때까지).
창이 열린 동안에는 같은 닫기가 '사람이 창을 닫음' 으로 오판돼 조작권이 반환됐다.

(1) 탭 1개에서 close → 빈 탭으로 교체(브라우저 유지) → navigate·observe 성공
(2) 영속 컨텍스트를 밖에서 닫음 / 브라우저 프로세스 종료 → 다음 호출 1회에서 headless 재오픈·마지막 URL 복원·
    browser_recovered 알림 → 이후 tab_control create 성공
(3) headed 창에서 에이전트가 마지막 탭을 닫아도 window_closed·release_by_server 없음
(4) holder=human 동안에는 자동 복구가 창을 바꾸지 않음
(5) 재오픈 실패 주입 → PAGE_CRASHED + 기존 안내, 호출당 재오픈 1회(무한 재시도 없음)
공개 사이트 없음. 프로필 폴더는 conftest 가 임시 디렉터리로 돌린다.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from contracts import ErrorCode
from interface.handoff import write_command
from interface.mcp_server import BrowserMCPServer, envelope_dict

from test_run_cli import requires_chromium

requires_display = pytest.mark.skipif(
    sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
    reason="창(headed) 테스트: 화면 없음",
)

PAGE = """<!doctype html><meta charset=utf-8><title>{title}</title>
<p id=t>{title}</p><button>예약 확인</button>"""


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            title = self.path.strip("/") or "home"
            data = PAGE.format(title=title).encode("utf-8")
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
        yield type("Site", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
    finally:
        srv.shutdown()
        srv.server_close()


def _inproc(s: BrowserMCPServer):
    async def call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name.startswith("browser_control") or name.startswith("browser_approval"):
            return await s.call_server_tool(name, args)
        return envelope_dict(await s.call_tool(name, args))
    return call


def _browser_pids(path) -> List[int]:
    out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True)
    needle = f"--user-data-dir={path}"
    pids = []
    for line in out.stdout.splitlines():
        if needle in line and "--type=" not in line:
            pids.append(int(line.strip().split(" ", 1)[0]))
    return pids


async def _wait_until(pred, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


# ------------------------------------------------------------------ (1) 마지막 탭 닫기


@requires_chromium
async def test_close_last_tab_keeps_browser_alive_on_demand(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        assert (await call("browser_navigate", {"url": site.url + "/camp"}))["success"]
        listed = await call("browser_tab_control", {"command": "list"})
        assert listed["data"]["count"] == 1
        only = listed["data"]["tabs"][0]["tab_id"]
        closed = await call("browser_tab_control", {"command": "close", "tab_id": only})
        assert closed["success"], closed
        assert closed["data"]["remaining"] == 1
        assert closed["data"]["replaced_with_blank"] is True
        assert closed["data"]["hint"]
        nav = await call("browser_navigate", {"url": site.url + "/after"})
        assert nav["success"], nav
        obs = await call("browser_observe_page", {})
        assert obs["success"], obs
        assert any(e["name"] == "예약 확인" for e in obs["data"]["observation"]["elements"])
        created = await call("browser_tab_control", {"command": "create", "url": site.url + "/two"})
        assert created["success"], created


@requires_chromium
async def test_close_last_tab_keeps_browser_alive_persistent_headless(site):
    """on-demand 가 아닌 영속 프로필(headless --profile)도 같은 규칙."""
    async with BrowserMCPServer(browser_mode="headless", profile="ws50") as s:
        call = _inproc(s)
        assert (await call("browser_navigate", {"url": site.url + "/camp"}))["success"]
        closed = await call("browser_tab_control", {"command": "close", "tab_id": s._core.active_tab_id})
        assert closed["success"] and closed["data"]["remaining"] == 1, closed
        assert closed["data"]["replaced_with_blank"] is True
        nav = await call("browser_navigate", {"url": site.url + "/after"})
        assert nav["success"], nav


@requires_chromium
async def test_close_non_last_tab_is_unchanged(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/a"})
        await call("browser_tab_control", {"command": "create", "url": site.url + "/b"})
        closed = await call("browser_tab_control", {"command": "close", "tab_id": s._core.active_tab_id})
        assert closed["success"] and closed["data"]["remaining"] == 1, closed
        assert "replaced_with_blank" not in closed["data"]


# ------------------------------------------------------------------ (2) 밖에서 꺼진 브라우저


async def _close_context(s: BrowserMCPServer) -> None:
    await s._core.context_for("mcp-session").close()


async def _kill_browser(s: BrowserMCPServer) -> None:
    for pid in _browser_pids(s._core.persistent_profile):
        os.kill(pid, signal.SIGKILL)
    assert await _wait_until(lambda: s._ctx_closed or not s._core.tabs())


@requires_chromium
@pytest.mark.parametrize("kill", [_close_context, _kill_browser], ids=["context_close", "process_kill"])
async def test_dead_browser_recovers_on_next_call(site, kill):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        assert (await call("browser_navigate", {"url": site.url + "/camp"}))["success"]
        epoch0 = s._engine.epoch
        await kill(s)
        obs = await call("browser_observe_page", {})
        assert obs["success"], obs
        win = obs["data"]["window"]
        assert win["notice"]["reason"] == "browser_recovered"
        assert "headless" in win["notice"]["hint"]
        assert win["state"] == "headless"
        assert obs["reobserve_required"] is True
        assert s._engine.epoch > epoch0
        assert s._page.url.endswith("/camp")  # 마지막 탭 URL 복원
        assert any(e["name"] == "예약 확인" for e in obs["data"]["observation"]["elements"])
        # 알림은 1회
        again = await call("browser_observe_page", {})
        assert again["success"] and "window" not in again["data"], again["data"].get("window")
        created = await call("browser_tab_control", {"command": "create", "url": site.url + "/two"})
        assert created["success"], created
        nav = await call("browser_navigate", {"url": site.url + "/three"})
        assert nav["success"], nav


@requires_chromium
async def test_dead_browser_navigate_recovers(site):
    """첫 호출이 navigate 여도 복구 뒤 실행된다(E_NAVIGATE_TIMEOUT 아님)."""
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/camp"})
        await _close_context(s)
        nav = await call("browser_navigate", {"url": site.url + "/next"})
        assert nav["success"], nav
        assert nav["data"]["window"]["notice"]["reason"] == "browser_recovered"


# ------------------------------------------------------------------ (4) holder=human


@requires_chromium
async def test_no_auto_recovery_while_human_holds(site, monkeypatch):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/camp"})
        # 사람이 조작권을 가짐(창 전환은 이 테스트에서 막는다 — 관심사는 자동 복구의 창 전환)
        switches: List[tuple] = []

        async def no_switch(headed, reason, **kw):
            switches.append((headed, reason))
            return {"ok": True, "reopened": False, "window": s._window.info()}

        monkeypatch.setattr(s, "_switch_window", no_switch)
        write_command(s.hub.root, s.hub.server_id, "take")
        for _ in range(100):
            await s._poll_handoff()
            if s.hub.holder == "human":
                break
            await asyncio.sleep(0.05)
        assert s.hub.holder == "human"
        await asyncio.sleep(0.3)  # take 가 시작한 창 전환 태스크(가짜)가 돌고 끝나게
        switches.clear()
        await _close_context(s)
        r = await call("browser_observe_page", {})
        assert not r["success"]
        assert switches == [], switches  # 자동 복구가 창을 바꾸지 않았다
        assert "browser_recovered" not in str(r["data"].get("window", {}))


# ------------------------------------------------------------------ (5) 복구 실패


@requires_chromium
async def test_recovery_failure_is_page_crashed_without_retry_storm(site, monkeypatch):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/camp"})
        core = s._core
        calls = {"n": 0}

        async def broken(*, headless=None):
            calls["n"] += 1
            raise RuntimeError("launch failed")

        monkeypatch.setattr(core, "open_persistent", broken)
        await _close_context(s)
        r = await call("browser_navigate", {"url": site.url + "/x"})
        assert not r["success"]
        assert r["error_code"] == ErrorCode.PAGE_CRASHED.value, r
        assert "serve 재시작" in r["error_message"]
        assert calls["n"] == 1
        r2 = await call("browser_observe_page", {})
        assert r2["error_code"] == ErrorCode.PAGE_CRASHED.value
        assert calls["n"] == 2  # 호출당 재오픈 시도 1회
        assert s._window.state == "failed"


# ------------------------------------------------------------------ (3) headed 창 — 에이전트 닫기 ≠ 사람 닫기


@requires_chromium
@requires_display
async def test_agent_closing_last_tab_in_window_is_not_human_close(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/camp"})
        r = await call("browser_control_request", {"reason": "확인"})
        assert r["data"]["window"]["state"] == "headed", r
        released = []
        real = s.hub.release_by_server

        def spy():
            released.append(1)
            return real()

        s.hub.release_by_server = spy
        closed = await call("browser_tab_control", {"command": "close", "tab_id": s._core.active_tab_id})
        assert closed["success"], closed
        for _ in range(5):
            await s._watch_window()
            await asyncio.sleep(0.1)
        assert released == []
        assert (s._window.notice or {}).get("reason") != "window_closed"
        assert s._window.state == "headed"
        obs = await call("browser_observe_page", {})
        assert obs["success"], obs


@requires_chromium
@requires_display
async def test_agent_close_marker_blocks_misjudgement_even_if_tabs_vanish(site):
    """C2 가 없더라도(탭이 모두 사라져도) 에이전트 닫기 표시가 있으면 사람 닫기로 판정하지 않는다."""
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/camp"})
        await call("browser_control_request", {"reason": "확인"})
        released = []
        real = s.hub.release_by_server
        s.hub.release_by_server = lambda: (released.append(1), real())[1]
        core = s._core
        tab = core.tabs()[0]
        await core.close_tab(tab.tab_id)  # 디스패처의 빈 탭 교체 없이 코어로 직접 — 탭 0
        await _wait_until(lambda: not core.tabs())
        for _ in range(3):
            await s._watch_window()
            await asyncio.sleep(0.1)
        assert released == []
        assert (s._window.notice or {}).get("reason") != "window_closed"
        obs = await call("browser_observe_page", {})  # 다음 호출이 복구
        assert obs["success"], obs
        assert obs["data"]["window"]["notice"]["reason"] == "browser_recovered"
