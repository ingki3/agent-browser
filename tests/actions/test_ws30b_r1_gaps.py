"""WS-30b 항목 4(d): WS-30 R1 재검증 NB-R1-1 테스트 고정 공백 (프레임 효과 판정 쪽).

재검증 뮤테이션 X1·X2·X11 은 코드가 맞게(fail-closed) 되어 있었지만 어느 테스트도 그 가드를
고정하지 않았다(뮤테이션해도 87 pass). 여기서 고정한다.

* X1  기록기 overflow(노드 5000 초과) → 최상위 약한 변화는 효과로 인정하지 않는다.
* X2  기록기 읽기 예외(페이지가 __abTopRec 을 막음) → 인정하지 않는다(fail-open 금지).
* X11 프레임 문서 이동이 커밋됐지만 domcontentloaded 가 상한 안에 오지 않음 → nav_timed_out 표시.

(X6 hasFocus 갈래·X12 Enter 제출 표는 게이트 쪽이라 tests/interface/test_ws30r1_gate.py.)
로컬 HTTP 서버·set_content 만 쓴다.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401

# ------------------------------------------------------------------ 단위: _top_change_attributable


class _Root:
    """최상위 Page 대역 — 기록기 판독 결과를 정해 준다."""

    def __init__(self, read: Any = None, raises: bool = False) -> None:
        self.read, self.raises = read, raises

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == "performance.now()":
            return 1000.0
        if self.raises:
            raise RuntimeError("페이지가 기록기 접근을 막음")
        return self.read

    async def wait_for_timeout(self, ms: int) -> None:
        return None


def _dispatcher(root: _Root):
    from actions import ActionDispatcher, DispatchContext

    class _Engine:
        epoch = 0

    d = ActionDispatcher(DispatchContext(page=object(), engine=_Engine()))  # type: ignore[arg-type]
    d.ctx.root_page = root
    d._top_start = 500.0  # noqa: SLF001
    d._top_effect_signals = lambda *a, **k: ["top:text_changed"]  # type: ignore[method-assign]
    return d


async def test_attribution_overflow_is_not_effect():
    """X1: 효과 노드가 있어도 기록이 넘쳤으면(일부 노드 미기록) 인정하지 않는다."""
    d = _dispatcher(_Root({"act": 3, "spontaneous": 0, "effect": 3, "overflow": True,
                           "baseline_ms": 5000, "post_ms": 0}))
    assert await d._top_change_attributable(None, None) is False  # noqa: SLF001
    ok = _dispatcher(_Root({"act": 3, "spontaneous": 0, "effect": 3, "overflow": False,
                            "baseline_ms": 5000, "post_ms": 0}))
    assert await ok._top_change_attributable(None, None) is True, "대조: 넘치지 않으면 인정"  # noqa: SLF001


async def test_attribution_read_error_is_not_effect():
    """X2: 기록기를 읽다 예외가 나면 판정 불가 → 효과 아님(fail-closed)."""
    d = _dispatcher(_Root(raises=True))
    assert await d._top_change_attributable(None, None) is False  # noqa: SLF001


# ------------------------------------------------------------------ 실제 브라우저


async def _setup(pw, html: str):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(html)
    await page.wait_for_timeout(300)
    engine = PerceptionEngine()
    return browser, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine))


async def _frame_noop_clicks(html: str, n: int = 3):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html)
        assert (await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})).success
        obs = await engine.observe_page(page=d.ctx.page)
        eid = next(e.element_id for e in obs.elements if e.name == "무반응")
        out = [await d.dispatch(ActionType.CLICK, {"element_id": eid, "epoch": engine.epoch})
               for _ in range(n)]
        await browser.close()
    return out


FRAME = "<iframe id=f srcdoc=\"<button id=n>무반응</button>\"></iframe>"

#: 5200 노드를 400ms 마다 모두 갱신 — 기록기 노드 상한(5000)을 넘긴다(overflow).
BIG_TOP = (
    "<!doctype html><meta charset=utf-8><div id=box></div><script>"
    "for(let i=0;i<5200;i++){const d=document.createElement('div');d.textContent='n'+i;box.appendChild(d);}"
    "setInterval(()=>{for(const d of box.children){d.textContent=String(Date.now());}},400);"
    "</script>" + FRAME
)
#: 페이지가 기록기 전역을 막는다(읽기·쓰기 모두 예외) + 200ms 시계.
GUARDED_TOP = (
    "<!doctype html><meta charset=utf-8><script>Object.defineProperty(window,'__abTopRec',"
    "{get(){throw new Error('nope')},set(){throw new Error('nope')}});</script>"
    "<p id=clock>0</p><script>setInterval(()=>{clock.textContent=Date.now()},200)</script>" + FRAME
)


@requires_chromium
async def test_frame_noop_click_overflowing_top_is_timeout():
    for r in await _frame_noop_clicks(BIG_TOP):
        assert not r.success, (r.data.get("signals"), r.data.get("top_change_attribution"))
        assert r.error_code is ErrorCode.TIMEOUT


@requires_chromium
async def test_frame_noop_click_guarded_recorder_is_timeout():
    for r in await _frame_noop_clicks(GUARDED_TOP):
        assert not r.success, r.data.get("signals")
        assert r.error_code is ErrorCode.TIMEOUT


# ------------------------------------------------------------------ X11 프레임 dcl 미완


@pytest.fixture
def slow_site():
    pages: Dict[str, str] = {
        "/top": "<!doctype html><meta charset=utf-8><title>top</title>"
                "<iframe id=sf src=\"/fs\" width=400 height=200></iframe>",
        "/fs": "<!doctype html><meta charset=utf-8><form action=\"/slow\">"
               "<input id=q name=q aria-label=\"느린 검색\"></form>",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if path == "/slow":
                # 커밋은 되지만(본문 일부) DOM 파싱이 끝나지 않는다 — dcl 이 상한 안에 오지 않음.
                self.wfile.write(b"<!doctype html><html><body><p>loading" + b" " * 2048)
                self.wfile.flush()
                time.sleep(3)
                try:
                    self.wfile.write(b"</p></body></html>")
                except OSError:
                    pass
                return
            self.wfile.write(pages.get(path, "<p>x</p>").encode("utf-8"))

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@requires_chromium
async def test_frame_nav_without_dcl_is_marked_timed_out(slow_site, monkeypatch):
    """X11: 프레임 문서가 커밋만 되고 dcl 이 상한 안에 오지 않으면 nav_timed_out 을 싣는다."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext, dispatcher as disp_mod
    from perception import PerceptionEngine

    monkeypatch.setattr(disp_mod, "NAV_SETTLE_TIMEOUT_MS", 1200)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(slow_site + "/top")
        await page.wait_for_timeout(300)
        engine = PerceptionEngine()
        d = ActionDispatcher(DispatchContext(page=page, engine=engine))
        assert (await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#sf"})).success
        obs = await engine.observe_page(page=d.ctx.page)
        eid = next(e.element_id for e in obs.elements if e.name == "느린 검색")
        r = await d.dispatch(ActionType.TYPE_TEXT, {"element_id": eid, "epoch": engine.epoch,
                                                    "text": "x", "press_enter": True})
        await browser.close()
    assert r.data.get("nav_committed") is True, r.data
    assert r.data.get("nav_frame") == "current_frame", r.data
    assert r.data.get("nav_timed_out") is True, r.data
