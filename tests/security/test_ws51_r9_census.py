"""WS-51 R9 — 컨테이너 안 코드 census 고정(R8-B1·B2, 소비자 표 독립 AST — tests/security/ws51_r9_census.py).

R8 census 는 키 칸에 실리는 속성 값만 보아, **목록 원소로 쌓는 복합 코드**(`attempts[-1] = f"{strategy.value}(ambiguous)"`)와
**신호 목록의 순수 코드**(`signals.append("text_changed")`)를 놓쳤다. 이 테스트는:

1. src/ 전체의 '목록에 문자열을 넣는 자리'(`item_records`)를 (컨테이너, 파일) 단위로 전부 분류한다 — 새 자리가 생기면
   실패해 분류를 요구한다(`CONTAINERS`).
2. healing_attempts(목록 경로 어휘 `CONTROL_LIST_VOCABULARY` — 복합 코드는 R5 의 '낱말만' 어휘 불변을 지키려 키 어휘가 아니다) — heal() 의 실제 원소 틀(`{strategy.value}`·`{strategy.value}(…)`)에서 결과 접미를 **생산 코드에서
   읽어** `HealingStrategy` × 접미로 전체 코드를 도출한다(손 목록 없음 — heal() 이 새 접미·새 모양을 쓰면 실패). 실제 heal()
   을 일반 입력으로 돌려 낸 코드가 그 도출 안이고, 도출 전체가 소비자 어휘와 **같다**.
3. signals — 신호 컨테이너의 틀마다 판정한다: 칸 없는 틀(`text_changed`)은 도구 고정 코드, 칸 있는 틀은 칸의 값 집합이
   유한한 도구 어휘면(불리언·대화상자 type·검증 코드) 그 펼침이 코드, 아니면(주소·페이지 id·입력값·숫자) 자유 문장(정제).
   도출 코드 전체가 소비자 `CONTROL_LIST_VOCABULARY["signals"]` 와 **같다**. 면제는 `signals` 목록 바로 아래 문자열
   원소에만 — 같은 키의 문자열·다른 목록·안쪽 목록/dict 는 가린다. 자유 문장 칸의 등록 값은 가린다(R4-B3 재발 금지).

보고서 표: .hermes/state/ws51-r9/report.md.
"""

from __future__ import annotations

import ast
import itertools
import json
import re
import sys
from pathlib import Path

import pytest

from contracts import ActionResult, ActionType
from security import secret_registry as sr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ws51_r7_census import NOT_OUT_FILES  # noqa: E402
from test_ws51_r8_census import _dialog_types  # noqa: E402
from ws51_r9_census import ItemRecord, item_records  # noqa: E402

R = "[REDACTED]"
CODES, SIGNALS, FREE, NOT_OUT = "CODES", "SIGNALS", "FREE", "NOT_OUT"


def _pu():
    import security.public_url  # noqa: F401

    return sys.modules["security.public_url"]


