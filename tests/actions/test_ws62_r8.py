"""WS-62 R8: 바깥 가림 + 안쪽 대칭 자리가 묻힌 상품명 신원을 함께 없애던 결함(R7 재검증 BLOCKING, #136).

상품 article×4, 각 article 안 옵션 section×2(안쪽 형성 쌍둥이). 상품명 H2 는 첫 section 안에만(비대칭) — 안쪽 고정은 대칭
자리 규칙으로 H2 를 빼고, 바깥(기준) 고정은 안쪽 쌍둥이 subtree 를 가려 H2 를 또 뺐다 → 상품 A→Z 뒤 옛 '담기' 가 Z 를 눌렀다.

원칙: 가린 신원 근거는 어딘가에서 반드시 비교된다. 바깥 지문의 가림은 안쪽 고정 지문이 실제로 비교하지 않는(비대칭 자리)
'신원 성격' 글자(제목 h1~h6·role=heading·caption·legend·th·쌍둥이 루트 aria-labelledby 대상 — 구조로만)를 가리지 않는다.
검증 증거 independent_adversarial.py 11변형을 그대로 편입한다. 모든 시험은 로컬 Chromium set_content(요청 차단).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r4_hanatour import _observe, _open
from test_ws62_r6 import _base_js, _census_html, _hanatour_states, _targets
from test_ws62_r7 import (NESTED_CASES, UNION_PAGES, _row_between_js, nested_product_html, nav_rows_html,
                          same_twin_layout_html)

# ------------------------------------------------------------------ 검증 증거 11변형(independent_adversarial.py 그대로)


def _quick() -> str:
    return "<ul>" + "".join("<li><button>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"


def _opts(kind: str = "ul", buried: bool = False, k: str = "A", rolling: bool = False) -> str:
    parts = []
    for i in range(2):
        title = f"<h2 class=nm>상품 {k}</h2>" if buried and i == 0 else ""
        roll = "<small class=roll>오늘 12명 구매</small>" if rolling and i == 0 else ""
        cells = "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3))
        if kind == "table":
            cells = ("<table><tbody><tr>" + "".join("<td><button class=target>담기</button><button>찜</button></td>"
                                                   for _ in range(3)) + "</tr></tbody></table>")
        else:
            cells = "<ul>" + cells + "</ul>"
        parts.append(f"<section><h3>옵션 {i}</h3>{title}{roll}{cells}</section>")
    return "<div class=options>" + "".join(parts) + "</div>"


def adversarial_html(case: str, rolling: bool = False) -> str:
    rows = []
    buried = case in BURIED
    for k in "ABCD":
        name = "" if buried else f"<h2 class=nm>상품 {k}</h2>"
        inner = _opts("table" if "table" in case else "ul", buried, k, rolling)
        if "deep" in case:
            inner = "<div>" * 7 + inner + "</div>" * 7
        if case == "tab_panel":
            inner = "<div role=tabpanel>" + inner + "</div>"
        if case == "carousel":
            inner += inner
        if case == "list_in_table":
            inner = "<table><tbody><tr><td>" + inner + "</td></tr></tbody></table>"
        if case == "multi_nested":
            inner = "<aside>" + _quick() + inner + "</aside>" + _opts(k=k)
        if case == "calendar":
            inner = inner.replace(">담기<", ">19<")
        if case == "nav_product":
            inner = "<nav>" + inner + "</nav>"
        rows.append(f"<article class=product data-sku={k}>{name}{_quick()}{inner}</article>")
    return "<!doctype html><html><body><p id=out>none</p><div class=grid>" + "".join(rows) + "</div></body></html>"


BURIED = ("buried_name", "buried_name_table", "buried_name_deep")
ADV_CASES = ["nested", "deep", "tab_panel", "carousel", "list_in_table", "multi_nested", "calendar", "nav_product",
             *BURIED]
SINK = ("() => { window.sink = []; document.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {"
        " const a = b.closest('.product'); window.sink.push(a.dataset.sku); document.querySelector('#out').textContent ="
        " a.dataset.sku + '/' + window.sink.length; })); }")  # 반복 클릭도 화면 변화(사후조건)가 나게 횟수를 붙인다
TO_Z = "() => { const a = document.querySelector('.product'); a.dataset.sku = 'Z'; a.querySelector('.nm').textContent = '상품 Z'; }"
ROLL = "(n) => { document.querySelectorAll('.product .roll').forEach(e => e.textContent = '오늘 ' + (12 + n) + '명 구매'); }"


async def _abort_all(page) -> None:
    await page.context.route("**/*", lambda route: route.abort())


async def _targets_by_sku(engine, page, sku: str, which: str = "all") -> List[Any]:
    """상품 sku 의 '.target' 대상 핸들(문서 순서) — which='all' 이면 전부."""
    out = []
    for h in engine.handles.values():
        if h.css_path and not h.is_shadow and await page.evaluate(
                "([css, sku]) => { const e = document.querySelector(css); const p = e && e.closest('.product');"
                " return !!(e && p && e.matches('.target') && p.dataset.sku === sku); }", [h.css_path, sku]):
            out.append(h)
    assert out, f"상품 {sku} 대상 없음"
    return out


@requires_chromium
@pytest.mark.parametrize("case", ADV_CASES)
async def test_adversarial_old_target_refused_after_product_change(case):
    """RED(R7 728b239): buried_name·_table·_deep 은 상품 A→Z 뒤 옛 '담기' 가 Z 를 눌렀다(success, sink=['Z']).

    11변형 모두: 옛 id → TOCTOU·sink=[] (상품 A 의 '담기' 전부 — 상품명 없는 둘째 옵션 section 의 '담기' 포함),
    바뀌지 않은 상품 B 는 정상 클릭.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, adversarial_html(case))
        try:
            await _abort_all(page)
            await page.evaluate(SINK)
            await _observe(d, {"force_full_tree": True, "prune_top_n": 1000})
            a_all = await _targets_by_sku(engine, page, "A")
            b = (await _targets_by_sku(engine, page, "B"))[0]
            await page.evaluate(TO_Z)
            bad = [await d.dispatch(ActionType.CLICK, {"element_id": h.element_id}) for h in a_all]
            sink = await page.evaluate("window.sink")
            good = await d.dispatch(ActionType.CLICK, {"element_id": b.element_id})
            ctrl = await page.evaluate("window.sink")
        finally:
            await browser.close()
    assert sink == [], f"다른 상품을 눌렀다: {sink}"
    assert all(r.success is False and r.error_code is ErrorCode.TOCTOU_MISMATCH for r in bad), \
        [(r.error_code, r.error_message) for r in bad]
    assert good.success and ctrl == ["B"], (good.error_code, good.error_message, ctrl)


