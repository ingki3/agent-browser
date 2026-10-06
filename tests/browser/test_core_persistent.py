"""WS-32: BrowserCore 영속 프로필 모드 (persistent_profile) — launch_persistent_context.

가짜 playwright 로 진입점별 설치를 고정한다(launch 인자·context 옵션·route·이벤트), 실제
Chromium 으로 열기/닫기/다시 열기(같은 프로필, headless 바꿔서 — WS-34 기반)를 확인한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from browser.core import BrowserCore, BrowserCoreError

from test_core_browser_modes import FakeContext, FakePage


class _Chromium:
    def __init__(self) -> None:
        self.persistent_calls: List[Dict[str, Any]] = []
        self.launch_calls: List[Dict[str, Any]] = []
        self.contexts: List[FakeContext] = []

    async def launch(self, **kw: Any) -> Any:
        self.launch_calls.append(kw)
        return object()

    async def launch_persistent_context(self, user_data_dir: str, **kw: Any) -> FakeContext:
        self.persistent_calls.append({"user_data_dir": user_data_dir, **kw})
        ctx = FakeContext(pages=[FakePage()], **kw)
        self.contexts.append(ctx)
        return ctx


class _PW:
    def __init__(self) -> None:
        self.chromium = _Chromium()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_pw(monkeypatch):
    import playwright.async_api as pw_api

    pw = _PW()

    class _Starter:
        async def start(self) -> Any:
            return pw

    monkeypatch.setattr(pw_api, "async_playwright", lambda: _Starter())
    return pw


async def test_persistent_uses_launch_persistent_context(fake_pw, tmp_path):
    core = await BrowserCore(persistent_profile=tmp_path / "p").start()
    try:
        assert fake_pw.chromium.launch_calls == []
        call = fake_pw.chromium.persistent_calls[-1]
        assert call["user_data_dir"] == str(tmp_path / "p")
        assert call["headless"] is True
        assert call["viewport"] == core.viewport  # 기존 headless 와 같은 context 옵션
    finally:
        await core.close()


async def test_persistent_human_mode_options(fake_pw, tmp_path):
    core = await BrowserCore(browser_mode="human", persistent_profile=tmp_path / "p").start()
    try:
        call = fake_pw.chromium.persistent_calls[-1]
        assert call["headless"] is False
        assert call["no_viewport"] is True and call["locale"] == "ko-KR"
    finally:
        await core.close()


async def test_persistent_gets_egress_proxy_and_flags(fake_pw, tmp_path):
    from security.egress_runtime import EgressRuntime

    rt = await EgressRuntime().start()
    try:
        core = await BrowserCore(egress=rt, persistent_profile=tmp_path / "p").start()
        await core.close()
    finally:
        await rt.close()
    call = fake_pw.chromium.persistent_calls[-1]
    assert call["proxy"]["server"].startswith("http://127.0.0.1:")
    assert call["proxy"]["username"] and call["proxy"]["password"]
    assert "--disable-quic" in call["args"]
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in call["args"]


async def test_persistent_context_is_adopted_and_first_page_reused(fake_pw, tmp_path):
    core = await BrowserCore(persistent_profile=tmp_path / "p").start()
    try:
        ctx = await core.new_context("mcp-session")
        assert ctx is fake_pw.chromium.contexts[-1]
        assert core.context_for("mcp-session") is ctx
        first = ctx.pages[0]
        tab = await core.new_tab("mcp-session")
        assert tab.page is first  # 빈 about:blank 탭을 창에 남기지 않는다
        # 팝업(context 'page' 이벤트)도 탭으로 등록된다
        popup = FakePage("http://x/")
        ctx.emit_page(popup)
        assert core.tab_for_page(popup) is not None
        with pytest.raises(BrowserCoreError):
            await core.new_context("other")  # 영속 컨텍스트는 1개
    finally:
        await core.close()
    assert ctx.closed  # 영속 컨텍스트는 닫는다(쿠키를 디스크에 남기려면 닫아야 한다)
    assert fake_pw.stopped


async def test_persistent_rejects_session_injection(fake_pw, tmp_path):
    core = await BrowserCore(persistent_profile=tmp_path / "p").start()
    try:
        with pytest.raises(BrowserCoreError):
            await core.new_context_with_session("x", "pw")
    finally:
        await core.close()


def test_persistent_with_user_chrome_rejected(tmp_path):
    with pytest.raises(ValueError, match="user-chrome"):
        BrowserCore(browser_mode="user-chrome", persistent_profile=tmp_path / "p")


async def test_reopen_same_profile_with_other_headless(fake_pw, tmp_path):
    """WS-34 기반: 같은 프로필을 닫고 headless=False 로 다시 여는 단위가 있다."""
    core = await BrowserCore(persistent_profile=tmp_path / "p").start()
    try:
        await core.new_tab("mcp-session")
        first = fake_pw.chromium.contexts[-1]
        await core.close_persistent()
        assert first.closed and core.tab_count == 0 and core.context_count == 0
        await core.open_persistent(headless=False)
        call = fake_pw.chromium.persistent_calls[-1]
        assert call["user_data_dir"] == str(tmp_path / "p") and call["headless"] is False
        tab = await core.new_tab("mcp-session")
        assert tab.page is fake_pw.chromium.contexts[-1].pages[0]
    finally:
        await core.close()


async def test_open_persistent_twice_refused(fake_pw, tmp_path):
    core = await BrowserCore(persistent_profile=tmp_path / "p").start()
    try:
        with pytest.raises(RuntimeError):
            await core.open_persistent()
    finally:
        await core.close()


# ------------------------------------------------------------------ 실제 Chromium


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")


@requires_chromium
async def test_real_persistent_cookie_survives_reopen(tmp_path):
    prof = tmp_path / "p"
    core = await BrowserCore(persistent_profile=prof).start()
    try:
        ctx = await core.new_context("s")
        await ctx.add_cookies([{"name": "keep", "value": "1", "domain": "example.test",
                                "path": "/", "expires": 4102444800}])
        await core.close_persistent()
        await core.open_persistent()
        ctx = await core.new_context("s")
        names = {c["name"] for c in await ctx.cookies("http://example.test/")}
        assert "keep" in names
    finally:
        await core.close()
