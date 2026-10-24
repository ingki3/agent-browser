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

R14(재검증 B1 — 전각·구분자 변형·계좌 사각) — changed 전용 보수적 숫자 가림(표시 전용이라 과잉 가림 허용, 공유 마스킹은 불변):
* 검사 뷰 — 공유 마스킹 뒤 글자를 NFKC 정규화 + 보이지 않는 글자 제거(줄바꿈은 줄 구분이라 둔다)하고 공유 마스킹을 한 번 더
  (전각 @ 이메일·전각 번호). **표시도 이 뷰 글자 그대로** — 원문/정규화 위치를 오가지 않는다.
* 숫자 사슬 — 숫자 덩어리 사이에 구분자(빈칸·/·-·.·'·'·_·,·: 와 유니코드 대시, 반복·조합)만 있으면 한 사슬. 숫자 합이 6자리 이상이면
  사슬 전체를 [REDACTED](전화·카드·주민·계좌·사업자번호; 날짜·긴 가격 과잉 가림 허용). 바깥 마지막 줄과 순서열 첫 항목을
  이은 것에서 고른다(경계 걸침). 비숫자 글자(만·원·요일 등)가 사이에 있으면 끊긴다 — 하나투어 '19 148만 20 127만' 은 남는다.
* 잔여 — 창(보일 80자)에 걸치는 숫자 사슬이 4자리 이상(구분자 제외 이음, 창에 잘린 사슬은 전체 자릿수)이면 그 쌍 생략. 예외는 4~5자리 바로 뒤에 고정 단위 글자
  (만·원·일·월·년·명·개·박·시·분)가 붙은 것뿐(하나투어 '1138만'). [REDACTED] 바로 옆(구분자 사이 포함)의 4자리 이상은 예외 없이
  불확실 — 그 부분 생략(조정자 결정, R14).
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
#: R14 숫자 사슬 구분자 — 빈칸·/·-·.·'·'·_·,·: 와 유니코드 대시(Pd: en/em dash·‐ 등 — NFKC 가 바꾸지 않는다)·가운뎃점류.
_SEP_CHARS = "/\\-.\u00b7\u30fb\u2027\u2022_,:" + "".join(
    re.escape(chr(c)) for c in range(0x2000, 0x2100) if unicodedata.category(chr(c)) == "Pd") + "\u2212\ufe58\ufe63"
_SEP = "[\\s" + _SEP_CHARS + "]"
#: 숫자 덩어리 사슬(구분자만 사이에 낀 숫자들). 앞뒤 구분자는 사슬에 넣지 않는다.
_CHAIN = re.compile(r"\d+(?:" + _SEP + r"+\d+)*")
#: 사슬 숫자 합이 이 이상이면 사슬 전체를 가린다.
_CHAIN_DIGITS = 6
#: 잔여 검사 — 4자리 이상 이음 숫자가 남으면 생략, 예외(4~5자리 바로 뒤 고정 단위 글자).
_RESIDUAL_DIGITS = 4
_UNITS = frozenset("만원일월년명개박시분")
_REDACTED = "[REDACTED]"
#: [REDACTED] 바로 옆(구분자 사이 포함)의 4자리 이상 숫자.
_NEAR_MASK = re.compile(re.escape(_REDACTED) + _SEP + r"*\d{4}|\d{4}" + _SEP + "*" + re.escape(_REDACTED))


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


def _view(masked: str) -> str:
    """R14 검사 뷰 = 표시 글자: NFKC + 보이지 않는 글자 제거(줄바꿈은 둠) + 줄마다 정리 + 공유 마스킹 한 번 더(전각 @·번호)."""
    t = unicodedata.normalize("NFKC", masked)
    t = "".join(ch for ch in t if ch == "\n" or unicodedata.category(ch) not in _HIDDEN)
    return "\n".join(_mask(_norm(line)) for line in t.split("\n"))


def _digits(chain: str) -> int:
    return sum(ch.isdigit() for ch in chain)


def _redact_chains(text: str) -> str:
    """숫자 합 6자리 이상 사슬을 통째로 [REDACTED] — 사슬 안 줄바꿈 수는 둔다(바깥 줄 나눔 유지)."""
    return _CHAIN.sub(lambda m: (_REDACTED + "\n" * m.group(0).count("\n")) if _digits(m.group(0)) >= _CHAIN_DIGITS
                      else m.group(0), text)


