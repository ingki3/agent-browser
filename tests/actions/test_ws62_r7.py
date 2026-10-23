"""WS-62 R7: 기준 고정을 줄이지 않는다(합집합) + 동적 글자 신원 제외 — R6 재검증 FAIL 3건.

R6-R1 상품 카드 안 옵션 패널(안쪽 형성 쌍둥이)이 '담기' 를 먼저 차지해 바깥 상품 신원 고정을 잃고 상품 A→Z 뒤 옛 '담기' 가
상품 Z 를 눌렀다(13변형 중 기준 대비 회귀 7). R6-R2 같은 레이아웃 쌍둥이(#wrap/#footer) 안에서만 중복인 '담기' 가 고정 없음.
R6-R3 롤링 공지·다른 날 가격 갱신만으로 매번 거부(예산 소진).

원칙(사용자 결정 A, #136): 기준(v1.16.0 — tests/fixtures/ws62/repeated_v1160.js)이 고정하던 대상은 계속 고정(기준 ∪ R6, 실행
직전 전부 비교). 빼는 유일한 예외는 쌍둥이와 대상 사이에 반복 행이 없는 대상(내비게이션). 바깥 쌍둥이 지문은 안쪽 형성 쌍둥이
내용을 가린다. 신원 지문은 항목 첫 낱말을 텍스트 노드 경계로 취하고 aria-live·role=marquee/timer/status 영역과 쌍둥이 비대칭
자리(형성된 쌍둥이 전부에 같은 구조 경로로 있지 않은 글자 — 조정자 답 A) 글자를 뺀다(R6 새 지문만, 기준 고정 비교는 그대로).
모든 시험은 로컬 Chromium set_content(요청 차단, 공개 사이트 접속 없음).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r1 import _by_selector
from test_ws62_r4_hanatour import _observe, _open, page_html
from test_ws62_r6 import CENSUS_PAGES, _base_js, _census_html, _hanatour_states, _targets

# ------------------------------------------------------------------ R6-R1: 상품 카드 안 옵션 패널 13변형


def _quick() -> str:
    return "<ul class=quick>" + "".join("<li><button>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"


def _options(kind: str = "ul", twins: int = 2, same: bool = False) -> str:
    sections = []
    for i in range(twins):
        label = "공통 옵션" if same else ("기본 옵션" if i == 0 else "추가 옵션")
        if kind == "table":
            inner = ("<table><tbody><tr>" + "".join("<td><button class=target>담기</button><button>찜</button></td>"
                                                   for _ in range(3)) + "</tr></tbody></table>")
        elif kind == "calendar":
            inner = "<ul>" + "".join(f"<li><button class=target>{n}</button><button>찜</button></li>" for n in (19, 20, 21)) + "</ul>"
        elif kind == "badges":
            inner = "<div>" + "".join("<span>할인<button class=target>담기</button><button>찜</button></span>" for _ in range(3)) + "</div>"
        else:
            inner = "<ul>" + "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3)) + "</ul>"
        sections.append(f'<section aria-label="{label}"><h3>{label}</h3>{inner}</section>')
    return "<div class=options>" + "".join(sections) + "</div>"


def nested_product_html(case: str) -> str:
    """검증 증거 adversarial_matrix.py 와 같은 13변형(상품 A~D, 같은 노드 유지·상품 신원만 바꾼다)."""
    rows = []
    for k in "ABCD":
        q = _quick() if case not in ("nested_no_prefix", "same_twin_no_prefix", "single_nested") else ""
        typ = {"table_in_table": "table", "calendar_product": "calendar", "repeated_badges": "badges"}.get(case, "ul")
        opt = _options(typ, 1 if case == "single_nested" else 2, case in ("carousel_clone", "same_twin_no_prefix"))
        if case == "basic_two_buttons":
            opt = "<div><button class=target>담기</button><button>찜</button></div>"
        if case == "deep_inner":
            opt = "<div>" * 7 + opt + "</div>" * 7
        if case == "inner_three_buttons":
            opt = opt.replace("<button>찜</button>", "<button>찜</button><button>비교</button>")
        if case in ("n2_one_empty", "n2_two_empty"):
            n = 1 if case == "n2_one_empty" else 2
            opt = opt.replace("</div>", '<section aria-label="빈 옵션"><ul><li>없음</li><li>없음</li><li>없음</li></ul></section>' * n + "</div>")
        if case == "table_in_table":
            rows.append(f'<tr class=product data-sku="{k}" aria-label="상품 {k}"><td><h2 class=nm>상품 {k}</h2>{q}</td>'
                        f"<td>재고 있음</td><td>{opt}</td></tr>")
        else:
            rows.append(f'<article class=product data-sku="{k}" aria-label="상품 {k}"><h2 class=nm>상품 {k}</h2>{q}{opt}</article>')
    body = ("<table><tbody>" + "".join(rows) + "</tbody></table>") if case == "table_in_table" else "<div class=grid>" + "".join(rows) + "</div>"
    return "<!doctype html><html><body><p id=out>none</p>" + body + "</body></html>"


NESTED_CASES = ["basic_two_buttons", "single_nested", "nested_no_prefix", "nested_prefix", "repeated_badges",
                "table_in_table", "carousel_clone", "calendar_product", "n2_one_empty", "n2_two_empty",
                "same_twin_no_prefix", "deep_inner", "inner_three_buttons"]

SINK_JS = ("() => { window.clicked = []; document.querySelectorAll('button').forEach(b => b.onclick = () => {"
           " const p = b.closest('.product'); window.clicked.push({sku: p.dataset.sku, action: b.textContent});"
           " document.querySelector('#out').textContent = p.dataset.sku + '/' + b.textContent; }); }")
PRODUCT_TO_Z = ("() => { const p = document.querySelector('.product'); p.dataset.sku = 'Z';"
                " p.setAttribute('aria-label', '상품 Z'); p.querySelector('.nm').textContent = '상품 Z'; }")


async def _abort_all(page) -> None:
    await page.context.route("**/*", lambda route: route.abort())


async def _target_in(engine, page, sku: str):
    for h in engine.handles.values():
        if h.css_path and await page.evaluate(
                "([css, sku]) => { const e = document.querySelector(css); const p = e && e.closest('.product');"
                " return !!(e && p && e.matches('button.target') && p.dataset.sku === sku); }", [h.css_path, sku]):
            return h
    raise AssertionError(f"상품 {sku} 대상 없음")


#: 기준(v1.16.0)에서도 오클릭인 변형(NB1) — 합집합으로는 고쳐지지 않는다(바깥 상품 쌍둥이가 기준·R6 어디서도 형성 안 됨).
#: 조정자 결정: 범위 밖, GitHub #153 으로 따로(xfail strict — 고쳐지면 알린다). 기준 대비 회귀 0 원칙.
NB1_BASELINE_DEFECT = {"nested_no_prefix", "table_in_table"}


@requires_chromium
@pytest.mark.parametrize("case", [pytest.param(c, marks=pytest.mark.xfail(strict=True, reason="#153 기준 동일 결함(NB1)"))
                                  if c in NB1_BASELINE_DEFECT else c for c in NESTED_CASES])
async def test_nested_product_old_target_refused_after_identity_change(case):
    """RED(R6 f8e132e): 7변형에서 안쪽 옵션 패널 고정만 남아 상품 A→Z 뒤 옛 '담기' 가 상품 Z 를 눌렀다.

    기준 대비 회귀 0: 11변형 거부·바뀌지 않은 상품 B 정상 클릭. 기준에서도 오클릭인 2변형(NB1)은 #153 xfail.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, nested_product_html(case))
        try:
            await _abort_all(page)
            await page.evaluate(SINK_JS)
            await _observe(d, {"force_full_tree": True, "prune_top_n": 300})
            a, b = await _target_in(engine, page, "A"), await _target_in(engine, page, "B")
            await page.evaluate(PRODUCT_TO_Z)
            res = await d.dispatch(ActionType.CLICK, {"element_id": a.element_id})
            sink = await page.evaluate("window.clicked")
            ctrl = await d.dispatch(ActionType.CLICK, {"element_id": b.element_id})
            ctrl_sink = await page.evaluate("window.clicked")
        finally:
            await browser.close()
    assert sink == [], f"다른 상품을 눌렀다: {sink}"
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH, (res.error_code, res.error_message)
    assert ctrl.success and ctrl_sink and ctrl_sink[-1]["sku"] == "B", (ctrl.error_code, ctrl.error_message, ctrl_sink)


