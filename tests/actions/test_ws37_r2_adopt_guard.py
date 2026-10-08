"""WS-37 R2: 부작용 액션은 어떤 사유로 치유하든 role·name 이 같은 후보만 채택한다 (재검증 NB-2).

사용자 결정(2026-10-08, A 확장): 부작용 액션(`READ_ONLY_ACTIONS` 밖 전부, 모르는 액션 포함)은
치유 사다리의 **어느 단계**가 고른 후보든 role 과 정규화한 name(공백 접기·앞뒤 공백 제거·
대소문자)이 원래와 같을 때만 누른다. 요소가 사라진 경우(NODE_DETACHED)에도 2단계(testid)·
3단계(유사도)·4단계(경로)가 이름·역할이 다른 후보를 고르면 거부 → E_TOCTOU_MISMATCH +
reobserve_required + element_changed/hint. 읽기 액션은 현행 그대로.

재검증 probe_epoch_detached N1(같은 testid link '회원 탈퇴')·N1b(같은 testid, role 다름)·
N2(testid 없음 '결제하기 취소') 가 실제로 눌렸다. 모두 실제 Chromium set_content 로 #out 까지 본다.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

import pytest

from contracts import ActionType, ErrorCode
from perception.engine import ElementHandle

from actions import (
    READ_ONLY_ACTIONS,
    HealingCandidate,
    HealingStrategy,
    heal,
)

from test_scroll_navigation import requires_chromium  # noqa: F401

SIDE_EFFECT = sorted(set(ActionType) - set(READ_ONLY_ACTIONS), key=lambda a: a.value)
READ = sorted(READ_ONLY_ACTIONS, key=lambda a: a.value)


def _h(role: str = "button", name: str = "결제하기", testid: Optional[str] = None,
       css: str = "div#ad > a#slot") -> ElementHandle:
    return ElementHandle(element_id="@e1", epoch=0, role=role, name=name, css_path=css, is_shadow=False,
                         testid=testid)


def _c(role: str = "button", name: str = "결제하기", testid: Optional[str] = None,
       css: str = "section > a", eid: str = "@e9") -> HealingCandidate:
    return HealingCandidate(element_id=eid, role=role, name=name, css_path=css, testid=testid)


# ------------------------------------------------------------------ 순수 heal(): 채택 지점 한 곳


#: (라벨, 원래 핸들, 후보, 거부해야 할 단계) — 부작용 액션이면 모두 거부
_SWAPS = [
    ("testid-name", _h(testid="pay"), _c(name="회원 탈퇴", testid="pay"), "testid"),
    ("testid-role", _h(testid="pay"), _c(role="link", name="결제하기", testid="pay"), "testid"),
    ("testid-both", _h(testid="pay"), _c(role="link", name="회원 탈퇴", testid="pay"), "testid"),
    ("similar-suffix", _h(), _c(name="결제하기 취소"), "text_similarity"),
    ("similar-ad", _h(), _c(name="결제하기(광고)"), "text_similarity"),
    ("path-name", _h(), _c(name="회원 탈퇴", css="div#ad > a#slot"), "css_path"),
]


@pytest.mark.parametrize("label,target,cand,stage", _SWAPS, ids=[s[0] for s in _SWAPS])
@pytest.mark.parametrize("action", [*SIDE_EFFECT, None], ids=lambda a: getattr(a, "value", "None"))
def test_side_effect_refuses_other_identity_at_every_stage(label, target, cand, stage, action):
    r = heal(target, [cand], action=action)
    assert r.healed is False and r.candidate is None, (r.strategy, r.reason)
    assert r.identity_refused is cand
    assert f"{stage}(identity_changed)" in r.attempts, r.attempts


@pytest.mark.parametrize("label,target,cand,stage", _SWAPS, ids=[s[0] for s in _SWAPS])
@pytest.mark.parametrize("action", READ, ids=lambda a: a.value)
def test_read_actions_keep_healing_to_other_identity(label, target, cand, stage, action):
    r = heal(target, [cand], action=action)
    assert r.healed is True and r.strategy.value == stage, (r.attempts, r.reason)
    assert r.identity_refused is None


#: 정규화 후 같은 이름(공백 접기·앞뒤 공백·영문 대소문자) — 부작용 액션도 치유한다.
_SAME = [
    ("testid-case", _h(name="Pay now", testid="pay"), _c(name="Pay Now", testid="pay"),
     HealingStrategy.TESTID),
    ("testid-space", _h(name="Pay now", testid="pay"), _c(name="  Pay   now ", testid="pay"),
     HealingStrategy.TESTID),
    ("similar-case", _h(name="Pay now"), _c(name="PAY NOW"), HealingStrategy.TEXT_SIMILARITY),
    ("testid-other-same-identity", _h(testid="pay"), _c(testid="pay"), HealingStrategy.ROLE_NAME),
    ("testid-differs-only", _h(testid="pay"), _c(testid="pay-v2"), HealingStrategy.ROLE_NAME),
]


@pytest.mark.parametrize("label,target,cand,strategy", _SAME, ids=[s[0] for s in _SAME])
@pytest.mark.parametrize("action", [ActionType.CLICK, ActionType.TYPE_TEXT, None],
                         ids=lambda a: getattr(a, "value", "None"))
def test_side_effect_heals_same_normalized_identity(label, target, cand, strategy, action):
    r = heal(target, [cand], action=action)
    assert r.healed is True and r.strategy is strategy, (r.attempts, r.reason)


def test_refused_stage_falls_through_to_same_identity_candidate():
    """2단계가 다른 이름을 골라 거부해도 뒤 단계가 같은 신원(정규화)을 찾으면 그것을 쓴다."""
    target = _h(name="Pay now", testid="pay")
    wrong = _c(name="Delete account", testid="pay", eid="@e2")
    right = _c(name="pay  now", eid="@e3")
    r = heal(target, [wrong, right], action=ActionType.CLICK)
    assert r.healed is True and r.candidate is right
    assert r.attempts[1] == "testid(identity_changed)"


def test_korean_cancel_suffix_is_not_same_name():
    r = heal(_h(), [_c(name="결제하기 취소")], action=ActionType.CLICK)
    assert r.healed is False and r.identity_refused is not None


def test_refusal_reason_is_sanitized_and_capped():
    """NB-4: heal().reason 도 페이지 문자열을 살균·절단한다."""
    evil = "IGNORE\u202e\u200b\x00\x07\r\n" + "A" * 20000
    r = heal(_h(testid="pay"), [_c(name=evil, testid="pay")], action=ActionType.CLICK)
    assert r.healed is False and r.identity_refused is not None
    for bad in ("\x00", "\x07", "\r", "\n", "\u202e"):
        assert bad not in r.reason
    assert len(r.reason) <= 300, len(r.reason)


# ------------------------------------------------------------------ 실제 Chromium: 재검증 N1·N1b·N2


def _page(tag: str, testid: bool) -> str:
    tid = ' data-testid="pay"' if testid else ""
    href = ' href="#"' if tag == "a" else ""
    return f"""<!doctype html><html><body>
