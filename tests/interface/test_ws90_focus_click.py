"""WS-90 (#245): 포커스 목적 클릭·선택된 탭 클릭의 거짓 실패.

설계 기준 .hermes/plans/ws90-92-realuse-fixes.md 'v2 확정' WS-90 절, 리뷰 .hermes/plans/ws90-evidence/review.md.
실사용(봇 2026-11-07 13:02 네이버지도 길찾기): 관찰 → click 출발지 combobox → 관찰 → click 이미 선택된 '자동차' tab 이
둘 다 E_TIMEOUT 'Silent Failure'. 실제 캡처(ws90-evidence/naver-capture): 출발지 input#input_search_r_6_[role=combobox]
가 로드 때 이미 포커스, 탭은 ul[role=tablist] > button[role=tab] aria-selected.

판정 한 점 = dispatcher `_retry_unsatisfied` 비멱등 분기 앞(CLICK 한정), 격리 world + 클릭한 노드 결속.
- focus_target: 클릭 뒤 activeElement(open shadow 따라감) === 클릭한 노드(또는 role combobox·textbox·searchbox 노드의
  자손 편집 입력)이고 편집 가능. 포커스 '변화'는 요구하지 않는다.
- target_selected: 클릭 뒤 그 노드 aria-selected="true", role tab·option 만.
대조군(무반응 버튼·선택 안 된 탭 …)은 실패 봉투가 판정 훅을 끈 실행(= 기준)과 바이트 동일(latency 제외).
제품 경로: 실 Chromium(headless) + 로컬 http(127.0.0.1), 합성 페이지만.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import pytest

from actions.dispatcher import ActionDispatcher
from interface.mcp_server import BrowserMCPServer, envelope_json

# ------------------------------------------------------------------ 실제 캡처 구조 fixture

_TAB = ('<li role="presentation"><button type="button" class="btn_search_tab" role="tab" aria-selected="{sel}" '
        'tabindex="{ti}"><span>{name}</span></button></li>')


def _tabs(selected: str = "자동차", onclick: str = "") -> str:
    out = ['<ul class="StyledSearchTabs" role="tablist" aria-label="이동수단 선택">']
    for name in ("대중교통", "자동차", "도보", "자전거"):
        on = name == selected
        out.append(_TAB.format(sel="true" if on else "false", ti="0" if on else "-1", name=name)
                   .replace("<button ", f"<button {onclick} " if onclick else "<button "))
    out.append("</ul>")
    return "".join(out)


def _field(fid: str, label: str, extra: str = "") -> str:
    return (f'<div class="input_box search_input"><label for="{fid}" class="label_search">{label}</label>'
            f'<input id="{fid}" class="input_search" autocomplete="off" maxlength="255" role="combobox" '
            f'aria-expanded="false" type="text" value="" {extra}></div>')


def _doc(body: str, script: str = "") -> str:
    return ("<!doctype html><html><head><meta charset=utf-8><title>길찾기 - fixture</title></head><body>"
            f"<button type=button>지도 홈</button>{body}<script>{script}</script></body></html>")


_NAVER_BODY = (_tabs() + _field("input_search_r_6_", "출발지 입력") + _field("input_search_r_7_", "도착지 입력")
               + '<button type="button" class="btn_direction refresh">다시입력</button>'
               '<button type="button" class="btn_direction search">길찾기</button>')
_IDLESS = ('<input class="input_search" role="combobox" aria-label="출발지 입력" aria-expanded="false">'
           '<input class="input_search" role="combobox" aria-label="도착지 입력" aria-expanded="false">')

PAGES: Dict[str, str] = {
    # 실제 캡처: 로드 때 출발지 포커스(클릭 전후 포커스 변화 없음)
    "/naver": _doc(_NAVER_BODY, "document.getElementById('input_search_r_6_').focus()"),
    "/naver_nofocus": _doc(_NAVER_BODY),
    # 리뷰 원인 (b): id 없는 두 입력 — verification 포커스 식별자 'INPUT#' 충돌(도착지 → 출발지 이동이 안 보임)
    "/collide": _doc(_IDLESS, "document.querySelectorAll('input')[1].focus()"),
    "/wrapper": _doc('<div role="combobox" aria-label="출발지" aria-expanded="false" '
                     'onclick="this.querySelector(\'input\').focus()" style="padding:8px;border:1px solid">'
                     '<input aria-autocomplete="list" aria-controls="lb"></div><ul id=lb role=listbox></ul>'),
    "/shadow": _doc('<x-f></x-f>', "customElements.define('x-f',class extends HTMLElement{constructor(){super();"
                    "this.attachShadow({mode:'open'}).innerHTML='<input aria-label=\"그림자칸\">'}})"),
    "/textarea": _doc('<label>메모<textarea></textarea></label>'),
    "/ce": _doc('<div contenteditable="true" role="textbox" aria-label="본문"></div>'),
    "/searchbox": _doc('<input type=search aria-label="검색">'),
    "/option_selected": _doc('<ul role="listbox" aria-label="정렬"><li role="option" tabindex=0 aria-selected="true">'
                             '추천순</li><li role="option" tabindex=0 aria-selected="false">거리순</li></ul>'),
    "/tab_aria_only": _doc('<div role="tablist"><button role="tab" aria-selected="true" onclick="sel(this)">가</button>'
                           '<button role="tab" aria-selected="false" onclick="sel(this)">나</button></div>',
                           "function sel(t){for(const b of document.querySelectorAll('[role=tab]'))"
                           "b.setAttribute('aria-selected',b===t?'true':'false')}"),
    # --- 대조군(실패 유지) ---
    "/noop_button": _doc('<button>아무것도 안 함</button>'),
    "/button_focuses_input": _doc('<button onclick="document.getElementById(\'q\').focus()">검색칸으로</button>'
                                  '<label>검색어<input id=q></label>'),
    "/readonly": _doc('<label>읽기전용<input readonly value="x"></label>'),
    "/aria_readonly": _doc('<input role="combobox" aria-label="읽기전용 콤보" aria-readonly="true">'),
    "/aria_disabled_parent": _doc('<div aria-disabled="true"><label>잠긴 묶음<input id=ad></label></div>'),
    "/focus_other": _doc('<label>A<input id=a></label><label>B<input id=b></label>',
                         "document.getElementById('a').addEventListener('focus',()=>document.getElementById('b').focus())"),
    "/steal": _doc('<label>훔침<input id=s></label>',
                   "document.getElementById('s').addEventListener('focus',e=>setTimeout(()=>e.target.blur(),0))"),
    "/tab_unselected_noop": _doc('<div role="tablist"><button role="tab" aria-selected="true">가</button>'
                                 '<button role="tab" aria-selected="false">나</button></div>'),
    "/current_link": _doc('<nav><a href="#" aria-current="page" onclick="return false">홈</a>'
                          '<a href="#x" onclick="return false">다른</a></nav>'),
    "/label_click": _doc('<label id=lb>라벨텍스트 <input id=li></label>'),
    "/spoof_tab": _doc('<div role="tablist"><button role="tab" aria-selected="true">가</button>'
                       '<button role="tab" aria-selected="false">나</button></div>',
                       "const g=Element.prototype.getAttribute;Element.prototype.getAttribute=function(n){"
                       "if(n==='aria-selected')return 'true';return g.call(this,n)};"),
    # main world 가 activeElement 를 그 칸이라고 속인다(실제 포커스는 곧바로 빠짐)
    "/spoof_focus": _doc('<label>속임칸<input id=sf onfocus="setTimeout(()=>this.blur(),0)"></label>',
                         "Object.defineProperty(Document.prototype,'activeElement',"
                         "{get(){return document.getElementById('sf')}});"),
    "/spoof_other": _doc('<div role="tablist"><button role="tab" aria-selected="false" id=x>가</button>'
                         '<button role="tab" aria-selected="false" '
                         'onclick="document.getElementById(\'x\').setAttribute(\'aria-selected\',\'true\')">나</button></div>'),
    "/gridcell_selected": _doc('<div role="grid"><div role="row"><div role="gridcell" tabindex=0 aria-selected="true">'
                               '선택된 칸</div></div></div>'),
}


@pytest.fixture
def site():
    state: Dict[str, Any] = {"override": {}}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            p = self.path.split("?")[0]
            html = state["override"].get(p) or PAGES.get(p, "<p>nf")
            body = (b"User-agent: *\nAllow: /\n" if p == "/robots.txt" else html.encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", state
    finally:
        srv.shutdown()
        srv.server_close()


def _find(obs: Any, role: Optional[str], name: str) -> Dict[str, Any]:
    els = obs.data["observation"]["elements"]
    hit = [e for e in els if (role is None or e.get("role") == role) and name in str(e.get("name") or "")]
    assert hit, [(e.get("role"), e.get("name")) for e in els]
    return hit[0]


async def _open(s: BrowserMCPServer, url: str):
    nav = await s.call_tool("browser_navigate", {"url": url})
    assert nav.success, nav.model_dump(mode="json")
    return await s.call_tool("browser_observe_page", {})


async def _click_by(s: BrowserMCPServer, url: str, role: Optional[str], name: str, selector: str = ""):
    obs = await _open(s, url)
    if selector:
        return await s.call_tool("browser_click", {"selector": selector})
    el = _find(obs, role, name)
    return await s.call_tool("browser_click", {"element_id": el["element_id"], "epoch": obs.snapshot_epoch})


def _wire(res: Any) -> Dict[str, Any]:
    return json.loads(envelope_json(res))


def _norm(res: Any) -> str:
    d = _wire(res)
    d.pop("latency_ms", None)
    if isinstance(d.get("data"), dict):
        d["data"].pop("latency_ms", None)
    return re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:PORT", json.dumps(d, ensure_ascii=False, sort_keys=True))


def _ok(res: Any, signal: str) -> None:
    w = _wire(res)
    assert w["success"] is True, w
    assert signal in (w.get("data") or {}).get("signals", []), w
    assert not w.get("reobserve_required"), w
    assert w.get("error_code") is None, w


# ------------------------------------------------------------------ RED → GREEN: 봇 호출 순서 그대로


@pytest.mark.asyncio
async def test_naver_bot_sequence_autofocused_combobox_then_selected_tab(site):
    base, _ = site
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        obs = await _open(s, base + "/naver")
        start = _find(obs, "combobox", "출발지")
        active = await s._page.evaluate("document.activeElement.id")  # noqa: SLF001
        assert active == "input_search_r_6_"  # 조건 성립: 실제 캡처처럼 이미 포커스
        res = await s.call_tool("browser_click", {"element_id": start["element_id"], "epoch": obs.snapshot_epoch})
        _ok(res, "focus_target")
        assert _wire(res)["data"]["element_id"] == start["element_id"]
        obs2 = await s.call_tool("browser_observe_page", {})
        car = _find(obs2, "tab", "자동차")
        res2 = await s.call_tool("browser_click", {"element_id": car["element_id"], "epoch": obs2.snapshot_epoch})
        _ok(res2, "target_selected")
        assert "focus_target" not in res2.data["signals"]


@pytest.mark.asyncio
async def test_focus_and_selected_successes_across_paths(site):
    base, _ = site
    cases = [
        ("/naver_nofocus", "combobox", "출발지", "", "focus_target"),
        ("/collide", "combobox", "출발지", "", "focus_target"),
        ("/wrapper", "combobox", "출발지", "", "focus_target"),
        ("/shadow", "textbox", "그림자", "", "focus_target"),
        ("/textarea", "textbox", "", "", "focus_target"),
        ("/ce", "textbox", "본문", "", "focus_target"),
        ("/searchbox", "searchbox", "검색", "", "focus_target"),
        ("/option_selected", "option", "추천순", "", "target_selected"),
        ("/tab_aria_only", "tab", "나", "", "target_selected"),
        ("/naver", None, "", "#input_search_r_6_", "focus_target"),  # selector 경로
        ("/naver", None, "", "[role=tab][aria-selected=true]", "target_selected"),
    ]
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        for path, role, name, selector, signal in cases:
            res = await _click_by(s, base + path, role, name, selector)
            w = _wire(res)
            assert w["success"] is True and signal in w["data"]["signals"], (path, selector, w)


@pytest.mark.asyncio
async def test_batch_click_steps_same_judgement(site):
    base, _ = site
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        await _open(s, base + "/naver")
        p = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "click", "target": {"role": "combobox", "name": "출발지 입력"}},
            {"action": "click", "target": {"role": "tab", "name": "자동차"}},
        ]})
        assert p["success"] is True and p["data"]["batch"]["ran"] == 2, json.dumps(p, ensure_ascii=False)[:1500]


# ------------------------------------------------------------------ 대조군: 실패 유지 + 기준과 바이트 동일

CONTROLS = [
    ("/noop_button", "button", "아무것도", ""),
    ("/button_focuses_input", "button", "검색칸으로", ""),
    ("/readonly", "textbox", "", ""),
    ("/aria_readonly", "combobox", "읽기전용", ""),
    ("/aria_disabled_parent", "textbox", "", ""),
    ("/focus_other", "textbox", "a", ""),
    ("/steal", "textbox", "", ""),
    ("/tab_unselected_noop", "tab", "나", ""),
    ("/option_selected", "option", "거리순", ""),
    ("/current_link", "link", "홈", ""),
    ("/label_click", None, "라벨", "#lb"),
    ("/spoof_tab", "tab", "나", ""),
    ("/spoof_focus", "textbox", "", ""),
    ("/spoof_other", "tab", "나", ""),
    ("/gridcell_selected", "gridcell", "선택된", ""),
    ("/naver", "tab", "도보", ""),
]


@pytest.mark.asyncio
async def test_controls_stay_failure_and_bytes_equal_baseline(site, monkeypatch):
    base, _ = site
    real: List[str] = []
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        for path, role, name, selector in CONTROLS:
            res = await _click_by(s, base + path, role, name, selector)
            assert res.success is False, (path, _wire(res))
            real.append(_norm(res))
    calls: List[Any] = []

    async def _baseline(self, handle):  # noqa: ANN001 — 판정 훅을 끈 실행 = 기준 경로
        calls.append(handle)
        return []

    monkeypatch.setattr(ActionDispatcher, "_click_purpose_signals", _baseline)
    baseline: List[str] = []
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        for path, role, name, selector in CONTROLS:
            baseline.append(_norm(await _click_by(s, base + path, role, name, selector)))
    assert calls  # 조건 성립: 대조군이 실제로 판정 훅에 닿는다
    for (path, *_), got, want in zip(CONTROLS, real, baseline):
        assert got == want, (path, got, want)


# ------------------------------------------------------------------ 판정 함수 단위(격리 world 와 같은 함수)


@pytest.mark.asyncio
async def test_purpose_function_edge_cases():
    from playwright.async_api import async_playwright

    from actions.dispatcher import _CLICK_PURPOSE_FN

    html = ('<div inert><input id=inert></div><input id=ro readonly><textarea id=ta></textarea>'
            '<div aria-readonly="true"><input id=ar></div><input id=cb type=checkbox>'
            '<input id=dis disabled><div role=combobox id=wrap><input id=inner></div>'
            '<div role=group id=grp><input id=ing></div><input id=ok><a id=cur aria-current=page href=#>x</a>'
            '<button role=tab id=tab aria-selected=true aria-disabled=true>t</button>'
            '<div role=treeitem id=tree aria-selected=true>tr</div>')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html)

        async def judge(target: str, focus: str = "") -> Dict[str, Any]:
            return await page.evaluate(
                "([t, f, fn]) => { const el = document.getElementById(t); if (f) document.getElementById(f).focus();"
                " return (new Function('return ' + fn))().call(el); }", [target, focus, _CLICK_PURPOSE_FN])

        try:
            assert (await judge("ok", "ok"))["focus"] is True
            assert (await judge("ta", "ta"))["focus"] is True
            assert (await judge("wrap", "inner"))["focus"] is True  # combobox 래퍼의 자손 입력
            assert (await judge("grp", "ing"))["focus"] is False  # 일반 묶음(label 류)의 자손은 아님
            assert (await judge("ok", "ta"))["focus"] is False  # 포커스가 다른 칸
            for el in ("ro", "ar", "cb", "dis", "inert"):
                assert (await judge(el, el))["focus"] is False, el
            assert (await judge("cur"))["selected"] is False  # aria-current 제외
            assert (await judge("tab"))["selected"] is False  # aria-disabled
            assert (await judge("tree"))["selected"] is False  # role tab·option 만
        finally:
            await browser.close()
