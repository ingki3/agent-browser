"""WS-51 R4 공용 — 누출 판정(§1.4)과 행렬 축(§4). 개발·검증 공통 기준: `representations(v)`.

* 누출: 출구 문자열을 디코드한 결과(wire 는 json.loads 후 모든 문자열 리프·키, 로그는 원문)에 representations(v) 중
  하나가 연속 부분 문자열로 있음. 긴 값(≥ 256자)은 원문과 연속 256자 이상 같은 조각이 남으면 누출.
* 값은 모두 합성 값.
"""

from __future__ import annotations

import json
import random
import string
from typing import Any, Iterator, List

from security.secret_registry import representations

LONG_RUN = 256
BLOCK = 128


def _rand(n: int, seed: int) -> str:
    rnd = random.Random(seed)
    return "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(n))


#: S 대표 값 8종(§4) — 이름 → 값. tool_vocab 은 등록 제외 확인용(누출 판정 대상이 아님).
VALUES = {
    "alnum8": "SYNTHr4Alnum88",
    "special": 'SYNTH_R4_\u00e9<&"\\+ /9',
    "unicode": "SYNTH\uc720\ub2c8\ucf54\ub4dc\ube44\ubc00\u00fc",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJTWU5USHI0In0.U1lOVEhyNHNpZ25hdHVyZQ",
    "long64k": "SYNTH" + _rand(65_600, 51),
    "short_nonalpha": "Zq7-k2",
    "tool_vocab": "navigate",
    "a_repeat": "a" * 10,
}
LEAK_VALUES = [k for k in VALUES if k != "tool_vocab"]


def strings(obj: Any) -> Iterator[str]:
    """json.loads 결과의 모든 문자열 리프와 dict 키."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)


def _long_run(text: str, value: str) -> bool:
    """value 와 연속 LONG_RUN 자 이상 같은 조각이 text 에 있는가(정렬 블록 출현마다 양쪽으로 넓혀 잰다)."""
    if len(value) < LONG_RUN or len(text) < LONG_RUN:
        return False
    for off in range(0, len(value) - BLOCK + 1, BLOCK):
        block = value[off:off + BLOCK]
        i = text.find(block)
        while i >= 0:
            s, vs = i, off
            while s > 0 and vs > 0 and text[s - 1] == value[vs - 1]:
                s, vs = s - 1, vs - 1
            e, ve = i + BLOCK, off + BLOCK
            while e < len(text) and ve < len(value) and text[e] == value[ve]:
                e, ve = e + 1, ve + 1
            if e - s >= LONG_RUN:
                return True
            i = text.find(block, i + 1)
    return False


def leaks_in_text(text: str, value: str) -> List[str]:
    found = [r for r in representations(value) if r in text]
    if not found and _long_run(text, value):
        found.append(f"<원문 연속 {LONG_RUN}자 이상>")
    return [f if len(f) < 80 else f[:40] + "…" for f in found]


def leaks_in_wire(wire: str, value: str) -> List[str]:
    out: List[str] = []
    for leaf in strings(json.loads(wire)):
        out += leaks_in_text(leaf, value)
    return out
