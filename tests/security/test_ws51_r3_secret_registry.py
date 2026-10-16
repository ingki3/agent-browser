"""WS-51 R3 — 자유 문장 가리기 설계 전환(D안: 비밀 값 레지스트리 + URL 문법 경계 + 인용 값 소비).

재검증 R2 FAIL(B8 `\\"`·JSON 인용, B9 1024자 상한 꼬리, B10 의미 붕괴)의 뿌리는 '출처를 모르는 문장에서 값이
어디서 끝나는지 추측' 이었다(설계 리뷰 §1). R3 는 추측을 버린다:

* 도구가 본 주소의 비밀 값(public_url 이 가리는 바로 그 값)은 레지스트리에 기록해 어디서든 정확 일치로 가린다.
* 출처 모를 문장은 URL 문법 경계만 — 공백은 주소의 끝(공백 연장 폐지). 빈 값·`\\` 뒤 따옴표는 이스케이프되지
  않은 짝 따옴표나 줄 끝까지 상한 없이 가린다.
* 문서화된 한계(사용자 결정 2026-10-16 A): 도구가 본 적 없는 값이 제목에만 공백 뒤로 이어지면 원문이 남는다,
  4~7자 값은 영숫자에 붙으면 가리지 않는다.

값은 모두 합성 값. 공개 사이트 없음.
"""

from __future__ import annotations

import copy
import html
import json
import os
import pickle
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from security import secret_registry
from security.public_url import public_payload, public_text, public_url

R = "[REDACTED]"
SECRET = "SYNTHr3Value8x"  # 8자 이상 — 부분 문자열 일치
ORIGIN = "http://127.0.0.1:1"


@pytest.fixture(autouse=True)
def _fresh_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


# ------------------------------------------------------------------ R2 검증 표본(8자 이상 값)

def _samples(secret: str) -> dict:
    """재검증 R2 boundary_probe.SAMPLES 와 같은 모양(key_ 표본 제외 — 키 정규형은 R2 테스트가 고정)."""
    s = {
        "control_plain": "/a?token=" + secret,
        "control_quoted": '/a?token="' + secret + '"',
        "unmatched_double": '/a?token="' + secret,
        "unmatched_single": "/a?token='" + secret,
        "mismatched_quote": '/a?token="' + secret + "'",
        "backtick": "/a?token=`" + secret + "`",
        "backtick_space": "/a?token=`prefix " + secret + "`",
        "curly_double": "/a?token=“" + secret + "”",
        "curly_double_space": "/a?token=“prefix " + secret + "”",
        "curly_single": "/a?token=‘prefix " + secret + "’",
        "corner_quote": "/a?token=「prefix " + secret + "」",
        "escaped_double": '/a?token=\\"' + secret + '\\"',
        "escaped_single": "/a?token=\\'" + secret + "\\'",
        "escaped_inner": '/a?token="prefix \\"' + secret + '\\""',
        "tab": "/a?token=prefix\t" + secret,
        "nbsp": "/a?token=prefix\u00a0" + secret,
        "fullwidth_space": "/a?token=prefix\u3000" + secret,
        "crlf": "/a?token=prefix\r\n" + secret,
        "quoted_crlf": '/a?token="prefix\r\n' + secret + '"',
        "after_space_quote": '/a?token=prefix "' + secret + '"',
        "empty_space_quote": '/a?token= "' + secret + '"',
        "multiple_secrets": '/a?token="' + secret + '"&session=prefix ' + secret + ';sid="' + secret + '"',
        "html_attribute": '<a href="/a?token=' + secret + '">next</a>',
        "json_url": json.dumps({"url": "/a?token=" + secret}),
        "json_quoted_url": json.dumps({"url": '/a?token="' + secret + '"'}),
        "json_space_url": json.dumps({"url": "/a?token=prefix " + secret}),
    }
    for offset in (1014, 1022, 1023, 1024, 1025, 1030):
        s[f"cap_space_{offset}"] = "/a?token=x " + ("w " * 600)[:offset] + secret
        s[f"cap_quote_{offset}"] = '/a?token="' + ("w " * 600)[:offset] + secret + '"'
    return s


