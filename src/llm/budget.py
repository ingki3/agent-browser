"""LLM 호출 예산 가드 (PRD §3.4 런타임 상한).

계약상 태스크당 상한:
* `MAX_USD_PER_TASK`     = $0.75
* `MAX_TOKENS_PER_TASK`  = 100,000 토큰
* `MAX_STEPS_PER_TASK`   = 30 스텝

**둘 중 먼저 도달하는 쪽에서 중단한다** (PRD §3.4).

이 가드는 '경고'가 아니라 '차단'이다. 무인 모드에서 루프가 폭주하면
실제 청구로 이어지므로, 초과 시 예외를 던져 호출 자체를 막는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from contracts import thresholds


class BudgetExceeded(RuntimeError):
    """예산 상한 초과. 호출을 중단해야 한다."""

    def __init__(self, kind: str, used: float, limit: float) -> None:
        self.kind = kind
        self.used = used
        self.limit = limit
        super().__init__(
            f"{kind} 예산 초과: {used:,.4f} / 상한 {limit:,.4f}. 태스크를 중단합니다."
        )


#: 모델별 100만 토큰당 단가 (USD). OpenRouter 공개가 기준이며,
#: 미등록 모델은 보수적으로 가장 비싼 값을 적용해 과소 추정을 피한다.
#: 정확한 과금은 OpenRouter 응답의 usage로 갱신된다.
MODEL_PRICING: Dict[str, tuple] = {
    # (입력 $/1M, 출력 $/1M)
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.00),
    "anthropic/claude-3.5-sonnet": (3.00, 15.00),
    "anthropic/claude-3.5-haiku": (0.80, 4.00),
    "google/gemini-2.0-flash-001": (0.10, 0.40),
    "meta-llama/llama-3.3-70b-instruct": (0.12, 0.30),
}

#: 미등록 모델의 보수적 기본 단가
FALLBACK_PRICING = (5.00, 15.00)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """토큰 사용량을 USD로 환산한다."""
    in_price, out_price = MODEL_PRICING.get(model, FALLBACK_PRICING)
    return (prompt_tokens / 1_000_000) * in_price + (
        completion_tokens / 1_000_000
    ) * out_price


@dataclass
class BudgetGuard:
    """태스크 단위 예산 추적기.

    각 LLM 호출 전에 `check()`, 호출 후에 `record()`를 부른다.
    """

    max_usd: float = thresholds.MAX_USD_PER_TASK
    max_tokens: int = thresholds.MAX_TOKENS_PER_TASK
    max_steps: int = thresholds.MAX_STEPS_PER_TASK

    used_usd: float = 0.0
    used_tokens: int = 0
    used_steps: int = 0
    calls: int = 0
    _history: List[Dict[str, float]] = field(default_factory=list, repr=False)

    def check(self, *, next_step: bool = False) -> None:
        """상한 도달 여부를 확인한다. 초과 시 `BudgetExceeded`."""
        if self.used_usd >= self.max_usd:
            raise BudgetExceeded("비용", self.used_usd, self.max_usd)
        if self.used_tokens >= self.max_tokens:
            raise BudgetExceeded("토큰", self.used_tokens, self.max_tokens)
        steps = self.used_steps + (1 if next_step else 0)
        if steps > self.max_steps:
            raise BudgetExceeded("스텝", steps, self.max_steps)

    def record(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        actual_usd: Optional[float] = None,
    ) -> float:
        """호출 결과를 누적한다. 반환값은 이번 호출 비용.

        `actual_usd`가 주어지면(OpenRouter가 실제 과금액을 반환하는 경우)
        추정치 대신 사용한다.
        """
        total = prompt_tokens + completion_tokens
        cost = (
            actual_usd
            if actual_usd is not None
            else estimate_cost(model, prompt_tokens, completion_tokens)
        )
        self.used_tokens += total
        self.used_usd += cost
        self.calls += 1
        self._history.append(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "usd": cost,
            }
        )
        return cost

    def begin_step(self) -> None:
        """스텝 시작. 상한 초과 시 즉시 중단한다."""
        self.check(next_step=True)
        self.used_steps += 1

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_usd - self.used_usd)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)

    def snapshot(self) -> Dict[str, float]:
        """리포트용 사용량 요약."""
        return {
            "steps": self.used_steps,
            "llm_calls": self.calls,
            "tokens": self.used_tokens,
            "usd": round(self.used_usd, 6),
            "usd_limit": self.max_usd,
            "token_limit": self.max_tokens,
            "step_limit": self.max_steps,
        }
