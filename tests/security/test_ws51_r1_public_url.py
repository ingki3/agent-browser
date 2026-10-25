"""WS-51 R1 — 독립 검증 BLOCKING(B1~B5)·NB-1·NB-3 회귀 (단위, 합성 값만).

비밀 값은 일부러 **짧은** 합성 값(`SYNTH7`)이다 — JWT·긴 무작위 값이면 값 규칙이 키 규칙의 구멍을 덮어
결함이 안 보인다(검증 B1 의 교훈).
"""

from __future__ import annotations

import copy
import io
import itertools
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from security.public_url import public_payload, public_text, public_url

R = "[REDACTED]"
SHORT = "SYNTH7"


def _case_variants(base: str, limit: int = 512):
    """base 의 대소문자 조합(글자 수가 많으면 결정적 표본 limit 개 + 전부 대/소문자)."""
    letters = [i for i, c in enumerate(base) if c.isalpha()]
    if len(letters) <= 9:
        combos = itertools.product([0, 1], repeat=len(letters))
    else:
        rng = random.Random(51)
        combos = [tuple(rng.randint(0, 1) for _ in letters) for _ in range(limit)]
        combos += [tuple([0] * len(letters)), tuple([1] * len(letters))]
    out = set()
    for bits in combos:
        chars = list(base)
        for i, bit in zip(letters, bits):
            chars[i] = chars[i].upper() if bit else chars[i].lower()
        out.add("".join(chars))
    return sorted(out)


# ------------------------------------------------------------------ B1 키 이름 대소문자 무시


#: 검증의 1040 조합 표본(14개 키) + 흔한 비밀 키 이름.
CASE_KEYS = ["token", "gateToken", "auth", "sid", "sig", "otp", "pwd", "jwt", "code", "sess", "saml",
             "session", "password", "key", "access_token", "X-Amz-Signature", "api_key", "apikey",
             "client_secret", "sessionid", "phpsessid", "csrftoken", "SAMLResponse", "passcode"]


@pytest.mark.parametrize("base", CASE_KEYS)
def test_every_case_variant_of_secret_key_masks_short_value(base):
    leaked = [k for k in _case_variants(base)
              if SHORT in public_url(f"https://h.test/p?{k}={SHORT}&page=2")]
    assert not leaked, f"{base}: {len(leaked)}개 조합에서 원문 잔존 — {leaked[:8]}"


def test_case_sample_size_covers_verification_sample():
    first14 = CASE_KEYS[:14]
    assert sum(len(_case_variants(k)) for k in first14) == 1040  # 검증 표본과 같은 1040 조합


@pytest.mark.parametrize("key", ["%54%6f%4b%65%4e", "%74%6f%6b%65%6e", "gate%54oken", "%74oken", "%2574oken",
                                 "GaTe%54oKeN", "%67ate%74oken", "sId", "aUtH", "cOdE", "JwT"])
def test_percent_encoded_and_mixed_case_keys_mask(key):
    out = public_url(f"https://h.test/p?{key}={SHORT}&date=2026-10-16")
    assert SHORT not in out and "date=2026-10-16" in out, out
    assert out.startswith(f"https://h.test/p?{key}=")  # 키 이름(인코딩 그대로)은 남긴다
    assert public_url(out) == out


@pytest.mark.parametrize("key", ["keyword", "KEYWORD", "Keyword", "monkey", "MONKEY", "codec", "CODEC",
                                 "zipcode", "ZIPCODE", "author", "AUTHOR", "Author", "keyboard", "hotkey"])
def test_boundary_words_stay_preserved(key):
    url = f"https://h.test/p?{key}={SHORT}"
    assert public_url(url) == url


# ------------------------------------------------------------------ B2 `;` 구분자


