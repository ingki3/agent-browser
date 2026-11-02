"""WS-77 실 서버 경로(창이 실제로 뜸) — serve --browser on-demand 에서 제어 창 [다 했어요] · 요청 만료 신호.

requires_display: macOS(또는 DISPLAY 있는 Linux)에서만 돈다. 창 없는 핵심 경로는 test_ws77_control_window.py.
'사람' 은 제어 창 대상에 직접 보낸 CDP Input(시험 전용 경로)으로 흉내 낸다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from contracts import ActionType
from interface import on_demand
from interface.mcp_server import BrowserMCPServer, envelope_dict, tool_name
from interface.server_control import NEXT_OBSERVE, TELL_WINDOW

from test_run_cli import requires_chromium
from test_ws34_on_demand_e2e import _headed, _headed_after_close, _wait_window, requires_display
from test_ws77_control_window import human_click, site  # noqa: F401  (픽스처 재사용)


async def _tool(s: BrowserMCPServer, action: ActionType, **args: Any) -> Dict[str, Any]:
    return envelope_dict(await s.call_tool(tool_name(action), args))


async def _human_login(s: BrowserMCPServer) -> None:
    page = s._page
    await page.fill("#user", "someone")
    await page.fill("#pw", "synthetic-human-pass")
    async with page.expect_navigation():
        await page.click("#go")


@requires_chromium
@requires_display
async def test_on_demand_window_done_returns_headless_and_records_login(tmp_path, site):  # noqa: F811
    async with BrowserMCPServer(browser_mode="on-demand", handoff_root=tmp_path / "servers",
                                nav_settle=False, recipes=False) as s:
        assert (await _tool(s, ActionType.NAVIGATE, url=site + "/login"))["success"]
        r = await s.call_server_tool("browser_control_request", {"reason": "로그인해 주세요", "secret_wanted": True})
        assert r["success"], r
        assert r["data"]["window"]["state"] == "headed" and _headed(s) == 1  # 제어 창은 같은 브라우저(프로세스 1개)
        hh = r["data"]["human_handoff"]
        assert hh["control_window"] == "open" and hh["tell_user"] == TELL_WINDOW and hh["expires_at"]
        cw = s._control_window
        assert all(t.page is not cw.page for t in s._core.all_tabs())
        await _human_login(s)  # take 없이 창에서 바로 로그인
        await human_click(cw)
        got = await s.call_server_tool("browser_control_wait", {"timeout_s": 30})
        assert got["data"]["changed"] == "released", got
        assert got["data"]["human_handoff"]["release_source"] == "window_done"
        assert got["data"]["human_handoff"]["human_activity_seen"] == "seen"
        assert got["data"]["human_handoff"]["next_step"] == NEXT_OBSERVE
        await _wait_window(s, "headless")
        assert await _headed_after_close(s) == 0
        assert not any(str(t.page.url).startswith("data:") for t in s._core.all_tabs())  # 복원 탭에 제어 창 없음
        obs = await _tool(s, ActionType.OBSERVE_PAGE)
        assert obs["success"], obs
        assert await s._page.inner_text("#who") == "로그인됨"  # 창에서 한 로그인이 headless 로 이어진다
        assert s._headless_retry.logins.memory.get(site) == "human_control"


@requires_chromium
@requires_display
@pytest.mark.parametrize("login", [True, False])
async def test_on_demand_request_expiry_reports_signals_and_records(tmp_path, site, monkeypatch, login):  # noqa: F811
    monkeypatch.setattr(on_demand, "REQUEST_WINDOW_TTL_S", 4.0)
    async with BrowserMCPServer(browser_mode="on-demand", handoff_root=tmp_path / "servers",
                                nav_settle=False, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + ("/login" if login else "/home"))
        r = await s.call_server_tool("browser_control_request", {"reason": "로그인", "secret_wanted": login})
        assert r["data"]["human_handoff"]["expires_at"]
        cw = s._control_window
        if login:
            await _human_login(s)  # 사람이 로그인했지만 [다 했어요] 는 누르지 않음
        got = await s.call_server_tool("browser_control_wait", {"timeout_s": 30})
        assert got["data"]["changed"] == "request_expired", got
        hh = got["data"]["human_handoff"]
        assert hh["state"] == "request_expired" and hh["next_step"] == NEXT_OBSERVE
        assert hh["human_activity_seen"] == ("seen" if login else "not_seen")
        assert hh["tab_origins"]["now"] == [site]
        assert not cw.is_open
        await _wait_window(s, "headless")
        if login:
            assert s._headless_retry.logins.memory.get(site) == "human_control"  # B5: 만료도 기록
        else:
            assert s._headless_retry.logins.memory == {}
        await asyncio.sleep(0)
