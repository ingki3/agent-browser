"""WS-69 R2 — navigate 부분 성공 판정: 문서 커밋 계수·마지막 await 뒤 동기 확인·시간 예산 (로컬만).

검증(R1 재검증 FAIL):
- B1-a: 최초 문서 DCL 에서 곧바로 parser-blocking 둘째 문서로 이동하면, R1 의 getFrameTree 표본이 둘째 문서를 기준으로
  잡아 ready=loading 인데 success=true(무주입 12회 중 7회).
- B1-b: 마지막 신원 판독 await 동안 오류 문서가 커밋되면 success=true + chrome-error://.
- B2: 신원 판독에 상한이 없어 timeout_ms=200 인데 800ms·무한 대기.

R2 설계: goto 전에 주 프레임 문서 커밋(새 문서만 — pushState·hash 제외)을 세고, 2단계 시간 초과의 부분 성공은
커밋 정확히 1회 + 마지막 await 뒤 동기적으로 오류 문서 아님 + readyState ∈ {interactive, complete}(남은 예산 안에서 읽음).
"""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from contracts import ActionType, ErrorCode


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

POLL = ("<!doctype html><meta charset=utf-8><title>폴링</title><h1 id=t>본문 열림</h1>"
        "<script>setInterval(()=>fetch('/ping?'+Math.random()).catch(()=>0),150)</script>")
HANG_IMG = "<!doctype html><meta charset=utf-8><title>이미지</title><p id=t>이미지 대기</p><img src='/hang'>"


def _dcl_hop(delay: int) -> str:
    """DCL 핸들러에서 즉시(delay<0) 또는 delay ms 뒤 parser-blocking 둘째 문서로 이동 — 검증 unhooked.py 그대로."""
    go = "location.href='/second'" if delay < 0 else f"setTimeout(()=>location.href='/second',{delay})"
    return ("<!doctype html><meta charset=utf-8><h1>FIRST</h1><img src='/hang'>"
            f"<script>document.addEventListener('DOMContentLoaded',()=>{{{go}}})</script>")


@pytest.fixture
def server():
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str) -> None:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _redirect(self, to: str) -> None:
            try:
                self.send_response(302)
                self.send_header("Location", to)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/hang":
                stop.wait(20)
            elif u.path == "/first":
                self._send(_dcl_hop(int(u.query or "-1")))
            elif u.path == "/second":
                self._send("<!doctype html><h1>SECOND_PARSER_BLOCKED</h1><script src='/hang'></script>")
            elif u.path == "/r1":
                self._redirect("/r2")
            elif u.path == "/r2":
                self._redirect("/poll")
            elif u.path == "/poll":
                self._send(POLL)
            elif u.path == "/img":
                self._send(HANG_IMG)
            else:
                self._send("ok")

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.block_on_close = False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()


def _dispatcher(page):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    return ActionDispatcher(DispatchContext(page=page, engine=PerceptionEngine()))


async def _nav(d, url, wait_until="load", timeout_ms=300):
    t0 = time.monotonic()
    # 바깥 상한 — 예산 강제가 빠지면 무한 대기 대신 이 시험이 실패한다.
    r = await asyncio.wait_for(
        d.dispatch(ActionType.NAVIGATE, {"url": url, "wait_until": wait_until, "timeout_ms": timeout_ms}), 5.0)
    return r, time.monotonic() - t0


# -- B1-a: DCL 직후 다른 문서로 이동(무주입) — 커밋 2회라 보수 실패 ---------------------------------


async def test_dcl_immediate_hop_never_partial_success_unhooked(server):
    """검증 unhooked.py: delay -1·0·1·2 × 3회 = 12회 모두 실패(R1 은 7회 거짓 성공, ready=loading)."""
    from playwright.async_api import async_playwright

    rows = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            for delay in (-1, 0, 1, 2):
                for _ in range(3):
                    page = await b.new_page()
                    r, _elapsed = await _nav(_dispatcher(page), f"{server}/first?{delay}", "load", 300)
                    ready = await page.evaluate("document.readyState")
                    rows.append((delay, r.success, r.error_code, ready, (r.data or {}).get("load_state")))
                    await page.close()
        finally:
            await b.close()
    assert len(rows) == 12
    bad = [row for row in rows if row[1] or row[2] is not ErrorCode.NAVIGATE_TIMEOUT or row[4] is not None]
    assert bad == [], bad


# -- B1-b: 마지막 await 동안 오류 문서 커밋 — 그 뒤 동기 확인으로 실패 ------------------------------


