"""WS-51 public_url — 에이전트에게 돌려주는 주소의 비밀 값 가리기 (단위, 합성 값만).

표본은 비밀 키 계열·값 기반·보존·조각·사용자정보·잘못된 주소·멱등성·오탐 경계를 모두 담는다.
토큰은 전부 합성 값이다(실제 비밀 아님).
"""

from __future__ import annotations

import logging

import pytest

from security.public_url import (
    CONTENT_KEYS,
    URL_MASK,
    has_redaction_mark,
    public_payload,
    public_text,
    public_url,
)

R = "[REDACTED]"
#: 합성 JWT(헤더 {"alg":"HS256","typ":"JWT"} 모양 + 합성 본문·서명). 실제 토큰 아님.
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJTWU5USF9OT1RfQV9TRUNSRVQifQ.c3ludGhfc2lnX25vdF9yZWFs"
B64 = "SYNTHnotAsecret0123456789abcdXYZ"  # 32자, 대·소문자·숫자 섞임
HEX = "0123456789abcdef0123456789abcdef"  # 32자 16진수
LOWER_RUN = "synthnotasecret0123456789xyz"  # 28자 구분자 없는 소문자+숫자

# (입력, 기대 출력)
CASES = [
    # --- (a) 키 이름이 비밀 계열 ---
    (f"https://camping.example.test/r?gateToken={JWT}&date=2026-10-16",
     f"https://camping.example.test/r?gateToken={R}&date=2026-10-16"),
    ("https://a.test/cb?access_token=short1&x=1", f"https://a.test/cb?access_token={R}&x=1"),
    ("https://s3.test/o?X-Amz-Signature=abc123&X-Amz-Date=20261016T000000Z",
     f"https://s3.test/o?X-Amz-Signature={R}&X-Amz-Date=20261016T000000Z"),
    ("https://s3.test/o?X-Amz-Credential=AKSYNTH%2F20261016&X-Amz-Expires=300",
     f"https://s3.test/o?X-Amz-Credential={R}&X-Amz-Expires=300"),
    ("https://a.test/?sessionid=abc", f"https://a.test/?sessionid={R}"),
    ("https://a.test/;x?JSESSIONID=abc", f"https://a.test/;x?JSESSIONID={R}"),
    ("https://a.test/?PHPSESSID=abc", f"https://a.test/?PHPSESSID={R}"),
    ("https://a.test/?sid=1", f"https://a.test/?sid={R}"),
    ("https://a.test/oauth/cb?code=synth1&state=xyz", f"https://a.test/oauth/cb?code={R}&state=xyz"),
    ("https://a.test/?api_key=k1&apiKey=k2&apikey=k3", f"https://a.test/?api_key={R}&apiKey={R}&apikey={R}"),
    ("https://a.test/reset?password=hunter2", f"https://a.test/reset?password={R}"),
    ("https://a.test/?pwd=x&passwd=y&otp=123456", f"https://a.test/?pwd={R}&passwd={R}&otp={R}"),
    ("https://a.test/sso?SAMLResponse=PHN5bnRo&RelayState=home", f"https://a.test/sso?SAMLResponse={R}&RelayState=home"),
    ("https://a.test/cas?ticket=ST-1-synth", f"https://a.test/cas?ticket={R}"),
    ("https://a.test/wp?_wpnonce=ab12", f"https://a.test/wp?_wpnonce={R}"),
    ("https://a.test/?client_secret=s&auth=a&jwt=j&sig=g", f"https://a.test/?client_secret={R}&auth={R}&jwt={R}&sig={R}"),
    ("https://a.test/?csrftoken=c&authToken=t", f"https://a.test/?csrftoken={R}&authToken={R}"),
    # --- (b) 값 기반 ---
    (f"https://a.test/?q={JWT}", f"https://a.test/?q={R}"),
    (f"https://a.test/?state={B64}", f"https://a.test/?state={R}"),
    (f"https://a.test/?h={HEX}", f"https://a.test/?h={R}"),
    (f"https://a.test/?v={LOWER_RUN}", f"https://a.test/?v={R}"),
    (f"https://a.test/?{B64}", f"https://a.test/?{R}"),
    # 값이 다시 주소: 안쪽 주소의 비밀만
    ("https://a.test/login?next=https%3A%2F%2Fb.test%2Fp%3Ftoken%3Dabc%26page%3D2",
     "https://a.test/login?next=https://b.test/p%3Ftoken%3D[REDACTED]%26page%3D2"),
    # --- 조각(#) ---
    (f"https://a.test/cb#access_token={JWT}&token_type=bearer&expires_in=3600",
     f"https://a.test/cb#access_token={R}&token_type={R}&expires_in=3600"),  # token_type 은 낱말 token — 과다(의도)
    ("https://a.test/app#/route?code=synth&tab=2", f"https://a.test/app#/route?code={R}&tab=2"),
    (f"https://a.test/p#{JWT}", f"https://a.test/p#{R}"),
    ("https://a.test/p#section-2", "https://a.test/p#section-2"),
    # --- 사용자정보 ---
    ("https://user:synthpass@a.test:8443/x?page=2", "https://a.test:8443/x?page=2"),
    ("ftp://anon@a.test/f", "ftp://a.test/f"),
    # --- 보존 ---
    ("https://a.test/search?query=아이오닉&page=2&sort=date", "https://a.test/search?query=아이오닉&page=2&sort=date"),
    ("https://a.test/s?query=%EC%95%84%EC%9D%B4%EC%98%A4%EB%8B%89", "https://a.test/s?query=%EC%95%84%EC%9D%B4%EC%98%A4%EB%8B%89"),
    ("https://a.test/item?id=12345&date=2026-10-16", "https://a.test/item?id=12345&date=2026-10-16"),
    ("https://a.test/blog/best-camping-sites-in-korea-2026?ref=home-page-banner-top-2026",
     "https://a.test/blog/best-camping-sites-in-korea-2026?ref=home-page-banner-top-2026"),
    ("https://a.test/o?orderId=550e8400-e29b-41d4-a716-446655440000",
     "https://a.test/o?orderId=550e8400-e29b-41d4-a716-446655440000"),
    ("https://a.test/watch?v=dQw4w9WgXcQ&t=42", "https://a.test/watch?v=dQw4w9WgXcQ&t=42"),
    ("http://127.0.0.1:8080/camp", "http://127.0.0.1:8080/camp"),
    ("about:blank", "about:blank"),
    ("", ""),
    ("https://a.test/?flag&x=", "https://a.test/?flag&x="),
    ("https://a.test/?token=", "https://a.test/?token="),  # 빈 값은 가릴 것이 없다
    # --- 오탐 경계(낱말 단위 판정) ---
    ("https://a.test/?keyword=tent&monkey=1&codec=h264", "https://a.test/?keyword=tent&monkey=1&codec=h264"),
    ("https://a.test/?zipcode=06236&author=kim&authuser=0", "https://a.test/?zipcode=06236&author=kim&authuser=0"),
    ("https://a.test/?signin=1&design=a&passage=3", "https://a.test/?signin=1&design=a&passage=3"),
    # WS-55: sortKey 는 비밀 아닌 복합어 허용 목록(보존), promoCode 는 개인 1회용일 수 있어 가림 유지.
    ("https://a.test/?sortKey=date&promoCode=SUMMER", f"https://a.test/?sortKey=date&promoCode={R}"),
    ("https://a.test/?sessions=3", f"https://a.test/?sessions={R}"),  # 긴 낱말 부분 일치 — 과다(의도)
]


