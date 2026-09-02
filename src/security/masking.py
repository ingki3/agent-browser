"""PII 정규식 마스킹 (PRD §5.3-1, §6.2).

아웃바운드 데이터와 로그·트레이스에서 민감정보를 자동 마스킹한다.

마스킹 대상 (PRD §6.2 민감정보 마스킹 범위):
* 비밀번호 인풋 값
* HTTP ``Authorization`` 헤더, ``Set-Cookie`` 헤더
* URL 쿼리스트링 내 Access Token
* 주민등록번호, 카드번호, 이메일, 전화번호 등 PII

원칙: **과다 마스킹이 과소 마스킹보다 안전하다.** 다만 일반 숫자열을
카드번호로 오인하지 않도록 Luhn 검증 등 최소한의 정밀도는 확보한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Pattern, Tuple

MASK = "[REDACTED]"


def _luhn_valid(digits: str) -> bool:
    """Luhn 알고리즘으로 카드번호 유효성을 확인한다."""
    total = 0
    reverse = digits[::-1]
    for idx, ch in enumerate(reverse):
        n = int(ch)
        if idx % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass(frozen=True)
class MaskRule:
    """단일 마스킹 규칙."""

    name: str
    pattern: Pattern[str]
    #: 치환 템플릿. 그룹 참조 가능 (예: r"\\1=" + MASK)
    replacement: str


#: 순서가 중요하다. 넓은 규칙이 좁은 규칙을 잡아먹지 않도록 배치한다.
#: 헤더 규칙은 `\S+`가 아니라 줄 끝까지 소비해야 한다. 토큰에 공백이
#: 포함되면(예: "Bearer xxx") 뒷부분이 마스킹되지 않고 남기 때문이다.
MASK_RULES: Tuple[MaskRule, ...] = (
    # --- HTTP 헤더 ---
    MaskRule(
        "authorization_header",
        re.compile(r"(?i)\b(authorization)\s*:\s*[^\r\n]+", re.M),
        r"\1: " + MASK,
    ),
    MaskRule(
        "set_cookie_header",
        re.compile(r"(?i)\b(set-cookie)\s*:\s*[^\r\n]+", re.M),
        r"\1: " + MASK,
    ),
    MaskRule(
        "cookie_header",
        re.compile(r"(?i)^(cookie)\s*:\s*[^\r\n]+", re.M),
        r"\1: " + MASK,
    ),
    # --- URL 쿼리스트링 토큰 ---
    MaskRule(
        "url_token_param",
        re.compile(
            r"(?i)\b(access_token|id_token|refresh_token|api_key|apikey|token|secret|password|passwd|pwd)"
            r"=([^&\s\"']+)"
        ),
        r"\1=" + MASK,
    ),
    # --- JSON/폼 필드 ---
    MaskRule(
        "json_secret_field",
        re.compile(
            r"(?i)([\"']?(?:password|passwd|pwd|secret|api_key|apikey|access_token|"
            r"refresh_token|authorization)[\"']?\s*[:=]\s*)[\"']?[^\s,\"'}\]]+[\"']?"
        ),
        r"\1" + MASK,
    ),
    # --- Bearer / JWT ---
    MaskRule(
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+=*"),
        "Bearer " + MASK,
    ),
    MaskRule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
        MASK,
    ),
    # --- 한국 주민등록번호 (YYMMDD-NNNNNNN) ---
    MaskRule(
        "kr_rrn",
        re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b"),
        MASK,
    ),
    # --- 이메일 ---
    MaskRule(
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        MASK,
    ),
    # --- 한국 휴대전화 ---
    MaskRule(
        "kr_phone",
        re.compile(r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b"),
        MASK,
    ),
)

#: 카드번호는 Luhn 검증이 필요해 별도 처리한다.
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

#: 구조화 로그에서 키 이름만으로 민감 필드를 판정한다.
#: 값이 평범한 문자열이면 정규식으로 잡히지 않으므로 키 기반 방어가 필요하다.
#:
#: `set-cookie`/`cookie`가 포함된 이유 — 헤더 규칙은 `set-cookie: v`
#: 형태의 **문자열**만 매칭한다. 구조체가 `{"Set-Cookie": "session=..."}`
#: 처럼 키/값으로 분리돼 있으면 규칙이 걸리지 않아 세션 토큰이
#: 부분 마스킹만 된 채 남았다(PRD 5.3 규정 미준수, 실측 확인).
_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token"
    r"|id[_-]?token|authorization|auth[_-]?token|session[_-]?id|private[_-]?key"
    r"|client[_-]?secret|credential|set[_-]?cookie|cookie)"
)


def _mask_card_numbers(text: str) -> Tuple[str, int]:
    """Luhn을 통과하는 13~19자리 숫자열만 마스킹한다."""
    count = 0

    def _sub(match: re.Match) -> str:
        nonlocal count
        raw = match.group(0)
        digits = re.sub(r"[ -]", "", raw)
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            count += 1
            return MASK
        return raw

    return _CARD_CANDIDATE.sub(_sub, text), count


@dataclass
class MaskingReport:
    """마스킹 결과 요약."""

    text: str
    hits: Dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.hits.values())

    @property
    def masked(self) -> bool:
        return self.total > 0


def mask_text(text: str) -> MaskingReport:
    """문자열에서 민감정보를 마스킹한다."""
    if not text:
        return MaskingReport(text=text, hits={})

    hits: Dict[str, int] = {}
    result = text

    for rule in MASK_RULES:
        result, n = rule.pattern.subn(rule.replacement, result)
        if n:
            hits[rule.name] = hits.get(rule.name, 0) + n

    result, card_hits = _mask_card_numbers(result)
    if card_hits:
        hits["credit_card"] = card_hits

    return MaskingReport(text=result, hits=hits)


def mask(text: str) -> str:
    """마스킹된 문자열만 반환하는 단축 함수."""
    return mask_text(text).text


def mask_mapping(data: Dict[str, object]) -> Dict[str, object]:
    """딕셔너리를 재귀적으로 마스킹한다 (로그 구조체용).

    문자열 값뿐 아니라 **키 이름이 민감 필드인 경우 값 자체를 마스킹**한다.
    `{"password": "hunter2"}` 처럼 값만 보면 평범한 문자열이라
    정규식으로는 잡히지 않기 때문이다.
    """
    masked: Dict[str, object] = {}
    for key, value in data.items():
        if isinstance(key, str) and _SENSITIVE_KEY.fullmatch(key.strip()):
            masked[key] = MASK
        elif isinstance(value, str):
            masked[key] = mask(value)
        elif isinstance(value, dict):
            masked[key] = mask_mapping(value)  # type: ignore[arg-type]
        elif isinstance(value, list):
            masked[key] = [
                mask(v) if isinstance(v, str)
                else mask_mapping(v) if isinstance(v, dict)
                else v
                for v in value
            ]
        else:
            masked[key] = value
    return masked


def find_leaks(text: str) -> List[str]:
    """마스킹 없이 남은 민감정보 종류를 반환한다 (검증용)."""
    report = mask_text(text)
    return sorted(report.hits.keys())