# ------------------------------------------------------------------ R6-R2: 같은 레이아웃 쌍둥이 안 중복


def same_twin_layout_html() -> str:
    def discover(names) -> str:
        return "<ul>" + "".join('<li><a href="#">' + x + "</a></li>" for x in names) + "</ul>"

    actions = "<div class=actions>" + "".join("<button class=buy>담기</button>" for _ in range(3)) + "</div>"
    wrap = (discover(("검색", "지도", "일정")) + '<a href="#">고객센터</a><main data-sku="A" aria-label="상품 A"><h2>상품 A</h2>'
            + "<section>" * 7 + actions + "</section>" * 7 + "</main>")
    footer = discover(("회사", "약관", "제휴")) + '<a href="#">고객센터</a>'
    return ("<!doctype html><html><body><p id=out>none</p><div id=screenArea><div id=wrap>" + wrap + "</div><div id=footer>"
            + footer + "</div></div></body></html>")


@requires_chromium
async def test_same_layout_twin_duplicate_refused():
    """RED(R6): #wrap 안에만 있는 담기×3 은 고정 없음(A') → 상품 A→Z 뒤 옛 '담기' 가 상품 Z 를 눌렀다. 기준은 #wrap 고정."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, same_twin_layout_html())
        try:
            await _abort_all(page)
            await page.evaluate("() => { window.sink = []; document.querySelectorAll('.buy').forEach(b => b.onclick = () =>"
                                " window.sink.push(b.closest('main').dataset.sku)); }")
            await _observe(d, {"force_full_tree": True, "prune_top_n": 100})
            h = next(h for h in engine.handles.values() if h.name == "담기")
            pinned = bool(h.group_path)
            await page.evaluate("() => { const p = document.querySelector('main'); p.dataset.sku = 'Z';"
                                " p.setAttribute('aria-label', '상품 Z'); p.querySelector('h2').textContent = '상품 Z'; }")
            res = await d.dispatch(ActionType.CLICK, {"element_id": h.element_id})
            sink = await page.evaluate("window.sink")
        finally:
            await browser.close()
    assert pinned
    assert sink == [] and res.error_code is ErrorCode.TOCTOU_MISMATCH, (sink, res.error_code, res.error_message)


# ------------------------------------------------------------------ R6-R3: 정상 동적 글자(롤링·다른 날 가격)


DYN_TARGET = '#cal > .inr a[data-date="2026.10.19"]'
DYN_CHANGE = {
    # 첫 달 패널 머리에만 붙은 공지(검증 증거 dynamic_budget.py 와 같은 DOM) — 쌍둥이 비대칭 자리
    "rolling": "(n) => { document.querySelector('#cal > .inr .header .dynamic').textContent = '특가 소식 ' + n; }",
    # 대상(19일)이 아닌 1일의 가격(span 과 p 사이 공백 없음 — '1138만' 결합 금지)
    "other_day_price": "(n) => { document.querySelector('#cal > .inr p.price').textContent = (120 + n) + '만'; }",
    # 롤링 + 다른 날 가격 + 같은 칸 호버 표시(세 가지 함께)
    "all": ("(n) => { document.querySelector('#cal > .inr .header .dynamic').textContent = '특가 소식 ' + n;"
            " document.querySelector('#cal > .inr p.price').textContent = (120 + n) + '만';"
            " const a = document.querySelector('#cal > .inr a[data-date=\"2026.10.19\"]'); let t = a.parentElement.querySelector('.tip');"
            " if (!t) { t = document.createElement('em'); t.className = 'tip'; a.parentElement.append(t); } t.textContent = '호버 ' + n; }"),
}
ADD_ROLLING = ("() => { const e = document.createElement('small'); e.className = 'dynamic'; e.textContent = '12000원';"
               " document.querySelector('#cal > .inr .header').append(e); }")


async def _dynamic_run(css: bool, change: str, attempts: int = 5) -> Dict[str, Any]:
    """관찰 → 동적 글자 갱신 → 옛 id 클릭을 반복(거부되면 다시 관찰). 실제 DOM 클릭 sink 로 센다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, page_html(css))
        try:
            await _abort_all(page)
            await page.evaluate(ADD_ROLLING)
            await page.evaluate("(s) => { window.targetNode = document.querySelector(s); window.sink = [];"
                                " document.addEventListener('click', e => { if (e.composedPath().includes(window.targetNode))"
                                " window.sink.push(1); }, true); }", DYN_TARGET)
            refusals = 0
            for n in range(1, attempts + 1):
                await _observe(d, {"force_full_tree": True, "prune_top_n": 300})
                eid = await _by_selector(engine, page, DYN_TARGET)
                await page.evaluate(DYN_CHANGE[change], n)
                res = await d.dispatch(ActionType.CLICK, {"element_id": eid})
                if res.error_code is ErrorCode.TOCTOU_MISMATCH:
                    refusals += 1
                if await page.evaluate("window.sink.length"):
                    break
            sink = await page.evaluate("window.sink.length")
        finally:
            await browser.close()
    return {"refusals": refusals, "sink": sink, "last": (res.error_code, res.error_message)}


