"""WS-82(#157): 표제만 바뀐 같은 배치의 달 — 기준(v1.16.0) 실행 직전 확인에 '패널 표제 첫 조각이 바뀌면 거부'를 더하기만.

사용자 결정 D2=A(2026-11-05): R12 결정 B(실행 직전 확인 = v1.16.0 원문)는 그대로 두고, 관찰 때 달력 묶음(표시 형성 — 서로 다른
표제 세트) 구성원이었던 대상의 패널 표제 첫 조각을 실행 직전 **격리 world**·같은 프레임에서 다시 읽어 다르면 거부한다. 세 호출부
(승인 신선도 approval_target_check · 주 경로 _fresh_or_healed_handle · 결속 재확인 _reresolved_check)와 치유 채택 핸들.

census(리뷰 B6): test_ws62_r12 의 8가족 흐름을 그대로 돌리며, 관찰 뒤 변화마다 '클릭 경로 거부 ⊇ 기준 거부, 추가 거부는 첫
조각이 바뀐 경우뿐(그리고 첫 조각이 바뀐 경우는 모두 거부)'을 본다. 첫 조각 판정(오라클)은 디스패처 표가 아니라 census 가 관찰
때·판정 때 직접 읽은 값이다. 모든 시험은 로컬 Chromium set_content(요청 차단, 공개 사이트 접속 없음).
"""

from __future__ import annotations

import html as html_mod
import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r1 import _by_selector
import test_ws62_r4_hanatour as r4
import test_ws62_r12 as r12
import ws62_corpus as corpus

#: 오라클(시험 쪽 독립 구현 — 제품 rpHeadFirst 를 쓰지 않는다): 패널 경로들 → 지금 표제 첫 조각. 표제(headingOf)가 ARIA 이름이면
#: 그 글자, 아니면 표제를 이루는 첫 텍스트 노드(대화형·반복 항목·숨김 안 제외). 페이지 world.
ORACLE_JS = ("(paths) => {" + __import__("perception.repeated", fromlist=["COMMON_JS"]).COMMON_JS + """
  function oracleFirst(g) {
    const h = headingOf(g);
    if (!h) return null;
    if (h.source === 'aria') return h.text;
    let scope = g;
    if (h.source !== 'text') {
      const sel = h.source === 'caption' ? 'caption' : h.source === 'legend' ? 'legend' : 'h1,h2,h3,h4,h5,h6,[role=heading]';
      scope = Array.from(g.querySelectorAll(sel)).find(function (e) { return (e === g || !rpBlocked(e, g)) && rpText(e, g); }) || g;
    }
    const w = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    let t;
    while ((t = w.nextNode())) { if (t.nodeValue.trim() && !rpBlocked(t.parentElement, g)) return rpClean(t.nodeValue); }
    return h.text;
  }
  return paths.map(function (p) { let g = null; try { g = document.querySelector(p); } catch (x) { g = null; }
    return g ? oracleFirst(g) : null; }); }""")