#: (컨테이너, 파일) → (분류, 근거). 컨테이너만(파일 None)이면 모든 파일.
CONTAINERS = {
    ("attempts", "actions/healing.py"): (CODES, "heal() → HealingResult.attempts → dispatcher data.healing_attempts"),
    ("signals", "actions/verification.py"): (SIGNALS, "verify_post_condition → PostConditionResult.signals → data.signals"),
    ("signals", "actions/dispatcher.py"): (SIGNALS, "data.signals 리터럴(navigation_started·file_saved)"),
    ("extra", "actions/dispatcher.py"): (SIGNALS, "_with_outside_effects → post.signals + extra → data.signals"),
    ("<return:_dialog_signals>", "actions/dispatcher.py"): (SIGNALS, "dialog_opened:{type} → data.signals"),
    ("<return:_top_effect_signals>", "actions/dispatcher.py"): (SIGNALS, "top:{신호} → data.signals"),
    ("__all__", None): (NOT_OUT, "모듈 재수출 목록"),
    ("<expr>", "actions/pdf_document.py"): (NOT_OUT, "_rss_bytes — /bin/ps argv"),
    ("<expr>", "interface/handoff.py"): (NOT_OUT, "_process_start_time — ps argv"),
    ("<expr>", "browser/user_chrome.py"): (NOT_OUT, "launch_user_chrome — Chrome argv"),
    ("<expr>", "browser/serve_profile.py"): (FREE, "safe_reason — 사람용 사유 문장 조각 결합"),
    ("<return:changed_kinds>", "actions/group_changed.py"): (FREE, "WS-62 R17 data.group_changed.changed.kinds — "
                                                                  "group_changed.KINDS 고정 어휘만(목록 경로 어휘 아님 "
                                                                  "→ public_text 정제, 어휘 낱말은 그대로)"),
    ("<expr>", "actions/group_changed.py"): (FREE, "WS-62 R17 changed_kinds 의 KINDS 거르기·hint 의 종류 낱말 결합"),
    ("<expr>", "security/egress_proxy.py"): (NOT_OUT, "_relay_response_head — 프록시 HTTP 응답 머리"),
    ("<expr>", "interface/server_elicit.py"): (NOT_OUT, "WS-56 elicitation_message — 클라이언트 사람 확인 문구 줄"
                                                  "(도구 응답 출구 아님, 표시 살균·가림 적용)"),
    ("<return:background_network_off_args>", "browser/core.py"): (NOT_OUT, "Chromium 실행 인자"),
    ("<return:build_chrome_args>", "browser/user_chrome.py"): (NOT_OUT, "Chrome 실행 인자"),
    ("<return:_launch_args>", "interface/code_window.py"): (NOT_OUT, "확인 코드 창 실행 인자"),
    ("<return:chromium_proxy_args>", "security/egress_proxy.py"): (NOT_OUT, "Chromium 실행 인자"),
    ("<return:chrome_args>", "security/egress_proxy.py"): (NOT_OUT, "Chrome 실행 인자"),
    ("result", "browser/headless_retry.py"): (NOT_OUT, "headed_brand_list — UA 브랜드(브라우저 설정)"),
    ("states", "browser/headless_retry.py"): (NOT_OUT, "재시도 상태 파일"),
    ("evidence", "browser/session_probe.py"): (NOT_OUT, "detect_expiry 근거 — session CLI(stdout)·하네스만"),
    ("lines", "interface/handoff.py"): (NOT_OUT, "_show_approval — 사람 CLI 승인 화면"),
    ("lines", "security/egress_proxy.py"): (NOT_OUT, "_forward_http — 업스트림 HTTP 요청 머리"),
    ("head", "security/egress_proxy.py"): (NOT_OUT, "_reply — 프록시 HTTP 응답 머리"),
    ("out", "interface/handoff.py"): (FREE, "display_safe — 사람용 표시 문자열(정제 대상 문장)"),
    ("out", "interface/mcp_server.py"): (NOT_OUT, "_neutralize_masked_specs — 로그 서식 문자열"),
    ("out", "recipes/store.py"): (FREE, "clean_text — 레시피 이름 문장 정리"),
    ("words", "recipes/store.py"): (FREE, "_auto_name — 레시피 자동 이름(사람용 문장)"),
    ("stages_used", "perception/engine.py"): (NOT_OUT, "RecoveryTrace — 엔진 내부 기록(응답에 싣지 않음)"),
    ("reasons", "perception/scorer.py"): (NOT_OUT, "ScoredElement.reasons — 스코어러 내부(ObservedElement 에 없음)"),
    ("table", "security/secret_registry.py"): (NOT_OUT, "_quote_table — 내부 인코딩 표"),
    ("text", "vision/grounder.py"): (NOT_OUT, "VLM 요청 메시지"),
}

#: 신호 틀(칸 있는 것) → 판정. CODES 면 칸 값 집합(생산 쪽 — 소비자 표를 읽지 않는다), FREE 면 근거.
_BOOL = (True, False)
_OPT_BOOL = (True, False, None)


def _verify_codes(records):
    """verification.py 신호 틀에서 도출한 도구 고정 코드(칸 없는 틀 + 유한 칸 펼침)."""
    return {c for r in records if (r.container, r.file) == ("signals", "actions/verification.py")
            for c in _expand(r.template, records)}


