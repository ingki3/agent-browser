"""Egress 정책을 브라우저 진입점에 설치하는 공통 묶음 (WS-29b).

serve(MCP)·run(예시 에이전트)이 같은 정책을 쓰게 한다:
가드(EgressGuard) → 검증 프록시(EgressProxy) 시작 → 브라우저 실행 인자(프록시·QUIC 끔·
비프록시 WebRTC UDP 끔) → 컨텍스트에 route 가드 설치.

기본 정책: 루프백 허용(로컬 Mock·개발 서버), 사설·링크로컬·CGNAT·ULA 차단, 메타데이터·
미지정 주소 상시 차단. ``allow_private_network`` / ``block_loopback`` 은 운영자 옵션이다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from security.egress import EgressGuard, EgressPolicy
from security.egress_proxy import EgressProxy, chromium_proxy_args

logger = logging.getLogger(__name__)

#: user-chrome 시작 알림(사람이 보는 stderr 한 줄). 우리가 띄운 Chrome 에만 플래그를 줄 수 있다.
USER_CHROME_NOTICE = (
    "agent-browser: user-chrome — Egress 프록시는 우리가 띄운 Chrome 에만 적용됩니다. "
    "프록시 자격증명을 Chrome 명령줄로 줄 수 없어 토큰 없는 127.0.0.1 프록시를 씁니다. "
    "이미 떠 있는 Chrome 에 붙는 경로는 Egress 정책 밖입니다(README 보안 절)."
)


@dataclass
class EgressRuntime:
    allowed_domains: Sequence[str] = ()
    allow_private_network: bool = False
    block_loopback: bool = False
    #: user-chrome: Chrome 명령줄로 프록시 자격증명을 줄 수 없어 토큰 없이 띄운다.
    tokenless: bool = False

    guard: EgressGuard = field(init=False)
    proxy: Optional[EgressProxy] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.guard = EgressGuard(
            allowed_domains=tuple(self.allowed_domains),
            policy=EgressPolicy.STRICT if self.allowed_domains else EgressPolicy.OPEN_SANDBOX,
            allow_loopback=not self.block_loopback,
            allow_private_network=self.allow_private_network,
        )

    async def start(self) -> "EgressRuntime":
        proxy = EgressProxy(self.guard)
        if self.tokenless:
            proxy.token = None
        self.proxy = await proxy.start()
        return self

    async def close(self) -> None:
        proxy, self.proxy = self.proxy, None
        if proxy is not None:
            await proxy.close()

    # -- 브라우저 인자 ---------------------------------------------------------

    def launch_kwargs(self) -> Dict[str, Any]:
        """chromium.launch(**) 에 더할 인자(Playwright 번들 Chromium)."""
        assert self.proxy is not None, "start() 먼저"
        return {"proxy": self.proxy.playwright_proxy(), "args": chromium_proxy_args()}

    def chrome_args(self) -> List[str]:
        """우리가 Popen 으로 띄우는 Chrome(user-chrome) 명령줄 인자."""
        assert self.proxy is not None, "start() 먼저"
        return self.proxy.chrome_args()

    async def install(self, context: Any) -> None:
        await self.guard.install(context)

    def summary(self) -> str:
        parts = ["loopback=" + ("blocked" if self.block_loopback else "allowed"),
                 "private=" + ("allowed" if self.allow_private_network else "blocked")]
        return "egress " + " ".join(parts)
