"""고위험 액션 HITL 게이트 (PRD §5.3-1, §3.3).

허용 도메인 내부라도 고위험 액션(폼 제출, 결제, 데이터 삭제)은
실행 모드에 따라 다르게 처리한다:

* 대화형(`interactive`) — `ConfirmDialog` 모달로 사용자 승인 대기
* 무인(`unattended`)   — `pre_approved_actions` 외 **전면 차단**하고
  `E_HITL_UNATTENDED_BLOCKED` 반환

무인 모드에서 승인 없이 통과시키면 결제·삭제가 무단 실행되므로,
기본값은 항상 "차단"이며 사전 승인은 명시적으로만 부여된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence, Set

from contracts import ActionType, ConfirmDialog, DangerLevel, ErrorCode, ExecutionMode


class RiskLevel(str, Enum):
    """액션 위험 등급."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: 본질적으로 부작용이 큰 액션 (PRD §4.1 retry_safe=No 계열과 정합)
_INHERENTLY_RISKY: Set[ActionType] = {
    ActionType.UPLOAD_FILE,
    ActionType.DOWNLOAD_FILE,
}

#: 고위험 의도를 드러내는 텍스트 신호 (요소 이름/셀렉터에서 탐지)
HIGH_RISK_KEYWORDS = (
    # 결제
    "결제", "구매", "주문", "송금", "이체", "출금", "pay", "purchase", "checkout",
    "order", "transfer", "withdraw", "subscribe",
    # 삭제
    "삭제", "제거", "탈퇴", "해지", "delete", "remove", "destroy", "terminate",
    "deactivate", "cancel account",
    # 제출/확정
    "제출", "확정", "승인", "동의", "submit", "confirm", "approve", "agree",
    # 권한 변경
    "권한", "관리자", "permission", "admin", "grant", "revoke",
)

MEDIUM_RISK_KEYWORDS = (
    "저장", "수정", "변경", "업로드", "save", "update", "edit", "upload", "apply",
)


@dataclass
class ActionContext:
    """HITL 판정에 필요한 액션 맥락."""

    action: ActionType
    #: 대상 요소의 접근성 이름 또는 버튼 라벨
    element_name: str = ""
    #: 셀렉터 또는 element_id
    selector: str = ""
    #: 대상 도메인
    domain: str = ""
    #: 폼 제출을 유발하는가 (press_enter, submit 버튼 등)
    submits_form: bool = False
    #: 금액 등 부가 정보 (ConfirmDialog 메시지에 사용)
    detail: str = ""


@dataclass
class HITLDecision:
    """HITL 게이트 판정 결과."""

    allowed: bool
    risk: RiskLevel
    requires_confirmation: bool
    reason: str
    error_code: Optional[ErrorCode] = None
    dialog: Optional[ConfirmDialog] = None


def _contains_keyword(text: str, keywords: Sequence[str]) -> Optional[str]:
    lowered = text.lower()
    for kw in keywords:
        if kw.lower() in lowered:
            return kw
    return None


def classify_risk(ctx: ActionContext) -> tuple[RiskLevel, str]:
    """액션의 위험 등급을 판정한다."""
    haystack = f"{ctx.element_name} {ctx.selector} {ctx.detail}"

    hit = _contains_keyword(haystack, HIGH_RISK_KEYWORDS)
    if hit:
        return RiskLevel.HIGH, f"고위험 키워드 '{hit}' 탐지"

    if ctx.submits_form:
        return RiskLevel.HIGH, "폼 제출 액션"

    if ctx.action in _INHERENTLY_RISKY:
        return RiskLevel.HIGH, f"부작용이 큰 액션: {ctx.action.value}"

    hit = _contains_keyword(haystack, MEDIUM_RISK_KEYWORDS)
    if hit:
        return RiskLevel.MEDIUM, f"중위험 키워드 '{hit}' 탐지"

    return RiskLevel.LOW, "고위험 신호 없음"


def _build_dialog(ctx: ActionContext, reason: str) -> ConfirmDialog:
    """승인 요청 모달을 생성한다 (PRD §6.1 정형 스키마)."""
    target = ctx.element_name or ctx.selector or ctx.action.value
    message = f"'{target}' 에 대한 {ctx.action.value} 액션을 실행합니다."
    if ctx.domain:
        message += f"\n대상 도메인: {ctx.domain}"
    if ctx.detail:
        message += f"\n{ctx.detail}"
    message += f"\n\n판정 근거: {reason}"

    return ConfirmDialog(
        title="고위험 액션 승인 요청",
        message=message,
        confirm_label="실행",
        cancel_label="취소",
        danger_level=DangerLevel.HIGH,
    )


@dataclass
class HITLGate:
    """실행 모드별 고위험 액션 게이트."""

    mode: ExecutionMode = ExecutionMode.UNATTENDED
    #: 무인 모드에서 사전 승인된 액션 식별자 집합.
    #: 형식: "click:결제 진행" 또는 "click:*" (액션 전체 승인)
    pre_approved_actions: Sequence[str] = field(default_factory=tuple)

    def _is_pre_approved(self, ctx: ActionContext) -> bool:
        specific = f"{ctx.action.value}:{ctx.element_name}"
        wildcard = f"{ctx.action.value}:*"
        return specific in self.pre_approved_actions or wildcard in self.pre_approved_actions

    def evaluate(self, ctx: ActionContext) -> HITLDecision:
        """액션 실행 가부를 판정한다."""
        risk, reason = classify_risk(ctx)

        # 저·중위험은 그대로 통과 (관측만)
        if risk is not RiskLevel.HIGH:
            return HITLDecision(
                allowed=True,
                risk=risk,
                requires_confirmation=False,
                reason=reason,
            )

        # --- 고위험 ---
        if self.mode is ExecutionMode.UNATTENDED:
            if self._is_pre_approved(ctx):
                return HITLDecision(
                    allowed=True,
                    risk=risk,
                    requires_confirmation=False,
                    reason=f"사전 승인 목록에 존재 ({reason})",
                )
            # 기본값은 차단이다.
            return HITLDecision(
                allowed=False,
                risk=risk,
                requires_confirmation=False,
                reason=f"무인 모드에서 사전 승인되지 않은 고위험 액션 ({reason})",
                error_code=ErrorCode.HITL_UNATTENDED_BLOCKED,
            )

        # 대화형: 승인 모달을 띄우고 사용자 응답을 기다린다.
        return HITLDecision(
            allowed=False,  # 승인 전까지는 실행 불가
            risk=risk,
            requires_confirmation=True,
            reason=f"사용자 승인 필요 ({reason})",
            dialog=_build_dialog(ctx, reason),
        )
