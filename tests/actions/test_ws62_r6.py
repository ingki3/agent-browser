"""WS-62 R6: R5 재검증 FAIL 2건 — 둘 다 '규칙 문구는 충족, 다른 대상 클릭'(신원 오클릭 회귀).

R1 이종 sig 혼합 깊이 — 같은 달의 1~18일은 ul/li, 19~31일은 section 7·10개 아래 table/td 처럼 행 틀이 다르면 R5 는 충돌
행 틀(국소 대상)만 고정해 깊은 이종 날짜가 고정 없이 남았고, 표제만 바뀐 달의 옛 '19' 가 다른 달에서 눌렸다.
R2 상품 카드(article>div×3)·표 행(tr>td×3) — R5 의 가장 안쪽 행(innerRow) 규칙으로 '담기' 가 상품 항목 쌍둥이 대상에서
빠져 고정이 사라졌고, 상품명·ARIA 이름만 바뀐 행의 옛 '담기' 가 다른 상품을 눌렀다.

원칙: 묶음 **형성**(어떤 쌍둥이가 생기나·충돌 판정 — 국소성·innerRow, R5 그대로)과 **고정**(누가 신원 확인을 받나)을
분리한다. 형성된 쌍둥이가 담은 대상 중 쌍둥이 안 어떤 반복 행(sig 무관) 안에 있는 대상은 전부 그 쌍둥이로 고정한다
(항목 쌍둥이는 항목 자체가 행 — 항목 안 대상 전부). 고정 없음은 쌍둥이와 대상 사이에 반복 행이 없는 대상(이전/다음·
표제·탭)뿐.

census: 시험 자료·WS-62 시험 페이지에서 v1.16.0(6e449e2) 의 REPEATED_JS 원문(tests/fixtures/ws62/repeated_v1160.js)이
고정하던 대상이 지금 고정 없음이면 실패(조정자 결정 A'). 같은 기준 세트의 다른 쌍둥이에 같은 첫 낱말이 있는 대상(쌍둥이 간
혼동)은 반드시 고정, 같은 쌍둥이 안 중복만 있는 대상은 기준 쌍둥이가 레이아웃(R4 가 없앤 흡수)일 때만 상실 허용
(classify_lost). 이름 목록(HANATOUR_NAV)은 보조.
모든 시험은 로컬 Chromium set_content(공개 사이트 접속 없음).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r4_hanatour import _n2_html, _named, _observe, _open, page_html
from test_ws62_r5 import mixed_depth_html

BASE_JS_FILE = Path(__file__).resolve().parents[1] / "fixtures" / "ws62" / "repeated_v1160.js"


# ------------------------------------------------------------------ R1: 이종 sig 혼합 깊이 18조합


def _cells(kind: str, first: int, last: int) -> str:
    if kind == "ul":
        return "<ul>" + "".join(f'<li><a href="#">{i}</a></li>' for i in range(first, last + 1)) + "</ul>"
    weeks = [range(w, min(last + 1, w + 7)) for w in range(first, last + 1, 7)]
    if kind == "table":
        return ("<table><tbody>" + "".join("<tr>" + "".join(f'<td><a href="#">{i}</a></td>' for i in w) + "</tr>"
                                           for w in weeks) + "</tbody></table>")
    assert kind == "div"
    return ("<div class=weeks>" + "".join("<div class=week>" + "".join(f'<div class=cell><a href="#">{i}</a></div>' for i in w)
                                          + "</div>" for w in weeks) + "</div>")


def mixed_sig_html(shallow: str, deep: str, depth: int) -> str:
    months = []
    for m, n in ((10, 31), (11, 30)):
        inner = "<section>" * depth + _cells(deep, 19, n) + "</section>" * depth
        months.append(f"<article class=month><h2>2026년 {m}월</h2>" + _cells(shallow, 1, 18) + inner + "</article>")
    script = ("<script>document.querySelectorAll('.month a').forEach(function (a) { a.onclick = function () {"
              " document.getElementById('out').textContent = this.closest('.month').querySelector('h2').textContent"
              " + '/' + this.textContent; return false; }; });</script>")
    return "<!doctype html><html><body><p id=out>none</p><div id=cal>" + "".join(months) + "</div>" + script + "</body></html>"


MATRIX = [(depth, a, b) for depth in (7, 10) for a in ("ul", "table", "div") for b in ("ul", "table", "div")]


@requires_chromium
@pytest.mark.parametrize("depth,shallow,deep", MATRIX, ids=[f"d{d}-{a}-{b}" for d, a, b in MATRIX])
async def test_mixed_sig_deep_dates_all_pinned_and_old_day_refused(depth, shallow, deep):
    """RED(R5 bfe3146): 이종 sig 12조합에서 깊은 '19' 고정 없음 → 표제만 바꾼 뒤 2027년 1월/19 를 눌렀다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, mixed_sig_html(shallow, deep, depth))
        try:
            await _observe(d)
            dates = [h for h in engine.handles.values() if h.name.isdigit()]
            unpinned = sorted({h.name for h in dates if not h.group_path}, key=int)
            old = next(h for h in engine.handles.values() if h.name == "19")
            await page.evaluate("document.querySelector('.month h2').textContent = '2027년 1월'")
            res = await d.dispatch(ActionType.CLICK, {"element_id": old.element_id})
            out = await page.text_content("#out")
        finally:
            await browser.close()
    assert len(dates) == 61 and not unpinned, unpinned
    assert out == "none", f"다른 달의 옛 날짜를 눌렀다: {out}"
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH, (res.error_code, res.error_message)


