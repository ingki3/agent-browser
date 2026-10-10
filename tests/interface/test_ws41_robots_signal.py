"""WS-41: local HTTP fixtures through MCP call_tool and real Chromium."""

from __future__ import annotations

import asyncio
import json
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
    url: str = ""


@pytest.fixture
def site():
    state = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            state.requests[path] += 1
            state.user_agents[path] = self.headers.get("User-Agent", "")
            robots = path == "/robots.txt" or path.startswith("/rep/")
            if robots:
                time.sleep(state.delay)
            redirect = state.robots_redirect if path == "/robots.txt" else state.page_redirect if path == "/redirect" else ""
            data = state.body if robots else b"<!doctype html><title>Mock</title><button>Read</button>"
            self.send_response(302 if redirect else state.status if robots else 200)
            if redirect:
                self.send_header("Location", redirect)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
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
async def test_single_fetch_for_concurrent_requests_and_failure_ttl(site):
    from playwright.async_api import async_playwright

    now = [0.0]
    service = RobotsSignals(clock=lambda: now[0])
    guard = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True)
    site.status, site.delay = 500, 0.1
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
    assert not service.pending
