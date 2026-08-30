"""Mock 사이트 기동 및 커버리지 검증 (Gate 1 항목 6).

`python -m harness.selfcheck --mock-sites 20`

개수만 세지 않는다. 다음을 함께 확인한다:
1. 선언된 사이트 수가 인자와 일치하는가
2. 13대 필수 시나리오가 전수 커버되는가 (빈 HTML 20개 방지)
3. 각 사이트가 실제로 HTTP 200과 기대 콘텐츠를 반환하는가
4. 골든셋이 사이트 정의와 정합한가
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from typing import List

from harness.golden_set import DENSE_CASE_SITE_ID, GOLDEN_SET, validate_golden_set
from harness.mock_sites import (
    MOCK_SITES,
    Scenario,
    MockServer,
    covered_scenarios,
    missing_scenarios,
)
from harness.result import MetricResult, emit, emit_error


def _fetch(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def run_checks(expected_sites: int) -> List[str]:
    """검증 실패 항목 목록을 반환한다."""
    problems: List[str] = []

    # 1. 사이트 수
    if len(MOCK_SITES) != expected_sites:
        problems.append(
            f"사이트 수 불일치: 선언 {len(MOCK_SITES)}종, 기대 {expected_sites}종"
        )

    # 2. site_id 중복
    ids = [s.site_id for s in MOCK_SITES]
    if len(set(ids)) != len(ids):
        problems.append("site_id 중복 존재")

    # 3. 13대 시나리오 전수 커버
    missing = missing_scenarios()
    if missing:
        problems.append(f"미커버 시나리오: {[m.value for m in missing]}")
    if len(Scenario) != 13:
        problems.append(f"필수 시나리오 정의가 13종이 아님: {len(Scenario)}종")

    # 4. 골든셋 정합성
    problems.extend(f"골든셋: {p}" for p in validate_golden_set())
    if len(GOLDEN_SET) < 10:
        problems.append(f"골든셋이 10종 미만: {len(GOLDEN_SET)}종")
    if not any(c.site_id == DENSE_CASE_SITE_ID for c in GOLDEN_SET):
        problems.append(
            f"골든셋에 고밀도 케이스({DENSE_CASE_SITE_ID})가 없음 "
            "— Top-N 프루닝이 검증되지 않는다"
        )

    # 5. 실제 기동 검증
    with MockServer() as server:
        status, index = _fetch(server.base_url)
        if status != 200:
            problems.append(f"인덱스 페이지 응답 {status}")

        for site in MOCK_SITES:
            status, body = _fetch(server.site_url(site.site_id))
            if status != 200:
                problems.append(f"{site.site_id}: HTTP {status}")
                continue
            if site.title not in body:
                problems.append(f"{site.site_id}: 제목 '{site.title}' 미포함")
            if len(body) < 120:
                problems.append(f"{site.site_id}: 본문이 비어 있음 ({len(body)}바이트)")
            if site.golden_target and site.golden_target not in body:
                problems.append(
                    f"{site.site_id}: 골든 정답 '{site.golden_target}' 미포함"
                )

        # 6. 시나리오 특수 동작 검증
        status, _ = _fetch(f"{server.site_url('s12_session_expiry')}/protected")
        if status != 401:
            problems.append(f"세션 만료 시나리오가 401을 반환하지 않음: {status}")

        status, csv = _fetch(f"{server.site_url('s04_download')}/report.csv")
        if status != 200 or "id,name,amount" not in csv:
            problems.append("CSV 다운로드 시나리오 응답 이상")

        status, inner = _fetch(f"{server.base_url}/s05_iframe/inner")
        if status != 200 or "pay-inner" not in inner:
            problems.append("중첩 iframe 하위 문서 응답 이상")

    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock 사이트 기동 및 커버리지 검증")
    parser.add_argument("--mock-sites", type=int, default=20, help="기대 사이트 수")
    args = parser.parse_args()

    try:
        problems = run_checks(args.mock_sites)
    except Exception as exc:  # noqa: BLE001
        sys.exit(int(emit_error("mock_sites_up", f"{type(exc).__name__}: {exc}")))

    coverage = covered_scenarios()
    sites_up = len(MOCK_SITES) - len({p.split(":")[0] for p in problems if ":" in p})

    result = MetricResult(
        metric="mock_sites_up",
        value=float(sites_up if not problems else 0),
        threshold=float(args.mock_sites),
        samples=len(MOCK_SITES),
        comparison="gte",
        extra={
            "scenarios_covered": len([s for s, v in coverage.items() if v]),
            "scenarios_required": len(Scenario),
            "golden_cases": len(GOLDEN_SET),
            "violations": problems or None,
        },
    )
    if problems:
        for p in problems:
            print(f"[-] {p}", file=sys.stderr)
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