# ------------------------------------------------------------------ R2: 상품 카드·표 행(중간 반복 셀)


def product_html(kind: str) -> str:
    rows = []
    for k in "ABCD":
        if kind == "card":
            rows.append(f'<article class=product aria-label="상품 {k}"><div class=nm>상품 {k}</div><div>재고 있음</div>'
                        f"<div><button>담기</button></div></article>")
        else:
            rows.append(f'<tr class=product aria-label="상품 {k}"><td class=nm>상품 {k}</td><td>12000원</td>'
                        f"<td><button>담기</button></td></tr>")
    body = ("<div class=grid>" + "".join(rows) + "</div>") if kind == "card" else "<table><tbody>" + "".join(rows) + "</tbody></table>"
    script = ("<script>document.querySelectorAll('button').forEach(function (b) { b.onclick = function () {"
              " document.getElementById('out').textContent = b.closest('.product').getAttribute('aria-label') + '/' + b.textContent;"
              " }; });</script>")
    return "<!doctype html><html><body><p id=out>none</p>" + body + script + "</body></html>"


@requires_chromium
@pytest.mark.parametrize("kind", ["card", "table"])
async def test_product_cell_rows_keep_identity_pin(kind):
    """RED(R5 bfe3146): '담기' pin=None → 상품명·ARIA 이름만 Z 로 바꾼 행의 옛 '담기' 가 상품 Z 를 눌렀다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, product_html(kind))
        try:
            await _observe(d)
            buys = [h for h in engine.handles.values() if h.name == "담기"]
            pins = [bool(h.group_path) for h in buys]
            await page.evaluate("document.querySelector('.product').setAttribute('aria-label', '상품 Z');"
                                " document.querySelector('.product .nm').textContent = '상품 Z'")
            res = await d.dispatch(ActionType.CLICK, {"element_id": buys[0].element_id})
            out = await page.text_content("#out")
            other = await d.dispatch(ActionType.CLICK, {"element_id": buys[1].element_id})
            out2 = await page.text_content("#out")
        finally:
            await browser.close()
    assert len(buys) == 4 and all(pins), pins
    assert out == "none", f"다른 상품을 눌렀다: {out}"
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH, (res.error_code, res.error_message)
    # 바뀌지 않은 상품 B 는 그대로 누를 수 있다(과잉 거부 없음)
    assert other.success and out2 == "상품 B/담기", (other.error_code, other.error_message, out2)


def deep_card_html(depth: int = 6) -> str:
    """상품 카드: article > [div.nm, div 재고, div.buy > div×depth > button] — 담기는 상품에서 depth+2 단계."""
    cards = "".join(f'<article class=product aria-label="상품 {k}"><div class=nm>상품 {k}</div><div>재고 있음</div><div class=buy>'
                    + "<div>" * depth + "<button>담기</button>" + "</div>" * depth + "</div></article>" for k in "ABCD")
    script = ("<script>document.querySelectorAll('button').forEach(function (b) { b.onclick = function () {"
              " document.getElementById('out').textContent = b.closest('.product').getAttribute('aria-label') + '/' + b.textContent;"
              " }; });</script>")
    return "<!doctype html><html><body><p id=out>none</p><div class=grid>" + cards + "</div>" + script + "</body></html>"


@requires_chromium
async def test_deep_product_card_same_name_refused():
    """조정자 조건(2): 항목에서 6단계 밖(8단계)인 '담기' 도 다른 항목과 같은 이름이면 고정 없이 두지 않는다 — A→Z 거부."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, deep_card_html())
        try:
            await _observe(d)
            buys = [h for h in engine.handles.values() if h.name == "담기"]
            dist = await page.evaluate("() => { let d = 0; for (let e = document.querySelector('button'); e && !e.matches('article');"
                                       " e = e.parentElement) d++; return d; }")
            pins = [bool(h.group_path) for h in buys]
            await page.evaluate("document.querySelector('.product').setAttribute('aria-label', '상품 Z');"
                                " document.querySelector('.product .nm').textContent = '상품 Z'")
            res = await d.dispatch(ActionType.CLICK, {"element_id": buys[0].element_id})
            out = await page.text_content("#out")
        finally:
            await browser.close()
    assert dist == 8 and len(buys) == 4 and all(pins), (dist, pins)
    assert out == "none" and res.error_code is ErrorCode.TOCTOU_MISMATCH, (out, res.error_code, res.error_message)


