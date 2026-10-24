"""WS-62 R12: GROUP_CHANGED 거부 응답의 '무엇이 바뀌었나'(data.group_changed.changed) — 표시 전용.

하나투어 '다음달' 처럼 기준 고정이 바깥 레이아웃이면 지문에 바깥 글자 줄이 없고(달 표제는 반복 항목 안) 순서열만 바뀐다 —
그때는 순서열 첫 차이 앞/뒤(items)만 보이고, 달 표제는 hint 대로 다시 관찰한 data.repeated 에서 본다(조정자 결정 1).

거부 판정은 v1.16.0 원문(`perception.repeated_v1160` 의 groupCheck)이 이미 끝냈다. 여기서는 그 판정에 쓴 옛(관찰 때)·지금
지문을 사람이 읽을 조각으로 나눌 뿐이며 판정·고정에 쓰지 않는다(안전과 무관 — 계획 R3 결정 B).

분해 규칙(계획 R2-1): 저장 지문은 줄바꿈으로 이은 [L:…][A:…][바깥 정적 글자…]['S:'+항목 순서열] + '\\n#k'.
마지막 줄이 `#k`, 그 앞 줄이 `S:` 로 시작해야 형식이 맞고, 나머지 줄이 바깥 글자다(바깥 글자도 'S:' 로 시작할 수 있으므로
'S: 로 시작하는 줄 찾기'를 쓰지 않는다). 옛·지금 지문 중 하나라도 상한(PIN_LIMIT)에 닿았거나(잘린 본문 — 순서열이 사라졌을 수
있다) 형식이 맞지 않으면 changed 를 내지 않는다.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional

#: 바깥 글자 앞/뒤 쌍 최대 수·각 글자 상한.
CHANGED_PAIRS = 3
CHANGED_CHARS = 80
#: 첫 차이 앞에 남길 글자 수·순서열 낱말 수(바뀐 자리가 80자 창 안에 들게).
_LEAD = 20
_LEAD_TOKENS = 3
_TAIL_RE = re.compile(r"^#-?\d+$")


def _split(pin: Any, limit: int) -> Optional[Dict[str, Any]]:
    if not isinstance(pin, str) or len(pin) >= limit:
        return None
    lines = pin.split("\n")
    if len(lines) < 2 or not _TAIL_RE.match(lines[-1]) or not lines[-2].startswith("S:"):
        return None
    return {"outer": lines[:-2], "seq": lines[-2], "k": lines[-1]}


def _masked(text: str) -> str:
    """PII 마스킹은 자르기 전 전체 글자에(잘린 전화번호·카드번호가 규칙을 빠져나가지 않게)."""
    from security.masking import mask

    return mask(" ".join(text.replace("\x01", " ").split()))


def _clean(text: str) -> str:
    from actions.verification import safe_page_text

    return safe_page_text(text, CHANGED_CHARS)


def _window(before: str, after: str) -> Dict[str, str]:
    """두 글자의 첫 차이 지점 조금 앞부터 각 80자(같은 앞부분 때문에 앞/뒤가 똑같이 잘리지 않게)."""
    p = 0
    while p < min(len(before), len(after)) and before[p] == after[p]:
        p += 1
    start = max(0, p - _LEAD)
    return {"before": _clean(before[start:]), "after": _clean(after[start:])}


def _token(t: str) -> str:
    """순서열 낱말 하나 — 구조 접두(i: 대상 이름 첫 낱말·t: 항목 글자)를 떼고, 빈 낱말은 '_'(빈칸)."""
    if t[:2] in ("i:", "t:"):
        t = t[2:]
    return t or "_"


def _items_window(before: str, after: str) -> Dict[str, str]:
    """항목 순서열(S:) 의 첫 차이 낱말 조금 앞부터 각 80자(조정자 결정 1 — 순서열만 바뀐 거부도 앞/뒤가 보이게)."""
    a = [_token(t) for t in before[2:].split("\x01")]
    b = [_token(t) for t in after[2:].split("\x01")]
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    start = max(0, i - _LEAD_TOKENS)
    return {"before": _clean(_masked(" ".join(a[start:]))), "after": _clean(_masked(" ".join(b[start:])))}


def changed_parts(old_pin: Any, new_pin: Any) -> Optional[Dict[str, Any]]:
    """옛·지금 지문 → {text: [{before, after}, ≤3], items?: {before, after}, position?: {before, after}}. 분해 불가면 None.

    text = 바깥 글자 줄의 바뀐 부분(줄 단위 차이, 첫 차이 조금 앞부터 80자). items = 항목 순서열이 바뀌었을 때 첫 차이 낱말
    조금 앞부터(접두 뗌). position = 대상 항목 순번(#k)이 바뀌었을 때.
    """
    from perception.repeated_v1160 import PIN_LIMIT

    old, new = _split(old_pin, PIN_LIMIT), _split(new_pin, PIN_LIMIT)
    if old is None or new is None:
        return None
    pairs: List[Dict[str, str]] = []
    ops = difflib.SequenceMatcher(a=old["outer"], b=new["outer"], autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        if len(pairs) >= CHANGED_PAIRS:
            break
        pairs.append(_window(_masked(" ".join(old["outer"][i1:i2])), _masked(" ".join(new["outer"][j1:j2]))))
    out: Dict[str, Any] = {"text": pairs}
    if old["seq"] != new["seq"]:
        out["items"] = _items_window(old["seq"], new["seq"])
    if old["k"] != new["k"]:
        out["position"] = {"before": old["k"], "after": new["k"]}
    return out
