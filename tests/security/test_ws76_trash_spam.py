"""WS-76 (#204): 되돌리기 어려운 메일·계정 조작(휴지통·스팸·차단·비우기)은 고위험(HIGH).

* 위험 표본: 한·영, 띄어쓰기·대소문자·보이지 않는 문자·전각·한 글자씩 띄운 영문 변형 → 전부 HIGH.
* 대조 표본(규칙 3 반대 방향 — 과잉 승인 금지): 폴더 열기(`휴지통`·`Trash`·`Spam`)·안내·레이아웃 이름·복원 계열
  (`스팸 아님`·`Not spam`·`차단 해제`·`Unblock`·`휴지통에서 복원`·`Restore`)은 기준(44db63c) 등급 그대로.
  복원 계열은 기준에서 LOW(되돌리는 쪽 동작 — '삭제'처럼 키워드가 든 경우만 HIGH 였다)라 그대로 둔다.
* 문맥 신호(aria·title·의사요소·식별자·목적지)에서도 같은 판정, 같은 출처 이동 링크의 마크업 신호 완화는 유지.
"""

from __future__ import annotations

import pytest

from contracts import ActionType
from security.hitl import ActionContext, RiskLevel, assess_risk, match_signal

RISKY = (
    # 한국어
    "휴지통으로 이동", "휴지통으로이동", "휴지통에 버리기", "휴지통 비우기", "휴지통을 비우기", "전체 비우기",
    "장바구니 비우기", "비우기", "스팸신고", "스팸 신고", "스팸으로 표시", "스팸 처리", "차단", "차단하기",
    "사용자 차단", "이 번호 차단", "발신자 차단하기", "계정 차단",
    "휴\u200b지통으로 이동",  # zero-width space
    "스팸\u3000신고",  # 전각 공백
    # 영어
    "Move to trash", "move to Trash", "MOVE TO TRASH", "Move to Bin", "moveToTrash", "Trash this message",
    "Empty trash", "Empty Trash now", "Report spam", "Report as spam", "Re\u200bport spam", "Mark as spam",
    "Mark as junk", "Block user", "Block this user", "Block sender", "Block number", "r e p o r t s p a m",
    "Send to trash", "Empty cart", "Move conversation to Trash",
    "\uff52\uff45\uff50\uff4f\uff52\uff54 \uff53\uff50\uff41\uff4d",  # 전각 영문 'report spam'
)

#: 대조 표본 → 기준(44db63c) 등급. 수집: 이 파일의 표본을 기준 커밋의 assess_risk 로 판정한 값.
CONTROLS = {
    "휴지통": "low", "스팸함": "low", "Trash": "low", "Spam": "low", "광고 차단 안내": "low", "blockquote": "low",
    "Block": "low", "Empty state": "low", "비우기 안내": "low", "스팸 아님": "low", "Not spam": "low",
    "차단 해제": "low", "Unblock": "low", "휴지통에서 복원": "low", "Restore": "low", "휴지통 보기": "low",
    "스팸메일함": "low", "Junk": "low", "차단 목록": "low", "Blocked users": "low", "스팸 신고 안내": "low",
    "Mark as not spam": "low", "Unblock user": "low", "Move to inbox": "low", "받은편지함으로 이동": "low",
    "Report a bug": "low", "Trash folder": "low", "사용자 차단 해제": "low", "Undo": "low",
    "Restore from trash": "low", "스팸 차단 설정": "low", "차단된 사용자": "low", "Blocking": "low",
    "Empty": "low", "Empty folder": "low", "Not junk": "low", "Trash (3)": "low", "Spam (12)": "low",
    "스팸 신고 취소": "low", "Undo move to trash": "low", "Block layout": "low", "광고차단": "low",
    "Content block": "low",
    # 기준에서 이미 HIGH — 그대로(키워드 '삭제'·'delete').
    "삭제 취소": "high", "영구 삭제": "high", "Delete": "high",
}


def _risk(name: str = "", signals=(), action: ActionType = ActionType.CLICK) -> RiskLevel:
    return assess_risk(ActionContext(action=action, element_name=name, signals=tuple(signals))).risk


def test_sample_sizes():
    assert len(RISKY) >= 24 and len(CONTROLS) >= 24


@pytest.mark.parametrize("name", RISKY)
def test_risky_names_are_high(name):
    assert _risk(name) is RiskLevel.HIGH, name
    assert match_signal("name", name), name


@pytest.mark.parametrize("name,level", sorted(CONTROLS.items()))
def test_controls_keep_baseline_level(name, level):
    assert _risk(name).value == level, name


@pytest.mark.parametrize("source,text", [
    ("aria", "Move to trash"), ("text", "스팸신고"), ("pseudo", "휴지통 비우기"), ("value", "Report spam"),
    ("child_aria", "Block sender"), ("alt", "차단하기"), ("svg_title", "Empty trash"),
    ("testid", "move-to-trash"), ("id", "emptyTrash"), ("class", "btn btn-report-spam"),
    ("name_attr", "mark_as_spam"), ("form_action", "/mail/move-to-trash"), ("href", "/mail/123/report-spam"),
])
def test_context_signal_sources_are_high(source, text):
    """이름이 비어 있는 아이콘 버튼 — 문맥 신호만으로 HIGH."""
    assert _risk("", [(source, text)]) is RiskLevel.HIGH, (source, text)


def test_title_only_for_icon_button():
    assert _risk("\U0001F5D1", [("title", "휴지통으로 이동")]) is RiskLevel.HIGH
    # 글자가 있는 이름이면 대상 자신의 title 은 보조 설명(기존 규칙 그대로).
    assert _risk("보기", [("title", "휴지통으로 이동")]) is RiskLevel.LOW


@pytest.mark.parametrize("source,text", [
    ("href", "/mail/trash"), ("href", "/mail/spam?page=2"), ("href", "/settings/blocked-users"),
    ("class", "folder-trash nav-spam"), ("testid", "trash-folder"), ("id", "blockquote"),
    ("class", "layout-block empty-state"), ("href", "/help/report-spam"), ("form_action", "/mail/not-spam"),
    ("aria", "휴지통"), ("text", "Spam"), ("pseudo", "비우기 안내"),
])
def test_context_controls_stay_low(source, text):
    assert _risk("", [(source, text)]) is RiskLevel.LOW, (source, text)


def test_nav_link_markup_relief_kept():
    """같은 출처 이동 링크의 class·testid 는 보지 않는다(WS-31 R1) — 링크 이름이 폴더면 LOW."""
    sig = [("nav_link", "1"), ("class", "btn-move-to-trash"), ("href", "/mail/trash")]
    assert _risk("휴지통", sig) is RiskLevel.LOW
    # 이름이 조작이면 링크여도 HIGH(이름 원천은 링크 완화 대상이 아니다).
    assert _risk("휴지통 비우기", [("nav_link", "1"), ("href", "/mail/trash")]) is RiskLevel.HIGH


def test_basis_names_keyword_and_source():
    a = assess_risk(ActionContext(action=ActionType.CLICK, element_name="", signals=(("aria", "Report spam"),)))
    assert a.risk is RiskLevel.HIGH
    assert a.basis["source"] == "aria" and a.basis["matched_keyword"]
