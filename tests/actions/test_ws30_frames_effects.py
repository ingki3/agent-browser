"""WS-30 항목 3·4: 중첩 iframe 전환 의미 + 놓치던 효과 신호.

로컬 MockServer / set_content 만 쓴다(실사이트 없음).

3  switch_frame: 현재 프레임 기준(상대) 먼저, 없으면 최상위 문서 기준(절대). 성공 data 에
   frame_url·frame_depth·frame_path·child_frames, 실패 data 에 현재 프레임 URL·iframe 목록.
   프레임 안 take_screenshot 은 내부 예외 대신 최상위 페이지로 찍는다.
4a type_text(press_enter) 로 새 문서로 이동 → 이동을 효과로 인정.
4b 프레임 안 클릭: 부모 문서/새 탭 변화를 본다(capture_state 가 프레임 문서만 보던 원인).
4c confirm() 이 뜨는 클릭: dialog_opened:<type> 신호 + data.dialogs.
공통: 진짜 무효과 클릭은 여전히 E_TIMEOUT (음성 테스트).
"""

from __future__ import annotations

import pytest

from contracts import ActionType, ErrorCode

requires_chromium = pytest.mark.requires_chromium


@pytest.fixture
def mock_server():
    from harness import MockServer

    with MockServer() as srv:
        yield srv


async def _setup(pw, url=None, html=None):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()
    if url:
        await page.goto(url)
    else:
        await page.set_content(html)
    await page.wait_for_timeout(300)
    engine = PerceptionEngine()
    return browser, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine))


async def _eid(d, name):
    obs = await d.ctx.engine.observe_page(page=d.ctx.page)
    for e in obs.elements:
        if e.name == name:
            return e.element_id
    raise AssertionError([e.name for e in obs.elements])


async def _click(d, name):
    return await d.dispatch(
        ActionType.CLICK, {"element_id": await _eid(d, name), "epoch": d.ctx.engine.epoch}
    )


# ------------------------------------------------------------------ 3. 중첩 iframe


@requires_chromium
async def test_nested_switch_relative_then_click(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, mock_server.site_url("s05_iframe"))
        top = await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "iframe"})
        assert top.success
        assert top.data["frame_url"].endswith("/s05_iframe/outer")
        assert top.data["frame_depth"] == 1
        assert top.data["child_frames"] == [
            {"selector_hint": "#inner", "url": mock_server.base_url + "/s05_iframe/inner"}
        ]
        # 같은 셀렉터를 다시 주면 **현재 프레임 안의** iframe 으로 들어간다(헛돌기 결함).
        inner = await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "iframe"})
        assert inner.success
        assert inner.data["frame_url"].endswith("/s05_iframe/inner"), inner.data
        assert inner.data["frame_depth"] == 2
        assert inner.data["child_frames"] == []
        assert inner.data["resolved_from"] == "current_frame"
        eid = await _eid(d, "프레임 내부 결제")
        r = await d.dispatch(ActionType.CLICK, {"element_id": eid, "epoch": engine.epoch})
        # 클릭은 inner 문서의 버튼에 실제로 닿는다(포커스가 그 버튼으로 간다).
        focused = await d.ctx.page.evaluate("document.activeElement && document.activeElement.id")
        assert focused == "pay-inner"
        # 이 목업 버튼에는 핸들러가 없다 — 진짜 무효과이므로 여전히 Silent Failure 다
        # (WS-30 실측: 비교 시험 L04 의 E_TIMEOUT 은 이 경우였다).
        assert r.error_code is ErrorCode.TIMEOUT
        await browser.close()


NESTED_HTML = """<p id=msg>결제 전</p>
<script>addEventListener('message', e => { if (e.data === 'paid')
  document.getElementById('msg').textContent = '결제 코드 발급됨'; });</script>
<iframe id=outer srcdoc="<p>외부</p><iframe id=inner srcdoc=&quot;<button id=b
 onclick=&amp;quot;window.top.postMessage('paid','*')&amp;quot;>프레임 내부 결제</button>&quot;></iframe>"></iframe>"""


@requires_chromium
async def test_nested_inner_click_with_top_effect_succeeds():
    """outer→inner 로 들어가 inner 버튼을 누르면(최상위 문서가 바뀜) 성공이다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=NESTED_HTML)
        assert (await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "iframe"})).success
        inner = await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "iframe"})
        assert inner.data["frame_depth"] == 2
        r = await _click(d, "프레임 내부 결제")
        assert r.success, (r.error_code, r.error_message)
        assert any(s.startswith("top:") for s in r.data["signals"]), r.data
        assert await page.text_content("#msg") == "결제 코드 발급됨"
        await browser.close()


@requires_chromium
async def test_switch_falls_back_to_root(mock_server):
    """현재 프레임에 없으면 최상위 문서 기준으로 찾는다(절대)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, mock_server.site_url("s05_iframe"))
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#outer"})
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
        back = await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#outer"})
        assert back.success
        assert back.data["frame_depth"] == 1
        assert back.data["resolved_from"] == "root"
        await browser.close()


@requires_chromium
async def test_frame_not_found_lists_children(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, mock_server.site_url("s05_iframe"))
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#outer"})
        r = await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#nope"})
        assert not r.success
        assert r.error_code is ErrorCode.FRAME_NOT_FOUND
        assert r.data["current_frame_url"].endswith("/s05_iframe/outer")
        assert r.data["child_frames"] == [
            {"selector_hint": "#inner", "url": mock_server.base_url + "/s05_iframe/inner"}
        ]
        assert "/s05_iframe/outer" in r.error_message
        assert "#inner" in r.error_message
        await browser.close()