def _near_mask(text: str) -> bool:
    """[REDACTED] 바로 옆(구분자 사이 포함)에 4자리 이상 숫자가 붙어 있는가(예외 없이 불확실 — 가린 번호의 덜 가린 끝)."""
    return bool(_NEAR_MASK.search(text))


def _residual_digits(text: str, start: int) -> bool:
    """창(`start` 부터 80자)에 걸치는 숫자 사슬 중 4자리 이상 이음 숫자가 남았는가 — 창 끝에 잘려 일부만 보이는 사슬도 사슬 전체
    자릿수로 본다. 4~5자리 바로 뒤 고정 단위 글자만 예외(하나투어 '1138만')."""
    end = start + CHANGED_CHARS
    for m in _CHAIN.finditer(text):
        if m.end() <= start or m.start() >= end:
            continue
        n = _digits(m.group(0))
        if n < _RESIDUAL_DIGITS:
            continue
        if n < _CHAIN_DIGITS and text[m.end():m.end() + 1] in _UNITS:
            continue
        return True
    return False


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
    """마스킹·살균을 끝낸 두 글자 — 실제 첫 문자 차이 앞 `_LEAD` 자부터 각 80자. 같으면 None(그 쌍 생략).
    R14 잔여 검사에 걸려도 None(그 쌍 생략)."""
    if before == after:
        return None
    start = _start(before, after)
    out = {"before": _cut(before, start), "after": _cut(after, start)}
    if out["before"] == out["after"] or _residual_digits(before, start) or _residual_digits(after, start):
        return None
    return out


def _start(before: str, after: str) -> int:
    p = 0
    while p < min(len(before), len(after)) and before[p] == after[p]:
        p += 1
    return max(0, p - _LEAD)


def _side(pin: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """한 쪽 지문 → 마스킹·검사 뷰·사슬 가림을 끝낸 바깥 줄들·순서열 이음 문자열과 각 불확실 여부. 마스킹 실패면 None."""
    outer_raw = "\n".join(_norm(line) for line in pin["outer"])
    seq_raw = _norm(" ".join(_token(t) for t in pin["seq"][2:].split("\x01")))
    try:
        outer_v, seq_v = _view(_mask(outer_raw)), _view(_mask(seq_raw))
        # 사슬은 바깥 마지막 줄과 순서열 첫 항목을 이은 것에서 고른다(경계 걸침) — 줄 수는 _redact_chains 가 둔다.
        joint = _redact_chains(outer_v + "\n" + seq_v) if outer_raw else _redact_chains(seq_v)
        outer, seq = (joint.rsplit("\n", 1) if outer_raw else ("", joint))
        return {"outer": outer.split("\n") if outer_raw else [], "seq": seq,
                "outer_unsure": _residual_pii(outer) or _near_mask(outer),
                "seq_unsure": _residual_pii(seq) or _near_mask(seq),
                # 바깥 마지막 줄과 순서열 첫 항목에 걸친 PII — 가린 둘을 이어 다시 본다.
                "joint_unsure": bool(outer_raw) and (_residual_pii(outer + " " + seq) or _near_mask(outer + " " + seq))}
    except Exception:  # noqa: BLE001 — 표시 전용: 마스킹이 실패하면 싣지 않는다(fail-closed)
        return None


def changed_parts(old_pin: Any, new_pin: Any) -> Optional[Dict[str, Any]]:
    """옛·지금 지문 → {text: [{before, after}, ≤3], items?: {before, after}, position?: {before, after}}. 분해 불가면 None.

    text = 바깥 글자 줄의 바뀐 부분(줄 단위 차이, 실제 첫 문자 차이 조금 앞부터 80자). items = 항목 순서열이 바뀌었을 때
    이음 문자열의 첫 문자 차이 조금 앞부터(접두 뗌). position = 대상 항목 순번(#k)이 바뀌었을 때. 마스킹 실패·바깥 또는
    바깥/순서열에 걸친 잔여 PII 면 None, 순서열에만 잔여 PII 면 items 생략. 마스킹 뒤 앞/뒤가 같은 쌍은 생략.
    """
    try:
        return _changed_parts(old_pin, new_pin)
    except Exception:  # noqa: BLE001 — 표시 전용: 어느 단계든 실패하면 싣지 않는다(fail-closed, 거부 응답은 그대로)
        return None


def _changed_parts(old_pin: Any, new_pin: Any) -> Optional[Dict[str, Any]]:
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
