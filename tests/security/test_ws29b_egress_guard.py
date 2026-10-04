"""WS-29b Egress 보강 — 대역 분리·표기 정규화·도메인 해석 검사·에이전트용 차단 이유.

브라우저 없이 EgressGuard 판정만 고정한다. 실측 근거: .hermes/state/ws29b/report.md 단계 1 표.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List

import pytest

from security import BlockReason, EgressGuard, EgressPolicy


def _mcp_default(**kw) -> EgressGuard:
    """serve 기본값과 같은 가드(open_sandbox, 루프백 허용, 사설망 닫힘)."""
    return EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True, **kw)


# 실측(base): serve 의 allow_loopback=True 가 아래 전부를 열어 목적지에 도달했다.
BLOCKED_BY_DEFAULT = [
    "http://10.0.0.5/x",
    "http://172.16.0.1/x",
    "http://192.168.1.1/admin",
    "http://169.254.1.1/x",  # 링크로컬
    "http://100.73.27.9/x",  # CGNAT(Tailscale)
    "http://[fd7a:115c:a1e0::1]/x",  # IPv6 ULA
    "http://[fe80::1]/x",  # IPv6 링크로컬
    "http://[::ffff:192.168.1.1]/x",  # IPv4-mapped
    "http://3232235777/x",  # 192.168.1.1 정수 표기
    "http://0xc0.0xa8.1.1/x",  # 16진 표기
    "http://0300.0250.1.1/x",  # 8진 표기
    "http://192.168.257/x",  # 축약 표기 192.168.1.1
    "http://0.0.0.0/x",
    "http://[::]/x",
    "http://2852039166/latest/meta-data/",  # 169.254.169.254 정수
    "http://[::ffff:a9fe:a9fe]/latest/",  # 메타데이터 mapped
    "http://[fd00:ec2::254]/latest/",  # AWS IMDS IPv6
    "http://224.0.0.1/x",  # 멀티캐스트
]

ALLOWED_BY_DEFAULT = [
    "http://127.0.0.1:8080/",
    "http://127.1:8080/",
    "http://2130706433:8080/",
    "http://0x7f.1/",
    "http://[::1]:8080/",
    "http://localhost:3000/",
    "http://app.localhost:3000/",
    "http://[::ffff:127.0.0.1]/",
    "https://example.com/",
    "http://93.184.216.34/",
]


@pytest.mark.parametrize("url", BLOCKED_BY_DEFAULT)
def test_serve_default_blocks_private_ranges_and_notations(url):
    d = _mcp_default().evaluate(url)
    assert d.allowed is False, url
    assert d.reason is BlockReason.PRIVATE_NETWORK


@pytest.mark.parametrize("url", ALLOWED_BY_DEFAULT)
def test_serve_default_allows_loopback_and_public(url):
    assert _mcp_default().evaluate(url).allowed is True, url


@pytest.mark.parametrize("url", [
    "http://10.0.0.5/x", "http://100.73.27.9/x", "http://[fd7a:115c:a1e0::1]/x",
    "http://[::ffff:192.168.1.1]/x", "http://3232235777/x",
])
def test_allow_private_network_opens_private_ranges(url):
    assert _mcp_default(allow_private_network=True).evaluate(url).allowed is True


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/", "http://2852039166/", "http://[::ffff:a9fe:a9fe]/",
    "http://0.0.0.0/", "http://[::]/", "http://metadata.google.internal/",
])
def test_allow_private_network_never_opens_metadata_or_unspecified(url):
    g = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True,
                    allow_private_network=True)
    assert g.evaluate(url).allowed is False, url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.1/", "http://2130706433/", "http://[::1]/",
    "http://localhost/", "http://app.localhost/", "http://[::ffff:127.0.0.1]/",
])
def test_block_loopback_blocks_every_loopback_notation(url):
    g = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=False)
    d = g.evaluate(url)
    assert d.allowed is False, url
    assert d.category == "loopback"


def test_unparseable_numeric_host_is_blocked_fail_closed():
    """브라우저가 IPv4 로 해석하려는(숫자로 끝나는) 호스트를 판정 못 하면 막는다."""
    d = _mcp_default().evaluate("http://1.2.3.4.5/")
    assert d.allowed is False


# -- 도메인 해석 검사 ---------------------------------------------------------


class FakeResolver:
    def __init__(self, table: Dict[str, List[str]]):
        self.table = table
        self.calls: List[str] = []

    async def __call__(self, host: str) -> List[str]:
        self.calls.append(host)
        if host not in self.table:
            raise OSError("NXDOMAIN")
        return list(self.table[host])


async def test_domain_resolving_to_private_ip_is_blocked():
    r = FakeResolver({"lan.example": ["192.168.1.10"]})
    d = await _mcp_default(resolver=r).evaluate_async("http://lan.example/x")
    assert d.allowed is False
    assert d.category == "private"
    assert d.host == "lan.example"


async def test_any_private_ip_among_resolved_blocks():
    r = FakeResolver({"mixed.example": ["93.184.216.34", "10.1.2.3"]})
    d = await _mcp_default(resolver=r).evaluate_async("https://mixed.example/")
    assert d.allowed is False


async def test_domain_resolving_to_metadata_blocked_even_with_private_open():
    r = FakeResolver({"meta.example": ["169.254.169.254"]})
    g = _mcp_default(resolver=r, allow_private_network=True)
    d = await g.evaluate_async("http://meta.example/")
    assert d.allowed is False and d.category == "metadata"


async def test_public_domain_allowed_with_verified_ips():
    r = FakeResolver({"pub.example": ["93.184.216.34", "2606:2800:220:1::1"]})
    d = await _mcp_default(resolver=r).evaluate_async("https://pub.example/")
    assert d.allowed is True
    assert d.resolved == ("93.184.216.34", "2606:2800:220:1::1")


async def test_domain_to_loopback_follows_loopback_switch():
    r = FakeResolver({"localtest.example": ["127.0.0.1"]})
    assert (await _mcp_default(resolver=r).evaluate_async("http://localtest.example/")).allowed
    g = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=False, resolver=r)
    assert (await g.evaluate_async("http://localtest.example/")).allowed is False


async def test_resolution_failure_is_not_blocked_but_recorded():
    r = FakeResolver({})
    g = _mcp_default(resolver=r)
    d = await g.evaluate_async("https://nowhere.example/")
    assert d.allowed is True
    assert g.resolve_failures and g.resolve_failures[0][0] == "nowhere.example"


async def test_resolution_is_cached_within_ttl(monkeypatch):
    from security import egress

    clock = [1000.0]
    monkeypatch.setattr(egress.time, "monotonic", lambda: clock[0])
    r = FakeResolver({"pub.example": ["93.184.216.34"]})
    g = _mcp_default(resolver=r)
    await g.evaluate_async("https://pub.example/a")
    await g.evaluate_async("https://pub.example/b")
    assert r.calls == ["pub.example"]
    clock[0] += egress.DNS_CACHE_TTL_S + 1
    await g.evaluate_async("https://pub.example/c")
    assert r.calls == ["pub.example", "pub.example"]
    assert egress.DNS_CACHE_TTL_S <= 30


async def test_allowlist_still_applies_after_resolution():
    r = FakeResolver({"other.example": ["93.184.216.34"]})
    g = EgressGuard(allowed_domains=("example.com",), allow_loopback=True, resolver=r)
    d = await g.evaluate_async("https://other.example/")
    assert d.allowed is False and d.reason is BlockReason.NOT_IN_ALLOWLIST


# -- 에이전트에 보일 이유 ------------------------------------------------------


def test_block_reason_for_agent_names_operator_option_only():
    d = _mcp_default().evaluate("http://192.168.1.1/")
    info = d.to_agent()
    assert info["code"] == "egress_blocked"
    assert info["host"] == "192.168.1.1"
    assert info["category"] == "private"
    assert info["open_with"] == "serve --allow-private-network"
    meta = _mcp_default().evaluate("http://169.254.169.254/").to_agent()
    assert meta["category"] == "metadata" and meta["open_with"] is None


def test_existing_harness_semantics_kept():
    """기존 strict allowlist 가드(egress_test)는 그대로: 루프백도 막고 허용 도메인은 통과."""
    g = EgressGuard(allowed_domains=("example.com",))
    assert g.is_allowed("https://api.example.com/")
    assert not g.is_allowed("http://127.0.0.1/")
    assert not g.is_allowed("http://localhost/")
