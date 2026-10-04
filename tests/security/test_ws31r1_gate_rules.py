"""WS-31 R1: 문맥 신호 규칙 보정 (순수 함수 — 브라우저 없음).

* NB-2 정규화: 유니코드 범주 Cf 전체 + U+034F(CGJ) 제거. 한글·이모지 ZWJ 시퀀스 부작용 없음.
* NB-6 과차단: 같은 출처 이동 링크(nav_link)는 마크업 식별자·title 원천 제외, GET 폼은 action 경로만,
  장바구니 아이콘은 사전에서 제외, 링크 href 의 권한 명사(admin·grant …) 제외, 대상 자신의 title 은
  이름에 글자가 없을 때만.
"""

from __future__ import annotations

import unicodedata

import pytest

from contracts import ActionType
from security import ActionContext, RiskLevel
from security.hitl import ICON_CLASS_RISK, assess_risk, normalize_gate_text


def _ctx(name: str = "", signals=(), **kw) -> ActionContext:
    return ActionContext(action=ActionType.CLICK, element_name=name, signals=tuple(signals), **kw)


def _risk(name: str = "", signals=()) -> RiskLevel:
    return assess_risk(_ctx(name, signals)).risk


# ------------------------------------------------------------------ NB-2 정규화

@pytest.mark.parametrize(
    "ch",
    ["\u200f", "\u200e", "\u2064", "\u061c", "\u034f", "\u2066", "\u202e", "\u180e", "\U000e0001"],
)
def test_invisible_format_chars_removed(ch):
    assert normalize_gate_text(f"결{ch}제") == "결제"
    assert assess_risk(_ctx(f"결{ch}제")).risk is RiskLevel.HIGH


