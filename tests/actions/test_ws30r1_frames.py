"""WS-30 R1: 프레임 안 액션의 효과 판정 — 자발 변화 구분(BLOCKING-4), 프레임 문서 이동(BLOCKING-5).

로컬 HTTP 서버 / set_content 만 쓴다(실사이트 없음).

* BLOCKING-4  최상위 문서가 스스로 바뀌는(setInterval 시계) 페이지에서 프레임 안 무효과 클릭은
              E_TIMEOUT 이어야 한다. 같은 페이지에서 프레임 버튼이 최상위를 postMessage 로 바꾸면 성공.
* BLOCKING-5  프레임 안 type_text(press_enter) 로 **프레임 문서**가 이동하면 `navigated:` 로 성공.
              프레임 안 무효과 제출은 여전히 실패.
* V4          최상위 포커스 이동은 top 효과 신호에 싣지 않는다.
* V5          기대값(입력값)이 있는 액션은 값이 안 들어갔으면 최상위 변화가 있어도 실패.
* NB-5        download_file save_dir: 상대 경로는 거부(서버 cwd 에 조용히 쓰지 않음), 절대 경로는
              정규화해 저장.
* NB-8        popup 리스너는 붙인 곳(최상위 Page)에서 떼어 누적되지 않는다.
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict

import pytest

from contracts import ActionType, ErrorCode

requires_chromium = pytest.mark.requires_chromium

PAGES: Dict[str, str] = {}


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = PAGES.get(self.path.split("?", 1)[0], "<!doctype html><title>x</title>x")
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def mock_server():
    from harness import MockServer

    with MockServer() as srv:
        yield srv


async def _setup(pw, url=None, html=None, accept_downloads=False):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(accept_downloads=accept_downloads)
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


# ------------------------------------------------------------------ BLOCKING-4

CLOCK_TOP = """<!doctype html><meta charset=utf-8><title>top</title>
<p id=clock>0</p><p id=msg>대기</p>
<script>
let n = 0; setInterval(() => { document.getElementById('clock').textContent = String(++n); }, __P__);
addEventListener('message', e => { if (e.data === 'paid')
  document.getElementById('msg').textContent = '결제 코드 발급됨'; });
</script>
<iframe id=f srcdoc="<button id=n>무반응</button>
<button id=a onclick=&quot;window.top.postMessage('paid','*')&quot;>최상위 변경</button>"></iframe>"""


def _clock_top(period_ms: int = 200) -> str:
    return CLOCK_TOP.replace("__P__", str(period_ms))


@requires_chromium
@pytest.mark.parametrize("period_ms", [50, 200])
async def test_frame_noop_click_with_ticking_top_is_timeout(period_ms):
    """음성: 최상위가 스스로 바뀌어도(시계) 프레임 안 무효과 클릭은 성공이 아니다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=_clock_top(period_ms))
        assert (await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})).success
        results = [await _click(d, "무반응") for _ in range(3)]
        await browser.close()
    for r in results:
        assert not r.success, r.data.get("signals")
        assert r.error_code is ErrorCode.TIMEOUT


@requires_chromium
async def test_frame_noop_click_slow_clock_uses_idle_baseline():
    """음성: 사후 관찰 창(0.3초)보다 느린 시계(0.5초)도 액션 사이 대기 동안 바뀐 노드로 가린다.

    에이전트는 액션 사이에 생각한다(여기선 1초). 그 사이 바뀐 노드는 자발 변화의 기준선이다.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=_clock_top(500))
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
        results = []
        for _ in range(4):
            await page.wait_for_timeout(1000)
            results.append(await _click(d, "무반응"))
        await browser.close()
    for r in results:
        assert not r.success, (r.data.get("signals"), r.data.get("top_change_attribution"))
        assert r.error_code is ErrorCode.TIMEOUT


@requires_chromium
async def test_frame_click_changing_ticking_top_is_success():
    """양성: 같은 시계 페이지에서 프레임 버튼이 최상위를 실제로 바꾸면 성공."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=_clock_top(200))
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
        r = await _click(d, "최상위 변경")
        msg = await page.text_content("#msg")
        await browser.close()
    assert r.success, (r.error_code, r.error_message)
    assert msg == "결제 코드 발급됨"
    assert any(s.startswith("top:") for s in r.data["signals"]), r.data
    # V4: 최상위 포커스 이동(BODY -> IFRAME)은 도달 신호일 뿐 top 효과로 싣지 않는다.
    assert not any(s.startswith("top:focus_moved") for s in r.data["signals"]), r.data


@requires_chromium
async def test_frame_click_static_top_change_still_success():
    """양성: 정적 최상위를 postMessage 로 바꾸는 경우(기존 양성)도 그대로 성공."""
    from playwright.async_api import async_playwright

    html = _clock_top(200).replace("setInterval", "void")
    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=html)
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
        r = await _click(d, "최상위 변경")
        await browser.close()
    assert r.success, (r.error_code, r.error_message)
    assert not any(s.startswith("top:focus_moved") for s in r.data["signals"]), r.data


# ------------------------------------------------------------------ V5

#: 입력을 지우고(값 미적용) 최상위 문서는 **동기로** 바꾼다 — 최상위 변화가 사후 캡처 전에 확실히 일어난다.
V5_HTML = """<p id=msg>대기</p>
<iframe id=f srcdoc="<input id=q aria-label=&quot;거부 입력&quot;
 oninput=&quot;this.value=''; parent.document.getElementById('msg').textContent='입력 알림 '+Date.now()&quot;>"></iframe>"""


