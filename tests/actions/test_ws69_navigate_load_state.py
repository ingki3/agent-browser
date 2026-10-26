"""WS-69 — navigate 2단계(부분 성공)·이동 실패 종류·extract 선택자 문법 오류 (디스패처, 로컬 서버만).

실사용(봇 10-22~25): networkidle 이 광고·추적 폴링 때문에 영원히 오지 않아 15초 뒤 E_NAVIGATE_TIMEOUT 인데
문서는 이미 열려 곧바로 extract 가 성공했다. 1단계 goto(domcontentloaded) 가 커밋+DCL 을 증명한 뒤 2단계
wait_for_load_state(요청값) 의 **시간 초과만** 부분 성공으로 본다.
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

#: 끝없는 폴링 — DCL·load 는 오지만 networkidle 은 영원히 오지 않는다.
POLL = ("<!doctype html><meta charset=utf-8><title>폴링</title><h1 id=t>본문 열림</h1>"
        "<script>setInterval(()=>fetch('/ping?'+Math.random()).catch(()=>0),150)</script>")
#: 끝나지 않는 이미지 — DCL 은 오지만 load 는 오지 않는다.
HANG_IMG = "<!doctype html><meta charset=utf-8><title>이미지</title><p id=t>이미지 대기</p><img src='/hang'>"
PLAIN = "<!doctype html><meta charset=utf-8><title>보통</title><p id=t>보통 문서</p><p class=x>둘</p>"


@pytest.fixture
def server():
    stop = threading.Event()
    hits = {"flaky": 0}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str, code: int = 200) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/loop":
                try:
                    self.send_response(302)
                    self.send_header("Location", "/loop")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            elif u.path == "/poll":
                self._send(POLL)
            elif u.path == "/ping":
                self._send("ok")
            elif u.path == "/img":
                self._send(HANG_IMG)
            elif u.path == "/hang":
                stop.wait(20)
            elif u.path == "/slow":
                stop.wait(int((qs.get("d") or ["0"])[0]) / 1000)
                self._send(PLAIN)
            elif u.path == "/flaky":
                # 첫 요청은 바로(끝없는 폴링 문서), 그 뒤는 느림 — 같은 주소 재이동이 1단계에서 시간 초과
                hits["flaky"] += 1
                if hits["flaky"] > 1:
                    stop.wait(5)
                self._send(POLL)
            else:
                self._send(PLAIN)

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


async def _open(pw, url=None):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    b = await pw.chromium.launch(headless=True)
    page = await (await b.new_context()).new_page()
    if url:
        await page.goto(url)
    engine = PerceptionEngine()
    return b, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine))


async def _nav(d, url, wait_until="networkidle", timeout_ms=1500):
    return await d.dispatch(ActionType.NAVIGATE, {"url": url, "wait_until": wait_until, "timeout_ms": timeout_ms})


# -- 1. 부분 성공 -------------------------------------------------------------------


async def test_endless_polling_networkidle_is_partial_success_and_epoch_bumps(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        before = engine.epoch
        t0 = time.monotonic()
        r = await _nav(d, server + "/poll")
        elapsed = time.monotonic() - t0
        after = engine.epoch
        ex = await d.dispatch(ActionType.EXTRACT, {"selector": "#t"})
        await b.close()
    assert r.success is True, r.error_message
    assert r.error_code is None
    assert r.data["load_state"] == {"requested": "networkidle", "reached": "domcontentloaded", "timed_out": True}
    assert "문서는 열렸" in r.data["hint"]
    assert r.data["url"].endswith("/poll")
    assert after == before + 1
    assert elapsed < 4.0  # 남은 시간만 기다린다(대기 두 배 없음)
    assert ex.success and ex.data["items"]["text"] == "본문 열림"


async def test_load_request_follows_same_rule(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        r = await _nav(d, server + "/img", wait_until="load")
        await b.close()
    assert r.success is True, r.error_message
    assert r.data["load_state"] == {"requested": "load", "reached": "domcontentloaded", "timed_out": True}


async def test_reached_load_state_has_no_load_state_key(server):
    """요청한 상태까지 닿았으면 기존 결과 그대로(load_state 키 없음)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        r = await _nav(d, server + "/plain", wait_until="networkidle", timeout_ms=8000)
        r2 = await _nav(d, server + "/plain2", wait_until="load", timeout_ms=8000)
        await b.close()
    assert r.success and "load_state" not in r.data and "hint" not in r.data
    assert r2.success and "load_state" not in r2.data


