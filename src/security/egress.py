"""Egress 제어: 도메인 Allowlist 및 요청 인터셉션 (PRD §5.3-1).

1차 방어선의 핵심. 미승인 도메인으로의 데이터 유출(XHR, Beacon, 이미지
픽셀)을 `page.route()` 레벨에서 차단한다.

정책 모드 (PRD §3.3):
* ``strict``       — allowlist 외 전면 차단 (무인 모드 기본)
* ``ask``          — 차단하되 사용자 승인 시 통과 (대화형)
* ``open_sandbox`` — 탐색 태스크용. 명시적 지정 필요

내부망 대역 (WS-29b):
* 루프백(127/8, ::1, localhost, *.localhost) — ``allow_loopback`` 일 때만 허용.
* 사설·링크로컬·CGNAT(100.64/10)·IPv6 ULA 등 공개가 아닌 대역 — ``allow_private_network``
  일 때만 허용(로컬 NAS·사내망 쓰는 운영자용, ``serve --allow-private-network``).
* 클라우드 메타데이터·미지정 주소(0.0.0.0, ::)·멀티캐스트·예약 대역 — 옵션과 무관하게 차단.
IPv4-mapped IPv6(``::ffff:a.b.c.d``)·정수/16진/축약 IPv4 표기는 같은 IPv4 로 정규화해 판정한다.

도메인 해석 검사 (WS-29b): ``evaluate_async`` 는 호스트가 도메인이면 해석된 IP **전부**를
위 대역 규칙으로 검사한다(하나라도 막힘이면 차단). 해석 실패는 막지 않고 기록만 한다
(브라우저도 같은 이름을 못 찾는다). 검사 시점과 브라우저 접속 시점의 해석이 다를 수
있으므로(DNS 재바인딩) 실제 경계는 검증한 IP 로만 접속하는 로컬 프록시
(`security.egress_proxy.EgressProxy`)가 맡는다 — ``page.route`` 는 리다이렉트 홉·
WebSocket·WebRTC 를 보지 못한다(WS-29b 실측).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple, Union
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

#: 항상 차단하는 메타데이터 IP (IPv4 표기 변형·IPv6 포함, WS-29b).
METADATA_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS IPv6
})

#: 대역 종류 (EgressDecision.category). 운영자 옵션 안내에 쓴다.
CAT_LOOPBACK = "loopback"
CAT_PRIVATE = "private"  # 사설·링크로컬·CGNAT·ULA 등 공개가 아닌 대역
CAT_METADATA = "metadata"
CAT_UNSPECIFIED = "unspecified"  # 0.0.0.0, ::
CAT_RESERVED = "reserved"  # 멀티캐스트·예약·문서용 등
CAT_ALLOWLIST = "not_in_allowlist"
CAT_SCHEME = "scheme"
CAT_MALFORMED = "malformed"

#: 대역 종류 → 여는 운영자 옵션(없으면 None = 옵션과 무관하게 차단). 우회법이 아니라 운영자 설정이다.
OPEN_OPTION: Dict[str, Optional[str]] = {
    CAT_LOOPBACK: "serve --block-loopback 을 끄면 허용(기본 허용)",
    CAT_PRIVATE: "serve --allow-private-network",
    CAT_ALLOWLIST: "serve --allow-domain <도메인>",
    CAT_METADATA: None,
    CAT_UNSPECIFIED: None,
    CAT_RESERVED: None,
    CAT_SCHEME: None,
    CAT_MALFORMED: None,
}

#: 도메인 해석 캐시 상한(초). 실제 경계는 프록시의 IP 고정이라 신선도만 좌우한다.
DNS_CACHE_TTL_S = 30.0
#: 해석 대기 상한(초). 넘으면 해석 실패로 기록하고 브라우저에 맡긴다.
DNS_TIMEOUT_S = 5.0

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Resolver = Callable[[str], Awaitable[List[str]]]


@dataclass
class EgressDecision:
    """단일 요청에 대한 판정."""

    allowed: bool
    url: str
    reason: Optional[BlockReason] = None
    detail: str = ""
    #: 막힌 호스트(정규화 전 URL 호스트)와 대역 종류 (WS-29b)
    host: str = ""
    category: str = ""
    #: 해석으로 판정했으면 해석된 IP 목록, 검증한 접속 대상 IP(프록시 고정용)
    resolved: Tuple[str, ...] = ()

    def to_agent(self) -> Dict[str, object]:
        """에이전트에 보일 차단 이유(MCP data.egress 항목). 우회 방법은 담지 않는다."""
        return {
            "code": "egress_blocked",
            "host": self.host,
            "category": self.category,
            "reason": self.reason.value if self.reason else "",
            "open_with": OPEN_OPTION.get(self.category),
        }


# ---------------------------------------------------------------------------
# 주소 판정 (순수 함수)
# ---------------------------------------------------------------------------


def _parse_ipv4_part(part: str) -> Optional[int]:
    """WHATWG IPv4 숫자 한 조각(10진·0x16진·0 접두 8진)."""
    if part == "":
        return None
    base = 10
    s = part
    if s[:2].lower() == "0x":
        base, s = 16, s[2:]
        if s == "":
            return 0
    elif len(s) > 1 and s[0] == "0":
        base, s = 8, s[1:]
    try:
        return int(s, base)
    except ValueError:
        return None


def _ends_in_number(host: str) -> bool:
    """WHATWG 'ends in a number' — 마지막 라벨이 숫자면 브라우저는 IPv4 로 해석한다."""
    labels = host.split(".")
    if labels and labels[-1] == "":
        labels = labels[:-1]
    if not labels:
        return False
    last = labels[-1]
    if last and all(c in "0123456789" for c in last):
        return True
    return _parse_ipv4_part(last) is not None and last[:2].lower() == "0x"


def parse_ipv4_like(host: str) -> Optional[ipaddress.IPv4Address]:
    """브라우저(WHATWG URL)와 같은 규칙으로 정수/16진/8진/축약 IPv4 표기를 해석한다.

    ``2130706433``·``0x7f.1``·``127.1``·``0177.0.0.1`` → 127.0.0.1. IPv4 꼴이 아니면 None.
    """
    labels = host.split(".")
    if labels and labels[-1] == "" and len(labels) > 1:
        labels = labels[:-1]
    if not 1 <= len(labels) <= 4:
        return None
    nums = [_parse_ipv4_part(p) for p in labels]
    if any(n is None for n in nums):
        return None
    vals: List[int] = [n for n in nums if n is not None]
    if any(v > 255 for v in vals[:-1]):
        return None
    if vals[-1] >= 256 ** (5 - len(vals)):
        return None
    ipv4 = vals[-1]
    for i, v in enumerate(vals[:-1]):
        ipv4 += v * 256 ** (3 - i)
    return ipaddress.IPv4Address(ipv4)


def host_to_ip(host: str) -> Optional[IPAddress]:
    """URL 호스트가 IP 리터럴(표기 변형 포함)이면 IP, 도메인이면 None."""
    h = host.strip("[]").lower()
    if "%" in h:  # IPv6 zone id (fe80::1%en0)
        h = h.split("%", 1)[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if ":" in h:
        return None
    return parse_ipv4_like(h)


def _unwrap(addr: IPAddress) -> IPAddress:
    """IPv4-mapped·IPv4-compatible·NAT64·6to4 IPv6 안의 IPv4 를 꺼낸다."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        if addr.sixtofour is not None:
            return addr.sixtofour
        packed = addr.packed
        if packed[:12] == b"\x00" * 12 and addr not in (
            ipaddress.IPv6Address("::"), ipaddress.IPv6Address("::1")
        ):
            return ipaddress.IPv4Address(packed[12:])  # ::a.b.c.d (구식 호환)
        if addr in ipaddress.IPv6Network("64:ff9b::/96"):
            return ipaddress.IPv4Address(packed[12:])  # NAT64 잘 알려진 접두
    return addr


