"""WS-52 R1: the settle tail's security boundaries without sampling blind spots.

Independent verification (B1~B4) found the 0.5 s sampling tail could still ledger
cookies it must not: a server tool did not end it (B1), a cookie read finishing
after the 10 s maximum was merged (B2), a related tab that lived between two
samples went unseen (B3), and a concurrent tool call slipped past end_tail's
final sample (B4). R1: a synchronous start barrier at every tool entry (generation
bump + closed mark before any await), a post-await generation/closed/deadline
re-check, no final merge on end, a bounded cookie read, and related-tab visits
latched as events.

Synthetic loopback sites only; profiles under the per-test temporary
AGENT_BROWSER_PROFILE_ROOT (tests/interface/conftest.py).
"""
from __future__ import annotations

import asyncio
import contextvars
import time

import pytest

from interface.mcp_server import BrowserMCPServer, create_server
from test_run_cli import requires_chromium
from test_ws52_ledger_drift import (FakeContext, FakePage, _cookie, assert_native_next, drift_site,
                                    first_retry, script, start_tail)


def _entry(retry, value):
    return [{"name": "visitor", "domain": "example.test", "path": "/", "value_digest": retry._digest(value)}]


async def _sdk_call(sdk, name, arguments):
    """One tools/call through the actual MCP SDK session (same path as stdio)."""
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as group:
            async def serve():
                await sdk.run(server_streams[0], server_streams[1], sdk.create_initialization_options())

            group.start_soon(serve)
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
            group.cancel_scope.cancel()
    return result


# -- the original symptom keeps its boundaries ------------------------------------


@requires_chromium
@pytest.mark.parametrize("delay", [0.4, 3.0, 9.0, 11.0])
async def test_original_symptom_boundaries(delay):
    # A script rewrites the ledgered visitor value `delay` seconds after the return.
    from test_ws52_ledger_drift import REWRITTEN, navigate

    with drift_site() as site:
        site.default_body = script(f"setTimeout(() => {{document.cookie = 'visitor={REWRITTEN}; path=/; "
                                   f"max-age=31536000';}}, {int(delay * 1000)})")
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            first = await navigate(server, site.url + "/one")
            assert first.data["last_http_status"] == 200
            await asyncio.sleep(delay + 0.7)
            second = await navigate(server, site.url + "/two")
            if delay < 10:
                assert second.data["last_http_status"] == 200, second.data.get("retry_skipped")
            else:  # outside the 10 s maximum: honestly refused
                assert second.data["last_http_status"] == 403
                assert second.data["retry_skipped"]["cookie_change"] == "known_value_changed"


# -- B1: server tools (and the SDK entry) end the tail ---------------------------


@requires_chromium
async def test_actual_server_tool_stops_tail():
    with drift_site() as site:
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            await first_retry(server, site)
            result = await server.call_server_tool("browser_control_status", {})
            assert result["success"]
            assert server._headless_retry._tail is None, "actual MCP server-tool entry bypasses the barrier"


@requires_chromium
async def test_actual_server_tool_must_not_ledger_later_writes():
    # The default page rewrites a ledgered value and adds _ga 400 ms after the return.
    with drift_site() as site:
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            await first_retry(server, site)
            assert (await server.call_server_tool("browser_control_status", {}))["success"]
            await asyncio.sleep(1.1)
            await assert_native_next(server, site, "new_cookie")


@requires_chromium
async def test_sdk_tool_entry_ends_tail_before_any_branch():
    # Through the real SDK call path (mcp_server branch → server tool), not the backend method.
    with drift_site() as site:
        sdk, backend = create_server(nav_settle=False, recipes=False)
        async with backend:
            await first_retry(backend, site)
            result = await _sdk_call(sdk, "browser_control_status", {})
            assert result.content and '"success":true' in result.content[0].text
            assert backend._headless_retry._tail is None
            await asyncio.sleep(1.1)
            await assert_native_next(backend, site, "new_cookie")


# -- B2: the 10 s maximum holds after every await ---------------------------------


async def test_real_ten_second_maximum_is_enforced_after_await():
    from browser import headless_retry as module

    assert module.TAIL_S == 10.0 and module.TAIL_INTERVAL_S == 0.5  # the shipped constants
    retry = module.HeadlessRetry()
    origin = "http://example.test"
    started = asyncio.Event()

    class Slow(FakeContext):
        async def cookies(self):
            started.set()
            await asyncio.sleep(10.1)
            return [_cookie("visitor", "synthetic-write-after-ten-seconds")]

    page = FakePage(origin, Slow([]))
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)
    start_tail(retry, page, origin, origin, before, lambda: True)
    task = retry._tail.task
    begun = time.monotonic()
    await started.wait()
    await task
    assert retry.ledger[origin] == before, "a cookie read outlasting the 10-second maximum was merged"
    assert time.monotonic() - begun < module.TAIL_S + 0.5  # the read itself is bounded by the deadline


