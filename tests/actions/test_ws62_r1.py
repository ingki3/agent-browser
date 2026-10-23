"""WS-62 R1: 검증 FAIL R1~R3 재현(디스패처 경로, 실제 Chromium set_content).

* R1 표시와 고정 분리 — 같은 표제 쌍둥이(group=null)·접힌 복제본도 핸들마다 실제 묶음 지문으로 고정한다.
* R2 치유 후 고정 유지 — 치유 재관찰도 묶음 주석을 달고, 채택한 핸들은 원래 선택 근거를 잇는다.
* R3 표제 적격성 — aria-labelledby 참조의 대화형·반복 목록 제외, 정적 후보가 여럿이면 하나를 고르지 않는다(표시는
  이은 글자, 결정 A) + 내부 고정은 정적 글자 전체 지문.
증거: .hermes/verify/ws62/evidence(probe.py·input_probe.py·heading_probe.py·security_probe.py).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_repeated import _groups, _member, _observe, _open
from ws62_pages import HANA_MINI_HTML, twin_panels

OUT = "<p id=out>대기</p>"


def _record_panel(html: str, panel_sel: str = ".p", head_sel: str = "span") -> str:
    """패널 안 링크를 누르면 #out 에 그 패널 표제 글자를 적는다(눌린 달을 사실로 확인)."""
    js = ("<script>document.querySelectorAll('%s a').forEach(function(a){a.onclick=function(){"
          "document.getElementById('out').textContent=this.closest('%s').querySelector('%s').textContent"
          "+'-'+this.textContent;return false;};});</script>" % (panel_sel, panel_sel, head_sel))
    return html.replace("<body>", "<body>" + OUT).replace("</body>", js + "</body>")


