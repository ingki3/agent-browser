"""WS-62 R4: 실제 여행 예약 페이지 구조(tests/fixtures/ws62/hanatour_calendar.html)에서 실패한 반복 묶음.

실사용(v1.16.0, 2026-10-23): 바깥 레이아웃 쌍둥이(#wrap/#footer)가 날짜·다음달·항공권 검색을 먼저 흡수해
(1) 달력 두 묶음이 만들어지지 않고 레이아웃 묶음은 크기 상한으로 통째로 빠져 `groups: [], omitted: 2`,
(2) 같은 '다음달' id 2회째가 E_TOCTOU_MISMATCH, (3) 달 이동 뒤 '항공권 검색' 도 거부,
(4) 무 CSS 에선 레이아웃 묶음 지문(1000자)이 메뉴 글자로 차 월 표제가 지문 밖 — 옛 '19' 가 통과.
R12(사용자 결정 B): 고정·실행 직전 확인은 v1.16.0 원문으로 되돌렸다 — 이 파일은 묶음 표시(data.repeated) 시험과 v1.16.0 한계
(엄격 xfail)만 남긴다. 같은 다음달 id 연속·치유 승계 css 조건 시험은 지웠다(진행성은 test_ws62_r12 의 '거부 → observe → 새 id').
모든 시험은 로컬 Chromium set_content(공개 사이트 접속 없음).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r1 import _by_selector

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ws62" / "hanatour_calendar.html"
HEADONLY = ("() => { const hs = document.querySelectorAll('#cal > .inr .header span');"
            " hs[0].textContent = '2027년 1월'; hs[1].textContent = '2027년 2월'; }")
FULL = {"force_full_tree": True, "prune_top_n": 50}


def page_html(css: bool = True) -> str:
    html = FIXTURE.read_text(encoding="utf-8")
    assert '<style id="hide">' in html
    return html if css else html.replace('<style id="hide">', '<style id="hide" media="not all">')


async def _open(pw, html: str):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    browser = await pw.chromium.launch(headless=True)
    page = await (await browser.new_context(viewport={"width": 1280, "height": 900})).new_page()
    await page.set_content(html)
    engine = PerceptionEngine()
    return browser, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine))


async def _observe(d, args: Optional[Dict[str, Any]] = None):
    r = await d.dispatch(ActionType.OBSERVE_PAGE, dict(args or FULL))
    assert r.success, r.error_message
    return r


def _named(r, name: str) -> str:
    ids = [e["element_id"] for e in r.data["observation"]["elements"] if e["name"] == name]
    assert ids, f"관찰에 '{name}' 없음"
    return ids[0]


def _cal_groups(r) -> List[Dict[str, Any]]:
    rep = r.data.get("repeated") or {}
    return [g for g in rep.get("groups", []) if isinstance(g.get("group"), str) and g["group"].startswith("20")]


def _day(groups, month: str, day: str) -> str:
    for g in groups:
        if g["group"] == month:
            for m in g["members"]:
                if m["name"].split()[0] == day:
                    return m["id"]
    raise AssertionError(f"{month}/{day} 없음: {json.dumps(groups, ensure_ascii=False)[:600]}")


async def _months(page) -> List[str]:
    return await page.evaluate("() => [...document.querySelectorAll('#cal > .inr .header span')].map(s => s.textContent)")


# ------------------------------------------------------------------ F1: 달력 두 묶음(레이아웃 쌍둥이에 흡수되지 않음)


@requires_chromium
async def test_full_observe_has_two_month_groups():
    """RED(v1.16.0): groups=[] omitted=2 — 바깥 레이아웃 묶음이 날짜를 흡수했다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, page_html())
        try:
            r = await _observe(d)
        finally:
            await browser.close()
    groups = _cal_groups(r)
    assert [g["group"] for g in groups] == ["2026년 10월", "2026년 11월"], json.dumps(r.data.get("repeated"), ensure_ascii=False)[:800]
    assert groups[0]["x"] < groups[1]["x"]
    assert len(groups[0]["members"]) == 31 and len(groups[1]["members"]) == 30
    # 행 밖 대상(이전/다음달·항공권 검색·TOP)은 묶음 대상이 아니다(R1-3)
    names = {m["name"].split()[0] for g in r.data["repeated"]["groups"] for m in g["members"]}
    assert not names & {"이전달", "다음달", "항공권", "TOP"}
    _day(groups, "2026년 10월", "19")
    _day(groups, "2026년 11월", "19")


