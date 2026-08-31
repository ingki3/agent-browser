"""실환경 인지 성능 측정 (Mock 대비 격차 확인).

`python -m harness.realworld --report artifacts/realworld.json`

**이 하네스는 판정 게이트가 아니다.** 외부 네트워크에 의존하므로
CI 필수 게이트로 쓰면 사이트 변경·네트워크 장애가 곧 빌드 실패가 된다.
목적은 Mock 환경 수치와 실제 웹의 격차를 수치로 드러내는 것이다.

배경:
Gate 2/3-B는 전부 Mock 사이트에서 측정됐다. Mock의 최대 후보 수는 69개인데
실제 위키백과 문서는 1,000개를 넘는다. 이 격차가 지표에 어떤 영향을 주는지
확인하지 않으면 "Recall 1.0"을 실제 성능으로 오해하게 된다.

측정 항목:
* 후보 요소 수 / 관찰 토큰 / 관찰 지연
* 점수 밀집도 — 상위 밴드에 몇 개가 몰려 Top-N 자리를 다투는가
* 대표 UI 요소(로그인·검색 등)가 Top-N에 드는가
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness.result import MetricResult, emit, emit_error

#: 측정 대상. 로그인 불필요, robots.txt 허용, 구조가 안정적인 공개 페이지만 사용한다.
#: (site_id, url, 기대 UI 요소 목록)
REALWORLD_PAGES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("example", "https://example.com", ()),
    (
        "wikipedia_article",
        "https://en.wikipedia.org/wiki/Web_browser",
        ("Search", "Create account", "Log in"),
    ),
    (
        "wikipedia_main",
        "https://en.wikipedia.org/wiki/Main_Page",
        ("Search", "Create account"),
    ),
    ("hackernews", "https://news.ycombinator.com", ("login",)),
    ("mdn", "https://developer.mozilla.org/en-US/", ()),
)

#: 상위 밴드 폭. 이 안에 든 후보들은 사실상 동점으로 경쟁한다.
SCORE_BAND = 0.3

DEFAULT_TOP_N = 20


async def _measure_page(
    page: Any, url: str, expected: Tuple[str, ...], top_n: int
) -> Dict[str, Any]:
    from perception import prune
    from perception.sanitizer import collect

    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(800)

    started = time.perf_counter()
    snapshot = await collect(page)
    ranked = prune(snapshot.elements, 100_000)
    elapsed_ms = (time.perf_counter() - started) * 1000

    def tokens(rows: List[Any]) -> int:
        return len(" ".join(f"{r.role} {r.name}" for r in rows)) // 4

    top = ranked[:top_n]
    band = [s for s in ranked if ranked and s.score >= ranked[0].score - SCORE_BAND]

    # 기대 UI 요소의 순위
    ranks: Dict[str, Optional[int]] = {}
    for label in expected:
        hit = next(
            (i for i, s in enumerate(ranked, 1) if s.name.strip().lower() == label.lower()),
            None,
        )
        ranks[label] = hit

    found_in_top = sum(1 for r in ranks.values() if r is not None and r <= top_n)

    return {
        "candidates": len(snapshot.elements),
        "tokens_top_n": tokens(top),
        "tokens_full": tokens(ranked),
        "observe_ms": round(elapsed_ms, 1),
        "band_size": len(band),
        "band_contention": round(len(band) / top_n, 2) if top_n else 0.0,
        "expected_ranks": ranks,
        "expected_total": len(expected),
        "expected_in_top_n": found_in_top,
    }


async def _run(top_n: int) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    results: Dict[str, Any] = {}
    errors: List[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        for site_id, url, expected in REALWORLD_PAGES:
            try:
                results[site_id] = await _measure_page(page, url, expected, top_n)
                results[site_id]["url"] = url
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{site_id}: {type(exc).__name__} {str(exc)[:80]}")

        await context.close()
        await browser.close()

    return {"pages": results, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description="실환경 인지 성능 측정 (판정 아님)")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--report", type=str, default="", help="JSON 리포트 저장 경로")
    args = parser.parse_args()

    try:
        data = asyncio.run(_run(args.top_n))
    except Exception as exc:  # noqa: BLE001
        sys.exit(int(emit_error("realworld_report", f"측정 실패: {exc}")))

    pages = data["pages"]
    if not pages:
        sys.exit(
            int(
                emit_error(
                    "realworld_report",
                    "측정된 페이지가 없습니다. 네트워크 접근을 확인하십시오. "
                    f"오류: {'; '.join(data['errors'][:3])}",
                )
            )
        )

    for err in data["errors"]:
        print(f"[!] 접근 실패(측정 제외): {err}", file=sys.stderr)

    # --- 요약 통계 ---
    candidates = [p["candidates"] for p in pages.values()]
    tokens = [p["tokens_top_n"] for p in pages.values()]
    latencies = [p["observe_ms"] for p in pages.values()]
    contention = [p["band_contention"] for p in pages.values()]

    expected_total = sum(p["expected_total"] for p in pages.values())
    expected_hit = sum(p["expected_in_top_n"] for p in pages.values())

    for site_id, p in sorted(pages.items()):
        misses = {k: v for k, v in p["expected_ranks"].items() if v is None or v > args.top_n}
        note = f"  Top-{args.top_n} 밖: {misses}" if misses else ""
        print(
            f"[*] {site_id:20} 후보 {p['candidates']:>5}  "
            f"토큰 {p['tokens_top_n']:>4}  관찰 {p['observe_ms']:>6.1f}ms  "
            f"동점밴드 {p['band_size']:>3}개{note}",
            file=sys.stderr,
        )

    report = {
        "pages": pages,
        "errors": data["errors"],
        "summary": {
            "pages_measured": len(pages),
            "max_candidates": max(candidates),
            "median_candidates": int(statistics.median(candidates)),
            "median_tokens_top_n": int(statistics.median(tokens)),
            "median_observe_ms": round(statistics.median(latencies), 1),
            "max_band_contention": max(contention),
            "expected_elements_total": expected_total,
            "expected_in_top_n": expected_hit,
        },
    }

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[*] 리포트 저장: {path}", file=sys.stderr)

    # 판정 게이트가 아니므로 '리포트 산출 여부'만 지표로 삼는다.
    result = MetricResult(
        metric="realworld_report",
        value=1.0,
        threshold=1.0,
        samples=len(pages),
        comparison="gte",
        extra={
            **report["summary"],
            "note": "판정 게이트 아님 — Mock 대비 격차 관측용",
            "unreachable": len(data["errors"]) or None,
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