async def _flow(html: str, pick, mutate_js: str, action: ActionType = ActionType.CLICK,
                params: Optional[Dict[str, Any]] = None, approval: bool = False):
    """관찰 → 대상 고르기(pick(groups, engine, page)) → 페이지 변형 → 같은 id 로 실행. (결과, #out, 승인 사전 검사)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, html)
        try:
            r = await _observe(d)
            eid = await pick(r, engine, page)
            await page.evaluate(mutate_js)
            check = await d.approval_target_check({"element_id": eid}) if approval else None
            res = await d.dispatch(action, {"element_id": eid, **(params or {})})
            out = await page.text_content("#out") if await page.locator("#out").count() else None
            value = (await page.locator("input").first.input_value()
                     if action is ActionType.TYPE_TEXT else None)
        finally:
            await browser.close()
    return res, out, check, value


async def _by_selector(engine, page, selector: str) -> str:
    """관찰 핸들 중 지금 DOM 에서 selector 와 같은 노드인 것(점수 순서가 아니라 실제 노드로 고른다)."""
    for h in engine.handles.values():
        if h.css_path and not h.is_shadow and await page.evaluate(
                "([p, s]) => document.querySelector(p) === document.querySelector(s)", [h.css_path, selector]):
            return h.element_id
    raise AssertionError(f"핸들 없음: {selector}")


def _refused(res, out, expect_out: str = "대기") -> None:
    assert out == expect_out, f"바뀐 묶음을 눌렀다: {out}"
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH, (res.error_code, res.error_message)
    assert res.reobserve_required is True
    assert "group_changed" in res.data


# ------------------------------------------------------------------ R1: 표시 null·복제 접기와 무관한 고정


SAME_HEAD = _record_panel(twin_panels("추천", "추천", names_b=("1", "2", "3", "5")))
FOLDED = _record_panel(
    "<!doctype html><html><body><div class=w style='display:flex'>"
    + "".join(f"<div class=p><span>{h}</span><ul>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in "1234")
              + "</ul></div>" for h in ("1월", "2월", "1월"))
    + "</div></body></html>")


@requires_chromium
async def test_same_heading_twin_title_change_is_refused():
    """표제 '추천/추천'(공개 group=null) — 한쪽 표제가 바뀌면 기존 id 를 누르지 않는다."""
    async def pick(r, engine, page):
        assert [g["group"] for g in _groups(r)] == [None, None]
        return await _by_selector(engine, page, ".p:first-child li:first-child a")

    res, out, check, _ = await _flow(SAME_HEAD, pick, "document.querySelector('.p span').textContent='다른달'",
                                     approval=True)
    _refused(res, out)
    assert check["fresh"] is False and check.get("group_changed") is True


@requires_chromium
async def test_same_heading_twin_unchanged_click_works():
    """대조군: 표제가 그대로면 같은 id 로 정상 클릭(과잉 거부 아님)."""
    async def pick(r, engine, page):
        return await _by_selector(engine, page, ".p:nth-child(2) li:first-child a")

    res, out, _, _ = await _flow(SAME_HEAD, pick, "0")
    assert res.success, (res.error_code, res.error_message)
    assert out == "추천-1"


@requires_chromium
async def test_folded_clone_title_change_is_refused():
    """1월/2월/1월 — 접힌 마지막 패널(복제)을 3월로 바꾸면 그 패널의 기존 id 는 누르지 않는다(승인 사전 검사도 거부)."""
    async def pick(r, engine, page):
        groups = _groups(r)
        assert [g["group"] for g in groups] == ["1월", "2월"] and groups[0]["copies"] == 1
        return await _by_selector(engine, page, ".p:nth-child(3) li:first-child a")

    res, out, check, _ = await _flow(FOLDED, pick, "document.querySelectorAll('.p span')[2].textContent='3월'",
                                     approval=True)
    _refused(res, out)
    assert check["fresh"] is False


@requires_chromium
async def test_same_heading_twin_type_text_is_refused():
    """input_probe: 같은 표제 쌍둥이의 입력칸 — 표제가 바뀌면 type_text 도 입력하지 않는다."""
    html = ("<!doctype html><body><div style=\"display:flex\">" + "".join(
        "<div class=p><span>" + h + "</span><ul>" + "".join(f"<li><input aria-label=\"{n}\"></li>" for n in ns)
        + "</ul></div>" for h, ns in zip(["추천", "추천"], [["메모", "대상", "소식"], ["메모", "기록", "검토"]]))
        + "</div></body>")

    async def pick(r, engine, page):
        g = _groups(r)[0]
        assert g["group"] is None
        return g["members"][0]["id"]

    res, _out, _check, value = await _flow(html, pick, "document.querySelector('.p span').textContent='2월'",
                                           action=ActionType.TYPE_TEXT, params={"text": "합성입력"})
    assert value == "", f"바뀐 묶음에 입력했다: {value}"
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH and res.reobserve_required


# ------------------------------------------------------------------ R2: 치유 후 고정 유지


@requires_chromium
async def test_heal_then_month_move_refuses_returned_and_new_handles():
    """같은 달 재렌더 → 치유 클릭 성공 → 달 이동 → 치유가 채택한 id·재관찰이 만든 다른 '19' id 모두 거부."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, HANA_MINI_HTML)
        try:
            groups = _groups(await _observe(d))
            old = _member(groups, "2026년 12월", "19")["id"]
            await page.evaluate("rerender(0)")
            first = await d.dispatch(ActionType.CLICK, {"element_id": old})
            assert first.success and first.healed, (first.error_code, first.error_message)
            assert await page.input_value("#dep") == "2026.12.19"
            adopted = first.data.get("element_id") or old
            pinned = engine.get_handle(adopted)
            assert pinned is not None and pinned.group_path and pinned.group_heading
            # 치유 재관찰이 만든 새 핸들 전부(채택 안 된 다른 달 '19' 포함)에도 묶음 고정이 있다.
            nineteens = [h for h in engine.handles.values() if h.name.split()[:1] == ["19"]]
            assert len(nineteens) >= 2 and all(h.group_path for h in nineteens), [
                (h.element_id, h.group_path) for h in nineteens]
            await page.evaluate("document.querySelector('#dep').value=''; move(1)")
            results = {}
            for h in nineteens:
                results[h.element_id] = await d.dispatch(ActionType.CLICK, {"element_id": h.element_id})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert dep == "", f"달이 바뀐 뒤 치유 id 로 눌렀다: {dep}"
    for eid, res in results.items():
        assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH, (eid, res.error_message)
        assert res.reobserve_required is True


