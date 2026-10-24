"""WS-62 R9: 안전 우선(사용자 결정 A, 2026-10-23) — 바깥 가림은 '안쪽이 실제로 비교하는 글자'만.

R8 재검증 BLOCKING(R1): 상품 article×4 안 옵션 section×2(안쪽 형성 쌍둥이)에서 상품명이 첫 section 에만 strong·span·dt/dd·
안쪽 루트 aria-labelledby(→ span) 로 있으면, 안쪽 고정은 대칭 자리 규칙으로 빼고 바깥(기준) 고정은 R8 의 '신원 성격' 태그 목록
밖이라 가려서 아무도 비교하지 않았다 — 상품 A→Z 뒤 옛 '담기' 가 Z 를 눌렀다(기준은 전부 거부, 4변형·21대상).

결정: 구조만으로는 롤링 공지와 비제목 상품명을 구별할 수 없다 → 바깥 가림은 안쪽 고정이 대칭 자리로 남긴 글자만 가리고,
나머지는 태그·역할과 무관하게 바깥 지문에 남긴다(R8 태그 목록 예외는 이 규칙에 포함). 대가: 안쪽 쌍둥이 한쪽에만 있는 표지
없는 롤링 글자는 바깥 고정 때문에 재관찰을 요구한다 — 기준(v1.16.0) 수준까지 허용(#154).

기준 비교는 기준 런타임(`baseline_runtime` — tests/fixtures/ws62/repeated_v1160.js 의 고정 계산·지문 함수로 핸들 고정과 실행 직전
확인·치유 거르기를 바꿔 끼운다)으로 같은 흐름을 다시 돌려 잰다. 모든 시험은 로컬 Chromium set_content(요청 차단).
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r4_hanatour import _observe, _open
from test_ws62_r6 import BASE_JS_FILE, _base_js, _hanatour_states, _targets
from test_ws62_r7 import DYN_CHANGE, _dynamic_run
from test_ws62_r8 import IDENTITY_PAGES, _page_for

# ------------------------------------------------------------------ 기준(v1.16.0) 런타임


#: 기준 실행 직전 확인(v1.16.0 groupCheck — 지문 하나, 가림·대칭 자리 없음). groupCheckAll 은 다중 고정 인자 모양 맞춤용.
_BASE_CHECK_TAIL = r"""
  function groupCheck(el, groupPath, groupPin) {
    let g = null;
    try { g = document.querySelector(groupPath); } catch (x) { g = null; }
    if (!g) return {ok: false, now: null, why: 'group_gone'};
    if (!el || !g.contains(el)) return {ok: false, now: null, why: 'outside_group'};
    if (groupPin === null || groupPin === undefined) return {ok: true, now: null, why: ''};
    const now = rpFingerprint(g, el);
    return {ok: now === groupPin, now: now, why: now === groupPin ? '' : 'heading_changed'};
  }
  function groupCheckAll(el, pins) {
    for (let k = 0; k < pins.length; k++) {
      const p = pins[k] || [];
      const r = groupCheck(el, p[0], p[1]);
      if (!r.ok) { r.k = k; return r; }
    }
    return {ok: true, now: null, why: ''};
  }
