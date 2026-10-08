"""WS-35 R2: 뮤테이션 감사(.hermes/verify/mut-audit)가 찾은 tests/actions 빈틈 고정.

* V2·V3 — 제자리 교체(광고 로테이션: 같은 css_path 에 다른 링크) staleness 판정
  (NAME_CHANGED·ROLE_CHANGED → E_TOCTOU_MISMATCH) 을 실제 Chromium 페이지로 고정한다.
* V8 — 비밀번호 칸(input[type=password])에 평문 type_text 를 해도 결과(직렬화 전체·MCP 봉투)에
  평문이 실리지 않는다(element_is_secret 자동 은닉).
* D1·D2·D5 — 디스패처 계약(호출자 epoch 불일치 거부·heal_disabled 면 대체 요소 금지·selector
  다중 매칭 거부)을 tests/interface 밖, 디스패처 단위로 내린다.

로컬 set_content 만 쓴다(네트워크 없음, headless — Linux CI 에서도 돈다).
"""

from __future__ import annotations

import json
from typing import Any, Tuple

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401

# 광고 슬롯 하나(링크) + 같은 이름이 아닌 다른 버튼 + 결과 표시줄.
# 슬롯 링크는 누르면 #out 에 자기 이름을 적는다 — 무엇이 눌렸는지 페이지 상태로 확인한다.
AD_HTML = """<!doctype html><html><body>
<div id=ad><a id=slot href="#"
  onclick="document.getElementById('out').textContent='clicked:'+this.textContent;return false"
  >결제하기</a></div>
<button id=other onclick="document.getElementById('out').textContent='other'">장바구니 비우기</button>
<p id=out>대기</p>
</body></html>"""

#: 제자리 교체 시나리오: (이름, 바꾸는 JS, 기대 사유 값)
SWAPS = [
    ("text", "document.getElementById('slot').textContent='회원 탈퇴'", "name_changed"),
    ("aria-label", "document.getElementById('slot').setAttribute('aria-label','회원 탈퇴')",
     "name_changed"),
    ("role", "document.getElementById('slot').setAttribute('role','checkbox')", "role_changed"),
    # 광고 로테이션: 노드 자체가 새 링크로 바뀌었지만 같은 자리(같은 css_path)
    ("ad-rotation",
     "(() => { const a = document.getElementById('slot'); const n = a.cloneNode(false);"
     " n.textContent = '광고: 대출 상담'; n.onclick = a.onclick; a.replaceWith(n); })()",
     "name_changed"),
]


async def _setup(pw, html: str):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    browser = await pw.chromium.launch(headless=True)
    page = await (await browser.new_context()).new_page()
    await page.set_content(html)
    engine = PerceptionEngine()
    return browser, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine))


async def _observe_slot(engine, page) -> Tuple[Any, Any]:
    obs = await engine.observe_page(page=page, prune_top_n=10)
    el = next(e for e in obs.elements if e.name == "결제하기")
    return obs, el


# ------------------------------------------------------------------ V2·V3: verify_staleness


@requires_chromium
@pytest.mark.parametrize("label,swap_js,reason", SWAPS, ids=[s[0] for s in SWAPS])
async def test_in_place_swap_is_stale_with_toctou(label, swap_js, reason):
    """관찰 뒤 같은 자리 요소의 이름/role 이 바뀌면 fresh 가 아니다(PRD §4.2 Role/Name 일치)."""
    from playwright.async_api import async_playwright

    from actions import StalenessReason, verify_staleness

    async with async_playwright() as pw:
        browser, page, engine, _ = await _setup(pw, AD_HTML)
        try:
            _, el = await _observe_slot(engine, page)
            handle = engine.get_handle(el.element_id)
            assert handle is not None
            before = await verify_staleness(page, handle, engine.epoch)
            assert before.fresh and before.reason is StalenessReason.FRESH, "대조: 바꾸기 전엔 fresh"
            await page.evaluate(swap_js)
            # 같은 자리다 — css_path 로 여전히 연결된 노드가 잡힌다(NODE_DETACHED 가 아니다).
            assert await page.locator(handle.css_path).count() == 1
            st = await verify_staleness(page, handle, engine.epoch)
            out = await page.text_content("#out")
        finally:
            await browser.close()
    assert st.fresh is False
    assert st.reason is StalenessReason(reason)
    assert st.error_code is ErrorCode.TOCTOU_MISMATCH
    if reason == "name_changed":
        assert st.observed_name and st.observed_name != "결제하기"
        assert "결제하기" in st.detail and st.observed_name in st.detail
    else:
        assert st.observed_role == "checkbox" and "link" in st.detail
    assert out == "대기", "검증만 했는데 페이지가 바뀌었다"


