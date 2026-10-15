"""에이전트에게 돌려주는 주소(URL)의 비밀 값 가리기 (WS-51).

실사용(2026-10): 대기열 통과 JWT(`?gateToken=eyJ…`)가 붙은 예약 페이지를 2분마다 보던 봇의 MCP 도구
결과에 토큰 원문이 657번 실렸다(observation.url·current_url·window.tabs[].url …). 결과는 대화 기록에
남고 모델 제공자로 간다. 이 모듈은 **페이지/탭 상태를 나타내는 주소 칸**을 바깥으로 내보내기 전에
거치는 하나의 공개용 함수 `public_url()` 과, 그것을 문자열·응답 구조에 적용하는 보조 함수를 둔다.

규칙(결정적·멱등 — public_url(public_url(x)) == public_url(x)):

* 스킴·호스트·포트·경로는 그대로, 사용자정보(`user:pass@`)는 뺀다.
* 쿼리와 조각(`#a=b&c=d` 꼴)의 **값**을 다음이면 `[REDACTED]` 로 바꾼다(키 이름은 남긴다).
  (a) 키 이름이 비밀 계열(`_secret_key`): 키를 낱말(토큰)로 쪼개(camelCase·`_`·`-`·`.` 경계) 판정한다.
      긴 낱말(token·session·secret·password·passwd·signature·ticket·credential·assertion·nonce·
      authorization)은 낱말 안 부분 일치(`gatetoken`·`JSESSIONID`), 짧은 낱말(key·sid·sig·otp·pwd·jwt·
      code·auth·sess·saml)은 낱말 전체 일치만 — `keyword`·`monkey`·`codec`·`zipcode`·`author` 오탐을 막는다.
      붙여 쓴 흔한 이름(apikey·phpsessid·passcode …)은 목록으로 둔다.
  (b) 값 자체가 비밀처럼 보임(`_secret_value`): JWT(`eyJ….….…`), 또는 길이 ≥ 24 의 base64/hex/url-safe
      문자열 중 ① 16진수만 ② 대문자·소문자·숫자가 모두 섞임 ③ 구분자 없는 24자 이상 덩어리에 글자와
      숫자가 섞임. 하이픈으로 이은 슬러그·UUID·날짜·검색어·짧은 id 는 남는다.
  값이 다시 절대 주소면(`next=https://…?token=…`) 그 주소에도 같은 규칙을 적용한다.
* 파싱 실패(`urlsplit` 의 ValueError)는 원문 대신 `[REDACTED-URL]` 표지.

**경계**: 페이지 *내용* 으로서의 주소(extract 결과 항목의 href, 관찰 요소)는 가리지 않는다 — 에이전트가
링크를 따라가려면 원문이 필요하다(`CONTENT_KEYS`). 서버 내부(탭 복원·창 전환·레시피·robots·쿠키
장부)는 원문을 계속 쓴다 — 가린 주소로 복원하면 토큰이 필요한 페이지가 깨진다.

가림 표지가 든 주소는 이동하지 않는다(`has_redaction_mark`) — 표지 문자열로 사이트에 요청이 나가지 않게.
"""

from __future__ import annotations

import re
from typing import Any, FrozenSet
from urllib.parse import quote, unquote, unquote_plus, urlsplit

from security.masking import MASK

#: 파싱하지 못한 주소 대신 쓰는 표지(원문을 남기지 않는다).
URL_MASK = "[REDACTED-URL]"

#: 가린 주소로 이동하려 할 때 돌려주는 안내(E_INVALID_URL).
REDACTED_NAVIGATE_HINT = (
    "가려진 주소입니다 — 결과의 주소는 비밀 값을 [REDACTED] 로 가린 표시용이라 이동에 쓸 수 없습니다. "
    "reload·go_back·tab 전환을 쓰거나 페이지의 링크를 다시 추출하세요."
)

#: 낱말 안 부분 일치로 비밀 키를 판정하는 긴 낱말.
_LONG_WORDS = (
    "token", "session", "secret", "password", "passwd", "signature", "ticket",
    "credential", "assertion", "nonce", "authorization",
)
#: 낱말 전체 일치로만 판정하는 짧은 낱말(부분 일치는 keyword·monkey·codec 같은 오탐을 낸다).
_SHORT_WORDS = frozenset({"key", "sid", "sig", "otp", "pwd", "jwt", "code", "auth", "sess", "saml"})
#: 붙여 쓴 흔한 비밀 키 이름(낱말 전체 일치).
_COMPOUNDS = frozenset({
    "apikey", "accesskey", "privatekey", "authkey", "sessid", "phpsessid",
    "authcode", "passcode", "otpcode",
})

_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_JWT_IN_TEXT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_TOKENISH = re.compile(r"[A-Za-z0-9+/=_\-.~]{24,}")
_HEX = re.compile(r"[0-9A-Fa-f]{24,}")
_SEP = re.compile(r"[-_.~+/=]")
#: 자유 문장 안의 절대 주소(공백·따옴표·꺾쇠에서 끝난다). 대괄호는 주소에 포함 — 가림 표지
#: `[REDACTED]` 가 다음 패스에서 쪼개지지 않아야 멱등이다.
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>]+")
_MARK = re.compile(r"\[REDACTED|%5BREDACTED", re.IGNORECASE)

