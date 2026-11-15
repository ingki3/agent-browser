"""WS-82(#198·#158): 활성 오버레이 안 요소를 기본 관찰 상위 N 안으로 — 감지(격리 world·가림 사실)·순서 후처리(엔진·점수기 동결 밖).

사용자 결정 D1=C: 하나투어 실제 CSS 없이 직접 만든 시험 페이지(ws82_overlay_pages)로만 — '하나투어가 고쳐졌다'고 주장하지 않는다.
리뷰 B1~B3·NB2~NB4: 감지 꺼진 쪽 관찰 바이트 동일(mock 25 + ws62 corpus — test_ws82_overlay_census), 적대 세트 오탐 0, base top-N
접두 불변·총 N·몫 ≤ N/2, v1160 고정은 base 와 같고 추가 구성원만 새로, 반환하지 않은 full 핸들 등록 해제(옛 id 오클릭 0), 전체 관찰은
오버레이 구성원을 앞에(크기 상한 뒤 생존), 숨긴 control 추가 요소와 id 충돌 0, 클릭 뒤 페이지 안 모달 열림 신호(`overlay_opened`).
모든 시험은 로컬 Chromium set_content(요청 차단).
"""

from __future__ import annotations

import html as html_mod
import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
import ws82_overlay_pages as P

FULL = {"force_full_tree": True, "prune_top_n": 2000}


async def _open(pw, body: str):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(viewport=P.VIEW)
    await ctx.route("**/*", lambda route: route.abort())
    page = await ctx.new_page()
    await page.set_content(P.doc(body))
    await page.wait_for_timeout(30)
    engine = PerceptionEngine()
    return browser, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine))


def _no_detect(monkeypatch):
    from actions.dispatcher import ActionDispatcher

    async def none(self):
        return None

    monkeypatch.setattr(ActionDispatcher, "_overlay_detect", none)


def _strip(r) -> Dict[str, Any]:
    out = r.model_dump(mode="json")
    out["data"].pop("latency_ms", None)
    return out


def _names(r) -> List[str]:
    return [e["name"] for e in r.data["observation"]["elements"]]


async def _observe(d, args: Optional[Dict[str, Any]] = None):
    r = await d.dispatch(ActionType.OBSERVE_PAGE, dict(args or {}))
    assert r.success, r.error_message
    return r


async def _run(body: str, args: Optional[Dict[str, Any]] = None, then=None):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, body)
        try:
            r = await _observe(d, args)
            pins = {e["element_id"]: (engine.get_handle(e["element_id"]).group_path,
                                      engine.get_handle(e["element_id"]).group_heading)
                    for e in r.data["observation"]["elements"]}
            extra = await then(page, engine, d, r) if then else None
        finally:
            await browser.close()
    return r, pins, extra


# ------------------------------------------------------------------ 정탐: 달력 오버레이(실제 CSS 형태 각각)


@requires_chromium
@pytest.mark.parametrize("shape", list(P.CALENDARS))
async def test_open_calendar_confirm_and_dates_in_default_top_n(shape):
    """열린 달력의 확정 단추·날짜가 기본 관찰(top-20) 안 — z-index 없음/낮음·fixed/absolute·배경 막·top layer·aria-modal."""

    async def click_date(page, engine, d, r):
        ids = [e["element_id"] for e in r.data["observation"]["elements"] if e["name"].isdigit()]
        res = await d.dispatch(ActionType.CLICK, {"element_id": ids[0]})
        return res.success, await page.input_value("#dep")

    r, _pins, (ok, dep) = await _run(P.CALENDARS[shape], then=click_date)
    names = _names(r)
    assert len(names) == 20, names
    assert "확정" in names and any(n.isdigit() for n in names), names
    ov = r.data["overlay"]
    assert set(ov) == {"kinds", "promoted"} and 0 < ov["promoted"] <= 10, ov
    assert ov["kinds"] == [{"top_layer_dialog": "top_layer", "aria_modal": "aria_modal"}.get(shape, "covering")], ov
    assert ok and dep.isdigit(), (ok, dep)


