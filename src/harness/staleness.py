"""동적 페이지 Staleness 불일치율 측정 (Gate 3-A 항목 4, <= 5.0%).

`python -m harness.staleness --runs 100`

**측정 대상은 "오탐율"이다.** 광고 로테이션처럼 페이지가 계속 변하는
환경에서, 실제로는 멀쩡한 요소를 stale로 잘못 판정하는 비율을 잰다.

이 값이 높으면 정상 요소마다 불필요한 치유·재관찰이 발생해 지연과
토큰 비용이 급증한다. PRD §4.2가 전역 에포크와 요소별 검증을 분리한
이유가 바로 이것이다.

동시에 **진짜 stale을 놓치지 않는지(미탐)** 도 검증한다. 검증기를
항상 fresh로 만들면 불일치율은 0이 되지만 TOCTOU를 막지 못한다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List

from contracts import thresholds

from harness.mock_sites import MockServer
from harness.result import MetricResult, emit, emit_error

#: 동적 페이지 Staleness 불일치율 상한 (src/AGENTS.md §5 Gate 3-A 항목 4).
#: `contracts.thresholds`는 Stage 0에서 동결되었고 본 지표가 포함되지
#: 않았으므로, 게이트 명령어에 명시된 값을 하네스 상수로 둔다.
#: 계약 재동결 시 thresholds로 이관해야 한다.
STALENESS_MISMATCH_RATE_MAX = 0.05

#: 오탐 측정에 사용할 동적 사이트 (광고 200ms 로테이션 등)
DYNAMIC_SITES = ("s09_ad_rotation", "s20_stress", "s17_feed", "s08_infinite")


async def _run(runs: int) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from actions import verify_staleness
    from perception import PerceptionEngine

    false_positives = 0
    checks = 0
    missed_stale = 0
    stale_checks = 0
    details: List[str] = []

    with MockServer() as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": thresholds.VIEWPORT_WIDTH,
                          "height": thresholds.VIEWPORT_HEIGHT}
            )
            page = await context.new_page()

            idx = 0
            while checks < runs:
                site_id = DYNAMIC_SITES[idx % len(DYNAMIC_SITES)]
                idx += 1

                engine = PerceptionEngine()
                await page.goto(
                    server.site_url(site_id), wait_until="domcontentloaded"
                )
                await page.wait_for_timeout(250)

                observation = await engine.observe_page(page=page, prune_top_n=20)
                if not observation.elements:
                    continue

                # --- (A) 오탐 측정: 안정적인 요소가 stale로 판정되는가 ---
                for element in observation.elements[:3]:
                    handle = engine.get_handle(element.element_id)
                    if handle is None:
                        continue

                    # 광고가 여러 번 회전할 만큼 대기 (200ms 주기 x 3)
                    await page.wait_for_timeout(650)

                    result = await verify_staleness(page, handle, engine.epoch)
                    checks += 1
                    if not result.fresh:
                        false_positives += 1
                        details.append(
                            f"{site_id} {element.role} {element.name!r}: "
                            f"{result.reason.value} ({result.detail})"
                        )
                    if checks >= runs:
                        break

                # --- (B) 미탐 측정: 진짜 stale을 잡아내는가 ---
                target = observation.elements[0]
                handle = engine.get_handle(target.element_id)
                if handle is not None:
                    # 요소를 실제로 제거한다 -> 반드시 stale로 판정되어야 한다
                    await page.evaluate(
                        "(sel) => { const el = document.querySelector(sel); "
                        "if (el) el.remove(); }",
                        handle.css_path,
                    )
                    stale_result = await verify_staleness(page, handle, engine.epoch)
                    stale_checks += 1
                    if stale_result.fresh:
                        missed_stale += 1
                        details.append(
                            f"{site_id}: 제거된 요소를 fresh로 판정 (미탐)"
                        )

            await context.close()
            await browser.close()

    return {
        "fpr": round(false_positives / checks, 4) if checks else 1.0,
        "checks": checks,
        "false_positives": false_positives,
        "missed_stale": missed_stale,
        "stale_checks": stale_checks,
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Staleness 불일치율 측정")
    parser.add_argument("--runs", type=int, default=100, help="검증 횟수")
    args = parser.parse_args()

    try:
        from actions import verify_staleness  # noqa: F401
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "staleness_mismatch_rate",
                    "Staleness 검증기(WS-3 actions/)가 아직 구현되지 않았습니다.",
                )
            )
        )

    try:
        metrics = asyncio.run(_run(args.runs))
    except ImportError:
        sys.exit(int(emit_error("staleness_mismatch_rate", "playwright 미설치.")))
    except Exception as exc:  # noqa: BLE001
        sys.exit(
            int(emit_error("staleness_mismatch_rate", f"{type(exc).__name__}: {exc}"))
        )

    for detail in metrics["details"][:10]:
        print(f"[-] {detail}", file=sys.stderr)

    # 미탐이 있으면 지표가 무의미하므로 즉시 실패시킨다.
    if metrics["missed_stale"] > 0:
        sys.exit(
            int(
                emit_error(
                    "staleness_mismatch_rate",
                    f"제거된 요소 {metrics['missed_stale']}건을 fresh로 오판했습니다. "
                    "검증기가 TOCTOU를 막지 못합니다.",
                )
            )
        )

    result = MetricResult(
        metric="staleness_mismatch_rate",
        value=metrics["fpr"],
        threshold=STALENESS_MISMATCH_RATE_MAX,
        samples=metrics["checks"],
        comparison="lte",
        extra={
            "false_positives": metrics["false_positives"],
            "stale_detection_checks": metrics["stale_checks"],
            "missed_stale": metrics["missed_stale"],
            "details": metrics["details"][:10] or None,
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
