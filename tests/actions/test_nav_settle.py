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

#: R1 추가 페이지 — 기존 HOME 요소 목록은 건드리지 않는다.
PAGES = {
    # 스트리밍 head 문서로 가는 링크(커밋 뒤 dcl 이 1.5초 늦다)
    "/tostream": "<!doctype html><meta charset=utf-8><title>홈</title>"
                 "<a href='/stream?d=1500'>느린 문서로</a>",
    # iframe 만 이동시키는 링크 — 메인 문서는 그대로
    "/iframe": "<!doctype html><meta charset=utf-8><title>홈</title>"
               "<iframe name=f src='/frame' width=300 height=100></iframe>"
               "<a href='/result?d=0' target=f>프레임만 이동</a>",
    "/frame": "<!doctype html><meta charset=utf-8><title>프레임</title><p>프레임</p>",
    # 같은 문서 안 이동(hash) — 문서 요청이 없다
    "/hash": "<!doctype html><meta charset=utf-8><title>홈</title>"
             "<a href='#section'>섹션으로</a>"
             "<button onclick=\"location.hash='#x';location.href='/nocontent'\">해시 후 빈 응답</button>"
             "<div style='height:3000px'></div><h2 id=section>섹션</h2>",
    # keydown 핸들러가 동기적으로 location 을 바꾼다 — 요청이 키 입력 중에 곧바로 나간다
    "/keynav": "<!doctype html><meta charset=utf-8><title>홈</title>"
               "<input id=q aria-label='검색어' "
               "onkeydown=\"if(event.key==='Enter'){location.href='/result?d=800'}\">",
}


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
            elif u.path == "/stream":
                # 스트리밍 head: 머리를 먼저 보내 문서를 커밋시키고, 나머지는 d ms 뒤에 보낸다.
                # domcontentloaded 는 커밋 뒤 d ms 늦게 온다.
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    head = ("<!doctype html><meta charset=utf-8><title>느린 문서</title>"
                            "<!--" + "x" * 4096 + "--><p>머리</p>")
                    self.wfile.write(head.encode())
                    self.wfile.flush()
                    time.sleep(d / 1000)
                    self.wfile.write("<p>본문</p>".encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            elif u.path in PAGES:
                body = PAGES[u.path]
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


# (g-1') 경계 — setTimeout(100) JS 이동은 창(200ms) 안이다: 기다린다.
# 실측 요청 시작 max 155ms(유휴)·161ms(CPU 부하) — 창을 줄이면 여기서 잡힌다.
@requires_chromium
async def test_js_location_change_after_100ms_is_inside_window(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?late=100")
        r = await _click(engine, page, d, "늦은 이동")
        url_at_return = page.url
        await b.close()
    assert r.success, r.error_message
    assert "/result" in url_at_return, url_at_return
    assert r.data.get("nav_committed") is True, r.data
    assert r.reobserve_required is True


# (g-2) 경계 — setTimeout(400) 이동은 창 밖: 기다리지 않는다(알려진 한계로 고정).
# 창을 400ms 이상으로 늘리면 여기서 잡힌다.
@requires_chromium
async def test_js_location_change_after_window_is_not_waited(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?late=400")
        t0 = time.perf_counter()
        r = await _click(engine, page, d, "늦은 이동")
        elapsed = (time.perf_counter() - t0) * 1000
        url_at_return = page.url
        await b.close()
    assert r.success, r.error_message
    assert "/result" not in url_at_return
    assert "nav_wait_ms" not in r.data, r.data
    assert elapsed < dmod.NAV_DETECT_MS + 300, elapsed


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


# ---- R1: 검증 B절 생존 뮤턴트를 잡는 테스트 -------------------------------------


# 감지 창 크기 — 검증 D절 실측으로 고른 값. 요청 시작 지연 최악(부하 setTimeout(100))
# 161ms 보다 커야 놓치지 않고, 이동 없는 모든 click/press_key 가 이 값만큼 느려지므로
# 크게 잡지 않는다.
def test_detect_window_covers_measured_worst_case_without_overpaying():
    assert 170 <= dmod.NAV_DETECT_MS <= 250, dmod.NAV_DETECT_MS


# (R1-a, M2c) 커밋만 보고 반환하지 않는다 — 스트리밍 head 로 커밋 뒤 dcl 이 1.5초 늦는
# 문서에서, 반환 시 문서가 아직 loading 이면 안 된다.
@requires_chromium
async def test_streaming_head_waits_for_domcontentloaded_not_just_commit(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/tostream")
        r = await _click(engine, page, d, "느린 문서로")
        state = await page.evaluate("document.readyState")
        url = page.url
        await b.close()
    assert r.success, r.error_message
    assert "/stream" in url, url
    assert state != "loading", state
    assert r.data.get("nav_committed") is True, r.data
    assert "nav_timed_out" not in r.data, r.data
    assert r.data["nav_wait_ms"] >= 1200, r.data


# (R1-b, M6) iframe 만 이동시키는 클릭은 메인 문서 이동이 아니다 — 기다리지 않는다.
@requires_chromium
async def test_iframe_only_navigation_is_not_waited(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/iframe")
        t0 = time.perf_counter()
        r = await _click(engine, page, d, "프레임만 이동")
        elapsed = (time.perf_counter() - t0) * 1000
        url = page.url
        frame_urls = [f.url for f in page.frames]
        await b.close()
    # 사후조건은 메인 문서만 보므로 프레임 이동은 "변화 없음"으로 판정될 수 있다(기존 동작,
    # 이 테스트 범위 밖). 여기서는 메인 문서 대기를 하지 않았는지만 본다.
    assert r.error_code is None or r.error_code.value != "E_PAGE_CRASHED", r.error_message
    assert url.endswith("/iframe"), url
    assert any("/result" in u for u in frame_urls), frame_urls  # 프레임은 실제로 옮겨갔다
    assert "nav_wait_ms" not in r.data, r.data
    assert "nav_committed" not in r.data, r.data
    assert elapsed < dmod.NAV_DETECT_MS + 1000, elapsed


# (R1-c, M7) 대기 리스너가 남지 않는다 — 이동 있는/없는 액션 여러 번 뒤 page 리스너 수 불변.
# 공개 API 에 리스너 수 조회가 없어 Playwright 구현 객체(_impl_obj, pyee EventEmitter)의
# listeners() 를 본다. page.on/remove_listener 가 바로 이 객체에 위임된다.
_WATCH_EVENTS = ("request", "response", "requestfailed", "framenavigated",
                 "domcontentloaded", "download")


def _listener_counts(page):
    return {ev: len(page._impl_obj.listeners(ev)) for ev in _WATCH_EVENTS}


@requires_chromium
async def test_nav_watch_leaves_no_page_listeners(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/?d=0")
        before = _listener_counts(page)
        for _ in range(3):
            r = await _click(engine, page, d, "더하기")  # 이동 없음
            assert r.success, r.error_message
            await page.focus("#q")
            rk = await d.dispatch(ActionType.PRESS_KEY, {"key": "a"})  # 이동 없음
            assert rk.success, rk.error_message
        rn = await _click(engine, page, d, "결과로 가기")  # 이동 있음
        assert rn.data.get("nav_committed") is True, rn.data
        after = _listener_counts(page)
        await b.close()
    assert after == before, (before, after)


# (R1-d, M10) press_enter 없는 type_text 는 문서 이동 감시를 하지 않는다 — 입력마다
# 감지 창만큼 느려지면 안 된다. 창을 1초로 키워 차이를 분명히 한다.
@requires_chromium
async def test_type_text_without_enter_has_no_nav_watch(server, monkeypatch):
    from playwright.async_api import async_playwright

    monkeypatch.setattr(dmod, "NAV_DETECT_MS", 1000)
    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/")
        eid = await _element(engine, page, "검색어")
        t0 = time.perf_counter()
        r = await d.dispatch(ActionType.TYPE_TEXT,
                             {"element_id": eid, "epoch": engine.epoch, "text": "abc"})
        elapsed = (time.perf_counter() - t0) * 1000
        await b.close()
    assert r.success, r.error_message
    assert "nav_wait_ms" not in r.data, r.data
    assert elapsed < 500, elapsed


# (R1-e, M12) hash 이동(같은 문서)은 문서 요청이 없다 — framenavigated 가 떠도 커밋으로
# 치지 않는다. 해시를 바꾼 직후 204 로 가는 버튼에서, 문서 요청은 시작됐지만 새 문서는
# 없으므로 nav_committed 는 False 여야 한다.
@requires_chromium
async def test_hash_link_is_not_a_document_commit(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/hash")
        r = await _click(engine, page, d, "섹션으로")
        rh = await _click(engine, page, d, "해시 후 빈 응답")
        await b.close()
    assert r.success, r.error_message
    assert "nav_committed" not in r.data, r.data
    assert "nav_wait_ms" not in r.data, r.data
    # 204 응답은 Chromium 이 내비게이션을 취소해 requestfailed 로 먼저 올 수도 있다.
    assert rh.data.get("nav_aborted") in ("status_204", "request_failed"), rh.data
    assert rh.data.get("nav_committed") is False, rh.data


# (R1-f, M1b) press_key 도 리스너를 키 입력 **전에** 단다 — keydown 핸들러가 동기적으로
# location 을 바꾸고 잠시 바쁘면, 문서 요청이 키 입력이 끝나기 전에 이미 나간다.
@requires_chromium
async def test_press_key_listener_attached_before_key(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, page, engine, d = await _open(pw, server + "/keynav")
        await page.focus("#q")
        r = await d.dispatch(ActionType.PRESS_KEY, {"key": "Enter"})
        url_at_return = page.url
        await b.close()
    assert r.success, r.error_message
    assert "/result" in url_at_return, url_at_return
    assert r.data.get("nav_committed") is True, r.data
