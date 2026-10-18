"""WS-51 R7 — 리터럴 제어값 census 고정(R6-B1 형제, 소비자 표 독립 AST — tests/security/ws51_r7_census.py).

R5 census 는 소비자 `CONTROL_VOCABULARY` 의 키를 조사 입력으로 써서, 표에 없는 제어 키(`nav_frame`·`resolved_from`)를
애초에 보지 못했다. 이 테스트는 src/ 전체의 **문자열 키 칸 × 리터럴 문자열 값** 생산 자리(`literal_records`)를 소비자
표 없이 모으고, 그 전부를 아래 표로 분류한다(새 키·새 파일이 생기면 실패해 분류를 요구한다).

분류:

* CONTROL — 도구 열거 어휘(에이전트·클라이언트가 값으로 분기). 모든 리터럴 값이 `control_value(키, 값)` 이어야 하고,
  실제 등록된 뒤에도 응답 wire 의 값이 원래와 같아야 한다(생산→소비 불변).
* FREE — 사람이 읽는 자유 문장(안내·사유·오류 문구). 어휘에 넣지 않는다 — 등록 값이 섞이면 정제(가림)가 맞다.
* NOT_OUT — MCP 응답 출구 아님(HTTP 요청 머리·LLM 요청·상태 파일·하위 프로세스 환경·내부 조회).

보고서 표: .hermes/state/ws51-r7/report.md.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import get_args

import pytest

from contracts import ActionResult, ActionType
from security import secret_registry as sr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ws51_r7_census import Record, literal_census, literal_records  # noqa: E402

R = "[REDACTED]"
CONTROL, FREE, NOT_OUT = "CONTROL", "FREE", "NOT_OUT"


def _pu():
    import security.public_url  # noqa: F401

    return sys.modules["security.public_url"]


#: 파일 단위 분류 — 이 파일들의 칸은 MCP 응답 출구가 아니다(또는 소비자 자신).
NOT_OUT_FILES = {
    "harness/": "하네스(게이트 측정 — MCP 서버 응답이 아니라 하네스 JSON 보고·Mock 사이트)",
    "agent/": "에이전트 루프(run CLI — MCP 서버 경로 아님, R5 와 같은 판정)",
    "llm/": "LLM 요청 본문",
    "contracts/": "계약 모델 기본값·설정(동결, 값 칸은 각 생산자가 채운다)",
    "interface/run_cli.py": "run CLI 보고(파일·stdout)",
    "interface/run_answer.py": "run CLI 답 생성(LLM 요청)",
    "interface/tui.py": "TUI 대시보드",
    "interface/observability.py": "run CLI 관측 기록",
    "interface/session_cli.py": "session CLI stdout",
    "interface/profile_cli.py": "profile CLI stdout",
    "browser/chrome_cookies.py": "WS-59 profile import-cookies 전용(사람 CLI — MCP·서버 도구에서 import 하지 않음)",
    "interface/login_cli.py": "login CLI",
    "interface/cli.py": "CLI 진입점",
    "interface/server_schema.py": "도구 스키마(정적 — 응답 data 아님, WS-48 골든 고정)",
    "security/public_url.py": "소비자 자신(제어 표 — 생산자 아님)",
}

#: 모듈·클래스 수준 조회표(Record.table) → 분류.
TABLES = {
    ("actions/dispatcher.py", "_KEY_ALIASES"): (NOT_OUT, "키 이름 정규화 조회표(Playwright 로 넘기는 입력)"),
    ("actions/dispatcher.py", "_PARAM_ALIASES"): (NOT_OUT, "계약 입력 키 → 디스패처 키 조회표"),
    ("browser/headless_retry.py", "SKIP_NOTES"): (FREE, "data.retry_skipped.note 고정 문장(키는 reason 어휘)"),
    ("browser/headless_retry.py", "COOKIE_CHANGE_NOTES"): (FREE, "WS-52 data.retry_skipped.cookie_change_note 고정 문장"
                                                               "(키는 cookie_change 낱말)"),
    ("interface/handoff.py", "_KEY_LABEL"): (FREE, "승인 불일치 사유 문장에 쓰는 사람용 이름"),
    ("recipes/store.py", "_ACTION_WORD"): (FREE, "레시피 자동 이름의 사람용 낱말"),
    ("security/hitl.py", "ICON_CLASS_RISK"): (NOT_OUT, "아이콘 class → 위험 키워드 조회표(판정 입력)"),
    ("security/robots_signal.py", "_SCOPE_NOTES"): (FREE, "data.robots.note 고정 문장(키는 scope 어휘)"),
    ("vision/grounder.py", "GROUND_REASONING"): (NOT_OUT, "VLM 요청 설정"),
    ("vision/overlay.py", "_INJECT_SCRIPT"): (NOT_OUT, "페이지 주입 JS 의 % 치환"),
    ("vision/overlay.py", "_REMOVE_SCRIPT"): (NOT_OUT, "페이지 주입 JS 의 % 치환"),
}

#: 키 → (분류, 근거). (키, 파일) 은 같은 키의 파일별 예외.
KEYS = {
    # --- CONTROL: 도구 열거 어휘
    "nav_frame": (CONTROL, "actions/dispatcher.py _NavWatch — iframe 안 문서 이동(R6-B1)"),
    "resolved_from": (CONTROL, "actions/dispatcher.py switch_frame — current_frame|root(server_schema 안내, R6-B1)"),
    "nav_aborted": (CONTROL, "actions/dispatcher.py _NavWatch.aborted(R6)"),
    "captured": (CONTROL, "actions/dispatcher.py 프레임 안 screenshot data.captured"),
    "handled": (CONTROL, "actions/dispatcher.py data.dialogs[].handled"),
    "coordinate_target": (CONTROL, "interface/server_hitl.py 좌표 대상 근거(gate_basis)"),
    "blocked_by": (CONTROL, "interface/server_display.py 캡처 차단 data.blocked_by"),
    "code": (CONTROL, "security/egress.py data.egress.code·data.upstream.code"),
    "category": (CONTROL, "security/egress.py data.egress.category(CAT_*)"),
    "scope": (CONTROL, "security/robots_signal.py data.robots.scope(RobotsScope)"),
    "storage": (CONTROL, "recipes/service.py list data.storage"),
    "source": (CONTROL, "interface/youtube_search.py data.youtube.source · security/hitl.py gate_basis.source"),
    "how_to_wait": (CONTROL, "interface/handoff.py 조작권 요청 안내 — 도구 이름"),
    "changed": (CONTROL, "interface/server_handoff.py control_wait"),
    "holder": (CONTROL, "interface/handoff.py control_status"),
    "outcome": (CONTROL, "interface/server_hitl.py 승인 결과"),
    "status": (CONTROL, "interface/handoff.py·server_hitl.py 승인 상태"),
    "reason_code": (CONTROL, "interface/server_hitl.py check_approval 사유 코드"),
    "op": (CONTROL, "interface/handoff.py·server_handoff.py 명령 op"),
    "kind": (CONTROL, "actions/pdf_document.py·recipes/keys.py 대상 종류"),
    "mode": (CONTROL, "interface/on_demand.py data.window.mode"),
    "state": (CONTROL, "interface/on_demand.py data.window.state"),
    "browser_mode": (CONTROL, "interface/handoff.py browser_mode"),
    "presented_as": (CONTROL, "interface/server_signals.py identity"),
    "reason": (CONTROL, "제어 사유 코드(pdf·youtube·egress·server_window·server_signals·recipes)"),
    "error_code": (CONTROL, "ErrorCode 값"),
    # --- FREE: 사람이 읽는 문장
    "detail": (FREE, "실패 세부 문장"),
    "error": (FREE, "창 전환 실패 문장"),
    "error_message": (FREE, "오류 문장"),
    "hint": (FREE, "안내 문장"),
    "how": (FREE, "레시피 안내 문장"),
    "message": (FREE, "로그인·조작권 안내 문장"),
    "next": (FREE, "다음 할 일 문장"),
    "note": (FREE, "주의 문장"),
    "unresolved": (FREE, "좌표 판정 불가 사유 문장"),
    # --- NOT_OUT
    "Accept": (NOT_OUT, "HTTP 요청 머리"),
    "Accept-Encoding": (NOT_OUT, "HTTP 요청 머리"),
    "LC_ALL": (NOT_OUT, "하위 프로세스(ps) 환경"),
    "locale": (NOT_OUT, "브라우저 컨텍스트 설정"),
    "part": (NOT_OUT, "YouTube API 요청 쿼리"),
    "type": (NOT_OUT, "YouTube API 요청 쿼리·VLM 요청 본문"),
    "role": (NOT_OUT, "VLM 요청 메시지"),
    "requestStage": (NOT_OUT, "CDP Fetch.enable 인자"),
    "urlPattern": (NOT_OUT, "CDP Fetch.enable 인자"),
    "password": (NOT_OUT, "security/credentials.placeholders — 호출처 없음(모델 지시문용 자리표시자 이름)"),
    "username": (NOT_OUT, "security/credentials.placeholders — 호출처 없음(모델 지시문용 자리표시자 이름)"),
    "nav": (NOT_OUT, "recipes/recorder.py 레시피 파일 expect.nav(응답에는 단계 수만)"),
    "profile": (NOT_OUT, "interface/handoff.py server.json 상태 파일"),
    "action": (NOT_OUT, "recipes/recorder.py 궤적 항목(레시피 파일) — 응답 action 은 ActionType 값(control_value)"),
    ("kind", "browser/site_session.py"): (NOT_OUT, "로그인 추적 이벤트 종류(tracer 파일 — interface 가 쓰지 않음)"),
    ("server_id", "browser/serve_profile.py"): (NOT_OUT, "프로필 잠금 파일 내용"),
}


def _skip(r: Record) -> bool:
    return not r.value or any(r.file.startswith(p) for p in NOT_OUT_FILES)


@pytest.fixture(scope="module")
def records():
    return literal_records()


def _classify(r: Record):
    if r.table is not None:
        return TABLES[(r.file, r.table)]
    return KEYS.get((r.key, r.file)) or KEYS[r.key]


def test_census_does_not_read_consumer_table():
    """조사가 소비자 표(CONTROL_VOCABULARY·control_value)를 입력으로 쓰지 않는다 — 모듈이 public_url 을 import 하지
    않고 그 이름을 참조하지 않는다(R6-B1 의 원인: 표에 없는 키는 조사되지 않았다)."""
    src = (Path(__file__).resolve().parent / "ws51_r7_census.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any("public_url" in (m or "") for m in imported), imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"CONTROL_VOCABULARY", "control_value", "CONTROL_ENUM_KEYS", "public_url"}, names


def test_census_finds_r6_b1_keys(records):
    """조건 성립: 소비자 표 없이 nav_frame·resolved_from(지역 이름을 거친 값)을 찾는다."""
    got = literal_census()
    assert set(got["nav_frame"]) == {"current_frame"}
    assert set(got["resolved_from"]) == {"current_frame", "root"}
    assert any(loc.startswith("actions/dispatcher.py:") for loc in got["resolved_from"]["root"])
    assert len(records) >= 400  # 빈 조사 통과 방지(실측 548 자리)


def test_every_literal_slot_classified(records):
    keys = {(r.key, r.file) for r in records if not _skip(r) and r.table is None}
    tables = {(r.file, r.table) for r in records if not _skip(r) and r.table is not None}
    missing = sorted(k for k in keys if (k not in KEYS and k[0] not in KEYS))
    assert missing == [], "새 리터럴 칸 — KEYS 에 분류(CONTROL·FREE·NOT_OUT)와 근거를 추가"
    assert sorted(tables - set(TABLES)) == [], "새 조회표 — TABLES 에 분류 추가"
    used = {k for k, _ in keys} | keys
    assert sorted(k for k in KEYS if k not in used) == [], "사라진 칸 — KEYS 정리"
    assert sorted(set(TABLES) - tables) == [], "사라진 조회표 — TABLES 정리"


def _control_records(records):
    return [r for r in records if not _skip(r) and _classify(r)[0] == CONTROL]


def test_control_literals_are_exempt(records):
    """제어로 분류된 칸의 모든 리터럴 값이 그 키의 어휘·형식(control_value) 안에 있다."""
    pu = _pu()
    rows = _control_records(records)
    bad = sorted({(r.key, r.value, f"{r.file}:{r.line}") for r in rows if not pu.control_value(r.key, r.value)})
    assert bad == []
    assert len({(r.key, r.value) for r in rows}) >= 70  # 실제로 값을 검사했다


def test_free_literals_not_vocabulary(records):
    """자유 문장 칸은 어휘에 없다 — 같은 값이 등록되면 가린다(어휘가 자유 문장으로 새지 않음)."""
    pu = _pu()
    rows = [r for r in records if not _skip(r) and _classify(r)[0] == FREE]
    assert rows
    assert sorted({(r.key, r.value) for r in rows if pu.control_value(r.key, r.value)}) == []


def _wire(data):
    from interface.mcp_server import envelope_json

    res = ActionResult(success=True, action=ActionType.OBSERVE_PAGE, current_url="http://127.0.0.1/",
                       snapshot_epoch=0, tab_id="tab-1", retry_safe=True, data=data)
    return json.loads(envelope_json(res))["data"]


def _force_register(value: str) -> bool:
    """값을 등록한다. 가릴 수 있는 값이면 True(조건 성립: 다른 칸에서 가린다). 구조상 가려지지 않는 값(4~7자
    알파벳 낱말·도구 어휘 — secret_registry._eligible None)이면 실제 주소 관측으로도 등록되지 않음을 확인하고 False."""
    if sr._eligible(value) is None:
        sr.register_url("http://127.0.0.1/seen?token=" + value)
        assert len(sr.REGISTRY) == 0 and not public_text_masks(value), value
        return False
    sr.REGISTRY.add_values([value])
    assert public_text_masks(value), value
    return True


def public_text_masks(value: str) -> bool:
    from security.public_url import public_text

    return R in public_text(f"x {value} y")


#: R7 에 새로 넣은 제어 키(어휘 + 불변 테스트 대상).
R7_KEYS = ("nav_frame", "resolved_from", "captured", "handled", "coordinate_target", "blocked_by", "code",
           "category", "scope", "storage", "source", "how_to_wait")


def _r7_pairs():
    """생산 쪽 값(소비자 어휘를 읽지 않는다): 리터럴 census 값 + 생산 코드 상수(egress CAT_*·UPSTREAM_*, RobotsScope,
    hitl 문맥 신호 원천)."""
    from security import egress, hitl
    from security.robots_signal import RobotsScope

    pairs = {(r.key, r.value) for r in literal_records() if r.key in R7_KEYS and r.value and not _skip(r)
             and r.table is None and _classify(r)[0] == CONTROL}
    pairs |= {("category", v) for k, v in vars(egress).items() if k.startswith("CAT_")}
    pairs |= {("code", egress.UPSTREAM_RESOLVE_FAILED), ("code", egress.UPSTREAM_CONNECT_FAILED)}
    pairs |= {("scope", v) for v in get_args(RobotsScope)}
    pairs |= {("source", v) for v in hitl.TEXT_SOURCES | hitl.PATH_SOURCES | hitl.TOKEN_SOURCES}
    return sorted(pairs)


@pytest.fixture
def fresh_registry():
    sr.clear()
    yield
    sr.clear()


@pytest.mark.parametrize("key,value", _r7_pairs(), ids=lambda x: x)
def test_r7_control_value_survives_registration(fresh_registry, key, value):
    """생산 값(어휘 전부)을 강제 등록해도 응답 wire 의 제어 칸 값은 원래와 같고, 자유 문장 칸(note)에서는 가린다."""
    maskable = _force_register(value)
    out = _wire({key: value, "nested": {key: value}, "items": [{key: value}], "note": f"about {value} here"})
    assert out[key] == value and out["nested"][key] == value and out["items"][0][key] == value
    if maskable:
        assert value not in out["note"] and R in out["note"]


def test_vocabulary_sources_match_code():
    """어휘가 생산 코드의 상수·타입과 같다(import 해 실제 값으로)."""
    from interface.youtube_search import SOURCE
    from security import egress, hitl
    from security.robots_signal import RobotsScope

    vocab = _pu().CONTROL_VOCABULARY
    cats = {v for k, v in vars(egress).items() if k.startswith("CAT_")}
    assert cats == set(egress.OPEN_OPTION) == vocab["category"]
    assert {egress.UPSTREAM_RESOLVE_FAILED, egress.UPSTREAM_CONNECT_FAILED, "egress_blocked"} == vocab["code"]
    assert set(get_args(RobotsScope)) == vocab["scope"]
    assert SOURCE in vocab["source"]
    from interface import youtube_search

    assert set(youtube_search._REASONS) <= vocab["reason"]
    basis = hitl.TEXT_SOURCES | hitl.PATH_SOURCES | hitl.TOKEN_SOURCES | {"unresolved", "form_submit", "action"}
    assert basis <= vocab["source"]


# ------------------------------------------------------------------ 실제 생산자 → wire(브라우저 없음)

def test_egress_producers_survive(fresh_registry):
    from security import egress

    guard = egress.EgressGuard(allowed_domains=["allowed.test"])
    blocked = guard.evaluate("http://not-allowed.test/path")
    assert not blocked.allowed
    info = blocked.to_agent()
    assert info["code"] == "egress_blocked" and info["category"] == "not_in_allowlist"
    up = egress.UpstreamFailure(host="h.test", code=egress.UPSTREAM_RESOLVE_FAILED).to_agent()
    for v in ("egress_blocked", "not_in_allowlist", egress.UPSTREAM_RESOLVE_FAILED):
        _force_register(v)
    out = _wire({"egress": info, "upstream": up})
    assert out["egress"]["code"] == "egress_blocked" and out["egress"]["category"] == "not_in_allowlist"
    assert out["egress"]["reason"] == info["reason"]
    assert out["upstream"]["code"] == egress.UPSTREAM_RESOLVE_FAILED


def test_youtube_source_survives(fresh_registry):
    from interface.youtube_search import SOURCE, internal_error_result

    res = internal_error_result()
    assert res["data"]["youtube"]["source"] == SOURCE
    _force_register(SOURCE)
    assert _wire(res["data"])["youtube"]["source"] == SOURCE


def test_robots_scope_survives(fresh_registry):
    from security.robots_signal import RobotsRules

    sig = RobotsRules.parse(b"User-agent: *\nDisallow: /\n").signal("/x", "http://127.0.0.1/robots.txt")
    assert sig is not None and sig["scope"] == "all_bots"
    _force_register("all_bots")
    assert _wire({"robots": dict(sig)})["robots"]["scope"] == "all_bots"


def test_hitl_gate_basis_source_survives(fresh_registry):
    from security.hitl import ActionContext, assess_risk

    a = assess_risk(ActionContext(action=ActionType.CLICK, unresolved_target="좌표에 요소 없음"))
    assert a.basis["source"] == "unresolved"
    b = assess_risk(ActionContext(action=ActionType.CLICK, submits_form=True))
    assert b.basis["source"] == "form_submit"
    for v in ("unresolved", "form_submit"):
        _force_register(v)
    out = _wire({"gate_basis": dict(a.basis), "other": {"gate_basis": dict(b.basis)}})
    assert out["gate_basis"]["source"] == "unresolved" and out["other"]["gate_basis"]["source"] == "form_submit"