#: 응답 구조에서 페이지 '내용' 으로 보고 가리지 않는 키(관찰 요소·요약, extract 항목).
CONTENT_KEYS: FrozenSet[str] = frozenset({"elements", "axtree_summary", "items"})


def _secret_key(raw_key: str) -> bool:
    """키 이름이 비밀 계열인가(낱말 단위 판정 — 모듈 설명 참조)."""
    key = unquote_plus(raw_key)
    for word in _KEY_WORD.findall(key):
        w = word.lower()
        if w in _SHORT_WORDS or w in _COMPOUNDS:
            return True
        if any(long in w for long in _LONG_WORDS):
            return True
    return False


def _secret_value(raw_value: str) -> bool:
    """값 자체가 비밀처럼 보이는가(JWT·긴 무작위 문자열). 단순·결정적 규칙."""
    v = unquote(raw_value)
    if _JWT.search(v):
        return True
    if len(v) < 24 or not _TOKENISH.fullmatch(v):
        return False
    if _HEX.fullmatch(v):
        return True
    has_digit = any(c.isdigit() for c in v)
    if has_digit and any(c.islower() for c in v) and any(c.isupper() for c in v):
        return True
    for seg in _SEP.split(v):
        if len(seg) >= 24 and any(c.isdigit() for c in seg) and any(c.isalpha() for c in seg):
            return True
    return False


def _public_value(raw_value: str) -> str:
    """비밀이 아닌 값 — 값이 절대 주소면 그 주소도 가린다(바뀐 경우에만 다시 인코딩)."""
    decoded = unquote(raw_value)
    if "://" not in decoded:
        return raw_value
    inner = public_url(decoded)
    if inner == decoded:
        return raw_value
    return quote(inner, safe="/:[]")


def _public_query(query: str) -> str:
    parts = []
    for part in query.split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            if value and (_secret_key(key) or _secret_value(value)):
                value = MASK
            else:
                value = _public_value(value)
            parts.append(f"{key}={value}")
        elif part and _secret_value(part):
            parts.append(MASK)
        else:
            parts.append(part)
    return "&".join(parts)


def _public_fragment(fragment: str) -> str:
    if "=" in fragment:
        if "?" in fragment:  # SPA 경로 `#/route?code=…`
            route, query = fragment.split("?", 1)
            return route + "?" + _public_query(query)
        return _public_query(fragment)
    return MASK if fragment and _secret_value(fragment) else fragment


def public_url(url: str) -> str:
    """에이전트에게 보여 줄 주소. 비밀 계열 쿼리/조각 값을 `[REDACTED]` 로 바꾸고 사용자정보를 뺀다."""
    if not isinstance(url, str) or not url:
        return url
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - 잘못된 포트는 여기서 ValueError
    except ValueError:
        return URL_MASK
    rest, has_frag, fragment = url.partition("#")
    prefix, has_query, query = rest.partition("?")
    netloc = parts.netloc
    if "@" in netloc:
        prefix = prefix.replace("//" + netloc, "//" + netloc.rsplit("@", 1)[1], 1)
    out = prefix
    if has_query:
        out += "?" + _public_query(query)
    if has_frag:
        out += "#" + _public_fragment(fragment)
    return out


def public_text(text: str) -> str:
    """자유 문장(오류 문구·안내·로그 줄) 안의 절대 주소에 public_url 을 적용하고 JWT 를 가린다."""
    if not isinstance(text, str) or not text:
        return text
    if "://" in text:
        text = _URL_IN_TEXT.sub(lambda m: public_url(m.group(0)), text)
    if "eyJ" in text:
        text = _JWT_IN_TEXT.sub(MASK, text)
    return text


def public_payload(obj: Any, *, content_keys: FrozenSet[str] = CONTENT_KEYS) -> Any:
    """응답 구조(dict/list)의 모든 문자열에 public_text 를 적용한 **새** 구조를 돌려준다.

    content_keys 아래(페이지 내용 — 관찰 요소, extract 항목)는 그대로 둔다. 입력은 바꾸지 않는다
    (서버 내부 상태 dict 를 그대로 실어 보내는 경로가 있어 원문을 계속 써야 한다).
    """
    if isinstance(obj, str):
        return public_text(obj)
    if isinstance(obj, dict):
        return {k: (v if k in content_keys else public_payload(v, content_keys=content_keys))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [public_payload(v, content_keys=content_keys) for v in obj]
    if isinstance(obj, tuple):
        return tuple(public_payload(v, content_keys=content_keys) for v in obj)
    return obj


def has_redaction_mark(url: Any) -> bool:
    """주소에 가림 표지(`[REDACTED…` 또는 퍼센트 인코딩)가 들어 있는가 — 이동 전에 거부한다."""
    return isinstance(url, str) and bool(_MARK.search(url))
