"""WS-69 — serve(egress 프록시 경유) 경로의 이동 실패 종류·부분 성공 (로컬 서버만).

serve 는 egress 프록시가 항상 켜져 있어 DNS·연결 실패가 Chrome 오류가 아니라 프록시 502 문서/CONNECT 실패로
보인다(리뷰 B3). 그래서 kind 는 서버 신호(_attach_egress_block·_attach_upstream_failure)가 결과를 다시 만든 뒤
data.egress.code 로 정한다.
"""

from __future__ import annotations

import datetime
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from contracts import ActionResult, ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

POLL = ("<!doctype html><meta charset=utf-8><title>폴링</title><h1 id=t>본문 열림</h1>"
        "<script>setInterval(()=>fetch('/ping?'+Math.random()).catch(()=>0),150)</script>")


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a: Any) -> None:  # noqa: D401
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/loop"):
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = (POLL if self.path.startswith("/poll") else "<p id=t>ok</p>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(srv: ThreadingHTTPServer):
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def http_port():
    srv = _serve(ThreadingHTTPServer(("127.0.0.1", 0), _H))
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _self_signed(tmp: Path) -> tuple:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    c, k = tmp / "c.pem", tmp / "k.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return c, k


@pytest.fixture
def https_port(tmp_path):
    c, k = _self_signed(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(c, k)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    _serve(srv)
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _nx_resolve(monkeypatch):
    from security import egress

    orig = egress._system_resolve

    def fake(h):
        if h.endswith(".invalid"):
            raise socket.gaierror(8, "nodename nor servname provided")
        return orig(h)

    monkeypatch.setattr(egress, "_system_resolve", fake)


async def _nav(srv: BrowserMCPServer, url: str, **kw: Any) -> ActionResult:
    return await srv.call_tool("browser_navigate", {"url": url, "timeout_ms": 8000, **kw})


# -- 이동 실패 종류 ------------------------------------------------------------------


@requires_chromium
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_unresolvable_host_is_dns(monkeypatch, scheme):
    _nx_resolve(monkeypatch)
    srv = BrowserMCPServer()
    try:
        r = await _nav(srv, f"{scheme}://nx-ws69-zz.invalid/x")
    finally:
        await srv.close()
    assert r.success is False and r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["egress"]["code"] == "resolve_failed"
    assert r.data["nav_failure"]["kind"] == "dns"
    assert "주소" in r.data["hint"] and "소용없" in r.data["hint"]


@requires_chromium
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_closed_port_is_connect(scheme):
    srv = BrowserMCPServer()
    try:
        r = await _nav(srv, f"{scheme}://127.0.0.1:{_closed_port()}/x")
    finally:
        await srv.close()
    assert r.success is False and r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["egress"]["code"] == "connect_failed"
    assert r.data["nav_failure"]["kind"] == "connect"
    assert "접속하지 못함" in r.data["hint"]


@requires_chromium
async def test_self_signed_https_is_tls(https_port):
    srv = BrowserMCPServer()
    try:
        r = await _nav(srv, f"https://localhost:{https_port}/x")
    finally:
        await srv.close()
    assert r.success is False and r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["nav_failure"]["kind"] == "tls", r.error_message
    assert r.data["nav_failure"]["net_error"].startswith("ERR_CERT_")
    assert "인증서" in r.data["hint"]


@requires_chromium
async def test_redirect_loop_is_other_with_net_error(http_port):
    srv = BrowserMCPServer()
    try:
        r = await _nav(srv, f"http://127.0.0.1:{http_port}/loop")
    finally:
        await srv.close()
    assert r.success is False and r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["nav_failure"] == {"kind": "other", "net_error": "ERR_TOO_MANY_REDIRECTS"}


@requires_chromium
async def test_egress_blocked_is_blocked_kind(http_port):
    srv = BrowserMCPServer(allowed_domains=("localhost",))
    try:
        r = await _nav(srv, f"http://127.0.0.1:{http_port}/x")
    finally:
        await srv.close()
    assert r.success is False and r.error_code is ErrorCode.INVALID_URL
    assert r.data["egress"]["code"] == "egress_blocked"
    assert r.data["nav_failure"] == {"kind": "blocked"}
    assert "hint" not in r.data  # 기존 egress 신호 그대로(새 안내 없음)


@requires_chromium
async def test_tab_create_connect_failure_has_kind():
    """https CONNECT 실패로 탭 생성이 실패하면 nav_failure(egress.code 기준). http 는 프록시 502 를 성공으로 보는
    문제가 남아 있다(N6 — 범위 밖, NB 이슈)."""
    srv = BrowserMCPServer()
    try:
        await srv.start()
        r = await srv.call_tool("browser_tab_control",
                                {"command": "create", "url": f"https://127.0.0.1:{_closed_port()}/x"})
    finally:
        await srv.close()
    assert r.success is False and r.error_code is ErrorCode.PAGE_CRASHED
    assert r.data["nav_failure"]["kind"] == "connect"


# -- 부분 성공(serve) -----------------------------------------------------------------


@requires_chromium
async def test_serve_partial_success_keeps_load_state(http_port):
    srv = BrowserMCPServer()
    try:
        r = await _nav(srv, f"http://127.0.0.1:{http_port}/poll", wait_until="networkidle", timeout_ms=1500)
        ex = await srv.call_tool("browser_extract", {"selector": "#t"})
        bad = await srv.call_tool("browser_extract", {"selector": "h1:has-text('본문')"})
    finally:
        await srv.close()
    assert r.success is True, r.error_message
    assert r.data["load_state"] == {"requested": "networkidle", "reached": "domcontentloaded", "timed_out": True}
    assert "nav_failure" not in r.data
    assert ex.success and ex.data["items"]["text"] == "본문 열림"
    assert bad.error_code is ErrorCode.ELEMENT_NOT_FOUND and bad.data["selector_error"] is True


# -- 단위: 결과 재구성 지점·헤드리스 재시도 ------------------------------------------------------


def _upstream_server(status):
    from security.egress import EgressGuard, EgressPolicy, UpstreamFailure

    class _StatusStub:
        def status_for(self, page):  # noqa: ANN001
            return status

    srv = BrowserMCPServer()
    page = SimpleNamespace(url="http://svc.test:8080/x")
    srv._page = page
    srv._dispatcher = SimpleNamespace(ctx=SimpleNamespace(page=page))
    srv._page_status = _StatusStub()
    srv._egress = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True)
    before = srv._egress.upstream_total
    srv._egress.record_upstream_failure(UpstreamFailure(
        host="svc.test", code="resolve_failed", url="http://svc.test:8080/x", port=8080, page=page))
    return srv, before


def test_rebuilt_upstream_result_carries_nav_failure():
    srv, before = _upstream_server(502)
    ok = ActionResult(success=True, action=ActionType.NAVIGATE, current_url="http://svc.test:8080/x",
                      snapshot_epoch=0, tab_id="t", retry_safe=True, data={"url": "http://svc.test:8080/x"})
    r = srv._attach_upstream_failure(ActionType.NAVIGATE, {"url": "http://svc.test:8080/x"}, ok, before)
    assert r.success is False and r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert r.data["nav_failure"] == {"kind": "dns"}


def test_dispatcher_kind_is_overridden_by_egress_code():
    """https CONNECT 실패 — 디스패처는 ERR_TUNNEL_CONNECTION_FAILED(connect) 로 보지만 프록시 기록은 resolve_failed."""
    from actions.nav_failure import attach_nav_failure

    srv, before = _upstream_server(None)
    data = attach_nav_failure({}, "net::ERR_TUNNEL_CONNECTION_FAILED")
    failed = ActionResult(success=False, action=ActionType.NAVIGATE, current_url="about:blank", snapshot_epoch=0,
                          tab_id="t", retry_safe=True, error_code=ErrorCode.NAVIGATE_TIMEOUT,
                          error_message="net::ERR_TUNNEL_CONNECTION_FAILED", data=data)
    r = srv._attach_upstream_failure(ActionType.NAVIGATE, {"url": "http://svc.test:8080/x"}, failed, before)
    assert r.data["nav_failure"] == {"kind": "dns"}
    assert "소용없" in r.data["hint"]


def test_success_and_crash_results_get_no_nav_failure():
    srv = BrowserMCPServer()
    for res in (
        ActionResult(success=True, action=ActionType.NAVIGATE, current_url="u", snapshot_epoch=0, tab_id="t",
                     retry_safe=True, data={}),
        ActionResult(success=False, action=ActionType.NAVIGATE, current_url="u", snapshot_epoch=0, tab_id="t",
                     retry_safe=True, error_code=ErrorCode.PAGE_CRASHED, error_message="closed", data={}),
        ActionResult(success=False, action=ActionType.CLICK, current_url="u", snapshot_epoch=0, tab_id="t",
                     retry_safe=True, error_code=ErrorCode.NAVIGATE_TIMEOUT, error_message="x", data={}),
    ):
        r = srv._attach_nav_failure(res.action, {}, res)
        assert "nav_failure" not in r.data


async def test_headless_retry_is_skipped_for_partial_success():
    """부분 성공(load_state.timed_out)은 헤드리스 재시도를 하지 않고 data.retry_skipped 를 남긴다(N4)."""
    srv = BrowserMCPServer()
    page = SimpleNamespace(url="http://svc.test/x")
    srv._dispatcher = SimpleNamespace(ctx=SimpleNamespace(page=page, root_page=None))
    srv._egress = object()
    srv._core = SimpleNamespace(headless=True)
    srv.headless_retry = True
    result = ActionResult(success=True, action=ActionType.NAVIGATE, current_url=page.url, snapshot_epoch=1,
                          tab_id="t", retry_safe=True, data={
                              "last_http_status": 403,
                              "challenge": {"kind": "blocked", "vendor": "generic", "reason": "HTTP 403 + 짧은 본문"},
                              "load_state": {"requested": "networkidle", "reached": "domcontentloaded",
                                             "timed_out": True}})
    out = await srv._retry_headless_navigation("browser_navigate", {"url": page.url}, {"url": page.url}, result)
    assert out is None
    assert result.data["retry_skipped"]["reason"] == "load_timed_out"
    assert result.data["retry_skipped"]["retried"] is False
