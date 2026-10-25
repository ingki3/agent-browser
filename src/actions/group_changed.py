"""WS-62 R17: GROUP_CHANGED 거부 응답의 '무엇이 바뀌었나' — 바뀐 **종류**만(페이지 글자 0, 사용자 결정 A · #136 · #158).

R13~R16 은 changed/before/after 에 원문 조각(마스킹·숫자 '#'·80자 창)을 실었고, 네 번 연속 새 형태의 비밀·PII 노출이
나왔다(줄·요소 경계로 나뉜 Cookie/Bearer 등). R17 은 원문을 싣지 않는다:

* `data.group_changed.changed = {"kinds": [...]}` — 값은 고정 어휘 `KINDS` 에서만(페이지 유래 문자열 0).
  - `number`  : 숫자 성격 글자(유니코드 Nd·Nl·No, NFKC 뒤 숫자 포함)만 다르거나, 숫자·빈 칸뿐인 항목이 생기거나 없어짐
                (날짜·가격·달 이동의 날짜 칸).
  - `text`    : 글자 내용이 바뀜(바깥 글자 줄, 또는 숫자를 빼고도 다른 항목).
  - `order`   : 항목 순서만 바뀜(같은 항목들의 재배열) 또는 대상 항목 순번만 바뀜.
  - `added` / `removed` : 글자가 있는 묶음 항목이 생김 / 없어짐.
  - `unknown` : 판정 불가(지문 상한 도달·형식 불일치·처리 실패·차이 없음).
* `data.group_changed.before/after`(#158) — 필드 계약 유지, 값은 고정 표식 `MARK_CHANGED`/`MARK_SAME`(그 쪽 지문이 없으면 None).
* hint 는 종류 어휘만 인용한다(`hint_for`).

판정은 v1.16.0 원문(`perception.repeated_v1160` 의 groupCheck)이 이미 끝냈다. 여기서는 그 판정에 쓴 옛(관찰 때)·지금
지문을 안에서만 비교해 종류를 고를 뿐이며 판정·고정에 쓰지 않는다(안전과 무관 — 계획 R3 결정 B).

지문 분해(계획 R2-1): 저장 지문은 줄바꿈으로 이은 [L:…][A:…][바깥 정적 글자…]['S:'+항목 순서열] + '\\n#k'. 마지막 줄이
`#k`, 그 앞 줄이 `S:` 로 시작해야 형식이 맞고 나머지 줄이 바깥 글자다. 옛·지금 지문 중 하나라도 상한(PIN_LIMIT)에 닿았거나
형식이 맞지 않으면 `unknown`.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple

#: 종류 어휘(출력 순서). changed.kinds 의 값은 이 안에서만 나온다.
KINDS = ("number", "text", "order", "added", "removed", "unknown")
#: before/after 고정 표식 — 페이지 글자 대신.
MARK_CHANGED = "(바뀜)"
MARK_SAME = "(같음)"
#: 거부 안내 — {kinds} 자리에 종류 어휘만 들어간다.
HINT_TEMPLATE = ("관찰 뒤 이 버튼이 속한 화면 부분이 바뀌어(종류: {kinds} — data.group_changed.changed.kinds) "
                 "누르지 않았습니다 — observe_page 로 다시 관찰한 뒤 같은 버튼을 새 id 로 누르세요.")

_TAIL_RE = re.compile(r"^#-?\d+$")
_NUMERIC_CATS = ("Nd", "Nl", "No")


def _split(pin: Any, limit: int) -> Optional[Dict[str, Any]]:
    if not isinstance(pin, str) or len(pin) >= limit:
        return None
    lines = pin.split("\n")
    if len(lines) < 2 or not _TAIL_RE.match(lines[-1]) or not lines[-2].startswith("S:"):
        return None
    return {"outer": lines[:-2], "seq": lines[-2], "k": lines[-1]}


def _tokens(seq: str) -> List[str]:
    """순서열 낱말들 — 구조 접두(i:·t:)를 떼고, 빈 항목 자리('_')는 빈 글자."""
    out = []
    for t in seq[2:].split("\x01"):
        if t[:2] in ("i:", "t:"):
            t = t[2:]
        out.append("" if t == "_" else t)
    return out


def _is_numeric(ch: str) -> bool:
    return unicodedata.category(ch) in _NUMERIC_CATS or any(
        unicodedata.category(x) in _NUMERIC_CATS for x in unicodedata.normalize("NFKC", ch))


def _shape(text: str) -> str:
    """숫자 성격 글자 연속 → '0' 하나, 공백 정리(NFKC) — 숫자만 다른 두 글자는 같은 모양."""
    t = "".join("0" if _is_numeric(c) else c for c in unicodedata.normalize("NFKC", text))
    return " ".join(re.sub("0+", "0", t).split())


def _letters(text: str) -> str:
    """숫자 성격 글자·공백을 뺀 나머지 — 비면 숫자·빈칸뿐인 항목."""
    return "".join(c for c in unicodedata.normalize("NFKC", text) if not _is_numeric(c) and not c.isspace())


def _diff(a: List[str], b: List[str], extra_add: str, extra_remove: str) -> Set[str]:
    """두 목록(바깥 줄 또는 항목)의 바뀐 종류. 모양(_shape)으로 맞춘 뒤 맞지 않는 구간을 낱낱이 본다."""
    out: Set[str] = set()
    if a == b:
        return out
    if sorted(a) == sorted(b):
        return {"order"}
    sa, sb = [_shape(x) for x in a], [_shape(x) for x in b]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=sa, b=sb, autojunk=False).get_opcodes():
        if tag == "equal":
            if a[i1:i2] != b[j1:j2]:
                out.add("number")
            continue
        old, new = a[i1:i2], b[j1:j2]
        for x, y in zip(old, new):
            out.add("number" if _letters(x) == _letters(y) else "text")
        for x in old[len(new):]:
            out.add("number" if not _letters(x) else extra_remove)
        for y in new[len(old):]:
            out.add("number" if not _letters(y) else extra_add)
    return out


def changed_kinds(old_pin: Any, new_pin: Any) -> List[str]:
    """옛·지금 지문 → 바뀐 종류 목록(KINDS 순서, 비지 않음). 판정 불가·실패는 ['unknown']."""
    try:
        from perception.repeated_v1160 import PIN_LIMIT

        old, new = _split(old_pin, PIN_LIMIT), _split(new_pin, PIN_LIMIT)
        if old is None or new is None:
            return ["unknown"]
        kinds = _diff(old["outer"], new["outer"], "text", "text")
        kinds |= _diff(_tokens(old["seq"]), _tokens(new["seq"]), "added", "removed")
        if old["k"] != new["k"] and old["seq"] == new["seq"]:
            kinds.add("order")
        out = [k for k in KINDS if k in kinds]
        return out or ["unknown"]
    except Exception:  # noqa: BLE001 — 표시 전용: 어느 단계든 실패하면 unknown(거부 응답은 그대로)
        return ["unknown"]


def display_pair(old_pin: Any, new_pin: Any) -> Tuple[Optional[str], Optional[str]]:
    """#158: data.group_changed.before/after — 필드 계약은 그대로, 값은 고정 표식(페이지 글자 0). 없는 쪽은 None."""
    mark = MARK_SAME if old_pin == new_pin else MARK_CHANGED
    return (None if old_pin is None else mark, None if new_pin is None else mark)


def hint_for(kinds: List[str]) -> str:
    return HINT_TEMPLATE.format(kinds=", ".join(k for k in kinds if k in KINDS) or "unknown")


def refusal_data(old_pin: Any, new_pin: Any) -> Tuple[Dict[str, Any], str]:
    """GROUP_CHANGED 거부 응답의 (data.group_changed, hint). 문자열 값은 전부 고정 어휘·표식·안내 틀에서만 나온다."""
    before, after = display_pair(old_pin, new_pin)
    kinds = changed_kinds(old_pin, new_pin)
    return {"before": before, "after": after, "changed": {"kinds": kinds}}, hint_for(kinds)
