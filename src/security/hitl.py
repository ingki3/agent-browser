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
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote_plus, urlsplit

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

# ---------------------------------------------------------------------------
# WS-31: 이름 밖 문맥 신호 (보이는 텍스트·aria·alt·목적지·클래스 토큰·의사요소 글자)
# ---------------------------------------------------------------------------

#: 정규화에서 지우는 보이지 않는 문자: zero-width space/non-joiner/joiner, word joiner,
#: BOM(zero-width no-break space), soft hyphen. `결\u200b제` 가 '결제' 키워드를 피하던 구멍.
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
_SPACE_RE = re.compile(r"\s+")


def normalize_gate_text(text: str) -> str:
    """게이트 매칭용 정규화: NFKC → 보이지 않는 문자 제거 → 공백 정리 → 소문자.

    한글 자모 분리(NFD) 같은 과한 정규화는 하지 않는다 — NFKC 는 완성형 한글을 유지한다.
    """
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _INVISIBLE_RE.sub("", text)
    return _SPACE_RE.sub(" ", text).strip().lower()


#: 문자열 원천 — 키워드를 **부분 문자열**로 찾는다(이름과 같은 규칙).
TEXT_SOURCES = frozenset(
    {"name", "text", "aria", "title", "alt", "svg_title", "child_aria", "value", "pseudo",
     "selector", "detail"}
)
#: 목적지 원천(URL) — 경로·쿼리를 **단어 경계 토큰**으로 매칭한다(`/payroll-info` ≠ pay).
PATH_SOURCES = frozenset({"href", "formaction", "form_action"})
#: 마크업 식별자 원천 — camelCase·kebab·snake 를 토큰으로 나눠 매칭한다.
TOKEN_SOURCES = frozenset({"class", "id", "name_attr", "testid"})

#: 목적지·식별자 토큰에서 빼는 키워드 — 폼 **메커니즘**을 뜻하는 일반어라 의도를 말하지 않는다.
#: 실측 근거: 로그인 버튼 `class="btn-submit"`·`id="submit"`, 로그인 폼 `action="/login/submit"`
#: 이 흔하다(이 토큰으로 막으면 정상 로그인이 차단된다 — WS-31 과차단 측정). 이 단어들은
#: 이름·보이는 글자(문자열 원천)에서는 그대로 고위험이다.
_MARKUP_GENERIC = frozenset({"submit", "confirm", "approve", "agree"})

#: 목적지·식별자 토큰과 비교할 영문 키워드 (HIGH_RISK_KEYWORDS 와 공유, 한 단어만).
_TOKEN_KEYWORDS = tuple(
    kw for kw in HIGH_RISK_KEYWORDS
    if kw.isascii() and " " not in kw and kw not in _MARKUP_GENERIC
)
#: 경로에서 부분 문자열로 찾는 한글 키워드 (한글 경로는 토큰 경계가 없다).
_HANGUL_KEYWORDS = tuple(kw for kw in HIGH_RISK_KEYWORDS if not kw.isascii())

#: 아이콘 클래스 사전 — 아이콘만 있는 버튼의 의미(삭제·결제). 접두사(fa-, bi-, icon- …)를 뗀
#: 이름이 이 표의 키와 같거나 `키-` 로 시작하면(`trash-can`, `credit-card-fill`) 해당한다.
#: 근거: Font Awesome(fa-trash, fa-credit-card, fa-cart-shopping), Bootstrap Icons(bi-trash,
#: bi-credit-card, bi-cart), Material Symbols(delete, shopping_cart, payment) 의 실제 이름.
#: 검색·홈·설정 같은 일반 아이콘은 넣지 않는다(과차단). 값은 판정 근거로 보고할 키워드.
ICON_CLASS_RISK = {
    "trash": "delete",
    "trash-can": "delete",
    "trash-alt": "delete",
    "delete": "delete",
    "delete-forever": "delete",
    "remove": "remove",
    "credit-card": "pay",
    "creditcard": "pay",
    "payment": "pay",
    "payments": "pay",
    "wallet": "pay",
    "cart": "checkout",
    "shopping-cart": "checkout",
    "cart-shopping": "checkout",
    "shopping-bag": "purchase",
    "bag-check": "purchase",
    "money-bill": "pay",
    "cash": "pay",
}
_ICON_PREFIXES = ("fa-", "fas-", "far-", "bi-", "icon-", "ico-", "glyphicon-", "mdi-", "ti-",
                  "ri-", "la-", "las-", "material-icons-", "ms-", "i-")