async def test_slow_first_response_is_still_navigate_timeout(server):
    """1단계(커밋·DCL 전) 시간 초과는 기존대로 E_NAVIGATE_TIMEOUT + kind=timeout(문서는 열림 hint 아님)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/plain")
        before = engine.epoch
        r = await _nav(d, server + "/slow?d=4000", timeout_ms=1000)
        after = engine.epoch
        await b.close()
    assert r.success is False
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["nav_failure"] == {"kind": "timeout"}
    assert "느려" in r.data["hint"] and "문서는 열렸" not in r.data["hint"]
    assert "load_state" not in r.data
    assert after == before


async def test_same_url_renavigate_does_not_mistake_previous_document(server):
    """같은 주소가 이미 열려 있어도(complete) 새 이동의 1단계 시간 초과는 실패 — 이전 문서로 성공을 내지 않는다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        first = await _nav(d, server + "/flaky", wait_until="domcontentloaded", timeout_ms=5000)
        await page.wait_for_load_state("load")
        again = await _nav(d, server + "/flaky", timeout_ms=1000)
        await b.close()
    assert first.success
    assert again.success is False
    assert again.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in again.data


async def test_remaining_time_below_1ms_skips_stage_two(server, monkeypatch):
    """남은 시간 <1ms 면 2단계를 부르지 않는다(timeout=0 은 무한 대기).

    R2 기대값 변경(부분 성공 → 보수 실패, 코디네이터 승인 A): 예산이 소진돼 부분 성공 판정(문서 커밋 계수·readyState
    판독)을 예산 안에서 할 수 없으므로 E_NAVIGATE_TIMEOUT. 1단계는 끝났으므로 epoch 는 오른다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        clock = iter([100.0, 101.4995])  # 시작 · 1단계 직후: timeout 1500ms 중 0.5ms 남음
        monkeypatch.setattr(dmod, "_monotonic", lambda: next(clock))
        called = []
        orig = page.wait_for_load_state

        async def spy(*a, **kw):
            called.append((a, kw))
            return await orig(*a, **kw)

        page.wait_for_load_state = spy
        before = engine.epoch
        r = await _nav(d, server + "/plain")
        after = engine.epoch
        await b.close()
    assert r.success is False
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in r.data
    assert r.data["nav_failure"] == {"kind": "timeout"}
    assert called == []
    assert after == before + 1


async def test_remaining_time_is_passed_to_stage_two(server, monkeypatch):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        clock = iter([100.0, 100.4])
        monkeypatch.setattr(dmod, "_monotonic", lambda: next(clock))
        seen = []

        async def spy(state, timeout=None):
            seen.append((state, timeout))

        page.wait_for_load_state = spy
        r = await _nav(d, server + "/plain", timeout_ms=1500)
        await b.close()
    assert r.success and "load_state" not in r.data
    assert seen and seen[0][0] == "networkidle" and abs(seen[0][1] - 1100) < 1


async def test_stage_two_target_closed_is_page_crashed(server):
    """2단계의 닫힘은 부분 성공이 아니라 E_PAGE_CRASHED(2단계 catch 를 넓히면 이 시험이 실패한다)."""
    from playwright.async_api import async_playwright

    class TargetClosedError(Exception):
        pass

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)

        async def closed(*a, **kw):
            raise TargetClosedError("Target page, context or browser has been closed")

        page.wait_for_load_state = closed
        r = await _nav(d, server + "/plain")
        await b.close()
    assert r.success is False
    assert r.error_code is ErrorCode.PAGE_CRASHED
    assert "load_state" not in r.data and "nav_failure" not in r.data


async def test_commit_request_has_no_stage_two(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        called = []

        async def spy(*a, **kw):
            called.append(a)

        page.wait_for_load_state = spy
        r = await _nav(d, server + "/poll", wait_until="commit")
        r2 = await _nav(d, server + "/poll", wait_until="domcontentloaded")
        await b.close()
    assert r.success and r2.success
    assert called == []
    assert "load_state" not in r.data and "load_state" not in r2.data


# -- 2. 이동 실패 종류(프록시 없는 디스패처 경로: Chrome 오류 문자열) --------------------------


async def test_connection_refused_is_connect_kind(server):
    import socket

    from playwright.async_api import async_playwright

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed = probe.getsockname()[1]
    probe.close()
    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        r = await _nav(d, f"http://127.0.0.1:{closed}/x", wait_until="domcontentloaded", timeout_ms=5000)
        await b.close()
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["nav_failure"] == {"kind": "connect", "net_error": "ERR_CONNECTION_REFUSED"}
    assert "접속하지 못함" in r.data["hint"]


@pytest.mark.parametrize("message,kind", [
    ("Page.goto: net::ERR_NAME_NOT_RESOLVED at http://x.invalid/", "dns"),
    ("net::ERR_NAME_RESOLUTION_FAILED", "dns"),
    ("net::ERR_CONNECTION_RESET", "connect"),
    ("net::ERR_CONNECTION_CLOSED", "connect"),
    ("net::ERR_CONNECTION_TIMED_OUT", "connect"),
    ("net::ERR_TUNNEL_CONNECTION_FAILED", "connect"),
    ("net::ERR_PROXY_CONNECTION_FAILED", "connect"),
    ("net::ERR_CERT_AUTHORITY_INVALID", "tls"),
    ("net::ERR_SSL_PROTOCOL_ERROR", "tls"),
    ("net::ERR_HTTP2_PROTOCOL_ERROR", "protocol"),
    ("net::ERR_QUIC_PROTOCOL_ERROR", "protocol"),
    ("Page.goto: Timeout 15000ms exceeded.", "timeout"),
    ("net::ERR_ABORTED", "aborted"),
    ("net::ERR_TOO_MANY_REDIRECTS", "other"),
    ("net::ERR_SOMETHING_NEW", "other"),
    ("unexpected", "other"),
])
def test_kind_table(message, kind):
    from actions.nav_failure import classify_nav_failure

    assert classify_nav_failure(message)["kind"] == kind


def test_kinds_are_control_vocabulary():
    """응답 출구의 제어 값(kind·reached·retry_skipped.reason)은 정제 면제 어휘 안에 있다(WS-51)."""
    from actions import nav_failure as nf
    from browser.headless_retry import SKIP_NOTES
    from security.public_url import CONTROL_VOCABULARY

    kinds = set(nf._EXACT_KINDS.values()) | set(nf._EGRESS_KINDS.values()) | set(nf.NAV_FAILURE_HINTS)
    assert kinds <= CONTROL_VOCABULARY["kind"]
    assert "domcontentloaded" in CONTROL_VOCABULARY["reached"]
    assert "load_timed_out" in SKIP_NOTES and "load_timed_out" in CONTROL_VOCABULARY["reason"]


def test_egress_code_wins_over_message():
    from actions.nav_failure import classify_nav_failure

    msg = "net::ERR_TUNNEL_CONNECTION_FAILED"
    assert classify_nav_failure(msg, "resolve_failed") == {"kind": "dns"}
    assert classify_nav_failure(msg, "connect_failed") == {"kind": "connect"}
    assert classify_nav_failure("", "egress_blocked") == {"kind": "blocked"}


def test_redirect_loop_hint_and_net_error():
    from actions.nav_failure import attach_nav_failure

    data = attach_nav_failure({}, "net::ERR_TOO_MANY_REDIRECTS at http://a/")
    assert data["nav_failure"] == {"kind": "other", "net_error": "ERR_TOO_MANY_REDIRECTS"}
    assert "리다이렉트" in data["hint"]


async def test_too_many_redirects_real(server):
    """리다이렉트 고리(로컬) — ERR_TOO_MANY_REDIRECTS → kind other + net_error."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw)
        r = await _nav(d, server + "/loop", wait_until="domcontentloaded", timeout_ms=5000)
        await b.close()
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["nav_failure"] == {"kind": "other", "net_error": "ERR_TOO_MANY_REDIRECTS"}


