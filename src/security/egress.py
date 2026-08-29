"""Egress 제어: 도메인 Allowlist 및 요청 인터셉션 (PRD §5.3-1).

1차 방어선의 핵심. 미승인 도메인으로의 데이터 유출(XHR, Beacon, 이미지
픽셀)을 `page.route()` 레벨에서 차단한다.

정책 모드 (PRD §3.3):
* ``strict``       — allowlist 외 전면 차단 (무인 모드 기본)
* ``ask``          — 차단하되 사용자 승인 시 통과 (대화형)
* ``open_sandbox`` — 탐색 태스크용. 명시적 지정 필요

기술적 한계 (PRD §5.3-1에 명시된 사항):
애플리케이션 레이어 차단이므로 Service Worker 백그라운드 싱크, WebRTC
피어 통신, 브라우저 DNS 프리페치는 완벽히 차단되지 않는다. 민감 환경에서는
브라우저 샌드박스 플래그를 병용해야 한다.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence, Set
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class EgressPolicy(str, Enum):
    """Egress 정책 모드 (PRD §3.3)."""

    STRICT = "strict"
    ASK = "ask"
    OPEN_SANDBOX = "open_sandbox"


class BlockReason(str, Enum):
    """차단 사유."""

    NOT_IN_ALLOWLIST = "not_in_allowlist"
    PRIVATE_NETWORK = "private_network"  # SSRF / 메타데이터 엔드포인트
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    MALFORMED_URL = "malformed_url"


#: 브라우저가 정상적으로 사용하는 스킴만 허용한다.
ALLOWED_SCHEMES = frozenset({"http", "https", "ws", "wss"})

#: 항상 차단하는 내부 대역 (클라우드 메타데이터 등 SSRF 표적)
#: allowlist에 명시적으로 들어와도 정책상 차단한다.
BLOCKED_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal"})


@dataclass
class EgressDecision:
    """단일 요청에 대한 판정."""

    allowed: bool
    url: str
    reason: Optional[BlockReason] = None
    detail: str = ""


@dataclass
class EgressGuard:
    """도메인 Allowlist 기반 Egress 가드.

    `harness.egress_test`가 `is_allowed(url)`을 호출해 유출 여부를 측정한다.
    """

    allowed_domains: Sequence[str] = field(default_factory=tuple)
    policy: EgressPolicy = EgressPolicy.STRICT
    #: 로컬 Mock 서버 등 루프백 허용 여부 (테스트/개발용)
    allow_loopback: bool = False

    _blocked_log: List[EgressDecision] = field(
        default_factory=list, init=False, repr=False
    )

    # -- 판정 ---------------------------------------------------------------

    def evaluate(self, url: str) -> EgressDecision:
        """요청 URL을 판정한다."""
        try:
            parsed = urlparse(url)
        except ValueError:
            return EgressDecision(False, url, BlockReason.MALFORMED_URL, "URL 파싱 실패")

        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()

        if not scheme or not host:
            return EgressDecision(
                False, url, BlockReason.MALFORMED_URL, "스킴 또는 호스트 없음"
            )

        if scheme not in ALLOWED_SCHEMES:
            return EgressDecision(
                False, url, BlockReason.UNSUPPORTED_SCHEME, f"스킴 '{scheme}' 미허용"
            )

        # 내부 대역은 정책 모드와 무관하게 차단 (SSRF 방어)
        if host in BLOCKED_HOSTS:
            return EgressDecision(
                False, url, BlockReason.PRIVATE_NETWORK, "메타데이터 엔드포인트"
            )

        if self._is_private_host(host) and not self.allow_loopback:
            return EgressDecision(
                False, url, BlockReason.PRIVATE_NETWORK, f"내부 대역 호스트: {host}"
            )

        # open_sandbox는 위 안전장치를 통과한 요청을 허용한다.
        if self.policy is EgressPolicy.OPEN_SANDBOX:
            return EgressDecision(True, url)

        if self._matches_allowlist(host):
            return EgressDecision(True, url)

        return EgressDecision(
            False, url, BlockReason.NOT_IN_ALLOWLIST, f"허용 목록에 없는 도메인: {host}"
        )

    def is_allowed(self, url: str) -> bool:
        """`harness.egress_test`가 사용하는 단순 판정 인터페이스."""
        decision = self.evaluate(url)
        if not decision.allowed:
            self._blocked_log.append(decision)
        return decision.allowed

    # -- 내부 판정 로직 ------------------------------------------------------

    def _matches_allowlist(self, host: str) -> bool:
        """정확히 일치하거나 등록 도메인의 하위 도메인이면 허용한다.

        문자열 접미사 비교는 ``evil-example.com``이 ``example.com``을
        통과시키므로 사용하지 않는다.
        """
        for entry in self.allowed_domains:
            allowed = entry.lower().lstrip(".")
            if not allowed:
                continue
            if host == allowed or host.endswith("." + allowed):
                return True
        return False

    @staticmethod
    def _is_private_host(host: str) -> bool:
        """루프백/사설/링크로컬 주소인지 판정한다."""
        if host in ("localhost", "localhost.localdomain"):
            return True
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
        )

    # -- 관측 ---------------------------------------------------------------

    @property
    def blocked_requests(self) -> List[EgressDecision]:
        return list(self._blocked_log)

    def clear_log(self) -> None:
        self._blocked_log.clear()

    # -- Playwright 연동 -----------------------------------------------------

    async def install(self, context) -> None:  # noqa: ANN001
        """`page.route()`로 모든 요청을 인터셉션한다.

        차단된 요청은 abort하여 네트워크에 나가지 않게 한다.
        """

        async def _handler(route, request) -> None:  # noqa: ANN001
            decision = self.evaluate(request.url)
            if decision.allowed:
                await route.continue_()
                return
            self._blocked_log.append(decision)
            logger.info(
                "Egress 차단: %s (%s)", request.url, decision.reason.value if decision.reason else "?"
            )
            await route.abort("blockedbyclient")

        await context.route("**/*", _handler)
