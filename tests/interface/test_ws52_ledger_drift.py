"""WS-52: page scripts that rewrite cookies after a renamed navigate returns.

The ledger (WS-47) only knew the cookies at the end of the renamed navigate;
GA4/Funding Choices style scripts rewrite them a moment later, so the next visit
saw "cookies present" forever. A background settle tail now keeps ledgering the
same tab's script writes for a few seconds, stopping (without merging) on any
non-read-only tool call, human control/handoff, a later main-frame document or
closed tab, a related other tab, feature/headless off, server close or unreadable
cookies.

Synthetic loopback sites only; profiles under the per-test temporary
AGENT_BROWSER_PROFILE_ROOT (tests/interface/conftest.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from interface.mcp_server import BrowserMCPServer
from test_run_cli import requires_chromium

VISITOR = "visitor-WS52-original-4d1e"
REWRITTEN = "rewritten-by-script-9b2c"
#: WS-58: cookies_present → new_server_cookie (only an HttpOnly key the ledger does not hold as a server cookie).
NOTE = "도구가 모르는 서버 전용(HttpOnly) 쿠키가 있어(로그인 세션 가능성) 이름을 바꾸지 않았습니다"
CHANGE_NOTES = {
    "new_cookie": "도구가 남기지 않은 새 서버 전용(HttpOnly) 쿠키 키가 있어(로그인 가능성) 이름을 바꾸지 않았습니다",
    "known_value_changed": ("도구가 사이트 스크립트 쿠키로 남긴 키가 서버 전용(HttpOnly)으로 바뀌어(로그인 가능성) "
                            "이름을 바꾸지 않았습니다 — 로그인 없이 읽어야 하면 운영자에게 이 사이트 쿠키 정리를 요청하세요"),
}


def script(js: str, extra: str = "") -> bytes:
    return f"<!doctype html><title>Read</title>{extra}<script>{js}</script>".encode()


#: Like GA4/Funding Choices: rewrite a known cookie and add one, after navigate returned.
REWRITE = script(f"setTimeout(() => {{document.cookie = 'visitor={REWRITTEN}; path=/; max-age=31536000';"
                 "document.cookie = '_ga=GA1.1.555; path=/; max-age=31536000';}, 400)")
#: Value-only drift — the real bot state (names within the ledger, values differ).
REWRITE_VALUE_ONLY = script(f"setTimeout(() => {{document.cookie = 'visitor={REWRITTEN}; path=/; max-age=31536000';}}, 400)")


@dataclass
class Site:
    url: str = ""
    bodies: dict = field(default_factory=dict)
    default_body: bytes = REWRITE
    requests: list = field(default_factory=list)

    def received(self, path):
        return [h for p, h in self.requests if p == path]

    def headless(self, path, index=0):
        return "HeadlessChrome" in self.received(path)[index]["user-agent"]


@contextmanager
def drift_site():
    state = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.requests.append((self.path, headers))
            cookie = None
            if self.path == "/robots.txt":
                data, status, kind = b"User-agent: *\nAllow: /\n", 200, "text/plain"
            elif "HeadlessChrome" in headers.get("user-agent", ""):
                data, status, kind = b"Forbidden", 403, "text/html"
                if self.path.startswith("/native"):
                    cookie = "native=planted-on-native-visit; Path=/; Max-Age=31536000"
            else:
                data, status, kind = state.bodies.get(self.path, state.default_body), 200, "text/html"
                if "visitor=" not in headers.get("cookie", ""):
                    cookie = f"visitor={VISITOR}; Path=/; Max-Age=31536000"
            self.send_response_only(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
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
    with drift_site() as state:
        yield state


async def navigate(server, url):
    return await server.call_tool("browser_navigate", {"url": url})


async def names(server):
    return sorted(c["name"] for c in await server._page.context.cookies())


def present(change):
    return {"retried": False, "reason": "new_server_cookie", "note": NOTE,
            "cookie_change": change, "cookie_change_note": CHANGE_NOTES[change]}


async def first_retry(server, site, path="/one"):
    first = await navigate(server, site.url + path)
    assert first.data["identity"]["original_status"] == 403, first.data
    assert server._headless_retry._tail is not None  # the settle tail is running
    return first


async def assert_native_next(server, site, change, path="/next"):
    """WS-58 (intended change): the WS-52 invariant is checked on the ledger (the change was not
    merged — strict WS-52 comparison); the next visit follows WS-58 rule (b)."""
    from ws58_helpers import assert_unmerged_next

    await assert_unmerged_next(server, site, change, navigate, path)
    assert len(site.received(path)) == 1


async def assert_renamed_next(server, site, path="/next"):
    result = await navigate(server, site.url + path)
    assert result.data["last_http_status"] == 200, result.data.get("retry_skipped")
    assert result.data["identity"]["remembered"] is True
    assert "retry_skipped" not in result.data
    assert len(site.received(path)) == 1 and not site.headless(path)


# -- 1. acceptance: script writes after the return are ours --------------------


@requires_chromium
@pytest.mark.parametrize("body", [REWRITE, REWRITE_VALUE_ONLY], ids=["rewrite_and_new", "value_only"])
async def test_script_rewrite_after_return_keeps_retry_memory(site, body):
    site.default_body = body
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await first_retry(server, site)
        await asyncio.sleep(1.5)  # the agent is reading; the script runs
        cookies = {c["name"]: c["value"] for c in await server._page.context.cookies()}
        assert cookies["visitor"] == REWRITTEN
        await assert_renamed_next(server, site, "/two")
        # The remembered visit's own tail keeps the memory alive on the next page too.
        await asyncio.sleep(1.5)
        await assert_renamed_next(server, site, "/three")


@requires_chromium
async def test_tail_ledger_persists_across_restart(site, _isolated_profile_root):
    from browser.serve_profile import profile_dir

    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws52-p") as server:
        await first_retry(server, site)
        await asyncio.sleep(1.5)
    raw = (profile_dir("ws52-p", root=_isolated_profile_root) / "headless_retry.json").read_bytes()
    assert REWRITTEN.encode() not in raw and VISITOR.encode() not in raw
    assert sorted(c["name"] for c in json.loads(raw)["origins"][site.url]["cookies"]) == ["_ga", "visitor"]
    async with BrowserMCPServer(nav_settle=False, recipes=False, profile="ws52-p") as server:
        await assert_renamed_next(server, site, "/after-restart")


@requires_chromium
async def test_tail_never_delays_the_navigate_response(site):
    from browser.headless_retry import TAIL_S

    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        started = time.monotonic()
        await first_retry(server, site)
        assert time.monotonic() - started < TAIL_S / 2
        task = server._headless_retry._tail.task
        assert not task.done()  # still settling in the background after the response
        started = time.monotonic()
        observed = await server.call_tool("browser_observe_page", {})
        assert observed.success and time.monotonic() - started < TAIL_S / 2
        assert not task.done()  # a read-only call does not end it


# -- 2. invariant 1: changes we must not merge ------------------------------------


@requires_chromium
@pytest.mark.parametrize("handoff", ["take_release", "request", "login_job"])
async def test_human_control_during_tail_is_never_merged(site, handoff):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await first_retry(server, site)
        hub = server.hub
        if handoff == "take_release":
            hub._apply({"op": "take"})
            assert hub.holder == "human"
        elif handoff == "request":
            hub.request("로그인 필요")
        else:
            hub.login = SimpleNamespace(status="pending")
        await asyncio.sleep(1.5)  # meanwhile the cookies change (script / human login)
        if handoff == "take_release":
            hub._apply({"op": "release"})
        elif handoff == "request":
            hub.requested = False
        else:
            hub.login = None
        await asyncio.sleep(0.6)
        assert server._headless_retry._tail is None
        await assert_native_next(server, site, "new_cookie")


@requires_chromium
async def test_agent_action_ends_tail_before_it_runs(site):
    # A click could log in: what happens after it is not ledgered.
    site.default_body = script(
        "document.getElementById('more').onclick = (e) => { e.target.textContent = 'Less'; setTimeout(() => {"
        "document.cookie = 'session=logged-in-after-click; path=/; max-age=31536000';}, 300); }",
        extra="<button id='more'>More</button>")
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await first_retry(server, site)
        clicked = await server.call_tool("browser_click", {"selector": "#more"})
        assert clicked.success, clicked.error_message
        assert server._headless_retry._tail is None
        await asyncio.sleep(1.0)
        assert "session" in await names(server)
        await assert_native_next(server, site, "new_cookie")


@requires_chromium
async def test_later_main_frame_document_is_a_native_visit(site):
    # The renamed page itself moves on (script navigation): /native is requested with the
    # native name and plants a cookie there.
    site.default_body = script("setTimeout(() => { location.href = '/native-next'; }, 200)")
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await first_retry(server, site)
        await asyncio.sleep(1.5)
        assert site.headless("/native-next") and "native" in await names(server)
        await assert_native_next(server, site, "new_cookie")


@requires_chromium
async def test_related_other_tab_visit_is_not_merged(site):
    # A popup/other tab on the same site is a native-name visit.
    site.default_body = script("")
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await first_retry(server, site)
        other = await server._page.context.new_page()
        await other.goto(site.url + "/native-popup")
        assert "native" in await names(server)
        await asyncio.sleep(1.2)
        await other.close()
        await asyncio.sleep(0.6)
        await assert_native_next(server, site, "new_cookie")


@requires_chromium
async def test_closed_tab_ends_tail_without_merge(site):
    site.default_body = script("")
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await first_retry(server, site)
        page = server._page
        context = page.context
        await context.new_page()  # keep the browser alive (unrelated blank tab)
        await page.close()
        await context.add_cookies([{"name": "session", "value": "set-elsewhere", "url": site.url}])
        await asyncio.sleep(1.2)
        assert server._headless_retry._tail is None
        assert [e["name"] for e in server._headless_retry.ledger[site.url]] == ["visitor"]


@requires_chromium
async def test_feature_off_during_tail_is_not_merged(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await first_retry(server, site)
        server.headless_retry = False
        await asyncio.sleep(1.5)
        server.headless_retry = True
        await assert_native_next(server, site, "new_cookie")


@requires_chromium
async def test_server_close_cancels_tail(site, caplog):
    caplog.set_level(logging.DEBUG)
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await first_retry(server, site)
        retry = server._headless_retry
        task = retry._tail.task
    assert retry._tail is None
    await asyncio.sleep(0)
    assert task.done()
    assert [e["name"] for e in retry.ledger[site.url]] == ["visitor"]  # nothing merged after close
    assert REWRITTEN not in caplog.text and VISITOR not in caplog.text


# -- 3. skip detail (signal only; never names or values) --------------------------


@requires_chromium
@pytest.mark.parametrize("change", ["new_cookie", "known_value_changed"])
async def test_cookie_change_detail_without_names_or_values(site, change, caplog, monkeypatch):
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_S", 0.3)
    caplog.set_level(logging.DEBUG)
    site.default_body = script("")
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await first_retry(server, site)
        await asyncio.sleep(1.0)  # the tail ran out
        assert server._headless_retry._tail is None
        # WS-58: the detail rides on new_server_cookie — an HttpOnly key that is new, or a script
        # (js) key that turned HttpOnly.
        if change == "new_cookie":
            cookie = {"name": "session-ws52", "value": "secret-ws52-login", "url": site.url, "httpOnly": True}
        else:
            ledger = server._headless_retry.ledger[site.url]
            server._headless_retry.ledger[site.url] = [{**e, "js": True} for e in ledger]
            cookie = {"name": "visitor", "value": "secret-ws52-login", "url": site.url, "httpOnly": True}
        await server._page.context.add_cookies([cookie])
        result = await navigate(server, site.url + "/after")
        assert result.data["retry_skipped"] == present(change)
        dumped = json.dumps(result.data, ensure_ascii=False)
    for secret in ("secret-ws52-login", "session-ws52", VISITOR, "visitor="):
        assert secret not in dumped and secret not in caplog.text


# -- 4. unit: unreadable cookies, concurrent saves --------------------------------


class FakePage:
    def __init__(self, url, context):
        self.url = url
        self.context = context
        self.main_frame = object()
        self.listeners = {}
        context.pages.append(self)

    def is_closed(self):
        return False

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.listeners[event].remove(handler)


class FakeContext:
    def __init__(self, cookies):
        self.cookies_list = cookies
        self.fail = False
        self.pages = []
        self.listeners = {}

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.listeners[event].remove(handler)

    async def cookies(self):
        if self.fail:
            raise RuntimeError("closed")
        return list(self.cookies_list)


FAKE_NONCE = "fake-observer-nonce"


def fake_observer(retry, origin):
    """Fakes have no CDP: install an observer whose records the test feeds (``wrote``)."""
    from browser.headless_retry import _Observer

    async def install(tail):
        observer = _Observer(retry=retry, origin=origin, nonce=FAKE_NONCE, binding="fake",
                             on_break=lambda: retry._end(tail))
        observer.contexts[1] = origin
        tail.observer = observer
        tail.ready.set()
        return observer

    retry._install_observer = install


async def start_tail(retry, page, url, origin, last=None, guard=lambda: True):
    """Start a tail the way a sole navigate call does (one call in flight), with a fake observer."""
    fake_observer(retry, origin)
    with retry.tool_call(read_only=False):
        return await retry.start_tail(page, url, origin, guard)


def wrote(retry, name, value, old=None):
    """The fake page's script wrote name=value — what the R3 observer would report."""
    record = {"k": FAKE_NONCE, "a": {"0": {"n": name, "v": value}},
              "r": {} if old is None else {"0": {"n": name, "v": old}}}
    retry._tail.observer.binding_called(1, json.dumps(record))