def test_every_cf_char_is_removed():
    import sys

    cf = [chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf"]
    assert len(cf) > 100
    for ch in cf:
        assert normalize_gate_text(f"pa{ch}y") == "pay", hex(ord(ch))


@pytest.mark.parametrize(
    "text",
    ["결제", "계정 삭제", "\u1100\u1167\u11af\u110c\u1166"],  # 완성형·NFD 자모(NFKC 가 합성)
)
def test_hangul_unchanged_by_invisible_strip(text):
    assert normalize_gate_text(text) == unicodedata.normalize("NFKC", text)


@pytest.mark.parametrize(
    "emoji",
    [
        "\U0001F468\u200D\U0001F469\u200D\U0001F467",  # 가족 ZWJ 시퀀스
        "\U0001F3F3\uFE0F\u200D\U0001F308",  # 무지개 깃발(변형 선택자 + ZWJ)
        "\U0001F9D1\u200D\U0001F4BB",  # 기술자
        "\u2764\uFE0F",
    ],
)
def test_emoji_zwj_sequences_no_false_keyword(emoji):
    # ZWJ(Cf)는 지워져 구성 이모지로 풀리지만 이모지는 키워드가 아니다 — 저위험 그대로.
    assert assess_risk(_ctx(f"{emoji} 보기", [("text", emoji)])).risk is RiskLevel.LOW
    assert "\u200d" not in normalize_gate_text(emoji)


def test_variation_selector_kept():
    # 변형 선택자(Mn)는 Cf 가 아니라 남는다(글자 모양에 영향).
    assert "\ufe0f" in normalize_gate_text("\u2764\ufe0f")


# ------------------------------------------------------------------ NB-6 같은 출처 이동 링크

NAV = ("nav_link", "1")


@pytest.mark.parametrize(
    "signals",
    [
        [("class", "fa fa-trash"), ("href", "/trash")],  # x06 휴지통 보기
        [("class", "fa fa-shopping-cart"), ("href", "/cart")],  # x20 헤더 장바구니
        [("testid", "nav-order-history"), ("href", "/history")],  # x25
        [("id", "deleteLink"), ("href", "/items")],
        [("title", "결제 안내"), ("href", "/help")],
        [("child_title", "결제"), ("href", "/help")],
    ],
)
def test_nav_link_skips_markup_and_title(signals):
    assert _risk("보기", [NAV, *signals]) is RiskLevel.LOW
    # 같은 신호가 링크가 아닌 대상(버튼)에서는 여전히 고위험이다(child_title·class·testid·id).
    if signals[0][0] != "title" and "cart" not in signals[0][1]:
        assert _risk("", signals[:1]) is RiskLevel.HIGH


@pytest.mark.parametrize(
    "name,signals",
    [
        ("계정 삭제", []),  # 이름
        ("다음", [("aria", "결제")]),  # aria
        ("다음", [("text", "결제하기")]),  # 보이는 글자
        ("보기", [("href", "/account/delete")]),  # href 마지막 조각(동사)
        ("보기", [("href", "/tokens/5/revoke")]),
        ("보기", [("href", "/cart/checkout")]),
        ("보기", [("alt", "결제")]),
        ("보기", [("pseudo", "삭제")]),
        ("보기", [("svg_title", "삭제")]),
    ],
)
def test_nav_link_keeps_name_aria_href_signals(name, signals):
    assert _risk(name, [NAV, *signals]) is RiskLevel.HIGH


@pytest.mark.parametrize("href", ["/admin", "/scholarships/grant-2025", "/관리자", "/settings/permission",
                                  "/x?tab=admin"])
def test_href_permission_nouns_are_view(href):
    assert _risk("대시보드", [NAV, ("href", href)]) is RiskLevel.LOW
    assert _risk("대시보드", [("href", href)]) is RiskLevel.LOW


@pytest.mark.parametrize(
    "name,signals",
    [
        ("admin", []),  # 버튼 이름 — 명사 제외는 href 에만
        ("관리자 지정", []),
        ("다음", [("form_action", "/admin/grant")]),
        ("다음", [("formaction", "/users/5/admin")]),
        ("다음", [("class", "btn-grant")]),
    ],
)
def test_permission_nouns_still_high_outside_href(name, signals):
    assert _risk(name, signals) is RiskLevel.HIGH


def test_transfer_guide_link_remains_overblocked():
    # 남은 과차단(보고) — 'transfer' 는 송금 동사라 링크에서도 막는다.
    assert _risk("환승 안내", [NAV, ("href", "/stations/transfer-guide")]) is RiskLevel.HIGH


# ------------------------------------------------------------------ NB-6 폼 action

def test_get_form_action_ignores_query():
    sig = [("form_action", "/search?type=order"), ("form_method", "get")]
    assert _risk("검색", sig) is RiskLevel.LOW


@pytest.mark.parametrize(
    "signals",
    [
        [("form_action", "/checkout"), ("form_method", "get")],
        [("form_action", "/search?type=order"), ("form_method", "post")],  # POST 는 쿼리도 본다
        [("form_action", "/search?type=order")],  # 메서드 모름 → 쿼리도 본다
        [("formaction", "/pay?x=1"), ("form_method", "get")],
    ],
)
def test_form_action_still_high(signals):
    assert _risk("다음", signals) is RiskLevel.HIGH


def test_get_search_form_with_risky_name_blocked():
    sig = [("form_action", "/search"), ("form_method", "get")]
    assert _risk("결제", sig) is RiskLevel.HIGH
    assert _risk("검색", [*sig, ("class", "btn-pay")]) is RiskLevel.HIGH


def test_meta_sources_never_match():
    assert _risk("", [("form_method", "delete"), ("nav_link", "pay")]) is RiskLevel.LOW


# ------------------------------------------------------------------ NB-6 cart 아이콘

@pytest.mark.parametrize("cls", ["fa fa-shopping-cart", "fa-cart-shopping", "bi bi-cart", "icon-cart"])
def test_cart_icon_is_not_checkout(cls):
    assert _risk("", [("class", cls)]) is RiskLevel.LOW


def test_cart_keys_not_in_icon_dictionary():
    assert not any("cart" in k for k in ICON_CLASS_RISK)


@pytest.mark.parametrize("cls", ["fa fa-credit-card", "bi bi-trash", "fa-money-bill", "bi-bag-check"])
def test_payment_and_delete_icons_still_high(cls):
    assert _risk("", [("class", cls)]) is RiskLevel.HIGH


# ------------------------------------------------------------------ NB-6 title

def test_own_title_ignored_when_name_has_letters():
    # x13: '저장' 버튼의 title 문장이 결제를 언급 — 보조 설명이라 무시.
    a = assess_risk(_ctx("저장", [("text", "저장"), ("title", "결제는 나중에 할 수 있습니다")]))
    assert a.risk is not RiskLevel.HIGH


@pytest.mark.parametrize("name", ["", "\U0001F4B3", "  ", "→"])
def test_own_title_used_when_name_empty(name):
    a = assess_risk(_ctx(name, [("title", "결제")]))
    assert a.risk is RiskLevel.HIGH and a.basis["source"] == "title"


def test_child_title_used_even_when_named():
    a = assess_risk(_ctx("다음", [("child_title", "결제")]))
    assert a.risk is RiskLevel.HIGH and a.basis["source"] == "child_title"
