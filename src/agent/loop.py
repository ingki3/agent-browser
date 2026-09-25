"""자율 에이전트 루프 (WS-8).

    관찰(키워드 주입) -> LLM 판단 -> 액션 -> 사후검증 -> 반복

종료 조건 (먼저 도달하는 것):
* LLM이 `finish` / `give_up` 반환
* 스텝 상한 (기본 30, PRD §3.4)
* 예산 상한 ($0.75 / 100,000토큰) — `BudgetGuard`가 강제 차단
* 연속 실패 임계 초과

설계상 중요한 점:

**키워드를 매 스텝 주입한다.** 실환경 검증에서 이것이 성공/실패를
갈랐다. 위키백과에서 키워드 없이 관찰하면 'Log in'이 41위로 밀려
Top-20에 들지 못하고, LLM은 정확하게 "필요한 요소가 없다"고 답한다.
관찰이 정답을 넘겨주지 않으면 어떤 모델을 써도 실패한다.

**LLM 응답을 신뢰하지 않는다.** 존재하지 않는 element_id를 반환하는
경우가 있으므로 디스패치 전에 관찰 결과와 대조한다. 잘못된 id로
디스패치하면 스텝과 비용만 낭비된다.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from browser.challenge import Challenge, detect_challenge
from contracts import ActionResult, ActionType, BBox, ErrorCode, ObserveResult
from contracts.thresholds import (
    MAX_WALL_CLOCK_SECONDS,
    TIER2_MAX_CALLS_INTERACTIVE,
    TIER2_MAX_CALLS_UNATTENDED,
)
from llm import BudgetExceeded, BudgetGuard, LLMError, OpenRouterClient
from llm.config import OPENROUTER_BASE_URL, LLMConfig
from llm.decisions import DecisionsClient

from agent.keywords import keywords_for_step
from agent.policy import (
    FINISH,
    GIVE_UP,
    Decision,
    build_messages,
    decision_to_params,
    parse_decision,
)

logger = logging.getLogger(__name__)

#: reasoning 계열 모델은 content 앞에 사고 토큰을 소비한다. 실측상
#: 위키백과 규모 프롬프트에서 512로는 본문이 잘렸다.
#:
#: 멀티스텝 태스크는 히스토리가 누적되어 프롬프트와 사고량이 함께 늘어난다.
#: 실측 — TodoMVC 3스텝 태스크에서 2048이 소진되어 루프가 give_up으로
#: 조기 종료됐다. 실패가 아니라 예산 부족이었다.
#:
#: 23태스크 측정에서 스텝당 실사용은 448~2,880토큰이었다(dyn-loading-wait
#: 최대). 4096에서도 소진이 2건 발생했고, 둘 다 **이미 목표를 달성한 뒤**
#: 종료 선언을 못 해 silent_win으로 집계됐다.
#:
#: 25태스크 측정에서는 8192도 2건 소진됐다. 두 태스크 모두 재실행 시
#: 성공하므로(2/2) 특정 스텝에서 사고량이 튀는 모델 특성이다.
#: 소진되면 그 스텝의 작업이 통째로 버려지므로 상한을 넉넉히 둔다.
#:
#: 주의: 상한을 늘려도 **평상시 토큰 사용량은 늘지 않는다.** 모델이
#: 실제 생성한 만큼만 과금되며, 이 값은 절단 지점일 뿐이다. 다만 폭주
#: 스텝에서는 그만큼 더 소비하므로 태스크 예산(BudgetGuard)이 최종
#: 방어선 역할을 한다.
DEFAULT_MAX_TOKENS = 32768

#: max_tokens **소진 후 더 큰 값으로 재시도**하는 방식은 기각됐다.
#: 실측 — 8192 소진 후 3배(24,576)로 재호출하니 모델이 더 긴 사고를
#: 이어가 한 태스크에 1,067초/$0.0089를 쓰고도 실패했다.
#: 같은 프롬프트를 두 번 태우는 것이 문제이지, 상한값 자체가 문제는
#: 아니다. 처음부터 넉넉히 주는 것(위 32768)과는 다른 이야기다.

#: 연속 실패 허용 횟수. 초과하면 루프를 끊는다. 같은 실패를 반복하며
#: 예산만 소진하는 상황을 막는다.
MAX_CONSECUTIVE_FAILURES = 3

#: 네비게이션 안정화 대기. 액션이 페이지 전환을 유발하면 실행 컨텍스트가
#: 교체되어 관찰이 실패하므로, 전환 완료를 기다린 뒤 재관찰한다.
SETTLE_TIMEOUT_MS = 8000
SETTLE_EXTRA_MS = 600

#: Tier-2 발동 조건 — 같은 목표에 대한 **무해하지 않은** 연속 실패 횟수
#: (PRD §3.1 사다리: Tier-1 2회 실패 -> SoM). 구식 참조 실패(WS-18)는
#: 직전 성공의 부산물이므로 세지 않는다.
TIER2_TRIGGER_FAILURES = 2

#: Tier-2에서 element_id를 바꿔 그대로 재실행할 수 있는 액션. 그 밖의
#: 액션(navigate, scroll 등)이 마지막 실패였다면 클릭으로 대체한다 —
#: 시각 폴백이 답할 수 있는 것은 "어느 요소인가"뿐이기 때문이다.
_TIER2_REPLAYABLE = frozenset(
    {
        ActionType.CLICK,
        ActionType.TYPE_TEXT,
        ActionType.HOVER,
        ActionType.SELECT_OPTION,
        ActionType.CHECK_BOX,
        ActionType.PRESS_KEY,
    }
)


@dataclass
class StepOutcome:
    """단일 스텝 실행 기록."""

    step: int
    decision: Decision
    result: Optional[ActionResult] = None
    observed: int = 0
    latency_ms: float = 0.0
    llm_tokens: int = 0
    llm_cost: float = 0.0
    note: str = ""
    #: Tier-2 VLM 왕복 지연 (Gate 4 p95 재료). Tier-1 스텝은 0.
    vision_latency_ms: float = 0.0
    #: 수용된 `request_vision`처럼 액션을 실행하지 않고도 성공인 판단 스텝.
    #: 실측(live 50런) — 이 표기가 없으면 요청 스텝이 FAIL로 히스토리에 남아
    #: LLM이 "요청이 실패했다"고 읽고 재요청, Tier-2 상한을 소진했다.
    judged_success: bool = False
    #: 판단 주체: "llm"(채팅 모델) | "jev" | "fallback"(Jev가 넘긴 폴백 모델).
    decided_by: str = "llm"
    #: Jev가 폴백에게 넘긴 이유(넘기지 않았으면 빈 값).
    defer_reason: str = ""

    @property
    def succeeded(self) -> bool:
        if self.decision.is_terminal:
            return self.decision.action == FINISH
        if self.judged_success:
            return True
        return bool(self.result and self.result.success)

    def summary(self) -> str:
        target = f" {self.decision.element_id}" if self.decision.element_id else ""
        mark = "OK" if self.succeeded else "FAIL"
        detail = self.note or (
            self.result.error_code.value
            if self.result and self.result.error_code
            else ""
        )
        line = f"{self.decision.action}{target} -> {mark} {detail}".strip()
        # 입력값·키를 남긴다. 실측(2026-09-24 todo-add-two) — 값 없이
        # 'type_text @e5 -> OK'만 남기면 판단기가 이미 넣은 값을 몰라 같은
        # 입력을 8번 반복했다. 값은 목표 문장이나 자격증명 키 이름에서 온 것
        # (치환 전)이라 새 정보를 흘리지 않는다.
        extra = []
        if self.decision.text:
            extra.append(f"'{self.decision.text[:60]}' 입력")
        if self.decision.value:
            extra.append(f"'{self.decision.value[:60]}' 선택")
        if self.decision.key:
            extra.append(f"키 {self.decision.key}")
        return line + (f" ({', '.join(extra)})" if extra else "")


@dataclass
class TaskRun:
    """태스크 실행 전체 결과."""

    goal: str
    completed: bool = False
    terminal_reason: str = ""
    steps: List[StepOutcome] = field(default_factory=list)
    budget: Dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    final_url: str = ""
    #: Tier-2 SoM 발동 횟수 (PRD §3.4 태스크당 상한 대상)
    tier2_calls: int = 0
    #: 차단/캡차 화면을 만났으면 그 종류("captcha" | "blocked"). 사람이 해결해
    #: 계속 진행했더라도 남는다(WS-22).
    challenge: Optional[str] = None

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def action_success_rate(self) -> float:
        real = [s for s in self.steps if not s.decision.is_terminal]
        if not real:
            return 0.0
        return sum(1 for s in real if s.succeeded) / len(real)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "completed": self.completed,
            "terminal_reason": self.terminal_reason,
            "steps": self.step_count,
            "action_success_rate": round(self.action_success_rate, 4),
            "elapsed_s": round(self.elapsed_s, 2),
            "final_url": self.final_url,
            "budget": self.budget,
            "tier2_calls": self.tier2_calls,
            "challenge": self.challenge,
            "trace": [s.summary() for s in self.steps],
        }


class HandoffOutcome(str, Enum):
    """사람 인계 훅(on_challenge)의 결과 (WS-22 R2).

    * UNRESOLVED — 시간 초과·신호 없음(False 와 같다). 끝낸다.
    * RESOLVED — 사람이 해결했다고 알림(True 와 같다). 믿지 않고 다시 본다.
    * FORCE — 사람이 "그냥 계속" 을 지시(오탐). 재확인 결과와 무관하게 진행하고,
      그 실행 동안 같은 판정(kind+vendor+reason)은 다시 인계하지 않는다.
    """

    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    FORCE = "force"


def normalize_handoff(value: Any) -> HandoffOutcome:
    """훅 반환값을 HandoffOutcome 으로. 기존 계약(bool)도 받는다."""
    if isinstance(value, HandoffOutcome):
        return value
    if isinstance(value, str):
        try:
            return HandoffOutcome(value.strip().lower())
        except ValueError:
            return HandoffOutcome.UNRESOLVED
    return HandoffOutcome.RESOLVED if value is True else HandoffOutcome.UNRESOLVED


def _challenge_key(ch: Challenge) -> Tuple[str, str, str]:
    return (ch.kind.value, ch.vendor, ch.reason)


class AgentLoop:
    """관찰-판단-액션 루프.

    브라우저/엔진/디스패처는 외부에서 주입받는다. 루프가 직접 브라우저를
    띄우면 테스트에서 격리가 불가능하다.
    """

    def __init__(
        self,
        *,
        page: Any,
        engine: Any,
        dispatcher: Any,
        config: Optional[LLMConfig] = None,
        budget: Optional[BudgetGuard] = None,
        max_steps: int = 0,
        top_n: int = 20,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        som_enabled: bool = False,
        unattended: bool = True,
        grounder: Optional[Callable[..., Any]] = None,
        on_challenge: Optional[
            Callable[[Challenge], Awaitable[Union[bool, HandoffOutcome]]]
        ] = None,
    ) -> None:
        self.page = page
        self.engine = engine
        self.dispatcher = dispatcher
        self.config = config
        self.budget = budget or BudgetGuard()
        self.max_steps = max_steps or self.budget.max_steps
        self.top_n = top_n
        self.max_tokens = max_tokens
        # Tier-2 SoM (PRD §3.1). 기본 off — 꺼져 있으면 아래 경로는 전혀
        # 타지 않으며 기존 동작과 동일하다. `grounder`는 테스트 주입용이며
        # 기본값은 `vision.grounder.ground`(지연 import).
        self.som_enabled = som_enabled
        self.unattended = unattended
        self._grounder = grounder
        #: read_text 결과 — 다음 스텝 프롬프트에 **한 번만** 넣고 비운다.
        self._pending_page_text: Optional[str] = None
        #: decider="jev"일 때 run()이 만든다.
        self._jev: Any = None
        self._jev_client: Any = None
        #: 차단/캡차 화면을 만나면 사람에게 넘기는 훅(WS-22). True/RESOLVED = 사람이
        #: 해결했다고 알림, FORCE = 오탐이니 그냥 계속. 자체 타임아웃을 가져야 한다
        #: (루프 벽시계 상한도 적용).
        self.on_challenge = on_challenge
        #: 사람이 강제 계속을 지시한 판정(kind, vendor, reason) — 그 실행 동안 재인계 안 함.
        self._forced: Set[Tuple[str, str, str]] = set()
        #: 마지막 메인 프레임 문서 응답의 HTTP 상태(차단 판정 보조).
        self._last_status: Optional[int] = None

    def _on_response(self, response: Any) -> None:
        """메인 프레임 문서 응답의 상태코드만 기억한다(본문·헤더는 읽지 않는다)."""
        try:
            frame = response.frame
            if response.request.is_navigation_request() and frame.parent_frame is None:
                self._last_status = response.status
        except Exception:  # noqa: BLE001
            pass

    async def _check_challenge(self, run: TaskRun, started: float) -> bool:
        """차단/캡차 화면이면 사람에게 넘기고, 끝내야 하면 True.

        LLM을 부르기 전에 본다 — 실측(네이버 쇼핑·쿠팡)에서 차단 화면을 보고
        scroll/read_text를 7~9번 헛돌다 포기했다. 캡차를 풀거나 우회하지 않는다.
        """
        active = getattr(self.dispatcher, "ctx", None)
        page = getattr(active, "page", None) or self.page
        found = await detect_challenge(page, last_status=self._last_status)
        if not found.detected:
            return False
        run.challenge = found.kind.value
        if _challenge_key(found) in self._forced:
            # 사람이 이 판정을 오탐이라며 강제 계속을 지시했다 — 다시 넘기지 않는다.
            return False
        logger.info("차단/캡차 감지: %s (%s) — %s", found.kind.value, found.vendor, found.reason)
        if self.on_challenge is not None:
            remaining = MAX_WALL_CLOCK_SECONDS - (time.perf_counter() - started)
            try:
                outcome = normalize_handoff(await asyncio.wait_for(
                    self.on_challenge(found), timeout=max(remaining, 0.0)
                ))
            except Exception:  # noqa: BLE001 - 타임아웃·훅 오류는 미해결로 본다
                outcome = HandoffOutcome.UNRESOLVED
            if outcome is HandoffOutcome.FORCE:
                # 사람이 명시적으로 "그냥 계속" — 판정기를 느슨하게 하는 대신 이 판정만 넘긴다.
                logger.info("사람 강제 계속: %s (%s) — %s", found.kind.value, found.vendor,
                            found.reason)
                self._forced.add(_challenge_key(found))
                self._last_status = None
                return False
            if outcome is HandoffOutcome.RESOLVED:
                # 사람이 해결했다고 알렸다 — 믿지 않고 다시 본다.
                self._last_status = None
                again = await detect_challenge(page, last_status=None)
                if not again.detected:
                    return False
                found = again
        run.terminal_reason = (
            f"{ErrorCode.CAPTCHA_DETECTED.value}: {found.kind.value} — {found.reason}"
        )
        return True

    def _jev_enabled(self) -> bool:
        """Jev 판단기를 쓸지. OpenRouter base_url에서만 켠다.

        로컬 base_url(예: mlx 127.0.0.1)은 로그인 작업용이다 — 로그인 뒤 화면
        (메일 제목 등)이 클라우드 모델(Jev)로 가면 안 된다. 설정을 잘못해도
        페이지 내용이 새지 않게 여기서 막는다.
        """
        cfg = self.config
        if cfg is None or getattr(cfg, "decider", "llm") != "jev":
            return False
        return (cfg.base_url or "").rstrip("/") == OPENROUTER_BASE_URL.rstrip("/")

    @property
    def tier2_max_calls(self) -> int:
        return TIER2_MAX_CALLS_UNATTENDED if self.unattended else TIER2_MAX_CALLS_INTERACTIVE

    async def run(self, goal: str) -> TaskRun:
        """목표를 달성할 때까지 루프를 돌린다."""
        run = TaskRun(goal=goal)
        started = time.perf_counter()
        history: List[str] = []
        failures: List[str] = []
        consecutive_failures = 0

        client = OpenRouterClient(self.config, self.budget)
        try:
            await client.__aenter__()
        except Exception as exc:  # noqa: BLE001
            run.terminal_reason = f"LLM 클라이언트 초기화 실패: {exc}"
            run.elapsed_s = time.perf_counter() - started
            run.budget = self.budget.snapshot()
            return run

        self._jev = None
        self._jev_client = None
        if self._jev_enabled():
            from agent.jev_decider import JevDecider

            self._jev_client = DecisionsClient(self.config, self.budget)
            await self._jev_client.start()
            self._jev = JevDecider(self._jev_client, limit=self.top_n)

        self._last_status = None
        self._forced = set()
        listened = self.page if callable(getattr(self.page, "on", None)) else None
        if listened is not None:
            listened.on("response", self._on_response)

        try:
            # 실패 직후 완료 선언 가드 상태 (WS-18 정밀화).
            # last_action_failed: 직전에 실행한 실제 액션의 성공 여부
            # last_failure_harmless: 그 실패가 '구식 참조 실패'인가
            # finish_rejected: 이미 한 번 거부했는지 (무한 루프 방지)
            # last_success_element: 마지막으로 성공한 액션의 대상 요소
            last_action_failed = False
            last_failure_harmless = False
            finish_rejected = False
            last_success_element: Optional[str] = None
            # Tier-2 발동 판정용 — 무해하지 않은 연속 실패 수와 마지막 실패 판단.
            tier2_pressure = 0
            last_failed_decision: Optional[Decision] = None
            #: LLM이 직전 스텝에서 request_vision을 골랐으면 그 Decision.
            vision_requested: Optional[Decision] = None

            for step in range(1, self.max_steps + 1):
                # PRD 실행 시간 상한 — 태스크당 Wall-Clock 10분.
                # 계약에 MAX_WALL_CLOCK_SECONDS가 정의돼 있는데 루프가
                # 이를 강제하지 않아, 네트워크 대기로 멈추면 무한정
                # 매달렸다. 실측 — internet-checkbox-both가 13분 넘게
                # CPU 0.1%로 정지해 통합 측정 전체를 막았다.
                #
                # 스텝 수와 예산만으로는 못 막는다. 한 스텝 안에서
                # 멈추면 스텝 카운터가 올라가지 않기 때문이다.
                elapsed = time.perf_counter() - started
                if elapsed >= MAX_WALL_CLOCK_SECONDS:
                    run.terminal_reason = (
                        f"실행 시간 상한 초과: {elapsed:.0f}초 "
                        f"(상한 {MAX_WALL_CLOCK_SECONDS}초)"
                    )
                    break

                # 차단/캡차 화면(WS-22) — LLM을 부르기 전에 본다. 매 스텝
                # evaluate 1회(수 ms)라 URL 변화 여부와 무관하게 확인한다
                # (SPA는 URL 없이 캡차를 띄우기도 한다).
                if await self._check_challenge(run, started):
                    break

                try:
                    self.budget.begin_step()
                except BudgetExceeded as exc:
                    run.terminal_reason = str(exc)
                    break

                # 두 발동 경로 (PRD §3.1 + 실측 보강):
                #  (1) 무해하지 않은 연속 실패 2회 — 원안.
                #  (2) LLM의 자발 요청(request_vision) — 성공-but-헛수고
                #      (라벨 없는 아이콘을 순서대로 다 눌러보는) 양상은
                #      (1)로는 영원히 안 잡힌다. 실측 — icon-buttons live.
                escalate = self.som_enabled and (
                    (tier2_pressure >= TIER2_TRIGGER_FAILURES and last_failed_decision is not None)
                    or vision_requested is not None
                )
                if escalate:
                    # 태스크당 상한 (PRD §3.4). 무인 3회 / 대화형 5회.
                    if run.tier2_calls >= self.tier2_max_calls:
                        run.terminal_reason = (
                            f"{ErrorCode.TIER2_BUDGET_EXCEEDED.value}: Tier-2 SoM "
                            f"호출 상한({self.tier2_max_calls}회) 초과"
                        )
                        break
                    run.tier2_calls += 1
                    # 자발 요청이면 재실행할 실패 액션이 없다 -> CLICK 기본.
                    replay = last_failed_decision or Decision(
                        action=ActionType.CLICK.value, reason=vision_requested.reason
                    )
                    vision_requested = None
                    outcome = await self._tier2_step(
                        client, goal, step, failures, replay
                    )
                else:
                    outcome = await self._run_step(
                        client, goal, step, history, failures
                    )
                run.steps.append(outcome)

                if outcome.decision.action == FINISH:
                    # 실패 직후 완료 선언 가드 (WS-15 도입, WS-18 정밀화).
                    #
                    # 원조 사례(hn-comments, WS-15 이전) — click이 E_TIMEOUT
                    # 으로 실패한 직후 finish를 선언했고 독립 검증에서
                    # false_claim으로 잡혔다. 이를 애초에 막는 것이 목적이다.
                    #
                    # 그러나 실측(melon-chart, naver-search)에서 가드가
                    # **진짜 성공을 두 번 막았다.** 두 사례 모두:
                    #   같은 요소에 성공 -> 같은 요소에 E_ELEMENT_NOT_FOUND
                    #   -> finish
                    # 직전 성공이 페이지를 이동시켜 옛 요소가 사라진 것이다.
                    # 실패는 성공의 부산물이지 목표 미달성의 증거가 아니다.
                    #
                    # 정밀화 두 가지:
                    # (1) 무해한 실패는 가드를 발동하지 않는다.
                    #     구식 참조 실패(ELEMENT_NOT_FOUND/TOCTOU_MISMATCH)가
                    #     '직전에 성공한 바로 그 요소'에 대해 났다면, 액션이
                    #     시작조차 못 한 것이므로 페이지를 나쁘게 만들 수
                    #     없다. E_TIMEOUT 등 효과 실패는 여전히 발동한다.
                    # (2) 재확인 후의 재선언은 수용한다.
                    #     1차 거부는 새 관찰을 강제하는 장치다. 새 관찰을
                    #     받고도 완료를 주장하면 그 판단을 존중한다. 목표
                    #     달성 여부는 루프가 알 수 없고, 최종 판정은 독립
                    #     검증(하네스)/호출자의 몫이다. 이때 자기 보고가
                    #     실패 후 재확인을 거쳤음을 terminal_reason에 남긴다.
                    guard_active = last_action_failed and not last_failure_harmless
                    if guard_active and not finish_rejected:
                        finish_rejected = True
                        history.append(
                            "[시스템] 직전 액션이 실패했는데 목표 달성을 "
                            "선언했습니다. 실패한 액션은 아무 효과가 없습니다. "
                            "페이지를 다시 관찰하고 목표가 정말 달성됐는지 "
                            "확인하십시오. 달성되지 않았다면 다른 방법을 "
                            "시도하십시오."
                        )
                        continue

                    run.completed = True
                    if finish_rejected:
                        run.terminal_reason = (
                            "LLM이 재확인 후 목표 달성을 선언했습니다 "
                            "(직전 실패로 1차 거부됨)."
                        )
                    elif last_action_failed:
                        run.terminal_reason = (
                            "LLM이 목표 달성을 선언했습니다 "
                            "(직전 실패는 구식 참조 — 무해)."
                        )
                    else:
                        run.terminal_reason = "LLM이 목표 달성을 선언했습니다."
                    break
                if outcome.decision.action == GIVE_UP:
                    run.terminal_reason = f"LLM 포기: {outcome.decision.reason}"
                    break

                if outcome.decision.is_vision_request and self.som_enabled:
                    # 다음 스텝에서 Tier-2로 간다. 실패로 세지 않는다 —
                    # 판단이지 액션이 아니다.
                    vision_requested = outcome.decision
                    history.append(outcome.summary())
                    continue

                if outcome.succeeded:
                    consecutive_failures = 0
                    tier2_pressure = 0
                    last_failed_decision = None
                    last_action_failed = False
                    last_failure_harmless = False
                    # 무해 판정용 — 이 요소에 대한 이후의 구식 참조 실패는
                    # 이 성공의 부산물일 가능성이 높다.
                    last_success_element = outcome.decision.element_id
                    history.append(outcome.summary())
                else:
                    consecutive_failures += 1
                    last_action_failed = True
                    # 구식 참조 실패 판정 (WS-18).
                    # ELEMENT_NOT_FOUND/TOCTOU_MISMATCH는 액션이 시작조차
                    # 못 한 실패다. 그것이 '직전에 성공한 바로 그 요소'에
                    # 대해 났다면, 직전 성공이 페이지를 바꿔 참조가 낡은
                    # 것이므로 목표 미달성의 증거가 아니다.
                    # 실측 — melon-chart(@e2 성공 -> @e2 NOT_FOUND),
                    #        naver-search(@e3 성공 -> @e3 NOT_FOUND).
                    error_code = (
                        outcome.result.error_code
                        if outcome.result and outcome.result.error_code
                        else None
                    )
                    is_stale_ref = error_code in (
                        ErrorCode.ELEMENT_NOT_FOUND,
                        ErrorCode.TOCTOU_MISMATCH,
                    )
                    same_element = (
                        outcome.decision.element_id is not None
                        and outcome.decision.element_id == last_success_element
                    )
                    last_failure_harmless = is_stale_ref and same_element
                    if not last_failure_harmless:
                        tier2_pressure += 1
                        last_failed_decision = outcome.decision
                    failures.append(outcome.summary())
                    history.append(outcome.summary())
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        run.terminal_reason = (
                            f"연속 {consecutive_failures}회 실패로 중단"
                        )
                        break
            else:
                run.terminal_reason = f"스텝 상한({self.max_steps}) 도달"
        except BudgetExceeded as exc:
            run.terminal_reason = str(exc)
        finally:
            if listened is not None:
                try:
                    listened.remove_listener("response", self._on_response)
                except Exception:  # noqa: BLE001
                    pass
            await client.close()
            if self._jev_client is not None:
                await self._jev_client.close()

        run.elapsed_s = time.perf_counter() - started
        run.budget = self.budget.snapshot()
        try:
            run.final_url = self.page.url
        except Exception:  # noqa: BLE001
            run.final_url = ""
        return run

    def _give_up_step(
        self, step: int, observation: Any, started: float, exc: Exception
    ) -> StepOutcome:
        """LLM 오류로 이 스텝을 포기한다."""
        return StepOutcome(
            step=step,
            decision=Decision(action=GIVE_UP, reason=f"LLM 오류: {exc}"),
            observed=len(observation.elements),
            latency_ms=(time.perf_counter() - started) * 1000,
            note=str(exc)[:120],
        )

    # -- Tier-2 SoM 시각 폴백 (PRD §3.1) -------------------------------------

    async def _tier2_step(
        self,
        client: OpenRouterClient,
        goal: str,
        step: int,
        failures: Sequence[str],
        last_decision: Decision,
    ) -> StepOutcome:
        """SoM 스크린샷 -> VLM 그라운딩 -> 같은 액션 재실행.

        태그 모드: 고른 태그를 `@sN` 핸들로 묶어 마지막 실패 액션을
        element_id만 바꿔 재디스패치한다(기존 검증·치유·HITL 경로 유지).
        좌표 모드(후보 0개, Canvas): CLICK {x, y}만 지원한다.
        """
        started = time.perf_counter()
        action = last_decision.action_type
        if action not in _TIER2_REPLAYABLE:
            action = ActionType.CLICK
        decision = Decision(
            action=action.value,
            element_id=None,
            text=last_decision.text,
            value=last_decision.value,
            key=last_decision.key,
            reason="Tier-2 시각 폴백",
        )
        outcome = StepOutcome(step=step, decision=decision, note="tier2")

        shot = await self.dispatcher.dispatch(
            ActionType.TAKE_SCREENSHOT, {"annotate_som": True}
        )
        if not shot.success:
            outcome.result = shot
            outcome.note = "tier2: 캡처 실패"
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            return outcome

        from vision import SomCandidate, bind_tag

        data = shot.data or {}
        png = base64.b64decode(data.get("image_b64", "") or "")
        candidates = [
            SomCandidate(
                tag=t["tag"],
                selector_path=t.get("selector_path", ""),
                bbox=BBox(**t["bbox"]),
                role=t.get("role", ""),
                name=t.get("name", ""),
            )
            for t in data.get("som_tags", [])
        ]

        grounder = self._grounder
        if grounder is None:
            from vision import ground as grounder  # noqa: F811 — 지연 import

        try:
            grounding = await grounder(
                client,
                png,
                candidates,
                goal,
                failure_context="\n".join(list(failures)[-3:]),
            )
        except (LLMError, BudgetExceeded) as exc:
            if isinstance(exc, BudgetExceeded):
                raise
            outcome.note = f"tier2: VLM 오류 {str(exc)[:100]}"
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            return outcome

        outcome.vision_latency_ms = grounding.latency_ms
        outcome.llm_tokens = grounding.tokens
        outcome.llm_cost = grounding.cost_usd

        if grounding.tag is not None:
            candidate = next(c for c in candidates if c.tag == grounding.tag)
            try:
                element_id = await bind_tag(self.engine, self.page, candidate)
            except LookupError as exc:
                outcome.note = f"tier2: {exc}"
                outcome.latency_ms = (time.perf_counter() - started) * 1000
                return outcome
            decision.element_id = element_id
            params = decision_to_params(decision)
            params["epoch"] = self.engine.epoch
            outcome.result = await self.dispatcher.dispatch(action, params)
        elif grounding.point is not None:
            x, y = grounding.point
            decision.action = ActionType.CLICK.value
            outcome.result = await self.dispatcher.dispatch(
                ActionType.CLICK, {"x": x, "y": y, "epoch": self.engine.epoch}
            )
        else:
            outcome.note = f"tier2: 그라운딩 실패 ({grounding.reason[:80]})"

        outcome.latency_ms = (time.perf_counter() - started) * 1000
        return outcome

    async def _settle(self) -> None:
        """네비게이션이 진행 중이면 안정될 때까지 잠시 기다린다.
        액션이 페이지 전환을 유발한 직후에는 실행 컨텍스트가 교체되어
        관찰이 실패한다. 실패로 처리하지 말고 전환 완료를 기다린다.
        """
        try:
            await self.page.wait_for_load_state(
                "domcontentloaded", timeout=SETTLE_TIMEOUT_MS
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.page.wait_for_timeout(SETTLE_EXTRA_MS)
        except Exception:  # noqa: BLE001
            pass

    async def _run_step(
        self,
        client: OpenRouterClient,
        goal: str,
        step: int,
        history: Sequence[str],
        failures: Sequence[str],
    ) -> StepOutcome:
        started = time.perf_counter()

        # switch_frame 등으로 활성 컨텍스트가 바뀌었으면 따라간다.
        # 디스패처가 프레임에 진입했는데 루프가 메인 페이지를 계속
        # 관찰하면, 전환 자체가 무의미해진다.
        active = getattr(self.dispatcher, "ctx", None)
        if active is not None and getattr(active, "page", None) is not None:
            self.page = active.page

        # --- 관찰: 목표 키워드를 주입한다 (핵심) ---
        keywords = keywords_for_step(goal, failures)
        try:
            observation: ObserveResult = await self.engine.observe_page(
                page=self.page, prune_top_n=self.top_n, goal_keywords=keywords
            )
        except Exception as exc:  # noqa: BLE001
            # 액션이 네비게이션을 유발하면 관찰 도중 실행 컨텍스트가
            # 파괴된다("Execution context was destroyed"). 이는 정상적인
            # 페이지 전환이므로 실패가 아니라 재시도 대상이다.
            # 실측 — MDN 검색이 성공해 결과 페이지로 이동하는 순간 발생했다.
            await self._settle()
            try:
                observation = await self.engine.observe_page(
                    page=self.page, prune_top_n=self.top_n, goal_keywords=keywords
                )
            except Exception as retry_exc:  # noqa: BLE001
                return StepOutcome(
                    step=step,
                    decision=Decision(
                        action=GIVE_UP, reason=f"관찰 실패: {retry_exc}"
                    ),
                    latency_ms=(time.perf_counter() - started) * 1000,
                    note=f"{type(exc).__name__} 후 재관찰도 실패",
                )

        # --- 판단 ---
        messages = build_messages(
            goal,
            observation,
            step=step,
            max_steps=self.max_steps,
            history=history,
            limit=self.top_n,
            # 링크 목적지 힌트를 위해 핸들을 넘긴다. 계약 모델에는 href가
            # 없으므로(동결) 내부 핸들에서 읽어 프롬프트에만 반영한다.
            handles=getattr(self.engine, "_handles", None),
            som_enabled=self.som_enabled,
            page_text=self._pending_page_text,
        )
        page_text = self._pending_page_text
        # 한 번 보여 줬으면 비운다 — 매 스텝 붙이면 프롬프트가 계속 커진다.
        self._pending_page_text = None

        decided_by, defer_reason, note = "llm", "", ""
        tokens, cost = 0, 0.0
        decision: Optional[Decision] = None
        if self._jev is not None:
            jr = await self._jev.decide(
                goal, observation, history=history, page_text=page_text,
                handles=getattr(self.engine, "_handles", None),
            )
            tokens, cost = jr.tokens, jr.cost_usd
            defer_reason = jr.defer_reason
            if not defer_reason and jr.decision is not None:
                decision, decided_by = parse_decision(jr.decision), "jev"
            else:
                try:
                    response = await client.complete(
                        messages,
                        model=self.config.fallback_model,
                        max_tokens=self.max_tokens,
                        # 실측 검증 설정: 생각 짧게 + JSON 강제(형식 깨짐 0, 최대 22초)
                        reasoning={"effort": "low"},
                        response_format={"type": "json_object"},
                    )
                    decision = parse_decision(response.parse_json())
                    decided_by = "fallback"
                    tokens += response.total_tokens
                    cost += response.cost_usd
                    self._jev.fallback_used(defer_reason)
                except LLMError as exc:
                    if jr.decision is None:
                        return self._give_up_step(step, observation, started, exc)
                    # 폴백이 막혀도 Jev 답이 있으면 쓴다(스텝을 버리지 않는다).
                    decision, decided_by = parse_decision(jr.decision), "jev"
                    note = f"폴백 실패 → Jev 답 사용: {str(exc)[:60]}"
        else:
            try:
                response = await client.complete(
                    messages, max_tokens=self.max_tokens
                )
                decision = parse_decision(response.parse_json())
            except LLMError as exc:
                return self._give_up_step(step, observation, started, exc)
            tokens, cost = response.total_tokens, response.cost_usd

        outcome = StepOutcome(
            step=step,
            decision=decision,
            observed=len(observation.elements),
            llm_tokens=tokens,
            llm_cost=cost,
            decided_by=decided_by,
            defer_reason=defer_reason,
        )
        if note:
            outcome.note = note

        if decision.is_terminal:
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            return outcome

        if decision.is_vision_request:
            # 액션을 실행하지 않는다. run()이 이 스텝을 보고 Tier-2로 간다.
            # SoM이 켜져 있으면 요청은 **수용된 것**이므로 성공 스텝이다 —
            # FAIL로 남기면 히스토리가 LLM에게 "요청이 실패했다"고 알려
            # 재요청을 유발한다(실측: icon-buttons 2런이 상한 소진).
            # SoM이 꺼져 있으면 갈 곳이 없으므로 실패 스텝으로 남긴다
            # (프롬프트에 제안하지 않았는데 모델이 고른 경우).
            outcome.judged_success = self.som_enabled
            outcome.note = (
                "vision_request: Tier-2 시각 폴백으로 전환합니다"
                if self.som_enabled
                else "vision_request: SoM 비활성"
            )
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            return outcome

        if decision.is_read_text:
            # 브라우저 액션이 아니라 읽기다. 페이지를 바꾸지 않으므로 성공이면
            # 판단 성공 스텝으로 남긴다(실패로 세면 연속 실패 중단에 걸린다).
            from agent.page_text import read_visible_text

            try:
                text = await read_visible_text(self.page)
            except Exception as exc:  # noqa: BLE001
                outcome.note = f"read_text 실패: {type(exc).__name__}"
            else:
                self._pending_page_text = text
                outcome.judged_success = True
                outcome.note = f"read_text: {len(text)}자"
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            return outcome

        action = decision.action_type
        if action is None:
            outcome.note = f"알 수 없는 액션: {decision.action!r}"
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            return outcome

        # --- LLM 응답 검증: 존재하지 않는 element_id를 걸러낸다 ---
        if decision.element_id:
            valid = {e.element_id for e in observation.elements}
            if decision.element_id not in valid:
                outcome.note = (
                    f"관찰에 없는 element_id: {decision.element_id}"
                )
                outcome.latency_ms = (time.perf_counter() - started) * 1000
                return outcome

        # --- 실행 ---
        params = decision_to_params(decision)
        if decision.element_id:
            params["epoch"] = observation.snapshot_epoch

        outcome.result = await self.dispatcher.dispatch(action, params)
        outcome.latency_ms = (time.perf_counter() - started) * 1000
        return outcome
