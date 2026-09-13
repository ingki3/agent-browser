"""WS-v1.1 vision 테스트 공용 픽스처.

`tests/perception/test_perception.py`의 실브라우저 패턴을 따른다.
Mock 서버는 모듈 스코프로 1회 기동하고, `page`는 계약 뷰포트로 연다.
skip 마커 `requires_chromium`은 `_browser.py`에서 import한다
(conftest는 모듈로 import할 수 없으므로 분리).
"""

from __future__ import annotations

import pytest

from contracts import thresholds


@pytest.fixture(scope="module")
def mock_server():
    from harness import MockServer

    with MockServer() as srv:
        yield srv


@pytest.fixture
async def page():
    """계약 뷰포트(1280×720)로 연 빈 페이지. 테스트가 끝나면 브라우저를 닫는다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={
                "width": thresholds.VIEWPORT_WIDTH,
                "height": thresholds.VIEWPORT_HEIGHT,
            }
        )
        pg = await context.new_page()
        try:
            yield pg
        finally:
            await browser.close()
