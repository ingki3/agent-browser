"""Jev 판단기 — 맥락을 주고 좁은 질문을 차례로 묻는다. 막히면 폴백 모델로 넘긴다.

실측(2026-09-24, agent_eval 31개 × 3회, artifacts/eval_jev*):
  Jev + qwen3.8-27b 폴백  28/28/28, 31개 약 300~370초 (glm 단독 1,769초)
  한 번에 여러 질문을 섞어 물으면(v1) 25/31 — 행동·대상·값이 서로 어긋났다.

한 스텝:
  Q1 행동 종류   요청 + 페이지 + 한 일 → click / type_text / ... / finish
  Q2 대상        Q1에 맞는 역할의 후보만, 화면 순서로 (후보 1개면 묻지 않음)
  Q3 입력값      요청 문장의 따옴표 값 중 (1개면 묻지 않음)

Jev는 글을 만들지 못한다 — 값은 요청의 따옴표 안에서만 고른다. 없으면 폴백.
'끝났다' 사후 확인은 넣지 않았다: 실측(3회)에서 거짓 완료는 없앴지만 맞는
완료도 막아 '다 해 놓고 못 했다'가 늘었다(사용자 결정, 2026-09-24).

보안: state에는 관찰(요소 이름, 제목, URL)과 read_text 글자가 들어간다. 즉
페이지 내용이 클라우드(Jev)로 간다. 로그인 작업은 이 판단기를 쓰지 않는다
(AgentLoop가 OpenRouter가 아닌 base_url에서는 끈다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from contracts import ObservedElement, ObserveResult
from llm import LLMError

#: 행동 확신이 이보다 낮으면 폴백.
LOW_ACTION_CONFIDENCE = 0.5
#: '끝났다'는 이보다 확신할 때만 받아들인다(거짓 완료가 가장 비싸다).
LOW_FINISH_CONFIDENCE = 0.7
#: 폴백이 한 번 넘겨받으면 다음 N스텝도 폴백이 이어서 판단한다.
#: 실측(hn-comments) — 폴백이 go_back으로 되돌리면 Jev가 같은 링크를 다시 눌러
#: 되돌리기-다시누르기가 반복됐다.
FALLBACK_STICKY_STEPS = 1
#: 이어받기 사유 문구(이 사유로 넘긴 경우는 다시 연장하지 않는다).
_STICKY_REASON = "직전에 폴백이 넘겨받음"

#: 페이지 글자 상한 (Jev 문맥 32K 토큰, 질문 여러 개를 고려해 여유).
PAGE_TEXT_MAX = 3000
HISTORY_TAIL = 6

ACTIONS: Dict[str, str] = {
    "click": "링크나 버튼을 누른다",
    "type_text": "입력칸에 글자를 입력한다",
    "select_option": "드롭다운에서 옵션을 고른다",
    "check_box": "체크박스·라디오 버튼을 선택한다",
    "press_enter": "방금 입력한 칸에서 Enter를 눌러 제출·검색한다",
    "scroll": "아래로 스크롤한다(찾는 요소가 목록에 없음)",
    "read_text": "페이지 글자를 읽어 확인한다",
    "finish": "요청을 이미 다 이뤘다",
}

#: Q2 후보: 행동 종류에 맞는 역할만 보여 준다.
ROLES: Dict[str, frozenset] = {
    "click": frozenset({"link", "button", "menuitem", "tab", "treeitem", "option"}),
    "type_text": frozenset({"textbox", "searchbox", "combobox", "spinbutton"}),
    "select_option": frozenset({"combobox", "listbox"}),
    "check_box": frozenset({"checkbox", "radio", "switch", "menuitemcheckbox"}),
}

_QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"|‘([^’]+)’|“([^”]+)”")


def quoted_values(goal: str) -> List[str]:
    """목표 문장의 따옴표 안 값들(순서 유지, 중복 제거)."""
    out: List[str] = []
    for groups in _QUOTED.findall(goal or ""):
        value = next(g for g in groups if g)
        if value not in out:
            out.append(value)
    return out


def screen_order(elements: Sequence[ObservedElement]) -> List[ObservedElement]:
    """화면 순서(위→아래, 왼쪽→오른쪽). 관찰 목록은 점수 순이라 '첫 번째'를 못 가린다."""
    return sorted(elements, key=lambda e: (e.bbox.y // 8, e.bbox.x))


def element_label(e: ObservedElement, handles: Optional[Dict[str, Any]], url: str) -> str:
    from agent.policy import _href_hint

    hint = ""
    if handles and e.role == "link":
        handle = handles.get(e.element_id)
        cand = _href_hint(getattr(handle, "href", None) if handle else None, url)
        if cand and cand.lower() not in e.name.lower():
            hint = f" -> {cand}"
    value = f" [현재값 '{e.value[:30]}']" if e.value else ""
    off = " (비활성)" if not e.interactable else ""
    return f"{e.role} '{e.name[:60]}'{hint}{value}{off}"


@dataclass
class JevResult:
    """한 스텝 판단 결과.

    decision: 루프가 parse_decision으로 받을 dict. Jev가 답하지 못했으면 None.
    defer_reason: 비어 있지 않으면 폴백 모델에게 넘긴다.
    """

    decision: Optional[Dict[str, Any]]
    defer_reason: str = ""
    action_confidence: float = 0.0
    calls: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    tokens: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)


class JevDecider:
    """`client`는 `ask(state, name, question) -> DecisionAnswer`를 가진 객체."""

    def __init__(self, client: Any, *, limit: int = 20) -> None:
        self.client = client
        self.limit = limit
        self._sticky = 0

    def fallback_used(self, reason: str) -> None:
        """폴백이 이번 스텝을 판단했다 — 다음 스텝도 이어받게 한다(이어받기 연장은 안 함)."""
        if reason != _STICKY_REASON and not reason.startswith(_STICKY_REASON):
            self._sticky = FALLBACK_STICKY_STEPS

    async def _ask(self, res: JevResult, state: Any, name: str, question: Dict[str, Any]):
        ans = await self.client.ask(state, name, question)
        res.calls += 1
        res.latency_ms += ans.latency_ms
        res.cost_usd += ans.cost_usd
        res.tokens += getattr(ans, "input_tokens", 0) or 0
        return ans

    async def decide(
        self,
        goal: str,
        observation: ObserveResult,
        *,
        history: Sequence[str],
        page_text: Optional[str] = None,
        handles: Optional[Dict[str, Any]] = None,
    ) -> JevResult:
        sticky = self._sticky > 0
        self._sticky = max(0, self._sticky - 1)
        res = JevResult(decision=None)
        try:
            await self._decide(res, goal, observation, history, page_text, handles)
        except LLMError as exc:
            res.decision = None
            res.defer_reason = f"Jev 오류: {str(exc)[:80]}"
            return res
        if not res.defer_reason:
            res.defer_reason = self._defer_reason(res, history)
        if not res.defer_reason and sticky:
            res.defer_reason = _STICKY_REASON
        return res

    async def _decide(self, res, goal, observation, history, page_text, handles) -> None:
        url = observation.url
        els = screen_order(observation.elements[: self.limit])
        texts = quoted_values(goal)
        done = list(history[-HISTORY_TAIL:]) or ["(아직 없음)"]
        base: Dict[str, Any] = {
            "request": goal,
            "done_so_far": done,
            "page": {
                "url": url,
                "title": observation.title,
                "elements_in_screen_order": [
                    f"{e.element_id} {element_label(e, handles, url)}" for e in els
                ],
            },
        }
        if page_text is not None:
            base["page"]["visible_text"] = (page_text or "")[:PAGE_TEXT_MAX]

        # Q1 — 행동 종류
        a1 = await self._ask(res, base, "action", {
            "type": "choice",
            "instructions": "요청(request)과 지금까지 한 일(done_so_far)을 보고, "
                            "이 페이지에서 할 다음 행동 종류 하나.",
            "criteria": ACTIONS,
        })
        act = a1.choice or ""
        res.action_confidence = a1.confidence
        res.detail["action"] = act
        reason = f"jev {act} {a1.confidence:.2f}"
        if act not in ACTIONS:
            res.defer_reason = f"알 수 없는 행동 {act!r}"
            return

        decision: Dict[str, Any] = {"action": act, "reason": reason}

        # Q2 — 대상 (그 종류의 후보만)
        if act in ROLES:
            cands = [e for e in els if e.role in ROLES[act]] or els
            if not cands:
                res.defer_reason = "후보 요소 없음"
                return
            if len(cands) == 1:
                target, tconf = cands[0].element_id, 1.0
            else:
                st = {"request": goal, "done_so_far": done,
                      "next_action": ACTIONS[act], "page_title": observation.title}
                a2 = await self._ask(res, st, "target", {
                    "type": "choice",
                    "instructions": f"다음 행동({ACTIONS[act]})의 대상. 요청에 맞는 것 하나. 후보는 화면 순서.",
                    "criteria": {e.element_id: element_label(e, handles, url) for e in cands},
                })
                target, tconf = a2.choice, a2.confidence
            if target not in {e.element_id for e in cands}:
                res.defer_reason = f"후보에 없는 대상 {target!r}"
                return
            decision["element_id"] = target
            res.detail.update(target=target, target_confidence=tconf, candidates=len(cands))

        # Q3 — 입력값 (요청의 따옴표 값 중)
        if act in ("type_text", "select_option"):
            if not texts:
                res.defer_reason = "입력값 없음(Jev는 글을 만들 수 없음)"
                return
            if len(texts) == 1:
                value = texts[0]
            else:
                field_el = next((e for e in els if e.element_id == decision.get("element_id")), None)
                st = {"request": goal, "done_so_far": done,
                      "field": element_label(field_el, handles, url) if field_el else "?"}
                a3 = await self._ask(res, st, "value", {
                    "type": "choice",
                    "instructions": "이 칸(field)에 넣을 값. 이미 입력한 값(done_so_far)은 빼고, 요청 순서대로.",
                    "criteria": {f"t{i}": f"'{t}'" for i, t in enumerate(texts)},
                })
                idx = int(str(a3.choice or "t0")[1:] or 0)
                value = texts[idx] if 0 <= idx < len(texts) else texts[0]
            decision["text" if act == "type_text" else "value"] = value

        if act == "press_enter":
            decision = {"action": "press_key", "key": "Enter", "reason": reason}
        elif act == "scroll":
            decision["direction"] = "down"
        res.decision = decision

    @staticmethod
    def _defer_reason(res: JevResult, history: Sequence[str]) -> str:
        if history and " -> FAIL" in history[-1]:
            return "직전 액션 실패"
        if res.action_confidence < LOW_ACTION_CONFIDENCE:
            return f"행동 확신 낮음 {res.action_confidence:.2f}"
        if res.decision and res.decision.get("action") == "finish" \
                and res.action_confidence < LOW_FINISH_CONFIDENCE:
            return f"완료 확신 낮음 {res.action_confidence:.2f}"
        return ""