# ------------------------------------------------------------------ census: 기준(v1.16.0) 대비 신원 회귀


def _base_js() -> str:
    text = BASE_JS_FILE.read_text(encoding="utf-8")
    return text[text.index("(args) =>"):]


def _targets(engine: Any, elements: Any) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """annotate_repeated 와 같은 대상 목록(같은 i)."""
    from perception.repeated import MAX_TARGETS, first_token

    targets, handles = [], []
    for el in list(elements)[:MAX_TARGETS]:
        h = engine.get_handle(getattr(el, "element_id", ""))
        if h is None or h.is_shadow or not h.css_path or not str(h.name or "").strip() or not first_token(h.name):
            continue
        targets.append({"i": len(handles), "css": h.css_path, "name": h.name, "role": h.role})
        handles.append(h)
    return targets, handles


def _base_pinned(raw: Any) -> Set[int]:
    out: Set[int] = set()
    for st in (raw or {}).get("sets") or []:
        for g in st.get("groups") or []:
            for m in list(g.get("members") or []) + list(g.get("folded") or []):
                if m.get("pin", True) is not False:
                    out.add(int(m["i"]))
    return out


#: 기준 쌍둥이가 바깥 레이아웃(R4 가 없앤 흡수)일 때만 '같은 쌍둥이 안 중복만 있는 대상' 의 고정 상실을 허용. 시험 자료가
#: 밝힌 레이아웃 쌍둥이 두 층: #screenArea > [#wrap, #footer], #wrap > [머리 div, #container] (무 CSS 에서 둘째 층이 나온다).
LAYOUT_JS = ("(p) => { const e = document.querySelector(p); if (!e) return null;"
             " const lay = e.id === 'wrap' || e.id === 'footer' || (e.parentElement && e.parentElement.id === 'wrap');"
             " return lay ? 'layout:' + (e.id || 'wrap>div') : (e.id || e.tagName.toLowerCase()); }")