# WS-72: 주입(판독 + 오류 문서 커밋 대기)이 판독 예산 안에 끝나야 시험 전제가 선다. 판독 예산은 남은 timeout +
# _NAV_READ_GRACE_S(50ms)인데, 부하에서 주입만 33~48ms 가 걸려 예산이 주입을 취소했다(계측 30회 중 3회: 값 None,
# 그중 1회는 커밋 전 취소 → 전제 실패 #181). 이 시험의 대상은 '마지막 await 뒤 동기 확인'이지 판독 예산이 아니다
# (예산은 B2 시험이 따로 잰다). 그래서 이 두 시험에서만 grace 를 주입 상한보다 넉넉히 둔다 — 제품 값은 그대로다.
_INJECT_GRACE_S = 3.0
_COMMIT_WAIT_S = 2.0  # 오류 문서 커밋 대기 상한(시각 기준 — 부하에서 sleep 횟수 기준 상한은 짧아진다)


async def _error_commits_during_last_await(d, page, monkeypatch):
    """판독(마지막 await) 도중 실제 오류 문서를 커밋시키고 그 커밋까지 붙잡는다 — 검증 final_race.py 의 경계 주입.

    판독 값은 커밋 전 최초 문서의 것(interactive)이라, 판독 뒤 동기 확인이 없으면 부분 성공으로 새어 나간다."""
    import actions.dispatcher as dispatcher_mod

    monkeypatch.setattr(dispatcher_mod, "_NAV_READ_GRACE_S", _INJECT_GRACE_S)
    real = d._main_ready_state
    seen = {"calls": 0, "value": None, "committed": False, "returned": False}

    async def slow(p):
        seen["calls"] += 1
        value = await real(p)
        seen["value"] = value
        await page.evaluate("location.href='http://127.0.0.1:1/'")
        deadline = time.monotonic() + _COMMIT_WAIT_S
        while not page.url.startswith("chrome-error://") and time.monotonic() < deadline:
            await asyncio.sleep(0.002)
        seen["committed"] = page.url.startswith("chrome-error://")
        seen["returned"] = True  # 예산에 취소되지 않고 판독 값을 돌려줬다
        return value

    d._main_ready_state = slow
    return seen


def _premise(seen, final, r) -> None:
    """전제: 주입이 마지막 await 에 1회 걸렸고, 판독이 문서 값(interactive/complete)을 돌려줬으며, 그 await 안에서
    오류 문서가 커밋됐다. 하나라도 아니면 제품 판정을 단정하지 않고 '전제 미성립'으로 실패한다(제품 결과 동봉)."""
    ok = (seen["calls"] == 1 and seen["returned"] and seen["value"] in ("interactive", "complete")
          and seen["committed"] and final.startswith("chrome-error://"))
    assert ok, f"전제 미성립: seen={seen} final={final[:40]} 제품 결과 success={r.success} " \
               f"error_code={r.error_code} data={r.data}"


async def test_error_document_committed_during_last_await_is_failure_direct(server, monkeypatch):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            d = _dispatcher(page)
            seen = await _error_commits_during_last_await(d, page, monkeypatch)
            r, _ = await _nav(d, server + "/img", "load", 300)
            final = page.url
        finally:
            await b.close()
    _premise(seen, final, r)  # 주입이 실제로 마지막 await 에 걸렸고 그 안에서 오류 문서가 커밋됐다
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in r.data
    assert r.data["nav_failure"]["kind"] == "other"


async def test_error_document_committed_during_last_await_is_failure_serve(server, tmp_path, monkeypatch):
    from interface.mcp_server import BrowserMCPServer

    s = BrowserMCPServer(recipes=False, handoff_root=tmp_path / "hub")
    await s.start()
    try:
        page = s._page
        seen = await _error_commits_during_last_await(s._dispatcher, page, monkeypatch)
        r = await asyncio.wait_for(s.call_tool(
            "browser_navigate", {"url": server + "/img", "wait_until": "load", "timeout_ms": 300}), 5.0)
        final = page.url
    finally:
        await s.close()
    _premise(seen, final, r)
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT


async def test_injection_cancelled_by_real_read_budget_is_conservative_failure(server):
    """WS-72 (#181): 제품 grace 그대로(50ms) — 판독이 예산을 넘겨 취소되는 동안 오류 문서가 커밋돼도 부분 성공이 아니라
    보수 실패다(위 두 시험이 grace 를 늘린 것이 취소 경로의 거짓 성공을 가리지 않음을 따로 증명)."""
    from playwright.async_api import async_playwright

    import actions.dispatcher as dispatcher_mod

    assert dispatcher_mod._NAV_READ_GRACE_S == 0.05  # 제품 값 그대로
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            d = _dispatcher(page)
            real = d._main_ready_state
            seen = {"value": None, "cancelled": False}

            async def slow_past_budget(p):
                try:
                    value = await real(p)
                    seen["value"] = value
                    await page.evaluate("location.href='http://127.0.0.1:1/'")
                    await asyncio.sleep(1.0)  # 예산(남은 timeout + 50ms)을 확실히 넘긴다
                except asyncio.CancelledError:
                    seen["cancelled"] = True
                    raise
                return value

            d._main_ready_state = slow_past_budget
            r, _ = await _nav(d, server + "/img", "load", 300)
        finally:
            await b.close()
    assert seen["cancelled"], seen  # 전제: 판독 예산이 주입을 취소했다(부하에서 판독 값 전 취소여도 같은 경로)
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in r.data


