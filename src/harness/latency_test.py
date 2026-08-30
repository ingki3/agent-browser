"""스텝 지연 측정 (Gate 3-B 항목 5·6).

`python -m harness.latency_test --steps 100`

PRD §1.5의 복합 스텝 지연을 측정한다:
* 로컬 순수 지연 p50 <= 800ms  (관찰 + 액션 + 사후검증, 외부 네트워크 제외)
* 복합 스텝 지연  p95 <= 2,200ms

**측정 범위 (중요)**: LLM 추론 시간은 제외한다. 본 지표는 런타임이
제어하는 구간만 다루며, LLM은 별도 예산으로 관리한다(PRD §1.5 주석).

**커버리지 요건 (AGENTS.md §5 규칙 1)**:
관찰과 액션이 모두 포함된 완전한 스텝을 측정해야 한다. 관찰만 반복하면
액션 지연이 빠져 실제보다 낙관적인 수치가 나온다.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from typing import Any, Dict, List, Tuple

from contracts import ActionType, thresholds

from harness.mock_sites import MockServer
from harness.result import MetricResult, emit, emit_error

#: 측정 시나리오: (사이트, 액션, 대상 role, 대상 name)
#: 관찰 -> 액션 -> 사후검증이 모두 포함된 완전한 스텝이어야 한다.
STEP_PLAN: Tuple[Tuple[str, ActionType, str, str], ...] = (
    ("s01_login", ActionType.CLICK, "button", "로그인"),
    ("s01_login", ActionType.TYPE_TEXT, "textbox", "아이디"),
    ("s09_ad_rotation", ActionType.CLICK, "button", "장바구니 담기"),
    ("s22_dense", ActionType.CLICK, "button", "주문 결제하기"),
    ("s21_widgets", ActionType.CHECK_BOX, "checkbox", "약관 동의"),
    ("s13_spa", ActionType.CLICK, "button", "설정으로 이동"),
)


async def _run_latency(steps: int) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    observe_ms: List[float] = []
    action_ms: List[float] = []
    step_ms: List[float] = []
    failures: List[str] = []
    actions_used: set = set()

    with MockServer() as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={
                    "width": thresholds.VIEWPORT_WIDTH,
                    "height": thresholds.VIEWPORT_HEIGHT,
                }
            )
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            engine = PerceptionEngine()
            dispatcher = ActionDispatcher(
                DispatchContext(page=page, engine=engine, cdp=cdp)
            )

            idx = 0
            while len(step_ms) < steps:
                site, action, role, name = STEP_PLAN[idx % len(STEP_PLAN)]
                idx += 1

                await page.goto(server.site_url(site), wait_until="domcontentloaded")
                await page.wait_for_timeout(350)

                step_start = time.perf_counter()

                # --- 관찰 구간 ---
                obs_start = time.perf_counter()
                observation = await engine.observe_page(page=page, cdp=cdp)
                observe_ms.append((time.perf_counter() - obs_start) * 1000)

                match = next(
                    (
                        e
                        for e in observation.elements
                        if e.role == role and e.name == name
                    ),
                    None,
                )
                if match is None:
                    failures.append(f"{site}: 대상 미발견 ({role}/{name})")
                    continue

                params: Dict[str, Any] = {
                    "element_id": match.element_id,
                    "epoch": observation.snapshot_epoch,
                }
                if action is ActionType.TYPE_TEXT:
                    params["text"] = "지연측정"
                if action is ActionType.CHECK_BOX:
                    params["checked"] = True

                # --- 액션 + 사후검증 구간 ---
                act_start = time.perf_counter()
                result = await dispatcher.dispatch(action, params)
                action_ms.append((time.perf_counter() - act_start) * 1000)
                actions_used.add(action)

                step_ms.append((time.perf_counter() - step_start) * 1000)

                if not result.success:
                    failures.append(
                        f"{site}/{action.value}: "
                        f"{result.error_code.value if result.error_code else '?'}"
                    )

            await context.close()
            await browser.close()

    def pct(values: List[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(int(len(ordered) * p), len(ordered) - 1)]

    return {
        "p50_step_ms": round(statistics.median(step_ms), 2) if step_ms else 0.0,
        "p95_step_ms": round(pct(step_ms, 0.95), 2),
        "p50_observe_ms": round(statistics.median(observe_ms), 2) if observe_ms else 0.0,
        "p50_action_ms": round(statistics.median(action_ms), 2) if action_ms else 0.0,
        "samples": len(step_ms),
        "failures": failures[:5],
        "actions_used": sorted(a.value for a in actions_used),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="스텝 지연 측정")
    parser.add_argument("--steps", type=int, default=100, help="측정할 스텝 수")
    args = parser.parse_args()

    try:
        metrics = asyncio.run(_run_latency(args.steps))
    except Exception as exc:  # noqa: BLE001
        sys.exit(int(emit_error("step_latency_p50_ms", f"측정 실패: {exc}")))

    for failure in metrics["failures"]:
        print(f"[-] {failure}", file=sys.stderr)

    # --- 커버리지: 관찰과 액션이 모두 포함되었는가 ---
    if metrics["p50_action_ms"] <= 0.0:
        sys.exit(
            int(
                emit_error(
                    "step_latency_p50_ms",
                    "액션 구간이 측정되지 않았습니다. 관찰만 반복하면 실제보다 "
                    "낙관적인 지연이 보고됩니다.",
                )
            )
        )
    if len(metrics["actions_used"]) < 2:
        sys.exit(
            int(
                emit_error(
                    "step_latency_p50_ms",
                    f"측정된 액션 종류가 {len(metrics['actions_used'])}종뿐입니다. "
                    "단일 액션만으로는 스텝 지연을 대표할 수 없습니다.",
                )
            )
        )

    p95_ok = metrics["p95_step_ms"] <= thresholds.COMPLEX_LATENCY_MS_P95

    result = MetricResult(
        metric="step_latency_p50_ms",
        value=metrics["p50_step_ms"],
        threshold=float(thresholds.STEP_LATENCY_MS_P50),
        samples=metrics["samples"],
        comparison="lte",
        extra={
            "p95_step_ms": metrics["p95_step_ms"],
            "p95_threshold": thresholds.COMPLEX_LATENCY_MS_P95,
            "p95_ok": p95_ok,
            "p50_observe_ms": metrics["p50_observe_ms"],
            "p50_action_ms": metrics["p50_action_ms"],
            "actions_measured": len(metrics["actions_used"]),
            "note": "LLM 추론 시간 제외 (런타임 제어 구간만 측정)",
        },
    )
    code = int(emit(result))
    # p50이 통과해도 p95가 초과하면 게이트 실패로 처리한다.
    sys.exit(code or (0 if p95_ok else 1))


if __name__ == "__main__":
    main()