# ------------------------------------------------------------------ 안전: 표제만 바뀐 달의 옛 '19' 는 거부


#: R12(사용자 결정 B — 고정은 v1.16.0 원문): 표제만 바뀌고 날짜 배치·바깥 글자가 같은 달은 v1.16.0 이 구분하지 못한다(옛 날짜 id 가
#: 다른 달을 누름 — v1.16.0 한계 '바깥 정적 글자가 없고 항목 순서열도 같은 두 묶음'). 기준 동일성 불변식이 이 동작을 고정한다.
V1160_LIMIT = pytest.mark.xfail(strict=True, reason="v1.16.0 동일 한계(R12 결정 B) — 표제만 바뀐 같은 배치의 달")


@requires_chromium
@V1160_LIMIT
@pytest.mark.parametrize("css,longnote", [(True, False), (False, False), (True, True)], ids=["css", "nocss", "longnote"])
async def test_heading_only_change_refuses_old_day(css, longnote):
    """무 CSS(RED v1.16.0 — 지문 1000자가 메뉴 글자로 차 옛 '19' 통과)·LONGNOTE(달 패널 1000자+ 안내문)에서도 거부."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, page_html(css))
        try:
            if longnote:
                await page.evaluate("longnote()")
            await _observe(d)
            # 10월 19 = 넷째 주 둘째 칸, 11월 19 = 셋째 주 다섯째 칸
            olds = [await _by_selector(engine, page, f"#cal > .inr:nth-of-type({k}) ul.day:nth-child({w}) li:nth-child({i}) a")
                    for k, w, i in ((1, 4, 2), (2, 3, 5))]
            names = [engine.get_handle(e).name.split()[0] for e in olds]
            pinned = [bool(engine.get_handle(e).group_path) for e in olds]
            await page.evaluate(HEADONLY)
            results = [await d.dispatch(ActionType.CLICK, {"element_id": e}) for e in olds]
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert names == ["19", "19"] and pinned == [True, True], (names, pinned)
    assert dep == "", f"표제가 바뀐 달의 옛 날짜를 눌렀다: {dep}"
    for res in results:
        assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH, (res.error_code, res.error_message)
        assert res.reobserve_required is True


# ------------------------------------------------------------------ B2: 6주 달의 마지막 주 단독 '31' 도 고정


@requires_chromium
@V1160_LIMIT
async def test_lone_last_week_day_is_pinned_and_refused_after_move():
    """2027년 10월은 6주 — 마지막 주가 '31' 하나뿐(li 하나 = 반복 항목 아님)이고 11월(30일·5주)에는 31 이 없다(자기 낱말
    충돌 없음). 같은 행 틀(ul.day > li)이므로 고정되고, 표제가 바뀌면 옛 id 는 거부된다(B2)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, page_html())
        try:
            r = await _observe(d)
            for _ in range(12):  # R12: 같은 id 2회째는 기준대로 거부 → 다시 관찰해 새 id 로(봇 정책)
                res = await d.dispatch(ActionType.CLICK, {"element_id": _named(r, "다음달")})
                if not res.success:
                    r = await _observe(d)
                    assert (await d.dispatch(ActionType.CLICK, {"element_id": _named(r, "다음달")})).success
            assert await _months(page) == ["2027년 10월", "2027년 11월"]
            await _observe(d)
            lone = await _by_selector(engine, page, "#cal > .inr:nth-of-type(1) ul.day:nth-child(6) li a")
            h = engine.get_handle(lone)
            pinned = (h.name.split()[0], bool(h.group_path))
            # 같은 노드가 남는 달(같은 시작 요일 근사): 표제만 바꾼다 — 옛 '31' 은 다른 달이 됐다.
            await page.evaluate("() => { const hs = document.querySelectorAll('#cal > .inr .header span');"
                                " hs[0].textContent = '2028년 3월'; hs[1].textContent = '2028년 4월'; }")
            res = await d.dispatch(ActionType.CLICK, {"element_id": lone})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert pinned == ("31", True)
    assert dep == "" and res.error_code is ErrorCode.TOCTOU_MISMATCH, (dep, res.error_code, res.error_message)


# ------------------------------------------------------------------ N2(R5 의미 변경): 행 틀 거르기 뒤 빈 쌍둥이는 빼고 남은 쌍둥이를 낸다