@requires_chromium
@pytest.mark.parametrize("css,change", [(True, "rolling"), (False, "other_day_price"), (False, "rolling"),
                                        (True, "other_day_price"), (True, "all"), (False, "all")],
                         ids=["rolling-css", "other_day_price-nocss", "rolling-nocss", "other_day_price-css", "all-css", "all-nocss"])
async def test_dynamic_text_does_not_refuse_stable_target(css, change):
    """RED(R6): rolling/css·other_day_price/nocss 각 15연속 거부(30-step 예산 소진). 기준은 거부 0·sink 1."""
    got = await _dynamic_run(css, change)
    assert got["refusals"] == 0 and got["sink"] == 1, got


@requires_chromium
async def test_month_heading_change_still_refused_with_rolling_present():
    """대칭 자리(월 표제)는 여전히 신원 — 비대칭 롤링 공지가 붙어 있어도 표제만 바뀐 달의 옛 '19' 는 거부(조정자 조건 2)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        for css in (True, False):
            browser, page, engine, d = await _open(pw, page_html(css))
            try:
                await page.evaluate(ADD_ROLLING)
                await _observe(d, {"force_full_tree": True, "prune_top_n": 300})
                eid = await _by_selector(engine, page, DYN_TARGET)
                await page.evaluate("() => { const hs = document.querySelectorAll('#cal > .inr .header span');"
                                    " hs[0].textContent = '2027년 1월'; hs[1].textContent = '2027년 2월'; }")
                res = await d.dispatch(ActionType.CLICK, {"element_id": eid})
                dep = await page.input_value("#dep")
            finally:
                await browser.close()
            assert dep == "" and res.error_code is ErrorCode.TOCTOU_MISMATCH, (css, dep, res.error_code, res.error_message)


def live_panels_html(kind: str) -> str:
    attr = {"live": 'aria-live="polite"', "timer": 'role="timer"', "status": 'role="status"', "marquee": 'role="marquee"'}[kind]

    def panel(month: str) -> str:
        days = "<ul class=day>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in range(1, 6)) + "</ul>"
        return f"<div class=inr><div class=header><span>{month}</span><em class=ticker {attr}>소식 0</em></div>{days}</div>"

    return ("<!doctype html><html><body><p id=out>none</p><div id=cal style='display:flex'>" + panel("2026년 12월")
            + panel("2027년 1월") + "</div><script>document.querySelectorAll('#cal a').forEach(function (a) { a.onclick ="
            " function () { document.getElementById('out').textContent = this.closest('.inr').querySelector('span').textContent"
            " + '/' + this.textContent; return false; }; });</script></body></html>")


@requires_chromium
@pytest.mark.parametrize("kind", ["live", "timer", "status", "marquee"])
async def test_live_region_text_excluded_from_identity(kind):
    """양쪽 패널 모두(대칭)에 있는 aria-live·role=timer/status/marquee 글자는 구조 표지로 뺀다 — 갱신만으로 거부하지 않고,
    월 표제가 바뀌면 여전히 거부."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, live_panels_html(kind))
        try:
            await _observe(d, {"prune_top_n": 300})
            one = await _by_selector(engine, page, "#cal > .inr:nth-of-type(1) li:nth-child(3) a")
            two = await _by_selector(engine, page, "#cal > .inr:nth-of-type(1) li:nth-child(4) a")
            pinned = bool(engine.get_handle(one).group_path)
            await page.evaluate("document.querySelectorAll('.ticker').forEach((e, k) => e.textContent = '소식 ' + (k + 7))")
            ok = await d.dispatch(ActionType.CLICK, {"element_id": one})
            out = await page.text_content("#out")
            await page.evaluate("document.querySelector('#cal .inr span').textContent = '2027년 3월'")
            bad = await d.dispatch(ActionType.CLICK, {"element_id": two})
            out2 = await page.text_content("#out")
        finally:
            await browser.close()
    assert pinned
    assert ok.success and out == "2026년 12월/3", (ok.error_code, ok.error_message, out)
    assert bad.error_code is ErrorCode.TOCTOU_MISMATCH and out2 == out, (bad.error_code, out2)