"""


def _base_check_js() -> str:
    """기준 원문의 공통 함수(rpFingerprint 까지) + 기준 groupCheck."""
    text = BASE_JS_FILE.read_text(encoding="utf-8")
    start = text.index("(args) => {") + len("(args) => {")
    common = text[start:text.index("  function tok(name)")]
    assert "function rpFingerprint(g, el)" in common
    return common + _BASE_CHECK_TAIL


@contextlib.contextmanager
def baseline_runtime():
    """v1.16.0 의 고정·확인으로 바꿔 끼운다(기준 거부 수를 같은 흐름으로 재기 위한 시험 도구 — 제품 코드가 아니다).

    관찰: 표시 블록은 지금 코드 그대로(대상 고르기용), 핸들 고정은 전부 지우고 기준 sets 의 members·folded 를 지문 하나로.
    실행 직전 확인·치유 거르기: 기준 groupCheck(지금 지문 함수가 아니라 기준 원문 rpFingerprint).
    """
    import actions.verification as ver
    import perception.repeated as rp

    original = rp.annotate_repeated
    base_js, check = _base_js(), _base_check_js()

    async def base_annotate(page, engine, elements):
        block = await original(page, engine, elements)
        for h in list(engine.handles.values()):
            rp.set_pins(h, [])
        targets, handles = _targets(engine, elements)
        raw = await page.evaluate(base_js, {"targets": targets}) if targets else {}
        for st in (raw or {}).get("sets") or []:
            for g in st.get("groups") or []:
                for m in [dict(m, path=g["path"]) for m in g.get("members") or []] + list(g.get("folded") or []):
                    if m.get("pin", True) is not False and isinstance(m.get("path"), str):
                        rp.set_pins(handles[int(m["i"])], [rp.GroupPin(m["path"], rp._pin_text(m.get("fp")))])
        return block

    assert rp.GROUP_CHECK_JS in ver.STALENESS_CHECK_SCRIPT and rp.GROUP_CHECK_JS in rp.GROUP_FILTER_JS
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(rp, "annotate_repeated", base_annotate)
        mp.setattr(ver, "STALENESS_CHECK_SCRIPT", ver.STALENESS_CHECK_SCRIPT.replace(rp.GROUP_CHECK_JS, check))
        mp.setattr(rp, "GROUP_FILTER_JS", rp.GROUP_FILTER_JS.replace(rp.GROUP_CHECK_JS, check))
        yield
    finally:
        mp.undo()


#: R8 가림 규칙(제목 성격 태그만 예외) — 주입(사보타주)용. 가림 판정 한 줄(R11 rpMaskedText — 안쪽이 실제 비교하는 글자만)을
#: R8 방식(대칭 자리 null 이면 통째로, 아니면 제목 성격 태그 밖 글자는 전부 가림)으로 되돌린다.
R9_RULE = "      if (c && c.text.has(t)) return true;"
R8_RULE = """      let head = false;
      for (let e = t.parentElement; e && e !== m; e = e.parentElement) {
        if (/^(H[1-6]|CAPTION|LEGEND|TH)$/.test(e.tagName) || (e.getAttribute('role') || '').toLowerCase().trim() === 'heading') { head = true; break; }
      }
      if (c && (c.slots === null || !head || c.text.has(t))) return true;"""


@contextlib.contextmanager
def r8_mask_rule():
    """제품 JS(관찰·실행 직전 확인·치유 거르기) 안 가림 규칙을 R8 방식(제목 성격 태그만 남김)으로 되돌린다."""
    import actions.verification as ver
    import perception.repeated as rp

    mp = pytest.MonkeyPatch()
    try:
        for mod, name in ((rp, "REPEATED_JS"), (rp, "GROUP_CHECK_JS"), (rp, "GROUP_FILTER_JS"),
                          (ver, "STALENESS_CHECK_SCRIPT")):
            src = getattr(mod, name)
            assert src.count(R9_RULE) == 1, name
            mp.setattr(mod, name, src.replace(R9_RULE, R8_RULE))
        yield
    finally:
        mp.undo()


# ------------------------------------------------------------------ 검증 증거 sibling_probe.py 32변형(그대로 편입)


SHAPES = {
    "h1": "<h1 class=nm>상품 {k}</h1>",
    "h2": "<h2 class=nm>상품 {k}</h2>",
    "h6": "<h6 class=nm>상품 {k}</h6>",
    "heading": "<div role=heading class=nm>상품 {k}</div>",
    "caption": "<table><caption class=nm>상품 {k}</caption></table>",
    "legend": "<fieldset><legend class=nm>상품 {k}</legend></fieldset>",
    "rowhead": "<table><tbody><tr><th class=nm>상품 {k}</th></tr></tbody></table>",
    "strong": "<strong class=nm>상품 {k}</strong>",
    "span": "<span class=nm>상품 {k}</span>",
    "aria_label": '<div class=nm aria-label="상품 {k}"></div>',
    "image_alt": '<img class=nm alt="상품 {k}">',
    "dt_dd": "<dl><dt>상품명</dt><dd class=nm>상품 {k}</dd></dl>",
    "link": '<a class=nm href="#detail">상품 {k}</a>',
    "root_labelled": '<span class=nm id="label{k}">상품 {k}</span>',
    "root_aria": "",
    "inner_aria": "",
    "inner_labelled": '<span class=nm id="label{k}">상품 {k}</span>',
}
SIBLING_CASES = [(s, p) for p in ("buried", "outside") for s in SHAPES
                 if not (p == "outside" and s in ("inner_aria", "inner_labelled"))]
#: R8 재검증 BLOCKING 4변형(기준은 9대상 전부 거부, R8 은 Z 클릭).
R8_REGRESSIONS = [("strong", "buried"), ("span", "buried"), ("dt_dd", "buried"), ("inner_labelled", "buried")]


def sibling_html(shape: str, pos: str) -> str:
    products = []
    for k in "ABCD":
        nm = SHAPES[shape].format(k=k)
        attrs = (f' aria-labelledby="label{k}"' if shape == "root_labelled" else
                 f' aria-label="상품 {k}"' if shape == "root_aria" else "")
        quick = "<ul>" + "".join("<li><button class=quick>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"
        inner = []
        for i in range(2):
            at = (f' aria-label="상품 {k}"' if shape == "inner_aria" and i == 0 else
                  f' aria-labelledby="label{k}"' if shape == "inner_labelled" and i == 0 else "")
            rows = "<ul>" + "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3)) + "</ul>"
            inner.append(f"<section{at}><h3>옵션 {i}</h3>{nm if i == 0 and pos == 'buried' else ''}{rows}</section>")
        products.append(f"<article class=product data-sku={k}{attrs}>{nm if pos == 'outside' else ''}{quick}"
                        "<div class=options>" + "".join(inner) + "</div></article>")
    return "<!doctype html><html><body><p id=out>none</p><div class=grid>" + "".join(products) + "</div></body></html>"


SIB_SINK = ("() => { window.sink = []; document.querySelectorAll('button').forEach(b => b.onclick = () => {"
            " const a = b.closest('.product'); window.sink.push(a.dataset.sku);"
            " document.querySelector('#out').textContent = a.dataset.sku + '/' + window.sink.length; }); }")
SIB_TO_Z = ("(shape) => { const a = document.querySelector('.product'); a.dataset.sku = 'Z'; const n = a.querySelector('.nm');"
            " if (shape === 'root_aria') a.setAttribute('aria-label', '상품 Z');"
            " else if (shape === 'inner_aria') a.querySelector('section').setAttribute('aria-label', '상품 Z');"
            " else if (shape === 'aria_label') n.setAttribute('aria-label', '상품 Z');"
            " else if (shape === 'image_alt') n.setAttribute('alt', '상품 Z'); else n.textContent = '상품 Z'; }")


async def _sibling_run(pw, shape: str, pos: str) -> Dict[str, Any]:
    """상품 A 의 '.target'·'.quick' 9대상을 관찰 → A→Z → 옛 id 로 전부 누르고, 바뀌지 않은 B 를 누른다."""
    browser, page, engine, d = await _open(pw, sibling_html(shape, pos))
    try:
        await page.context.route("**/*", lambda route: route.abort())
        await page.evaluate(SIB_SINK)
        await _observe(d, {"force_full_tree": True, "prune_top_n": 1000})
        targets, b = [], None
        for h in engine.handles.values():
            if not h.css_path or h.is_shadow:
                continue
            sku = await page.evaluate("(css) => { const e = document.querySelector(css), a = e && e.closest('.product');"
                                      " return a && e.matches('.target,.quick') ? a.dataset.sku : null; }", h.css_path)
            if sku == "A":
                targets.append(h)
            if sku == "B" and b is None:
                b = h
        assert len(targets) == 9 and b is not None, len(targets)
        await page.evaluate(SIB_TO_Z, shape)
        res = [await d.dispatch(ActionType.CLICK, {"element_id": h.element_id}) for h in targets]
        sink = await page.evaluate("window.sink")
        good = await d.dispatch(ActionType.CLICK, {"element_id": b.element_id})
        ctrl = await page.evaluate("window.sink")
    finally:
        await browser.close()
    return {"css": [h.css_path for h in targets], "refused": [r.error_code is ErrorCode.TOCTOU_MISMATCH for r in res],
            "clicked": [bool(r.success) for r in res], "sink": sink, "b_ok": bool(good.success) and ctrl[-1:] == ["B"]}


def _assert_not_worse(now: Dict[str, Any], base: Dict[str, Any]) -> None:
    """기준 대비 오클릭 0: 기준이 거부한 대상은 지금도 거부, 다른 상품 클릭 수는 기준 이하, B 는 둘 다 정상."""
    assert now["css"] == base["css"]
    lost = [c for c, rb, rn in zip(now["css"], base["refused"], now["refused"]) if rb and not rn]
    assert not lost, f"기준이 거부한 옛 대상을 눌렀다: {lost} sink={now['sink']}"
    assert now["sink"].count("Z") <= base["sink"].count("Z"), (now["sink"], base["sink"])
    assert now["b_ok"] and base["b_ok"], (now, base)


@requires_chromium
@pytest.mark.parametrize("shape,pos", SIBLING_CASES, ids=[f"{s}/{p}" for s, p in SIBLING_CASES])
async def test_sibling_identity_not_worse_than_baseline(shape, pos):
    """RED(R8 33435b2): strong·span·dt_dd·inner_labelled /buried 에서 기준은 9대상 전부 거부, R8 은 Z 6·6·6·3회 클릭.

    32변형 모두 기준 런타임과 같은 흐름으로 비교 — 기준 대비 오클릭 0, 바뀌지 않은 B 정상(32/32).
    (aria_label·image_alt·link·inner_aria 는 기준에서도 오클릭 — 기준 동일 한계, 더 나빠지지만 않으면 된다.)
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        now = await _sibling_run(pw, shape, pos)
        with baseline_runtime():
            base = await _sibling_run(pw, shape, pos)
    _assert_not_worse(now, base)
    if (shape, pos) in R8_REGRESSIONS:
        assert all(base["refused"]) and all(now["refused"]) and now["sink"] == [], (now, base)


