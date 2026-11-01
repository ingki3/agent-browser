"""WS-76 R1 (#204 검증 B1~B4): 휴지통·스팸·차단·비우기 판정의 과잉·미탐 보정 (assess_risk 단위).

* B1 과잉: 단독 `차단`·`비우기` 는 HIGH 그대로지만 **탐색·편집 문맥이 확인될 때만** 기준 등급 —
  상태 필터 탭(`tab_context`: role=tab/tablist 안·aria-selected·aria-controls)의 `차단`, 텍스트 입력칸 옆
  (`edit_context`: 같은 label·form·이웃 형제)의 `비우기`(검색어 지우기). 문맥 신호는 dispatcher gateSignals 의
  META 원천(키워드 매칭 안 함). 문맥이 없으면 HIGH(fail-closed).
* B2 과잉: 부정·안내(`… 취소`·`… 안내`·`How to …`·`… 방법`·`도움말`)는 활용형·역추적 뒤에도 기준 등급.
* B3 미탐: 활용형(`옮겨`·`보내기`·`버려`)·`Block "Alice"`(따옴표·@·메일 주소 대상)·동작 역할의 단독 `Trash`.
* B4 미탐: 목적지가 **항목 조작**(id 조각 뒤 /trash·/spam·/block, 쿼리 action=trash|spam|block)이면 HIGH,
  폴더 탐색(/mail/trash·/spam·?folder=trash)은 기준 그대로.
"""

from __future__ import annotations

import pytest

from contracts import ActionType
from security.hitl import ActionContext, RiskLevel, assess_risk


def _risk(name: str = "", signals=()) -> RiskLevel:
    return assess_risk(ActionContext(action=ActionType.CLICK, element_name=name, signals=tuple(signals))).risk


TAB = ("tab_context", "1")
EDIT = ("edit_context", "1")
BUTTON = ("role", "button")

# ---------------------------------------------------------------------------- B1
B1_RISKY = [
    ("차단", ()), ("비우기", ()), ("차단", (BUTTON,)), ("비우기", (BUTTON, ("text", "비우기"))),
    # 편집 예외는 비우기만, 탭 예외는 차단만.
    ("차단", (EDIT,)), ("비우기", (TAB,)),
    # 단독 낱말이 아닌 원천은 예외가 덮지 않는다(aria 가 대상을 말함).
    ("차단", (TAB, ("aria", "이 사용자 차단"))), ("비우기", (EDIT, ("aria", "휴지통 비우기"))),
    ("차단하기", (TAB,)),
]
B1_CONTROLS = [
    ("차단", (TAB,)), ("차단", (("role", "tab"), TAB)), ("차단", (TAB, ("text", "차단"))),
    ("비우기", (EDIT,)), ("비우기", (BUTTON, EDIT, ("text", "비우기"))), ("비우기", (EDIT, ("value", "비우기"))),
    ("", (TAB, ("text", "차단"))), ("", (EDIT, ("aria", "비우기"))),
]


@pytest.mark.parametrize("name,signals", B1_RISKY)
def test_b1_lone_label_without_nav_or_edit_context_is_high(name, signals):
    assert _risk(name, signals) is RiskLevel.HIGH, (name, signals)


@pytest.mark.parametrize("name,signals", B1_CONTROLS)
def test_b1_lone_label_in_tab_or_edit_context_keeps_baseline(name, signals):
    assert _risk(name, signals) is RiskLevel.LOW, (name, signals)


def test_b1_context_sources_are_meta_not_keywords():
    """문맥 신호 값 자체는 키워드로 매칭하지 않는다(값이 위험 낱말이어도)."""
    assert _risk("보기", [("tab_context", "차단"), ("edit_context", "휴지통 비우기"), ("role", "Move to trash")]) \
        is RiskLevel.LOW


# ---------------------------------------------------------------------------- B2
B2_CONTROLS = (
    "휴지통 비우기 취소", "휴지통 비우기 안내", "스팸함 비우기 안내", "스팸 신고하기 안내", "How to report spam",
    "휴지통으로 이동 방법", "도움말: 스팸 신고", "사용자 차단하기 취소", "Move to trash help center",
    "How to block a user", "Learn how to empty trash", "스팸으로 보내기 안내", "휴지통 비우기 방법 보기",
    "장바구니를 비우기 취소", "Report spam (help)",
)
B2_STILL_RISKY = ("휴지통 비우기", "스팸 신고하기", "Report spam", "사용자 차단하기", "Empty trash", "휴지통 비우기 실행")


