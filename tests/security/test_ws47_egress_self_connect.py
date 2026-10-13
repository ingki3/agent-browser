"""WS-47 R2: the egress proxy never connects to its own listener, whatever the host string.

The R1 re-verification rebound a site hostname to the proxy's listener IP: the
proxy relayed the request to itself (dropping Proxy-Authorization on the way)
and the inner hop answered with the instance's 407 realm, which the page read.
(WS-47 R3: the proxy no longer has credentials or a challenge at all; the self
block stays — a page must never reach the proxy's own listener.)
The self check now compares the resolved IP and port actually about to be
connected against every listener address, for plain HTTP forwarding and CONNECT.
"""
from __future__ import annotations

import asyncio
import base64
import socket
from typing import List, Tuple

import pytest

from security import EgressGuard, EgressPolicy
from security.egress_proxy import BLOCK_HEADER, EgressProxy, targets_listener

TOKEN = "t0k-ws47"


def _auth() -> str:
    return "Basic " + base64.b64encode(f"agent-browser:{TOKEN}".encode()).decode()


def _guard(answers: List[str]) -> EgressGuard:
    async def resolver(host: str) -> List[str]:
        assert host == "alias.test", host
        return list(answers)

    return EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True, resolver=resolver,
                       dns_cache_ttl_s=0)


async def _ask(proxy: EgressProxy, raw: bytes) -> bytes:
    r, w = await asyncio.open_connection(proxy.host, proxy.port)
    w.write(raw)
    await w.drain()
    data = await asyncio.wait_for(r.read(), 5)
    w.close()
    return data


def _get(host: str, port: int, path: str = "/probe") -> bytes:
    return (f"GET http://{host}:{port}{path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Proxy-Authorization: {_auth()}\r\n\r\n").encode()


def _connect(host: str, port: int) -> bytes:
    return f"CONNECT {host}:{port} HTTP/1.1\r\nProxy-Authorization: {_auth()}\r\n\r\n".encode()