def classify_lost(raw: Any, handles: List[Any], twin_ids: List[List[str]]) -> List[Dict[str, Any]]:
    """기준이 고정했고 지금 고정 없는 대상마다 판정(조정자 결정 A').

    * cross: 같은 기준 세트의 **다른 쌍둥이** 안에 같은 첫 낱말 대상이 있다(쌍둥이 간 혼동 = 신원 위험) → 허용 안 함.
    * 그 밖(같은 쌍둥이 안 중복만·짝 없음)은 기준 쌍둥이가 레이아웃(LAYOUT_IDS)일 때만 허용.
    twin_ids[s][g] = 기준 세트 s 의 쌍둥이 g 요소 id.
    """
    from perception.repeated import first_token

    out = []
    for si, st in enumerate((raw or {}).get("sets") or []):
        groups = st.get("groups") or []
        toks = [{first_token(handles[int(m["i"])].name) for m in list(g.get("members") or []) + list(g.get("folded") or [])}
                for g in groups]
        for gi, g in enumerate(groups):
            for m in list(g.get("members") or []) + list(g.get("folded") or []):
                h = handles[int(m["i"])]
                if m.get("pin", True) is False or h.group_path:
                    continue
                tok = first_token(h.name)
                cross = any(tok in t for j, t in enumerate(toks) if j != gi)
                layout = str(twin_ids[si][gi] or "").startswith("layout:")
                out.append({"name": h.name, "twin": twin_ids[si][gi], "cross": cross, "ok": (not cross) and layout})
    return out


def install_census(monkeypatch) -> List[Dict[str, Any]]:
    """관찰마다(annotate_repeated 호출마다) 기준 고정 대상 중 지금 고정 없는 대상을 판정해 기록한다."""
    import perception.repeated as rp

    original = rp.annotate_repeated
    log: List[Dict[str, Any]] = []
    base_js = _base_js()

    async def census(page, engine, elements):
        block = await original(page, engine, elements)
        targets, handles = _targets(engine, elements)
        raw = await page.evaluate(base_js, {"targets": targets}) if targets else {"sets": []}
        twin_ids = [[await page.evaluate(LAYOUT_JS, g["path"])
                     for g in st.get("groups") or []] for st in raw.get("sets") or []]
        lost = classify_lost(raw, handles, twin_ids)
        log.append({"base": len(_base_pinned(raw)), "now": sum(1 for h in handles if h.group_path),
                    "bad": [x for x in lost if not x["ok"]], "allowed": sorted({x["name"] for x in lost if x["ok"]})})
        return block

    monkeypatch.setattr(rp, "annotate_repeated", census)
    return log


#: 보조(이름 목록): 시험 자료에서 레이아웃 흡수 제거로 고정이 없어져야 하는(R4 과잉 거부 수정) 내비게이션 — census 가 실제로
#: 레이아웃 경우를 재고 있는지(규칙 4) 확인용. 판정은 위 일반 규칙(classify_lost)이 한다.
HANATOUR_NAV = {"이전달", "다음달", "항공권 검색", "TOP"}


async def _hanatour_states(d, page, css: bool):
    """시험 자료 6상태: 처음·오는 날 미선택·다음달·가는 날 선택·LONGNOTE·다음달 12회(6주 달)."""
    from test_ws62_r4_hanatour import _cal_groups, _day

    r = await _observe(d)
    nxt = _named(r, "다음달")
    await page.evaluate("pendingReturn()")
    r = await _observe(d)
    assert (await d.dispatch(ActionType.CLICK, {"element_id": _named(r, "다음달")})).success
    r = await _observe(d)
    if css:
        assert (await d.dispatch(ActionType.CLICK, {"element_id": _day(_cal_groups(r), "2026년 12월", "19")})).success
        await _observe(d)
    await page.evaluate("longnote()")
    r = await _observe(d)
    nxt = _named(r, "다음달")
    for _ in range(11):
        assert (await d.dispatch(ActionType.CLICK, {"element_id": nxt})).success
    await _observe(d)


CENSUS_PAGES = ["hanatour_css", "hanatour_nocss", "mixed_ul", "mixed_table", "mixed_div", "sig_d7_ul_table",
                "sig_d10_div_ul", "sig_d10_table_div", "product_card", "product_table", "n2_one", "n2_two", "legend",
                "widgets"]


def _legend_html() -> str:
    def panel(month: str, notes) -> str:
        days = "<ul class=day>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in range(1, 6)) + "</ul>"
        legend = "<div class=legend>" + "".join(f"<span><a href='#'>{n} 안내</a></span>" for n in notes) + "</div>"
        return f"<div class=inr><div class=header><span>{month}</span></div>{days}{legend}</div>"

    return ("<!doctype html><html><body><div id=cal style='display:flex'>" + panel("2026년 12월", ("휴무", "마감", "특가"))
            + panel("2027년 1월", ("운항", "좌석", "요금")) + "</div></body></html>")


