"""WS-93 A (#270) — Bing 과제 화면이 서버 경로(BrowserMCPServer.call_tool navigate, networkidle)에서 data.challenge 로 실린다.

실사용 모양: bing.com 검색이 과제 화면 + 폴링(networkidle 미도달, #170 1커밋 부분 성공) 또는 같은 주소 2번째 커밋(WS-92
documents=2)으로 끝났는데 data.challenge 가 null 이었다. 정적 화면(networkidle 도달)도 함께 본다.
로컬 HTTP(127.0.0.1)만 쓴다 — bing 벤더 도메인을 127.0.0.1 로 바꿔(monkeypatch) 호스트 한정 규칙을 통과시킨다.
hint 는 바꾸지 않는다(문서는 열렸… 바로 읽어도 됩니다 그대로). robots 금지 경로에서는 인계 안내가 없는 것이 정상이다.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urlparse

import pytest


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

POLL_JS = "<script>setInterval(()=>fetch('/ping?'+Math.random()).catch(()=>0),150)</script>"
KO = ("<a href='#m'>콘텐츠로 건너뛰기</a><a>접근성 피드백</a><span>Rewards</span>"
      "<h1>마지막 한 단계</h1><p>계속하려면 아래 과제 해결</p><div style='width:300px;height:65px'></div>")
EN = ("<a href='#m'>Skip to content</a><a>Accessibility Feedback</a><span>Rewards</span>"
      "<h1>One last step</h1><p>Please solve the challenge below to continue</p>"
      "<div style='width:300px;height:65px'></div>")
SELF_RELOAD = ("<script>addEventListener('load',()=>{if(!sessionStorage.r){sessionStorage.r=1;"
               "setTimeout(()=>location.reload(),50)}})</script>")
REASON = "Bing 과제 화면(계속하려면 과제 해결 요구)"


def _doc(body: str, extra: str = "") -> str:
    return f"<!doctype html><meta charset=utf-8><title>Bing</title>{body}{extra}"


PAGES = {
    "/search/ko_poll": _doc(KO, POLL_JS),
    "/search/en_poll": _doc(EN, POLL_JS),
    "/search/ko_multi": _doc(KO, POLL_JS + SELF_RELOAD),
    "/search/en_multi": _doc(EN, POLL_JS + SELF_RELOAD),
    "/search/ko_static": _doc(KO),
    "/search/en_static": _doc(EN),
}
#: 경로 → 기대 load_state(None 이면 networkidle 도달 — 키 없음)
EXPECTED_LOAD_STATE = {
    "poll": {"requested": "networkidle", "reached": "domcontentloaded", "timed_out": True},
    "multi": {"requested": "networkidle", "reached": "domcontentloaded", "timed_out": True, "documents": 2},
    "static": None,
}


@pytest.fixture
def site_factory():
    servers = []

    def make(robots: bytes) -> str:
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                u = urlparse(self.path)
                if u.path == "/robots.txt":
                    body, ct = robots, "text/plain"
                else:
                    body, ct = PAGES.get(u.path, _doc("<p>x</p>")).encode(), "text/html; charset=utf-8"
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.daemon_threads = True
        srv.block_on_close = False
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def bing_is_local(monkeypatch):
    from browser import challenge

    monkeypatch.setitem(challenge.SITE_VENDOR_DOMAINS, "bing", ("127.0.0.1",))


async def _navigate(url: str) -> Dict[str, Any]:
    from interface.mcp_server import BrowserMCPServer

    s = BrowserMCPServer()
    try:
        r = await asyncio.wait_for(s.call_tool("browser_navigate", {
            "url": url, "wait_until": "networkidle", "timeout_ms": 1500}), 40.0)
        assert r.success is True, (r.error_code, r.error_message, r.data)
        return r.data
    finally:
        await s.close()


def _assert_load_state(data: Dict[str, Any], shape: str) -> None:
    from actions.nav_failure import LOAD_STATE_TIMED_OUT_HINT

    expected = EXPECTED_LOAD_STATE[shape]
    if expected is None:
        assert "load_state" not in data, data
    else:
        assert data["load_state"] == expected, data
        # hint 불변(B5): 부분 성공 문장 그대로 — 판정 신호는 data.challenge
        assert LOAD_STATE_TIMED_OUT_HINT.format(requested="networkidle") in data["hint"], data["hint"]


@pytest.mark.parametrize("path", sorted(PAGES))
async def test_bing_challenge_signal_on_navigate(site_factory, bing_is_local, path):
    from interface.server_signals import HANDOFF_HINT

    base = site_factory(b"User-agent: *\nAllow: /\n")
    data = await _navigate(base + path)
    assert data["challenge"] == {"kind": "captcha", "vendor": "bing", "reason": REASON}, data
    _assert_load_state(data, path.rsplit("_", 1)[1])
    assert data["handoff"]["suggested"] is True and data["handoff"]["reason"] == "captcha"
    assert data["hint"].count(HANDOFF_HINT) == 1


@pytest.mark.parametrize("path", ["/search/ko_poll", "/search/en_multi", "/search/en_static"])
async def test_bing_challenge_robots_disallowed_has_no_handoff(site_factory, bing_is_local, path):
    from interface.server_signals import HANDOFF_HINT

    base = site_factory(b"User-agent: *\nDisallow: /search\n")
    data = await _navigate(base + path)
    assert data["challenge"] == {"kind": "captcha", "vendor": "bing", "reason": REASON}, data
    assert data["robots"]["disallowed"] is True
    assert "handoff" not in data  # settle_handoff: robots 안내가 우선
    assert HANDOFF_HINT not in (data.get("hint") or "")
    _assert_load_state(data, path.rsplit("_", 1)[1])


@pytest.mark.parametrize("path", ["/search/ko_poll", "/search/en_static"])
async def test_same_text_off_bing_host_is_no_challenge(site_factory, path):
    """호스트 한정: 같은 화면이라도 bing.com 이 아니면(여기선 127.0.0.1 그대로) challenge 는 null."""
    base = site_factory(b"User-agent: *\nAllow: /\n")
    data = await _navigate(base + path)
    assert data["challenge"] is None, data
    assert "handoff" not in data
