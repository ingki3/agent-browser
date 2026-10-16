"""도구가 본 주소의 비밀 값 레지스트리 (WS-51 R3 D안 → R4 결함 수정).

출처를 모르는 자유 문장(페이지 제목·오류 문구·로그 줄)에서는 `Request /a?token=abc HTTP 401` 과
`/a?token=prefix SECRET` 이 같은 모양이라, 값이 어디서 끝나는지 문장만 보고는 정할 수 없다(R1→R2 경계 추측의
한계). 그 대신 **도구가 실제로 본 주소**에서 public_url 이 `[REDACTED]` 로 바꾸는 바로 그 원 값을 여기에 기록해
두고, 자유 문장·주소 칸에서 그 값의 **표현 집합**(`representations`)을 정확 일치로 가린다. CI 로그의 비밀 가리기
(`::add-mask::`)와 같은 방식이다. 위협 모델(닫힌 목록)은 `.hermes/plans/ws51-r4-plan.md` §1.

* 등록 판정은 public_url 과 **같은 함수**(`security.public_url.secret_values`)라 주소 칸과 갈라지지 않는다.
* 보호 대상(§1.1): 값(원문·unquote·unquote_plus 각각) 길이 ≥ 8, 또는 4~7 이고 순수 알파벳이 아님. 제외: 도구 어휘
  (ActionType·ErrorCode 값)와 도구가 만든 식별자 형식(`tab-N`·승인 id·요청 id·요소 id(`@eN`·`@rN`)·서버 id·레시피 id·
  도구 이름) — 이런
  값을 가리면 응답의 제어 필드·탭 전환이 깨진다(R3-B7).
* 표현 집합(§1.3)은 `representations(v)` 한 곳에서 만든다(검증자도 이 함수를 import 한다).
* 일치(§2 P4): ≥ 8 값은 표현을 길이 내림차순 `str.replace` 로 본문에 없는 사설 영역 센티널로 바꾸고, 연속 센티널을
  표지 하나로 합친다. 4~7 값은 본문 영숫자 덩어리를 한 번 토큰화해 집합으로 조회한다(값 안에 영숫자가 아닌 글자가
  있으면 `str.find` + 앞뒤 경계 확인). 값마다 정규식을 돌리지 않는다. 서로 다른 등록 값이 겹쳐 남는 일부 조각은
  §1.4 의 NB 다(전체 표현은 남지 않는다).
* 긴 값(원문 ≥ 256자)은 128자 정렬 블록 색인으로 **잘리거나 일부가 바뀐 꼴**에서도 원문 연속 256자 이상이 남지 않게
  가린다(본문 위치마다 사전 조회 — 값 수와 무관한 선형 시간).
* 상한(§2 P5): 값 256개 + 표현·블록 글자 합 16Mi 자, 넘으면 오래된 값부터 **값 단위로** 잊는다(LRU — 다시 본 값은
  뒤로). 살아 있는 값은 언제나 모든 표현을 갖는다(R5 B2 — 표현 일부만 버리지 않는다). 값 하나의 전 표현이 예산을
  넘으면 그 값만 혼자 남는다(메모리 상한 ≈ 그 값 하나의 표현 합). 긴 값도 원문 그대로 보관한다(지문·절단 없음).
* 프로세스 메모리 전용: 디스크·로그·응답에 쓰지 않는다. repr·str 은 개수만 보이고, pickle·copy 는 거부한다.
* 스레드 안전: 등록은 Lock 안에서 새 스냅샷을 만들고, 조회는 스냅샷을 한 번 읽는다(잠금 없음).
* 등록은 예외를 내지 않는다 — 등록 실패가 도구 동작을 막지 않는다.
"""

from __future__ import annotations

import functools
import html
import json
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, FrozenSet, Iterable, List, NamedTuple, Optional, Tuple
from urllib.parse import unquote, unquote_plus

from contracts import ActionType, ErrorCode
from security.masking import MASK

