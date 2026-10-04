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


# -- WS-29b R1 (NB-2: 검증자 뮤턴트 V2·V3·V6, NB-4: URL 비밀 제거, NB-5: 기록 상한) ------


@pytest.mark.parametrize("url", ["http://[fd00:ec2::254]/latest/", "http://[64:ff9b::a9fe:a9fe]/"])
def test_metadata_ipv6_forms_stay_metadata_even_with_private_open(url):
    """AWS IMDS IPv6·NAT64 로 감싼 메타데이터는 사설이 아니라 메타데이터 — 옵션으로 못 연다(V3·V6)."""
    g = _mcp_default(allow_private_network=True)
    d = g.evaluate(url)
    assert d.allowed is False, url
    assert d.category == "metadata"


def test_nat64_wrapped_private_ipv4_is_private():
    """NAT64(64:ff9b::/96) 안의 사설 IPv4 는 사설로 판정한다(V6)."""
    d = _mcp_default().evaluate("http://[64:ff9b::c0a8:101]/")
    assert d.allowed is False and d.category == "private"


async def test_unparseable_resolved_ip_blocks_fail_closed():
    """해석 결과에 IP 가 아닌 값이 섞이면 판정 불가 — 막는다(V2)."""
    r = FakeResolver({"weird.example": ["93.184.216.34", "garbage"]})
    d = await _mcp_default(resolver=r).evaluate_async("https://weird.example/")
    assert d.allowed is False
    assert d.category == "malformed"


def test_blocked_log_url_has_no_query_fragment_or_userinfo():
    """차단 기록 URL 은 스킴+호스트+포트+경로까지만 — 쿼리 비밀이 남지 않는다(NB-4)."""
    g = _mcp_default()
    assert g.is_allowed("http://user:pw@192.168.1.1:8080/a/b?token=SECRET#frag") is False
    rec = g.blocked_requests[-1]
    assert rec.url == "http://192.168.1.1:8080/a/b"
    assert "SECRET" not in repr(rec) and "pw" not in rec.url
    assert "SECRET" not in repr(rec.to_agent())


def test_guard_records_are_bounded():
    """장시간 세션에서 기록이 무한히 늘지 않는다(NB-5). 최근 것은 남는다."""
    from security import egress

    g = _mcp_default()
    n = egress.MAX_GUARD_RECORDS + 50
    for i in range(n):
        g.is_allowed(f"http://10.0.0.{i % 250}/x{i}")
    assert len(g.blocked_requests) == egress.MAX_GUARD_RECORDS
    assert g.blocked_requests[-1].url.endswith(f"/x{n - 1}")
    assert egress.MAX_GUARD_RECORDS <= 1000


async def test_resolve_failures_are_bounded():
    from security import egress

    g = _mcp_default(resolver=FakeResolver({}))
    for i in range(egress.MAX_GUARD_RECORDS + 20):
        await g.evaluate_async(f"https://nx{i}.example/")
    assert len(g.resolve_failures) == egress.MAX_GUARD_RECORDS
    assert g.resolve_failures[-1][0] == f"nx{egress.MAX_GUARD_RECORDS + 19}.example"


def test_blocked_since_counts_new_entries_after_cap():
    """상한을 넘긴 뒤에도 '이번 호출 이후 신규 차단' 을 정확히 돌려준다(MCP data.egress 첨부용)."""
    from security import egress

    g = _mcp_default()
    for i in range(egress.MAX_GUARD_RECORDS + 5):
        g.is_allowed(f"http://10.0.0.1/old{i}")
    before = g.blocked_total
    assert g.blocked_since(before) == []
    g.is_allowed("http://192.168.1.1/new")
    new = g.blocked_since(before)
    assert [d.url for d in new] == ["http://192.168.1.1/new"]