SAMPLES = _samples(SECRET)
#: 미등록이어도 URL 문법 계층(인용 값 소비)만으로 0 이어야 하는 표본(수용 기준 2).
QUOTED_UNREGISTERED = ["escaped_double", "escaped_single", "escaped_inner", "json_quoted_url",
                       "unmatched_double", "unmatched_single", "control_quoted",
                       "multiple_secrets_quoted_only"] + [f"cap_quote_{o}" for o in (1014, 1022, 1023, 1024, 1025, 1030)]
SAMPLES_Q = dict(SAMPLES, multiple_secrets_quoted_only='/a?token="' + SECRET + '"&sid="' + SECRET + '"')


def _leaks(text: str, secret: str = SECRET) -> int:
    forms = {secret, quote(secret, safe=""), json.dumps(secret)[1:-1], html.escape(secret)}
    return sum(text.count(f) for f in forms)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_registered_value_never_leaks_in_any_sample(name):
    """수용 1(함수 단위): 도구가 `?token=SECRET` 주소를 본 뒤 같은 문장 — 원문·퍼센트·JSON·HTML 변형 0."""
    secret_registry.register_url(f"{ORIGIN}/a?token={SECRET}")
    text = SAMPLES[name]
    out = public_text(text)
    assert _leaks(out) == 0, out[:300]
    wire = json.dumps(public_payload({"error_message": "goto failed: " + text,
                                      "data": {"observation": {"title": text}}}), ensure_ascii=False)
    assert _leaks(wire) == 0
    assert _leaks(json.dumps(public_payload({"m": text}))) == 0  # ASCII JSON(\uXXXX) 직렬화도
    assert public_text(out) == out  # 멱등


@pytest.mark.parametrize("name", QUOTED_UNREGISTERED)
def test_quoted_values_masked_without_registry(name):
    """수용 2: 레지스트리가 비어도 인용 값(`\\"`·JSON·짝 없음·긴 인용)은 URL 문법 계층이 가린다."""
    assert len(secret_registry.REGISTRY) == 0
    text = SAMPLES_Q[name]
    out = public_text(text)
    assert SECRET not in out, out[:300]
    assert public_text(out) == out


@pytest.mark.parametrize("text,expected", [
    ('제목 /a?token="SYNTHq1234" 끝', f'제목 /a?token="{R}" 끝'),
    ("/a?token='SYNTHq1234' 그리고 'x'", f"/a?token='{R}' 그리고 'x'"),
    ('/a?token="SYNTHq1234\n다음 줄', f'/a?token="{R}\n다음 줄'),        # 짝 없으면 줄 끝까지
    ('/a?token="" 빈 값', '/a?token="" 빈 값'),
    ('/a?token=\\"SYNTHq1234\\" 끝', f'/a?token=\\"{R}\\" 끝'),         # 이스케이프된 따옴표 쌍
    ('{"u": "/a?token=\\"SYNTHq1234\\""} 끝', f'{{"u": "/a?token=\\"{R}\\""}} 끝'),  # JSON 문자열 안
    ('{"u": "/a?token=\\"SYNTHq1234"} 끝', f'{{"u": "/a?token=\\"{R}"}} 끝'),  # 이스케이프 열림 + 바깥 닫힘
    ('/a?token="pre \\"SYNTHq1234\\" post" 뒤', f'/a?token="{R}" 뒤'),  # 안쪽 이스케이프는 값 내용
    ('/a?token="x"&sid=SYNTHq1234 뒤', f'/a?token="{R}"&sid={R} 뒤'),   # 인용 뒤 이어지는 쿼리
    ('/a?token="x"#access_token=SYNTHq1234', f'/a?token="{R}"#access_token={R}'),
    ('/a?token="x"&sid="SYNTHq1234" 뒤', f'/a?token="{R}"&sid="{R}" 뒤'),
    # 주소를 감싼 따옴표(값이 비지 않음)는 닫는 따옴표 — 뒤 문장을 먹지 않는다
    ('"/a?token=SYNTHq1234" and "other"', f'"/a?token={R}" and "other"'),
    ('/a?q="hello world" 끝', '/a?q="hello world" 끝'),                  # 비밀 키가 아니면 그대로
])
def test_quote_consumption_rules(text, expected):
    out = public_text(text)
    assert out == expected
    assert public_text(out) == out