def _cookie(name, value, domain="example.test"):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": -1,
            "secure": False, "httpOnly": False, "sameSite": "Lax"}


async def test_unreadable_cookies_end_the_tail_fail_closed(tmp_path, monkeypatch):
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 0.01)
    retry = module.HeadlessRetry()
    origin = "http://example.test"
    context = FakeContext([_cookie("visitor", "v1")])
    page = FakePage(origin + "/", context)
    after = await retry.snapshot(context, origin + "/")
    retry.commit_success(origin, [], after)
    context.fail = True
    await start_tail(retry, page, origin + "/", origin, after, lambda: True)
    wrote(retry, "visitor", "v2")  # R3: a cookie read happens once something was observed
    await asyncio.sleep(0.05)
    assert retry._tail is None and page.listeners == {"framenavigated": [], "close": [], "crash": []}
    assert context.listeners == {"close": []}
    context.fail = False
    context.cookies_list = [_cookie("visitor", "v2")]
    await asyncio.sleep(0.05)
    await retry.end_tail()
    assert retry.ledger[origin] == after  # nothing merged after the read failure
    # WS-58 (intended change): the unmerged non-HttpOnly value no longer blocks — the decision
    # still fails closed when cookies cannot be read.
    assert await retry.cookies_cleared(context, origin + "/")
    context.fail = True
    assert not await retry.cookies_cleared(context, origin + "/")


