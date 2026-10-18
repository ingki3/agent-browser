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
      ③ 원래 키의 camelCase 낱말이 그렇다(`promoCode`·`pageKey` — 기존 낱말 분해 규칙).
      짧은 낱말을 부분 일치로 보지 않아 `keyword`·`monkey`·`codec`·`zipcode`·`author` 는 남는다.
      ⓪ (WS-55, ①~③ 보다 먼저) 정규화한 키에서 `_`·`-` 를 뺀 정규형이 **비밀 아닌 복합어** 허용 목록
      (`_NOT_SECRET_KEYS` — sortkey·countrycode·productcode·statuscode …)과 정확히 같으면 비밀 키가 아니다
      (`sortKey`·`sort_key`·`SORTKEY` 같은 판정). 같은 정규형이 `_COMPOUNDS`(promocode·couponcode …)면 비밀 키.
      허용 목록 키도 값 판정 (b) 와 레지스트리 가림은 그대로 받는다.
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

**자유 문장**(R3 — 설계 전환, 사용자 결정 2026-10-16 A). public_text 는 세 계층이다:

1. **도구가 본 주소의 비밀 값은 어디서든 가린다** — `security.secret_registry`. 이동 대상·리다이렉트 체인·main-frame
   이동·탭 주소·같은 응답의 주소 칸에서 이 모듈이 `[REDACTED]` 로 바꾸는 바로 그 값(`secret_values` — 같은 판정
   함수)과 인코딩 변형을 정확 일치로 가린다. 값이 문장에서 공백·따옴표·이스케이프로 어떻게 둘러싸여 있든 같다.
2. **출처 모를 문장은 URL 문법 경계만** — 공백·따옴표·꺾쇠로 나뉜 주소 덩어리에 public_url 을 적용한다. 공백은
   주소의 끝이다(R2 의 공백 연장·1024자 상한은 없앴다 — 문장만 보고는 `Request /a?token=abc HTTP 401` 과
   `/a?token=prefix SECRET` 을 가를 수 없다, 설계 리뷰 §1). 단 비밀 키 값이 비었거나 `\\` 뿐인데 바로 따옴표가
   오면(`token="X"`·`token=\\"X\\"`·JSON 문자열 안 주소) 이스케이프되지 않은 짝 따옴표나 줄 끝까지 상한 없이
   가린다(이스케이프 열림 `\\"` 은 `\\"` 로 닫히고, 그 전에 맨 따옴표가 오면 거기서 끝 — 바깥 문자열의 끝). 인용 뒤
   `&`·`;`·`#` 로 이어지는 같은 주소의 나머지도 같은 규칙.
3. JWT 모양 덩어리.

**한계(문서화, R4 §1.6)**: 도구가 어떤 주소에서도 본 적 없는 비밀이 페이지 제목 같은 출처 모를 문장에만 공백 뒤로
이어지면(`/a?token=prefix X`) X 는 '페이지가 쓴 내용' 으로 남는다(본문 elements 와 같은 취급). 4~7자 등록 값은 앞뒤가
영숫자가 아닐 때만 가리고 주소 칸에는 쓰지 않는다. 레지스트리 상한(값 256개·글자 16Mi)을 넘어 잊힌 옛 값은 URL 문법
계층만 적용된다. 표현 범위는 `secret_registry.representations` (대소문자 섞인 16진·base64·NFC/NFD·말줄임은 범위 밖).
호스트 라벨은 라벨 전체가 등록 값일 때만 가린다(`xx<V>yy.도메인` 은 남는다 — 정상 도메인 보호, 2026-10-17 결정).
페이지 내용 subtree(아래 '경계')는 범위 밖(원문 보존 계약, 2026-10-17 결정).

**주소 칸**(url·*_url): public_url → 레지스트리(≥ 8 값의 R1·R2 표현·긴 원문 조각, 호스트는 라벨 단위 casefold) → JWT
(R4 P3). 제어 필드(action·error_code·*tab_id·approval_id·레시피 id·status … — `control_value`)는 값이 그 키의 실제
열거 어휘(`CONTROL_VOCABULARY`, 코드 상수에서 도출) 또는 도구 식별자 형식이면 그대로 둔다(R4 P2 · R5 B3·B4 — 낱말 모양
일반 면제는 없다, 자유 문장 칸은 정제).

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
from typing import Any, Dict, FrozenSet, List, Optional, Tuple
from urllib.parse import quote, unquote, unquote_plus, urlsplit

