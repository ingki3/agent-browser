"""단계 인식 자가 치유 사다리 (PRD §4.3).

액션 실행 직전 요소가 stale이면 4단계 사다리로 대체 요소를 찾는다:

1. Role + Accessible Name 검색
2. TestId (`data-testid`) 매칭
3. Text 콘텐츠 Levenshtein 유사도
4. CSS 경로 복구

**Shadow DOM 예외 (PRD §4.3)**: XPath는 Shadow Boundary를 통과할 수 없으므로
shadow 내부 요소는 4단계를 CSS Piercing으로 대체한다.

**단계 인식(Phase-Aware)의 의미**: 치유는 "부작용이 없는 시점"에만 안전하다.
dispatch 이전 실패는 언제나 치유 가능하지만, dispatch 이후 사후조건 미충족은
액션이 이미 발송된 상태라 재시도가 이중 제출을 유발할 수 있다.
`retry_safe` 판정이 이 경계를 담당한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Sequence

from contracts import ActionType
from perception import label_similarity
from perception.engine import ElementHandle

logger = logging.getLogger(__name__)

#: 텍스트 유사도 치유의 최소 임계값. 이보다 낮으면 다른 요소로 간주한다.
TEXT_SIMILARITY_THRESHOLD = 0.75

#: 상위 두 후보의 점수 차가 이 값보다 작으면 '모호'로 보고 치유를 포기한다.
#: 잘못된 요소를 클릭하는 것보다 실패를 보고하는 편이 안전하다.
AMBIGUITY_MARGIN = 0.05


class HealingStrategy(str, Enum):
    """자가 치유 전략 (PRD §4.3 사다리 순서)."""

    ROLE_NAME = "role_name"
    TESTID = "testid"
    TEXT_SIMILARITY = "text_similarity"
    CSS_PATH = "css_path"
    CSS_PIERCING = "css_piercing"  # shadow 내부 요소용 (XPath 대체)


#: 일반 요소의 사다리 순서
DEFAULT_LADDER = (
    HealingStrategy.ROLE_NAME,
    HealingStrategy.TESTID,
    HealingStrategy.TEXT_SIMILARITY,
    HealingStrategy.CSS_PATH,
)

#: shadow 내부 요소의 사다리 (4단계를 CSS Piercing으로 대체)
SHADOW_LADDER = (
    HealingStrategy.ROLE_NAME,
    HealingStrategy.TESTID,
    HealingStrategy.TEXT_SIMILARITY,
    HealingStrategy.CSS_PIERCING,
)


@dataclass
class HealingCandidate:
    """치유 후보 요소 (관찰 결과에서 가져온다)."""

    element_id: str
    role: str
    name: str
    css_path: str = ""
    testid: Optional[str] = None
    is_shadow: bool = False


@dataclass
class HealingResult:
    """치유 시도 결과."""

    healed: bool
    strategy: Optional[HealingStrategy] = None
    candidate: Optional[HealingCandidate] = None
    attempts: List[str] = field(default_factory=list)
    reason: str = ""


def ladder_for(handle: ElementHandle) -> Sequence[HealingStrategy]:
    """요소 특성에 맞는 사다리를 선택한다."""
    return SHADOW_LADDER if handle.is_shadow else DEFAULT_LADDER


def heal(
    target: ElementHandle,
    candidates: Sequence[HealingCandidate],
    *,
    similarity_threshold: float = TEXT_SIMILARITY_THRESHOLD,
) -> HealingResult:
    """사다리를 순서대로 시도해 대체 요소를 찾는다.

    상위 단계가 성공하면 즉시 반환한다. 하위 단계일수록 오탐 위험이
    크므로 순서를 건너뛰지 않는다.
    """
    attempts: List[str] = []

    for strategy in ladder_for(target):
        attempts.append(strategy.value)

        if strategy is HealingStrategy.ROLE_NAME:
            for c in candidates:
                if c.role == target.role and c.name == target.name:
                    return HealingResult(True, strategy, c, attempts, "role+name 정확 일치")

        elif strategy is HealingStrategy.TESTID:
            if target.testid:
                for c in candidates:
                    if c.testid and c.testid == target.testid:
                        return HealingResult(True, strategy, c, attempts, "testid 일치")

        elif strategy is HealingStrategy.TEXT_SIMILARITY:
            # role이 다르면 의미가 다른 요소이므로 제외한다.
            scored = sorted(
                (
                    (label_similarity(target.name, c.name), c)
                    for c in candidates
                    if c.role == target.role
                ),
                key=lambda pair: pair[0],
                reverse=True,
            )
            passing = [(s, c) for s, c in scored if s >= similarity_threshold]

            if len(passing) >= 2 and (passing[0][0] - passing[1][0]) < AMBIGUITY_MARGIN:
                # 예: '삭제'가 사라진 자리에 '삭제 취소'와 '삭제 확인'이 함께
                # 남은 경우. 어느 쪽을 눌러도 부작용이 크므로 치유하지 않고
                # 다음 단계로 넘긴다. 잘못 치유하는 것보다 실패가 안전하다.
                logger.debug(
                    "텍스트 유사도 모호: %r vs %r (%.3f, %.3f)",
                    passing[0][1].name,
                    passing[1][1].name,
                    passing[0][0],
                    passing[1][0],
                )
                attempts[-1] = f"{strategy.value}(ambiguous)"
            elif passing:
                best_score, best = passing[0]
                return HealingResult(
                    True,
                    strategy,
                    best,
                    attempts,
                    f"텍스트 유사도 {best_score:.2f}",
                )

        elif strategy in (HealingStrategy.CSS_PATH, HealingStrategy.CSS_PIERCING):
            if target.css_path:
                for c in candidates:
                    if c.css_path and c.css_path == target.css_path:
                        return HealingResult(
                            True, strategy, c, attempts, "CSS 경로 일치"
                        )

    return HealingResult(
        False, None, None, attempts, "모든 치유 전략이 대체 요소를 찾지 못함"
    )


# ---------------------------------------------------------------------------
# retry_safe 판정 (PRD §4.1, §4.3)
# ---------------------------------------------------------------------------


class FailurePhase(str, Enum):
    """실패가 발생한 단계."""

    PRE_DISPATCH = "pre_dispatch"  # 이벤트 발송 전 (부작용 없음)
    POST_DISPATCH = "post_dispatch"  # 발송 후 사후조건 미충족


#: 본질적으로 멱등한 액션 (발송 후에도 재시도 안전)
IDEMPOTENT_ACTIONS = frozenset(
    {
        ActionType.OBSERVE_PAGE,
        ActionType.TAKE_SCREENSHOT,
        ActionType.NAVIGATE,
        ActionType.GO_BACK,
        ActionType.RELOAD,
        ActionType.SELECT_OPTION,
        ActionType.CHECK_BOX,
        ActionType.SCROLL,
        ActionType.HOVER,
        ActionType.WAIT_FOR,
        ActionType.EXTRACT,
        ActionType.SWITCH_FRAME,
    }
)

#: 발송 후 재시도가 이중 부작용을 낳는 액션
SIDE_EFFECT_ACTIONS = frozenset(
    {
        ActionType.CLICK,
        ActionType.TYPE_TEXT,
        ActionType.PRESS_KEY,
        ActionType.HANDLE_DIALOG,
        ActionType.UPLOAD_FILE,
        ActionType.DOWNLOAD_FILE,
    }
)


def is_retry_safe(action: ActionType, phase: FailurePhase) -> bool:
    """(액션 × 실패 단계)로 재시도 안전성을 판정한다.

    dispatch 이전 실패는 브라우저에 아무 이벤트도 가지 않았으므로
    어떤 액션이든 안전하다. dispatch 이후는 멱등 액션만 안전하다.
    """
    if phase is FailurePhase.PRE_DISPATCH:
        return True
    return action in IDEMPOTENT_ACTIONS