@requires_chromium
@pytest.mark.parametrize("shape", list(P.CALENDARS))
async def test_base_prefix_kept_total_n_and_pushed_are_non_members(shape, monkeypatch):
    """base top-N 접두 불변(N−q)·총 N·몫 q ≤ N/2. 밀려난 base 요소(측정 B1-b)는 모두 오버레이 밖이다."""
    on, on_pins, _ = await _run(P.CALENDARS[shape])
    _no_detect(monkeypatch)
    off, off_pins, _ = await _run(P.CALENDARS[shape])
    a, b = on.data["observation"]["elements"], off.data["observation"]["elements"]
    k = next(i for i, (x, y) in enumerate(zip(a + [None], b + [None])) if x != y)  # 공통 접두 길이
    assert len(a) == len(b) == 20 and k >= 10, (len(a), len(b), k)  # 총 N, 몫 ≤ N/2
    member = {"확정", "닫기", "이전달", "다음달"}
    assert all(e["name"] in member or e["name"].isdigit() for e in a[k:]), [e["name"] for e in a[k:]]
    pushed = [e for e in b if e not in a]
    assert pushed and len(pushed) == on.data["overlay"]["promoted"], (pushed, on.data["overlay"])
    assert not [e for e in pushed if e["name"] in member or e["name"].isdigit()], pushed
    print(f"\nWS82-PUSHED {shape} {[e['name'] for e in pushed]}")
    q = 20 - k
    # v1160 고정: base 에 남은 요소는 감지를 끈 관찰과 같다(엔진 원래 순서·집합으로 먼저)
    for e in a[:20 - q]:
        assert on_pins[e["element_id"]] == off_pins[e["element_id"]], e


@requires_chromium
async def test_v1160_pins_engine_original_order_first_then_added_only(monkeypatch):
    """v1160 고정 1차 = 엔진 원래 순서·집합(감지를 끈 관찰의 top-N 과 같은 id 열), 2차 = 최종 목록(추가 구성원 고정용) — 그 뒤
    base 핸들의 고정은 1차 결과로 원복(base ∪ 새 고정). 순서를 바꾼 목록을 1차로 넘기면 실패(리뷰 사보타주)."""
    import perception.repeated_v1160 as v

    real = v.annotate_repeated
    calls: List[List[str]] = []

    async def spy(page, engine, elements):
        calls.append([e.element_id for e in elements])
        return await real(page, engine, elements)

    monkeypatch.setattr(v, "annotate_repeated", spy)
    on, _p, _ = await _run(P.CALENDARS["fixed_z_low"])
    on_calls = list(calls)
    calls.clear()
    _no_detect(monkeypatch)
    off, _p, _ = await _run(P.CALENDARS["fixed_z_low"])
    off_ids = [e["element_id"] for e in off.data["observation"]["elements"]]
    assert calls == [off_ids], calls
    assert len(on_calls) == 2 and on_calls[0] == off_ids, on_calls
    # WS-95a(#275): 2차 = 최종 목록 + 반환 안 된 레이어 구성원(같은 호출에 합쳐 호출 수 2 유지) — 앞은 최종 목록 그대로.
    final_ids = [e["element_id"] for e in on.data["observation"]["elements"]]
    assert on_calls[1][:len(final_ids)] == final_ids


# ------------------------------------------------------------------ 적대: 오탐 0 + 꺼진 쪽 바이트 동일


@requires_chromium
@pytest.mark.parametrize("name", list(P.ADVERSARIAL))
async def test_adversarial_no_detection_and_bytes_identical(name, monkeypatch):
    """히어로·지도·채팅·SPA 껍데기·고정 머리띠·쿠키 띠·옆 서랍·워터마크·숨은 aria-modal: 감지 0, 관찰 = 감지를 끈 관찰(바이트)."""
    on, _p, _ = await _run(P.ADVERSARIAL[name])
    assert "overlay" not in on.data
    _no_detect(monkeypatch)
    off, _p, _ = await _run(P.ADVERSARIAL[name])
    assert json.dumps(_strip(on), ensure_ascii=False) == json.dumps(_strip(off), ensure_ascii=False)


@requires_chromium
async def test_main_world_forgery_cannot_turn_detection_on(monkeypatch):
    """페이지 world 의 elementFromPoint·getComputedStyle 위조(가짜 가림)는 격리 world 감지에 닿지 않는다 — 꺼짐 그대로."""
    forge = ("<script>const L = document.createElement('div'); L.style.cssText = 'position:fixed;inset:0';"
             "document.addEventListener('DOMContentLoaded', () => document.body.appendChild(L));"
             "Document.prototype.elementFromPoint = function () { return L; };</script>")
    r, _p, _ = await _run(forge + P.BASE)
    assert "overlay" not in r.data


