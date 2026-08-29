"""Recall@20 평가 파이프라인 (Gate 1 항목 7 / Gate 2 항목 2).

`python -m harness.recall --golden`            # 하네스 자체 검증 (recall == 1.0)
`python -m harness.recall --pages 100 --top-n 20`  # Stage 2 인지 엔진 평가

설계 원칙:
* 인지 엔진(WS-2)은 Stage 2에서 구현되므로, 본 파이프라인은 **엔진을 주입받는다**.
* `--golden`은 엔진 없이 참조 추출기로 동작하며, **하네스 자신의 정확성**을 검증한다.
  골든셋은 정답이 사전 고정되어 있으므로 recall이 1.0이 아니면 하네스에 결함이 있다.
* 엔진이 없는 상태에서 `--pages`를 호출하면 측정이 불가능하므로 exit 2로 명확히 실패한다.
  (0.0을 반환해 "측정했으나 미달"로 위장하지 않는다.)
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from contracts import thresholds

from harness.golden_set import GOLDEN_SET, GoldenCase, validate_golden_set
from harness.mock_sites import SITE_INDEX, MockServer
from harness.result import MetricResult, emit, emit_error

#: 인지 엔진 주입 시그니처: (url, top_n) -> [(role, name), ...]
ExtractorFn = Callable[[str, int], Sequence[Tuple[str, str]]]


@dataclass(frozen=True)
class CaseOutcome:
    case: GoldenCase
    found: bool
    rank: Optional[int]


# ---------------------------------------------------------------------------
# 참조 추출기 (골든 모드 전용)
# ---------------------------------------------------------------------------

_TAG_ROLE = {"button": "button", "a": "link", "input": "textbox"}


def reference_extractor(html: str, top_n: int) -> List[Tuple[str, str]]:
    """정적 HTML에서 (role, name) 후보를 추출하는 참조 구현.

    WS-2의 AxTree 기반 엔진을 대체하지 않는다. 골든셋 정답이 실제로
    페이지에 존재하고 하네스가 이를 올바르게 대조하는지 확인하기 위한
    최소 구현이다. JS로 삽입되는 노드까지 포함하도록 스크립트 본문의
    태그 리터럴도 함께 스캔한다.
    """
    candidates: List[Tuple[str, str]] = []

    # <button ...>텍스트</button>
    for match in re.finditer(r"<button[^>]*>(.*?)</button>", html, re.S | re.I):
        text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        if text:
            candidates.append(("button", text))

    # <a ...>텍스트</a>
    for match in re.finditer(r"<a[^>]*>(.*?)</a>", html, re.S | re.I):
        text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        if text:
            candidates.append(("link", text))

    # 스크립트 내 문자열로 삽입되는 버튼/링크 (지연 로딩, Shadow DOM)
    for match in re.finditer(r"<button[^>]*>([^<]+)</button>", html):
        text = match.group(1).strip()
        if text and ("button", text) not in candidates:
            candidates.append(("button", text))

    # 중복 제거 후 상위 N개
    seen = set()
    unique: List[Tuple[str, str]] = []
    for role, name in candidates:
        key = (role, name)
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique[:top_n]


# ---------------------------------------------------------------------------
# 평가 로직
# ---------------------------------------------------------------------------


def evaluate_golden(top_n: int) -> List[CaseOutcome]:
    """골든셋 10종에 대해 정답 요소가 Top-N에 남는지 확인한다."""
    outcomes: List[CaseOutcome] = []
    with MockServer() as server:
        import urllib.request

        for case in GOLDEN_SET:
            url = server.site_url(case.site_id)
            with urllib.request.urlopen(url, timeout=5) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            candidates = reference_extractor(html, top_n)
            rank: Optional[int] = None
            for idx, (role, name) in enumerate(candidates, start=1):
                if role == case.expected_role and name == case.expected_name:
                    rank = idx
                    break
            outcomes.append(CaseOutcome(case=case, found=rank is not None, rank=rank))
    return outcomes


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[idx]


async def _evaluate_engine(extractor, pages: int, top_n: int) -> dict:
    """실브라우저로 인지 엔진의 Recall@N / 토큰 / 지연을 실측한다.

    골든셋 정답을 정답 레이블로 사용하되, 사이트를 반복 순회해
    요청된 페이지 수만큼 표본을 채운다.
    """
    from playwright.async_api import async_playwright

    hits = 0
    total = 0
    misses: List[str] = []
    token_counts: List[float] = []
    latencies: List[float] = []

    with MockServer() as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 720})
            page = await context.new_page()

            idx = 0
            while total < pages:
                case = GOLDEN_SET[idx % len(GOLDEN_SET)]
                idx += 1

                await page.goto(
                    server.site_url(case.site_id), wait_until="domcontentloaded"
                )
                # 지연 로딩 시나리오를 위해 짧게 안정화 대기
                await page.wait_for_timeout(350)

                started = time.perf_counter()
                candidates = await extractor(page, top_n)
                latencies.append((time.perf_counter() - started) * 1000)

                summary = "\n".join(f"{r} {n}" for r, n in candidates)
                token_counts.append(float(_estimate_tokens(summary)))

                found = any(
                    role == case.expected_role and name == case.expected_name
                    for role, name in candidates
                )
                total += 1
                if found:
                    hits += 1
                elif case.site_id not in misses:
                    misses.append(case.site_id)

            await context.close()
            await browser.close()

    return {
        "recall": round(hits / total, 4) if total else 0.0,
        "samples": total,
        "misses": misses,
        "p50_tokens": round(statistics.median(token_counts), 1) if token_counts else 0.0,
        "p95_tokens": round(_percentile(token_counts, 0.95), 1),
        "p50_latency_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
        "p95_latency_ms": round(_percentile(latencies, 0.95), 2),
    }


def _estimate_tokens(text: str) -> int:
    try:
        from perception import estimate_tokens

        return estimate_tokens(text)
    except ImportError:
        return max(1, len(text) // 3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Recall@20 평가 파이프라인")
    parser.add_argument("--golden", action="store_true", help="골든셋 자체 검증 모드")
    parser.add_argument("--pages", type=int, default=None, help="평가 페이지 수")
    parser.add_argument(
        "--top-n", type=int, default=thresholds.DEFAULT_PRUNE_TOP_N, help="프루닝 상한"
    )
    args = parser.parse_args()

    # --- 골든 모드: 하네스 자체 검증 ---------------------------------------
    if args.golden:
        integrity = validate_golden_set()
        if integrity:
            for problem in integrity:
                print(f"[-] {problem}", file=sys.stderr)
            sys.exit(int(emit_error("golden_recall", "골든셋 정의가 사이트와 불일치")))

        try:
            outcomes = evaluate_golden(args.top_n)
        except Exception as exc:  # noqa: BLE001
            sys.exit(int(emit_error("golden_recall", f"{type(exc).__name__}: {exc}")))

        hits = sum(1 for o in outcomes if o.found)
        recall = hits / len(outcomes) if outcomes else 0.0
        misses = [o.case.site_id for o in outcomes if not o.found]
        if misses:
            for site_id in misses:
                print(f"[-] 골든 정답 미검출: {site_id}", file=sys.stderr)

        result = MetricResult(
            metric="golden_recall",
            value=recall,
            threshold=1.0,  # 골든셋은 완전 일치여야 한다
            samples=len(outcomes),
            comparison="gte",
            extra={"misses": misses or None, "top_n": args.top_n},
        )
        sys.exit(int(emit(result)))

    # --- 평가 모드: 인지 엔진 필요 ------------------------------------------
    if args.pages is None:
        parser.error("--golden 또는 --pages 중 하나를 지정해야 합니다.")

    try:
        from perception import build_extractor  # type: ignore[import-not-found]
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "element_recall_at_20",
                    "인지 엔진(WS-2 perception/)이 아직 구현되지 않아 측정할 수 없습니다. "
                    "Stage 2 착수 후 실행하십시오.",
                )
            )
        )

    try:
        metrics = asyncio.run(_evaluate_engine(build_extractor(), args.pages, args.top_n))
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "element_recall_at_20",
                    "playwright 미설치. 'uv sync --extra dev' 및 "
                    "'playwright install chromium' 실행 필요.",
                )
            )
        )
    except Exception as exc:  # noqa: BLE001
        sys.exit(
            int(emit_error("element_recall_at_20", f"{type(exc).__name__}: {exc}"))
        )

    if metrics["misses"]:
        for site_id in metrics["misses"]:
            print(f"[-] 정답 미검출: {site_id}", file=sys.stderr)

    result = MetricResult(
        metric="element_recall_at_20",
        value=metrics["recall"],
        threshold=thresholds.RECALL_AT_20,
        samples=metrics["samples"],
        comparison="gte",
        extra={
            "top_n": args.top_n,
            "misses": metrics["misses"] or None,
            "p50_tokens": metrics["p50_tokens"],
            "p95_tokens": metrics["p95_tokens"],
            "p50_latency_ms": metrics["p50_latency_ms"],
            "p95_latency_ms": metrics["p95_latency_ms"],
            "token_threshold_p50": thresholds.OBSERVATION_TOKENS_P50,
            "latency_threshold_p50": thresholds.OBSERVE_LATENCY_MS_P50,
            "token_budget_ok": metrics["p50_tokens"] <= thresholds.OBSERVATION_TOKENS_P50,
            "latency_budget_ok": (
                metrics["p50_latency_ms"] <= thresholds.OBSERVE_LATENCY_MS_P50
            ),
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