def _widgets_html() -> str:
    def widget(months) -> str:
        btns = "<ul class=btns>" + "".join(f"<li><a href='#'>{b}</a></li>" for b in ("초기화", "닫기", "적용")) + "</ul>"
        cal = "".join("<div class=inr><span>" + m + "</span><ul class=day>"
                      + "".join(f"<li><a href='#'>{n}</a></li>" for n in range(1, 4)) + "</ul></div>" for m in months)
        return f"<div class=w>{btns}<div class=cal style='display:flex'>{cal}</div></div>"

    return ("<!doctype html><html><body><div style='display:flex'>" + widget(("2026년 12월", "2027년 1월"))
            + widget(("2027년 2월", "2027년 3월")) + "</div></body></html>")


def _census_html(name: str) -> str:
    if name.startswith("hanatour"):
        return page_html(name.endswith("_css"))
    if name.startswith("mixed_"):
        return mixed_depth_html(name.split("_")[1])
    if name.startswith("sig_"):
        _, d, a, b = name.split("_")
        return mixed_sig_html(a, b, int(d[1:]))
    if name == "product_deep":
        return deep_card_html()
    if name.startswith("product_"):
        return product_html(name.split("_")[1])
    if name.startswith("n2_"):
        return _n2_html(1 if name == "n2_one" else 2)
    return _legend_html() if name == "legend" else _widgets_html()


@requires_chromium
@pytest.mark.parametrize("name", CENSUS_PAGES)
async def test_identity_census_against_v1160(name, monkeypatch):
    """기준(v1.16.0) 이 고정하던 대상이 지금 고정 없음이면 실패 — 허용 목록(사이 반복 행 없는 내비게이션)만 예외."""
    from playwright.async_api import async_playwright

    log = install_census(monkeypatch)
    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, _census_html(name))
        try:
            if name.startswith("hanatour"):
                await _hanatour_states(d, page, name.endswith("_css"))
            else:
                await _observe(d, {"prune_top_n": 300})
        finally:
            await browser.close()
    assert log and all(e["base"] > 0 for e in log), log  # 측정 조건(규칙 4): 기준이 실제로 고정한 대상이 있다
    bad = [x for e in log for x in e["bad"]]
    assert not bad, json.dumps(bad, ensure_ascii=False)[:1500]
    if name.startswith("hanatour"):
        assert HANATOUR_NAV <= set(log[0]["allowed"]), log[0]["allowed"]
    else:
        assert not any(e["allowed"] for e in log), log  # 레이아웃 쌍둥이가 없는 페이지는 상실 0


def test_census_classifies_lost_pins():
    """census 판정 단위 시험: 다른 쌍둥이에 같은 첫 낱말이 있으면(cross) 레이아웃이어도 실패, 같은 쌍둥이 안 중복만이면
    레이아웃일 때만 허용, 레이아웃 아닌 쌍둥이의 상실은 실패, pin=False·지금 고정된 대상은 상실 아님."""
    from types import SimpleNamespace

    def h(name, pinned=False):
        return SimpleNamespace(name=name, group_path="#p" if pinned else None)

    handles = [h("19"), h("19"), h("이전달"), h("오는 날"), h("오는 날"), h("20", True), h("다음달")]
    raw = {"sets": [{"groups": [{"members": [{"i": 0}, {"i": 2, "pin": False}, {"i": 3}, {"i": 4}], "folded": []},
                                {"members": [{"i": 1}, {"i": 5}], "folded": [{"i": 6}]}]}]}
    assert _base_pinned(raw) == {0, 1, 3, 4, 5, 6}
    got = {(x["name"], x["twin"]): x["ok"] for x in classify_lost(raw, handles, [["layout:wrap", "layout:footer"]])}
    assert got == {("19", "layout:wrap"): False, ("19", "layout:footer"): False, ("오는 날", "layout:wrap"): True,
                   ("다음달", "layout:footer"): True}
    got2 = {x["name"]: x["ok"] for x in classify_lost(raw, handles, [["cal1", "cal2"]])}
    assert got2 == {"19": False, "오는 날": False, "다음달": False}