async def test_snapshot_completing_after_deadline_must_not_merge(monkeypatch):
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_S", 0.08)
    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 0.02)
    retry = module.HeadlessRetry()
    origin = "http://example.test"
    entered = asyncio.Event()
    release = asyncio.Event()

    class Slow(FakeContext):
        async def cookies(self):
            entered.set()
            await release.wait()
            return [_cookie("visitor", "after-window")]

    page = FakePage(origin, Slow([]))
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)
    start_tail(retry, page, origin, origin, before, lambda: True)
    task = retry._tail.task
    await entered.wait()
    await asyncio.sleep(0.1)
    release.set()
    await task
    assert retry.ledger[origin] == before


async def test_read_that_overruns_the_deadline_without_yielding_is_discarded(monkeypatch):
    # A read the timeout cannot interrupt (the loop is busy until it returns): its result
    # arrives after the deadline, so only the post-await deadline re-check can refuse it.
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_S", 0.08)
    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 0.02)
    retry = module.HeadlessRetry()
    origin = "http://example.test"

    class Blocking(FakeContext):
        async def cookies(self):
            time.sleep(0.15)  # past the 0.08 s maximum, without giving the loop a turn
            return [_cookie("visitor", "after-window")]

    page = FakePage(origin, Blocking([]))
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)
    start_tail(retry, page, origin, origin, before, lambda: True)
    await retry._tail.task
    assert retry.ledger[origin] == before


# -- B3: a related tab shorter than the sample interval ---------------------------


@requires_chromium
async def test_short_related_tab_visit_does_not_get_ledgered():
    with drift_site() as site:
        site.default_body = script("")
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            await first_retry(server, site)
            started = time.monotonic()
            other = await server._page.context.new_page()
            await other.goto(site.url + "/native-short-tab")
            await other.close()
            elapsed = time.monotonic() - started
            assert elapsed < 0.4, "invalid trial: the related tab spanned the normal 0.5 s sample interval"
            assert site.headless("/native-short-tab")
            await asyncio.sleep(0.65)
            await assert_native_next(server, site, "new_cookie")


@requires_chromium
async def test_short_related_popup_from_the_page_does_not_get_ledgered():
    # A popup the renamed page itself opens (window.open) and closes again at once.
    with drift_site() as site:
        site.default_body = script("")
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            await first_retry(server, site)
            async with server._page.context.expect_page() as info:
                await server._page.evaluate("window.open('/native-popup-short')")
            popup = await info.value
            await popup.wait_for_load_state("load")
            await popup.close()
            assert site.headless("/native-popup-short")
            await asyncio.sleep(0.65)
            await assert_native_next(server, site, "new_cookie")


async def test_tail_does_not_start_when_event_subscription_fails():
    from browser.headless_retry import HeadlessRetry

    class NoEvents(FakeContext):
        def on(self, event, handler):
            raise RuntimeError("context events unavailable")

    retry = HeadlessRetry()
    origin = "http://example.test"
    page = FakePage(origin, NoEvents([]))
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)
    start_tail(retry, page, origin, origin, before, lambda: True)
    assert retry._tail is None
    assert all(not handlers for handlers in page.listeners.values())


# -- B4: concurrent tool calls never meet an in-flight merge ----------------------


@requires_chromium
@pytest.mark.parametrize("first_tool", ["browser_tab_control", "browser_control_status"])
async def test_two_concurrent_calls_do_not_merge_action_cookie(monkeypatch, first_tool):
    with drift_site() as site:
        site.default_body = script(
            "document.querySelector('button').onclick=()=>{document.querySelector('button').textContent='Less';"
            "document.cookie='visitor=after-action; path=/; max-age=31536000';"
            "document.cookie='session_marker=after-action; path=/; max-age=31536000';}",
            extra="<button>More</button>")
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            await first_retry(server, site)
            retry = server._headless_retry
            entered = asyncio.Event()
            release = asyncio.Event()
            original = retry.snapshot

            async def held(context, url):
                # Every cookie read from now on (the background sample, or any final sample a
                # tool call might take) is held until the second call's click has run.
                entered.set()
                await release.wait()
                return await original(context, url)

            monkeypatch.setattr(retry, "snapshot", held)
            await asyncio.wait_for(entered.wait(), 2)  # a tail sample is in flight
            if first_tool == "browser_tab_control":
                first = asyncio.create_task(server.call_tool(first_tool, {"command": "list"}))
            else:
                first = asyncio.create_task(server.call_server_tool(first_tool, {}))
            await asyncio.sleep(0)
            try:
                clicked = await asyncio.wait_for(server.call_tool("browser_click", {"selector": "button"}), 5)
                assert clicked.success, clicked
            finally:
                release.set()
                await first
            await asyncio.sleep(0.1)
            monkeypatch.setattr(retry, "snapshot", original)
            cookies = {c["name"]: c["value"] for c in await server._page.context.cookies()}
            assert cookies["visitor"] == "after-action" and cookies["session_marker"] == "after-action"
            await assert_native_next(server, site, "new_cookie")


async def test_in_flight_read_whose_cancel_is_lost_is_refused_by_generation(monkeypatch):
    # asyncio.wait_for (3.11) can return a finished read instead of the cancellation; the
    # barrier's generation/closed mark is what refuses the result after the await.
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 0.01)
    retry = module.HeadlessRetry()
    origin = "http://example.test"
    entered = asyncio.Event()
    release = asyncio.Event()

    class Stubborn(FakeContext):
        async def cookies(self):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue  # the cancellation is lost
            return [_cookie("visitor", "after-tool-call"), _cookie("session_marker", "after-tool-call")]

    page = FakePage(origin, Stubborn([]))
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)
    start_tail(retry, page, origin, origin, before, lambda: True)
    task = retry._tail.task
    await entered.wait()
    # R2 cancels the task at the barrier; model the cancellation being lost entirely (the handle
    # is not reachable) so only the post-await generation/closed re-check can refuse the read.
    retry._tail.task = None
    try:
        with retry.tool_call(read_only=False):  # a click starts while the read is in flight
            release.set()
            await asyncio.wait_for(task, 2)
    finally:
        release.set()  # never leave a task that ignores cancellation behind
        await asyncio.wait([task], timeout=2)
    assert retry.ledger[origin] == before


def test_barrier_is_synchronous_and_only_a_sole_passive_read_keeps_the_tail():
    # R2: every entry bumps the generation before its body runs, except a passive read that is
    # the only call in flight (and its own nested wrapper). Nested calls bump too (R1 exempted them).
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    gen = retry._tool_gen
    with retry.tool_call(read_only=True):
        assert retry._tool_gen == gen
        with retry.tool_call(read_only=True):
            assert retry._tool_gen == gen  # the SDK wrapper → backend entry of the same read
    with retry.tool_call(read_only=False):
        assert retry._tool_gen == gen + 1  # bumped before the body (any await) runs
        with retry.tool_call(read_only=False):
            assert retry._tool_gen == gen + 2
    with retry.tool_call(read_only=False):
        with retry.tool_call(read_only=True):
            assert retry._tool_gen == gen + 4  # a read nested in a changing call is not passive


async def test_end_tail_cancels_without_a_final_merge(monkeypatch):
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_INTERVAL_S", 5.0)  # no background sample before the end
    retry = module.HeadlessRetry()
    origin = "http://example.test"
    context = FakeContext([_cookie("visitor", "changed-just-before-the-end")])
    page = FakePage(origin, context)
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)
    start_tail(retry, page, origin, origin, before, lambda: True)
    task = retry._tail.task
    await retry.end_tail()
    await asyncio.sleep(0)
    assert task.done() and retry._tail is None
    assert retry.ledger[origin] == before  # fail-closed: the last change is not merged


async def test_tool_call_during_the_navigate_prevents_the_tail():
    # R2 rule 3: a navigate that another call overlapped (started before it or during it,
    # even if that call already ended) starts no tail; the sole call does.
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    origin = "http://example.test"
    context = FakeContext([])
    retry.track_context(context)  # counted from adoption, before the tab opened
    page = FakePage(origin, context)
    before = _entry(retry, "before")
    retry.commit_success(origin, [], before)

    async def other_call():
        with retry.tool_call(read_only=True):  # even a passive read overlaps
            await asyncio.sleep(0)

    with retry.tool_call(read_only=False):
        # Another MCP request (its own task, outside this call — as the SDK dispatches it),
        # started and ended while the navigate ran.
        await asyncio.create_task(other_call(), context=contextvars.Context())
        retry.start_tail(page, origin, origin, before, lambda: True)
    assert retry._tail is None
    with retry.tool_call(read_only=False):
        retry.start_tail(page, origin, origin, before, lambda: True)
    assert retry._tail is not None
    retry.cancel_tail()
