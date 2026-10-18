"""WS-57 코어 작업 공간: 탭 묶음·활성 탭이 작업 공간마다 따로, 컨텍스트(쿠키)·탭 상한은 공유.

가짜 playwright(test_core_browser_modes 의 Fake*)로 돈다 — 브라우저 없음.
"""

from __future__ import annotations

import pytest

from browser.core import BrowserCore, BrowserCoreError
from browser.workspace import DEFAULT_WORKSPACE, current_workspace, workspace_scope
from contracts import ErrorCode

from test_core_browser_modes import FakePage, fake_pw  # noqa: F401 - 픽스처 재사용


async def _core(**kw) -> BrowserCore:
    core = BrowserCore(headless=True, **kw)
    await core.start()
    await core.new_context("mcp-session")
    return core


async def test_default_scope_is_the_old_single_session(fake_pw):  # noqa: F811
    core = await _core()
    assert current_workspace() == DEFAULT_WORKSPACE
    t1 = await core.new_tab("mcp-session")
    t2 = await core.new_tab("mcp-session")
    assert [t.tab_id for t in core.tabs()] == [t1.tab_id, t2.tab_id]
    assert core.active_tab_id == t2.tab_id
    assert core.tab_workspace(t1.tab_id) == DEFAULT_WORKSPACE


async def test_tabs_and_active_tab_are_per_workspace(fake_pw):  # noqa: F811
    core = await _core()
    a = await core.new_tab("mcp-session")
    with workspace_scope("watch"):
        b = await core.new_tab("mcp-session")
        assert [t.tab_id for t in core.tabs()] == [b.tab_id]
        assert core.active_tab_id == b.tab_id
        assert core.get_tab(a.tab_id) is None  # 남의 탭은 보이지 않는다
        assert core.foreign_tab_count() == 1
        with pytest.raises(BrowserCoreError) as e:
            core.set_active_tab(a.tab_id)
        assert e.value.code is ErrorCode.TAB_NOT_FOUND
        with pytest.raises(BrowserCoreError):
            await core.close_tab(a.tab_id)
    # 기본 작업 공간은 그대로
    assert [t.tab_id for t in core.tabs()] == [a.tab_id]
    assert core.active_tab_id == a.tab_id
    assert {t.tab_id for t in core.all_tabs()} == {a.tab_id, b.tab_id}
    assert core.tab_workspace(b.tab_id) == "watch"


async def test_closing_active_tab_falls_back_within_own_workspace(fake_pw):  # noqa: F811
    core = await _core()
    a1 = await core.new_tab("mcp-session")
    with workspace_scope("watch"):
        b1 = await core.new_tab("mcp-session")
        b2 = await core.new_tab("mcp-session")
    a2 = await core.new_tab("mcp-session")
    await core.close_tab(a2.tab_id)
    assert core.active_tab_id == a1.tab_id  # 감시 탭(b1)으로 새지 않는다
    with workspace_scope("watch"):
        assert core.active_tab_id == b2.tab_id
        await core.close_tab(b2.tab_id)
        assert core.active_tab_id == b1.tab_id


async def test_new_tab_owner_is_caller_even_if_page_event_runs_elsewhere(fake_pw):  # noqa: F811
    """context 'page' 이벤트가 다른 작업 공간 문맥에서 먼저 등록해도 new_tab 을 부른 쪽이 주인."""
    core = await _core()
    ctx = core.context_for("mcp-session")
    orig_new_page = ctx.new_page

    async def new_page_registering_in_default():
        page = FakePage()
        with workspace_scope(DEFAULT_WORKSPACE):
            ctx.emit_page(page)
        return page

    ctx.new_page = new_page_registering_in_default
    with workspace_scope("watch"):
        tab = await core.new_tab("mcp-session")
        assert core.tab_workspace(tab.tab_id) == "watch"
        assert core.active_tab_id == tab.tab_id
    assert core.get_tab(tab.tab_id) is None
    ctx.new_page = orig_new_page


async def test_popup_belongs_to_the_opener_workspace(fake_pw):  # noqa: F811
    core = await _core()
    with workspace_scope("watch"):
        opener = await core.new_tab("mcp-session")
    # Playwright 순서: context 'page' 이벤트(이벤트 문맥 = 아무 작업 공간) → opener 의 'popup' 이벤트.
    popup = FakePage("http://127.0.0.1/popup")
    ctx = core.context_for("mcp-session")
    ctx.emit_page(popup)
    for cb in opener.page.handlers.get("popup", []):
        cb(popup)
    tab = core.tab_for_page(popup)
    assert tab is not None
    assert core.tab_workspace(tab.tab_id) == "watch"
    assert core.get_tab(tab.tab_id) is None  # 기본 작업 공간에는 안 보임
    with workspace_scope("watch"):
        assert core.get_tab(tab.tab_id) is not None
        assert core.active_tab_id == opener.tab_id  # 팝업은 활성 탭을 바꾸지 않는다(기존 규칙)


async def test_tab_limit_is_shared(fake_pw):  # noqa: F811
    core = await _core(max_tabs=2)
    await core.new_tab("mcp-session")
    with workspace_scope("watch"):
        await core.new_tab("mcp-session")
        with pytest.raises(BrowserCoreError) as e:
            await core.new_tab("mcp-session")
    assert e.value.code is ErrorCode.TAB_LIMIT_EXCEEDED


async def test_page_created_for_another_workspace_is_never_counted_as_mine(fake_pw):  # noqa: F811
    """WS-57 R1(R4): 다른 작업 공간의 new_tab 이 새 페이지를 기다리는 동안('page' 이벤트가 먼저 옴) 그 페이지는
    기본 작업 공간의 탭·탭 수(클릭 사후조건의 '새 탭' 신호)에 잡히지 않는다."""
    import asyncio

    core = await _core()
    a = await core.new_tab("mcp-session")
    ctx = core.context_for("mcp-session")
    gate, emitted = asyncio.Event(), asyncio.Event()

    async def slow_new_page():
        page = FakePage()
        ctx.emit_page(page)
        emitted.set()
        await gate.wait()
        return page

    ctx.new_page = slow_new_page

    async def watch_creates():
        with workspace_scope("watch"):
            return await core.new_tab("mcp-session")

    task = asyncio.create_task(watch_creates())
    await asyncio.wait_for(emitted.wait(), 5)
    assert core.workspace_page_count(ctx.pages) == 1
    assert [t.tab_id for t in core.tabs()] == [a.tab_id]
    gate.set()
    b = await task
    assert core.workspace_page_count(ctx.pages) == 1
    with workspace_scope("watch"):
        assert core.workspace_page_count(ctx.pages) == 1
        assert [t.tab_id for t in core.tabs()] == [b.tab_id]
    # 아무도 만들지 않을 때 생긴 페이지(팝업)는 예전처럼 기본 작업 공간 몫
    ctx.emit_page(FakePage())
    assert core.workspace_page_count(ctx.pages) == 2
