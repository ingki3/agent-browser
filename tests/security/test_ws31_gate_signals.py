"""WS-31: HITL 게이트의 문맥 신호 판정 (순수 함수 — 브라우저 없음).

이름에 위험 단어가 없어도 다른 원천(보이는 텍스트·aria·title·alt·value·링크/폼 목적지·
id/class 토큰·CSS 의사요소 글자)에 있으면 고위험이다. 정규화(NFKC·zero-width 제거) 뒤 매칭하고,
판정 근거(gate_basis)를 돌려준다.
"""

from __future__ import annotations

import pytest

from contracts import ActionType, ErrorCode, ExecutionMode
from security import ActionContext, HITLGate, RiskLevel, classify_risk
from security.hitl import (
    ICON_CLASS_RISK,
    assess_risk,
    class_tokens,
    normalize_gate_text,
    path_tokens,
)


def _ctx(name: str = "", signals=(), **kw) -> ActionContext:
    return ActionContext(action=ActionType.CLICK, element_name=name, signals=tuple(signals), **kw)


# ------------------------------------------------------------------ 정규화

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("결\u200b제", "결제"),
        ("결\u200c제", "결제"),
        ("결\u200d제", "결제"),
        ("결\u2060제", "결제"),
        ("결\ufeff제", "결제"),
        ("check\u00adout", "checkout"),
        ("ＣＨＥＣＫＯＵＴ", "checkout"),  # 전각 → NFKC
        ("  결제   진행 \n", "결제 진행"),
    ],
)
def test_normalize_gate_text(raw, expected):
    assert normalize_gate_text(raw) == expected


def test_normalize_does_not_decompose_hangul():
    # 자모 분리 같은 과한 정규화는 하지 않는다 — NFKC 는 완성형 한글을 유지한다.
    assert normalize_gate_text("결제") == "결제"


def test_zero_width_name_is_high():
    risk, _ = classify_risk(_ctx("결\u200b제"))
    assert risk is RiskLevel.HIGH


# ------------------------------------------------------------------ 텍스트형 원천

@pytest.mark.parametrize("source", ["text", "aria", "child_title", "alt", "value", "pseudo"])
def test_text_sources_match_keyword(source):
    a = assess_risk(_ctx("다음", [(source, "결제")]))
    assert a.risk is RiskLevel.HIGH
    assert a.basis == {"name": "다음", "matched_keyword": "결제", "source": source}


def test_own_title_matches_when_name_empty():
    # WS-31 R1: 대상 자신의 title 은 이름에 글자가 없을 때만 본다(tests/security/test_ws31r1_gate_rules.py).
    a = assess_risk(_ctx("", [("title", "결제")]))
    assert a.risk is RiskLevel.HIGH
    assert a.basis == {"name": "", "matched_keyword": "결제", "source": "title"}


def test_aria_and_text_mismatch_both_checked():
    # 이름(aria)='확인', 보이는 글자='결제' — 둘 다 본다.
    a = assess_risk(_ctx("확인", [("aria", "확인"), ("text", "결제")]))
    assert a.risk is RiskLevel.HIGH and a.basis["source"] == "text"
    a = assess_risk(_ctx("다음", [("aria", "결제"), ("text", "다음")]))
    assert a.risk is RiskLevel.HIGH and a.basis["source"] == "aria"


def test_name_match_reports_name_source():
    a = assess_risk(_ctx("결제 진행"))
    assert a.basis == {"name": "결제 진행", "matched_keyword": "결제", "source": "name"}


def test_benign_signals_are_low():
    a = assess_risk(_ctx("로그인", [("text", "로그인"), ("form_action", "/login"),
                                    ("class", "btn btn-primary"), ("href", "/search?q=x")]))
    assert a.risk is RiskLevel.LOW
    assert a.basis["matched_keyword"] is None


# ------------------------------------------------------------------ 경로 신호

@pytest.mark.parametrize(
    "path,hit",
    [
        ("/checkout", "checkout"),
        ("/cart/checkout?step=2", "checkout"),
        ("/pay", "pay"),
        ("/order", "order"),
        ("/account/delete", "delete"),
        ("/purchase/123", "purchase"),
        ("/subscribe", "subscribe"),
        ("/withdraw", "withdraw"),
        ("/api/deleteAccount", "delete"),
        ("/%EA%B2%B0%EC%A0%9C", "결제"),  # 퍼센트 인코딩된 한글 경로
    ],
)
def test_path_tokens_match(path, hit):
    a = assess_risk(_ctx("다음", [("form_action", path)]))
    assert a.risk is RiskLevel.HIGH, path
    assert a.basis["matched_keyword"] == hit
    assert a.basis["source"] == "form_action"


@pytest.mark.parametrize(
    "path",
    ["/payroll-info", "/payment-history", "/orders-faq", "/display", "/search?q=apple",
     "/repayment", "/login", "/admins-guide-xyz".replace("admins", "adminz")],
)
def test_path_word_boundary_avoids_overmatch(path):
    a = assess_risk(_ctx("보기", [("href", path)]))
    assert a.risk is RiskLevel.LOW, (path, a.basis)


