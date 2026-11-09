"""WS-89 (#244): press_key 키 뒤 판독(페이지 판독·격리 판독·오버레이 감지)이 함께 쓰는 마감 하나(0.3s).

HEAD 는 키 뒤 판독 둘을 순서대로 각각 0.3s, 그 뒤 오버레이 감지를 0.25s 더 기다려 nav_settle=False 에서도 최악 0.85s
멈췄다 — Enter 제출이면 그 사이 새 문서가 와 '이동을 기다리지 않음'(WS-28) 계약이 깨졌다. 마감은 watch.settle() 뒤에
잡는다(키 시각이 아님 — nav_settle=True 무이동이면 settle 200ms 가 오버레이 예산을 먹어 WS-82 회귀).
판정 의미(페이지 OR 격리·열쇠 일치)는 그대로, 마감 뒤 도착한 격리 판독은 버린다(경계 시험). 로컬 페이지·합성 값만.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from actions import dispatcher as dmod
from contracts import ActionType

from test_nav_settle import requires_chromium, server  # noqa: F401 - 픽스처 재사용
from test_nav_settle_switch import _open

_HANG_S = 5.0


async def _hang(*_a, **_k):
    await asyncio.sleep(_HANG_S)


def _bare():
    from actions import ActionDispatcher, DispatchContext

    return ActionDispatcher(DispatchContext(page=None, engine=None))


class _Top:
    """민감 판정 뒤 sticky 노드 잡기(evaluate_handle)만 — 실패해도 판정은 그대로."""

    async def evaluate_handle(self, *_a):
        raise RuntimeError("no page")


async def _later(delay: float, value):
    await asyncio.sleep(delay)
    return value


# ------------------------------------------------------------------ 단위: _key_plain_after 동시 대기


async def test_both_key_reads_hang_wait_once_not_twice():
    """두 키 판독이 모두 멈춰도 하나의 마감(0.3s) 안에서 함께 끝난다(HEAD 순차 0.6s). 미완 판독은 취소된다."""
    d = _bare()
    page_f = asyncio.ensure_future(_hang())
    iso_f = asyncio.ensure_future(_hang())
    t0 = time.perf_counter()
    await d._key_plain_after(_Top(), page_f, iso_f, "k1")  # noqa: SLF001
    el = time.perf_counter() - t0
    await asyncio.sleep(0)
    assert el <= 0.35, f"_key_plain_after {el:.3f}s"
    assert d.last_key_sensitive is False
    assert page_f.cancelled() and iso_f.cancelled(), (page_f, iso_f)


async def test_no_pending_reads_skips_wait():
    d = _bare()
    t0 = time.perf_counter()
    await d._key_plain_after(_Top(), None, None, "k1")  # noqa: SLF001
    assert time.perf_counter() - t0 < 0.05
    assert d.last_key_sensitive is False


@pytest.mark.parametrize("delay,iso_k,expected", [
    (0.25, "k1", True),    # 마감 안 격리 판독 password·같은 열쇠 → 민감(OR)
    (0.45, "k1", False),   # 마감 뒤 도착 → 버림(일반) — 문서화된 한계(docs/security.md WS-85 한계)
    (0.0, "other", False),  # 열쇠 불일치(포커스가 다른 칸으로) → 일반(R1 B1 유지)
])
async def test_isolated_read_boundary(delay, iso_k, expected):
    d = _bare()
    page_f = asyncio.ensure_future(_hang())  # 페이지 판독은 멈춤
    iso_f = asyncio.ensure_future(_later(delay, {"s": True, "v": "SynthWs89-" + iso_k, "k": iso_k}))
    await d._key_plain_after(_Top(), page_f, iso_f, "k1")  # noqa: SLF001
    for f in (page_f, iso_f):
        f.cancel()
    assert d.last_key_sensitive is expected, (delay, iso_k)


async def test_failed_reads_are_no_read():
    d = _bare()

    async def boom():
        raise RuntimeError("detached")

    page_f = asyncio.ensure_future(boom())
    iso_f = asyncio.ensure_future(boom())
    await d._key_plain_after(_Top(), page_f, iso_f, "k1")  # noqa: SLF001
    assert d.last_key_sensitive is False


# ------------------------------------------------------------------ dispatch: 세 판독 공유 마감


def _hang_all(monkeypatch):
    D = dmod.ActionDispatcher
    monkeypatch.setattr(D, "_key_plain_read", staticmethod(_hang))
    monkeypatch.setattr(D, "_key_field_read", staticmethod(_hang))
    monkeypatch.setattr(D, "_isolated_focus_field", _hang)
    monkeypatch.setattr(D, "_overlay_detect", _hang)


@requires_chromium
async def test_dispatch_press_key_three_reads_hang_shared_deadline(server, monkeypatch):
    """무이동 로컬 페이지·nav_settle=False·세 판독 모두 멈춤 → press_key 반환 ≤0.45s(HEAD 0.86s)."""
    from playwright.async_api import async_playwright

    _hang_all(monkeypatch)
    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/", nav_settle=False)
        try:
            await page.focus("#q")
            t0 = time.perf_counter()
            r = await d.dispatch(ActionType.PRESS_KEY, {"key": "a"})
            el = time.perf_counter() - t0
            typed = await page.evaluate("document.getElementById('q').value")
        finally:
            await b.close()
    assert r.success, r.error_message
    assert typed == "a"
    assert el <= 0.45, f"press_key {el:.3f}s latency_ms={r.data.get('latency_ms')}"
    assert "overlay_opened" not in r.data


@requires_chromium
async def test_dispatch_press_key_sensitive_branch_shared_deadline(server, monkeypatch):
    """민감 칸 갈래(_key_field_after)도 같은 마감 — 키 뒤 판독 멈춤 + 오버레이 멈춤 → ≤0.45s(HEAD 0.55s)."""
    from playwright.async_api import async_playwright

    _hang_all(monkeypatch)
    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/", nav_settle=False)
        try:
            await page.set_content("<input id=p type=password aria-label='pw' value='SynthWs89Pw'>")
            await page.focus("#p")
            t0 = time.perf_counter()
            r = await d.dispatch(ActionType.PRESS_KEY, {"key": "End"})
            el = time.perf_counter() - t0
        finally:
            await b.close()
    assert r.success, r.error_message
    assert d.last_key_sensitive is True
    assert r.data.get("sensitive_input", {}).get("value_read") is False, r.data  # 키 뒤 못 읽음 신호(B4)
    assert el <= 0.45, f"press_key {el:.3f}s latency_ms={r.data.get('latency_ms')}"


@requires_chromium
@pytest.mark.parametrize("nav_settle", [True, False])
async def test_press_key_overlay_signal_kept(server, monkeypatch, nav_settle):
    """WS-82: press_key 뒤 오버레이 감지(0.2s 걸림)는 마감 안이면 그대로 붙는다 — 두 모드 모두(키 시각 마감이면 on 에서 놓침)."""
    from playwright.async_api import async_playwright

    async def slow_ov(self):
        await asyncio.sleep(0.2)
        return {"on": True, "kinds": ["dialog"], "opened": True}

    monkeypatch.setattr(dmod.ActionDispatcher, "_overlay_detect", slow_ov)
    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/", nav_settle=nav_settle)
        try:
            await page.focus("#q")
            r = await d.dispatch(ActionType.PRESS_KEY, {"key": "a"})
        finally:
            await b.close()
    assert r.success, r.error_message
    assert r.data.get("overlay_opened") == {"kinds": ["dialog"]}, r.data


@requires_chromium
async def test_click_overlay_budget_not_shortened_by_earlier_press_key(server, monkeypatch):
    """마감은 사용 뒤·dispatch 시작에 비운다 — 앞 press_key 의 마감이 뒤 click 의 오버레이 예산(0.25s)을 줄이지 않는다."""
    from playwright.async_api import async_playwright

    from test_nav_settle import _element

    calls = {"n": 0}

    async def slow_ov(self):
        calls["n"] += 1
        await asyncio.sleep(0.2)
        return {"on": True, "kinds": ["dialog"], "opened": True}

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/", nav_settle=False)
        try:
            eid = await _element(engine, page, "더하기")
            d._post_key_deadline = time.monotonic() - 1  # noqa: SLF001 - 남은 마감(지난 값)
            monkeypatch.setattr(dmod.ActionDispatcher, "_overlay_detect", slow_ov)
            r = await d.dispatch(ActionType.CLICK, {"element_id": eid, "epoch": engine.epoch})
        finally:
            await b.close()
    assert r.success, r.error_message
    assert r.data.get("overlay_opened") == {"kinds": ["dialog"]}, r.data


# ------------------------------------------------------------------ 서버 경로(KEY_AFTER_DEFER)


@requires_chromium
async def test_server_press_key_deferred_reads_hang_bounded(server, monkeypatch):
    """BrowserMCPServer(nav_settle=False): 미룬 키 뒤 판독(KEY_AFTER_DEFER)도 공유 마감 — 응답까지 HEAD 0.6s 대기가 사라진다."""
    from interface.mcp_server import BrowserMCPServer, tool_name

    async with BrowserMCPServer(nav_settle=False) as srv:
        nav = await srv.call_tool(tool_name(ActionType.NAVIGATE), {"url": server + "/"})
        assert nav.success, nav.error_message
        page = srv._dispatcher.ctx.page  # noqa: SLF001
        await page.focus("#q")
        warm = await srv.call_tool(tool_name(ActionType.PRESS_KEY), {"key": "b"})  # 첫 호출 준비 비용 제외
        assert warm.success, warm.error_message
        t1 = time.perf_counter()
        await srv.call_tool(tool_name(ActionType.PRESS_KEY), {"key": "c"})
        base = time.perf_counter() - t1
        _hang_all(monkeypatch)
        t0 = time.perf_counter()
        r = await srv.call_tool(tool_name(ActionType.PRESS_KEY), {"key": "a"})
        el = time.perf_counter() - t0
    assert r.success, r.error_message
    # 판독 멈춤이 더하는 몫 ≤ 마감 0.3s + 여유 0.15s (HEAD 는 순차 0.6s).
    assert el - base <= 0.45, f"server press_key {el:.3f}s (정상 {base:.3f}s)"
