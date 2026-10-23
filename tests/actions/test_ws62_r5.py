"""WS-62 R5: R4 검증 FAIL 2건.

R1 혼합 깊이 — 월 패널 안 날짜 목록 일부(19~31일)가 패널에서 6단계를 넘으면 R4 대상 국소성이 그 날짜를 묶음에서
뺐고 고정까지 사라져, 표제만 바뀐 달의 옛 '19' 가 다른 달(2027년 1월/19)에서 눌렸다. 국소성은 묶음 형성(먼저 차지)
에만 쓰고, 형성된 패널이 담은 충돌 행과 같은 틀의 행 대상은 깊이와 무관하게 그 패널로 고정한다.

R2 오는 날 미선택 — 바깥 오는날 표시 '오는 날' 이 팝업 안 입력칸 이름과 같아 팝업 래퍼 항목 쌍둥이(div.row ↔ 달력
팝업)가 형성되고 이전/다음달이 항목 단위로 고정돼 2회째 다음달·이전달이 거부됐다. 항목 쌍둥이는 대상의 가장 안쪽 반복
행이 그 항목 자신일 때만 대상으로 받는다.
모든 시험은 로컬 Chromium set_content(공개 사이트 접속 없음).
"""

from __future__ import annotations

import json
from typing import List

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r1 import _by_selector
from test_ws62_r4_hanatour import FULL, _cal_groups, _day, _months, _named, _observe, _open, nav_pins_ok, page_html


# ------------------------------------------------------------------ R1: 혼합 깊이(얕은 1~18일 + section 7개 아래 19~31일)


def _dates(kind: str, first: int, last: int) -> str:
    if kind == "ul":
        return "<ul>" + "".join(f"<li><a href='#'>{d}</a></li>" for d in range(first, last + 1)) + "</ul>"
    weeks = [range(w, min(w + 7, last + 1)) for w in range(first, last + 1, 7)]
    if kind == "table":
        return ("<table><tbody>" + "".join("<tr>" + "".join(f"<td><a href='#'>{d}</a></td>" for d in w) + "</tr>"
                                            for w in weeks) + "</tbody></table>")
    assert kind == "div"
    return ("<div class=weeks>" + "".join("<div class=week>" + "".join(f"<div class=cell><a href='#'>{d}</a></div>" for d in w)
                                          + "</div>" for w in weeks) + "</div>")


def mixed_depth_html(kind: str) -> str:
    months = []
    for month, n in ((10, 31), (11, 30)):
        deep = "<section>" * 7 + _dates(kind, 19, n) + "</section>" * 7
        months.append(f"<div class=month><h2>2026년 {month}월</h2>{_dates(kind, 1, 18)}{deep}</div>")
    script = ("<script>document.querySelectorAll('.month a').forEach(function (a) { a.onclick = function () {"
              " document.getElementById('out').textContent = this.closest('.month').querySelector('h2').textContent"
              " + '/' + this.textContent; return false; }; });</script>")
    return "<!doctype html><html><body><p id=out>none</p><div id=cal>" + "".join(months) + "</div>" + script + "</body></html>"


async def _date_in(engine, page, month_index: int, day: str) -> str:
    for h in engine.handles.values():
        if h.name == day and h.css_path and await page.evaluate(
                "([p, k]) => { const e = document.querySelector(p); return !!e && e.closest('.month') ==="
                " document.querySelectorAll('.month')[k]; }", [h.css_path, month_index]):
            return h.element_id
    raise AssertionError(f"{month_index}/{day} 핸들 없음")


