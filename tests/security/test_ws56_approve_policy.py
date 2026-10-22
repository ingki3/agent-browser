"""WS-56 — 사이트별 상시 허용 정책(approve-policy) 저장·일치·최고위험 범주 (브라우저 없음).

* 정책 = (origin, 액션, [대상 이름]) — 사람 CLI 만 추가한다(MCP·서버 도구에 추가 수단 없음).
* 프로필 안 0600 파일. 손상·권한 느슨함·심볼릭 링크 → 정책 없음(fail-closed).
* 최고위험 범주(돈 이동·계정 해지·권한 변경·판정 불가·upload_file)는 정책으로 열 수 없다 —
  matched_keyword 하나가 아니라 이름·selector·문맥 신호 전 원천을 다시 훑는다.

값은 합성 값. 공개 사이트 없음.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from contracts import ActionType
from security import ActionContext
from security import approve_policy as ap
from security.hitl import HIGH_RISK_KEYWORDS


ORIGIN = "https://shop.test"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    d = tmp_path / "serve-p1"
    d.mkdir(mode=0o700)
    return d / ap.POLICY_FILE


# ------------------------------------------------------------------ 범주 목록 고정 (조정자 승인 2026-10-18)


def test_forbidden_categories_are_fixed():
    assert ap.FORBIDDEN_KEYWORDS == {
        "payment": ("결제", "구매", "주문", "송금", "이체", "출금", "pay", "purchase", "checkout",
                    "order", "transfer", "withdraw", "subscribe"),
        "account_termination": ("탈퇴", "해지", "deactivate", "cancel account", "terminate"),
        "permission": ("권한", "관리자", "permission", "admin", "grant", "revoke"),
    }
    assert ap.CATEGORY_UNRESOLVED == "unresolved_target"
    assert ap.FORBIDDEN_ACTIONS == {ActionType.UPLOAD_FILE: "upload_file"}
    # 모두 기존 HIGH 판정 키워드의 부분집합(새 위험 판정을 만들지 않는다)
    for words in ap.FORBIDDEN_KEYWORDS.values():
        assert set(words) <= set(HIGH_RISK_KEYWORDS)


@pytest.mark.parametrize("name", ["제출", "확인", "동의하고 계속", "Submit", "삭제", "Delete mail", "remove item",
                                  "다음", "approve request"])
def test_openable_names_are_not_forbidden(name):
    assert ap.forbidden_category(ActionContext(action=ActionType.CLICK, element_name=name)) is None


@pytest.mark.parametrize("name,cat", [("결제하기", "payment"), ("Pay now", "payment"),
                                      ("회원 탈퇴", "account_termination"),
                                      ("관리자 권한 부여", "permission"), ("Grant access", "permission")])
def test_forbidden_name(name, cat):
    assert ap.forbidden_category(ActionContext(action=ActionType.CLICK, element_name=name)) == cat


def test_all_sources_rescanned_not_only_matched_keyword():
    """'제출' 버튼(matched_keyword=제출)이라도 문맥 신호에 '결제'가 있으면 거부."""
    ctx = ActionContext(action=ActionType.CLICK, element_name="제출",
                        signals=(("text", "제출하면 결제가 진행됩니다"),))
    assert ap.forbidden_category(ctx) == "payment"
    # 한 원천 안에 두 키워드(submit 이 먼저 맞는 문장)도 끝까지 본다
    ctx2 = ActionContext(action=ActionType.CLICK, element_name="submit and make admin")
    assert ap.forbidden_category(ctx2) == "permission"


@pytest.mark.parametrize("source,text,cat", [
    ("href", "/cart/checkout", "payment"),
    ("form_action", "/account/deactivate", "account_termination"),
    ("class", "fa-credit-card", "payment"),
    ("testid", "grantRoleButton", "permission"),
    ("selector", None, None),
])
def test_forbidden_in_markup_sources(source, text, cat):
    if source == "selector":
        ctx = ActionContext(action=ActionType.CLICK, element_name="확인", selector="#btn-pay")
        assert ap.forbidden_category(ctx) == "payment"
        return
    ctx = ActionContext(action=ActionType.CLICK, element_name="확인", signals=((source, text),))
    assert ap.forbidden_category(ctx) == cat


def test_unresolved_and_upload_are_forbidden():
    assert ap.forbidden_category(ActionContext(action=ActionType.CLICK, element_name="제출",
                                               unresolved_target="selector 대상 여러 개")) == "unresolved_target"
    assert ap.forbidden_category(ActionContext(action=ActionType.UPLOAD_FILE)) == "upload_file"


# ------------------------------------------------------------------ 저장·0600·손상


def test_add_list_remove_and_mode(path):
    entry = ap.add_policy(path, "HTTPS://Shop.Test/", "click", "제출")
    assert entry["origin"] == ORIGIN and entry["action"] == "click" and entry["target_name"] == "제출"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    ap.add_policy(path, ORIGIN, "press_key")
    rows = ap.read_policies(path)
    assert [(r["origin"], r["action"], r.get("target_name")) for r in rows] == [
        (ORIGIN, "click", "제출"), (ORIGIN, "press_key", None)]
    # 같은 정책은 중복 저장하지 않는다
    ap.add_policy(path, ORIGIN, "click", "제출")
    assert len(ap.read_policies(path)) == 2
    assert ap.remove_policy(path, ORIGIN, "click", "제출") is True
    assert ap.remove_policy(path, ORIGIN, "click", "제출") is False
    assert [r["action"] for r in ap.read_policies(path)] == ["press_key"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.parametrize("origin", ["ftp://shop.test", "shop.test", "https://shop.test/path", "https://shop.test/?q=1",
                                    "https://user:pw@shop.test", "javascript:alert(1)", ""])
def test_bad_origin_rejected(path, origin):
    with pytest.raises(ap.PolicyError):
        ap.add_policy(path, origin, "click")


def test_forbidden_policies_rejected_at_add(path):
    with pytest.raises(ap.PolicyError, match="upload_file"):
        ap.add_policy(path, ORIGIN, "upload_file")
    with pytest.raises(ap.PolicyError, match="payment"):
        ap.add_policy(path, ORIGIN, "click", "결제하기")
    with pytest.raises(ap.PolicyError):
        ap.add_policy(path, ORIGIN, "fly_away")
    assert not path.exists()


def test_missing_file_is_empty(path):
    assert ap.read_policies(path) == []


@pytest.mark.parametrize("body", ["{not json", json.dumps([1, 2]), json.dumps({"version": 1, "policies": [{"x": 1}]}),
                                  json.dumps({"version": 1, "policies": [{"origin": "shop.test", "action": "click"}]}),
                                  json.dumps({"version": 99, "policies": []})])
def test_corrupt_file_fails_closed(path, body):
    path.write_text(body)
    os.chmod(path, 0o600)
    with pytest.raises(ap.PolicyError):
        ap.read_policies(path)
    store = ap.PolicyStore(path)
    assert store.match(ORIGIN, ActionType.CLICK, "제출") is None
    assert store.error


def test_loose_permissions_fail_closed(path):
    ap.add_policy(path, ORIGIN, "click")
    os.chmod(path, 0o644)
    with pytest.raises(ap.PolicyError):
        ap.read_policies(path)
    assert ap.PolicyStore(path).match(ORIGIN, ActionType.CLICK, "제출") is None


def test_symlink_fails_closed(path, tmp_path):
    real = tmp_path / "elsewhere.json"
    ap.add_policy(real, ORIGIN, "click")
    path.symlink_to(real)
    with pytest.raises(ap.PolicyError):
        ap.read_policies(path)


# ------------------------------------------------------------------ 일치·불일치


def test_store_match_origin_action_target(path):
    ap.add_policy(path, ORIGIN, "click", "제출")
    ap.add_policy(path, "http://127.0.0.1:8000", "press_key")
    store = ap.PolicyStore(path)
    assert store.match(ORIGIN, ActionType.CLICK, "제출") is not None
    assert store.match(ORIGIN, ActionType.CLICK, " 제출 ") is not None  # 공백 정규화
    assert store.match(ORIGIN, ActionType.CLICK, "제출 취소") is None  # 대상 불일치
    assert store.match(ORIGIN, ActionType.TYPE_TEXT, "제출") is None  # 액션 불일치
    assert store.match("https://shop.test.evil.test", ActionType.CLICK, "제출") is None  # origin 불일치
    assert store.match("https://evil-shop.test", ActionType.CLICK, "제출") is None
    assert store.match("http://shop.test", ActionType.CLICK, "제출") is None  # 스킴 다름
    assert store.match("https://shop.test:8443", ActionType.CLICK, "제출") is None  # 포트 다름
    # 대상 이름 없는 정책 = 그 사이트의 그 액션 전부
    assert store.match("http://127.0.0.1:8000", ActionType.PRESS_KEY, "") is not None
    assert store.match("http://127.0.0.1:8000", ActionType.PRESS_KEY, "아무거나") is not None
    assert store.match("http://127.0.0.1:8001", ActionType.PRESS_KEY, "") is None


def test_store_rereads_on_change(path):
    store = ap.PolicyStore(path)
    assert store.match(ORIGIN, ActionType.CLICK, "제출") is None
    ap.add_policy(path, ORIGIN, "click", "제출")
    assert store.match(ORIGIN, ActionType.CLICK, "제출") is not None
    ap.remove_policy(path, ORIGIN, "click", "제출")
    assert store.match(ORIGIN, ActionType.CLICK, "제출") is None
    # 손상되면 이전 메모리 값으로 열지 않는다
    ap.add_policy(path, ORIGIN, "click", "제출")
    assert store.match(ORIGIN, ActionType.CLICK, "제출") is not None
    path.write_text("{broken")
    os.chmod(path, 0o600)
    assert store.match(ORIGIN, ActionType.CLICK, "제출") is None