@requires_chromium
async def test_detection_failure_is_off_bytes_identical(monkeypatch):
    """격리 world 판독 실패 = 꺼짐: 달력이 열려 있어도 관찰은 감지를 끈 관찰과 바이트 동일."""
    from actions.dispatcher import ActionDispatcher

    async def boom(self, expression):
        raise RuntimeError("격리 world 없음")

    monkeypatch.setattr(ActionDispatcher, "isolated_eval", boom)
    on, _p, _ = await _run(P.CALENDARS["fixed_z_low"])
    _no_detect(monkeypatch)
    off, _p, _ = await _run(P.CALENDARS["fixed_z_low"])
    assert json.dumps(_strip(on), ensure_ascii=False) == json.dumps(_strip(off), ensure_ascii=False)


# ------------------------------------------------------------------ 핸들: 옛 id 오클릭 0


@requires_chromium
async def test_stale_full_id_is_unregistered_not_misclicked():
    """전체 관찰(@e1..@eM) 뒤 페이지가 바뀌고 기본 관찰(감지 켜짐 — 엔진 full)이 오면, 반환하지 않은 id 는 등록 해제 —
    옛 @e40 을 보내도 지금 순위 40위(다른 요소)를 누르지 않는다."""

    async def flow(page, engine, d, r):
        old = {e["element_id"]: e["name"] for e in r.data["observation"]["elements"]}
        await page.evaluate("() => { window.hits = []; document.addEventListener('click', e => window.hits.push("
                            "(e.target.closest('a,button') || e.target).textContent.trim()), true);"
                            " const m = document.querySelector('main'); for (let i = 0; i < 5; i++) {"
                            " const b = document.createElement('button'); b.textContent = '새 단추 ' + i; m.prepend(b); } }")
        r2 = await _observe(d)
        kept = {e["element_id"] for e in r2.data["observation"]["elements"]}
        stale = next(k for k in old if k not in kept and int(k[2:]) > 20)
        res = await d.dispatch(ActionType.CLICK, {"element_id": stale})
        return old[stale], res, await page.evaluate("window.hits")

    _r, _p, (old_name, res, hits) = await _run(P.CALENDARS["fixed_z_low"], FULL, then=flow)
    assert res.success is False and hits == [], (old_name, res.error_code, hits)


# ------------------------------------------------------------------ 전체 관찰: 구성원 앞 + 크기 상한


@requires_chromium
async def test_full_observe_puts_members_first_and_cap_keeps_them():
    """force_full_tree: 오버레이 구성원이 앞(고정은 원래 순서로) — 작은 크기 상한(접두만 남김)에서도 확정·날짜가 남는다."""
    from interface.server_envelope import cap_result_size

    r, _p, _ = await _run(P.CALENDARS["absolute_z_auto"], FULL)
    names = _names(r)
    k = r.data["overlay"]["promoted"]
    assert k >= 60 and set(names[:k]) >= {"확정", "닫기", "이전달", "다음달"}, names[:k]
    trial = r.model_copy(deep=True)
    cap_result_size(trial, 6000)
    kept = _names(trial)
    assert trial.data.get("truncated") and "확정" in kept and any(n.isdigit() for n in kept), kept


# ------------------------------------------------------------------ NB4: 숨긴 control 추가 요소와 순서·id


HIDDEN = ("<div style='height:1400px'></div><label for=agree>약관 동의</label>"
          "<input type=checkbox id=agree style='position:absolute;opacity:0;width:1px;height:1px'>")


