"""하네스 공통 출력 규약 (src/AGENTS.md §5).

모든 `harness.*` 실행 모듈은 본 모듈의 `emit()`으로 결과를 출력해야 한다.

규약:
1. 임계값은 하드코딩하지 않고 `contracts.thresholds`에서 읽는다.
2. stdout에 **단일 JSON 라인**을 출력한다.
   ``{"metric": str, "value": float, "threshold": float, "passed": bool, "samples": int}``
3. 임계값 미달 시 **exit code 1**, 실행 오류 시 **exit code 2**를 반환한다.

이 규약이 지켜져야 게이트가 "실행됐다"가 아니라 "통과했다"를 판정할 수 있다.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, Literal, Optional

#: 결과 JSON에 반드시 포함되어야 하는 키 (contract_selftest가 검증)
REQUIRED_KEYS = ("metric", "value", "threshold", "passed", "samples")

#: 비교 방향. "gte" = 값이 임계값 이상이어야 통과, "lte" = 이하여야 통과.
Comparison = Literal["gte", "lte"]


class ExitCode(IntEnum):
    """게이트 판정 종료 코드."""

    PASSED = 0
    THRESHOLD_NOT_MET = 1
    EXECUTION_ERROR = 2


@dataclass(frozen=True)
class MetricResult:
    """단일 지표 측정 결과."""

    metric: str
    value: float
    threshold: float
    samples: int
    comparison: Comparison = "gte"
    extra: Optional[Dict[str, Any]] = None

    @property
    def passed(self) -> bool:
        if self.comparison == "gte":
            return self.value >= self.threshold
        return self.value <= self.threshold

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "passed": self.passed,
            "samples": self.samples,
        }
        if self.extra:
            # 규약 키를 덮어쓰지 못하도록 보호한다.
            for key, val in self.extra.items():
                if key not in REQUIRED_KEYS:
                    payload[key] = val
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=False)


def emit(result: MetricResult, stream=None) -> ExitCode:
    """결과를 단일 JSON 라인으로 출력하고 종료 코드를 반환한다.

    호출부는 반환값을 그대로 `sys.exit()`에 전달해야 한다.
    """
    print(result.to_json(), file=stream or sys.stdout)
    return ExitCode.PASSED if result.passed else ExitCode.THRESHOLD_NOT_MET


def emit_error(metric: str, message: str, stream=None) -> ExitCode:
    """실행 오류를 규약 형태로 출력한다 (임계값 미달과 구분되는 exit 2)."""
    payload = {
        "metric": metric,
        "value": 0.0,
        "threshold": 0.0,
        "passed": False,
        "samples": 0,
        "error": message,
    }
    print(json.dumps(payload, ensure_ascii=False), file=stream or sys.stdout)
    return ExitCode.EXECUTION_ERROR
