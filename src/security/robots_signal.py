"""WS-41 robots.txt intent signal; never authorizes or blocks navigation.

REP groups are merged by product token, with '*' used only as a fallback.
Paths and queries use RFC 9309 octet comparison. Retrieval uses the existing
browser context request client and explicitly checks egress on every hop
(APIRequestContext does not pass through context.route).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypedDict
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page

from contracts import ActionResult
from security.egress import EgressGuard

MAX_ROBOTS_BYTES = 500 * 1024
AI_USER_AGENTS = (
    "GPTBot", "ChatGPT-User", "OAI-SearchBot", "ClaudeBot", "Claude-User",
    "anthropic-ai", "PerplexityBot", "Google-Extended", "CCBot", "Bytespider",
    "Applebot-Extended", "meta-externalagent", "meta-externalfetcher",
)
ROBOTS_INSTRUCTIONS = (
    "결과에 data.robots 가 있으면 그 사이트가 자동 접근을 막고 있다는 뜻입니다 — "
    "계속할지는 사용자 뜻에 따라 판단하세요."
)
_UNRESERVED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
_AGENT = re.compile(r"(?:[a-z_-]+|\*)\Z", re.IGNORECASE)
_ESCAPE = re.compile(r"%([0-9a-f]{2})", re.IGNORECASE)


class RobotsSignal(TypedDict):
    disallowed: bool
    ai_agents_disallowed: list[str]
    rule: str
    robots_url: str


def _octets(value: str) -> str:
    """Encode UTF-8 and decode only percent-encoded unreserved ASCII."""
    encoded = quote(value, safe="/%:;?@&=+$,[]!()*'~-._")

    def normalize(match: re.Match[str]) -> str:
        char = chr(int(match[1], 16))
        return char if char in _UNRESERVED else "%" + match[1].upper()

    return _ESCAPE.sub(normalize, encoded)


def _matches(pattern: str, path: str) -> bool:
    """Anchored glob, using monotone searches instead of regex backtracking."""
    anchored = pattern.endswith("$")
    pieces = (pattern[:-1] if anchored else pattern).split("*")
    if not path.startswith(pieces[0]):
        return False
    end = len(pieces[0])
    if len(pieces) == 1:
        return not anchored or end == len(path)
    for piece in pieces[1:-1]:
        found = path.find(piece, end)
        if found < 0:
            return False
        end = found + len(piece)
    last = pieces[-1]
    if anchored:
        return path.endswith(last) and len(path) - len(last) >= end
    return path.find(last, end) >= 0


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    source: str

    @property
    def specificity(self) -> int:
        # The normalized pattern is ASCII; its length is its octet length,
        # including the wildcard/anchor operators (RFC 9309 longest rule).
        return len(self.pattern)


@dataclass(frozen=True)
class Match:
    disallowed: bool = False
    rule: str = ""


@dataclass(frozen=True)
class RobotsRules:
    groups: dict[str, tuple[Rule, ...]]

    @classmethod
    def parse(cls, body: bytes) -> RobotsRules:
        limited = body[:MAX_ROBOTS_BYTES]
        if len(body) > MAX_ROBOTS_BYTES:
            # Do not reinterpret a partially cut rule as a shorter prefix ban.
            limited = limited[:max(limited.rfind(b"\n"), limited.rfind(b"\r")) + 1]
        text = limited.decode("utf-8-sig", errors="replace")
        groups: dict[str, list[Rule]] = {}
        agents: list[str] = []
        has_rules = False
        for raw in re.split(r"\r\n|\r|\n", text):
            line = raw.partition("#")[0].strip()
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if not _AGENT.fullmatch(value):
                    continue
                if has_rules:
                    agents, has_rules = [], False
                token = value.lower()
                if token not in agents:
                    agents.append(token)
                    groups.setdefault(token, [])
            elif key in {"allow", "disallow"} and agents:
                if value and (
                    not value.startswith("/")
                    or any(ord(c) < 32 or ord(c) == 127 or c == "\ufffd" for c in value)
                ):
                    continue
                has_rules = True
                if not value:
                    continue
                rule = Rule(key == "allow", _octets(value), f"{key.title()}: {value}"[:80])
                for token in agents:
                    groups[token].append(rule)
        return cls({token: tuple(rules) for token, rules in groups.items()})

    def match(self, path: str, agent: str) -> Match:
        rules = self.groups.get(agent.lower(), self.groups.get("*", ()))
        path = _octets(path)
        matches = [rule for rule in rules if _matches(rule.pattern, path)]
        if not matches:
            return Match()
        winner = max(matches, key=lambda rule: (rule.specificity, rule.allow))
        return Match(not winner.allow, winner.source)

    def signal(self, path: str, robots_url: str) -> Optional[RobotsSignal]:
        wildcard = self.match(path, "*")
        blocked = [(agent, self.match(path, agent)) for agent in AI_USER_AGENTS]
        blocked = [(agent, match) for agent, match in blocked if match.disallowed][:10]
        if not wildcard.disallowed and not blocked:
            return None
        return {
            "disallowed": wildcard.disallowed,
            "ai_agents_disallowed": [agent for agent, _ in blocked],
            "rule": wildcard.rule if wildcard.disallowed else blocked[0][1].rule,
            "robots_url": robots_url,
        }


WAIT_TIMEOUT_S = 1.5
BACKGROUND_TIMEOUT_S = 10.0
CACHE_TTL_S = 24 * 60 * 60
UNKNOWN_TTL_S = 10 * 60
MAX_REDIRECTS = 5


def _origin(url: str) -> Optional[str]:
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        port = parts.port
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            host += f":{port}"
        return urlunsplit((parts.scheme, host, "", "", ""))
    except ValueError:
        return None


@dataclass(frozen=True)
class CacheEntry:
    expires: float
    rules: Optional[RobotsRules]


class RobotsSignals:
    """Server-local cache and shared, bounded background retrieval.

    The response waits at most 1.5 seconds; the same request can finish in the
    background up to 10 seconds. Confirmed 2xx/4xx-missing results live 24h;
    unavailable (including auth-denied) results live 10 minutes. Shutdown
    cancels and joins every request before the browser context is closed.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cache: dict[str, CacheEntry] = {}
        self._pending: dict[str, asyncio.Task[None]] = {}

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    async def signal(self, context: BrowserContext, url: str, guard: EgressGuard) -> Optional[RobotsSignal]:
        origin = _origin(url)
        if origin is None:
            return None
        robots_url = origin + "/robots.txt"
        # APIRequestContext skips route handlers. Check policy even on cache
        # hits, so tightening a live session's policy does not reuse a signal.
        if not guard.evaluate(robots_url).allowed:
            return None
        entry = self._cache.get(origin)
        if entry is None or entry.expires <= self._clock():
            task = self._pending.get(origin)
            if task is None:
                task = asyncio.create_task(self._populate(context, origin, guard))
                self._pending[origin] = task
            # shield keeps the shared request alive after the response budget
            # or a caller's cancellation expires. _populate owns its lifetime.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=WAIT_TIMEOUT_S)
            except asyncio.TimeoutError:
                return None
            entry = self._cache.get(origin)
        if entry is None or entry.rules is None:
            return None
        parts = urlsplit(url)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        return entry.rules.signal(path, robots_url)

    async def _populate(self, context: BrowserContext, origin: str, guard: EgressGuard) -> None:
        rules: Optional[RobotsRules] = None
        try:
            rules = await asyncio.wait_for(self._fetch(context, origin, guard), timeout=BACKGROUND_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - diagnostic failures never change the action result
            pass
        finally:
            self._pending.pop(origin, None)
        ttl = CACHE_TTL_S if rules is not None else UNKNOWN_TTL_S
        self._cache[origin] = CacheEntry(self._clock() + ttl, rules)

    async def _fetch(self, context: BrowserContext, origin: str, guard: EgressGuard) -> Optional[RobotsRules]:
        url = origin + "/robots.txt"
        for hop in range(MAX_REDIRECTS + 1):
            if not (await guard.evaluate_async(url)).allowed:
                return None
            # Browser-context client inherits its UA, proxy and TLS options.
            # Disable automatic redirects: every target must pass the guard.
            response = await context.request.get(url, max_redirects=0, timeout=BACKGROUND_TIMEOUT_S * 1000)
            try:
                status = response.status
                if 300 <= status < 400:
                    target = urljoin(url, response.headers.get("location", ""))
                    # Conservative same-site boundary: exact origin only.
                    if not response.headers.get("location") or _origin(target) != origin or hop == MAX_REDIRECTS:
                        return None
                    url = target
                elif 200 <= status < 300:
                    return RobotsRules.parse(await response.body())
                elif 400 <= status < 500 and status not in {401, 403}:
                    return RobotsRules({})
                else:
                    return None
            finally:
                await response.dispose()
        return None

    async def close(self) -> None:
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()


async def attach_robots_signal(service: RobotsSignals, result: ActionResult,
                               page: Optional[Page], guard: Optional[EgressGuard]) -> None:
    """Small MCP hook; only successful navigate/observe results carry signals."""
    if not result.success or page is None or guard is None:
        return
    try:
        signal = await service.signal(page.context, page.url, guard)
        if signal is not None:
            result.data["robots"] = signal
    except Exception:  # noqa: BLE001 - a diagnostic must not fail navigation/observation
        pass