@requires_chromium
async def test_adopted_handle_inherits_original_pin_when_ids_shift(monkeypatch):
    """채택 핸들 승계: 치유 재관찰이 다른 id 를 매기고(번호 밀림) 그 관찰에서 묶음 주석이 나지 않아도(예: 다른 달이
    상위 N 밖), 치유가 반환한 id 는 원래 선택 근거(12월 지문)를 이어 달이 바뀌면 거부된다."""
    from playwright.async_api import async_playwright

    import perception.repeated as rep

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, HANA_MINI_HTML)
        try:
            groups = _groups(await _observe(d))
            old = _member(groups, "2026년 12월", "19")["id"]
            real_observe = engine.observe_page

            async def shifted(**kw):  # 치유 재관찰: id 를 +100 밀어 새로 매긴다(이전 관찰 id 와 겹치지 않게)
                res = await real_observe(**kw)
                moved = []
                for el in res.elements:
                    nid = "@e" + str(100 + int(el.element_id[2:]))
                    engine._handles[nid] = engine._handles.pop(el.element_id)
                    engine._handles[nid].element_id = nid
                    moved.append(el.model_copy(update={"element_id": nid}))
                return res.model_copy(update={"elements": moved})

            async def no_annotation(*_a, **_k):
                return None

            monkeypatch.setattr(engine, "observe_page", shifted)
            monkeypatch.setattr(rep, "annotate_repeated", no_annotation)
            await page.evaluate("rerender(0)")
            first = await d.dispatch(ActionType.CLICK, {"element_id": old})
            assert first.success and first.healed, (first.error_code, first.error_message)
            adopted = first.data.get("element_id")
            assert adopted and adopted != old, first.data
            await page.evaluate("document.querySelector('#dep').value=''; move(1)")
            second = await d.dispatch(ActionType.CLICK, {"element_id": adopted})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert dep == "", f"치유가 반환한 id 로 달이 바뀐 뒤 눌렀다: {dep}"
    assert second.success is False and second.error_code is ErrorCode.TOCTOU_MISMATCH


# ------------------------------------------------------------------ R3: 표제 적격성·정적 글자 지문


def _badge_panels(labels: Tuple[str, str]) -> str:
    """heading_probe: 패널마다 [배지/가격 span][달 span][날짜 목록] — 배지가 달 앞에 온다."""
    return ("<!doctype html><body>" + OUT + "<div class=w style=\"display:flex\">" + "".join(
        f"<div class=p><span class=badge>{badge}</span><span class=month>{month}</span><ul>" + "".join(
            f"<li><a href=\"#\" onclick=\"document.querySelector('#out').textContent="
            f"this.closest('.p').querySelector('.month').textContent+'-{day}';return false\">{day}</a></li>"
            for day in ["18", "19", "20", "21"]) + "</ul></div>"
        for badge, month in zip(labels, ["2026년 12월", "2027년 1월"])) + "</div></body>")


@requires_chromium
@pytest.mark.parametrize("labels", [("148만", "149만"), ("AD", "BEST")], ids=["price", "badge"])
async def test_static_candidates_month_change_is_refused(labels):
    """가격·배지가 달 앞에 있어도 하나를 표제로 고르지 않는다 — 표시는 이은 글자, 달만 바꿔도 거부."""
    async def pick(r, engine, page):
        groups = _groups(r)
        assert [g["group"] for g in groups] == [f"{labels[0]} 2026년 12월", f"{labels[1]} 2027년 1월"]
        assert [g["group_source"] for g in groups] == ["text", "text"]
        return next(m["id"] for m in groups[0]["members"] if m["name"] == "19")

    res, out, _, _ = await _flow(_badge_panels(labels), pick,
                                 "document.querySelector('.month').textContent='2027년 2월'")
    _refused(res, out)


@requires_chromium
async def test_same_joined_text_twins_show_null():
    """이은 글자가 쌍둥이끼리 같으면 표시하지 않는다(null+위치) — 고정은 여전히 핸들마다."""
    html = _badge_panels(("AD", "AD")).replace("2027년 1월", "2026년 12월")
    head, tail = html.rsplit(">21</a>", 1)
    html = head + ">25</a>" + tail  # 이름 열이 달라 복제로 접히지 않게
    from test_ws62_repeated import _observe_html

    groups = _groups(await _observe_html(html))
    assert [g["group"] for g in groups] == [None, None]


