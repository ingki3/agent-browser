"""엔진 지연 실측 스파이크 (Gate 1 항목 8, src/AGENTS.md §5).

`python -m harness.engine_spike --sites 20 --report artifacts/engine_spike.json`

Playwright + CDP의 실제 오버헤드를 측정해 아키텍처 판단 근거를 만든다.
**임계값 판정 게이트가 아니며**, 리포트 산출 여부만 확인한다.

측정 4종 (AGENTS.md §5 표와 1:1 대응):
1. AxTree 추출 단독 지연        — Accessibility.getFullAXTree
2. CDP 왕복 오버헤드            — Runtime.evaluate("1") 무연산 호출
3. Actionability 대기 지연      — 200ms 광고 로테이션 페이지 click
4. 관찰 파이프라인 총 지연      — 추출 + 살균 + 프루닝 전 구간

Playwright 미설치 또는 브라우저 바이너리 부재 시 exit 2로 명확히 실패한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List

from harness.mock_sites import MOCK_SITES, MockServer
from harness.result import MetricResult, emit, emit_error

#: CDP 왕복 측정 반복 횟수 (AGENTS.md §5 기준)
CDP_ROUNDTRIP_SAMPLES = 1_000

#: 광고 로테이션 클릭 반복 횟수
ACTIONABILITY_SAMPLES = 20


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[idx]


def _summary(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "samples": 0}
    return {
        "p50": round(statistics.median(values), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "mean": round(statistics.fmean(values), 3),
        "samples": len(values),
    }


async def _measure(site_count: int) -> Dict[str, Dict[str, float]]:
    from playwright.async_api import async_playwright

    axtree_ms: List[float] = []
    cdp_ms: List[float] = []
    actionability_ms: List[float] = []
    pipeline_ms: List[float] = []

    targets = list(MOCK_SITES)[:site_count]

    with MockServer() as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 720})
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            await cdp.send("Accessibility.enable")

            # --- 1 & 4. 사이트별 AxTree 추출 및 파이프라인 총 지연 ---
            for site in targets:
                await page.goto(server.site_url(site.site_id), wait_until="domcontentloaded")

                pipeline_start = time.perf_counter()
                ax_start = time.perf_counter()
                tree = await cdp.send("Accessibility.getFullAXTree")
                axtree_ms.append((time.perf_counter() - ax_start) * 1000)

                # 살균 + 프루닝 근사: 노드 필터링 및 상위 20개 선별
                nodes = tree.get("nodes", [])
                interactive = [
                    n
                    for n in nodes
                    if n.get("role", {}).get("value") in ("button", "link", "textbox")
                    and not n.get("ignored", False)
                ]
                interactive.sort(key=lambda n: len(str(n.get("name", {}))), reverse=True)
                _ = interactive[:20]
                pipeline_ms.append((time.perf_counter() - pipeline_start) * 1000)

            # --- 2. CDP 왕복 오버헤드 ---
            for _ in range(CDP_ROUNDTRIP_SAMPLES):
                start = time.perf_counter()
                await cdp.send("Runtime.evaluate", {"expression": "1"})
                cdp_ms.append((time.perf_counter() - start) * 1000)

            # --- 3. Actionability 대기 (200ms 광고 로테이션) ---
            await page.goto(
                server.site_url("s09_ad_rotation"), wait_until="domcontentloaded"
            )
            for _ in range(ACTIONABILITY_SAMPLES):
                start = time.perf_counter()
                await page.click("#cart", timeout=5_000)
                actionability_ms.append((time.perf_counter() - start) * 1000)

            await context.close()
            await browser.close()

    return {
        "axtree_extraction_ms": _summary(axtree_ms),
        "cdp_roundtrip_ms": _summary(cdp_ms),
        "actionability_wait_ms": _summary(actionability_ms),
        "observation_pipeline_ms": _summary(pipeline_ms),
    }


def _advisories(metrics: Dict[str, Dict[str, float]]) -> List[str]:
    """AGENTS.md §5 '리포트 활용 규칙'에 따른 후속 조치 권고."""
    notes: List[str] = []
    axtree_p50 = metrics["axtree_extraction_ms"]["p50"]
    if axtree_p50 > 150:  # 관찰 예산 300ms의 50%
        notes.append(
            f"AxTree 추출 p50={axtree_p50}ms — 관찰 예산 300ms의 50% 초과. "
            "Stage 2 착수 전 사람 감독자 보고 필요."
        )
    cdp_mean = metrics["cdp_roundtrip_ms"]["mean"]
    if cdp_mean > 1.0:
        notes.append(
            f"CDP 왕복 평균 {cdp_mean}ms — 1ms 초과. Prune4Web은 요소별 호출이 아닌 "
            "단일 Runtime.evaluate 일괄 처리로 설계해야 함 (WS-2 필수)."
        )
    act_p95 = metrics["actionability_wait_ms"]["p95"]
    if act_p95 > 1_000:
        notes.append(
            f"광고 로테이션 페이지 actionability p95={act_p95}ms — 1,000ms 초과. "
            "staleness 검증 통과 요소에 force=True 허용 여부 판단 필요."
        )
    return notes


def main() -> None:
    parser = argparse.ArgumentParser(description="엔진 지연 실측 스파이크")
    parser.add_argument("--sites", type=int, default=20, help="측정 대상 사이트 수")
    parser.add_argument(
        "--report", type=str, default="artifacts/engine_spike.json", help="리포트 경로"
    )
    args = parser.parse_args()

    try:
        metrics = asyncio.run(_measure(args.sites))
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "engine_spike_report",
                    "playwright 미설치. 'uv sync --extra dev' 및 "
                    "'playwright install chromium' 실행 필요.",
                )
            )
        )
    except Exception as exc:  # noqa: BLE001
        sys.exit(
            int(emit_error("engine_spike_report", f"{type(exc).__name__}: {exc}"))
        )

    notes = _advisories(metrics)
    report = {
        "metrics": metrics,
        "advisories": notes,
        "budget_reference": {
            "observe_latency_ms_p50": 300,
            "step_latency_ms_p50": 800,
            "complex_latency_ms_p95": 2_200,
        },
    }

    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    for note in notes:
        print(f"[!] {note}", file=sys.stderr)

    # 판정 게이트가 아니므로 '리포트 산출 여부'만 지표로 삼는다.
    result = MetricResult(
        metric="engine_spike_report",
        value=1.0 if path.exists() else 0.0,
        threshold=1.0,
        samples=args.sites,
        comparison="gte",
        extra={"report_path": str(path), "advisories": notes or None, **metrics},
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
