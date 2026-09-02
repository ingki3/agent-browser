"""WS-4 보안 패키지 (PRD §5.3 3중 가드레일 중 1·2차 방어선).

* `EgressGuard`      — 도메인 Allowlist 및 요청 인터셉션 (1차)
* `mask` / `mask_text` — PII 정규식 마스킹 (1차)
* `HITLGate`         — 고위험 액션 승인 게이트 (1차)
* `build_prompt`     — 신뢰 경계 델리미터 격리 (2차)

3차 방어선(Guardrail Evaluator LLM)은 Post-MVP v1.1 범위이다.
"""

from security.egress import (
    ALLOWED_SCHEMES,
    BLOCKED_HOSTS,
    BlockReason,
    EgressDecision,
    EgressGuard,
    EgressPolicy,
)
from security.hitl import (
    HIGH_RISK_KEYWORDS,
    ActionContext,
    HITLDecision,
    HITLGate,
    RiskLevel,
    classify_risk,
)
from security.secrets import SecretResolution, SecretsError, SecretStore
from security.masking import (
    MASK,
    MASK_RULES,
    MaskingReport,
    find_leaks,
    mask,
    mask_mapping,
    mask_text,
)
from security.prompt_isolation import (
    InjectionVerdict,
    detect_injection,
    SYSTEM_CLOSE,
    SYSTEM_OPEN,
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    SanitizeReport,
    build_prompt,
    build_vision_prompt,
    detect_injection_markers,
    neutralize_delimiters,
    wrap_untrusted,
)

__all__ = [
    # Egress
    "EgressGuard",
    "EgressPolicy",
    "EgressDecision",
    "BlockReason",
    "ALLOWED_SCHEMES",
    "BLOCKED_HOSTS",
    # 마스킹
    "mask",
    "mask_text",
    "mask_mapping",
    "find_leaks",
    "MaskingReport",
    "MASK",
    "MASK_RULES",
    "SecretStore",
    "SecretsError",
    "SecretResolution",
    # HITL
    "HITLGate",
    "HITLDecision",
    "ActionContext",
    "RiskLevel",
    "classify_risk",
    "HIGH_RISK_KEYWORDS",
    # 프롬프트 격리
    "build_prompt",
    "build_vision_prompt",
    "wrap_untrusted",
    "neutralize_delimiters",
    "detect_injection_markers",
    "detect_injection",
    "InjectionVerdict",
    "SanitizeReport",
    "SYSTEM_OPEN",
    "SYSTEM_CLOSE",
    "UNTRUSTED_OPEN",
    "UNTRUSTED_CLOSE",
]