from contracts import ActionType, ErrorCode
from security import secret_registry
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
#: 줄바꿈 글자(str.splitlines 기준) — 짝 없는 따옴표 값은 줄을 넘지 않는다.
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
    # WS-55: 개인 1회용일 수 있는 할인 코드 — 소문자 붙여쓰기도 camelCase(`promoCode`)와 같은 판정(가림).
    "promocode", "couponcode",
    # WS-55 R1 B3: 커서·페이지 토큰(다음 페이지를 여는 불투명 값 — 세션·계정에 묶이기도 한다). camelCase(`pageKey`)는 ③,
    # `page_key` 는 ② 로 이미 가렸고 소문자·대문자 붙여쓰기(`pagekey`·`PAGEKEY`)만 공개되던 철자 차이를 정규형으로 없앤다.
    # `*token` 꼴(pagetoken·continuationtoken)은 ① 이 낱말 사이 구분자 없는 철자를 이미 가리고, 글자마다 `_`·`-` 를 끼운
    # 철자(`p_a_g_e_t_o_k_e_n`)까지 같은 판정이 되게 정규형 정확 일치로도 둔다(① 을 정규형으로 넓히면 `auto_kenya` 같은
    # 키가 `token` 을 품게 되어 과잉 가림 — 정확 일치만).
    "pagekey", "nextpagekey", "cursorkey", "nextkey", "pagingkey", "continuationkey", "resumekey",
    "startkey", "exclusivestartkey", "lastevaluatedkey",
    "pagetoken", "nextpagetoken", "continuationtoken", "cursortoken", "nexttoken", "pagingtoken",
})
#: 비밀이 아닌 붙여 쓴 키 이름(WS-55 — WS-51 NB-2 과잉 가림). 정규형(`_secret_key`: 퍼센트 디코딩·NFKC·casefold 뒤 `_`·`-` 제거)의
#: **정확 일치**만 — 낱말 휴리스틱(②③)보다 먼저 본다. 정렬·국가/지역·우편·상태/오류·언어·통화·상품 분류 같은
#: 공개 열거 값의 이름이다. 커서일 수 있는 `pagekey`, 개인 코드일 수 있는 `promocode`·`couponcode`, OAuth `code`,
#: `token_type` 은 넣지 않는다. 키가 허용 목록이어도 값 판정(`_secret_value` — JWT·고엔트로피)과 레지스트리
#: 등록 값 가림은 그대로다(`_public_param`·`_registered_in_url`). 근거 표: .hermes/state/ws55/report.md.
_NOT_SECRET_KEYS = frozenset({
    "sortkey", "sortorder", "orderby",
    "countrycode", "zipcode", "postcode", "postalcode", "regioncode", "areacode", "citycode", "statecode",
    "airportcode", "stationcode",
    "statuscode", "errorcode", "keycode",
    "languagecode", "langcode", "currencycode",
    "productcode", "itemcode", "categorycode", "colorcode", "stylecode", "sizecode",
})
_KEY_JOINERS = re.compile(r"[_\-]")

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
#: 인용 값 안에서 다음으로 볼 글자: 같은 따옴표·백슬래시·줄바꿈(소비형 — 지난 글자를 다시 훑지 않는다).
_QUOTE_STOP = {q: re.compile("[" + re.escape(q) + "\\\\" + _LINE_BREAKS + "]") for q in _QUOTES}
#: 인용 값 뒤 구분 기호에서 이어지는 같은 주소의 나머지(공백·따옴표·꺾쇠 전까지).
_RUN = re.compile(r"[^\s\"'<>]*")
_BACKSLASHES = re.compile(r"\\+")

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
    compact = _KEY_JOINERS.sub("", low)  # WS-55: sortKey·sort_key·sort-key·SORTKEY 는 같은 키
    if compact in _NOT_SECRET_KEYS:
        return False
    if compact in _COMPOUNDS:
        return True
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


#: 가린 원 값을 모으는 자리(secret_values) — None 이면 모으지 않는다.
_Sink = Optional[List[str]]


def _public_value(raw_value: str, depth: int, sink: _Sink = None) -> str:
    """비밀이 아닌 값 — 값이 절대 주소면 그 주소도 가린다(바뀐 경우에만 다시 인코딩)."""
    decoded = unquote(raw_value)
    if "://" not in decoded:
        return raw_value
    if depth >= MAX_URL_DEPTH:
        if sink is not None:
            sink.append(raw_value)
        return MASK  # 너무 깊은 중첩 주소 — 더 풀지 않고 값 전체를 가린다
    inner = _public_url(decoded, depth + 1, sink)
    if inner == decoded:
        return raw_value
    return quote(inner, safe="/:[]")


def _public_param(part: str, depth: int, sink: _Sink = None) -> str:
    if "=" in part:
        key, value = part.split("=", 1)
        if value and (_secret_key(key) or _secret_value(value)):
            if sink is not None:
                sink.append(value)
            value = MASK
        else:
            value = _public_value(value, depth, sink)
        return f"{key}={value}"
    if part and _secret_value(part):
        if sink is not None:
            sink.append(part)
        return MASK
    return part


def _public_query(query: str, depth: int, sink: _Sink = None) -> str:
    """`&`·`;` 로 나눈 각 파라미터의 값을 가린다. 구분자·순서는 그대로(짝수 칸 = 파라미터, 홀수 칸 = 구분자)."""
    pieces = _PARAM_SEP.split(query)
    for i in range(0, len(pieces), 2):
        pieces[i] = _public_param(pieces[i], depth, sink)
    return "".join(pieces)


