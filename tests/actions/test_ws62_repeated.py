"""WS-62: 반복 묶음 대상 구분(data.repeated) + 실행 직전 묶음 고정 + 치유 묶음 비교.

실사례(하나투어 달력, 2026-10-22): 두 달 패널의 같은 날짜(`link "19"` ×2)에 어느 달인지 단서가 없어 다른 달을 눌렀다.
모든 시험은 실제 Chromium set_content(로컬) — 증거 구조를 본뜬 페이지(tests/actions/ws62_pages.py).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from ws62_pages import CAPTION_HTML, GRID_HTML, HANA_HTML, HANA_MINI_HTML, PRODUCTS_HTML, twin_panels

TOP_N = 300


async def _open(pw, html: str):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    browser = await pw.chromium.launch(headless=True)
    page = await (await browser.new_context(viewport={"width": 1280, "height": 900})).new_page()
    await page.set_content(html)
    engine = PerceptionEngine()
    return browser, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine))


async def _observe(d, top_n: int = TOP_N):
    r = await d.dispatch(ActionType.OBSERVE_PAGE, {"prune_top_n": top_n})
    assert r.success, r.error_message
    return r


async def _observe_html(html: str, top_n: int = TOP_N) -> Any:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, _page, _engine, d = await _open(pw, html)
        try:
            return await _observe(d, top_n)
        finally:
            await browser.close()


def _groups(r) -> List[Dict[str, Any]]:
    rep = r.data.get("repeated")
    assert rep is not None, sorted(r.data)
    return rep["groups"]


def _member(groups, group_text: str, name: str) -> Dict[str, Any]:
    for g in groups:
        if g["group"] == group_text:
            for m in g["members"]:
                if m["name"] == name:
                    return m
    raise AssertionError(f"{group_text}/{name} 없음: {json.dumps(groups, ensure_ascii=False)[:600]}")


# ------------------------------------------------------------------ 블록 모양(증거 구조)


@requires_chromium
async def test_hana_two_month_panels_are_told_apart():
    """두 달 패널 → 묶음 2개, 표제는 span 달 이름(text), 이전/다음달·요일·가격·이름 없는 링크·캐러셀·푸터 제외."""
    r = await _observe_html(HANA_HTML)
    rep = r.data["repeated"]
    groups = rep["groups"]
    assert [g["group"] for g in groups] == ["2026년 12월", "2027년 1월"], json.dumps(groups, ensure_ascii=False)[:800]
    assert [g["group_source"] for g in groups] == ["text", "text"]
    assert groups[0]["x"] < groups[1]["x"]
    assert "서버 판정 아님" in rep["how"] or "서버 판정이 아닙니다" in rep["how"]
    assert "입력칸 값" in rep["how"]
    left = {m["name"] for m in groups[0]["members"]}
    right = {m["name"] for m in groups[1]["members"]}
    assert "19" in left and "이전달" in left and "다음달" not in left
    assert "19 148만" in right and "다음달" in right   # 가격 섞인 이름(줄바꿈 접기)
    # 요일 머리글·이름 없는 링크·캐러셀 카드·푸터 링크는 묶음에 없다.
    names = left | right
    assert not names & set("일월화수목금토")
    assert "" not in names
    assert not any("특가" in n or n in ("회사소개", "이용약관") for n in names)
    assert len(groups) == 2
    # ARIA 없는 위젯 — 상태는 class 추측하지 않는다(states 없음).
    assert not any("states" in m for g in groups for m in g["members"])
    # 블록 상한·id 는 관찰 id
    assert len(json.dumps(groups, ensure_ascii=False, separators=(",", ":"))) <= 3000
    ids = {e["element_id"] for e in r.data["observation"]["elements"]}
    assert all(m["id"] in ids for g in groups for m in g["members"])


@requires_chromium
async def test_caption_tables():
    r = await _observe_html(CAPTION_HTML)
    groups = _groups(r)
    assert [(g["group"], g["group_source"]) for g in groups] == [("2026년 12월", "caption"), ("2027년 1월", "caption")]
    assert {m["name"] for m in groups[0]["members"]} >= {"1", "19", "28"}


@requires_chromium
async def test_aria_grid_heading_and_states():
    r = await _observe_html(GRID_HTML)
    groups = _groups(r)
    assert [(g["group"], g["group_source"]) for g in groups] == [("2026년 12월", "aria"), ("2027년 1월", "aria")]
    assert _member(groups, "2026년 12월", "19")["states"] == ["selected"]
    assert "states" not in _member(groups, "2027년 1월", "19")
    assert _member(groups, "2027년 1월", "3")["states"] == ["disabled"]


@requires_chromium
async def test_product_rows_same_button_name():
    """같은 이름 '담기' 버튼이 든 상품 행 → 행마다 묶음, 표제는 행의 첫 비대화형 글자(상품명)."""
    r = await _observe_html(PRODUCTS_HTML)
    groups = _groups(r)
    assert [g["group"] for g in groups] == ["상품 A", "상품 B", "상품 C", "상품 D"]
    assert all([m["name"] for m in g["members"]] == ["담기"] for g in groups)
    assert len({g["members"][0]["id"] for g in groups}) == 4


@requires_chromium
async def test_weekday_header_before_label_is_not_heading():
    """요일 머리글(반복 목록 항목 글자)이 달 이름보다 앞에 있어도 표제는 달 이름이다."""
    week = "<ul class=week>" + "".join(f"<li>{d}</li>" for d in "일월화수목금토") + "</ul>"
    html = ("<!doctype html><html><body><div class=w style='display:flex'>" + "".join(
        f"<div class=p>{week}<b>{h}</b><ul class=day>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in "1234")
        + "</ul></div>" for h in ("2026년 12월", "2027년 1월")) + "</div></body></html>")
    groups = _groups(await _observe_html(html))
    assert [g["group"] for g in groups] == ["2026년 12월", "2027년 1월"]


@requires_chromium
async def test_same_heading_twins_get_null_and_position():
    r = await _observe_html(twin_panels("추천", "추천", names_b=("1", "2", "3", "5")))
    groups = _groups(r)
    assert [g["group"] for g in groups] == [None, None]
    assert [g["group_source"] for g in groups] == [None, None]
    assert groups[0]["x"] != groups[1]["x"]


@requires_chromium
async def test_only_link_text_candidate_gives_null():
    """표제 후보가 '이전달' 링크뿐 → 대화형 요소 글자는 표제가 아니다(null)."""
    r = await _observe_html(twin_panels("", "", names_b=("1", "2", "3", "5"), prefix_link="이전달"))
    groups = _groups(r)
    assert [g["group"] for g in groups] == [None, None]
    assert all("이전달" in {m["name"] for m in g["members"]} for g in groups)


@requires_chromium
@pytest.mark.parametrize("html", [
    twin_panels("회사", "고객", names_a=("회사소개", "채용", "제휴"), names_b=("공지사항", "자주묻는질문", "문의")),
    # 메가메뉴 열: 같은 틀의 열, 이름 첫 낱말이 다 다름
    twin_panels("여행", "호텔", names_a=("해외여행", "국내여행", "크루즈"), names_b=("국내호텔", "해외호텔", "리조트")),
], ids=["footer-cols", "mega-menu-cols"])
async def test_twins_without_shared_first_word_do_not_fire(html):
    """사용자 결정 A: 묶음 사이에 이름 첫 낱말이 겹치지 않으면 내지 않는다(응답 키 자체가 없다)."""
    r = await _observe_html(html)
    assert "repeated" not in r.data


@requires_chromium
async def test_carousel_clones_fold_then_do_not_fire():
    """캐러셀 복제(같은 이름 열) — 접으면 서로 다른 슬라이드만 남고 첫 낱말이 겹치지 않아 내지 않는다."""
    page = ("<!doctype html><html><body><div class=wrapper style='display:flex'>"
            + "".join(f"<div class=slide><a href='#s'>{n} 왕복 특가</a></div>"
                      for n in ("괌", "하와이", "사이판", "괌", "하와이"))
            + "</div></body></html>")
    r = await _observe_html(page)
    assert "repeated" not in r.data


@requires_chromium
async def test_carousel_clone_panels_fold_with_copies():
    """복제 패널(같은 (role,name) 열)은 하나로 접고 copies 로 알린다 — 다른 묶음과는 계속 구분."""
    html = ("<!doctype html><html><body><div class=w style='display:flex'>"
            + "".join(f"<div class=p><span>{h}</span><ul>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in "1234")
                      + "</ul></div>" for h in ("1월", "2월", "1월"))
            + "</div></body></html>")
    groups = _groups(await _observe_html(html))
    assert [g["group"] for g in groups] == ["1월", "2월"]
    assert groups[0]["copies"] == 1 and "copies" not in groups[1]


@requires_chromium
async def test_no_repeated_page_has_no_key():
    r = await _observe_html("<!doctype html><html><body><a href='#'>홈</a><button>검색</button>"
                            "<ul><li><a href='#'>하나</a></li><li><a href='#'>둘</a></li><li><a href='#'>셋</a></li></ul>"
                            "</body></html>")
    assert "repeated" not in r.data


@requires_chromium
async def test_names_like_server_keys_stay_values():
    """B1 회귀: 이름이 hint·url·truncated 인 대상 — 이름은 값으로만 실린다(키가 되지 않는다)."""
    r = await _observe_html(twin_panels("가", "나", names_a=("hint", "url", "truncated"),
                                        names_b=("hint", "url", "truncated")))
    groups = _groups(r)
    assert [m["name"] for m in groups[0]["members"]] == ["hint", "url", "truncated"]
    for g in groups:
        assert set(g) <= {"group", "group_source", "x", "y", "members", "copies"}
        for m in g["members"]:
            assert set(m) <= {"id", "role", "name", "states"}


# ------------------------------------------------------------------ B4: 실행 직전 묶음 고정


async def _hana_flow(mutate_js: str, pick: Tuple[str, str], approval: bool = False, html: str = HANA_HTML):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, html)
        try:
            r = await _observe(d)
            target = _member(_groups(r), *pick)
            if mutate_js:
                await page.evaluate(mutate_js)
            if approval:
                check = await d.approval_target_check({"element_id": target["id"]})
                with d.heal_disabled_scope():
                    res = await d.dispatch(ActionType.CLICK, {"element_id": target["id"]})
            else:
                check = None
                res = await d.dispatch(ActionType.CLICK, {"element_id": target["id"]})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    return res, dep, check


@requires_chromium
async def test_click_in_unchanged_month_works():
    res, dep, _ = await _hana_flow("", ("2027년 1월", "19 148만"))
    assert res.success, (res.error_code, res.error_message)
    assert dep == "2027.01.19"
    res, dep, _ = await _hana_flow("", ("2026년 12월", "19"))
    assert res.success and dep == "2026.12.19"


@requires_chromium
async def test_month_moved_same_node_reused_is_refused():
    """다음달을 누른 뒤 같은 노드(a)가 다른 달 '19' 가 됐다 — role·이름·경로가 같아도 누르지 않는다(epoch 무관)."""
    res, dep, _ = await _hana_flow("move(1)", ("2026년 12월", "19"))
    assert dep == "", f"다른 달이 눌렸다: {dep}"
    assert res.success is False
    assert res.error_code is ErrorCode.TOCTOU_MISMATCH
    assert res.reobserve_required is True
    assert "healing_attempts" not in res.data  # 치유로 다른 대상을 찾지 않는다
    changed = res.data.get("group_changed")
    assert changed == {"before": "2026년 12월", "after": "2027년 1월"}, res.data


@requires_chromium
async def test_month_moved_refused_on_approval_path():
    """승인 증표 경로: approval_target_check 가 fresh=False, 치유 금지 실행도 누르지 않는다."""
    res, dep, check = await _hana_flow("move(1)", ("2026년 12월", "19"), approval=True)
    assert check["fresh"] is False and "묶음" in check["detail"]
    assert dep == "" and res.success is False
    assert res.error_code is ErrorCode.TOCTOU_MISMATCH and res.reobserve_required


@requires_chromium
async def test_rerender_heal_does_not_pick_other_month():
    """노드를 지우고 다른 구조로 다시 그림(1월|2월) — 원래 '12월 19' 는 사라졌고, 치유가 role+이름이 같은
    다른 달(1월 19)을 고르지 않는다."""
    res, dep, _ = await _hana_flow("rerender(1)", ("2026년 12월", "19"), html=HANA_MINI_HTML)
    assert dep == "", f"치유가 다른 달을 눌렀다: {dep} (healed={res.healed})"
    assert res.success is False and res.healed is False
    assert res.reobserve_required is True


@requires_chromium
async def test_rerender_same_month_heal_still_works():
    """같은 달로 다시 그리면(구조만 바뀜) 같은 묶음·같은 표제의 같은 날짜로 치유된다(과잉 거부 아님)."""
    res, dep, _ = await _hana_flow("rerender(0)", ("2026년 12월", "19"), html=HANA_MINI_HTML)
    assert res.success and res.healed, (res.error_code, res.error_message, res.data)
    assert dep == "2026.12.19"
