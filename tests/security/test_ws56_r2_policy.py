"""WS-56 R2 — 정책 관문(B1 두 번째 방어 · B5 계정 삭제 범주). 합성 임시 파일·합성 프로필만.

B1(두 번째 방어): 정책 파일은 사람 CLI(add_policy/remove_policy)가 쓸 때 프로필 밖 0600 키(서버 상태 폴더)로 만든 HMAC 을
    함께 남긴다. 서버(PolicyStore)는 검증 실패(없음·변조·다른 프로필에서 복사·키 없음·키 권한 느슨함)면 정책을 쓰지
    않는다(fail-closed). 0600·소유자·일반 파일 검사만으로는 '다운로드가 덮어쓴 0600 파일'을 구별하지 못했다.
B5: 계정/account/my account/profile/회원 + 삭제/delete/remove/close/영구 삭제/비활성화 조합은 account_termination
    (한/영·대소문자·전각·공백 변형). 일반 삭제(메일·장바구니 항목)는 범주 아님. CLI·서버 같은 함수.

원 재현: .hermes/verify/ws56-r1/test_policy_edges.py -k account_deletion, test_policy_probe.py.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat

import pytest

from contracts import ActionType
from interface import handoff
from security import ActionContext
from security import approve_policy as ap
from security.hitl import assess_risk


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))


def _ctx(name: str) -> ActionContext:
    return ActionContext(action=ActionType.CLICK, element_name=name)


# ------------------------------------------------------------------ B5: 계정 삭제 = 최고위험 범주

ACCOUNT_DELETION = [
    "계정 삭제", "Delete account", "Delete my account", "계정 영구 삭제", "계정삭제", "DELETE ACCOUNT",
    "ｄｅｌｅｔｅ　ａｃｃｏｕｎｔ", "delete\u00a0 my  account", "Remove account", "Close account",
    "Close my account", "회원 삭제", "회원정보 영구 삭제", "프로필 삭제", "Delete profile", "계정 비활성화",
    "Account deletion", "계\u200b정 삭제",
]
GENERAL_DELETION = ["메일 삭제", "Delete email", "장바구니 항목 삭제", "Remove from cart", "삭제", "Delete",
                    "Remove item", "Close", "댓글 삭제", "Delete draft", "계정 설정 열기", "My account"]


@pytest.mark.parametrize("name", ACCOUNT_DELETION)
def test_account_deletion_is_forbidden_category(tmp_path, name):
    assert ap.forbidden_category(_ctx(name)) == "account_termination", name
    with pytest.raises(ap.PolicyError):
        ap.add_policy(tmp_path / "approve_policy.json", "https://local.test", "click", name)


@pytest.mark.parametrize("name", GENERAL_DELETION)
def test_general_deletion_is_not_forbidden(tmp_path, name):
    """대조: 일반 데이터 삭제는 범주가 아니다 — 정책으로 열 수 있다(과차단 아님)."""
    assert ap.forbidden_category(_ctx(name)) is None, name
    assert ap.add_policy(tmp_path / "approve_policy.json", "https://local.test", "click", name) is not None


@pytest.mark.parametrize("source,text", [
    ("text", "Delete my account permanently"), ("aria", "계정 삭제"), ("id", "delete-account-btn"),
    ("class", "btn-close-account"), ("href", "/settings/account/delete"), ("selector", "#deleteAccount"),
])
def test_account_deletion_found_in_context_sources(source, text):
    """이름이 '확인'이어도 문맥 원천(aria·id·class·href·selector)에 계정 삭제가 있으면 범주(전 원천 재훑기)."""
    ctx = ActionContext(action=ActionType.CLICK, element_name="확인", signals=((source, text),)) \
        if source != "selector" else ActionContext(action=ActionType.CLICK, element_name="확인", selector=text)
    assert ap.forbidden_category(ctx) == "account_termination", (source, text)


@pytest.mark.parametrize("name", ["계정 삭제", "Delete my account"])
def test_account_deletion_refused_by_cli_and_server(tmp_path, monkeypatch, name):
    """CLI 추가 거부(exit 2) · 서버 관문도 같은 함수로 거부(직접 쓴 서명 정책이 있어도 실행 안 함)."""
    from browser import serve_profile as sp
    from interface import approve_policy_cli, cli
    from interface.mcp_server import BrowserMCPServer

    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    monkeypatch.setattr(approve_policy_cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("https://local.test\n"))
    assert cli.main(["approve-policy", "add", "--profile", "probe", "--origin", "https://local.test",
                     "--action", "click", "--target-name", name]) == 2
    # 서버: 이름 무관 click 정책(사람이 연 것)이 있어도 계정 삭제는 범주로 거부.
    path = tmp_path / "approve_policy.json"
    ap.add_policy(path, "https://local.test", "click")
    s = BrowserMCPServer(recipes=False)
    try:
        s._approval_policies = lambda: ap.PolicyStore(path)
        s._current_origin = lambda: "https://local.test"
        assert s._policy_pass(ActionType.CLICK, _ctx(name)) is not True
        assert s._oob_note == {"policy_refused": "account_termination"}
    finally:
        s.hub.close()


# ------------------------------------------------------------------ B1: 사람 CLI 가 쓴 정책만(HMAC)


def test_cli_written_policy_verifies(tmp_path):
    path = tmp_path / "p" / "approve_policy.json"
    path.parent.mkdir()
    ap.add_policy(path, "https://shop.test", "click", "제출")
    store = ap.PolicyStore(path)
    assert store.match("https://shop.test", ActionType.CLICK, "제출") is not None
    assert store.error == ""
    key = ap.policy_key_path()
    assert stat.S_IMODE(os.lstat(key).st_mode) == 0o600
    assert not str(key.resolve()).startswith(str(path.parent.resolve()))  # 키는 프로필 밖


def _rewrite(path, mutate):
    data = json.loads(path.read_text())
    mutate(data)
    tmp = path.with_name("x.tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@pytest.mark.parametrize("how", ["unsigned", "added-row", "mac-copied", "plain-policy-file"])
def test_tampered_policy_is_ignored(tmp_path, how):
    """0600·같은 소유자·일반 파일이어도 CLI 서명이 맞지 않으면 정책 없음(다운로드 덮어쓰기 모양 포함)."""
    path = tmp_path / "p" / "approve_policy.json"
    path.parent.mkdir()
    ap.add_policy(path, "https://shop.test", "download_file")
    row = {"origin": "https://shop.test", "action": "click", "target_name": "제출"}
    if how == "unsigned":
        _rewrite(path, lambda d: d.pop("mac", None))
    elif how == "added-row":
        _rewrite(path, lambda d: d["policies"].append(row))
    elif how == "mac-copied":
        _rewrite(path, lambda d: d.__setitem__("policies", [row]))
    else:  # 사이트가 내려준 그대로의 정책 JSON(검증 탐침의 다운로드 내용)
        tmp = path.with_name("x.tmp")
        tmp.write_text(json.dumps({"version": 1, "policies": [row]}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    store = ap.PolicyStore(path)
    assert store.match("https://shop.test", ActionType.CLICK, "제출") is None
    assert store.match("https://shop.test", ActionType.DOWNLOAD_FILE, "") is None
    assert store.error


def test_policy_copied_from_another_profile_is_ignored(tmp_path):
    """다른 프로필 폴더의 서명된 파일을 그대로 복사해도 이 프로필에서는 맞지 않는다(폴더 결속)."""
    a = tmp_path / "serve-a" / ap.POLICY_FILE
    b = tmp_path / "serve-b" / ap.POLICY_FILE
    a.parent.mkdir()
    b.parent.mkdir()
    ap.add_policy(a, "https://shop.test", "click", "제출")
    shutil.copy2(a, b)
    os.chmod(b, 0o600)
    assert ap.PolicyStore(a).match("https://shop.test", ActionType.CLICK, "제출") is not None
    assert ap.PolicyStore(b).match("https://shop.test", ActionType.CLICK, "제출") is None


@pytest.mark.parametrize("how", ["missing", "loose", "replaced"])
def test_key_problems_fail_closed(tmp_path, how):
    path = tmp_path / "p" / "approve_policy.json"
    path.parent.mkdir()
    ap.add_policy(path, "https://shop.test", "click", "제출")
    key = ap.policy_key_path()
    if how == "missing":
        key.unlink()
    elif how == "loose":
        os.chmod(key, 0o644)
    else:
        key.unlink()
        fd = os.open(key, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, os.urandom(32).hex().encode())
        os.close(fd)
    assert ap.PolicyStore(path).match("https://shop.test", ActionType.CLICK, "제출") is None


def test_remove_keeps_signature_valid(tmp_path):
    path = tmp_path / "p" / "approve_policy.json"
    path.parent.mkdir()
    ap.add_policy(path, "https://shop.test", "click", "제출")
    ap.add_policy(path, "https://shop.test", "press_key")
    assert ap.remove_policy(path, "https://shop.test", "click", "제출")
    store = ap.PolicyStore(path)
    assert store.match("https://shop.test", ActionType.PRESS_KEY, "") is not None
    assert store.match("https://shop.test", ActionType.CLICK, "제출") is None
    assert store.error == ""


def test_risk_of_account_deletion_is_high():
    """전제: 게이트도 계정 삭제를 고위험으로 본다(그래서 정책 관문까지 온다)."""
    for name in ("계정 삭제", "Delete my account"):
        assert assess_risk(_ctx(name)).risk.value == "high"