@requires_chromium
async def test_injected_r8_rule_misclicks_four_siblings():
    """주입: 가림 규칙을 R8 방식(제목 성격 태그만 남김)으로 되돌리면 4변형 모두 기준 대비 오클릭(시험이 결함을 잡는다)."""
    from playwright.async_api import async_playwright

    got = {}
    async with async_playwright() as pw:
        with r8_mask_rule():
            for shape, pos in R8_REGRESSIONS:
                got[shape] = await _sibling_run(pw, shape, pos)
    assert all(r["sink"].count("Z") >= 3 and r["b_ok"] for r in got.values()), {k: v["sink"] for k, v in got.items()}


# ------------------------------------------------------------------ 동적 글자: 거부 수 ≤ 기준(#154 — 비대칭 롤링은 재관찰)


#: role=timer 롤링(구조 표지 — 신원 제외) + 다른 날 가격 + 같은 칸 호버(세 가지 함께).
TIMER_ALL = ("(n) => { let t = document.querySelector('#cal > .inr .header .ticker');"
             " if (!t) { t = document.createElement('em'); t.className = 'ticker'; t.setAttribute('role', 'timer');"
             " document.querySelector('#cal > .inr .header').append(t); } t.textContent = '마감 ' + n + '분 전'; ("
             + DYN_CHANGE["all"] + ")(n); }")
