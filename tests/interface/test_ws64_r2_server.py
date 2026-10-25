"""WS-64 R2 — R1 재검증 BLOCKING 3건 재현(실제 Chromium·로컬 SRT 모형·공개 call_tool 경로, 관문 monkeypatch 없음).

B1 clear(fill(\"\")) 등 다단계 입력의 각 단계 직전 재판정 / B2 초기 about:blank 의 실효 출처를 브라우저 값(CDP 격리 world)
으로 / B3 불투명(opaque) 문서의 생성 이력(lineage) — opener 가 떠나거나 닫혀도 목록 출처 이력이면 차단.
실제 SRT·외부 사이트에 접속하지 않는다. 부작용은 로컬 모형 서버 요청 수·DOM 값으로만 잰다.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode

from test_run_cli import requires_chromium  # noqa: F401
from test_ws64_manual_server import _call, _eid, _epoch, _goto_observe, _server
from ws64_pages import SrtMock

pytestmark = requires_chromium

_LOCAL_BUTTON = ("<button id=go onclick=\"document.getElementById('out').textContent='clicked'\">Local button</button>"
                 "<div id=out></div>")


@pytest.fixture
def mock():
    with SrtMock() as m:
        yield m


def _reason(r: Any) -> Any:
    return (r.data.get("handoff") or {}).get("reason")


async def _switch_to(srv: Any, pred: Any) -> None:
    tabs = await _call(srv, ActionType.TAB_CONTROL, command="list")
    tab = next(t for t in tabs.data["tabs"] if pred(t["url"]))
    assert (await _call(srv, ActionType.TAB_CONTROL, command="switch", tab_id=tab["tab_id"])).success


def _count_effects(mock: SrtMock) -> List[Dict[str, Any]]:
    """목록 서버의 /effect-local 요청 기록(실제 부작용 증거)."""
    effects: List[Dict[str, Any]] = []
    handler: Any = mock.httpd.RequestHandlerClass
    real_get = handler.do_GET

    def record_get(h: Any) -> None:
        if h.path.startswith("/effect-local"):
            effects.append({"path": h.path})
        return real_get(h)

    handler.do_GET = record_get
    handler.pages["/effect-local"] = "<!doctype html><title>Local counted effect</title>"
    return effects


def _opaque_popup_script(markup: str) -> str:
    return ("<script>const b=new Blob([" + json.dumps(markup) + "],{type:'text/html'});"
            "window.open(URL.createObjectURL(b),'opaque-local');</script>")


async def _open_opaque_popup(page: Any, markup: str) -> Any:
    """page 안 sandbox srcdoc(allow-scripts allow-popups)가 blob:null 팝업(불투명 출처)을 연다."""
    async with page.context.expect_page(timeout=5000) as opened:
        await page.evaluate("s => { const f = document.createElement('iframe'); f.sandbox = 'allow-scripts allow-popups';"
                            " f.srcdoc = s; document.body.appendChild(f); }", _opaque_popup_script(markup))
    popup = await opened.value
    await popup.wait_for_selector("#go")
    assert popup.url.startswith("blob:null/") and await popup.evaluate("self.origin") == "null"
    return popup


def _effect_button(url: str) -> str:
    return ("<button id=go onclick=\"const i=new Image();i.src=" + _html.escape(json.dumps(url), quote=True)
            + ";document.getElementById('out').textContent='requested'\">Local opaque action</button><div id=out></div>")


# ======================================================================= B1 clear 직전 재판정


@pytest.mark.parametrize("listed,clear_before", [(True, True), (True, False), (False, True)])
async def test_clear_rechecks_origin_after_sensitive_field_await(tmp_path, mock, monkeypatch, listed, clear_before):
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        obs = await _goto_observe(srv, mock.unlisted("/plain"))
        page = srv._page
        disp = srv._dispatcher
        real = disp._mark_sensitive_input
        calls: List[str] = []

        async def navigate_at_sensitive_await(handle: Any, params: Any, text: str) -> None:
            if not calls:  # 첫 민감 칸 판정 await(clear 전)에서 이동이 끝난 시점을 고정 — 관문은 바꾸지 않음
                calls.append("navigated")
                await page.goto(mock.listed("/plain") if listed else mock.unlisted("/plain"))
                await page.evaluate("document.getElementById('q').value='PreserveLocalValue';window.__inputEvents=0;"
                                    "document.addEventListener('input',()=>window.__inputEvents++)")
            return await real(handle, params, text)

        monkeypatch.setattr(disp, "_mark_sensitive_input", navigate_at_sensitive_await)
        r = await _call(srv, ActionType.TYPE_TEXT, element_id=_eid(obs, "검색어"), epoch=_epoch(obs),
                        text="SyntheticNewValue", clear_before=clear_before)
        state = await page.evaluate("({value:document.getElementById('q').value,events:window.__inputEvents,"
                                    "origin:location.origin})")
        assert calls == ["navigated"] and state["origin"] == (mock.listed("") if listed else mock.unlisted(""))
        if listed:
            assert _reason(r) == "manual_site"
            assert state["value"] == "PreserveLocalValue" and state["events"] == 0, state
        else:
            # R3 B1: 목록이 있으면 입력은 관문을 통과한 문서의 고정 노드로만 — 문서가 바뀌었으니(목록 밖이어도) 새 문서에는
            # 보내지 않고 '대상 바뀜'(재관찰). 목록 사이트 거부(manual_site)는 아니다(과잉 거부 대조).
            assert _reason(r) != "manual_site" and r.error_code is ErrorCode.TOCTOU_MISMATCH, r.error_message
            assert r.reobserve_required and state["value"] == "PreserveLocalValue" and state["events"] == 0, state
    finally:
        await srv.close()


async def test_navigation_mid_typing_stops_remaining_keys(tmp_path, mock, monkeypatch):
    """type_text 의 글자 단계: 세 번째 글자 뒤 목록 사이트로 이동하면 남은 글자는 새 문서로 가지 않는다."""
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        obs = await _goto_observe(srv, mock.unlisted("/plain"))
        page = srv._page
        disp = srv._dispatcher
        real = disp._manual_guard
        seen: List[int] = []

        async def guard_then_maybe_move(action: Any, params: Any) -> None:
            seen.append(1)
            if await page.evaluate("document.getElementById('q') && document.getElementById('q').value") == "abc":
                await page.goto(mock.listed("/plain"))  # 글자 사이 대기점에서 이동이 끝남(관문은 그대로 실행)
                await page.evaluate("window.__keys=0;document.addEventListener('keydown',()=>window.__keys++)")
            return await real(action, params)

        monkeypatch.setattr(disp, "_manual_guard", guard_then_maybe_move)
        r = await _call(srv, ActionType.TYPE_TEXT, element_id=_eid(obs, "검색어"), epoch=_epoch(obs), text="abcdef")
        assert page.url.startswith(mock.listed("")) and _reason(r) == "manual_site"
        assert await page.evaluate("[document.getElementById('q').value, window.__keys]") == ["", 0]
    finally:
        await srv.close()


# ======================================================================= B2 초기 about:blank 실효 출처


@pytest.mark.parametrize("shadow", [False, True])
async def test_initial_blank_dom_inserted_inherited_origin(tmp_path, mock, shadow):
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.listed("/srt"))
        page = srv._page
        async with page.context.expect_page() as opened:
            await page.evaluate("html => { const w = window.open('', 'initial-blank'); w.document.body.innerHTML = html; }",
                                _LOCAL_BUTTON)
        popup = await opened.value
        await popup.wait_for_selector("#go")
        # 조건 성립: 주소·location 으로는 출처가 없고(초기 빈 문서) 실효 출처는 목록 출처, creator 문서도 읽힌다.
        assert popup.url == "about:blank" and await popup.evaluate("location.origin") == "null"
        assert await popup.evaluate("self.origin") == mock.listed("")
        assert await popup.evaluate("opener.document.title") == "SRT 모형"
        if shadow:  # 페이지 world 가 self.origin·location 을 가려도(목록 밖 값으로) 판정은 브라우저 값으로
            await popup.evaluate("u => { Object.defineProperty(window, 'origin', {value: u}); }", mock.unlisted(""))
            assert await popup.evaluate("self.origin") == mock.unlisted("")
        await page.goto(mock.unlisted("/plain"))
        await _switch_to(srv, lambda u: u == "about:blank")
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        assert _reason(r) == "manual_site" and await popup.locator("#out").text_content() == "", r.error_message
    finally:
        await srv.close()


async def test_probe_reads_browser_origin_not_page_world(tmp_path, mock):
    """B2 조회기 자체(실제 Chromium): 초기 빈 문서·srcdoc 은 상속 출처, sandbox 는 불투명 — 페이지가 가려도 같은 값."""
    from security.manual_origin import OriginProbe

    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.listed("/srt"))
        page = srv._page
        async with page.context.expect_page() as opened:
            await page.evaluate("() => { window.open('', 'initial-blank'); }")
        popup = await opened.value
        await popup.evaluate("u => { Object.defineProperty(window, 'origin', {value: u}); }", mock.unlisted(""))
        await page.evaluate("h => { for (const [id, sb] of [['sd', null], ['sb', 'allow-scripts']]) {"
                            " const f = document.createElement('iframe'); f.id = id; if (sb) f.sandbox = sb;"
                            " f.srcdoc = h; document.body.appendChild(f); } }", _LOCAL_BUTTON)
        await page.wait_for_function("document.querySelectorAll('iframe').length === 2")
        by_id = {}
        for f in page.frames[1:]:
            await f.wait_for_selector("#go")
            by_id[await (await f.frame_element()).get_attribute("id")] = f
        srcdoc, sandbox = by_id["sd"], by_id["sb"]
        assert srcdoc.url == sandbox.url == "about:srcdoc" and await sandbox.evaluate("self.origin") == "null"
        probe = OriginProbe(1.0)
        try:
            assert await probe.origins(popup) == [mock.listed("")]
            # 같은 주소 사슬의 형제(srcdoc 둘)는 구분하지 않고 둘 다 후보(보수적 — 가장 나쁜 판정을 쓴다)
            assert sorted(await probe.origins(srcdoc) or []) == sorted([mock.listed(""), "null"])
            assert sorted(await probe.origins(sandbox) or []) == sorted([mock.listed(""), "null"])
            assert await probe.origins(page) == [mock.listed("")]
        finally:
            await probe.close()
    finally:
        await srv.close()


async def test_initial_blank_from_unlisted_still_runs(tmp_path, mock):
    """과차단 대조: 목록 밖 페이지가 연 빈 팝업은 그대로 누른다."""
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.unlisted("/plain"))
        page = srv._page
        async with page.context.expect_page() as opened:
            await page.evaluate("html => { const w = window.open('', 'initial-blank'); w.document.body.innerHTML = html; }",
                                _LOCAL_BUTTON)
        popup = await opened.value
        await popup.wait_for_selector("#go")
        await _switch_to(srv, lambda u: u == "about:blank")
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        assert r.success, r.error_message
        assert await popup.locator("#out").text_content() == "clicked"
    finally:
        await srv.close()


# ======================================================================= B3 불투명 문서 lineage


@pytest.mark.parametrize("after", ["opener-left", "opener-closed"])
async def test_opaque_blob_popup_cannot_trigger_listed_request(tmp_path, mock, after):
    effects = _count_effects(mock)
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.listed("/srt"))
        page = srv._page
        popup = await _open_opaque_popup(page, _effect_button(mock.listed("/effect-local?from=opaque-popup")))
        if after == "opener-left":
            await page.goto(mock.unlisted("/plain"))
        else:
            await _call(srv, ActionType.NAVIGATE, url=mock.unlisted("/plain"))
            await page.close()
        await _switch_to(srv, lambda u: u.startswith("blob:null/"))
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        await asyncio.sleep(0.2)
        assert _reason(r) == "manual_site", r.error_message
        assert effects == [] and await popup.locator("#out").text_content() == ""
    finally:
        await srv.close()


async def test_opaque_popup_of_opaque_popup_inherits_lineage(tmp_path, mock):
    effects = _count_effects(mock)
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.listed("/srt"))
        page = srv._page
        first = await _open_opaque_popup(page, "<button id=go>first</button>")
        await page.goto(mock.unlisted("/plain"))
        second = await _open_opaque_popup(first, _effect_button(mock.listed("/effect-local?from=grandchild")))
        await first.close()  # 중간 opener 도 닫힘
        await _switch_to(srv, lambda u: u == second.url)
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        await asyncio.sleep(0.2)
        assert _reason(r) == "manual_site" and effects == []
    finally:
        await srv.close()


async def test_sandbox_iframe_direct_click_in_listed_page_blocked(tmp_path, mock):
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.listed("/srt"))
        await srv._page.evaluate("h => { const f = document.createElement('iframe'); f.id = 'sb';"
                                 " f.sandbox = 'allow-scripts'; f.srcdoc = h; document.body.appendChild(f); }",
                                 _LOCAL_BUTTON)
        assert (await _call(srv, ActionType.SWITCH_FRAME, frame_selector="#sb")).success
        target = srv._dispatcher.ctx.page
        await target.wait_for_selector("#go")
        assert await target.evaluate("self.origin") == "null"
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        assert _reason(r) == "manual_site" and await target.locator("#out").text_content() == ""
    finally:
        await srv.close()


async def test_unlisted_sandbox_ad_iframe_click_runs(tmp_path, mock):
    """과차단 대조: 목록 밖 사이트의 sandbox 광고 iframe(불투명, lineage 에 목록 없음)은 지금처럼 누른다."""
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.unlisted("/plain"))
        await srv._page.evaluate("h => { const f = document.createElement('iframe'); f.id = 'ad';"
                                 " f.sandbox = 'allow-scripts'; f.srcdoc = h; document.body.appendChild(f); }",
                                 _LOCAL_BUTTON)
        assert (await _call(srv, ActionType.SWITCH_FRAME, frame_selector="#ad")).success
        target = srv._dispatcher.ctx.page
        await target.wait_for_selector("#go")
        assert await target.evaluate("self.origin") == "null"
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        assert r.success, r.error_message
        assert await target.locator("#out").text_content() == "clicked"
    finally:
        await srv.close()


async def test_unlisted_opaque_popup_click_runs(tmp_path, mock):
    """과차단 대조: 목록 밖 페이지가 만든 불투명 팝업은 누른다(lineage 에 목록 없음)."""
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.unlisted("/plain"))
        popup = await _open_opaque_popup(srv._page, _LOCAL_BUTTON)
        await _switch_to(srv, lambda u: u.startswith("blob:null/"))
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        assert r.success, r.error_message
        assert await popup.locator("#out").text_content() == "clicked"
    finally:
        await srv.close()