def test_path_tokens_split():
    assert path_tokens("/cart/checkout_submit?x=deleteAll") == [
        "cart", "checkout", "submit", "x", "delete", "all",
    ]


# 링크(GET 이동)는 목적지 **마지막 경로 조각**과 **값 전체가 키워드인 쿼리 값**만 본다 —
# 조회 링크(`/order/123` 주문 상세, `?sort=order_date` 정렬)를 막지 않기 위해(WS-31 과차단 측정).
@pytest.mark.parametrize(
    "href",
    ["/order/123", "/list?page=2&sort=order_date", "/orders/123/detail", "/pay/history",
     "/help?topic=payment"],
)
def test_href_view_links_are_low(href):
    assert assess_risk(_ctx("보기", [("href", href)])).risk is RiskLevel.LOW, href


@pytest.mark.parametrize(
    "href,hit",
    [("/account/delete", "delete"), ("/cart/checkout", "checkout"), ("/checkout/", "checkout"),
     ("/api/deleteAccount", "delete"), ("/item/3?action=delete", "delete"),
     ("/order/123/pay", "pay"), ("/%EA%B2%B0%EC%A0%9C", "결제")],
)
def test_href_action_links_are_high(href, hit):
    a = assess_risk(_ctx("보기", [("href", href)]))
    assert a.risk is RiskLevel.HIGH, href
    assert a.basis["matched_keyword"] == hit and a.basis["source"] == "href"


def test_form_action_keeps_full_path():
    # 폼 제출 목적지는 링크보다 엄격 — 경로 전체 토큰.
    assert assess_risk(_ctx("다음", [("form_action", "/order/123")])).risk is RiskLevel.HIGH


# ------------------------------------------------------------------ id/class 토큰·아이콘

@pytest.mark.parametrize(
    "raw,source,hit",
    [
        ("btn-pay", "class", "pay"),
        ("deleteAccount", "id", "delete"),
        ("checkout_submit", "id", "checkout"),
        ("PurchaseButton", "class", "purchase"),
    ],
)
def test_class_and_id_tokens(raw, source, hit):
    a = assess_risk(_ctx("", [(source, raw)]))
    assert a.risk is RiskLevel.HIGH
    assert a.basis["matched_keyword"] == hit and a.basis["source"] == source


# WS-31 R1: 장바구니 아이콘(icon-cart 등)은 사전에서 뺐다 — '장바구니 보기'는 결제가 아니다.
@pytest.mark.parametrize("cls", ["fa fa-trash", "icon-credit-card", "bi bi-credit-card", "fa-trash-can"])
def test_icon_class_dictionary(cls):
    a = assess_risk(_ctx("", [("class", cls)]))
    assert a.risk is RiskLevel.HIGH, cls
    assert a.basis["source"] == "class"


@pytest.mark.parametrize("cls", ["btn btn-primary", "nav-link active", "fa fa-search", "icon-home",
                                 "display-4", "payroll-info"])
def test_benign_classes_low(cls):
    assert assess_risk(_ctx("", [("class", cls)])).risk is RiskLevel.LOW, cls


def test_class_tokens_split_camel_kebab_snake():
    assert class_tokens("btnPay checkout_submit fa-credit-card") == [
        "btn", "pay", "checkout", "submit", "fa", "credit", "card",
    ]


def test_icon_dictionary_is_small_and_meaningful():
    # 삭제·결제 의미만 — 사전이 커져 일반 아이콘(검색·홈)을 잡으면 과차단이다.
    assert 0 < len(ICON_CLASS_RISK) <= 40
    assert not any("search" in k or "home" in k for k in ICON_CLASS_RISK)


# ------------------------------------------------------------------ 게이트 결과

def test_decision_carries_basis_and_source_word_in_message():
    gate = HITLGate(mode=ExecutionMode.UNATTENDED)
    d = gate.evaluate(_ctx("다음", [("form_action", "/checkout")]))
    assert d.allowed is False and d.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert d.basis == {"name": "다음", "matched_keyword": "checkout", "source": "form_action"}
    assert "form_action" in d.reason


def test_unresolved_basis_source():
    d = HITLGate().evaluate(_ctx("", unresolved_target="좌표에 요소 없음"))
    assert d.allowed is False
    assert d.basis["source"] == "unresolved"


def test_pre_approved_by_name_still_works_with_signals():
    gate = HITLGate(pre_approved_actions=("click:다음",))
    assert gate.evaluate(_ctx("다음", [("form_action", "/checkout")])).allowed is True


def test_signals_default_empty_keeps_old_behaviour():
    # 신호가 없으면 예전과 같다(이름만).
    assert classify_risk(_ctx("다음 페이지"))[0] is RiskLevel.LOW
    assert classify_risk(_ctx("계정 삭제"))[0] is RiskLevel.HIGH