#: Bootstrap 정렬 유틸리티(`order-1`, `order-md-2`, `order-first`) — 'order' 토큰 과매칭 방지.
_BOOTSTRAP_ORDER_RE = re.compile(r"^order-(\d|first|last|sm|md|lg|xl|xxl)")

_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _split_camel(word: str) -> List[str]:
    return [m.group(0).lower() for m in _CAMEL_RE.finditer(word)]


def class_tokens(text: str) -> List[str]:
    """식별자 문자열을 토큰으로: camelCase·kebab-case·snake_case 분해, 소문자."""
    out: List[str] = []
    # 대소문자는 camelCase 분해에 필요하므로 정규화는 NFKC·보이지 않는 문자 제거까지만.
    cleaned = _INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", str(text or "")))
    for part in _TOKEN_SPLIT_RE.split(cleaned):
        if part:
            out.extend(_split_camel(part))
    return out


def path_tokens(url: str) -> List[str]:
    """URL 경로·쿼리를 토큰으로 (퍼센트 디코딩 뒤 class_tokens 와 같은 분해)."""
    raw = str(url or "")
    try:
        parts = urlsplit(raw)
        raw = f"{parts.path} {parts.query}"
    except ValueError:
        pass
    return class_tokens(unquote_plus(raw))


def _match_text(text: str) -> Optional[str]:
    return _contains_keyword(normalize_gate_text(text), HIGH_RISK_KEYWORDS)


def _match_tokens(tokens: Sequence[str]) -> Optional[str]:
    present = set(tokens)
    for kw in _TOKEN_KEYWORDS:
        if kw in present:
            return kw
    return None


def _match_path(url: str) -> Optional[str]:
    hit = _match_tokens(path_tokens(url))
    if hit:
        return hit
    try:
        decoded = normalize_gate_text(unquote_plus(url))
    except Exception:  # noqa: BLE001
        return None
    return _contains_keyword(decoded, _HANGUL_KEYWORDS)


def _match_icon_class(cls: str) -> Optional[str]:
    lowered = cls.strip().lower()
    for prefix in _ICON_PREFIXES:
        if lowered.startswith(prefix):
            stem = lowered[len(prefix):].replace("_", "-")
            for key in ICON_CLASS_RISK:
                if stem == key or stem.startswith(key + "-"):
                    return cls.strip()
    # Material Symbols 는 클래스가 아니라 글자(ligature)로 쓴다 — 그건 text 원천이 맡는다.
    return None


def _match_markup(source: str, text: str) -> Optional[str]:
    if source == "class":
        for cls in str(text or "").split():
            icon = _match_icon_class(cls)
            if icon:
                return icon
            if _BOOTSTRAP_ORDER_RE.match(cls.lower()):
                continue
            hit = _match_tokens(class_tokens(cls))
            if hit:
                return hit
        return None
    return _match_tokens(class_tokens(text))


def match_signal(source: str, text: str) -> Optional[str]:
    """원천 종류에 맞는 규칙으로 고위험 키워드를 찾는다. 없으면 None."""
    if not text:
        return None
    if source in PATH_SOURCES:
        return _match_path(text)
    if source in TOKEN_SOURCES:
        return _match_markup(source, text)
    return _match_text(text)


@dataclass
class RiskAssessment:
    """위험 판정 + 근거(운영자가 왜 막혔는지 알 수 있게)."""

    risk: RiskLevel
    reason: str
    basis: Dict[str, Any]


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
    #: 대상을 특정·판정할 수 없었던 사유 (WS-30). 비어 있지 않으면 고위험으로 본다
    #: (fail-closed) — 예: selector 가 0개/여러 개에 맞음, 포커스 요소를 읽을 수 없음.
    unresolved_target: str = ""
    #: 이름 밖 문맥 신호 (WS-31) — (원천, 텍스트) 쌍. 원천: text·aria·title·alt·svg_title·
    #: child_aria·value·pseudo(문자열), href·formaction·form_action(경로 토큰),
    #: class·id·name_attr·testid(식별자 토큰). 페이지에서 읽은 그대로 넘기면 여기서 정규화한다.
    signals: Tuple[Tuple[str, str], ...] = ()
    #: 판정 근거에 덧붙일 사실(좌표 해석 결과 등) — 판정에는 쓰지 않는다.
    basis_extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HITLDecision:
    """HITL 게이트 판정 결과."""

    allowed: bool
    risk: RiskLevel
    requires_confirmation: bool
    reason: str
    error_code: Optional[ErrorCode] = None
    dialog: Optional[ConfirmDialog] = None
    #: 판정 근거 {name, matched_keyword, source, …} (WS-31) — 결과 data.gate_basis 로 나간다.
    basis: Dict[str, Any] = field(default_factory=dict)


