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

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from contracts import ActionResult, ActionType, ObserveResult
from llm import BudgetExceeded, BudgetGuard, LLMError, OpenRouterClient
from llm.config import LLMConfig

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
DEFAULT_MAX_TOKENS = 4096

#: 연속 실패 허용 횟수. 초과하면 루프를 끊는다. 같은 실패를 반복하며
#: 예산만 소진하는 상황을 막는다.
MAX_CONSECUTIVE_FAILURES = 3

#: 네비게이션 안정화 대기. 액션이 페이지 전환을 유발하면 실행 컨텍스트가
#: 교체되어 관찰이 실패하므로, 전환 완료를 기다린 뒤 재관찰한다.
SETTLE_TIMEOUT_MS = 8000
SETTLE_EXTRA_MS = 600


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

    @property
    def succeeded(self) -> bool:
        if self.decision.is_terminal:
            return self.decision.action == FINISH
        return bool(self.result and self.result.success)

    def summary(self) -> str:
        target = f" {self.decision.element_id}" if self.decision.element_id else ""
        mark = "OK" if self.succeeded else "FAIL"
        detail = self.note or (
            self.result.error_code.value
            if self.result and self.result.error_code
            else ""
        )
        return f"{self.decision.action}{target} -> {mark} {detail}".strip()


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
            "trace": [s.summary() for s in self.steps],
        }


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
    ) -> None:
        self.page = page
        self.engine = engine
        self.dispatcher = dispatcher
        self.config = config
        self.budget = budget or BudgetGuard()
        self.max_steps = max_steps or self.budget.max_steps
        self.top_n = top_n
        self.max_tokens = max_tokens

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

        try:
            for step in range(1, self.max_steps + 1):
                try:
                    self.budget.begin_step()
                except BudgetExceeded as exc:
                    run.terminal_reason = str(exc)
                    break

                outcome = await self._run_step(
                    client, goal, step, history, failures
                )
                run.steps.append(outcome)

                if outcome.decision.action == FINISH:
                    run.completed = True
                    run.terminal_reason = "LLM이 목표 달성을 선언했습니다."
                    break
                if outcome.decision.action == GIVE_UP:
                    run.terminal_reason = f"LLM 포기: {outcome.decision.reason}"
                    break

                if outcome.succeeded:
                    consecutive_failures = 0
                    history.append(outcome.summary())
                else:
                    consecutive_failures += 1
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
            await client.close()

        run.elapsed_s = time.perf_counter() - started
        run.budget = self.budget.snapshot()
        try:
            run.final_url = self.page.url
        except Exception:  # noqa: BLE001
            run.final_url = ""
        return run

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
        )
        try:
            response = await client.complete(
                messages, max_tokens=self.max_tokens
            )
            decision = parse_decision(response.parse_json())
        except LLMError as exc:
            return StepOutcome(
                step=step,
                decision=Decision(action=GIVE_UP, reason=f"LLM 오류: {exc}"),
                observed=len(observation.elements),
                latency_ms=(time.perf_counter() - started) * 1000,
                note=str(exc)[:120],
            )

        outcome = StepOutcome(
            step=step,
            decision=decision,
            observed=len(observation.elements),
            llm_tokens=response.total_tokens,
            llm_cost=response.cost_usd,
        )

        if decision.is_terminal:
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
