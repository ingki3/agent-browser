"""WS-93 R1 (#270 검증 R1) — 짧은 정상 Bing 검색 결과(SERP)를 과제 화면으로 오탐하지 않는다: 순수 판정 표.

검증 R1: 두 문장을 검색어로 넣은 짧은 정상 SERP(검색 폼·탭·#b_results/.b_algo·인용문)가 captcha/bing + handoff 로
바뀌었다(기준 null). 두 문장이 **검색어·검색 결과에서 온 것**이면 두 쌍 규칙에서 뺀다:
1. Bing 결과 구조(.b_algo)가 하나라도 있으면 NONE (프로브 serp=True). 판독 실패(serp=None)는 텍스트 규칙만.
2. 두 문장 중 하나라도 URL 검색어(q)·검색 입력칸 값·제목에 있으면, 그 출처를 뺀 보이는 본문에서 다시 판정.
3. 짝이 맞는 따옴표(" “” ‘’ 「」 ') 안 인용 구간은 두 쌍 규칙에서 뺀다(실제 과제 본문에는 따옴표가 없다).
실제 과제 화면(검색어 site:…, 결과 항목 없음, 따옴표 없음)은 계속 captcha. 실제 bing.com 에는 접속하지 않는다.
"""

from __future__ import annotations

import pytest

from browser import challenge as ch
from browser.challenge import ChallengeKind, classify

KO1, KO2 = "마지막 한 단계", "계속하려면 아래 과제 해결"
EN1, EN2 = "One last step", "Please solve the challenge below to continue"
KO_PAIR, EN_PAIR = f"{KO1} {KO2}", f"{EN1} {EN2}"
REAL_KO = f"콘텐츠로 건너뛰기\n접근성 피드백\nRewards\n{KO1}\n{KO2}"
REAL_EN = f"Skip to content\nAccessibility Feedback\nRewards\n{EN1}\n{EN2}"
REAL_URL = "https://www.bing.com/search?q=site%3Aexample.com+%EB%89%B4%EC%8A%A4&form=QBLH"
REASON = "Bing 과제 화면(계속하려면 과제 해결 요구)"


def _kind(title, text, url, **kw):
    return classify(title, text[:4000], len(text), "", 200, url=url, **kw)


# ── 검증 extra_matrix 15종(경로만 바꿔 옮김 — 대상 모듈 한 개, 기대값 그대로) ─────────────────────
_ko = "마지막 한 단계 계속하려면 아래 과제 해결"
_en = "One last step Please solve the challenge below to continue"
EXTRA_MATRIX = [
    ("case", _en.upper(), "https://bing.com", True),
    ("NBSP", _en.replace(" ", "\u00a0"), "https://cn.bing.com", True),
    ("newline", _en.replace(" ", "\n"), "https://BING.COM.", True),
    ("other-short-quote", _en, "https://faq.example", False),
    ("userinfo", _en, "https://***@evil.test", False),
    ("prefix", _en, "https://evil-bing.com", False),
    ("suffix", _en, "https://bing.com.evil.test", False),
    ("query-domain", _en, "https://evil.test/?site=bing.com", False),
    ("ko-single", "마지막 한 단계", "https://bing.com", False),
    ("login", "One last step Sign in to Bing", "https://bing.com/login", False),
    ("rewards", "One last step Earn rewards", "https://rewards.bing.com", False),
    ("800", _en + "x" * (800 - len(_en)), "https://bing.com", True),
    ("801", _en + "x" * (801 - len(_en)), "https://bing.com", False),
    ("serp-short", f'All Images News Search results for "{_en}" Troubleshooting Bing: this guide explains the message.',
     "https://bing.com/search?q=x", False),
    ("serp-long", f'All Images News Search results for "{_en}" ' + ("ordinary result " * 60),
     "https://bing.com/search?q=x", False),
]


@pytest.mark.parametrize("name,text,url,expected", EXTRA_MATRIX, ids=[c[0] for c in EXTRA_MATRIX])
def test_extra_matrix(name, text, url, expected):
    got = classify("Bing", text, len(text), "", 200, url=url)
    assert got.detected is expected, (name, len(text), got)
    if expected:
        assert (got.kind, got.vendor, got.reason) == (ChallengeKind.CAPTCHA, "bing", REASON)


