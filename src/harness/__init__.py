"""WS-6 평가 하네스 패키지.

모든 실행 모듈은 `harness.result`의 공통 출력 규약을 따른다:
stdout에 단일 JSON 라인, 임계값 미달 시 exit 1, 실행 오류 시 exit 2.

Gate 1 대응 모듈:
* `harness.contract_selftest` — 출력 규약 자체 검증
* `harness.egress_test`       — 비인가 Egress 차단 (WS-4 의존)
* `harness.selfcheck`         — Mock 20종 기동 및 13대 시나리오 커버리지
* `harness.recall --golden`   — 골든셋 10종 정합성 (하네스 자체 검증)
* `harness.engine_spike`      — 엔진 지연 실측 리포트
"""

from harness.golden_set import GOLDEN_SET, GoldenCase, validate_golden_set
from harness.mock_sites import (
    MOCK_SITES,
    SITE_INDEX,
    MockServer,
    MockSite,
    Scenario,
    covered_scenarios,
    missing_scenarios,
)
from harness.result import (
    REQUIRED_KEYS,
    ExitCode,
    MetricResult,
    emit,
    emit_error,
)

__all__ = [
    # 출력 규약
    "MetricResult",
    "ExitCode",
    "emit",
    "emit_error",
    "REQUIRED_KEYS",
    # Mock 사이트
    "MOCK_SITES",
    "SITE_INDEX",
    "MockSite",
    "MockServer",
    "Scenario",
    "covered_scenarios",
    "missing_scenarios",
    # 골든셋
    "GOLDEN_SET",
    "GoldenCase",
    "validate_golden_set",
]
