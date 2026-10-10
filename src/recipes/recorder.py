"""궤적 기록 (WS-38 단계 3, 설계 §2).

서버가 세션 중 **성공하고 사후 확인을 통과한** 액션만 최근 MAX_ENTRIES 개 보관한다(메모리).
실패·치유된(healed) 단계·승인 증표로 실행한 단계·사람 조작 구간·지원하지 않는 동작(프레임 안,
shadow 대상, selector·좌표 클릭, 탭·프레임 전환 등)에서 끊는다(오염 방지). 읽기 전용 동작
(관찰·추출·스크린샷·대기)과 스크롤·호버는 넣지도 끊지도 않는다.

각 항목은 **기록 시점** 스냅숏(PageKey: 출처·URL 패턴·골격·준비 수, 대상 기술, Expect)을 담는다 —
저장(save·자동 저장) 때 이것으로 레시피를 만든다. 입력 글자 원문은 메모리에만 있고 저장 때 params 로 바뀐다.

같은 통과 단계를 현재 구간(Segment, WS-38b)에도 넣는다 — 구간이 2단계 이상이면 서비스가 자동 저장한다.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional

from recipes import keys
from recipes.store import RECORDABLE

MAX_ENTRIES = 30

#: 궤적에 넣지도 끊지도 않는 동작.
NEUTRAL = frozenset({"observe_page", "take_screenshot", "extract", "wait_for", "scroll", "hover"})
#: 저장하지 않는 인자(핸들·에포크·확인용 기대값·위치 지정).
DROP_ARGS = frozenset({"element_id", "epoch", "expected_role", "expected_name", "selector", "x", "y",
                       "target_element_id", "trigger_element_id"})


def signal_kinds(signals: Any) -> List[str]:
    """사후 확인 신호 문자열 → 종류(':' 앞). 도달 신호(focus_moved)는 뺀다."""
    out = set()
    for s in signals or []:
        kind = str(s).split(":", 1)[0].strip()
        if kind and kind != "focus_moved":
            out.add(kind)
    return sorted(out)


def nav_kind(before: str, after: str) -> str:
    """none | same | cross | error(이동 뒤 주소가 chrome-error:// 등 http(s) 출처가 아님 — R1 NB-3)."""
    b, a = (before or "").split("#", 1)[0], (after or "").split("#", 1)[0]
    if not a or a == b:
        return "none"
    ob, oa = keys.origin_of(b), keys.origin_of(a)
    if not oa:
        return "error"
    return "same" if ob and ob == oa else "cross"


def build_expect(action: str, before_url: str, after_url: str, data: Dict[str, Any]) -> Dict[str, Any]:
    kinds = signal_kinds(data.get("signals"))
    nav = nav_kind(before_url, after_url)
    if data.get("nav_committed") and nav == "none" and action != "navigate":
        nav = "same"
    if nav != "none" and "navigated" not in kinds:
        kinds = sorted(set(kinds) | {"navigated"})
    return {"nav": nav, "url_pat": keys.url_pattern(after_url) if nav != "none" or action == "navigate"
            else None, "signals": kinds}


class Trajectory:
    """최근 통과 단계(메모리) — 에이전트 save(last_n)용."""

    def __init__(self, maxlen: int = MAX_ENTRIES) -> None:
        self.entries: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self.last_break: str = ""

    def reset(self, why: str) -> None:
        if self.entries:
            self.entries.clear()
        self.last_break = why

    def add(self, entry: Dict[str, Any]) -> None:
        self.entries.append(entry)

    def last(self, n: int) -> List[Dict[str, Any]]:
        if n <= 0 or n > len(self.entries):
            return []
        return list(self.entries)[-n:]


def entry_origin(entry: Dict[str, Any]) -> str:
    """단계의 출처(navigate 는 이동한 곳의 출처)."""
    if entry.get("action") == "navigate":
        return keys.origin_of(str((entry.get("args") or {}).get("url") or ""))
    return str(entry.get("origin") or "")


class Segment:
    """현재 구간(흐름) — 자동 저장 단위(WS-38b).

    새 구간: navigate 단계(그 단계가 첫 단계), 출처가 바뀜, 궤적 끊김(reset), MAX_STEPS 초과.
    stuck 이면 이 구간은 더 저장하지 않는다(비밀 단계 이후·민감 키 이동·컴파일 거부) — 다음 새 구간까지.
    rid/owned: 이 구간이 저장한 레시피 id 와, 그것을 이 구간이 만들었는지(만든 것만 짧은 판 대체·삭제).
    pending: 기존 더 긴 레시피의 앞부분이라 저장을 미룬 판 — 구간이 끝날 때(새 구간·끊김·종료) 저장.
    """

    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []
        self.origin: str = ""
        self.rid: Optional[str] = None
        self.owned: bool = False
        self.stuck: bool = False
        self.announced: bool = False
        #: 저장을 미룬 최신 판(기존 더 긴 레시피의 앞부분과 같은 구조) — 구간이 끝날 때 저장.
        self.pending: Optional[Dict[str, Any]] = None

    def starts_new(self, entry: Dict[str, Any], max_steps: int) -> bool:
        if not self.entries and not self.stuck:
            return True
        return (entry.get("action") == "navigate" or entry_origin(entry) != self.origin
                or len(self.entries) >= max_steps)


def make_entry(action: str, params: Dict[str, Any], pre: Dict[str, Any], after_url: str,
               data: Dict[str, Any], secret: bool) -> Dict[str, Any]:
    """기록 직전 스냅숏(pre) + 결과로 궤적 항목을 만든다."""
    assert action in RECORDABLE
    url = str(pre.get("url") or "")
    return {
        "action": action,
        "args": {k: v for k, v in params.items() if k not in DROP_ARGS and not k.startswith("_")},
        "origin": keys.origin_of(url),
        "url": url,
        "url_pat": keys.url_pattern(url),
        "skel": str(pre.get("skel") or ""),
        "ready": int(pre.get("ready") or 0),
        "desc": pre.get("target"),
        "secret": bool(secret),
        "expect": build_expect(action, url, after_url, data),
    }
