"""WS-71(#183) — 따옴표 많은 주소 1MiB 마스킹 속도 개선의 **동등성** 차등 시험.

기준 커밋(BASE_REF — 최적화 직전 dev)의 `security/public_url.py`·`security/secret_registry.py` 를 `git show` 로 읽어
스크래치 모듈 두 개(서로만 참조 — 현재 모듈과 레지스트리를 공유하지 않는다)로 띄우고, 현재 구현과 같은 입력·같은 등록
순서로 다음 출력이 **완전히** 같은지 본다: public_payload·public_text·public_url·secret_values·주소 칸(_public_url_field)·
레지스트리 가림(mask·mask_url_part)·표현 집합(representations·url_representations·_parts)·퍼센트 인코더(_quote).

* 무작위 생성 RANDOM_CASES 건(따옴표·백슬래시·공백·`&`·`=`·`%`·`;`·`#`·유니코드·짝 없는 서로게이트·긴 값·등록한 비밀
  값/URL 조합) + 기존 성능 표본(r1·r2 PERF_CASES, r3 SHAPES, r4 CASES)과 누출 시험 표본(r2 B6·BENIGN, r3 SAMPLES,
  ws51_r4_leak VALUES) 전부.
* 시험 안에서 git 이 없거나 기준 커밋이 없으면 skip(CI 는 fetch-depth 0 이라 돈다).
"""

from __future__ import annotations

import random
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

import security.public_url  # noqa: F401 - 모듈 객체(패키지가 같은 이름의 함수를 재수출한다)
from security import secret_registry as cur_reg
from test_ws51_r1_public_url import PERF_CASES as R1_PERF_CASES
from test_ws51_r2_public_url import B6_SAMPLES, BENIGN as R2_BENIGN, PERF_CASES as R2_PERF_CASES, SHORT
from test_ws51_r3_secret_registry import SAMPLES_Q as R3_SAMPLES, SECRET as R3_SECRET, SHAPES as R3_SHAPES
from test_ws51_r4_perf import CASES as R4_CASES, _text as r4_text, _values as r4_values
from ws51_r4_leak import VALUES as LEAK_VALUES

cur_pu = sys.modules["security.public_url"]
#: 최적화 직전 dev 커밋(WS-70 #182).
BASE_REF = "dcd644c"
ROOT = Path(__file__).resolve().parents[2]
RANDOM_CASES = 5000
QUOTE_CASES = 3000


def _git_show(path: str) -> str:
    try:
        proc = subprocess.run(["git", "show", f"{BASE_REF}:{path}"], cwd=ROOT, capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"git 없음: {exc}")
    if proc.returncode != 0:
        pytest.skip(f"기준 커밋 {BASE_REF} 없음(얕은 clone): {proc.stderr.strip()[:200]}")
    return proc.stdout


def _replace_once(src: str, old: str, new: str) -> str:
    # 바꿀 자리가 정확히 한 곳이어야 한다 — 아니면 기준 모듈이 현재 모듈을 몰래 부를 수 있다(비교 무효).
    assert src.count(old) == 1, (old, src.count(old))
    return src.replace(old, new)


@pytest.fixture(scope="module")
def base():
    """기준 커밋의 (public_url, secret_registry) — 서로만 참조하는 별도 모듈(레지스트리도 별개)."""
    reg_src = _replace_once(_git_show("src/security/secret_registry.py"),
                            "        from security.public_url import secret_values\n",
                            "        secret_values = _WS71_BASE_PUBLIC_URL.secret_values\n")
    pu_src = _replace_once(_git_show("src/security/public_url.py"), "from security import secret_registry\n", "")
    reg = types.ModuleType("ws71_base_secret_registry")
    pu = types.ModuleType("ws71_base_public_url")
    sys.modules[reg.__name__] = reg
    sys.modules[pu.__name__] = pu
    try:
        exec(compile(reg_src, f"<{BASE_REF}:secret_registry.py>", "exec"), reg.__dict__)
        pu.__dict__["secret_registry"] = reg
        exec(compile(pu_src, f"<{BASE_REF}:public_url.py>", "exec"), pu.__dict__)
        reg.__dict__["_WS71_BASE_PUBLIC_URL"] = pu
        assert pu.secret_registry is reg and pu.secret_registry is not cur_reg
        yield pu, reg
    finally:
        sys.modules.pop(reg.__name__, None)
        sys.modules.pop(pu.__name__, None)


@pytest.fixture(autouse=True)
def _fresh(base):
    for reg in (cur_reg, base[1]):
        reg.clear()
    yield
    for reg in (cur_reg, base[1]):
        reg.clear()


def _call(fn, *args):
    try:
        return ("ok", fn(*args))
    except Exception as exc:  # noqa: BLE001 - 예외 종류까지 같아야 한다
        return ("raise", type(exc).__name__)