def test_quote_consumption_has_no_cap():
    """R2 의 1024자 상한(B9)은 없다 — 아무리 긴 인용 값도 짝 따옴표(없으면 줄 끝)까지."""
    for n in (1014, 1024, 1025, 5000, 100_000):
        text = '/a?token="' + "w " * n + SECRET + '" 뒤 문장'
        out = public_text(text)
        assert out == f'/a?token="{R}" 뒤 문장'
    assert not hasattr(sys.modules["security.public_url"], "MAX_VALUE_TAIL_CHARS")


# ------------------------------------------------------------------ 문서화된 한계(수용 3)

def test_limit_unregistered_title_only_space_tail_remains():
    """도구가 본 적 없는 값이 공백 뒤로 이어지면 원문이 남는다(페이지가 쓴 내용 — 사용자 결정 2026-10-16 A)."""
    out = public_text(f"/a?token=prefix {SECRET}")
    assert out == f"/a?token={R} {SECRET}"
    # 같은 값을 도구가 주소에서 봤다면 가린다
    secret_registry.register_url(f"{ORIGIN}/x?sid={SECRET}")
    assert public_text(f"/a?token=prefix {SECRET}") == f"/a?token={R} {R}"


def test_limit_short_values_need_non_alnum_boundary():
    """4~7자 값은 앞뒤가 영숫자가 아닐 때만 가린다(짧은 값의 과잉 가림 방지). 4자 미만·글자만 4~7자는 미등록."""
    secret_registry.register_url(f"{ORIGIN}/x?code=SYN7X2")
    assert public_text("코드 SYN7X2 확인") == f"코드 {R} 확인"
    assert public_text("wSYN7X2") == "wSYN7X2"          # 영숫자 인접 — 미가림(한계)
    assert public_text("SYN7X2z 다음") == "SYN7X2z 다음"
    secret_registry.register_url(f"{ORIGIN}/x?code=ab1")  # 3자
    secret_registry.register_url(f"{ORIGIN}/x?auth=login&session=true")  # 글자만 4~7자
    assert public_text("ab1 login required true") == "ab1 login required true"


# ------------------------------------------------------------------ 의미 보존(수용 4)

CONTEXT = [
    'token=abc expired, please retry',
    '/a?token=abc expired, please retry',
    'Request /a?token=abc expired, please retry',
    'Request /a?token=abc succeeded, continue checkout',
    'Request /a?token=abc failed: connection refused',
    'Request /a?token=abc failed: certificate expired',
    'Request /a?token=abc failed: domain blocked',
    'Request /a?token=abc failed: login required',
    'Request /a?token=abc completed: payment confirmed',
    'Request /a?token=abc failed: payment declined',
    'Request /a?token=abc HTTP 401 Unauthorized',
    'Request /a?token=abc HTTP 200 OK',
    'Search: /a?token=abc Best camping sites in October',
    'Search: /a?token=abc No results found',
    'Search: /a?token=abc Booking confirmed',
    'Search: /a?token=abc Booking cancelled',
    '이동 /a?token=abc 예약에 성공했습니다',
    '이동 /a?token=abc 예약에 실패했습니다',
]
PAIRS = [(2, 3), (4, 5), (8, 9), (10, 11), (14, 15), (16, 17)]


@pytest.mark.parametrize("value,register", [("abc", False), ("SYNTHmeaning16ch", True), ("SYNTHmeaning16ch", False)])
def test_meaning_pairs_stay_distinct(value, register):
    if register:
        secret_registry.register_url(f"{ORIGIN}/a?token={value}")
    rows = [s.replace("abc", value) for s in CONTEXT]
    for a, b in PAIRS:
        for wrap in (lambda t: public_text(t),
                     lambda t: public_payload({"error_message": t})["error_message"],
                     lambda t: public_payload({"data": {"observation": {"title": t}}})["data"]["observation"]["title"]):
            out_a, out_b = wrap(rows[a]), wrap(rows[b])
            assert out_a != out_b, (rows[a], out_a)
            for src, out in ((rows[a], out_a), (rows[b], out_b)):
                assert value not in out
                assert R in out
                # `[REDACTED]` 뒤 결과 문구가 원문과 같다
                assert out.split(R, 1)[1] == src.split(value, 1)[1]


