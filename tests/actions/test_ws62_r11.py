"""WS-62 R11: 원칙 — '가린 글자는 반드시 어딘가에서 비교된다'(사용자 결정 A, 2026-10-24, #136).

R10 재검증 BLOCKING(R1): 상품 article×4 안 옵션 section×2(안쪽 형성 쌍둥이), 각 section 의 버튼 밖 `aria-live=polite` 영역에 상품명
A. 안쪽 고정은 live 영역을 동적 글자로 빼는데, 대칭 자리 목록이 null 이면 바깥(기준 raw) 고정이 '안쪽이 전체 비교' 로 보고 section 을
통째로 가렸다 — 아무도 상품명을 비교하지 않아 상품 A→Z 뒤 옛 '정보'·'담기' 가 Z 를 눌렀다(기준은 거부, 14변형 중 4).

결정: 바깥 가림은 안쪽 고정 지문이 **실제로 포함한** 글자(rpCompared — 지문과 같은 함수: 대칭 자리·동적 제외·막힘)만. 산출이 없으면
가리지 않는다. 유일한 예외는 #156(가린 안쪽 항목 단위의 버튼 안 조각 글자 — 하나투어 달력 '4<p>101만</p>' 흐름 보존).

검사는 사례가 아니라 불변식(기준 기반 census): 모든 시험 페이지에서 실제 v1.16.0 원문 JS(tests/fixtures/ws62/repeated_v1160.js)가
고정한 (대상, 쌍둥이)마다, 기준 지문을 실제로 바꾸는 글자 조각(텍스트 노드 하나씩 바꿔 기준 지문이 달라지는 것 — 후보 선택도 기준)을
R11 의 그 대상 고정 또는 그 글자를 가린 안쪽 쌍둥이 대상들의 고정이 거부해야 한다. 누락 = 실패(#156 경로만 사유와 함께 허용).
모든 시험은 로컬 Chromium set_content(요청 차단).
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r4_hanatour import _observe
from test_ws62_r6 import CENSUS_PAGES, _base_js, _hanatour_states, _targets
from test_ws62_r7 import NESTED_CASES, UNION_PAGES, live_panels_html
from test_ws62_r8 import ADV_CASES, _page_for, adversarial_html
from test_ws62_r9 import SIBLING_CASES, _base_check_js, baseline_runtime, sibling_html
from test_ws62_r10 import CORPUS, NEW_CASES, corpus_html, new_html

# ------------------------------------------------------------------ 검증 증거 cold_symmetric.py 14변형(그대로 편입)

#: (이름 태그, live 영역 안, 반복 span 셋) — 상품명이 모든 안쪽 section 에 같은 자리로(대칭 → 안쪽 대칭 자리 null).
SYM_CASES: List[Tuple[str, bool, bool]] = [("h2", False, False), ("span", False, False), ("strong", False, False),
                                           ("dd", False, False), ("span", True, False), ("span", False, True),
                                           ("span", True, True)]
SYM_SELECTORS = (".quick", ".target")


def symmetric_html(tag: str, live: bool = False, units: bool = False) -> str:
    """검증 증거 cold_symmetric.py make() 와 같은 HTML."""
    cards = []
    for sku in "ABCD":
        opts = []
        for i in range(2):
            nm = f"<{tag} class=nm>여행 상품 {sku}</{tag}>"
            if units:
                nm = "<div>" + "".join(f"<span class=nm>{sku}{j}</span>" for j in range(3)) + "</div>"
            if live:
                nm = "<div aria-live=polite>" + nm + "</div>"
            opts.append("<section><h3>옵션 " + str(i) + "</h3>" + nm + "<p><small>공통 설명</small></p><ul>"
                        + "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3))
                        + "</ul></section>")
        quick = "<ul>" + "".join("<li><button class=quick>" + s + "</button></li>" for s in ["정보", "배송", "문의"]) + "</ul>"
        cards.append(f"<article class=product data-sku={sku}>{quick}<div class=options>" + "".join(opts) + "</div></article>")
    return "<!doctype html><html><body><output id=out>none</output><div class=grid>" + "".join(cards) + "</div></body></html>"


SYM_SINK = ("() => { window.sink = []; document.querySelectorAll('button').forEach(b => b.onclick = () => {"
            " const a = b.closest('.product'); window.sink.push(a.dataset.sku);"
            " document.querySelector('#out').textContent = a.dataset.sku + '/' + window.sink.length; }); }")
SYM_TO_Z = ("() => { const a = document.querySelector('.product'); a.dataset.sku = 'Z';"
            " a.querySelectorAll('.nm').forEach(n => n.textContent = n.textContent.replace(/A/g, 'Z')); }")


async def _first_click(browser, html: str, selector: str) -> Dict[str, Any]:
    """상품 A 의 첫 selector 대상 하나만: 관찰 → A→Z → 옛 id 로 한 번 누르고, 바뀌지 않은 B 의 '.quick' 을 누른다."""
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = await ctx.new_page()
        await ctx.route("**/*", lambda route: route.abort())
        await page.set_content(html)
        await page.evaluate(SYM_SINK)
        engine = PerceptionEngine()
        d = ActionDispatcher(DispatchContext(page=page, engine=engine))
        await _observe(d, {"force_full_tree": True, "prune_top_n": 1000})
        hs = [h for h in engine.handles.values() if h.css_path and not h.is_shadow]
        hit = await page.evaluate("(a) => { const want = document.querySelector('.product').querySelector(a.sel);"
                                  " return a.css.map(s => document.querySelector(s) === want); }",
                                  {"sel": selector, "css": [h.css_path for h in hs]})
        bhit = await page.evaluate("(css) => css.map(s => { const e = document.querySelector(s), a = e && e.closest('.product');"
                                   " return !!(e && a && a.dataset.sku === 'B' && e.matches('.quick')); })",
                                   [h.css_path for h in hs])
        h = next(x for x, ok in zip(hs, hit) if ok)
        bh = next(x for x, ok in zip(hs, bhit) if ok)
        await page.evaluate(SYM_TO_Z)
        bad = await d.dispatch(ActionType.CLICK, {"element_id": h.element_id})
        sink = await page.evaluate("window.sink")
        good = await d.dispatch(ActionType.CLICK, {"element_id": bh.element_id})
        ctrl = await page.evaluate("window.sink")
    finally:
        await ctx.close()
    return {"refused": bad.error_code is ErrorCode.TOCTOU_MISMATCH, "sink": sink,
            "b_ok": bool(good.success) and ctrl[-1:] == ["B"]}


@requires_chromium
@pytest.mark.parametrize("case", SYM_CASES, ids=[f"{t}-live{int(lv)}-units{int(u)}" for t, lv, u in SYM_CASES])
async def test_symmetric_first_click_not_worse_than_baseline(case):
    """RED(R10 1438616): span-live·span-live-units 의 '.quick'·'.target' 4건 — 기준은 TOCTOU·sink=[], R10 은 sink=['Z'].

    14변형(7 HTML × 2 selector) 모두 첫 클릭 하나만 비교: 기준이 거부한 클릭을 통과시키지 않고, B 는 둘 다 정상.
    """
    from playwright.async_api import async_playwright

    html = symmetric_html(*case)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            rows = []
            for sel in SYM_SELECTORS:
                now = await _first_click(browser, html, sel)
                with baseline_runtime():
                    base = await _first_click(browser, html, sel)
                rows.append((sel, now, base))
        finally:
            await browser.close()
    for sel, now, base in rows:
        assert base["b_ok"] and now["b_ok"], (sel, now, base)
        assert not (base["refused"] and not now["refused"]), f"기준 대비 오클릭 {case} {sel}: now={now} base={base}"
        assert now["sink"].count("Z") <= base["sink"].count("Z"), (sel, now, base)


# ------------------------------------------------------------------ 조정자 결정 B 의 위험축: 모든 형제 항목에 같은 자리로 반복된 상품명

#: 상품명이 **모든** 안쪽 section 에 같은 자리로(대칭, 비-live) — 태그 5 × 형제 2·3·4. 쌍둥이 밖 대상('정보/배송/문의')에게 가린
#: 상품명은 안쪽 대상 고정만 비교한다(census '다른 대상 고정만 비교') — 실제 v1.16.0 JS 대비 행동으로 오클릭 0 을 잰다.
SYMNAME_TAGS = ("h2", "span", "strong", "dd", "labelledby")
SYMNAME_CASES = [(t, n) for t in SYMNAME_TAGS for n in (2, 3, 4)]


def symname_html(tag: str, n: int) -> str:
    cards = []
    for sku in "ABCD":
        opts = []
        for i in range(n):
            attr, nm = "", f"<{tag} class=nm>여행 상품 {sku}</{tag}>"
            if tag == "labelledby":
                attr, nm = f' aria-labelledby="lbl{sku}{i}"', f"<span class=nm id=lbl{sku}{i}>여행 상품 {sku}</span>"
            opts.append(f"<section{attr}><h3>옵션 {i}</h3>{nm}<p><small>공통 설명</small></p><ul>"
                        + "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3))
                        + "</ul></section>")
        quick = "<ul>" + "".join("<li><button class=quick>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"
        cards.append(f"<article class=product data-sku={sku}>{quick}<div class=options>" + "".join(opts) + "</div></article>")
    return "<!doctype html><html><body><output id=out>none</output><div class=grid>" + "".join(cards) + "</div></body></html>"


@requires_chromium
async def test_symmetric_names_all_siblings_not_worse_than_baseline():
    """조정자 결정 B 조건(2): 상품명이 모든 형제 section 에 대칭 반복(h2·span·strong·dd·aria-labelledby × 형제 2·3·4) — 쌍둥이 밖
    '정보/배송/문의'·쌍둥이 안 '담기' 전부, 상품 A→Z 뒤 기준(v1.16.0 원문)이 거부한 (대상, 변화)를 하나도 통과시키지 않는다."""
    from playwright.async_api import async_playwright

    lost, refused = [], 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for tag, n in SYMNAME_CASES:
                html = symname_html(tag, n)
                now = await _all_targets_flow(browser, html, SYM_TO_Z)
                with baseline_runtime():
                    base = await _all_targets_flow(browser, html, SYM_TO_Z)
                assert now["css"] == base["css"] and now["b_ok"] and base["b_ok"], (tag, n, now, base)
                refused += sum(base["refused"])
                lost += [(tag, n, c) for c, rb, rn in zip(now["css"], base["refused"], now["refused"]) if rb and not rn]
                assert now["sink"].count("Z") <= base["sink"].count("Z"), (tag, n, now["sink"], base["sink"])
        finally:
            await browser.close()
    print(f"\nWS62R11-SYMNAME cases={len(SYMNAME_CASES)} base_refused={refused} lost={len(lost)}")
    assert refused > 0  # 측정 조건(규칙 4)
    assert not lost, lost


# ------------------------------------------------------------------ 검증 증거 corpus_probe.py 독립 48조합(그대로 편입)


def independent_cases() -> List[Dict[str, str]]:
    return [{"id": f"{shape}-{layout}-{pos}", "shape": shape, "layout": layout, "pos": pos}
            for shape in ("data_mix", "title_mix", "alt_mix", "data_labelled", "title_labelled", "alt_same_name")
            for layout in ("twins5", "deep4", "list_table_list", "same_names")
            for pos in ("first", "last")]


def independent_html(c: Dict[str, str]) -> str:
    """검증 증거 corpus_probe.py independent_html 과 같은 HTML(data-*·title·img alt 와 글자 혼합, 쌍둥이 5·4중 중첩·목록/표/목록·같은 이름)."""
    def name(sku: str, uid: str) -> str:
        text = f"여행 상품 {sku}"
        shape = c["shape"]
        if shape == "data_mix":
            return f'<span class=identity data-name="{text}">여행 상품 <em>{sku}</em></span>'
        if shape == "title_mix":
            return f'<div class=identity title="{text}"><span>여행 상품</span> <strong>{sku}</strong></div>'
        if shape == "alt_mix":
            return f'<div class=identity><img alt="{text}"><span>여행 상품 {sku}</span></div>'
        if shape == "data_labelled":
            return f'<div class=identity data-product="{text}" role=heading aria-label="{text}"><small>{text}</small></div>'
        if shape == "title_labelled":
            return f'<div class=identity title="{text}" aria-labelledby="{uid}"><span id="{uid}">{text}</span></div>'
        return f'<div class=identity><img alt="같은 이름 여행"><span>같은 이름 여행</span><em>재고 {sku}</em></div>'

    cards = []
    for sku in "ABCDE":
        counter = [0]

        def section(depth: int) -> str:
            n = 5 if c["layout"] == "twins5" else 2
            out = []
            for i in range(n):
                counter[0] += 1
                uid = f"caption_{sku}_{counter[0]}"
                at = 0 if c["pos"] == "first" else n - 1
                nm = name(sku, uid) if i == at else ""
                if depth:
                    body = section(depth - 1)
                else:
                    rows = "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3))
                    body = "<ul>" + rows + "</ul>"
                    if c["layout"] == "list_table_list":
                        body = "<table><tbody><tr><td>" + body + "</td></tr></tbody></table>"
                out.append(f"<section><h3>옵션 {i}</h3>{nm}<p><small>공통 설명</small></p>"
                           + ("<aside><em>추가 정보</em></aside>" if i < n - 1 else "") + body + "</section>")
            return "<div class=options>" + "".join(out) + "</div>"

        quick = "<ul>" + "".join("<li><button class=quick>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"
        title = "<h2>공동 여행 상품</h2>" if c["layout"] == "same_names" else ""
        body = section(2 if c["layout"] == "deep4" else 0)
        article = f"<article class=product data-sku={sku}>{title}{quick}{body}</article>"
        cards.append("<li>" + article + "</li>" if c["layout"] == "list_table_list" else article)
    body = ("<ul>" + "".join(cards) + "</ul>") if c["layout"] == "list_table_list" else "<div class=grid>" + "".join(cards) + "</div>"
    return "<!doctype html><html><body><output id=out>none</output>" + body + "</body></html>"


IND_CHANGE = """() => { const a = document.querySelector('.product'); a.dataset.sku = 'Z';
 if (a.hasAttribute('aria-label')) a.setAttribute('aria-label', '상품 Z');
 a.querySelectorAll('.identity').forEach(n => { n.querySelectorAll('img').forEach(e => e.setAttribute('alt', '여행 상품 Z'));
  for (const attr of ['data-name', 'data-product', 'title', 'aria-label']) if (n.hasAttribute(attr)) n.setAttribute(attr, '여행 상품 Z');
  const w = document.createTreeWalker(n, NodeFilter.SHOW_TEXT); let t; while ((t = w.nextNode())) t.textContent = t.textContent.replace(/A/g, 'Z'); }); }"""


async def _all_targets_flow(browser, html: str, change: str) -> Dict[str, Any]:
    """상품 A 의 '.target'·'.quick' 대상 관찰 → change → 옛 id 로 전부 누르고, 바뀌지 않은 B 를 누른다(R10 _flow 와 같은 흐름)."""
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = await ctx.new_page()
        await ctx.route("**/*", lambda route: route.abort())
        await page.set_content(html)
        engine = PerceptionEngine()
        d = ActionDispatcher(DispatchContext(page=page, engine=engine))
        await page.evaluate(SYM_SINK)
        await _observe(d, {"force_full_tree": True, "prune_top_n": 2000})
        hs = [h for h in engine.handles.values() if h.css_path and not h.is_shadow]
        skus = await page.evaluate("(css) => css.map(s => { const e = document.querySelector(s), a = e && e.closest('.product');"
                                   " return e && a && e.matches('.target,.quick') ? a.dataset.sku : null; })",
                                   [h.css_path for h in hs])
        targets = [h for h, s in zip(hs, skus) if s == "A"]
        b = next(h for h, s in zip(hs, skus) if s == "B")
        await page.evaluate(change)
        res = [await d.dispatch(ActionType.CLICK, {"element_id": h.element_id}) for h in targets]
        sink = await page.evaluate("window.sink")
        good = await d.dispatch(ActionType.CLICK, {"element_id": b.element_id})
        ctrl = await page.evaluate("window.sink")
    finally:
        await ctx.close()
    return {"css": [h.css_path for h in targets], "refused": [r.error_code is ErrorCode.TOCTOU_MISMATCH for r in res],
            "sink": sink, "b_ok": bool(good.success) and ctrl[-1:] == ["B"]}


@requires_chromium
async def test_independent_48_not_worse_than_baseline():
    """독립 48조합(검증 생성기): 기준이 거부한 (대상, 상품 A→Z)를 통과시키지 않는다, 다른 상품 클릭 ≤ 기준, B 정상."""
    from playwright.async_api import async_playwright

    lost, refused = [], 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for c in independent_cases():
                html = independent_html(c)
                now = await _all_targets_flow(browser, html, IND_CHANGE)
                with baseline_runtime():
                    base = await _all_targets_flow(browser, html, IND_CHANGE)
                assert now["css"] == base["css"] and now["b_ok"] and base["b_ok"], (c, now, base)
                refused += sum(base["refused"])
                lost += [(c["id"], x) for x, rb, rn in zip(now["css"], base["refused"], now["refused"]) if rb and not rn]
                assert now["sink"].count("Z") <= base["sink"].count("Z"), (c, now["sink"], base["sink"])
        finally:
            await browser.close()
    assert refused > 0  # 측정 조건(규칙 4): 기준이 실제로 거부하던 대상이 있다
    assert not lost, lost


# ------------------------------------------------------------------ 기준 기반 census(불변식)


#: 기준 고정 (대상, 쌍둥이)마다: 그 쌍둥이(+ 바깥 aria-labelledby 참조) 안 텍스트 노드를 하나씩 바꿔 **기준 원문 지문**이 달라지면
#: 그 글자는 기준이 비교하던 조각(need). R11 에서 (1) 그 대상의 다중 고정이 거부(self), (2) 형제 항목(가린 조각이 대상을 담은 형제와
#: 다른 형제 안 — 대상이 자기 형제 고정을 가질 때) 안 대상 고정이 거부(inner), (3) 그 밖 다른 대상 고정만 거부(other — 별도 집계)면
#: 비교된 것. 아니면 누락 — #156(가린 안쪽 쌍둥이 안 대화형 요소의 첫 텍스트 노드 밖 조각)만 사유와 함께 허용(allowed).
_CENSUS_JS = r"""(args) => {
  const BASE = (function () {
    __BASE__
    return {identity: rpIdentity, pinOf: rpPinOf, limit: RP_FP_LIMIT};
  })();
  const NOW = (function () {
    __NOW__
    // 같은 DOM 상태 안에서 같은 (묶음, 가림, 대칭 자리, raw) 지문은 한 번만 계산한다(대상마다 순번만 다르다) — 비교 결과는 같다.
    const uid = new WeakMap();
    let un = 0, memo = new Map();
    function U(e) { if (!uid.has(e)) uid.set(e, ++un); return uid.get(e); }
    const rawIdentity = rpIdentity;
    rpIdentity = function (g, opt) {
      const ms = (opt && opt.maskSlots) || null;
      const key = U(g) + '|' + ((opt && opt.masks) || []).map(function (m) {
        return U(m) + ':' + (ms && ms.has(m) ? (ms.get(m) ? Array.from(ms.get(m)).sort().join('\u0001') : 'N') : 'X');
      }).join(',') + '|' + (opt && opt.slots ? Array.from(opt.slots).sort().join('\u0001') : '-') + '|' + !!(opt && opt.raw);
      let v = memo.get(key);
      if (!v) { v = rawIdentity(g, opt); memo.set(key, v); }
      return v;
    };
    return {all: groupCheckAll, interactive: rpInteractive, clean: rpClean, sig: rpSig, slot: rpSlot, isItem: rpIsItem, reset: function () { memo = new Map(); }};
  })();
  function q(s) { try { return s ? document.querySelector(s) : null; } catch (x) { return null; } }
  function mutate(v) { return v.replace(/\S/, '\u0416$&'); }
  function firstText(e) {
    const w = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) { if (n.nodeValue && n.nodeValue.trim()) return n; }
    return null;
  }
  const judges = (args.handles || []).map(function (h) { const el = q(h[0]); return {el: el, pins: h[1], ok: !!el && NOW.all(el, h[1]).ok}; });
  // 조정자 결정(R11): 가린 조각이 대상이 속한 쌍둥이의 **다른 형제 항목** m 안에 있고, 대상을 담은 형제 a(같은 부모·같은 틀)를 그
  // 대상 고정이 실제로 비교할 때(대상 고정 경로 중 a)만 형제 쪽 고정의 비교를 인정한다.
  function sibOf(m, p) {
    const P = m.parentElement;
    for (let a = p.el; a && a !== P; a = a.parentElement) {
      if (a.parentElement === P) return a !== m && NOW.sig(a) === NOW.sig(m) && p.pins.some(function (x) { return q(x[0]) === a; });
    }
    return false;
  }
  function why156(t, p) {
    let top = null;
    for (let e = t.parentElement; e && e !== p.g; e = e.parentElement) { if (NOW.interactive(e)) top = e; }
    if (!top || firstText(top) === t) return null;
    return p.masks.some(function (m) { return m.contains(t); }) ? '#156 버튼 안 조각 글자(가린 안쪽 항목 낱말)' : null;
  }
  const byPath = new Map();
  for (const p of args.pairs) { if (!byPath.has(p.path)) byPath.set(p.path, []); byPath.get(p.path).push(p); }
  const out = [];
  let tested = 0;
  byPath.forEach(function (pairs, path) {
    const g = q(path);
    const live = [];
    for (const p of pairs) {
      const el = q(p.css);
      const r = {k: p.k, need: 0, self: 0, inner: 0, other: 0, allowed: [], miss: []};
      out.push(r);
      if (!g || !el || !g.contains(el)) { r.miss.push('gone'); continue; }
      if (!NOW.all(el, p.pins).ok) { r.miss.push('pre'); continue; }
      const masks = [], minfo = [];
      for (const pin of p.pins) {
        (pin[2] || []).forEach(function (mp, k) {
          const m = q(mp);
          if (!m) return;
          if (masks.indexOf(m) < 0) masks.push(m);
          const sl = Array.isArray(pin[4]) ? pin[4][k] : null;
          minfo.push({m: m, sl: Array.isArray(sl) ? sl : null});
        });
      }
      live.push({r: r, el: el, pins: p.pins, g: g, masks: masks, minfo: minfo});
    }
    if (!live.length) return;
    const seen = new Set(), nodes = [];
    const roots = [g];
    for (const id of (g.getAttribute('aria-labelledby') || '').split(/\s+/)) { const x = id && document.getElementById(id); if (x && !g.contains(x)) roots.push(x); }
    for (const h of roots) {
      const w = document.createTreeWalker(h, NodeFilter.SHOW_TEXT);
      let t;
      while ((t = w.nextNode())) { if (t.nodeValue && t.nodeValue.trim() && !seen.has(t)) { seen.add(t); nodes.push(t); } }
    }
    tested += nodes.length;
    // 기준 지문(상한 안 본문)을 바꾸는 노드 찾기 — 여러 노드를 함께 바꿔 그대로면 그 노드들은 모두 무관(삽입은 서로 상쇄되지 않는다),
    // 바뀌면 반으로 나눈다. 찾은 노드마다 아래에서 대상별 정확 비교를 다시 한다.
    const b0 = BASE.identity(g).body.slice(0, BASE.limit);
    function changes(list) {
      const keep = list.map(function (t) { return t.nodeValue; });
      list.forEach(function (t, i) { t.nodeValue = mutate(keep[i]); });
      try { return BASE.identity(g).body.slice(0, BASE.limit) !== b0; } finally { list.forEach(function (t, i) { t.nodeValue = keep[i]; }); }
    }
    function find(list) {
      if (!list.length || !changes(list)) return [];
      if (list.length === 1) return list;
      const h = list.length >> 1;
      return find(list.slice(0, h)).concat(find(list.slice(h)));
    }
    const id0 = BASE.identity(g);
    const fp0 = live.map(function (p) { return BASE.pinOf(id0, p.el); });
    for (const t of find(nodes)) {
      const v = t.nodeValue;
      t.nodeValue = mutate(v);
      NOW.reset();
      const jc = new Map();
      const jfail = function (k) { if (!jc.has(k)) jc.set(k, !NOW.all(judges[k].el, judges[k].pins).ok); return jc.get(k); };
      try {
        const id1 = BASE.identity(g);
        live.forEach(function (p, i) {
          if (BASE.pinOf(id1, p.el) === fp0[i]) return;
          p.r.need++;
          if (!NOW.all(p.el, p.pins).ok) { p.r.self++; return; }
          // 다른 대상 고정의 비교를 인정하는 것은 그 조각이 가림 m 의 **대칭 자리**(가림과 함께 저장한 mask_slots, null 이면 전체)이거나
          // m 안 반복 항목(순서열 — 형제 쌍둥이마다 항목 순서열이 있다) 안일 때뿐 — 대칭 자리 글자는 형제 쌍둥이마다 대응 글자가 있다(R9
          // census 규칙). 비대칭 자리 글자는 그 대상 고정이 비교해야 한다.
          const symm = p.minfo.filter(function (x) {
            if (!x.m.contains(t)) return false;
            if (x.sl === null || x.sl.indexOf(NOW.slot(t.parentElement, x.m)) >= 0) return true;
            for (let e = t.parentElement; e && e !== x.m; e = e.parentElement) { if (NOW.isItem(e)) return true; }
            return false;
          }).map(function (x) { return x.m; });
          // (2) 형제 항목(조정자 결정 — 인정): 가린 조각이 대상을 담은 형제 쌍둥이와 다른 형제 m 안이고 그 대상이 자기 형제 고정을 가질 때,
          // m 안 대상 고정이 비교.
          for (const m of symm) {
            if (m.contains(p.el) || !sibOf(m, p)) continue;
            if (judges.some(function (j, k) { return j.ok && j.el && j.el !== p.el && m.contains(j.el) && jfail(k); })) { p.r.inner++; return; }
          }
          // (3) 별도 집계(조정자 결정 B): 그 대상 고정은 비교하지 않고 다른 대상 고정만 비교(행동 비교로 기준 대비 오클릭 0 을 따로 잰다).
          if (symm.length && judges.some(function (j, k) { return j.ok && j.el && j.el !== p.el && jfail(k); })) { p.r.other++; return; }
          const why = why156(t, p);
          if (why) p.r.allowed.push(why + ': ' + NOW.clean(v)); else p.r.miss.push(NOW.clean(v));
        });
      } finally {
        t.nodeValue = v;
        NOW.reset();
      }
    }
  });
  return {rows: out, tested: tested};
}"""


def census_js() -> str:
    """지금(주입 포함) 모듈의 GROUP_CHECK_JS 와 기준 원문 공통 함수로 census JS 를 만든다(호출 시점에 읽는다)."""
    from perception.repeated import GROUP_CHECK_JS

    return _CENSUS_JS.replace("__BASE__", _base_check_js()).replace("__NOW__", GROUP_CHECK_JS)


def install_baseline_census(monkeypatch) -> List[Dict[str, Any]]:
    """관찰마다: 기준(v1.16.0 원문 JS)이 고정한 (대상, 쌍둥이) 중 사이 반복 행이 있는 것(R7 내비게이션 예외 밖)에 census."""
    import perception.repeated as rp

    from test_ws62_r7 import _row_between_js

    original = rp.annotate_repeated
    log: List[Dict[str, Any]] = []
    base_js, row_js, cjs = _base_js(), _row_between_js(), census_js()

    async def census(page, engine, elements):
        block = await original(page, engine, elements)
        targets, handles = _targets(engine, elements)
        raw = await page.evaluate(base_js, {"targets": targets}) if targets else {"sets": []}
        pairs, nav = [], 0
        for st in raw.get("sets") or []:
            for g in st.get("groups") or []:
                for m in [dict(m, path=g["path"]) for m in g.get("members") or []] + list(g.get("folded") or []):
                    if m.get("pin", True) is False or not isinstance(m.get("path"), str):
                        continue
                    h = handles[int(m["i"])]
                    if await page.evaluate(row_js, {"path": m["path"], "css": h.css_path}) is not True:
                        nav += 1
                        continue
                    pairs.append({"k": len(pairs), "path": m["path"], "css": h.css_path, "pins": rp.pin_args(h), "name": h.name})
        jud = [[h.css_path, rp.pin_args(h)] for h in engine.handles.values() if h.css_path and rp.handle_pins(h)]
        got = await page.evaluate(cjs, {"pairs": [{k: v for k, v in p.items() if k != "name"} for p in pairs],
                                        "handles": jud}) if pairs else {"rows": [], "tested": 0}
        tested, got = got["tested"], got["rows"]
        by_k = {r["k"]: r for r in got}
        missing = [{"name": pairs[k]["name"], "path": pairs[k]["path"], "miss": r["miss"][:4]}
                   for k, r in sorted(by_k.items()) if r["miss"]]
        allowed = sorted({a for r in got for a in r["allowed"]})
        log.append({"pairs": len(pairs), "nav": nav, "other_pairs": sum(1 for r in got if r["other"]), "tested": tested, "need": sum(r["need"] for r in got),
                    "self": sum(r["self"] for r in got), "inner": sum(r["inner"] for r in got), "other": sum(r["other"] for r in got),
                    "allowed": sum(len(r["allowed"]) for r in got), "allowed_kinds": allowed[:6],
                    "bad": len(missing), "missing": missing[:6]})
        return block

    monkeypatch.setattr(rp, "annotate_repeated", census)
    return log


CAPTURE_DIR = Path(__file__).resolve().parents[2] / ".hermes" / "ws62-r4-evidence"
CAPTURES = ("hana_calendar_wrap.html", "hana_full_calendar_open.html")


def _family_pages(family: str) -> List[Tuple[str, Any]]:
    """census 페이지 묶음 → [(이름, html | 'hanatour_css' 같은 흐름 이름)]."""
    if family == "fixture":
        return [("hanatour_css", "hanatour_css"), ("hanatour_nocss", "hanatour_nocss")]
    if family == "captures":
        return [(n, (CAPTURE_DIR / n).read_text(encoding="utf-8")) for n in CAPTURES if (CAPTURE_DIR / n).is_file()]
    if family == "prior":
        names = [n for n in dict.fromkeys(CENSUS_PAGES + UNION_PAGES + ADV_CASES + NESTED_CASES) if not n.startswith("hanatour")]
        out: List[Tuple[str, Any]] = [(n, _page_for(n)) for n in names]
        out += [(f"rolling:{b}", adversarial_html(b, rolling=True)) for b in ("buried_name", "buried_name_table", "buried_name_deep")]
        out += [(f"livepanels:{k}", live_panels_html(k)) for k in ("live", "timer", "status", "marquee")]
        out += [(f"sib:{s}/{p}", sibling_html(s, p)) for s, p in SIBLING_CASES]
        out += [(f"new:{c[0]}", new_html(c[0])) for c in NEW_CASES]
        return out
    if family == "corpus":
        return [(c["id"], corpus_html(c["n"], c["levels"], c["tag"], c["table"], c["inside"], c["named"], c["pos"],
                                      c["partial"], c.get("wrap", ""))) for c in CORPUS]
    if family == "independent":
        return [(c["id"], independent_html(c)) for c in independent_cases()]
    if family == "symname":
        return [(f"{t}-n{n}", symname_html(t, n)) for t, n in SYMNAME_CASES]
    assert family == "symmetric"
    return [(f"{t}-live{int(lv)}-units{int(u)}", symmetric_html(t, lv, u)) for t, lv, u in SYM_CASES]


async def run_census(family: str, monkeypatch, only: Optional[List[str]] = None) -> Dict[str, Any]:
    """묶음의 모든 페이지에서 관찰마다 census. 페이지별 요약과 합계."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine
    from test_ws62_r4_hanatour import page_html

    log = install_baseline_census(monkeypatch)
    pages = [p for p in _family_pages(family) if only is None or p[0] in only]
    rows = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for name, html in pages:
                ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
                try:
                    page = await ctx.new_page()
                    await ctx.route("**/*", lambda route: route.abort())
                    flow = html if html in ("hanatour_css", "hanatour_nocss") else None
                    await page.set_content(page_html(flow == "hanatour_css") if flow else html)
                    engine = PerceptionEngine()
                    d = ActionDispatcher(DispatchContext(page=page, engine=engine))
                    start = len(log)
                    if flow:
                        await _hanatour_states(d, page, flow == "hanatour_css")
                    else:
                        await _observe(d, {"force_full_tree": True, "prune_top_n": 2000})
                    obs = log[start:]
                finally:
                    await ctx.close()
                rows.append({"page": name, "observes": len(obs), "pairs": sum(e["pairs"] for e in obs),
                             "nav": sum(e["nav"] for e in obs), "tested": sum(e["tested"] for e in obs),
                             "need": sum(e["need"] for e in obs), "self": sum(e["self"] for e in obs),
                             "inner": sum(e["inner"] for e in obs), "other": sum(e["other"] for e in obs),
                             "other_pairs": sum(e["other_pairs"] for e in obs), "allowed": sum(e["allowed"] for e in obs),
                             "allowed_kinds": sorted({a for e in obs for a in e["allowed_kinds"]})[:4],
                             "bad": sum(e["bad"] for e in obs), "missing": [m for e in obs for m in e["missing"]][:4]})
        finally:
            await browser.close()
    total = {k: sum(r[k] for r in rows) for k in ("observes", "pairs", "nav", "tested", "need", "self", "inner", "other", "other_pairs", "allowed",
                                                "bad")}
    return {"family": family, "pages": len(rows), **total, "rows": rows}


