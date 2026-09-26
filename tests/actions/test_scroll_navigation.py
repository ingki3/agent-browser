"""SCROLL 이 문서 전환 중에도 견디는지 (WS-24 F1).

실측(2026-09-25 G마켓 user-chrome): 검색 Enter 직후 scroll 이 `E_PAGE_CRASHED` 로
실패했다(원 실행의 예외 메시지는 남지 않았다). 로컬에서 같은 SCROLL 경로의 실패
두 가지를 재현했다:
  1) 폼 제출 내비게이션 중 → "Page.evaluate: Execution context was destroyed,
     most likely because of a navigation"
  2) 새 문서의 head 가 아직 오는 중(document.body === null) →
     "TypeError: Cannot read properties of null (reading 'scrollHeight')"
이 두 경우만 처리하고, 그 밖의 예외는 여전히 E_PAGE_CRASHED 여야 한다.
로컬 서버만 쓴다(외부 요청 없음).
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


requires_chromium = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

PAGE = (b"<!doctype html><html><body><form action='/next'><input name=q></form>"
        + b"<p>item</p>" * 300 + b"</body></html>")


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if self.path.startswith("/next"):
                # 검색 결과처럼 응답이 조금 늦다 — 그 사이 스크롤이 들어온다.
                time.sleep(0.3)
                self.wfile.write(PAGE)
            elif self.path.startswith("/slowhead"):
                # head 까지만 보내고 body 는 늦게 — document.body 가 null 인 구간.
                self.wfile.write(b"<!doctype html><html><head><title>r</title>" + b" " * 4096)
                self.wfile.flush()
                time.sleep(1.5)
                self.wfile.write(b"</head><body>" + b"<p>item</p>" * 300 + b"</body></html>")
            else:
                self.wfile.write(PAGE)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _dispatcher(page):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    return ActionDispatcher(DispatchContext(page=page, engine=PerceptionEngine()))


@requires_chromium
async def test_scroll_during_form_navigation_succeeds_and_asks_reobserve(server):
    """스크롤 중 문서가 바뀌면(컨텍스트 파괴) 로드 후 1회 재시도 → 성공 + 재관찰."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await (await b.new_context()).new_page()
        await page.goto(server + "/")
        d = _dispatcher(page)
        await page.evaluate("document.forms[0].submit()")
        r = await d.dispatch(ActionType.SCROLL, {"direction": "down", "distance": 500})
        url = page.url
        await b.close()
    assert r.success is True, r.error_message
    assert r.error_code is None
    assert r.reobserve_required is True
    assert "/next" in url


@requires_chromium
async def test_scroll_while_body_is_null_succeeds(server):
    """새 문서의 body 가 아직 없어도(head 스트리밍 중) 스크롤이 TypeError 로 죽지 않는다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await (await b.new_context()).new_page()
        await page.goto(server + "/slowhead", wait_until="commit")
        assert await page.evaluate("document.body === null"), "fixture: body 가 없어야 한다"
        d = _dispatcher(page)
        r = await d.dispatch(ActionType.SCROLL, {"direction": "down", "distance": 500})
        await b.close()
    assert r.success is True, r.error_message
    assert r.error_code is None


class _FakePage:
    """evaluate 가 정해진 예외를 던지는 가짜 페이지(브라우저 없음)."""

    url = "http://fake.test/"

    def __init__(self, exc_factory, fail_times=99):
        self.exc_factory = exc_factory
        self.fail_times = fail_times
        self.evaluates = 0
        self.load_waits = 0

    async def evaluate(self, js):
        self.evaluates += 1
        if self.evaluates <= self.fail_times:
            raise self.exc_factory()
        return 1000

    async def wait_for_timeout(self, ms):
        return None

    async def wait_for_load_state(self, state="load", timeout=None):
        self.load_waits += 1


async def test_scroll_other_exception_is_still_page_crashed():
    """확정된 두 원인 밖의 예외는 넓게 삼키지 않는다 — 그대로 E_PAGE_CRASHED."""
    page = _FakePage(lambda: RuntimeError("Target page, context or browser has been closed"))
    r = await _dispatcher(page).dispatch(ActionType.SCROLL, {"direction": "down"})
    assert r.success is False
    assert r.error_code is ErrorCode.PAGE_CRASHED
    assert "has been closed" in (r.error_message or "")
    assert page.load_waits == 0, "다른 예외에서는 재시도 대기를 하지 않는다"


async def test_scroll_context_destroyed_twice_fails():
    """재시도도 컨텍스트 파괴면 기존대로 실패(E_PAGE_CRASHED) — 재시도는 1회뿐."""
    page = _FakePage(lambda: RuntimeError(
        "Page.evaluate: Execution context was destroyed, most likely because of a navigation"))
    r = await _dispatcher(page).dispatch(ActionType.SCROLL, {"direction": "down"})
    assert r.success is False
    assert r.error_code is ErrorCode.PAGE_CRASHED
    assert page.load_waits == 1


async def test_scroll_context_destroyed_once_retries():
    page = _FakePage(lambda: RuntimeError(
        "Execution context was destroyed, most likely because of a navigation"), fail_times=1)
    r = await _dispatcher(page).dispatch(ActionType.SCROLL, {"direction": "up", "distance": 300})
    assert r.success is True and r.reobserve_required is True
    assert r.data.get("scrolled") == -300
    assert page.load_waits == 1
