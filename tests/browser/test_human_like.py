"""BrowserCore human_like — 사람이 쓰는 브라우저와 같은 기본값 (위장 없음).

human_like=True 는
  - viewport 고정 대신 no_viewport (창 크기 = 실제 창, screen 위장 없음)
  - locale="ko-KR" (Accept-Language 헤더와 navigator.languages 채움)
만 바꾼다. UA 변경·navigator.webdriver 숨기기 같은 위장은 하지 않는다 — 그 계약도 여기서 고정한다.
headed 창 테스트는 CI(헤드리스 러너)에서 못 돌 수 있어 넣지 않는다.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browser.core import BrowserCore


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")


@pytest.fixture
def local_server():
    """127.0.0.1 스레드 HTTP 서버. 받은 요청 헤더를 기록한다."""
    seen: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = b"<!doctype html><meta charset=utf-8><h1>ok</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", seen
    finally:
        server.shutdown()
        server.server_close()


async def _open(core: BrowserCore, url: str):
    await core.new_tab("p", url)
    return await core.get_active_page()


async def test_default_core_keeps_fixed_viewport(local_server):
    url, _ = local_server
    async with BrowserCore() as core:
        page = await _open(core, url)
        assert page.viewport_size == {"width": 1280, "height": 720}


async def test_human_like_sends_ko_kr_language(local_server):
    url, seen = local_server
    async with BrowserCore(headless=True, human_like=True) as core:
        page = await _open(core, url)
        langs = await page.evaluate("navigator.languages")
        assert langs and langs[0] == "ko-KR"
    doc = [h for h in seen if "accept-language" in h]
    assert doc, f"Accept-Language 헤더 없음: {seen}"
    assert "ko-KR" in doc[0]["accept-language"]


async def test_human_like_uses_real_window_size(local_server):
    url, _ = local_server
    async with BrowserCore(headless=True, human_like=True) as core:
        page = await _open(core, url)
        assert page.viewport_size is None


async def test_human_like_does_not_hide_webdriver(local_server):
    url, _ = local_server
    async with BrowserCore(headless=True, human_like=True) as core:
        page = await _open(core, url)
        assert await page.evaluate("navigator.webdriver") is True


async def test_human_like_does_not_change_user_agent(local_server):
    url, seen = local_server
    async with BrowserCore(headless=True) as plain:
        page = await _open(plain, url)
        plain_ua = await page.evaluate("navigator.userAgent")
    async with BrowserCore(headless=True, human_like=True) as core:
        page = await _open(core, url)
        human_ua = await page.evaluate("navigator.userAgent")
        browser_ua = core._browser.version  # noqa: SLF001 — 버전만 교차 확인
    assert human_ua == plain_ua
    assert browser_ua.split(".")[0] in human_ua
    assert seen[-1]["user-agent"] == human_ua
