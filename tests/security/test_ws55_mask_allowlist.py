"""WS-55 — 출력 가림 과잉 완화: 비밀 아닌 복합 쿼리 키(정확 일치 허용 목록) 보존.

WS-51 검증 NB-2: 일반 페이지 표본 66개 중 25개가 가려졌고(`sortKey`·`countryCode`·`productCode` …), 가린 주소는
navigate 가 거부되어 정렬·페이지 이동 주소를 재사용하지 못했다. 소문자 `sortkey` 는 보존·camelCase `sortKey` 는
가림 — 대소문자에 따라 판정이 갈렸다.

규칙(security.public_url._secret_key):
* 키를 정규화(퍼센트 디코딩 → NFKC → casefold → `_`·`-` 제거)한 뒤 **정확 일치** 허용 목록이면 비밀 키 아님 —
  camelCase/낱말 휴리스틱보다 먼저.
* 허용 목록 키라도 값이 JWT·고엔트로피(`_secret_value`)·레지스트리 등록 값이면 가린다. 평범한 값은 등록하지 않는다.
* promocode·couponcode(개인 1회용 가능)·token_type·code·비밀 낱말 키·혼합 대소문자 UUID 는 계속 가린다.

값은 모두 합성 값. 공개 사이트 없음(`synthetic.invalid`·`h.test` 는 문자열일 뿐 요청하지 않는다).
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from security import secret_registry
from security.public_url import has_redaction_mark, public_payload, public_text, public_url, secret_values

R = "[REDACTED]"
BASE = "https://synthetic.invalid/p"


@pytest.fixture(autouse=True)
def _fresh_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


# ------------------------------------------------------------------ WS-51 benign 66 표본(검증 보고서 NB-2 표)

#: (번호, 키/구분, 값). 키가 None 이면 값이 전체 주소(경로·조각·인코딩 표본).
BENIGN_66 = [
    (1, "query", "아이오닉"), (2, "query", "전기차 충전소"), (3, "query", "hello world"),
    (4, "q", "climate-change-mitigation"), (5, "q", "internationalization"), (6, "q", "characteristically"),
    (7, "page", "2"), (8, "sort", "date"), (9, "date", "2026-10-16"), (10, "start", "2026-10-16T12:30:00Z"),
    (11, "id", "12345"), (12, "id", "123456789012345678901234"),
    (13, "id", "550e8400-e29b-41d4-a716-446655440000"), (14, "id", "550E8400-E29B-41D4-A716-446655440000"),
    (15, "id", "550e8400-E29b-41D4-a716-446655440000"),
    (16, "keyword", "camping"), (17, "monkey", "banana"), (18, "codec", "h264"), (19, "zipcode", "04524"),
    (20, "author", "kim"), (21, "keyboard", "us"), (22, "sortkey", "date"), (23, "hotkey", "ctrl-k"),
    (24, "countrycode", "KR"), (25, "productCode", "SKU12"), (26, "session", "morning"),
    (27, "sessionName", "afternoon"), (28, "tokenization", "words"), (29, "secretariat", "office"),
    (30, "noncelebrity", "news"), (31, "ticket", "adult"), (32, "signature", "serif"),
    (33, "authorization", "public"), (34, "passwordless", "true"), (35, "assertion", "true"),
    (36, "q", "python311documentation2026"), (37, "slug", "how-to-build-a-garden-2026"),
    (38, "q", "2026-10-16-2026-10-30"), (39, "locale", "ko-KR"), (40, "lat", "37.5665"), (41, "lon", "126.9780"),
    (42, "price", "123456.78"), (43, "filter", "red,blue,green"), (44, "file", "annual-report-2026.pdf"),
    (45, "q", "abcDEF1234567890abcdef1234"), (46, "q", "abcdefghijklmnopqrstuvwx"),
    (47, "q", "aaaaaaaaaaaaaaaaaaaaaaaa"), (48, "id", "abcdefabcdefabcdefabcdef"),
    (49, "path", "docs/reference/overview"), (50, "redirect", "https://synthetic.invalid/help?page=2"),
    (51, "q", "한글검색어를길게적어도원문이남아야합니다"), (52, "id", "ORD-2026-10-16-000123"), (53, "time", "12:30:00"),
    (54, "tags", "science+technology"), (55, "name", "Alice-Smith"), (56, "code", "US"), (57, "key", "ArrowDown"),
    (58, "sig", "public"), (59, "sortKey", "date"), (60, "promoCode", "SUMMER"), (61, "countryCode", "KR"),
    (62, "token_type", "bearer"), (63, "sessions", "3"),
    (64, None, "https://synthetic.invalid/assets/abcdefabcdefabcdefabcdef/site.css?page=2"),
    (65, None, "https://synthetic.invalid/page#2026-10-16"),
    (66, None, "https://synthetic.invalid/search?q=%ED%95%9C%EA%B8%80"),
]

#: WS-51(e0b1093) 에서 가려진 25개(검증 보고서 표 그대로).
MASKED_BEFORE = {12, 15, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 45, 47, 48, 56, 57, 58, 59, 60, 61, 62, 63}
#: WS-55 에서 보존으로 바뀌는 것 — 허용 목록 복합어(productCode·sortKey·countryCode).
NOW_PRESERVED = {25, 59, 61}
MASKED_AFTER = MASKED_BEFORE - NOW_PRESERVED


def _sample_url(key, value):
    if key is None:
        return value
    return f"{BASE}?{key}={quote(value, safe='-._~')}"


def test_benign_66_sample_is_complete():
    assert [n for n, _, _ in BENIGN_66] == list(range(1, 67))
    assert len(MASKED_BEFORE) == 25 and len(MASKED_AFTER) == 22


@pytest.mark.parametrize("num,key,value", BENIGN_66, ids=[f"{n}-{k}" for n, k, _ in BENIGN_66])
def test_benign_66_after(num, key, value):
    url = _sample_url(key, value)
    out = public_url(url)
    if num in MASKED_AFTER:
        assert out != url and R in out, (num, out)
    else:
        assert out == url, (num, out)


def test_benign_66_masked_count_drops_25_to_22():
    masked = {n for n, k, v in BENIGN_66 if public_url(_sample_url(k, v)) != _sample_url(k, v)}
    assert masked == MASKED_AFTER


# ------------------------------------------------------------------ 허용 목록 × 정규화

#: (정규형, camelCase 원형) — 정규형은 소문자·구분자 없음.
ALLOW = [
    ("sortkey", "sortKey"), ("sortorder", "sortOrder"), ("orderby", "orderBy"),
    ("countrycode", "countryCode"), ("zipcode", "zipCode"), ("postcode", "postCode"),
    ("postalcode", "postalCode"), ("statuscode", "statusCode"), ("errorcode", "errorCode"),
    ("languagecode", "languageCode"), ("langcode", "langCode"), ("currencycode", "currencyCode"),
    ("productcode", "productCode"), ("itemcode", "itemCode"), ("categorycode", "categoryCode"),
    ("regioncode", "regionCode"), ("areacode", "areaCode"), ("citycode", "cityCode"),
    ("statecode", "stateCode"), ("airportcode", "airportCode"), ("stationcode", "stationCode"),
    ("colorcode", "colorCode"), ("stylecode", "styleCode"), ("sizecode", "sizeCode"), ("keycode", "keyCode"),
]


def _snake(camel: str) -> str:
    return "".join("_" + c.lower() if c.isupper() else c for c in camel)


def _variants(canon: str, camel: str):
    snake = _snake(camel)
    return sorted({
        canon, canon.upper(), camel, camel[0].upper() + camel[1:], snake, snake.upper(), snake.replace("_", "-"),
        "%" + format(ord(camel[0]), "02X") + camel[1:],  # 퍼센트 인코딩 첫 글자
        "".join(chr(ord(c) + 0xFEE0) for c in camel),  # 전각(NFKC → 원형)
    })


ALLOW_VARIANTS = [(canon, v) for canon, camel in ALLOW for v in _variants(canon, camel)]


@pytest.mark.parametrize("canon,key", ALLOW_VARIANTS, ids=[v for _, v in ALLOW_VARIANTS])
def test_allowlisted_key_every_spelling_preserves_plain_value(canon, key):
    url = f"{BASE}?{key}=date&page=2"
    assert public_url(url) == url
    assert secret_values(url) == []


@pytest.mark.parametrize("canon,camel", ALLOW)
def test_allowlisted_key_spellings_agree(canon, camel):
    """정규화 — 대소문자·`_`·`-` 에 따라 판정이 갈리지 않는다(WS-51 의 sortkey 보존 ↔ sortKey 가림 불일치)."""
    outs = {public_url(f"{BASE}?{k}=KR").replace(k, "K") for k in _variants(canon, camel)}
    assert outs == {f"{BASE}?K=KR"}


# ------------------------------------------------------------------ 허용 목록 키 × 값 행렬

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aCJ9.c3ludGhzaWduYXR1cmU"
HIGH_ENTROPY = "aB3dE5fG7hJ9kL1mN3pQ5rS7tU"  # 26자, 대·소문자·숫자
HEX32 = "0123456789abcdef0123456789abcdef"
PLAIN = ["date", "KR", "04524", "SKU12", "desc", "ko"]


@pytest.mark.parametrize("value", [JWT, HIGH_ENTROPY, HEX32, quote(JWT, safe="")])
@pytest.mark.parametrize("key", [camel for _, camel in ALLOW] + ["sort_key", "COUNTRY-CODE"])
def test_allowlisted_key_secret_looking_value_masked(key, value):
    url = f"{BASE}?{key}={value}&page=2"
    out = public_url(url)
    assert out == f"{BASE}?{key}={R}&page=2", out
    assert has_redaction_mark(out)  # 가린 주소 navigate 거부 유지(표지 판정)
    raw = secret_values(url)
    assert raw == [value]  # 값 기반으로 가린 것은 키와 무관하게 비밀 값(레지스트리 등록 대상)


@pytest.mark.parametrize("value", PLAIN)
@pytest.mark.parametrize("key", [camel for _, camel in ALLOW])
def test_allowlisted_key_plain_value_preserved_not_registered(key, value):
    url = f"{BASE}?{key}={value}"
    secret_registry.register_url(url)
    assert len(secret_registry.REGISTRY) == 0  # 평범한 값은 레지스트리에 들어가지 않는다
    assert public_url(url) == url
    assert public_payload({"url": url, "title": f"see {value}"}) == {"url": url, "title": f"see {value}"}
    assert not has_redaction_mark(public_url(url))


@pytest.mark.parametrize("key", ["sortKey", "countryCode", "productCode", "zip_code"])
def test_allowlisted_key_registry_value_masked(key):
    """도구가 다른 주소에서 비밀로 본 값이 허용 목록 키에 실리면 주소 칸·자유 문장에서 가린다."""
    secret = "SYNTHws55Val9"  # 13자 — 값 모양만으로는 가리지 않는다
    assert public_url(f"{BASE}?{key}={secret}") == f"{BASE}?{key}={secret}"
    secret_registry.register_url(f"http://127.0.0.1:1/a?token={secret}")
    url = f"{BASE}?{key}={secret}&page=2"
    out = public_payload({"url": url, "message": f"opened {url}"})
    assert secret not in out["url"] and secret not in out["message"], out
    assert has_redaction_mark(out["url"])
    assert out["url"].endswith("&page=2")


def test_allowlisted_key_in_free_text_preserved():
    text = "go to /p?sortKey=date&countryCode=KR next"
    assert public_text(text) == text


# ------------------------------------------------------------------ 유지(가림)

KEEP_MASKED = [
    "promoCode", "promocode", "PROMOCODE", "promo_code", "promo-code", "PromoCode",
    "couponCode", "couponcode", "coupon_code", "COUPON-CODE",
    "token_type", "tokenType", "tokentype", "code", "Code", "key", "sig", "auth", "sid",
    "session", "sessions", "sessionKey", "secret", "password", "ticket", "nonce", "signature",
    "api_key", "apiKey", "access_token", "authCode", "auth_code", "passcode", "pageKey", "page_key", "cursorKey",
]


@pytest.mark.parametrize("key", KEEP_MASKED)
def test_secret_keys_stay_masked(key):
    url = f"{BASE}?{key}=SUMMER&page=2"
    out = public_url(url)
    assert out == f"{BASE}?{key}={R}&page=2", out
    assert secret_values(url) == ["SUMMER"]


@pytest.mark.parametrize("near", ["sort.key", "country code", "sort_key_id", "sortKeyId", "x_sort_key",
                                  "countryCodeKey", "promo.countrycode.key"])
def test_allowlist_is_exact_match_only(near):
    """허용 목록은 정확 일치 — 비슷한 키는 기존 휴리스틱으로 판정(가림 쪽으로 보수적)."""
    url = f"{BASE}?{quote(near, safe='[]')}=SUMMER"
    assert R in public_url(url), near


def test_mixed_case_uuid_still_masked_under_allowlisted_key():
    url = f"{BASE}?productCode=550e8400-E29b-41D4-a716-446655440000"
    assert public_url(url) == f"{BASE}?productCode={R}"