BENIGN = [
    'a=b', 'x=1, y=2', 'a?b=c:d', 'if (x == y) return z;', 'C# tutorials',
    'Search query=a=b', '가격은 x=100 입니다', 'error E_TIMEOUT: wait_for(x=1)',
    '정말? 그렇다.', 'issue #12 page=2', 'x=1?', 'foo?bar=baz', 'Use a[]=1 for arrays',
    '결과 없음: q=아이오닉', 'HTTP status=404', 'Use --flag=value', 'env PATH=/usr/bin',
    'git branch=dev', 'CSS color=#fff', '식: a?b=c 그리고 x=1', 'JS: let a=b;',
    '이름: alpha_beta=gamma', '검색어: a=b+c', 'URL 없이 x=y 를 찾으세요',
    '계산 a?rate=1.5&count=3', 'See README#usage', 'Python kwargs(x=2)',
    'filter?category=books&page=2', '선택자 [name=search]', '도움말: /docs?lang=ko',
    'Check x?code=123', '질문?token=word', '제목: C#?query=hello',
    'Why? a=b', 'print("a=b")', 'https://host.invalid/path?query=hello+world',
]


def test_benign_preserved_with_registry_loaded():
    """benign 36 중 34 보존 — 레지스트리에 값이 있어도(일반 낱말은 등록되지 않는다)."""
    for i in range(40):
        secret_registry.register_url(f"{ORIGIN}/p?gateToken=SYNTH{i:04d}abcdefXYZ&sid=s{i:06d}&auth=login")
    changed = [s for s in BENIGN if public_text(s) != s]
    assert len(BENIGN) == 36 and len(BENIGN) - len(changed) >= 34
    assert changed == ['Check x?code=123', '질문?token=word'], changed


# ------------------------------------------------------------------ 레지스트리 규칙

def test_registration_shares_public_url_decision():
    """등록 = public_url 이 [REDACTED] 로 바꾸는 값 — 같은 함수(secret_values)."""
    from security.public_url import secret_values

    url = ("https://u1:SYNTHpassw0rd@h.test/p?gateToken=SYNTHgate1234&page=2&date=2026-10-16"
           "&next=" + quote("https://i.test/cb?code=SYNTHinner99&lang=ko", safe="")
           + ";sid=SYNTHsemi777#access_token=SYNTHfrag5555&x=1")
    found = secret_values(url)
    assert set(found) == {"SYNTHgate1234", "SYNTHinner99", "SYNTHsemi777", "SYNTHfrag5555", "SYNTHpassw0rd"}
    masked = public_url(url)
    for value in found:
        assert value not in masked
    assert "page=2" in masked and "date=2026-10-16" in masked
    # 토큰을 사용자 자리에 쓴 꼴(비밀번호 없음)은 사용자 이름을 등록
    assert secret_values("https://ghpSYNTHtoken0001@h.test/") == ["ghpSYNTHtoken0001"]


def test_registered_variants_cover_encodings():
    raw = 'SYN"T&H<é>R3+/=v'
    secret_registry.register_url(f"{ORIGIN}/a?token=" + quote(raw, safe=""))
    forms = [raw, quote(raw, safe=""), json.dumps(raw)[1:-1], json.dumps(raw, ensure_ascii=False)[1:-1],
             html.escape(raw), repr(raw)[1:-1]]
    for form in forms:
        out = public_text(f"제목 {form} 끝")
        assert out == f"제목 {R} 끝", (form, out)
    # 쿼리의 `+` 는 공백(unquote_plus) — 디코드된 꼴로 문장에 나와도 가린다
    secret_registry.register_url(f"{ORIGIN}/a?sid=SYNTH+plus+val9")
    assert public_text("세션 SYNTH plus val9 끝") == f"세션 {R} 끝"


def test_lru_cap_and_thread_safety():
    # R4 §5: 상한 단위는 변형이 아니라 값(MAX_VALUES) + 글자 예산(MAX_CHARS).
    reg = secret_registry.SecretRegistry(cap=secret_registry.MAX_VALUES)
    errors = []

    def worker(k):
        try:
            for i in range(200):
                reg.add_value(f"SYNTHthread{k:02d}x{i:05d}")
                reg.mask(f"x SYNTHthread{k:02d}x{i:05d} y")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(reg) == secret_registry.MAX_VALUES
    assert 0 < reg.chars <= secret_registry.MAX_CHARS
    reg.add_value("SYNTHnewest0001")
    assert reg.mask("SYNTHnewest0001") == R
    assert reg.mask("SYNTHthread00x00000") == "SYNTHthread00x00000"  # 가장 오래된 값은 잊었다


