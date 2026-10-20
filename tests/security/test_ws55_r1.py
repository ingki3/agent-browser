"""WS-55 R1 — 검증 FAIL 3건(.hermes/verify/ws55/report.md B1~B3) 회귀 고정. 순수 함수·직렬화 경계(실 브라우저 없음).

* B2: 경로 matrix 매개변수(`/p;sortKey=…`, 세그먼트마다·여러 개)도 쿼리와 같은 `_public_param` 판정과
  `secret_values()` 등록을 거친다. 평범한 값(`;v=2`)·경로 세그먼트 자체는 보존, `;jsessionid=` 는 가림.
* B3: 커서·페이지 토큰 의미의 붙여쓴 키(`pagekey`·`cursorkey`·`nextkey` …)는 모든 철자 변형이 같은 판정(가림).
* 검증 matrix.py(허용 25키 × 철자 9 × 값 6 × query/중복/조각/matrix × 출구 6 + 근접 키)를 위반 0 으로 고정.
* B1 의 레지스트리 등록 헬퍼(`register_secret_values`)·로그인 칸 채우기 등록(login_flow).

값은 모두 합성 값. 공개 사이트 없음(`synthetic.invalid`·`127.0.0.1` 은 문자열일 뿐 요청하지 않는다).
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import io
import logging
import random
import string
from urllib.parse import quote, unquote

import pytest

from contracts import ActionResult, ActionType
from interface.mcp_server import install_public_log_filter
from interface.server_envelope import envelope_json
from security import secret_registry
from security.public_url import has_redaction_mark, public_payload, public_text, public_url, secret_values

pu = importlib.import_module("security.public_url")  # `security.public_url` 이름은 패키지에서 함수로 가려진다

R = "[REDACTED]"
BASE = "https://synthetic.invalid"
HEX40 = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _fresh_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


# ------------------------------------------------------------------ B2 경로 matrix 매개변수


@pytest.mark.parametrize("url, expected", [
    (f"{BASE}/p;sortKey={HEX40}", f"{BASE}/p;sortKey={R}"),
    (f"{BASE}/p;jsessionid=ABC123xyz", f"{BASE}/p;jsessionid={R}"),
    (f"{BASE}/p;JSESSIONID=ABC123xyz?q=1", f"{BASE}/p;JSESSIONID={R}?q=1"),
    (f"{BASE}/a;v=2/b;sortKey={HEX40};lang=ko/c", f"{BASE}/a;v=2/b;sortKey={R};lang=ko/c"),
    (f"{BASE}/a;token=SUMMER;x=1/b;sid=abcd", f"{BASE}/a;token={R};x=1/b;sid={R}"),
    (f"{BASE}/a;{HEX40}/b", f"{BASE}/a;{R}/b"),  # 키 없는 고엔트로피 matrix 값
    ("/rel;sortKey=" + HEX40 + "?x=1", "/rel;sortKey=" + R + "?x=1"),  # 스킴 없는 상대 주소
    (f"{BASE}/p;sortKey=date;v=2;lang=ko", f"{BASE}/p;sortKey=date;v=2;lang=ko"),  # 평범한 값 보존
    (f"{BASE}/p;v=2/q;page=3/", f"{BASE}/p;v=2/q;page=3/"),
    (f"{BASE}/a;b/c;;d", f"{BASE}/a;b/c;;d"),  # 빈·키 없는 평범한 matrix 조각 보존
])
def test_path_matrix_params_use_query_policy(url, expected):
    assert public_url(url) == expected
    assert public_url(expected) == expected  # 멱등


def test_path_matrix_secret_values_registered():
    url = f"{BASE}/a;sortKey={HEX40};v=2/b;jsessionid=ABC123xyz?token=T0ken55x"
    assert secret_values(url) == [HEX40, "ABC123xyz", "T0ken55x"]
    secret_registry.register_url(url)
    assert len(secret_registry.REGISTRY) == 3
    assert HEX40 not in public_text(f"title {HEX40} end")


def test_path_matrix_plain_values_not_registered():
    url = f"{BASE}/p;sortKey=date;v=2;lang=ko"
    secret_registry.register_url(url)
    assert len(secret_registry.REGISTRY) == 0
    assert not has_redaction_mark(public_url(url))


def test_path_matrix_host_and_userinfo_untouched():
    # 호스트에는 matrix 판정을 하지 않는다(경로 시작 뒤만). 사용자정보는 기존대로 뺀다.
    url = f"https://user:pw55secret@h.test:8443/p;sortKey={HEX40}"
    assert public_url(url) == f"https://h.test:8443/p;sortKey={R}"


@pytest.mark.parametrize("text", [
    f"Failed URL http://127.0.0.1/p;sortKey={HEX40}",
    f"redirect to https://h.test/x;jsessionid={HEX40}?a=1 now",
])
def test_path_matrix_in_free_text(text):
    assert HEX40 not in public_text(text)


def test_path_matrix_log_filter():
    stream = io.StringIO()
    logger = logging.getLogger("ws55_r1_matrix")
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    restore = install_public_log_filter()
    try:
        logger.info("Failed URL %s", "http://127.0.0.1/p;sortKey=" + HEX40)
    finally:
        restore()
        logger.removeHandler(handler)
    assert HEX40 not in stream.getvalue(), stream.getvalue()
    assert ";sortKey=" + R in stream.getvalue()


def test_path_matrix_error_and_redirect_payload():
    url = f"http://127.0.0.1:9/p;sortKey={HEX40}"
    payload = {"current_url": url, "data": {"url": url, "redirect_url": url},
               "error_message": f"net::ERR_CONNECTION_REFUSED at {url}"}
    out = public_payload(payload)
    assert HEX40 not in repr(out), out


def test_data_url_semicolons_preserved():
    url = "data:text/html;charset=utf-8;base64,PGI+aGk8L2I+"
    assert public_url(url) == url


# ------------------------------------------------------------------ B3 커서 의미 붙여쓴 키

CURSOR_KEYS = ["pagekey", "cursorkey", "nextkey", "nextpagekey", "pagetoken", "nextpagetoken",
               "continuationkey", "continuationtoken", "startkey", "exclusivestartkey", "lastevaluatedkey",
               "resumekey", "pagingkey"]


def _spellings(k: str):
    return [k, k.upper(), k.title(), "_".join(k), "-".join(k), quote(k, safe=""),
            "".join("%%%02X" % ord(c) for c in k), "".join(chr(ord(c) + 0xFEE0) for c in k)]


@pytest.mark.parametrize("key", CURSOR_KEYS)
def test_cursor_compound_keys_masked_all_spellings(key):
    for spelling in _spellings(key):
        url = f"{BASE}/p?{spelling}=SYNTHcursor55&page=2"
        assert public_url(url) == f"{BASE}/p?{spelling}={R}&page=2", spelling
        assert secret_values(url) == ["SYNTHcursor55"], spelling


@pytest.mark.parametrize("spelling", ["pagekey", "PAGEKEY", "pageKey", "page_key", "PageKey", "page-key", "Pagekey"])
def test_pagekey_spellings_consistent(spelling):
    """검증 B3 재현: 소문자·대문자 붙여쓰기만 공개되던 것."""
    assert public_url(f"{BASE}/p?{spelling}=SYNTHcursor55") == f"{BASE}/p?{spelling}={R}"
    assert public_url(f"{BASE}/p;{spelling}=SYNTHcursor55") == f"{BASE}/p;{spelling}={R}"


@pytest.mark.parametrize("key", ["page", "pagesize", "nextpage", "keyword", "monkey", "sortkey"])
def test_cursor_compounds_do_not_widen(key):
    assert public_url(f"{BASE}/p?{key}=date") == f"{BASE}/p?{key}=date"


# ------------------------------------------------------------------ 검증 matrix.py 이식(위반 0)

_rnd = random.Random(5501)


def _rand(n: int) -> str:
    return "".join(_rnd.choice(string.ascii_letters + string.digits) for _ in range(n))


#: 합성 JWT(서명 없음 — 모양만).
_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiJzeW50aGV0aWMtd3M1NSIsImV4cCI6MjAwMDAwMDAwMH0."
        "c3ludGhldGljc2lnbmF0dXJld3M1NXZhbHVlMDA")
MATRIX_VALUES = {
    "jwt": _JWT, "random32": _rand(32), "random64": _rand(64),
    "hex40": "".join(_rnd.choice("0123456789abcdef") for _ in range(40)),
    "base64url": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
    "registered": "SYNTHknownSecret55",
}


def _key_variants(k: str):
    return [k, k.upper(), k.title(), "_".join(k), "-".join(k),
            "".join("%%%02X" % ord(c) for c in k), quote("%%%02X" % ord(k[0]) + k[1:]),
            "".join(chr(ord(c) + 0xFEE0) for c in k),
            "".join(chr(0x1D41A + ord(c) - ord("a")) for c in k)]


def _sinks(url: str):
    data = {"url": url, "tabs": [{"url": url}], "redirect_url": url,
            "observation": {"url": url, "title": "Opened " + url}}
    for action in (ActionType.NAVIGATE, ActionType.OBSERVE_PAGE, ActionType.EXTRACT, ActionType.TAB_CONTROL):
        raw = ActionResult(action=action, success=True, snapshot_epoch=0, tab_id="tab-1", retry_safe=True,
                           current_url=url, data=data)
        yield action.value, envelope_json(raw)
    yield "error", envelope_json(ActionResult(action=ActionType.NAVIGATE, success=False, snapshot_epoch=0,
                                              tab_id="tab-1", retry_safe=True, error_message="Failed: " + url,
                                              current_url=url))
    yield "log_text", public_text("Failed: " + url)


def _check(url: str, value: str, kind: str, label: str, failures: list, counts: dict) -> None:
    secret_registry.clear()
    if kind == "registered":
        secret_registry.register_url("http://127.0.0.1/?token=" + value)
    for sink, text in _sinks(url):
        counts[sink] = counts.get(sink, 0) + 1
        if value in unquote(text):
            failures.append((label, kind, sink))


@pytest.mark.parametrize("key", sorted(pu._NOT_SECRET_KEYS))
def test_sink_matrix_allowlisted_keys_no_leak(key):
    failures: list = []
    counts: dict = {}
    for spelling in _key_variants(key):
        for kind, value in MATRIX_VALUES.items():
            for form, url in (
                ("query", f"http://127.0.0.1/p?{spelling}={value}"),
                ("duplicate", f"http://127.0.0.1/p?{spelling}=a&{spelling}={value}"),
                ("fragment", f"http://127.0.0.1/p#{spelling}={value}"),
                ("matrix", f"http://127.0.0.1/p;{spelling}={value}"),
                ("matrix2", f"http://127.0.0.1/a;v=2/p;x=1;{spelling}={value}/z"),
            ):
                _check(url, value, kind, f"{form}:{spelling}", failures, counts)
    for near in (key + "token", "x_" + key, key + "[]"):
        for kind, value in MATRIX_VALUES.items():
            _check(f"http://127.0.0.1/p?{near}={value}", value, kind, f"near:{near}", failures, counts)
    assert sum(counts.values()) == 6 * (9 * 6 * 5 + 3 * 6)  # 측정 범위(출구 6 전부)
    assert failures == [], failures[:10]


def test_sink_matrix_allowlist_size():
    assert len(pu._NOT_SECRET_KEYS) == 25


# ------------------------------------------------------------------ B1 등록 헬퍼·로그인 칸


def test_register_secret_values_policy():
    secret_registry.register_secret_values(["SYNTHtypedSecret55", "abc", "login", None, 7])
    assert len(secret_registry.REGISTRY) == 1  # 짧은 값·흔한 낱말·문자열 아님은 기존 규칙대로 제외
    out = public_payload({"url": f"{BASE}/land?sortKey=SYNTHtypedSecret55", "title": "SYNTHtypedSecret55"})
    assert "SYNTHtypedSecret55" not in repr(out), out
    secret_registry.register_secret_values(["0012"])  # 4~7자 비알파벳(PIN) — 경계 일치로 등록
    assert len(secret_registry.REGISTRY) == 2


def test_register_secret_values_never_raises(monkeypatch):
    def boom(self, values):
        raise RuntimeError("x")

    monkeypatch.setattr(secret_registry.SecretRegistry, "add_values", boom)
    secret_registry.register_secret_values(["SYNTHtypedSecret55"])  # 예외 없음


class _FakeLoginPage:
    url = "https://login.synthetic.invalid/form"

    def __init__(self):
        self.filled = {}

    async def evaluate(self, script, *args):
        return {"password": True, "username": True}

    async def fill(self, sel, value):
        self.filled[sel] = value


def test_fill_login_fields_registers_credential():
    from browser.login_flow import fill_login_fields
    from security.credentials import Credential

    cred = Credential(domain="synthetic.invalid", username="synthUser55@x.test", password="SYNTHpw55word")
    page = _FakeLoginPage()
    asyncio.run(fill_login_fields(page, cred))
    assert set(page.filled.values()) == {"synthUser55@x.test", "SYNTHpw55word"}
    title = "welcome synthUser55@x.test SYNTHpw55word"
    out = public_text(title)
    assert "SYNTHpw55word" not in out and "synthUser55@x.test" not in out, out