@requires_chromium
@pytest.mark.parametrize("kind", ["ul", "table", "div"])
async def test_mixed_depth_dates_pinned_and_old_day_refused(kind):
    """RED(R4 dfef218): month_groups 18/18, 깊은 '19' 고정 없음 → 표제만 바꾼 뒤 옛 '19' 가 2027년 1월/19 를 눌렀다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, mixed_depth_html(kind))
        try:
            r = await _observe(d)
            groups = r.data["repeated"]["groups"]
            dates = [h for h in engine.handles.values() if h.name.isdigit()]
            unpinned = sorted({h.name for h in dates if not h.group_path}, key=int)
            old19 = await _date_in(engine, page, 0, "19")
            nov19 = await _date_in(engine, page, 1, "19")
            await page.evaluate("document.querySelector('.month h2').textContent = '2027년 1월'")
            res = await d.dispatch(ActionType.CLICK, {"element_id": old19})
            out = await page.text_content("#out")
            same = await d.dispatch(ActionType.CLICK, {"element_id": nov19})
            out2 = await page.text_content("#out")
        finally:
            await browser.close()
    assert [(g["group"], len(g["members"])) for g in groups] == [("2026년 10월", 31), ("2026년 11월", 30)], \
        json.dumps(groups, ensure_ascii=False)[:400]
    assert len(dates) == 61 and not unpinned, unpinned
    assert out == "none", f"다른 달의 옛 날짜를 눌렀다: {out}"
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH and res.reobserve_required
    # 바뀌지 않은 달(11월)의 같은 '19' 는 그대로 누를 수 있다(과잉 거부 없음)
    assert same.success and out2 == "2026년 11월/19", (same.error_code, same.error_message, out2)


# ------------------------------------------------------------------ R2: 오는 날 미선택 상태의 이전/다음달


@requires_chromium
@pytest.mark.parametrize("css", [True, False], ids=["css", "nocss"])
async def test_pending_return_month_nav_not_refused_then_pick_jan6(css):
    """RED(R4 dfef218): 바깥 '오는 날' ↔ 팝업 입력칸 '오는 날' 충돌로 팝업 래퍼 항목 쌍둥이가 다음달·이전달을 고정 —
    2회째 다음달·이전달 E_TOCTOU_MISMATCH. 지금은 다음달 연속 3회·이전달 거부 0, 그 뒤 2027년 1월 6일 선택."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, page_html(css))
        try:
            await page.evaluate("pendingReturn()")
            r = await _observe(d)
            nxt, prv = _named(r, "다음달"), _named(r, "이전달")
            # 팝업 래퍼 항목 쌍둥이(#tabCon01 > div 의 div.row ↔ 달력 팝업)에 고정되지 않는다(R5 R2). 기준 바깥 레이아웃
            # 고정(R7 합집합)은 달 패널을 가린 지문이라 달 이동에 흔들리지 않는다.
            pins = {n: await nav_pins_ok(page, engine.get_handle(e), "#tabCon01 > div") for n, e in (("다음달", nxt), ("이전달", prv))}
            moves = [await d.dispatch(ActionType.CLICK, {"element_id": nxt}) for _ in range(3)]
            back = await d.dispatch(ActionType.CLICK, {"element_id": prv})
            months = await _months(page)
            r2 = await _observe(d)
            groups = _cal_groups(r2)
            day_res = await d.dispatch(ActionType.CLICK, {"element_id": _day(groups, "2027년 1월", "6")}) if css else None
            values = [await page.input_value("#dep"), await page.input_value("#ret")]
        finally:
            await browser.close()
    assert pins == {"다음달": True, "이전달": True}, pins
    outcomes: List = [(m.success, m.error_code) for m in moves + [back]]
    assert outcomes == [(True, None)] * 4, [(m.error_code, m.error_message) for m in moves + [back]]
    assert months == ["2026년 12월", "2027년 1월"]
    if css:
        assert day_res.success, (day_res.error_code, day_res.error_message)
        assert values == ["2026.10.24", "2027.01.06"], values


@requires_chromium
async def test_pending_return_tabs_and_inputs_not_pinned_to_popup_wrapper():
    """팝업 래퍼 항목 쌍둥이(div.row ↔ 달력 팝업)에 입력칸·이전/다음·왕복/편도 탭이 고정되지 않는다 — 가장 안쪽 반복 행이
    그 래퍼 항목 자신이 아닌 대상(바깥 '오는 날' = div.bowl)이 빠지면 쌍둥이는 형성되지 않는다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, page_html())
        try:
            await page.evaluate("pendingReturn()")
            await _observe(d)
            sels = ["#cal > a.next", "#cal > a.prev", "#dep", "#ret", "#retset", ".tabs li:nth-child(1) a",
                    ".tabs li:nth-child(2) a"]
            ids = [await _by_selector(engine, page, s) for s in sels]
            pins = {s: await nav_pins_ok(page, engine.get_handle(e), "#tabCon01 > div") for s, e in zip(sels, ids)}
            nxt = ids[0]
            first = await d.dispatch(ActionType.CLICK, {"element_id": nxt})
            again = await d.dispatch(ActionType.CLICK, {"element_id": nxt})
            back = await d.dispatch(ActionType.CLICK, {"element_id": ids[1]})
        finally:
            await browser.close()
    assert all(pins.values()), pins
    assert [r.success for r in (first, again, back)] == [True] * 3, [(r.error_code, r.error_message) for r in (first, again, back)]


@requires_chromium
async def test_dep_pick_clears_return_then_next_month_not_refused():
    """가는 날을 고르면(오는 날 표시가 '오는 날' 로 돌아감) 재관찰 뒤 다음달을 같은 id 로 두 번 눌러도 거부 0."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, page_html())
        try:
            r = await _observe(d)
            assert (await d.dispatch(ActionType.CLICK, {"element_id": _named(r, "다음달")})).success
            r = await _observe(d)
            assert (await d.dispatch(ActionType.CLICK, {"element_id": _day(_cal_groups(r), "2026년 12월", "19")})).success
            ret_label = await page.text_content("#retset")
            r = await _observe(d)
            nxt = _named(r, "다음달")
            moves = [await d.dispatch(ActionType.CLICK, {"element_id": nxt}) for _ in range(2)]
        finally:
            await browser.close()
    assert ret_label == "오는 날"
    assert [(m.success, m.error_code) for m in moves] == [(True, None)] * 2, [(m.error_code, m.error_message) for m in moves]