def _public_fragment(fragment: str, depth: int, sink: _Sink = None) -> str:
    if "=" in fragment:
        if "?" in fragment:  # SPA 경로 `#/route?code=…`
            route, query = fragment.split("?", 1)
            return route + "?" + _public_query(query, depth, sink)
        return _public_query(fragment, depth, sink)
    if fragment and _secret_value(fragment):
        if sink is not None:
            sink.append(fragment)
        return MASK
    return fragment


def _public_matrix(prefix: str, has_netloc: bool, depth: int, sink: _Sink = None) -> str:
    """경로 matrix 매개변수(`/p;jsessionid=…;v=2/q;sortKey=…`, WS-55 R1 B2) — 세그먼트마다 첫 `;` 뒤 조각을 쿼리와
    같은 `_public_param` 으로 판정·등록한다(같은 함수라 secret_values·주소 칸·자유 문장·로그가 갈라지지 않는다).
    세그먼트 이름(첫 `;` 앞)·호스트·구분자는 그대로. 호스트가 있으면 그 뒤 첫 `/` 부터가 경로다."""
    start = 0
    if has_netloc:
        auth = prefix.find("//")
        start = prefix.find("/", auth + 2) if auth >= 0 else -1
        if start < 0:
            return prefix  # 경로 없음(`https://h;x=1` — 호스트 쪽은 판정하지 않는다)
    head, path = prefix[:start], prefix[start:]
    segments = path.split("/")
    for i, seg in enumerate(segments):
        if ";" not in seg:
            continue
        pieces = seg.split(";")
        segments[i] = ";".join([pieces[0]] + [_public_param(p, depth, sink) for p in pieces[1:]])
    return head + "/".join(segments)


def _public_url(url: str, depth: int, sink: _Sink = None) -> str:
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
        if sink is not None:
            # 사용자정보는 통째로 뺀다 — 비밀번호(없으면 사용자 자리에 쓴 토큰)를 등록 대상으로.
            user, _, password = netloc.rsplit("@", 1)[0].partition(":")
            sink.append(password or user)
        prefix = prefix.replace("//" + netloc, "//" + netloc.rsplit("@", 1)[1], 1)
    if ";" in prefix:
        prefix = _public_matrix(prefix, bool(netloc), depth, sink)
    out = prefix
    if has_query:
        out += "?" + _public_query(query, depth, sink)
    if has_frag:
        out += "#" + _public_fragment(fragment, depth, sink)
    return out


def public_url(url: str) -> str:
    """에이전트에게 보여 줄 주소. 비밀 계열 쿼리/조각 값을 `[REDACTED]` 로 바꾸고 사용자정보를 뺀다.

    스킴과 무관(about:·data:·blob:·상대 주소 포함). 예외를 내지 않는다(깊은 중첩·긴 입력은 표지).
    """
    if not isinstance(url, str) or not url:
        return url
    return _public_url(url, 0)


def secret_values(url: str) -> List[str]:
    """public_url(url) 이 `[REDACTED]` 로 바꾸는(또는 사용자정보로 빼는) 원 값들 — 비밀 값 레지스트리의 등록
    판정이다(같은 함수라 주소 칸 판정과 갈라지지 않는다). 중첩 주소(`next=`)의 값은 디코드된 원 값."""
    if not isinstance(url, str) or not url:
        return []
    sink: List[str] = []
    _public_url(url, 0, sink)
    return [v for v in sink if v]


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
    """인용 값 뒤 구분 기호에서 이어지는 같은 주소의 나머지(`&b=1&sid=…`·`;…`·`#…`)."""
    if run[0] in "&;":
        masked = public_url("?" + run)
        return masked if masked == URL_MASK else masked[1:]
    return public_url(run)


def _quoted_end(text: str, pos: int, depth: int) -> Tuple[int, int]:
    """pos 의 따옴표(바로 앞 백슬래시 depth 개로 열림)로 시작한 인용 값의 (안쪽 끝, 소비 끝).

    임의 깊이 이스케이프(R4 P1 — repr·JSON 을 몇 겹 거친 로그 문장): 같은 따옴표 앞 백슬래시 덩어리의 길이를 연 쪽과
    비교한다. 이스케이프는 겹마다 백슬래시를 늘리므로(0 → 1·2 → 3·5 …) 값 **안** 의 따옴표는 연 쪽보다 길다.

    * 같은 길이 → 닫는 따옴표(소비).
    * 더 긴 덩어리 → 한 겹 더 이스케이프된 따옴표 — 값 내용.
    * 더 짧은 덩어리(맨 따옴표 포함) → 바깥 문자열의 끝 — 거기서 멈추고 소비하지 않는다(맨 따옴표로 연 값에는
      더 짧은 것이 없다).
    * 줄바꿈·문장 끝에서 멈춘다(상한 없음). 정규식은 매번 직전 위치부터 다음 멈춤 글자만 찾는다 — 선형.
    """
    q = text[pos]
    stop = _QUOTE_STOP[q]
    i = pos + 1
    n = len(text)
    while True:
        m = stop.search(text, i)
        if m is None:
            return n, n
        j = m.start()
        c = text[j]
        if c == "\\":
            r = _BACKSLASHES.match(text, j).end()  # type: ignore[union-attr]  # j 가 백슬래시
            if r < n and text[r] == q:
                run = r - j
                if run == depth:
                    return j, r + 1
                if run < depth:
                    return j, j
            i = r + 1 if r < n and text[r] == q else r
            continue
        if c == q:
            return (j, j + 1) if depth == 0 else (j, j)
        return j, j  # 줄바꿈