@requires_chromium
async def test_screenshot_inside_frame_uses_root_page(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, mock_server.site_url("s05_iframe"))
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#outer"})
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
        r = await d.dispatch(ActionType.TAKE_SCREENSHOT, {})
        assert r.success, r.error_message
        assert r.data["bytes"] > 0
        assert r.data["captured"] == "root_page"
        assert r.data["frame_bbox"]["width"] > 0
        assert "Frame" not in (r.error_message or "")
        await browser.close()


# ------------------------------------------------------------------ 4a. press_enter 이동


SEARCH_SITE = {
    "/search": """<!doctype html><meta charset=utf-8><title>검색</title>
<form action="/results" method="get"><input id=q name=q aria-label="검색어"></form>""",
    "/results": """<!doctype html><meta charset=utf-8><title>결과</title><h1>결과</h1>""",
    "/stay": """<!doctype html><meta charset=utf-8><title>제자리</title>
<form onsubmit="return false"><input id=q name=q aria-label="검색어"></form>""",
}


@pytest.fixture
def search_site():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = SEARCH_SITE.get(self.path.split("?", 1)[0], "x").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@requires_chromium
async def test_type_press_enter_navigation_is_effect(search_site):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, search_site + "/search")
        eid = await _eid(d, "검색어")
        r = await d.dispatch(ActionType.TYPE_TEXT,
                             {"element_id": eid, "epoch": engine.epoch, "text": "노트북",
                              "press_enter": True})
        assert r.success, (r.error_code, r.error_message)
        assert r.data["nav_committed"] is True
        assert any(s.startswith("navigated") for s in r.data["signals"]), r.data
        assert r.reobserve_required is True
        assert "/results" in r.current_url
        await browser.close()


@requires_chromium
async def test_type_press_enter_without_navigation_still_checks_value(search_site):
    """이동이 없으면 기존처럼 입력값으로 판정한다 — 값이 맞으면 성공."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, search_site + "/stay")
        eid = await _eid(d, "검색어")
        r = await d.dispatch(ActionType.TYPE_TEXT,
                             {"element_id": eid, "epoch": engine.epoch, "text": "노트북",
                              "press_enter": True})
        assert r.success
        assert "value_applied: '노트북'" in r.data["signals"]
        assert "nav_committed" not in r.data
        await browser.close()


# ------------------------------------------------------------------ 4b. 프레임 안 클릭


FRAME_HTML = """<p id=top>대기</p><iframe id=f srcdoc="
<button id=a onclick=&quot;parent.document.getElementById('top').textContent='눌림'&quot;>부모 변경</button>
<button id=p onclick=&quot;window.open('about:blank')&quot;>새 창</button>
<button id=n>무반응</button>"></iframe>"""


@requires_chromium
async def test_frame_click_changing_parent_is_success():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=FRAME_HTML)
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
        r = await _click(d, "부모 변경")
        assert r.success, (r.error_code, r.error_message)
        assert await page.text_content("#top") == "눌림"
        await browser.close()


@requires_chromium
async def test_frame_click_opening_popup_is_success():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=FRAME_HTML)
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
        r = await _click(d, "새 창")
        assert r.success, (r.error_code, r.error_message)
        assert any(s.startswith("new_tab") for s in r.data["signals"]), r.data
        await browser.close()


@requires_chromium
async def test_frame_click_without_effect_still_fails():
    """음성: 프레임 안 무반응 버튼은 여전히 Silent Failure."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=FRAME_HTML)
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
        r = await _click(d, "무반응")
        assert not r.success
        assert r.error_code is ErrorCode.TIMEOUT
        await browser.close()


# ------------------------------------------------------------------ 4c. confirm 다이얼로그


@requires_chromium
async def test_confirm_click_is_effect_with_dialog_info(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, mock_server.site_url("s10_dialog"))
        await d.dispatch(ActionType.HANDLE_DIALOG, {"accept": True})
        r = await _click(d, "계정 삭제")
        assert r.success, (r.error_code, r.error_message)
        assert "dialog_opened:confirm" in r.data["signals"]
        assert r.data["dialogs"] == [
            {"type": "confirm", "message": "정말 삭제하시겠습니까?", "handled": "accepted"}
        ]
        await browser.close()


@requires_chromium
async def test_dialog_without_handler_is_reported_dismissed():
    """handle_dialog 없이 뜬 confirm 은 (Playwright 기본) 거절됐음을 알린다 — 그래도 효과."""
    from playwright.async_api import async_playwright

    html = "<button onclick=\"window.__r = confirm('진행?')\">진행</button>"
    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=html)
        r = await _click(d, "진행")
        assert r.success, (r.error_code, r.error_message)
        assert r.data["dialogs"][0]["handled"] == "dismissed"
        assert await page.evaluate("window.__r") is False
        await browser.close()


@requires_chromium
async def test_no_effect_click_still_fails_top_level():
    """음성: 최상위 문서의 무반응 버튼은 여전히 E_TIMEOUT(다이얼로그·이동·팝업 없음)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html="<button>무반응</button><p>x</p>")
        r = await _click(d, "무반응")
        assert not r.success
        assert r.error_code is ErrorCode.TIMEOUT
        assert "dialogs" not in r.data
        await browser.close()