@pytest.mark.parametrize("url,expected", [
    (f"https://h.test/p?page=2;gateToken={SHORT}", f"https://h.test/p?page=2;gateToken={R}"),
    (f"https://h.test/p?token={SHORT};page=2", f"https://h.test/p?token={R};page=2"),
    (f"https://h.test/p?a=1;b=2&c=3;token={SHORT}&d=4", f"https://h.test/p?a=1;b=2&c=3;token={R}&d=4"),
    (f"https://h.test/p#page=2;token={SHORT}", f"https://h.test/p#page=2;token={R}"),
    (f"https://h.test/app#/route?tab=2;code={SHORT}", f"https://h.test/app#/route?tab=2;code={R}"),
    # 인코딩된 구분자(%3B·%26)는 값 내용 — 쪼개지 않는다
    (f"https://h.test/s?q=abc%3Btoken%3D{SHORT}", f"https://h.test/s?q=abc%3Btoken%3D{SHORT}"),
    (f"https://h.test/s?q=abc%26token%3D{SHORT}", f"https://h.test/s?q=abc%26token%3D{SHORT}"),
    (f"https://h.test/s?token=abc%3B{SHORT}", f"https://h.test/s?token={R}"),
    ("https://h.test/s?filter=red;blue;green&page=2", "https://h.test/s?filter=red;blue;green&page=2"),
])
def test_semicolon_is_a_parameter_separator(url, expected):
    out = public_url(url)
    assert out == expected
    assert public_url(out) == out


# ------------------------------------------------------------------ B3 주소 필드는 스킴과 무관하게 public_url


@pytest.mark.parametrize("url,expected", [
    (f"about:blank#access_token={SHORT}", f"about:blank#access_token={R}"),
    (f"about:blank?token={SHORT}", f"about:blank?token={R}"),
    (f"/guard?gateToken={SHORT}&page=2", f"/guard?gateToken={R}&page=2"),
    (f"?token={SHORT}", f"?token={R}"),
    (f"#access_token={SHORT}", f"#access_token={R}"),
    (f"data:text/html,hello#token={SHORT}", f"data:text/html,hello#token={R}"),
    (f"blob:http://h.test/0f1e#sid={SHORT}", f"blob:http://h.test/0f1e#sid={R}"),
    (f"chrome-error://chromewebdata/?code={SHORT}", f"chrome-error://chromewebdata/?code={R}"),
])
def test_public_url_masks_non_http_and_relative(url, expected):
    assert public_url(url) == expected
    assert public_url(expected) == expected


def test_url_fields_go_through_public_url_whatever_the_scheme():
    src = {
        "current_url": f"about:blank#access_token={SHORT}",
        "data": {
            "url": f"about:blank#access_token={SHORT}",
            "tab_id": "t2",
            "frame_url": f"about:srcdoc?token={SHORT}",
            "current_frame_url": f"/frame?sid={SHORT}",
            "tabs": [{"tab_id": "t1", "url": f"about:blank#id_token={SHORT}"}],
            "robots": {"rule": f"Disallow: /guard?gateToken={SHORT}",
                       "robots_url": f"http://127.0.0.1:1/robots.txt?sig={SHORT}"},
            "request_urls": [f"/a?code={SHORT}", "/b?page=2"],
        },
    }
    out = public_payload(src)
    assert SHORT not in json.dumps(out), out
    assert out["data"]["request_urls"][1] == "/b?page=2"
    assert out["data"]["robots"]["rule"] == f"Disallow: /guard?gateToken={R}"


@pytest.mark.parametrize("value", [
    f"about:blank#x=1 y&access_token={SHORT}",          # 조각 안 공백(자유 문장 규칙이면 'y&…' 덩어리를 놓친다)
    f"data:text/html,a b?x=1 c&token={SHORT}",
    f"/p?q=a b\"c&sid={SHORT}",
])
def test_url_field_is_one_address_even_with_spaces_or_quotes(value):
    """주소 칸은 자유 문장처럼 공백·따옴표로 쪼개지 않고 주소 전체를 public_url 로 본다(B3)."""
    out = public_payload({"data": {"url": value, "frame_url": value, "tabs": [{"url": value}]}})
    assert SHORT not in json.dumps(out), out