def _outputs(pu, reg, text: str, extra: str) -> dict:
    """한 구현의 출력 묶음. 순서가 레지스트리 상태를 바꾸므로(public_payload 가 주소 칸을 등록) 두 구현에 같은 순서."""
    out: dict = {
        "public_url": _call(pu.public_url, text),
        "secret_values": _call(pu.secret_values, text),
        "public_text_pre": _call(pu.public_text, text),
        "field_pre": _call(pu._public_url_field, text),
        "payload": _call(pu.public_payload, {"message": text, "url": text, "title": extra,
                                             "tabs": [{"url": extra, "title": text}],
                                             "data": {"items": [text], "frame_url": extra}}),
    }
    out.update({
        "public_text_post": _call(pu.public_text, text),
        "public_text_extra": _call(pu.public_text, extra),
        "field_post": _call(pu._public_url_field, extra),
        "mask": _call(reg.REGISTRY.mask, text + " " + extra),
        "mask_url_part": _call(reg.REGISTRY.mask_url_part, text),
        "registry_len": len(reg.REGISTRY),
        "registry_chars": reg.REGISTRY.chars,
    })
    return out


def _assert_same(base, text: str, extra: str = "", values=(), urls=()) -> None:
    pu, reg = base
    got = {}
    for name, (p, r) in (("base", (pu, reg)), ("cur", (cur_pu, cur_reg))):
        r.clear()
        r.REGISTRY.add_values(list(values))
        for u in urls:
            r.register_url(u)
        got[name] = _outputs(p, r, text, extra)
    diff = [k for k in got["base"] if got["base"][k] != got["cur"][k]]
    assert not diff, (diff, repr(text)[:300], repr(extra)[:120])


# ------------------------------------------------------------------ 무작위 생성

_PIECES = [
    '"', "'", "\\", "\\\\", '\\"', "\\'", '\\\\\\"', " ", "  ", "\t", "\n", "\r\n", "\u00a0", "\u3000",
    "&", "=", "%", "%22", "%2", "%25", "%2B", "%3D", "%26", "+", ";", "#", "?", "/", "://", ":", "@", "<", ">", "`",
    "/a?token=", "?sid=", "&code=", ";jsessionid=", "#access_token=", "&b=1", "?q=hello+world", "&page=2",
    "https://h.invalid/p?", "http://127.0.0.1:1/x?gateToken=", "user:pw@", "//u:p@h.invalid/",
    "next=https%3A%2F%2Fx.invalid%2F%3Ftoken%3D", "next=https://n.invalid/?sid=",
    "é", "한글", "\U0001F600", "\ud800", "\ue000", "\u2028", "ｔｏｋｅｎ=", "ſeſſion=",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "0123456789abcdef0123456789abcdef",
    "SYNTHnotAsecret0123456789abcdXYZ", "[REDACTED]", "%5BREDACTED%5D", "x", "abc", "token", "w w", SHORT,
]
_ALPHABET = "aZ09_-.~ \"'\\&=%;#?/+:@<>é한\U0001F600\n\ue000\ue001"


def _random_value(rnd: random.Random) -> str:
    kind = rnd.random()
    if kind < 0.15:  # 긴 값(블록 색인 ≥ 256)
        return "".join(rnd.choice(_ALPHABET) for _ in range(rnd.randint(256, 1500)))
    if kind < 0.3:
        return rnd.choice(list(LEAK_VALUES.values()))[:2000]
    n = rnd.randint(4, 40)
    return "".join(rnd.choice(_ALPHABET if rnd.random() < 0.5 else "abcdefXYZ0123456789") for _ in range(n))


def _random_text(rnd: random.Random, secrets: list) -> str:
    parts = []
    for _ in range(rnd.randint(1, 20)):
        r = rnd.random()
        if r < 0.15 and secrets:
            s = rnd.choice(secrets)
            parts.append(rnd.choice([s, quote(s, safe=""), quote_plus(s), s.replace('"', '\\"')]))
        elif r < 0.2:
            parts.append(_random_value(rnd))
        else:
            parts.append(rnd.choice(_PIECES))
    return "".join(parts)


def test_random_inputs_match_base(base):
    rnd = random.Random(7183)
    for i in range(RANDOM_CASES):
        secrets = [_random_value(rnd) for _ in range(rnd.randint(0, 3))]
        urls = [f"https://h.invalid/p?token={quote(s, safe='')}" for s in secrets if rnd.random() < 0.5]
        values = [s for s in secrets if rnd.random() < 0.5]
        _assert_same(base, _random_text(rnd, secrets), _random_text(rnd, secrets), values, urls)


