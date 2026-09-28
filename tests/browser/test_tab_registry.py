"""BrowserCore 팝업 탭 등록 (WS-26b, 검증 NB-3).

target=_blank·window.open 으로 열린 페이지도 core.tabs() 에 관리 탭으로 올라와야
tab_control list/switch 로 다룰 수 있다. 팝업은 활성 탭을 바꾸지 않는다(부르는 쪽이
switch 로 옮긴다). 로컬 about:blank 팝업만 쓴다(실사이트 접속 없음).
"""

from __future__ import annotations

import logging

import pytest

from browser import SessionStore

from test_browser import PASSPHRASE, requires_chromium  # noqa: F401


async def _popup(page):
    """window.open 으로 팝업을 열고 그 Page 를 돌려준다."""
    async with page.expect_popup() as info:
        await page.evaluate("window.open('about:blank')")
    popup = await info.value
    # context 'page' 이벤트 처리(등록)가 끝나도록 이벤트 루프를 한 번 양보한다.
    await page.wait_for_timeout(50)
    return popup


@requires_chromium
async def test_new_tab_is_registered_once(tmp_path):
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        t1 = await core.new_tab("p")
        t2 = await core.new_tab("p")
        await t2.page.wait_for_timeout(50)
        assert core.tab_count == 2
        assert [t.tab_id for t in core.tabs()] == [t1.tab_id, t2.tab_id]
        assert len({id(t.page) for t in core.tabs()}) == 2
        assert core.active_tab_id == t2.tab_id


@requires_chromium
async def test_popup_is_registered_without_changing_active_tab(tmp_path):
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        t1 = await core.new_tab("p")
        popup = await _popup(t1.page)
        tabs = core.tabs()
        assert core.tab_count == 2
        assert tabs[1].page is popup
        assert tabs[1].profile_name == "p"
        assert core.active_tab_id == t1.tab_id  # 팝업이 활성 탭을 바꾸지 않는다
        assert core.get_tab(tabs[1].tab_id) is tabs[1]
        # 전환은 부르는 쪽 몫 — 등록된 id 로 옮길 수 있다.
        core.switch_tab(tabs[1].tab_id)
        assert core.active_tab_id == tabs[1].tab_id


@requires_chromium
async def test_closed_popup_leaves_tab_list(tmp_path):
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        t1 = await core.new_tab("p")
        popup = await _popup(t1.page)
        assert core.tab_count == 2
        await popup.close()
        await t1.page.wait_for_timeout(50)
        assert core.tab_count == 1
        assert core.tabs()[0].tab_id == t1.tab_id
        assert core.active_tab_id == t1.tab_id


@requires_chromium
async def test_closing_active_popup_moves_active_to_next_tab(tmp_path):
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        t1 = await core.new_tab("p")
        popup = await _popup(t1.page)
        pop_id = core.tabs()[1].tab_id
        core.switch_tab(pop_id)
        await popup.close()
        await t1.page.wait_for_timeout(50)
        assert core.active_tab_id == t1.tab_id  # close_tab 과 같은 규칙: 남은 첫 탭


@requires_chromium
async def test_close_tab_does_not_double_remove(tmp_path):
    """core.close_tab 뒤 page 'close' 이벤트가 와도 다른 탭을 건드리지 않는다."""
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        t1 = await core.new_tab("p")
        t2 = await core.new_tab("p")
        core.switch_tab(t1.tab_id)
        await core.close_tab(t2.tab_id)
        await t1.page.wait_for_timeout(50)
        assert [t.tab_id for t in core.tabs()] == [t1.tab_id]
        assert core.active_tab_id == t1.tab_id


@requires_chromium
async def test_popup_over_tab_limit_is_not_registered_and_does_not_raise(tmp_path, caplog):
    from browser import BrowserCore

    caplog.set_level(logging.INFO, logger="browser.core")
    async with BrowserCore(
        session_store=SessionStore(tmp_path / "auth"), max_tabs=1
    ) as core:
        t1 = await core.new_tab("p")
        await _popup(t1.page)
        assert core.tab_count == 1
        assert core.active_tab_id == t1.tab_id
    assert any("탭 상한" in r.getMessage() for r in caplog.records)


@requires_chromium
async def test_set_active_tab_is_available_for_dispatcher(tmp_path):
    """디스패처가 부르는 core.set_active_tab 이 있어야 switch/close 가 동작한다."""
    from browser import BrowserCore, BrowserCoreError

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        t1 = await core.new_tab("p")
        await core.new_tab("p")
        core.set_active_tab(t1.tab_id)
        assert core.active_tab_id == t1.tab_id
        with pytest.raises(BrowserCoreError):
            core.set_active_tab("tab-999")


@requires_chromium
async def test_popup_registered_in_session_context(tmp_path):
    from browser import BrowserCore

    store = SessionStore(tmp_path / "auth")
    async with BrowserCore(session_store=store) as core:
        await core.new_context("saved")
        await core.save_session("saved", PASSPHRASE)

    async with BrowserCore(session_store=store) as core2:
        await core2.new_context_with_session("saved", PASSPHRASE)
        t1 = await core2.new_tab("saved")
        popup = await _popup(t1.page)
        assert core2.tab_count == 2
        assert core2.tabs()[1].page is popup
        assert core2.tabs()[1].profile_name == "saved"
        assert core2.active_tab_id == t1.tab_id
        await popup.close()
        await t1.page.wait_for_timeout(50)
        assert core2.tab_count == 1