@requires_chromium
@pytest.mark.parametrize("label,swap_js,reason", SWAPS, ids=[s[0] for s in SWAPS])
async def test_in_place_swap_blocks_approved_click(label, swap_js, reason):
    """승인 증표 호출(heal_disabled) 경로: 제자리 교체면 클릭하지 않고 E_TOCTOU_MISMATCH(D2 겸)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, AD_HTML)
        try:
            obs, el = await _observe_slot(engine, page)
            await page.evaluate(swap_js)
            d.heal_disabled = True
            r = await d.dispatch(ActionType.CLICK,
                                 {"element_id": el.element_id, "epoch": obs.snapshot_epoch})
            out = await page.text_content("#out")
        finally:
            await browser.close()
    assert r.success is False
    assert r.error_code is ErrorCode.TOCTOU_MISMATCH
    assert r.healed is False and r.data.get("heal_disabled") is True
    assert r.reobserve_required is True
    kind = reason.split("_")[0]  # name / role — 사유의 상세(무엇이 무엇으로)가 메시지에 실린다
    assert f"{kind} '" in (r.error_message or ""), r.error_message
    assert out == "대기", "바뀐 요소가 눌렸다"


@requires_chromium
@pytest.mark.parametrize("label,swap_js,reason", SWAPS, ids=[s[0] for s in SWAPS])
async def test_in_place_swap_default_dispatch_does_not_click(label, swap_js, reason):
    """WS-37 결함1: 기본 디스패치(치유 켜짐)도 제자리 교체된 요소를 누르지 않는다.

    전에는 치유 4단계(css_path)가 같은 자리의 교체된 요소('회원 탈퇴', '광고: 대출 상담')로
    '치유'해 healed=True 로 눌렀다. 부작용 액션은 경로 단계에서 role/name 이 바뀐 요소를
    고르지 않는다 — TOCTOU_MISMATCH 와 '요소가 바뀜(이전 → 현재) — 다시 관찰하라' 안내.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, AD_HTML)
        try:
            obs, el = await _observe_slot(engine, page)
            await page.evaluate(swap_js)
            r = await d.dispatch(ActionType.CLICK,
                                 {"element_id": el.element_id, "epoch": obs.snapshot_epoch})
            out = await page.text_content("#out")
        finally:
            await browser.close()
    assert out == "대기", f"교체된 요소가 눌렸다: {out!r} (healed={r.healed})"
    assert r.success is False and r.error_code is ErrorCode.TOCTOU_MISMATCH
    assert r.healed is False and r.reobserve_required is True
    changed = r.data.get("element_changed")
    assert changed is not None, r.data
    assert changed["before"] == {"role": "link", "name": "결제하기"}
    if reason == "role_changed":
        assert changed["after"] == {"role": "checkbox", "name": "결제하기"}
    else:
        assert changed["after"]["role"] == "link"
        assert changed["after"]["name"] in ("회원 탈퇴", "광고: 대출 상담")
    assert "다시 관찰" in r.data["hint"] and "결제하기" in r.data["hint"]
    assert "다시 관찰" in (r.error_message or "")
    assert any("identity_changed" in a for a in r.data["healing_attempts"])


