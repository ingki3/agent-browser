"""WS-51 R6 — 도구 식별자 생산자 census 일반화(R5-F3) 고정.

`ws51_r6_census.id_candidates` 가 src/ 전체의 식별자 모양 문자열 조립(f-string·`+` 사슬·`%`·`.format`, 상수 이름 접두사·
동적 접두사·임의 산술 포함)을 전부 내고, 아래 표가 그 전부를 분류한다(새 후보가 생기면 테스트가 바뀐다).
면제 분류는 실제 생산자 모양의 표본이 등록 제외 + 제어 칸 값 불변인지 확인한다.
"""

from __future__ import annotations

import secrets
import sys
import textwrap
from pathlib import Path
from urllib.parse import quote

import pytest

from security import secret_registry as sr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ws51_r5_census import id_producers  # noqa: E402
from ws51_r6_census import assembly_counts, id_candidates  # noqa: E402


def _pu():
    import security.public_url  # noqa: F401

    return sys.modules["security.public_url"]


EXEMPT = "도구 식별자 — 등록 제외 + 제어 키 면제"
VOCAB = "제어 키 열거 어휘 — 제어 키 면제(등록은 됨, 다른 칸에서는 가림)"
NOT_OUT = "MCP 응답 출구 아님"

#: (파일, 식) → (분류·근거, 형식 이름(TOOL_ID_FORMATS) 또는 (제어 키, None), 표본 생성기 또는 None).
CANDIDATES = {
    # --- 도구 식별자(등록 제외 + 제어 면제)
    ("browser/core.py", "f'tab-{self._tab_counter}'"): (EXEMPT, "tab_id", lambda: f"tab-{secrets.randbelow(999)}"),
    ("perception/engine.py", "f'@e{idx}'"): (EXEMPT, "element_id", lambda: f"@e{10 + secrets.randbelow(9990)}"),
    ("recipes/service.py", "f'@r{self._seq}'"): (EXEMPT, "element_id", lambda: f"@r{10 + secrets.randbelow(9990)}"),
    ("vision/bridge.py", "f'{SOM_ID_PREFIX}{used + 1}'"): (EXEMPT + " (R6 신규 — 상수 이름 접두사·산술)", "element_id",
                                                         lambda: f"@s{10 + secrets.randbelow(9990)}"),
    ("recipes/store.py", "f'{prefix}{counters[prefix]}'"): (
        EXEMPT + " (R6 신규 — 동적 접두사 AUTO_PARAM_PREFIX·AUTO_CHOICE_PREFIX, 응답 recipe.params)", "param_name",
        lambda: secrets.choice(("text", "choice")) + str(secrets.randbelow(99) + 1)),
    ("recipes/store.py", "'r_' + _secrets.token_hex(4)"): (EXEMPT, "recipe_id", lambda: "r_" + secrets.token_hex(4)),
    ("interface/handoff.py", "f'rq_{secrets.token_urlsafe(9)}'"): (EXEMPT, "request_id",
                                                                 lambda: "rq_" + secrets.token_urlsafe(9)),
    ("interface/handoff.py", "f'ap_{secrets.token_urlsafe(12)}'"): (EXEMPT, "approval_id",
                                                                  lambda: "ap_" + secrets.token_urlsafe(12)),
    ("interface/handoff.py", "f'{os.getpid()}-{secrets.token_hex(3)}'"): (EXEMPT, "server_id",
                                                                         lambda: f"4321-{secrets.token_hex(3)}"),
    ("interface/login_cli.py", "'login-' + secrets.token_hex(4)"): (EXEMPT, "server_id",
                                                                  lambda: "login-" + secrets.token_hex(4)),
    ("interface/server_schema.py", "f'{TOOL_PREFIX}{action.value}'"): (EXEMPT + " (도구 이름)", "tool_name",
                                                                       lambda: "browser_" + secrets.choice(
                                                                           ("click", "navigate", "observe_page"))),
    # --- 제어 키 열거 어휘
    ("actions/dispatcher.py", "f'status_{status}'"): (VOCAB + " (R6 신규 — data.nav_aborted: _NO_DOCUMENT_STATUS 204·205)",
                                                      ("nav_aborted", None), lambda: f"status_{secrets.choice((204, 205))}"),
    ("actions/pdf_document.py", "f'http_{response.status_code}'"): (VOCAB + " (CONTROL_PATTERNS['reason'])",
                                                                    ("reason", None),
                                                                    lambda: f"http_{400 + secrets.randbelow(200)}"),
    # --- 면제하지 않음(근거)
    ("interface/handoff.py", "f'{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}'"): (
        "확인 코드 — 사람 화면 전용 비밀(면제 금지)", None, None),
    ("vision/candidates.py", "f'{letter}{digit}'"): ("SoM 태그 — 1글자+1숫자 고정 2자 < MIN_VALUE_CHARS(4), 등록 불가 "
                                                     "(VLM 프롬프트 내부)", None, None),
    ("agent/jev_decider.py", "f't{i}'"): (NOT_OUT + " (에이전트 루프 내부 LLM criteria 키)", None, None),
    ("browser/serve_profile.py", "f'{DIR_PREFIX}{name}'"): (NOT_OUT + " (디스크 프로필 디렉터리 이름)", None, None),
    ("browser/serve_profile.py", "f'{EPHEMERAL_PREFIX}{server_id}'"): (NOT_OUT + " (디스크 임시 프로필 디렉터리 이름)",
                                                                       None, None),
    ("interface/profile_cli.py", "f'{n}B'"): (NOT_OUT + " (CLI 크기 표기)", None, None),
    ("interface/profile_cli.py", "f'{size:.0f}{unit}'"): (NOT_OUT + " (CLI 크기 표기)", None, None),
    ("interface/profile_cli.py", "f'{size:.1f}{unit}'"): (NOT_OUT + " (CLI 크기 표기)", None, None),
    ("harness/ipi_test.py", "f'a{i}'"): (NOT_OUT + " (하네스 표본 키)", None, None),
    ("harness/ipi_test.py", "f'u{i}'"): (NOT_OUT + " (하네스 표본 키)", None, None),
    ("harness/ipi_test.py", "f'v{i}'"): (NOT_OUT + " (하네스 표본 키)", None, None),
    ("harness/ipi_test.py", "f'b{i}'"): (NOT_OUT + " (하네스 표본 키)", None, None),
    ("harness/recall.py", "f'#{i}'"): (NOT_OUT + " (하네스 순위 표기)", None, None),
    ("harness/ws40_mock.py", "'N' + '😀' * 1000"): (NOT_OUT + " (하네스 Mock 본문)", None, None),
    ("perception/shadow_dom.py", "f\"{name}#{pairs['id']}\""): ("페이지 유래 CSS 경로 조각(id 는 페이지 속성) — 도구 "
                                                               "식별자 아님", None, None),
    ("interface/mcp_server.py", "f'{safe_key}#{n}'"): ("로그 레코드 속성 이름 충돌 꼬리(가린 비문자열 extra 키, R6 F1) — "
                                                       "출구 아님(출력은 handler_format 이 가림)", None, None),
    ("browser/chrome_cookies.py", "'Cookies' + suffix"): ("WS-59 Chrome 쿠키 DB 곁 파일(-journal·-wal) 사본 이름(경로 "
                                                           "조립) — 값 생산 아님", None, None),
    ("security/hitl.py", "key + '-'"): ("아이콘 클래스 접두 비교용 조각 — 값 생산 아님", None, None),
    ("security/public_url.py", "'#' + _public_fragment(fragment, depth, sink)"): ("주소 fragment 조립(이미 가린 값) — "
                                                                                  "식별자 아님", None, None),
}


