"""KPI 임계값 상수 (Stage 0 동결).

PRD.md §1.5 "정량적 성공 지표 및 통일된 수치 계약" 표의 유일한 코드 표현이다.

`harness.*` 모듈은 임계값을 하드코딩하지 않고 반드시 본 모듈에서 읽어야 한다
(src/AGENTS.md §5 하네스 공통 출력 규약). 그래야 PRD, 게이트 명령어, 하네스
구현 세 곳의 수치가 갈라지지 않는다.
"""

# ---------------------------------------------------------------------------
# 인프라 코어 (MVP v1.0)
# ---------------------------------------------------------------------------

#: 프루닝 후 Top-20 요소 내 정답 포함 비율 (WebArena 100개 샘플 페이지)
RECALL_AT_20: float = 0.95

#: 단일 스텝 입력 토큰 (tiktoken cl100k_base 기준)
OBSERVATION_TOKENS_P50: int = 2_500
OBSERVATION_TOKENS_P95: int = 6_500

#: 스텝당 로컬 순수 지연 (외부 네트워크 지연 제외)
OBSERVE_LATENCY_MS_P50: int = 300  # (a) 관찰 / 프루닝 구간
ACTION_LATENCY_MS_P50: int = 500  # (b) 단일 액션 실행 / 사후검증 구간
STEP_LATENCY_MS_P50: int = 800  # (a) + (b) 합계

#: 복합 스텝 지연 (자가치유 1회전 포함, 외부 원격 서버 응답 제외)
COMPLEX_LATENCY_MS_P95: int = 2_200

#: 정상 식별 요소에 대한 이벤트 트리거 성공률
ACTION_SUCCESS_RATE: float = 0.92

#: 셀렉터 불일치 시 자가 복구 성공률
SELF_HEALING_RATE: float = 0.80

#: 동일 고정 Mock 사이트 100회 반복 시 비결정론적 실패율
FLAKY_RATE: float = 0.02

# ---------------------------------------------------------------------------
# 에이전트 참조 성능 (MVP v1.0)
# ---------------------------------------------------------------------------

#: WebArena Lite 100개 태스크 완수율 (Claude 3.7 Sonnet 고정, 최대 15스텝)
TASK_SUCCESS_RATE: float = 0.60

#: 스트레치 목표 (게이트 판정 기준 아님)
TASK_SUCCESS_RATE_STRETCH: float = 0.68

# ---------------------------------------------------------------------------
# 보안 (MVP v1.0 / v1.1)
# ---------------------------------------------------------------------------

#: 도메인 / Egress 1차 방어선 결정론적 IPI 차단율 (MVP)
WASP_DETERMINISTIC_BLOCK_RATE: float = 0.90

#: 3중 방어 종합 IPI 차단율 (v1.1)
WASP_FULL_BLOCK_RATE: float = 0.98
STAKEBENCH_BLOCK_RATE: float = 0.96

#: 가드레일 오탐율 상한
GUARDRAIL_FPR: float = 0.02

#: 세션 만료 감지 프로브 오탐율 상한 (PRD §5.1)
SESSION_PROBE_FPR: float = 0.01

#: 가드레일 미적용 베이스라인 (참고용, 판정 기준 아님)
WASP_BASELINE: float = 0.35
STAKEBENCH_BASELINE: float = 0.42

# ---------------------------------------------------------------------------
# 비용 및 효율
# ---------------------------------------------------------------------------

#: Tier-2 SoM 발동 비율 상한 (무인 모드 실행분 기준, v1.1)
TIER2_TRIGGER_RATE: float = 0.10

#: Tier-2 VLM 왕복 지연 (외부 API 왕복을 유일하게 포함, v1.1)
TIER2_VLM_LATENCY_MS_P95: int = 3_500

#: Tier-2 시각 폴백 태스크당 호출 상한 (PRD §3.3)
TIER2_MAX_CALLS_UNATTENDED: int = 3
TIER2_MAX_CALLS_INTERACTIVE: int = 5

#: 스텝당 LLM 호출 예산
LLM_CALLS_PER_STEP_DEFAULT: int = 1
LLM_CALLS_PER_STEP_MAX: int = 3

# ---------------------------------------------------------------------------
# 전역 루프 및 리소스 가드 (PRD §3.4)
# ---------------------------------------------------------------------------

#: 단일 태스크 최대 스텝 수
MAX_STEPS_PER_TASK: int = 30

#: 단일 태스크 최대 Wall-Clock Time (초)
MAX_WALL_CLOCK_SECONDS: int = 600

#: 태스크당 누적 LLM 사용량 상한 (선도달 기준 적용)
MAX_TOKENS_PER_TASK: int = 100_000
MAX_USD_PER_TASK: float = 0.75

#: 동일 (action, selector, value) 조합 연속 실패 허용 횟수
MAX_CONSECUTIVE_IDENTICAL_FAILURES: int = 3

#: 세션 리소스 상한
MAX_TABS_PER_SESSION: int = 10
MAX_ACTIVE_CONTEXTS: int = 5
MEMORY_LIMIT_BYTES: int = 1_610_612_736  # 1.5 GiB

# ---------------------------------------------------------------------------
# 관찰 및 렌더링 기준
# ---------------------------------------------------------------------------

#: observe_page 기본 프루닝 상한
DEFAULT_PRUNE_TOP_N: int = 20

#: SoM 스크린샷 기준 뷰포트 (PRD §3.4 이미지 토큰 산정 기준)
VIEWPORT_WIDTH: int = 1_280
VIEWPORT_HEIGHT: int = 720

#: 위 뷰포트 기준 SoM 캡처 1장당 추정 이미지 토큰
SOM_IMAGE_TOKENS_PER_CAPTURE: int = 1_600