DYN_CONDITIONS = [(True, "rolling"), (False, "other_day_price"), (False, "rolling"), (True, "other_day_price"),
                  (True, "all"), (False, "all"), (True, "timer_all"), (False, "timer_all")]


@requires_chromium
@pytest.mark.parametrize("css,change", DYN_CONDITIONS,
                         ids=[f"{c}-{'css' if s else 'nocss'}" for s, c in DYN_CONDITIONS])
async def test_dynamic_refusals_at_most_baseline(css, change, monkeypatch):
    """R6-R3 6조건 + timer 롤링·가격·호버: 같은 흐름의 거부 수가 기준 런타임 이하, 대상은 결국 한 번 눌린다.

    안전 우선(#154): 안쪽 쌍둥이 한쪽에만 있는 표지 없는 롤링 글자는 바깥 고정 때문에 재관찰을 요구할 수 있다 — 기준 수준까지
    허용(기준도 바깥 지문 전체를 비교한다). 지금 시험 자료에서는 둘 다 0 이다(R7 test_dynamic_text_does_not_refuse_stable_target).
    """
    import test_ws62_r7 as r7

    monkeypatch.setitem(r7.DYN_CHANGE, "timer_all", TIMER_ALL)
    now = await _dynamic_run(css, change)
    with baseline_runtime():
        base = await _dynamic_run(css, change)
    assert now["refusals"] <= base["refusals"] and now["sink"] == 1, (now, base)
    print(f"\nWS62R9-DYN {change}-{'css' if css else 'nocss'} base={base['refusals']} r9={now['refusals']}")