@pytest.mark.parametrize("url,expected", CASES)
def test_public_url_samples(url, expected):
    assert public_url(url) == expected


def test_sample_count_at_least_30():
    assert len(CASES) >= 30


@pytest.mark.parametrize("url", [c[0] for c in CASES] + [c[1] for c in CASES])
def test_idempotent(url):
    once = public_url(url)
    assert public_url(once) == once


@pytest.mark.parametrize("bad", ["http://[::1/x?token=SYNTH", "https://a.test:99999999/p?gateToken=SYNTH",
                                 "http://[bad host]/?sid=SYNTH"])
def test_unparseable_url_leaves_no_original(bad):
    out = public_url(bad)
    assert out == URL_MASK
    assert "SYNTH" not in out
    assert public_url(out) == out


def test_no_raw_secret_survives_in_any_masked_sample():
    for url, _ in CASES:
        out = public_url(url)
        for secret in (JWT, B64, HEX, LOWER_RUN, "synthpass"):
            assert secret not in out, (url, out)


def test_public_text_masks_urls_and_jwts_in_sentences():
    msg = (f"Page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:9/camp?gateToken={JWT}&date=2026-10-16\n"
           f"(현재 프레임 https://a.test/f?sid=SYNTH1 과 최상위) bare {JWT} end")
    out = public_text(msg)
    assert JWT not in out and "SYNTH1" not in out
    assert "date=2026-10-16" in out and "현재 프레임" in out
    assert public_text(out) == out