def _contains_keyword(text: str, keywords: Sequence[str]) -> Optional[str]:
    lowered = text.lower()
    for kw in keywords:
        if kw.lower() in lowered:
            return kw
    return None


def _basis(ctx: ActionContext, keyword: Optional[str], source: Optional[str]) -> Dict[str, Any]:
    basis: Dict[str, Any] = {
        "name": ctx.element_name,
        "matched_keyword": keyword,
        "source": source,
    }
    for key, value in ctx.basis_extra.items():
        basis.setdefault(key, value)
    return basis


def assess_risk(ctx: ActionContext) -> RiskAssessment:
    """위험 등급 + 판정 근거. 고위험 키워드는 이름·셀렉터·부가정보 다음 문맥 신호 순서로 찾는다."""
    if ctx.unresolved_target:
        return RiskAssessment(
            RiskLevel.HIGH,
            f"대상 판정 불가({ctx.unresolved_target}) — 안전하게 차단",
            _basis(ctx, None, "unresolved"),
        )

    sources: List[Tuple[str, str]] = [
        ("name", ctx.element_name), ("selector", ctx.selector), ("detail", ctx.detail),
    ]
    sources.extend((str(src), str(text or "")) for src, text in ctx.signals)
    for source, text in sources:
        hit = match_signal(source, text)
        if hit:
            return RiskAssessment(
                RiskLevel.HIGH,
                f"고위험 키워드 '{hit}' 탐지(출처 {source})",
                _basis(ctx, hit, source),
            )

    if ctx.submits_form:
        return RiskAssessment(RiskLevel.HIGH, "폼 제출 액션", _basis(ctx, None, "form_submit"))

    if ctx.action in _INHERENTLY_RISKY:
        return RiskAssessment(
            RiskLevel.HIGH,
            f"부작용이 큰 액션: {ctx.action.value}",
            _basis(ctx, None, "action"),
        )

    haystack = normalize_gate_text(f"{ctx.element_name} {ctx.selector} {ctx.detail}")
    hit = _contains_keyword(haystack, MEDIUM_RISK_KEYWORDS)
    if hit:
        return RiskAssessment(
            RiskLevel.MEDIUM, f"중위험 키워드 '{hit}' 탐지", _basis(ctx, hit, "name")
        )

    return RiskAssessment(RiskLevel.LOW, "고위험 신호 없음", _basis(ctx, None, None))


def classify_risk(ctx: ActionContext) -> tuple[RiskLevel, str]:
    """액션의 위험 등급을 판정한다."""
    assessed = assess_risk(ctx)
    return assessed.risk, assessed.reason


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
        assessed = assess_risk(ctx)
        risk, reason, basis = assessed.risk, assessed.reason, assessed.basis

        # 저·중위험은 그대로 통과 (관측만)
        if risk is not RiskLevel.HIGH:
            return HITLDecision(
                allowed=True,
                risk=risk,
                requires_confirmation=False,
                reason=reason,
                basis=basis,
            )

        # --- 고위험 ---
        if self.mode is ExecutionMode.UNATTENDED:
            if self._is_pre_approved(ctx):
                return HITLDecision(
                    allowed=True,
                    risk=risk,
                    requires_confirmation=False,
                    reason=f"사전 승인 목록에 존재 ({reason})",
                    basis=basis,
                )
            # 기본값은 차단이다.
            return HITLDecision(
                allowed=False,
                risk=risk,
                requires_confirmation=False,
                reason=f"무인 모드에서 사전 승인되지 않은 고위험 액션 ({reason})",
                error_code=ErrorCode.HITL_UNATTENDED_BLOCKED,
                basis=basis,
            )

        # 대화형: 승인 모달을 띄우고 사용자 응답을 기다린다.
        return HITLDecision(
            allowed=False,  # 승인 전까지는 실행 불가
            risk=risk,
            requires_confirmation=True,
            reason=f"사용자 승인 필요 ({reason})",
            dialog=_build_dialog(ctx, reason),
            basis=basis,
        )
