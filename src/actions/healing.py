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

from actions.verification import StalenessReason, safe_page_text
from perception.scorer import _normalize as _normalize_label

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
    #: 사다리의 어느 단계가 후보를 골랐지만 role/(정규화) name 이 원래와 달라 부작용
    #: 액션이라 채택을 거부한 첫 후보(WS-37 R2). 호출자가 "요소가 바뀜(이전 → 현재)"
    #: 안내를 만드는 데 쓴다.
    identity_refused: Optional[HealingCandidate] = None


#: 페이지 상태를 바꾸지 않는(읽기) 액션. 경로 단계 치유는 이 액션들에만 허용한다.
#: hover 는 메뉴를 펼칠 수 있지만 되돌릴 수 있는 표시 변화일 뿐 제출·입력·선택이
#: 아니므로 읽기 쪽에 둔다. 이 집합 밖(미지 액션 포함)은 전부 부작용으로 본다
#: (fail-closed).
READ_ONLY_ACTIONS = frozenset(
    {
        ActionType.OBSERVE_PAGE,
        ActionType.TAKE_SCREENSHOT,
        ActionType.SCROLL,
        ActionType.HOVER,
        ActionType.WAIT_FOR,
        ActionType.EXTRACT,
    }
)


def path_heal_allowed(action: Optional[ActionType]) -> bool:
    """치유 사다리가 role·name 이 다른 요소를 대신 써도 되는 액션인가(읽기 액션만).

    이름은 경로 단계(4단계)에서 왔지만 WS-37 R2 부터 **모든 단계**의 후보 채택에 쓴다
    (`heal` 의 채택 지점). 2단계(testid 유지)·3단계(비슷한 이름)·4단계(같은 경로)가 고른
    이름·역할이 다른 요소를 부작용 액션이 누르면 '결제하기' 대신 '회원 탈퇴'·'결제하기 취소'
    를 누르게 되므로 읽기 액션에만 허용한다. action 을 모르면(None) 허용하지 않는다.
    """
    return action is not None and action in READ_ONLY_ACTIONS


#: 같은 자리 요소의 신원(role/name)이 바뀌었다는 staleness 사유.
IDENTITY_CHANGE_REASONS = frozenset(
    {StalenessReason.NAME_CHANGED, StalenessReason.ROLE_CHANGED}
)


def identity_change_refused(
    reason: Optional[StalenessReason], action: Optional[ActionType]
) -> bool:
    """치유 사다리를 **돌리기 전에** 거부해야 하는가 (WS-37 R1, 사용자 결정 A).

    부작용 액션(`path_heal_allowed` 가 거짓 — 읽기 액션 밖 전부, 모르는 액션 포함)에서
    staleness 가 NAME_CHANGED/ROLE_CHANGED 면 관찰했던 그 자리의 요소가 다른 요소로
    바뀐 것이다. 사다리의 어느 단계로 고르든(testid 유지·유사한 이름·같은 경로)
    '결제하기' 자리의 '회원 탈퇴'·'결제하기 취소'를 누를 수 있으므로 단계별 검사가
    아니라 앞단에서 한 번에 막는다. 요소가 사라진 경우(NODE_DETACHED)는 사다리를 돌되
    `heal` 의 채택 지점이 role/정규화 name 이 같은 후보만 받는다(WS-37 R2). 읽기 액션은
    현행 치유 그대로다.
    """
    return reason in IDENTITY_CHANGE_REASONS and not path_heal_allowed(action)


def ladder_for(handle: ElementHandle) -> Sequence[HealingStrategy]:
    """요소 특성에 맞는 사다리를 선택한다."""
    return SHADOW_LADDER if handle.is_shadow else DEFAULT_LADDER


def _norm_name(name: Optional[str]) -> str:
    """신원 비교용 이름 정규화: 공백 접기·앞뒤 공백 제거·대소문자(3단계 유사도와 같은 규칙)."""
    return _normalize_label(name or "")


def same_identity(target: ElementHandle, candidate: HealingCandidate) -> bool:
    """후보가 원래 요소와 같은 신원(role 일치 + 정규화 name 일치)인가.

    '결제하기' 와 '결제하기 취소'·'결제 하기' 는 다른 이름이다(공백은 접을 뿐 지우지 않는다).
    """
    return candidate.role == target.role and _norm_name(candidate.name) == _norm_name(
        target.name
    )


