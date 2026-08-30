"""WS-3 액션 스페이스 패키지 (PRD §4.1, §4.3).

* `ActionDispatcher` — 19종 액션 실행기 (staleness → dispatch → 사후조건)
* `heal`            — 4단계 자가 치유 사다리
* `is_retry_safe`   — (액션 × 실패 단계) 재시도 안전성 판정
* `verify_staleness` / `verify_post_condition` — 실행 전후 검증
"""

from actions.dispatcher import (
    EPOCH_BUMPING_ACTIONS,
    ActionDispatcher,
    DispatchContext,
)
from actions.healing import (
    DEFAULT_LADDER,
    IDEMPOTENT_ACTIONS,
    SHADOW_LADDER,
    SIDE_EFFECT_ACTIONS,
    TEXT_SIMILARITY_THRESHOLD,
    FailurePhase,
    HealingCandidate,
    HealingResult,
    HealingStrategy,
    heal,
    is_retry_safe,
    ladder_for,
)
from actions.verification import (
    PageStateSnapshot,
    PostConditionResult,
    StalenessReason,
    StalenessResult,
    capture_state,
    verify_post_condition,
    verify_staleness,
)

__all__ = [
    # 디스패처
    "ActionDispatcher",
    "DispatchContext",
    "EPOCH_BUMPING_ACTIONS",
    # 자가 치유
    "heal",
    "ladder_for",
    "is_retry_safe",
    "HealingStrategy",
    "HealingCandidate",
    "HealingResult",
    "FailurePhase",
    "DEFAULT_LADDER",
    "SHADOW_LADDER",
    "IDEMPOTENT_ACTIONS",
    "SIDE_EFFECT_ACTIONS",
    "TEXT_SIMILARITY_THRESHOLD",
    # 검증
    "verify_staleness",
    "verify_post_condition",
    "capture_state",
    "StalenessResult",
    "StalenessReason",
    "PostConditionResult",
    "PageStateSnapshot",
]
