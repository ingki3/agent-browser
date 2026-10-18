"""One disclosed headless-name attempt, remembering success per origin.

Fetch.continueRequest changes only this paused request's headers, unlike a
target-wide UA override or Playwright route headers inherited by redirects.
Client Hint lists remove only the additional headless brand and use Chromium's
native version-seeded ordering; Client Hint versions, GREASE and platform stay
native. The UA uses headed Chromium's native major-only version reduction.

WS-47: cookies a successful retry caused are ledgered (keyed fingerprints only)
so a site's visitor cookie does not disable the memory; with serve --profile
the memory persists in the profile folder (headless_retry.json, 0600).

WS-52 R3: for up to TAIL_S after a success, a disclosed observer in that tab
identifies the cookie writes the tab's own scripts made (native getter
difference, same-site execution context, non-HttpOnly, latest value, no
Set-Cookie seen); only those are ledgered, marked ``js`` — and a ``js`` key may
later change its value while it stays non-HttpOnly (D).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import hmac
from itertools import permutations
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import AsyncIterator, Callable, Iterator, Literal
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page

from actions.pdf_document import cookie_matches
from security.egress import EgressGuard
from security.egress_proxy import EgressProxy
from security.robots_signal import RobotsSignals, _origin

logger = logging.getLogger(__name__)

LEDGER_FILE = "headless_retry.json"
MAX_ORIGINS = 1000
MAX_COOKIES = 50
MAX_FILE_BYTES = 8 * 1024 * 1024
#: A final failure is retried again after a day (sites change; a stale failure costs one request).
FAILED_TTL_S = 24 * 3600
#: WS-52 settle tail: after a committed success, cookie writes the tab's own scripts
#: provably made keep being ledgered this long (unless a stop condition fires first).
TAIL_S = 10.0
TAIL_INTERVAL_S = 0.5
#: WS-52 server.close waits this long for cancelled tail tasks (they own no merge after cancel).
TAIL_REAP_S = 2.0
#: WS-52 R3 cookie-write observer limits (per tail): more records or a larger payload stop merging.
OBSERVER_MAX_RECORDS = 256
OBSERVER_MAX_PAYLOAD = 4096
#: WS-52 R3: installing the observer / each teardown step may take at most this long.
OBSERVER_SETUP_S = 2.0
OBSERVER_TEARDOWN_S = 1.0


@dataclass(eq=False)
class _Call:
    """One MCP tool call in flight (WS-52 R2); nested calls of the same request share it."""

    passive: bool
    #: Another call was in flight at some point during this one: its navigate starts no tail.
    overlapped: bool = False


#: WS-52 R2: the top-level tool call this task runs inside (nested calls see their owner's).
_CURRENT_CALL: ContextVar["_Call | None"] = ContextVar("headless_retry_current_call", default=None)

SUCCESS_NOTE = "이 사이트가 헤드리스 브라우저 이름을 막아, 같은 버전 일반 Chrome 이름으로 다시 열었습니다(위장 범위: 이름·Client Hints 만)"
REMEMBERED_NOTE = "이 사이트는 앞서 헤드리스 이름을 막아, 처음부터 일반 Chrome 이름으로 열었습니다"
FAILED_NOTE = "일반 Chrome 이름으로도 막혔습니다 — 사이트가 자동화 접근을 막고 있습니다"
#: data.retry_skipped.note — fixed text per reason code, never page-derived.
SKIP_NOTES = {
    "cookies_present": "이 사이트에 로그인 등 기존 쿠키가 있어 이름을 바꾸지 않았습니다",
    "already_failed": "앞서 일반 Chrome 이름으로도 막혀 다시 시도하지 않았습니다",
    "robots_not_confirmed": "robots.txt 가 일반 접근 허용을 확정해 주지 않아 이름을 바꾸지 않았습니다",
    "vendor_challenge": "보안 업체의 확인·차단 화면이라 이름을 바꿔 다시 시도하지 않았습니다",
    "disabled": "헤드리스 이름 재시도가 꺼져 있어(--no-headless-retry) 다시 시도하지 않았습니다",
    "headed": "창 모드 브라우저라 헤드리스 이름 문제가 아니어서 다시 시도하지 않았습니다",
}
IDENTITY_INSTRUCTIONS = (
    "data.identity 가 있으면 사이트가 헤드리스 이름을 막아 도구가 이름만 바꿔 열었다는 뜻 — "
    "사용자에게 설명할 때 note 를 그대로 쓸 것(일시적 차단 아님)\n"
    "data.retry_skipped 가 있으면(retried:false) 이름을 바꾸지 않은 이유 — note 를 그대로 설명할 것(일시적 차단이라 하지 말 것)"
)


#: data.retry_skipped.cookie_change(_note) — WS-52 detail for cookies_present (fixed text; no names/values).
COOKIE_CHANGE_NOTES = {
    "new_cookie": "도구가 남기지 않은 새 쿠키가 있어(로그인 가능성) 이름을 바꾸지 않았습니다",
    "known_value_changed": ("도구가 앞서 남긴 쿠키의 값이 바뀌어(사이트 스크립트 또는 로그인) 로그인 여부를 확인할 수 없어 "
                            "이름을 바꾸지 않았습니다 — 로그인 없이 읽어야 하면 운영자에게 이 사이트 쿠키 정리를 요청하세요"),
}


def skip_reason(code: str, cookie_change: str | None = None) -> dict:
    out = {"retried": False, "reason": code, "note": SKIP_NOTES[code]}
    if cookie_change is not None:
        out["cookie_change"] = cookie_change
        out["cookie_change_note"] = COOKIE_CHANGE_NOTES[cookie_change]
    return out


def _related_host(host: str, other: str) -> bool:
    """Could the two hosts share cookies? Over-inclusive on purpose (ends the tail sooner)."""
    if not host or not other:
        return False
    if host == other or host.endswith("." + other) or other.endswith("." + host):
        return True
    return host.split(".")[-2:] == other.split(".")[-2:] and "." in host


def headed_brand_list(value: str, major: int) -> str:
    """Undo the native shuffle, remove HeadlessChrome, reshuffle the smaller list.

    Chromium components/embedder_support/user_agent_utils.cc:
    GetRandomOrder/ShuffleBrandList/GenerateBrandVersionList use lexicographic
    permutations seeded by the actual major version (2..4 native brands).
    No brands or version/platform values are invented. Unknown formats fail
    closed instead of guessing. Also applies to Sec-CH-UA-Full-Version-List.
    """
    brands = value.split(", ")
    if not 2 <= len(brands) <= 4 or any(not re.fullmatch(r'"[^"\\]*";v="[0-9.]+"', b) for b in brands):
        raise ValueError("unsupported native Client Hint list")
    if not any(b.startswith('"HeadlessChrome";') for b in brands):
        return value
    if len(brands) < 3:
        raise ValueError("incomplete native Client Hint list")
    orders = list(permutations(range(len(brands))))
    native = [brands[index] for index in orders[major % len(orders)]]
    if not native[-1].startswith('"HeadlessChrome";') or not native[1].startswith('"Chromium";'):
        raise ValueError("unexpected native Client Hint order")
    native.pop()
    orders = list(permutations(range(len(native))))
    result = [""] * len(native)
    for index, destination in enumerate(orders[major % len(orders)]):
        result[destination] = native[index]
    return ", ".join(result)


async def origin_cookies(context: BrowserContext, url: str) -> list[dict]:
    """Cookies sent to any path on the url's origin (domain/secure/expiry checked).

    Cookies scoped to another path still indicate a potentially logged-in origin.
    Raises when cookies are unavailable; callers fail closed.
    """
    parts = urlsplit(url)
    found = []
    for cookie in await context.cookies():
        destination = urlunsplit((parts.scheme, parts.netloc, cookie.get("path") or "/", "", ""))
        if cookie_matches(cookie, destination):
            found.append(cookie)
    return found


#: {"name", "domain", "path", "value_digest"} — never the value itself. WS-52 R3: an entry the
#: settle tail identified as a script write also carries ``"js": true`` (D: its value may change).
LedgerEntry = dict
_ENTRY_KEYS = ("name", "domain", "path", "value_digest")


def _valid_entry(entry: object) -> bool:
    return (isinstance(entry, dict)
            and set(entry) in ({*_ENTRY_KEYS}, {*_ENTRY_KEYS, "js"}) and entry.get("js", True) is True
            and all(isinstance(entry[k], str) for k in _ENTRY_KEYS)
            and re.fullmatch(r"[0-9a-f]{64}", entry["value_digest"]) is not None)


def _key(entry: LedgerEntry) -> tuple[str, str, str]:
    return (entry["name"], entry["domain"], entry["path"])


def _fp(entry: LedgerEntry) -> tuple[str, str, str, str]:
    return (entry["name"], entry["domain"], entry["path"], entry["value_digest"])


class HeadlessRetry:
    """Per-origin retry memory plus the ledger of cookies our successful retries caused.

    A cookie the site planted during a successful renamed navigate (a visitor
    id) must not later count as "logged in": the origin is eligible when every
    cookie it sends is exactly (name, domain, path, keyed value digest) one we
    recorded (WS-52 R3: or a ``js`` key, any value, while not HttpOnly). A new
    cookie or a changed value restores the native name. With
    ``load(path)`` (serve --profile) the memory persists in a 0600 file; the
    value digest is HMAC-SHA256 keyed by a random per-profile key — no value or
    plain hash is ever stored.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        # Server lifetime, independent of tab/context replacement (on-demand).
        self.states: dict[str, Literal["succeeded", "failed"]] = {}
        self.ledger: dict[str, list[LedgerEntry]] = {}
        self.updated: dict[str, float] = {}
        self.path: Path | None = None
        self._key = secrets.token_bytes(32)
        self._clock = clock
        self._tail: _Tail | None = None
        #: WS-52 R1: bumped synchronously when a tool call starts (``tool_call``) — refuses in-flight reads.
        self._tool_gen = 0
        #: WS-52 R2: top-level MCP tool calls in flight (the tail starts only for the sole one).
        self._calls: set[_Call] = set()
        #: WS-52 R2: every tail task not yet finished — server close cancels and joins them all.
        self._tail_tasks: set[asyncio.Task] = set()
        #: WS-52 R3: observer teardowns in flight (a tool waits for them before it acts — ``settled``).
        self._teardowns: set[asyncio.Task] = set()

    @property
    def attempted(self) -> set[str]:
        """Origins reserved or attempted, including remembered successes."""
        return set(self.states)

    # -- state ---------------------------------------------------------------

    def state(self, origin: str | None) -> Literal["succeeded", "failed"] | None:
        """Current state; a final failure is forgotten after FAILED_TTL_S."""
        if origin is None:
            return None
        current = self.states.get(origin)
        if current == "failed" and self._clock() - self.updated.get(origin, 0.0) > FAILED_TTL_S:
            self.states.pop(origin, None)
            return None
        return current

    def reserve(self, origin: str) -> None:
        """In-memory reservation before any await (never persisted on its own)."""
        self.states[origin] = "failed"
        self.updated[origin] = self._clock()

    def release(self, origin: str) -> None:
        self.states.pop(origin, None)

    def mark_failed(self, origin: str) -> None:
        if not origin:
            return
        self.states[origin] = "failed"
        self.updated[origin] = self._clock()
        self._bound()
        self.save()

    def commit_success(self, origin: str, before: list[LedgerEntry] | None,
                       after: list[LedgerEntry] | None) -> None:
        """Record success; ledger the cookies that appeared/changed during the navigate."""
        if not origin:
            return
        self.states[origin] = "succeeded"
        self.updated[origin] = self._clock()
        if before is not None and after is not None:
            self._merge(origin, before, after)
        self._bound()
        self.save()

    def _merge(self, origin: str, before: list[LedgerEntry], after: list[LedgerEntry]) -> bool:
        """Ledger what appeared/changed between two snapshots; True when the ledger changed.

        A cookie already present in ``before`` but never ledgered stays excluded. WS-52 R3 (D):
        a ``js`` key keeps its mark and follows its current value (the value change was
        allowed for that key anyway); every other entry is matched exactly, as before.
        """
        current = self.ledger.get(origin, [])
        prior = {_fp(e) for e in before}
        latest = {_key(e): e for e in after}
        after_fps = {_fp(e) for e in after}
        kept = []
        for entry in current:
            if _fp(entry) in after_fps:
                kept.append(entry)
            elif entry.get("js") and _key(entry) in latest:
                kept.append({**latest[_key(entry)], "js": True})
        known = {_fp(e) for e in kept}
        created = [e for e in after if _fp(e) not in prior and _fp(e) not in known
                   and _key(e) not in {_key(k) for k in kept}]
        merged = (kept + created)[-MAX_COOKIES:]  # overflow forgets oldest: fewer retries
        if merged == current:
            return False
        self.ledger[origin] = merged
        return True

    # -- WS-52 settle tail --------------------------------------------------

    @contextmanager
    def tool_call(self, read_only: bool) -> Iterator[None]:
        """WS-52 start barrier: enter it at every tool entry, before any await.

        R2 — simple, conservative rules instead of telling overlaps apart one by one:
        every entry marks every other call in flight as overlapped (a navigate that
        overlapped another call starts no tail) and, unless it is a passive read
        (``read_only``: the caller passes it only for observe_page/extract/
        take_screenshot/wait_for) running as the only call, synchronously bumps the
        generation and ends the running tail for good — task cancelled, observer torn
        down, a cookie read in flight refused after its await (``_tail_current``).
        Nested calls (SDK wrapper → backend, a navigate's first retry, recipe steps)
        share their request's call; they end a running tail too unless they are the
        passive call's own wrapper.
        """
        current = _CURRENT_CALL.get()
        if current is not None and current not in self._calls:
            current = None  # a task that outlived its call: count it as a request of its own
        others = [call for call in self._calls if call is not current]
        for call in others:
            call.overlapped = True
        if not (read_only and not others and (current is None or current.passive)):
            self._tool_gen += 1
            self._close_tail()
        if current is not None:
            yield
            return
        call = _Call(passive=read_only, overlapped=bool(others))
        self._calls.add(call)
        token = _CURRENT_CALL.set(call)
        try:
            yield
        finally:
            _CURRENT_CALL.reset(token)
            self._calls.discard(call)

    def _close_tail(self) -> None:
        """End the running tail for good (synchronous; see ``_end``)."""
        tail = self._tail
        if tail is not None:
            self._end(tail)

    def _end(self, tail: _Tail) -> None:
        """The single end of a tail, for every reason (R3 §5) — synchronous, idempotent.

        Closed mark, observer closed (a late ``bindingCalled`` is dropped), listeners off,
        task cancelled, and one owned teardown task that removes the init script and the
        binding and detaches the CDP session (``_teardown``). No final sample, nothing merged.
        """
        if self._tail is tail:
            self._tail = None
        if tail.stopped:
            return
        tail.stopped = True
        if tail.observer is not None:
            tail.observer.closed = True
        _detach(tail)
        if tail.task is not None and tail.task is not _current_task():
            tail.task.cancel()
        try:
            teardown = asyncio.get_running_loop().create_task(self._teardown(tail))
        except RuntimeError:
            return  # no loop (sync unit use): nothing was installed
        self._teardowns.add(teardown)
        teardown.add_done_callback(self._teardowns.discard)

    async def _teardown(self, tail: _Tail) -> None:
        """Remove what the observer installed: init script, binding, CDP session (R3 §5).

        Waits for an install still in flight first, so nothing it adds afterwards survives.
        A wrapper already in the current document stays for that document's lifetime
        (README limit); later documents of the tab get none.
        """
        try:
            await asyncio.wait_for(tail.ready.wait(), OBSERVER_SETUP_S)
        except Exception:  # noqa: BLE001 - a stuck install: detach anyway
            pass
        observer = tail.observer
        session = observer.session if observer is not None else None
        if session is None:
            return
        steps = []
        if observer.script is not None:
            steps.append(("Page.removeScriptToEvaluateOnNewDocument", {"identifier": observer.script}))
        if observer.binding_added:
            steps.append(("Runtime.removeBinding", {"name": observer.binding}))
        for method, params in steps:
            try:
                await asyncio.wait_for(session.send(method, params), OBSERVER_TEARDOWN_S)
            except Exception:  # noqa: BLE001 - a closed/crashed tab already dropped them
                pass
        try:
            await asyncio.wait_for(session.detach(), OBSERVER_TEARDOWN_S)
        except Exception:  # noqa: BLE001 - already detached/closed
            pass

    async def settled(self) -> None:
        """Wait (bounded) for pending observer teardowns — called before a tool acts, so a
        document the tool opens never gets the observer's init script."""
        pending = [task for task in self._teardowns if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=OBSERVER_SETUP_S + 3 * OBSERVER_TEARDOWN_S)

    async def start_tail(self, page: Page, url: str, origin: str, guard: Callable[[], bool]) -> bool:
        """Keep ledgering this tab's own script cookie writes for TAIL_S after a committed success.

        Runs in the background (the tool response is not delayed beyond installing the
        observer). Starts only when the calling tool call is the only call in flight and no
        other call overlapped it (R2 rule 3); True when the observer is installed.

        R3: a disclosed observer (``_OBSERVER_JS``) wraps ``document.cookie`` and
        ``CookieStore.set/delete`` in this tab's same-site documents and reports, per write,
        the difference the native getter shows before/after. Only cookies identified by
        ``_identified`` are ledgered (marked ``js``). Server Set-Cookie never produces an
        observation on any path, so no request classification is needed.

        Ends for good — nothing merged, a read in flight refused, observer torn down — on any
        other tool call (a sole passive read excepted, ``tool_call``), a main-frame
        navigation or origin change, the tab closing or crashing, the context closing,
        ``guard()`` turning false (human control/handoff/login/request, headed, feature
        off), an observer overflow or malformed record, the deadline or server close.
        Every condition is re-checked after each cookie read's await.
        """
        self._close_tail()
        call = _CURRENT_CALL.get()
        if call is None or call.overlapped or self._calls != {call}:
            return False  # rule 3: another call overlapped this navigate (or no call owns it)
        tail = _Tail(page=page, url=url, origin=origin, guard=guard,
                     deadline=time.monotonic() + TAIL_S, gen=self._tool_gen)

        def ended(*_: object) -> None:
            self._end(tail)

        def navigated(frame: object) -> None:
            # Any later main-frame document (same origin included) is not the observed one.
            if frame is page.main_frame:
                self._end(tail)

        try:
            context = page.context
            _listen(tail, page, "framenavigated", navigated)
            _listen(tail, page, "close", ended)
            _listen(tail, page, "crash", ended)
            _listen(tail, context, "close", ended)
        except Exception:  # noqa: BLE001 - unwatched means not started (fail-closed; WS-47 behavior)
            _detach(tail)
            return False
        self._tail = tail  # a tool call during the install closes it (and tears the install down)
        try:
            observer = await asyncio.wait_for(self._install_observer(tail), OBSERVER_SETUP_S)
        except BaseException as exc:  # noqa: BLE001 - not installed: never started
            self._end(tail)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return False
        if observer is None or not self._tail_current(tail) or not self._tail_open(tail):
            self._end(tail)
            return False
        task = asyncio.get_running_loop().create_task(self._run_tail(tail))
        tail.task = task
        self._tail_tasks.add(task)
        task.add_done_callback(self._tail_tasks.discard)
        return True

    async def _install_observer(self, tail: _Tail) -> "_Observer | None":
        """CDP session on the tab + binding + init script (sub-frames, later documents) +
        the same wrapper evaluated in the current same-site documents (R3 §3)."""
        observer = _Observer(retry=self, origin=tail.origin, nonce=secrets.token_hex(16),
                             binding=_binding_name(), on_break=lambda: self._end(tail))
        tail.observer = observer
        try:
            session = await tail.page.context.new_cdp_session(tail.page)
            observer.session = session
            if tail.stopped:
                return None

            def called(event: dict) -> None:
                if event.get("name") == observer.binding:
                    observer.binding_called(event.get("executionContextId"), event.get("payload"))

            session.on("Runtime.executionContextCreated", observer.context_created)
            session.on("Runtime.executionContextDestroyed", observer.context_destroyed)
            session.on("Runtime.executionContextsCleared", observer.contexts_cleared)
            session.on("Runtime.bindingCalled", called)
            session.on("Network.responseReceivedExtraInfo",
                       lambda event: observer.response_headers(event.get("headers") or {}))
            session.on("Page.frameNavigated", observer.frame_navigated)
            session.on("Page.frameStartedLoading", observer.frame_started_loading)
            frame = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]
            observer.main_frame, observer.loader = str(frame["id"]), str(frame.get("loaderId") or "")
            await session.send("Page.enable")
            await session.send("Network.enable")
            await session.send("Runtime.enable")
            await session.send("Runtime.addBinding", {"name": observer.binding})
            observer.binding_added = True
            source = _observer_source(observer.binding, observer.nonce)
            added = await session.send("Page.addScriptToEvaluateOnNewDocument", {"source": source})
            observer.script = str(added["identifier"])
            for context_id, context_origin in list(observer.contexts.items()):
                if _same_site(context_origin, tail.origin):
                    try:
                        await session.send("Runtime.evaluate", {"expression": source, "contextId": context_id})
                    except Exception:  # noqa: BLE001 - a frame that went away: nothing to wrap
                        pass
            if tail.stopped or observer.broken:
                return None
            return observer
        except Exception:  # noqa: BLE001 - not installed: no tail (fail-closed)
            return None
        finally:
            tail.ready.set()

    def cancel_tail(self) -> None:
        """Stop the tail without a final sample (cancelled; nothing merged)."""
        self._close_tail()

    async def end_tail(self) -> None:
        """Stop the tail: cancel only — no final sample (fail-closed).

        A script change in the last moment before the end is lost; the next visit
        then honestly reports ``known_value_changed``/``new_cookie``.
        """
        self.cancel_tail()
        await self.settled()

    async def aclose(self) -> None:
        """Server close: end the tail, cancel + join every tail task and teardown still owned."""
        self._close_tail()
        tasks = [task for task in self._tail_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=TAIL_REAP_S)
        await self.settled()

    async def _run_tail(self, tail: _Tail) -> None:
        try:
            while not tail.stopped:
                await asyncio.sleep(TAIL_INTERVAL_S)
                if tail.stopped or time.monotonic() > tail.deadline or not await self._tail_sample(tail):
                    break
        except Exception:  # noqa: BLE001 - closed tab/context: end silently, nothing half-merged
            pass
        finally:
            self._end(tail)

    def _tail_current(self, tail: _Tail) -> bool:
        """No tool call started since the tail began, it was not closed, the deadline holds."""
        return (not tail.stopped and tail.gen == self._tool_gen and self._tail is tail
                and time.monotonic() <= tail.deadline)

    def _tail_open(self, tail: _Tail) -> bool:
        page = tail.page
        if (tail.observer is None or tail.observer.broken or tail.observer.closed
                or self.states.get(tail.origin) != "succeeded" or not tail.guard() or page.is_closed()):
            return False
        return _origin(page.url) == tail.origin

    async def _tail_sample(self, tail: _Tail) -> bool:
        """One merge step; False ends the tail (conditions broke or cookies unreadable)."""
        if not self._tail_current(tail) or not self._tail_open(tail):
            return False
        if not tail.observer.latest:
            return True  # nothing observed yet: no cookie read needed
        remaining = tail.deadline - time.monotonic()
        try:
            current = await asyncio.wait_for(self._cookies(tail.page.context, tail.url), remaining)
        except asyncio.TimeoutError:
            return False  # the read outlasted the maximum window
        # Re-check after the await (nothing awaits between here and the merge): a tool call,
        # human control, navigation, crash or the deadline that raced the read wins.
        if current is None or not self._tail_current(tail) or not self._tail_open(tail):
            return False
        identified = self._identified(tail.origin, tail.observer, current)
        if identified:
            self._absorb_identified(tail.origin, identified)
        return True

    def _identified(self, origin: str, observer: "_Observer",
                    current: list[tuple[LedgerEntry, bool]]) -> list[LedgerEntry]:
        """R3 §1 — the jar cookies a script of this tab provably wrote (rules 1~5; rule 6 and
        the same-site attribution of rule 2 are enforced when a record arrives)."""
        known = {_fp(entry) for entry in self.ledger.get(origin, [])}
        counts: dict[tuple[str, str], int] = {}
        for entry, _ in current:
            pair = (entry["name"], entry["value_digest"])
            counts[pair] = counts.get(pair, 0) + 1
        found = []
        for entry, http_only in current:
            pair = (entry["name"], entry["value_digest"])
            if (_fp(entry) in known
                    or http_only  # rule 3: a refused/blind write over a server HttpOnly cookie
                    or observer.latest.get(entry["name"]) != entry["value_digest"]  # rules 1 + 4
                    or counts[pair] != 1  # rule 3: ambiguous (another domain/path holds the same pair)
                    or pair in observer.server):  # rule 5: a Set-Cookie seen during the tail
                continue
            found.append(entry)
        return found

    def _absorb_identified(self, origin: str, entries: list[LedgerEntry]) -> None:
        """Ledger identified script cookies with the ``js`` mark (replacing that key's entry)."""
        if self.states.get(origin) != "succeeded":
            return
        current = list(self.ledger.get(origin, []))
        changed = False
        for entry in entries:
            marked = {**{k: entry[k] for k in _ENTRY_KEYS}, "js": True}
            if marked in current:
                continue
            current = [e for e in current if _key(e) != _key(marked)] + [marked]
            changed = True
        if not changed:
            return
        self.ledger[origin] = current[-MAX_COOKIES:]
        self.updated[origin] = self._clock()
        self._bound()
        self.save()

    # -- cookies -------------------------------------------------------------

    def _digest(self, value: str) -> str:
        return hmac.new(self._key, value.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()

    async def _cookies(self, context: BrowserContext, url: str) -> list[tuple[LedgerEntry, bool]] | None:
        """(fingerprint, httpOnly) of the origin's cookies, or None when cookies are unavailable."""
        try:
            cookies = await origin_cookies(context, url)
        except Exception:  # noqa: BLE001 - unavailable cookies never authorize a changed name
            return None
        return [({"name": str(c.get("name", "")), "domain": str(c.get("domain", "")),
                  "path": str(c.get("path") or "/"), "value_digest": self._digest(str(c.get("value", "")))},
                 c.get("httpOnly") is not False)
                for c in cookies]

    async def snapshot(self, context: BrowserContext, url: str) -> list[LedgerEntry] | None:
        """Fingerprints of the origin's cookies, or None when cookies are unavailable."""
        current = await self._cookies(context, url)
        return None if current is None else [entry for entry, _ in current]

    @staticmethod
    def _accepted(ledger: list[LedgerEntry], entry: LedgerEntry, http_only: bool) -> bool:
        """Exact ledger match — or (WS-52 R3 D) a ``js`` key that is still not HttpOnly."""
        for known in ledger:
            if _key(known) == _key(entry):
                if known.get("js"):
                    return not http_only
                return _fp(known) == _fp(entry)
        return False

    async def cookies_cleared(self, context: BrowserContext, url: str) -> bool:
        """No cookie for the origin, or every one is accepted by the ledger (``_accepted``)."""
        current = await self._cookies(context, url)
        if current is None:
            return False
        ledger = self.ledger.get(_origin(url) or "", [])
        return all(self._accepted(ledger, entry, http_only) for entry, http_only in current)

    async def cookie_change(self, context: BrowserContext, url: str) -> str | None:
        """Why cookies are not cleared: "new_cookie" (an unledgered name/domain/path — maybe a
        login) wins over "known_value_changed" (only ledgered cookies with other values, or a
        ``js`` key that turned HttpOnly). None when cleared or unreadable. Never returns names
        or values."""
        current = await self._cookies(context, url)
        if current is None:
            return None
        ledger = self.ledger.get(_origin(url) or "", [])
        keys = {_key(e) for e in ledger}
        changed = [e for e, http_only in current if not self._accepted(ledger, e, http_only)]
        if not changed:
            return None
        if any(_key(e) not in keys for e in changed):
            return "new_cookie"
        return "known_value_changed"

    # -- persistence ---------------------------------------------------------

    def load(self, path: Path) -> None:
        """Adopt the profile file; any unreadable/corrupt file means 'no memory'."""
        self.path = Path(path)
        try:
            # O_NONBLOCK: a FIFO in the ledger's place would otherwise block open() waiting for a writer
            # (and with it the server). Only a regular file is read; anything else is a corrupt ledger.
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(self.path, flags)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("ledger is not a regular file")
                os.set_blocking(fd, True)
                raw = b""
                while chunk := os.read(fd, 1 << 20):
                    raw += chunk
                    if len(raw) > MAX_FILE_BYTES:
                        raise ValueError("ledger file too large")
            finally:
                os.close(fd)
            data = json.loads(raw)
            key, states, ledger, updated = self._parse(data)
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 - corrupt memory restarts from the first-retry conditions
            logger.warning("헤드리스 재시도 기억 파일을 읽지 못해 빈 기억으로 시작합니다")
            return
        self._key, self.states, self.ledger, self.updated = key, states, ledger, updated
        for origin in list(self.states):
            self.state(origin)  # drop expired failures
        self._bound()

    @staticmethod
    def _parse(data: object) -> tuple[bytes, dict, dict, dict]:
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("unsupported ledger")
        key_hex, origins = data.get("key"), data.get("origins")
        if not isinstance(key_hex, str) or re.fullmatch(r"[0-9a-f]{64}", key_hex) is None:
            raise ValueError("bad key")
        if not isinstance(origins, dict):
            raise ValueError("bad origins")
        states: dict = {}
        ledger: dict = {}
        updated: dict = {}
        for origin, entry in origins.items():
            if not (isinstance(origin, str) and _origin(origin) == origin and isinstance(entry, dict)):
                raise ValueError("bad origin")
            state, when, cookies = entry.get("state"), entry.get("updated"), entry.get("cookies")
            if (state not in ("succeeded", "failed", None) or isinstance(when, bool)
                    or not isinstance(when, (int, float)) or not math.isfinite(when)
                    or not isinstance(cookies, list)
                    or not all(_valid_entry(c) for c in cookies)):
                raise ValueError("bad entry")
            if state is not None:
                states[origin] = state
            if cookies:
                ledger[origin] = cookies[-MAX_COOKIES:]
            updated[origin] = float(when)
        return bytes.fromhex(key_hex), states, ledger, updated

    def _bound(self) -> None:
        origins = set(self.states) | set(self.ledger)
        for origin in list(self.updated):
            if origin not in origins:
                self.updated.pop(origin)
        if len(origins) > MAX_ORIGINS:
            oldest = sorted(origins, key=lambda o: self.updated.get(o, 0.0))[:len(origins) - MAX_ORIGINS]
            for origin in oldest:  # forgetting only removes memory: next time re-check from scratch
                self.states.pop(origin, None)
                self.ledger.pop(origin, None)
                self.updated.pop(origin, None)

    def _entry(self, origin: str) -> dict:
        return {"state": self.states.get(origin), "updated": self.updated.get(origin, 0.0),
                "cookies": self.ledger.get(origin, [])}

    def _entry_json(self, origin: str) -> bytes:
        return json.dumps({origin: self._entry(origin)}, separators=(",", ":")).encode()

    def _payload(self) -> bytes:
        origins = {origin: self._entry(origin) for origin in
                   sorted(set(self.states) | set(self.ledger), key=lambda o: self.updated.get(o, 0.0))}
        return json.dumps({"version": 1, "key": self._key.hex(), "origins": origins},
                          separators=(",", ":")).encode()

    def save(self) -> bool:
        """Atomic 0600 write (temp + fsync + rename). Without a profile: memory only."""
        if self.path is None:
            return False
        payload = self._payload()
        while len(payload) > MAX_FILE_BYTES:
            # Never write what load() would reject: forget the oldest origins
            # (memory too, so memory and file agree) until the payload fits.
            excess = len(payload) - MAX_FILE_BYTES
            for origin in sorted(set(self.states) | set(self.ledger), key=lambda o: self.updated.get(o, 0.0)):
                excess -= len(self._entry_json(origin))
                self.states.pop(origin, None)
                self.ledger.pop(origin, None)
                self.updated.pop(origin, None)
                if excess <= 0:
                    break
            payload = self._payload()
        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            return True
        except OSError as exc:
            # Origins/values never logged; memory still applies for this server.
            logger.warning("헤드리스 재시도 기억 파일 쓰기 실패(%s) — 메모리에는 남음", type(exc).__name__)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    @asynccontextmanager
    async def present(self, page: Page, url: str, original: str, robots: RobotsSignals,
                      guard: EgressGuard, proxy: EgressProxy | None,
                      tail_guard: Callable[[], bool] | None = None) -> AsyncIterator[RetryRequest]:
        """Present the native headed Chromium name and reduced UA during this retry.

        Every request/redirect is separately scoped; all existing egress routes
        and the pinned proxy remain installed. Cookie changes fail closed (a
        cookie created during this navigate is not ledgered yet, so the rest of
        the navigate keeps the native name), and robots-disallowed subpaths keep
        the native name. Detach restores the native request path even when
        navigation is cancelled or fails. When the caller marks the attempt
        succeeded, the cookies that appeared or changed are ledgered on exit;
        with ``tail_guard`` (WS-52) a background settle tail then keeps ledgering
        the cookie writes this tab's own scripts provably made (see ``start_tail``).
        """
        destination = _origin(url)
        version = re.search(r"HeadlessChrome/([0-9]+)\.", original)
        if version is None:
            raise ValueError("unavailable native headless version")
        major = int(version[1])
        before = await self.snapshot(page.context, url)
        session = await page.context.new_cdp_session(page)
        attempt = RetryRequest(origin=destination)

        async def paused(event: dict) -> None:
            request = event["request"]
            target = request["url"]
            params: dict = {"requestId": event["requestId"]}
            try:
                headers = request["headers"]
                ua_key = next((key for key in headers if key.lower() == "user-agent"), None)
                if (_origin(target) == destination and ua_key is not None and headers[ua_key] == original
                        and await self.cookies_cleared(page.context, target)
                        and await robots.allowed(page.context, target, guard, proxy) is True):
                    updated = dict(headers)
                    # Headed Chromium's native UA reduction keeps only the major.
                    updated[ua_key] = re.sub(r"HeadlessChrome/[0-9.]+", f"Chrome/{major}.0.0.0", original)
                    for key in updated:
                        if key.lower() in {"sec-ch-ua", "sec-ch-ua-full-version-list"}:
                            updated[key] = headed_brand_list(updated[key], major)
                    params["headers"] = [{"name": key, "value": value} for key, value in updated.items()]
                await session.send("Fetch.continueRequest", params)
                if ("headers" in params and event.get("resourceType") == "Document"
                        and event.get("frameId") == main_frame):
                    attempt.document_requested = True
            except Exception:  # noqa: BLE001 - preserve native headers if inspection fails
                try:
                    await session.send("Fetch.continueRequest", {"requestId": event["requestId"]})
                except Exception:  # noqa: BLE001 - request/session may already have closed
                    pass

        session.on("Fetch.requestPaused", paused)
        try:
            main_frame = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
            # 인증 가로채기 옵션은 켜지 않는다(CDP 기본값): 이 세션은 어떤 인증 요청에도 답하지 않으며
            # 프록시 자격은 어느 경로에도 주지 않는다(egress 프록시는 무자격, WS-47 R3).
            await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            yield attempt
        finally:
            try:
                await session.send("Fetch.disable")
            except Exception:  # noqa: BLE001 - a closed/crashed tab must preserve its action result
                pass
            try:
                await session.detach()
            except Exception:  # noqa: BLE001 - already detached/closed
                pass
            if attempt.succeeded and destination is not None:
                after = await self.snapshot(page.context, url)
                self.commit_success(destination, before, after)
                if (tail_guard is not None and after is not None and tail_guard()
                        and await self.start_tail(page, url, destination, tail_guard)):
                    # R3 §3: the observation is disclosed in data.identity (never hidden).
                    attempt.observing = True
                    if attempt.identity is not None:
                        attempt.identity["cookie_write_observer"] = True


@dataclass
class RetryRequest:
    """Whether a renamed main-document request actually left interception."""

    document_requested: bool = False
    origin: str | None = None
    #: Set by the caller once the final result is a same-origin 2xx (ledger commit on exit).
    succeeded: bool = False
    #: WS-52 R3: the cookie-write observer was installed for this navigate's settle tail.
    observing: bool = False
    #: WS-52 R3: the navigate result's data.identity when it was attached before the scope
    #: exits (remembered success) — gets ``cookie_write_observer`` once the observer runs.
    identity: dict | None = None


@dataclass
class _Tail:
    """One running WS-52 settle tail (one per server: a new success replaces it)."""

    page: Page
    url: str
    origin: str
    guard: Callable[[], bool]
    deadline: float
    stopped: bool = False
    #: The tool generation at the start (HeadlessRetry._tool_gen); any later tool call refuses merges.
    gen: int = 0
    task: "asyncio.Task | None" = field(default=None, repr=False)
    #: (emitter, event, handler) — removed together when the tail ends.
    listeners: list = field(default_factory=list, repr=False)
    observer: "_Observer | None" = field(default=None, repr=False)
    #: Set when the observer install finished (installed or not) — teardown waits for it.
    ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass(eq=False)
class _Observer:
    """WS-52 R3 — what one tail's observer saw (values only as keyed digests).

    The page can call the binding itself (it may grab it before the wrapper hides it, review
    F2) and patch any intrinsic the wrapper did not capture (F1): a record counts only with
    the per-tail nonce the wrapper keeps in a closure, from a default-world execution
    context of this tab whose CDP origin is same-site with the retried origin. Records carry
    the native getter's difference, never the assigned string.
    """

    retry: "HeadlessRetry"
    origin: str
    nonce: str
    binding: str = ""
    on_break: Callable[[], None] | None = field(default=None, repr=False)
    session: object = field(default=None, repr=False)
    binding_added: bool = False
    script: str | None = None
    main_frame: str = ""
    loader: str = ""
    #: default-world execution context id → its CDP origin (this tab's target only).
    contexts: dict = field(default_factory=dict)
    #: cookie name → digest of its latest observed value; None = deleted or ambiguous.
    latest: dict = field(default_factory=dict)
    #: (name, value digest) of every Set-Cookie header seen during the tail (rule 5).
    server: set = field(default_factory=set)
    records: int = 0
    #: Something could not be trusted (overflow, malformed record, new main-frame document).
    broken: bool = False
    #: The tail ended: late events are dropped.
    closed: bool = False

    def _break(self) -> None:
        if not self.broken:
            self.broken = True
            if self.on_break is not None:
                self.on_break()

    def context_created(self, event: dict) -> None:
        context = event.get("context") or {}
        aux = context.get("auxData") or {}
        if aux.get("isDefault") is True and isinstance(context.get("id"), int):
            self.contexts[context["id"]] = str(context.get("origin") or "")

    def context_destroyed(self, event: dict) -> None:
        self.contexts.pop(event.get("executionContextId"), None)

    def contexts_cleared(self, _event: object = None) -> None:
        self.contexts.clear()

    def frame_navigated(self, event: dict) -> None:
        frame = event.get("frame") or {}
        if not frame.get("parentId") and str(frame.get("loaderId") or "") != self.loader:
            self._break()  # rule 6: a new main-frame document is not the observed one

    def frame_started_loading(self, event: dict) -> None:
        if event.get("frameId") == self.main_frame:
            self._break()  # end early: the init script is removed before the next document commits

    def response_headers(self, headers: dict) -> None:
        if self.closed:
            return
        for key, value in headers.items():
            if str(key).lower() != "set-cookie":
                continue
            for line in str(value).split("\n"):
                first = line.split(";", 1)[0]
                name, value_ = first.split("=", 1) if "=" in first else ("", first)
                self.server.add((name.strip(), self.retry._digest(value_.strip())))

    def binding_called(self, context_id: object, payload: object) -> None:
        if self.closed or self.broken:
            return
        origin = self.contexts.get(context_id)
        if origin is None or not _same_site(origin, self.origin):
            return  # rule 2: another site's frame (or a context of no document of this tab)
        if not isinstance(payload, str):
            return
        if len(payload.encode("utf-8", "surrogatepass")) > OBSERVER_MAX_PAYLOAD:
            self._break()
            return
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if not isinstance(data, dict) or not isinstance(data.get("k"), str) or not hmac.compare_digest(
                data["k"], self.nonce):
            return  # not our wrapper: a forged call is ignored
        self.records += 1
        if self.records > OBSERVER_MAX_RECORDS:
            self._break()
            return
        try:
            added = _pairs(data.get("a"))
            removed = _pairs(data.get("r"))
        except ValueError:
            self._break()
            return
        names = {name for name, _ in added + removed}
        if len(names) > 1:
            # One write sets one cookie: a difference over several names (a concurrent change)
            # cannot be attributed — none of them is mergeable until a clean observation.
            for name in names:
                self.latest[name] = None
            return
        for name, _ in removed:
            self.latest[name] = None
        for name, value in added:
            self.latest[name] = self.retry._digest(value)


def _pairs(raw: object) -> list[tuple[str, str]]:
    if not isinstance(raw, dict):
        raise ValueError("bad record")
    out = []
    for item in raw.values():
        if (not isinstance(item, dict) or set(item) != {"n", "v"}
                or not isinstance(item["n"], str) or not isinstance(item["v"], str)):
            raise ValueError("bad record")
        out.append((item["n"], item["v"]))
    return out


def _same_site(origin: str, other: str) -> bool:
    """Conservative same-site: same scheme and the same host or a parent/child host.

    Sibling hosts (a.example.com vs b.example.com) count as different — fewer merges.
    """
    first, second = urlsplit(origin or ""), urlsplit(other or "")
    host, peer = (first.hostname or ""), (second.hostname or "")
    if not host or not peer or first.scheme != second.scheme:
        return False
    return host == peer or host.endswith("." + peer) or peer.endswith("." + host)


def _binding_name() -> str:
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(secrets.choice(letters) for _ in range(14))


def _current_task() -> "asyncio.Task | None":
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


#: WS-52 R3 observer (disclosed, not stealth): the assigned value goes unchanged to the
#: native setter — same return, exceptions, getter and value conversions; the wrapper's
#: own source/name/stack frames are observable. Intrinsics are captured at install; the
#: binding global is deleted at once and every record carries a closure-held nonce.
_OBSERVER_JS = r"""(() => {
  const g = globalThis;
  let B;
  try { B = g[__BINDING__]; delete g[__BINDING__]; } catch (e) { return; }
  if (typeof B !== 'function') return;
  const K = __NONCE__;
  const O = Object, apply = Reflect.apply, gopd = O.getOwnPropertyDescriptor, def = O.defineProperty, mk = O.create;
  const stringify = JSON.stringify, split = String.prototype.split, indexOf = String.prototype.indexOf;
  const slice = String.prototype.slice, then = g.Promise && g.Promise.prototype.then;
  const D = g.Document && g.Document.prototype;
  const d = D && gopd(D, 'cookie');
  if (!d || typeof d.get !== 'function' || typeof d.set !== 'function' || !d.configurable) return;
  const get = d.get, set = d.set;
  const bag = (s) => {
    const b = mk(null);
    if (s === '') return b;
    const parts = apply(split, s, ['; ']);
    for (let i = 0; i < parts.length; i++) { const k = parts[i]; b[k] = (b[k] || 0) + 1; }
    return b;
  };
  const pair = (k) => {
    const p = mk(null), i = apply(indexOf, k, ['=']);
    if (i < 0) { p.n = ''; p.v = k; } else { p.n = apply(slice, k, [0, i]); p.v = apply(slice, k, [i + 1]); }
    return p;
  };
  const report = (before, after) => {
    if (typeof before !== 'string' || typeof after !== 'string' || before === after) return;
    const b = bag(before), a = bag(after), P = mk(null), A = mk(null), R = mk(null);
    let na = 0, nr = 0;
    for (const k in a) { let n = a[k] - (b[k] || 0); while (n-- > 0) A[na++] = pair(k); }
    for (const k in b) { let n = b[k] - (a[k] || 0); while (n-- > 0) R[nr++] = pair(k); }
    P.k = K; P.a = A; P.r = R;
    B(stringify(P));
  };
  const read = (doc) => { try { return apply(get, doc, []); } catch (e) { return undefined; } };
  const quiet = (before, after) => { try { report(before, after); } catch (e) {} };
  const wrapped = { set(v) { const before = read(this); const r = apply(set, this, [v]); quiet(before, read(this)); return r; } }.set;
  const desc = mk(null);
  desc.get = get; desc.set = wrapped; desc.enumerable = d.enumerable; desc.configurable = true;
  def(D, 'cookie', desc);
  const S = g.CookieStore && g.CookieStore.prototype, doc = g.document;
  const store = (m) => {
    const od = S && gopd(S, m);
    if (!od || typeof od.value !== 'function' || typeof then !== 'function') return;
    const orig = od.value;
    const w = { [m]() {
      const before = read(doc);
      const p = apply(orig, this, arguments);
      try { apply(then, p, [() => quiet(before, read(doc)), () => {}]); } catch (e) {}
      return p;
    } }[m];
    const nd = mk(null);
    nd.value = w; nd.writable = od.writable; nd.enumerable = od.enumerable; nd.configurable = od.configurable;
    def(S, m, nd);
  };
  try { store('set'); store('delete'); } catch (e) {}
})();"""


def _observer_source(binding: str, nonce: str) -> str:
    return _OBSERVER_JS.replace("__BINDING__", json.dumps(binding)).replace("__NONCE__", json.dumps(nonce))


def _listen(tail: _Tail, emitter: object, event: str, handler: Callable[..., None]) -> None:
    emitter.on(event, handler)  # type: ignore[attr-defined]
    tail.listeners.append((emitter, event, handler))


def _detach(tail: _Tail) -> None:
    listeners, tail.listeners = tail.listeners, []
    for emitter, event, handler in listeners:
        try:
            emitter.remove_listener(event, handler)
        except Exception:  # noqa: BLE001 - a closed page may refuse; the tail is over either way
            pass
