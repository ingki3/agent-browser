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

from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import hmac
from itertools import permutations
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from typing import AsyncIterator, Callable, Literal
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


def skip_reason(code: str) -> dict:
    return {"retried": False, "reason": code, "note": SKIP_NOTES[code]}


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
            known = {tuple(sorted(e.items())) for e in self.ledger.get(origin, [])}
            prior = {tuple(sorted(e.items())) for e in before}
            kept = [e for e in self.ledger.get(origin, []) if e in after]
            created = [e for e in after if tuple(sorted(e.items())) not in prior
                       and tuple(sorted(e.items())) not in known]
            merged = kept + created
            self.ledger[origin] = merged[-MAX_COOKIES:]  # overflow forgets oldest: fewer retries
        self._bound()
        self.save()

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

    # -- persistence ---------------------------------------------------------

    def load(self, path: Path) -> None:
        """Adopt the profile file; any unreadable/corrupt file means 'no memory'."""
        self.path = Path(path)
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
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
                    or not isinstance(when, (int, float)) or not isinstance(cookies, list)
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

    def save(self) -> bool:
        """Atomic 0600 write (temp + fsync + rename). Without a profile: memory only."""
        if self.path is None:
            return False
        origins = {}
        for origin in sorted(set(self.states) | set(self.ledger), key=lambda o: self.updated.get(o, 0.0)):
            origins[origin] = {"state": self.states.get(origin), "updated": self.updated.get(origin, 0.0),
                               "cookies": self.ledger.get(origin, [])}
        payload = json.dumps({"version": 1, "key": self._key.hex(), "origins": origins},
                             separators=(",", ":")).encode()
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
                      guard: EgressGuard, proxy: EgressProxy | None) -> AsyncIterator[RetryRequest]:
        """Present the native headed Chromium name and reduced UA during this retry.

        Every request/redirect is separately scoped; all existing egress routes
        and the pinned proxy remain installed. Cookie changes fail closed (a
        cookie created during this navigate is not ledgered yet, so the rest of
        the navigate keeps the native name), and robots-disallowed subpaths keep
        the native name. Detach restores the native request path even when
        navigation is cancelled or fails. When the caller marks the attempt
        succeeded, the cookies that appeared or changed are ledgered on exit.
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

        async def auth_required(event: dict) -> None:
            # With interception on, this session must answer auth itself. Only the
            # pinned egress proxy's own 407 gets its credentials; a site's auth
            # challenge keeps the browser default (no credentials invented).
            challenge = event.get("authChallenge") or {}
            response: dict = {"response": "Default"}
            creds = proxy.playwright_proxy() if proxy is not None else {}
            if (challenge.get("source") == "Proxy" and creds.get("username")
                    and str(challenge.get("origin") or "").rstrip("/") == creds["server"].rstrip("/")):
                response = {"response": "ProvideCredentials", "username": creds["username"],
                            "password": creds["password"]}
            try:
                await session.send("Fetch.continueWithAuth", {"requestId": event["requestId"],
                                                              "authChallengeResponse": response})
            except Exception:  # noqa: BLE001 - request/session may already have closed
                pass

        session.on("Fetch.authRequired", auth_required)
        try:
            main_frame = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
            await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}],
                                                "handleAuthRequests": True})
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


@dataclass
class RetryRequest:
    """Whether a renamed main-document request actually left interception."""

    document_requested: bool = False
    origin: str | None = None
    #: Set by the caller once the final result is a same-origin 2xx (ledger commit on exit).
    succeeded: bool = False