@requires_chromium
async def test_identity_word_split_at_text_node_boundary():
    """항목 첫 낱말은 텍스트 노드 경계 — <a><span>1</span><p>138만</p></a> 는 'i:1'(textContent 결합 '1138만' 금지)."""
    from playwright.async_api import async_playwright

    from perception.repeated import COMMON_JS

    html = ("<!doctype html><html><body><div id=g><ul>" + "".join(
        f"<li><a href='#'><span>{n}</span><p>1{n}8만</p></a></li>" for n in (1, 2, 3)) + "</ul></div></body></html>")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            body = await page.evaluate("() => {" + COMMON_JS + " return rpIdentity(document.getElementById('g')).body; }")
        finally:
            await browser.close()
    assert "S:i:1\x01i:2\x01i:3" in body, body


@requires_chromium
async def test_common_slots_fail_closed_single_or_none():
    """대칭 자리: 쌍둥이가 하나뿐이거나 공통 자리가 없으면 null(전체 정적 글자 — fail-closed), 비대칭이 있을 때만 목록."""
    from playwright.async_api import async_playwright

    from perception.repeated import COMMON_JS

    html = ("<!doctype html><html><body>"
            "<div class=p id=a><div class=h><span>10월</span><small>공지</small></div></div>"
            "<div class=p id=b><div class=h><span>11월</span></div></div>"
            "<div class=p id=c><p>다른 구조</p></div>"
            "<div class=p id=d><div class=h><span>12월</span></div></div></body></html>")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            got = await page.evaluate("() => {" + COMMON_JS + " const q = (s) => [...s].map(x => document.getElementById(x));"
                                      " return [rpCommonSlots(q('ab')), rpCommonSlots(q('a')), rpCommonSlots(q('ac')),"
                                      " rpCommonSlots(q('bd'))]; }")
        finally:
            await browser.close()
    assert got == [["SPAN<DIV"], None, None, None], got


