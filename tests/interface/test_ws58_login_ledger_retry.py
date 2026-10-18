"""WS-58 — '쿠키가 있으면 이름 안 바꿈' → '실제로 로그인한 사이트면 이름 안 바꿈'.

헤드리스 이름 재시도는 (a) 로그인 기록(login_ledger + 서버 메모리)이 그 호스트를 덮거나 (b) 장부에 서버 쿠키로
없는 HttpOnly 키가 있을 때만 거부한다. 로컬 루프백 사이트·임시 프로필·합성 값만 쓴다.
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from browser import login_ledger as ll
from interface.mcp_server import BrowserMCPServer
from test_run_cli import requires_chromium

ROBOTS = b"User-agent: *\nAllow: /\n"
FORM = (b"<!doctype html><title>Sign in</title><form method='post' action='/login'>"
        b"<input id='user' name='user' aria-label='User'><input id='pw' type='password' name='pw' aria-label='Password'>"
        b"<button id='go' type='submit'>Sign in</button></form>")
#: 사람·도구가 직접 여는 화면(이름 차단 없음) — 로그인 폼과 그 제출.
OPEN_PATHS = ("/form", "/login", "/plant")


@dataclass
class Site:
    url: str = ""
    #: 일반 Chrome 이름 200 응답에 붙일 Set-Cookie 들.
    set_cookies: list = field(default_factory=list)
    #: POST /login · GET /plant 응답의 Set-Cookie 들.
    login_cookies: list = field(default_factory=list)
    requests: list = field(default_factory=list)

    def received(self, path):
        return [h for p, h in self.requests if p == path]

    def headless(self, path, index=0):
        return "HeadlessChrome" in self.received(path)[index]["user-agent"]


@contextmanager
def serve_site():
    state = Site()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body, cookies=()):
            self.send_response_only(status)
            self.send_header("Content-Type", "text/plain" if self.path == "/robots.txt" else "text/html")
            self.send_header("Content-Length", str(len(body)))
            for value in cookies:
                self.send_header("Set-Cookie", value)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            state.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            self._reply(200, b"<!doctype html><title>Welcome</title>", state.login_cookies)

        def do_GET(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.requests.append((self.path, headers))
            if self.path == "/robots.txt":
                return self._reply(200, ROBOTS)
            if self.path == "/form":
                return self._reply(200, FORM)
            if self.path == "/plant":
                return self._reply(200, b"<!doctype html><title>Planted</title>", state.login_cookies)
            if "HeadlessChrome" in headers.get("user-agent", ""):
                return self._reply(403, b"Forbidden")
            return self._reply(200, b"<!doctype html><title>Read</title>", state.set_cookies)

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
    with serve_site() as state:
        yield state


async def navigate(server, url):
    return await server.call_tool("browser_navigate", {"url": url})


def renamed(result):
    assert result.data["last_http_status"] == 200, result.data
    assert result.data["identity"]["presented_as"] == "chrome"
    assert "retry_skipped" not in result.data


def native(result, reason, change=None):
    from browser.headless_retry import skip_reason

    assert result.data["last_http_status"] == 403, result.data
    assert "identity" not in result.data
    assert result.data["retry_skipped"] == skip_reason(reason, change)


def ledger_dir(root, name):
    from browser import serve_profile

    return serve_profile.prepare(serve_profile.profile_dir(name, root=root))


# -- 1. 쿠키 규칙 (b): 비 HttpOnly·장부 HttpOnly 키 값 변경은 막지 않음 ---------------------------


@requires_chromium
async def test_analytics_cookie_only_still_retries(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await server._page.context.add_cookies([{"name": "_ga", "value": "GA1.1.58", "url": site.url}])
        renamed(await navigate(server, site.url + "/one"))
    assert len(site.received("/one")) == 2 and not site.headless("/one", 1)


@requires_chromium
async def test_non_httponly_value_change_still_renamed(site):
    site.set_cookies = ["visitor=v1; Path=/; Max-Age=3600"]
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        renamed(await navigate(server, site.url + "/one"))
        server._headless_retry.cancel_tail()
        await server._page.context.add_cookies([{"name": "visitor", "value": "v2", "url": site.url},
                                                {"name": "_gid", "value": "x", "url": site.url}])
        again = await navigate(server, site.url + "/two")
        assert again.data["last_http_status"] == 200 and again.data["identity"]["remembered"] is True
    assert not site.headless("/two")


@requires_chromium
async def test_ledgered_httponly_key_value_rotation_still_renamed(site):
    site.set_cookies = ["__cf_bm=v1; Path=/; HttpOnly; Max-Age=3600"]
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        renamed(await navigate(server, site.url + "/one"))
        assert [e["name"] for e in server._headless_retry.ledger[site.url]] == ["__cf_bm"]
        server._headless_retry.cancel_tail()
        # The server rotates the value outside our retry (another request) — same key, HttpOnly.
        await server._page.context.add_cookies([{"name": "__cf_bm", "value": "v2", "url": site.url,
                                                 "httpOnly": True}])
        again = await navigate(server, site.url + "/two")
        assert again.data["last_http_status"] == 200 and again.data["identity"]["remembered"] is True
        # A fresh 403 decision (memory forgotten) also accepts the rotated ledgered key.
        server._headless_retry.states.clear()
        renamed(await navigate(server, site.url + "/three"))


@requires_chromium
async def test_new_httponly_key_keeps_native_name(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await server._page.context.add_cookies([{"name": "session", "value": "s", "url": site.url,
                                                 "httpOnly": True}])
        native(await navigate(server, site.url + "/one"), "new_server_cookie", "new_cookie")
        assert not server._headless_retry.attempted
    assert len(site.received("/one")) == 1


@requires_chromium
async def test_new_httponly_key_during_retry_keeps_native_name_for_rest(site):
    # A server-only cookie created by the renamed document blocks the renamed name for the
    # rest of that navigate (per-request check), like WS-47 did for any new cookie.
    site.set_cookies = ["sid=fresh; Path=/; HttpOnly; Max-Age=3600"]
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        renamed(await navigate(server, site.url + "/one"))
        retry = server._headless_retry
        assert await retry.cookies_cleared(server._page.context, site.url)  # ledgered by the success
        await server._page.context.add_cookies([{"name": "auth", "value": "a", "url": site.url,
                                                 "httpOnly": True}])
        assert await retry.block_reason(server._page.context, site.url) == ("new_server_cookie", "new_cookie")


# -- 2. 로그인 기록 규칙 (a): 도구가 아는 로그인 사건 -------------------------------------------------


@requires_chromium
async def test_chrome_import_record_keeps_native_name(site, _isolated_profile_root):
    folder = ledger_dir(_isolated_profile_root, "ws58-c")
    # WS-59 records https://<site>; scheme and port do not matter (cookies ignore the port).
    ll.record(folder, ["https://127.0.0.1"], source=ll.SOURCE_CHROME_IMPORT)
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws58-c") as server:
        native(await navigate(server, site.url + "/one"), "login_recorded")
        assert not server._headless_retry.attempted
    assert len(site.received("/one")) == 1


@requires_chromium
async def test_record_after_success_stops_remembered_rename(site, _isolated_profile_root):
    folder = ledger_dir(_isolated_profile_root, "ws58-r")
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws58-r") as server:
        renamed(await navigate(server, site.url + "/one"))
        ll.record(folder, [site.url], source=ll.SOURCE_CHROME_IMPORT)  # the human imports cookies meanwhile
        native(await navigate(server, site.url + "/two"), "login_recorded")
    assert site.headless("/two")


@requires_chromium
async def test_corrupt_login_ledger_fails_closed(site, _isolated_profile_root):
    folder = ledger_dir(_isolated_profile_root, "ws58-x")
    (folder / ll.LEDGER_FILE).write_bytes(b"{broken")
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws58-x") as server:
        result = await navigate(server, site.url + "/one")
        native(result, "login_ledger_unreadable")
        assert "broken" not in json.dumps(result.data, ensure_ascii=False)
        assert str(folder) not in json.dumps(result.data, ensure_ascii=False)
    assert len(site.received("/one")) == 1
    assert (folder / ll.LEDGER_FILE).read_bytes() == b"{broken"  # never overwritten


async def type_password(server, text):
    obs = await server.call_tool("browser_observe_page", {})
    by = {e["name"]: e["element_id"] for e in obs.data["observation"]["elements"]}
    return await server.call_tool("browser_type_text", {"element_id": by["Password"], "text": text,
                                                        "epoch": obs.data["observation"]["snapshot_epoch"]})


@requires_chromium
async def test_credentials_fill_records_login_flow(site):
    from security.secrets import SecretStore

    secrets = SecretStore({"SITE_PASSWORD": "synthetic-pass-58"})
    async with BrowserMCPServer(nav_settle=False, recipes=False, secrets=secrets) as server:
        assert (await navigate(server, site.url + "/form")).success
        typed = await type_password(server, "SITE_PASSWORD")
        assert typed.success and typed.data["secret_resolved"] is True
        assert server._headless_retry.logins.memory == {site.url: ll.SOURCE_LOGIN_FLOW}
        native(await navigate(server, site.url + "/one"), "login_recorded")


@requires_chromium
async def test_credentials_fill_persists_with_profile(site, _isolated_profile_root):
    from security.secrets import SecretStore

    secrets = SecretStore({"SITE_PASSWORD": "synthetic-pass-58"})
    async with BrowserMCPServer(nav_settle=False, recipes=False, secrets=secrets, profile="ws58-p") as server:
        assert (await navigate(server, site.url + "/form")).success
        assert (await type_password(server, "SITE_PASSWORD")).success
    folder = ledger_dir(_isolated_profile_root, "ws58-p")
    raw = (folder / ll.LEDGER_FILE).read_text()
    assert "synthetic-pass-58" not in raw and "SITE_PASSWORD" not in raw
    assert ll.load(folder)[site.url]["source"] == ll.SOURCE_LOGIN_FLOW
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws58-p") as server:
        native(await navigate(server, site.url + "/one"), "login_recorded")


async def _take(server):
    from interface.handoff import write_command

    write_command(server.hub.root, server.hub.server_id, "take")
    assert "taken" in await server._poll_handoff()
    assert server.hub.holder == "human"


async def _release(server):
    from interface.handoff import write_command

    write_command(server.hub.root, server.hub.server_id, "release")
    assert "released" in await server._poll_handoff()


@requires_chromium
async def test_human_control_password_submit_records_login(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await _take(server)
        page = server._page  # the human, in the window
        await page.goto(site.url + "/form")
        await page.fill("#user", "someone")
        await page.fill("#pw", "synthetic-human-pass")
        async with page.expect_navigation():
            await page.click("#go")
        await _release(server)
        assert server._headless_retry.logins.memory == {site.url: ll.SOURCE_HUMAN_CONTROL}
        native(await navigate(server, site.url + "/one"), "login_recorded")


@requires_chromium
async def test_human_control_without_login_records_nothing(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await _take(server)
        page = server._page
        await page.goto(site.url + "/form")
        await page.fill("#user", "someone")  # no password: reading, not a login
        await page.click("#user")
        await _release(server)
        assert server._headless_retry.logins.memory == {}
        renamed(await navigate(server, site.url + "/one"))


@requires_chromium
async def test_human_control_new_httponly_cookie_records_login(site):
    site.login_cookies = ["sid=human-session; Path=/; HttpOnly; Max-Age=3600"]
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await _take(server)
        await server._page.goto(site.url + "/plant")
        await _release(server)
        assert server._headless_retry.logins.memory == {"http://127.0.0.1": ll.SOURCE_HUMAN_CONTROL}
        # Rule (a) wins over rule (b): the record is the reason.
        native(await navigate(server, site.url + "/one"), "login_recorded")


@requires_chromium
async def test_secret_wanted_handoff_records_login(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await navigate(server, site.url + "/form")
        server.hub.request("비밀번호 입력", secret_wanted=True)
        await _take(server)
        await _release(server)
        assert server._headless_retry.logins.memory == {site.url: ll.SOURCE_HUMAN_CONTROL}
        native(await navigate(server, site.url + "/one"), "login_recorded")


@requires_chromium
async def test_login_job_records_login_flow(site, monkeypatch, _isolated_profile_root):
    from interface import login_cli
    from interface.handoff import write_command

    monkeypatch.setattr(login_cli, "display_available", lambda: True)
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws58-l") as server:
        monkeypatch.setattr(server, "_human_can_see", lambda: True)
        assert server.open_handoff()
        write_command(server.hub.root, server.hub.server_id, "login", url=site.url + "/form",
                      timeout=60, login_id="ws58login")
        await server._poll_handoff()
        for _ in range(100):
            if server.hub.login is not None and server.hub.login.status == "ready":
                break
            await asyncio.sleep(0.05)
        assert server.hub.login.status == "ready"
        write_command(server.hub.root, server.hub.server_id, "login_finish", login_id="ws58login")
        assert "released" in await server._poll_handoff()
        native(await navigate(server, site.url + "/one"), "login_recorded")
    folder = ledger_dir(_isolated_profile_root, "ws58-l")
    assert ll.load(folder)[site.url]["source"] == ll.SOURCE_LOGIN_FLOW


async def test_login_with_handoff_records_when_logged_in(tmp_path):
    from browser.login_flow import LoginState, login_with_handoff
    from security.credentials import Credential

    folder = tmp_path / "root" / "serve-x"
    folder.mkdir(parents=True)

    class Page:
        url = "https://example.test/home"

        async def evaluate(self, *_):
            return {"captcha": False, "pw": False}

    cred = Credential(domain="example.test", username="u", password="synthetic")
    out = await login_with_handoff(Page(), cred, ledger_profile=folder)
    assert out.state is LoginState.LOGGED_IN
    assert ll.load(folder)["https://example.test"]["source"] == ll.SOURCE_LOGIN_FLOW
    # Without a profile nothing is written; a corrupt ledger never breaks the login result.
    (folder / ll.LEDGER_FILE).write_bytes(b"[")
    out = await login_with_handoff(Page(), cred, ledger_profile=folder)
    assert out.state is LoginState.LOGGED_IN and (folder / ll.LEDGER_FILE).read_bytes() == b"["


# -- 3. 단위: 범위 경계·HttpOnly 판정·장부 키 추종 ------------------------------------------------


def _cookie(name, value, domain=".example.com", path="/", http_only=False):
    return {"name": name, "value": value, "domain": domain, "path": path, "expires": -1,
            "secure": False, "httpOnly": http_only, "sameSite": "Lax"}


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return list(self._cookies)


def test_login_record_scope_is_host_and_subdomains(tmp_path):
    from browser.login_records import LoginRecords, host_covered

    folder = tmp_path / "root" / "serve-b"
    folder.mkdir(parents=True)
    ll.record(folder, ["https://example.com"], source=ll.SOURCE_CHROME_IMPORT)
    records = LoginRecords()
    records.attach(folder)
    for url in ("https://example.com/x", "http://example.com:8080/", "https://www.example.com/",
                "https://a.b.example.com/"):
        assert records.covers(url), url
    for url in ("https://evil-example.com/", "https://example.com.evil.test/", "https://other.test/",
                "https://notexample.com/", "about:blank", ""):
        assert not records.covers(url), url
    # A login on a sub-domain covers that host (and its sub-domains), not its parent.
    records.record("https://accounts.shop.test/login", ll.SOURCE_LOGIN_FLOW)
    assert records.covers("https://accounts.shop.test/") and records.covers("https://x.accounts.shop.test/")
    assert not records.covers("https://shop.test/")
    assert host_covered("example.com", "example.com") and not host_covered("xexample.com", "example.com")


async def test_block_reason_order_and_detail(tmp_path):
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    url = "https://www.example.com/a"
    # rule (b): new HttpOnly key, then a js key that turned HttpOnly
    assert await retry.block_reason(FakeContext([_cookie("_ga", "1")]), url) is None
    assert await retry.block_reason(FakeContext([_cookie("sid", "1", http_only=True)]), url) == (
        "new_server_cookie", "new_cookie")
    origin = "https://www.example.com"
    retry.ledger[origin] = [{"name": "pref", "domain": ".example.com", "path": "/", "value_digest": "0" * 64,
                             "js": True},
                            {"name": "__cf_bm", "domain": ".example.com", "path": "/", "value_digest": "0" * 64}]
    assert await retry.block_reason(FakeContext([_cookie("__cf_bm", "any", http_only=True),
                                                 _cookie("pref", "2")]), url) is None
    assert await retry.block_reason(FakeContext([_cookie("pref", "2", http_only=True)]), url) == (
        "new_server_cookie", "known_value_changed")
    # same name on another path is another key
    assert await retry.block_reason(FakeContext([_cookie("__cf_bm", "v", path="/x", http_only=True)]), url) == (
        "new_server_cookie", "new_cookie")
    # rule (a) wins; an evil look-alike record does not cover
    retry.logins.record("https://evil-example.com", ll.SOURCE_LOGIN_FLOW)
    assert await retry.block_reason(FakeContext([]), url) is None
    retry.logins.record("https://example.com", ll.SOURCE_LOGIN_FLOW)
    assert await retry.block_reason(FakeContext([]), url) == ("login_recorded", None)

    class Broken:
        async def cookies(self):
            raise RuntimeError("closed")

    assert await HeadlessRetry().block_reason(Broken(), url) == ("cookies_unreadable", None)
    folder = tmp_path / "root" / "serve-k"
    folder.mkdir(parents=True)
    (folder / ll.LEDGER_FILE).write_bytes(b"{")
    corrupt = HeadlessRetry()
    corrupt.logins.attach(folder)
    assert await corrupt.block_reason(FakeContext([]), url) == ("login_ledger_unreadable", None)
    assert not await corrupt.cookies_cleared(FakeContext([]), url)


async def test_ledgered_key_follows_rotation_outside_retry():
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    origin, url = "https://example.com", "https://example.com/"
    one = await retry.snapshot(FakeContext([_cookie("__cf_bm", "v1", http_only=True)]), url)
    retry.commit_success(origin, [], one)
    two = await retry.snapshot(FakeContext([_cookie("__cf_bm", "v2", http_only=True)]), url)
    retry.commit_success(origin, two, two)  # rotated between visits: before == after
    assert [e["value_digest"] for e in retry.ledger[origin]] == [two[0]["value_digest"]]
    assert await retry.block_reason(FakeContext([_cookie("__cf_bm", "v3", http_only=True)]), url) is None


def test_skip_notes_cover_new_reasons_and_are_page_free():
    from browser.headless_retry import COOKIE_CHANGE_NOTES, SKIP_NOTES
    from security.public_url import CONTROL_VOCABULARY

    assert {"login_recorded", "new_server_cookie", "login_ledger_unreadable", "cookies_unreadable"} <= set(SKIP_NOTES)
    assert "cookies_present" not in SKIP_NOTES
    assert set(SKIP_NOTES) <= CONTROL_VOCABULARY["reason"]
    assert set(COOKIE_CHANGE_NOTES) == {"new_cookie", "known_value_changed"}
    assert "HttpOnly" in SKIP_NOTES["new_server_cookie"] and "HttpOnly" in COOKIE_CHANGE_NOTES["new_cookie"]


def test_login_records_never_store_values(tmp_path):
    from browser.login_records import LoginRecords

    folder = tmp_path / "root" / "serve-v"
    folder.mkdir(parents=True)
    records = LoginRecords()
    records.attach(folder)
    assert records.record("https://example.com/path?token=secret-58#frag", ll.SOURCE_LOGIN_FLOW)
    raw = (folder / ll.LEDGER_FILE).read_text()
    assert "secret-58" not in raw and "/path" not in raw
    assert not records.record("about:blank", ll.SOURCE_LOGIN_FLOW)
    with pytest.raises(ValueError):
        records.record("https://example.com", "made_up")
    assert Path(folder / ll.LEDGER_FILE).stat().st_mode & 0o777 == 0o600
