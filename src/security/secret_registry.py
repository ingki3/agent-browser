"""도구가 본 주소의 비밀 값 레지스트리 (WS-51 R3 — 자유 문장 가리기 D안).

출처를 모르는 자유 문장(페이지 제목·오류 문구·로그 줄)에서는 `Request /a?token=abc HTTP 401` 과
`/a?token=prefix SECRET` 이 같은 모양이라, 값이 어디서 끝나는지 문장만 보고는 정할 수 없다(R1→R2 경계 추측의
한계). 그 대신 **도구가 실제로 본 주소**(이동 대상·리다이렉트 체인·탭 주소·응답의 주소 칸)에서 public_url 이
`[REDACTED]` 로 바꾸는 바로 그 원 값을 여기에 기록해 두고, 자유 문장에서는 그 값(과 인코딩 변형)을 정확히 일치시켜
가린다. CI 로그의 비밀 가리기(`::add-mask::`)와 같은 방식이다.

* 등록 판정은 public_url 과 **같은 함수**(`security.public_url.secret_values` — `_public_url` 이 가린 값을 모은다)
  라 주소 칸과 갈라지지 않는다. 사용자정보 비밀번호(비밀번호가 없으면 사용자 이름 — 토큰을 사용자 자리에 쓰는
  꼴)와 `next=` 안 중첩 주소의 값도 포함한다.
* 변형: 원문·unquote·unquote_plus 각각에 대해 그대로·quote(safe="")·quote_plus·quote(safe="/:")·JSON 이스케이프
  (ASCII/비ASCII)·HTML 이스케이프(따옴표 포함/제외)·repr 이스케이프.
* 길이 ≥ MIN_SUBSTR_CHARS(8) 는 부분 문자열 일치, MIN_VALUE_CHARS(4)~7 은 앞뒤가 영숫자가 아닐 때만 일치하고
  **글자만으로 된 4~7자 값(`true`·`login`·`null`)은 등록하지 않는다**(모든 자유 문장의 흔한 낱말을 가리게 된다 —
  URL 문법 계층은 그대로 가린다). 4자 미만은 등록하지 않는다.
* 프로세스 메모리 전용: 디스크·로그·응답에 쓰지 않는다. repr·str 은 개수만 보이고, pickle·copy 는 거부한다.
* LRU 상한 MAX_VARIANTS(256 변형) — 다시 본 값은 뒤로 옮긴다. 상한을 넘으면 가장 오래된 변형부터 잊는다.
* 스레드 안전: 등록은 Lock 안에서 새 스냅샷 튜플을 만들고, 조회는 그 튜플을 한 번 읽는다(잠금 없음).
* 등록은 예외를 내지 않는다 — 등록 실패가 도구 동작을 막지 않는다.

일치 탐색: 정규식 대안 묶음(`a|b|…`)은 1MiB 에 0.6초(리뷰 실측)라 쓰지 않고 값마다 `str.find` 로 찾는다. 다음
탐색은 값의 최소 주기(period)만큼 건너뛴다 — 겹친 출현은 그 간격 이상 떨어져 있으므로 빠뜨리지 않고, 경계가
없는(주기 = 길이) 무작위 토큰은 겹침이 없어 출현 수만큼만 돈다.
"""

from __future__ import annotations

import html
import json
import threading
from collections import OrderedDict
from typing import Any, Iterable, List, Optional, Tuple
from urllib.parse import quote, quote_plus, unquote, unquote_plus

from security.masking import MASK

#: 레지스트리가 기억하는 변형 수 상한(LRU).
MAX_VARIANTS = 256
#: 이 길이 이상은 부분 문자열 일치.
MIN_SUBSTR_CHARS = 8
#: 이 길이 미만은 등록하지 않는다(URL 문법 계층만).
MIN_VALUE_CHARS = 4
#: 이보다 긴 값은 등록하지 않는다(메모리 상한 — 64KiB 넘는 쿼리 값은 URL 문법 계층만).
MAX_VALUE_CHARS = 64 * 1024
#: 가림 표지 — 이 안에 들어 있는 문자열(`REDACTED` 등)을 등록하면 표지가 다시 가려져 멱등이 깨진다.
_MARKERS = ("[REDACTED]", "[REDACTED-URL]", "[REDACTED-DEPTH]")

_Entry = Tuple[str, bool, int]  # (변형, 경계 일치 여부, 다음 탐색 간격 = 최소 주기)


def _period(value: str) -> int:
    """value 의 최소 주기(KMP 실패 함수). 겹친 두 출현의 시작 간격은 항상 이 값 이상이다."""
    fail = [0] * len(value)
    k = 0
    for i in range(1, len(value)):
        while k and value[i] != value[k]:
            k = fail[k - 1]
        if value[i] == value[k]:
            k += 1
        fail[i] = k
    return len(value) - fail[-1] if value else 1


