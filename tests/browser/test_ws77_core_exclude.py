"""WS-77 B1 — 코어가 관리 탭에서 뺀 페이지(사람용 제어 창)는 어느 탭 목록·상한·창 감시에도 보이지 않는다."""

from __future__ import annotations

import pytest

from browser import BrowserCore

from test_browser import requires_chromium


@requires_chromium
async def test_excluded_page_leaves_every_tab_view_and_is_not_reregistered():
    core = BrowserCore(headless=True)
    await core.start()
    try:
        agent = await core.new_tab("p")
        context = core.context_for("p")
        other = await context.new_page()  # 'page' 이벤트 경로로 관리 탭이 된다(제어 창과 같은 경로)
        assert core.tab_for_page(other) is not None
        before = core.tab_count
        core.exclude_page(other)
        assert core.tab_for_page(other) is None
        assert core.tab_count == before - 1
        assert [t.page for t in core.all_tabs()] == [agent.page]
        assert list(core.list_tabs()) == [agent.tab_id]
        assert core.is_excluded(other)
        # 같은 페이지가 다시 사건으로 들어와도(팝업 채택 등) 등록하지 않는다.
        assert core._register_page("p", other, popup=True) is None
        # 클릭 사후조건의 '새 탭' 세기에서도 빠진다.
        assert core.workspace_page_count(context.pages) == 1
        # 다른 페이지는 그대로 관리 탭이 된다(플래그 방식이 아님 — 같은 순간 생긴 남의 팝업은 보인다).
        third = await context.new_page()
        assert core.tab_for_page(third) is not None
        await other.close()
        assert core.tab_count == 2  # 뺀 페이지 닫힘은 탭 목록·닫기 표시를 건드리지 않는다
        assert not core.tabs_emptied_by_close_tab
    finally:
        await core.close()
    assert not core._excluded_pages


def test_exclude_unknown_page_is_harmless():
    core = BrowserCore(headless=True)
    page = object()
    core.exclude_page(page)
    assert core.is_excluded(page)
    assert not core.is_excluded(object())
    assert core.workspace_page_count([page]) == 0
    with pytest.raises(Exception):
        core.exclude_page(None)
