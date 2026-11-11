"""WS-93 B (#271-2) — 기본 timeout 의 navigate networkidle 은 load 뒤 3초까지만 더 기다린다 (디스패처, 로컬 서버만).

실사용(봇): 모든 navigate 를 wait_until=networkidle 로 부르는데 계속 통신하는 페이지(Bing)는 15초를 다 쓰고
부분 성공(#170)이었다. 사용자 결정 A: 계약 기본 timeout_ms(15000) 일 때만 load 뒤 networkidle 대기를
min(남은 예산, 3초)로 줄인다. 못 닿으면 기존 #170 부분 성공 그대로(reached='domcontentloaded', 문구 불변, B2 a).
명시 timeout_ms(기본값이 아닌 값)는 기준 그대로(B3). wait_for(network_idle) 는 상한 없음(B4).
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from actions import dispatcher as dmod
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

DEFAULT_MS = 15000  # contracts NavigateInput.timeout_ms 기본값


def _doc(body: str) -> str:
    return f"<!doctype html><meta charset=utf-8><title>t</title><h1>HEAD</h1><div id=r>EMPTY</div>{body}"


POLL = _doc("<script>setInterval(()=>fetch('/ping?'+Math.random()).catch(()=>0),150)</script>")
LATE = "<script>addEventListener('load',()=>fetch('/slow?d={d}').then(r=>r.text()).then(t=>{{r.textContent=t}}))</script>"
PAGES = {
    "/static": _doc(""),
    "/poll": POLL,
    "/late2": _doc(LATE.format(d=2000)),
    "/late4": _doc(LATE.format(d=4000)),
}


@pytest.fixture
def server():
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/slow":
                stop.wait(int((parse_qs(u.query).get("d") or ["0"])[0]) / 1000)
                self._send("FILLED", "text/plain")
            elif u.path == "/ping":
                self._send("ok", "text/plain")
            else:
                self._send(PAGES.get(u.path, _doc("")))

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


async def _open(pw):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    b = await pw.chromium.launch(headless=True)
    page = await (await b.new_context()).new_page()
    engine = PerceptionEngine()
    return b, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine))


async def _nav(d, url, timeout_ms=DEFAULT_MS, wait_until="networkidle"):
    return await d.dispatch(ActionType.NAVIGATE, {"url": url, "wait_until": wait_until, "timeout_ms": timeout_ms})


async def _text(d) -> str:
    ex = await d.dispatch(ActionType.EXTRACT, {"selector": "#r"})
    assert ex.success, ex.error_message
    return ex.data["items"]["text"]


BASE_PARTIAL = {"requested": "networkidle", "reached": "domcontentloaded", "timed_out": True}


async def test_endless_polling_default_timeout_returns_within_cap(server):
    """기준은 15초 뒤 부분 성공 — 후보는 load 뒤 3초(≤3.5s). 어휘·hint 는 기준 그대로."""
    from actions.nav_failure import LOAD_STATE_TIMED_OUT_HINT
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        before = engine.epoch
        t0 = time.monotonic()
        r = await _nav(d, server + "/poll")
        elapsed = time.monotonic() - t0
        after = engine.epoch
        await b.close()
    assert r.success is True and r.error_code is None, r.error_message
    assert elapsed <= 3.5, elapsed
    assert r.data["load_state"] == BASE_PARTIAL
    assert r.data["hint"] == LOAD_STATE_TIMED_OUT_HINT.format(requested="networkidle")
    assert after == before + 1


async def test_late_xhr_2s_still_filled_under_cap(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        r = await _nav(d, server + "/late2")
        text = await _text(d)
        await b.close()
    assert r.success and "load_state" not in r.data and "hint" not in r.data
    assert text == "FILLED"


async def test_late_xhr_4s_partial_then_wait_for_network_idle_fills(server):
    """3초 넘는 XHR 은 덜 채워진 채 부분 성공 — wait_for(network_idle) 는 상한 없이 기다려 채운다(B4)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        r = await _nav(d, server + "/late4")
        early = await _text(d)
        t0 = time.monotonic()
        w = await d.dispatch(ActionType.WAIT_FOR, {"condition": "network_idle", "timeout_ms": 10000})
        waited = time.monotonic() - t0
        late = await _text(d)
        await b.close()
    assert r.success and r.data["load_state"] == BASE_PARTIAL
    assert early == "EMPTY"
    assert w.success, w.error_message
    assert late == "FILLED"
    assert waited > 0.3  # 실제로 더 기다렸다(이미 idle 이라 즉시 끝난 것이 아님)