async def test_tail_saves_and_other_commits_never_lose_a_write(tmp_path, monkeypatch):
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 0.005)
    path = tmp_path / "headless_retry.json"
    retry = module.HeadlessRetry()
    retry.load(path)
    origin = "http://example.test"
    context = FakeContext([_cookie("visitor", "v0")])
    page = FakePage(origin + "/", context)
    after = await retry.snapshot(context, origin + "/")
    retry.commit_success(origin, [], after)
    await start_tail(retry, page, origin + "/", origin, after, lambda: True)
    for index in range(1, 30):
        context.cookies_list = [_cookie("visitor", f"v{index}")]
        wrote(retry, "visitor", f"v{index}", f"v{index - 1}")
        retry.commit_success(f"http://other{index}.test", [], [])
        await asyncio.sleep(0.006)
    await asyncio.sleep(0.05)  # R1: end_tail no longer takes a final sample — let the tail read the last value
    await retry.end_tail()
    again = module.HeadlessRetry()
    again.load(path)
    assert again.states == retry.states and again.ledger == retry.ledger
    assert all(f"http://other{index}.test" in again.states for index in range(1, 30))
    assert await again.cookies_cleared(context, origin + "/")  # the last value was merged
    assert [p.name for p in tmp_path.iterdir()] == ["headless_retry.json"]  # no temp leftovers