def _quoted_secret(chunk: str, text: str, pos: int) -> Optional[str]:
    """덩어리가 비밀 키의 빈 값(또는 `\\` 뿐인 값)으로 끝나고 바로 따옴표가 오면 그 값(백슬래시들), 아니면 None."""
    if pos >= len(text) or text[pos] not in _QUOTES:
        return None
    value = _secret_tail_value(chunk)
    if value is None or value.strip("\\"):
        return None
    return value


def _public_urls_in_text(text: str) -> str:
    out: List[str] = []
    pos = 0
    n = len(text)
    while True:
        m = _CANDIDATE.search(text, pos)
        if m is None:
            break
        out.append(text[pos:m.start()])
        piece = m.group(0)  # 입력에 실제로 있는 조각
        mask_piece = _public_chunk
        pos = m.end()
        # 비밀 키의 빈 값 + 따옴표: 인용 값(짝 따옴표·줄 끝까지) — 매 단계가 글자를 소비하므로 전체 선형.
        while True:
            # 인용 뒤 이어지는 나머지(`&sid=`)는 쿼리로 판정한다(앞에 `?` 를 붙여 본다).
            probe = "?" + piece if mask_piece is _public_run and piece[0] in "&;" else piece
            slashes = _quoted_secret(probe, text, pos)
            if slashes is None:
                out.append(mask_piece(piece))
                break
            head = piece[:len(piece) - len(slashes)]
            out.append(mask_piece(head) + slashes + text[pos])
            inner_end, end = _quoted_end(text, pos, len(slashes))
            out.append((MASK if inner_end > pos + 1 else "") + text[inner_end:end])
            pos = end
            if pos >= n or text[pos] not in "&;#":
                break
            piece = _RUN.match(text, pos).group(0)  # type: ignore[union-attr]  # 빈 일치 가능 패턴
            mask_piece = _public_run
            pos += len(piece)
    out.append(text[pos:])
    return "".join(out)


def _public_text(text: str) -> str:
    text = secret_registry.mask_registered(text)
    if "://" in text or "?" in text or "#" in text:
        text = _public_urls_in_text(text)
    return _mask_jwts(text)


@functools.lru_cache(maxsize=8)
def _public_text_long(text: str, registry_version: int) -> str:
    return _public_text(text)


def public_text(text: str) -> str:
    """자유 문장(오류 문구·안내·로그 줄·robots 규칙·제목)을 가린다 — 도구가 본 비밀 값(레지스트리) → 주소 덩어리의
    public_url(+인용 값) → JWT (모듈 설명 '자유 문장').

    절대 주소뿐 아니라 `://` 없는 상대 주소 조각(`/p?token=…`, `?token=…`, `#token=…`)도 같은 규칙. 선형 시간.
    긴 문자열(_CACHE_MIN_CHARS 이상)은 최근 8개 결과를 레지스트리 버전과 함께 기억한다 — 크기 상한(cap_result_size)이
    같은 결과를 여러 번 직렬화하며 같은 긴 제목을 다시 가리지 않게(새 값이 등록되면 다시 계산).
    """
    if not isinstance(text, str) or not text:
        return text
    if len(text) >= _CACHE_MIN_CHARS:
        return _public_text_long(text, secret_registry.version())
    return _public_text(text)


def _is_url_key(key: Any) -> bool:
    """응답 구조에서 '주소 칸' 인 키 이름(url·urls·*_url·*_urls — current_url·frame_url·robots_url …)."""
    return isinstance(key, str) and (key in ("url", "urls") or key.endswith("_url") or key.endswith("_urls"))


def _registered_in_url(url: str) -> str:
    """주소 칸(public_url 뒤)의 등록 값(R4 P3 — R3-B1): 호스트 뒤(경로·쿼리·조각)는 ≥ 8 값의 R1·R2 표현과 긴 원문
    조각을, 호스트는 라벨 단위로 등록 원문과 casefold 비교해 같은 라벨만 가린다(Chromium 은 호스트를 소문자로
    바꾼다). 4~7자 값은 쓰지 않는다 — `?code=2026` 을 본 뒤 `/archive/2026/` 같은 정상 경로가 가려져 이동이
    거부되지 않게(§1.1)."""
    registry = secret_registry.REGISTRY
    if not len(registry) or url == URL_MASK:
        return url
    scheme_end = url.find("://")
    if scheme_end < 0 or not _SCHEME.fullmatch(url, 0, scheme_end + 3):
        return registry.mask_url_part(url)  # about:·data:·상대 주소 — 호스트가 없다
    start = scheme_end + 3
    stop = len(url)
    for mark in "/?#":
        k = url.find(mark, start)
        if 0 <= k < stop:
            stop = k
    authority = url[start:stop]
    host, port = authority, ""
    if not authority.startswith("["):  # IPv6 리터럴은 라벨이 없다
        head, colon, tail = authority.rpartition(":")
        if colon and tail.isdigit():
            host, port = head, colon + tail
        labels = host.split(".")
        if any(registry.host_label_registered(label) for label in labels):
            host = ".".join(MASK if registry.host_label_registered(label) else label for label in labels)
    return url[:start] + host + port + registry.mask_url_part(url[stop:])