def test_verifier_r1_assert_short_serp():
    """검증 R1 assert 시험 그대로(test_expected_short_serp.py)."""
    text = f'All Images News Search results for "{_en}" Troubleshooting Bing: this guide explains the message.'
    got = classify("Bing", text, len(text), "", 200, url="https://bing.com/search?q=x")
    assert got.kind is ChallengeKind.NONE, (len(text), got)


# ── 결과 구조(serp) ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [REAL_KO, REAL_EN], ids=["ko", "en"])
def test_result_structure_excludes_pair_rule(text):
    assert _kind("Bing", text, REAL_URL, serp=True).kind is ChallengeKind.NONE


@pytest.mark.parametrize("serp", [False, None], ids=["no-results", "unreadable"])
@pytest.mark.parametrize("text", [REAL_KO, REAL_EN], ids=["ko", "en"])
def test_real_challenge_without_results_stays_captcha(text, serp):
    got = _kind("Bing", text, REAL_URL, serp=serp)
    assert (got.kind, got.vendor, got.reason) == (ChallengeKind.CAPTCHA, "bing", REASON)


def test_result_structure_does_not_touch_other_rules():
    """결과 구조 제외는 두 쌍 규칙에만 — 위젯·기존 문구·차단 규칙은 그대로."""
    assert _kind("Bing", "x", REAL_URL, serp=True).kind is ChallengeKind.NONE
    assert classify("Bing", "x", 1, "recaptcha", 200, url=REAL_URL, serp=True).vendor == "recaptcha"
    assert _kind("Just a moment...", "x", REAL_URL, serp=True).vendor == "cloudflare"
    assert _kind("", "Sorry, you have been blocked", REAL_URL, serp=True).kind is ChallengeKind.BLOCKED
    assert classify("", "x", 10, "", 403, url=REAL_URL, serp=True).kind is ChallengeKind.BLOCKED


# ── 검색어 출처(URL q·검색 입력칸·제목) ─────────────────────────────────────────────────────────────
ZERO_KO = f"콘텐츠로 건너뛰기\n접근성 피드백\nRewards\n전체 이미지 동영상\n{KO_PAIR}\n에 대한 결과가 없습니다.\n개인 정보 보호"
ZERO_EN = f"Skip to content\nAccessibility Feedback\nRewards\nAll Images Videos\nThere are no results for {EN_PAIR}\nPrivacy"


@pytest.mark.parametrize("pair,body", [(KO_PAIR, ZERO_KO), (EN_PAIR, ZERO_EN)], ids=["ko", "en"])
@pytest.mark.parametrize("source", ["input", "url-q", "both"])
def test_zero_result_serp_query_echo_is_none(pair, body, source):
    from urllib.parse import quote_plus

    url = "https://www.bing.com/search" + (f"?q={quote_plus(pair)}" if source in ("url-q", "both") else "")
    query = pair if source in ("input", "both") else ""
    assert _kind(f"{pair} - 검색", body, url, serp=False, query=query).kind is ChallengeKind.NONE


@pytest.mark.parametrize("pair", [KO_PAIR, EN_PAIR], ids=["ko", "en"])
def test_title_only_reflection_is_none(pair):
    """제목(검색어 반영)에만 두 문장 — 본문엔 없다."""
    body = "콘텐츠로 건너뛰기\n접근성 피드백\nRewards\n전체 이미지 동영상\n결과가 없습니다."
    assert _kind(f"{pair} - 검색", body, "https://www.bing.com/search", serp=False).kind is ChallengeKind.NONE


def test_query_source_is_removed_then_rechecked():
    """검색어가 한 줄만 담아도 그 출처 문자열을 뺀 본문에서 다시 본다 — 검색어 문자열은 모든 위치에서 빠진다."""
    body = f"{EN1}\n{EN1}\n{EN2}"  # 검색어 반영 1줄 + 같은 문장 1줄 + 둘째 줄
    got = _kind("Bing", body, "https://www.bing.com/search", serp=False, query=EN1)
    assert got.kind is ChallengeKind.NONE
    got = _kind("Bing", REAL_EN, "https://www.bing.com/search?q=one+last", serp=False, query="one last")
    assert got.kind is ChallengeKind.CAPTCHA  # 출처가 문장을 담지 않으면 제외하지 않는다


def test_real_challenge_site_query_stays_captcha():
    got = _kind("Bing", REAL_KO, REAL_URL, serp=False, query="site:example.com 뉴스")
    assert (got.kind, got.vendor) == (ChallengeKind.CAPTCHA, "bing")


