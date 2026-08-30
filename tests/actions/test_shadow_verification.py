"""WS-11 shadow DOM 검증 경로 테스트.

관찰은 shadow 요소를 수집하는데 검증만 document 범위로 조회하면
정상 요소를 '사라졌다'(NODE_DETACHED)거나 값이 None이라고 오판한다.

실측 — MDN 검색창(shadow 내부)에 'flexbox'가 정상 입력됐는데
"기대값 'flexbox' != 실제 None"으로 Silent Failure 처리되어
액션이 3회 연속 실패했다.
"""

from __future__ import annotations

import pytest

from actions.verification import (
    STALENESS_CHECK_SCRIPT,
    StalenessReason,
    capture_state,
    verify_staleness,
)
from perception.engine import ElementHandle

#: shadow root를 만드는 페이지. 파이썬 이스케이프 사고를 피하려고
#: 삼중 따옴표 원시 문자열로 둔다.
SHADOW_INPUT_HTML = """
<div id="host"></div>
<script>
  const root = document.getElementById('host').attachShadow({mode: 'open'});
  root.innerHTML = '<input id="inner" value="채워진값">';
</script>
"""

SHADOW_BUTTON_HTML = """
<div id="host"></div>
<script>
  const root = document.getElementById('host').attachShadow({mode: 'open'});
  root.innerHTML = '<button id="sbtn">섀도우 버튼</button>';
</script>
"""


def test_staleness_script_pierces_shadow():
    """staleness 검증 스크립트가 shadow root를 관통해야 한다."""
    assert "deepQuery" in STALENESS_CHECK_SCRIPT
    assert "shadowRoot" in STALENESS_CHECK_SCRIPT
    assert "__DEEP_QUERY__" not in STALENESS_CHECK_SCRIPT, "플레이스홀더 미치환"


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_capture_state_reads_shadow_input_value():
    """shadow 내부 입력값을 읽어야 Silent Failure 오판이 없다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.set_content(SHADOW_INPUT_HTML)
        await page.wait_for_timeout(150)

        # 전제 확인: 문서 범위로는 찾을 수 없어야 한다.
        assert await page.evaluate(
            "() => document.querySelector('input#inner') === null"
        ), "전제 오류 — shadow가 생성되지 않았습니다"

        handle = ElementHandle(
            element_id="@e1",
            role="textbox",
            name="inner",
            css_path="input#inner",
            epoch=0,
            is_shadow=True,
        )
        state = await capture_state(page, handle)
        assert state.element_value == "채워진값", (
            f"shadow 입력값을 읽지 못함: {state.element_value!r}"
        )
        await browser.close()


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_staleness_does_not_flag_shadow_element_as_detached():
    """shadow 요소를 NODE_DETACHED로 오판하면 안 된다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.set_content(SHADOW_BUTTON_HTML)
        await page.wait_for_timeout(150)

        assert await page.evaluate(
            "() => document.querySelector('button#sbtn') === null"
        ), "전제 오류 — shadow가 생성되지 않았습니다"

        handle = ElementHandle(
            element_id="@e1",
            role="button",
            name="섀도우 버튼",
            css_path="button#sbtn",
            epoch=0,
            is_shadow=True,
        )
        result = await verify_staleness(page, handle, 0)
        assert result.reason is not StalenessReason.NODE_DETACHED, (
            "shadow 요소를 사라졌다고 오판함"
        )
        await browser.close()