@requires_chromium
@pytest.mark.parametrize("label,swap_js,reason", SWAPS, ids=[s[0] for s in SWAPS])
async def test_in_place_swap_read_action_still_heals_by_path(label, swap_js, reason):
    """대조(WS-37): 읽기 액션(hover)은 현행대로 경로 단계로 같은 자리 요소에 닿는다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, AD_HTML)
        try:
            obs, el = await _observe_slot(engine, page)
            await page.evaluate(swap_js)
            r = await d.dispatch(ActionType.HOVER,
                                 {"element_id": el.element_id, "epoch": obs.snapshot_epoch})
            out = await page.text_content("#out")
        finally:
            await browser.close()
    assert r.healed is True, (r.error_code, r.error_message, r.data)
    assert "element_changed" not in r.data
    assert out == "대기", "hover 는 클릭하지 않는다"


@requires_chromium
async def test_moved_same_identity_element_still_heals_for_click():
    """대조(WS-37): 같은 이름·역할 요소가 다른 경로로 옮겨지면 클릭도 1단계로 치유한다."""
    from playwright.async_api import async_playwright

    move_js = ("(() => { const a = document.getElementById('slot'); const s = "
               "document.createElement('section'); document.body.prepend(s); "
               "a.removeAttribute('id'); s.appendChild(a); })()")
    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, AD_HTML)
        try:
            obs, el = await _observe_slot(engine, page)
            await page.evaluate(move_js)
            r = await d.dispatch(ActionType.CLICK,
                                 {"element_id": el.element_id, "epoch": obs.snapshot_epoch})
            out = await page.text_content("#out")
        finally:
            await browser.close()
    assert r.success is True and r.healed is True, (r.error_code, r.error_message, r.data)
    assert out == "clicked:결제하기"


# ------------------------------------------------------------------ D2: heal_disabled 대체 금지


@requires_chromium
async def test_heal_disabled_never_substitutes_similar_element():
    """이름이 살짝 바뀐(치유 3단계가 잡을 만한) 대상도 heal_disabled 면 누르지 않는다. 대조: 끄면 치유."""
    from playwright.async_api import async_playwright

    html = AD_HTML.replace("<p id=out>", "<a id=near href=\"#\" onclick=\"document.getElementById('out')"
                           ".textContent='near';return false\">결제 하기</a><p id=out>")
    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html)
        try:
            obs, el = await _observe_slot(engine, page)
            await page.evaluate("document.getElementById('slot').remove()")
            d.heal_disabled = True
            r = await d.dispatch(ActionType.CLICK,
                                 {"element_id": el.element_id, "epoch": obs.snapshot_epoch})
            out1 = await page.text_content("#out")
            d.heal_disabled = False
            r2 = await d.dispatch(ActionType.CLICK,
                                  {"element_id": el.element_id, "epoch": obs.snapshot_epoch})
            out2 = await page.text_content("#out")
        finally:
            await browser.close()
    assert r.success is False and r.healed is False and r.data.get("heal_disabled") is True
    assert r.error_code is ErrorCode.ELEMENT_NOT_FOUND  # 제거(NODE_DETACHED) 사유의 코드
    assert out1 == "대기"
    assert r2.healed is True, "대조: heal_disabled 가 아니면 치유 사다리가 돈다"
    assert out2 == "near"


# ------------------------------------------------------------------ D1: 호출자 epoch 불일치


@requires_chromium
@pytest.mark.parametrize("claimed", [1, 999, -1])
async def test_dispatcher_rejects_caller_epoch_mismatch(claimed):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, AD_HTML)
        try:
            obs, el = await _observe_slot(engine, page)
            assert obs.snapshot_epoch == 0 and engine.epoch == 0
            r = await d.dispatch(ActionType.CLICK, {"element_id": el.element_id, "epoch": claimed})
            out = await page.text_content("#out")
            ok = await d.dispatch(ActionType.CLICK, {"element_id": el.element_id, "epoch": 0})
            out_ok = await page.text_content("#out")
        finally:
            await browser.close()
    assert r.success is False
    assert r.error_code is ErrorCode.TOCTOU_MISMATCH
    assert r.reobserve_required is True
    assert f"요청 {claimed}" in (r.error_message or "") and "현재 0" in (r.error_message or "")
    assert out == "대기", "거부됐는데 클릭됐다"
    assert ok.success, (ok.error_code, ok.error_message)  # 대조: 맞는 epoch 는 통과
    assert out_ok == "clicked:결제하기"


# ------------------------------------------------------------------ D5: selector 다중 매칭


@requires_chromium
async def test_dispatcher_selector_must_match_exactly_one():
    from playwright.async_api import async_playwright

    html = """<!doctype html><body>
    <button class=btn onclick="document.getElementById('out').textContent='a'">가</button>
    <button class=btn onclick="document.getElementById('out').textContent='b'">나</button>
    <p id=out>대기</p></body>"""
    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, html)
        try:
            await engine.observe_page(page=page, prune_top_n=10)
            many = await d.dispatch(ActionType.CLICK, {"selector": ".btn"})
            none = await d.dispatch(ActionType.CLICK, {"selector": "#nope"})
            out = await page.text_content("#out")
            one = await d.dispatch(ActionType.CLICK, {"selector": ".btn:nth-of-type(2)"})
            out_one = await page.text_content("#out")
        finally:
            await browser.close()
    assert many.success is False and many.error_code is ErrorCode.ELEMENT_NOT_FOUND
    assert many.data.get("match_count") == 2
    assert none.success is False and none.error_code is ErrorCode.ELEMENT_NOT_FOUND
    assert none.data.get("match_count") == 0
    assert out == "대기", "모호한 selector 로 무언가 눌렸다"
    assert one.success, (one.error_code, one.error_message)  # 대조: 정확히 1개면 실행
    assert out_one == "b"


# ------------------------------------------------------------------ V8: 비밀번호 칸 자동 은닉

SECRET = "hunter2-평문"

PW_HTML = """<!doctype html><body>
<input id=pw type=password aria-label="비밀번호">
<input id=pw_up type=password aria-label="비밀번호 확인"
  oninput="this.value=this.value.toUpperCase()">