class ClickCensus(r12.Census):
    """기준 census + 클릭 경로 판정(production verify_staleness + 디스패처 패널 표제 확인)."""

    def __init__(self) -> None:
        super().__init__()
        self.dispatcher: Any = None
        self.entries = 0
        self.click_judged = 0
        self.added: List[Dict[str, Any]] = []
        self.violations: List[Dict[str, Any]] = []
        self._heads: Optional[Tuple[Any, List[Tuple[Any, str, Any]]]] = None

    async def after_observe(self, page, engine, elements) -> None:
        await super().after_observe(page, engine, elements)
        table = dict(getattr(self.dispatcher, "__dict__", {}).get("_panel_heads") or {})
        rows = []
        for (eid, css, epoch), (path, _first, _pg) in table.items():
            h = engine.get_handle(eid)
            if h is not None and h.css_path == css and h.epoch == epoch:
                rows.append((h, path))
        before = await page.evaluate(ORACLE_JS, [p for _h, p in rows]) if rows else []
        self.entries += len(rows)
        self._heads = (page, [(h, p, b) for (h, p), b in zip(rows, before)])

    async def checkpoint(self, page) -> None:
        await super().checkpoint(page)
        if self._heads is None or self._heads[0] is not page or self._last is None:
            return
        import actions.dispatcher as disp

        arch = r12.archive()
        heads = {h.element_id: (p, b) for h, p, b in self._heads[1]}
        pairs = list(self._last[1])
        paired = {h.element_id for h, _a in pairs}
        pairs += [(h, h) for h, _p, _b in self._heads[1] if h.element_id not in paired]
        if not pairs:
            return
        prod = await r12._verdicts(page, disp.verify_staleness, [h for h, _a in pairs])
        base = await r12._verdicts(page, arch.ver.verify_staleness, [a for _h, a in pairs])
        paths = sorted({heads[h.element_id][0] for h, _a in pairs if h.element_id in heads})
        now_first = dict(zip(paths, await page.evaluate(ORACLE_JS, paths))) if paths else {}
        for (h, _a), p, b in zip(pairs, prod, base):
            self.click_judged += 1
            click_refused = p[1] == "group_changed" or (p[0] and await self.dispatcher._panel_heading_changed(h))
            base_refused = b[1] == "group_changed"
            first_changed = h.element_id in heads and now_first.get(heads[h.element_id][0]) != heads[h.element_id][1]
            row = {"page": self.label, "name": h.name, "base": b, "click": click_refused, "first_changed": first_changed}
            if base_refused and not click_refused:
                self.violations.append(dict(row, why="기준 거부를 클릭 경로가 통과"))
            elif click_refused and not base_refused:
                if not first_changed:
                    self.violations.append(dict(row, why="첫 조각이 같은데 추가 거부"))
                else:
                    self.added.append(row)
            elif b[0] and first_changed and not click_refused:
                self.violations.append(dict(row, why="첫 조각이 바뀌었는데 통과"))


def _csum(c: ClickCensus) -> str:
    return (r12._summary(c) + f" entries={c.entries} click_judged={c.click_judged} added={len(c.added)} "
            f"violations={len(c.violations)}")


@requires_chromium
@pytest.mark.parametrize("family", corpus.FAMILIES)
async def test_click_path_refusals_superset_of_baseline_only_first_fragment(family):
    """클릭 경로 거부 ⊇ 기준 거부, 추가 거부 = 패널 표제 첫 조각이 바뀐 경우(정확히). 기준 동일성(핸들 고정·verify_staleness)은
    test_ws62_r12 가 그대로 본다 — 이 시험은 그 위에 더한 확인만. 측정 조건(규칙 4): 시험 자료 흐름은 표가 차고 추가 거부가 실제로 난다."""
    r12.archive()
    got = await r12.run_family(family, census=ClickCensus())
    print(f"\nWS82-CENSUS {family} {_csum(got)}")
    assert got.observes and got.click_judged, _csum(got)
    if family == "captures":
        assert got.pages == 2, _csum(got)  # type: ignore[attr-defined]
    if family == "fixture":
        # 측정 조건: 표가 찬다. 시험 자료 흐름의 달 이동은 날짜 배치도 바뀌어 기준이 이미 거부한다(추가 거부 0 이 정상 —
        # 리뷰 F7 첫 조각 비교). LONGNOTE(같은 달 안내문)가 전체 글자 비교의 과잉 거부를 잡는다(사보타주 1).
        assert got.entries, _csum(got)
    assert not got.violations, json.dumps(got.violations[:5], ensure_ascii=False)[:2000]
    assert not got.pin_mismatch and not got.verdict_mismatch, _csum(got)