# -- B2: 추가 판독은 남은 deadline 안에서만 — 넘기면 보수 실패, 반환 ≤ timeout_ms + 100ms ------------


@pytest.mark.parametrize("mode", ["delay", "hang", "raise"])
@pytest.mark.parametrize("path,wait_until", [("/img", "load"), ("/poll", "networkidle")])
async def test_ready_state_read_is_bounded_by_budget(server, mode, path, wait_until):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            d = _dispatcher(page)

            async def bad_read(p):
                if mode == "raise":
                    raise RuntimeError("synthetic read failure")
                await (asyncio.sleep(0.8) if mode == "delay" else asyncio.Event().wait())
                return "complete"

            d._main_ready_state = bad_read
            r, elapsed = await _nav(d, server + path, wait_until, 200)
        finally:
            await b.close()
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in r.data
    assert elapsed < 0.2 + 0.1, elapsed


@pytest.mark.parametrize("path,wait_until", [("/img", "load"), ("/poll", "networkidle")])
async def test_partial_success_returns_within_budget(server, path, wait_until):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            r, elapsed = await _nav(_dispatcher(page), server + path, wait_until, 400)
        finally:
            await b.close()
    assert r.success is True, (r.error_message, r.data)
    assert r.data["load_state"]["timed_out"] is True
    assert elapsed < 0.4 + 0.1, elapsed


@pytest.mark.parametrize("ready", ["loading", None])
async def test_ready_state_not_interactive_is_failure(server, ready):
    """(d) readyState 가 interactive/complete 가 아니면(또는 못 읽으면) 부분 성공 아님."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            d = _dispatcher(page)

            async def fake_read(p):
                return ready

            d._main_ready_state = fake_read
            r, _ = await _nav(d, server + "/img", "load", 300)
        finally:
            await b.close()
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT


# -- 유지: 서버 리다이렉트(커밋 1회) 뒤 폴링은 부분 성공 -------------------------------------------


async def test_server_redirect_chain_then_polling_is_partial_success(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            r, _ = await _nav(_dispatcher(page), server + "/r1", "networkidle", 1200)
        finally:
            await b.close()
    assert r.success is True, (r.error_message, r.data)
    assert r.data["url"].endswith("/poll")
    assert r.data["load_state"] == {"requested": "networkidle", "reached": "domcontentloaded", "timed_out": True}


async def test_commit_listener_removed_after_navigate(server):
    """리스너는 finally 에서 해제 — 이동을 거듭해도 쌓이지 않는다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            emitter = page.main_frame._impl_obj._event_emitter
            before = len(emitter.listeners("navigated"))
            d = _dispatcher(page)
            for path, wait in (("/poll", "networkidle"), ("/img", "load"), ("/plain", "load")):
                await _nav(d, server + path, wait, 300)
            after = len(emitter.listeners("navigated"))
        finally:
            await b.close()
    assert after == before


# -- Playwright 내부 이벤트 의존 고정: 연결 단정 + 없을 때 보수 실패 --------------------------------


async def test_internal_navigated_event_is_wired(server):
    """주 프레임 내부 'navigated' 이벤트가 실제로 연결돼 새 문서만 센다 — Playwright 업그레이드로 모양이 바뀌면
    여기서 깨진다(조용히 부분 성공이 전부 꺼지는 것을 막는다)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            d = _dispatcher(page)
            state = d._watch_document_commits(page)
            assert state["count"] == 0  # 내부 API 가 있다
            await page.goto(server + "/r1", wait_until="domcontentloaded")  # 302 ×2 → /poll
            after_redirects = state["count"]
            await page.evaluate("history.pushState({}, '', '/poll/next'); location.hash = '#a'")
            await asyncio.sleep(0.1)
            after_same_doc = state["count"]
            await page.goto(server + "/plain", wait_until="domcontentloaded")
            after_second = state["count"]
            d._unwatch_document_commits(state)
        finally:
            await b.close()
    assert (after_redirects, after_same_doc, after_second) == (1, 1, 2)


class _NoInternalEventsPage:
    """공개 API 는 그대로, Playwright 내부 프레임 이벤트(_impl_obj)만 없는 페이지 — 내부 API 가 사라진 경우의 대역."""

    def __init__(self, page):
        self._page = page

    @property
    def main_frame(self):
        return object()

    def __getattr__(self, name):
        return getattr(self._page, name)


async def test_missing_internal_event_api_is_conservative_failure(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            proxy = _NoInternalEventsPage(page)
            r, _ = await _nav(_dispatcher(proxy), server + "/poll", "networkidle", 600)
            ok, _ = await _nav(_dispatcher(proxy), server + "/plain", "load", 3000)  # 완료 경로는 영향 없음
        finally:
            await b.close()
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in r.data
    assert ok.success is True and "load_state" not in ok.data