# -- 3. extract 선택자 문법 오류 ------------------------------------------------------


@pytest.mark.parametrize("selector", ["text=/본문/i", "p:has-text('보통')", "##bad"])
async def test_extract_invalid_selector_is_element_not_found(server, selector):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/plain")
        r = await d.dispatch(ActionType.EXTRACT, {"selector": selector})
        r_all = await d.dispatch(ActionType.EXTRACT, {"selector": selector, "extract_all": True})
        ok = await d.dispatch(ActionType.EXTRACT, {"selector": "p.x", "extract_all": True})
        await b.close()
    for res in (r, r_all):
        assert res.success is False
        assert res.error_code is ErrorCode.ELEMENT_NOT_FOUND
        assert res.data["selector_error"] is True
        assert "표준 CSS" in res.data["hint"]
    assert ok.success and ok.data["items"] == [{"text": "둘"}]


async def test_extract_real_js_error_still_page_crashed(server):
    """선택자 문법 오류가 아닌 evaluate 오류는 기존 포괄 처리(E_PAGE_CRASHED) 그대로 — 숨기지 않는다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/plain")
        await page.evaluate("Object.defineProperty(HTMLElement.prototype, 'innerText',"
                            " {get(){ throw new SyntaxError('boom in getter'); }})")
        r = await d.dispatch(ActionType.EXTRACT, {"selector": "#t"})
        await b.close()
    assert r.success is False
    assert r.error_code is ErrorCode.PAGE_CRASHED
    assert "selector_error" not in r.data


async def test_shadow_root_selector_syntax_error(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/plain")
        r = await d.dispatch(ActionType.SWITCH_FRAME, {"shadow_root_selector": "div:has-text('x')"})
        missing = await d.dispatch(ActionType.SWITCH_FRAME, {"shadow_root_selector": "#nope"})
        await b.close()
    assert r.success is False
    assert r.error_code is ErrorCode.SHADOW_ROOT_NOT_FOUND
    assert r.data["selector_error"] is True and "표준 CSS" in r.data["hint"]
    assert missing.error_code is ErrorCode.SHADOW_ROOT_NOT_FOUND and "selector_error" not in missing.data