@requires_chromium
async def test_baseline_runtime_refuses_changed_month():
    """시험 도구 자체 확인(규칙 5): 기준 런타임도 표제만 바뀐 달의 옛 날짜를 거부하고(고정이 실제로 걸린다), 바뀌지 않은 달은
    누른다. (하나투어 CSS 시험 자료는 기준에서 레이아웃 지문 상한 때문에 월 표제가 지문 밖이라 쓰지 않는다 — R4 기록.)"""
    from playwright.async_api import async_playwright

    from test_ws62_r1 import _by_selector
    from test_ws62_r7 import live_panels_html

    async with async_playwright() as pw:
        with baseline_runtime():
            browser, page, engine, d = await _open(pw, live_panels_html("live"))
            try:
                await _observe(d, {"prune_top_n": 300})
                one = await _by_selector(engine, page, "#cal > .inr:nth-of-type(1) li:nth-child(3) a")
                two = await _by_selector(engine, page, "#cal > .inr:nth-of-type(2) li:nth-child(3) a")
                pinned = bool(engine.get_handle(one).group_path)  # type: ignore[union-attr]
                await page.evaluate("document.querySelector('#cal .inr span').textContent = '2027년 3월'")
                res = await d.dispatch(ActionType.CLICK, {"element_id": one})
                ok = await d.dispatch(ActionType.CLICK, {"element_id": two})
                out = await page.text_content("#out")
            finally:
                await browser.close()
    assert pinned and res.error_code is ErrorCode.TOCTOU_MISMATCH, (pinned, res.error_code, res.error_message)
    assert ok.success and out == "2027년 1월/3", (ok.error_code, out)


@requires_chromium
@pytest.mark.parametrize("case", ["buried_name", "buried_name_table", "buried_name_deep"])
async def test_buried_rolling_small_refusals_at_most_baseline(case):
    """결합(R8 의 '롤링만 바뀌면 거부 0' 을 바꿈 — #154): 첫 옵션 section 에만 있는 표지 없는 <small> 롤링은 구조로 상품명과
    구별되지 않으므로 바깥 고정이 비교한다 → 재관찰 요구. 거부 수 ≤ 기준, 다른 상품 클릭 0, 상품 A→Z 뒤 옛 id 전부 거부."""
    from playwright.async_api import async_playwright

    from test_ws62_r8 import ROLL, SINK, TO_Z, _abort_all, _targets_by_sku, adversarial_html

    async def run(pw) -> Dict[str, Any]:
        browser, page, engine, d = await _open(pw, adversarial_html(case, rolling=True))
        try:
            await _abort_all(page)
            await page.evaluate(SINK)
            await _observe(d, {"force_full_tree": True, "prune_top_n": 1000})
            a_all = await _targets_by_sku(engine, page, "A")
            refusals = 0
            for n in range(1, 6):
                await page.evaluate(ROLL, n)
                r = await d.dispatch(ActionType.CLICK, {"element_id": a_all[n % len(a_all)].element_id})
                refusals += r.error_code is ErrorCode.TOCTOU_MISMATCH
            ok_sink = await page.evaluate("window.sink")
            await page.evaluate(TO_Z)
            await page.evaluate(ROLL, 9)
            bad = [await d.dispatch(ActionType.CLICK, {"element_id": h.element_id}) for h in a_all]
            sink = await page.evaluate("window.sink")
        finally:
            await browser.close()
        return {"refusals": refusals, "ok_sink": ok_sink, "sink": sink,
                "bad": [r.error_code is ErrorCode.TOCTOU_MISMATCH for r in bad]}

    async with async_playwright() as pw:
        now = await run(pw)
        with baseline_runtime():
            base = await run(pw)
    assert now["refusals"] <= base["refusals"], (now, base)
    print(f"\nWS62R9-ROLL {case} base={base['refusals']} r9={now['refusals']}")
    assert set(now["ok_sink"]) <= {"A"} and now["sink"] == now["ok_sink"], now
    assert all(now["bad"]), now