def test_related_host_is_over_inclusive():
    from browser.headless_retry import _related_host

    assert _related_host("news.hada.io", "hada.io")
    assert _related_host("news.hada.io", "blog.hada.io")
    assert _related_host("news.hada.io", "news.hada.io")
    assert not _related_host("news.hada.io", "example.com")
    assert not _related_host("news.hada.io", "")


async def test_change_racing_the_snapshot_with_human_control_is_not_merged(monkeypatch):
    # The human takes control while the cookie read is in flight: re-checked after the await.
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 0.01)
    retry = module.HeadlessRetry()
    origin = "http://example.test"
    open_ = {"ok": True}

    class RacingContext(FakeContext):
        async def cookies(self):
            open_["ok"] = False  # handoff lands during the read
            return [_cookie("visitor", "changed-during-handoff")]

    context = RacingContext([])
    page = FakePage(origin + "/", context)
    after = [{"name": "visitor", "domain": "example.test", "path": "/", "value_digest": retry._digest("v1")}]
    retry.commit_success(origin, [], after)
    await start_tail(retry, page, origin + "/", origin, after, lambda: open_["ok"])
    wrote(retry, "visitor", "changed-during-handoff")
    await asyncio.sleep(0.05)
    assert retry._tail is None
    assert retry.ledger[origin] == after