def _n2_html(empties: int) -> str:
    def panel(head: str, body: str) -> str:
        return f"<div class=p><span>{head}</span>{body}</div>"

    days = "<ul>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in (1, 2, 3)) + "</ul>"

    def only_buttons(names: str) -> str:   # 패널마다 다른 이름 — 단추끼리 항목 쌍둥이 충돌을 만들지 않게
        return ("<ul><li>가</li><li>나</li><li>다</li></ul><div class=b>"
                + "".join(f"<button>{n}번</button>" for n in names) + "</div>")

    extra = "".join(panel(head, only_buttons(names)) for head, names in (("안내", "xyz"), ("공지", "uvw"))[:empties])
    return ("<!doctype html><html><body><div style='display:flex'>" + panel("1월", days) + panel("2월", days)
            + extra + "</div></body></html>")


@requires_chromium
@pytest.mark.parametrize("empties", [1, 2], ids=["one_empty", "two_empty"])
async def test_twin_left_empty_by_row_filter_is_dropped_rest_shown(empties):
    """셋(넷) 중 하나(둘)가 행(ul>li) 밖 단추만 가진 패널 — 거르기 뒤 멤버 0 인 쌍둥이만 빼고, 남은 두 달 패널은 여전히
    충돌하므로 묶음을 낸다(표시 [1월, 2월] + 고정, 패널마다 다른 path).

    의미 변경(R5, 조정자 결정 B): R4 까지는 빈 쌍둥이가 생기면 세트를 통째로 버리고 세 패널 항목 쌍둥이가 날짜를 받았다
    ([1월, 2월, 안내]). 항목 쌍둥이는 이제 대상의 가장 안쪽 반복 행이 그 항목일 때만 받으므로(R2 — 날짜의 행은 li)
    버리면 달 정보가 사라진다. 충돌 대상은 늘 행 대상이라 충돌한 쌍둥이는 비지 않는다 — 남은 쌍둥이가 2 미만이 되는
    경우는 구조상 생기지 않고, 생기면 표시 없이 고정만 한다(아래 단위 시험)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, _n2_html(empties))
        try:
            r = await _observe(d, {"prune_top_n": 300})
            ones = [await _by_selector(engine, page, f".p:nth-child({k}) li:first-child a") for k in (1, 2)]
            pins = [engine.get_handle(e).group_path for e in ones]
            btn_pin = engine.get_handle(_named(r, "x번")).group_path
        finally:
            await browser.close()
    assert [g["group"] for g in r.data["repeated"]["groups"]] == ["1월", "2월"]
    assert all(pins) and pins[0] != pins[1], pins
    # R6(형성/고정 분리): 단추 셋(button×3 — 반복 행)은 형성된 패널 쌍둥이 안 반복 행 대상이라 표시 없이 그 패널로 고정
    # (v1.16.0 과 같음 — R4·R5 는 충돌 행 틀 밖이라 고정을 뺐다).
    assert btn_pin, btn_pin


# ------------------------------------------------------------------ 크기(R1-4): 예산 초과 시 먼저 압축 → 그래도 넘는 세트만 빼기


@requires_chromium
@pytest.mark.parametrize("longnote", [False, True], ids=["plain", "longnote"])
async def test_over_budget_block_is_compacted_not_dropped(longnote):
    """RED(v1.16.0): 두 달(가격·배지 이름) + 머리 묶음이 3000자를 넘어 달력이 통째로 빠졌다. 지금은 전부 압축(첫 낱말·
    묶음 role·표제 첫 조각)한 뒤 탐욕으로 담는다 — 두 달은 ≤2,200자로 남는다."""
    from playwright.async_api import async_playwright

    from perception.repeated import COMPACT_HOW, REPEATED_MAX_CHARS

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, page_html())
        try:
            if longnote:
                await page.evaluate("longnote()")
            r = await _observe(d)
        finally:
            await browser.close()
    rep = r.data["repeated"]
    assert rep.get("compact") is True and COMPACT_HOW in rep["how"]
    cal = _cal_groups(r)
    # LONGNOTE: 표제는 첫 조각(달 이름)만 — 안내문 글자가 표제에 섞이지 않는다(N3)
    assert [g["group"] for g in cal] == ["2026년 10월", "2026년 11월"]
    assert all(g["role"] == "link" and all(set(m) <= {"id", "name", "states"} for m in g["members"]) for g in cal)
    names = [m["name"] for g in cal for m in g["members"]]
    assert names[:3] == ["1", "2", "3"] and all(n.isdigit() for n in names), names  # '27 127만'·'23 TODAY' → 숫자만
    cal_chars = len(json.dumps(cal, ensure_ascii=False, separators=(",", ":")))
    assert cal_chars <= 2200, cal_chars
    assert len(json.dumps(rep["groups"], ensure_ascii=False, separators=(",", ":"))) <= REPEATED_MAX_CHARS


def test_build_block_compacts_then_greedy_omits():
    """빌드 단계 단위 시험: 보통 크기로 다 들어가면 그대로, 넘으면 전부 압축하고 그래도 넘는 세트만 omitted."""
    from types import SimpleNamespace

    from perception import repeated as rp

    def handle(i: int, name: str, role: str = "link"):
        return SimpleNamespace(element_id=f"@e{i}", role=role, name=name, group_path=None, group_heading=None)

    def make_set(start: int, n: int, heads, name_fn, first=None):
        groups = []
        for k, head in enumerate(heads):
            members = [{"i": start + k * n + j, "states": [], "fp": f"fp{k}", "pin": True} for j in range(n)]
            groups.append({"path": f"#p{start}-{k}", "x": k, "y": 0, "heading": head, "source": "text",
                           "first": (first[k] if first else None), "members": members, "copies": 0, "folded": []})
        return {"kind": "panel", "groups": groups}

    handles = [handle(i, f"{i % 31 + 1}\n\n{100 + i}만 최저가 특가 상품 안내 문구") for i in range(124)]
    handles += [handle(124 + i, f"메뉴항목이름이아주긴낱말{i} 설명") for i in range(40)]
    cal = make_set(0, 31, ["2026년 10월 안내문 가나다", "2026년 11월 안내문 가나다"], None,
                   first=["2026년 10월", "2026년 11월"])
    big = make_set(62, 31, ["가", "나"], None)
    menu = make_set(124, 20, ["메뉴1", "메뉴2"], None)
    # 1) 다 들어가면 보통 형태(이름 전체·표제 전체·멤버 role)
    small = rp._build_block([make_set(124, 3, ["a", "b"], None)], handles)
    assert "compact" not in small and small["groups"][0]["members"][0]["role"] == "link"
    # 2) 넘으면 전부 압축 — 큰 세트부터 담고 안 들어가는 세트만 뺀다(탐욕)
    block = rp._build_block([menu, cal, big], handles)
    assert block["compact"] is True
    heads = [g["group"] for g in block["groups"]]
    assert heads[:2] == ["2026년 10월", "2026년 11월"], heads   # 표제 첫 조각(N3)
    assert block["groups"][0]["role"] == "link" and "role" not in block["groups"][0]["members"][0]
    assert block["groups"][0]["members"][0]["name"] == "1"
    assert all(len(m["name"]) <= rp.COMPACT_WORD for g in block["groups"] for m in g["members"])
    assert rp._block_chars(block["groups"]) <= rp.REPEATED_MAX_CHARS
    assert block.get("omitted", 0) >= 2 and len(block["groups"]) + block["omitted"] == 6
    # R12: 표시 모듈은 핸들을 고정하지 않는다(고정은 perception.repeated_v1160 원문만)
    assert not any(h.group_path or h.group_heading for h in handles)


# ------------------------------------------------------------------ R1-3: 충돌을 만든 행 틀 밖 대상은 묶음 대상(표시)이 아니다(R6: 반복 행 안이면 고정)


@requires_chromium
async def test_panel_targets_outside_colliding_row_kind_are_not_pinned():
    """달 패널 안이지만 날짜 행(ul>li) 이 아닌 다른 행 틀(div>span)의 안내 링크 — 이름이 패널마다 달라 충돌을 만들지
    않았으므로 묶음 대상(표시)은 아니다. 날짜는 그대로 고정.

    R6 의미 변경(형성/고정 분리): 안내 링크도 형성된 달 패널 안 반복 행(span×3) 대상이라 표시 없이 그 패널로 고정한다
    (v1.16.0 과 같은 fail-closed — 표제가 바뀐 달의 안내 링크는 거부, 재관찰 뒤 누른다). 행 밖 대상(이전/다음달)만 고정 없음."""
    from playwright.async_api import async_playwright

    def panel(month: str, notes) -> str:
        days = "<ul class=day>" + "".join(f"<li><a href='#' onclick=\"return false\">{n}</a></li>" for n in range(1, 6)) + "</ul>"
        legend = "<div class=legend>" + "".join(
            f"<span><a href='#' onclick=\"document.getElementById('out').textContent='{n}';return false\">{n} 안내</a></span>"
            for n in notes) + "</div>"
        return f"<div class=inr><div class=header><span>{month}</span></div>{days}{legend}</div>"

    html = ("<!doctype html><html><body><p id=out>대기</p><div id=cal style='display:flex'>"
            + panel("2026년 12월", ("휴무", "마감", "특가")) + panel("2027년 1월", ("운항", "좌석", "요금")) + "</div></body></html>")
    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, html)
        try:
            r = await _observe(d, {"prune_top_n": 300})
            note = _named(r, "휴무 안내")
            day = await _by_selector(engine, page, "#cal .inr:first-child li:nth-child(2) a")
            await page.evaluate("document.querySelector('#cal .inr .header span').textContent = '2027년 2월'")
            res = await d.dispatch(ActionType.CLICK, {"element_id": note})
            out = await page.text_content("#out")
            day_res = await d.dispatch(ActionType.CLICK, {"element_id": day})
        finally:
            await browser.close()
    names = {m["name"] for g in r.data["repeated"]["groups"] for m in g["members"]}
    assert "2" in names and not any("안내" in n for n in names), names
    assert res.error_code is ErrorCode.TOCTOU_MISMATCH and out == "대기", (res.error_code, res.error_message, out)
    assert day_res.error_code is ErrorCode.TOCTOU_MISMATCH


# ------------------------------------------------------------------ R1-1: 깊은(작은) 패널 쌍둥이 먼저


@requires_chromium
async def test_deeper_panel_twins_finish_first_regardless_of_target_order():
    """가는 편·오는 편 두 달력 위젯이 나란히 있고(바깥 쌍둥이), 각 위젯 안에 두 달 패널(안쪽 쌍둥이)이 있다. 대상 순서상
    위젯 단추(바깥 쌍둥이를 만드는 대상)가 먼저 와도 날짜는 달 패널 묶음이 받는다 — 바깥 위젯 묶음은 단추만."""
    from playwright.async_api import async_playwright

    from perception.repeated import REPEATED_JS

    def widget(months) -> str:
        btns = "<ul class=btns>" + "".join(f"<li><a href='#'>{b}</a></li>" for b in ("초기화", "닫기", "적용")) + "</ul>"
        cal = "".join("<div class=inr><span>" + m + "</span><ul class=day>"
                      + "".join(f"<li><a href='#'>{n}</a></li>" for n in range(1, 4)) + "</ul></div>" for m in months)
        return f"<div class=w>{btns}<div class=cal style='display:flex'>{cal}</div></div>"

    html = ("<!doctype html><html><body><div style='display:flex'>" + widget(("2026년 12월", "2027년 1월"))
            + widget(("2027년 2월", "2027년 3월")) + "</div></body></html>")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            sels = [f".w:nth-child({w}) .btns li:nth-child({k}) a" for w in (1, 2) for k in (1, 2, 3)]
            sels += [f".w:nth-child({w}) .inr:nth-child({p}) li:nth-child({k}) a" for w in (1, 2) for p in (1, 2) for k in (1, 2, 3)]
            names = await page.evaluate("(ss) => ss.map(s => document.querySelector(s).textContent)", sels)
            targets = [{"i": i, "css": c, "name": n, "role": "link"} for i, (c, n) in enumerate(zip(sels, names))]
            raw = await page.evaluate(REPEATED_JS, {"targets": targets})
        finally:
            await browser.close()
    heads = sorted(tuple(g["heading"] for g in st["groups"]) for st in raw["sets"])
    assert ("2026년 12월", "2027년 1월") in heads and ("2027년 2월", "2027년 3월") in heads, heads
    months = {("2026년 12월", "2027년 1월"), ("2027년 2월", "2027년 3월")}
    outer = [st for st in raw["sets"] if tuple(g["heading"] for g in st["groups"]) not in months]
    assert outer and all(m["i"] < 6 for st in outer for g in st["groups"] for m in g["members"]), outer