def test_public_payload_skips_content_keys_and_does_not_mutate():
    src = {
        "current_url": f"http://h.test/camp?gateToken={JWT}",
        "data": {
            "url": f"http://h.test/camp?gateToken={JWT}",
            "tabs": [{"tab_id": "t1", "url": f"http://h.test/?sid={B64}"}],
            "observation": {"url": f"http://h.test/?code=SYNTHX", "title": "t",
                            "elements": [{"name": f"http://h.test/?token={JWT}"}],
                            "axtree_summary": f"link http://h.test/?token={JWT}"},
            "items": [{"href": f"http://h.test/next?gateToken={JWT}"}],
            "n": 3, "ok": True, "none": None,
        },
    }
    import copy

    before = copy.deepcopy(src)
    out = public_payload(src)
    assert src == before  # 입력 불변
    assert JWT not in out["current_url"] and JWT not in out["data"]["url"]
    assert B64 not in out["data"]["tabs"][0]["url"]
    assert "SYNTHX" not in out["data"]["observation"]["url"]
    # 내용(관찰 요소·요약·extract 항목)은 원문
    assert out["data"]["observation"]["elements"] == src["data"]["observation"]["elements"]
    assert out["data"]["observation"]["axtree_summary"] == src["data"]["observation"]["axtree_summary"]
    assert out["data"]["items"] == src["data"]["items"]
    assert out["data"]["n"] == 3 and out["data"]["ok"] is True and out["data"]["none"] is None
    # WS-62: 관찰 data.repeated(반복 묶음 대상 이름·표제)도 페이지 내용 경로 — elements[].name 과 같은 원문.
    assert CONTENT_KEYS == frozenset({"elements", "axtree_summary", "items", "repeated"})


@pytest.mark.parametrize("url,marked", [
    ("https://a.test/?gateToken=[REDACTED]&date=1", True),
    ("https://a.test/?gateToken=%5BREDACTED%5D", True),
    ("https://a.test/?gateToken=%5bredacted%5d", True),
    (URL_MASK, True),
    ("https://a.test/redacted-documents?page=2", False),
    ("https://a.test/?q=REDACTED", False),
    (None, False),
])
def test_has_redaction_mark(url, marked):
    assert has_redaction_mark(url) is marked


def test_stderr_line_masks_page_urls(capfd):
    from interface.server_common import _stderr_line

    _stderr_line(f"agent-browser serve: 창 닫힘 http://127.0.0.1:1/camp?gateToken={JWT}&date=2026-10-16")
    err = capfd.readouterr().err
    assert JWT not in err and "date=2026-10-16" in err


def test_log_record_factory_masks_messages_and_tracebacks():
    from interface.mcp_server import install_public_log_filter

    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    lg = logging.getLogger("ws51.test")
    h = H()
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    lg.propagate = False
    restore = install_public_log_filter()
    try:
        lg.warning("이동 실패 %s", f"http://h.test/camp?gateToken={JWT}")
        try:
            raise RuntimeError(f"boom at http://h.test/?sid={B64}")
        except RuntimeError:
            lg.exception("예외")
    finally:
        restore()
        lg.removeHandler(h)
    text = "\n".join(records)
    assert JWT not in text and B64 not in text
    assert "http://h.test/camp?gateToken=[REDACTED]" in text
