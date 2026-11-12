"""WS-93 A (#270) — Bing 과제 화면(캡차) 감지: 순수 판정 표.

실사용(봇 기록): bing.com 검색이 '마지막 한 단계 / 계속하려면 아래 과제 해결'(영어판 'One last step / Please solve the
challenge below to continue') 화면을 냈는데 data.challenge 가 null 이라 에이전트가 빈 결과로 읽었다. 기존 표에는 이
문구가 없다.

규칙(계획 v2 A): 두 줄이 **함께** 있고, 본문 ≤ SHORT_BODY_CHARS, 페이지 주소가 bing.com(또는 하위 도메인)일 때만 captcha
· vendor bing · 고정 이유 한 문장. 이 규칙만 공백을 정규화한다(NBSP·줄바꿈). 한 줄만·긴 글·다른 도메인 인용·미러는 NONE.
실제 bing.com 에는 접속하지 않는다 — 합성 문자열만 쓴다.
"""

from __future__ import annotations

import pytest

from browser.challenge import SHORT_BODY_CHARS, ChallengeKind, classify

KO1, KO2 = "마지막 한 단계", "계속하려면 아래 과제 해결"
EN1, EN2 = "One last step", "Please solve the challenge below to continue"
REAL_KO = f"콘텐츠로 건너뛰기\n접근성 피드백\nRewards\n{KO1}\n{KO2}"
REAL_EN = f"Skip to content\nAccessibility Feedback\nRewards\n{EN1}\n{EN2}"
FILL = "이 기사는 검색 엔진 이용 중 겪는 문제를 다룹니다. " * 10  # 약 300자
BING = "https://www.bing.com/search?q=x"
REASON = "Bing 과제 화면(계속하려면 과제 해결 요구)"

CAPTCHA_CASES = [
    ("bing KO real", "Bing", REAL_KO, BING),
    ("bing EN real", "Bing", REAL_EN, BING),
    ("bing KO nbsp", "Bing", REAL_KO.replace(" ", "\u00a0"), BING),
    ("bing EN nbsp+newline split", "Bing", REAL_EN.replace("last step", "last\u00a0\nstep"), BING),
    ("cn.bing EN", "", REAL_EN, "https://cn.bing.com/search?q=x"),
    ("bing.com apex KO", "", REAL_KO, "https://bing.com/search?q=x"),
]

NONE_CASES = [
    ("bing KO line1 only", "Bing", f"Rewards\n{KO1}\n결제를 마치세요", "https://www.bing.com/x"),
    ("bing KO line2 only", "Bing", f"Rewards\n{KO2}", "https://www.bing.com/x"),
    ("bing EN line1 only (signup)", "", f"{EN1}\nConfirm your email", "https://www.bing.com/x"),
    ("evil bing.com.evil", "", REAL_EN, "https://bing.com.evil.test/x"),
    ("evil-bing.com", "", REAL_EN, "https://evil-bing.com/x"),
    ("other domain real copy (mirror)", "", REAL_KO, "https://mirror.example/x"),
    ("localhost copy", "", REAL_EN, "http://127.0.0.1:8080/search"),
    ("FAQ short 400c quoting both", "FAQ", FILL + f"'{KO1}' '{KO2}' 라고 나오면?", "https://faq.example/q"),
    ("news 1500c quoting both", "뉴스", FILL * 4 + f"화면에 '{EN1}', '{EN2}' 문구", "https://news.example/a"),
    ("bing 1500c article quoting both", "Bing", FILL * 4 + f"'{KO1}' '{KO2}'", BING),
    ("bing 2900c quoting both", "Bing", FILL * 9 + f"{EN1} {EN2}", BING),
    ("long 5000c quoting both", "긴 글", FILL * 16 + f"{EN1} {EN2}", BING),
    ("checkout 'One last step' + 'continue'", "Checkout",
     "One last step\nPlease review your order below to continue", "https://shop.example/"),
    ("bing checkout-like 'One last step'", "Bing",
     "One last step\nPlease review your order below to continue", BING),
    ("empty SERP 58c", "x - 검색", "콘텐츠로 건너뛰기\n접근성 피드백\nRewards\n전체이미지동영상지도뉴스\n더 보기\n"
     "개인 정보 보호\n사용 약관", BING),
    ("no url", "", REAL_KO, None),
]


@pytest.mark.parametrize("name,title,text,url", CAPTCHA_CASES, ids=[c[0] for c in CAPTCHA_CASES])
def test_bing_challenge_pair_is_captcha(name, title, text, url):
    got = classify(title, text[:4000], len(text), "", 200, url=url)
    assert got.kind is ChallengeKind.CAPTCHA, name
    assert got.vendor == "bing", name
    assert got.reason == REASON, name


@pytest.mark.parametrize("name,title,text,url", NONE_CASES, ids=[c[0] for c in NONE_CASES])
def test_bing_rule_does_not_fire(name, title, text, url):
    got = classify(title, text[:4000], len(text), "", 200, url=url)
    assert got.kind is ChallengeKind.NONE, (name, got)


def test_length_cap_is_short_body_chars():
    """경계: 본문 SHORT_BODY_CHARS 자는 잡고 +1 자는 놓는다(길이는 프로브의 len — 정규화 전 길이)."""
    pad = "가" * (SHORT_BODY_CHARS - len(REAL_KO) - 1)
    text = REAL_KO + "\n" + pad
    assert len(text) == SHORT_BODY_CHARS
    assert classify("Bing", text, len(text), "", 200, url=BING).kind is ChallengeKind.CAPTCHA
    text += "가"
    assert classify("Bing", text, len(text), "", 200, url=BING).kind is ChallengeKind.NONE


def test_existing_rules_keep_priority():
    """보이는 위젯·기존 캡차 문구가 먼저다(위치: _CAPTCHA_PHRASES 뒤·_BLOCKED_PHRASES 앞)."""
    got = classify("Bing", REAL_KO, len(REAL_KO), "recaptcha", 200, url=BING)
    assert got.vendor == "recaptcha"
    text = REAL_EN + "\nverify you are human"
    assert classify("Bing", text, len(text), "", 200, url=BING).vendor == "cloudflare"
    text = REAL_EN + "\nsorry, you have been blocked"
    assert classify("Bing", text, len(text), "", 200, url=BING).kind is ChallengeKind.CAPTCHA
