"""One disclosed headless-name attempt, remembering success per origin.

Fetch.continueRequest changes only this paused request's headers, unlike a
target-wide UA override or Playwright route headers inherited by redirects.
Client Hint lists remove only the additional headless brand and use Chromium's
native version-seeded ordering; Client Hint versions, GREASE and platform stay
native. The UA uses headed Chromium's native major-only version reduction.

WS-47: cookies a successful retry caused are ledgered (keyed fingerprints only)
so a site's visitor cookie does not disable the memory; with serve --profile
the memory persists in the profile folder (headless_retry.json, 0600).
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
#: WS-52 settle tail: after a committed success, page scripts' cookie writes on
#: the same tab keep being ledgered this long (unless a stop condition fires first).
TAIL_S = 10.0
TAIL_INTERVAL_S = 0.5
#: WS-52 R1: set inside a tool call (same task) so the tool's own nested calls do not bump the generation.
_IN_TOOL_CALL: ContextVar[bool] = ContextVar("headless_retry_in_tool_call", default=False)

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


LedgerEntry = dict  # {"name", "domain", "path", "value_digest"} — never the value itself


def _valid_entry(entry: object) -> bool:
    return (isinstance(entry, dict) and set(entry) == {"name", "domain", "path", "value_digest"}
            and all(isinstance(entry[k], str) for k in entry)
            and re.fullmatch(r"[0-9a-f]{64}", entry["value_digest"]) is not None)


class HeadlessRetry:
    """Per-origin retry memory plus the ledger of cookies our successful retries caused.

    A cookie the site planted during a successful renamed navigate (a visitor
    id) must not later count as "logged in": the origin is eligible when every
    cookie it sends is exactly (name, domain, path, keyed value digest) one we
    recorded. A new cookie or a changed value restores the native name. With
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
        #: WS-52 R1: bumped synchronously when a non-read-only tool call starts (``tool_call``).
        self._tool_gen = 0

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

        A cookie already present in ``before`` but never ledgered stays excluded.
        """
        current = self.ledger.get(origin, [])
        known = {tuple(sorted(e.items())) for e in current}
        prior = {tuple(sorted(e.items())) for e in before}
        kept = [e for e in current if e in after]
        created = [e for e in after if tuple(sorted(e.items())) not in prior
                   and tuple(sorted(e.items())) not in known]
        merged = (kept + created)[-MAX_COOKIES:]  # overflow forgets oldest: fewer retries
        if merged == current:
            return False
        self.ledger[origin] = merged
        return True

    def absorb(self, origin: str, before: list[LedgerEntry], after: list[LedgerEntry]) -> None:
        """WS-52 tail: ledger cookie changes after a committed success (same merge rule)."""
        if self.states.get(origin) != "succeeded" or not self._merge(origin, before, after):
            return
        self.updated[origin] = self._clock()
        self._bound()
        self.save()

    # -- WS-52 settle tail --------------------------------------------------

    @contextmanager
    def tool_call(self, read_only: bool) -> Iterator[None]:
        """WS-52 R1 start barrier: enter it at every tool entry, before any await.

        A non-read-only call synchronously bumps the tool generation and marks the
        running tail closed, so a tail read already in flight is refused after its
        await (``_tail_current``). Calls the tool makes itself (the first retry's
        recursive navigate, recipe steps) still close a running tail but do not bump
        the generation: they belong to the call that started the navigate, so the
        tail that navigate starts is not refused because of them.
        """
        nested = _IN_TOOL_CALL.get()
        if not read_only:
            if not nested:
                self._tool_gen += 1
            self._close_tail()
        token = _IN_TOOL_CALL.set(True)
        try:
            yield
        finally:
            _IN_TOOL_CALL.reset(token)

    def _close_tail(self) -> None:
        """Mark the running tail closed (synchronous; no final sample, nothing merged)."""
        tail, self._tail = self._tail, None
        if tail is not None:
            tail.stopped = True
            _detach(tail)

    def start_tail(self, page: Page, url: str, origin: str, last: list[LedgerEntry],
                   guard: Callable[[], bool], gen: int | None = None) -> None:
        """Keep ledgering this tab's script cookie writes for TAIL_S after a committed success.

        Runs in the background (the tool response is not delayed). Each sample
        merges only while ``guard()`` (no human control/handoff, still headless
        with the feature on), no tool call started since (generation), the
        deadline has not passed, and the tab is open, still on the origin, with no
        related visit in another tab — latched from context events, so a tab that
        lived between two samples still ends it. Every condition is re-checked
        after the cookie read's await. Anything unexpected ends it without merging;
        when the events cannot be subscribed the tail does not start at all.
        """
        self.cancel_tail()
        if gen is not None and gen != self._tool_gen:
            return  # a tool call started while the navigate ran: its effects are not ours
        tail = _Tail(page=page, url=url, origin=origin, last=last, guard=guard,
                     deadline=time.monotonic() + TAIL_S, gen=self._tool_gen)
        host = urlsplit(url).hostname or ""

        def broken(*_: object) -> None:
            tail.broken = True

        def navigated(frame: object) -> None:
            # Any later main-frame document (same origin included) was requested with the
            # native name: its cookies are not ours.
            if frame is page.main_frame:
                tail.broken = True

        def other_navigated(frame: object) -> None:
            try:
                if _related_host(host, urlsplit(getattr(frame, "url", "") or "").hostname or ""):
                    tail.broken = True
            except Exception:  # noqa: BLE001 - unknown frame: assume the worst
                tail.broken = True

        def watch(other: object) -> None:
            # Another tab (existing, new or popup): any related visit there latches the end.
            if other is page:
                return
            try:
                if _related_host(host, urlsplit(getattr(other, "url", "") or "").hostname or ""):
                    tail.broken = True
                _listen(tail, other, "framenavigated", other_navigated)
            except Exception:  # noqa: BLE001 - an unwatchable tab cannot be ruled out
                tail.broken = True

        def requested(request: object) -> None:
            # Context-wide: every request of every tab (popups included) as it is issued.
            try:
                target = urlsplit(getattr(request, "url", "") or "").hostname or ""
            except Exception:  # noqa: BLE001 - unreadable request: assume the worst
                tail.broken = True
                return
            try:
                frame = request.frame  # type: ignore[attr-defined]
                own = frame.page is page
            except Exception:  # noqa: BLE001 - no frame (service worker): not provably this tab
                frame, own = None, False
            if own:
                try:
                    # The renamed page's own subresources are same-tab script traffic; a new
                    # main-frame document (download included) is a native-name visit.
                    if frame is page.main_frame and request.is_navigation_request():  # type: ignore[attr-defined]
                        tail.broken = True
                except Exception:  # noqa: BLE001
                    tail.broken = True
                return
            if _related_host(host, target):
                tail.broken = True

        try:
            _listen(tail, page, "framenavigated", navigated)
            _listen(tail, page, "close", broken)
            context = page.context
            _listen(tail, context, "page", watch)
            _listen(tail, context, "request", requested)
            for other in list(context.pages):
                watch(other)
            tail.task = asyncio.get_running_loop().create_task(self._run_tail(tail))
        except Exception:  # noqa: BLE001 - unwatched means not started (fail-closed; WS-47 behavior)
            _detach(tail)
            return
        self._tail = tail

    def cancel_tail(self) -> None:
        """Stop the tail without a final sample (server close, context replacement)."""
        tail = self._tail
        self._close_tail()
        if tail is not None and tail.task is not None:
            tail.task.cancel()

    async def end_tail(self) -> None:
        """Stop the tail: cancel only — R1 takes no final sample (fail-closed).

        A script change in the last moment before the end is lost; the next visit
        then honestly reports ``known_value_changed``/``new_cookie``.
        """
        self.cancel_tail()

    async def _run_tail(self, tail: _Tail) -> None:
        try:
            while not tail.stopped:
                await asyncio.sleep(TAIL_INTERVAL_S)
                if tail.stopped or time.monotonic() > tail.deadline or not await self._tail_sample(tail):
                    break
        except Exception:  # noqa: BLE001 - closed tab/context: end silently, nothing half-merged
            pass
        finally:
            if self._tail is tail:
                self._tail = None
                _detach(tail)

    def _tail_current(self, tail: _Tail) -> bool:
        """No tool call started since the tail began, it was not closed, the deadline holds."""
        return (not tail.stopped and tail.gen == self._tool_gen and self._tail is tail
                and time.monotonic() <= tail.deadline)

    def _tail_open(self, tail: _Tail) -> bool:
        page = tail.page
        if (tail.broken or self.states.get(tail.origin) != "succeeded" or not tail.guard()
                or page.is_closed()):
            return False
        if _origin(page.url) != tail.origin:
            return False
        host = urlsplit(tail.url).hostname or ""
        return not any(other is not page and _related_host(host, urlsplit(other.url).hostname or "")
                       for other in page.context.pages)

    async def _tail_sample(self, tail: _Tail) -> bool:
        """One merge step; False ends the tail (conditions broke or cookies unreadable)."""
        if not self._tail_current(tail) or not self._tail_open(tail):
            return False
        remaining = tail.deadline - time.monotonic()
        try:
            current = await asyncio.wait_for(self.snapshot(tail.page.context, tail.url), remaining)
        except asyncio.TimeoutError:
            return False  # the read outlasted the maximum window
        # Re-check after the await (nothing awaits between here and the merge): a tool call,
        # human control, navigation, related visit or the deadline that raced the read wins.
        if current is None or not self._tail_current(tail) or not self._tail_open(tail):
            return False
        self.absorb(tail.origin, tail.last, current)
        tail.last = current
        return True

    # -- cookies -------------------------------------------------------------

    def _digest(self, value: str) -> str:
        return hmac.new(self._key, value.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()

    async def snapshot(self, context: BrowserContext, url: str) -> list[LedgerEntry] | None:
        """Fingerprints of the origin's cookies, or None when cookies are unavailable."""
        try:
            cookies = await origin_cookies(context, url)
        except Exception:  # noqa: BLE001 - unavailable cookies never authorize a changed name
            return None
        return [{"name": str(c.get("name", "")), "domain": str(c.get("domain", "")),
                 "path": str(c.get("path") or "/"), "value_digest": self._digest(str(c.get("value", "")))}
                for c in cookies]

    async def cookies_cleared(self, context: BrowserContext, url: str) -> bool:
        """No cookie for the origin, or every one exactly matches the ledger."""
        current = await self.snapshot(context, url)
        if current is None:
            return False
        ledger = self.ledger.get(_origin(url) or "", [])
        return all(entry in ledger for entry in current)

    async def cookie_change(self, context: BrowserContext, url: str) -> str | None:
        """Why cookies are not cleared: "new_cookie" (an unledgered name/domain/path — maybe a
        login) wins over "known_value_changed" (only ledgered cookies with other values).
        None when cleared or unreadable. Never returns names or values."""
        current = await self.snapshot(context, url)
        if current is None:
            return None
        ledger = self.ledger.get(_origin(url) or "", [])
        keys = {(e["name"], e["domain"], e["path"]) for e in ledger}
        changed = [e for e in current if e not in ledger]
        if not changed:
            return None
        if any((e["name"], e["domain"], e["path"]) not in keys for e in changed):
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
        this tab's script cookie writes (see ``start_tail``).
        """
        destination = _origin(url)
        version = re.search(r"HeadlessChrome/([0-9]+)\.", original)
        if version is None:
            raise ValueError("unavailable native headless version")
        major = int(version[1])
        gen = self._tool_gen  # the calling tool already passed the barrier; a later call refuses the tail
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
                if tail_guard is not None and after is not None and tail_guard():
                    self.start_tail(page, url, destination, after, tail_guard, gen)


@dataclass
class RetryRequest:
    """Whether a renamed main-document request actually left interception."""

    document_requested: bool = False
    origin: str | None = None
    #: Set by the caller once the final result is a same-origin 2xx (ledger commit on exit).
    succeeded: bool = False


@dataclass
class _Tail:
    """One running WS-52 settle tail (one per server: a new success replaces it)."""

    page: Page
    url: str
    origin: str
    last: list
    guard: Callable[[], bool]
    deadline: float
    stopped: bool = False
    #: The tab closed, its main frame navigated, or a related visit happened in another tab
    #: (latched from events): nothing more is merged.
    broken: bool = False
    #: The tool generation at the start (HeadlessRetry._tool_gen); any later tool call refuses merges.
    gen: int = 0
    task: "asyncio.Task | None" = field(default=None, repr=False)
    #: (emitter, event, handler) — removed together when the tail ends.
    listeners: list = field(default_factory=list, repr=False)


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
