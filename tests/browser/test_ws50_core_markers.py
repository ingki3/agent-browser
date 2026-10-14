"""WS-50 BrowserCore: 에이전트 닫기 표시(close_tab 으로 탭 0) · 닫힌 영속 컨텍스트 감지 (실 Chromium, 로컬만)."""

from __future__ import annotations

import asyncio

import pytest

from browser import SessionStore
from contracts import ErrorCode

from test_browser import requires_chromium  # noqa: F401


@requires_chromium
async def test_close_tab_marks_emptied_by_agent(tmp_path):
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        t1 = await core.new_tab("p")
        t2 = await core.new_tab("p")
        await core.close_tab(t1.tab_id)
        assert core.tabs_emptied_by_close_tab is False  # 아직 탭이 남음
        await core.close_tab(t2.tab_id)
        assert core.tabs_emptied_by_close_tab is True
        t3 = await core.new_tab("p")  # 새 탭 등록 → 표시 해제
        assert core.tabs_emptied_by_close_tab is False
        await t3.page.close()  # close_tab 이 아닌 경로(사람·사이트 JS)로 탭 0
        for _ in range(50):
            if not core.tabs():
                break
            await asyncio.sleep(0.02)
        assert core.tabs() == []
        assert core.tabs_emptied_by_close_tab is False


@requires_chromium
async def test_persistent_context_closed_outside_is_detected(tmp_path):
    from browser import BrowserCore
    from browser.core import BrowserCoreError

    core = BrowserCore(persistent_profile=tmp_path / "prof")
    await core.start()
    try:
        await core.new_context("s")
        await core.new_tab("s")
        assert core.persistent_closed is False
        await core.context_for("s").close()  # 밖에서 닫힘
        assert core.persistent_closed is True
        with pytest.raises(BrowserCoreError) as ei:
            await core.new_tab("s")
        assert ei.value.code is ErrorCode.PAGE_CRASHED
        assert "1개만" not in str(ei.value)  # 죽은 컨텍스트를 '사용 중' 으로 오해하던 문구가 아님
        await core.close_persistent()
        await core.open_persistent()
        assert core.persistent_closed is False
        await core.new_context("s")
        tab = await core.new_tab("s")
        assert tab.page.url == "about:blank"
    finally:
        await core.close()
