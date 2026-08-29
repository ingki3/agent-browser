"""세션 만료 감지 프로브 (PRD §5.1-3).

3단계 우선순위로 `E_AUTH_EXPIRED`를 판정한다:

1순위 — 사용자 정의 프로브: 프로파일에 등록된 보호 API URL의 200 OK 검증
2순위 — HTTP 상태 검증: 네비게이션 시 401 / 403 수신
3순위 — 휴리스틱: 로그인 리다이렉트 또는 패스워드 인풋 비정상 출현

3순위는 오탐(FPR)이 발생하기 쉬우므로 **보수적으로 판정**한다.
무인 모드에서 오탐은 정상 세션을 만료로 오인해 태스크를 중단시키므로,
"로그인 URL 패턴" 하나만으로는 만료로 보지 않고 추가 신호를 요구한다.
FPR ≤ 1.0% 는 `harness.session_probe`가 검증한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence
from urllib.parse import urlparse


class ProbeTier(str, Enum):
    """판정 근거가 된 프로브 단계."""

    CUSTOM_ENDPOINT = "custom_endpoint"  # 1순위
    HTTP_STATUS = "http_status"  # 2순위
    HEURISTIC = "heuristic"  # 3순위
    NONE = "none"  # 만료 아님


#: 로그인 페이지로 간주하는 경로 패턴 (PRD §5.1-3)
LOGIN_PATH_PATTERN = re.compile(r"/(login|auth|signin|sign-in|session/new)\b", re.I)

#: 인증 실패를 뜻하는 HTTP 상태
AUTH_FAILURE_STATUSES = frozenset({401, 403})


@dataclass
class PageSignals:
    """프로브 판정에 사용하는 페이지 관측 신호."""

    url: str
    http_status: Optional[int] = None
    #: 화면에 보이는 password 인풋 개수
    visible_password_inputs: int = 0
    #: 인증된 사용자에게만 보이는 요소(프로파일/로그아웃 등) 존재 여부
    has_authenticated_markers: bool = False
    #: 사용자 정의 보호 엔드포인트 응답 상태 (조회했다면)
    custom_probe_status: Optional[int] = None
    #: 네비게이션 과정에서 로그인 페이지로 리다이렉트되었는가
    redirected_to_login: bool = False


@dataclass
class ProbeResult:
    """세션 만료 판정 결과."""

    expired: bool
    tier: ProbeTier
    reason: str
    #: 3순위 휴리스틱에서 수집된 근거 신호 수 (2개 이상일 때만 만료 판정)
    evidence: List[str] = field(default_factory=list)


@dataclass
class ProfileProbeConfig:
    """프로파일별 프로브 설정."""

    #: 1순위 사용자 정의 보호 API URL (없으면 2·3순위로 폴백)
    protected_endpoints: Sequence[str] = ()
    #: 인증 후에도 로그인 폼이 노출되는 사이트를 위한 예외 경로
    login_form_allowed_paths: Sequence[str] = ()


def _is_login_url(url: str) -> bool:
    try:
        path = urlparse(url).path or "/"
    except ValueError:
        return False
    return bool(LOGIN_PATH_PATTERN.search(path))


def detect_expiry(
    signals: PageSignals, config: Optional[ProfileProbeConfig] = None
) -> ProbeResult:
    """3단계 우선순위로 세션 만료를 판정한다."""
    config = config or ProfileProbeConfig()

    # --- 1순위: 사용자 정의 보호 엔드포인트 --------------------------------
    if signals.custom_probe_status is not None:
        if signals.custom_probe_status in AUTH_FAILURE_STATUSES:
            return ProbeResult(
                expired=True,
                tier=ProbeTier.CUSTOM_ENDPOINT,
                reason=f"보호 엔드포인트가 {signals.custom_probe_status} 반환",
            )
        if signals.custom_probe_status == 200:
            # 가장 신뢰도 높은 신호이므로 하위 순위를 덮어쓴다.
            return ProbeResult(
                expired=False,
                tier=ProbeTier.CUSTOM_ENDPOINT,
                reason="보호 엔드포인트 200 OK — 세션 유효",
            )

    # --- 2순위: HTTP 상태 ---------------------------------------------------
    if signals.http_status in AUTH_FAILURE_STATUSES:
        return ProbeResult(
            expired=True,
            tier=ProbeTier.HTTP_STATUS,
            reason=f"네비게이션 응답 {signals.http_status}",
        )

    # --- 3순위: 휴리스틱 (보수적 판정) --------------------------------------
    # 오탐 억제: 인증 마커가 보이면 로그인 폼이 있어도 만료로 보지 않는다.
    # (예: 재인증 모달, 사이드바 로그인 위젯이 있는 대시보드)
    if signals.has_authenticated_markers:
        return ProbeResult(
            expired=False,
            tier=ProbeTier.NONE,
            reason="인증 사용자 전용 요소가 존재 — 세션 유효",
        )

    path_is_login = _is_login_url(signals.url)
    allowed = any(
        signals.url.rstrip("/").endswith(p.rstrip("/"))
        for p in config.login_form_allowed_paths
    )
    if allowed:
        return ProbeResult(
            expired=False,
            tier=ProbeTier.NONE,
            reason="프로파일이 허용한 로그인 폼 노출 경로",
        )

    evidence: List[str] = []
    if signals.redirected_to_login:
        evidence.append("로그인 페이지로 리다이렉트됨")
    if path_is_login:
        evidence.append("URL이 로그인 경로 패턴과 일치")
    if signals.visible_password_inputs > 0:
        evidence.append(
            f"패스워드 인풋 {signals.visible_password_inputs}개가 화면에 노출"
        )

    # 단일 신호로는 만료로 판정하지 않는다 (FPR ≤ 1.0% 확보).
    # 예: /login 경로를 그냥 방문한 경우, 검색 결과에 password 필드가 있는 경우.
    if len(evidence) >= 2:
        return ProbeResult(
            expired=True,
            tier=ProbeTier.HEURISTIC,
            reason="휴리스틱 신호 2개 이상 일치",
            evidence=evidence,
        )

    return ProbeResult(
        expired=False,
        tier=ProbeTier.NONE,
        reason="만료 신호 부족" if evidence else "만료 신호 없음",
        evidence=evidence,
    )
