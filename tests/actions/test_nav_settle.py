"""페이지를 옮기는 액션 뒤 새 문서가 뜰 때까지 기다리는지 (WS-25).

실측(2026-09-27 G마켓, v1.5.0): press_key Enter 는 즉시 반환되지만 검색 결과 문서는
0.7~0.9초 뒤에 커밋됐다. 루프가 그 사이 **홈 화면**을 관찰해 scroll 을 골랐고,
scroll 도중 문서가 바뀌어 E_PAGE_CRASHED 가 났다. 액션 직후 짧은 감지 창 안에
메인 프레임 문서 요청이 시작되면 새 문서의 domcontentloaded 까지 기다린다.
로컬 서버만 쓴다(외부 요청 없음). 응답 지연은 쿼리 ?d=<ms> 로 조절한다.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from actions import dispatcher as dmod
from contracts import ActionType


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

HOME = """<!doctype html><meta charset=utf-8><title>홈</title>
<form action='/result'><input id=q name=q aria-label='검색어'><input type=hidden name=d value='{d}'></form>
<a href='/result?d={d}'>결과로 가기</a>
<button id=add onclick="document.getElementById('o').textContent='눌림'+Date.now()">더하기</button>
<button id=late onclick="document.getElementById('o').textContent='예약됨';setTimeout(()=>location.href='/result?d=0', {late})">늦은 이동</button>
<button id=soon onclick="setTimeout(()=>location.href='/result?d={d}', 30)">곧 이동</button>
<a href='/result?d=0' target=_blank>새 탭</a>
<a href='/nocontent'>빈 응답</a>
<a href='/download'>파일 받기</a>
<a href='/fail'>끊김</a>
<p id=o></p>"""


@pytest.fixture
def server():
    hits = {"result": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            d = int((qs.get("d") or ["0"])[0])
            if u.path == "/result":
                hits["result"] += 1
                time.sleep(d / 1000)
                body = "<!doctype html><meta charset=utf-8><title>검색 결과</title><p>상품 1</p>"
            elif u.path == "/nocontent":
                self.send_response(204)
                self.end_headers()
                return
            elif u.path == "/download":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename=a.bin")
                self.end_headers()
                self.wfile.write(b"x" * 10)
                return
            elif u.path == "/fail":
                self.connection.close()
                return
            else:
                late = int((qs.get("late") or ["1000"])[0])
                body = HOME.format(d=d, late=late)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _open(pw, url):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    b = await pw.chromium.launch(headless=True)
    ctx = await b.new_context(accept_downloads=True)
    page = await ctx.new_page()
    await page.goto(url)
    engine = PerceptionEngine()
    return b, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine))


async def _element(engine, page, name):
    obs = await engine.observe_page(page=page, prune_top_n=50)
    for e in obs.elements:
        if e.name == name:
            return e.element_id
    raise AssertionError(f"{name} 없음: {[e.name for e in obs.elements]}")


async def _click(engine, page, d, name):
    eid = await _element(engine, page, name)
    return await d.dispatch(ActionType.CLICK, {"element_id": eid, "epoch": engine.epoch})


# (a) 폼 제출(Enter) — 응답 800ms 지연
@requires_chromium
async def test_enter_submit_waits_for_result_document(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=800")
        await page.focus("#q")
        await page.keyboard.type("키보드")
        r = await d.dispatch(ActionType.PRESS_KEY, {"key": "Enter"})
        url_at_return = page.url
        obs = await engine.observe_page(page=page)
        await b.close()
    assert r.success, r.error_message
    assert "/result" in url_at_return, url_at_return
    assert "/result" in r.current_url
    assert obs.title == "검색 결과"
    assert r.reobserve_required is True
    assert r.data["nav_wait_ms"] >= 500, r.data
    assert r.data.get("nav_committed") is True


# (b) 링크 클릭 — 응답 800ms 지연
@requires_chromium
async def test_link_click_waits_for_result_document(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=800")
        r = await _click(engine, page, d, "결과로 가기")
        url_at_return = page.url
        obs = await engine.observe_page(page=page)
        await b.close()
    assert r.success, r.error_message
    assert "/result" in url_at_return
    assert obs.title == "검색 결과"
    assert r.reobserve_required is True
    assert "nav_wait_ms" in r.data


# (g-1) JS 가 감지 창 안에서 location 변경 → 기다린다
@requires_chromium
async def test_js_location_change_inside_window_waits(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=800")
        r = await _click(engine, page, d, "곧 이동")
        url_at_return = page.url
        await b.close()
    assert r.success, r.error_message
    assert "/result" in url_at_return, url_at_return
    assert r.data.get("nav_committed") is True
    assert r.reobserve_required is True


# (g-2) 창 밖(1초 뒤) 이동은 기다리지 않는다 — 알려진 한계
@requires_chromium
async def test_js_location_change_after_window_is_not_waited(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?late=1000")
        t0 = time.perf_counter()
        r = await _click(engine, page, d, "늦은 이동")
        elapsed = (time.perf_counter() - t0) * 1000
        url_at_return = page.url
        await b.close()
    assert r.success, r.error_message
    assert "/result" not in url_at_return
    assert "nav_wait_ms" not in r.data
    assert elapsed < 900, elapsed


# (c) 이동 없는 클릭 — 추가 지연은 감지 창 + 여유 이하, 결과 동일
@requires_chromium
async def test_click_without_navigation_adds_at_most_detect_window(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/")
        t0 = time.perf_counter()
        r = await _click(engine, page, d, "더하기")
        elapsed = (time.perf_counter() - t0) * 1000
        text = await page.inner_text("#o")
        await page.focus("#q")
        t1 = time.perf_counter()
        rk = await d.dispatch(ActionType.PRESS_KEY, {"key": "a"})
        key_ms = (time.perf_counter() - t1) * 1000
        await b.close()
    assert r.success and text.startswith("눌림")
    assert "nav_wait_ms" not in r.data
    assert r.reobserve_required is False
    assert elapsed < dmod.NAV_DETECT_MS + 400, elapsed
    assert rk.success and "nav_wait_ms" not in rk.data
    assert key_ms < dmod.NAV_DETECT_MS + 200, key_ms


# (d) 응답이 상한보다 오래 → 상한에서 진행, 예외 없음
@requires_chromium
async def test_navigation_longer_than_cap_proceeds_at_cap(server, monkeypatch):
    from playwright.async_api import async_playwright

    monkeypatch.setattr(dmod, "NAV_SETTLE_TIMEOUT_MS", 600)
    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=3000")
        await page.focus("#q")
        t0 = time.perf_counter()
        r = await d.dispatch(ActionType.PRESS_KEY, {"key": "Enter"})
        elapsed = (time.perf_counter() - t0) * 1000
        await b.close()
    assert r.success, r.error_message
    assert r.data.get("nav_timed_out") is True, r.data
    assert 500 <= elapsed < 1500, elapsed


# (e) 요청 실패 / 다운로드 / 204 → 곧바로 진행
@requires_chromium
@pytest.mark.parametrize("name", ["빈 응답", "파일 받기", "끊김"])
async def test_aborted_navigation_proceeds_quickly(server, name):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/")
        t0 = time.perf_counter()
        r = await _click(engine, page, d, name)
        elapsed = (time.perf_counter() - t0) * 1000
        url = page.url
        await b.close()
    assert r.error_code is None or r.error_code.value != "E_PAGE_CRASHED", r.error_message
    assert r.data.get("nav_timed_out") is not True, r.data
    if name != "끊김":
        # 연결 끊김은 Chromium 이 chrome-error 문서를 커밋한다(실제 문서 변화) — 그건 인정.
        assert r.data.get("nav_committed") is not True, r.data
    assert "/result" not in url
    assert elapsed < 2000, elapsed


# (e') Enter 폼 제출이 204/다운로드/연결 끊김 → 새 문서 없이 곧바로 진행(press_key 경로)
@requires_chromium
@pytest.mark.parametrize("path,reason", [("/nocontent", "status_204"),
                                         ("/download", "download"),
                                         ("/fail", "request_failed")])
async def test_enter_to_non_document_proceeds_quickly(server, path, reason):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/")
        await page.evaluate(f"document.forms[0].action = '{path}'")
        await page.focus("#q")
        t0 = time.perf_counter()
        r = await d.dispatch(ActionType.PRESS_KEY, {"key": "Enter"})
        elapsed = (time.perf_counter() - t0) * 1000
        url = page.url
        await b.close()
    assert r.success, r.error_message
    assert r.data.get("nav_aborted") == reason, r.data
    assert r.data.get("nav_committed") is False
    assert r.reobserve_required is False
    assert url.endswith("/"), url
    assert elapsed < 1000, elapsed


# (f) 새 탭 popup 클릭 — popup_tab_id/new_tab 동작 유지, 멈추지 않음
@requires_chromium
async def test_popup_click_unchanged_and_not_stalled(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/")
        t0 = time.perf_counter()
        r = await _click(engine, page, d, "새 탭")
        elapsed = (time.perf_counter() - t0) * 1000
        pages = len(page.context.pages)
        url = page.url
        await b.close()
    assert r.success, r.error_message
    assert any(s.startswith("new_tab") for s in r.data.get("signals", [])), r.data
    assert pages == 2
    assert "/result" not in url
    assert "nav_committed" not in r.data
    assert elapsed < dmod.NAV_DETECT_MS + 1000, elapsed


# 좌표 클릭도 같은 대기를 탄다(Tier-2)
@requires_chromium
async def test_coordinate_click_on_link_waits(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=800")
        box = await page.locator("text=결과로 가기").bounding_box()
        r = await d.dispatch(ActionType.CLICK, {"x": int(box["x"] + 3), "y": int(box["y"] + 3),
                                                "epoch": engine.epoch})
        url_at_return = page.url
        await b.close()
    assert r.success, r.error_message
    assert "/result" in url_at_return
    assert r.reobserve_required is True