#: 레지스트리가 기억하는 값 수 상한(LRU — 표현 수가 아니라 등록 값 단위).
MAX_VALUES = 256
#: 기억하는 표현·블록 글자 합 상한(값 단위로 계산, 넘으면 오래된 값부터 잊는다).
MAX_CHARS = 16 * 1024 * 1024
#: 이 길이 이상은 부분 문자열 일치.
MIN_SUBSTR_CHARS = 8
#: 이 길이 미만은 등록하지 않는다(URL 문법 계층만).
MIN_VALUE_CHARS = 4
#: 긴 값 블록 색인의 블록 길이. 원문 연속 2×BLOCK_CHARS 이상이 든 조각은 정렬 블록을 하나 이상 품는다.
BLOCK_CHARS = 128
#: 이 길이 이상의 원문은 블록 색인(잘린·일부 바뀐 꼴 대비, §1.4 '원문 연속 256자').
LONG_VALUE_CHARS = 2 * BLOCK_CHARS
#: 가림 표지 — 이 안에 들어 있는 문자열(`REDACTED` 등)을 등록하면 표지가 다시 가려져 멱등이 깨진다.
_MARKERS = ("[REDACTED]", "[REDACTED-URL]", "[REDACTED-DEPTH]")

#: 도구 어휘(§1.1 제외) — 응답의 action·error_code 값.
TOOL_VOCABULARY: FrozenSet[str] = frozenset(a.value for a in ActionType) | frozenset(e.value for e in ErrorCode)
#: 도구가 만든 식별자 형식(§1.1 제외). 이름 → 형식(전체 일치).
TOOL_ID_FORMATS: Dict[str, "re.Pattern[str]"] = {
    "tab_id": re.compile(r"tab-\d+"),                       # browser.core: f"tab-{n}"
    "approval_id": re.compile(r"ap_[A-Za-z0-9_-]{1,64}"),    # handoff: f"ap_{token_urlsafe(12)}"
    "request_id": re.compile(r"rq_[A-Za-z0-9_-]{1,64}"),     # handoff: f"rq_{token_urlsafe(9)}"
    "element_id": re.compile(r"@[er]\d+"),                   # perception.engine: f"@e{idx}" · recipes.service: f"@r{seq}"
    "recipe_id": re.compile(r"r_[0-9a-f]{8}"),                # recipes.store: "r_" + token_hex(4)
    "server_id": re.compile(r"\d+-[0-9a-f]{6}|login-[0-9a-f]{8}"),  # handoff: f"{pid}-{hex(3)}" · login_cli
    "tool_name": re.compile(r"browser_[a-z_]{1,48}"),       # MCP 도구 이름
}


def is_tool_token(value: str) -> bool:
    """도구 어휘이거나 도구가 만든 식별자 형식인가(§1.1 등록 제외)."""
    return value in TOOL_VOCABULARY or any(p.fullmatch(value) for p in TOOL_ID_FORMATS.values())


# ------------------------------------------------------------------ 표현 집합(§1.3)

#: JS encodeURIComponent 가 남기는 기호(파이썬 quote 는 영숫자와 `_.-~` 를 늘 남긴다).
_JS_COMPONENT_SAFE = "!*'()"
#: JS encodeURI 가 남기는 기호.
_JS_URI_SAFE = "!*'();/?:@&=+$,#"
#: html.escape(quote=True) 가 바꾸는 5글자의 숫자 참조(10진, 소문자 16진·앞자리 0 없음).
_HTML_NUM = {"&": ("&#38;", "&#x26;"), "<": ("&#60;", "&#x3c;"), ">": ("&#62;", "&#x3e;"),
             '"': ("&#34;", "&#x22;"), "'": ("&#39;", "&#x27;")}
#: 모든 표현에서 그대로 남는 글자(퍼센트·JSON·repr·HTML 어느 것도 바꾸지 않는다) — 사전 거르기 닻.
_INVARIANT = re.compile(r"[A-Za-z0-9_.~-]+")
#: 영숫자 덩어리(4~7자 값의 경계 판정 단위). `_` 는 경계(str.isalnum 과 같은 판정).
_WORD = re.compile(r"[^\W_]+")
_ANCHOR_MIN = 4


_ALWAYS_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~")