@pytest.fixture(scope="module")
def found():
    return id_candidates()


def test_id_candidate_inventory_fixed(found):
    keys = {(f, e) for f, _, e, _ in found}
    assert keys - set(CANDIDATES) == set(), "새 식별자 모양 조립 — 분류(면제·어휘·출구 아님·비밀)를 CANDIDATES 에 추가"
    assert set(CANDIDATES) - keys == set(), "사라진 후보 — CANDIDATES 정리"
    assert assembly_counts()["assemblies"] >= 800  # 조사가 실제로 src 전체 조립 식을 봤다(빈 조사 통과 방지)


def test_r5_census_missed_som_prefix():
    """R5 `id_producers` 는 상수 이름 접두사 `@s` 를 놓쳤다 — 일반화 census 는 잡는다(R5-F3 원인 고정)."""
    r5 = {(f, e) for f, _, e in id_producers()}
    assert ("vision/bridge.py", "f'{SOM_ID_PREFIX}{used + 1}'") not in r5
    assert ("vision/bridge.py", "f'{SOM_ID_PREFIX}{used + 1}'") in {(f, e) for f, _, e, _ in id_candidates()}


_EXEMPT = [k for k, v in CANDIDATES.items() if isinstance(v[1], str)]


@pytest.mark.parametrize("where", _EXEMPT, ids=lambda k: f"{k[0]}:{k[1]}")
def test_exempt_identifier_samples_not_registered_and_kept(monkeypatch, where):
    _, fmt, make = CANDIDATES[where]
    keys = {"tab_id": "tab_id", "element_id": "element_id", "recipe_id": "id", "request_id": "request_id",
            "approval_id": "approval_id", "server_id": "server_id", "tool_name": "action", "param_name": "params"}
    sr.clear()
    try:
        for _ in range(50):
            value = make()
            assert sr.TOOL_ID_FORMATS[fmt].fullmatch(value), (fmt, value)
            assert sr.is_tool_token(value)
            sr.register_url("http://127.0.0.1/seen?token=" + quote(value, safe=""))
            assert len(sr.REGISTRY) == 0, value
        monkeypatch.setattr(sr, "is_tool_token", lambda _v: False)  # 등록 제외가 깨진 경우(이중 방어 — 제어 키만으로)
        sr.REGISTRY.add_values([value])
        assert _pu().public_text(f"x {value} y") == "x [REDACTED] y", value  # 조건 성립: 실제로 등록됐다
        payload = {"data": {keys[fmt]: value}}
        assert _pu().public_payload(payload) == payload
        listed = {"data": {keys[fmt]: [value]}}
        assert _pu().public_payload(listed) == listed
    finally:
        sr.clear()