def test_quote_matches_base_and_urllib(base):
    """퍼센트 인코더 — 기준 구현·urllib 와 같다(짝 없는 서로게이트는 둘 다 UnicodeError)."""
    rnd = random.Random(183)
    samples = [_random_value(rnd) for _ in range(QUOTE_CASES)]
    samples += [_random_text(rnd, []) for _ in range(QUOTE_CASES)]
    samples += ["", "%", "+", " ", "%+ ", "a" * 5000, ('"x" /?=&' * 700), ("한" * 3000) + "%+ é"]
    samples += [bytes(range(1, 128)).decode("ascii") * 3, "".join(map(chr, range(0x80, 0x800)))]
    samples += ["a" * 200 + "!\"#$%&'()*+,/:;<=>?@[]^`{|} " * 2]  # 바꿀 종류 > 16 이지만 대부분 남는 값(표 조회 분기)
    branches = set()
    for s in samples:
        try:
            data = s.encode("utf-8")
        except UnicodeError:
            continue
        rest = data.translate(None, cur_reg._quote_keep(""))
        if rest:
            branches.add("table_ratio" if len(rest) * 2 > len(data) else
                         "table_kinds" if len(set(rest)) > cur_reg._QUOTE_MAX_KINDS else "replace")
    # 조건 성립: 바이트별 치환·표 조회(비율)·표 조회(종류 초과) 세 분기가 모두 표본에 있다
    assert branches == {"replace", "table_ratio", "table_kinds"}, branches
    for s in samples:
        for safe, plus in cur_reg._ENCODERS:
            for lower in (False, True):
                got = _call(cur_reg._quote, s, safe, plus, lower)
                assert got == _call(base[1]._quote, s, safe, plus, lower), (repr(s)[:200], safe, plus, lower)
                if not lower and got[0] == "ok":
                    assert got[1] == (quote_plus if plus else quote)(s, safe=safe), (repr(s)[:200], safe, plus)


def test_representations_match_base(base):
    rnd = random.Random(1830)
    values = [_random_value(rnd) for _ in range(1500)] + list(LEAK_VALUES.values())
    values += [R3_SECRET, SHORT, '"' * 300, "&<>\"'" * 100, "\ue000\ue001 secret value"]
    for v in values:
        for fn in ("representations", "url_representations", "_parts", "_html", "_json", "_percent"):
            assert _call(getattr(cur_reg, fn), v) == _call(getattr(base[1], fn), v), (fn, repr(v)[:200])


# ------------------------------------------------------------------ 기존 성능·누출 표본

def test_r2_perf_cases_match_base(base):
    for name, make in sorted(R2_PERF_CASES.items()):
        n = 1 << 20 if name == "many_quote_ext" else 1 << 18
        text = make(n)
        _assert_same(base, text, text[: 1 << 12])


def test_r1_perf_cases_match_base(base):
    for name, make in sorted(R1_PERF_CASES.items()):
        _assert_same(base, make(1 << 16), SHORT, urls=[f"http://127.0.0.1:1/seen?token={SHORT}"])


@pytest.mark.parametrize("nvals", [0, 8])
def test_r3_shapes_match_base(base, nvals):
    urls = [f"https://h.invalid/p?gateToken=SYNTH{i:04d}abcdefXYZ&sid=s{i:06d}x" for i in range(nvals)]
    for name, make in sorted(R3_SHAPES.items()):
        _assert_same(base, make(1 << 16), make(1 << 10), urls=urls)


def test_r4_cases_match_base(base):
    for case in R4_CASES:
        values = r4_values(case)
        _assert_same(base, r4_text(case, 1 << 16, values), "/p?" + r4_text(case, 1 << 12, values), values=values)


def test_leak_samples_match_base(base):
    seen = [f"http://127.0.0.1:1/seen?token={SHORT}", f"http://127.0.0.1:1/seen?token={R3_SECRET}"]
    texts = list(B6_SAMPLES) + list(R2_BENIGN) + list(R3_SAMPLES.values())
    for text in texts:
        _assert_same(base, text, text)  # 미등록(URL 문법 계층만)
        _assert_same(base, text, text, urls=seen)  # 등록
    for name, value in sorted(LEAK_VALUES.items()):
        for text in (value, f"/a?token={quote(value, safe='')}", f'/a?token="{value}"', f"title {value} end"):
            _assert_same(base, text, text[:200], values=[value])
            _assert_same(base, text, text[:200], urls=[f"https://h.invalid/p?token={quote(value, safe='')}"])


def test_private_use_sentinel_samples_match_base(base):
    """등록 값에 사설 영역 글자(센티널 후보)가 든 경우 — 센티널이 그 글자를 피해야 짧은 값이 센티널에 붙어 다시 가려지지
    않는다(`used` 집합). 긴 값 A 를 가린 센티널 바로 뒤에 짧은 값 B(`<PUA>ab1`)의 나머지가 오는 문장."""
    long_value, short_value = "head5678abcd", "\ue000ab1"
    for pua in ("\ue000", "\ue001"):
        short = short_value.replace("\ue000", pua)
        for text in (long_value + "ab1", f"x {long_value}ab1 y", long_value + short, f"/p?token={long_value}ab1"):
            _assert_same(base, text, text, values=[long_value, short])
            _assert_same(base, text, text, values=[short, long_value])