@requires_chromium
@pytest.mark.parametrize("label", [
    "'<a href=\"#\" id=\"label-' + side + '\">' + (side === 'left' ? '고정 A' : '고정 B') + '</a>'",
    # 참조가 링크 안 span — 참조 노드 자신은 정적이어도 조상(링크)이 대화형이면 제외
    "'<a href=\"#\"><span id=\"label-' + side + '\">' + (side === 'left' ? '고정 A' : '고정 B') + '</span></a>'",
], ids=["link", "span-in-link"])
async def test_labelledby_interactive_reference_is_not_heading(label):
    """security_probe aria_interactive: aria-labelledby 가 패널 안 링크('고정 A/B')를 가리키면 표제가 아니다 —
    표시는 달, 달을 넘기면 기존 id 를 누르지 않는다."""
    html = HANA_MINI_HTML.replace(
        "inr.className = 'inr' + (side === 'right' ? ' right' : '');",
        "inr.className = 'inr' + (side === 'right' ? ' right' : '');inr.setAttribute('aria-labelledby', 'label-'+side);",
    ).replace(
        "+ '<span class=\"month\"></span>'",
        "+ " + label + " + '<span class=\"month\"></span>'",
    )
    assert "aria-labelledby" in html and "고정 A" in html
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, html)
        try:
            groups = _groups(await _observe(d))
            assert [(g["group"], g["group_source"]) for g in groups] == [("2026년 12월", "text"), ("2027년 1월", "text")]
            eid = _member(groups, "2026년 12월", "19")["id"]
            assert "고정" not in (engine.get_handle(eid).group_heading or "")
            await page.evaluate("move(1)")
            res = await d.dispatch(ActionType.CLICK, {"element_id": eid})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert dep == "" and res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH


@requires_chromium
async def test_labelledby_repeated_item_reference_is_not_heading():
    """aria-labelledby 가 반복 목록 항목(요일 머리글)을 가리키면 표제가 아니다 → 다음 단계(정적 글자)로."""
    html = ("<!doctype html><body><div class=w style='display:flex'>" + "".join(
        f"<div class=p aria-labelledby='wk{k}'><b>{m}</b><ul class=week>"
        + "".join(f"<li{' id=wk' + str(k) if i == 0 else ''}>{d}</li>" for i, d in enumerate("일월화"))
        + "</ul><ul>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in "1234") + "</ul></div>"
        for k, m in enumerate(("12월", "1월"))) + "</div></body>")
    from test_ws62_repeated import _observe_html

    groups = _groups(await _observe_html(html))
    assert [(g["group"], g["group_source"]) for g in groups] == [("12월", "text"), ("1월", "text")]


@requires_chromium
@pytest.mark.parametrize("prefix", ["", "예약 가능한 출발일의 항공권 가격 및 특별 혜택을 아래에서 확인하세요 - "])
async def test_three_sibling_spans_panel_month_change_is_refused(prefix):
    """security_probe triple_spans: 배지·달·특가 span 셋(같은 틀 형제 ≥3 → 반복 목록처럼 보임) — 표시는 null 이어도
    지문에는 들어가 달을 넘기면 거부한다. R6: 월 글자가 40자(표시 절단) 뒤에 있어도 신원에 남아야 한다."""
    html = HANA_MINI_HTML.replace(
        "+ '<span class=\"month\"></span>'",
        "+ '<span class=badge>추천</span><span class=\"month\"></span><span>특가</span>'",
    )
    if prefix:
        old = ".textContent = y + '년 ' + (m + 1) + '월';"
        assert old in html
        html = html.replace(old, ".textContent = " + json.dumps(prefix, ensure_ascii=False) + " + y + '년 ' + (m + 1) + '월';")
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, html)
        try:
            await _observe(d)
            eid = await _by_selector(engine, page, "#cal .inr:first-child ul.day li:nth-child(6) a")
            assert (engine.get_handle(eid).name or "").startswith("19")
            await page.evaluate("move(1)")
            res = await d.dispatch(ActionType.CLICK, {"element_id": eid})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert dep == "", f"달이 바뀐 뒤 눌렀다: {dep}"
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH
