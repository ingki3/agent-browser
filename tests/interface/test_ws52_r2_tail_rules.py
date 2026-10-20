"""WS-52 R2: the settle tail runs only where nothing can overlap it.

Independent verification of R1 (B5~B8) still found overlaps: another request's
saved-recipe click ran while a navigate was in flight and its later cookie was
ledgered by that navigate's tail (B5), a download another tab requested before
the tail subscribed set a cookie the tail ledgered (B6), a renderer crash did not
discard a cookie read in flight (B7), and server close lost the tail task a tool
barrier had dropped (B8). R2 replaces case-by-case fences with simple rules:

Start only when (1) the tab is the context's only tab, (2) no request of the context
is in flight except the tab's own subresources (counted since the context was
adopted), (3) the navigate's call is the only call in flight and none overlapped it.
End for good on (4) any other tool call — a passive read (observe_page/extract/
take_screenshot/wait_for) running as the only call excepted — and (5) a new tab, a
request outside the tab's own subresources, a main-frame navigation, tab close or
crash, context close, human control/handoff, the deadline or server close. Every
stop cancels the task; the server owns every tail task until it has ended.

R3: rules (1) and (2) are gone — the tail ledgers only cookie writes the tab's own
scripts provably made (test_ws52_r3_script_writes.py), so a second tab or a request in
flight no longer has to stop it; the B6/B7 regressions below keep their final
assertion (the next visit keeps the native name). Every event now ends the tail at once
through one end function (task cancelled, observer torn down).

Synthetic loopback sites only; profiles under the per-test temporary
AGENT_BROWSER_PROFILE_ROOT (tests/interface/conftest.py).
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from interface.mcp_server import BrowserMCPServer
from test_run_cli import requires_chromium
from test_ws52_ledger_drift import (REWRITTEN, FakeContext, FakePage, Site, _cookie, assert_native_next,
                                    drift_site, fake_observer, first_retry, navigate, script, start_tail, wrote)


def _entry(retry, value):
    return [{"name": "visitor", "domain": "example.test", "path": "/", "value_digest": retry._digest(value)}]


# -- B5: another request's real saved-recipe click overlapping a navigate -----------


@requires_chromium
async def test_real_saved_recipe_overlapping_a_navigate_starts_no_tail(monkeypatch):
    import recipes.service as service

    with drift_site() as site:
        site.default_body = script("document.querySelector('button').onclick=()=>{"
                                   "document.querySelector('button').textContent='Less'}", extra="<button>More</button>")
        async with BrowserMCPServer(nav_settle=False, recipes=True) as server:
            await first_retry(server, site)
            retry = server._headless_retry
            observed = (await server.call_tool("browser_observe_page", {})).data["observation"]
            target = next(e for e in observed["elements"] if e["name"] == "More")
            clicked = await server.call_tool("browser_click", {"element_id": target["element_id"],
                                                               "epoch": observed["snapshot_epoch"]})
            assert clicked.success, clicked
            saved = await server.call_server_tool("browser_recipe", {"op": "save", "name": "SyntheticMore", "last_n": 1})
            assert saved["success"], saved
            recipe_id = saved["data"]["recipe"]["id"]
            await navigate(server, site.url + "/one")
            # From now on the button's click plants a login-like cookie 2 s later.
            site.default_body = script(
                "document.querySelector('button').onclick=()=>{document.querySelector('button').textContent='Less';"
                "setTimeout(()=>document.cookie='session_marker=synthetic-recipe-login;path=/;max-age=31536000',2000)}",
                extra="<button>More</button>")
            recipe_ready, click_now = asyncio.Event(), asyncio.Event()
            final_read, finish_nav = asyncio.Event(), asyncio.Event()
            real_run = service.run_recipe

            async def scheduled_recipe(*args, **kwargs):
                # Scheduling seam only: the unmodified saved-recipe replay engine runs.
                recipe_ready.set()
                await click_now.wait()
                return await real_run(*args, **kwargs)

            monkeypatch.setattr(service, "run_recipe", scheduled_recipe)
            other = asyncio.create_task(server.call_server_tool("browser_recipe", {"op": "run", "id": recipe_id}))
            await recipe_ready.wait()
            original = retry.snapshot

            async def held_final(context, url):
                frame = inspect.currentframe().f_back
                final = frame.f_code.co_name == "present" and getattr(frame.f_locals.get("attempt"), "succeeded", False)
                current = await original(context, url)
                if final:
                    final_read.set()
                    await finish_nav.wait()
                return current

            monkeypatch.setattr(retry, "snapshot", held_final)
            nav = asyncio.create_task(navigate(server, site.url + "/one"))
            try:
                await asyncio.wait_for(final_read.wait(), 3)
                click_now.set()
                out = await asyncio.wait_for(other, 4)
                assert out["success"] and out["data"]["recipe"]["ran"] == 1, out
                finish_nav.set()
                await asyncio.wait_for(nav, 3)
                assert "session_marker" not in [e["name"] for e in retry.ledger[site.url]], "invalid timing"
                assert retry._tail is None, "a navigate overlapped by another request started a tail"
                monkeypatch.setattr(retry, "snapshot", original)
                await asyncio.sleep(2.4)
                assert any(c["name"] == "session_marker" for c in await server._page.context.cookies())
                await assert_native_next(server, site, "new_cookie")
            finally:
                click_now.set()
                finish_nav.set()
                await asyncio.gather(nav, other, return_exceptions=True)


# -- B6: a request issued before the tail (another tab's download) ------------------


@contextmanager
def held_download_site():
    state = Site()
    state.default_body = script("")
    state.download_started = threading.Event()
    state.download_release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.requests.append((self.path, headers))
            cookie = None
            if self.path == "/robots.txt":
                body, status, kind = b"User-agent: *\nAllow: /\n", 200, "text/plain"
            elif self.path == "/native-download":
                state.download_started.set()
                state.download_release.wait(15)
                body, status, kind = b"synthetic download", 200, "application/octet-stream"
                cookie = "native=synthetic-download;Path=/;Max-Age=31536000"
            elif "HeadlessChrome" in headers.get("user-agent", ""):
                body, status, kind = b"Forbidden", 403, "text/html"
            else:
                body, status, kind = state.default_body, 200, "text/html"
                if "visitor=" not in headers.get("cookie", ""):
                    cookie = "visitor=synthetic-original;Path=/;Max-Age=31536000"
            self.send_response_only(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            if self.path == "/native-download":
                self.send_header("Content-Disposition", 'attachment; filename="synthetic.txt"')
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield state
    finally:
        state.download_release.set()
        httpd.shutdown()
        thread.join()
        httpd.server_close()


@requires_chromium
async def test_request_issued_before_the_tail_cannot_ledger_other_tab_download():
    with held_download_site() as site:
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            context = server._page.context
            other = await context.new_page()
            download = asyncio.create_task(other.goto(site.url + "/native-download"))
            try:
                assert await asyncio.to_thread(site.download_started.wait, 3)
                assert other.url == "about:blank"
                first = await navigate(server, site.url + "/one")
                assert first.data["identity"]["original_status"] == 403, first.data
                # R3: the tail may run (rule 2 removed) — a download's Set-Cookie is never observed.
                site.download_release.set()
                await asyncio.gather(download, return_exceptions=True)
                await asyncio.sleep(0.8)
                assert site.headless("/native-download")
                assert any(c["name"] == "native" for c in await context.cookies())
                await assert_native_next(server, site, "new_cookie")
            finally:
                site.download_release.set()
                await asyncio.gather(download, return_exceptions=True)


@requires_chromium
async def test_second_tab_means_no_tail_even_without_requests():
    # Rule 1 alone: an idle same-site tab (no request in flight, no new tab later) whose own
    # script writes a login-like cookie while the renamed tab would be settling.
    with drift_site() as site:
        site.default_body = script("")
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            other = await server._page.context.new_page()
            await other.goto(site.url + "/idle-other-tab")  # native name → 403, no cookie
            await asyncio.sleep(0.3)
            first = await navigate(server, site.url + "/one")
            assert first.data["identity"]["original_status"] == 403, first.data
            # R3: the tail may run (rule 1 removed) — another tab's script is another target, never observed.
            await other.evaluate("setTimeout(() => { document.cookie = "
                                 "'session_marker=other-tab-script; path=/; max-age=31536000'; }, 600)")
            await asyncio.sleep(1.4)
            assert any(c["name"] == "session_marker" for c in await server._page.context.cookies())
            await assert_native_next(server, site, "new_cookie")


# -- B7: renderer crash ---------------------------------------------------------------


@requires_chromium
async def test_actual_renderer_crash_discards_the_inflight_read(monkeypatch):
    with drift_site() as site:
        site.default_body = script("")
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            await first_retry(server, site)
            retry = server._headless_retry
            before = list(retry.ledger[site.url])
            entered, release, crashed = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original = retry._cookies
            server._page.on("crash", lambda *args: crashed.set())

            async def held(*args):
                entered.set()
                await release.wait()
                return await original(*args)

            monkeypatch.setattr(retry, "_cookies", held)
            # R3: a cookie read happens once the observer saw a write (this one would be mergeable).
            await server._page.evaluate("document.cookie = 'visitor=synthetic-post-read-change; path=/'")
            await asyncio.wait_for(entered.wait(), 2)
            session = await server._page.context.new_cdp_session(server._page)
            crash = asyncio.create_task(session.send("Page.crash"))
            try:
                await asyncio.wait_for(crashed.wait(), 3)
                assert not server._page.is_closed()  # a crash is not a close
                release.set()
                await asyncio.sleep(0.2)
                assert retry.ledger[site.url] == before, "renderer crash did not discard the pending cookie read"
                assert retry._tail is None
            finally:
                release.set()
                crash.cancel()
                await asyncio.gather(crash, return_exceptions=True)


# -- B8: server close owns every tail task --------------------------------------------


@requires_chromium
@pytest.mark.parametrize("barrier,stubborn", [(False, False), (True, False), (True, True)],
                         ids=["direct-close", "tool-then-close", "tool-then-close-first-cancel-lost"])
async def test_server_close_reaps_the_read_after_a_tool_barrier(monkeypatch, barrier, stubborn):
    with drift_site() as site:
        site.default_body = script("")
        server = BrowserMCPServer(nav_settle=False, recipes=False)
        await server.start()
        await first_retry(server, site)
        retry = server._headless_retry
        task = retry._tail.task
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = retry._cookies

        lost = {"left": 1 if stubborn else 0}

        async def held(*args):
            entered.set()
            while True:
                try:
                    await release.wait()
                    break
                except asyncio.CancelledError:
                    if lost["left"]:
                        lost["left"] -= 1  # the barrier's cancellation is lost: the task keeps running
                        continue
                    cancelled.set()
                    raise
            return await original(*args)

        monkeypatch.setattr(retry, "_cookies", held)
        await server._page.evaluate("document.cookie = 'visitor=synthetic-write; path=/'")  # R3: observed write
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if barrier:
                assert (await server.call_server_tool("browser_control_status", {}))["success"]
                assert retry._tail is None
                if stubborn:
                    await asyncio.sleep(0.1)
                    assert not task.done() and task in retry._tail_tasks  # still owned by the server
            await server.close()
            assert task.done(), "server.close lost ownership of the in-flight tail task"
            assert cancelled.is_set() or stubborn, "the cookie read was not cancelled"
            assert not retry._tail_tasks
        finally:
            task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await server.close()


async def test_every_stop_cancels_the_task_and_close_joins_it(monkeypatch):
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 0.01)
    retry = module.HeadlessRetry()
    origin = "http://example.test"
    entered = asyncio.Event()

    class Hanging(FakeContext):
        async def cookies(self):
            entered.set()
            await asyncio.sleep(30)

    page = FakePage(origin, Hanging([]))
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)
    await start_tail(retry, page, origin, origin, before, lambda: True)
    wrote(retry, "visitor", "hanging-read")
    task = retry._tail.task
    await entered.wait()
    with retry.tool_call(read_only=False):
        pass
    await asyncio.wait([task], timeout=1)
    assert task.done() and task.cancelled()  # the barrier cancels, not only marks
    await retry.aclose()
    assert not retry._tail_tasks


# -- R3: rule 1 (one tab) and rule 2 (request counter) were removed — positive identification
# and same-site attribution replace them (tests/interface/test_ws52_r3_script_writes.py).


def _fake_tab(retry, origin="http://example.test"):
    context = FakeContext([])
    page = FakePage(origin, context)
    page.main_frame = SimpleNamespace(page=page)
    fake_observer(retry, origin)
    return context, page


@pytest.mark.parametrize("emitter,event", [("page", "close"), ("page", "crash"), ("page", "framenavigated"),
                                           ("context", "close")])
async def test_tab_and_context_events_end_the_tail_for_good(monkeypatch, emitter, event):
    # Rule 5 as events: latched at once, the read in flight refused, nothing merged afterwards.
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 0.01)
    retry = module.HeadlessRetry()
    origin = "http://example.test"
    context, page = _fake_tab(retry)
    context.cookies_list = [_cookie("visitor", "before")]
    before = await retry.snapshot(context, origin)
    retry.commit_success(origin, [], before)
    with retry.tool_call(read_only=False):
        assert await retry.start_tail(page, origin, origin, lambda: True)
    tail = retry._tail
    source = page if emitter == "page" else context
    argument = page.main_frame if event == "framenavigated" else SimpleNamespace()
    for handler in list(source.listeners.get(event, [])):
        handler(argument)
    assert tail.stopped and tail.observer.closed  # R3: every event ends it at once (single end)
    tail.observer.binding_called(1, '{"late": true}')
    context.cookies_list = [_cookie("visitor", "after-event")]
    await asyncio.sleep(0.05)
    assert retry._tail is None and retry.ledger[origin] == before


# -- rule 4: only a sole passive read keeps the tail -----------------------------------


@requires_chromium
@pytest.mark.parametrize("tool", ["browser_scroll", "browser_hover", "browser_control_status"])
async def test_non_passive_calls_end_the_tail(tool):
    # scroll/hover are read-only for healing but can wake page scripts: they end the tail.
    with drift_site() as site:
        site.default_body = script("", extra="<button onmouseover=\"this.textContent='Hovered'\">More</button>")
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            await first_retry(server, site)
            if tool == "browser_control_status":
                assert (await server.call_server_tool(tool, {}))["success"]
            elif tool == "browser_scroll":
                assert (await server.call_tool(tool, {"direction": "down"})).success
            else:
                observed = (await server.call_tool("browser_observe_page", {})).data["observation"]
                assert server._headless_retry._tail is not None  # the passive read kept it
                target = next(e for e in observed["elements"] if e["name"] == "More")
                hovered = await server.call_tool(tool, {"element_id": target["element_id"],
                                                        "epoch": observed["snapshot_epoch"]})
                assert hovered.success, hovered
            assert server._headless_retry._tail is None


@requires_chromium
@pytest.mark.parametrize("tool,args", [("browser_observe_page", {}), ("browser_extract", {"selector": "body"}),
                                       ("browser_take_screenshot", {}),
                                       ("browser_wait_for", {"condition": "selector", "selector": "body", "timeout_ms": 500})])
async def test_sole_passive_read_keeps_the_tail(tool, args):
    with drift_site() as site:
        site.default_body = script("", extra="<p>text</p>")
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            await first_retry(server, site)
            tail = server._headless_retry._tail
            result = await server.call_tool(tool, args)
            assert result.success, result
            assert server._headless_retry._tail is tail and not tail.task.done()


async def test_a_call_overlapping_a_passive_read_ends_the_tail(monkeypatch):
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 5.0)
    retry = module.HeadlessRetry()
    origin = "http://example.test"
    context, page = _fake_tab(retry)
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)
    with retry.tool_call(read_only=False):
        assert await retry.start_tail(page, origin, origin, lambda: True)
    tail = retry._tail
    inside = asyncio.Event()
    leave = asyncio.Event()

    async def passive_read():
        with retry.tool_call(read_only=True):
            inside.set()
            await leave.wait()

    async def second_read():
        with retry.tool_call(read_only=True):
            pass

    first = asyncio.create_task(passive_read(), context=contextvars.Context())
    await inside.wait()
    assert retry._tail is tail  # the sole passive read keeps it
    await asyncio.create_task(second_read(), context=contextvars.Context())  # even a second read
    assert retry._tail is None and tail.stopped
    leave.set()
    await first
    await retry.aclose()


async def test_passive_read_running_when_the_navigate_ends_prevents_the_tail():
    # Rule 3 with a passive read: a read that overlapped the navigate is still an overlap.
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    origin = "http://example.test"
    context, page = _fake_tab(retry)
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)
    leave = asyncio.Event()
    inside = asyncio.Event()

    async def passive_read():
        with retry.tool_call(read_only=True):
            inside.set()
            await leave.wait()

    reader = asyncio.create_task(passive_read(), context=contextvars.Context())
    await inside.wait()
    with retry.tool_call(read_only=False):
        leave.set()
        await reader
        assert not await retry.start_tail(page, origin, origin, lambda: True)
    assert retry._tail is None


# -- the bot's order: navigate → extract → extract …, with analytics beacons ----------


@contextmanager
def beacon_site(writes_ms):
    """drift_site's rules plus analytics-like beacons: the page fetches, sendBeacon()s and loads an
    image beacon every 0.3 s (same origin, answered 204) and rewrites the ledgered cookie at
    ``writes_ms`` after loading — what GA-style scripts do around their cookie writes."""
    from test_ws52_ledger_drift import VISITOR

    state = Site()
    writes = "".join(f"setTimeout(() => {{document.cookie = 'visitor={REWRITTEN}-{ms}; path=/; "
                     f"max-age=31536000';}}, {ms});" for ms in writes_ms)
    state.default_body = script(
        "setInterval(() => { fetch('/beacon?f=' + Date.now()); navigator.sendBeacon('/beacon');"
        " new Image().src = '/beacon?i=' + Date.now(); }, 300);" + writes, extra="<p>article</p>")

    class Handler(BaseHTTPRequestHandler):
        def _beacon(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.requests.append((self.path, headers))
            length = int(headers.get("content-length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response_only(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            self._beacon()

        def do_GET(self):
            if self.path.startswith("/beacon"):
                self._beacon()
                return
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.requests.append((self.path, headers))
            cookie = None
            if self.path == "/robots.txt":
                data, status, kind = b"User-agent: *\nAllow: /\n", 200, "text/plain"
            elif "HeadlessChrome" in headers.get("user-agent", ""):
                data, status, kind = b"Forbidden", 403, "text/html"
            else:
                data, status, kind = state.default_body, 200, "text/html"
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


async def _bot_order(server, site, until_s):
    """navigate (default settle) → extract at once → extract every second → next visit."""
    first = await navigate(server, site.url + "/one")
    assert first.data["identity"]["original_status"] == 403, first.data
    started = time.monotonic()
    extracted = await server.call_tool("browser_extract", {"selector": "body"})
    assert extracted.success, extracted
    while time.monotonic() - started < until_s:
        await asyncio.sleep(1.0)
        assert (await server.call_tool("browser_extract", {"selector": "body"})).success
    return await navigate(server, site.url + "/two")


@requires_chromium
@pytest.mark.parametrize("delay", [0.4, 3.0, 9.0, 11.0])
async def test_bot_order_with_beacons_keeps_late_script_cookies(delay):
    with beacon_site([int(delay * 1000)]) as site:
        async with BrowserMCPServer(recipes=False) as server:  # nav_settle on, as the bot runs
            second = await _bot_order(server, site, delay + 0.7)
            assert sum(p.startswith("/beacon") for p, _ in site.requests) >= 3  # the beacons really ran
            # WS-58 (intended change): outside the 10 s maximum the rewrite is not merged, but a
            # non-HttpOnly value change no longer keeps the native name (r1 symptom test checks the ledger).
            assert second.data["last_http_status"] == 200, second.data.get("retry_skipped")


@requires_chromium
async def test_bot_order_with_beacons_and_three_rewrites():
    with beacon_site([400, 3000, 9000]) as site:
        async with BrowserMCPServer(recipes=False) as server:
            second = await _bot_order(server, site, 9.7)
            assert second.data["last_http_status"] == 200, second.data.get("retry_skipped")