@pytest.mark.parametrize("name", B2_CONTROLS)
def test_b2_negation_and_guidance_after_inflection_keep_baseline(name):
    assert _risk(name) is RiskLevel.LOW, name


@pytest.mark.parametrize("name", B2_STILL_RISKY)
def test_b2_plain_phrases_stay_high(name):
    assert _risk(name) is RiskLevel.HIGH, name


# ---------------------------------------------------------------------------- B3
B3_RISKY = [
    ("스팸으로 보내기", ()), ("휴지통으로 옮겨", ()), ("휴지통으로 옮김", ()), ("스팸함으로 보내기", ()),
    ("휴지통에 버려", ()), ("스팸으로 옮기기", ()), ("스팸메일로 이동", ()),
    ('Block "Alice"', ()), ("Block \u201cBob Smith\u201d", ()), ("Block @alice", ()), ("Block alice@example.com", ()),
    ('"Alice" 차단', ()), ("Alice님 차단하기", ()),
    ("Trash", (BUTTON,)), ("trash", (("role", "button"), ("text", "Trash"))), ("", (BUTTON, ("aria", "Trash"))),
]
B3_CONTROLS = [
    ("Trash", (("role", "link"),)), ("Trash", (("role", "tab"),)), ("Trash", (("role", "menuitem"),)),
    ("Trash", (("role", "treeitem"),)), ("Trash", ()), ("Trash (3)", (BUTTON,)), ("스팸함", (BUTTON,)),
    ("Spam", (BUTTON,)), ('Unblock "Alice"', ()), ("Block", ()), ("스팸함 보기", ()), ("휴지통으로", ()),
    ("Block Ads", ()), ("Block Cookies", ()), ("Block Images", ()), ("Block Layout", ()), ("Block quote", ()),
    ("Block all", ()), ("Unblock Alice", ()),
]


@pytest.mark.parametrize("name,signals", B3_RISKY)
def test_b3_inflections_quoted_targets_and_trash_action_are_high(name, signals):
    assert _risk(name, signals) is RiskLevel.HIGH, (name, signals)


@pytest.mark.parametrize("name,signals", B3_CONTROLS)
def test_b3_controls_keep_baseline(name, signals):
    assert _risk(name, signals) is RiskLevel.LOW, (name, signals)


# ---------------------------------------------------------------------------- B4
B4_RISKY = [
    ("href", "/messages/42/trash"), ("href", "/messages/42/spam"), ("href", "/mail/abc123def/trash"),
    ("href", "/threads/42/block"), ("form_action", "/messages/42/trash"), ("formaction", "/api/msg/42/junk"),
    ("href", "/mail?action=trash&id=42"), ("href", "/mail/42?op=spam"), ("href", "https://mail.test/u/7/block"),
    ("form_action", "/m/6f1c2a9e-0b1d-4c3e-9f00-1234abcd5678/spam"),
]
B4_CONTROLS = [
    ("href", "/mail/trash"), ("href", "/spam"), ("href", "/folder/Trash"), ("href", "/mail?folder=trash"),
    ("href", "/mail/trash?page=2"), ("href", "/help/42/spam"), ("href", "/messages/42"), ("href", "/messages/42/reply"),
    ("href", "/users/blocked"), ("href", "/mail?action=view&id=42"), ("form_action", "/search"),
]


@pytest.mark.parametrize("source,url", B4_RISKY)
def test_b4_item_operation_destination_is_high(source, url):
    assert _risk("\U0001F5D1", [(source, url)]) is RiskLevel.HIGH, (source, url)


@pytest.mark.parametrize("source,url", B4_CONTROLS)
def test_b4_folder_navigation_destination_keeps_baseline(source, url):
    assert _risk("\U0001F5D1", [(source, url)]) is RiskLevel.LOW, (source, url)


def test_b4_get_form_query_is_not_read():
    """GET 제출은 action 의 쿼리를 보내지 않는다(기존 path_only 규칙) — 쿼리 action=trash 로 올리지 않는다."""
    sig = [("form_action", "/mail?action=trash"), ("form_method", "get")]
    assert _risk("보기", sig) is RiskLevel.LOW
    assert _risk("보기", [("form_action", "/mail?action=trash"), ("form_method", "post")]) is RiskLevel.HIGH


def test_sample_sizes():
    assert len(B1_CONTROLS) >= 5 and len(B2_CONTROLS) >= 5 and len(B3_CONTROLS) >= 5 and len(B4_CONTROLS) >= 5
