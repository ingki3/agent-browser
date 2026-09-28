"""이동 대기 스위치 (WS-28) — DispatchContext.nav_settle.

WS-25 의 "이동 뒤 새 문서 대기"는 이동이 없는 click/press_key 도 감지 창(NAV_DETECT_MS)만큼
느리게 만든다. 속도가 우선이고 스스로 wait_for 로 기다리는 호출자를 위해 끌 수 있게 한다.
기본값은 on — 켜진 경우 동작은 WS-25 와 완전히 같다(tests/actions/test_nav_settle.py 무수정 통과).
로컬 서버만 쓴다(외부 요청 없음). 서버·헬퍼는 WS-25 테스트의 것을 재사용한다.
"""

from __future__ import annotations

import statistics
import time

from actions import dispatcher as dmod
from contracts import ActionType

from test_nav_settle import (  # noqa: F401 - 픽스처·헬퍼 재사용
    _click,
    _element,
    _listener_counts,
    requires_chromium,
    server,
)

_NAV_KEYS = ("nav_wait_ms", "nav_committed", "nav_timed_out", "nav_aborted")


def test_dispatch_context_nav_settle_default_true():
    from actions import DispatchContext

    ctx = DispatchContext(page=None, engine=None)
    assert ctx.nav_settle is True


async def _open(pw, url, *, nav_settle):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    b = await pw.chromium.launch(headless=True)
    ctx = await b.new_context(accept_downloads=True)
    page = await ctx.new_page()
    await page.goto(url)
    engine = PerceptionEngine()
    d = ActionDispatcher(DispatchContext(page=page, engine=engine, nav_settle=nav_settle))
    return b, page, engine, d


async def _no_nav_latencies(pw, url, *, nav_settle, n=20):
    """이동 없는 click / press_key 각 n 회의 지연(ms)."""
    b, page, engine, d = await _open(pw, url, nav_settle=nav_settle)
    try:
        eid = await _element(engine, page, "더하기")
        clicks, keys = [], []
        for _ in range(n):
            t0 = time.perf_counter()
            r = await d.dispatch(ActionType.CLICK, {"element_id": eid, "epoch": engine.epoch})
            clicks.append((time.perf_counter() - t0) * 1000)
            assert r.success, r.error_message
            assert not any(k in r.data for k in _NAV_KEYS), r.data
        await page.focus("#q")
        for _ in range(n):
            t0 = time.perf_counter()
            rk = await d.dispatch(ActionType.PRESS_KEY, {"key": "a"})
            keys.append((time.perf_counter() - t0) * 1000)
            assert rk.success, rk.error_message
            assert not any(k in rk.data for k in _NAV_KEYS), rk.data
    finally:
        await b.close()
    return statistics.median(clicks), statistics.median(keys)