@pytest.mark.parametrize("text,expected", [
    (f"Disallow: /guard?gateToken={SHORT}", f"Disallow: /guard?gateToken={R}"),
    # R3: 공백은 주소의 끝 — R2 의 공백 연장(뒤 낱말까지 가림)을 되돌렸다(B10 의미 보존, 비밀 값 레지스트리로 대체)
    (f"상대 주소 ?token={SHORT} 끝", f"상대 주소 ?token={R} 끝"),
    (f"조각 #access_token={SHORT}&x=1", f"조각 #access_token={R}&x=1"),
    (f"/login?next=https://h.test/p?token={SHORT}", None),  # 안쪽 절대 주소도
    # 자유 문장의 물음표·해시·등호는 그대로
    ("정말? 그렇다. C# 과 a?b=c 그리고 x=1?", "정말? 그렇다. C# 과 a?b=c 그리고 x=1?"),
    ("issue #12 page=2", "issue #12 page=2"),
])
def test_public_text_masks_relative_url_pieces(text, expected):
    out = public_text(text)
    assert SHORT not in out, out
    if expected is not None:
        assert out == expected
    assert public_text(out) == out


# ------------------------------------------------------------------ NB-1 내용 제외는 경로 기준


def test_content_exclusion_is_path_based():
    url = f"https://h.test/camp?gateToken={SHORT}"
    src = {
        "current_url": url,
        "data": {
            "items": [{"href": url}],
            "observation": {"url": url, "elements": [{"name": url}], "axtree_summary": url},
            "diagnostic": {"items": [{"url": url}], "elements": [url], "axtree_summary": url},
            "window": {"items": [url]},
        },
        "items": [url],
    }
    before = copy.deepcopy(src)
    out = public_payload(src)
    assert src == before
    d = out["data"]
    assert SHORT in json.dumps(d["items"]) and SHORT in json.dumps(d["observation"]["elements"])
    assert SHORT in d["observation"]["axtree_summary"]
    assert SHORT not in d["observation"]["url"]
    assert SHORT not in json.dumps(d["diagnostic"]) and SHORT not in json.dumps(d["window"])
    assert SHORT not in json.dumps(out["items"]) and SHORT not in out["current_url"]


def test_envelope_content_subtree_not_shared_with_action_result():
    """public_payload 는 내용 subtree 를 복사하지 않는다(1MiB 관찰 deepcopy ≈ 3ms × 크기 상한 반복). 안전 근거:
    envelope_dict 의 입력은 새로 만든 model_dump 라, 봉투를 고쳐도 원래 ActionResult(서버 내부)는 그대로다."""
    from contracts import ActionResult, ActionType
    from interface.server_envelope import envelope_dict

    url = f"https://h.test/camp?gateToken={SHORT}"
    r = ActionResult(action=ActionType.EXTRACT, success=True, snapshot_epoch=1, current_url=url, tab_id="t",
                     retry_safe=True, data={"items": [{"href": url}]})
    env = envelope_dict(r)
    env["data"]["items"][0]["href"] = "changed"
    env["data"]["items"].append("x")
    assert r.data["items"] == [{"href": url}]


def test_caps_end_in_safe_marks():
    from security.public_url import MAX_URL_CHARS, URL_MASK

    assert public_url("https://h.test/?q=" + "a" * MAX_URL_CHARS) == URL_MASK
    assert public_text("x https://h.test/?q=" + "a" * MAX_URL_CHARS + " y") == f"x {URL_MASK} y"