def _public_url_field_uncached(value: str) -> str:
    return _mask_jwts(_registered_in_url(public_url(value)))


@functools.lru_cache(maxsize=8)
def _public_url_field_long(value: str, registry_version: int) -> str:
    """긴 주소 칸 결과 캐시 — 키에 레지스트리 버전(§2 P8: 등록 뒤 옛 결과를 돌려주지 않는다)."""
    return _public_url_field_uncached(value)


def _public_url_field(value: str) -> str:
    if len(value) >= _CACHE_MIN_CHARS:
        return _public_url_field_long(value, secret_registry.version())
    return _public_url_field_uncached(value)


# ------------------------------------------------------------------ 제어 필드(§1.5, R4 P2 — R3-B7)

#: actions.healing.HealingStrategy 값과 heal() 이 전략 뒤에 붙이는 결과(R9 R8-B1 — `attempts[-1] = f"{strategy.value}(…)"`).
_HEAL_STRATEGIES: Tuple[str, ...] = ("role_name", "testid", "text_similarity", "css_path", "css_piercing")
_HEAL_OUTCOMES: Tuple[str, ...] = ("ambiguous", "identity_changed")
_DIALOG_TYPES: FrozenSet[str] = frozenset({"alert", "beforeunload", "confirm", "prompt", "unknown"})
#: actions.verification.verify_post_condition 의 **도구 고정** 신호(페이지·주소·입력값·숫자가 들지 않는 것만 — R9 R8-B2).
#: `url_changed: A -> B`·`dom_delta: 3 -> 5`·`focus_moved: …`·`value_applied: '…'` 같은 `종류: 상세` 는 넣지 않는다(정제).
_VERIFY_SIGNAL_CODES: FrozenSet[str] = frozenset(
    {"text_changed", "element_attr_changed", "element_value_changed: <secret>"}
    | {f"checked_applied: {b}" for b in (True, False)}
    | {f"element_checked_changed: {a} -> {b}" for a in (True, False, None) for b in (True, False, None) if a != b})

_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
#: 제어 키 → 그 키에 실리는 **도구 열거 어휘**(R5 B3 — 낱말 모양 일반 면제를 폐지하고 코드 상수에서 도출한 실제 값만).
#: 출처(생산 코드)는 각 줄 주석, 전수 조사는 tests/security/test_ws51_r5_census.py(AST — src/ 전체의 리터럴·상수·str Enum
#: 생산자가 이 표 안에 있는지, 동적 생산자는 분류됐는지) 가 고정한다. 자유 문장 칸(handoff 요청 사유·hitl 사유·challenge
#: 사유·sticky_reason 등)은 이 어휘에 없는 값이면 정제한다.
_APPROVAL_STATUS = ("pending", "approved", "used", "denied", "revoked", "expired", "unknown")  # handoff.Approval·_FINISHED
_REPLAY_STOP = ("page_changed", "not_ready", "target_not_found", "target_ambiguous", "approval_required",
                "action_failed", "expect_mismatch")  # recipes.replay._Stop · recipes.keys found.reason
_PDF_REASONS = ("egress_unavailable", "user_agent_unavailable", "egress_blocked", "redirect_origin", "redirect_limit",
                "content_encoding", "file_size_limit", "invalid_length", "not_pdf", "fetch_timeout", "fetch_failed",
                "encrypted", "page_range", "chars_limit", "pages_limit", "resource_limit", "parse_error",
                "parse_timeout", "no_pages", "no_text_layer")  # actions.pdf_document FetchResult·doc.update
_YOUTUBE_REASONS = ("egress_unavailable", "egress_blocked", "redirect_refused", "request_failed", "response_too_large",
                    "timeout", "invalid_response", "quota_exceeded", "key_invalid", "rate_limited", "bad_request",
                    "upstream_error", "http_error",
                    "invalid_arguments", "key_malformed", "internal_error")  # interface.youtube_search _REASONS(R7)
