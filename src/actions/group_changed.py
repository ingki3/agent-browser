"""WS-62 R12: GROUP_CHANGED 거부 응답의 '무엇이 바뀌었나'(data.group_changed.changed) — 표시 전용.

하나투어 '다음달' 처럼 기준 고정이 바깥 레이아웃이면 지문에 바깥 글자 줄이 없고(달 표제는 반복 항목 안) 순서열만 바뀐다 —
그때는 순서열 첫 차이 앞/뒤(items)만 보이고, 달 표제는 hint 대로 다시 관찰한 data.repeated 에서 본다(조정자 결정 1).

거부 판정은 v1.16.0 원문(`perception.repeated_v1160` 의 groupCheck)이 이미 끝냈다. 여기서는 그 판정에 쓴 옛(관찰 때)·지금
지문을 사람이 읽을 조각으로 나눌 뿐이며 판정·고정에 쓰지 않는다(안전과 무관 — 계획 R3 결정 B).

분해 규칙(계획 R2-1): 저장 지문은 줄바꿈으로 이은 [L:…][A:…][바깥 정적 글자…]['S:'+항목 순서열] + '\\n#k'.
마지막 줄이 `#k`, 그 앞 줄이 `S:` 로 시작해야 형식이 맞고, 나머지 줄이 바깥 글자다(바깥 글자도 'S:' 로 시작할 수 있으므로
'S: 로 시작하는 줄 찾기'를 쓰지 않는다). 옛·지금 지문 중 하나라도 상한(PIN_LIMIT)에 닿았거나(잘린 본문 — 순서열이 사라졌을 수
있다) 형식이 맞지 않으면 changed 를 내지 않는다.

R13(재검증 B1·B2):
* PII — 자르기 전에 **바깥 글자 전체(줄바꿈으로 이은 것)와 순서열 전체(항목을 빈칸으로 이은 '항목 이음 문자열')** 를 마스킹한다.
  항목·줄 경계로 나뉜 번호도 이음 문자열에서는 한 덩어리다. 마스킹 뒤에도 PII 가 남아 보이면(보이지 않는 글자로 쪼갬·빈칸 여럿·
  바깥과 순서열에 걸침 — 불확실) 그 부분을 싣지 않는다(fail-closed). 마스킹 자체가 실패해도 마찬가지.
* 창 — 마스킹·표시 살균을 끝낸 before/after 의 **실제 첫 문자 차이**를 중심으로 앞 문맥 `_LEAD` 자 + 차이 부분, 각 80자.
  잘린 쪽은 '…'. 결과 before==after(차이가 PII 안에만 있었던 경우 등)면 그 쌍을 싣지 않는다.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Dict, List, Optional

#: 바깥 글자 앞/뒤 쌍 최대 수·각 글자 상한.
CHANGED_PAIRS = 3
CHANGED_CHARS = 80
#: 창에서 첫 차이 앞에 남길 문맥 글자 수.
_LEAD = 20
_TAIL_RE = re.compile(r"^#-?\d+$")
#: '@ . -' 옆 빈칸(항목 이음에서 생긴 것) — 이메일·하이픈 번호가 항목 경계로 쪼개져도 이어지게.
_PUNCT_GAP = re.compile(r" *([@.\-]) *")
#: 빈 항목 자리 표시('_') — 잔여 PII 검사에서는 없는 것으로 본다.
_PLACEHOLDER = re.compile(r"(?:(?<= )|^)_(?= |$)")
#: 번호·주소 글자 사이 빈칸(빈칸 여럿으로 쪼갠 번호·도메인) — 잔여 PII 검사에서 붙여 본다(한글 등 다른 글자 옆 빈칸은 둔다).
_ASCII_GAP = re.compile(r"(?<=[A-Za-z0-9._%+@-])\s+(?=[A-Za-z0-9._%+@-])")
#: 잔여 PII 검사에서 지우는 보이지 않는 글자 범주.
_HIDDEN = ("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp")


def _split(pin: Any, limit: int) -> Optional[Dict[str, Any]]:
    if not isinstance(pin, str) or len(pin) >= limit:
        return None
    lines = pin.split("\n")
    if len(lines) < 2 or not _TAIL_RE.match(lines[-1]) or not lines[-2].startswith("S:"):
        return None
    return {"outer": lines[:-2], "seq": lines[-2], "k": lines[-1]}


def _norm(text: str) -> str:
    """한 줄 정리: 항목 구분(\\x01)·연속 공백 → 빈칸 하나, '@ . -' 옆 빈칸 제거."""
    return _PUNCT_GAP.sub(r"\1", " ".join(text.replace("\x01", " ").split()))


def _token(t: str) -> str:
    """순서열 낱말 하나 — 구조 접두(i: 대상 이름 첫 낱말·t: 항목 글자)를 떼고, 빈 낱말은 '_'(빈칸)."""
    if t[:2] in ("i:", "t:"):
        t = t[2:]
    return t or "_"


def _residual_pii(masked: str) -> bool:
    """마스킹한 글자에 아직 PII 가 보이는가(불확실). 보이지 않는 글자·빈 항목 자리를 지우고, 줄바꿈을 빈칸으로 본 것과
    번호·주소 글자 사이 빈칸을 지운 것(빈칸 여럿으로 쪼갠 번호) 둘 다에 같은 규칙을 다시 돌린다. 빈칸을 지운 쪽은 카드 규칙을 뺀다
    (달력 날짜 숫자 이음이 Luhn 을 우연히 통과하는 과다 생략 방지 — 카드는 빈칸·하이픈 구분을 규칙이 직접 잡는다)."""
    from security.masking import mask_text

    t = masked.replace("\n", " ")
    t = "".join(ch for ch in t if unicodedata.category(ch) not in _HIDDEN)
    t = _PUNCT_GAP.sub(r"\1", " ".join(_PLACEHOLDER.sub("", t).split()))
    if mask_text(t).hits:
        return True
    return bool({k for k in mask_text(_ASCII_GAP.sub("", t)).hits if k != "credit_card"})


def _mask(text: str) -> str:
    from security.masking import mask

    return mask(text)


def _escape(text: str) -> str:
    """보이지 않는·화면 조작 글자를 보이는 표기로(여기서는 자르지 않음 — 창은 다음 단계)."""
    from actions.verification import safe_page_text

    return safe_page_text(text, 8 * len(text) + 16)


def _cut(text: str, start: int) -> str:
    head = "…" if start > 0 else ""
    room = CHANGED_CHARS - len(head)
    body = text[start:]
    if len(body) > room:
        body = body[:room - 1] + "…"
    return head + body


def _window(before: str, after: str) -> Optional[Dict[str, str]]:
    """마스킹·살균을 끝낸 두 글자 — 실제 첫 문자 차이 앞 `_LEAD` 자부터 각 80자. 같으면 None(그 쌍 생략)."""
    if before == after:
        return None
    p = 0
    while p < min(len(before), len(after)) and before[p] == after[p]:
        p += 1
    start = max(0, p - _LEAD)
    out = {"before": _cut(before, start), "after": _cut(after, start)}
    return None if out["before"] == out["after"] else out


def _side(pin: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """한 쪽 지문 → 마스킹한 바깥 줄들·순서열 이음 문자열과 각 불확실 여부. 마스킹 실패면 None."""
    outer_raw = "\n".join(_norm(line) for line in pin["outer"])
    seq_raw = _norm(" ".join(_token(t) for t in pin["seq"][2:].split("\x01")))
    try:
        outer, seq = _mask(outer_raw), _mask(seq_raw)
        return {"outer": outer.split("\n") if outer_raw else [], "seq": seq,
                "outer_unsure": _residual_pii(outer), "seq_unsure": _residual_pii(seq),
                # 바깥 마지막 줄과 순서열 첫 항목에 걸친 PII — 따로 가린 둘을 이어 다시 본다.
                "joint_unsure": bool(outer_raw) and _residual_pii(outer + " " + seq)}
    except Exception:  # noqa: BLE001 — 표시 전용: 마스킹이 실패하면 싣지 않는다(fail-closed)
        return None


def changed_parts(old_pin: Any, new_pin: Any) -> Optional[Dict[str, Any]]:
    """옛·지금 지문 → {text: [{before, after}, ≤3], items?: {before, after}, position?: {before, after}}. 분해 불가면 None.

    text = 바깥 글자 줄의 바뀐 부분(줄 단위 차이, 실제 첫 문자 차이 조금 앞부터 80자). items = 항목 순서열이 바뀌었을 때
    이음 문자열의 첫 문자 차이 조금 앞부터(접두 뗌). position = 대상 항목 순번(#k)이 바뀌었을 때. 마스킹 실패·바깥 또는
    바깥/순서열에 걸친 잔여 PII 면 None, 순서열에만 잔여 PII 면 items 생략. 마스킹 뒤 앞/뒤가 같은 쌍은 생략.
    """
    from perception.repeated_v1160 import PIN_LIMIT

    old, new = _split(old_pin, PIN_LIMIT), _split(new_pin, PIN_LIMIT)
    if old is None or new is None:
        return None
    a, b = _side(old), _side(new)
    if a is None or b is None or a["outer_unsure"] or b["outer_unsure"] or a["joint_unsure"] or b["joint_unsure"]:
        return None
    pairs: List[Dict[str, str]] = []
    ops = difflib.SequenceMatcher(a=a["outer"], b=b["outer"], autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        if len(pairs) >= CHANGED_PAIRS:
            break
        pair = _window(_escape(" ".join(a["outer"][i1:i2])), _escape(" ".join(b["outer"][j1:j2])))
        if pair is not None:
            pairs.append(pair)
    out: Dict[str, Any] = {"text": pairs}
    if old["seq"] != new["seq"] and not a["seq_unsure"] and not b["seq_unsure"]:
        items = _window(_escape(a["seq"]), _escape(b["seq"]))
        if items is not None:
            out["items"] = items
    if old["k"] != new["k"]:
        out["position"] = {"before": old["k"], "after": new["k"]}
    return out