CENSUS_FAMILIES = ("fixture", "captures", "prior", "corpus", "independent", "symmetric", "symname")


@requires_chromium
@pytest.mark.parametrize("family", CENSUS_FAMILIES)
async def test_baseline_census_every_compared_fragment_still_compared(family, monkeypatch):
    """RED(R10): symmetric 의 live 상품명(span-live·span-live-units) — 기준 지문 글자인데 R10 의 어떤 고정도 비교하지 않는다.

    불변식(조정자 결정 B — 두 단계): 기준이 비교하던 조각은 전부 R11 의 어떤 고정이 비교한다 — 그 대상 고정(self), 형제 항목 쪽 대상
    고정(inner — 인정), 다른 대상 고정만(other — 별도 집계, 행동 비교는 symname·corpus·독립·대칭 시험). 어디서도 비교 안 되면 실패
    (#156 경로만 사유와 함께 허용).
    """
    if family == "captures" and not _family_pages("captures"):
        pytest.skip("보존 캡처(.hermes/ws62-r4-evidence) 없음 — 로컬 검증 자료")
    got = await run_census(family, monkeypatch)
    print(f"\nWS62R11-CENSUS {family} pages={got['pages']} observes={got['observes']} pairs={got['pairs']} nav={got['nav']}"
          f" tested={got['tested']}"
          f" need={got['need']} self={got['self']} inner={got['inner']} other={got['other']} other_pairs={got['other_pairs']} allowed156={got['allowed']} bad={got['bad']}")
    assert got["pages"] and got["pairs"] and got["need"], got  # 측정 조건(규칙 4)
    bad = [r for r in got["rows"] if r["bad"]]
    assert not bad, json.dumps(bad, ensure_ascii=False)[:2000]
    for r in got["rows"]:
        assert all(k.startswith("#156") for k in r["allowed_kinds"]), r