CONTROL_VOCABULARY: Dict[str, FrozenSet[str]] = {
    "status": frozenset(_APPROVAL_STATUS + ("opening", "ready")),  # + server_handoff LoginJob.status
    "state": frozenset({"headless", "headed", "switching", "failed",  # on_demand.WindowState·server_window
                        "logged_in", "login_form", "captcha",  # browser.login_flow.LoginState
                        "succeeded"}),  # browser.headless_retry states
    "holder": frozenset({"agent", "human"}),  # handoff.HOLDER_AGENT·HOLDER_HUMAN
    "reason": frozenset(_REPLAY_STOP + _PDF_REASONS + _YOUTUBE_REASONS + (
        "headless_ua_blocked",  # server_signals identity
        "cookies_present", "already_failed", "robots_not_confirmed", "vendor_challenge", "disabled", "headed",  # headless_retry.SKIP_NOTES
        "browser_recovered", "request_expired", "window_closed",  # server_window notice
        "control_request", "recover", "taken",  # server_window _switch_window 전환 사유(R7 census)
        "capture_busy", "target_changed",  # server_display·server_hitl
        "not_in_allowlist", "private_network", "unsupported_scheme", "malformed_url",  # security.egress.BlockReason
        "fresh", "epoch_mismatch", "node_detached", "role_changed", "name_changed",  # actions.verification.StalenessReason
        "sabotage",  # harness.tier2_som(사보타주 표지)
    )),
    "reason_code": frozenset({"unknown", "expired", "used", "denied", "revoked", "not_approved", "target_changed",
                              "mismatch"}),  # handoff.check_approval 사유 코드
    "kind": frozenset({"none", "captcha", "blocked",  # browser.challenge.ChallengeKind
                       "slot", "ui", "identity",  # recipes.keys 대상 종류
                       "pdf", "positive", "negative"}),  # actions.pdf_document·browser.site_session
    "mode": frozenset({"interactive", "unattended",  # contracts.ExecutionMode
                       "on-demand",  # interface.on_demand.ON_DEMAND
                       "offline", "live", "after", "json"}),  # harness 보고 모드
    "browser_mode": frozenset({"headless", "human", "user-chrome", "on-demand"}),  # interface.cli --browser choices
    "outcome": frozenset({"succeeded", "failed", "outcome_unknown",  # server_hitl._attach_approval_outcome
                          "unresolved", "resolved", "force"}),  # agent.loop.HandoffOutcome
    "op": frozenset({"save", "run", "list", "delete",  # browser_recipe op
                     "take", "release", "approve", "deny", "show_code", "describe", "login", "login_finish",
                     "cookie_sites", "status"}),  # handoff._OPS · cli status
    "risk": frozenset({"low", "medium", "high"}),  # security.hitl.RiskLevel
    "presented_as": frozenset({"chrome"}),  # server_signals identity
    "sticky_reason": frozenset(),  # 자유 문장(on_demand — 도메인·사유 문장) — 늘 정제
    "changed": frozenset({"none_pending", "released", "taken", "timeout"}),  # server_handoff._control_wait
    # actions.dispatcher _NavWatch.aborted → data.nav_aborted (R6 — f"status_{status}" 는 _NO_DOCUMENT_STATUS 204·205)
    "nav_aborted": frozenset({"download", "request_failed", "status_204", "status_205"}),
    # R7 — 리터럴 제어값 census(tests/security/test_ws51_r7_census.py, 소비자 표 독립 AST)로 드러난 키.
    "nav_frame": frozenset({"current_frame"}),  # actions.dispatcher _NavWatch (R6-B1)
    "resolved_from": frozenset({"current_frame", "root"}),  # actions.dispatcher switch_frame · server_schema 안내
    "coordinate_target": frozenset({"interactive", "non_interactive", "rejected_by_dispatcher",
                                    "unresolved"}),  # server_hitl 좌표 대상 근거
    "handled": frozenset({"accepted", "dismissed"}),  # actions.dispatcher data.dialogs[].handled
    "blocked_by": frozenset({"approval_code_displayed"}),  # server_display 캡처 차단
    "captured": frozenset({"root_page"}),  # actions.dispatcher 프레임 안 screenshot
    "code": frozenset({"egress_blocked",  # security.egress EgressDecision.to_agent
                       "resolve_failed", "connect_failed"}),  # security.egress UPSTREAM_* (UpstreamFailure.to_agent)
    "category": frozenset({"loopback", "private", "metadata", "unspecified", "reserved", "not_in_allowlist",
                           "scheme", "malformed"}),  # security.egress CAT_* (EgressDecision.category)
    "scope": frozenset({"all_bots", "named_ai", "all_bots_and_named_ai", "none"}),  # robots_signal RobotsScope
    "storage": frozenset({"profile", "memory"}),  # recipes.service list
    "source": frozenset({"youtube_data_api_v3",  # interface.youtube_search SOURCE
                         # security.hitl _basis(gate_basis.source): 판정 출처 + 문맥 신호 원천(TEXT·PATH·TOKEN_SOURCES)
                         "unresolved", "form_submit", "action",
                         "name", "text", "aria", "title", "child_title", "alt", "svg_title", "child_aria", "value",
                         "pseudo", "selector", "detail", "href", "formaction", "form_action",
                         "class", "id", "name_attr", "testid",
                         "keyring", "prompt", "ci_env",  # browser.session_store KeyResolution.source(응답 출구 아님)
                         "chrome_import", "login_flow", "human_control"}),  # browser.login_ledger SOURCE_*(WS-59, 기록 파일)
    "how_to_wait": frozenset({"browser_control_wait"}),  # interface.handoff 조작권 요청 안내(도구 이름)
    # R8 — 속성 전달값 census(tests/security/test_ws51_r8_census.py)로 드러난 키.
    "vendor": frozenset({"generic", "hcaptcha", "cloudflare", "recaptcha", "naver", "akamai"}),  # browser.challenge
    # healing_attempts 는 R9 부터 목록 경로 어휘(CONTROL_LIST_VOCABULARY — 복합 코드는 낱말 모양이 아니다)
}
#: 목록 안 dict 의 키 경로로 정해진 제어 값: (목록 키, 항목 키) → 어휘(R8 — 다른 위치의 같은 키 이름은 면제하지 않는다).
CONTROL_ITEM_VOCABULARY: Dict[Tuple[str, str], FrozenSet[str]] = {
    # actions.dispatcher _on_dialog → data.dialogs[].type: Playwright Dialog.type + 읽기 실패 폴백 "unknown"
    ("dialogs", "type"): _DIALOG_TYPES,
}
#: 목록 키 바로 아래 문자열 원소의 제어 값: 목록 키 → 어휘(R9 R8-B1·B2 — `data.healing_attempts`·`data.signals`). 그 목록 안의
#: 다른 원소(`종류: 상세` 문장)·목록이 아닌 같은 키·다른 목록·안쪽 목록/dict 는 면제하지 않는다.
CONTROL_LIST_VOCABULARY: Dict[str, FrozenSet[str]] = {
    # actions.healing heal() → data.healing_attempts: HealingStrategy 값 + 복합 코드 f"{전략}(ambiguous|identity_changed)"
    # (R9 R8-B1 — tests/security/test_ws51_r9_census.py 가 heal() 의 실제 원소 틀에서 도출해 이 표와 같음을 고정)
    "healing_attempts": frozenset(
        set(_HEAL_STRATEGIES) | {f"{s}({o})" for s in _HEAL_STRATEGIES for o in _HEAL_OUTCOMES}),
    # verify_post_condition 고정 신호 + dispatcher "navigation_started" + _top_effect_signals f"top:{s}"
    # + _dialog_signals f"dialog_opened:{type}"(dialogs[].type 어휘)
    "signals": frozenset(_VERIFY_SIGNAL_CODES | {"navigation_started"} | {f"top:{c}" for c in _VERIFY_SIGNAL_CODES}
                         | {f"dialog_opened:{t}" for t in _DIALOG_TYPES}),
}
#: 어휘 외에 형식으로 정해진 제어 값(키 → 전체 일치 형식).
CONTROL_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "reason": re.compile(r"http_\d{3}"),  # actions.pdf_document f"http_{status_code}"
}
#: 값이 도구 열거 어휘인 제어 키(호환 이름 — 판정은 CONTROL_VOCABULARY).
CONTROL_ENUM_KEYS: FrozenSet[str] = frozenset(CONTROL_VOCABULARY)
#: 값이 도구가 찍은 시각(ISO 8601)인 제어 키.
CONTROL_TIME_KEYS: FrozenSet[str] = frozenset({
    "since", "expires_at", "started_at", "created_at", "detected_at", "reset_at",
})
#: 그 밖의 제어 키 → 허용 형식(키 이름은 전체 일치, `*_tab_id` 는 접미).
_CONTROL_ID_KEYS: Dict[str, str] = {
    "tab_id": "tab_id", "approval_id": "approval_id", "request_id": "request_id", "server_id": "server_id",
    "element_id": "element_id", "trigger_element_id": "element_id",
    "id": "recipe_id",  # recipes.service list·save·run 의 레시피 id(R5 B4)
    "params": "param_name",  # recipes.service recipe.params 의 자동 이름 textN·choiceN(R6)
}
_ACTION_VALUES: FrozenSet[str] = frozenset(a.value for a in ActionType)
_ERROR_VALUES: FrozenSet[str] = frozenset(e.value for e in ErrorCode)


