"""WS-51 R8 — 속성 전달값 census 고정(R7-B1, 소비자 표 독립 AST — tests/security/ws51_r8_census.py).

R7 census 는 리터럴 값만 보아 `data.dialogs[].type = str(dialog.type)`(Playwright Dialog.type) 처럼 **외부 객체 속성·열거형을
응답 칸에 옮겨 담는 생산자**를 놓쳤다. 이 테스트는 src/ 전체의 **문자열 키 칸 × 다른 객체의 속성 접근 값** 생산 자리
(`attr_records`)를 소비자 표 없이 모으고, 그 전부를 아래 표로 분류한다(새 자리가 생기면 실패해 분류를 요구한다).

분류(값 출처 속성 기준):

* CONTROL — 도구·라이브러리 열거 어휘(에이전트가 값으로 분기). 생산 쪽 값 집합(코드 상수·라이브러리 문서)이 모두 그 칸의
  어휘(`control_value` · 목록 안 dict 키 경로는 `control_item_value`) 안이어야 하고, 등록 뒤에도 wire 값이 같아야 한다.
* ID — 도구가 만든 식별자(`tab-N`·`@eN`·SoM 태그 `A1` …) — 등록 제외(secret_registry `_eligible` None)라 가려지지 않는다.
* URL — 주소 칸(`_is_url_key`) — 주소 규칙으로 정제.
* FREE — 사람이 읽는 문장(오류·안내·진단) — 등록 값이 섞이면 가림이 맞다.
* PAGE — 페이지·사이트가 쓴 값(대화상자 문구·접근성 이름·role·robots 규칙) — 도구 어휘 아님, 내용 경로 밖이면 정제가 정책.
* STRUCT — dict/list 를 통째로 옮김 — 안쪽 칸은 R7 리터럴 census·이 census 가 각각 잡는다.
* NOT_STR — 숫자·불리언(정제 대상 아님).
* NOT_OUT — MCP 응답 출구 아님(상태·명령 파일, 사람 CLI, 페이지 주입, Playwright 인자).

보고서 표: .hermes/state/ws51-r8/report.md.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

from contracts import ActionResult, ActionType
from security import secret_registry as sr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ws51_r7_census import NOT_OUT_FILES  # noqa: E402
from ws51_r8_census import AttrRecord, attr_records  # noqa: E402

R = "[REDACTED]"
CONTROL, ID, URL, FREE, PAGE, STRUCT, NOT_STR, NOT_OUT = (
    "CONTROL", "ID", "URL", "FREE", "PAGE", "STRUCT", "NOT_STR", "NOT_OUT")


def _pu():
    import security.public_url  # noqa: F401

    return sys.modules["security.public_url"]


#: (키, 속성) → (분류, 근거). (키, 속성, 파일) 은 같은 쌍의 파일별 예외.
ATTRS = {
    # --- CONTROL
    ("type", "type"): (CONTROL, "actions/dispatcher.py _on_dialog — Playwright Dialog.type → data.dialogs[].type (R7-B1)"),
    ("vendor", "vendor"): (CONTROL, "interface/server_signals.py data.challenge.vendor — browser.challenge 벤더 어휘(R8)"),
    ("healing_attempts", "attempts"): (CONTROL, "actions/dispatcher.py 치유 실패 data.healing_attempts — "
                                                "actions.healing.HealingStrategy 값 목록(R8)"),
    ("kind", "value"): (CONTROL, "interface/server_signals.py data.challenge.kind — browser.challenge.ChallengeKind"),
    ("reason", "value"): (CONTROL, "security/egress.py data.egress.reason — BlockReason"),
    ("risk", "value"): (CONTROL, "interface/server_hitl.py — security.hitl.RiskLevel"),
    ("action", "value"): (CONTROL, "interface/server_hitl.py 승인 components.action — ActionType"),
    ("code", "code"): (CONTROL, "security/egress.py UpstreamFailure.to_agent — UPSTREAM_*"),
    ("changed", "last_event"): (CONTROL, "interface/server_handoff.py control_wait — hub.last_event(taken·released 만)"),
    ("status", "status"): (CONTROL, "interface/handoff.py approval_status·login — Approval/LoginJob.status"),
    ("outcome", "outcome"): (CONTROL, "interface/handoff.py approval_status — 승인 결과"),
    ("reason", "reason"): (CONTROL, "pdf FetchResult.reason · youtube reply.reason · replay _Stop.reason 사유 코드"),
    ("expires_at", "isoformat()"): (CONTROL, "browser/serve_profile.py cookie_sites — ISO 시각(CONTROL_TIME_KEYS)"),
    # --- ID
    ("active", "active_tab_id"): (ID, "actions/dispatcher.py tab list data.active — tab-N"),
    ("active_tab_id", "tab_id"): (ID, "interface/server_lifecycle.py 탭 복구 안내 — tab-N"),
    ("closed", "tab_id"): (ID, "actions/dispatcher.py tab close data.closed — tab-N"),
    ("closed_tab_id", "tab_id"): (ID, "interface/server_lifecycle.py — tab-N"),
    ("tab_id", "tab_id"): (ID, "tab-N"),
    ("tab_id", "active_tab_id"): (ID, "tab-N"),
    ("approval_id", "approval_id"): (ID, "ap_…"),
    ("element_id", "element_id"): (ID, "@eN·@sN"),
    ("server_id", "server_id"): (ID, "{pid}-{hex6}"),
    ("tag", "tag"): (ID, "actions/dispatcher.py data.som_tags[].tag — vision.candidates.make_tag 두 글자(A1…)"),
    # --- URL
    ("url", "url"): (URL, "주소 칸"),
    ("current_url", "url"): (URL, "주소 칸"),
    ("frame_url", "url"): (URL, "주소 칸"),
    # --- FREE
    ("api_reason", "api_reason"): (FREE, "interface/youtube_search.py 외부 API 진단 원문(정규화 reason 은 CONTROL — R7 NB)"),
    ("detail", "__name__"): (FREE, "실패 세부 문장(예외 클래스 이름)"),
    ("detail", "detail"): (FREE, "실패 세부 문장"),
    ("error_message", "name"): (FREE, "오류 문장(알 수 없는 도구 이름)"),
    ("error_message", "reason"): (FREE, "오류 문장(f-string)"),
    ("hint", "url"): (FREE, "안내 문장 안 주소(f-string — 문장 규칙)"),
    ("host", "host"): (FREE, "data.egress.host — 차단된 외부 주소의 호스트(주소 유래)"),
    ("host", "hostname"): (FREE, "data.egress.host — 차단된 외부 주소의 호스트(주소 유래)"),
    ("how_to_approve", "approval_id"): (FREE, "승인 안내 문장(f-string)"),
    ("signals", "signals"): (FREE, "actions/verification.py 사후조건 진단 문장(`url_changed: A -> B`·값 원문 포함)"),
    ("unresolved", "__name__"): (FREE, "좌표 판정 불가 사유 문장"),
    # --- PAGE
    ("message", "message"): (PAGE, "actions/dispatcher.py data.dialogs[].message — 페이지가 쓴 대화상자 문구"),
    ("name", "element_name"): (PAGE, "security/hitl.py gate_basis.name — 요소 접근성 이름"),
    ("name", "name"): (PAGE, "actions/dispatcher.py data.som_tags[].name — 요소 접근성 이름"),
    ("role", "role"): (PAGE, "actions/dispatcher.py data.som_tags[].role — 요소 role(페이지 role 속성 가능)"),
    ("selector_path", "selector_path"): (PAGE, "actions/dispatcher.py data.som_tags[].selector_path — 페이지 구조 경로"),
    ("rule", "rule"): (PAGE, "security/robots_signal.py data.robots.rule — 사이트 robots.txt 규칙 원문"),
    # --- STRUCT
    ("bbox", "model_dump()"): (STRUCT, "SoM 상자 숫자 dict"),
    ("frame_bbox", "bounding_box()"): (STRUCT, "프레임 상자 숫자 dict"),
    ("control", "status()"): (STRUCT, "handoff.control_status dict(holder 등 — R5/R7 census)"),
    ("egress", "to_agent()"): (STRUCT, "EgressDecision.to_agent dict(code·category·reason — R7·이 census)"),
    ("quota", "snapshot()"): (STRUCT, "youtube QuotaLedger.snapshot 숫자·시각 dict"),
    ("window", "info()"): (STRUCT, "on_demand 창 정보 dict(mode·state — R5/R7 census)"),
    ("step_result", "step_result"): (STRUCT, "레시피 단계 ActionResult dict(봉투 정제 대상)"),
    # --- NOT_STR
    ("count", "count()"): (NOT_STR, "int"),
    ("match_count", "count()"): (NOT_STR, "int"),
    ("unresolved", "count()"): (NOT_STR, "int(문장 f-string 안 개수)"),
    ("epoch", "epoch()"): (NOT_STR, "int"),
    ("snapshot_epoch", "epoch"): (NOT_STR, "int"),
    ("http_status", "status"): (NOT_STR, "int"),
    ("last_http_status", "status"): (NOT_STR, "int"),
    ("image_tokens", "SOM_IMAGE_TOKENS_PER_CAPTURE"): (NOT_STR, "int 상수"),
    ("disallowed", "disallowed"): (NOT_STR, "bool"),
    ("requires_confirmation", "requires_confirmation"): (NOT_STR, "bool"),
    ("pid", "getpid()"): (NOT_STR, "int"),
    ("uid", "geteuid()"): (NOT_STR, "int"),
    ("started", "time()"): (NOT_STR, "float"),
    ("server_id", "getpid()"): (NOT_STR, "int(server.json·잠금 파일)"),
    # --- NOT_OUT
    ("action_digest", "digest"): (NOT_OUT, "interface/handoff.py approval_view — 사람 CLI·approvals 파일"),
    ("components", "components"): (NOT_OUT, "interface/handoff.py approval_view — 사람 CLI·approvals 파일"),
    ("summary", "summary"): (NOT_OUT, "interface/handoff.py approval_view — 사람 CLI·approvals 파일"),
    ("browser_mode", "browser"): (NOT_OUT, "interface/handoff.py server.json 상태 파일"),
    ("op", "op"): (NOT_OUT, "interface/handoff.py 명령 파일"),
    ("login_id", "login_id"): (NOT_OUT, "interface/handoff.py 내부 _apply 명령"),
    ("key", "hex()"): (NOT_OUT, "browser/headless_retry.py 상태 파일"),
    ("cssPath", "css_path"): (NOT_OUT, "actions/verification.py page.evaluate 인자"),
    ("profile", "profile"): (NOT_OUT, "interface/mcp_server.py 생성자 인자"),
    ("proxy", "playwright_proxy()"): (NOT_OUT, "security/egress_runtime.py 브라우저 실행 인자"),
    ("text", "value"): (NOT_OUT, "actions/dispatcher.py 자격증명 치환 params(Playwright 입력 — 응답에 싣지 않음)"),
    ("via", "method"): (NOT_OUT, "browser/session_reuse.py 세션 저장 파일 메타"),
    ("height", "VIEWPORT_HEIGHT"): (NOT_OUT, "vision/overlay.py 페이지 주입"),
    ("width", "VIEWPORT_WIDTH"): (NOT_OUT, "vision/overlay.py 페이지 주입"),
    ("x", "x"): (NOT_OUT, "vision/overlay.py 페이지 주입"),
    ("y", "y"): (NOT_OUT, "vision/overlay.py 페이지 주입"),
    # 파일별 예외
    ("action", "value", "recipes/recorder.py"): (NOT_OUT, "레시피 궤적 항목(파일)"),
    ("approval_id", "approval_id", "interface/handoff.py"): (ID, "approval_status(응답)·approval_view(파일) — ap_…"),
    ("outcome", "outcome", "interface/handoff.py"): (CONTROL, "approval_status — 승인 결과(approval_view 는 파일)"),
    ("reason", "reason", "interface/server_hitl.py"): (FREE, "승인 요청 사유 문장(hitl 판정 문장)"),
    ("reason", "reason", "interface/server_signals.py"): (FREE, "data.challenge.reason — browser.challenge 판정 문장"),
    ("name", "name", "browser/serve_profile.py"): (NOT_OUT, "profile CLI 목록(list_profiles — MCP 아님)"),
    ("tag", "tag", "vision/overlay.py"): (NOT_OUT, "페이지 주입"),
}

#: 목록 안 dict 의 키 경로로 면제되는 CONTROL 칸: (키, 파일) → 목록 키.
ITEM_PATHS = {("type", "actions/dispatcher.py"): "dialogs"}


def _skip(r: AttrRecord) -> bool:
    return any(r.file.startswith(p) for p in NOT_OUT_FILES)


def _classify(r: AttrRecord):
    return ATTRS.get((r.key, r.attr, r.file)) or ATTRS[(r.key, r.attr)]


@pytest.fixture(scope="module")
def records():
    return [r for r in attr_records() if not _skip(r)]


def test_census_does_not_read_consumer_table():
    src = (Path(__file__).resolve().parent / "ws51_r8_census.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any("public_url" in (m or "") for m in imported), imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"CONTROL_VOCABULARY", "control_value", "control_item_value", "CONTROL_ITEM_VOCABULARY",
                        "public_url"}, names


def test_census_finds_r7_b1(records):
    """조건 성립: 소비자 표 없이 dialogs[].type 의 속성 출처(지역 이름 kind = str(dialog.type) 를 거친 값)를 찾는다."""
    got = {(r.key, r.expr, r.file) for r in records}
    assert ("type", "dialog.type", "actions/dispatcher.py") in got
    assert ("vendor", "found.vendor", "interface/server_signals.py") in got
    assert ("healing_attempts", "healing.attempts", "actions/dispatcher.py") in got
    assert len(records) >= 120  # 빈 조사 통과 방지(실측 158 자리)


def test_every_attr_slot_classified(records):
    pairs = {(r.key, r.attr) for r in records}
    missing = sorted(p for p in pairs if p not in ATTRS)
    assert missing == [], "새 속성 전달 칸 — ATTRS 에 분류와 근거를 추가"
    used = pairs | {(r.key, r.attr, r.file) for r in records}
    assert sorted(k for k in ATTRS if k not in used) == [], "사라진 칸 — ATTRS 정리"


# ------------------------------------------------------------------ CONTROL — 생산 쪽 값(소비자 표를 읽지 않는다)

def _dialog_types():
    """Playwright Dialog.type 문서의 값(`alert`·`beforeunload`·`confirm`·`prompt`) + 디스패처의 읽기 실패 폴백."""
    from playwright.async_api import Dialog

    doc = Dialog.type.__doc__ or ""
    found = set(re.findall(r"`([a-z]+)`", doc.split("can be one of", 1)[1]))
    assert found == {"alert", "beforeunload", "confirm", "prompt"}, doc
    return found | {"unknown"}


def _vendors():
    from browser import challenge

    js = set(re.findall(r"'(hcaptcha|cloudflare|recaptcha|generic)'", challenge._PROBE_JS))
    phrases = {v for _, v, _ in challenge._CAPTCHA_PHRASES + challenge._BLOCKED_PHRASES}
    lits = {v for v in re.findall(r'"([a-z]+)"', Path(challenge.__file__).read_text(encoding="utf-8"))
            if v in ("generic", "akamai")}
    assert js == {"hcaptcha", "cloudflare", "recaptcha", "generic"} and lits == {"generic", "akamai"}
    return js | phrases | lits


def _producer_values():
    from actions.healing import HealingStrategy
    from actions.pdf_document import FetchResult  # noqa: F401 — 생산자 존재
    from browser.challenge import ChallengeKind
    from interface import youtube_search
    from security import egress
    from security.hitl import RiskLevel

    return {
        ("type", "type"): _dialog_types(),
        ("vendor", "vendor"): _vendors(),
        ("healing_attempts", "attempts"): {s.value for s in HealingStrategy},
        ("kind", "value"): {k.value for k in ChallengeKind},
        ("reason", "value"): {b.value for b in egress.BlockReason},
        ("risk", "value"): {r.value for r in RiskLevel},
        ("action", "value"): {a.value for a in ActionType},
        ("code", "code"): {egress.UPSTREAM_RESOLVE_FAILED, egress.UPSTREAM_CONNECT_FAILED},
        ("changed", "last_event"): {"taken", "released"},  # server_handoff: last_event in ("taken", "released") 일 때만
        ("status", "status"): {"pending", "approved", "denied", "expired", "used", "revoked", "opening", "ready"},
        ("outcome", "outcome"): {"succeeded", "failed", "outcome_unknown"},
        ("reason", "reason"): set(youtube_search._REASONS) | {"page_changed", "not_ready", "target_not_found",
                                                             "target_ambiguous", "approval_required", "action_failed",
                                                             "expect_mismatch", "fetch_failed", "not_pdf"},
        ("expires_at", "isoformat()"): {"2026-10-17T00:00:00+00:00"},
    }


def _exempt(key, file, value):
    pu = _pu()
    item = ITEM_PATHS.get((key, file))
    if item is not None:
        return pu.control_item_value(item, key, value)
    return pu.control_value(key, value)


def test_control_attr_values_exempt(records):
    rows = [r for r in records if _classify(r)[0] == CONTROL]
    values = _producer_values()
    assert {(r.key, r.attr) for r in rows} == set(values), "CONTROL 칸마다 생산 쪽 값 집합이 있어야 한다"
    bad = sorted({(r.key, v, r.file) for r in rows for v in values[(r.key, r.attr)] if not _exempt(r.key, r.file, v)})
    assert bad == []


def test_id_attr_values_never_registered(records):
    """ID 칸 값 모양은 등록 제외 — 실제 주소 관측으로도 등록되지 않는다."""
    samples = {"tab-3", "ap_Zx9-abc", "@e12", "@s4", "12345-a1b2c3", "A1", "J9"}
    assert all(sr._eligible(v) is None for v in samples)
    assert {_classify(r)[0] for r in records if r.key in ("closed", "active", "tag") and r.file.startswith("actions/")} \
        == {ID}


def test_url_attr_keys_are_url_fields(records):
    pu = _pu()
    assert all(pu._is_url_key(r.key) for r in records if _classify(r)[0] == URL)


def test_free_page_keys_not_wholesale_exempt(records):
    """자유 문장·페이지 값 칸은 키 통째 면제가 아니다(식별자 형식·어휘의 정확 일치만 예외)."""
    pu = _pu()
    rows = [r for r in records if _classify(r)[0] in (FREE, PAGE)]
    assert rows
    assert not [r for r in rows if pu.control_value(r.key, "synthetic page words")]
    assert not pu.control_item_value("dialogs", "message", "beforeunload")


# ------------------------------------------------------------------ wire 불변(브라우저 없음 — 실 Chromium 은 interface E2E)

def _wire(data):
    from interface.mcp_server import envelope_json

    res = ActionResult(success=True, action=ActionType.OBSERVE_PAGE, current_url="http://127.0.0.1/",
                       snapshot_epoch=0, tab_id="tab-1", retry_safe=True, data=data)
    return json.loads(envelope_json(res))["data"]


@pytest.fixture
def fresh_registry():
    sr.clear()
    yield
    sr.clear()


def _register(value, monkeypatch):
    """등록 제외(_eligible None)를 끈 이중 방어 조건으로 강제 등록."""
    eligible = sr._eligible
    monkeypatch.setattr(sr, "_eligible", lambda b: (len(b) >= 8) if b == value and eligible(b) is None else eligible(b))
    sr.register_url("http://127.0.0.1/seen?token=" + value)
    from security.public_url import public_text

    assert public_text(f"x {value} y") == f"x {R} y", value


@pytest.mark.parametrize("value", sorted(_dialog_types()))
def test_dialogs_type_survives_registration(fresh_registry, monkeypatch, value):
    _register(value, monkeypatch)
    out = _wire({"dialogs": [{"type": value, "message": f"about {value} here", "handled": "accepted"}]})
    assert out["dialogs"][0]["type"] == value
    assert out["dialogs"][0]["message"] == f"about {R} here"


def test_type_elsewhere_not_exempt(fresh_registry, monkeypatch):
    """키 경로 기준 — dialogs 목록 안 dict 의 type 만. 다른 위치의 type 은 가린다."""
    _register("beforeunload", monkeypatch)
    out = _wire({"type": "beforeunload", "items_x": [{"type": "beforeunload"}], "dialogs_meta": {"type": "beforeunload"},
                 "nested": {"dialogs": {"type": "beforeunload"}}, "other": [{"dialogs": [{"kind": "beforeunload"}]}],
                 "dialogs": [{"type": "beforeunload", "inner": {"type": "beforeunload"}}, ["beforeunload"]]})
    assert out["type"] == R and out["items_x"][0]["type"] == R and out["dialogs_meta"]["type"] == R
    assert out["nested"]["dialogs"]["type"] == R  # dict(목록 아님) 안의 type
    assert out["other"][0]["dialogs"][0]["kind"] == R
    assert out["dialogs"][0]["type"] == "beforeunload"
    assert out["dialogs"][0]["inner"]["type"] == R and out["dialogs"][1] == [R]
    assert not _pu().control_value("type", "beforeunload")  # 키만으로는 면제 아님


@pytest.mark.parametrize("value", sorted(_vendors()))
def test_challenge_vendor_survives(fresh_registry, monkeypatch, value):
    from browser.challenge import Challenge, ChallengeKind

    ch = Challenge(ChallengeKind.CAPTCHA, f"사유 {value} 문장", value)
    _register(value, monkeypatch)
    out = _wire({"challenge": {"kind": ch.kind.value, "vendor": ch.vendor, "reason": ch.reason}})
    assert out["challenge"]["vendor"] == value and out["challenge"]["kind"] == "captcha"
    assert out["challenge"]["reason"] == f"사유 {R} 문장"


def test_challenge_classify_vendors_real_producer(fresh_registry, monkeypatch):
    """실제 판정 함수(browser.challenge.classify)가 낸 vendor 값이 등록 뒤에도 같다."""
    from browser.challenge import classify

    produced = {classify("t", "", 10, w).vendor for w in ("hcaptcha", "cloudflare", "recaptcha", "generic")}
    produced.add(classify("t", "Access Denied reference #1", 30, "").vendor)
    produced.add(classify("t", "x", 10, "", 403).vendor)
    assert produced == {"hcaptcha", "cloudflare", "recaptcha", "generic", "akamai"}
    for v in produced:
        _register(v, monkeypatch)
    out = _wire({"challenges": [{"vendor": v} for v in sorted(produced)]})
    assert [c["vendor"] for c in out["challenges"]] == sorted(produced)


def test_healing_attempts_survive(fresh_registry, monkeypatch):
    from actions.healing import HealingStrategy

    vals = [s.value for s in HealingStrategy]
    for v in vals:
        _register(v, monkeypatch)
    out = _wire({"healing_attempts": vals, "note": " ".join(vals)})
    assert out["healing_attempts"] == vals
    assert R in out["note"] and not any(v in out["note"] for v in vals)
