"""WS-47: cookies our retry caused, persisted success memory and skip reasons.

Synthetic loopback sites only; profiles live under the per-test temporary
AGENT_BROWSER_PROFILE_ROOT (tests/interface/conftest.py).
"""
from __future__ import annotations

import json
import os
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from interface.mcp_server import BrowserMCPServer, create_server
from test_run_cli import requires_chromium

ROBOTS = b"User-agent: *\nAllow: /\n"
VISITOR = "visitor-WS47-value-7f3a9c"

NOTES = {
    "cookies_present": "이 사이트에 로그인 등 기존 쿠키가 있어 이름을 바꾸지 않았습니다",
    "already_failed": "앞서 일반 Chrome 이름으로도 막혀 다시 시도하지 않았습니다",
    "robots_not_confirmed": "robots.txt 가 일반 접근 허용을 확정해 주지 않아 이름을 바꾸지 않았습니다",
    "vendor_challenge": "보안 업체의 확인·차단 화면이라 이름을 바꿔 다시 시도하지 않았습니다",
    "disabled": "헤드리스 이름 재시도가 꺼져 있어(--no-headless-retry) 다시 시도하지 않았습니다",
    "headed": "창 모드 브라우저라 헤드리스 이름 문제가 아니어서 다시 시도하지 않았습니다",
}
SKIP_GUIDANCE = "data.retry_skipped 가 있으면(retried:false) 이름을 바꾸지 않은 이유 — note 를 그대로 설명할 것(일시적 차단이라 하지 말 것)"


@dataclass
class Site:
    url: str = ""
    robots: bytes = ROBOTS
    robots_status: int = 200
    retry_status: int = 200
    headless_status: int = 403
    auth_challenge: str = ""
    body: bytes = b"Forbidden"
    cookie: str = VISITOR
    success_body: bytes = b"<!doctype html><title>Read</title>"
    headers: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)

    def received(self, path):
        return [h for p, h in self.requests if p == path]

    def headless(self, path, index=0):
        return "HeadlessChrome" in self.received(path)[index]["user-agent"]


@contextmanager
def visitor_site():
    state = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.requests.append((self.path, headers))
            if self.path == "/robots.txt":
                data, status = state.robots, state.robots_status
            elif self.path.startswith("/auth") and state.auth_challenge and "authorization" not in headers:
                self.send_response_only(401)
                self.send_header("WWW-Authenticate", state.auth_challenge)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif "HeadlessChrome" in headers.get("user-agent", ""):
                data, status = state.body, state.headless_status
            else:
                status = state.retry_status
                data = state.success_body if status == 200 else state.body
            self.send_response_only(status)
            self.send_header("Content-Type", "text/plain" if self.path == "/robots.txt" else "text/html")
            self.send_header("Content-Length", str(len(data)))
            if status == 200 and self.path != "/robots.txt" and state.cookie:
                # A persistent visitor cookie, like the real site that triggered WS-47.
                self.send_header("Set-Cookie", f"visitor={state.cookie}; Path=/; Max-Age=31536000")
            if self.path != "/robots.txt":
                for key, value in state.headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield state
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


@pytest.fixture
def site():
    with visitor_site() as state:
        yield state


async def navigate(server, url):
    return await server.call_tool("browser_navigate", {"url": url})


def ledger_path(profile_root, name):
    from browser.serve_profile import profile_dir

    return profile_dir(name, root=profile_root) / "headless_retry.json"


#: WS-52: cookies_present carries which kind of cookie change blocked the rename.
CHANGE_NOTES = {
    "new_cookie": "도구가 남기지 않은 새 쿠키가 있어(로그인 가능성) 이름을 바꾸지 않았습니다",
    "known_value_changed": ("도구가 앞서 남긴 쿠키의 값이 바뀌어(사이트 스크립트 또는 로그인) 로그인 여부를 확인할 수 없어 "
                            "이름을 바꾸지 않았습니다 — 로그인 없이 읽어야 하면 운영자에게 이 사이트 쿠키 정리를 요청하세요"),
}