def test_overlapping_and_periodic_values():
    """R4 §5(의도적 변경): 겹침은 §1.4 정의로 — 어느 등록 값의 전체 표현도 남지 않는다. 서로 다른 등록 값이 겹쳐
    남는 일부 조각(`ab`·`SYNTHoverlap`)은 NB(긴 것부터 str.replace — P4)."""
    secret_registry.REGISTRY.add_value("abababab")
    out = public_text("xx ababababab yy")
    assert "abababab" not in out and out.startswith(f"xx {R}") and out.endswith(" yy")
    secret_registry.REGISTRY.add_value("SYNTHoverlapAAAA")
    secret_registry.REGISTRY.add_value("AAAAtailSYNTH999")
    out = public_text("SYNTHoverlapAAAAtailSYNTH999!")
    assert "SYNTHoverlapAAAA" not in out and "AAAAtailSYNTH999" not in out and R in out and out.endswith("!")


def test_registry_never_serializes_values():
    """수용 7: repr·str·format·pickle·copy 어디에도 값이 나가지 않는다."""
    secret_registry.register_url(f"{ORIGIN}/a?token={SECRET}")
    reg = secret_registry.REGISTRY
    for text in (repr(reg), str(reg), f"{reg}", "%r" % (reg,), repr(secret_registry)):
        assert SECRET not in text
    assert not hasattr(reg, "__dict__")
    for fn in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            fn(reg)
    with pytest.raises(TypeError):
        json.dumps(reg)


def test_registry_module_has_no_output_paths():
    """정적 audit: 레지스트리 모듈은 파일·로그·출력 경로가 없다."""
    src = Path(secret_registry.__file__).read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    for banned in ("open(", "logging", "print(", ".write(", "os.write", "sys.stderr", "sys.stdout", "Path("):
        assert banned not in code, banned


def test_registration_failure_never_raises(monkeypatch):
    pu = sys.modules["security.public_url"]  # `security.public_url` 속성은 같은 이름의 함수(재수출)

    def boom(url):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(pu, "secret_values", boom)
    secret_registry.register_url(f"{ORIGIN}/a?token={SECRET}")  # 예외 없음
    secret_registry.register_urls([None, 3, f"{ORIGIN}/a?token={SECRET}"])
    assert len(secret_registry.REGISTRY) == 0


def test_payload_registers_url_fields_before_masking_text():
    """등록 지점 (a): 같은 응답의 주소 칸 원문 — 제목이 주소 칸보다 먼저 나와도."""
    payload = {"data": {"observation": {"title": f"/a?token=prefix {SECRET}"}},
               "current_url": f"{ORIGIN}/a?token={SECRET}"}
    out = public_payload(payload)
    assert SECRET not in json.dumps(out, ensure_ascii=False)
    assert out["data"]["observation"]["title"] == f"/a?token={R} {R}"


def test_payload_content_paths_do_not_register():
    """페이지 내용(elements 의 링크)은 도구가 본 주소가 아니다 — 등록하지 않는다."""
    payload = {"data": {"observation": {"elements": [{"url": f"/next?token={SECRET}"}], "title": f"x {SECRET} y"}}}
    out = public_payload(payload)
    assert out["data"]["observation"]["title"] == f"x {SECRET} y"
    assert len(secret_registry.REGISTRY) == 0


def test_cache_tracks_registry_version():
    """긴 문장 캐시는 레지스트리가 바뀌면 다시 계산한다(등록 뒤 옛 결과가 남지 않게)."""
    long = "w " * 10_000 + SECRET
    assert public_text(long).endswith(SECRET)
    secret_registry.register_url(f"{ORIGIN}/a?token={SECRET}")
    assert public_text(long).endswith(R)


# ------------------------------------------------------------------ 리다이렉트·이동 이벤트(등록 지점 c)

class _Frame:
    def __init__(self, url, parent=None):
        self.url, self.parent_frame = url, parent


class _Req:
    def __init__(self, url, nav=True, frame=None, redirected_from=None):
        self.url, self._nav, self.frame, self.redirected_from = url, nav, frame or _Frame(url), redirected_from

    def is_navigation_request(self):
        return self._nav