@requires_chromium
async def test_hidden_controls_added_ids_do_not_collide_and_fit_drops_extras_first():
    """오버레이 구성원(@e40 등, 비연속 id) + 숨긴 체크박스 짝 라벨 추가 요소: 추가 요소 id 는 가장 큰 @eN 다음이라 겹치지 않고,
    크기 맞춤(fit_hidden_controls)은 블록·추가 요소를 원래 요소(오버레이 구성원 포함)보다 먼저 줄인다."""
    from interface.server_envelope import cap_result_size, envelope_json
    from perception import hidden_controls as hc

    r, _p, _ = await _run(P.CALENDARS["fixed_z_low"] + HIDDEN)
    els = r.data["observation"]["elements"]
    ids = [e["element_id"] for e in els]
    assert len(ids) == len(set(ids)), ids
    block = r.data.get(hc.BLOCK_KEY) or []
    added = [b["element_id"] for b in block if b.get("added")]
    assert added and max(int(i[2:]) for i in ids if i not in added) < min(int(i[2:]) for i in added), (ids, added)
    assert "확정" in _names(r)
    size = len(envelope_json(r))
    trial = r.model_copy(deep=True)
    hc.fit_hidden_controls(trial, size - 40)
    cap_result_size(trial, size - 40)
    names = _names(trial)
    assert "확정" in names and hc.BLOCK_KEY not in trial.data, (names, trial.data.get(hc.BLOCK_KEY))


# ------------------------------------------------------------------ 프레임


@requires_chromium
async def test_calendar_overlay_inside_frame():
    """switch_frame 한 프레임의 관찰: 감지·구성원 판독은 그 프레임의 격리 world — 확정·날짜가 상위 N 안."""
    from playwright.async_api import async_playwright

    inner = P.doc(P.CALENDARS["fixed_z_low"])
    outer = ("<iframe id=f style='width:1260px;height:880px;border:0' srcdoc=\"" + html_mod.escape(inner, quote=True)
             + "\"></iframe>")
    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, outer)
        try:
            await page.wait_for_function("() => { const f = document.getElementById('f');"
                                         " return f.contentDocument && f.contentDocument.querySelector('.ok'); }")
            sw = await d.dispatch(ActionType.SWITCH_FRAME, {"frame_selector": "#f"})
            assert sw.success, sw.error_message
            r = await _observe(d)
        finally:
            await browser.close()
    names = _names(r)
    assert r.data.get("overlay", {}).get("kinds") == ["covering"] and "확정" in names, (r.data.get("overlay"), names)


# ------------------------------------------------------------------ #158: 클릭 뒤 페이지 안 모달 열림 신호


OPENER = (P.BASE.replace("<button type=button>항공권 검색</button>",
                         "<button type=button id=open onclick=\"document.body.appendChild(document.getElementById('tpl').content.cloneNode(true))\">달력 열기</button>"
                         "<button type=button id=noop onclick='return false'>아무것도</button>"
                         "<button type=button id=al onclick=\"alert('x')\">경고</button>")
          + "<template id=tpl><div id=cal style='position:fixed;top:120px;left:240px;width:800px;height:560px;background:#fff'>"
          + P.CAL_BODY + "</div></template>" + P.PICK_JS)


@requires_chromium
async def test_click_that_opens_modal_signals_overlay_opened():
    """달력 열기 클릭 → data.overlay_opened={kinds:[covering]}(JS 대화상자 dialog_opened 와 다른 키). 열린 달력 안 날짜 클릭·무반응
    단추·JS alert 에는 붙지 않는다(이미 열린 레이어는 '새로' 가 아님)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, OPENER)
        try:
            r = await _observe(d, FULL)
            by = {e["name"]: e["element_id"] for e in r.data["observation"]["elements"]}
            opened = await d.dispatch(ActionType.CLICK, {"element_id": by["달력 열기"]})
            r = await _observe(d)
            day = next(e["element_id"] for e in r.data["observation"]["elements"] if e["name"].isdigit())
            inside = await d.dispatch(ActionType.CLICK, {"element_id": day})
            await page.evaluate("() => { document.getElementById('cal').remove(); }")
            r = await _observe(d, FULL)
            by = {e["name"]: e["element_id"] for e in r.data["observation"]["elements"]}
            await d.dispatch(ActionType.HANDLE_DIALOG, {"accept": True})
            alert = await d.dispatch(ActionType.CLICK, {"element_id": by["경고"]})
        finally:
            await browser.close()
    assert opened.success and opened.data.get("overlay_opened") == {"kinds": ["covering"]}, opened.data
    assert inside.success and "overlay_opened" not in inside.data, inside.data
    assert "overlay_opened" not in alert.data and alert.data.get("dialogs"), alert.data