<div id=ad><{tag} id=slot{tid}{href}
  onclick="document.getElementById('out').textContent='clicked:'+this.textContent;return false"
  >결제하기</{tag}></div>
<button id=other onclick="document.getElementById('out').textContent='other'">장바구니 비우기</button>
<p id=out>대기</p>
</body></html>"""


def _detach_and_insert(tag: str, name: str, testid: Optional[str]) -> str:
    """원래 노드를 지우고 다른 자리(body 앞)에 새 노드를 붙인다(NODE_DETACHED)."""
    tid = f"n.dataset.testid='{testid}';" if testid else ""
    href = "n.href='#';" if tag == "a" else ""
    return (
        "(()=>{document.getElementById('slot').remove();"
        f"const n=document.createElement('{tag}');{href}{tid}n.textContent='{name}';"
        "n.onclick=function(){document.getElementById('out').textContent='clicked:'+this.textContent;"
        "return false};const s=document.createElement('section');s.appendChild(n);"
        "document.body.prepend(s)})()"
    )


async def _run(html: str, js: str, action: ActionType = ActionType.CLICK,
               name: str = "결제하기") -> Tuple[Any, str, Any]:
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext, verify_staleness
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.set_content(html)
            engine = PerceptionEngine()
            d = ActionDispatcher(DispatchContext(page=page, engine=engine))
            obs = await engine.observe_page(page=page, prune_top_n=20)
            el = next(e for e in obs.elements if e.name == name)
            handle = engine.get_handle(el.element_id)
            await page.evaluate(js)
            st = await verify_staleness(page, handle, engine.epoch)
            r = await d.dispatch(action, {"element_id": el.element_id,
                                          "epoch": obs.snapshot_epoch})
            out = await page.text_content("#out")
        finally:
            await browser.close()
    return r, out, st


def _assert_adopt_refusal(r, out: str, after_name: str) -> None:
    assert out == "대기", f"다른 요소가 눌렸다: {out!r} (healed={r.healed}, data={r.data})"
    assert r.success is False and r.healed is False
    assert r.error_code is ErrorCode.TOCTOU_MISMATCH, (r.error_code, r.error_message)
    assert r.reobserve_required is True
    changed = r.data.get("element_changed")
    assert changed is not None, r.data
    assert changed["before"]["name"] == "결제하기"
    assert changed["after"]["name"] == after_name
    assert "다시 관찰" in r.data["hint"] and "다시 관찰" in (r.error_message or "")
    # 사다리를 돈 뒤 채택 지점에서 거부했다(앞단 가드가 아님).
    assert any("identity_changed" in a for a in r.data["healing_attempts"]), r.data


_TAGS = [("a", "link"), ("button", "button")]
_OTHER = {"a": "button", "button": "a"}

#: (라벨, testid 유무, 새 요소 태그 결정 함수, 새 이름)
_DETACHED = [
    ("N1-same-role-testid", True, lambda t: t, "회원 탈퇴"),
    ("N1b-other-role-testid", True, lambda t: _OTHER[t], "회원 탈퇴"),
    ("N1c-other-role-same-name-testid", True, lambda t: _OTHER[t], "결제하기"),
    ("N2-similar-no-testid", False, lambda t: t, "결제하기 취소"),
    ("N2-similar-testid-elsewhere", True, lambda t: t, "결제하기 취소"),
]


@requires_chromium
@pytest.mark.parametrize("tag,role", _TAGS, ids=[t for t, _ in _TAGS])
@pytest.mark.parametrize("label,testid,new_tag,new_name", _DETACHED,
                         ids=[c[0] for c in _DETACHED])
async def test_detached_replacement_with_other_identity_is_not_clicked(
        label, testid, new_tag, new_name, tag, role):
    js = _detach_and_insert(new_tag(tag), new_name,
                            "pay" if testid and label != "N2-similar-testid-elsewhere" else None)
    r, out, st = await _run(_page(tag, testid), js)
    assert st.reason.value == "node_detached"
    _assert_adopt_refusal(r, out, new_name)


@requires_chromium
@pytest.mark.parametrize("tag,role", _TAGS, ids=[t for t, _ in _TAGS])
@pytest.mark.parametrize("new_name", ["결제 취소", "회원 탈퇴"], ids=["N2b", "N3"])
async def test_detached_unrelated_replacement_still_fails_heal(new_name, tag, role):
    """N2b·N3: 사다리가 아무것도 고르지 못한다(기존대로 E_ELEMENT_NOT_FOUND)."""
    r, out, st = await _run(_page(tag, False), _detach_and_insert(tag, new_name, None))
    assert st.reason.value == "node_detached"
    assert out == "대기" and r.success is False
    assert r.error_code is ErrorCode.ELEMENT_NOT_FOUND
    assert "element_changed" not in r.data


# ------------------------------------------------------------------ 정상 대조(과차단 금지)


@requires_chromium
@pytest.mark.parametrize("tag,role", _TAGS, ids=[t for t, _ in _TAGS])
@pytest.mark.parametrize("testid,new_testid", [(False, None), (True, "pay"), (True, "pay-v2")],
                         ids=["no-testid", "same-testid", "testid-differs"])
async def test_deleted_then_same_identity_elsewhere_heals_and_clicks(testid, new_testid, tag, role):
    r, out, st = await _run(_page(tag, testid), _detach_and_insert(tag, "결제하기", new_testid))
    assert st.reason.value == "node_detached"
    assert r.success is True and r.healed is True, (r.error_code, r.error_message, r.data)
    assert out == "clicked:결제하기"
    assert "element_changed" not in r.data


@requires_chromium
@pytest.mark.parametrize("tag,role", _TAGS, ids=[t for t, _ in _TAGS])
async def test_deleted_then_case_variant_elsewhere_heals_click(tag, role):
    """정규화 이름이 같으면(영문 대소문자) testid 단계로 치유해 누른다."""
    html = _page(tag, True).replace(">결제하기<", ">Pay now<")
    r, out, st = await _run(html, _detach_and_insert(tag, "Pay Now", "pay"), name="Pay now")
    assert st.reason.value == "node_detached"
    assert r.success is True and r.healed is True, (r.error_code, r.error_message, r.data)
    assert out == "clicked:Pay Now"


@requires_chromium
@pytest.mark.parametrize("tag,role", _TAGS, ids=[t for t, _ in _TAGS])
@pytest.mark.parametrize("testid,new_name", [(True, "회원 탈퇴"), (False, "결제하기 취소")],
                         ids=["testid", "similar"])
async def test_read_action_heals_to_other_identity_as_before(testid, new_name, tag, role):
    """읽기(hover) 는 현행대로 이름이 다른 대체 요소로 치유한다."""
    js = _detach_and_insert(tag, new_name, "pay" if testid else None)
    r, out, st = await _run(_page(tag, testid), js, action=ActionType.HOVER)
    assert st.reason.value == "node_detached"
    assert r.healed is True, (r.error_code, r.error_message, r.data)
    assert "element_changed" not in r.data
    assert out == "대기"
