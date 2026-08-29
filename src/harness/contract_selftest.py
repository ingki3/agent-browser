"""하네스 출력 계약 자체 검증 (Gate 1 항목 3).

`python -m harness.contract_selftest`

하네스가 규약을 지키는지 하네스로 검증한다. 구체적으로:
1. 통과 케이스가 exit 0 + `passed=true`를 내는가
2. **의도적 미달 케이스가 exit 1을 내는가** (항상 0을 반환하는 하네스 차단)
3. 출력이 규약 키 5종을 모두 포함하는 단일 JSON 라인인가
4. 실행 오류가 exit 2로 구분되는가
5. 임계값이 `contracts.thresholds`에서 오는가 (하드코딩 금지)

이 검사가 없으면 하네스가 무조건 통과를 반환해도 게이트가 알아채지 못한다.
"""

from __future__ import annotations

import io
import json
import sys
from typing import List

from contracts import thresholds

from harness.result import REQUIRED_KEYS, ExitCode, MetricResult, emit, emit_error


def _capture(result: MetricResult) -> tuple[dict, ExitCode]:
    """emit() 출력을 가로채 (payload, exit_code)로 반환한다."""
    buffer = io.StringIO()
    code = emit(result, stream=buffer)
    lines = [ln for ln in buffer.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1, f"단일 JSON 라인이 아님: {len(lines)}줄"
    return json.loads(lines[0]), code


def run_checks() -> List[str]:
    """규약 위반 목록을 반환한다 (빈 리스트면 통과)."""
    problems: List[str] = []

    # --- 1. 통과 케이스: exit 0 -------------------------------------------
    payload, code = _capture(
        MetricResult(
            metric="selftest_pass",
            value=0.97,
            threshold=thresholds.RECALL_AT_20,
            samples=100,
            comparison="gte",
        )
    )
    if code != ExitCode.PASSED:
        problems.append(f"통과 케이스가 exit {int(code)}를 반환 (0이어야 함)")
    if payload.get("passed") is not True:
        problems.append("통과 케이스의 passed가 true가 아님")

    # --- 2. 미달 케이스: exit 1 (가장 중요) -------------------------------
    payload, code = _capture(
        MetricResult(
            metric="selftest_fail",
            value=0.10,
            threshold=thresholds.RECALL_AT_20,
            samples=100,
            comparison="gte",
        )
    )
    if code != ExitCode.THRESHOLD_NOT_MET:
        problems.append(
            f"임계값 미달인데 exit {int(code)}를 반환 (1이어야 함) "
            "— 게이트가 무력화됩니다"
        )
    if payload.get("passed") is not False:
        problems.append("미달 케이스의 passed가 false가 아님")

    # --- 3. lte 비교 방향 --------------------------------------------------
    _, code = _capture(
        MetricResult(
            metric="selftest_lte_fail",
            value=0.50,
            threshold=thresholds.FLAKY_RATE,
            samples=100,
            comparison="lte",
        )
    )
    if code != ExitCode.THRESHOLD_NOT_MET:
        problems.append("lte 비교에서 미달이 감지되지 않음")

    # --- 4. 규약 키 5종 존재 ----------------------------------------------
    payload, _ = _capture(
        MetricResult(metric="selftest_keys", value=1.0, threshold=1.0, samples=1)
    )
    missing = [k for k in REQUIRED_KEYS if k not in payload]
    if missing:
        problems.append(f"규약 키 누락: {missing}")

    # --- 5. extra가 규약 키를 덮어쓰지 못하는가 ----------------------------
    payload, _ = _capture(
        MetricResult(
            metric="selftest_extra",
            value=1.0,
            threshold=1.0,
            samples=1,
            extra={"passed": False, "note": "덮어쓰기 시도"},
        )
    )
    if payload.get("passed") is not True:
        problems.append("extra가 규약 키 passed를 덮어씀")
    if payload.get("note") != "덮어쓰기 시도":
        problems.append("extra의 비규약 키가 반영되지 않음")

    # --- 6. 실행 오류: exit 2 ---------------------------------------------
    buffer = io.StringIO()
    code = emit_error("selftest_error", "의도적 오류", stream=buffer)
    if code != ExitCode.EXECUTION_ERROR:
        problems.append(f"실행 오류가 exit {int(code)}를 반환 (2여야 함)")
    err_payload = json.loads(buffer.getvalue().strip())
    if err_payload.get("passed") is not False or "error" not in err_payload:
        problems.append("오류 페이로드 형식이 규약과 다름")

    # --- 7. 임계값이 thresholds에서 오는가 --------------------------------
    if thresholds.RECALL_AT_20 != 0.95 or thresholds.FLAKY_RATE != 0.02:
        problems.append(
            "contracts.thresholds 값이 PRD §1.5와 불일치 — 하네스 임계값 공급원 오염"
        )

    return problems


def main() -> None:
    problems = run_checks()
    result = MetricResult(
        metric="harness_contract_compliance",
        value=0.0 if problems else 1.0,
        threshold=1.0,
        samples=7,
        comparison="gte",
        extra={"violations": problems} if problems else None,
    )
    if problems:
        for p in problems:
            print(f"[-] {p}", file=sys.stderr)
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
