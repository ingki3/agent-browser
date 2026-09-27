"""디스패처 tab_control "create" 명령 (WS-26b, 검증 NB-1). 가짜 코어로 빠르게 고정."""

from __future__ import annotations

from types import SimpleNamespace

from contracts import ActionType, ErrorCode


class _Page:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url


class _Core:
    def __init__(self) -> None:
        self._tabs = [SimpleNamespace(tab_id="tab-1", page=_Page("http://a/"))]
        self.active_tab_id = "tab-1"
        self.active_profile = "p"

    async def new_tab(self, profile, url=None):
        tab = SimpleNamespace(tab_id=f"tab-{len(self._tabs) + 1}", page=_Page(url or "about:blank"))
        self._tabs.append(tab)
        self.active_tab_id = tab.tab_id
        return tab

    def tabs(self):
        return list(self._tabs)

    def get_tab(self, tab_id):
        return next((t for t in self._tabs if t.tab_id == tab_id), None)

    def set_active_tab(self, tab_id):
        self.active_tab_id = tab_id


def _dispatcher():
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    core = _Core()
    ctx = DispatchContext(page=core._tabs[0].page, engine=PerceptionEngine(), core=core)
    return ActionDispatcher(ctx), core


async def test_create_command_opens_tab():
    d, core = _dispatcher()
    d.ctx.root_page = object()
    r = await d.dispatch(ActionType.TAB_CONTROL, {"command": "create", "url": "http://b/"})
    assert r.success is True, r.error_message
    assert r.data["tab_id"] == "tab-2" and r.data["url"] == "http://b/"
    assert d.ctx.tab_id == "tab-2" and d.ctx.page is core._tabs[1].page
    assert d.ctx.root_page is None


async def test_legacy_new_command_still_works():
    d, _core = _dispatcher()
    r = await d.dispatch(ActionType.TAB_CONTROL, {"command": "new"})
    assert r.success is True, r.error_message
    assert r.data["tab_id"] == "tab-2"


async def test_switch_resets_root_page():
    d, core = _dispatcher()
    await d.dispatch(ActionType.TAB_CONTROL, {"command": "create"})
    d.ctx.root_page = object()
    r = await d.dispatch(ActionType.TAB_CONTROL, {"command": "switch", "tab_id": "tab-1"})
    assert r.success is True, r.error_message
    assert d.ctx.page is core._tabs[0].page
    assert d.ctx.root_page is None


async def test_unknown_command_message_lists_create():
    d, _core = _dispatcher()
    r = await d.dispatch(ActionType.TAB_CONTROL, {"command": "open"})
    assert r.success is False
    assert r.error_code is ErrorCode.FEATURE_NOT_IMPLEMENTED
    assert "create" in r.error_message
