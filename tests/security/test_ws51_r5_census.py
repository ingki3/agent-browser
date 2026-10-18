"""WS-51 R5 — 제어 키 열거 어휘·도구 식별자 생산자 전수 조사 고정(R4-B3·B4 형제, AST — src/ 전체).

* 제어 키(public_url.CONTROL_VOCABULARY)에 값이 실리는 모든 리터럴·상수·str Enum 생산자의 값이 그 키의 어휘 안에 있다.
* 동적 생산자(변수·속성·f-string)는 아래 REVIEWED 에 분류가 있다 — 새 생산자가 생기면 이 테스트가 실패해 분류를
  요구한다(분류: 어휘 출처 또는 '자유 문장 — 정제' 또는 '응답 출구 아님').
* 출처 Enum·상수 집합이 어휘 안에 있다(import 해 실제 값으로 확인).
* 식별자 생산자(token_hex·token_urlsafe·token_bytes·randbelow·uuid·카운터 f-string id) 전수와 각각의 면제 여부.

보고서 표: .hermes/state/ws51-r5/report.md.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from security import secret_registry as sr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ws51_r5_census import control_census, id_producers  # noqa: E402

def _public_url_module():
    import security.public_url  # noqa: F401

    return sys.modules["security.public_url"]


FREE = "자유 문장 — 어휘 밖이면 정제"
NOT_OUT = "MCP 응답 출구 아님(에이전트 루프·LLM·하네스 내부·CLI 파일)"

#: (키, 파일, 식) → 분류. 어휘 출처면 그 집합이 CONTROL_VOCABULARY[키] 에 든다(아래 enum 테스트).
REVIEWED = {
    ("status", "interface/handoff.py", "ap.status"): "handoff Approval.status — _APPROVAL_STATUS",
    ("status", "interface/handoff.py", "self.login.status"): "LoginJob.status — opening·ready",
    ("status", "interface/handoff.py", "status"): "handoff._set_status 인자 — _APPROVAL_STATUS",
    ("status", "interface/youtube_search.py", "status"): "HTTP 상태 정수(문자열 아님)",
    ("state", "browser/headless_retry.py", "self.states.get(origin)"): "Literal['succeeded','failed']",
    ("state", "browser/login_flow.py", "state"): "LoginState",
    ("state", "browser/site_session.py", "state"): "LoginState",
    ("state", "browser/site_session.py", "state.value"): "LoginState",
    ("state", "interface/on_demand.py", "self.state"): "WindowState.state — headless·headed·switching·failed",
    ("state", "interface/server_window.py", "target"): "headed·headless",
    ("state", "llm/decisions.py", "state"): NOT_OUT,
    ("holder", "interface/handoff.py", "self.holder"): "HOLDER_AGENT·HOLDER_HUMAN",
    ("reason", "actions/pdf_document.py", "f'http_{response.status_code}'"): "CONTROL_PATTERNS['reason']",
    ("reason", "actions/pdf_document.py", "reason"): "_PDF_REASONS(FetchResult.reason)",
    ("reason", "agent/jev_decider.py", "reason"): NOT_OUT,
    ("reason", "agent/loop.py", "f'LLM 오류: {exc}'"): NOT_OUT,
    ("reason", "agent/loop.py", "f'관찰 실패: {retry_exc}'"): NOT_OUT,
    ("reason", "agent/loop.py", "vision_requested.reason"): NOT_OUT,
    ("reason", "agent/policy.py", "f'응답이 객체가 아님: {type(payload).__name__}'"): NOT_OUT,
    ("reason", "agent/policy.py", "str(payload.get('reason') or '')[:200]"): NOT_OUT,
    ("reason", "browser/headless_retry.py", "code"): "SKIP_NOTES 키",
    ("source", "browser/headless_retry.py", "source"): NOT_OUT + " (WS-52 R3 CDP addScriptToEvaluateOnNewDocument 인자 — 페이지 주입 JS)",
    ("reason", "browser/login_flow.py", "reason"): FREE,
    ("reason", "browser/session_probe.py", "f'네비게이션 응답 {signals.http_status}'"): FREE,
    ("reason", "browser/session_probe.py", "f'보호 엔드포인트가 {signals.custom_probe_status} 반환'"): FREE,
    ("reason", "harness/recipe_replay.py", "reason"): NOT_OUT,
    ("reason", "harness/tier2_som.py", "error"): NOT_OUT,
    ("reason", "harness/tier2_som.py", "reason"): NOT_OUT,
    ("reason", "interface/handoff.py", "self.reason"): FREE + " (browser_control_request 사유 — R4-B3)",
    ("reason", "interface/handoff.py", "str(reason or '')[:500]"): FREE + " (R4-B3)",
    ("reason", "interface/run_cli.py", "challenge.reason"): FREE + " (challenge 사유 문장)",
    ("reason", "interface/server_hitl.py", "decision.reason"): FREE + " (hitl 사유 문장)",
    ("reason", "interface/server_hitl.py", "why"): FREE + " (check_approval 사유 문장)",
    ("reason", "interface/server_schema.py", "{'type': 'string'}"): "도구 스키마(값 아님)",
    ("reason", "interface/server_signals.py", "found.reason"): FREE + " (challenge 사유 문장)",
    ("reason", "interface/server_window.py", "reason"): "server_window 전환 사유 — browser_recovered·request_expired·window_closed",
    ("reason", "interface/youtube_search.py", "more.reason"): "_YOUTUBE_REASONS",
    ("reason", "interface/youtube_search.py", "reason"): "_YOUTUBE_REASONS",
    ("reason", "llm/client.py", "str(exc)[:200]"): NOT_OUT,
    ("reason", "recipes/replay.py", "reason"): "_REPLAY_STOP",
    ("reason", "recipes/replay.py", "stop.reason"): "_REPLAY_STOP",
    ("reason", "recipes/replay.py", "str(found.get('reason') or 'target_not_found')"): "_REPLAY_STOP(keys.py JS)",
    ("reason", "security/egress.py", "self.reason.value"): "BlockReason",
    ("reason", "security/hitl.py", "f'무인 모드에서 사전 승인되지 않은 고위험 액션 ({reason})'"): FREE,
    ("reason", "security/hitl.py", "f'사용자 승인 필요 ({reason})'"): FREE,
    ("reason", "security/hitl.py", "f'사전 승인 목록에 존재 ({reason})'"): FREE,
    ("reason", "security/hitl.py", "reason"): FREE,
    ("reason", "vision/grounder.py", "f'VLM 응답 파싱 실패: {str(exc)[:120]}'"): NOT_OUT,
    ("reason", "vision/grounder.py", "str(reason)[:200]"): NOT_OUT,
    ("reason_code", "interface/server_hitl.py", "reason_code"): "handoff.check_approval 사유 코드",
    ("kind", "browser/site_session.py", "kind"): "positive·negative",
    ("kind", "harness/recipe_replay.py", "SCENARIOS[name][0]"): NOT_OUT,
    ("kind", "harness/recipe_replay.py", "kind"): NOT_OUT,
    ("kind", "harness/staleness.py", "kind"): NOT_OUT,
    ("kind", "interface/on_demand.py", "str(challenge.get('kind') or '')"): "ChallengeKind",
    ("kind", "interface/run_cli.py", "challenge.kind.value"): "ChallengeKind",
    ("kind", "interface/server_signals.py", "found.kind.value"): "ChallengeKind",
    ("kind", "llm/budget.py", "kind"): NOT_OUT,
    ("mode", "harness/tier2_som.py", "args.mode"): NOT_OUT,
    ("mode", "harness/tier2_som.py", "mode"): NOT_OUT,
    ("mode", "interface/cli.py", "ExecutionMode(args.mode)"): "ExecutionMode",
    ("mode", "interface/mcp_server.py", "mode"): "ExecutionMode",
    ("mode", "interface/server_lifecycle.py", "self.mode"): "ExecutionMode",
    ("mode", "interface/session_cli.py", "oct(stat_result.st_mode & 511)"): NOT_OUT + " (파일 권한 8진 문자열, CLI)",
    ("browser_mode", "browser/core.py", "browser_mode"): "cli --browser choices",
    ("browser_mode", "interface/cli.py", "args.browser"): "cli --browser choices",
    ("browser_mode", "interface/handoff.py", "self.browser_mode"): "cli --browser choices",
    ("browser_mode", "interface/mcp_server.py", "browser_mode"): "cli --browser choices",
    ("browser_mode", "interface/server_handoff.py", "self.browser_mode"): "cli --browser choices",
    ("browser_mode", "interface/server_lifecycle.py", "self.browser_mode"): "cli --browser choices",
    ("outcome", "interface/handoff.py", "ap.outcome"): "succeeded·failed·outcome_unknown",
    ("outcome", "interface/handoff.py", "outcome"): "succeeded·failed·outcome_unknown",
    ("outcome", "interface/server_hitl.py", "outcome"): "succeeded·failed·outcome_unknown",
    ("op", "interface/handoff.py", "op"): "handoff._OPS · status",
    ("op", "interface/server_schema.py", "{'enum': ['save', 'run', 'list', 'delete']}"): "도구 스키마(값 아님)",
    ("risk", "interface/server_hitl.py", "decision.risk.value"): "RiskLevel",
    ("risk", "security/hitl.py", "risk"): "RiskLevel",
    ("sticky_reason", "interface/on_demand.py",
     "f\"{pending.get('domain')} 에서 창에서 해결한 뒤 headless 복귀 후 {pending.get('kind') or '차단'} 재감지 — 서버 수명 동안 창 유지\""): FREE,
    ("sticky_reason", "interface/on_demand.py", "self.sticky_reason"): FREE,
    ("changed", "interface/server_handoff.py", "changed"): "released·taken·timeout",
    ("nav_aborted", "actions/dispatcher.py", "self.aborted"): "_NavWatch.aborted — download·request_failed·"
                                                              "status_204·status_205 (R6)",
    # R7 — 리터럴 제어값 census(test_ws51_r7_census.py)로 어휘에 들어온 키의 동적 생산자.
    ("resolved_from", "actions/dispatcher.py", "resolved_from"): "switch_frame 지역 이름 — current_frame·root (R7)",
    ("category", "security/egress.py", "category"): "EgressGuard._block 인자 — CAT_* (R7)",
    ("category", "security/egress.py", "self.category"): "EgressDecision.category — CAT_* (R7)",
    ("code", "security/egress.py", "self.code"): "UpstreamFailure.code — UPSTREAM_RESOLVE/CONNECT_FAILED (R7)",
    ("code", "security/egress_proxy.py", "code"): "UpstreamError.code — UPSTREAM_* (R7)",
    ("code", "security/egress_proxy.py", "exc.code"): "UpstreamFailure(code=UpstreamError.code) — UPSTREAM_* (R7)",
    ("code", "browser/core.py", "code"): NOT_OUT + " (BrowserError.code 예외 속성 — 응답은 error_code 로)",
    ("code", "interface/cli.py", "args.code"): NOT_OUT + " (approve CLI 인자)",
    ("code", "interface/code_window.py", "code"): NOT_OUT + " (사람 창 HTML 의 확인 코드)",
    ("code", "interface/handoff.py", "str(code).strip()"): NOT_OUT + " (approve 명령 파일)",
    ("code", "interface/server_display.py", "job.code"): NOT_OUT + " (확인 코드 창 표시 인자)",
    ("scope", "security/robots_signal.py", "scope"): "RobotsScope — all_bots·named_ai·all_bots_and_named_ai·none (R7)",
    ("source", "browser/site_session.py", "source"): NOT_OUT + " (로그인 추적 이벤트 — interface 가 쓰지 않음)",
    ("source", "browser/login_ledger.py", "source"): NOT_OUT + " (WS-59 로그인 기록 파일 login_ledger.json 의 source 칸 — 응답 출구 아님)",
    ("source", "browser/login_ledger.py", "_check_source(entry['source'])"): NOT_OUT + " (WS-59 로그인 기록 파일 읽기 검증 "
                                                                         "— 알려진 어휘만)",
    ("source", "browser/login_records.py", "source"): NOT_OUT + " (WS-58 로그인 기록 사건 종류 — login_ledger 파일·메모리)",
    ("source", "browser/login_watch.py", "source"): NOT_OUT + " (WS-58 조작권 감시 CDP 스크립트 본문 — 응답 출구 아님)",
    ("reason", "browser/chrome_cookies.py", "reason"): NOT_OUT + " (WS-59 _Skip 예외 속성 — 사람 CLI 가 SKIP_LABELS 고정 문구로 개수만 출력)",
    ("source", "security/hitl.py", "source"): "gate_basis.source — 판정 출처·문맥 신호 원천(TEXT·PATH·TOKEN_SOURCES) (R7)",
    # R8 — 속성 전달값 census(test_ws51_r8_census.py)로 어휘에 들어온 키의 동적 생산자.
    ("vendor", "interface/server_signals.py", "found.vendor"): "browser.challenge Challenge.vendor — generic·hcaptcha·"
                                                               "cloudflare·recaptcha·naver·akamai (R8)",
    ("vendor", "interface/run_cli.py", "challenge.vendor"): NOT_OUT + " (run CLI 보고)",
    # healing_attempts 는 R9 에 목록 경로 어휘(CONTROL_LIST_VOCABULARY)로 옮겨 이 조사(키 어휘) 밖 — R9 census 가 heal() 의
    # 원소 틀(복합 코드 포함)에서 도출해 고정한다(tests/security/test_ws51_r9_census.py).
}


@pytest.fixture(scope="module")
def census():
    pu = _public_url_module()
    raw = control_census(pu.CONTROL_VOCABULARY)
    own = "security/public_url.py"  # 소비자(이 표 자체)는 생산자가 아니다
    for entry in raw.values():
        for k in [k for k in entry["dyn"] if k[0] == own]:
            del entry["dyn"][k]
        for bucket in (entry["words"], entry["text"]):
            for k in [k for k, locs in bucket.items() if all(loc.startswith(own + ":") for loc in locs)]:
                del bucket[k]
    return raw


def test_source_literals_within_vocabulary(census):
    pu = _public_url_module()
    missing = {k: sorted(set(c["words"]) - pu.CONTROL_VOCABULARY[k]) for k, c in census.items()}
    assert {k: v for k, v in missing.items() if v} == {}
    assert sum(len(c["words"]) for c in census.values()) >= 80  # 조사가 실제로 값을 찾았다(빈 조사 통과 방지)


def test_dynamic_producers_all_classified(census):
    found = {(k, f, e) for k, c in census.items() for (f, e) in c["dyn"]}
    assert found - set(REVIEWED) == set(), "새 동적 생산자 — 분류(어휘 출처·자유 문장·출구 아님)를 REVIEWED 에 추가"
    assert set(REVIEWED) - found == set(), "사라진 생산자 — REVIEWED 정리"


def test_enum_sources_within_vocabulary():
    from agent.loop import HandoffOutcome
    from browser.challenge import ChallengeKind
    from browser.headless_retry import SKIP_NOTES
    from browser.login_flow import LoginState
    from contracts import ExecutionMode
    from interface import handoff
    from actions.verification import StalenessReason
    from security.egress import BlockReason
    from security.hitl import RiskLevel

    vocab = _public_url_module().CONTROL_VOCABULARY
    assert {e.value for e in ChallengeKind} <= vocab["kind"]
    assert {e.value for e in LoginState} <= vocab["state"]
    assert {e.value for e in ExecutionMode} <= vocab["mode"]
    assert {e.value for e in HandoffOutcome} <= vocab["outcome"]
    assert {e.value for e in RiskLevel} == vocab["risk"]
    assert {e.value for e in BlockReason} <= vocab["reason"]
    assert {e.value for e in StalenessReason} <= vocab["reason"]
    assert set(SKIP_NOTES) <= vocab["reason"]
    assert set(handoff._OPS) <= vocab["op"]
    assert set(handoff._FINISHED) <= vocab["status"]
    assert {handoff.HOLDER_AGENT, handoff.HOLDER_HUMAN} == vocab["holder"]
    assert vocab["sticky_reason"] == frozenset()  # 자유 문장 — 늘 정제


def test_vocabulary_is_closed_words_only():
    """어휘는 낱말(공백·주소 문법 없음)만 — 자유 문장이 어휘로 새어 들지 않는다."""
    import re

    for key, words in _public_url_module().CONTROL_VOCABULARY.items():
        for w in words:
            assert re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", w), (key, w)


# ------------------------------------------------------------------ 도구 식별자 생산자 전수(R4-B4 형제)

EXEMPT = "등록 제외 + 제어 키 면제"
#: (파일, 식) → (분류, TOOL_ID_FORMATS 이름 또는 None, 표본 생성기).
ID_PRODUCERS = {
    ("browser/core.py", "f'tab-{self._tab_counter}'"): (EXEMPT, "tab_id", lambda: f"tab-{secrets.randbelow(999)}"),
    ("perception/engine.py", "f'@e{idx}'"): (EXEMPT, "element_id", lambda: f"@e{secrets.randbelow(9999)}"),
    ("recipes/service.py", "f'@r{self._seq}'"): (EXEMPT, "element_id", lambda: f"@r{secrets.randbelow(9999)}"),
    ("recipes/store.py", "_secrets.token_hex(4)"): (EXEMPT, "recipe_id", lambda: "r_" + secrets.token_hex(4)),
    ("interface/handoff.py", "secrets.token_urlsafe(9)"): (EXEMPT, "request_id",
                                                          lambda: "rq_" + secrets.token_urlsafe(9)),
    ("interface/handoff.py", "secrets.token_urlsafe(12)"): (EXEMPT, "approval_id",
                                                           lambda: "ap_" + secrets.token_urlsafe(12)),
    ("interface/handoff.py", "secrets.token_hex(3)"): (EXEMPT, "server_id", lambda: f"4321-{secrets.token_hex(3)}"),
    ("interface/login_cli.py", "secrets.token_hex(4)"): (EXEMPT, "server_id",
                                                        lambda: "login-" + secrets.token_hex(4)),
    # 응답 출구에 실리지 않거나(파일·IPC), 비밀 그 자체(가려야 맞다) — 면제하지 않는다.
    ("interface/handoff.py", "secrets.token_hex(6)"): ("임시 파일 이름(.tmp-) — 출구 아님", None, None),
    ("interface/handoff.py", "secrets.token_hex(8)"): ("IPC nonce(파일) — 출구 아님", None, None),
    ("interface/handoff.py", "secrets.token_hex(16)"): ("describe challenge(IPC) — 출구 아님", None, None),
    ("interface/handoff.py", "secrets.token_bytes(32)"): ("확인 코드 HMAC 키(바이트) — 출구 아님", None, None),
    ("interface/handoff.py", "secrets.randbelow(10 ** CODE_DIGITS)"): ("확인 코드 — 사람 화면 전용 비밀(면제 금지)", None,
                                                                       None),
    ("interface/login_cli.py", "secrets.token_hex(16)"): ("login_id(IPC 파일) — 출구 아님", None, None),
    ("interface/observability.py", "uuid.uuid4()"): ("correlation_id(JSONL 추적 파일, 디스크) — 출구 아님", None, None),
    ("interface/youtube_search.py", "secrets.token_hex(4)"): ("임시 파일 이름 — 출구 아님", None, None),
    ("browser/headless_retry.py", "secrets.token_bytes(32)"): ("HMAC 키(바이트) — 출구 아님", None, None),
    ("browser/headless_retry.py", "secrets.token_hex(3)"): ("임시 파일 이름 — 출구 아님", None, None),
    ("browser/headless_retry.py", "secrets.choice(letters)"): ("WS-52 R3 CDP 바인딩 이름(페이지 안) — 출구 아님", None, None),
    ("browser/headless_retry.py", "secrets.token_hex(16)"): ("WS-52 R3 관찰 기록 nonce(래퍼 클로저) — 출구 아님", None,
                                                           None),
    ("browser/session_store.py", "secrets.token_bytes(SALT_BYTES)"): ("암호화 salt(바이트) — 출구 아님", None, None),
    ("browser/session_store.py", "secrets.token_bytes(NONCE_BYTES)"): ("암호화 nonce(바이트) — 출구 아님", None, None),
    ("recipes/store.py", "_secrets.token_hex(3)"): ("임시 파일 이름 — 출구 아님", None, None),
    ("browser/login_ledger.py", "secrets.token_hex(6)"): ("WS-59 로그인 기록 임시 파일 이름 — 출구 아님", None, None),
    ("browser/login_watch.py", "secrets.token_hex(6)"): ("WS-58 조작권 감시 격리 world 이름(CDP 내부) — 출구 아님", None, None),
    ("browser/login_watch.py", "secrets.token_hex(8)"): ("WS-58 조작권 감시 바인딩 이름(CDP 내부) — 출구 아님", None, None),
    ("interface/profile_cli.py", "secrets.token_hex(4)"): ("WS-59 import-cookies 프로필 잠금 server_id(잠금 파일·사람 CLI "
                                                          "stderr) — 출구 아님", None, None),
}


def test_id_producer_inventory_fixed():
    found = {(f, e) for f, _, e in id_producers()}
    assert found == set(ID_PRODUCERS), (sorted(found - set(ID_PRODUCERS)), sorted(set(ID_PRODUCERS) - found))


@pytest.mark.parametrize("where", [k for k, v in ID_PRODUCERS.items() if v[1]], ids=lambda k: f"{k[0]}:{k[1]}")
def test_exempt_identifier_samples_not_registered_and_kept(where):
    _, fmt, make = ID_PRODUCERS[where]
    pu = _public_url_module()
    sr.clear()
    value = make()
    try:
        for _ in range(50):
            value = make()
            assert sr.TOOL_ID_FORMATS[fmt].fullmatch(value), (fmt, value)
            assert sr.is_tool_token(value)
            sr.register_url("http://127.0.0.1/seen?token=" + quote(value, safe=""))
            assert len(sr.REGISTRY) == 0, value
        keys = {"tab_id": "tab_id", "element_id": "element_id", "recipe_id": "id", "request_id": "request_id",
                "approval_id": "approval_id", "server_id": "server_id"}
        sr.REGISTRY.add_values([value])  # 등록 제외가 깨져도 제어 키 값은 그대로(이중 방어)
        payload = {"data": {keys[fmt]: value}}
        assert pu.public_payload(payload) == payload
    finally:
        sr.clear()
