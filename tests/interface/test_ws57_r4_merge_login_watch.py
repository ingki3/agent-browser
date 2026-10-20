"""WS-57 × WS-58 병합 — 사람 조작권은 브라우저 전체: 로그인 감시·로그인 기록이 작업 공간 분리 뒤에도 모든 탭을 덮는다.

조작권(take~release) 동안의 Network 감시는 공유 영속 컨텍스트에 붙으므로, 사람이 *다른 작업 공간*의 탭에서
로그인해도 기록된다. 로그인 기록은 origin 단위(쿠키도 작업 공간끼리 공유)라 어느 작업 공간의 이름 재시도도
그 사이트로 가지 않는다. 반납은 모든 작업 공간의 관찰 근거를 무효로 한다. 로컬 루프백·합성 값만.
"""
from __future__ import annotations

import pytest

from browser import login_ledger as ll
from interface.mcp_server import BrowserMCPServer
from test_run_cli import requires_chromium
from test_ws58_login_ledger_retry import _release, _take, native, serve_site


@pytest.fixture
def site():
    with serve_site() as state:
        yield state


async def _nav(server, url, workspace=None):
    args = {"url": url}
    if workspace is not None:
        args["workspace"] = workspace
    return await server.call_tool("browser_navigate", args)


@requires_chromium
async def test_human_login_in_other_workspace_tab_is_recorded_for_every_workspace(site):
    from browser.workspace import workspace_scope

    site.login_cookies = ["signed_in=synthetic-session; Path=/; Max-Age=3600"]  # 비 HttpOnly — 규칙 (b) 밖
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert (await _nav(server, site.url + "/form", "watch")).success
        with workspace_scope("watch"):
            watch_page = server._page
            watch_epoch = server._engine.epoch
        assert watch_page is not server._page  # 기본 작업 공간과 다른 탭
        default_epoch = server._engine.epoch
        assert server.open_handoff()
        await _take(server)  # 조작권은 기본 작업 공간 문맥이 집는다
        # 사람이 '감시' 작업 공간 탭에서 로그인 POST
        await watch_page.evaluate("(u) => fetch(u, {method: 'POST', body: 'x'}).then(r => r.text())",
                                  site.url + "/login")
        assert len(site.received("/login")) == 1
        assert server._login_watch.posted == {site.url}
        await _release(server)
        # POST(network_request) + 쿠키 기준점 뒤에 생긴 쿠키(cookie_change, 쿠키 도메인 — 포트 없음)
        assert server._headless_retry.logins.memory == {site.url: ll.SOURCE_HUMAN_CONTROL,
                                                         "http://127.0.0.1": ll.SOURCE_HUMAN_CONTROL}
        # 반납은 모든 작업 공간의 관찰 근거를 무효로 한다(WS-57 R1 R3)
        assert server._engine.epoch > default_epoch
        with workspace_scope("watch"):
            assert server._engine.epoch > watch_epoch
        # 로그인 기록은 origin 단위 — 어느 작업 공간의 이름 재시도도 거부된다
        native(await _nav(server, site.url + "/one", "watch"), "login_recorded")
        native(await _nav(server, site.url + "/two"), "login_recorded")
        assert all("HeadlessChrome" in h["user-agent"] for h in site.received("/one") + site.received("/two"))


@requires_chromium
async def test_human_without_login_in_other_workspace_records_nothing(site):
    # 반대 방향(과기록 아님): 다른 작업 공간 탭에서 읽기만 하면 기록이 생기지 않는다.
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert (await _nav(server, site.url + "/form", "watch")).success
        assert server.open_handoff()
        await _take(server)
        from browser.workspace import workspace_scope

        with workspace_scope("watch"):
            await server._page.goto(site.url + "/form")
        await _release(server)
        assert server._headless_retry.logins.memory == {}