def _hole_domains(records):
    return {
        "checked_applied: {expected_checked}": [(b,) for b in _BOOL],
        "element_checked_changed: {before.element_checked} -> {after.element_checked}":
            [(a, b) for a, b in itertools.product(_OPT_BOOL, _OPT_BOOL) if a != b],
        "dialog_opened:{d['type']}": [(t,) for t in sorted(_dialog_types())],
        "top:{s}": None,  # 검증 코드 전체(_verify_codes) — 재귀
    }


SIGNAL_FREE = {
    "value_applied: {shown}": "입력값 repr(또는 <secret>) — 입력 원문이 든 문장",
    "url_changed: {before.url} -> {after.url}": "주소 원문",
    "dom_delta: {before.dom_node_count} -> {after.dom_node_count}": "숫자 — 4~7자 등록 값과 경계 일치 가능, 면제 불가",
    "element_value_changed: {before.element_value} -> {after.element_value}": "필드 값 원문",
    "new_tab: {before.page_count} -> {after.page_count}": "숫자(위와 같은 이유)",
    "focus_moved: {before.active_element} -> {after.active_element}": "페이지 tag#id",
    "navigated: {self._current_url()}": "주소 원문",
    "file_saved: {downloaded}": "내려받은 파일 경로(페이지 유래 파일 이름)",
}
_HOLE = re.compile(r"\{[^{}]*\}")


def _expand(template, records):
    """CODES 틀의 펼침(칸 없는 틀은 자기 자신, FREE 틀은 빈 집합)."""
    if "{" not in template:
        return {template}
    if template in SIGNAL_FREE:
        return set()
    domains = _hole_domains(records)
    assert template in domains, f"새 신호 틀 — CODES(칸 값 집합) 또는 SIGNAL_FREE 로 분류: {template!r}"
    if template == "top:{s}":
        return {f"top:{c}" for c in _verify_codes(records)}
    parts = _HOLE.split(template)
    out = set()
    for values in domains[template]:
        assert len(values) == len(parts) - 1
        out.add("".join(p + (str(v) if i < len(values) else "") for i, (p, v) in
                        enumerate(itertools.zip_longest(parts, values, fillvalue=""))))
    return out


@pytest.fixture(scope="module")
def records():
    return [r for r in item_records() if not any(r.file.startswith(p) for p in NOT_OUT_FILES)]


def _klass(r: ItemRecord):
    return CONTAINERS.get((r.container, r.file)) or CONTAINERS[(r.container, None)]