@functools.lru_cache(maxsize=16)
def _quote_table(safe: str, plus: bool, lower: bool) -> Tuple[str, ...]:
    pct = "%{:02x}" if lower else "%{:02X}"
    keep = _ALWAYS_SAFE | frozenset(safe.encode("ascii"))
    table = [chr(b) if b in keep else pct.format(b) for b in range(256)]
    if plus:
        table[0x20] = "+"
    return tuple(table)


def _quote(value: str, safe: str = "", plus: bool = False, lower: bool = False) -> str:
    """urllib.parse.quote(value, safe)·quote_plus 와 같은 결과(lower=True 면 16진 소문자). 바이트마다 파이썬 함수를
    부르는 urllib 구현과 달리 표 조회를 map 으로 돌려 긴 값(수 MiB)도 빠르다. 짝 없는 서로게이트는 UnicodeError."""
    if _INVARIANT.fullmatch(value) is not None:
        return value
    return "".join(map(_quote_table(safe, plus, lower).__getitem__, value.encode("utf-8")))


#: R2 인코더(safe, plus): quote(safe="")·quote_plus(safe="")·encodeURIComponent·encodeURI 상당.
_ENCODERS = (("", False), ("", True), (_JS_COMPONENT_SAFE, False), (_JS_URI_SAFE, False))


def _percent_upper(value: str) -> List[str]:
    """R2 대문자 꼴."""
    try:
        return [_quote(value, safe, plus) for safe, plus in _ENCODERS]
    except UnicodeError:  # 짝 없는 서로게이트 — 퍼센트 꼴이 없다
        return []


def _percent(value: str) -> List[str]:
    """R2 전체(인코더마다 대문자·소문자)."""
    try:
        return [_quote(value, safe, plus, lower) for safe, plus in _ENCODERS for lower in (False, True)]
    except UnicodeError:
        return []


def _json(value: str) -> List[str]:
    return [json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]]


_HTML_DEC = {ord(c): dec for c, (dec, _) in _HTML_NUM.items()}
_HTML_HEX = {ord(c): hexa for c, (_, hexa) in _HTML_NUM.items()}


def _html(value: str) -> List[str]:
    return [html.escape(value, quote=True), value.translate(_HTML_DEC), value.translate(_HTML_HEX)]


def _tagged(value: str) -> "OrderedDict[str, bool]":
    """표현 → 주소 칸 계열(R1·R2·R2∘R2) 여부. 순서는 결정적(우선순위 순).
    같은 입력의 퍼센트 꼴은 한 번만 만든다(JSON·HTML 꼴이 원문과 같은 흔한 경우 — 긴 값의 등록 비용)."""
    memo: Dict[str, List[str]] = {}

    def pct(text: str) -> List[str]:
        found = memo.get(text)
        if found is None:
            found = memo[text] = _percent(text)
        return found

    r2 = pct(value)
    r2r2 = [_quote(upper) for upper in r2[0::2]]  # 이중 퍼센트(대문자 꼴 위에 quote(safe=""))
    r3 = _json(value)
    r4 = [repr(value)[1:-1]]
    r5 = _html(value)
    r6 = [p for j in r3 for p in pct(j)] + [j for h in r5 for j in _json(h)]  # R2∘R3, R3∘R5
    out: "OrderedDict[str, bool]" = OrderedDict()
    for form in [value] + r2 + r2r2:
        out.setdefault(form, True)
    for form in r3 + r4 + r5 + r6:
        out.setdefault(form, False)
    return out


def representations(value: str) -> List[str]:
    """값 v 의 표현 집합 R(v)(§1.3, 중복 제거·결정적 순서). 누출 판정·행렬 테스트·검증자의 기준.

    R1 원문 · R2 퍼센트(quote(safe="")·quote_plus·encodeURIComponent·encodeURI 상당, 16진 대문자·소문자) · R2∘R2
    이중 퍼센트(대문자) · R3 JSON 문자열 안쪽(ensure_ascii True/False) · R4 repr 안쪽 · R5 HTML(html.escape 의 명명
    참조, 그 5글자의 `&#NN;`·`&#xnn;`) · R6 R2∘R3·R3∘R5. 범위 밖: 대소문자 섞인 16진·base64·`\\u00XX` 전수·NFC/NFD.
    """
    return list(_tagged(value))