def test_envelope_noncontent_items_masked():
    from contracts import ActionResult, ActionType
    from interface.server_envelope import envelope_dict

    url = f"https://h.test/camp?gateToken={SHORT}"
    r = ActionResult(action=ActionType.NAVIGATE, success=True, snapshot_epoch=1, current_url=url,
                     tab_id="t", retry_safe=True, data={"diagnostic": {"items": [{"url": url}]}, "items": [{"href": url}]})
    env = envelope_dict(r)
    assert SHORT not in json.dumps(env["data"]["diagnostic"])
    assert SHORT in json.dumps(env["data"]["items"])  # extract 결과 자리(data.items)는 원문


# ------------------------------------------------------------------ NB-3 깊은 입력은 예외 없이 안전 표지


def test_deep_nested_next_url_returns_safely():
    u = f"https://h.test/camp?gateToken={SHORT}"
    for _ in range(400):
        u = "https://h.test/?next=" + u
    out = public_url(u)
    assert SHORT not in out
    assert public_url(out) == out
    enc = f"https://h.test/camp?gateToken={SHORT}"
    from urllib.parse import quote

    for _ in range(30):
        enc = "https://h.test/?next=" + quote(enc, safe="")
    assert SHORT not in public_url(enc)


def test_deep_payload_returns_safely():
    deep: dict = {"url": f"https://h.test/?token={SHORT}"}
    for _ in range(2000):
        deep = {"d": deep}
    out = public_payload(deep)
    assert SHORT not in json.dumps(out)
    deep_list: list = [f"https://h.test/?token={SHORT}"]
    for _ in range(2000):
        deep_list = [deep_list]
    out = public_payload(deep_list)
    assert SHORT not in json.dumps(out)


# ------------------------------------------------------------------ B4 로그 형식 오류·깊은 입력


def _capture_logs(fn):
    """기본 StreamHandler(stderr 진단 포함)를 단 로거로 fn(logger) 를 부르고 (handler 출력, stderr) 를 돌려준다."""
    from interface.mcp_server import install_public_log_filter

    lg = logging.getLogger("ws51.r1.log")
    prior = (lg.level, lg.propagate)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    lg.addHandler(handler)
    err = io.StringIO()
    old_err = sys.stderr
    restore = install_public_log_filter()
    try:
        sys.stderr = err
        fn(lg)
    finally:
        sys.stderr = old_err
        restore()
        lg.removeHandler(handler)
        lg.setLevel(prior[0])
        lg.propagate = prior[1]
    return out.getvalue(), err.getvalue()


def test_logging_format_error_never_prints_raw_args():
    url = f"https://h.test/camp?gateToken={SHORT}&date=2026-10-16"
    out, err = _capture_logs(lambda lg: (lg.warning("bad %d", url), lg.error("two %s %s", url),
                                         lg.info("mapping %(u)s", {"x": url})))
    assert SHORT not in out and SHORT not in err, (out, err)
    # 조용히 버리지 않는다 — 형식 실패 사실과 가린 주소가 남는다
    assert out.count("로그 형식 오류") == 3, out
    assert "gateToken=[REDACTED]" in out and "date=2026-10-16" in out


def test_logging_deep_url_is_still_emitted():
    u = f"https://h.test/camp?gateToken={SHORT}"
    for _ in range(400):
        u = "https://h.test/?next=" + u
    out, err = _capture_logs(lambda lg: lg.warning("deep %s", u))
    assert "deep https://h.test/" in out
    assert SHORT not in out and SHORT not in err
    assert "Traceback" not in err


def test_logging_normal_and_exception_paths_still_masked():
    url = f"https://h.test/camp?gateToken={SHORT}"

    def emit(lg):
        lg.warning("이동 %s", url)
        try:
            raise RuntimeError(url)
        except RuntimeError:
            lg.exception("실패")
        lg.warning("stack", stack_info=True)

    out, err = _capture_logs(emit)
    assert SHORT not in out and SHORT not in err
    assert "gateToken=[REDACTED]" in out and "RuntimeError" in out


# ------------------------------------------------------------------ B5 선형 시간(1MiB < 1초)

