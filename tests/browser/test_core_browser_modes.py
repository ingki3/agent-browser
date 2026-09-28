"""BrowserCore 브라우저 방식 3종 (WS-27): headless / human / user-chrome.

가짜 playwright·가짜 launch_user_chrome 으로 브라우저 없이 수명주기를 고정한다.
  - headless: launch(headless=True) + 고정 viewport (현행 그대로)
  - human: launch(headless=False) + no_viewport + locale=ko-KR (위장 없음)
  - user-chrome: launch_user_chrome → connect_over_cdp(127.0.0.1) → 기본 컨텍스트 채택
    (browser.new_context 호출 없음), 컨텍스트 1개 상한, close 시 기본 컨텍스트는 닫지 않고
    우리가 띄운 Chrome 은 keep_open 이 아니면 종료, 실패 시 띄운 Chrome 정리.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from browser import core as core_mod
from browser import user_chrome
from browser.core import BROWSER_MODES, BrowserCore, BrowserCoreError


# ---------------------------------------------------------------- 가짜 playwright


class FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.handlers: Dict[str, List[Any]] = {}
        self.closed = False
        self.gotos: List[str] = []

    def on(self, event: str, cb: Any) -> None:
        self.handlers.setdefault(event, []).append(cb)

    async def goto(self, url: str, **_: Any) -> None:
        self.url = url
        self.gotos.append(url)

    async def close(self) -> None:
        self.closed = True
        for cb in self.handlers.get("close", []):
            cb(self)


class FakeContext:
    def __init__(self, pages: Optional[List[FakePage]] = None, **options: Any) -> None:
        self.options = options
        self.pages: List[FakePage] = list(pages or [])
        self.handlers: Dict[str, List[Any]] = {}
        self.closed = False
        self.routes: List[str] = []

    def on(self, event: str, cb: Any) -> None:
        self.handlers.setdefault(event, []).append(cb)

    def emit_page(self, page: FakePage) -> None:
        self.pages.append(page)
        for cb in self.handlers.get("page", []):
            cb(page)

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.emit_page(page)  # Playwright 도 new_page 에 'page' 이벤트를 낸다
        return page

    async def new_cdp_session(self, page: Any) -> Any:
        return ("cdp", page)

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append(pattern)

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, contexts: Optional[List[FakeContext]] = None) -> None:
        self.contexts: List[FakeContext] = list(contexts or [])
        self.new_context_calls: List[Dict[str, Any]] = []
        self.closed = False

    async def new_context(self, **kw: Any) -> FakeContext:
        self.new_context_calls.append(kw)
        ctx = FakeContext(**kw)
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, *, connect_error: Optional[BaseException] = None) -> None:
        self.launch_calls: List[Dict[str, Any]] = []
        self.connect_calls: List[str] = []
        self.connect_error = connect_error
        self.browser: Optional[FakeBrowser] = None
        self.default_context: Optional[FakeContext] = None

    async def launch(self, **kw: Any) -> FakeBrowser:
        self.launch_calls.append(kw)
        self.browser = FakeBrowser()
        return self.browser

    async def connect_over_cdp(self, endpoint: str, **_: Any) -> FakeBrowser:
        self.connect_calls.append(endpoint)
        if self.connect_error is not None:
            raise self.connect_error
        self.default_context = FakeContext(pages=[FakePage("about:blank")])
        self.browser = FakeBrowser(contexts=[self.default_context])
        return self.browser


class FakePW:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeUC:
    def __init__(self, port: int = 9555) -> None:
        self.port = port
        self.close_calls = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self, timeout: float = 10.0) -> None:
        self.close_calls += 1


@pytest.fixture
def fake_pw(monkeypatch):
    """playwright.async_api.async_playwright 를 가짜로 바꾼다."""
    import playwright.async_api as pw_api

    state: Dict[str, Any] = {"chromium": FakeChromium(), "pw": None}

    class _Starter:
        async def start(self) -> FakePW:
            state["pw"] = FakePW(state["chromium"])
            return state["pw"]

    monkeypatch.setattr(pw_api, "async_playwright", lambda: _Starter())
    return state


@pytest.fixture
def fake_launch(monkeypatch):
    """browser.user_chrome.launch_user_chrome 를 가짜로 바꾼다(프로세스 없음)."""
    rec: Dict[str, Any] = {"calls": [], "uc": FakeUC(), "error": None}

    async def _launch(**kw: Any) -> FakeUC:
        rec["calls"].append(kw)
        if rec["error"] is not None:
            raise rec["error"]
        return rec["uc"]

    monkeypatch.setattr(user_chrome, "launch_user_chrome", _launch)
    return rec


# ---------------------------------------------------------------- 방식 이름


def test_browser_modes_are_three():
    assert BROWSER_MODES == ("headless", "human", "user-chrome")


def test_unknown_browser_mode_rejected():
    with pytest.raises(ValueError):
        BrowserCore(browser_mode="stealth")


# ---------------------------------------------------------------- headless / human


async def test_headless_mode_launches_headless_with_fixed_viewport(fake_pw):
    core = await BrowserCore(browser_mode="headless").start()
    await core.new_context("p")
    chromium = fake_pw["chromium"]
    assert chromium.launch_calls == [{"headless": True}]
    assert chromium.connect_calls == []
    assert chromium.browser.new_context_calls == [{"viewport": {"width": 1280, "height": 720}}]
    await core.close()
    assert chromium.browser.closed is True
    assert fake_pw["pw"].stopped is True


async def test_default_core_is_headless(fake_pw):
    core = await BrowserCore().start()
    assert core.browser_mode == "headless"
    assert fake_pw["chromium"].launch_calls == [{"headless": True}]
    await core.close()


async def test_human_mode_launches_window_without_viewport(fake_pw):
    core = await BrowserCore(browser_mode="human").start()
    await core.new_context("p")
    chromium = fake_pw["chromium"]
    assert chromium.launch_calls == [{"headless": False}]
    assert chromium.browser.new_context_calls == [{"no_viewport": True, "locale": "ko-KR"}]
    assert core.headless is False and core.human_like is True
    await core.close()


async def test_human_mode_overrides_headless_true(fake_pw):
    """human 은 창이 보여야 한다 — headless=True 를 같이 줘도 창을 띄운다."""
    core = await BrowserCore(browser_mode="human", headless=True).start()
    assert fake_pw["chromium"].launch_calls == [{"headless": False}]
    await core.close()


# ---------------------------------------------------------------- user-chrome


async def test_user_chrome_launches_and_connects_loopback(fake_pw, fake_launch, tmp_path):
    prof = tmp_path / "prof"
    core = await BrowserCore(browser_mode="user-chrome", chrome_profile=prof).start()
    chromium = fake_pw["chromium"]
    assert len(fake_launch["calls"]) == 1
    assert fake_launch["calls"][0]["profile_dir"] == prof
    assert chromium.launch_calls == []  # Playwright 번들 Chromium 은 띄우지 않는다
    assert chromium.connect_calls == ["http://127.0.0.1:9555"]
    await core.close()


async def test_user_chrome_default_profile_dir(fake_pw, fake_launch):
    core = await BrowserCore(browser_mode="user-chrome").start()
    assert fake_launch["calls"][0]["profile_dir"] == user_chrome.DEFAULT_PROFILE_DIR
    await core.close()


async def test_user_chrome_adopts_default_context(fake_pw, fake_launch, tmp_path):
    core = await BrowserCore(browser_mode="user-chrome", chrome_profile=tmp_path / "p").start()
    ctx = await core.new_context("mcp-session")
    chromium = fake_pw["chromium"]
    assert ctx is chromium.default_context
    assert chromium.browser.new_context_calls == []  # incognito 컨텍스트를 만들지 않는다
    assert core.context_for("mcp-session") is chromium.default_context
    # 같은 이름 재요청은 같은 컨텍스트
    assert await core.new_context("mcp-session") is ctx
    await core.close()


async def test_user_chrome_first_tab_reuses_initial_blank_page(fake_pw, fake_launch, tmp_path):
    core = await BrowserCore(browser_mode="user-chrome", chrome_profile=tmp_path / "p").start()
    await core.new_context("s")
    blank = fake_pw["chromium"].default_context.pages[0]
    tab = await core.new_tab("s")
    assert tab.page is blank  # 이미 열린 about:blank 를 첫 탭으로 채택
    assert core.tab_count == 1
    assert core.active_tab_id == tab.tab_id
    # 두 번째 new_tab 은 새 탭
    tab2 = await core.new_tab("s", "http://127.0.0.1:1/x")
    assert tab2.page is not blank
    assert core.tab_count == 2
    assert tab2.page.gotos == ["http://127.0.0.1:1/x"]
    assert await core.new_cdp_session(tab.tab_id) == ("cdp", blank)
    await core.close()


async def test_user_chrome_second_profile_rejected(fake_pw, fake_launch, tmp_path):
    core = await BrowserCore(browser_mode="user-chrome", chrome_profile=tmp_path / "p").start()
    await core.new_context("a")
    with pytest.raises(BrowserCoreError) as ei:
        await core.new_context("b")
    assert "user-chrome" in str(ei.value)
    assert fake_pw["chromium"].browser.new_context_calls == []
    await core.close()


async def test_user_chrome_session_injection_rejected(fake_pw, fake_launch, tmp_path):
    core = await BrowserCore(browser_mode="user-chrome", chrome_profile=tmp_path / "p").start()
    with pytest.raises(BrowserCoreError) as ei:
        await core.new_context_with_session("naver", "pass")
    assert "user-chrome" in str(ei.value)
    assert fake_pw["chromium"].browser.new_context_calls == []
    await core.close()


async def test_user_chrome_close_keeps_default_context_and_closes_chrome(
    fake_pw, fake_launch, tmp_path
):
    core = await BrowserCore(browser_mode="user-chrome", chrome_profile=tmp_path / "p").start()
    ctx = await core.new_context("s")
    await core.new_tab("s")
    await core.close()
    assert ctx.closed is False  # 기본 컨텍스트를 닫으면 Chrome 창이 사라진다
    assert fake_launch["uc"].close_calls == 1
    assert fake_pw["pw"].stopped is True
    assert core.tab_count == 0 and core.context_count == 0


async def test_user_chrome_keep_open_leaves_chrome(fake_pw, fake_launch, tmp_path):
    core = await BrowserCore(
        browser_mode="user-chrome", chrome_profile=tmp_path / "p", keep_open=True
    ).start()
    ctx = await core.new_context("s")
    await core.close()
    assert fake_launch["uc"].close_calls == 0
    assert ctx.closed is False
    assert fake_pw["pw"].stopped is True


async def test_user_chrome_connect_failure_closes_launched_chrome(fake_pw, fake_launch, tmp_path):
    fake_pw["chromium"].connect_error = ConnectionError("cdp 연결 실패")
    with pytest.raises(ConnectionError):
        await BrowserCore(browser_mode="user-chrome", chrome_profile=tmp_path / "p").start()
    assert fake_launch["uc"].close_calls == 1
    assert fake_pw["pw"].stopped is True


async def test_user_chrome_connect_failure_closes_chrome_even_with_keep_open(
    fake_pw, fake_launch, tmp_path
):
    """keep_open 은 정상 종료 때만 — 시작 실패로 남은 Chrome 은 반드시 정리한다."""
    fake_pw["chromium"].connect_error = ConnectionError("x")
    with pytest.raises(ConnectionError):
        await BrowserCore(
            browser_mode="user-chrome", chrome_profile=tmp_path / "p", keep_open=True
        ).start()
    assert fake_launch["uc"].close_calls == 1


async def test_user_chrome_launch_failure_stops_playwright(fake_pw, fake_launch, tmp_path):
    fake_launch["error"] = RuntimeError("Chrome 이 바로 종료됨")
    with pytest.raises(RuntimeError):
        await BrowserCore(browser_mode="user-chrome", chrome_profile=tmp_path / "p").start()
    assert fake_pw["pw"] is None or fake_pw["pw"].stopped is True
    assert fake_pw["chromium"].connect_calls == []


async def test_user_chrome_usual_profile_rejected_before_any_process(fake_pw, monkeypatch):
    """평소 Chrome 프로필이면 ValueError — Chrome 프로세스도 playwright 도 시작하지 않는다."""
    spawned: List[Any] = []
    monkeypatch.setattr(
        user_chrome.subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("Popen")
    )
    usual = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    with pytest.raises(ValueError):
        await BrowserCore(browser_mode="user-chrome", chrome_profile=usual).start()
    assert spawned == []
    assert fake_pw["pw"] is None  # playwright 도 띄우지 않음


async def test_user_chrome_popup_registered_as_tab(fake_pw, fake_launch, tmp_path):
    """채택한 기본 컨텍스트에도 WS-26b 팝업 등록이 붙는다."""
    core = await BrowserCore(browser_mode="user-chrome", chrome_profile=tmp_path / "p").start()
    await core.new_context("s")
    t1 = await core.new_tab("s")
    popup = FakePage("http://127.0.0.1:1/popup")
    fake_pw["chromium"].default_context.emit_page(popup)
    tabs = core.tabs()
    assert core.tab_count == 2
    assert tabs[1].page is popup and tabs[1].profile_name == "s"
    assert core.active_tab_id == t1.tab_id  # 팝업은 활성 탭을 바꾸지 않는다
    await popup.close()
    assert core.tab_count == 1
    await core.close()


def test_core_module_has_no_stealth():
    """위장 금지 계약: 코어 소스에 UA 변경·webdriver 숨기기·stealth 흔적이 없다."""
    src = Path(core_mod.__file__).read_text(encoding="utf-8")
    for bad in ("user_agent", "add_init_script", "stealth", "AutomationControlled"):
        assert bad not in src, bad