def url_representations(value: str) -> List[str]:
    """주소 칸 치환에 쓰는 표현(R1·R2·R2∘R2, §2 P3)."""
    return [form for form, url in _tagged(value).items() if url]


# ------------------------------------------------------------------ 레지스트리

def _bases(raw: str) -> List[str]:
    out: List[str] = []
    for base in (raw, unquote(raw), unquote_plus(raw)):
        if base not in out:
            out.append(base)
    return out


def _eligible(base: str) -> Optional[bool]:
    """보호 대상이면 긴 값 여부(≥ 8), 아니면 None(§1.1)."""
    n = len(base)
    if n < MIN_VALUE_CHARS or is_tool_token(base):
        return None
    if n < MIN_SUBSTR_CHARS and base.isalpha():
        return None  # `true`·`login`·`null` — 흔한 낱말을 모든 문장에서 가리지 않는다
    return n >= MIN_SUBSTR_CHARS


def _registrable(form: str) -> bool:
    return len(form) >= MIN_VALUE_CHARS and not any(form in marker for marker in _MARKERS)


class _Part(NamedTuple):
    """등록 값 하나의 기저(원문·unquote·unquote_plus) 하나."""

    r1: str                          # 원문(이 기저)
    long: bool                       # ≥ 8 — 부분 일치. 아니면 4~7(경계 일치)
    anchor: str                      # 모든 표현에 그대로 든 가장 긴 덩어리(사전 거르기, 없으면 "")
    forms: Tuple[str, ...]           # 표현(우선순위 순)
    url_forms: Tuple[str, ...]       # 주소 칸 계열
    blocks: Tuple[Tuple[str, int], ...]  # 원문 정렬 블록(긴 값만)
    chars: int


def _parts(raw: str) -> Tuple[_Part, ...]:
    """값 하나의 기저별 표현 전부(R5 B2: 값 단위 원자적 — 표현 일부만 예산에서 버리지 않는다. 글자 예산은 값 단위
    LRU 퇴출로만 지킨다, `SecretRegistry.add_values`)."""
    parts: List[_Part] = []
    bases = _bases(raw)
    if any(is_tool_token(base) for base in bases):
        return ()  # `%40e3`(= `@e3`) 처럼 인코딩된 도구 식별자도 통째로 제외
    for base in bases:
        long = _eligible(base)
        if long is None:
            continue
        runs = _INVARIANT.findall(base)
        anchor = max(runs, key=len) if runs else ""
        forms: List[str] = []
        url_forms: List[str] = []
        blocks: List[Tuple[str, int]] = []
        chars = 0
        if long and len(base) >= LONG_VALUE_CHARS:
            seen = set()
            for off in range(0, len(base) - BLOCK_CHARS + 1, BLOCK_CHARS):
                block = base[off:off + BLOCK_CHARS]
                if block not in seen:
                    seen.add(block)
                    blocks.append((block, off))
            chars += len(base)
        for form, url in _tagged(base).items():
            if not _registrable(form):
                continue
            forms.append(form)
            chars += len(form)
            if url:
                url_forms.append(form)
        if not forms:
            continue
        parts.append(_Part(base, long, anchor if len(anchor) >= _ANCHOR_MIN else "", tuple(forms),
                           tuple(url_forms), tuple(blocks), chars))
    return tuple(parts)


class _Snapshot(NamedTuple):
    long_groups: Tuple[Tuple[str, Tuple[str, ...]], ...]  # (닻, 표현들) — ≥ 8 값
    url_groups: Tuple[Tuple[str, Tuple[str, ...]], ...]   # (닻, 주소 칸 계열 표현들)
    short_words: FrozenSet[str]                           # 4~7 값의 영숫자뿐인 표현
    short_other: Tuple[str, ...]                          # 4~7 값의 그 밖 표현(경계 find)
    blocks: Dict[str, Tuple[str, int]]                    # 블록 → (원문, 원문 안 위치)
    hosts: FrozenSet[str]                                 # ≥ 8 원문의 casefold(호스트 라벨 비교)
    sentinels: Tuple[str, ...]                            # 표현에 쓰이지 않은 사설 영역 글자 후보
    empty: bool