@requires_chromium
@pytest.mark.parametrize("css,longnote", [(True, False), (False, False), (True, True)], ids=["css", "nocss", "longnote"])
async def test_click_census_heading_only_change_adds_refusals(css, longnote):
    """같은 census 를 표제만 바뀐 같은 배치의 달(V1160_LIMIT 상태)에 — 추가 거부가 실제로 나고(규칙 4) 모두 첫 조각 변경이다."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    r12.archive()
    census = ClickCensus()
    with r12.census_installed(census):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await (await browser.new_context(viewport={"width": 1280, "height": 900})).new_page()
                await page.context.route("**/*", lambda route: route.abort())
                await page.set_content(r4.page_html(css))
                if longnote:
                    await page.evaluate("longnote()")
                d = ActionDispatcher(DispatchContext(page=page, engine=PerceptionEngine()))
                census.label = f"headonly css={css} longnote={longnote}"
                await r12._observe(d, r12.HANA)
                await page.evaluate(r4.HEADONLY)
                await census.checkpoint(page)
            finally:
                await browser.close()
    print(f"\nWS82-CENSUS-HEADONLY css={css} longnote={longnote} {_csum(census)}")
    assert census.entries and census.added, _csum(census)
    assert not census.violations, json.dumps(census.violations[:5], ensure_ascii=False)[:2000]


# ------------------------------------------------------------------ 진행성: 기준과 같은 호출 수


@requires_chromium
@pytest.mark.parametrize("css", [True, False], ids=["css", "nocss"])
async def test_progress_calls_equal_baseline(css, monkeypatch):
    """하나투어 시험 자료 다음달×3 → 이전달 → 12/19 → 1/6: 다음달 클릭당 호출 수·결과가 확인을 끈 기준과 같다(진행 손실 0)."""
    from actions.dispatcher import ActionDispatcher

    now = await r12._progress(css)

    async def never(self, handle):
        return False

    monkeypatch.setattr(ActionDispatcher, "_panel_heading_changed", never)
    base = await r12._progress(css)
    assert now == base, (now, base)


# ------------------------------------------------------------------ 호출부별


async def _heading_case(pw, css: bool = True, top_n: Optional[int] = None):
    if top_n is None:
        browser, page, engine, d = await r4._open(pw, r4.page_html(css))
    else:  # 치유 재관찰(엔진 기본 top-N)이 날짜를 후보로 보게 — 치유 채택 경로를 실제로 돌린다
        from actions import ActionDispatcher, DispatchContext
        from perception import PerceptionEngine

        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context(viewport={"width": 1280, "height": 900})).new_page()
        await page.set_content(r4.page_html(css))
        engine = PerceptionEngine(default_top_n=top_n)
        full_obs = engine.observe_page

        async def wide(*a, **k):  # 치유 재관찰은 prune_top_n 기본값(20)으로 부른다 — 시험에서만 넓힌다
            k.setdefault("prune_top_n", top_n)
            return await full_obs(*a, **k)

        engine.observe_page = wide
        d = ActionDispatcher(DispatchContext(page=page, engine=engine))
    await r4._observe(d)
    olds = [await _by_selector(engine, page, f"#cal > .inr:nth-of-type({k}) ul.day:nth-child({w}) li:nth-child({i}) a")
            for k, w, i in ((1, 4, 2), (2, 3, 5))]
    return browser, page, engine, d, olds


@requires_chromium
async def test_refusal_shape_kinds_text_and_no_heading_text():
    """거부 = 기존 GROUP_CHANGED 모양(TOCTOU·reobserve·changed.kinds=['text']·hint 틀) — 표제 원문은 응답 어디에도 없다."""
    from playwright.async_api import async_playwright

    from actions.group_changed import HINT_TEMPLATE, MARK_CHANGED

    async with async_playwright() as pw:
        browser, page, engine, d, olds = await _heading_case(pw)
        try:
            await page.evaluate(r4.HEADONLY)
            res = await d.dispatch(ActionType.CLICK, {"element_id": olds[0]})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert dep == ""
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH and res.reobserve_required
    assert res.data["group_changed"] == {"before": MARK_CHANGED, "after": MARK_CHANGED, "changed": {"kinds": ["text"]}}
    assert res.data["hint"] == HINT_TEMPLATE.format(kinds="text")
    assert res.error_message == r12.ERR_PREFIX + res.data["hint"]
    blob = json.dumps(res.model_dump(mode="json"), ensure_ascii=False)
    for raw in ("2026년", "2027년", "10월", "11월", "1월"):
        assert raw not in blob, (raw, blob)


@requires_chromium
async def test_observe_response_carries_no_heads_table():
    """표(패널 경로·표제 첫 조각)는 디스패처 안에만 — 관찰 응답에 내부 키가 없다."""
    from playwright.async_api import async_playwright

    from perception.repeated import HEADS_KEY

    async with async_playwright() as pw:
        browser, page, engine, d = await r4._open(pw, r4.page_html(True))
        try:
            r = await r4._observe(d)
        finally:
            await browser.close()
    assert d.__dict__["_panel_heads"], "측정 조건: 표가 차야 한다"
    assert HEADS_KEY not in json.dumps(r.model_dump(mode="json"), ensure_ascii=False)
    assert HEADS_KEY not in (r.data.get("repeated") or {})


@requires_chromium
async def test_approval_path_refuses_heading_change():
    """승인 신선도(approval_target_check, :1699): 기준은 fresh 지만 패널 표제 첫 조각이 바뀌었으면 fresh=False·group_changed."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d, olds = await _heading_case(pw)
        try:
            same = await d.approval_target_check({"element_id": olds[0]})
            await page.evaluate(r4.HEADONLY)
            moved = await d.approval_target_check({"element_id": olds[0]})
        finally:
            await browser.close()
    assert same["fresh"] is True, same
    assert moved == {"fresh": False, "detail": "묶음 표제 바뀜", "group_changed": True}, moved