@requires_chromium
async def test_frame_type_value_rejected_with_top_change_is_failure():
    """입력값이 안 들어갔으면(기대값 불일치) 최상위가 바뀌어도 실패다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=V5_HTML)
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
        eid = await _eid(d, "거부 입력")
        r = await d.dispatch(ActionType.TYPE_TEXT,
                             {"element_id": eid, "epoch": engine.epoch, "text": "abc"})
        msg = await page.text_content("#msg")
        await browser.close()
    assert msg.startswith("입력 알림"), "전제: 최상위가 실제로 바뀌었다"
    assert not r.success, r.data.get("signals")
    assert r.error_code is ErrorCode.TIMEOUT


# ------------------------------------------------------------------ BLOCKING-5

def _frame_search_pages() -> None:
    PAGES["/top"] = """<!doctype html><meta charset=utf-8><title>top</title><h1>상위</h1>
<iframe id=sf src="/fsearch" width=400 height=200></iframe>"""
    PAGES["/fsearch"] = """<!doctype html><meta charset=utf-8><title>fs</title>
<form action="/fresults"><input id=q name=q aria-label="프레임 검색"></form>
<form onsubmit="return false"><input id=s name=s aria-label="제자리 검색"
 oninput="this.value=''"><button id=nb>무효과 제출</button></form>"""
    PAGES["/fresults"] = """<!doctype html><meta charset=utf-8><title>fr</title><h1>프레임 결과</h1>"""


@requires_chromium
async def test_frame_press_enter_frame_navigation_is_effect(site):
    from playwright.async_api import async_playwright

    _frame_search_pages()
    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, url=site + "/top")
        assert (await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#sf"})).success
        eid = await _eid(d, "프레임 검색")
        r = await d.dispatch(ActionType.TYPE_TEXT,
                             {"element_id": eid, "epoch": engine.epoch, "text": "노트북",
                              "press_enter": True})
        frame_url = d.ctx.page.url
        top_url = page.url
        await browser.close()
    assert r.success, (r.error_code, r.error_message)
    assert "/fresults" in frame_url
    assert top_url.endswith("/top")
    assert r.data.get("nav_committed") is True, r.data
    assert any(s.startswith("navigated") and "/fresults" in s for s in r.data["signals"]), r.data
    assert r.reobserve_required is True


@requires_chromium
async def test_frame_noop_submit_still_fails(site):
    from playwright.async_api import async_playwright

    _frame_search_pages()
    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, url=site + "/top")
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#sf"})
        eid = await _eid(d, "제자리 검색")
        typed = await d.dispatch(ActionType.TYPE_TEXT,
                                 {"element_id": eid, "epoch": engine.epoch, "text": "x",
                                  "press_enter": True})
        clicked = await _click(d, "무효과 제출")
        await browser.close()
    assert typed.error_code is ErrorCode.TIMEOUT, typed.data
    assert "nav_committed" not in typed.data
    assert clicked.error_code is ErrorCode.TIMEOUT, clicked.data


# ------------------------------------------------------------------ NB-8

FRAME_HTML = """<p id=top>대기</p><iframe id=f srcdoc="<button id=n>무반응</button>
<button id=a onclick=&quot;parent.document.getElementById('top').textContent+='!'&quot;>부모 변경</button>"></iframe>"""


def _listener_count(page, event: str) -> int:
    return len(page._impl_obj.listeners(event))  # noqa: SLF001


@requires_chromium
async def test_frame_clicks_do_not_leak_popup_listeners():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html=FRAME_HTML)
        before = _listener_count(page, "popup")
        await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
        for name in ("부모 변경", "무반응", "부모 변경"):
            await _click(d, name)
        after = _listener_count(page, "popup")
        await browser.close()
    assert after == before, f"popup 리스너 누적: {before} -> {after}"


# ------------------------------------------------------------------ NB-5

@requires_chromium
async def test_download_relative_save_dir_rejected(mock_server, tmp_path, monkeypatch):
    """상대 경로는 서버 cwd 기준이 되어 호출자가 모르는 곳에 쓴다 — 누르기 전에 거부한다."""
    from playwright.async_api import async_playwright

    monkeypatch.chdir(tmp_path)
    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(
            pw, url=mock_server.site_url("s04_download"), accept_downloads=True
        )
        eid = await _eid(d, "CSV 내려받기")
        results = [
            await d.dispatch(ActionType.DOWNLOAD_FILE, {"element_id": eid, "save_dir": sd})
            for sd in ("reldir", "sub/../../escape", "", ".")
        ]
        await browser.close()
    for r in results:
        assert not r.success
        assert r.error_code is ErrorCode.DOWNLOAD_FAILED, (r.error_code, r.error_message)
        assert "절대 경로" in (r.error_message or "")
    assert list(tmp_path.iterdir()) == [], "거부했는데 파일이 생김"


@requires_chromium
async def test_download_absolute_save_dir_normalized(mock_server, tmp_path):
    from playwright.async_api import async_playwright

    target = tmp_path / "a" / "b"
    dotted = str(tmp_path / "a" / "x" / ".." / "b")
    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(
            pw, url=mock_server.site_url("s04_download"), accept_downloads=True
        )
        eid = await _eid(d, "CSV 내려받기")
        r = await d.dispatch(ActionType.DOWNLOAD_FILE, {"element_id": eid, "save_dir": dotted})
        await browser.close()
    assert r.success, (r.error_code, r.error_message)
    saved = Path(r.downloaded_path)
    assert ".." not in saved.parts
    assert saved.parent == Path(os.path.realpath(target))
    assert saved.read_text(encoding="utf-8").startswith("id,name,amount")
