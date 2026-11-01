"""WS-76 R2 (#204 재검증 R1-A·R1-B): 일상 탐색 UI 과잉 승인 보정 (assess_risk 단위).

* R1-A: `Block + 대문자 단어`(상품명 `Block Party`·`Block Tower`)는 사람 차단이 아니다 — 대상 지목은 따옴표 이름·
  @아이디·메일 주소·명시 대상 낱말(user·sender·contact·number·사용자·발신자·번호)일 때만 HIGH.
* R1-B: id 뒤 `/trash|/spam|/block` 은 메일·대화 **항목 조작 접두**(messages·mail·threads·conversations…) 뒤일 때만
  HIGH. 폴더·위키·블로그·문서 경로(`/folders/42/spam`·`/wiki/42/block`)는 기준 그대로. 쿼리 action=… 규칙은 유지.
"""

from __future__ import annotations

import pytest

from contracts import ActionType
from security.hitl import ActionContext, RiskLevel, assess_risk


def _risk(name: str = "", signals=()) -> RiskLevel:
    return assess_risk(ActionContext(action=ActionType.CLICK, element_name=name, signals=tuple(signals))).risk


BUTTON = ("role", "button")
LINK = ("role", "link")

# ---------------------------------------------------------------------------- R1-A
R1A_CONTROLS = [
    ("Block Party", (LINK,)), ("Block Tower", (BUTTON,)), ("Block Party", ()), ("Block Tower", ()),
    ("Block Party", (LINK, ("href", "/products/1042"))),
    ("Block Tower Deluxe", (BUTTON,)), ("Block Alice", ()), ("Block Alice Kim", ()),
]
R1A_RISKY = [
    ('Block "Alice"', ()), ("Block \u201cBob Smith\u201d", ()), ("Block @alice", ()), ("Block alice@example.com", ()),
    ("Block user", ()), ("Block this user", ()), ("Block sender", ()), ("Block contact", ()),
    ("Block number 010-1234-5678", ()), ("사용자 차단", ()), ("발신자 차단", ()), ("번호 차단", ()),
]


@pytest.mark.parametrize("name,signals", R1A_CONTROLS)
def test_r1a_capitalized_block_names_keep_baseline(name, signals):
    assert _risk(name, signals) is RiskLevel.LOW, (name, signals)


@pytest.mark.parametrize("name,signals", R1A_RISKY)
def test_r1a_explicit_block_targets_stay_high(name, signals):
    assert _risk(name, signals) is RiskLevel.HIGH, (name, signals)


# ---------------------------------------------------------------------------- R1-B
R1B_CONTROLS = [
    ("href", "/folders/42/spam"), ("href", "/wiki/42/block"), ("href", "/blog/42/trash"), ("href", "/blog/42/spam"),
    ("href", "/docs/42/block"), ("href", "/products/42/block"), ("href", "/folders/abc123/trash"),
    ("form_action", "/wiki/42/block"),
]
R1B_RISKY = [
    ("href", "/messages/42/trash"), ("href", "/message/42/spam"), ("href", "/mail/abc123def/trash"),
    ("href", "/mails/42/junk"), ("href", "/threads/42/block"), ("href", "/thread/7/spam"),
    ("href", "/conversations/9/block"), ("href", "/conversation/9/trash"), ("href", "/inbox/42/trash"),
    ("href", "/mail/u/0/messages/42/trash"), ("href", "/folders/42/spam?action=spam"),
    ("href", "/wiki?op=block&id=42"),
]


@pytest.mark.parametrize("source,url", R1B_CONTROLS)
def test_r1b_non_item_paths_keep_baseline(source, url):
    assert _risk("\U0001F4C1", [(source, url)]) is RiskLevel.LOW, (source, url)


@pytest.mark.parametrize("source,url", R1B_RISKY)
def test_r1b_item_operation_paths_stay_high(source, url):
    assert _risk("\U0001F4C1", [(source, url)]) is RiskLevel.HIGH, (source, url)


def test_sample_sizes():
    assert len(R1A_CONTROLS) >= 6 and len(R1B_CONTROLS) >= 6