#: 각 표본을 따로 자식 프로세스에서 잰다 — 이차시간 회귀는 정규식이 C 안에서 돌아 신호로 끊을 수 없어
#: 자식 프로세스 제한시간으로 잡는다.
PERF_CASES = {
    "title_prefix": lambda n: "Z" * n + f" http://127.0.0.1:1/lab?gateToken={SHORT}",
    "one_chunk_scheme_backtrack": lambda n: "Z" * n + f"-:/x://h.test/?token={SHORT}",
    "relative_long": lambda n: "/p?" + "a" * n + f"&token={SHORT}",
    "jwt_like_run": lambda n: "eyJ-" * (n // 4) + f" ?token={SHORT}",
    "dots": lambda n: "eyJabcdef." * (n // 10) + f" ?token={SHORT}",
    "question_marks": lambda n: "?" * n + f" ?token={SHORT}",
    "many_short_urls": lambda n: (f"http://h.test/?token={SHORT} " * (n // 32)),
}
PERF_BUDGET_S = 1.0
#: WS-66(#149) 측정 방식 — 판정은 '새 자식 프로세스 PERF_RUNS 번 측정의 최솟값'. 기준(PERF_BUDGET_S·비율 64)은 그대로다
#: (규칙 6 비해당): 최솟값은 부하가 가장 적은 순간의 실제 처리 시간이라 러너의 순간 부하만 걸러내고, 이차시간 회귀는 매번
#: 넘어 그대로 잡힌다. 긴 문자열은 lru_cache 에 남아 한 프로세스에서 되풀이하면 캐시를 재므로 매번 새 프로세스에서 재고,
#: 자식이 캐시 적중 0·계산 1회 이상을 보고하며, 반복 측정이 첫 측정의 1/PERF_SAME_WORK 미만이면 아래(R1) 재측정을 거친다.
PERF_RUNS = 3
PERF_SAME_WORK = 20
#: R1(B1) — '같은 일'의 증명은 LRU 통계·시간비가 아니라 환경 격리다: 자식마다 새 빈 임시 디렉터리를 만들어 홈·임시·
#: XDG 캐시/데이터/상태·바이트코드 경로와 작업 디렉터리를 모두 그리로 돌리고 측정 뒤 지운다 — 앞 자식이 남긴 파일 캐시를
#: 다음 자식이 읽을 길이 없다. 저장소·고정 경로 캐시는 격리로 못 막으므로 보조 단정: 첫 측정만 기준을 넘고 최솟값은 기준
#: 아래이며 첫/최솟값이 PERF_FIRST_ONLY 배 이상이면 '첫 실행만 느림 = 캐시성 회귀 의심' — 캐시 키가 다른 같은 크기 입력의
#: 첫 측정을 새 격리 자식에서 한 번 더 하고, 그것도 기준을 넘으면 실패(순간 부하는 되풀이되지 않는다; 실측 load 63/12코어
#: 스파이크에서 [3.01, 0.59, 0.61]초가 나온 적이 있어 비율만으로는 실패시키지 않는다). 실패 문구에 표본 전부·최댓값을 남긴다.
#: 반복이 첫 측정의 1/PERF_SAME_WORK 미만인 경우도 같은 재측정을 거친다(R1 — 스파이크 표본 [2.10, 0.10, 0.10]초 실측).
PERF_FIRST_ONLY = 3
PERF_ALT_GROW = 256  #: 의심 시 재측정할 다른 입력 — 길이를 1/이만큼 늘려 캐시 키만 바꾼다(1MiB 에서 +4KiB; 줄이면 캐시 하한 아래로 갈 수 있다)
PERF_ISOLATED_ENV = ("HOME", "TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                     "PYTHONPYCACHEPREFIX")
#: R2(B1-R1) — 제품 소스 사본: 저장소 고정 경로(`__file__`·저장소 루트 기준) 캐시는 위 격리로 못 비운다 — 다른 입력도
#: 같은 공유 초기화·길이 버킷 표식을 써서 재측정까지 warm 이 된다(검증 실측 [2.48, 0.27, 0.26]초·재측정 0.26초 통과).
#: 그래서 자식마다 새 임시 디렉터리에 `src/` 를 통째로 복사해 그 사본을 PYTHONPATH 맨 앞에 두고, 자식이 제품 모듈을 실제로
#: 사본에서 import 했는지(모듈 `__file__` 이 사본 아래·원본 src 에서 읽은 모듈 0개) 보고받아 아니면 실패시킨다. 첫 측정·반복·
#: 재측정 모두 각자 새 사본이다. 사본 생성은 자식 실행 전이라 측정(자식 안 public_payload 한 번)에 들어가지 않는다.
#: 한계(NB): 절대 시스템 경로(`/tmp/고정` 등)·프로세스 밖 서비스(데몬·DB·네트워크)에 저장하는 캐시는 이 방식으로 격리할 수
#: 없다 — 그런 경로는 위 '첫 실행만 느림' 재측정이 잡을 수 있는 범위(입력별 캐시)까지만 잡는다.
PERF_CODE_COPY_IGNORE = ("__pycache__", "*.pyc")


def _perf_child(case: str, n: int) -> None:
    from security.public_url import _public_text_long, _public_url_field_long

    text = PERF_CASES[case](n)
    # 첫 호출만 잰다 — 긴 문자열은 결과를 기억하므로(public_text 캐시) 두 번째 호출은 이 측정의 대상이 아니다.
    t = time.perf_counter()
    out = public_payload({"message": text, "url": text})
    best = time.perf_counter() - t
    assert SHORT not in out["message"] and SHORT not in out["url"], case
    # 조건 성립: 격리가 실제로 걸렸다 — 홈·임시·캐시 경로가 모두 이 자식의 빈 작업 디렉터리다
    here = os.path.realpath(os.getcwd())
    isolated = all(os.path.realpath(os.environ.get(k, "")) == here for k in PERF_ISOLATED_ENV)
    # 조건 성립(R2): 제품 모듈을 원본 저장소 src 가 아니라 부모가 만든 사본에서 읽었다 — 부모가 사본 경로와 맞춰 본다
    measured = sys.modules["security.public_url"]  # 패키지가 같은 이름의 함수를 재수출하므로 모듈은 sys.modules 에서
    origin = Path(__file__).resolve().parents[2] / "src"
    loaded = [os.path.realpath(f) for m in list(sys.modules.values()) if isinstance(f := getattr(m, "__file__", None), str)]
    from_origin = [f for f in loaded if Path(f).is_relative_to(origin)]
    caches = [_public_text_long.cache_info(), _public_url_field_long.cache_info()]
    print(json.dumps({"case": case, "n": n, "seconds": best,
                      "hits": sum(c.hits for c in caches), "misses": [c.misses for c in caches],
                      "isolated": isolated, "module": os.path.realpath(measured.__file__),
                      "from_origin": from_origin}))


def _best_of(case: str, n: int, runs: int = PERF_RUNS) -> float:
    """새 자식 프로세스 `runs` 번 측정의 최솟값(WS-66 — 위 설명)."""
    samples = [_run_perf(case, n) for _ in range(runs)]
    record = {"samples": samples, "min": min(samples), "max": max(samples)}
    first, rest = samples[0], min(samples[1:])
    over_budget = first >= PERF_BUDGET_S > min(samples) and first >= PERF_FIRST_ONLY * min(samples)
    not_same = rest * PERF_SAME_WORK < first  # 반복이 첫 측정의 1/PERF_SAME_WORK 미만
    if over_budget or not_same:
        # 첫 실행만 느림 — 캐시성 회귀인지 순간 부하인지 가른다: 캐시 키가 다른 같은 크기 입력의 '첫' 측정을 새 자식에서
        # 한 번 더. 캐시라면 새 입력마다 첫 실행이 다시 느리고, 순간 부하라면 되풀이되지 않는다.
        record["recheck"] = recheck = _run_perf(case, n + n // PERF_ALT_GROW)
        assert not (over_budget and recheck >= PERF_BUDGET_S) and not (not_same and recheck > rest * PERF_SAME_WORK), (
            f"{case}: 첫 실행만 느림(첫≥기준·첫/최솟값≥{PERF_FIRST_ONLY}배 또는 반복<첫의 1/{PERF_SAME_WORK})이 "
            "다른 입력의 첫 실행에서도 되풀이 — 캐시성 회귀", record)
    return min(samples)


def _run_perf(case: str, n: int, timeout: float = 30.0) -> float:
    """격리 자식 한 번의 측정(초). 자식마다 빈 홈·임시 디렉터리와 제품 소스 사본(R2)을 새로 만든다 — 사본 생성은 측정 밖.

    한계(NB): 절대 시스템 경로·프로세스 밖 서비스에 저장하는 캐시는 사본으로도 격리되지 않는다(PERF_CODE_COPY_IGNORE 위 설명).
    """
    env = dict(os.environ)
    src = Path(__file__).resolve().parents[2] / "src"
    # 작업 디렉터리를 옮기므로 상대 경로 항목은 절대 경로로
    rest = [os.path.abspath(p) for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    root = os.path.realpath(tempfile.mkdtemp(prefix="ws66-perf-"))  # 자식마다 새 임시 디렉터리(R1·R2 — 위 설명)
    box = os.path.join(root, "box")  # 빈 작업 디렉터리 = 홈·임시·캐시
    code = os.path.join(root, "code", "src")  # 제품 소스 사본(R2) — 저장소 고정 경로 캐시도 사본마다 새로
    try:
        os.mkdir(box)
        shutil.copytree(src, code, ignore=shutil.ignore_patterns(*PERF_CODE_COPY_IGNORE))
        env["PYTHONPATH"] = os.pathsep.join([code, *rest])
        env.update(dict.fromkeys(PERF_ISOLATED_ENV, box))
        try:
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), case, str(n)], capture_output=True,
                                  text=True, timeout=timeout, env=env, cwd=box)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{case} n={n}: {timeout}초 안에 끝나지 않음(이차시간 회귀)")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    row = json.loads(proc.stdout.strip().splitlines()[-1])
    # 조건 성립: 잰 호출이 캐시를 거치지 않고 주소 칸·자유 문장을 실제로 계산했고, 격리된 환경에서 돌았다
    assert row["hits"] == 0 and min(row["misses"]) >= 1, (case, row)
    assert row["isolated"], (case, "자식 환경 격리 실패", row)
    # 조건 성립(R2): 잰 제품 모듈이 이 자식의 사본에서 왔고, 원본 저장소 src 에서 읽은 모듈이 없다
    assert Path(row["module"]).is_relative_to(code) and not row["from_origin"], (case, "제품 소스 사본 미사용", code, row)
    return row["seconds"]


@pytest.mark.parametrize("case", sorted(PERF_CASES))
def test_one_mib_input_is_processed_in_under_a_second(case):
    seconds = _best_of(case, 1 << 20)
    assert seconds < PERF_BUDGET_S, f"{case}: 1MiB {seconds:.3f}s (자식 프로세스 {PERF_RUNS}회 최솟값)"


def test_growth_is_roughly_linear():
    small = max(_best_of("title_prefix", 1 << 14), 1e-4)  # 크기마다 최솟값을 쓴 뒤 비율
    big = _best_of("title_prefix", 1 << 18)
    # 16배 길이 → 이차시간이면 256배. 선형(측정 잡음 포함)이면 그 훨씬 아래.
    assert big / small < 64, (small, big)


if __name__ == "__main__":
    _perf_child(sys.argv[1], int(sys.argv[2]))