def classify_ip(addr: IPAddress) -> Optional[str]:
    """IP 의 대역 종류. 공개(global) 주소면 None."""
    if addr in METADATA_IPS:
        return CAT_METADATA
    inner = _unwrap(addr)
    if inner is not addr:
        return classify_ip(inner)
    if addr.is_unspecified:
        return CAT_UNSPECIFIED
    if addr.is_loopback:
        return CAT_LOOPBACK
    if addr.is_multicast:
        return CAT_RESERVED
    if addr.is_private or addr.is_link_local:
        # ipaddress 의 is_private 는 문서용·벤치마크 대역 등도 포함한다 — 사설로 묶는다.
        return CAT_PRIVATE
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.IPv4Network("100.64.0.0/10"):
        return CAT_PRIVATE  # CGNAT(Tailscale 등)
    if addr.is_reserved:
        return CAT_RESERVED
    if not addr.is_global:
        return CAT_PRIVATE
    return None


def _is_localhost_name(host: str) -> bool:
    h = host.rstrip(".")
    return h in ("localhost", "localhost.localdomain") or h.endswith(".localhost")


def _system_resolve(host: str) -> List[str]:
    """시스템 리졸버로 host 의 IP 전부를 얻는다(동기, 실행기에서 부른다)."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    out: List[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


async def default_resolver(host: str) -> List[str]:
    loop = asyncio.get_running_loop()
    # 모듈 속성을 그때그때 찾는다(시험에서 바꿔 끼울 수 있게).
    return await loop.run_in_executor(None, globals()["_system_resolve"], host)


@dataclass
class EgressGuard:
    """도메인 Allowlist 기반 Egress 가드.

    `harness.egress_test`가 `is_allowed(url)`을 호출해 유출 여부를 측정한다.
    """

    allowed_domains: Sequence[str] = field(default_factory=tuple)
    policy: EgressPolicy = EgressPolicy.STRICT
    #: 루프백(127/8·::1·localhost) 허용 여부 — 로컬 Mock·개발 서버용. 다른 사설 대역은 열지 않는다.
    allow_loopback: bool = False
    #: 사설·링크로컬·CGNAT·ULA 대역 허용 (운영자 옵션 serve --allow-private-network, WS-29b)
    allow_private_network: bool = False
    #: 도메인 해석기(테스트에서 바꿔 끼운다). None 이면 시스템 리졸버.
    resolver: Optional[Resolver] = None
    dns_cache_ttl_s: float = DNS_CACHE_TTL_S

    _blocked_log: List[EgressDecision] = field(
        default_factory=list, init=False, repr=False
    )
    _dns_cache: Dict[str, Tuple[float, Tuple[str, ...]]] = field(
        default_factory=dict, init=False, repr=False
    )
    #: 해석 실패 기록(호스트, 사유) — 막지는 않는다.
    resolve_failures: List[Tuple[str, str]] = field(
        default_factory=list, init=False, repr=False
    )

    # -- 판정 ---------------------------------------------------------------

    def _block(self, url: str, host: str, category: str, detail: str,
               reason: BlockReason = BlockReason.PRIVATE_NETWORK,
               resolved: Tuple[str, ...] = ()) -> EgressDecision:
        return EgressDecision(False, url, reason, detail, host=host, category=category,
                              resolved=resolved)

    def ip_category_blocked(self, addr: IPAddress) -> Optional[str]:
        """이 가드 설정에서 addr 이 막히면 대역 종류, 허용이면 None."""
        cat = classify_ip(addr)
        if cat is None:
            return None
        if cat == CAT_LOOPBACK and self.allow_loopback:
            return None
        if cat == CAT_PRIVATE and self.allow_private_network:
            return None
        return cat

    def evaluate(self, url: str) -> EgressDecision:
        """요청 URL을 판정한다(해석 없이 — IP 리터럴·이름 규칙만)."""
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
        except ValueError:
            return EgressDecision(False, url, BlockReason.MALFORMED_URL, "URL 파싱 실패",
                                  category=CAT_MALFORMED)

        scheme = (parsed.scheme or "").lower()

        if not scheme or not host:
            return EgressDecision(
                False, url, BlockReason.MALFORMED_URL, "스킴 또는 호스트 없음",
                host=host, category=CAT_MALFORMED,
            )

        if scheme not in ALLOWED_SCHEMES:
            return EgressDecision(
                False, url, BlockReason.UNSUPPORTED_SCHEME, f"스킴 '{scheme}' 미허용",
                host=host, category=CAT_SCHEME,
            )
        return self._evaluate_host(url, host)

    def _evaluate_host(self, url: str, host: str) -> EgressDecision:
        # 내부 대역은 정책 모드와 무관하게 차단 (SSRF 방어)
        if host.rstrip(".") in BLOCKED_HOSTS:
            return self._block(url, host, CAT_METADATA, "메타데이터 엔드포인트")

        addr = host_to_ip(host)
        if addr is None and ":" not in host and _ends_in_number(host):
            # 브라우저는 숫자로 끝나는 호스트를 IPv4 로 해석한다 — 해석 못 하면 판정 불가(차단).
            return EgressDecision(False, url, BlockReason.MALFORMED_URL,
                                  f"IPv4 꼴이지만 해석할 수 없는 호스트: {host}",
                                  host=host, category=CAT_MALFORMED)
        if addr is not None:
            cat = self.ip_category_blocked(addr)
            if cat is not None:
                return self._block(url, host, cat, f"내부 대역 호스트: {host} ({cat})")
        elif _is_localhost_name(host):
            # 브라우저는 localhost·*.localhost 를 DNS 없이 루프백으로 해석한다.
            if not self.allow_loopback:
                return self._block(url, host, CAT_LOOPBACK, f"내부 대역 호스트: {host} (loopback)")

        # open_sandbox는 위 안전장치를 통과한 요청을 허용한다.
        if self.policy is EgressPolicy.OPEN_SANDBOX:
            return EgressDecision(True, url, host=host)

        if self._matches_allowlist(host):
            return EgressDecision(True, url, host=host)

        return EgressDecision(
            False, url, BlockReason.NOT_IN_ALLOWLIST, f"허용 목록에 없는 도메인: {host}",
            host=host, category=CAT_ALLOWLIST,
        )

    async def resolve(self, host: str) -> Optional[Tuple[str, ...]]:
        """host 의 IP 전부(캐시 TTL 상한 dns_cache_ttl_s). 실패면 None(기록)."""
        now = time.monotonic()
        hit = self._dns_cache.get(host)
        if hit is not None and hit[0] > now:
            return hit[1]
        resolver = self.resolver or default_resolver
        try:
            ips = tuple(await asyncio.wait_for(resolver(host), timeout=DNS_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 - 해석 실패는 막지 않고 기록
            self.resolve_failures.append((host, type(exc).__name__))
            logger.info("Egress 해석 실패(브라우저에 맡김): %s (%s)", host, type(exc).__name__)
            return None
        if not ips:
            self.resolve_failures.append((host, "empty"))
            return None
        self._dns_cache[host] = (now + self.dns_cache_ttl_s, ips)
        return ips

    async def evaluate_async(self, url: str) -> EgressDecision:
        """evaluate + 도메인이면 해석된 IP 전부 검사 (WS-29b).

        허용 판정에는 ``resolved`` 에 검증한 IP 들이 담긴다(프록시가 그 IP 로만 접속).
        """
        decision = self.evaluate(url)
        if not decision.allowed:
            return decision
        host = decision.host
        addr = host_to_ip(host)
        if addr is not None:
            mapped = getattr(addr, "ipv4_mapped", None)
            decision.resolved = (str(mapped) if mapped is not None else str(addr),)
            return decision
        if _is_localhost_name(host):
            # 브라우저와 같이 DNS 없이 루프백(허용 판정은 evaluate 가 했다).
            decision.resolved = ("127.0.0.1", "::1")
            return decision
        ips = await self.resolve(host)
        if ips is None:
            return decision  # 해석 실패: 브라우저도 실패한다 — 기록만
        for ip in ips:
            try:
                ip_addr = ipaddress.ip_address(ip.split("%", 1)[0])
            except ValueError:
                return self._block(url, host, CAT_MALFORMED, f"해석 결과 판정 불가: {ip}",
                                   reason=BlockReason.MALFORMED_URL, resolved=ips)
            cat = self.ip_category_blocked(ip_addr)
            if cat is not None:
                return self._block(url, host, cat,
                                   f"도메인 {host} 이(가) 내부 대역 {ip} ({cat}) 로 해석됨",
                                   resolved=ips)
        decision.resolved = ips
        return decision

    def is_allowed(self, url: str) -> bool:
        """`harness.egress_test`가 사용하는 단순 판정 인터페이스."""
        decision = self.evaluate(url)
        if not decision.allowed:
            self._blocked_log.append(decision)
        return decision.allowed

    def record_block(self, decision: EgressDecision) -> None:
        self._blocked_log.append(decision)

    # -- 내부 판정 로직 ------------------------------------------------------

    def _matches_allowlist(self, host: str) -> bool:
        """정확히 일치하거나 등록 도메인의 하위 도메인이면 허용한다.

        문자열 접미사 비교는 ``evil-example.com``이 ``example.com``을
        통과시키므로 사용하지 않는다.
        """
        host = host.rstrip(".")
        for entry in self.allowed_domains:
            allowed = entry.lower().lstrip(".").rstrip(".")
            if not allowed:
                continue
            if host == allowed or host.endswith("." + allowed):
                return True
        return False

    @staticmethod
    def _is_private_host(host: str) -> bool:
        """루프백/사설/링크로컬/예약 주소인지 판정한다(표기 변형 포함)."""
        if _is_localhost_name(host):
            return True
        addr = host_to_ip(host)
        if addr is None:
            return False
        return classify_ip(addr) is not None

    # -- 관측 ---------------------------------------------------------------

    @property
    def blocked_requests(self) -> List[EgressDecision]:
        return list(self._blocked_log)

    def clear_log(self) -> None:
        self._blocked_log.clear()

    # -- Playwright 연동 -----------------------------------------------------

    async def install(self, context) -> None:  # noqa: ANN001
        """`page.route()`로 모든 요청을 인터셉션한다.

        차단된 요청은 abort하여 네트워크에 나가지 않게 한다. 도메인은 해석 IP 전부를
        검사한다. route 는 리다이렉트 홉·WebSocket·WebRTC·서비스워커 요청을 보지 못하므로
        실제 경계는 EgressProxy 가 맡는다(여기는 조기 차단 + 에이전트용 이유 기록).
        """

        async def _handler(route, request) -> None:  # noqa: ANN001
            decision = await self.evaluate_async(request.url)
            if decision.allowed:
                await route.continue_()
                return
            self._blocked_log.append(decision)
            logger.info(
                "Egress 차단: %s (%s)", request.url, decision.reason.value if decision.reason else "?"
            )
            await route.abort("blockedbyclient")

        await context.route("**/*", _handler)
