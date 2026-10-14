"""WS-50 디스패처: 마지막 탭 닫기 → 빈 탭 교체 · 닫힌 대상 navigate 오류 분류 (가짜 코어·페이지)."""

from __future__ import annotations

from types import SimpleNamespace

from contracts import ActionType, ErrorCode


class _Page:
    def __init__(self, url: str = "about:blank", exc: BaseException = None) -> None:
        self.url = url
        self.closed = False
        self._exc = exc

    async def close(self):
        self.closed = True

    async def goto(self, url, **_kw):
        if self._exc is not None:
            raise self._exc
        self.url = url


class _Core:
    def __init__(self, n: int = 1) -> None:
        self._tabs = [SimpleNamespace(tab_id=f"tab-{i + 1}", page=_Page(f"http://a/{i}"), profile_name="p")
                      for i in range(n)]
        self.active_tab_id = "tab-1"
        self.active_profile = "p"
        self.order: list = []

    async def new_tab(self, profile, url=None):
        self.order.append(("new", profile))
        tab = SimpleNamespace(tab_id=f"tab-{len(self._tabs) + 10}", page=_Page(url or "about:blank"),
                              profile_name=profile)
        self._tabs.append(tab)
        self.active_tab_id = tab.tab_id
        return tab

    async def close_tab(self, tab_id):
        self.order.append(("close", tab_id))
        tab = self.get_tab(tab_id)
        self._tabs.remove(tab)
        await tab.page.close()

    def tabs(self):
        return list(self._tabs)

    def get_tab(self, tab_id):
        return next((t for t in self._tabs if t.tab_id == tab_id), None)

    def set_active_tab(self, tab_id):
        self.active_tab_id = tab_id


def _dispatcher(core):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    ctx = DispatchContext(page=core._tabs[0].page, engine=PerceptionEngine(), core=core, tab_id="tab-1")
    return ActionDispatcher(ctx)


async def test_closing_last_tab_opens_blank_first():
    core = _Core(1)
    d = _dispatcher(core)
    r = await d.dispatch(ActionType.TAB_CONTROL, {"command": "close", "tab_id": "tab-1"})
    assert r.success is True, r.error_message
    assert core.order == [("new", "p"), ("close", "tab-1")]  # 빈 탭이 먼저 — 탭 0 순간 없음
    assert r.data["remaining"] == 1
    assert r.data["replaced_with_blank"] is True
    assert r.data["hint"] and "\n" not in r.data["hint"]
    assert r.reobserve_required is True
    blank = core.tabs()[0]
    assert blank.page.url == "about:blank"
    assert core.active_tab_id == blank.tab_id
    assert d.ctx.tab_id == blank.tab_id and d.ctx.page is blank.page


async def test_closing_one_of_two_tabs_is_unchanged():
    core = _Core(2)
    d = _dispatcher(core)
    r = await d.dispatch(ActionType.TAB_CONTROL, {"command": "close", "tab_id": "tab-1"})
    assert r.success is True
    assert core.order == [("close", "tab-1")]
    assert "replaced_with_blank" not in r.data and "hint" not in r.data
    assert r.data["closed"] == "tab-1" and r.data["remaining"] == 1


async def test_closing_unknown_tab_still_tab_not_found():
    core = _Core(1)
    d = _dispatcher(core)
    r = await d.dispatch(ActionType.TAB_CONTROL, {"command": "close", "tab_id": "tab-9"})
    assert r.error_code is ErrorCode.TAB_NOT_FOUND
    assert core.order == []


class TargetClosedError(Exception):
    """playwright._impl._errors.TargetClosedError 와 같은 이름(분류는 이름·문구로 한다)."""


async def _navigate_with(exc):
    core = _Core(1)
    core._tabs[0].page = _Page("http://a/", exc=exc)
    d = _dispatcher(core)
    d.ctx.page = core._tabs[0].page
    return await d.dispatch(ActionType.NAVIGATE, {"url": "http://b/"})


async def test_navigate_on_closed_target_is_page_crashed():
    r = await _navigate_with(TargetClosedError("Page.goto: Target page, context or browser has been closed"))
    assert r.success is False
    assert r.error_code is ErrorCode.PAGE_CRASHED


async def test_navigate_closed_message_without_type_is_page_crashed():
    r = await _navigate_with(RuntimeError("Target page, context or browser has been closed"))
    assert r.error_code is ErrorCode.PAGE_CRASHED


async def test_navigate_timeout_stays_navigate_timeout():
    r = await _navigate_with(TimeoutError("Timeout 30000ms exceeded."))
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
