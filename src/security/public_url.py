"""에이전트에게 돌려주는 주소(URL)의 비밀 값 가리기 (WS-51).

실사용(2026-10): 대기열 통과 JWT(`?gateToken=eyJ…`)가 붙은 예약 페이지를 2분마다 보던 봇의 MCP 도구
결과에 토큰 원문이 657번 실렸다(observation.url·current_url·window.tabs[].url …). 결과는 대화 기록에
남고 모델 제공자로 간다. 이 모듈은 **페이지/탭 상태를 나타내는 주소 칸**을 바깥으로 내보내기 전에
거치는 하나의 공개용 함수 `public_url()` 과, 그것을 문자열·응답 구조에 적용하는 보조 함수를 둔다.

규칙(결정적·멱등 — public_url(public_url(x)) == public_url(x)):

* 스킴·호스트·포트·경로는 그대로, 사용자정보(`user:pass@`)는 뺀다. 스킴은 묻지 않는다 — `about:blank#…`,
  `data:`·`blob:`, 스킴 없는 상대 주소(`/p?x=1`, `?x=1`, `#x=1`)도 같은 규칙(R1 B3).
* 쿼리와 조각(`#a=b&c=d` 꼴)의 **값**을 다음이면 `[REDACTED]` 로 바꾼다(키 이름은 남긴다). 파라미터
  구분자는 `&` 와 `;` 둘 다이고, 구분자와 순서는 그대로 둔다. 인코딩된 `%26`·`%3B` 는 값 내용이다(R1 B2).
  (a) 키 이름이 비밀 계열(`_secret_key`, 대소문자 무시 — R1 B1). 키는 퍼센트 인코딩을 (최대 3겹) 풀고
      Unicode NFKC + casefold 로 정규화한다(R2 B7 — 전각 `ｔｏｋｅｎ`, long-s `ſeſſion`. 터키어 `ı`·`İ` 는
      ASCII 의 casefold 동치가 아니라 범위 밖):
      ① **정규화한 전체 키**에 긴 낱말(token·session·secret·password·passwd·signature·ticket·credential·
         assertion·nonce·authorization)이 들어 있거나(`gatetoken`·`GaTeToKeN`·`JSESSIONID`),
      ② 정규화한 키를 영문/숫자 덩어리로 쪼갠 낱말이 짧은 낱말(key·sid·sig·otp·pwd·jwt·code·auth·sess·saml)
         이나 붙여 쓴 흔한 이름(apikey·phpsessid·passcode …) 과 **통째로** 같거나(`sId`·`API_KEY`),
      ③ 원래 키의 camelCase 낱말이 그렇다(`sortKey`·`promoCode` — 기존 낱말 분해 규칙).
      짧은 낱말을 부분 일치로 보지 않아 `keyword`·`monkey`·`codec`·`zipcode`·`author` 는 남는다.
      ①② 는 casefold 만 보므로 어떤 대소문자 조합도 같은 판정이다. ③ 은 그 위에 더하는 규칙이다.
  (b) 값 자체가 비밀처럼 보임(`_secret_value`): JWT(`eyJ….….…`), 또는 길이 ≥ 24 의 base64/hex/url-safe
      문자열 중 ① 16진수만 ② 대문자·소문자·숫자가 모두 섞임 ③ 구분자 없는 24자 이상 덩어리에 글자와
      숫자가 섞임. 하이픈으로 이은 슬러그·UUID·날짜·검색어·짧은 id 는 남는다.
  값이 다시 절대 주소면(`next=https://…?token=…`) 그 주소에도 같은 규칙을 적용한다(중첩 4단계까지, 그보다
  깊으면 그 값을 `[REDACTED]` — 깊은 입력에서 재귀 예외 없이 끝난다).
* 파싱 실패(`urlsplit` 의 ValueError)·2MiB 초과 주소는 원문 대신 `[REDACTED-URL]` 표지.

**시간**: 모든 탐색은 입력 길이에 선형이다(R1 B5 — 64KiB 제목 하나로 observe 16초였다). 자유 문장의 주소
후보는 공백·따옴표·꺾쇠로 나눈 덩어리의 **시작에서만** 찾고, 스킴 후보는 64자 상한으로 찾는다(상한이 없으면
긴 글자 덩어리에서 시작점마다 다시 훑는 이차시간). JWT 는 정규식 대신 덩어리 단위로 한 번씩 훑는다.

**자유 문장의 값 경계**(R2 B6): 주소 덩어리가 비밀 키 값으로 끝났는데 문장이 이어지면 — 값이 비었고 바로
따옴표면(`token="X"`) 짝 따옴표까지(없으면 줄 끝), 값 뒤가 공백이면(`token=prefix X`) 같은 값일 수 있어 다음
구분 기호(`&`·`;`·`#`·줄바꿈·닫는 괄호·따옴표·꺾쇠)까지 안전 쪽으로 가린다(한 번에 MAX_VALUE_TAIL_CHARS, 상한이
단어 안에 걸리면 그 단어 끝까지 — 다음 주소 덩어리 한가운데서 끊지 않는다). 대가: `?token=X 다음 말` 의
'다음 말' 도 가려진다. 주소 칸(url·*_url)은 주소 전체를 하나로 보므로 이 규칙이 필요 없다.

**경계**: 페이지 *내용* 으로서의 주소는 가리지 않는다 — 에이전트가 링크를 따라가려면 원문이 필요하다
(`CONTENT_PATHS`: 응답의 `data.items`(extract 항목), `data.observation.elements`·`axtree_summary`. **경로**로
정한다 — 다른 자리의 같은 이름 키는 가린다, R1 NB-1). 서버 내부(탭 복원·창 전환·레시피·robots·쿠키
장부)는 원문을 계속 쓴다 — 가린 주소로 복원하면 토큰이 필요한 페이지가 깨진다.

가림 표지가 든 주소는 이동하지 않는다(`has_redaction_mark`) — 표지 문자열로 사이트에 요청이 나가지 않게.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from typing import Any, FrozenSet, List, Optional, Tuple
from urllib.parse import quote, unquote, unquote_plus, urlsplit

from security.masking import MASK

#: 파싱하지 못한(또는 너무 긴) 주소 대신 쓰는 표지(원문을 남기지 않는다).
URL_MASK = "[REDACTED-URL]"
#: 응답 구조가 너무 깊어 더 내려가지 않은 자리의 표지.
DEPTH_MASK = "[REDACTED-DEPTH]"

#: 가린 주소로 이동하려 할 때 돌려주는 안내(E_INVALID_URL).
REDACTED_NAVIGATE_HINT = (
    "가려진 주소입니다 — 결과의 주소는 비밀 값을 [REDACTED] 로 가린 표시용이라 이동에 쓸 수 없습니다. "
    "reload·go_back·tab 전환을 쓰거나 페이지의 링크를 다시 추출하세요."
)

#: 주소 하나의 길이 상한(Chromium 주소 상한 2MB). 넘으면 URL_MASK.
MAX_URL_CHARS = 2 * 1024 * 1024
#: 자유 문장에서 찾는 스킴 후보의 길이 상한(실제 스킴은 짧다 — http·chrome-extension …).
MAX_SCHEME_CHARS = 64
#: 값 안의 주소(next=…) 중첩 상한.
MAX_URL_DEPTH = 4
#: 응답 구조(dict/list) 깊이 상한.
MAX_PAYLOAD_DEPTH = 64
#: 이 길이 이상의 문자열은 가린 결과를 잠시 기억한다(크기 상한의 반복 직렬화 — public_text 참조).
_CACHE_MIN_CHARS = 16 * 1024
#: 키 이름의 퍼센트 인코딩을 푸는 최대 겹 수(`%2574oken` → `%74oken` → `token`).
_MAX_KEY_UNQUOTE = 3
#: 자유 문장에서 비밀 키 값이 공백·따옴표 뒤로 이어질 때 한 번에 더 가리는 최대 글자 수(R2 B6).
MAX_VALUE_TAIL_CHARS = 1024
#: 줄바꿈 글자(str.splitlines 기준) — 공백 연장·짝 없는 따옴표 연장은 줄을 넘지 않는다.
_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_QUOTES = "\"'"

#: 낱말 안 부분 일치로 비밀 키를 판정하는 긴 낱말(소문자 전체 키에 대해).
_LONG_WORDS = (
    "token", "session", "secret", "password", "passwd", "signature", "ticket",
    "credential", "assertion", "nonce", "authorization",
)
#: 낱말 전체 일치로만 판정하는 짧은 낱말(부분 일치는 keyword·monkey·codec 같은 오탐을 낸다).
_SHORT_WORDS = frozenset({"key", "sid", "sig", "otp", "pwd", "jwt", "code", "auth", "sess", "saml"})
#: 붙여 쓴 흔한 비밀 키 이름(낱말 전체 일치).
_COMPOUNDS = frozenset({
    "apikey", "accesskey", "privatekey", "authkey", "sessid", "phpsessid",
    "authcode", "passcode", "otpcode", "samlresponse", "samlrequest",
})

_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_KEY_SEGMENT = re.compile(r"[a-z]+|\d+")
_PARAM_SEP = re.compile(r"([&;])")
_B64_RUN = re.compile(r"[A-Za-z0-9_\-]+")
_TOKENISH = re.compile(r"[A-Za-z0-9+/=_\-.~]{24,}")
_HEX = re.compile(r"[0-9A-Fa-f]{24,}")
_SEP = re.compile(r"[-_.~+/=]")
#: 자유 문장의 주소 후보 덩어리: 공백·따옴표·꺾쇠로 나뉜 덩어리 중 `://`·`?`·`#` 가 든 것.
#: 덩어리 **시작에서만** 시작한다(앞 글자가 덩어리 글자면 즉시 실패) — 시작점마다 다시 훑지 않아 선형.
#: 대괄호는 덩어리에 포함 — 가림 표지 `[REDACTED]` 가 다음 패스에서 쪼개지지 않아야 멱등이다.
_CANDIDATE = re.compile(r"(?<![^\s\"'<>])[^\s\"'<>]*?(?:://|[?#])[^\s\"'<>]*")
#: 덩어리 안 절대 주소의 시작(스킴). 스킴 길이 상한이 이차시간을 막는다.
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,%d}://" % (MAX_SCHEME_CHARS - 1))
_MARK = re.compile(r"\[REDACTED|%5BREDACTED", re.IGNORECASE)
#: 공백 뒤 이어지는 비밀 값 묶음의 끝: 파라미터·조각 구분자, 줄바꿈, 닫는 괄호, 따옴표·꺾쇠(R2 B6).
#: `]` 는 넣지 않는다 — 가림 표지 `[REDACTED]` 안에서 끊기면 멱등이 깨진다.
_TAIL_END = re.compile("[&;#)}\"'<>" + _LINE_BREAKS + "]")
#: 따옴표로 시작한 값의 끝: 짝 따옴표 또는 줄바꿈.
_QUOTE_STOP = {q: re.compile("[" + re.escape(q) + _LINE_BREAKS + "]") for q in _QUOTES}
#: 상한에 걸린 연장을 끝낼 자리(공백·구분 기호·닫는 괄호·따옴표·꺾쇠).
_WORD_END = re.compile(r"[\s&;#)}\"'<>]")
#: 연장이 구분 기호에서 멈춘 뒤 이어지는 덩어리(공백·따옴표·꺾쇠 전까지).
_RUN = re.compile(r"[^\s\"'<>]*")

#: 응답 구조에서 페이지 '내용' 으로 보고 가리지 않는 **경로**(최상위 응답 기준): extract 항목, 관찰 요소·요약.
CONTENT_PATHS: FrozenSet[Tuple[str, ...]] = frozenset({
    ("data", "items"),
    ("data", "observation", "elements"),
    ("data", "observation", "axtree_summary"),
})
#: (호환) 위 경로의 마지막 키 이름. 판정은 CONTENT_PATHS(경로)로 한다.
CONTENT_KEYS: FrozenSet[str] = frozenset(p[-1] for p in CONTENT_PATHS)
_MAX_CONTENT_PATH = max(len(p) for p in CONTENT_PATHS)


# ------------------------------------------------------------------ 키·값 판정


def _secret_key(raw_key: str) -> bool:
    """키 이름이 비밀 계열인가(대소문자 무시 — 모듈 설명 (a)).

    판정 정규형(R2 B7): 퍼센트 디코딩 → NFKC(전각 `ｔｏｋｅｎ` → token) → casefold(long-s `ſeſſion` → session).
    """
    key = raw_key
    for _ in range(_MAX_KEY_UNQUOTE):
        decoded = unquote_plus(key)
        if decoded == key:
            break
        key = decoded
    key = unicodedata.normalize("NFKC", key)
    low = key.casefold()
    if any(word in low for word in _LONG_WORDS):
        return True
    for seg in _KEY_SEGMENT.findall(low):
        if seg in _SHORT_WORDS or seg in _COMPOUNDS:
            return True
    for word in _KEY_WORD.findall(key):
        w = word.casefold()
        if w in _SHORT_WORDS or w in _COMPOUNDS:
            return True
    return False


def _word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _jwt_spans(text: str, boundary: bool) -> List[Tuple[int, int]]:
    """`eyJ[A-Za-z0-9_-]{5,}\\.[A-Za-z0-9_-]{5,}\\.[A-Za-z0-9_-]*` 와 같은 구간들(boundary=True 면 앞에 `\\b`).

    정규식으로 찾으면 `eyJ-eyJ-…` 같은 덩어리에서 시작점마다 덩어리 끝까지 다시 훑어 이차시간이 된다. 여기서는
    `eyJ` 가 든 base64url 덩어리를 한 번만 재고 그 덩어리 안에서 조건을 만족하는 가장 앞 시작점을 고른다(선형).
    """
    spans: List[Tuple[int, int]] = []
    pos = 0
    while True:
        p = text.find("eyJ", pos)
        if p < 0:
            return spans
        end0 = _B64_RUN.match(text, p).end()  # type: ignore[union-attr]  # 'eyJ' 자체가 덩어리 글자
        start: Optional[int] = None
        q = p
        while q >= 0 and end0 - (q + 3) >= 5:
            if not boundary or q == 0 or not _word_char(text[q - 1]):
                start = q
                break
            q = text.find("eyJ", q + 1, end0)
        if start is None or text[end0:end0 + 1] != ".":
            pos = end0
            continue
        m1 = _B64_RUN.match(text, end0 + 1)
        if m1 is None or m1.end() - (end0 + 1) < 5 or text[m1.end():m1.end() + 1] != ".":
            pos = end0 + 1
            continue
        m2 = _B64_RUN.match(text, m1.end() + 1)
        end = m2.end() if m2 is not None else m1.end() + 1
        spans.append((start, end))
        pos = end


def _mask_jwts(text: str) -> str:
    if "eyJ" not in text:
        return text
    spans = _jwt_spans(text, boundary=True)
    if not spans:
        return text
    out, last = [], 0
    for start, end in spans:
        out.append(text[last:start])
        out.append(MASK)
        last = end
    out.append(text[last:])
    return "".join(out)


def _secret_value(raw_value: str) -> bool:
    """값 자체가 비밀처럼 보이는가(JWT·긴 무작위 문자열). 단순·결정적 규칙."""
    v = unquote(raw_value)
    if "eyJ" in v and _jwt_spans(v, boundary=False):
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


# ------------------------------------------------------------------ 주소


def _public_value(raw_value: str, depth: int) -> str:
    """비밀이 아닌 값 — 값이 절대 주소면 그 주소도 가린다(바뀐 경우에만 다시 인코딩)."""
    decoded = unquote(raw_value)
    if "://" not in decoded:
        return raw_value
    if depth >= MAX_URL_DEPTH:
        return MASK  # 너무 깊은 중첩 주소 — 더 풀지 않고 값 전체를 가린다
    inner = _public_url(decoded, depth + 1)
    if inner == decoded:
        return raw_value
    return quote(inner, safe="/:[]")


def _public_param(part: str, depth: int) -> str:
    if "=" in part:
        key, value = part.split("=", 1)
        if value and (_secret_key(key) or _secret_value(value)):
            value = MASK
        else:
            value = _public_value(value, depth)
        return f"{key}={value}"
    if part and _secret_value(part):
        return MASK
    return part


def _public_query(query: str, depth: int) -> str:
    """`&`·`;` 로 나눈 각 파라미터의 값을 가린다. 구분자·순서는 그대로(짝수 칸 = 파라미터, 홀수 칸 = 구분자)."""
    pieces = _PARAM_SEP.split(query)
    for i in range(0, len(pieces), 2):
        pieces[i] = _public_param(pieces[i], depth)
    return "".join(pieces)


def _public_fragment(fragment: str, depth: int) -> str:
    if "=" in fragment:
        if "?" in fragment:  # SPA 경로 `#/route?code=…`
            route, query = fragment.split("?", 1)
            return route + "?" + _public_query(query, depth)
        return _public_query(fragment, depth)
    return MASK if fragment and _secret_value(fragment) else fragment


def _public_url(url: str, depth: int) -> str:
    if len(url) > MAX_URL_CHARS:
        return URL_MASK
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
        out += "?" + _public_query(query, depth)
    if has_frag:
        out += "#" + _public_fragment(fragment, depth)
    return out


def public_url(url: str) -> str:
    """에이전트에게 보여 줄 주소. 비밀 계열 쿼리/조각 값을 `[REDACTED]` 로 바꾸고 사용자정보를 뺀다.

    스킴과 무관(about:·data:·blob:·상대 주소 포함). 예외를 내지 않는다(깊은 중첩·긴 입력은 표지).
    """
    if not isinstance(url, str) or not url:
        return url
    return _public_url(url, 0)


# ------------------------------------------------------------------ 자유 문장·응답 구조


def _public_chunk(chunk: str) -> str:
    scheme_at = chunk.find("://")
    marks = [i for i in (chunk.find("?"), chunk.find("#")) if i >= 0]
    first_mark = min(marks) if marks else -1
    if scheme_at >= 0 and (first_mark < 0 or scheme_at < first_mark):
        # 첫 `://` 바로 앞 MAX_SCHEME_CHARS 글자 안에서만 스킴을 찾는다(덩어리 전체를 훑지 않는다).
        found = _SCHEME.search(chunk, max(0, scheme_at - MAX_SCHEME_CHARS), scheme_at + 3)
        if found is not None:
            start = found.start()
            return chunk[:start] + public_url(chunk[start:])
    if first_mark >= 0:
        # 스킴 없는 상대 주소 조각(`/guard?gateToken=…`, `?token=…`, `#access_token=…`) — 같은 규칙.
        return public_url(chunk)
    return chunk


def _secret_tail_value(chunk: str) -> Optional[str]:
    """덩어리의 마지막 쿼리/조각 파라미터가 비밀 키면 그 (원문) 값, 아니면 None."""
    marks = [i for i in (chunk.find("?"), chunk.find("#")) if i >= 0]
    if not marks:
        return None
    cut = max(chunk.rfind(c) for c in "?#&;")
    if cut < min(marks):
        return None
    key, eq, value = chunk[cut + 1:].partition("=")
    if not eq or not _secret_key(key):
        return None
    return value


def _public_run(run: str) -> str:
    """연장이 구분 기호에서 멈춘 뒤 이어지는 같은 주소의 나머지(`&b=1&sid=…`·`#…`·`)?…`)."""
    if run[0] in "&;":
        masked = public_url("?" + run)
        return masked if masked == URL_MASK else masked[1:]
    if run[0] == "#":
        return public_url(run)
    return _public_chunk(run)


def _word_end(text: str, pos: int) -> int:
    """상한에 걸린 연장을 그 자리 단어(덩어리) 끝까지 늘린다 — 주소 덩어리 한가운데서 끊기면 남은 꼬리가 덩어리
    시작이 아니라 다음 탐색에서 빠지고 원문이 남는다(1MiB 반복 주소 표본)."""
    end = _WORD_END.search(text, pos)
    return end.start() if end is not None else len(text)


def _extend_secret_value(text: str, pos: int, chunk: str, out: List[str]) -> int:
    """덩어리가 비밀 키 값으로 끝났는데 문장이 공백·따옴표로 이어지면 그 값의 경계를 안전 쪽으로 정한다(R2 B6).

    * 값이 비었고 바로 따옴표가 오면(`token="X"`) 짝 따옴표까지(없으면 줄 끝·상한까지) 가린다.
    * 값 뒤가 가로 공백이면(`token=prefix X`) 같은 단어 묶음으로 보고 다음 구분 기호(`&`·`;`·`#`·줄바꿈·
      닫는 괄호·따옴표·꺾쇠) 또는 MAX_VALUE_TAIL_CHARS 까지 가린다(공백은 남긴다).
    * 연장이 `&`·`;`·`#`·닫는 괄호에서 멈추면 이어지는 덩어리도 같은 주소의 나머지로 가리고, 그 끝이 다시 비밀
      키 값이면 반복한다. 매 단계가 글자를 소비하므로 전체 선형.
    반환: 다음 탐색 위치. 가린 조각은 out 에 붙인다.
    """
    n = len(text)
    while pos < n:
        value = _secret_tail_value(chunk)
        if value is None:
            return pos
        ch = text[pos]
        if ch in _QUOTES and value == "":
            limit = min(n, pos + 1 + MAX_VALUE_TAIL_CHARS)
            end = _QUOTE_STOP[ch].search(text, pos + 1, limit)
            stop = end.start() if end is not None else _word_end(text, limit)
            inner = MASK if stop > pos + 1 else ""
            if end is None or text[stop] != ch:  # 짝 없음 — 줄 끝·상한까지
                out.append(ch + inner)
                return stop
            out.append(ch + inner + ch)
            pos = stop + 1
        elif ch.isspace() and ch not in _LINE_BREAKS:
            limit = min(n, pos + MAX_VALUE_TAIL_CHARS)
            end = _TAIL_END.search(text, pos, limit)
            stop = end.start() if end is not None else _word_end(text, limit)
            seg = text[pos:stop]
            body = seg.strip()
            if body:
                out.append(seg[:len(seg) - len(seg.lstrip())] + MASK + seg[len(seg.rstrip()):])
            else:
                out.append(seg)
                if stop < n and text[stop] in _QUOTES:  # `token= "X"` — 빈 값 뒤 공백 다음 따옴표
                    pos = stop
                    continue
            pos = stop
        else:
            return pos
        if pos >= n or text[pos] not in "&;#)}":
            return pos
        run = _RUN.match(text, pos).group(0)  # type: ignore[union-attr]  # 빈 일치 가능 패턴
        out.append(_public_run(run))
        chunk = "?" + run if run[0] in "&;" else run
        pos += len(run)
    return pos


def _public_urls_in_text(text: str) -> str:
    out: List[str] = []
    pos = 0
    while True:
        m = _CANDIDATE.search(text, pos)
        if m is None:
            break
        out.append(text[pos:m.start()])
        chunk = m.group(0)
        out.append(_public_chunk(chunk))
        pos = _extend_secret_value(text, m.end(), chunk, out)
    out.append(text[pos:])
    return "".join(out)


def _public_text(text: str) -> str:
    if "://" in text or "?" in text or "#" in text:
        text = _public_urls_in_text(text)
    return _mask_jwts(text)


@functools.lru_cache(maxsize=8)
def _public_text_long(text: str) -> str:
    return _public_text(text)


def public_text(text: str) -> str:
    """자유 문장(오류 문구·안내·로그 줄·robots 규칙) 안의 주소에 public_url 을 적용하고 JWT 를 가린다.

    절대 주소뿐 아니라 `://` 없는 상대 주소 조각(`/p?token=…`, `?token=…`, `#token=…`)도 같은 규칙. 선형 시간.
    긴 문자열(_CACHE_MIN_CHARS 이상)은 최근 8개 결과를 기억한다 — 크기 상한(cap_result_size)이 같은 결과를 여러 번
    직렬화하며 같은 긴 제목을 다시 가리지 않게.
    """
    if not isinstance(text, str) or not text:
        return text
    if len(text) >= _CACHE_MIN_CHARS:
        return _public_text_long(text)
    return _public_text(text)


def _is_url_key(key: Any) -> bool:
    """응답 구조에서 '주소 칸' 인 키 이름(url·urls·*_url·*_urls — current_url·frame_url·robots_url …)."""
    return isinstance(key, str) and (key in ("url", "urls") or key.endswith("_url") or key.endswith("_urls"))


def _public_url_field_uncached(value: str) -> str:
    return _mask_jwts(public_url(value))


@functools.lru_cache(maxsize=8)
def _public_url_field_long(value: str) -> str:
    return _public_url_field_uncached(value)


def _public_url_field(value: str) -> str:
    if len(value) >= _CACHE_MIN_CHARS:
        return _public_url_field_long(value)
    return _public_url_field_uncached(value)


def _payload(obj: Any, path: Optional[Tuple[Any, ...]], depth: int,
             paths: FrozenSet[Tuple[str, ...]], url_field: bool) -> Any:
    if isinstance(obj, str):
        return _public_url_field(obj) if url_field else public_text(obj)
    if not isinstance(obj, (dict, list, tuple)):
        return obj
    if depth >= MAX_PAYLOAD_DEPTH:
        return DEPTH_MASK
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            sub = path + (k,) if path is not None and len(path) < _MAX_CONTENT_PATH else None
            if sub is not None and sub in paths:
                out[k] = v  # 페이지 내용(공유 — 소비자는 바로 직렬화한다, 모듈 설명·보고서 참조)
            else:
                out[k] = _payload(v, sub, depth + 1, paths, _is_url_key(k))
        return out
    items = [_payload(v, None, depth + 1, paths, url_field) for v in obj]
    return items if isinstance(obj, list) else tuple(items)


def public_payload(obj: Any, *, content_paths: FrozenSet[Tuple[str, ...]] = CONTENT_PATHS) -> Any:
    """응답 구조(dict/list)의 문자열을 가린 **새** 구조를 돌려준다(입력은 바꾸지 않는다).

    * 주소 칸(`_is_url_key`: url·current_url·frame_url·robots_url·tabs[].url …)의 문자열은 스킴과 무관하게
      public_url(+JWT 가림), 그 밖의 문자열은 public_text.
    * content_paths(최상위 응답 기준 경로 — extract 항목, 관찰 요소·요약)는 원문 그대로 둔다. 그 subtree 는
      입력과 같은 객체다(복사하지 않음 — 두 소비자 envelope_dict·call_server_tool 은 새로 만든 dict 를 넘기고
      결과를 바로 직렬화한다).
    * 깊이 MAX_PAYLOAD_DEPTH 를 넘는 자리는 DEPTH_MASK — 예외를 내지 않는다.
    """
    return _payload(obj, (), 0, content_paths, False)


def has_redaction_mark(url: Any) -> bool:
    """주소에 가림 표지(`[REDACTED…` 또는 퍼센트 인코딩)가 들어 있는가 — 이동 전에 거부한다."""
    return isinstance(url, str) and bool(_MARK.search(url))
