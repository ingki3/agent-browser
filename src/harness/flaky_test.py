"""테스트 플레이키율 측정 (Gate 3-B 항목 4, <= 2.0%).

`python -m harness.flaky_test --runs 5`

동일 시나리오를 반복 실행해 결과가 흔들리는 비율을 측정한다.
플레이키는 "가끔 실패"뿐 아니라 **"가끔 다른 결과"**도 포함한다.
관찰 결과의 요소 순서가 실행마다 바뀌면 에이전트의 판단도 흔들리므로
element_id 순서까지 비교한다.

**커버리지 요건 (AGENTS.md §5 규칙 1)**:
동적 시나리오(광고 로테이션, 지연 로딩, SPA)가 표본에 포함되어야 한다.
정적 페이지만 반복하면 플레이키가 발생할 수 없어 항상 0%가 나온다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Set, Tuple

from harness.mock_sites import MockServer
from harness.result import MetricResult, emit, emit_error

#: 플레이키율 상한 (PRD §1.5). thresholds.FLAKY_RATE와 동일해야 한다.
from contracts import thresholds  # noqa: E402

#: 반복 측정 대상. 동적 요소가 있는 사이트를 반드시 포함한다.
FLAKY_SITES: Tuple[str, ...] = (
    "s09_ad_rotation",  # 200ms 광고 로테이션
    "s14_lazy",  # 300ms 지연 삽입
    "s13_spa",  # SPA 라우팅
    "s20_stress",  # 고부하
    "s01_login",  # 정적 대조군
    "s22_dense",  # 고밀도
)

#: 동적 시나리오로 간주하는 사이트. 하나도 없으면 측정이 무의미하다.
DYNAMIC_SITES: Set[str] = {"s09_ad_rotation", "s14_lazy", "s13_spa", "s20_stress"}


async def _observe_signature(page: Any, engine: Any) -> str:
    """관찰 결과의 서명. 요소 순서까지 포함해 비교한다."""
    result = await engine.observe_page(page=page)
    return "|".join(f"{e.role}:{e.name}" for e in result.elements)


async def _run_flaky(runs: int) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from perception import PerceptionEngine

    signatures: Dict[str, List[str]] = {site: [] for site in FLAKY_SITES}
    errors: List[str] = []

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
            engine = PerceptionEngine()

            for _ in range(runs):
                for site in FLAKY_SITES:
                    try:
                        await page.goto(
                            server.site_url(site), wait_until="domcontentloaded"
                        )
                        await page.wait_for_timeout(350)
                        signatures[site].append(await _observe_signature(page, engine))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{site}: {exc}")
                        signatures[site].append(f"ERROR:{exc}")

            await context.close()
            await browser.close()

    # 사이트별로 서명이 갈리면 플레이키로 집계한다.
    flaky_sites: List[str] = []
    total_observations = 0
    unstable_observations = 0
    error_count = 0

    for site, sigs in signatures.items():
        total_observations += len(sigs)
        error_count += sum(1 for s in sigs if s.startswith("ERROR:"))
        if not sigs:
            continue
        majority = max(set(sigs), key=sigs.count)
        deviations = sum(1 for s in sigs if s != majority)
        unstable_observations += deviations
        if deviations:
            flaky_sites.append(f"{site} ({deviations}/{len(sigs)}회 불일치)")

    rate = unstable_observations / total_observations if total_observations else 0.0
    return {
        "rate": round(rate, 4),
        "samples": total_observations,
        "flaky_sites": flaky_sites,
        "errors": errors[:5],
        "error_count": error_count,
        "dynamic_covered": sorted(DYNAMIC_SITES & set(FLAKY_SITES)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="테스트 플레이키율 측정")
    parser.add_argument("--runs", type=int, default=5, help="시나리오당 반복 횟수")
    args = parser.parse_args()

    if args.runs < 2:
        sys.exit(
            int(
                emit_error(
                    "flaky_rate",
                    "플레이키는 반복 간 편차이므로 --runs는 2 이상이어야 합니다.",
                )
            )
        )

    try:
        metrics = asyncio.run(_run_flaky(args.runs))
    except Exception as exc:  # noqa: BLE001
        sys.exit(int(emit_error("flaky_rate", f"측정 실패: {exc}")))

    for site in metrics["flaky_sites"]:
        print(f"[-] 불안정: {site}", file=sys.stderr)
    for err in metrics["errors"]:
        print(f"[-] 오류: {err}", file=sys.stderr)

    # --- 커버리지: 동적 시나리오 포함 여부 ---
    if not metrics["dynamic_covered"]:
        sys.exit(
            int(
                emit_error(
                    "flaky_rate",
                    "동적 시나리오가 표본에 없습니다. 정적 페이지만 반복하면 "
                    "플레이키가 발생할 수 없어 항상 0%가 됩니다.",
                )
            )
        )

    # --- 관찰 오류는 '안정'이 아니라 '측정 불가'다 ---
    # 모든 실행이 동일한 예외로 실패하면 서명이 일치해 flaky_rate가 0이 된다.
    # 사보타주 실험에서 실제로 이 경로로 통과가 발생했다.
    if metrics["error_count"]:
        sys.exit(
            int(
                emit_error(
                    "flaky_rate",
                    f"관찰 중 오류가 {metrics['error_count']}건 발생했습니다. "
                    "모든 실행이 같은 오류로 실패하면 서명이 일치해 플레이키율이 "
                    "0%로 보고되므로 측정을 신뢰할 수 없습니다.",
                )
            )
        )

    result = MetricResult(
        metric="flaky_rate",
        value=metrics["rate"],
        threshold=thresholds.FLAKY_RATE,
        samples=metrics["samples"],
        comparison="lte",
        extra={
            "flaky_sites": metrics["flaky_sites"] or None,
            "runs_per_site": args.runs,
            "dynamic_sites_covered": len(metrics["dynamic_covered"]),
            "sites_measured": len(FLAKY_SITES),
            "observation_errors": metrics["error_count"],
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