# ── 따옴표 인용 ───────────────────────────────────────────────────────────────────────────────
QUOTES = [('"', '"'), ("“", "”"), ("‘", "’"), ("「", "」"), ("'", "'")]


@pytest.mark.parametrize("o,c", QUOTES, ids=["ascii-double", "curly-double", "curly-single", "corner", "ascii-single"])
@pytest.mark.parametrize("pair", [KO_PAIR, EN_PAIR], ids=["ko", "en"])
def test_quoted_pair_is_none(pair, o, c):
    text = f"전체 이미지 뉴스\n{o}{pair}{c} 검색 결과\n도움말 문서"
    assert _kind("Bing", text, "https://www.bing.com/search?q=x").kind is ChallengeKind.NONE


@pytest.mark.parametrize("o,c", QUOTES, ids=["ascii-double", "curly-double", "curly-single", "corner", "ascii-single"])
def test_one_line_quoted_rest_unquoted_is_none(o, c):
    text = f"Skip to content\n{o}{EN1}{c}\n{EN2}"
    assert _kind("Bing", text, "https://www.bing.com/search?q=x").kind is ChallengeKind.NONE


@pytest.mark.parametrize("o", ['"', "“", "‘", "「", "'"])
def test_unpaired_quote_is_not_removed(o):
    text = f"Skip to content\n{o}{EN1}\n{EN2}"
    got = _kind("Bing", text, "https://www.bing.com/search?q=x")
    assert (got.kind, got.vendor) == (ChallengeKind.CAPTCHA, "bing")


def test_mismatched_quote_kinds_are_not_removed():
    text = f"Skip to content\n“{EN1}\n{EN2}'"
    assert _kind("Bing", text, "https://www.bing.com/search?q=x").kind is ChallengeKind.CAPTCHA


@pytest.mark.parametrize("text", [REAL_KO, REAL_EN], ids=["ko", "en"])
def test_real_challenge_has_no_quotes_and_stays_captcha(text):
    assert not any(q in text for q in "\"“”‘’「」'")
    assert _kind("Bing", text, REAL_URL).kind is ChallengeKind.CAPTCHA


# ── 판독 실패 주입: detect_challenge / detect_challenge_strict ────────────────────────────────────
class _Page:
    def __init__(self, probe):
        self.probe, self.url = probe, REAL_URL

    async def evaluate(self, _js):
        return self.probe


def _probe(text, **extra):
    return {"title": "Bing", "text": text, "len": len(text), "widget": "", "url": REAL_URL, **extra}


BAD_FIELDS = [{}, {"serp": "yes", "query": 5}, {"serp": 1, "query": None}, {"serp": None, "query": ["x"]}]


@pytest.mark.parametrize("extra", BAD_FIELDS, ids=["missing", "str-int", "int-none", "none-list"])
async def test_unreadable_structure_falls_back_to_text_rule(extra):
    page = _Page(_probe(REAL_KO, **extra))
    got = await ch.detect_challenge(page)
    assert (got.kind, got.vendor) == (ChallengeKind.CAPTCHA, "bing")

    async def iso(_expr):
        return page.probe

    got = await ch.detect_challenge_strict(page, isolated=iso)
    assert got is not None and (got.kind, got.vendor) == (ChallengeKind.CAPTCHA, "bing")


async def test_probe_structure_and_query_reach_classify():
    assert (await ch.detect_challenge(_Page(_probe(REAL_KO, serp=True, query="")))).kind is ChallengeKind.NONE
    page = _Page(_probe(f"전체\n{KO_PAIR}\n결과 없음", serp=False, query=KO_PAIR))
    assert (await ch.detect_challenge(page)).kind is ChallengeKind.NONE

    async def iso(_expr):
        return page.probe

    assert (await ch.detect_challenge_strict(page, isolated=iso)).kind is ChallengeKind.NONE


def test_probe_reads_structure_in_same_evaluate():
    """결과 구조·검색칸 판독은 같은 프로브 한 번(새 왕복 없음) — 엄격판도 같은 식."""
    js = ch._PROBE_JS
    assert ".b_algo" in js and "serp" in js and "query" in js
    assert ch.STRICT_PROBE_EXPRESSION == f"({js.strip()})()"