_VOCAB = [k for k, v in CANDIDATES.items() if isinstance(v[1], tuple)]


@pytest.mark.parametrize("where", _VOCAB, ids=lambda k: f"{k[0]}:{k[1]}")
def test_vocabulary_samples_kept_in_control_key(where):
    _, (key, _), make = CANDIDATES[where]
    sr.clear()
    try:
        for _ in range(20):
            value = make()
            sr.REGISTRY.add_values([value])
            assert _pu().control_value(key, value), (key, value)
            payload = {"data": {key: value}}
            assert _pu().public_payload(payload) == payload
            assert _pu().public_payload({"data": {"note": value}})["data"]["note"] == "[REDACTED]"  # 다른 칸은 가림
    finally:
        sr.clear()


def test_som_tag_below_registration_length():
    from vision.candidates import make_tag

    assert {len(make_tag(i)) for i in range(60)} == {2} and 2 < sr.MIN_VALUE_CHARS


def test_census_generalization_self_check(tmp_path: Path):
    """합성 소스: 상수 이름 접두사·동적 접두사·임의 산술·`%`·`.format`·`+` 사슬은 후보, 주소·경로 조립은 아님."""
    (tmp_path / "m.py").write_text(textwrap.dedent("""
        PREFIX = "@q"
        class K:
            SEP = "-"
        def a(used, prefix, n, c, host):
            x = f"{PREFIX}{used + 1}"
            y = f"{prefix}{n}"
            z = "id_%d" % n
            w = "v{}".format(n * 2)
            v = "k" + K.SEP + str(c)
            u = f"{used:04d}"
            no1 = f"http://{host}/x"
            no2 = f"{host}.json"
            no3 = "/path/" + host
            no4 = f"문장 {n} 개"
            return x, y, z, w, v, u, no1, no2, no3, no4
    """), encoding="utf-8")
    got = {e for _, _, e, _ in id_candidates(tmp_path)}
    assert got == {"f'{PREFIX}{used + 1}'", "f'{prefix}{n}'", "'id_%d' % n", "'v{}'.format(n * 2)",
                   "'k' + K.SEP + str(c)", "f'{used:04d}'"}, got
