"""WS-34 R1 — 독립 검증 비차단 지적(NB-2·3·4·5·6) 회귀 고정.

* NB-2 프록시가 죽은 상태에서 창 전환 → 열지 않고 state=failed(+이유), 다음 호출도 열지 않음.
* NB-3 사람이 승인 코드 창을 닫음 → 그 코드 무효·캡처 거부 해제, approve 다시 → 새 창·새 코드로 성공.
* NB-4 대상 이름의 6자리 이상 숫자열 마스킹 + "(사이트가 붙인 이름)" 표기(코드 창·오버레이).
* NB-5 긴 wait_for 진행 중 control_request → 대기를 E_TIMEOUT 으로 끝내고 전환. window_closed 알림 1회.
* NB-6 --profile 서버 기동도 남은 임시 프로필(잠금 풀린 것)을 정리, 살아 있는 서버 것은 그대로.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from contracts import ErrorCode
from interface import code_window, handoff, on_demand
from interface.handoff import write_command
from interface.mcp_server import BrowserMCPServer

from test_run_cli import requires_chromium
from test_ws34_on_demand_e2e import (  # noqa: F401 — site 는 픽스처
    _headed, _inproc, _procs_for, _show_code, _wait_window, requires_display, site,
)


async def _pay_approval(call, site):
    await call("browser_navigate", {"url": site.url + "/pay"})
    obs = await call("browser_observe_page", {})
    by = {e["name"]: e["element_id"] for e in obs["data"]["observation"]["elements"]}
    pay = {"element_id": by["결제하기"], "epoch": obs["data"]["observation"]["snapshot_epoch"]}
    r1 = await call("browser_click", dict(pay))
    assert r1["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value, r1
    return pay, r1["data"]["approval"]["approval_id"]


async def _approve(s: BrowserMCPServer, aid: str, digest: str, code: str):
    nonce = write_command(s.hub.root, s.hub.server_id, "approve", approval_id=aid,
                          action_digest=digest, code=code)
    await s._poll_handoff()
    return await asyncio.to_thread(handoff.wait_ack, s.hub.root, s.hub.server_id, nonce, 5.0)


# ------------------------------------------------------------------ NB-2


@requires_chromium
async def test_nb2_dead_proxy_switch_fails_closed_without_opening(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        assert (await call("browser_navigate", {"url": site.url + "/"}))["success"]
        await s._egress_runtime.proxy.close()
        r = await call("browser_control_request", {"reason": "창"})
        win = r["data"]["window"]
        assert win["state"] == "failed", r
        assert "프록시" in win.get("error", ""), win
        path = s._core.persistent_profile
        assert _procs_for(path) == []  # 창을 열지 않았고 남은 브라우저도 없다
        nav = await call("browser_navigate", {"url": site.url + "/after"})
        assert not nav["success"] and nav["error_code"] == ErrorCode.PAGE_CRASHED.value, nav
        assert "프록시" in nav["data"]["window"].get("error", "")
        assert _procs_for(path) == []


def test_nb2_egress_launch_kwargs_refuse_dead_proxy():
    from security.egress_runtime import EgressRuntime

    rt = EgressRuntime()

    async def go():
        await rt.start()
        assert rt.launch_kwargs()["proxy"]
        await rt.proxy.close()
        with pytest.raises(RuntimeError, match="프록시"):
            rt.launch_kwargs()
        with pytest.raises(RuntimeError, match="프록시"):
            rt.chrome_args()
        await rt.close()
        with pytest.raises(RuntimeError, match="프록시"):
            rt.launch_kwargs()

    asyncio.run(go())


# ------------------------------------------------------------------ NB-3


@requires_chromium
@requires_display
async def test_nb3_closing_code_window_withdraws_code(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        pay, aid = await _pay_approval(call, site)
        got = await _show_code(s, aid)
        assert got["ack"]["ok"], got
        win = s._code_window
        old = (await win.page.inner_text("#code")).replace(" ", "")
        await win.page.close()  # 사람이 코드 창을 닫음
        await s._poll_handoff()
        assert not s._code_on_overlay  # 캡처 거부 해제
        shot = await call("browser_take_screenshot", {})
        assert shot["success"], shot
        ack = await _approve(s, aid, got["digest"], old)
        assert ack is not None and not ack["ok"], ack  # 창에 없는 코드는 무효
        # 사람이 approve 를 다시 실행 → 새 창·새 코드
        got2 = await _show_code(s, aid)
        assert got2["ack"]["ok"], got2
        assert s._code_window.is_open
        new = (await s._code_window.page.inner_text("#code")).replace(" ", "")
        ack2 = await _approve(s, aid, got2["digest"], new)
        assert ack2 is not None and ack2["ok"], ack2
        r2 = await call("browser_click", {**pay, "approval_id": aid})
        assert r2["success"], r2


# ------------------------------------------------------------------ NB-4


@pytest.mark.parametrize("raw", [
    "확인 코드 123456 결제하기", "확인 코드 1 2 3 4 5 6", "코드 １２３４５６７", "code 12-34-56",
])
def test_nb4_mask_code_like_digits(raw):
    out = handoff.mask_code_like(raw)
    assert not re.search(r"\d(?:[\s\-.]?\d){5,}", out), out
    assert "••••••" in out


def test_nb4_short_numbers_kept():
    assert handoff.mask_code_like("사과 3000원 12345") == "사과 3000원 12345"


def test_nb4_code_window_marks_site_name_and_masks():
    page = code_window.render_html(action="click", target="확인 코드 654321 결제", origin="o",
                                   expires_at="e", code="111222", ttl_s=120)
    target = re.search(r"<dd id=target>(.*?)</dd>", page).group(1)
    assert target.startswith("(사이트가 붙인 이름)")
    assert "654321" not in page and "••••••" in target
    assert "1 1 1 2 2 2" in page  # 진짜 코드는 별도 칸에 그대로


def test_nb4_overlay_text_marks_site_name_and_masks():
    from interface.mcp_server import code_overlay_text

    text = code_overlay_text("111222", "click", "확인 코드 654321 결제", 120)
    assert "111222" in text and "654321" not in text
    assert "대상: (사이트가 붙인 이름) 확인 코드 ••••••" in text


# ------------------------------------------------------------------ NB-5


@requires_chromium
@requires_display
async def test_nb5_long_wait_is_ended_for_switch(site, monkeypatch):
    monkeypatch.setattr(on_demand, "DRAIN_TIMEOUT_S", 3.0)
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/"})
        waiting = asyncio.ensure_future(call("browser_wait_for", {
            "condition": "selector", "selector": "#never", "timeout_ms": 60_000}))
        await asyncio.sleep(0.5)
        r = await asyncio.wait_for(call("browser_control_request", {"reason": "창"}), 20)
        assert r["data"]["window"]["state"] == "headed", r
        w = await asyncio.wait_for(waiting, 5)
        assert not w["success"] and w["error_code"] == ErrorCode.TIMEOUT.value, w
        assert "window" in w["data"]


@requires_chromium
@requires_display
async def test_nb5_window_closed_spawns_once(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/"})
        await call("browser_control_request", {"reason": "창"})
        spawned = []
        real = s._spawn_switch

        def counting(*a, **k):
            spawned.append(a)
            return real(*a, **k)

        s._spawn_switch = counting
        s._ctx_closed = True  # 사람이 창을 닫음(컨텍스트 close 이벤트와 같은 상태)
        await s._watch_window()
        await s._watch_window()  # 태스크 첫 틱 전 재진입
        assert len(spawned) == 1, spawned
        await _wait_window(s, "headless")


# ------------------------------------------------------------------ NB-6


def test_nb6_profile_server_cleans_dead_ephemeral(_isolated_profile_root):
    from browser import serve_profile as sp

    live = sp.acquire_ephemeral(server_id="1-aa")
    dead = sp.acquire_ephemeral(server_id="2-bb")
    dead.release()  # 비정상 종료처럼 잠금만 풀리고 폴더가 남음
    s = BrowserMCPServer(browser_mode="headless", profile="work")
    try:
        assert s.acquire_profile() is not None
        assert not dead.path.exists()
        assert live.path.exists()  # 다른 살아 있는 서버의 임시 프로필은 그대로
    finally:
        s.release_profile()
        s.hub.close()
        sp.remove_ephemeral(live)