@requires_chromium
@pytest.mark.parametrize("case", BURIED)
async def test_buried_name_with_rolling_small(case):
    """결합 회귀: 첫 옵션 section 에 표지 없는 롤링 <small>(비제목 비대칭 글자)이 함께 있을 때 — 롤링만 바뀌면 거부 0
    (5회 갱신 뒤 같은 id 로 상품 A 정상 클릭), 상품 A→Z 면 옛 id 거부·sink 없음."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, adversarial_html(case, rolling=True))
        try:
            await _abort_all(page)
            await page.evaluate(SINK)
            await _observe(d, {"force_full_tree": True, "prune_top_n": 1000})
            a_all = await _targets_by_sku(engine, page, "A")
            rolled = []
            for n in range(1, 6):
                await page.evaluate(ROLL, n)
                rolled.append(await d.dispatch(ActionType.CLICK, {"element_id": a_all[n % len(a_all)].element_id}))
            ok_sink = await page.evaluate("window.sink")
            await page.evaluate(TO_Z)
            await page.evaluate(ROLL, 9)
            bad = [await d.dispatch(ActionType.CLICK, {"element_id": h.element_id}) for h in a_all]
            sink = await page.evaluate("window.sink")
        finally:
            await browser.close()
    assert [r.error_code for r in rolled] == [None] * 5 and ok_sink == ["A"] * 5, \
        ([(r.error_code, r.error_message) for r in rolled], ok_sink)
    assert sink == ok_sink, f"다른 상품을 눌렀다: {sink}"
    assert all(r.error_code is ErrorCode.TOCTOU_MISMATCH for r in bad), [(r.error_code, r.error_message) for r in bad]


# ------------------------------------------------------------------ census 보강: 신원 보존 단정


#: 기준 고정 (대상, 쌍둥이)마다: 기준 지문이 비교하던 '제목 성격' 글자(쌍둥이 g 안 h1~h6·role=heading·caption·legend·th 의
#: 막히지 않은 텍스트 노드 + g 의 aria-labelledby 대상)를 하나씩 바꿔 보고, 그 대상의 R8 다중 고정(groupCheckAll)이 하나라도
#: 다르다고 판정하는지 센다(같다고 하면 신원 누락). 바꾼 글자는 같은 evaluate 안에서 즉시 되돌린다.
#: 하나의 허용: 대상을 담지 않은 형제 안쪽 형성 쌍둥이(그 대상 기준 고정의 가림 경로) 안 글자는 그 안쪽 쌍둥이의 대상 고정이
#: 실제로 비교하면 된다(어딘가에서 비교 — 다음달 등 안쪽 내용 변화로 바깥 대상을 거부하지 않는 가림의 목적). 그 안쪽 고정도
#: 비교하지 않으면 누락이다(R7 buried: 상품명 H2 는 형제 section 쪽 대상에게도 누구도 비교하지 않았다).
def _identity_census_js() -> str:
    from perception.repeated import GROUP_CHECK_JS

    return ("(args) => {" + GROUP_CHECK_JS + r"""
  const HEAD = 'h1,h2,h3,h4,h5,h6,[role=heading],caption,legend,th';
  const out = [];
  for (const q of args.pairs) {
    const g = document.querySelector(q.path), el = document.querySelector(q.css);
    if (!g || !el || !g.contains(el)) { out.push({k: q.k, need: 0, miss: [], bad: 'gone'}); continue; }
    const nodes = [];
    const heads = Array.from(g.querySelectorAll(HEAD));
    for (const id of (g.getAttribute('aria-labelledby') || '').split(/\s+/)) { const r = id && document.getElementById(id); if (r) heads.push(r); }
    for (const h of heads) {
      const w = document.createTreeWalker(h, NodeFilter.SHOW_TEXT);
      let t;
      while ((t = w.nextNode())) {
        if (!t.nodeValue || !t.nodeValue.trim() || nodes.indexOf(t) >= 0) continue;
        if (g.contains(t) && rpBlocked(t.parentElement, g)) continue;   // 기준도 비교하지 않던 글자(대화형·반복 항목·숨김)
        nodes.push(t);
      }
    }
    // 기준 지문 상한(1,000자) 안에 든 글자만 기준이 비교했다.
    const base = rpIdentity(g).body.slice(0, RP_FP_LIMIT - 8);
    const miss = [];
    let need = 0, sib = 0;
    for (const t of nodes) {
      const v = t.nodeValue;
      if (base.indexOf(rpNorm(v)) < 0) continue;
      need++;
      if (!groupCheckAll(el, q.pins).ok) { miss.push('pre:' + rpClean(v)); continue; }
      // 대상을 담지 않은 가림(형제 안쪽 쌍둥이) 안 글자면 그 쌍둥이 고정을 가진 대상들 쪽에서 본다.
      let judges = [[el, q.pins]];
      for (const p of q.pins) {
        if (p[0] !== q.path) continue;
        for (const mp of (p[2] || [])) {
          const m = document.querySelector(mp);
          if (m && m.contains(t) && !m.contains(el)) {
            judges = (args.inner[mp] || []).map(function (x) { return [document.querySelector(x[0]), x[1]]; });
            sib++;
          }
        }
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


def install_identity_census(monkeypatch) -> List[Dict[str, Any]]:
    """관찰마다 기준(v1.16.0 원문 JS)이 고정한 (대상, 쌍둥이) 중 사이 반복 행이 있는 것에 대해 신원 보존을 잰다."""
    import perception.repeated as rp

    original = rp.annotate_repeated
    log: List[Dict[str, Any]] = []
    base_js, row_js, census_js = _base_js(), _row_between_js(), _identity_census_js()

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
        # 안쪽 쌍둥이 경로 → 그 경로로 고정된 대상들(css, 다중 고정) — 형제 가림 글자의 '어딘가' 비교 판정용.
        inner: Dict[str, List[Any]] = {}
        for h in handles:
            for pin in rp.handle_pins(h):
                inner.setdefault(pin.path, []).append([h.css_path, rp.pin_args(h)])
        got = await page.evaluate(census_js, {"pairs": [{k: v for k, v in p.items() if k != "name"} for p in pairs],
                                              "inner": inner}) if pairs else []
        need = sum(r["need"] for r in got)
        missing = [{"name": pairs[r["k"]]["name"], "path": pairs[r["k"]]["path"], "miss": r["miss"]}
                   for r in got if r["miss"] or r.get("bad")]
        log.append({"pairs": len(pairs), "need": need, "sibling": sum(r.get("sib", 0) for r in got), "bad": len(missing),
                    "missing": missing[:6]})
        return block

    monkeypatch.setattr(rp, "annotate_repeated", census)
    return log


IDENTITY_PAGES = UNION_PAGES + list(BURIED) + ["nested", "multi_nested", "carousel"]


def _page_for(name: str) -> str:
    if name == "same_twin":
        return same_twin_layout_html()
    if name == "nav_rows":
        return nav_rows_html()
    if name in ADV_CASES and name not in NESTED_CASES:
        return adversarial_html(name)
    if name in NESTED_CASES:
        return nested_product_html(name)
    return _census_html(name)


async def _run_census(name: str, monkeypatch) -> List[Dict[str, Any]]:
    from playwright.async_api import async_playwright

    log = install_identity_census(monkeypatch)
    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, _page_for(name))
        try:
            if name.startswith("hanatour"):
                await _hanatour_states(d, page, name.endswith("_css"))
            else:
                await _observe(d, {"force_full_tree": True, "prune_top_n": 1000})
        finally:
            await browser.close()
    return log


@requires_chromium
@pytest.mark.parametrize("name", IDENTITY_PAGES)
async def test_identity_census_baseline_headings_still_compared(name, monkeypatch):
    """RED(R7): buried 3쪽에서 기준 지문이 비교하던 상품명 H2 를 어느 R7 고정도 비교하지 않았다(경로 합집합 census 는 통과).

    기준이 고정한 대상마다 기준 지문의 제목 성격 글자가 R8 다중 고정 중 하나의 비교 대상이어야 한다(누락 = 실패).
    """
    log = await _run_census(name, monkeypatch)
    assert log and any(e["pairs"] for e in log), log  # 측정 조건(규칙 4): 기준이 행 안 대상을 고정했다
    assert not any(e["bad"] for e in log), json.dumps(log, ensure_ascii=False)[:1500]


@requires_chromium
async def test_identity_census_has_heading_samples():
    """측정 조건(규칙 4): census 가 실제로 제목 성격 글자를 바꿔 본 페이지가 있다(buried·nested·deep_inner)."""
    import _pytest.monkeypatch

    mp = _pytest.monkeypatch.MonkeyPatch()
    try:
        needs = {n: sum(e["need"] for e in await _run_census(n, mp)) for n in ("buried_name", "nested_prefix", "deep_inner")}
    finally:
        mp.undo()
    assert all(v > 0 for v in needs.values()), needs


@requires_chromium
async def test_identity_census_detects_injected_loss(monkeypatch):
    """census 자체의 사보타주: 상품 A 첫 '담기' 핸들에서 기준(바깥 article) 고정 하나를 빼면 누락 1건으로 잡는다."""
    import perception.repeated as rp

    real = rp.annotate_repeated
    state = {"done": False}

    async def drop_one(page, engine, elements):
        block = await real(page, engine, elements)
        if not state["done"]:
            for h in engine.handles.values():
                pins = rp.handle_pins(h)
                if h.name == "담기" and any(p.path.endswith("article:nth-of-type(1)") for p in pins):
                    rp.set_pins(h, [p for p in pins if not p.path.endswith("article:nth-of-type(1)")])
                    state["done"] = True
                    break
        return block

    monkeypatch.setattr(rp, "annotate_repeated", drop_one)
    log = await _run_census("buried_name", monkeypatch)
    assert state["done"]
    assert sum(e["bad"] for e in log) == 1, json.dumps(log, ensure_ascii=False)[:1500]
