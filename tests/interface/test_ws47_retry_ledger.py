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
            elif "HeadlessChrome" in headers.get("user-agent", ""):
                data, status = state.body, 403
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


def skipped(code):
    return {"retried": False, "reason": code, "note": NOTES[code]}


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
        await server._page.context.add_cookies([cookie])
        result = await navigate(server, site.url + "/after-login")
        assert result.data["last_http_status"] == 403
        assert "identity" not in result.data
        assert result.data["retry_skipped"] == skipped("cookies_present")
    assert len(site.received("/after-login")) == 1 and site.headless("/after-login")


@requires_chromium
async def test_preexisting_cookie_before_ledger_means_zero_retries(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await server._page.context.add_cookies([{"name": "visitor", "value": VISITOR, "url": site.url}])
        result = await navigate(server, site.url + "/one")
        assert result.data["retry_skipped"] == skipped("cookies_present")
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
