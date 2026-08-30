"""WS-12 switch_frame 컨텍스트 전환 테스트.

`switch_frame`이 프레임을 찾아 epoch만 올리고 실제 활성 컨텍스트를
바꾸지 않으면, 이후 관찰이 계속 메인 문서를 본다. 전환이 성공했다고
보고되므로 상위에서는 원인을 알 수 없다.

실측 — the-internet /iframe에서 switch_frame이 success=True를 반환했지만
전환 전후 관찰 결과가 19개로 동일했다.
"""

from __future__ import annotations

import pytest

from contracts import ActionType, ErrorCode

FRAME_PAGE = """
<h1>메인 문서</h1>
<button id="main-btn">메인 버튼</button>
<iframe id="inner" srcdoc="<button id='frame-btn'>프레임 버튼</button>"></iframe>
"""


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_switch_frame_changes_active_context():
    """전환 후 활성 컨텍스트가 프레임이어야 한다."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(FRAME_PAGE)
        await page.wait_for_timeout(300)

        engine = PerceptionEngine()
        dispatcher = ActionDispatcher(DispatchContext(page=page, engine=engine))

        result = await dispatcher.dispatch(
            ActionType.SWITCH_FRAME, {"frame_selector": "#inner"}
        )
        assert result.success, result.error_message
        assert dispatcher.ctx.page is not page, (
            "활성 컨텍스트가 바뀌지 않았습니다 — 이후 관찰이 메인 문서를 봅니다"
        )
        assert dispatcher.ctx.root_page is page, "메인 페이지가 보존되지 않았습니다"
        await browser.close()


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_observation_after_switch_sees_frame_elements():
    """전환 후 관찰이 프레임 내부 요소를 보아야 한다."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(FRAME_PAGE)
        await page.wait_for_timeout(300)

        engine = PerceptionEngine()
        dispatcher = ActionDispatcher(DispatchContext(page=page, engine=engine))

        before = await engine.observe_page(page=dispatcher.ctx.page, prune_top_n=20)
        assert any("메인" in e.name for e in before.elements)

        await dispatcher.dispatch(
            ActionType.SWITCH_FRAME, {"frame_selector": "#inner"}
        )
        after = await engine.observe_page(page=dispatcher.ctx.page, prune_top_n=20)

        names = [e.name for e in after.elements]
        assert any("프레임" in n for n in names), f"프레임 요소 미검출: {names}"
        assert not any("메인" in n for n in names), (
            f"전환 후에도 메인 요소가 보임: {names}"
        )
        await browser.close()


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_switch_frame_can_return_to_main():
    """메인 문서로 복귀할 수 있어야 한다."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(FRAME_PAGE)
        await page.wait_for_timeout(300)

        engine = PerceptionEngine()
        dispatcher = ActionDispatcher(DispatchContext(page=page, engine=engine))

        await dispatcher.dispatch(
            ActionType.SWITCH_FRAME, {"frame_selector": "#inner"}
        )
        result = await dispatcher.dispatch(
            ActionType.SWITCH_FRAME, {"to_main": True}
        )
        assert result.success
        assert dispatcher.ctx.page is page
        assert dispatcher.ctx.root_page is None
        await browser.close()


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_switch_frame_bumps_epoch():
    """프레임 전환은 기존 element_id를 무효화해야 한다 (PRD §4.2)."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(FRAME_PAGE)
        await page.wait_for_timeout(300)

        engine = PerceptionEngine()
        dispatcher = ActionDispatcher(DispatchContext(page=page, engine=engine))
        before_epoch = engine.epoch

        await dispatcher.dispatch(
            ActionType.SWITCH_FRAME, {"frame_selector": "#inner"}
        )
        assert engine.epoch > before_epoch
        await browser.close()


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_missing_frame_reports_error():
    """없는 프레임은 명확한 오류를 내야 한다."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(FRAME_PAGE)

        dispatcher = ActionDispatcher(
            DispatchContext(page=page, engine=PerceptionEngine())
        )
        result = await dispatcher.dispatch(
            ActionType.SWITCH_FRAME, {"frame_selector": "#does-not-exist"}
        )
        assert not result.success
        assert result.error_code is ErrorCode.FRAME_NOT_FOUND
        await browser.close()
