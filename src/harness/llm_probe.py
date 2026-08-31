"""LLM 연동 사전 점검 (실환경 검증 준비).

`python -m harness.llm_probe`            # 실제 호출로 자격증명 확인
`python -m harness.llm_probe --offline`  # 호출 없이 설정만 확인 (비용 0)

**판정 게이트가 아니다.** 외부 API와 과금에 의존하므로 CI 필수 체크로
등록하지 않는다. 실환경 검증을 시작하기 전에 "키가 유효한가"를 미리
확인해 시간과 비용 낭비를 막는 것이 목적이다.

키가 없으면 exit 2(측정 불가)로 명확히 실패한다. 0을 반환해 "통과"로
위장하면 이후 단계에서 원인 파악이 어려워진다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List

from harness.result import MetricResult, emit, emit_error

#: 설정에서 반드시 확인해야 할 항목. 하나라도 빠지면 실환경 검증을
#: 시작할 수 없으므로 커버리지 요건으로 다룬다.
REQUIRED_CHECKS = ("api_key", "model", "base_url", "budget_limits")


def _inspect_config() -> Dict[str, Any]:
    """호출 없이 설정 상태를 점검한다."""
    from contracts import thresholds
    from llm import BudgetGuard, load_config

    config = load_config()
    guard = BudgetGuard()

    checks: Dict[str, bool] = {
        "api_key": config.configured,
        "model": bool(config.model),
        "base_url": config.base_url.startswith("https://"),
        # 예산 상한이 계약값과 일치해야 한다. 하드코딩된 값이 계약과
        # 갈라지면 상한이 무의미해진다.
        "budget_limits": (
            guard.max_usd == thresholds.MAX_USD_PER_TASK
            and guard.max_tokens == thresholds.MAX_TOKENS_PER_TASK
            and guard.max_steps == thresholds.MAX_STEPS_PER_TASK
        ),
    }
    return {
        "checks": checks,
        "model": config.model,
        "masked_key": config.masked_key,
        "placeholder": config.has_placeholder_key,
        "base_url": config.base_url,
        "budget": guard.snapshot(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM 연동 사전 점검")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="실제 API를 호출하지 않고 설정만 확인합니다 (비용 0).",
    )
    args = parser.parse_args()

    try:
        import llm  # noqa: F401
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "llm_connectivity",
                    "LLM 어댑터(WS-7)가 아직 구현되지 않아 측정할 수 없습니다.",
                )
            )
        )

    info = _inspect_config()
    checks = info["checks"]

    for name in REQUIRED_CHECKS:
        if not checks.get(name):
            print(f"[-] 설정 미비: {name}", file=sys.stderr)

    if not checks["api_key"]:
        reason = (
            ".env의 OPENROUTER_API_KEY가 플레이스홀더입니다. 실제 키로 "
            "교체하십시오 (발급: https://openrouter.ai/keys)."
            if info["placeholder"]
            else "OPENROUTER_API_KEY가 없습니다. `cp .env.example .env` 후 "
            "키를 채우거나 환경변수로 지정하십시오."
        )
        sys.exit(int(emit_error("llm_connectivity", reason)))

    failed: List[str] = [n for n in REQUIRED_CHECKS if not checks.get(n)]
    if failed:
        sys.exit(
            int(
                emit_error(
                    "llm_connectivity",
                    f"설정 항목 {len(failed)}건({', '.join(failed)})이 유효하지 "
                    "않습니다.",
                )
            )
        )

    if args.offline:
        result = MetricResult(
            metric="llm_connectivity",
            value=1.0,
            threshold=1.0,
            samples=len(REQUIRED_CHECKS),
            comparison="gte",
            extra={
                "mode": "offline",
                "model": info["model"],
                "key": info["masked_key"],
                "checks_passed": len(REQUIRED_CHECKS),
                "checks_required": len(REQUIRED_CHECKS),
                "note": "설정만 확인 — 실제 호출 없음",
            },
        )
        sys.exit(int(emit(result)))

    from llm import probe_connection

    probe = asyncio.run(probe_connection())
    if not probe["ok"]:
        sys.exit(
            int(
                emit_error(
                    "llm_connectivity",
                    f"API 호출 실패: {probe['reason']}",
                )
            )
        )

    result = MetricResult(
        metric="llm_connectivity",
        value=1.0,
        threshold=1.0,
        samples=len(REQUIRED_CHECKS),
        comparison="gte",
        extra={
            "mode": "live",
            "model": probe["model"],
            "key": info["masked_key"],
            "reply": probe["reply"],
            "prompt_tokens": probe["prompt_tokens"],
            "completion_tokens": probe["completion_tokens"],
            "cost_usd": probe["cost_usd"],
            "checks_passed": len(REQUIRED_CHECKS),
            "checks_required": len(REQUIRED_CHECKS),
            "budget_limits": info["budget"],
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