_EMPTY = _Snapshot((), (), frozenset(), (), {}, frozenset(), ("\ue000",), True)
_PUA = [chr(c) for c in range(0xE000, 0xE100)]
_PUA_RE = re.compile("[\ue000-\ue0ff]")


def _build(entries: Iterable[Tuple[_Part, ...]]) -> _Snapshot:
    long_groups: List[Tuple[str, Tuple[str, ...]]] = []
    url_groups: List[Tuple[str, Tuple[str, ...]]] = []
    short_words: set = set()
    short_other: List[str] = []
    blocks: Dict[str, Tuple[str, int]] = {}
    hosts: set = set()
    used: set = set()
    any_part = False
    for parts in entries:
        for part in parts:
            any_part = True
            for form in part.forms:
                if _PUA_RE.search(form):
                    used.update(_PUA_RE.findall(form))
            if part.long:
                long_groups.append((part.anchor, part.forms))
                url_groups.append((part.anchor, part.url_forms))
                hosts.add(part.r1.casefold())
                for block, off in part.blocks:
                    blocks.setdefault(block, (part.r1, off))
            else:
                for form in part.forms:
                    if _WORD.fullmatch(form):
                        short_words.add(form)
                    elif form not in short_other:
                        short_other.append(form)
    if not any_part:
        return _EMPTY
    sentinels = tuple(c for c in _PUA if c not in used) or ("\ue000",)
    return _Snapshot(tuple(long_groups), tuple(url_groups), frozenset(short_words), tuple(short_other),
                     blocks, frozenset(hosts), sentinels, False)


def _extend_right(text: str, end: int, value: str, vend: int) -> int:
    n, m = len(text), len(value)
    while vend < m and end < n:
        step = min(4096, m - vend, n - end)
        if text[end:end + step] == value[vend:vend + step]:
            end += step
            vend += step
            continue
        while end < n and vend < m and text[end] == value[vend]:
            end += 1
            vend += 1
        break
    return end


def _extend_left(text: str, start: int, value: str, vstart: int) -> int:
    while vstart > 0 and start > 0:
        step = min(4096, vstart, start)
        if text[start - step:start] == value[vstart - step:vstart]:
            start -= step
            vstart -= step
            continue
        while start > 0 and vstart > 0 and text[start - 1] == value[vstart - 1]:
            start -= 1
            vstart -= 1
        break
    return start


def _mask_blocks(text: str, blocks: Dict[str, Tuple[str, int]], sentinel: str) -> str:
    """긴 원문의 정렬 블록이 나오면 그 자리에서 원문과 맞는 데까지 양쪽으로 넓혀 가린다(본문 위치마다 사전 조회 —
    값 수와 무관, 맞은 뒤에는 맞은 끝으로 건너뛴다). 남는 원문 조각은 양 끝 각각 BLOCK_CHARS 미만이다."""
    n = len(text)
    last = n - BLOCK_CHARS
    get = blocks.get
    spans: List[Tuple[int, int]] = []
    p = 0
    while p <= last:
        hit = get(text[p:p + BLOCK_CHARS])
        if hit is None:
            p += 1
            continue
        value, off = hit
        start = _extend_left(text, p, value, off)
        end = _extend_right(text, p + BLOCK_CHARS, value, off + BLOCK_CHARS)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(end, spans[-1][1]))
        else:
            spans.append((start, end))
        p = end
    if not spans:
        return text
    out: List[str] = []
    prev = 0
    for start, end in spans:
        out.append(text[prev:start])
        out.append(sentinel)
        prev = end
    out.append(text[prev:])
    return "".join(out)


def _mask_bounded(text: str, form: str, sentinel: str) -> str:
    """4~7 값의 영숫자가 아닌 글자가 든 표현 — 앞뒤가 영숫자가 아닐 때만(find 소비, 정규식 없음)."""
    size = len(form)
    n = len(text)
    out: List[str] = []
    prev = 0
    i = text.find(form)
    while i >= 0:
        end = i + size
        if (i == 0 or not text[i - 1].isalnum()) and (end >= n or not text[end].isalnum()):
            out.append(text[prev:i])
            out.append(sentinel)
            prev = end
            i = text.find(form, end)
        else:
            i = text.find(form, i + 1)
    if not out:
        return text
    out.append(text[prev:])
    return "".join(out)