def control_value(key: Any, value: str) -> bool:
    """키 이름이 제어 키이고 값이 그 키의 도구 어휘·식별자 형식이면 True — 정제하지 않는다(값이 원래 값과 같아야
    응답 계약·탭 전환·승인 대기가 동작한다). 형식이 다르면 False(정제 — 방어)."""
    if not isinstance(key, str):
        return False
    if key == "action":
        return value in _ACTION_VALUES or secret_registry.TOOL_ID_FORMATS["tool_name"].fullmatch(value) is not None
    if key == "error_code":
        return value in _ERROR_VALUES
    fmt = _CONTROL_ID_KEYS.get(key) or ("tab_id" if key.endswith("_tab_id") else None)
    if fmt is not None:
        return secret_registry.TOOL_ID_FORMATS[fmt].fullmatch(value) is not None
    vocabulary = CONTROL_VOCABULARY.get(key)
    if vocabulary is not None:
        if value in vocabulary:
            return True
        pattern = CONTROL_PATTERNS.get(key)
        return pattern is not None and pattern.fullmatch(value) is not None
    if key in CONTROL_TIME_KEYS:
        return _ISO_TIME.fullmatch(value) is not None
    return False


def control_list_value(container: Any, value: str) -> bool:
    """목록 `container` 바로 아래 문자열 원소이고 값이 그 목록의 어휘면 True(R9 — `healing_attempts[]`·`signals[]`)."""
    vocabulary = CONTROL_LIST_VOCABULARY.get(container) if isinstance(container, str) else None
    return vocabulary is not None and value in vocabulary


