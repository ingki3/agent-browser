"""WS-62 R13(검증 NB): 기준 동일성 — 동적 갱신 8조건(롤링·다른 날 가격·hover·timer, css/nocss).

R12 에서 삭제된 R7/R9 동적 행동 시험의 변화 상수(e9bf379 `test_ws62_r7.py` ADD_ROLLING·DYN_CHANGE·DYN_TARGET,
`test_ws62_r9.py` TIMER_ALL·DYN_CONDITIONS — 바이트 그대로 옮김)로 검증 evidence/dynamic-*.json 방식을 영구 시험으로 둔다:
조건마다 관찰 → 동적 변화 → 같은 대상(10/19) 클릭을 5회. 매 관찰의 핸들 고정과 변화 뒤 실행 직전 판정이 6e449e2 archive 원문과
같아야 하고(R12 Census), 기준(검증 실측 head==base)이 거부한 회차가 그대로 거부되며 거부 회차는 실제 클릭이 0이다.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r12 import Census, _summary, archive, census_installed
import ws62_corpus as corpus

ADD_ROLLING = ("() => { const e = document.createElement('small'); e.className = 'dynamic'; e.textContent = '12000원'; "
               "document.querySelector('#cal > .inr .header').append(e); }")
DYN_CHANGE = {
    "rolling": "(n) => { document.querySelector('#cal > .inr .header .dynamic').textContent = '특가 소식 ' + n; }",
    "other_day_price": "(n) => { document.querySelector('#cal > .inr p.price').textContent = (120 + n) + '만'; }",
    "all": ("(n) => { document.querySelector('#cal > .inr .header .dynamic').textContent = '특가 소식 ' + n; "
            "document.querySelector('#cal > .inr p.price').textContent = (120 + n) + '만'; "
            "const a = document.querySelector('#cal > .inr a[data-date=\"2026.10.19\"]'); let t = a.parentElement.querySelector('.tip'); "
            "if (!t) { t = document.createElement('em'); t.className = 'tip'; a.parentElement.append(t); } t.textContent = '호버 ' + n; }"),
}
DYN_TARGET = "#cal > .inr a[data-date=\"2026.10.19\"]"
TIMER_ALL = ("(n) => { let t = document.querySelector('#cal > .inr .header .ticker'); if (!t) { t = document.createElement('em'); "
             "t.className = 'ticker'; t.setAttribute('role', 'timer'); document.querySelector('#cal > .inr .header').append(t); } "
             "t.textContent = '마감 ' + n + '분 전'; (" + DYN_CHANGE["all"] + ")(n); }")
DYN_CONDITIONS = [(True, "rolling"), (False, "other_day_price"), (False, "rolling"), (True, "other_day_price"),
                  (True, "all"), (False, "all"), (True, "timer_all"), (False, "timer_all")]
#: 검증 실측(dynamic-head.json == dynamic-base.json): 회차별 기준 거부(TOCTOU) 여부.
BASE_REFUSED = {
    "True-rolling": [False] * 5, "False-rolling": [False] * 5,
    "False-other_day_price": [False, True, True, True, True], "True-other_day_price": [True] * 5,
    "True-all": [True] * 5, "False-all": [False, True, True, True, True],
    "True-timer_all": [True] * 5, "False-timer_all": [False, True, True, True, True],
}
SINK_JS = ("(s)=>{window.targetNode=document.querySelector(s);window.actual=[];document.addEventListener('click',e=>{"
           "if(e.composedPath().includes(window.targetNode))actual.push(window.targetNode.textContent)},true)}")
FIND_JS = ("(a)=>a.css.map(s=>{try{return document.querySelector(s)===document.querySelector(a.target)}catch(x){return false}})")


async def _trial(browser, census: Census, css: bool, change: str) -> List[Dict[str, Any]]:
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    await ctx.route("**/*", lambda route: route.abort())
    rows: List[Dict[str, Any]] = []
    try:
        page = await ctx.new_page()
        await page.set_content(corpus.page_html(css))
        await page.evaluate(ADD_ROLLING)
        await page.evaluate(SINK_JS, DYN_TARGET)
        engine = PerceptionEngine()
        d = ActionDispatcher(DispatchContext(page=page, engine=engine))
        for n in range(1, 6):
            obs = await d.dispatch(ActionType.OBSERVE_PAGE, {"force_full_tree": True, "prune_top_n": 300})
            assert obs.success, obs.error_message
            hs = list(engine.handles.values())
            hit = await page.evaluate(FIND_JS, {"css": [h.css_path for h in hs], "target": DYN_TARGET})
            h = next(h for h, v in zip(hs, hit) if v)
            await page.evaluate(TIMER_ALL if change == "timer_all" else DYN_CHANGE[change], n)
            await census.checkpoint(page)  # 변화 직후 실행 직전 판정 R12 == archive
            sink_before = len(await page.evaluate("window.actual"))
            res = await d.dispatch(ActionType.CLICK, {"element_id": h.element_id})
            sink_after = len(await page.evaluate("window.actual"))
            rows.append({"refused": res.error_code is ErrorCode.TOCTOU_MISMATCH, "success": res.success,
                         "clicks": sink_after - sink_before, "pinned": bool(h.group_path)})
        census._last = None
    finally:
        await ctx.close()
    return rows


@requires_chromium
async def test_dynamic_updates_baseline_identity():
    from playwright.async_api import async_playwright

    archive()
    census = Census()
    got: Dict[str, List[Dict[str, Any]]] = {}
    with census_installed(census):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                for css, change in DYN_CONDITIONS:
                    census.label = f"{css}-{change}"
                    got[census.label] = await _trial(browser, census, css, change)
            finally:
                await browser.close()
    census.pages = len(DYN_CONDITIONS)  # type: ignore[attr-defined]
    print(f"\nWS62R13-DYNAMIC {_summary(census)}")
    assert len(got) == 8 and census.pinned and census.refused and census.passed, _summary(census)
    assert not census.pin_mismatch, json.dumps(census.pin_mismatch[:5], ensure_ascii=False)[:2000]
    assert not census.verdict_mismatch, json.dumps(census.verdict_mismatch[:5], ensure_ascii=False)[:2000]
    assert {k: [r["refused"] for r in v] for k, v in got.items()} == BASE_REFUSED, got
    for k, v in got.items():
        assert all(r["pinned"] for r in v), k  # 측정 조건: 대상이 묶음에 고정돼 있다
        assert all(r["clicks"] == 0 for r in v if r["refused"]), (k, v)  # 거부는 누르기 전
        assert all(r["clicks"] >= 1 for r in v if r["success"]), (k, v)  # 통과는 바로 그 대상을 눌렀다