</body>"""


async def _type_into(name: str):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page, engine, d = await _setup(pw, PW_HTML)
        try:
            obs = await engine.observe_page(page=page, prune_top_n=10)
            el = next(e for e in obs.elements if e.name == name)
            r = await d.dispatch(ActionType.TYPE_TEXT, {"element_id": el.element_id,
                                                        "epoch": obs.snapshot_epoch,
                                                        "text": SECRET})
            values = await page.evaluate("[pw.value, pw_up.value]")
        finally:
            await browser.close()
    return r, values


def _assert_no_plaintext(r) -> None:
    from interface.mcp_server import envelope_dict, envelope_json

    dumped = json.dumps(r.model_dump(mode="json"), ensure_ascii=False)
    env = json.dumps(envelope_dict(r), ensure_ascii=False)
    for blob in (dumped, env, envelope_json(r)):
        for leak in (SECRET, SECRET.upper(), "hunter2", "HUNTER2"):
            assert leak not in blob, f"평문 {leak!r} 이 결과에 실림: {blob[:300]}"


@requires_chromium
async def test_plain_type_into_password_field_conceals_value():
    """자격증명 치환이 아닌 평문 type_text 라도 password 칸이면 신호에 값 대신 <secret>."""
    r, values = await _type_into("비밀번호")
    assert values[0] == SECRET, "대조: 값은 실제로 들어갔다"
    assert r.success, (r.error_code, r.error_message)
    assert r.data["signals"] == ["value_applied: <secret>"]
    _assert_no_plaintext(r)


@requires_chromium
async def test_transformed_password_value_mismatch_detail_conceals_both():
    """필드가 값을 가공(대문자화)해 불일치 → 실패 detail 에도 기대값·실제값 평문이 없다."""
    r, values = await _type_into("비밀번호 확인")
    assert values[1] == SECRET.upper(), "대조: 필드가 값을 가공했다"
    assert r.success is False
    assert "길이" in (r.error_message or ""), r.error_message
    _assert_no_plaintext(r)