def _pick_sentinel(text: str, snap: _Snapshot) -> Optional[str]:
    for c in snap.sentinels:
        if c not in text:
            return c
    return None


class SecretRegistry:
    """비밀 값의 LRU 집합. 값은 이 객체 밖으로 내보내지 않는다(repr·pickle·copy 금지)."""

    __slots__ = ("_lock", "_order", "_snapshot", "_version", "_cap", "_max_chars", "_chars")

    def __init__(self, cap: int = MAX_VALUES, max_chars: int = MAX_CHARS) -> None:
        self._lock = threading.Lock()
        self._order: "OrderedDict[str, Tuple[_Part, ...]]" = OrderedDict()
        self._snapshot: _Snapshot = _EMPTY
        self._version = 0
        self._cap = cap
        self._max_chars = max_chars
        self._chars = 0

    # -- 노출 금지 --------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<SecretRegistry {len(self._order)} values (values hidden)>"

    __str__ = __repr__

    def __reduce__(self) -> Any:  # pickle·copy 모두 이 경로를 쓴다
        raise TypeError("SecretRegistry 는 직렬화하지 않는다(프로세스 메모리 전용)")

    def __len__(self) -> int:
        """기억하는 값 수(상한 MAX_VALUES — LRU 밀려남 NB 의 증거 entries == cap)."""
        return len(self._order)

    @property
    def chars(self) -> int:
        """기억하는 표현·블록 글자 합(상한 MAX_CHARS)."""
        return self._chars

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def version(self) -> int:
        """등록·삭제마다 증가 — 가린 결과 캐시의 키에 쓴다(§2 P8)."""
        return self._version

    # -- 등록 -------------------------------------------------------------------

    def add_value(self, value: Any) -> None:
        if not isinstance(value, str) or len(value) < MIN_VALUE_CHARS:
            return
        self.add_values([value])

    def add_values(self, values: Iterable[str]) -> None:
        fresh = [v for v in values if isinstance(v, str) and len(v) >= MIN_VALUE_CHARS]
        if not fresh:
            return
        with self._lock:
            changed = False
            for value in fresh:
                if value in self._order:
                    self._order.move_to_end(value)  # 다시 본 값은 뒤로(스냅샷 내용은 같다)
                    continue
                parts = _parts(value)
                if not parts:
                    continue
                self._order[value] = parts
                self._chars += sum(p.chars for p in parts)
                changed = True
            # 값 단위 LRU(R5 B2): 오래된 값부터 통째로 잊는다. 가장 최근 값은 남긴다 — 값 하나의 전 표현이 글자 예산을
            # 넘으면 그 값만 혼자 남는다(표현 일부를 버려 살아 있는 값이 새는 일이 없다).
            while len(self._order) > 1 and (len(self._order) > self._cap or self._chars > self._max_chars):
                _, parts = self._order.popitem(last=False)
                self._chars -= sum(p.chars for p in parts)
                changed = True
            if changed:
                self._snapshot = _build(self._order.values())
                self._version += 1

    def clear(self) -> None:
        with self._lock:
            self._order.clear()
            self._chars = 0
            self._snapshot = _EMPTY
            self._version += 1

    # -- 조회 -------------------------------------------------------------------

    def mask(self, text: str) -> str:
        """자유 문장: 등록 값의 표현(≥ 8 부분 일치, 4~7 경계 일치)과 긴 원문의 조각을 `[REDACTED]` 로."""
        return self._mask(text, url=False)

    def mask_url_part(self, text: str) -> str:
        """주소 칸(호스트 뒤): ≥ 8 값의 R1·R2 표현과 긴 원문 조각만(4~7 은 정상 경로 보존 — §2 P3)."""
        return self._mask(text, url=True)

    def host_label_registered(self, label: str) -> bool:
        """호스트 라벨이 ≥ 8 등록 값(원문)과 casefold 로 같은가(Chromium 은 호스트를 소문자로 바꾼다)."""
        hosts = self._snapshot.hosts
        return bool(hosts) and label.casefold() in hosts

    def _mask(self, text: str, url: bool) -> str:
        snap = self._snapshot
        if snap.empty or len(text) < MIN_VALUE_CHARS:
            return text
        sentinel = _pick_sentinel(text, snap)
        if sentinel is None:  # 사설 영역 글자 256개를 모두 담은 본문 — 표지로 바로 바꾼다(합치기만 못 한다)
            sentinel = MASK
        hits = set()
        for anchor, forms in (snap.url_groups if url else snap.long_groups):
            if anchor and anchor not in text:
                continue
            for form in forms:
                if form in text:
                    hits.add(form)
        out = text
        for form in sorted(hits, key=lambda f: (-len(f), f)):
            out = out.replace(form, sentinel)
        if snap.blocks and len(out) >= LONG_VALUE_CHARS:
            out = _mask_blocks(out, snap.blocks, sentinel)
        if not url:
            if snap.short_words:
                words = snap.short_words.intersection(_WORD.findall(out))
                if words:
                    out = _WORD.sub(lambda m: sentinel if m.group(0) in words else m.group(0), out)
            for form in snap.short_other:
                if form in out:
                    out = _mask_bounded(out, form, sentinel)
        if out is text:
            return text
        if sentinel != MASK:
            out = re.sub(re.escape(sentinel) + "+", MASK, out)
        return out