# ------------------------------------------------------------------ 다중 고정: 저장·전부 비교·치유 거르기·승계·승인


@requires_chromium
async def test_multi_pin_all_compared_heal_filter_and_inheritance():
    """옵션 패널 '담기' 는 안쪽 패널 고정 + 바깥 상품(기준) 고정 둘 다. 어느 하나만 바뀌어도 거부, 치유 후보에서도 빠지고,
    치유 재관찰 승계·승인(치유 끔) 경로도 다중 고정을 그대로 쓴다."""
    from playwright.async_api import async_playwright

    from actions.healing import HealingCandidate
    from perception.repeated import handle_pins

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, nested_product_html("nested_prefix"))
        try:
            await page.evaluate(SINK_JS)
            await _observe(d, {"force_full_tree": True, "prune_top_n": 300})
            a = await _target_in(engine, page, "A")
            pins = handle_pins(a)
            paths = [p.path for p in pins]
            # 치유 재관찰 승계: 같은 노드면 다중 고정 전부를 잇는다
            healed = await d._attempt_heal(a, ActionType.CLICK)
            a2 = engine.get_handle(a.element_id)
            inherited = [p.path for p in handle_pins(a2)] if a2 is not None and a2.css_path == a.css_path else None
            # 안쪽 패널 표제만 바꿔도(바깥 상품 그대로) 거부
            await page.evaluate("document.querySelector('.product section').setAttribute('aria-label', '다른 옵션')")
            inner = await d.dispatch(ActionType.CLICK, {"element_id": a.element_id})
            await page.evaluate("document.querySelector('.product section').setAttribute('aria-label', '기본 옵션')")
            # 바깥 상품만 바꿔도 거부 — 치유 후보 거르기에서도 같은 노드가 빠진다
            await page.evaluate(PRODUCT_TO_Z)
            outer = await d.dispatch(ActionType.CLICK, {"element_id": a.element_id})
            kept = await d._same_group_candidates(a, [HealingCandidate(element_id="x", role=a.role, name=a.name,
                                                                       css_path=a.css_path, testid=None, is_shadow=False)])
            with d.heal_disabled_scope():
                approved = await d.dispatch(ActionType.CLICK, {"element_id": a.element_id})
            sink = await page.evaluate("window.clicked")
        finally:
            await browser.close()
    assert len(pins) >= 2 and len(set(paths)) == len(paths), pins
    assert healed is not None and inherited is not None and set(paths) <= set(inherited), (paths, inherited)
    assert inner.error_code is ErrorCode.TOCTOU_MISMATCH, (inner.error_code, inner.error_message)
    assert outer.error_code is ErrorCode.TOCTOU_MISMATCH, (outer.error_code, outer.error_message)
    assert kept == []
    assert approved.success is False and sink == [], (approved.error_code, sink)


