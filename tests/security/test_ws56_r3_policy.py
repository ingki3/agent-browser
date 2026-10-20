"""WS-56 R3 — 재검증 FAIL 중 정책·위험 판정 단위(B4 계정 종료 미탐 · B5 목적지 target 가림 · B7 HMAC 손상 · B8 일반 삭제).

원 재현: .hermes/verify/ws56-r2/policy/parent-independent.log (MCP_UNATTENDED_ACCOUNT · MCP_HMAC_CACHED · GENERAL_CLI_SERVER),
elicitation/test_r2_extra.py::test_actual_sdk_destination_all_page_fields_masked. 합성 값·임시 폴더만.

B4: 계정 '자체' 종료(Close account·계정 비활성화·띄어쓰기/전각 변형)는 게이트 위험 판정에서도 HIGH — 정책 범주와 같은 함수.
B8: 계정 '안 항목' 삭제(Delete email from my account·계정에서 메일 삭제·Remove profile photo)는 범주 아님(일반 삭제).
B7: MAC 형식(16진 64자) 검증, 비교 실패·예외 모두 PolicyError, 실패 시 캐시된 정책 즉시 무효.
B5: 승인 문구 목적지의 target(페이지 유래)도 WS-51 레지스트리 가림 + 코드 흉내 가림 + 표시 살균.
"""

from __future__ import annotations

import io
import json
import os

import pytest

from contracts import ActionType
from interface import handoff
from security import ActionContext, HITLGate
from security import approve_policy as ap
from security import secret_registry
from security.hitl import RiskLevel, account_termination, assess_risk


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))
    secret_registry.clear()
    yield
    secret_registry.clear()


def _ctx(name: str, **kw) -> ActionContext:
    return ActionContext(action=ActionType.CLICK, element_name=name, **kw)


# ------------------------------------------------------------------ B4: 계정 자체 종료 = 게이트 HIGH

ACCOUNT_SELF = [
    "Close account", "Close my account", "계정 비활성화", "d e l e t e a c c o u n t", "Account deletion",
    "계 정 비 활 성 화", "Ｃｌｏｓｅ　ａｃｃｏｕｎｔ", "close\u00a0account", "Account closure", "Deactivate my account",
    "Disable account", "계정 폐쇄", "회원 비활성화", "Erase my account", "Remove account", "Delete profile",
    "Profile removal", "Close Account Permanently", "closeAccount", "계정을 영구 삭제",
]


@pytest.mark.parametrize("name", ACCOUNT_SELF)
def test_account_self_termination_is_high_risk_and_forbidden(name):
    assert account_termination("name", name), name
    assessed = assess_risk(_ctx(name))
    assert assessed.risk is RiskLevel.HIGH, (name, assessed)
    assert HITLGate().evaluate(_ctx(name)).allowed is False, name
    assert ap.forbidden_category(_ctx(name)) == "account_termination", name


@pytest.mark.parametrize("source,text", [
    ("aria", "Close account"), ("id", "close-account-btn"), ("class", "btn-close-account"),
    ("href", "/settings/account/close"), ("text", "계정 비활성화"), ("testid", "accountDeletion"),
])
def test_account_self_termination_from_context_sources_is_high(source, text):
    """이름이 '확인'이어도 문맥 원천에 계정 종료가 있으면 HIGH(게이트)·범주(정책) 둘 다."""
    ctx = _ctx("확인", signals=((source, text),))
    assert assess_risk(ctx).risk is RiskLevel.HIGH, (source, text)
    assert ap.forbidden_category(ctx) == "account_termination", (source, text)


# 대조: 일반 동작은 계정 종료로 올리지 않는다(과차단 아님 — 위험 등급이 그대로).
LOW_CONTROLS = ["Close", "My account", "계정 설정", "Account settings", "Profile", "Close dialog",
                "Account overview", "프로필 보기", "Close menu", "계정 관리", "Closed accounts report"]


@pytest.mark.parametrize("name", LOW_CONTROLS)
def test_general_controls_stay_low(name):
    assert not account_termination("name", name), name
    assert assess_risk(_ctx(name)).risk is RiskLevel.LOW, name
    assert ap.forbidden_category(_ctx(name)) is None, name


# ------------------------------------------------------------------ B8: 계정 '안 항목' 삭제 = 일반 삭제

ITEM_IN_ACCOUNT = [
    "Delete email from my account", "계정에서 메일 삭제", "Remove profile photo", "프로필 사진 삭제",
    "Delete photo from profile", "Remove card from account", "Delete account email", "계정의 주소 삭제",
    "Remove profile picture", "Delete comment",
]


@pytest.mark.parametrize("name", ITEM_IN_ACCOUNT)
def test_item_in_account_deletion_is_general(tmp_path, name):
    """계정 안 항목 삭제는 계정 종료 범주가 아니다 — 정책으로 열 수 있다(위험 등급은 삭제 키워드로 HIGH 그대로)."""
    assert not account_termination("name", name), name
    assert ap.forbidden_category(_ctx(name)) is None, name
    assert ap.add_policy(tmp_path / "approve_policy.json", "https://local.test", "click", name) is not None