@requires_chromium
async def test_census_measures_inner_and_156_paths(monkeypatch):
    """측정 조건(규칙 4): census 가 가린 안쪽 쌍둥이 쪽 비교(inner)와 #156 허용 경로를 실제로 지난다(하나투어 달력 가격·btnsplit)."""
    hana = await run_census("fixture", monkeypatch)
    mp = pytest.MonkeyPatch()
    try:
        btn = await run_census("corpus", mp, only=["btnsplit-first-span-n3", "btnsplit-middle-strong-n4-T"])
    finally:
        mp.undo()
    assert hana["inner"] > 0 and hana["self"] > 0, hana
    assert btn["allowed"] > 0, btn


# ------------------------------------------------------------------ 주입(사보타주): census 불변식이 잡아야 한다

#: 주입 기준점(제품 JS 원문 줄). 모두 REPEATED_JS·GROUP_CHECK_JS·GROUP_FILTER_JS·STALENESS_CHECK_SCRIPT 에 한 번씩 있다.
INJECT = {
    # (a) 'mask_slots=null 이면 안쪽 전체 비교' 가정 되살림 — 대칭 자리 거르기가 없는 가림은 통째로 가린다.
    "null_all": ("      if (c && c.text.has(t)) return true;",
                 "      if (c && (c.slots === null || c.text.has(t))) return true;"),
    # (b) 동적 제외를 가림 산출에서 빼기 — 안쪽이 live 영역을 비교한다고 본다.
    "no_dynamic": ("    rpMasks = []; rpMaskCmp = null; rpRaw = false;  // 안쪽 고정과 같은 조건",
                   "    rpMasks = []; rpMaskCmp = null; rpRaw = true;  // 안쪽 고정과 같은 조건"),
    # (c) 대칭 자리 제외를 가림 산출에서 빼기 — 안쪽이 비대칭 자리 글자도 비교한다고 본다.
    "no_slots": ("      const sl = slots || null;  // 대칭 자리(R7)",
                 "      const sl = null;  // 대칭 자리(R7)"),
}


