"""WS-77 후속 — 감시 태스크가 반납을 먼저 처리하는 동안(기록 전 await 구간) 들어온 control_wait/status 경합.

CI(#219 릴리스 게이트)에서 ``browser_control_wait`` 가 창 [다 했어요] 반납을 ``none_pending`` 으로 놓쳤다: 감시 태스크가
hub 를 이미 requested=False 로 바꾼 뒤 로그인 기록(``_login_watch_finish``) 등 await 를 지나는 동안 wait 가 들어오면
'끝난 인계' 표시가 아직 없어 반납 사실·human_handoff 를 못 받는다. 여기서는 그 await 지점(로그인 기록 직후)에 지연을
넣어 wait/status 가 반드시 그 틈에 들어오게 한다 — 수정 전 실패, 수정 후 통과.

사보타주(보고서 기록): ``_poll_handoff`` 의 ``_begin_handoff_end()`` 호출을 지우면 이 파일의 시험이 none_pending 으로 실패한다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Tuple

import pytest

from contracts import ActionType
from interface import handoff, on_demand
from interface.mcp_server import BrowserMCPServer
from interface.server_control import NEXT_OBSERVE

from test_run_cli import requires_chromium
from test_ws34_on_demand_e2e import requires_display
from test_ws77_control_window import _server, _tool, human_click, site  # noqa: F401  (픽스처 재사용)


def _hold_after_login_record(s: BrowserMCPServer) -> Tuple[asyncio.Event, asyncio.Event]:
    """반납 처리의 로그인 기록이 끝난 바로 뒤(끝난 인계 기록 전)에서 한 번 멈춘다 — entered 가 서면 틈 안이다."""
    orig = s._login_watch_finish
    entered, go = asyncio.Event(), asyncio.Event()
    armed = {"on": True}

    async def held() -> None:
        had_watch = s._login_watch is not None
        await orig()
        if armed["on"] and had_watch and not s.hub.requested and s.hub.holder == "agent":
            armed["on"] = False
            entered.set()
            await go.wait()

    s._login_watch_finish = held  # type: ignore[method-assign]
    return entered, go


async def _in_gap(entered: asyncio.Event, go: asyncio.Event, call: Any) -> Any:
    await asyncio.wait_for(entered.wait(), 10)
    task = asyncio.ensure_future(call)
    await asyncio.sleep(0.3)  # 틈 안에서 응답을 정하게 한다(수정 전: 여기서 이미 none_pending 으로 끝남)
    go.set()
    return await asyncio.wait_for(task, 15)


async def _login(s: BrowserMCPServer) -> None:
    page = s._page
    await page.fill("#user", "someone")
    await page.fill("#pw", "synthetic-human-pass")
    async with page.expect_navigation():
        await page.click("#go")


@requires_chromium
async def test_wait_entering_mid_release_reports_release(tmp_path, site):  # noqa: F811
    async with _server(tmp_path, recipes=False) as s:
        assert (await _tool(s, ActionType.NAVIGATE, url=site + "/login"))["success"]
        r = await s.call_server_tool("browser_control_request", {"reason": "로그인해 주세요", "secret_wanted": True})
        assert r["success"], r
        cw = s._control_window
        await _login(s)
        entered, go = _hold_after_login_record(s)
        await human_click(cw)
        got = await _in_gap(entered, go, s.call_server_tool("browser_control_wait", {"timeout_s": 10}))
        assert got["data"]["changed"] == "released", got
        hh = got["data"]["human_handoff"]
        assert hh["state"] == "released" and hh["release_source"] == "window_done"
        assert hh["human_activity_seen"] == "seen" and hh["next_step"] == NEXT_OBSERVE
        again = await s.call_server_tool("browser_control_wait", {"timeout_s": 1})
        assert again["data"]["changed"] == "none_pending"  # 한 번만 알린다


@requires_chromium
@pytest.mark.parametrize("source", ["window_done", "cli"])
async def test_status_entering_mid_release_shows_ended_handoff(tmp_path, site, source):  # noqa: F811
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        await s.call_server_tool("browser_control_request", {"reason": "캡차"})
        cw = s._control_window
        entered, go = _hold_after_login_record(s)
        if source == "window_done":
            await human_click(cw)
        else:
            handoff.write_command(s.hub.root, s.hub.server_id, "release")
        st = await _in_gap(entered, go, s.call_server_tool("browser_control_status", {}))
        hh = st["data"].get("human_handoff")
        assert hh is not None and hh["state"] == "released" and hh["release_source"] == source, st
        assert hh["next_step"] == NEXT_OBSERVE
        if source == "window_done":
            got = await s.call_server_tool("browser_control_wait", {"timeout_s": 5})
            assert got["data"]["changed"] == "released", got  # status 가 보여 줘도 wait 는 그 반납을 한 번 듣는다


@requires_chromium
@requires_display
async def test_on_demand_wait_entering_mid_expiry_reports_expired(tmp_path, site, monkeypatch):  # noqa: F811
    monkeypatch.setattr(on_demand, "REQUEST_WINDOW_TTL_S", 3.0)
    async with BrowserMCPServer(browser_mode="on-demand", handoff_root=tmp_path / "servers",
                                nav_settle=False, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/login")
        r = await s.call_server_tool("browser_control_request", {"reason": "로그인", "secret_wanted": True})
        assert r["data"]["human_handoff"]["expires_at"]
        await _login(s)
        entered, go = _hold_after_login_record(s)
        got = await _in_gap(entered, go, s.call_server_tool("browser_control_wait", {"timeout_s": 10}))
        assert got["data"]["changed"] == "request_expired", got
        hh = got["data"]["human_handoff"]
        assert hh["state"] == "request_expired" and hh["human_activity_seen"] == "seen"
        assert hh["next_step"] == NEXT_OBSERVE