class _Upstream:
    def __init__(self) -> None:
        self.heads: List[bytes] = []

    async def start(self, host: str, port: int = 0) -> "_Upstream":
        async def on(r, w):
            try:
                self.heads.append(await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 3))
            except Exception:  # noqa: BLE001
                self.heads.append(b"")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await w.drain()
            w.close()

        self.server = await asyncio.start_server(on, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


def _assert_self_blocked(raw: bytes, proxy: EgressProxy) -> None:
    assert raw.startswith(b"HTTP/1.1 403"), raw[:60]
    assert f"{BLOCK_HEADER}: blocked; category=loopback".encode() in raw, raw[:300]
    assert b"proxy-authenticate" not in raw.lower()
    # The outer hop refused before opening a socket: the listener saw one client only.
    assert proxy.handled == 1, proxy.handled


# -- (a) DNS alias / re-resolution to the listener: HTTP forward and CONNECT ---------


@pytest.mark.parametrize("answers", [
    ["127.0.0.1"],
    ["::ffff:127.0.0.1"],
    ["::1", "127.0.0.1"],  # first answer has no listener: the loop must not fall through to self
])
@pytest.mark.parametrize("method", ["http", "connect"])
async def test_alias_resolving_to_listener_is_blocked(answers, method):
    px = await EgressProxy(_guard(answers)).start()
    try:
        raw = await _ask(px, _get("alias.test", px.port) if method == "http" else _connect("alias.test", px.port))
    finally:
        await px.close()
    _assert_self_blocked(raw, px)


@pytest.mark.parametrize("host", ["2130706433", "127.1", "0x7f.0.0.1", "0177.0.0.1", "localhost.",
                                  "[::ffff:127.0.0.1]", "[::ffff:7f00:1]"])
@pytest.mark.parametrize("method", ["http", "connect"])
async def test_literal_spellings_of_listener_are_blocked(host, method):
    px = await EgressProxy(_guard([])).start()
    try:
        raw = await _ask(px, _get(host, px.port) if method == "http" else _connect(host, px.port))
    finally:
        await px.close()
    _assert_self_blocked(raw, px)


@pytest.mark.parametrize("method", ["http", "connect"])
async def test_ipv6_listener_alias_is_blocked(method):
    px = await EgressProxy(_guard(["::1"]), host="::1").start()
    try:
        raw = await _ask(px, _get("alias.test", px.port) if method == "http" else _connect("alias.test", px.port))
    finally:
        await px.close()
    _assert_self_blocked(raw, px)


# -- (c) no over-blocking: other local services stay reachable ------------------------


async def test_same_ip_other_port_is_relayed():
    up = await _Upstream().start("127.0.0.1")
    px = await EgressProxy(_guard(["127.0.0.1"])).start()
    try:
        http = await _ask(px, _get("alias.test", up.port))
        r, w = await asyncio.open_connection("127.0.0.1", px.port)
        w.write(_connect("alias.test", up.port))
        await w.drain()
        established = await r.readuntil(b"\r\n\r\n")
        w.write(b"GET /t HTTP/1.1\r\nHost: x\r\n\r\n")
        await w.drain()
        tunneled = await asyncio.wait_for(r.read(), 5)
        w.close()
    finally:
        await px.close()
        await up.close()
    assert http.startswith(b"HTTP/1.1 200") and http.endswith(b"ok")
    assert established.startswith(b"HTTP/1.1 200") and tunneled.endswith(b"ok")
    assert len(up.heads) == 2


async def test_other_ip_same_port_is_relayed():
    """A local service on ::1 at the proxy's own port number is not the proxy."""
    px = await EgressProxy(_guard(["::1"])).start()
    up = await _Upstream().start("::1", px.port)
    try:
        raw = await _ask(px, _get("alias.test", px.port))
    finally:
        await px.close()
        await up.close()
    assert raw.startswith(b"HTTP/1.1 200") and raw.endswith(b"ok")
    assert len(up.heads) == 1 and b"proxy-authorization" not in up.heads[0].lower()


# -- listener matching (pure) ---------------------------------------------------------


def _local_ipv4() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # UDP connect sends nothing; it only picks a route
        return s.getsockname()[0]
    except OSError:
        pytest.skip("no non-loopback IPv4 route")
    finally:
        s.close()


def test_targets_listener_exact_and_mapped():
    listeners: List[Tuple[str, int]] = [("127.0.0.1", 5000)]
    assert targets_listener("127.0.0.1", 5000, listeners)
    assert targets_listener("::ffff:127.0.0.1", 5000, listeners)
    assert not targets_listener("127.0.0.1", 5001, listeners)
    assert not targets_listener("::1", 5000, listeners)
    assert not targets_listener("127.0.0.2", 5000, listeners)
    # an unspecified destination reaches a local listener on common stacks
    assert targets_listener("0.0.0.0", 5000, listeners)


@pytest.mark.parametrize("bind", ["0.0.0.0", "::"])
def test_targets_listener_wildcard_bind_covers_local_interfaces(bind):
    listeners = [(bind, 5000)]
    local = _local_ipv4()
    assert targets_listener("127.0.0.1", 5000, listeners)
    assert targets_listener("::1", 5000, listeners)
    assert targets_listener(local, 5000, listeners)
    assert targets_listener("::ffff:" + local, 5000, listeners)
    assert not targets_listener(local, 5001, listeners)
    assert not targets_listener("192.0.2.55", 5000, listeners)  # TEST-NET: not a local address


# -- defence in depth: only proxy-form requests are served -----------------------------


async def test_origin_form_request_is_refused_at_the_gate():
    """A request that reached the listener as origin-form (relayed, not from a proxy client)
    gets 400 whatever it carries; a proxy client's absolute-form request is served (no 407)."""
    px = await EgressProxy(_guard([])).start()
    try:
        bare = await _ask(px, b"GET /probe HTTP/1.1\r\nHost: alias.test\r\n\r\n")
        query = await _ask(px, b"GET /probe?next=http://alias.test/ HTTP/1.1\r\nHost: x\r\n\r\n")
        absolute = await _ask(px, b"GET http://127.0.0.1:9/probe HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    finally:
        await px.close()
    for raw in (bare, query):
        assert raw.startswith(b"HTTP/1.1 400"), raw[:60]
        assert b"proxy-authenticate" not in raw.lower()
    # absolute-form goes through the guard/relay path (port 9 is closed: proxy-marked 502), never 407
    assert absolute.startswith(b"HTTP/1.1 502") and b"upstream-failed" in absolute, absolute[:200]