def test_census_does_not_read_consumer_table():
    src = (Path(__file__).resolve().parent / "ws51_r9_census.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names} | {
        n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {n for n in names if "public_url" in n or "CONTROL" in n or n.startswith("control_")}, names


def test_census_finds_r8_b1_b2(records):
    """조건 성립: 소비자 표 없이 복합 코드·순수 신호 코드 생산 자리를 찾는다."""
    got = {(r.container, r.template, r.file) for r in records}
    assert ("attempts", "{strategy.value}(identity_changed)", "actions/healing.py") in got
    assert ("attempts", "{strategy.value}(ambiguous)", "actions/healing.py") in got
    assert ("signals", "text_changed", "actions/verification.py") in got
    assert ("signals", "element_attr_changed", "actions/verification.py") in got
    assert ("signals", "navigation_started", "actions/dispatcher.py") in got
    assert len(records) >= 200  # 빈 조사 통과 방지(실측 257 자리)


def test_every_container_classified(records):
    groups = {(r.container, r.file) for r in records}
    missing = sorted(g for g in groups if g not in CONTAINERS and (g[0], None) not in CONTAINERS)
    assert missing == [], "새 목록 원소 생산 자리 — CONTAINERS 에 분류와 근거를 추가"
    used = groups | {(c, None) for c, _ in groups}
    assert sorted(k for k in CONTAINERS if k not in used) == [], "사라진 자리 — CONTAINERS 정리"


# ------------------------------------------------------------------ healing_attempts(R8-B1)

_ATTEMPT = re.compile(r"\{strategy\.value\}(?:\((?P<outcome>[a-z_]+)\))?")


def _heal_codes(records):
    """heal() 원소 틀에서 도출: HealingStrategy 값 × (없음 | 접미). 다른 모양의 원소가 있으면 실패."""
    from actions.healing import HealingStrategy

    rows = [r for r in records if _klass(r)[0] == CODES]
    assert rows and {r.func for r in rows} == {"heal"}
    outcomes = set()
    for r in rows:
        m = _ATTEMPT.fullmatch(r.template)
        assert m, f"heal() 의 새 attempts 원소 모양 — 도출 규칙·소비자 어휘 갱신 필요: {r.template!r}"
        outcomes.add(m.group("outcome"))
    assert None in outcomes and len(outcomes) >= 2  # 기본값 + 복합 접미가 실제로 있다
    strategies = {s.value for s in HealingStrategy}
    return strategies | {f"{s}({o})" for s in strategies for o in outcomes if o}


def _heal_runs():
    """실제 heal() 를 일반 입력으로 — 단계마다 신원 변경 거부·모호함 분기를 실제로 지난다."""
    from actions.healing import HealingCandidate, heal
    from perception.engine import ElementHandle

    def h(**kw):
        base = dict(element_id="@e1", epoch=0, role="button", name="Open notes", css_path="#a", is_shadow=False)
        base.update(kw)
        return ElementHandle(**base)

    def c(eid, **kw):
        base = dict(element_id=eid, role="button", name="Open notes", css_path="", testid=None)
        base.update(kw)
        return HealingCandidate(**base)

    runs = [
        (h(), [c("@e2", name="Open notes!")]),  # text_similarity(identity_changed)
        (h(), [c("@e2", name="Open notes!"), c("@e3", name="Open notes?")]),  # text_similarity(ambiguous)
        (h(testid="t1"), [c("@e2", name="Delete all", testid="t1")]),  # testid(identity_changed)
        (h(), [c("@e2", name="Delete all", css_path="#a")]),  # css_path(identity_changed)
        (h(is_shadow=True), [c("@e2", name="Delete all", css_path="#a")]),  # css_piercing(identity_changed)
        (h(), [c("@e2")]),  # role_name 채택
        (h(), []),  # 전부 실패
    ]
    produced = set()
    for target, cands in runs:
        produced |= set(heal(target, cands, action=ActionType.CLICK).attempts)
    return produced


def test_heal_codes_derived_from_producer_equal_vocabulary(records):
    derived = _heal_codes(records)
    produced = _heal_runs()
    assert produced <= derived, produced - derived
    # 조건 성립: 검증자 보고의 복합 코드 5개가 실제 heal() 에서 나온다.
    assert {"css_path(identity_changed)", "css_piercing(identity_changed)", "testid(identity_changed)",
            "text_similarity(ambiguous)", "text_similarity(identity_changed)"} <= produced, produced
    assert derived == _pu().CONTROL_LIST_VOCABULARY["healing_attempts"]
    assert all(_pu().control_list_value("healing_attempts", v) for v in derived)
    assert not _pu().control_list_value("healing_attempts", "text_similarity(other)")
    assert not _pu().control_list_value("healing_attempts", "text_similarity(identity_changed) x")
    assert not any(_pu().control_value("healing_attempts", v) for v in derived)  # 키만으로는 면제 아님


# ------------------------------------------------------------------ signals(R8-B2)


def _signal_codes(records):
    rows = [r for r in records if _klass(r)[0] == SIGNALS]
    assert rows
    return {c for r in rows for c in _expand(r.template, records)}


def test_signal_templates_all_judged(records):
    rows = [r for r in records if _klass(r)[0] == SIGNALS]
    holes = {r.template for r in rows if "{" in r.template}
    assert holes == set(SIGNAL_FREE) | set(_hole_domains(records)), "신호 틀 표와 생산 틀이 다르다"


def test_signal_codes_derived_from_producer_equal_vocabulary(records):
    codes = _signal_codes(records)
    assert {"text_changed", "element_attr_changed", "navigation_started", "top:text_changed",
            "dialog_opened:beforeunload"} <= codes
    assert codes == _pu().CONTROL_LIST_VOCABULARY["signals"]
    assert not any(_pu().control_value("signals", c) for c in codes)  # 키만으로는 면제 아님(목록 원소 경로만)


def test_real_verify_post_condition_codes_in_vocabulary():
    from actions.verification import PageStateSnapshot, verify_post_condition

    b = PageStateSnapshot(url="http://127.0.0.1/p", dom_node_count=1, text_signature=1, element_signature=1,
                          element_checked=False)
    a = PageStateSnapshot(url="http://127.0.0.1/p", dom_node_count=1, text_signature=2, element_signature=2,
                          element_checked=True)
    sig = verify_post_condition(b, a).signals
    assert {"text_changed", "element_attr_changed", "element_checked_changed: False -> True"} <= set(sig), sig
    assert set(sig) <= _pu().CONTROL_LIST_VOCABULARY["signals"]


# ------------------------------------------------------------------ wire 불변·자유 문장 정제


def _wire(data):
    from interface.mcp_server import envelope_json

    res = ActionResult(success=False, action=ActionType.CLICK, current_url="http://127.0.0.1/",
                       snapshot_epoch=0, tab_id="tab-1", retry_safe=True, data=data)
    return json.loads(envelope_json(res))["data"]


@pytest.fixture
def fresh_registry():
    sr.clear()
    yield
    sr.clear()


def _register(value, monkeypatch):
    """등록 제외(_eligible None — 짧은 알파벳·불리언 조각)를 끈 이중 방어 조건으로 강제 등록."""
    eligible = sr._eligible
    monkeypatch.setattr(sr, "_eligible", lambda b: (len(b) >= 8) if b == value and eligible(b) is None else eligible(b))
    sr.register_url("http://127.0.0.1/seen?token=" + value)
    from security.public_url import public_text

    assert public_text(f"x {value} y") == f"x {R} y", value


def test_heal_codes_survive_registration(fresh_registry, monkeypatch, records):
    codes = sorted(_heal_codes(records))
    for v in codes:
        _register(v, monkeypatch)
    out = _wire({"healing_attempts": codes, "note": " ".join(codes), "other": codes, "healing_x": {"healing_attempts": codes},
                 "bare": {"healing_attempts": codes[0]}, "nest": {"healing_attempts": [codes]}})
    assert out["healing_attempts"] == codes and out["healing_x"]["healing_attempts"] == codes
    assert not any(v in out["note"] for v in codes)
    assert out["other"] == [R] * len(codes) and out["bare"]["healing_attempts"] == R  # 목록 경로 밖은 가린다
    assert out["nest"]["healing_attempts"] == [[R] * len(codes)]


def test_signal_codes_survive_registration(fresh_registry, monkeypatch, records):
    codes = sorted(_signal_codes(records))
    for v in codes:
        _register(v, monkeypatch)
    out = _wire({"signals": codes, "nested": {"signals": codes}, "steps": [{"signals": codes}]})
    assert out["signals"] == codes and out["nested"]["signals"] == codes and out["steps"][0]["signals"] == codes


def test_signal_exemption_is_list_path_only(fresh_registry, monkeypatch):
    _register("text_changed", monkeypatch)
    out = _wire({"signals": "text_changed", "other": ["text_changed"], "items_x": [{"signals_x": ["text_changed"]}],
                 "signals_meta": {"kind": "text_changed"},
                 "nest": {"signals": [["text_changed"], {"kind": "text_changed"}, "text_changed"]}})
    assert out["signals"] == R and out["other"] == [R] and out["items_x"][0]["signals_x"] == [R]
    assert out["signals_meta"]["kind"] == R
    assert out["nest"]["signals"] == [[R], {"kind": R}, "text_changed"]


@pytest.mark.parametrize("template", sorted(SIGNAL_FREE))
def test_free_signal_detail_masked(fresh_registry, monkeypatch, template):
    """자유 문장 칸에 등록 값을 넣으면 가린다(목록 통째 면제 아님 — R4-B3 재발 금지)."""
    secret = "SyntheticSecret88"
    _register(secret, monkeypatch)
    sample = _HOLE.sub(secret, template)
    out = _wire({"signals": ["text_changed", sample, "top:" + sample]})
    assert out["signals"][0] == "text_changed"
    assert secret not in json.dumps(out) and all(R in s for s in out["signals"][1:]), out