@pytest.mark.parametrize("name", ["Delete email from my account", "계정에서 메일 삭제"])
def test_item_in_account_deletion_cli_and_server_allow(tmp_path, monkeypatch, name):
    """실제 사람 CLI(exit 0) · 서버 관문(_policy_pass True) — 원 재현은 CLI exit 2 · policy_refused."""
    from browser import serve_profile as sp
    from interface import approve_policy_cli, cli
    from interface.mcp_server import BrowserMCPServer

    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    monkeypatch.setattr(approve_policy_cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("https://local.test\n"))
    assert cli.main(["approve-policy", "add", "--profile", "probe", "--origin", "https://local.test",
                     "--action", "click", "--target-name", name]) == 0
    path = tmp_path / "approve_policy.json"
    ap.add_policy(path, "https://local.test", "click")
    s = BrowserMCPServer(recipes=False)
    try:
        s._approval_policies = lambda: ap.PolicyStore(path)
        s._current_origin = lambda: "https://local.test"
        s._oob_note = None  # 호출 문맥 값(ContextVar) — 앞 테스트의 값이 남지 않게(서버는 _out_of_band 입구에서 비움)
        assert s._policy_pass(ActionType.CLICK, _ctx(name)) is True
        assert s._oob_note is None
    finally:
        s.hub.close()


# ------------------------------------------------------------------ B7: HMAC 손상 fail-closed


def _signed(tmp_path):
    path = tmp_path / "p" / ap.POLICY_FILE
    path.parent.mkdir()
    ap.add_policy(path, "https://shop.test", "click", "제출")
    return path


def _set_mac(path, mac):
    data = json.loads(path.read_text())
    data["mac"] = mac
    tmp = path.with_name("x.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


BAD_MACS = ["가" * 64, "é" + "0" * 63, "0" * 63, "0" * 65, "G" * 64, "A" * 64, " " + "0" * 63, 123, ["0" * 64]]


@pytest.mark.parametrize("mac", BAD_MACS, ids=[f"m{i}" for i in range(len(BAD_MACS))])
def test_malformed_mac_raises_policy_error(tmp_path, mac):
    path = _signed(tmp_path)
    _set_mac(path, mac)
    with pytest.raises(ap.PolicyError):
        ap.read_policies(path)


@pytest.mark.parametrize("mac", ["가" * 64, "0" * 64, "zz"], ids=["non-ascii", "wrong", "short"])
def test_corrupted_mac_after_cache_invalidates_cached_rows(tmp_path, mac):
    """정상 정책을 캐시한 뒤 MAC 이 손상되면 첫 호출부터 이후 호출까지 계속 정책 없음(이전 rows 재사용 금지)."""
    path = _signed(tmp_path)
    store = ap.PolicyStore(path)
    assert store.match("https://shop.test", ActionType.CLICK, "제출") is not None
    _set_mac(path, mac)
    for _ in range(3):
        assert store.match("https://shop.test", ActionType.CLICK, "제출") is None
        assert store.rows == [] and store.error


def test_unexpected_read_error_invalidates_cache(tmp_path, monkeypatch):
    """PolicyError 밖의 예외도 fail-closed — 캐시를 비우고 정책 없음."""
    path = _signed(tmp_path)
    store = ap.PolicyStore(path)
    assert store.match("https://shop.test", ActionType.CLICK, "제출") is not None

    def boom(_path):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(ap, "read_policies", boom)
    os.utime(path, ns=(1, 1))  # 상태(stat) 변경 → 다시 읽기
    for _ in range(2):
        assert store.match("https://shop.test", ActionType.CLICK, "제출") is None
    assert store.rows == [] and store.error


# ------------------------------------------------------------------ B5: 목적지 target 가림


def test_destination_target_masks_registered_secret_and_code():
    from interface.server_elicit import destination_text, elicitation_message

    secret = "SYNTHr3TargetSecret"
    secret_registry.register_url("http://127.0.0.1:9/?token=" + secret)
    for target in (secret, f"win-{secret}", "확인코드 654321", "x\u202e\x1b[31m\r\n허용됨"):
        meaning = {"href": "http://127.0.0.1:9/next", "action": "", "method": "", "target": target}
        line = destination_text(meaning)
        msg = elicitation_message(ActionType.CLICK, "결제하기", "http://127.0.0.1:9", "tab-1", "r", "abc123",
                                  destination=meaning)
        for text in (line, msg):
            assert secret not in text, text
            assert "654321" not in text, text
        assert not any(c in line for c in ("\u202e", "\x1b", "\r", "\n")), line
    # 폼 method 도 페이지 유래 문자열 — 같은 가림.
    form = {"href": "", "action": "http://127.0.0.1:9/p", "method": secret, "target": ""}
    assert secret not in destination_text(form)