def skipped(code, change=None):
    out = {"retried": False, "reason": code, "note": NOTES[code]}
    if change is not None:
        out.update(cookie_change=change, cookie_change_note=CHANGE_NOTES[change])
    return out


# -- 1. visitor cookie ledger + persistence across restart ------------------


@requires_chromium
async def test_visitor_cookie_ledger_survives_restart(site, _isolated_profile_root):
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws47-a") as server:
        first = await navigate(server, site.url + "/one")
        assert first.data["identity"]["original_status"] == 403
        assert "retry_skipped" not in first.data
        cookies = await server._page.context.cookies()
        assert [c["name"] for c in cookies] == ["visitor"]  # the site planted it during our retry
        second = await navigate(server, site.url + "/two")
        assert second.data["last_http_status"] == 200
        assert second.data["identity"]["remembered"] is True
    assert len(site.received("/two")) == 1 and not site.headless("/two")

    path = ledger_path(_isolated_profile_root, "ws47-a")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    assert VISITOR.encode() not in raw  # never the cookie value
    data = json.loads(raw)
    entry = data["origins"][site.url]
    assert entry["state"] == "succeeded"
    assert [(c["name"], c["path"]) for c in entry["cookies"]] == [("visitor", "/")]
    assert set(entry["cookies"][0]) == {"name", "domain", "path", "value_digest"}
    import hashlib
    assert entry["cookies"][0]["value_digest"] != hashlib.sha256(VISITOR.encode()).hexdigest()

    # Restart with the same profile: memory and cookie both come back.
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws47-a") as server:
        assert [c["name"] for c in await server._page.context.cookies()] == ["visitor"]
        third = await navigate(server, site.url + "/three")
        assert third.data["last_http_status"] == 200
        assert third.data["identity"]["remembered"] is True
    assert len(site.received("/three")) == 1 and not site.headless("/three")


@requires_chromium
@pytest.mark.parametrize("change", ["new_session_cookie", "visitor_value_changed"])
async def test_login_like_cookie_change_restores_native_name(site, change):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert (await navigate(server, site.url + "/one")).data["last_http_status"] == 200
        if change == "new_session_cookie":
            cookie = {"name": "session", "value": "logged-in", "url": site.url}
        else:
            cookie = {"name": "visitor", "value": "another-value", "url": site.url}
        # WS-52: a login needs an agent action or a handoff, and either ends the settle tail first.
        server._headless_retry.cancel_tail()
        await server._page.context.add_cookies([cookie])
        result = await navigate(server, site.url + "/after-login")
        assert result.data["last_http_status"] == 403
        assert "identity" not in result.data
        assert result.data["retry_skipped"] == skipped(
            "cookies_present", "new_cookie" if change == "new_session_cookie" else "known_value_changed")
    assert len(site.received("/after-login")) == 1 and site.headless("/after-login")


