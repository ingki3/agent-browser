"""WS-29b EgressProxy 단위 — 인증·바인드·CONNECT 판정·판정 불가 차단·검증 IP 고정."""

from __future__ import annotations

import asyncio
import base64
from typing import List, Tuple

import pytest

from security import EgressGuard, EgressPolicy
from security.egress_proxy import PROXY_USER, EgressProxy


class _Upstream:
    """접속을 받으면 기록하고 고정 응답을 돌려주는 업스트림."""

    def __init__(self) -> None:
        self.conns: List[Tuple[str, bytes]] = []
        self.server = None
        self.port = 0

    async def start(self, host: str = "127.0.0.1") -> "_Upstream":
        async def on(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 3)
            except Exception:  # noqa: BLE001
                head = b""
            self.conns.append((w.get_extra_info("sockname")[0], head))
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await w.drain()
            w.close()

        self.server = await asyncio.start_server(on, host, 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


def _auth(token: str) -> str:
    return "Basic " + base64.b64encode(f"{PROXY_USER}:{token}".encode()).decode()


async def _ask(proxy: EgressProxy, raw: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
    w.write(raw)
    await w.drain()
    data = await asyncio.wait_for(r.read(), 5)
    w.close()
    return data


def _guard(**kw) -> EgressGuard:
    return EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True, **kw)


async def test_proxy_refuses_non_loopback_bind():
    with pytest.raises(ValueError):
        await EgressProxy(_guard(), host="0.0.0.0").start()


async def test_proxy_requires_token_when_set():
    up = await _Upstream().start()
    px = await EgressProxy(_guard(), token="t0k").start()
    try:
        no = await _ask(px, f"GET http://127.0.0.1:{up.port}/x HTTP/1.1\r\nHost: a\r\n\r\n".encode())
        bad = await _ask(px, (f"GET http://127.0.0.1:{up.port}/x HTTP/1.1\r\nHost: a\r\n"
                              f"Proxy-Authorization: {_auth('nope')}\r\n\r\n").encode())
        ok = await _ask(px, (f"GET http://127.0.0.1:{up.port}/x HTTP/1.1\r\nHost: a\r\n"
                             f"Proxy-Authorization: {_auth('t0k')}\r\n\r\n").encode())
    finally:
        await px.close()
        await up.close()
    assert no.startswith(b"HTTP/1.1 407")
    assert bad.startswith(b"HTTP/1.1 407")
    assert ok.startswith(b"HTTP/1.1 200")
    assert len(up.conns) == 1
    # 프록시 자격증명은 업스트림에 넘기지 않는다
    assert b"proxy-authorization" not in up.conns[0][1].lower()


async def test_connect_to_blocked_target_never_opens_upstream():
    up = await _Upstream().start()
    px = await EgressProxy(_guard(), token=None).start()
    try:
        r1 = await _ask(px, b"CONNECT 192.168.1.1:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        r2 = await _ask(px, b"CONNECT 169.254.169.254:80 HTTP/1.1\r\nHost: x\r\n\r\n")
        r3 = await _ask(px, b"GET http://10.0.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n")
    finally:
        await px.close()
        await up.close()
    for r in (r1, r2, r3):
        assert r.startswith(b"HTTP/1.1 403"), r[:40]
        assert b"egress_blocked" in r


async def test_proxy_does_not_relay_to_itself():
    px = await EgressProxy(_guard(), token=None).start()
    try:
        r = await _ask(px, f"GET http://127.0.0.1:{px.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    finally:
        await px.close()
    assert r.startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("raw", [
    b"GARBAGE\r\n\r\n",
    b"GET /relative HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",  # absolute-form 아님
    b"GET ftp://127.0.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET http://127.0.0.1:99999/ HTTP/1.1\r\nHost: x\r\n\r\n",
    b"CONNECT :443 HTTP/1.1\r\n\r\n",
    b"GET http://127.0.0.1/ HTTP/1.1\r\n bad-fold: x\r\n\r\n",
])
async def test_unjudgeable_requests_are_refused(raw):
    px = await EgressProxy(_guard(), token=None).start()
    try:
        r = await _ask(px, raw)
    finally:
        await px.close()
    assert r.startswith(b"HTTP/1.1 4"), r[:40]


async def test_connects_only_to_verified_ip_not_reresolved():
    """검사 시점 해석(127.0.0.1)만 쓴다 — 해석기가 이후 다른 값을 내도 재해석하지 않는다."""
    up = await _Upstream().start()
    answers = [["127.0.0.1"], ["10.9.9.9"]]
    calls: List[str] = []

    async def resolver(host: str) -> List[str]:
        calls.append(host)
        return answers[min(len(calls) - 1, 1)]

    px = await EgressProxy(_guard(resolver=resolver), token=None).start()
    try:
        r = await _ask(px, f"GET http://svc.test:{up.port}/a HTTP/1.1\r\nHost: svc.test\r\n\r\n".encode())
    finally:
        await px.close()
        await up.close()
    assert r.startswith(b"HTTP/1.1 200")
    assert calls == ["svc.test"]
    assert up.conns[0][0] == "127.0.0.1"
    assert b"GET /a HTTP/1.1" in up.conns[0][1]


async def test_resolution_failure_does_not_connect():
    async def resolver(host: str) -> List[str]:
        raise OSError("NXDOMAIN")

    px = await EgressProxy(_guard(resolver=resolver), token=None).start()
    try:
        r = await _ask(px, b"CONNECT nowhere.test:443 HTTP/1.1\r\n\r\n")
    finally:
        await px.close()
    assert r.startswith(b"HTTP/1.1 502")


async def test_connect_tunnel_to_allowed_target_relays_bytes():
    up = await _Upstream().start()
    px = await EgressProxy(_guard(), token=None).start()
    try:
        rd, wr = await asyncio.open_connection("127.0.0.1", px.port)
        wr.write(f"CONNECT 127.0.0.1:{up.port} HTTP/1.1\r\n\r\n".encode())
        await wr.drain()
        established = await rd.readuntil(b"\r\n\r\n")
        wr.write(b"GET /t HTTP/1.1\r\nHost: x\r\n\r\n")
        await wr.drain()
        body = await asyncio.wait_for(rd.read(), 5)
        wr.close()
    finally:
        await px.close()
        await up.close()
    assert established.startswith(b"HTTP/1.1 200")
    assert body.endswith(b"ok")
