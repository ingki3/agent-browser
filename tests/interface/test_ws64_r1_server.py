"""WS-64 R1 — 검증 BLOCKING 재현(실제 Chromium·로컬 SRT 모형·공개 call_tool 경로, 관문 monkeypatch 없음).

B1 치유·대기 뒤 실제 입력 직전 재판정 / B2 실효 출처(blob·about:blank·srcdoc·data:) / B3 손상 목록 cold-start /
B5 IDN·B6 IPv4 가 Chromium URL 파서와 일치 / B7 민감 select 값 가림. 실제 SRT·외부 사이트에 접속하지 않는다.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.async_api import async_playwright

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer
from security import manual_sites as ms
from security.manual_sites import ManualSiteError, ManualSites

from test_run_cli import requires_chromium  # noqa: F401
from test_ws64_manual_server import _assert_manual, _call, _eid, _epoch, _goto_observe, _server
from ws64_pages import SrtMock

pytestmark = requires_chromium

_EXTRA = ("() => { if (document.getElementById('sel')) return; document.body.insertAdjacentHTML('beforeend',"
          " '<select id=sel aria-label=\"등급\"><option value=a>가</option><option value=b>나</option></select>"
          "<label><input type=checkbox id=chk aria-label=\"표시함\"> 표시함</label>');"
          " window.__events = 0; for (const ev of ['input','change','mouseover','click'])"
          " document.addEventListener(ev, () => window.__events++, true); }")


@pytest.fixture
def mock():
    with SrtMock() as m:
        yield m


def _manual_reason(r: Any) -> Any:
    return (r.data.get("handoff") or {}).get("reason")


# ======================================================================= B1 치유 뒤 재판정(형제 포함)


@pytest.mark.parametrize("action,name,selector,args", [
    (ActionType.CLICK, "일반 버튼", "#go", {}),
    (ActionType.TYPE_TEXT, "검색어", "#q", {"text": "SyntheticLocalValue"}),
    (ActionType.HOVER, "일반 버튼", "#go", {}),
    (ActionType.SELECT_OPTION, "등급", "#sel", {"value": "b"}),
    (ActionType.CHECK_BOX, "표시함", "#chk", {"checked": True}),
])
async def test_navigated_to_listed_during_real_healing_is_not_input(tmp_path, mock, monkeypatch, action, name,
                                                                    selector, args):
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _call(srv, ActionType.NAVIGATE, url=mock.unlisted("/plain"))
        page = srv._page
        await page.evaluate(_EXTRA)
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        eid = _eid(obs, name)
        await page.locator(selector).evaluate("(el) => el.remove()")  # 치유 사다리가 돌게(요소 떨어짐)
        disp = srv._dispatcher
        real = disp._attempt_heal
        healed = []

        async def redirect_then_heal(handle, act=None):
            # 치유 대기점에서 리디렉트가 끝난 시점을 고정한다 — 실제 치유는 그대로(관문은 바꾸지 않음).
            await page.goto(mock.listed("/plain"))
            await page.evaluate(_EXTRA)
            result = await real(handle, act)
            healed.append(result.healed)
            return result

        monkeypatch.setattr(disp, "_attempt_heal", redirect_then_heal)
        r = await _call(srv, action, element_id=eid, epoch=_epoch(obs), **args)
        state = await page.evaluate("() => ({o: document.getElementById('o').textContent, ev: window.__events,"
                                    " q: document.getElementById('q').value, sel: document.getElementById('sel').value,"
                                    " chk: document.getElementById('chk').checked})")
        assert healed == [True]  # 조건 성립: 실제 치유가 새 핸들을 채택했다
        assert page.url.startswith(mock.listed(""))
        assert r.success is False and r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED and _manual_reason(r)
        assert state == {"o": "", "ev": 0, "q": "", "sel": "a", "chk": False}
    finally:
        await srv.close()


async def test_coordinate_click_rechecked_after_wait(tmp_path, mock, monkeypatch):
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.unlisted("/plain"))
        page = srv._page
        box = await page.locator("#go").bounding_box()
        shot = await _call(srv, ActionType.OBSERVE_PAGE)
        disp = srv._dispatcher
        real = disp._capture_state
        moved = []

        async def navigate_then_capture(pg, handle=None):
            if not moved:
                moved.append(True)
                await page.goto(mock.listed("/plain"))  # 관문 뒤·누르기 전 대기점에서 목록 사이트로
            return await real(pg, handle)

        monkeypatch.setattr(disp, "_capture_state", navigate_then_capture)
        r = await _call(srv, ActionType.CLICK, x=int(box["x"] + 5), y=int(box["y"] + 5), epoch=_epoch(shot))
        assert moved and _manual_reason(r) == "manual_site" and r.success is False
        assert await page.locator("#o").text_content() == ""
    finally:
        await srv.close()


async def test_handle_dialog_reservation_not_used_on_listed_tab(tmp_path, mock):
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.unlisted("/plain"))
        assert (await _call(srv, ActionType.HANDLE_DIALOG, accept=True)).success  # 목록 밖에서 예약
        page = srv._page
        await page.goto(mock.listed("/plain"))
        await page.evaluate("() => { document.getElementById('o').textContent = confirm('x') ? '수락' : '거절'; }")
        assert await page.locator("#o").text_content() == "거절"  # 예약(수락)을 쓰지 않았다
    finally:
        await srv.close()


# ======================================================================= B2 실효 출처


async def test_blob_popup_keeps_listed_origin_after_opener_navigation(tmp_path, mock):
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.listed("/srt"))
        page = srv._page
        async with page.context.expect_page() as opened:
            await page.evaluate("html => { const b = new Blob([html], {type: 'text/html'});"
                                " window.open(URL.createObjectURL(b), 'local-blob'); }",
                                "<!doctype html><button id=go onclick=\"document.getElementById('out')"
                                ".textContent='clicked'\">Local button</button><div id=out></div>")
        popup = await opened.value
        await popup.wait_for_load_state()
        assert await popup.evaluate("location.origin") == mock.listed("")
        await page.goto(mock.unlisted("/plain"))  # opener 는 목록 밖으로
        tabs = await _call(srv, ActionType.TAB_CONTROL, command="list")
        tab = next(t for t in tabs.data["tabs"] if t["url"].startswith("blob:"))
        assert (await _call(srv, ActionType.TAB_CONTROL, command="switch", tab_id=tab["tab_id"])).success
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        assert _manual_reason(r) == "manual_site" and await popup.locator("#out").text_content() == ""
    finally:
        await srv.close()


async def test_about_blank_popup_inherits_listed_origin_after_opener_navigation(tmp_path, mock):
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.listed("/srt"))
        page = srv._page
        async with page.context.expect_page() as opened:
            await page.evaluate("html => { const w = window.open('', 'blank-pop'); w.document.write(html);"
                                " w.document.close(); }",
                                "<!doctype html><button id=go onclick=\"document.getElementById('out')"
                                ".textContent='clicked'\">Blank button</button><div id=out></div>")
        popup = await opened.value
        assert popup.url == "about:blank" and await popup.evaluate("location.origin") == mock.listed("")
        await page.goto(mock.unlisted("/plain"))
        tabs = await _call(srv, ActionType.TAB_CONTROL, command="list")
        tab = next(t for t in tabs.data["tabs"] if t["url"] == "about:blank")
        assert (await _call(srv, ActionType.TAB_CONTROL, command="switch", tab_id=tab["tab_id"])).success
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        assert _manual_reason(r) == "manual_site" and await popup.locator("#out").text_content() == ""
    finally:
        await srv.close()


@pytest.mark.parametrize("kind", ["srcdoc", "aboutblank", "data"])
async def test_nested_inherited_or_opaque_frame_in_listed_parent(tmp_path, mock, kind):
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.unlisted("/host"))
        page = srv._page
        frame = page.frame(url=mock.listed("/srt"))
        assert frame is not None
        html = ("<button id=go onclick=\"document.getElementById('out').textContent='clicked'\">Nested button"
                "</button><div id=out></div>")
        await frame.evaluate("""args => { const f = document.createElement('iframe'); f.id = 'nested';
            if (args.kind === 'srcdoc') f.srcdoc = args.html;
            if (args.kind === 'data') f.src = 'data:text/html,' + encodeURIComponent(args.html);
            document.body.appendChild(f);
            if (args.kind === 'aboutblank') { f.contentDocument.write(args.html); f.contentDocument.close(); } }""",
                             {"kind": kind, "html": html})
        assert (await _call(srv, ActionType.SWITCH_FRAME, frame_selector="#f")).success
        assert (await _call(srv, ActionType.SWITCH_FRAME, frame_selector="#nested")).success
        target = srv._dispatcher.ctx.page
        await target.wait_for_selector("#go")
        assert not page.url.startswith(mock.listed(""))  # 최상위는 목록 밖 — 부모 프레임 사슬로만 막혀야 한다
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        assert _manual_reason(r) == "manual_site" and await target.locator("#out").text_content() == ""
    finally:
        await srv.close()


async def test_srcdoc_in_unlisted_page_still_runs(tmp_path, mock):
    """과차단 대조: 목록 밖 문서의 srcdoc 은 그대로 동작한다."""
    srv = _server(tmp_path, mock)
    await srv.start()
    try:
        await _goto_observe(srv, mock.unlisted("/plain"))
        await srv._page.evaluate("() => { const f = document.createElement('iframe'); f.id = 'sd';"
                                 " f.srcdoc = '<button id=go onclick=\"this.textContent=1\">S</button>';"
                                 " document.body.appendChild(f); }")
        assert (await _call(srv, ActionType.SWITCH_FRAME, frame_selector="#sd")).success
        await srv._dispatcher.ctx.page.wait_for_selector("#go")
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))
        assert r.success, r.error_message
    finally:
        await srv.close()


# ======================================================================= B3 손상 목록 cold-start · B6 IPv4 (공개 경로)


@pytest.mark.parametrize("case", ["cold-invalid-json", "cold-loose-permissions", "ip-short", "ip-integer"])
async def test_public_gate_prevents_input(tmp_path, mock, case):
    if case.startswith("ip-"):
        host = "127.1" if case == "ip-short" else "2130706433"
        store: Any = ManualSites.of([mock.entry.replace("127.0.0.1", host)])
    else:
        path = tmp_path / "manual_sites.json"
        ms.add_site(mock.entry, path)
        if case == "cold-invalid-json":
            path.write_text("{broken")
        else:
            path.chmod(0o644)
        store = ms.ManualSiteStore(path)
    srv = BrowserMCPServer(handoff_root=tmp_path / "servers", manual_sites=store, recipes=False)
    await srv.start()
    try:
        obs = await _goto_observe(srv, mock.listed("/srt"))
        r = await _call(srv, ActionType.CLICK, element_id=_eid(obs, "조회하기"), epoch=_epoch(obs))
        assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED and _manual_reason(r) == "manual_site"
        assert await srv._page.evaluate("window.__clicks") == 0
        if case.startswith("cold"):
            assert "고쳐" in r.error_message
            # 판정 불가는 목록 밖 출처에서도 막는다(빈 목록으로 열리지 않음).
            obs2 = await _goto_observe(srv, mock.unlisted("/plain"))
            r2 = await _call(srv, ActionType.CLICK, element_id=_eid(obs2, "일반 버튼"), epoch=_epoch(obs2))
            assert _manual_reason(r2) == "manual_site" and await srv._page.locator("#o").text_content() == ""
    finally:
        await srv.close()


# ======================================================================= B5·B6 Chromium URL 파서와 같은 표현


_ACCEPTED = ["한국.kr", "Bücher.de", "ｅｘａｍｐｌｅ.com", "xn--fa-hia.de", "127.1", "2130706433", "0x7f000001",
             "0177.0.0.1", "0x7f.1", "10.0.0.1", "EXAMPLE.com", "☃.net"]
_REJECTED = ["faß.de", "straße.de", "ςa.gr", "a\u200db.com", "256.0.0.1", "1.2.3.4.5", "example.123"]


async def test_entries_match_chromium_canonical_host():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            hosts = await page.evaluate("raws => raws.map(r => { try { return new URL('https://' + r + '/').hostname;"
                                        " } catch (e) { return null; } })", _ACCEPTED + _REJECTED)
        finally:
            await browser.close()
    for raw, host in zip(_ACCEPTED, hosts):
        assert host is not None, raw
        entry = ms.parse_entry(raw)
        assert entry.text == host, (raw, entry.text, host)
        assert ManualSites([entry]).match(f"https://{host}/"), raw
    for raw in _REJECTED:
        with pytest.raises(ManualSiteError):
            ms.parse_entry(raw)


# ======================================================================= B7 민감 select 값 가림


async def _select_banner(tmp_path, mock, label: str, **args: Any) -> str:
    srv = _server(tmp_path, mock, visible=True)
    banners = []

    async def spy(text):
        banners.append(text)

    srv._show_control_banner = spy
    await srv.start()
    srv.open_handoff()
    try:
        await _goto_observe(srv, mock.listed("/srt"))
        await srv._page.evaluate("label => { const s = document.getElementById('seat'); s.setAttribute('aria-label',"
                                 " label); s.options[1].value = 'synthetic-account-value'; }", label)
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.SELECT_OPTION, element_id=_eid(obs, label), epoch=_epoch(obs), **args)
        _assert_manual(r, available=True)
        assert "synthetic-account-value" not in r.model_dump_json()
        assert await srv._page.locator("#seat").input_value() == "a"
        return banners[-1]
    finally:
        await srv.close()


@pytest.mark.parametrize("label", ["계좌", "카드 종류", "보안코드 선택"])
async def test_sensitive_select_value_masked_in_banner(tmp_path, mock, label):
    banner = await _select_banner(tmp_path, mock, label, value="synthetic-account-value")
    assert "synthetic-account-value" not in banner and "값 가림" in banner


async def test_plain_select_value_still_shown(tmp_path, mock):
    banner = await _select_banner(tmp_path, mock, "좌석 등급", value="synthetic-account-value")
    assert "synthetic-account-value" in banner  # 과잉 가림 대조