class _Emitter:
    def __init__(self, pages=()):
        self.handlers, self.pages, self.url = {}, list(pages), ""

    def on(self, name, fn):
        self.handlers.setdefault(name, []).append(fn)

    def emit(self, name, arg):
        for fn in self.handlers.get(name, []):
            fn(arg)


def test_context_watch_registers_redirect_chain_and_main_frame_moves():
    ctx = _Emitter()
    secret_registry.watch_browser_context(ctx)
    hop1 = _Req(f"{ORIGIN}/r1?token=SYNTHhop1aaaa")
    hop2 = _Req(f"{ORIGIN}/r2?sid=SYNTHhop2bbbb", redirected_from=hop1)
    ctx.emit("request", _Req(f"{ORIGIN}/clean", redirected_from=hop2))
    sub = _Req(f"{ORIGIN}/sub?token=SYNTHsubframe1", frame=_Frame("", parent=_Frame("")))
    ctx.emit("request", sub)
    ctx.emit("request", _Req(f"{ORIGIN}/img?token=SYNTHnotnav01", nav=False))
    page = _Emitter()
    ctx.emit("page", page)
    page.emit("framenavigated", _Frame(f"{ORIGIN}/spa#access_token=SYNTHspahash1"))
    page.emit("framenavigated", _Frame(f"{ORIGIN}/if?token=SYNTHiframe01", parent=_Frame("")))
    out = public_text("SYNTHhop1aaaa SYNTHhop2bbbb SYNTHspahash1 SYNTHsubframe1 SYNTHnotnav01 SYNTHiframe01")
    assert out == f"{R} {R} {R} SYNTHsubframe1 SYNTHnotnav01 SYNTHiframe01"


# ------------------------------------------------------------------ 성능(수용 5)

SHAPES = {
    "words": lambda n: ("lorem ipsum " * (n // 12 + 1))[:n],
    "url_repeat": lambda n: ("https://h.invalid/p?gateToken=SYNTH0001abcdefXYZ&x=1 " * (n // 52 + 1))[:n],
    "escaped_repeat": lambda n: ('/a?token=\\"SYNTH2Xlong\\" ' * (n // 26 + 1))[:n],
    "quoted_repeat": lambda n: ('/a?token="w w SYNTH2Xlong" ' * (n // 28 + 1))[:n],
    "quote_open_repeat": lambda n: ('/a?token="x ' * (n // 12 + 1))[:n],
    "unclosed_quote_long": lambda n: '/a?token="' + "w " * (n // 2),
    "prefix_partial": lambda n: ("SYNTH0001abcdefXY " * (n // 18 + 1))[:n],
}
SIZES = [4 << 10, 64 << 10, 256 << 10, 512 << 10, 1 << 20, 2 << 20]


def _perf_child(shape: str, nvals: int) -> None:
    from security.public_url import _public_text  # 캐시를 거치지 않는 본체

    for i in range(nvals):
        secret_registry.register_url(f"https://h.invalid/p?gateToken=SYNTH{i:04d}abcdefXYZ&sid=s{i:06d}x")
    rows = {"variants": len(secret_registry.REGISTRY), "times": {}}
    for n in SIZES:
        text = SHAPES[shape](n)
        best = float("inf")
        for _ in range(3):
            t = time.perf_counter()
            _public_text(text)
            best = min(best, time.perf_counter() - t)
        rows["times"][n] = best
    print(json.dumps(rows))


def _run_perf(shape: str, nvals: int) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run([sys.executable, __file__, shape, str(nvals)], capture_output=True, text=True,
                              timeout=120, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{shape}: 120초 안에 끝나지 않음(이차시간 회귀)")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("nvals", [0, 128])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_performance_linear_and_under_a_second(shape, nvals):
    rows = _run_perf(shape, nvals)
    if nvals:
        assert rows["variants"] == secret_registry.MAX_VALUES  # 레지스트리를 값 상한까지 채운 조건 성립(R4 §5)
    times = {int(k): v for k, v in rows["times"].items()}
    assert times[1 << 20] < 1.0, (shape, times)
    # 2배 입력 → 시간 ≤ 2.5배 (잡음이 큰 아주 짧은 측정은 바닥 5ms 로 본다)
    for small, big in zip(SIZES[2:], SIZES[3:]):
        assert times[big] <= 2.5 * max(times[small], 0.005), (shape, small, big, times)


if __name__ == "__main__":
    _perf_child(sys.argv[1], int(sys.argv[2]))
