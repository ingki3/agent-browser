"""WS-47 R3: no 407 ever reaches the browser over the proxy connection.

The proxy asks for no credentials (no challenge of its own), and a site's own
``407 + Proxy-Authenticate`` relayed as-is would look, in CDP, exactly like a
proxy challenge (source=Proxy, origin=our proxy) that Playwright then tries to
answer. Plain HTTP forwarding turns an upstream 407 into a proxy-marked 502 and
strips the proxy-auth header family from every upstream response. (CONNECT
tunnels are TLS: untouched.)

The origin-form guard parses the request-target form (RFC 9112 §3.2) instead
of searching for ``://`` anywhere in it.
"""
from __future__ import annotations

import asyncio
from typing import List

import pytest

from security import EgressGuard, EgressPolicy
from security.egress_proxy import BLOCK_HEADER, EgressProxy, request_target_form


def _guard() -> EgressGuard:
    async def resolver(host: str) -> List[str]:
        return ["127.0.0.1"]

    return EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True, resolver=resolver,
                       dns_cache_ttl_s=0)


class _Site:
    """Answers every request with a fixed raw response (head + body)."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.heads: List[bytes] = []

    async def start(self) -> "_Site":
        async def on(r, w):
            try:
                self.heads.append(await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 3))
            except Exception:  # noqa: BLE001
                self.heads.append(b"")
            w.write(self.response)
            await w.drain()
            w.close()

        self.server = await asyncio.start_server(on, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


async def _through_proxy(response: bytes, *, path: str = "/x") -> tuple:
    site = await _Site(response).start()
    px = await EgressProxy(_guard()).start()
    try:
        r, w = await asyncio.open_connection(px.host, px.port)
        w.write(f"GET http://site.test:{site.port}{path} HTTP/1.1\r\nHost: site.test:{site.port}\r\n"
                "\r\n".encode())
        await w.drain()
        raw = await asyncio.wait_for(r.read(), 5)
        w.close()
    finally:
        await px.close()
        await site.close()
    return raw, site, px


# -- (ii) upstream 407 never reaches the browser as a proxy challenge ---------------


@pytest.mark.parametrize("challenge", [
    'Basic realm="local-synthetic"', 'Basic realm="agent-browser"', 'Digest realm="x", nonce="n"', "NTLM"])
async def test_upstream_407_becomes_marked_502_without_challenge(challenge):
    raw, site, px = await _through_proxy(
        f"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: {challenge}\r\n"
        f"Proxy-Authentication-Info: nextnonce=\"x\"\r\nContent-Length: 4\r\nConnection: close\r\n\r\nnope".encode())
    assert raw.startswith(b"HTTP/1.1 502"), raw[:80]
    assert b"proxy-authenticate" not in raw.lower() and b"proxy-authentication-info" not in raw.lower()
    assert challenge.encode() not in raw
    assert f"{BLOCK_HEADER}: upstream-407-replaced".encode() in raw, raw[:300]
    assert b"nope" not in raw  # the site's 407 body is not relayed either
    assert len(site.heads) == 1  # exactly one upstream request, no replay


async def test_upstream_proxy_auth_headers_are_stripped_from_other_responses():
    raw, site, px = await _through_proxy(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nProxy-Authenticate: Basic realm=\"x\"\r\n"
        b"proxy-authorization: Basic Zm9vOmJhcg==\r\nProxy-Authentication-Info: a=b\r\nX-Kept: yes\r\n"
        b"Content-Length: 5\r\nConnection: close\r\n\r\nhello")
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert b"proxy-auth" not in head.lower(), head
    assert b"X-Kept: yes" in head and b"Content-Type: text/plain" in head
    assert body == b"hello"


async def test_informational_then_final_response_is_relayed():
    raw, site, px = await _through_proxy(
        b"HTTP/1.1 103 Early Hints\r\nLink: </a.css>; rel=preload\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    assert raw.startswith(b"HTTP/1.1 103") and raw.endswith(b"ok") and b"HTTP/1.1 200 OK" in raw


async def test_informational_then_407_is_still_replaced():
    raw, site, px = await _through_proxy(
        b"HTTP/1.1 100 Continue\r\n\r\n"
        b"HTTP/1.1 407 Nope\r\nProxy-Authenticate: Basic realm=\"x\"\r\nContent-Length: 0\r\n\r\n")
    assert b"407" not in raw.split(b"\r\n\r\n", 1)[1][:20]
    assert b"HTTP/1.1 502" in raw and b"proxy-authenticate" not in raw.lower()


async def test_unparsable_upstream_status_fails_closed():
    raw, site, px = await _through_proxy(b"garbage without a status line\r\n\r\nbody")
    assert raw.startswith(b"HTTP/1.1 502"), raw[:80]


# -- (iv) request-target form parsing, not substring search -------------------------


@pytest.mark.parametrize("target, form", [
    ("/probe", "origin"),
    ("/probe?next=http://evil.test/", "origin"),
    ("/a://b", "origin"),
    ("http://site.test/p?x=http://y", "absolute"),
    ("HTTP://site.test/", "absolute"),
    ("https://site.test/", "absolute"),
    ("site.test:443", "authority"),
    ("*", "asterisk"),
    ("probe?next=http://evil.test/", "invalid"),
    ("", "invalid"),
    ("//site.test/", "origin"),
])
def test_request_target_form(target, form):
    method = "CONNECT" if form == "authority" else "GET"
    assert request_target_form(method, target) == form


@pytest.mark.parametrize("target", ["/probe?next=http://evil.test/", "/x?u=https://a.test/b", "/a://b"])
async def test_origin_form_with_scheme_in_query_gets_400_without_challenge(target):
    px = await EgressProxy(_guard()).start()
    try:
        r, w = await asyncio.open_connection(px.host, px.port)
        w.write(f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await w.drain()
        raw = await asyncio.wait_for(r.read(), 5)
        w.close()
    finally:
        await px.close()
    assert raw.startswith(b"HTTP/1.1 400"), raw[:60]
    assert b"proxy-authenticate" not in raw.lower()
    assert px.handled == 1  # rejected at the gate: no relay, no second connection


async def test_absolute_form_with_scheme_in_query_is_relayed_intact():
    raw, site, px = await _through_proxy(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok",
                                         path="/p?next=http://y.test/&u=https://z.test")
    assert raw.startswith(b"HTTP/1.1 200") and raw.endswith(b"ok")
    assert site.heads[0].startswith(b"GET /p?next=http://y.test/&u=https://z.test HTTP/1.1\r\n")


# -- no credentials anywhere: the proxy never challenges ------------------------------


@pytest.mark.parametrize("auth", [None, "Basic YWdlbnQtYnJvd3Nlcjp4", "Bearer x", "garbage"])
async def test_proxy_never_challenges_and_never_forwards_proxy_auth(auth):
    """No 407 from the proxy itself, with or without any Proxy-Authorization; the
    header (hop-by-hop) is never forwarded upstream."""
    site = await _Site(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok").start()
    px = await EgressProxy(_guard()).start()
    extra = f"Proxy-Authorization: {auth}\r\n" if auth else ""
    try:
        r, w = await asyncio.open_connection(px.host, px.port)
        w.write(f"GET http://site.test:{site.port}/a HTTP/1.1\r\nHost: site.test\r\n{extra}\r\n".encode())
        await w.drain()
        http = await asyncio.wait_for(r.read(), 5)
        w.close()
        r, w = await asyncio.open_connection(px.host, px.port)
        w.write(f"CONNECT site.test:{site.port} HTTP/1.1\r\n{extra}\r\n".encode())
        await w.drain()
        established = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
        w.close()
    finally:
        await px.close()
        await site.close()
    assert http.startswith(b"HTTP/1.1 200") and established.startswith(b"HTTP/1.1 200")
    assert b"proxy-authenticate" not in (http + established).lower()
    assert b"proxy-authorization" not in site.heads[0].lower()


def test_launch_proxy_carries_no_credentials():
    """Every browser path gets the bare proxy address: nothing Playwright could hand to a site."""
    from security.egress_runtime import EgressRuntime

    for tokenless in (False, True):
        runtime = EgressRuntime(tokenless=tokenless)
        runtime.proxy = EgressProxy(runtime.guard, port=12345)
        runtime.proxy._server = object()  # treat as running for launch_kwargs
        proxy = runtime.launch_kwargs()["proxy"]
        assert proxy == {"server": "http://127.0.0.1:12345"}, proxy
        assert not any(a.startswith("--proxy-auth") for a in runtime.chrome_args())
