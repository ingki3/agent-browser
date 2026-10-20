"""WS-58 R2 — 조작권 감시 설치 공백(검증 NB1).

holder 가 human 으로 바뀐 뒤 감시의 첫 await(쿠키 기준점 읽기·안내 띠 표시 등) 동안 사람이 로그인해도
그 요청을 놓치지 않는다: 요청 감시(Network 계층 ``request``)는 take 와 같은 동기 구간에서 먼저 붙고, 쿠키
기준점은 그 뒤에 읽는다. 검증의 지연 주입 재현(take_install_gap)을 저장소 테스트로 옮긴 것이다.
로컬 루프백 사이트·합성 값만 쓴다.
"""
from __future__ import annotations

import asyncio

import pytest

from browser import login_ledger as ll
from interface.mcp_server import BrowserMCPServer
from test_run_cli import requires_chromium
from test_ws58_login_ledger_retry import _release, _take, native, navigate, serve_site


@pytest.fixture
def site():
    with serve_site() as state:
        yield state


async def _human_post_login(server, site):
    """사람이 창에서 로그인 POST 를 보낸다(비 HttpOnly 세션 쿠키 — 규칙 (b)로는 못 잡는다)."""
    await server._page.evaluate("(u) => fetch(u, {method: 'POST', body: 'x'}).then(r => r.text())",
                                site.url + "/login")
    assert len(site.received("/login")) == 1


async def _after_release_is_native(server, site):
    await _release(server)
    assert server._headless_retry.logins.memory == {site.url: ll.SOURCE_HUMAN_CONTROL}
    native(await navigate(server, site.url + "/one"), "login_recorded")
    assert all("HeadlessChrome" in h["user-agent"] for h in site.received("/one"))
    assert ["signed_in=" in h.get("cookie", "") for h in site.received("/one")] == [True]


@requires_chromium
async def test_login_during_baseline_cookie_read_is_recorded(site, monkeypatch):
    # Verifier NB1 repro: only the baseline `context.cookies()` await is slowed; the human logs in inside it.
    from playwright.async_api import BrowserContext

    site.login_cookies = ["signed_in=synthetic-session; Path=/; Max-Age=3600"]
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await server._page.goto(site.url + "/form")
        assert server.open_handoff()
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = BrowserContext.cookies
        seen = False

        async def delayed(self, *args, **kw):
            nonlocal seen
            if not seen and self == server._page.context:
                seen = True
                entered.set()
                await proceed.wait()
            return await original(self, *args, **kw)

        monkeypatch.setattr(BrowserContext, "cookies", delayed)
        task = asyncio.create_task(_take(server))
        await asyncio.wait_for(entered.wait(), 10)
        assert server.hub.holder == "human"
        await _human_post_login(server, site)
        proceed.set()
        await task
        watch = server._login_watch
        assert watch.posted == {site.url} and watch.degraded is False
        await _after_release_is_native(server, site)


@requires_chromium
async def test_login_before_first_tick_is_recorded(site, monkeypatch):
    # The first await after the take (the banner) comes before the per-tick install: the request
    # watch must already be armed in the same synchronous section as the holder change.
    site.login_cookies = ["signed_in=synthetic-session; Path=/; Max-Age=3600"]
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await server._page.goto(site.url + "/form")
        assert server.open_handoff()
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = server._show_control_banner

        async def delayed(text):
            if text is not None and not entered.is_set():
                entered.set()
                await proceed.wait()
            return await original(text)

        monkeypatch.setattr(server, "_show_control_banner", delayed)
        task = asyncio.create_task(_take(server))
        await asyncio.wait_for(entered.wait(), 10)
        assert server.hub.holder == "human"
        await _human_post_login(server, site)
        proceed.set()
        await task
        assert server._login_watch.posted == {site.url}
        await _after_release_is_native(server, site)


async def test_arm_registers_request_listener_before_any_await():
    # Unit: arm() is synchronous and ensure() never reads cookies before the listener is on.
    from browser.login_watch import ControlLoginWatch

    order = []

    class Ctx:
        pages = []

        def on(self, name, _handler):
            order.append(("on", name))

        async def cookies(self):
            order.append(("cookies",))
            return []

    watch, ctx = ControlLoginWatch(), Ctx()
    watch.arm(ctx)
    assert order == [("on", "request"), ("on", "page")]
    await watch.ensure(ctx)
    assert order == [("on", "request"), ("on", "page"), ("cookies",)]

    fresh, other = ControlLoginWatch(), Ctx()
    order.clear()
    await fresh.ensure(other)  # ensure alone: listener first, then the baseline read
    assert order == [("on", "request"), ("on", "page"), ("cookies",)]
