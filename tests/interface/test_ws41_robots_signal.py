"""WS-41: local HTTP fixtures through MCP call_tool and real Chromium."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from interface.mcp_server import BrowserMCPServer, create_server, envelope_json
from security.egress import EgressGuard, EgressPolicy
from security.robots_signal import RobotsSignals

from test_run_cli import requires_chromium


@dataclass
class Site:
    body: bytes = b"User-agent: *\nDisallow: /\n"
    status: int = 200
    delay: float = 0
    robots_redirect: str = ""
    page_redirect: str = ""
    requests: Counter = field(default_factory=Counter)
    user_agents: dict[str, str] = field(default_factory=dict)
    headers: dict[str, dict[str, str]] = field(default_factory=dict)
    stream_bytes: int = 0
    chunked: bool = False
    robots_set_cookie: str = ""
    page_body: bytes = b"<!doctype html><title>Mock</title><button>Read</button>"
    content_encoding: str = ""
    url: str = ""


@pytest.fixture
def site():
    state = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            state.requests[path] += 1
            state.user_agents[path] = self.headers.get("User-Agent", "")
            state.headers[path] = {key.lower(): value for key, value in self.headers.items()}
            robots = path == "/robots.txt" or path.startswith("/rep/")
            if robots:
                time.sleep(state.delay)
            redirect = state.robots_redirect if path == "/robots.txt" else state.page_redirect if path == "/redirect" else ""
            data = state.body if robots else state.page_body
            self.send_response(302 if redirect else state.status if robots else 200)
            if redirect:
                self.send_header("Location", redirect)
            if robots and state.robots_set_cookie:
                self.send_header("Set-Cookie", state.robots_set_cookie)
            if robots and state.content_encoding:
                self.send_header("Content-Encoding", state.content_encoding)
            streaming = robots and state.stream_bytes
            if streaming and state.chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(state.stream_bytes if streaming else len(data)))
            self.end_headers()
            try:
                if streaming:
                    remaining = state.stream_bytes
                    while remaining:
                        chunk = (data if remaining == state.stream_bytes else b"#" + b"x" * 65535)[:remaining]
                        if state.chunked:
                            self.wfile.write(f"{len(chunk):x}".encode() + b"\r\n" + chunk + b"\r\n")
                        else:
                            self.wfile.write(chunk)
                        remaining -= len(chunk)
                    if state.chunked:
                        self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()


async def navigate(server, url):
    return await server.call_tool("browser_navigate", {"url": url})


@requires_chromium
async def test_disallow_signal_navigate_observe_cache_and_browser_ua(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        first = await navigate(server, site.url + "/a")
        second = await navigate(server, site.url + "/b")
        obs = await server.call_tool("browser_observe_page", {})
    for result in (first, second, obs):
        assert result.success
        assert result.data["robots"]["disallowed"] is True
        assert result.data["robots"]["rule"] == "Disallow: /"
        assert result.data["robots"]["ai_agents_disallowed"]
        assert json.loads(envelope_json(result))["data"]["robots"] == result.data["robots"]
    assert site.requests["/robots.txt"] == 1
    assert site.requests["/a"] == site.requests["/b"] == 1
    assert site.user_agents["/robots.txt"] == site.user_agents["/a"]
    assert len(json.dumps(first.data["robots"], separators=(",", ":"))) < 300


@requires_chromium
@pytest.mark.parametrize("body,status", [(b"User-agent: *\nAllow: /", 200), (b"", 404), (b"", 410),
                                        (b"User-agent: *\nDisallow: /", 500), (b"", 401), (b"", 403)])
async def test_allowed_missing_and_unknown_are_silent_and_cached(site, body, status):
    site.body, site.status = body, status
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        for _ in range(2):
            result = await navigate(server, site.url + "/read")
            assert result.success and "robots" not in result.data
    assert site.requests["/robots.txt"] == 1


@requires_chromium
async def test_final_url_and_ai_only_signal(site):
    site.page_redirect = "/private"
    site.body = b"User-agent: *\nAllow: /\nUser-agent: GPTBot\nDisallow: /private"
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        result = await navigate(server, site.url + "/redirect")
        allowed = await navigate(server, site.url + "/public")
    assert result.success
    assert result.data["robots"]["disallowed"] is False
    assert result.data["robots"]["ai_agents_disallowed"] == ["GPTBot"]
    assert "robots" not in allowed.data


@requires_chromium
async def test_slow_response_returns_then_populates_next_result(site):
    site.delay = 2.0
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        start = time.monotonic()
        first = await navigate(server, site.url + "/read")
        elapsed = time.monotonic() - start
        assert first.success and "robots" not in first.data
        assert elapsed < 1.9, elapsed
        await asyncio.sleep(0.7)
        second = await server.call_tool("browser_observe_page", {})
        assert second.data["robots"]["disallowed"]
        assert site.requests["/robots.txt"] == 1


@requires_chromium
async def test_blocked_egress_makes_zero_requests(site):
    async with BrowserMCPServer(allowed_domains=["allowed.invalid"], recipes=False) as server:
        result = await navigate(server, site.url + "/read")
        assert not result.success and "robots" not in result.data
        assert site.requests == Counter()


@requires_chromium
async def test_cached_signal_is_not_used_after_egress_restriction(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert (await navigate(server, site.url + "/read")).data["robots"]
        server._egress.allow_loopback = False
        result = await server.call_tool("browser_observe_page", {})
        assert "robots" not in result.data
    assert site.requests["/robots.txt"] == 1


@requires_chromium
@pytest.mark.parametrize("status", [500, 401, 403])
async def test_single_fetch_for_concurrent_requests_and_failure_ttl(site, status):
    from playwright.async_api import async_playwright

    now = [0.0]
    service = RobotsSignals(clock=lambda: now[0])
    guard = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True)
    site.status, site.delay = status, 0.1
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        try:
            results = await asyncio.gather(*(service.signal(context, site.url + "/", guard) for _ in range(3)))
            assert results == [None, None, None]
            assert site.requests["/robots.txt"] == 1
            now[0] = 599
            assert await service.signal(context, site.url + "/", guard) is None
            assert site.requests["/robots.txt"] == 1
            site.status, site.delay = 200, 0
            now[0] = 601
            assert await service.signal(context, site.url + "/", guard)
            assert site.requests["/robots.txt"] == 2
            now[0] += 86399
            assert await service.signal(context, site.url + "/", guard)
            assert site.requests["/robots.txt"] == 2
            now[0] += 2
            assert await service.signal(context, site.url + "/", guard)
            assert site.requests["/robots.txt"] == 3
        finally:
            await service.close()
            await browser.close()


@requires_chromium
@pytest.mark.parametrize("redirect", [False, True])
async def test_robots_uses_proxy_and_browser_ua_without_credentials(site, redirect):
    site.robots_redirect = "/rep/ok" if redirect else ""
    site.robots_set_cookie = "mock_robots=synthetic; Path=/"
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await server._page.context.add_cookies([{"name": "mock_session", "value": "synthetic", "url": site.url}])
        await server._page.context.set_extra_http_headers({"Authorization": "Bearer synthetic", "X-Mock": "context-header"})
        before = server._egress_proxy.handled
        result = await navigate(server, site.url + "/read")
        assert result.success and result.data["robots"]
        assert server._egress_proxy.handled >= before + 2
        assert site.user_agents["/robots.txt"] == site.user_agents["/read"]
        assert "cookie" in site.headers["/read"]
        for header in ("cookie", "authorization", "proxy-authorization", "x-mock"):
            assert header not in site.headers["/robots.txt"]
            if redirect:
                assert header not in site.headers["/rep/ok"]


def rss_mb():
    return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True).strip()) / 1024


@requires_chromium
@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("size_mb", [1, 64])
async def test_oversize_unknown_and_other_origin_navigation_remains_responsive(site, chunked, size_mb, record_property):
    site.stream_bytes, site.chunked = size_mb * 1024 * 1024, chunked
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        # A different host is a different origin even on the same local Mock.
        benign = site.url.replace("127.0.0.1", "localhost")
        site.stream_bytes = 0
        await navigate(server, benign + "/warm")
        site.stream_bytes = size_mb * 1024 * 1024
        baseline = rss_mb()
        peaks = [baseline]
        done = asyncio.Event()

        async def sample():
            while not done.is_set():
                peaks.append(rss_mb())
                await asyncio.sleep(0.03)

        sampler = asyncio.create_task(sample())
        try:
            hostile = await navigate(server, site.url + "/hostile")
            started = time.monotonic()
            followup = await navigate(server, benign + "/followup")
            elapsed = time.monotonic() - started
            assert hostile.success and "robots" not in hostile.data
            assert followup.success
            assert elapsed < 0.5, elapsed
            assert not server._robots.pending
            peaks.append(rss_mb())
            record_property("followup_navigation_s", elapsed)
            record_property("peak_python_rss_increase_mb", max(peaks) - baseline)
            assert max(peaks) - baseline < 100, peaks
        finally:
            done.set()
            await sampler


@requires_chromium
@pytest.mark.parametrize("target", ["http://10.10.10.10/robots.txt", "http://outside.invalid/robots.txt",
                                      "http://169.254.169.254/robots.txt"])
async def test_redirect_to_private_or_outside_allowlist_never_requests_target(site, target):
    site.robots_redirect = target
    async with BrowserMCPServer(nav_settle=False, recipes=False, allowed_domains=["127.0.0.1"]) as server:
        result = await navigate(server, site.url + "/read")
        assert result.success and "robots" not in result.data
    assert site.requests == Counter({"/read": 1, "/robots.txt": 1})


async def test_origin_cache_is_bounded_lru_and_expired_entries_refetch(monkeypatch):
    from security.robots_signal import RobotsRules

    now = [0.0]
    service = RobotsSignals(clock=lambda: now[0])
    guard = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True)
    fetched = Counter()

    async def fetch(context, origin, guard, *args):
        fetched[origin] += 1
        return RobotsRules.parse(b"User-agent: *\nDisallow: /\n")

    monkeypatch.setattr(service, "_fetch", fetch)
    urls = [f"http://127.0.0.1:{10000 + i}" for i in range(1001)]
    for url in urls[:1000]:
        assert await service.signal(None, url, guard)
    assert await service.signal(None, urls[0], guard)  # refresh recency
    assert await service.signal(None, urls[1000], guard)
    assert len(service._cache) == 1000
    assert urls[0] in service._cache and urls[1] not in service._cache
    assert await service.signal(None, urls[1], guard)
    assert fetched[urls[1]] == 2 and len(service._cache) == 1000
    now[0] = 86401
    assert await service.signal(None, urls[0], guard)
    assert fetched[urls[0]] == 2
    await service.close()


@requires_chromium
async def test_background_deadline_unknown_retry_and_close(site):
    site.delay = 12
    now = [0.0]
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        server._robots = RobotsSignals(clock=lambda: now[0])
        result = await navigate(server, site.url + "/read")
        assert result.success and "robots" not in result.data
        await asyncio.sleep(8.7)
        assert not server._robots.pending
        assert "robots" not in (await server.call_tool("browser_observe_page", {})).data
        assert site.requests["/robots.txt"] == 1
        now[0], site.delay = 601, 0
        assert (await server.call_tool("browser_observe_page", {})).data["robots"]
        assert site.requests["/robots.txt"] == 2
    assert not server._robots.pending


@requires_chromium
@pytest.mark.parametrize("target", ["/rep/ok", "/robots.txt", "cross-origin"])
async def test_robots_redirect_scope_and_hop_limit(site, target):
    site.robots_redirect = site.url.replace("127.0.0.1", "localhost") + "/rep/ok" if target == "cross-origin" else target
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        result = await navigate(server, site.url + "/read")
        assert result.success
        assert ("robots" in result.data) is (target == "/rep/ok")
    expected = 2 if target == "/rep/ok" else 1 if target == "cross-origin" else 6
    assert sum(n for p, n in site.requests.items() if p == "/robots.txt" or p.startswith("/rep/")) == expected


@requires_chromium
async def test_host_is_part_of_origin_cache_key(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        first = await navigate(server, site.url + "/read")
        other = await navigate(server, site.url.replace("127.0.0.1", "localhost") + "/read")
        assert first.data["robots"] and other.data["robots"]
        assert first.data["robots"]["robots_url"] != other.data["robots"]["robots_url"]
    assert site.requests["/robots.txt"] == 2


@requires_chromium
async def test_shutdown_cancels_inflight_background_fetch(site):
    site.delay = 12
    server = BrowserMCPServer(nav_settle=False, recipes=False)
    await server.start()
    try:
        result = await navigate(server, site.url + "/read")
        assert result.success and "robots" not in result.data
        assert server._robots.pending
        started = time.monotonic()
        await server.close()
        assert time.monotonic() - started < 1
        assert not server._robots.pending
    finally:
        await server.close()


@requires_chromium
async def test_non_web_url_does_not_fetch(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        result = await navigate(server, "about:blank")
        assert result.success and "robots" not in result.data
        obs = await server.call_tool("browser_observe_page", {})
        assert "robots" not in obs.data
    assert site.requests == Counter()


@pytest.mark.parametrize("recipes", [True, False])
async def test_initialize_instructions_always_describe_signal(recipes):
    sdk, backend = create_server(recipes=recipes)
    try:
        instructions = sdk.create_initialization_options().instructions
        assert "data.robots" in instructions and "사용자 뜻" in instructions
    finally:
        await backend.close()


@pytest.mark.parametrize("url", ["about:blank", "data:text/html,test", "chrome-error://chromewebdata/",
                                 "file:///mock.html", "http://127.0.0.1:bad/", "http:///missing",
                                 "http://user:pass@127.0.0.1/"])
async def test_non_web_and_malformed_urls_skip_request(url):
    class Context:
        request_reads = 0

        @property
        def request(self):
            self.request_reads += 1
            raise AssertionError("request must not be accessed")

    service = RobotsSignals()
    guard = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True)
    context = Context()
    assert await service.signal(context, url, guard) is None
    assert context.request_reads == 0
    assert not service.pending


async def test_private_ip_and_dns_resolution_block_before_request():
    class Context:
        request_reads = 0
        page_reads = 0

        @property
        def pages(self):
            self.page_reads += 1
            raise AssertionError("blocked fetch must not access the browser")

        @property
        def request(self):
            self.request_reads += 1
            raise AssertionError("request must not be accessed")

    async def resolve(host):
        return ["10.10.10.10"]

    service = RobotsSignals()
    guard = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, resolver=resolve)
    context = Context()
    assert await service.signal(context, "http://10.10.10.10/", guard) is None
    assert await service.signal(context, "http://private.invalid/", guard) is None
    assert context.request_reads == 0
    assert context.page_reads == 0
    assert not service.pending


@requires_chromium
async def test_configured_context_ua_is_preserved_without_probe_page(site):
    from playwright.async_api import async_playwright

    service = RobotsSignals()
    guard = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(user_agent="SyntheticMockBrowser/1.0")
        try:
            page = await context.new_page()
            await page.goto(site.url + "/configured")
            await page.close()
            assert await service.signal(context, site.url, guard)
            assert site.user_agents["/robots.txt"] == site.user_agents["/configured"] == "SyntheticMockBrowser/1.0"
            assert context.pages == []
        finally:
            await service.close()
            await browser.close()


@requires_chromium
@pytest.mark.parametrize("getter", ["return 'EvilBot/9.9'", "throw new Error('mock')", "while(true){}"])
async def test_hostile_tab_zero_cannot_control_other_origin_robots_ua(site, getter, record_property):
    site.page_body = ("<!doctype html><title>Mock</title><script>"
                      "Object.defineProperty(navigator,'userAgent',{get(){" + getter + "}})"
                      "</script><button>Read</button>").encode()
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert (await navigate(server, site.url + "/tab0")).success
        assert (await server.call_tool("browser_tab_control", {"command": "create", "url": "about:blank"})).success
        # A new origin forces a robots cache miss while tab zero stays hostile.
        other = site.url.replace("127.0.0.1", "localhost")
        started = time.monotonic()
        first = await navigate(server, other + "/other")
        elapsed = time.monotonic() - started
        second = await navigate(server, other + "/again")
        assert elapsed < 0.5, elapsed
        assert first.success and second.success
        assert first.data["robots"]["disallowed"] and second.data["robots"]["disallowed"]
        assert site.user_agents["/robots.txt"] == site.user_agents["/other"]
        assert "HeadlessChrome" in site.user_agents["/other"]
        assert site.requests["/robots.txt"] == 2  # one per origin
        assert not server._robots.pending
        record_property("other_origin_navigation_s", elapsed)


@requires_chromium
@pytest.mark.parametrize("rejection", ["content-length", "content-encoding"])
async def test_rejected_headers_do_not_consume_raw_stream(site, monkeypatch, rejection):
    import httpx

    if rejection == "content-length":
        site.stream_bytes = 1024 * 1024
    else:
        # Deliberately plain text despite the header: deleting the encoding
        # check must not quietly become a parser-negative compressed fixture.
        site.content_encoding = "gzip"
    reads = []
    original = httpx.Response.aiter_raw

    async def raw(response, *args, **kwargs):
        reads.append(str(response.url))
        async for chunk in original(response, *args, **kwargs):
            yield chunk

    monkeypatch.setattr(httpx.Response, "aiter_raw", raw)
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        result = await navigate(server, site.url + "/read")
        assert result.success and "robots" not in result.data
        assert not server._robots.pending
    assert site.requests["/robots.txt"] == 1
    assert reads == []


@requires_chromium
async def test_robots_ignores_environment_transport_configuration(site, monkeypatch):
    # httpx trusts SSL_CERT_FILE at construction even for plain HTTP. The
    # nonexistent local path exposes trust_env=True without any public egress.
    monkeypatch.setenv("SSL_CERT_FILE", "nonexistent-ws41-r2-ca.pem")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        result = await navigate(server, site.url + "/read")
        assert result.success and result.data["robots"]["disallowed"]
    assert site.requests["/robots.txt"] == 1
    assert "authorization" not in site.headers["/robots.txt"]


@requires_chromium
async def test_browser_ua_is_discovered_once_per_context_without_pages(site, monkeypatch):
    from playwright.async_api import async_playwright

    service = RobotsSignals()
    guard = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        original = browser.new_browser_cdp_session
        calls = []

        async def cdp():
            calls.append(True)
            return await original()

        async def forbidden_page():
            raise AssertionError("UA discovery must not create a page")

        monkeypatch.setattr(browser, "new_browser_cdp_session", cdp)
        monkeypatch.setattr(context, "new_page", forbidden_page)
        try:
            results = await asyncio.gather(service.signal(context, site.url, guard),
                                           service.signal(context, site.url.replace("127.0.0.1", "localhost"), guard))
            assert all(results)
            assert calls == [True]
            assert context.pages == []
            assert "HeadlessChrome" in site.user_agents["/robots.txt"]
        finally:
            await service.close()
            await browser.close()


@requires_chromium
@pytest.mark.parametrize("failure", ["throw", "stall"])
async def test_unavailable_browser_ua_is_cached_unknown_without_navigation_delay(site, monkeypatch, failure):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        calls = []

        async def unavailable():
            calls.append(True)
            if failure == "stall":
                await asyncio.sleep(30)
            raise RuntimeError("synthetic browser protocol failure")

        monkeypatch.setattr(server._page.context.browser, "new_browser_cdp_session", unavailable)
        for url in (site.url, site.url.replace("127.0.0.1", "localhost")):
            started = time.monotonic()
            result = await navigate(server, url + "/read")
            assert time.monotonic() - started < 0.5
            assert result.success and "robots" not in result.data
        assert calls == [True]
        assert not server._robots.pending
    assert site.requests["/robots.txt"] == 0


@requires_chromium
async def test_each_same_origin_redirect_checks_guard_before_proxy_request(site, monkeypatch):
    from security.egress import EgressDecision

    site.robots_redirect = "/rep/ok"
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await navigate(server, site.url + "/warm")
        server._robots._cache.clear()
        evaluate = server._egress.evaluate_async

        async def blocked_hop(url):
            if url.endswith("/rep/ok"):
                return EgressDecision(False, url, None, "synthetic hop denied")
            return await evaluate(url)

        monkeypatch.setattr(server._egress, "evaluate_async", blocked_hop)
        before = server._egress_proxy.handled
        assert await server._robots.signal(server._page.context, site.url, server._egress, server._egress_proxy) is None
        assert server._egress_proxy.handled == before + 1
        assert site.requests["/rep/ok"] == 1  # warmup only