def _registrable(variant: str) -> bool:
    if len(variant) < MIN_VALUE_CHARS or len(variant) > MAX_VALUE_CHARS:
        return False
    if len(variant) < MIN_SUBSTR_CHARS and variant.isalpha():
        return False  # `true`·`login`·`null` — 흔한 낱말을 모든 문장에서 가리지 않는다
    return not any(variant in marker for marker in _MARKERS)


def variants(value: str) -> List[str]:
    """값 하나의 표현 변형(중복 제거, 등록 조건을 통과한 것만). 순서는 결정적."""
    out: "OrderedDict[str, None]" = OrderedDict()
    bases: List[str] = []
    for base in (value, unquote(value), unquote_plus(value)):
        if base not in bases:
            bases.append(base)
    for base in bases:
        forms = (
            base,
            quote(base, safe=""),
            quote_plus(base, safe=""),
            quote(base, safe="/:"),
            json.dumps(base)[1:-1],
            json.dumps(base, ensure_ascii=False)[1:-1],
            html.escape(base),
            html.escape(base, quote=False),
            repr(base)[1:-1],
        )
        for form in forms:
            if _registrable(form):
                out[form] = None
    return list(out)


class SecretRegistry:
    """비밀 값 변형의 LRU 집합. 값은 이 객체 밖으로 내보내지 않는다(repr·pickle·copy 금지)."""

    __slots__ = ("_lock", "_order", "_snapshot", "_version", "_cap")

    def __init__(self, cap: int = MAX_VARIANTS) -> None:
        self._lock = threading.Lock()
        self._order: "OrderedDict[str, _Entry]" = OrderedDict()
        self._snapshot: Tuple[_Entry, ...] = ()
        self._version = 0
        self._cap = cap

    # -- 노출 금지 --------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<SecretRegistry {len(self._snapshot)} variants (values hidden)>"

    __str__ = __repr__

    def __reduce__(self) -> Any:  # pickle·copy 모두 이 경로를 쓴다
        raise TypeError("SecretRegistry 는 직렬화하지 않는다(프로세스 메모리 전용)")

    def __len__(self) -> int:
        return len(self._snapshot)

    @property
    def version(self) -> int:
        """등록·삭제마다 증가 — 가린 결과 캐시의 키에 쓴다."""
        return self._version

    # -- 등록 -------------------------------------------------------------------

    def add_value(self, value: Any) -> None:
        if not isinstance(value, str) or len(value) < MIN_VALUE_CHARS:
            return
        self.add_values([value])

    def add_values(self, values: Iterable[str]) -> None:
        forms: List[str] = []
        for value in values:
            if isinstance(value, str) and MIN_VALUE_CHARS <= len(value) <= MAX_VALUE_CHARS:
                forms.extend(variants(value))
        if not forms:
            return
        with self._lock:
            changed = False
            for form in forms:
                if form in self._order:
                    self._order.move_to_end(form)
                    continue
                self._order[form] = (form, len(form) < MIN_SUBSTR_CHARS, _period(form))
                changed = True
            while len(self._order) > self._cap:
                self._order.popitem(last=False)
                changed = True
            if changed:
                self._snapshot = tuple(self._order.values())
                self._version += 1

    def clear(self) -> None:
        with self._lock:
            self._order.clear()
            self._snapshot = ()
            self._version += 1

    # -- 조회 -------------------------------------------------------------------

    def spans(self, text: str) -> List[Tuple[int, int]]:
        """text 안 등록 값의 출현 구간들(정렬·병합 전)."""
        entries = self._snapshot
        out: List[Tuple[int, int]] = []
        if not entries or len(text) < MIN_VALUE_CHARS:
            return out
        n = len(text)
        for value, bounded, step in entries:
            i = text.find(value)
            size = len(value)
            while i >= 0:
                end = i + size
                if not bounded or ((i == 0 or not text[i - 1].isalnum())
                                   and (end >= n or not text[end].isalnum())):
                    out.append((i, end))
                i = text.find(value, i + step)
        return out

    def mask(self, text: str) -> str:
        """등록 값의 출현을 `[REDACTED]` 로 바꾼다(겹치거나 붙은 구간은 하나로)."""
        found = self.spans(text)
        if not found:
            return text
        found.sort()
        buf: List[str] = []
        last = 0
        for start, end in found:
            if end <= last:
                continue
            if start >= last:
                buf.append(text[last:start])
                buf.append(MASK)
            last = end
        buf.append(text[last:])
        return "".join(buf)


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