@requires_chromium
async def test_reresolved_check_refuses_heading_change():
    """결속 재확인(_reresolved_check, :3618): 다시 해석한 노드가 같은 문서·기준 fresh 여도 표제 첫 조각이 바뀌면 거부."""
    from playwright.async_api import async_playwright

    from actions.dispatcher import _TargetRefused

    async with async_playwright() as pw:
        browser, page, engine, d, olds = await _heading_case(pw)
        try:
            h = engine.get_handle(olds[0])
            node = await page.query_selector(h.css_path)
            first_doc = await page.evaluate_handle("() => document")
            await d._reresolved_check(ActionType.CLICK, h, {}, node, first_doc)  # 같으면 통과(예외 없음)
            await page.evaluate(r4.HEADONLY)
            with pytest.raises(_TargetRefused) as info:
                await d._reresolved_check(ActionType.CLICK, h, {}, node, first_doc)
        finally:
            await browser.close()
    res = info.value.result if hasattr(info.value, "result") else info.value.args[0]
    assert res.error_code is ErrorCode.TOCTOU_MISMATCH and res.data["group_changed"]["changed"] == {"kinds": ["text"]}


@requires_chromium
async def test_healed_adopted_handle_is_checked():
    """치유 채택 핸들: 옛 '19' 노드가 교체(분리)되고 표제가 바뀜 → 치유가 같은 묶음의 새 '19' 를 골라도 패널 표제 확인을 잇는다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d, olds = await _heading_case(pw, top_n=2000)
        try:
            # 10월 31(11월엔 31 이 없다 — 치유 사다리가 이름으로 하나만 고를 수 있다)
            target = None
            for h in engine.handles.values():
                if h.css_path and h.name.split()[:1] == ["31"] and await page.evaluate(
                        "(p) => { const e = document.querySelector(p); return !!e && e.closest('#cal > .inr')"
                        " === document.querySelector('#cal > .inr'); }", h.css_path):
                    target = h
            assert target is not None
            css = target.css_path
            olds = [target.element_id]
            await page.evaluate("(css) => { const a = document.querySelector(css); const b = a.cloneNode(true);"
                                " b.onclick = a.onclick; const s = document.createElement('span'); s.appendChild(b);"
                                " a.replaceWith(s); }", css)  # css 경로가 바뀐다(li > span > a) — 표 키가 직접 맞지 않게
            await page.evaluate(r4.HEADONLY)
            healed = []
            orig = d._attempt_heal

            async def spy(handle, action=None):
                res = await orig(handle, action)
                healed.append(res.healed)
                return res

            d._attempt_heal = spy
            res = await d.dispatch(ActionType.CLICK, {"element_id": olds[0]})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert healed == [True], f"측정 조건: 치유가 실제로 채택해야 한다 {healed}"
    assert dep == "", f"치유로 다른 달의 날짜를 눌렀다: {dep}"
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH, (res.error_code, res.error_message)


@requires_chromium
async def test_main_world_forgery_does_not_bypass():
    """페이지 world 위조(querySelector 가 옛 표제의 미끼 패널을 돌려줌)는 격리 world 판독에 닿지 않는다 — 그래도 거부."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d, olds = await _heading_case(pw)
        try:
            h = engine.get_handle(olds[0])
            path = d._panel_entry(h)[0]
            await page.evaluate("""(path) => { const decoy = document.querySelector(path).cloneNode(true);
              decoy.style.display = 'none'; document.body.appendChild(decoy);
              const real = Document.prototype.querySelector;
              Document.prototype.querySelector = function (s) { return s === path ? decoy : real.call(this, s); }; }""", path)
            await page.evaluate(r4.HEADONLY.replace("document.querySelectorAll", "document.querySelectorAll"))
            res = await d.dispatch(ActionType.CLICK, {"element_id": olds[0]})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert dep == "" and res.error_code is ErrorCode.TOCTOU_MISMATCH, (dep, res.error_code)


