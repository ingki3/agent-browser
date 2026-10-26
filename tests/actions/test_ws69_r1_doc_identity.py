"""WS-69 R1 — navigate 부분 성공은 1단계 문서가 그대로일 때만, 오류 문서(chrome-error://)는 언제나 실패 (로컬만).

검증(B1): DCL 70ms 뒤 `location.href='http://127.0.0.1:1/'` + 끝나지 않는 이미지 문서를 networkidle/load 로 열면
최초 문서는 사라지고 주 프레임이 chrome-error://chromewebdata/ 인데 success=true 였다. 형제 load 완료 경로는
기준(cf70270)부터 같은 거짓 성공이었다.
"""

from __future__ import annotations

import socket
import threading
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


def _hop(target: str) -> str:
    """DCL 뒤 70ms 에 target 으로 옮기는 문서 — 끝나지 않는 이미지 때문에 load·networkidle 은 오지 않는다."""
    return ("<!doctype html><meta charset=utf-8><title>최초</title><h1 id=t>INITIAL_DOCUMENT</h1><img src='/hang'>"
            f"<script>setTimeout(()=>location.href={target!r},70)</script>")


POLL = ("<!doctype html><meta charset=utf-8><title>폴링</title><h1 id=t>본문 열림</h1>"
        "<script>setInterval(()=>fetch('/ping?'+Math.random()).catch(()=>0),150)</script>")
#: 같은 문서 안의 이동(pushState) + 끝없는 폴링 — 문서 신원(loaderId)은 그대로다.
SPA_POLL = POLL.replace("<script>", "<script>setTimeout(()=>history.pushState({}, '', '/spa/next'),60);")
HANG_IMG = "<!doctype html><meta charset=utf-8><title>이미지</title><p id=t>둘째 문서</p><img src='/hang'>"


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.fixture
def server():
    stop = threading.Event()
    refused = f"http://127.0.0.1:{_closed_port()}/"

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

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/hang":
                stop.wait(20)
            elif path == "/to-error":
                self._send(_hop("http://127.0.0.1:1/"))
            elif path == "/to-refused":
                self._send(_hop(refused))
            elif path == "/to-poll":
                self._send(_hop("/poll"))
            elif path == "/to-img":
                self._send(_hop("/img"))
            elif path == "/poll":
                self._send(POLL)
            elif path == "/spa":
                self._send(SPA_POLL)
            elif path == "/img":
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


async def _run(url: str, wait_until: str, timeout_ms: int = 1500):
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await (await b.new_context()).new_page()
            d = ActionDispatcher(DispatchContext(page=page, engine=PerceptionEngine()))
            r = await d.dispatch(ActionType.NAVIGATE, {"url": url, "wait_until": wait_until, "timeout_ms": timeout_ms})
            return r, page.url
        finally:
            await b.close()


# -- 1. DCL 뒤 오류 문서로 바뀜(witness 재현) -------------------------------------------------


@pytest.mark.parametrize("wait_until", ["networkidle", "load"])
async def test_post_dcl_error_document_is_navigate_failure(server, wait_until):
    """witness 그대로(600ms). 원인 kind 는 오류 문서로 끝난 주 프레임 문서 요청의 net::ERR_* —
    Chromium 은 포트 1 을 '안전하지 않은 포트'로 막는다(ERR_UNSAFE_PORT → other)."""
    r, final = await _run(server + "/to-error", wait_until, timeout_ms=600)
    assert final.startswith("chrome-error://")
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in r.data
    assert r.data["nav_failure"] == {"kind": "other", "net_error": "ERR_UNSAFE_PORT"}
    assert "문서는 열렸" not in r.data["hint"]


@pytest.mark.parametrize("wait_until", ["networkidle", "load"])
async def test_post_dcl_refused_document_has_connect_kind(server, wait_until):
    r, final = await _run(server + "/to-refused", wait_until, timeout_ms=600)
    assert final.startswith("chrome-error://")
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["nav_failure"] == {"kind": "connect", "net_error": "ERR_CONNECTION_REFUSED"}


@pytest.mark.parametrize("wait_until", ["networkidle", "load"])
async def test_post_dcl_error_document_fails_with_long_timeout(server, wait_until):
    """2단계가 시간 안에 끝나는 쪽(오류 문서가 load/networkidle 도달)도 실패 — 기준부터 있던 거짓 성공."""
    r, final = await _run(server + "/to-error", wait_until, timeout_ms=4000)
    assert final.startswith("chrome-error://")
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["nav_failure"]["net_error"] == "ERR_UNSAFE_PORT"


# -- 2. DCL 뒤 다른 정상 문서로 바뀜 — 보수적으로 실패 -----------------------------------------


@pytest.mark.parametrize("path,wait_until", [("/to-poll", "networkidle"), ("/to-img", "load")])
async def test_post_dcl_other_document_is_conservative_failure(server, path, wait_until):
    r, final = await _run(server + path, wait_until, timeout_ms=1200)
    assert not final.startswith("chrome-error://") and final != server + path
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in r.data
    assert r.data["nav_failure"] == {"kind": "timeout"}
    assert "이동 중 다른 문서로 바뀜" in r.data["hint"]


# -- 3. 같은 문서 유지 — 부분 성공 그대로 ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/poll", "/spa"])
async def test_same_document_endless_polling_stays_partial_success(server, path):
    r, final = await _run(server + path, "networkidle", timeout_ms=1200)
    assert r.success is True, (r.error_message, r.data)
    assert r.data["load_state"] == {"requested": "networkidle", "reached": "domcontentloaded", "timed_out": True}
    if path == "/spa":
        assert final.endswith("/spa/next")  # 같은 문서 안 이동은 문서 교체가 아니다


# -- 4. 1단계만 쓰는 경로(commit·domcontentloaded) — goto 가 끝났는데 오류 문서면 실패 -------------


class _ErrorDocPage:
    """goto 는 예외 없이 끝났지만 주 프레임이 오류 문서인 페이지(Chromium 이 실제로 그렇게 끝나는 경우의 대역)."""

    url = "chrome-error://chromewebdata/"

    async def goto(self, url, wait_until=None, timeout=None):  # noqa: ANN001
        return None


@pytest.mark.parametrize("wait_until", ["commit", "domcontentloaded"])
async def test_stage_one_only_error_document_is_failure(wait_until):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    d = ActionDispatcher(DispatchContext(page=_ErrorDocPage(), engine=PerceptionEngine()))
    r = await d.dispatch(ActionType.NAVIGATE, {"url": "http://127.0.0.1:9/", "wait_until": wait_until,
                                               "timeout_ms": 1000})
    assert r.success is False
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["nav_failure"]["kind"] == "other"


async def test_stage_one_only_normal_document_unchanged(server):
    r, _ = await _run(server + "/poll", "domcontentloaded")
    assert r.success is True and "load_state" not in r.data and "nav_failure" not in r.data