# ------------------------------------------------------------------ 내비게이션 예외: 사이 반복 행 없는 대상은 기준 고정도 빼기


def nav_rows_html() -> str:
    """기준(v1.16.0)이 항목 쌍둥이(div.row ×3 — 바깥 '가는 날' ↔ 팝업 입력칸 '가는 날' 충돌)로 팝업 안 이전/다음달까지 고정하는
    구조. 팝업 항목 안 기간 표시(p.range)가 달 이동마다 바뀐다 — 달 패널 밖이라 가림으로는 흡수되지 않는다."""
    def panel() -> str:
        return ("<div class=inr><div class=header><span></span></div><ul class=day>"
                + "".join("<li><a href='#'></a></li>" for _ in range(5)) + "</ul></div>")

    script = """<script>
let base = 10; window.picks = [];
function render() {
  document.querySelectorAll('#cal .inr').forEach(function (p, k) {
    const m = base + k; p.querySelector('.header span').textContent = '2026년 ' + m + '월';
    p.querySelectorAll('li a').forEach(function (a, i) { a.textContent = String(i + 1 + (m % 2)); a.dataset.m = m; });
  });
  document.querySelector('.range').textContent = '2026.' + base + ' ~ 2026.' + (base + 1);
}
document.querySelector('#cal .next').onclick = function () { base += 1; render(); return false; };
document.querySelector('#cal .prev').onclick = function () { base -= 1; render(); return false; };
document.querySelectorAll('#cal li a').forEach(function (a) { a.onclick = function () { window.picks.push(a.dataset.m + '/' + a.textContent); return false; }; });
render();
</script>"""
    return ("<!doctype html><html><body><div class=rows>"
            "<div class=row><div class=bowl><a href='#'>가는 날</a></div><div class=bowl><a href='#'>오는 날</a></div>"
            "<div class=bowl><a href='#'>성인 1명</a></div></div>"
            "<div class=row><a href='#'>항공권 검색</a></div>"
            "<div class=row id=pop><div class=ly><p class=range></p><div id=cal><a href='#' class=prev>이전달</a>"
            "<a href='#' class=next>다음달</a>" + panel() + panel() + "</div></div>"
            "<div class=opt><input aria-label='가는 날'><input aria-label='오는 날'></div></div>"
            "</div>" + script + "</body></html>")


