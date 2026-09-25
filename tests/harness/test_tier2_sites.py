"""Stage 4 Tier-2 실패 유발 Mock 페이지 3종의 동작 검증.

`test_harness.py`는 정적 정의만 검사한다. 여기서는 실브라우저로
(1) 정답 클릭이 body[data-result="ok"]를 만드는지,
(2) Tier-1 텍스트 파이프라인이 **실제로** 정답을 지목하지 못하는지
(사보타주 검증 — 이 조건이 깨지면 발동률 측정이 유형 D가 된다)를 확인한다.
"""

from __future__ import annotations

import pytest

from contracts import thresholds


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Chromium 바이너리 없음"
)


@pytest.fixture(scope="module")
def server():
    from harness import MockServer

    with MockServer() as srv:
        yield srv


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={
                "width": thresholds.VIEWPORT_WIDTH,
                "height": thresholds.VIEWPORT_HEIGHT,
            }
        )
        pg = await ctx.new_page()
        try:
            yield pg
        finally:
            await browser.close()


async def _result(page) -> str | None:
    return await page.evaluate("document.body.getAttribute('data-result')")


@requires_chromium
async def test_icon_buttons_third_button_is_the_answer(server, page):
    await page.goto(server.site_url("icon-buttons"))
    assert await page.locator("button").count() == 5
    await page.click("#ic1")
    assert await _result(page) is None
    await page.click("#ic3")
    assert await _result(page) == "ok"


@requires_chromium
async def test_obfuscated_search_button_is_the_answer(server, page):
    await page.goto(server.site_url("obfuscated-labels"))
    await page.click("#k1")
    assert await _result(page) is None
    await page.click("#k2")
    assert await _result(page) == "ok"


@requires_chromium
async def test_canvas_hit_test_only_cart_rect_is_ok(server, page):
    await page.goto(server.site_url("canvas-ui"))
    # 검색 사각형 → wrong, 빈 곳 → 변화 없음, 장바구니 사각형 → ok
    await page.mouse.click(100, 150)
    assert await _result(page) == "wrong"
    await page.mouse.click(300, 150)
    assert await _result(page) == "ok"


@requires_chromium
async def test_tier1_cannot_name_the_answer_on_tier2_pages(server, page):
    """사보타주 검증: Tier-1 관찰 결과에 정답을 텍스트로 지목할 이름이 없어야 한다.

    icon-buttons — 5개 버튼의 접근성 이름이 전부 id 폴백(무의미)
    obfuscated-labels — 어떤 요소 이름에도 '검색'이 없음
    canvas-ui — 상호작용 요소 자체가 0개
    """
    from perception import PerceptionEngine

    engine = PerceptionEngine()

    await page.goto(server.site_url("icon-buttons"))
    obs = await engine.observe_page(page=page)
    names = [e.name for e in obs.elements if e.role == "button"]
    assert len(names) == 5
    assert all(n.startswith("ic") for n in names), names

    await page.goto(server.site_url("obfuscated-labels"))
    obs = await engine.observe_page(page=page)
    assert not any("검색" in e.name for e in obs.elements)
    assert any(e.name == "q9zz" for e in obs.elements)

    await page.goto(server.site_url("canvas-ui"))
    obs = await engine.observe_page(page=page)
    assert obs.elements == []