def heal(
    target: ElementHandle,
    candidates: Sequence[HealingCandidate],
    *,
    action: Optional[ActionType] = None,
    similarity_threshold: float = TEXT_SIMILARITY_THRESHOLD,
) -> HealingResult:
    """사다리를 순서대로 시도해 대체 요소를 찾는다.

    상위 단계가 성공하면 즉시 반환한다. 하위 단계일수록 오탐 위험이
    크므로 순서를 건너뛰지 않는다.

    **후보 채택 규칙 (WS-37 R2, 사용자 결정 A 확장)**: 각 단계는 후보를 *고르기만* 하고,
    채택 여부는 아래 한 곳에서 정한다. ``action`` 이 읽기 액션(`READ_ONLY_ACTIONS`)이 아니면
    (모르는 액션 None 포함 — fail-closed) 어떤 단계가 골랐든 role 과 정규화한 name 이 원래와
    같은 후보만 채택한다(`same_identity`). 요소가 사라진(NODE_DETACHED) 자리에 같은 testid 의
    '회원 탈퇴'(2단계)나 '결제하기 취소'(3단계)가 들어와도 부작용 액션으로 누르지 않는다.
    거부한 첫 후보는 ``identity_refused`` 에 담고 다음 단계를 계속 본다 — 뒤 단계가 같은
    신원의 후보를 찾으면 그것을 쓴다. 읽기 액션은 현행 그대로 이름·역할이 다른 후보도 쓴다.

    디스패처는 부작용 액션 + NAME/ROLE_CHANGED 를 사다리 **앞단**에서 이미 거부한다
    (`identity_change_refused`). 이 채택 규칙은 앞단 가드가 다루지 않는 사유(요소가 사라진
    NODE_DETACHED)를 막고, heal() 을 직접 부르는 호출자(하네스·테스트)에게도 같은 계약을
    지킨다. (디스패처 경로에서 EPOCH_MISMATCH 는 핸들 조회가 먼저 막으므로 사다리에 닿지 않는다.)
    """
    attempts: List[str] = []
    refused: Optional[HealingCandidate] = None
    any_identity = path_heal_allowed(action)

    for strategy in ladder_for(target):
        attempts.append(strategy.value)
        pick: Optional[HealingCandidate] = None
        why = ""

        if strategy is HealingStrategy.ROLE_NAME:
            for c in candidates:
                if c.role == target.role and c.name == target.name:
                    pick, why = c, "role+name 정확 일치"
                    break

        elif strategy is HealingStrategy.TESTID:
            if target.testid:
                for c in candidates:
                    if c.testid and c.testid == target.testid:
                        pick, why = c, "testid 일치"
                        break

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
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "텍스트 유사도 모호: %r vs %r (%.3f, %.3f)",
                        safe_page_text(passing[0][1].name),
                        safe_page_text(passing[1][1].name),
                        passing[0][0],
                        passing[1][0],
                    )
                attempts[-1] = f"{strategy.value}(ambiguous)"
            elif passing:
                best_score, pick = passing[0]
                why = f"텍스트 유사도 {best_score:.2f}"

        elif strategy in (HealingStrategy.CSS_PATH, HealingStrategy.CSS_PIERCING):
            if target.css_path:
                for c in candidates:
                    if c.css_path and c.css_path == target.css_path:
                        pick, why = c, "CSS 경로 일치"
                        break

        if pick is None:
            continue
        # --- 후보 채택 지점 (모든 단계 공통, 한 곳) ---
        if any_identity or same_identity(target, pick):
            return HealingResult(True, strategy, pick, attempts, why)
        # 부작용 액션인데 다른 요소다(이름·역할이 바뀜). 누르지 않는다 — 재관찰이 정답이다.
        if refused is None:
            refused = pick
        attempts[-1] = f"{strategy.value}(identity_changed)"

    if refused is not None:
        return HealingResult(
            False,
            None,
            None,
            attempts,
            (
                f"요소가 바뀜(이전 {safe_page_text(target.role)} '{safe_page_text(target.name)}'"
                f" → 현재 {safe_page_text(refused.role)} '{safe_page_text(refused.name)}')"
                " — 부작용 액션이라 다른 요소로 치유하지 않음"
            ),
            identity_refused=refused,
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