# ------------------------------------------------------------------ census: 비제목 글자까지 신원 보존


#: 기준 고정 (대상, 쌍둥이)마다 기준 지문이 비교하던 **모든** 정적 글자(쌍둥이 g 안 막히지 않은 텍스트 노드 + g 의
#: aria-labelledby 대상 — 태그 무관)를 하나씩 바꿔 그 대상의 다중 고정(groupCheckAll)이 다르다고 하는지 센다.
#: 허용 하나: 대상을 담지 않은 형제 안쪽 쌍둥이(가림) 안의 글자가 그 안쪽 고정의 **대칭 자리**(가림과 함께 저장한 mask_slots,
#: null 이면 전체)이면 그 안쪽 쌍둥이 대상들의 고정이 비교하면 된다(다음달 등 안쪽 내용 변화로 바깥 대상을 거부하지 않는 가림의
#: 목적). 비대칭 자리 글자는 그 대상의 고정이 직접 비교해야 한다(R8: 비제목 상품명을 형제 section 대상 쪽에서 아무도 비교 안 함).
def _identity_census_all_js() -> str:
    from perception.repeated import GROUP_CHECK_JS

    return ("(args) => {" + GROUP_CHECK_JS + r"""
  const out = [];
  for (const q of args.pairs) {
    const g = document.querySelector(q.path), el = document.querySelector(q.css);
    if (!g || !el || !g.contains(el)) { out.push({k: q.k, need: 0, miss: [], bad: 'gone'}); continue; }
    const nodes = [];
    const roots = [g];
    for (const id of (g.getAttribute('aria-labelledby') || '').split(/\s+/)) { const r = id && document.getElementById(id); if (r && !g.contains(r)) roots.push(r); }
    for (const h of roots) {
      const w = document.createTreeWalker(h, NodeFilter.SHOW_TEXT);
      let t;
      while ((t = w.nextNode())) {
        if (!t.nodeValue || !t.nodeValue.trim() || nodes.indexOf(t) >= 0) continue;
        if (g.contains(t) && rpBlocked(t.parentElement, g)) continue;   // 기준도 비교하지 않던 글자(대화형·반복 항목·숨김·live)
        nodes.push(t);
      }
    }
    const base = rpIdentity(g).body.slice(0, RP_FP_LIMIT - 8);
    const miss = [];
    let need = 0, sib = 0;
    for (const t of nodes) {
      const v = t.nodeValue;
      if (base.indexOf(rpNorm(v)) < 0) continue;
      need++;
      if (!groupCheckAll(el, q.pins).ok) { miss.push('pre:' + rpClean(v)); continue; }
      let judges = [[el, q.pins]];
      for (const p of q.pins) {
        if (p[0] !== q.path) continue;
        (p[2] || []).forEach(function (mp, k) {
          const m = document.querySelector(mp);
          if (!m || !m.contains(t) || m.contains(el)) return;
          const sl = Array.isArray(p[4]) ? p[4][k] : null;
          if (Array.isArray(sl) && sl.indexOf(rpSlot(t.parentElement, m)) < 0) return;  // 비대칭 자리 — 자기 고정이 비교해야
          judges = (args.inner[mp] || []).map(function (x) { return [document.querySelector(x[0]), x[1]]; });
          sib++;
        });
      }
      t.nodeValue = v + ' \u2063신원';
      const seen = judges.some(function (j) { return j[0] && !groupCheckAll(j[0], j[1]).ok; });
      t.nodeValue = v;
      if (!seen) miss.push(rpClean(v));
    }
    out.push({k: q.k, need: need, sib: sib, miss: miss});
  }
  return out;
}""")