@requires_chromium
async def test_table_keyed_by_epoch_no_false_refusal_after_navigation():
    """표 키 = (element_id, css_path, epoch): 이동(epoch 증가) 뒤 같은 id·css 로 다른 경로가 등록한 핸들은 옛 표로 거부하지 않는다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d, olds = await _heading_case(pw)
        try:
            h = engine.get_handle(olds[0])
            engine.bump_epoch("navigation")
            await page.evaluate(r4.HEADONLY)
            from dataclasses import replace

            fresh = replace(h, epoch=engine.epoch, group_path=None, group_heading=None)
            engine.register_external_handle(h.element_id, fresh)
            changed = await d._panel_heading_changed(fresh)
            stale = await d._panel_heading_changed(h)
        finally:
            await browser.close()
    assert changed is False
    assert stale is True  # 옛 epoch 핸들은 여전히 옛 표로 판정(측정 조건: 표제는 실제로 바뀌었다)


@requires_chromium
async def test_calendar_inside_frame_refused():
    """프레임 안 달력(switch_frame): 판독은 그 프레임의 격리 world — 표제만 바뀐 달의 옛 날짜 거부."""
    from playwright.async_api import async_playwright

    doc = "<!doctype html><html><body><iframe id=f style='width:1200px;height:900px' srcdoc=\"" + \
        html_mod.escape(r4.page_html(True), quote=True) + "\"></iframe></body></html>"
    async with async_playwright() as pw:
        browser, page, engine, d = await r4._open(pw, doc)
        try:
            await page.wait_for_function("() => { const f = document.getElementById('f');"
                                         " return f.contentDocument && f.contentDocument.querySelector('#cal .inr a'); }")
            sw = await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
            assert sw.success, sw.error_message
            await r4._observe(d)
            frame = d.ctx.page
            sel = "#cal > .inr:nth-of-type(1) ul.day:nth-child(4) li:nth-child(2) a"
            eid = None
            for h in engine.handles.values():
                if h.css_path and await frame.evaluate("([p, s]) => document.querySelector(p) === document.querySelector(s)",
                                                       [h.css_path, sel]):
                    eid = h.element_id
                    break
            assert eid and d._panel_entry(engine.get_handle(eid)), "측정 조건: 프레임 안 날짜가 표에 있어야 한다"
            await frame.evaluate(r4.HEADONLY)
            res = await d.dispatch(ActionType.CLICK, {"element_id": eid})
            dep = await frame.input_value("#dep")
        finally:
            await browser.close()
    assert dep == "" and res.error_code is ErrorCode.TOCTOU_MISMATCH, (dep, res.error_code, res.error_message)


#: 리뷰 적대 사례: 표제 안 타이머(같은 달인데 표제 글자 계속 바뀜)·패널 안 선택 요약(첫 선택 뒤 바뀜) — 기준과 결과가 같다.
ADV = {
    "timer_in_heading": "<div style='display:flex'>" + "".join(
        f"<div class=p><span class=h>{m}월 <b class=t>0</b></span><ul>" + "".join(
            f"<li><a href='#' onclick='this.dataset.c=1;return false'>{x}</a></li>" for x in range(1, 8)) + "</ul></div>"
        for m in (10, 11)) + "</div><script>setInterval(()=>document.querySelectorAll('.t').forEach(t=>t.textContent=+t.textContent+1),50)</script>",
    "selection_text_in_panel": "<div style='display:flex'>" + "".join(
        f"<div class=p><span>{m}월</span><span class=sel></span><ul>" + "".join(
            f"<li><a href='#' onclick=\"document.querySelectorAll('.sel').forEach(s=>s.textContent='선택 {m}/{x}');return false\">{x}</a></li>"
            for x in range(1, 8)) + "</ul></div>" for m in (10, 11)) + "</div>",
    "price_in_cell": "<div style='display:flex'>" + "".join(
        f"<div class=p><h3>{m}월</h3><ul>" + "".join(
            f"<li><a href='#' onclick='return false'>{x}<em class=pr>10만</em></a></li>" for x in range(1, 8)) + "</ul></div>"
        for m in (10, 11)) + "</div><script>setTimeout(()=>document.querySelectorAll('.pr').forEach(p=>p.textContent='12만'),100)</script>",
}


async def _adv_run(name: str) -> List[Any]:
    import asyncio

    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context(viewport={"width": 1280, "height": 900})).new_page()
            await page.set_content("<!doctype html><html><body>" + ADV[name] + "</body></html>")
            engine = PerceptionEngine()
            d = ActionDispatcher(DispatchContext(page=page, engine=engine))
            r = await d.dispatch(ActionType.OBSERVE_PAGE, {"force_full_tree": True, "prune_top_n": 200})
            groups = (r.data.get("repeated") or {}).get("groups", [])
            ids = [m["id"] for g in groups for m in g["members"] if m["name"].split()[0] in ("3", "5")]
            await asyncio.sleep(0.3)
            out = [(await d.dispatch(ActionType.CLICK, {"element_id": i})).success for i in ids[:2]]
        finally:
            await browser.close()
    return [len(groups), out]


@requires_chromium
@pytest.mark.parametrize("name", list(ADV))
async def test_adversarial_same_result_as_baseline(name, monkeypatch):
    """같은 달인데 패널 글자가 바뀌는 경우(타이머·선택 요약·칸 안 가격): 확인을 켠 결과 == 끈 결과(과잉 거부 0)."""
    from actions.dispatcher import ActionDispatcher

    now = await _adv_run(name)

    async def never(self, handle):
        return False

    monkeypatch.setattr(ActionDispatcher, "_panel_heading_changed", never)
    base = await _adv_run(name)
    assert now == base, (name, now, base)
    assert now[0] >= 2, f"측정 조건: 두 묶음이 형성돼야 한다 {now}"