#: 프로세스 하나에 하나(서버·로그 필터·stderr 줄이 같은 값을 본다).
REGISTRY = SecretRegistry()


def register_url(url: Any) -> None:
    """도구가 본 주소 하나의 비밀 값을 등록한다(public_url 이 가리는 값과 같은 판정). 예외를 내지 않는다."""
    if not isinstance(url, str) or not url:
        return
    try:
        from security.public_url import secret_values

        found = secret_values(url)
        if found:
            REGISTRY.add_values(found)
    except Exception:  # noqa: BLE001 - 등록 실패가 도구 동작을 막지 않는다(URL 문법 계층은 그대로 동작)
        return


def register_urls(urls: Iterable[Any]) -> None:
    try:
        for url in urls:
            register_url(url)
    except Exception:  # noqa: BLE001
        return


def mask_registered(text: str) -> str:
    return REGISTRY.mask(text)


def version() -> int:
    return REGISTRY.version


def clear() -> None:
    """테스트용 — 등록 값을 모두 잊는다."""
    REGISTRY.clear()


def _on_request(request: Any) -> None:
    """문서(main-frame) 이동 요청 — 리다이렉트 체인의 각 홉도 새 요청으로 온다."""
    try:
        if not request.is_navigation_request():
            return
        try:
            frame = request.frame
            if frame.parent_frame is not None:
                return
        except Exception:  # noqa: BLE001 - 새 탭 첫 요청은 프레임이 아직 없을 수 있다 → 등록(안전 쪽)
            pass
        register_url(request.url)
        previous: Optional[Any] = getattr(request, "redirected_from", None)
        hops = 0
        while previous is not None and hops < 32:
            register_url(previous.url)
            previous = getattr(previous, "redirected_from", None)
            hops += 1
    except Exception:  # noqa: BLE001
        return


def _watch_page(page: Any) -> None:
    def moved(frame: Any) -> None:
        try:
            if frame.parent_frame is None:
                register_url(frame.url)
        except Exception:  # noqa: BLE001
            return

    try:
        page.on("framenavigated", moved)
        register_url(getattr(page, "url", ""))
    except Exception:  # noqa: BLE001
        return


def watch_browser_context(context: Any) -> None:
    """브라우저 컨텍스트의 main-frame 이동(framenavigated — 같은 문서 안 이동 포함)과 문서 요청 리다이렉트
    체인을 등록한다. 새 탭·팝업도 context 의 page 이벤트로 잡는다. 실패해도 조용히 끝난다."""
    on = getattr(context, "on", None)
    if not callable(on):
        return
    try:
        on("request", _on_request)
        on("page", _watch_page)
        for page in list(getattr(context, "pages", []) or []):
            _watch_page(page)
    except Exception:  # noqa: BLE001
        return