@requires_chromium
async def test_preexisting_cookie_before_ledger_means_zero_retries(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await server._page.context.add_cookies([{"name": "visitor", "value": VISITOR, "url": site.url}])
        result = await navigate(server, site.url + "/one")
        assert result.data["retry_skipped"] == skipped("cookies_present", "new_cookie")
        assert not server._headless_retry.attempted
    assert len(site.received("/one")) == 1


@requires_chromium
async def test_cookie_created_during_retry_keeps_native_name_for_rest_of_navigate(site, monkeypatch):
    site.success_body = b"<!doctype html><title>Read</title><img src='/inline.png'>"
    async with BrowserMCPServer(recipes=False) as server:
        hook = server._attach_block_hint

        async def wait_inline(result):
            await hook(result)
            await server._page.wait_for_load_state("load")

        # Hold the navigate open until inline requests have left (same window as WS-45 tests).
        monkeypatch.setattr(server, "_attach_block_hint", wait_inline)
        first = await navigate(server, site.url + "/one")
        assert first.data["identity"]["original_status"] == 403
        # During the retry the visitor cookie was new: the image kept the native name.
        assert site.headless("/inline.png", 0)
        second = await navigate(server, site.url + "/two")
        assert second.data["identity"]["remembered"] is True
    # Once ledgered, the same cookie no longer blocks the renamed request.
    assert not site.headless("/two")
    assert not site.headless("/inline.png", len(site.received("/inline.png")) - 1)


@requires_chromium
async def test_corrupt_ledger_file_starts_without_memory(site, _isolated_profile_root):
    from browser import serve_profile

    folder = serve_profile.prepare(serve_profile.profile_dir("ws47-c", root=_isolated_profile_root))
    (folder / "headless_retry.json").write_bytes(b"{not json")
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws47-c") as server:
        assert not server._headless_retry.attempted
        result = await navigate(server, site.url + "/one")
        assert result.data["identity"]["original_status"] == 403  # first retry from scratch
    data = json.loads((folder / "headless_retry.json").read_bytes())
    assert data["origins"][site.url]["state"] == "succeeded"


@requires_chromium
async def test_failed_state_persists_with_profile(site, _isolated_profile_root):
    site.retry_status = 403
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws47-f") as server:
        assert (await navigate(server, site.url + "/one")).data["identity"]["retry_failed"] is True
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws47-f") as server:
        result = await navigate(server, site.url + "/two")
        assert result.data["retry_skipped"] == skipped("already_failed")
    assert len(site.received("/two")) == 1 and site.headless("/two")


# -- 2. unit: ledger, digest, bounds, expiry, persistence --------------------


def _cookie(name, value, domain="example.test", path="/"):
    return {"name": name, "value": value, "domain": domain, "path": path,
            "expires": -1, "secure": False, "httpOnly": False, "sameSite": "Lax"}


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return list(self._cookies)


async def test_ledger_compares_value_fingerprint_not_just_name(tmp_path):
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    retry.load(tmp_path / "headless_retry.json")
    origin = "http://example.test"
    before = await retry.snapshot(FakeContext([]), origin + "/")
    after = await retry.snapshot(FakeContext([_cookie("visitor", "v1")]), origin + "/")
    retry.commit_success(origin, before, after)
    assert await retry.cookies_cleared(FakeContext([_cookie("visitor", "v1")]), origin + "/x")
    assert not await retry.cookies_cleared(FakeContext([_cookie("visitor", "v2")]), origin + "/x")
    assert not await retry.cookies_cleared(FakeContext([_cookie("visitor", "v1"), _cookie("session", "s")]),
                                           origin + "/x")
    assert not await retry.cookies_cleared(FakeContext([_cookie("visitor", "v1", path="/account")]),
                                           origin + "/x")
    # Unrelated origins' cookies are irrelevant; failure to read cookies is not "cleared".
    assert await retry.cookies_cleared(FakeContext([_cookie("x", "y", domain="other.test")]), origin + "/")

    class Broken:
        async def cookies(self):
            raise RuntimeError("closed")

    assert not await retry.cookies_cleared(Broken(), origin + "/")
    # Persisted and reloaded with the same per-profile key.
    again = HeadlessRetry()
    again.load(tmp_path / "headless_retry.json")
    assert again.state(origin) == "succeeded"
    assert await again.cookies_cleared(FakeContext([_cookie("visitor", "v1")]), origin + "/x")
    assert not await again.cookies_cleared(FakeContext([_cookie("visitor", "v2")]), origin + "/x")


async def test_digest_is_keyed_hmac_and_file_is_private(tmp_path, monkeypatch):
    import hashlib

    from browser.headless_retry import HeadlessRetry

    # The temporary file must already be private before it is renamed into place.
    real_replace = os.replace
    modes = []

    def recording_replace(src, dst):
        modes.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    path = tmp_path / "headless_retry.json"
    retry = HeadlessRetry()
    retry.load(path)
    origin = "http://example.test"
    after = await retry.snapshot(FakeContext([_cookie("visitor", VISITOR)]), origin + "/")
    retry.commit_success(origin, [], after)
    assert modes == [0o600]
    raw = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert VISITOR.encode() not in raw
    assert hashlib.sha256(VISITOR.encode()).hexdigest().encode() not in raw
    other = HeadlessRetry()
    other.load(tmp_path / "other.json")
    digest = (await other.snapshot(FakeContext([_cookie("visitor", VISITOR)]), origin + "/"))[0]["value_digest"]
    assert digest != after[0]["value_digest"]  # random key per profile


def test_corrupt_or_unreadable_ledger_is_empty(tmp_path):
    from browser.headless_retry import HeadlessRetry

    for payload in (b"{broken", b"[]", json.dumps({"version": 1, "key": "zz", "origins": {}}).encode(),
                    json.dumps({"version": 1, "key": "00" * 32,
                                "origins": {"http://a.test": {"state": "bogus", "updated": 1, "cookies": []}}}).encode()):
        path = tmp_path / "headless_retry.json"
        path.write_bytes(payload)
        retry = HeadlessRetry()
        retry.load(path)
        assert retry.states == {} and retry.ledger == {}
    missing = HeadlessRetry()
    missing.load(tmp_path / "absent" / "headless_retry.json")
    assert missing.states == {}


def test_size_bounds_drop_oldest(tmp_path):
    from browser.headless_retry import MAX_COOKIES, MAX_ORIGINS, HeadlessRetry

    assert (MAX_ORIGINS, MAX_COOKIES) == (1000, 50)
    now = [1_000_000.0]
    path = tmp_path / "headless_retry.json"
    retry = HeadlessRetry(clock=lambda: now[0])
    retry.load(path)
    for index in range(MAX_ORIGINS + 5):
        now[0] += 1
        entries = [{"name": f"c{n}", "domain": "a.test", "path": "/", "value_digest": f"{n:064x}"}
                   for n in range(MAX_COOKIES + 3)] if index == MAX_ORIGINS + 4 else []
        retry.commit_success(f"http://s{index}.test", [], entries)
    assert len(retry.states) == MAX_ORIGINS
    assert "http://s0.test" not in retry.states and "http://s5.test" in retry.states
    newest = retry.ledger[f"http://s{MAX_ORIGINS + 4}.test"]
    assert len(newest) == MAX_COOKIES and newest[0]["name"] == "c3"  # oldest cookies dropped
    data = json.loads(path.read_bytes())
    assert len(data["origins"]) == MAX_ORIGINS
    reloaded = HeadlessRetry()
    reloaded.load(path)
    assert len(reloaded.states) == MAX_ORIGINS


def test_failed_memory_expires_after_a_day(tmp_path):
    from browser.headless_retry import FAILED_TTL_S, HeadlessRetry

    now = [5_000_000.0]
    path = tmp_path / "headless_retry.json"
    retry = HeadlessRetry(clock=lambda: now[0])
    retry.load(path)
    retry.mark_failed("http://a.test")
    assert retry.state("http://a.test") == "failed"
    reloaded = HeadlessRetry(clock=lambda: now[0])
    reloaded.load(path)
    assert reloaded.state("http://a.test") == "failed"
    now[0] += FAILED_TTL_S + 1
    assert reloaded.state("http://a.test") is None
    late = HeadlessRetry(clock=lambda: now[0])
    late.load(path)
    assert late.state("http://a.test") is None


def test_memory_only_without_profile_writes_nothing(tmp_path, monkeypatch):
    from browser.headless_retry import HeadlessRetry

    monkeypatch.chdir(tmp_path)
    retry = HeadlessRetry()
    retry.mark_failed("http://a.test")
    retry.commit_success("http://b.test", [], [])
    assert retry.state("http://b.test") == "succeeded"
    assert list(tmp_path.iterdir()) == []


# -- 3. skip reasons ----------------------------------------------------------


@requires_chromium
@pytest.mark.parametrize("code", ["already_failed", "robots_not_confirmed", "vendor_challenge_body",
                                  "vendor_challenge_header", "disabled", "headed"])
async def test_each_skip_reason(site, code):
    options = {}
    if code == "robots_not_confirmed":
        site.robots_status = 503
    if code == "vendor_challenge_body":
        site.body = b"<title>Just a moment</title>Checking your browser"
    if code == "vendor_challenge_header":
        site.headers = {"cf-mitigated": "challenge"}
    if code == "disabled":
        options["headless_retry"] = False
    if code == "headed":
        options["headless"] = False
        site.retry_status = 403  # a headed name is also refused
    if code == "already_failed":
        site.retry_status = 403
    async with BrowserMCPServer(nav_settle=False, recipes=False, **options) as server:
        result = await navigate(server, site.url + "/one")
        if code == "already_failed":
            assert result.data["identity"]["retry_failed"] is True
            assert "retry_skipped" not in result.data  # we did retry
            result = await navigate(server, site.url + "/two")
        assert result.data["last_http_status"] == 403
        assert "identity" not in result.data
        assert result.data["retry_skipped"] == skipped(code.removesuffix("_body").removesuffix("_header"))
    assert len(site.received("/one")) == (2 if code == "already_failed" else 1)


@requires_chromium
async def test_no_skip_reason_on_success_or_remembered(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        ok = await navigate(server, site.url + "/one")
        assert "retry_skipped" not in ok.data
        again = await navigate(server, site.url + "/two")
        assert again.data["identity"]["remembered"] is True and "retry_skipped" not in again.data
        site.retry_status = 403
        denied = await navigate(server, site.url + "/three")
        assert denied.data["identity"]["retry_failed"] is True and "retry_skipped" not in denied.data


def test_skip_notes_are_fixed_and_page_free():
    from browser.headless_retry import SKIP_NOTES

    assert SKIP_NOTES == NOTES


async def test_skip_guidance_reaches_sdk_initialize():
    from harness.mcp_binding import _run_session

    sdk, _ = create_server()
    assert SKIP_GUIDANCE in sdk.instructions
    findings = await _run_session()
    assert SKIP_GUIDANCE in findings["instructions"]


def test_ledger_file_never_reaches_profile_listing(_isolated_profile_root):
    from browser import serve_profile

    folder = serve_profile.prepare(serve_profile.profile_dir("ws47-l", root=_isolated_profile_root))
    (folder / "headless_retry.json").write_text(json.dumps({"secret_marker": VISITOR}))
    listing = json.dumps(serve_profile.list_profiles(root=_isolated_profile_root), ensure_ascii=False)
    assert VISITOR not in listing and "secret_marker" not in listing
    assert os.path.exists(folder / "headless_retry.json")


# -- 4. R3: nobody answers authentication with our credentials --------------------------
# The browser is given no proxy credentials at all (the proxy asks for none), and
# the retry interception does not enable auth handling. The egress proxy turns a
# site's own 407 into a marked 502 (tests/security/test_ws47_r3_upstream_407.py),
# so no 407 reaches the browser over the proxy connection.


def _record_cdp(server):
    """Record every CDP command our interception session sends (method, params)."""
    calls = []
    context = server._page.context
    new_session = context.new_cdp_session

    async def watched(page):
        session = await new_session(page)
        send = session.send

        async def recorder(method, params=None):
            calls.append((method, dict(params or {})))
            return await send(method, params)

        session.send = recorder
        return session

    context.new_cdp_session = watched
    return calls


def _no_auth_handling(calls):
    methods = [m for m, _ in calls]
    assert "Fetch.continueWithAuth" not in methods, methods
    enables = [p for m, p in calls if m == "Fetch.enable"]
    assert enables and all(not p.get("handleAuthRequests") for p in enables), enables


def _no_credentials_sent(requests):
    assert not any("authorization" in h or "proxy-authorization" in h for h in requests), requests


@requires_chromium
async def test_restart_first_navigate_remembered_succeeds_without_our_auth(site, _isolated_profile_root):
    """(i) The original WS-47 symptom: a restarted server whose very first navigate
    is a remembered retry met our proxy's 407 inside the interception (empty
    proxy-auth cache; the challenge went to our Fetch session, not Playwright's)
    and ended in 407. Without proxy credentials there is no challenge: it succeeds
    and our session sends no auth answer at all."""
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws47-p") as server:
        assert (await navigate(server, site.url + "/one")).data["identity"]["original_status"] == 403
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws47-p") as server:
        calls = _record_cdp(server)
        again = await navigate(server, site.url + "/two")
    assert again.data["last_http_status"] == 200, again.data
    assert again.data["identity"]["remembered"] is True
    _no_auth_handling(calls)
    assert len(site.received("/two")) == 1 and not site.headless("/two")
    _no_credentials_sent(site.received("/two"))


@requires_chromium
@pytest.mark.parametrize("remembered", [False, True])
async def test_site_407_reaches_browser_as_502_and_is_not_amplified(site, remembered):
    """(ii) A site's 407 + Proxy-Authenticate becomes the proxy's marked 502: no
    challenge reaches the browser, nobody answers it, the site sees one request."""
    challenge = 'Basic realm="local-synthetic"'
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        if remembered:
            assert (await navigate(server, site.url + "/warm")).data["identity"]["original_status"] == 403
            site.retry_status = 407
        site.headers = {"Proxy-Authenticate": challenge}
        calls = _record_cdp(server)
        seen = []
        server._page.on("response", lambda r: seen.append((r.url, r.status, dict(r.headers))))
        target = site.url + "/target"
        if not remembered:
            site.headless_status = 407  # outside any retry: the native request itself gets 407
        result = await navigate(server, target)
    document = [s for s in seen if s[0] == target]
    assert document and all(status == 502 for _, status, _ in document), document
    assert all("proxy-authenticate" not in {k.lower() for k in headers} for _, _, headers in document)
    assert all(headers.get("x-agent-browser-egress") == "upstream-407-replaced" for _, _, headers in document)
    assert result.data["last_http_status"] == 502
    if remembered:
        assert result.data["identity"]["remembered"] is True
        _no_auth_handling(calls)
    received = site.received("/target")
    assert len(received) == 1, len(received)  # no replay with credentials, no amplification
    _no_credentials_sent(received)


@requires_chromium
@pytest.mark.parametrize("challenge", [
    'Basic realm="local-synthetic"', 'Digest realm="local-synthetic", nonce="abc", qop="auth"', "NTLM"])
async def test_site_401_inside_retry_gets_no_credentials(site, challenge):
    """(v) A site's 401 inside the retry keeps the browser default: no credentials."""
    site.retry_status = 401
    site.headers = {"WWW-Authenticate": challenge}
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        calls = _record_cdp(server)
        result = await navigate(server, site.url + "/one")
    assert result.data["last_http_status"] == 401
    _no_auth_handling(calls)
    received = site.received("/one")
    assert len(received) == 2  # native 403 + one renamed attempt, no auth replay
    _no_credentials_sent(received)


@requires_chromium
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("challenge", ['Basic realm="local-synthetic"',
                                       'Digest realm="local-synthetic", nonce="abc", qop="auth"'])
async def test_site_401_never_receives_proxy_credentials(site, warm, challenge):
    """(v) Outside any retry, with a cold or a warm proxy-auth cache, a site's 401
    gets no Authorization header (no proxy token handed to the site)."""
    site.auth_challenge = challenge
    async with BrowserMCPServer(nav_settle=False, recipes=False, headless_retry=False) as server:
        if warm:
            assert (await navigate(server, site.url + "/warm")).data["last_http_status"] == 403
        result = await navigate(server, site.url + "/auth")
    assert result.data["last_http_status"] == 401
    received = site.received("/auth")
    assert len(received) == 1, len(received)
    _no_credentials_sent(received)


# -- 4b. R2: a page cannot reach the proxy listener through a DNS alias -----------------


async def _rebind_probe(server, *, intercepted=False):
    """Local re-run of the R1 verifier's attack (audit_realm_rebind.py) via the resolver seam.

    ``rebind.test`` first resolves to ``::1`` where a local page is served on the proxy's
    own port number; then it re-resolves to ``127.0.0.1`` (the proxy listener) and the
    page fetches same-origin ``/probe``. Returns what the page JavaScript saw.
    """
    import asyncio

    from browser.headless_retry import HeadlessRetry

    guard, proxy = server._egress, server._egress_proxy
    port = proxy.port
    target = ["::1"]
    old = guard.resolver, guard.dns_cache_ttl_s

    async def resolver(host):
        assert host == "rebind.test", host
        return [target[0]]

    async def origin(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        data = ROBOTS if b"/robots.txt " in head else b"<!doctype html><title>local attacker page</title>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                     + str(len(data)).encode() + b"\r\nConnection: close\r\n\r\n" + data)
        await writer.drain()
        writer.close()

    guard.resolver, guard.dns_cache_ttl_s = resolver, 0
    upstream = await asyncio.start_server(origin, "::1", port)
    try:
        url = f"http://rebind.test:{port}"
        loaded = await navigate(server, url + "/page")
        assert loaded.success and loaded.data["last_http_status"] == 200  # other IP, same port: allowed
        page = server._page
        ua = await page.evaluate("navigator.userAgent")

        async def probe():
            target[0] = "127.0.0.1"
            return await page.evaluate("""async () => {
                try {
                    const r = await fetch('/probe');
                    const headers = {};
                    r.headers.forEach((v, k) => { headers[k] = v; });
                    return {status: r.status, headers, body: await r.text()};
                } catch (e) { return {error: String(e)}; }
            }""")

        if intercepted:
            async with HeadlessRetry().present(page, url, ua, server._robots, guard, proxy):
                return await asyncio.wait_for(probe(), 20)
        return await asyncio.wait_for(probe(), 20)
    finally:
        guard.resolver, guard.dns_cache_ttl_s = old
        upstream.close()
        await upstream.wait_closed()


@requires_chromium
@pytest.mark.parametrize("intercepted", [False, True])
async def test_page_cannot_reach_proxy_listener_through_dns_alias(intercepted):
    async with BrowserMCPServer(nav_settle=False, recipes=False, headless_retry=False) as server:
        seen = await _rebind_probe(server, intercepted=intercepted)
    assert "proxy-authenticate" not in seen.get("headers", {}), seen
    # the outer hop itself refused the self-connection as an egress block
    assert seen.get("status") == 403, seen
    assert seen["headers"].get("x-agent-browser-egress") == "blocked; category=loopback", seen


# -- 5. R1: file byte cap on write, finite timestamps on read -----------------


def test_save_keeps_file_under_read_cap_dropping_oldest(tmp_path):
    from browser.headless_retry import MAX_FILE_BYTES, MAX_ORIGINS, HeadlessRetry

    now = [2_000_000.0]
    path = tmp_path / "headless_retry.json"
    retry = HeadlessRetry(clock=lambda: now[0])
    retry.load(path)
    for index in range(MAX_ORIGINS):
        now[0] += 1
        origin = f"http://s{index}.test"
        retry.states[origin] = "succeeded"
        retry.updated[origin] = now[0]
        retry.ledger[origin] = [{"name": f"c{n}-" + "x" * 2000, "domain": f"s{index}.test", "path": "/",
                                 "value_digest": f"{n:064x}"} for n in range(5)]
    assert retry.save() is True
    size = path.stat().st_size
    assert size <= MAX_FILE_BYTES
    reloaded = HeadlessRetry()
    reloaded.load(path)
    assert 0 < len(reloaded.states) < MAX_ORIGINS
    newest = f"http://s{MAX_ORIGINS - 1}.test"
    assert reloaded.state(newest) == "succeeded" and len(reloaded.ledger[newest]) == 5
    assert "http://s0.test" not in reloaded.states  # oldest forgotten first
    assert set(reloaded.states) == set(retry.states)  # memory matches what was written


@pytest.mark.parametrize("when", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_timestamp_is_corrupt(tmp_path, when):
    from browser.headless_retry import HeadlessRetry

    path = tmp_path / "headless_retry.json"
    path.write_text('{"version":1,"key":"' + "00" * 32 + '","origins":{"http://a.test":'
                    '{"state":"failed","updated":' + when + ',"cookies":[]}}}')
    retry = HeadlessRetry()
    retry.load(path)
    assert retry.states == {} and retry.ledger == {} and retry.state("http://a.test") is None