@requires_chromium
async def test_nav_without_row_not_pinned_by_baseline_item_twin():
    """내비게이션 예외(원칙 2)는 기준 고정에도 적용: 팝업 항목과 이전/다음달 사이에 반복 행이 없어 기준 고정(항목 지문 —
    기간 표시 포함)을 빼므로 같은 id 다음달×3·이전달 거부 0. 사이 반복 행이 있는 바깥 '가는 날'(div.bowl 행)은 기준 고정 유지,
    날짜는 달 패널 고정으로 달이 바뀌면 거부."""
    from playwright.async_api import async_playwright

    from perception.repeated import handle_pins

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, nav_rows_html())
        try:
            await _observe(d, {"prune_top_n": 300})
            nxt, prv = (await _by_selector(engine, page, "#cal .next"), await _by_selector(engine, page, "#cal .prev"))
            bowl = engine.get_handle(await _by_selector(engine, page, ".bowl a"))
            day = await _by_selector(engine, page, "#cal .inr li a")
            nav_pins = [handle_pins(engine.get_handle(e)) for e in (nxt, prv)]
            moves = [await d.dispatch(ActionType.CLICK, {"element_id": nxt}) for _ in range(3)]
            back = await d.dispatch(ActionType.CLICK, {"element_id": prv})
            old_day = await d.dispatch(ActionType.CLICK, {"element_id": day})
            rng = await page.text_content(".range")
            picks = await page.evaluate("window.picks")
        finally:
            await browser.close()
    assert [(m.success, m.error_code) for m in moves + [back]] == [(True, None)] * 4, [(m.error_code, m.error_message) for m in moves + [back]]
    assert nav_pins == [[], []], nav_pins
    assert [p.path for p in handle_pins(bowl)], "사이 반복 행이 있는 대상(div.bowl)은 기준 고정 유지"
    assert rng == "2026.12 ~ 2026.13"
    assert old_day.error_code is ErrorCode.TOCTOU_MISMATCH and picks == [], (old_day.error_code, picks)


# ------------------------------------------------------------------ census: 기준 고정 ⊆ R7 다중 고정(내비게이션만 예외)


def _row_between_js() -> str:
    from perception.repeated import COMMON_JS

    return ("(a) => {" + COMMON_JS + " const g = document.querySelector(a.path), el = document.querySelector(a.css);"
            " if (!g || !el || !g.contains(el)) return null;"
            " for (let e = el; e && e !== g; e = e.parentElement) { if (rpIsItem(e)) return true; } return false; }")