def install_identity_census_all(monkeypatch) -> List[Dict[str, Any]]:
    """관찰마다 기준(v1.16.0 원문 JS)이 고정한 (대상, 쌍둥이) 중 사이 반복 행이 있는 것에 대해 모든 정적 글자 신원 보존을 잰다."""
    import perception.repeated as rp

    from test_ws62_r7 import _row_between_js

    original = rp.annotate_repeated
    log: List[Dict[str, Any]] = []
    base_js, row_js, census_js = _base_js(), _row_between_js(), _identity_census_all_js()

    async def census(page, engine, elements):
        block = await original(page, engine, elements)
        targets, handles = _targets(engine, elements)
        raw = await page.evaluate(base_js, {"targets": targets}) if targets else {"sets": []}
        pairs = []
        for st in raw.get("sets") or []:
            for g in st.get("groups") or []:
                for m in [dict(m, path=g["path"]) for m in g.get("members") or []] + list(g.get("folded") or []):
                    if m.get("pin", True) is False:
                        continue
                    h = handles[int(m["i"])]
                    if await page.evaluate(row_js, {"path": m["path"], "css": h.css_path}) is not True:
                        continue
                    pairs.append({"k": len(pairs), "path": m["path"], "css": h.css_path, "pins": rp.pin_args(h),
                                  "name": h.name})
        inner: Dict[str, List[Any]] = {}
        for h in handles:
            for pin in rp.handle_pins(h):
                inner.setdefault(pin.path, []).append([h.css_path, rp.pin_args(h)])
        got = await page.evaluate(census_js, {"pairs": [{k: v for k, v in p.items() if k != "name"} for p in pairs],
                                              "inner": inner}) if pairs else []
        missing = [{"name": pairs[r["k"]]["name"], "path": pairs[r["k"]]["path"], "miss": r["miss"]}
                   for r in got if r["miss"] or r.get("bad")]
        log.append({"pairs": len(pairs), "need": sum(r["need"] for r in got), "sibling": sum(r.get("sib", 0) for r in got),
                    "bad": len(missing), "missing": missing[:6]})
        return block

    monkeypatch.setattr(rp, "annotate_repeated", census)
    return log


CENSUS_ALL_PAGES = IDENTITY_PAGES + [f"sib:{s}/{p}" for s, p in R8_REGRESSIONS]


async def _run_census_all(name: str, monkeypatch) -> List[Dict[str, Any]]:
    from playwright.async_api import async_playwright

    log = install_identity_census_all(monkeypatch)
    html = sibling_html(*name[4:].split("/")) if name.startswith("sib:") else _page_for(name)
    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, html)
        try:
            if name.startswith("hanatour"):
                await _hanatour_states(d, page, name.endswith("_css"))
            else:
                await _observe(d, {"force_full_tree": True, "prune_top_n": 1000})
        finally:
            await browser.close()
    return log


@requires_chromium
@pytest.mark.parametrize("name", CENSUS_ALL_PAGES)
async def test_identity_census_all_static_text_compared(name, monkeypatch):
    """RED(R8): sib:strong·span·dt_dd·inner_labelled — 첫 section 의 비제목 상품명을 둘째 section 대상의 어느 고정도 비교 안 함.

    기준이 고정한 대상마다 기준 지문의 모든 정적 글자가 그 대상의 다중 고정에서 비교된다(형제 안쪽 쌍둥이의 대칭 자리만 예외).
    """
    log = await _run_census_all(name, monkeypatch)
    assert log and any(e["pairs"] for e in log), log  # 측정 조건(규칙 4)
    assert not any(e["bad"] for e in log), json.dumps(log, ensure_ascii=False)[:1500]


@requires_chromium
async def test_identity_census_all_has_non_heading_samples():
    """측정 조건(규칙 4): census 가 비제목 글자를 실제로 바꿔 봤고(need>0), 형제 대칭 자리 예외도 실제로 쓰였다(sibling>0)."""
    mp = pytest.MonkeyPatch()
    try:
        logs = {n: await _run_census_all(n, mp) for n in ("sib:strong/buried", "sib:inner_labelled/buried", "hanatour_css")}
    finally:
        mp.undo()
    assert all(sum(e["need"] for e in log) > 0 for log in logs.values()), logs
    assert sum(e["sibling"] for log in logs.values() for e in log) > 0, logs


@requires_chromium
@pytest.mark.parametrize("name", [f"sib:{s}/{p}" for s, p in R8_REGRESSIONS])
async def test_identity_census_all_detects_r8_rule(name, monkeypatch):
    """주입: 가림 규칙을 R8 방식으로 되돌리면 census 가 4변형 모두에서 누락을 잡는다."""
    with r8_mask_rule():
        log = await _run_census_all(name, monkeypatch)
    assert sum(e["bad"] for e in log) >= 1, json.dumps(log, ensure_ascii=False)[:1500]
