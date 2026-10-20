"""WS-57 R2 — 재검증 FAIL B1·B2 를 저장소 테스트로 고정(실 Chromium, 로컬 http·합성 값만).

B1 같은 작업 공간의 동시 액션이 디스패처 인스턴스 임시 상태(_dialogs 등)를 나눠 써, 아무 효과 없는 클릭이 형제
호출의 alert 를 자기 효과로 빌려 성공·레시피 궤적에 들어갔다 → 같은 작업 공간 dispatch 는 작업 공간 락으로 직렬화.
B2 egress 차단 기록을 전역 누적 차분으로 읽어(출처 없는 new[-1] fallback 포함) 다른 작업 공간이 막힌 호스트가 내
navigate 오류로 복사됐다 → 기록을 요청의 Page(탭)→작업 공간에 귀속하고 그 호출 작업 공간 탭 기록만 본다.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import quote, unquote

import pytest

from interface.mcp_server import BrowserMCPServer, envelope_dict

from test_run_cli import requires_chromium

PAGE = b"""<title>local call attribution</title>
<button id="noop" onclick="fetch('/signal-a')">plain noop</button>
<button id="dialog" onclick="alert('OTHER_CALL_DIALOG')">show notice</button>"""

#: 문서용 예약 주소(TEST-NET-1) — egress 가 사설/예약 대역으로 막아 실제 접속 전에 끊는다.
RESERVED = "192.0.2.123"


@pytest.fixture
def site():
    signals: Dict[str, threading.Event] = {"a": threading.Event(), "slow": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            ctype = "text/html; charset=utf-8"
            if self.path.startswith("/signal-a"):
                signals["a"].set()
                body = b"ok"
            elif self.path.startswith("/slow"):
                signals["slow"].set()
                time.sleep(2.0)
                body = b"<title>slow</title><p>chat only</p>"
            elif self.path.startswith("/redir?to="):
                self.send_response(302)
                self.send_header("Location", unquote(self.path.split("=", 1)[1]))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif self.path.startswith("/sw.js?t="):
                target = json.dumps(unquote(self.path.split("=", 1)[1]))
                body = ("self.addEventListener('install', e => { e.waitUntil(fetch(%s).catch(()=>0));"
                        " self.skipWaiting(); });" % target).encode()
                ctype = "text/javascript"
            elif self.path.startswith("/swpage?t="):
                body = ("<title>sw</title><script>navigator.serviceWorker.register('/sw.js?t=%s')</script>"
                        % self.path.split("=", 1)[1]).encode()
            elif self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
            else:
                body = PAGE
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):  # noqa: D401
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    srv.port = srv.server_address[1]
    srv.signals = signals
    yield srv
    srv.shutdown()


async def call(s: BrowserMCPServer, name: str, args: Dict[str, Any] = None, ws: str = None) -> Dict[str, Any]:
    args = dict(args or {})
    if ws is not None:
        args["workspace"] = ws
    return envelope_dict(await s.call_tool("browser_" + name, args))


async def _ids(s: BrowserMCPServer, ws: str = None) -> Dict[str, Any]:
    obs = await call(s, "observe_page", {}, ws)
    els = obs["data"]["observation"]["elements"]
    return {e["name"]: {"element_id": e["element_id"], "epoch": obs["snapshot_epoch"]} for e in els}


# ---------------------------------------------------------------------------------------------- B1


@requires_chromium
async def test_b1_same_workspace_noop_click_does_not_borrow_sibling_dialog(site):
    """A(무효 클릭)의 사후조건 대기 중 같은 작업 공간 B 가 alert 를 열어도 A 는 실패·궤적 밖(제품 대체 없음)."""
    async with BrowserMCPServer(recipes=True) as s:
        assert (await call(s, "navigate", {"url": site.url + "/local"}))["success"]
        ids = await _ids(s)
        solo = await call(s, "click", ids["plain noop"])
        assert not solo["success"], solo  # 단독 대조: 무효 클릭은 실패
        ids = await _ids(s)
        site.signals["a"].clear()
        ta = asyncio.create_task(call(s, "click", ids["plain noop"]))
        assert await asyncio.to_thread(site.signals["a"].wait, 10)  # A 의 클릭이 실제로 실행됐다
        rb = await call(s, "click", ids["show notice"])
        ra = await ta
        assert rb["success"] and rb["data"]["dialogs"][0]["message"] == "OTHER_CALL_DIALOG", rb
        assert not ra["success"], ra
        assert "OTHER_CALL_DIALOG" not in json.dumps(ra, ensure_ascii=False), ra
        assert not any(str(x).startswith("dialog_opened") for x in ra["data"].get("signals", []) or []), ra
        steps = [e for e in s._recipes.trajectory.entries if e.get("action") == "click"]
        assert all("plain noop" not in json.dumps(e, ensure_ascii=False) for e in steps), steps


@requires_chromium
async def test_b1_other_workspaces_still_run_concurrently(site):
    """락은 작업 공간 단위 — default 의 긴 호출 중에도 watch 호출은 끝난다(전역 직렬화 아님)."""
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "navigate", {"url": site.url + "/local"})
        await call(s, "navigate", {"url": site.url + "/local"}, "watch")
        site.signals["slow"].clear()
        slow = asyncio.create_task(call(s, "navigate", {"url": site.url + "/slow", "timeout_ms": 8000}))
        assert await asyncio.to_thread(site.signals["slow"].wait, 10)
        t0 = time.monotonic()
        r = await call(s, "observe_page", {}, "watch")
        assert r["success"] and not slow.done(), r
        assert time.monotonic() - t0 < 1.5
        await slow


@requires_chromium
async def test_b1_cancelled_waiter_and_holder_leave_no_lock(site):
    """락을 기다리던 호출·쥔 호출이 취소돼도 다음 같은 작업 공간 호출이 막히지 않는다."""
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "navigate", {"url": site.url + "/local"})
        site.signals["slow"].clear()
        holder = asyncio.create_task(call(s, "navigate", {"url": site.url + "/slow", "timeout_ms": 8000}))
        assert await asyncio.to_thread(site.signals["slow"].wait, 10)
        waiter = asyncio.create_task(call(s, "observe_page", {}))
        await asyncio.sleep(0.1)
        assert not waiter.done()  # 같은 작업 공간이라 기다린다
        waiter.cancel()
        holder.cancel()
        for t in (waiter, holder):
            with pytest.raises(asyncio.CancelledError):
                await t
        # 취소된 이동은 브라우저 안에서 계속될 수 있다 — 다음 호출은 새 이동(관찰은 문맥 파기와 겹칠 수 있다).
        r = await asyncio.wait_for(call(s, "navigate", {"url": site.url + "/local"}), 10)
        assert r["success"], r


@requires_chromium
async def test_b1_queued_call_rechecks_human_control_after_waiting(site):
    """줄 서 있던 사이 사람이 조작권을 잡으면, 차례가 온 조작 호출은 누르지 않고 조작권 거부(기다리기 전 검사만 믿지 않음)."""
    from interface.handoff import write_command

    async with BrowserMCPServer(recipes=False) as s:
        s._human_can_see = lambda: True
        s.open_handoff()
        await call(s, "navigate", {"url": site.url + "/local"})
        site.signals["slow"].clear()
        holder = asyncio.create_task(call(s, "navigate", {"url": site.url + "/slow", "timeout_ms": 8000}))
        assert await asyncio.to_thread(site.signals["slow"].wait, 10)
        queued = asyncio.create_task(call(s, "click", {"selector": "#noop"}))
        await asyncio.sleep(0.1)
        assert not queued.done()
        write_command(s.hub.root, s.hub.server_id, "take")  # 사람이 조작권을 잡는다(줄 선 뒤)
        await holder
        r = await queued
        assert r["error_code"] == "E_HITL_UNATTENDED_BLOCKED" and r["data"]["control"]["holder"] != "agent", r


# ---------------------------------------------------------------------------------------------- B2


@requires_chromium
async def test_b2_other_workspace_block_does_not_rewrite_my_navigate_error(site):
    """default 이동이 느린 사이트에서 끝나기 전에 watch 가 막힌 예약 주소로 가도, default 오류는 자기 timeout 그대로."""
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "observe_page", {}, "watch")
        base = await call(s, "navigate", {"url": site.url + "/slow-baseline", "timeout_ms": 500})
        assert base["error_code"] == "E_NAVIGATE_TIMEOUT", base
        site.signals["slow"].clear()
        task = asyncio.create_task(call(s, "navigate", {"url": site.url + "/slow-race", "timeout_ms": 500}))
        assert await asyncio.to_thread(site.signals["slow"].wait, 10)
        watch = await call(s, "navigate", {"url": f"http://{RESERVED}/watch-private"}, "watch")
        chat = await task
        assert watch["error_code"] == "E_INVALID_URL" and watch["data"]["egress"]["host"] == RESERVED, watch
        assert chat["error_code"] == "E_NAVIGATE_TIMEOUT", chat
        assert RESERVED not in json.dumps(chat, ensure_ascii=False), chat
        assert "egress" not in chat.get("data", {}), chat


@requires_chromium
async def test_b2_block_records_are_attributed_to_the_requesting_tab(site):
    """route 차단 기록은 요청한 Page 에, 그 Page 는 주인 작업 공간 탭이다(전역 기록에서 남의 것을 고르지 않는다)."""
    async with BrowserMCPServer(recipes=False) as s:
        watch = await call(s, "navigate", {"url": f"http://{RESERVED}/w"}, "watch")
        assert watch["error_code"] == "E_INVALID_URL", watch
        rec = [d for d in s._egress.blocked_requests if d.host == RESERVED]
        assert rec and rec[-1].page is not None
        tab = s._core.tab_for_page(rec[-1].page)
        assert tab is not None and tab.workspace == "watch"
        # 같은 호스트로 default 가 직접 가면 자기 기록으로 자기 오류를 받는다(양성 대조).
        mine = await call(s, "navigate", {"url": f"http://{RESERVED}/d"})
        assert mine["error_code"] == "E_INVALID_URL" and mine["data"]["egress"]["host"] == RESERVED, mine


@requires_chromium
async def test_b2_proxy_redirect_hop_block_stays_with_its_workspace(site):
    """프록시가 막은 리다이렉트 홉(route 밖 — 기록에 탭이 없다)도 브라우저 응답 이벤트로 그 요청 탭에 귀속:
    watch 는 자기 차단 이유를 받고, 그 사이 timeout 난 default 오류에는 그 호스트가 없다."""
    local = f"http://localhost:{site.port}"
    async with BrowserMCPServer(recipes=False, allowed_domains=("localhost",)) as s:
        await call(s, "observe_page", {}, "watch")
        site.signals["slow"].clear()
        task = asyncio.create_task(call(s, "navigate", {"url": local + "/slow-race", "timeout_ms": 500}))
        assert await asyncio.to_thread(site.signals["slow"].wait, 10)
        hop = quote(f"http://127.0.0.1:{site.port}/hop-target", safe="")
        watch = await call(s, "navigate", {"url": f"{local}/redir?to={hop}", "timeout_ms": 8000}, "watch")
        chat = await task
        assert watch["success"] is False and watch["data"]["egress"]["host"] == "127.0.0.1", watch
        rec = [d for d in s._egress.blocked_requests if d.host == "127.0.0.1"]
        assert rec and s._core.tab_for_page(rec[-1].page).workspace == "watch"
        assert chat["error_code"] == "E_NAVIGATE_TIMEOUT", chat
        assert "egress" not in chat.get("data", {}) and "127.0.0.1" not in json.dumps(chat), chat


@requires_chromium
async def test_b2_service_worker_block_is_attributed_to_no_tab(site):
    """서비스 워커 요청의 차단 기록은 탭이 없다(귀속 불가) → 어느 호출 오류에도 붙지 않는다."""
    local = f"http://localhost:{site.port}"
    target = quote(f"http://127.0.0.1:{site.port}/sw-target", safe="")
    async with BrowserMCPServer(recipes=False, allowed_domains=("localhost",)) as s:
        r = await call(s, "navigate", {"url": f"{local}/swpage?t={target}"})
        assert r["success"], r
        for _ in range(30):
            rec = [d for d in s._egress.blocked_requests if d.host == "127.0.0.1"]
            if rec:
                break
            await asyncio.sleep(0.1)
        assert rec, "service worker fetch was not blocked/recorded"
        assert all(d.page is None for d in rec), rec