def install_union_census(monkeypatch) -> List[Dict[str, Any]]:
    """관찰마다 기준(v1.16.0 원문 JS)이 고정한 (대상, 쌍둥이) 쌍 중 R7 다중 고정에 그 쌍둥이가 없는 것을 기록한다.

    허용 예외는 하나: 쌍둥이와 대상 사이에 반복 행이 없는 대상(이전/다음·표제·탭 — 구조 판정, 이름 목록 없음).
    """
    import perception.repeated as rp

    original = rp.annotate_repeated
    log: List[Dict[str, Any]] = []
    base_js, row_js = _base_js(), _row_between_js()

    async def census(page, engine, elements):
        block = await original(page, engine, elements)
        targets, handles = _targets(engine, elements)
        raw = await page.evaluate(base_js, {"targets": targets}) if targets else {"sets": []}
        need = bad = nav = 0
        missing = []
        for st in raw.get("sets") or []:
            for g in st.get("groups") or []:
                entries = [dict(m, path=g["path"]) for m in g.get("members") or []] + list(g.get("folded") or [])
                for m in entries:
                    if m.get("pin", True) is False:
                        continue
                    h = handles[int(m["i"])]
                    row = await page.evaluate(row_js, {"path": m["path"], "css": h.css_path})
                    if row is not True:
                        nav += 1
                        continue
                    need += 1
                    if m["path"] not in {p.path for p in rp.handle_pins(h)}:
                        bad += 1
                        missing.append({"name": h.name, "path": m["path"]})
        log.append({"need": need, "nav": nav, "bad": bad, "missing": missing[:8]})
        return block

    monkeypatch.setattr(rp, "annotate_repeated", census)
    return log


UNION_PAGES = CENSUS_PAGES + ["same_twin", "nav_rows", "nested_prefix", "nested_no_prefix", "table_in_table",
                              "deep_inner", "calendar_product", "n2_two_empty"]


@requires_chromium
@pytest.mark.parametrize("name", UNION_PAGES)
async def test_union_census_contains_baseline_pins(name, monkeypatch):
    """RED(R6): nested·same_twin 에서 기준 고정 쌍둥이 상실(R6 census 는 A' 예외로 bad=0). 지금은 기준 고정 ⊆ 다중 고정."""
    from playwright.async_api import async_playwright

    log = install_union_census(monkeypatch)
    if name == "same_twin":
        html = same_twin_layout_html()
    elif name == "nav_rows":
        html = nav_rows_html()
    elif name in NESTED_CASES:
        html = nested_product_html(name)
    else:
        html = _census_html(name)
    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, html)
        try:
            if name.startswith("hanatour"):
                await _hanatour_states(d, page, name.endswith("_css"))
            else:
                await _observe(d, {"prune_top_n": 300})
        finally:
            await browser.close()
    assert log and all(e["need"] > 0 for e in log), log  # 측정 조건(규칙 4): 기준이 행 안 대상을 실제로 고정했다
    assert not any(e["bad"] for e in log), json.dumps(log, ensure_ascii=False)[:1500]


# ------------------------------------------------------------------ 동적 흐름 예산: 두 날짜 선택 재관찰 ≤1


@requires_chromium
@pytest.mark.parametrize("css", [True, False], ids=["css", "nocss"])
async def test_two_dates_with_dynamic_text_reobserve_at_most_once(css):
    """롤링 공지·다른 칸 가격 갱신·호버 표시가 도는 시험 자료에서 한 관찰의 id 로 가는 날·오는 날을 고른다(재관찰 ≤1)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, page_html(css))
        try:
            await _abort_all(page)
            await page.evaluate(ADD_ROLLING)
            r = await _observe(d, {"force_full_tree": True, "prune_top_n": 300})
            reobs = 0
            picks = []
            for n, (sel, day) in enumerate(((DYN_TARGET, "19"), ('#cal > .inr a[data-date="2026.10.24"]', "24")), start=1):
                await page.evaluate(DYN_CHANGE["all"], n)
                eid = await _by_selector(engine, page, sel)
                res = await d.dispatch(ActionType.CLICK, {"element_id": eid})
                if res.error_code is ErrorCode.TOCTOU_MISMATCH:
                    reobs += 1
                    await _observe(d, {"force_full_tree": True, "prune_top_n": 300})
                    res = await d.dispatch(ActionType.CLICK, {"element_id": await _by_selector(engine, page, sel)})
                picks.append(res.success)
            dep, ret = await page.input_value("#dep"), await page.input_value("#ret")
        finally:
            await browser.close()
    assert reobs <= 1 and picks == [True, True], (reobs, picks)
    assert (dep, ret) == ("2026.10.19", "2026.10.24"), (dep, ret)
    assert r.success