async def test_explicit_non_default_timeout_keeps_baseline_wait(server):
    """timeout_ms 가 계약 기본값이 아니면 상한 없음 — 기준처럼 예산을 다 쓴다(B3)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        t0 = time.monotonic()
        r = await _nav(d, server + "/poll", timeout_ms=6000)
        elapsed = time.monotonic() - t0
        await b.close()
    assert r.success and r.data["load_state"] == BASE_PARTIAL
    assert 5.8 <= elapsed <= 6.5, elapsed


async def test_static_page_result_same_as_uncapped_path(server):
    """정적 페이지: 기본(상한 경로)과 비기본(기준 경로) 결과가 바이트 단위로 같다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        capped = await _nav(d, server + "/static")
        b2, page2, engine2, d2 = await _open(pw)
        uncapped = await _nav(d2, server + "/static", timeout_ms=14999)
        await b.close()
        await b2.close()
    def _bytes(r):
        dumped = r.model_dump(mode="json")
        dumped["data"].pop("latency_ms", None)  # 측정값 — 경로와 무관
        return dumped

    assert capped.success and set(capped.data) == {"url", "latency_ms"}
    assert _bytes(capped) == _bytes(uncapped)


async def test_cap_passes_load_then_min_remaining_or_3s(server, monkeypatch):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        clock = iter([100.0, 100.1, 100.5])  # 시작 · 1단계 직후 · load 직후
        monkeypatch.setattr(dmod, "_monotonic", lambda: next(clock))
        seen = []

        async def spy(state, timeout=None):
            seen.append((state, timeout))

        page.wait_for_load_state = spy
        r = await _nav(d, server + "/static")
        await b.close()
    assert r.success and "load_state" not in r.data
    assert seen[0][0] == "load" and abs(seen[0][1] - 14900) < 1
    assert seen[1] == ("networkidle", 3000.0)
    assert len(seen) == 2


async def test_cap_uses_remaining_when_below_3s(server, monkeypatch):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        clock = iter([100.0, 100.1, 113.0])  # load 직후 2000ms 남음
        monkeypatch.setattr(dmod, "_monotonic", lambda: next(clock))
        seen = []

        async def spy(state, timeout=None):
            seen.append((state, timeout))

        page.wait_for_load_state = spy
        r = await _nav(d, server + "/static")
        await b.close()
    assert r.success
    assert seen[1][0] == "networkidle" and abs(seen[1][1] - 2000) < 1


async def test_cap_skips_networkidle_wait_when_budget_below_1ms(server, monkeypatch):
    """load 뒤 남은 예산 <1ms 면 networkidle 대기를 부르지 않는다(timeout=0 은 무한, NN3) — 기존 부분 성공 규칙."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        clock = iter([100.0, 100.1, 114.9995])  # load 직후 0.5ms 남음
        monkeypatch.setattr(dmod, "_monotonic", lambda: next(clock))
        seen = []
        orig = page.wait_for_load_state

        async def spy(state, timeout=None):
            seen.append((state, timeout))
            return await orig(state, timeout=timeout)

        page.wait_for_load_state = spy
        r = await _nav(d, server + "/static")
        await b.close()
    assert [s for s, _ in seen] == ["load"]
    assert r.success is True and r.error_code is None
    assert r.data["load_state"] == BASE_PARTIAL


async def test_cap_path_close_is_page_crashed(server):
    """상한 경로에서도 시간 초과만 부분 성공 후보 — 닫힘은 기존 분류(E_PAGE_CRASHED)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)

        async def closed(state, timeout=None):
            raise RuntimeError("Target page, context or browser has been closed")

        page.wait_for_load_state = closed
        r = await _nav(d, server + "/static")
        await b.close()
    assert r.success is False and r.error_code is ErrorCode.PAGE_CRASHED


def test_cap_key_is_contract_default_timeout():
    """상한 조건의 기준값은 계약 기본값과 같아야 한다(서버가 model_dump 로 기본값을 채움, B3)."""
    from contracts.inputs import NavigateInput

    assert dmod._NAV_IDLE_CAP_DEFAULT_TIMEOUT_MS == NavigateInput(url="https://example.com").timeout_ms == DEFAULT_MS
    assert dmod._NAV_IDLE_CAP_MS == 3000.0