def control_item_value(container: Any, key: Any, value: str) -> bool:
    """목록 `container` 의 dict 항목 안 `key` 칸이고 값이 그 경로의 어휘면 True(R8 — `dialogs[].type`)."""
    vocabulary = CONTROL_ITEM_VOCABULARY.get((container, key)) if isinstance(container, str) \
        and isinstance(key, str) else None
    return vocabulary is not None and value in vocabulary


#: 도구가 만든 고정 키 이름 모양(소문자 snake) — 정제하지 않는다. 그 밖의 키(페이지 유래 이름 등)는 public_text.
_TOOL_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _payload_key(key: Any) -> Any:
    if isinstance(key, str) and not _TOOL_KEY.fullmatch(key):
        return public_text(key)
    return key


def _payload(obj: Any, path: Optional[Tuple[Any, ...]], depth: int,
             paths: FrozenSet[Tuple[str, ...]], url_field: bool, key: Any = None, item_of: Any = None) -> Any:
    # item_of — dict 이면 그 dict 가 원소인 목록의 키, 문자열이면 그 문자열을 담은 dict 가 원소인 목록의 키(R8),
    # 목록이면 그 목록이 dict 의 바로 아래 값일 때의 키(R9 — 목록 안 목록은 None).
    if isinstance(obj, str):
        if key is not None and (control_value(key, obj) or
                                (item_of is not None and control_item_value(item_of, key, obj))):
            return obj
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
                out[_payload_key(k)] = _payload(v, sub, depth + 1, paths, _is_url_key(k), k,
                                                item_of if isinstance(v, str) else
                                                (k if isinstance(v, (list, tuple)) else None))
        return out
    items = [v if isinstance(v, str) and control_list_value(item_of, v) else
             _payload(v, None, depth + 1, paths, url_field, key, key if isinstance(v, dict) else None) for v in obj]
    return items if isinstance(obj, list) else tuple(items)


def public_payload(obj: Any, *, content_paths: FrozenSet[Tuple[str, ...]] = CONTENT_PATHS) -> Any:
    """응답 구조(dict/list)의 문자열을 가린 **새** 구조를 돌려준다(입력은 바꾸지 않는다).

    * 주소 칸(`_is_url_key`: url·current_url·frame_url·robots_url·tabs[].url …)의 문자열은 스킴과 무관하게
      public_url(+JWT 가림), 그 밖의 문자열은 public_text.
    * content_paths(최상위 응답 기준 경로 — extract 항목, 관찰 요소·요약)는 원문 그대로 둔다. 그 subtree 는
      입력과 같은 객체다(복사하지 않음 — 두 소비자 envelope_dict·call_server_tool 은 새로 만든 dict 를 넘기고
      결과를 바로 직렬화한다).
    * 깊이 MAX_PAYLOAD_DEPTH 를 넘는 자리는 DEPTH_MASK — 예외를 내지 않는다.
    * 가리기 전에 같은 응답의 주소 칸 원문(내용 경로 밖)을 비밀 값 레지스트리에 등록한다(R3 등록 지점 a) — 제목이
      주소 칸보다 먼저 나와도 그 주소의 비밀 값은 제목에서 가려진다.
    """
    try:
        _register_url_fields(obj, (), 0, content_paths, False)
    except Exception:  # noqa: BLE001 - 등록 실패가 응답을 막지 않는다(URL 문법 계층은 그대로)
        pass
    return _payload(obj, (), 0, content_paths, False)


def _register_url_fields(obj: Any, path: Optional[Tuple[Any, ...]], depth: int,
                         paths: FrozenSet[Tuple[str, ...]], url_field: bool) -> None:
    if isinstance(obj, str):
        if url_field:
            secret_registry.register_url(obj)
        return
    if depth >= MAX_PAYLOAD_DEPTH:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = path + (k,) if path is not None and len(path) < _MAX_CONTENT_PATH else None
            if sub is None or sub not in paths:
                _register_url_fields(v, sub, depth + 1, paths, _is_url_key(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _register_url_fields(v, None, depth + 1, paths, url_field)


def has_redaction_mark(url: Any) -> bool:
    """주소에 가림 표지(`[REDACTED…` 또는 퍼센트 인코딩)가 들어 있는가 — 이동 전에 거부한다."""
    return isinstance(url, str) and bool(_MARK.search(url))
