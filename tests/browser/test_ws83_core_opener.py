"""WS-83(B4·B6) — 코어의 opener 기록: 팝업이 닫히면 opener 로, 사슬 이어 붙이기, opener 관계 탭(제어 창 제외).

가짜 Page(이벤트 없음)로 코어 장부만 본다 — 브라우저 없이 결정적.
"""

from __future__ import annotations

from types import SimpleNamespace

from browser.core import BrowserCore, ManagedContext
from browser.workspace import workspace_scope


def _core() -> BrowserCore:
    core = BrowserCore(headless=True)
    core._contexts["p"] = ManagedContext(profile_name="p", context=None)
    return core


def _tab(core: BrowserCore, opener=None):
    page = SimpleNamespace(url="about:blank")
    tab = core._register_page("p", page, popup=True, owner="default")
    if opener is not None:
        core._adopt_popup(opener, page)
    return tab


def test_popup_records_its_opener_and_closing_it_returns_there():
    core = _core()
    with workspace_scope("default"):
        t1 = _tab(core)
        t2 = _tab(core)
        pop = _tab(core, opener=t2)
        assert pop.opener_tab_id == t2.tab_id
        assert t1.opener_tab_id is None and t2.opener_tab_id is None
        core.switch_tab(pop.tab_id)
        core._on_page_closed(pop.tab_id)
        assert core.active_tab_id == t2.tab_id  # 남은 첫 탭(t1)이 아니라 opener


def test_chain_is_reparented_when_the_middle_closes():
    core = _core()
    with workspace_scope("default"):
        root = _tab(core)
        _tab(core)
        mid = _tab(core, opener=root)
        leaf = _tab(core, opener=mid)
        core._on_page_closed(mid.tab_id)
        assert leaf.opener_tab_id == root.tab_id
        core.switch_tab(leaf.tab_id)
        core._on_page_closed(leaf.tab_id)
        assert core.active_tab_id == root.tab_id


def test_no_live_opener_falls_back_to_first_remaining_tab():
    core = _core()
    with workspace_scope("default"):
        first = _tab(core)
        opener = _tab(core)
        pop = _tab(core, opener=opener)
        core._on_page_closed(opener.tab_id)
        assert pop.opener_tab_id is None
        core.switch_tab(pop.tab_id)
        core._on_page_closed(pop.tab_id)
        assert core.active_tab_id == first.tab_id


def test_closing_a_non_active_popup_keeps_the_active_tab():
    core = _core()
    with workspace_scope("default"):
        t1 = _tab(core)
        t2 = _tab(core)
        pop = _tab(core, opener=t1)
        core.switch_tab(t2.tab_id)
        core._on_page_closed(pop.tab_id)
        assert core.active_tab_id == t2.tab_id


def test_related_tabs_are_only_opener_relations_and_skip_excluded_pages():
    core = _core()
    with workspace_scope("default"):
        opener = _tab(core)
        me = _tab(core, opener=opener)
        child = _tab(core, opener=me)
        stranger = _tab(core)
        grandchild = _tab(core, opener=child)
        got = {t.tab_id for t in core.related_tabs(me.tab_id)}
        assert got == {opener.tab_id, child.tab_id}
        assert stranger.tab_id not in got and grandchild.tab_id not in got
        core.exclude_page(child.page)  # WS-77 제어 창처럼 관리 탭에서 뺀 페이지
        after = {t.tab_id for t in core.related_tabs(me.tab_id)}
        assert child.tab_id not in after and opener.tab_id in after
        assert after == {opener.tab_id, grandchild.tab_id}  # 뺀 페이지가 연 탭은 사슬로 이어 붙는다
        assert core.related_tabs("tab-999") == []