# off 면 이동 없는 click / press_key 가 감지 창만큼 빨라진다(각 20회 p50).
@requires_chromium
async def test_off_removes_detect_window_latency(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        on_click, on_key = await _no_nav_latencies(pw, server + "/", nav_settle=True)
        off_click, off_key = await _no_nav_latencies(pw, server + "/", nav_settle=False)
    print(f"p50 click on={on_click:.1f} off={off_click:.1f} key on={on_key:.1f} off={off_key:.1f}")
    win = dmod.NAV_DETECT_MS
    # on 은 감지 창을 다 기다린다(이동 없으면 창 끝까지).
    assert on_click >= win * 0.9, on_click
    assert on_key >= win * 0.9, on_key
    # off 는 창의 절반 이상 확실히 짧다(여유 있는 기준).
    assert off_click <= on_click - win * 0.5, (on_click, off_click)
    assert off_key <= on_key - win * 0.5, (on_key, off_key)
    assert off_key < win * 0.5, off_key


# off: Enter 폼 제출 — 결과 문서(400ms 지연)를 기다리지 않는다 → 옛 문서 기준 결과.
@requires_chromium
async def test_off_enter_submit_does_not_wait(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=400", nav_settle=False)
        await page.focus("#q")
        await page.keyboard.type("키보드")
        r = await d.dispatch(ActionType.PRESS_KEY, {"key": "Enter"})
        url_at_return = page.url
        # 호출자 책임: 스스로 기다리면 새 문서가 뜬다.
        await page.wait_for_url("**/result**", timeout=5000)
        await b.close()
    assert r.success, r.error_message
    assert "/result" not in url_at_return, url_at_return
    assert "/result" not in r.current_url
    assert not any(k in r.data for k in _NAV_KEYS), r.data


# 같은 페이지·지연에서 on 은 새 문서 기준 결과(대조군).
@requires_chromium
async def test_on_enter_submit_waits_same_fixture(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=400", nav_settle=True)
        await page.focus("#q")
        await page.keyboard.type("키보드")
        r = await d.dispatch(ActionType.PRESS_KEY, {"key": "Enter"})
        url_at_return = page.url
        await b.close()
    assert "/result" in url_at_return, url_at_return
    assert r.data.get("nav_committed") is True, r.data


# off: 클릭이 곧(30ms) JS 로 이동시키는 버튼 — 느린 응답(400ms)을 기다리지 않는다.
# on 은 같은 버튼에서 새 문서까지 기다린다(대조군).
@requires_chromium
async def test_click_js_navigation_off_returns_old_document_on_waits(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=400", nav_settle=False)
        r_off = await _click(engine, page, d, "곧 이동")
        url_off = page.url
        await b.close()
        b, page, engine, d = await _open(pw, server + "/?d=400", nav_settle=True)
        r_on = await _click(engine, page, d, "곧 이동")
        url_on = page.url
        await b.close()
    # off 는 옛 문서 기준으로 판정한다 — 효과가 아직 안 보여 사후조건 실패로 돌아올 수 있다
    # (WS-25 이전 동작). 성공 여부는 고정하지 않고, 기다리지 않았다는 사실만 본다.
    assert "/result" not in url_off, url_off
    assert "/result" not in r_off.current_url, r_off.current_url
    assert not any(k in r_off.data for k in _NAV_KEYS), r_off.data
    assert "/result" in url_on, url_on
    assert r_on.data.get("nav_committed") is True, r_on.data


# off: 링크 클릭(400ms 지연) — nav 대기 흔적이 없다. Playwright click() 은 링크 이동의
# 커밋까지는 자체적으로 기다릴 수 있어 URL 은 새 것일 수 있다(대기 여부는 data 로 본다).
@requires_chromium
async def test_off_link_click_has_no_nav_wait_info(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=400", nav_settle=False)
        r = await _click(engine, page, d, "결과로 가기")
        await b.close()
    assert r.success, r.error_message
    assert not any(k in r.data for k in _NAV_KEYS), r.data


# off: _NavWatch 가 만들어지지 않는다 — 생성 횟수 0, page 리스너 수 전후 동일.
@requires_chromium
async def test_off_never_creates_nav_watch(server, monkeypatch):
    from playwright.async_api import async_playwright

    created = []
    real = dmod._NavWatch

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, page):
            created.append(page)
            super().__init__(page)

    monkeypatch.setattr(dmod, "_NavWatch", Spy)
    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=0", nav_settle=False)
        before = _listener_counts(page)
        r = await _click(engine, page, d, "더하기")
        assert r.success, r.error_message
        await page.focus("#q")
        rk = await d.dispatch(ActionType.PRESS_KEY, {"key": "a"})
        assert rk.success, rk.error_message
        rn = await _click(engine, page, d, "결과로 가기")
        assert rn.success, rn.error_message
        # 좌표 클릭(click x/y) 경로도 같은 스위치를 따른다.
        await page.go_back()
        box = await page.locator("#add").bounding_box()
        rc = await d.dispatch(ActionType.CLICK, {"x": int(box["x"] + 5), "y": int(box["y"] + 5),
                                                "epoch": engine.epoch})
        assert rc.success, rc.error_message
        assert not any(k in rc.data for k in _NAV_KEYS), rc.data
        after = _listener_counts(page)
        await b.close()
    assert created == [], len(created)
    assert after == before, (before, after)


# on(기본) 은 같은 액션들에서 _NavWatch 를 만든다 — 위 스파이가 제대로 동작함을 보인다.
@requires_chromium
async def test_on_creates_nav_watch_for_click_and_press_key(server, monkeypatch):
    from playwright.async_api import async_playwright

    created = []
    real = dmod._NavWatch

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, page):
            created.append(page)
            super().__init__(page)

    monkeypatch.setattr(dmod, "_NavWatch", Spy)
    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=0", nav_settle=True)
        await _click(engine, page, d, "더하기")
        await page.focus("#q")
        await d.dispatch(ActionType.PRESS_KEY, {"key": "a"})
        box = await page.locator("#add").bounding_box()
        await d.dispatch(ActionType.CLICK, {"x": int(box["x"] + 5), "y": int(box["y"] + 5),
                                                "epoch": engine.epoch})
        await b.close()
    assert len(created) == 3, len(created)