@contextlib.contextmanager
def injected(kind: str):
    import actions.verification as ver
    import perception.repeated as rp

    old, new = INJECT[kind]
    mp = pytest.MonkeyPatch()
    try:
        for mod, name in ((rp, "REPEATED_JS"), (rp, "GROUP_CHECK_JS"), (rp, "GROUP_FILTER_JS"), (ver, "STALENESS_CHECK_SCRIPT")):
            src = getattr(mod, name)
            assert src.count(old) == 1, (kind, name)
            mp.setattr(mod, name, src.replace(old, new))
        yield
    finally:
        mp.undo()


#: 주입별 census 페이지(묶음, 페이지 이름들) — 그 결함이 실제로 신원을 지우는 구조.
INJECT_PAGES = {
    "null_all": ("symmetric", ["span-live1-units0", "span-live1-units1"]),
    "no_dynamic": ("symmetric", ["span-live1-units0", "span-live1-units1"]),
    "no_slots": ("prior", ["sib:strong/buried", "sib:span/buried", "sib:dt_dd/buried", "new:three_buried"]),
}


@requires_chromium
@pytest.mark.parametrize("kind", list(INJECT))
async def test_census_detects_injected_mask_defect(kind, monkeypatch):
    """주입 (a) null=전체 가정 · (b) 가림 산출의 동적 제외 빼기 · (c) 가림 산출의 대칭 자리 빼기 — census 가 누락을 잡는다."""
    family, only = INJECT_PAGES[kind]
    clean = await run_census(family, monkeypatch, only=only)
    assert clean["bad"] == 0 and clean["need"] > 0, clean
    mp = pytest.MonkeyPatch()
    try:
        with injected(kind):
            got = await run_census(family, mp, only=only)
    finally:
        mp.undo()
    print(f"\nWS62R11-INJECT {kind} clean_bad={clean['bad']} injected_bad={got['bad']} need={got['need']}")
    assert got["bad"] >= 1, json.dumps(got, ensure_ascii=False)[:1500]
