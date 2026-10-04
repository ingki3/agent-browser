"""WS-29b Egress — 실제 Chromium 으로 도달 여부를 목적지 서버 로그로 판정한다.

page.route 만으로는 못 막던 경로(실측, report.md 단계 1):
리다이렉트 홉 · DNS 재바인딩 · WebSocket · WebRTC(STUN UDP). 검증 프록시(EgressProxy)와
Chromium 플래그로 막는지, 정상 사용(루프백 Mock)은 그대로인지 본다.

CI 에 사설망이 없으므로 '막혀야 할 목적지'를 루프백 서버로 만든다:
* allowlist 설정(allowed_domains=("localhost",))에서 127.0.0.1 은 비허용 목적지다.
* 재바인딩: Chrome 의 해석(--host-resolver-rules, 시험 전용)은 ::1(막힘으로 간주),
  가드의 검사 시점 해석은 127.0.0.1(허용) — 요청이 ::1 서버에 닿으면 실패.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest

from contracts import ErrorCode
from interface.mcp_server import BrowserMCPServer


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")


class _Sink:
    """목적지 서버 — 받은 요청 경로와 목적지 주소를 남긴다."""

    def __init__(self) -> None:
        self.hits: List[Dict[str, Any]] = []
        self.udp: List[int] = []
        self.lock = threading.Lock()


def _handler(sink: _Sink):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a: Any) -> None:  # noqa: D401
            pass

        def _rec(self) -> None:
            with sink.lock:
                sink.hits.append({"path": self.path, "dst": self.connection.getsockname()[0],
                                  "upgrade": self.headers.get("Upgrade")})

        def _send(self, code: int, body: bytes = b"", extra: Dict[str, str] | None = None,
                  ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._rec()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/redir":
                self._send(302, b"", {"Location": q["to"][0]})
            elif u.path == "/bad502":
                self._send(502, b"<html><body>upstream app down</body></html>")
            elif u.path == "/page":
                self._send(200, q["h"][0].encode())
            elif u.path == "/sw.js":
                js = ("self.addEventListener('install', e => { e.waitUntil(fetch(%s)"
                      ".catch(()=>0)); self.skipWaiting(); });" % json.dumps(q["t"][0]))
                self._send(200, js.encode(), ctype="text/javascript")
            else:
                self._send(200, f"<html><body><h1>ok {u.path}</h1><button>b</button></body></html>".encode())

    return H


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


@pytest.fixture
def sink():
    s = _Sink()
    v4 = ThreadingHTTPServer(("127.0.0.1", 0), _handler(s))
    port = v4.server_address[1]
    servers = [v4]
    try:
        v6 = _V6Server(("::1", port), _handler(s))
        servers.append(v6)
        s.has_v6 = True  # type: ignore[attr-defined]
    except OSError:
        s.has_v6 = False  # type: ignore[attr-defined]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", port))
    udp.settimeout(0.2)
    stop = threading.Event()

    def _udp_loop() -> None:
        while not stop.is_set():
            try:
                data, _ = udp.recvfrom(4096)
            except OSError:
                continue
            with s.lock:
                s.udp.append(len(data))

    threading.Thread(target=_udp_loop, daemon=True).start()
    s.port = port  # type: ignore[attr-defined]
    yield s
    stop.set()
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    udp.close()


def _reached(sink: _Sink, token: str) -> List[Dict[str, Any]]:
    with sink.lock:
        return [h for h in sink.hits if h["path"].startswith(token)]


def _page(port: int, html: str, host: str = "localhost") -> str:
    from urllib.parse import quote

    return f"http://{host}:{port}/page?h={quote(html)}"


async def _nav(srv: BrowserMCPServer, url: str):
    return await srv.call_tool("browser_navigate", {"url": url, "timeout_ms": 8000})


# -- 리다이렉트 홉 ------------------------------------------------------------


async def test_redirect_hop_to_non_allowlisted_target_is_not_reached(sink):
    """허용 도메인(localhost)이 302 로 비허용 목적지(127.0.0.1)를 가리키면 도달하지 않는다.

    실측(base): route 는 리다이렉트 홉을 보지 못해 도달했다.
    """
    port = sink.port
    srv = BrowserMCPServer(allowed_domains=("localhost",))
    try:
        r = await _nav(srv, f"http://localhost:{port}/redir?to=http://127.0.0.1:{port}/p/redir_hop")
        await asyncio.sleep(0.3)
    finally:
        await srv.close()
    assert _reached(sink, "/redir?")  # 첫 홉(허용)은 도달
    assert _reached(sink, "/p/redir_hop") == []
    assert r.success is False
    assert r.data["egress"]["code"] == "egress_blocked"
    assert r.data["egress"]["host"] == "127.0.0.1"


async def test_direct_navigate_block_reports_reason_to_agent(sink):
    srv = BrowserMCPServer(allowed_domains=("localhost",))
    try:
        r = await _nav(srv, f"http://127.0.0.1:{sink.port}/p/direct")
    finally:
        await srv.close()
    assert _reached(sink, "/p/direct") == []
    assert r.success is False
    assert r.error_code is ErrorCode.INVALID_URL
    eg = r.data["egress"]
    assert eg["code"] == "egress_blocked" and eg["category"] == "not_in_allowlist"
    assert eg["open_with"] == "serve --allow-domain <도메인>"


# -- 재바인딩 ------------------------------------------------------------------


async def test_dns_rebinding_connects_only_to_verified_ip(sink, monkeypatch):
    """검사 시점 해석(127.0.0.1, 허용)과 Chrome 해석(::1, 막힘으로 간주)이 달라도
    요청은 검증한 IP 로만 간다.

    실측(base): route 는 도메인을 해석하지 않고 Chrome 이 따로 해석해 ::1 에 도달했다.
    """
    if not sink.has_v6:
        pytest.skip("::1 바인드 불가")
    import ipaddress

    from playwright.async_api._generated import BrowserType

    from security import egress

    orig_launch = BrowserType.launch

    async def launch(self, *a, **kw):  # 시험 전용: Chrome 해석을 ::1 로
        kw["args"] = list(kw.get("args") or []) + ["--host-resolver-rules=MAP rebind.test [::1]"]
        return await orig_launch(self, *a, **kw)

    monkeypatch.setattr(BrowserType, "launch", launch)
    monkeypatch.setattr(egress, "_system_resolve",
                        lambda h: ["127.0.0.1"] if h == "rebind.test" else socket.getaddrinfo(h, None))
    orig_cat = egress.EgressGuard.ip_category_blocked

    def cat(self, addr):  # ::1 을 '사설 목적지'로 간주(재바인딩 표적 역할)
        if addr == ipaddress.ip_address("::1"):
            return "private"
        return orig_cat(self, addr)

    monkeypatch.setattr(egress.EgressGuard, "ip_category_blocked", cat)
    srv = BrowserMCPServer()
    try:
        await _nav(srv, f"http://rebind.test:{sink.port}/p/rebind")
        await asyncio.sleep(0.3)
    finally:
        await srv.close()
    hits = _reached(sink, "/p/rebind")
    assert [h["dst"] for h in hits if h["dst"] == "::1"] == []
    assert [h["dst"] for h in hits] == ["127.0.0.1"]


# -- route 밖 경로 ---------------------------------------------------------------


async def test_websocket_to_blocked_target_is_not_reached(sink):
    port = sink.port
    html = f"<script>new WebSocket('ws://127.0.0.1:{port}/p/ws_tok')</script>"
    srv = BrowserMCPServer(allowed_domains=("localhost",))
    try:
        await _nav(srv, _page(port, html))
        await asyncio.sleep(1.0)
    finally:
        await srv.close()
    assert _reached(sink, "/p/ws_tok") == []


async def test_webrtc_stun_udp_is_not_sent(sink):
    port = sink.port
    html = ("<script>const pc=new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:%d'}]});"
            "pc.createDataChannel('x');pc.createOffer().then(o=>pc.setLocalDescription(o));</script>"
            % port)
    srv = BrowserMCPServer()
    try:
        await _nav(srv, _page(port, html))
        await asyncio.sleep(2.0)
    finally:
        await srv.close()
    assert sink.udp == []


async def test_service_worker_fetch_to_blocked_target_is_not_reached(sink):
    port = sink.port
    html = ("<script>navigator.serviceWorker.register('/sw.js?t=http://127.0.0.1:%d/p/sw_tok')"
            "</script>" % port)
    srv = BrowserMCPServer(allowed_domains=("localhost",))
    try:
        await _nav(srv, _page(port, html))
        await asyncio.sleep(1.5)
    finally:
        await srv.close()
    assert _reached(sink, "/sw.js")
    assert _reached(sink, "/p/sw_tok") == []


# -- 정상 사용 ------------------------------------------------------------------


async def test_loopback_mock_still_works_by_default(sink):
    srv = BrowserMCPServer()
    try:
        r = await _nav(srv, f"http://127.0.0.1:{sink.port}/p/normal")
        obs = await srv.call_tool("browser_observe_page", {})
    finally:
        await srv.close()
    assert r.success is True
    assert "egress" not in r.data
    assert obs.success is True
    assert _reached(sink, "/p/normal")


async def test_block_loopback_blocks_mock_and_redirect_to_loopback(sink):
    srv = BrowserMCPServer(block_loopback=True)
    try:
        r = await _nav(srv, f"http://localhost:{sink.port}/p/bl_direct")
    finally:
        await srv.close()
    assert r.success is False and r.data["egress"]["category"] == "loopback"
    assert _reached(sink, "/p/bl_direct") == []


async def test_proxy_death_does_not_fall_back_to_direct(sink):
    """프록시가 죽으면 브라우저가 직접 접속으로 빠지지 않는다(fail-closed)."""
    srv = BrowserMCPServer()
    try:
        await srv.start()
        await srv._egress_proxy.close()
        r = await _nav(srv, f"http://127.0.0.1:{sink.port}/p/after_death")
    finally:
        await srv.close()
    assert r.success is False
    assert _reached(sink, "/p/after_death") == []


# -- WS-29b R1: 업스트림 실패를 이동 실패로 (NB-1), 쿼리 비밀 미노출 (NB-4) ------------


def _nx_resolve(monkeypatch):
    from security import egress

    orig = egress._system_resolve

    def fake(h):
        if h.endswith(".invalid"):
            raise socket.gaierror(8, "nodename nor servname provided")
        return orig(h)

    monkeypatch.setattr(egress, "_system_resolve", fake)


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_unresolvable_domain_navigate_fails_like_base(sink, monkeypatch, caplog, scheme):
    """해석 실패 도메인 navigate 는 base 처럼 이동 실패 — 프록시 502 를 성공으로 보이지 않는다."""
    import logging

    _nx_resolve(monkeypatch)
    caplog.set_level(logging.DEBUG)
    srv = BrowserMCPServer()
    try:
        r = await _nav(srv, f"{scheme}://nx-does-not-exist-zz.invalid/x?token=SECRET#frag")
    finally:
        await srv.close()
    assert r.success is False
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["egress"] == {"code": "resolve_failed", "host": "nx-does-not-exist-zz.invalid"}
    assert "SECRET" not in caplog.text


async def test_connect_failure_navigate_fails(sink):
    """검증 통과 목적지(루프백 닫힌 포트) 접속 실패도 이동 실패(connect_failed)."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed = probe.getsockname()[1]
    probe.close()
    srv = BrowserMCPServer()
    try:
        r = await _nav(srv, f"http://127.0.0.1:{closed}/x")
    finally:
        await srv.close()
    assert r.success is False
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["egress"]["code"] == "connect_failed"


async def test_site_returning_real_502_still_navigates(sink):
    """사이트가 실제로 준 502 는 이동 성공 그대로(last_http_status 로 보인다) — 프록시 기록 없음."""
    srv = BrowserMCPServer()
    try:
        r = await _nav(srv, f"http://127.0.0.1:{sink.port}/bad502")
    finally:
        await srv.close()
    assert r.success is True
    assert "egress" not in r.data
    assert r.data.get("last_http_status") == 502
    assert _reached(sink, "/bad502")


async def test_blocked_navigate_query_secret_not_in_result_or_log(sink, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    srv = BrowserMCPServer(allowed_domains=("localhost",))
    try:
        r = await _nav(srv, f"http://127.0.0.1:{sink.port}/p/q?token=SECRET#frag")
        recs = srv._egress.blocked_requests
    finally:
        await srv.close()
    assert r.success is False
    assert "SECRET" not in json.dumps(r.data["egress"])
    assert recs and all("SECRET" not in d.url for d in recs)
    assert "SECRET" not in caplog.text
    assert _reached(sink, "/p/q") == []
