"""WS-85 (#211, WS-86 리뷰 B2): 안내·취소 낱말은 그 구와 **바로 붙어 있을 때만** 동작이 아니다(LOW 사유).

표는 WS-86 리뷰 lex2(후보 규칙 cand.irr_adjacent 와 같은 결과)를 그대로 옮겼다. 도움말 목적지 링크(`/help/…`)는 예전처럼
앞뒤 어디든 안내로 본다(추가 LOW 사유만).
"""

from __future__ import annotations

import pytest

from contracts import ActionType
from security import ActionContext, RiskLevel
from security.hitl import assess_risk, irreversible_action

#: (문구, 새 규칙 결과 — None 이면 동작 아님)
TABLE = [
    ("안내 확인 후 휴지통 비우기", "empty"), ("휴지통 비우기 지금 실행 안내", "empty"),
    ("Report spam and cancel subscription", "report spam"), ("How to report spam now", "report spam"),
    ("How to report spam in Gmail", "report spam"),
    ("휴지통 비우기 취소", None), ("휴지통 비우기 안내", None), ("스팸 신고하기 안내", None), ("How to report spam", None),
    ("스팸함 비우기 안내", None), ("Report spam (help)", None), ("Report spam help center", None),
    ("Learn how to empty trash", None), ("Move to trash help", None), ("스팸 신고 취소하기", None), ("사용자 차단 목록", None),
    ("차단 사용자 관리", None), ("휴지통 비우기 방법 보기", None), ("Undo move to trash", None),
    ("Report spam? Learn more", None), ("도움말: 휴지통 비우기", None), ("About reporting spam", None),
    ("휴지통 비우기 설정", None), ("Report spam settings", None), ("Block user list", None),
    ("Empty trash", "empty"), ("Report spam", "report spam"), ("휴지통 비우기", "empty"), ("스팸 신고", "report spam"),
    ("휴지통으로 이동", "move to trash"), ("Move to trash", "move to trash"), ("Block this user", "block"),
    ("이 사용자 차단", "block"), ("Empty trash now", "empty"), ("휴지통 비우기 실행", "empty"),
]


@pytest.mark.parametrize("text,expected", TABLE)
def test_guide_words_count_only_when_adjacent(text, expected):
    assert irreversible_action("text", text) == expected


@pytest.mark.parametrize("text", ["안내 확인 후 휴지통 비우기", "How to report spam now", "How to report spam in Gmail"])
def test_help_destination_link_keeps_lenient_guide_rule(text):
    """링크의 도움말 목적지는 LOW 사유를 더할 뿐 — 같은 문구라도 버튼이면 HIGH, /help/ 링크면 예전처럼 LOW."""
    button = assess_risk(ActionContext(action=ActionType.CLICK, element_name=text, signals=(("text", text),)))
    link = assess_risk(ActionContext(action=ActionType.CLICK, element_name=text,
                                     signals=(("text", text), ("href", "/help/report-spam"), ("role", "link"))))
    other = assess_risk(ActionContext(action=ActionType.CLICK, element_name=text,
                                      signals=(("text", text), ("href", "/mail/inbox"), ("role", "link"))))
    assert button.risk is RiskLevel.HIGH and other.risk is RiskLevel.HIGH
    assert link.risk is RiskLevel.LOW


def test_table_size():
    assert len(TABLE) == 35
