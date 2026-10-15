"""WS-51 R2 — 재검증 BLOCKING B6(자유 문장 값 경계)·B7(Unicode 키) 와 로그 잔존 NB-5.

* B6: `/a?token="X"`·`/a?token='X'`·`/a?token=prefix X` 는 주소 칸에서는 이미 가려지지만 자유 문장(오류 문구·
  stderr·로그·관찰 제목)에서는 공백·따옴표에서 덩어리가 끊겨 원문이 남았다. **같은 표본을 주소 칸과 자유 문장
  양쪽에** 고정한다(주소 칸 테스트가 자유 문장 결함을 가리지 않게).
* B7: 키 판정 정규형 = 퍼센트 디코딩 → NFKC → casefold (전각 `ｔｏｋｅｎ`, long-s `ſeſſion`). 터키어 dotless ı·İ 는
  casefold 동치가 아니라 범위 밖 — 현재 동작(가리지 않음)을 고정한다.
* NB-5: 로그 — 이미 채워진 exc_text·extra 문자열 속성·겹친 설치 refcount.

값은 일부러 짧은 합성 값(값 규칙이 키 규칙의 구멍을 덮지 않게). 공개 사이트 없음.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from security.public_url import public_payload, public_text, public_url

R = "[REDACTED]"
SHORT = "SYNTHr2X"

# ------------------------------------------------------------------ B6 자유 문장 값 경계

#: 재검증 B6 표본 + 변형(절대 주소·탭·오류 문구·repr). 주소 칸·자유 문장·stderr·로그 모두 원문 0 이어야 한다.
B6_SAMPLES = [
    f'/a?token="{SHORT}"',
    f"/a?token='{SHORT}'",
    f"/a?token=prefix {SHORT}",
    f"/a?token=prefix\t{SHORT}",
    f'http://127.0.0.1:1/a?token="{SHORT}"',
    f"http://127.0.0.1:1/a?b=1&gateToken=prefix {SHORT}",
    f'goto failed: http://127.0.0.1:1/a?token="{SHORT}"',
    f"#access_token='{SHORT}'",
    f"/a?token=a b {SHORT} c",
    f'/a?token="pre {SHORT} post"',
    f'/a?token="{SHORT}',                     # 짝 없는 따옴표 → 줄 끝까지
    f"/a?token= {SHORT}",                     # 빈 값 뒤 공백
]


def _log_once(text: str) -> str:
    from interface.mcp_server import install_public_log_filter

    lg = logging.getLogger("ws51.r2.b6")
    prior = (lg.level, lg.propagate)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    lg.addHandler(handler)
    restore = install_public_log_filter()
    try:
        lg.warning("이동 %s", text)
        lg.warning("repr %r", text)
    finally:
        restore()
        lg.removeHandler(handler)
        lg.setLevel(prior[0])
        lg.propagate = prior[1]
    return out.getvalue()


@pytest.mark.parametrize("text", B6_SAMPLES)
def test_b6_url_field_and_free_text_both_mask(text):
    """같은 표본을 주소 칸(url·*_url)과 자유 문장(error_message·안내) 양쪽에 넣는다."""
    out = public_payload({"current_url": text, "error_message": text,
                          "data": {"sample_url": text, "hint": f"다시 시도: {text}", "title": text}})
    wire = json.dumps(out, ensure_ascii=False)
    assert SHORT not in wire, wire
    assert SHORT not in public_text(text)
    assert public_payload(out) == out  # 멱등


@pytest.mark.parametrize("text", B6_SAMPLES)
def test_b6_stderr_real_fd2_masks(text, capfd):
    from interface.server_common import _stderr_line

    _stderr_line(text)
    err = capfd.readouterr().err
    assert err.strip(), "stderr 줄이 사라지면 안 된다"
    assert SHORT not in err, err


@pytest.mark.parametrize("text", B6_SAMPLES)
def test_b6_logging_masks(text):
    out = _log_once(text)
    assert out.count("\n") == 2, out  # 로그는 남는다
    assert SHORT not in out, out


@pytest.mark.parametrize("text,expected", [
    # 따옴표로 시작하는 값 → 짝 따옴표까지, 그 뒤 문장은 남는다
    (f'제목 /a?token="{SHORT}" 끝', f'제목 /a?token="{R}" 끝'),
    (f"/a?token='{SHORT}' 그리고 'x'", f"/a?token='{R}' 그리고 'x'"),
    (f'/a?token="{SHORT}\n다음 줄', f'/a?token="{R}\n다음 줄'),          # 짝이 없으면 줄 끝까지
    ('/a?token="" 빈 값', '/a?token="" 빈 값'),
    # 공백 뒤 같은 단어 묶음 → 다음 구분 기호(& ; # 줄바꿈 닫는 괄호 따옴표)까지
    (f"/a?token=prefix {SHORT}&page=2", f"/a?token={R} {R}&page=2"),
    (f"/a?token=prefix {SHORT};page=2", f"/a?token={R} {R};page=2"),
    (f"/a?token=prefix {SHORT}#top", f"/a?token={R} {R}#top"),
    (f"(see /a?token=prefix {SHORT}) 다음", f"(see /a?token={R} {R}) 다음"),
    (f"/a?token=prefix {SHORT}\n다음 줄", f"/a?token={R} {R}\n다음 줄"),
    (f'"/a?token=prefix {SHORT}" 끝', f'"/a?token={R} {R}" 끝'),
    # 주소를 감싼 따옴표(값이 비지 않음)는 닫는 따옴표 — 뒤 문장을 먹지 않는다
    (f'"/a?token={SHORT}" and "other"', f'"/a?token={R}" and "other"'),
    (f"'/a?token={SHORT}' 끝", f"'/a?token={R}' 끝"),
    # 비밀 키가 아니면 경계를 넓히지 않는다
    ('/a?q="hello world" 끝', '/a?q="hello world" 끝'),
    ("/a?q=hello world", "/a?q=hello world"),
    ("/a?page=2 다음 쪽", "/a?page=2 다음 쪽"),
])
def test_b6_boundary_rules(text, expected):
    out = public_text(text)
    assert out == expected
    assert public_text(out) == out


@pytest.mark.parametrize("text", [
    f"/a?token=x y&sid={SHORT}",            # 연장이 & 에서 멈춘 뒤 이어지는 쿼리도 같은 규칙
    f"/a?token=x y;code={SHORT}",
    f"/a?token=x y&b=1&token=z {SHORT}",     # 이어지는 쿼리의 끝 값도 다시 연장
    f'/a?token="x"&sid={SHORT}',
    f"/a?token=x y#access_token={SHORT}",
])
def test_b6_query_resumes_after_extension(text):
    out = public_text(text)
    assert SHORT not in out, out
    assert public_text(out) == out


def test_b6_extension_is_capped():
    from security.public_url import MAX_VALUE_TAIL_CHARS

    tail = "w" * (MAX_VALUE_TAIL_CHARS * 3) + " " + "z" * (MAX_VALUE_TAIL_CHARS * 2)
    out = public_text(f"/a?token=prefix {tail}")
    assert out.startswith(f"/a?token={R} {R}")
    assert out.endswith("z" * (MAX_VALUE_TAIL_CHARS * 2))  # 상한 뒤 다음 단어부터는 문장 그대로
    assert len(out) < len(tail)


def test_b6_cap_never_cuts_inside_a_following_url():
    """상한이 다음 주소 덩어리 한가운데에 걸려도 그 덩어리 꼬리(`…ken=X`)가 원문으로 남지 않는다."""
    from security.public_url import MAX_VALUE_TAIL_CHARS

    for shift in range(0, 40):
        text = "/a?token=x " + "w" * (MAX_VALUE_TAIL_CHARS - 20 + shift) + f" http://h.test/?token={SHORT} 끝"
        assert SHORT not in public_text(text), shift
    many = f"http://h.test/?token={SHORT} " * 400
    assert SHORT not in public_text(many)


#: 재검증 benign 36표본(matrix.json/benign). R1 에서 2개(code·token 키)만 바뀌었다 — 34개 보존 유지.
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


def test_benign_sample_preservation_unchanged():
    assert len(BENIGN) == 36
    changed = [s for s in BENIGN if public_text(s) != s]
    assert changed == ['Check x?code=123', '질문?token=word'], changed


# ------------------------------------------------------------------ B7 Unicode 키 정규형

B7_KEYS = ["ｔｏｋｅｎ", quote("ｔｏｋｅｎ"), "ſeſſion", "ＧａｔｅＴｏｋｅｎ", "ＳＩＤ", "ſid", "ｃｏｄｅ",
           "access_ｔｏｋｅｎ", quote("ſeſſion"), "ＳＥＳＳＩＯＮ"]


@pytest.mark.parametrize("key", B7_KEYS)
def test_b7_unicode_equivalent_keys_mask(key):
    url = f"http://127.0.0.1:1/a?{key}={SHORT}&date=2026-10-16"
    out = public_url(url)
    assert SHORT not in out and "date=2026-10-16" in out, out
    assert out.startswith(f"http://127.0.0.1:1/a?{key}=")  # 키 이름은 원래 표기 그대로
    assert public_url(out) == out
    text = f"오류: {url} 끝"
    assert SHORT not in public_text(text)
    assert SHORT not in json.dumps(public_payload({"current_url": url, "error_message": text}), ensure_ascii=False)


@pytest.mark.parametrize("key", ["keyword", "monkey", "codec", "zipcode", "author", "ｋｅｙｗｏｒｄ", "ｍｏｎｋｅｙ",
                                 "ＡＵＴＨＯＲ", "ｚｉｐｃｏｄｅ", "straße", "page"])
def test_b7_boundary_words_still_preserved(key):
    url = f"https://h.test/p?{key}={SHORT}"
    assert public_url(url) == url


@pytest.mark.parametrize("key", ["sıd", "SİD"])
def test_b7_turkish_dotless_i_is_out_of_scope(key):
    """터키어 ı·İ 는 ASCII sid 의 casefold 동치가 아니다 — 범위 밖(보고서 한계). 현재 동작을 고정한다."""
    assert SHORT in public_url(f"https://h.test/p?{key}={SHORT}")


# ------------------------------------------------------------------ NB-5 로그

@pytest.fixture
def log_sink():
    lg = logging.getLogger("ws51.r2.log")
    prior = (lg.level, lg.propagate)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    lg.addHandler(handler)
    try:
        yield lg, handler, out
    finally:
        lg.removeHandler(handler)
        lg.setLevel(prior[0])
        lg.propagate = prior[1]


def test_log_prefilled_exc_text_is_masked(log_sink):
    from interface.mcp_server import install_public_log_filter

    lg, _, out = log_sink
    url = f"https://h.test/camp?gateToken={SHORT}"
    original = logging.getLogRecordFactory()

    def pre(*args, **kwargs):  # 앞선 팩토리가 exc_text 를 미리 채운다
        rec = original(*args, **kwargs)
        rec.exc_text = f"Traceback: 이동 실패 {url}"
        return rec

    logging.setLogRecordFactory(pre)
    restore = install_public_log_filter()
    try:
        lg.warning("x")
    finally:
        restore()
        logging.setLogRecordFactory(original)
    text = out.getvalue()
    assert "Traceback: 이동 실패" in text and "gateToken=[REDACTED]" in text, text
    assert SHORT not in text, text


def test_log_extra_string_attributes_are_masked(log_sink):
    from interface.mcp_server import install_public_log_filter

    lg, handler, out = log_sink
    handler.setFormatter(logging.Formatter("%(message)s %(url)s %(n)s"))
    restore = install_public_log_filter()
    try:
        lg.warning("이동", extra={"url": f"/a?token={SHORT}", "n": 3})
        lg.warning("이동", extra={"url": f'/a?token="{SHORT}"', "n": 4})
    finally:
        restore()
    text = out.getvalue()
    assert text.count("이동 /a?token=") == 2 and " 3\n" in text and " 4\n" in text, text
    assert SHORT not in text, text


def test_log_filter_refcount_survives_inner_restore(log_sink):
    from interface.mcp_server import install_public_log_filter

    lg, handler, out = log_sink
    handler.setFormatter(logging.Formatter("%(message)s %(url)s"))
    original_factory = logging.getLogRecordFactory()
    original_make = logging.Logger.makeRecord
    url = f"/a?token={SHORT}"
    first = install_public_log_filter()
    second = install_public_log_filter()
    try:
        first()
        first()  # 같은 restore 를 두 번 불러도 다른 설치의 몫을 빼지 않는다
        lg.warning("남은 설치 %s", url, extra={"url": url})
        assert SHORT not in out.getvalue(), out.getvalue()
    finally:
        second()
    assert logging.getLogRecordFactory() is original_factory
    assert logging.Logger.makeRecord is original_make
    lg.warning("해제 뒤 %s", url, extra={"url": url})
    assert SHORT in out.getvalue()  # 마지막 설치가 끝나면 원래대로(조건 성립 확인)


# ------------------------------------------------------------------ 선형 시간(B6 연장 경로)

PERF_CASES = {
    "space_tail": lambda n: "/a?token=prefix " + "w" * n,
    "quote_tail": lambda n: '/a?token="' + "w" * n,
    "many_space_ext": lambda n: f"/a?token=x {SHORT} " * (n // 22),
    "many_quote_ext": lambda n: f'/a?token="{SHORT}" ' * (n // 22),
    "resume_runs": lambda n: f"/a?token=x y&b=1&c=2&sid={SHORT} " * (n // 36),
}


def _perf_child(case: str, n: int) -> None:
    text = PERF_CASES[case](n)
    t = time.perf_counter()
    out = public_payload({"message": text, "url": text})
    seconds = time.perf_counter() - t
    assert SHORT not in out["message"], case
    print(json.dumps({"case": case, "n": n, "seconds": seconds}))


def _run_perf(case: str, n: int, timeout: float = 30.0) -> float:
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run([sys.executable, __file__, case, str(n)], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case} n={n}: {timeout}초 안에 끝나지 않음(이차시간 회귀)")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])["seconds"]


@pytest.mark.parametrize("case", sorted(PERF_CASES))
def test_b6_one_mib_under_a_second(case):
    seconds = _run_perf(case, 1 << 20)
    assert seconds < 1.0, f"{case}: 1MiB {seconds:.3f}s"


if __name__ == "__main__":
    _perf_child(sys.argv[1], int(sys.argv[2]))
