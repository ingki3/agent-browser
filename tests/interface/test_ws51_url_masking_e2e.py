"""WS-51 E2E — 에이전트에게 나가는 응답의 주소 칸에 비밀 값이 실리지 않는다 (실 Chromium + 로컬 페이지).

실사용(2026-10): 대기열 JWT(`?gateToken=eyJ…`)가 붙은 예약 페이지를 보던 봇의 MCP 결과에 토큰 원문이 657번
실렸다. 여기서는 합성 JWT 로 같은 모양의 로컬 페이지(`/camp?gateToken=<JWT>&date=2026-10-16`, 토큰이 맞아야
200)를 열고, MCP 가 실제로 보내는 직렬화 문자열(_call_tool_impl 과 같은 envelope_json / json.dumps)을 모두
모아 토큰 원문이 0회인지, `date=2026-10-16` 은 남는지 본다. 서버 stderr 도 같은 검사.

* 내부 복원(브라우저 kill·마지막 탭 사라짐 → 다음 호출 복구)은 **원문** 주소로 — 토큰이 있어야 200.
* 가린 주소를 navigate·tab create 에 넘기면 E_INVALID_URL, 로컬 서버 접근 0건.
* extract 결과의 href 는 원문(페이지 내용 — 의도된 경계).
공개 사이트 없음. 프로필·인계 상태는 conftest 가 임시 디렉터리로 돌린다.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlsplit

import pytest

from contracts import ErrorCode
from interface.mcp_server import BrowserMCPServer, envelope_json

from test_run_cli import requires_chromium

requires_display = pytest.mark.skipif(
    sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
    reason="창(headed) 테스트: 화면 없음",
)

#: 합성 JWT — 실제 토큰 아님(본문 {"sub":"SYNTH_NOT_A_SECRET"}).
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJTWU5USF9OT1RfQV9TRUNSRVQifQ.U1lOVEhfTk9UX0FfU0VDUkVUX1NJRw"
DATE = "date=2026-10-16"

OK_PAGE = """<!doctype html><meta charset=utf-8><title>camp-ok</title>
<p id=t>예약 대기 통과</p><button>예약 확인</button>
<a id=next href="/camp?gateToken={jwt}&date=2026-10-17">다음 날짜</a>"""
DENIED = """<!doctype html><meta charset=utf-8><title>denied</title><p>대기열 토큰 없음</p>"""


@pytest.fixture
def site():
    hits: List[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            parts = urlsplit(self.path)
            ok = parts.path == "/camp" and parse_qs(parts.query).get("gateToken") == [JWT]
            body = (OK_PAGE.format(jwt=JWT) if ok else DENIED).encode("utf-8")
            self.send_response(200 if ok else 403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield type("Site", (), {"url": base, "camp": f"{base}/camp?gateToken={JWT}&{DATE}", "hits": hits})
    finally:
        srv.shutdown()
        srv.server_close()


class Wire:
    """MCP 서버가 클라이언트에게 보내는 텍스트(_call_tool_impl 과 같은 직렬화)를 모은다."""

    def __init__(self, s: BrowserMCPServer) -> None:
        self.s = s
        self.sent: List[str] = []

    async def __call__(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name.startswith("browser_control") or name.startswith("browser_approval"):
            text = json.dumps(await self.s.call_server_tool(name, args), ensure_ascii=False,
                              separators=(",", ":"))
        else:
            text = envelope_json(await self.s.call_tool(name, args))
        self.sent.append(text)
        return json.loads(text)

    def assert_no_secret(self) -> None:
        leaks = [t[:300] for t in self.sent if JWT in t]
        assert not leaks, f"토큰 원문이 응답 {len(leaks)}건에 실림: {leaks}"


def _browser_pids(path) -> List[int]:
    out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True)
    needle = f"--user-data-dir={path}"
    return [int(line.strip().split(" ", 1)[0]) for line in out.stdout.splitlines()
            if needle in line and "--type=" not in line]


async def _wait_until(pred, timeout: float = 15.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _title(s: BrowserMCPServer) -> str:
    return await s._page.title()


# ------------------------------------------------------------------ on-demand headless: 응답 전수 + 브라우저 kill 복구


@requires_chromium
async def test_on_demand_responses_never_carry_token_and_recovery_uses_raw_url(site, capfd):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = Wire(s)
        nav = await call("browser_navigate", {"url": site.camp})
        assert nav["success"], nav
        assert await _title(s) == "camp-ok"  # 실제 이동은 원문 주소
        assert DATE in nav["current_url"] and DATE in nav["data"]["url"]
        assert "gateToken=[REDACTED]" in nav["current_url"]
        obs = await call("browser_observe_page", {})
        assert obs["success"], obs
        assert DATE in obs["data"]["observation"]["url"] and "gateToken=[REDACTED]" in obs["data"]["observation"]["url"]
        listed = await call("browser_tab_control", {"command": "list"})
        first = listed["data"]["tabs"][0]["tab_id"]
        assert DATE in listed["data"]["tabs"][0]["url"]
        created = await call("browser_tab_control", {"command": "create", "url": site.camp})
        assert created["success"], created
        assert DATE in created["data"]["url"]
        switched = await call("browser_tab_control", {"command": "switch", "tab_id": first})
        assert switched["success"], switched
        await call("browser_reload", {})
        status = await call("browser_control_status", {})
        assert status["success"], status
        # 오류 문구에 페이지 주소가 들어가는 경로(닫힌 포트로 이동)
        bad = f"http://127.0.0.1:{_closed_port()}/camp?gateToken={JWT}&{DATE}"
        await call("browser_navigate", {"url": bad})
        back = await call("browser_navigate", {"url": site.camp})
        assert back["success"], back

        # 브라우저 프로세스 kill → 다음 호출이 원문 주소로 복원(토큰이 있어야 200)
        before = len(site.hits)
        for pid in _browser_pids(s._core.persistent_profile):
            os.kill(pid, signal.SIGKILL)
        assert await _wait_until(lambda: s._ctx_closed or not s._core.tabs())
        rec = await call("browser_observe_page", {})
        assert rec["success"], rec
        win = rec["data"]["window"]
        assert win["notice"]["reason"] == "browser_recovered"
        assert any(DATE in t["url"] for t in win.get("tabs") or []), win
        assert JWT in s._page.url  # 내부는 원문
        assert await _title(s) == "camp-ok"
        assert any(JWT in h for h in site.hits[before:])  # 복원 요청에 토큰 원문이 실림(200)
        status2 = await call("browser_control_status", {})
        assert status2["success"]
        assert DATE in json.dumps(status2, ensure_ascii=False) or "window" in status2["data"]

    call.assert_no_secret()
    err = capfd.readouterr().err
    assert JWT not in err, "서버 stderr 에 토큰 원문"


@requires_chromium
async def test_vanished_last_tab_recovers_with_raw_url(site):
    """코어에서 마지막 탭이 사라짐(디스패처의 빈 탭 교체 없이) → 다음 호출이 원문 주소로 복원(200)."""
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = Wire(s)
        assert (await call("browser_navigate", {"url": site.camp}))["success"]
        core = s._core
        await core.close_tab(core.tabs()[0].tab_id)
        assert await _wait_until(lambda: not core.tabs())
        obs = await call("browser_observe_page", {})
        assert obs["success"], obs
        assert obs["data"]["window"]["notice"]["reason"] == "browser_recovered"
        assert JWT in s._page.url and await _title(s) == "camp-ok"
    call.assert_no_secret()


# ------------------------------------------------------------------ headed 창 — 사람이 창을 닫음(window_closed)


@requires_chromium
@requires_display
async def test_window_closed_notice_never_carries_token(site, capfd):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = Wire(s)
        assert (await call("browser_navigate", {"url": site.camp}))["success"]
        req = await call("browser_control_request", {"reason": "확인"})
        assert req["data"]["window"]["state"] == "headed", req
        await s._watch_window()  # 마지막 탭 URL 기록
        await call("browser_control_status", {})
        for pid in _browser_pids(s._core.persistent_profile):
            os.kill(pid, signal.SIGKILL)
        assert await _wait_until(lambda: s._ctx_closed or not s._core.tabs())
        for _ in range(50):
            await s._watch_window()
            if (s._window.notice or {}).get("reason") == "window_closed":
                break
            await asyncio.sleep(0.1)
        assert (s._window.notice or {}).get("reason") == "window_closed"
        assert await _wait_until(lambda: s._window.state == "headless", 30)
        waited = await call("browser_control_wait", {"timeout_s": 1})
        assert waited["success"], waited
        obs = await call("browser_observe_page", {})
        assert obs["success"], obs
        text = json.dumps(waited, ensure_ascii=False) + json.dumps(obs, ensure_ascii=False)
        assert "window_closed" in text
        assert DATE in text
        assert JWT in s._page.url and await _title(s) == "camp-ok"
    call.assert_no_secret()
    assert JWT not in capfd.readouterr().err


# ------------------------------------------------------------------ 가린 주소로 이동 거부


@requires_chromium
@pytest.mark.parametrize("mode", ["headless", "on-demand"])
async def test_masked_url_navigate_is_refused_without_request(site, mode):
    async with BrowserMCPServer(browser_mode=mode) as s:
        call = Wire(s)
        for masked in (f"{site.url}/camp?gateToken=[REDACTED]&{DATE}",
                       f"{site.url}/camp?gateToken=%5BREDACTED%5D&{DATE}"):
            r = await call("browser_navigate", {"url": masked})
            assert not r["success"]
            assert r["error_code"] == ErrorCode.INVALID_URL.value, r
            assert "가려진 주소" in r["error_message"]
        t = await call("browser_tab_control", {"command": "create", "url": f"{site.url}/camp?sid=[REDACTED]"})
        assert t["error_code"] == ErrorCode.INVALID_URL.value, t
        assert site.hits == [], site.hits
        # 돌려받은 주소를 그대로 다시 넘겨도 같은 거부
        nav = await call("browser_navigate", {"url": site.camp})
        assert nav["success"]
        hits = len(site.hits)
        again = await call("browser_navigate", {"url": nav["current_url"]})
        assert again["error_code"] == ErrorCode.INVALID_URL.value, again
        assert len(site.hits) == hits
        assert JWT in s._page.url  # 거부는 현재 페이지를 바꾸지 않는다
    call.assert_no_secret()


# ------------------------------------------------------------------ 의도된 경계: extract 의 href 는 원문


@requires_chromium
async def test_extract_href_keeps_raw_url(site):
    async with BrowserMCPServer() as s:
        call = Wire(s)
        assert (await call("browser_navigate", {"url": site.camp}))["success"]
        ex = await call("browser_extract", {"selector": "#next", "attributes": ["href"]})
        assert ex["success"], ex
        assert JWT in json.dumps(ex["data"]["items"], ensure_ascii=False)
        assert JWT not in ex["current_url"]
