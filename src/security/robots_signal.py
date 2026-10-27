"""WS-41 robots.txt intent signal; never authorizes or blocks navigation.

REP groups are merged by product token, with '*' used only as a fallback.
Paths and queries use RFC 9309 octet comparison. Bounded HTTP streaming uses
the existing egress proxy and explicitly checks policy on every hop, without
sharing the Playwright command pipe or browser cookies/authentication.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Literal, NotRequired, Optional, TypedDict
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from weakref import WeakKeyDictionary

import httpx
from playwright.async_api import BrowserContext, CDPSession, Page

from contracts import ActionResult
from security.egress import EgressGuard
from security.egress_proxy import EgressProxy

logger = logging.getLogger(__name__)

MAX_ROBOTS_BYTES = 500 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024
AI_USER_AGENTS = (
    "GPTBot", "ChatGPT-User", "OAI-SearchBot", "ClaudeBot", "Claude-User",
    "anthropic-ai", "PerplexityBot", "Google-Extended", "CCBot", "Bytespider",
    "Applebot-Extended", "meta-externalagent", "meta-externalfetcher",
)
# Provider-documented user/search fetch product tokens (as opposed to training):
# support.claude.com/en/articles/8896518 (Claude-User/Claude-SearchBot),
# developers.openai.com/api/docs/bots (ChatGPT-User/OAI-SearchBot),
# docs.perplexity.ai/guides/bots (PerplexityBot/Perplexity-User).
# Presence here is descriptive only; Allow must be explicit for this path.
USER_FETCH_AGENTS = (
    "Claude-User", "ChatGPT-User", "OAI-SearchBot", "Claude-SearchBot",
    "PerplexityBot", "Perplexity-User",
)
ROBOTS_INSTRUCTIONS = (
    "결과의 data.robots 는 그 사이트의 자동 접근 의사 표시입니다 — "
    "계속할지는 사용자 뜻에 따라 판단하세요. "
    "사용자에게 robots 를 설명할 때 note 를 그대로 쓰세요. "
    "ai_agents_disallowed 는 이름으로 지정된 것만입니다. "
    "ai_agents_disallowed 는 주로 학습·수집 봇 목록이며 사용자가 요청한 읽기 자체를 막는 뜻은 "
    "아닙니다(기존 목록에는 사용자 요청 봇도 포함) — disallowed·user_fetch_agents_allowed·content_signal 을 참고하세요. "
    "data.robots.user_request 가 있으면 따르세요: 사용자가 직접 준 주소·콕 집은 한 페이지 읽기는 named_ai 금지여도 "
    "진행하고 robots 신호를 한 줄로 알리되, 같은 사이트 여러 페이지 수집·반복 크롤은 하지 마세요. "
    "all_bots(`User-agent: *` 금지)는 사용자 확인 뒤에만 진행하세요."
)
RobotsScope = Literal["all_bots", "named_ai", "all_bots_and_named_ai", "none"]
_SCOPE_NOTES = {
    "all_bots": "이 경로는 robots.txt 가 모든 자동 접근(봇 전체)에 대해 금지합니다 — AI 만 골라 막은 것이 아닙니다.",
    "named_ai": "robots.txt 가 일부 AI 봇 이름(주로 학습·수집용)을 지정해 이 경로를 금지합니다 — 모든 봇 대상 금지는 아닙니다.",
    "all_bots_and_named_ai": "이 경로는 robots.txt 가 모든 봇에 대해 금지하고, AI 봇 일부도 이름으로 따로 금지합니다.",
}
#: WS-54: data.robots.user_request 고정 문장 — 금지 신호(scope != none)가 있을 때만 싣는다(허용·none·미확인은 생략).
#: 키는 scope 어휘. named_ai 만 '사용자가 직접 요청한 한 페이지 읽기는 진행', `*` 금지는 사용자 확인 뒤.
_USER_REQUEST_NOTES = {
    "named_ai": "사용자가 이 주소를 직접 주거나 이 페이지를 콕 집어 요청한 한 페이지 읽기는 사람이 브라우저로 여는 것과 "
                "같은 성격이라 진행해도 됩니다 — 결과에 robots 신호(note)를 한 줄로 알리세요. "
                "같은 사이트의 여러 페이지를 스스로 돌며 모으는 수집·반복 크롤은 하지 마세요.",
    "all_bots": "이 경로는 robots.txt 가 `User-agent: *` 로 모든 봇을 막았습니다 — 사용자가 이 주소를 직접 줬더라도 "
                "robots 신호(note)를 알리고 사용자 확인을 받기 전에는 진행하지 마세요. "
                "같은 사이트의 여러 페이지를 스스로 돌며 모으는 수집·반복 크롤은 하지 마세요.",
    "all_bots_and_named_ai": "이 경로는 robots.txt 가 `User-agent: *` 로 모든 봇을 막았습니다 — 사용자가 이 주소를 직접 "
                             "줬더라도 robots 신호(note)를 알리고 사용자 확인을 받기 전에는 진행하지 마세요. "
                             "같은 사이트의 여러 페이지를 스스로 돌며 모으는 수집·반복 크롤은 하지 마세요.",
}
_UNRESERVED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
_AGENT = re.compile(r"[A-Za-z_-]+|\*(?=\s|$)")
_ESCAPE = re.compile(r"%([0-9a-f]{2})", re.IGNORECASE)


class RobotsSignal(TypedDict):
    disallowed: bool
    ai_agents_disallowed: list[str]
    rule: str
    robots_url: str
    user_fetch_agents_allowed: list[str]
    content_signal: str
    scope: RobotsScope
    note: NotRequired[str]
    user_request: NotRequired[str]


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
    content_signal: str = ""

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
        content_signals: list[str] = []
        for raw in re.split(r"\r\n|\r|\n", text):
            line = raw.partition("#")[0].strip()
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "content-signal":
                # Treat this as untrusted text, not an interpreted permission.
                safe = " ".join("".join(c for c in value if unicodedata.category(c)
                                       not in {"Cc", "Cf", "Cs"}).split())
                if safe and len("; ".join(content_signals)) < 256:
                    content_signals.append(safe[:256])
            elif key == "user-agent":
                if has_rules:
                    agents, has_rules = [], False
                # A malformed header still separates groups; its rules must
                # never attach to the preceding agents. Consecutive valid
                # headers continue to describe one group (RFC 9309).
                product = _AGENT.match(value)
                if product is None:
                    continue
                token = product[0].lower()
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
        return cls({token: tuple(rules) for token, rules in groups.items()}, "; ".join(content_signals)[:256])

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
        # Signal named bans only; match() retains '*' fallback for general policy checks.
        blocked = [(agent, self.match(path, agent)) for agent in AI_USER_AGENTS
                   if agent.lower() in self.groups]
        blocked = [(agent, match) for agent, match in blocked if match.disallowed][:10]
        allowed = [agent for agent in USER_FETCH_AGENTS if agent.lower() in self.groups
                   and (match := self.match(path, agent)).rule.startswith("Allow:") and not match.disallowed]
        if not wildcard.disallowed and not blocked and not allowed and not self.content_signal:
            return None
        scope: RobotsScope
        if wildcard.disallowed:
            scope = "all_bots_and_named_ai" if blocked else "all_bots"
        else:
            scope = "named_ai" if blocked else "none"
        signal: RobotsSignal = {
            "disallowed": wildcard.disallowed,
            "ai_agents_disallowed": [agent for agent, _ in blocked],
            "rule": wildcard.rule if wildcard.disallowed else blocked[0][1].rule if blocked else wildcard.rule,
            "robots_url": robots_url,
            "user_fetch_agents_allowed": allowed,
            "content_signal": self.content_signal,
            "scope": scope,
        }
        if scope != "none":
            signal["note"] = _SCOPE_NOTES[scope]
            signal["user_request"] = _USER_REQUEST_NOTES[scope]
        return signal


WAIT_TIMEOUT_S = 1.5
BACKGROUND_TIMEOUT_S = 10.0
CACHE_TTL_S = 24 * 60 * 60
UNKNOWN_TTL_S = 10 * 60
MAX_REDIRECTS = 5
MAX_CACHE_ORIGINS = 1000
UA_TIMEOUT_S = 0.1
# WS-72 (#184): 실패한 UA 판독을 공유하는 시간. 이 창 안의 navigate 는 지연 없이 unknown, 지나면 다음 호출이 다시 읽는다
# — 첫 판독이 루프 정지(GC·부하)로 UA_TIMEOUT_S 를 넘겨도 robots 신호·headless 재시도가 세션 끝까지 꺼지지 않게.
UA_UNKNOWN_TTL_S = 30.0


def _origin(url: str) -> Optional[str]:
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.lower().rstrip(".")
        if not host:
            return None
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
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._user_agents: WeakKeyDictionary[BrowserContext, asyncio.Future[Optional[str]]] = WeakKeyDictionary()
        self._ua_retry_at: WeakKeyDictionary[BrowserContext, float] = WeakKeyDictionary()
        # WS-72 R1: 실패 창마다 예산 있는 재판독은 한 번만, 동시 호출자는 그 한 task 를 함께 기다린다(단일 비행).
        # 키는 그 창을 연 실패 future — 창이 바뀌면(새 판독 실패) 새 재판독이 허용된다.
        self._ua_rereads: WeakKeyDictionary[
            BrowserContext, tuple[asyncio.Future[Optional[str]], asyncio.Task[Optional[str]]]] = WeakKeyDictionary()
        # CDP 세션 detach 는 호출자 예산 밖에서 끝나도록 미뤄 두고, close() 가 모두 기다린다(누수 없음).
        self._ua_cleanups: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    async def signal(self, context: BrowserContext, url: str, guard: EgressGuard,
                     proxy: Optional[EgressProxy] = None) -> Optional[RobotsSignal]:
        rules = await self._rules_for(context, url, guard, proxy)
        if rules is None:
            return None
        parts = urlsplit(url)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        return rules.signal(path, (_origin(url) or "") + "/robots.txt")

    async def allowed(self, context: BrowserContext, url: str, guard: EgressGuard,
                      proxy: Optional[EgressProxy] = None) -> Optional[bool]:
        """Confirmed wildcard-path permission; None means retrieval/policy unknown."""
        rules = await self._rules_for(context, url, guard, proxy)
        if rules is None:
            return None
        parts = urlsplit(url)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        return not rules.match(path, "*").disallowed

    async def _rules_for(self, context: BrowserContext, url: str, guard: EgressGuard,
                         proxy: Optional[EgressProxy] = None) -> Optional[RobotsRules]:
        origin = _origin(url)
        if origin is None:
            return None
        robots_url = origin + "/robots.txt"
        # Check policy even on cache hits, so tightening a live session's
        # policy does not reuse a signal.
        if not guard.evaluate(robots_url).allowed:
            return None
        entry = self._cache.get(origin)
        if entry is None or entry.expires <= self._clock():
            self._cache.pop(origin, None)
            task = self._pending.get(origin)
            if task is None:
                task = asyncio.create_task(self._populate(context, origin, guard, proxy))
                self._pending[origin] = task
            # shield keeps the shared request alive after the response budget
            # or a caller's cancellation expires. _populate owns its lifetime.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=WAIT_TIMEOUT_S)
            except asyncio.TimeoutError:
                return None
            entry = self._cache.get(origin)
        if entry is not None:
            self._cache.move_to_end(origin)
        if entry is None or entry.rules is None:
            return None
        return entry.rules

    async def _populate(self, context: BrowserContext, origin: str, guard: EgressGuard,
                        proxy: Optional[EgressProxy] = None) -> None:
        rules: Optional[RobotsRules] = None
        try:
            rules = await asyncio.wait_for(self._fetch(context, origin, guard, proxy), timeout=BACKGROUND_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - diagnostic failures never change the action result
            logger.debug("robots.txt 백그라운드 조회 실패 — 판정 불가로 캐시 (%s)", type(exc).__name__)  # WS-48: 흐름 불변, 진단용
            pass
        finally:
            self._pending.pop(origin, None)
        ttl = CACHE_TTL_S if rules is not None else UNKNOWN_TTL_S
        self._cache[origin] = CacheEntry(self._clock() + ttl, rules)
        self._cache.move_to_end(origin)
        while len(self._cache) > MAX_CACHE_ORIGINS:
            self._cache.popitem(last=False)

    async def _fetch(self, context: BrowserContext, origin: str, guard: EgressGuard,
                     proxy: Optional[EgressProxy] = None) -> Optional[RobotsRules]:
        url = origin + "/robots.txt"
        if not (await guard.evaluate_async(url)).allowed:
            return None
        user_agent = await self._context_user_agent(context)
        if user_agent is None:
            return None
        owned_proxy = proxy is None
        if owned_proxy:
            # Standalone callers still use the same guarded/pinned transport.
            proxy = await EgressProxy(guard).start()
        try:
            if proxy is None or not proxy.running:
                return None
            transport_proxy = httpx.Proxy(proxy.server_url)
            async with httpx.AsyncClient(proxy=transport_proxy, trust_env=False, follow_redirects=False,
                                         timeout=BACKGROUND_TIMEOUT_S,
                                         headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}) as client:
                for hop in range(MAX_REDIRECTS + 1):
                    if not (await guard.evaluate_async(url)).allowed:
                        return None
                    # Never forward even cookies set by a previous robots hop.
                    client.cookies.clear()
                    async with client.stream("GET", url) as response:
                        status = response.status_code
                        if 300 <= status < 400:
                            target = urljoin(url, response.headers.get("location", ""))
                            if not response.headers.get("location") or _origin(target) != origin or hop == MAX_REDIRECTS:
                                return None
                            url = target
                        elif 200 <= status < 300:
                            length = response.headers.get("content-length")
                            if length is not None:
                                try:
                                    if int(length) < 0 or int(length) > MAX_DOWNLOAD_BYTES:
                                        return None
                                except ValueError:
                                    return None
                            if response.headers.get("content-encoding", "identity").lower() != "identity":
                                return None
                            body = bytearray()
                            async for chunk in response.aiter_raw(chunk_size=16 * 1024):
                                body.extend(chunk)
                                if len(body) >= MAX_DOWNLOAD_BYTES:
                                    # At the limit, unknown-length streams are
                                    # unknown. Do not read another byte for EOF.
                                    if length is None or int(length) != MAX_DOWNLOAD_BYTES:
                                        return None
                                    break
                            return RobotsRules.parse(bytes(body))
                        elif 400 <= status < 500 and status not in {401, 403, 429}:
                            return RobotsRules({})
                        else:
                            return None
        finally:
            if owned_proxy and proxy is not None:
                await proxy.close()
        return None

    async def _context_user_agent(self, context: BrowserContext) -> Optional[str]:
        # Share discovery across origins, including failures — but a failure only
        # for UA_UNKNOWN_TTL_S (WS-72). A page must never supply this header: its
        # navigator getter is controlled by the site.
        task = self._user_agents.get(context)
        if task is not None and self._failed(task) and self._clock() >= self._ua_retry_at.get(context, 0.0):
            task = None
        if task is None:
            task = asyncio.create_task(self._discover_user_agent(context, UA_TIMEOUT_S))
            self._user_agents[context] = task
        return await asyncio.shield(task)

    async def refresh_user_agent(self, context: BrowserContext, timeout_s: float) -> Optional[str]:
        """UA 를 아직 모르면(판독 실패 공유 중이어도) timeout_s 안에서 한 번 더 읽는다 — 자기 예산이 있는 호출자용
        (PDF 재요청, WS-72 #184). 성공하면 공유 캐시를 갱신해 navigate 경로도 회복한다. 페이지에서는 읽지 않는다.

        WS-72 R1: 재판독은 컨텍스트·실패 창마다 최대 1회다. 진행 중이면 모든 호출자가 같은 task 를 기다리고
        (호출자 취소는 공유 task 에 번지지 않는다), 이미 실패했으면 창이 끝날 때까지 다시 읽지 않는다.
        반환은 detach 정리를 포함해 timeout_s 안이다."""
        try:
            async with asyncio.timeout(timeout_s):
                known = await self._context_user_agent(context)
                if known is not None:
                    return known
                window = self._user_agents.get(context)
                if window is None:
                    return None
                entry = self._ua_rereads.get(context)
                if entry is None or entry[0] is not window:
                    entry = (window, asyncio.create_task(self._reread_user_agent(context, window, timeout_s)))
                    self._ua_rereads[context] = entry
                return await asyncio.shield(entry[1])
        except TimeoutError:
            return None

    async def _reread_user_agent(self, context: BrowserContext, window: "asyncio.Future[Optional[str]]",
                                 timeout_s: float) -> Optional[str]:
        user_agent = await self._read_user_agent(context, timeout_s)
        if user_agent is not None and self._user_agents.get(context) is window:
            done: asyncio.Future[Optional[str]] = asyncio.get_running_loop().create_future()
            done.set_result(user_agent)
            self._user_agents[context] = done
            self._ua_retry_at.pop(context, None)
        return user_agent

    @staticmethod
    def _failed(task: "asyncio.Future[Optional[str]]") -> bool:
        return task.done() and not task.cancelled() and task.exception() is None and task.result() is None

    async def _discover_user_agent(self, context: BrowserContext, timeout_s: float) -> Optional[str]:
        user_agent = await self._read_user_agent(context, timeout_s)
        if user_agent is None:
            self._ua_retry_at[context] = self._clock() + UA_UNKNOWN_TTL_S
        return user_agent

    async def _read_user_agent(self, context: BrowserContext, timeout_s: float = UA_TIMEOUT_S) -> Optional[str]:
        session = None
        try:
            # Playwright exposes no public context-options getter. Read only
            # its immutable, host-side userAgent option, never browser headers
            # or cookies. Missing internals fail closed rather than guess a UA.
            override = context._impl_obj._options.get("userAgent")
            if override is not None:
                return override if isinstance(override, str) else None
            browser = context.browser
            if browser is None:
                return None
            async with asyncio.timeout(timeout_s):
                session = await browser.new_browser_cdp_session()
                version = await session.send("Browser.getVersion")
                user_agent = version.get("userAgent")
                return user_agent if isinstance(user_agent, str) else None
        except Exception:  # noqa: BLE001 - unavailable UA means unknown, never a page probe
            return None
        finally:
            if session is not None:
                # WS-72 R1: 정리는 호출자 예산 밖으로 미루되 추적해 close() 에서 끝낸다.
                cleanup = asyncio.create_task(self._detach(session))
                self._ua_cleanups.add(cleanup)
                cleanup.add_done_callback(self._ua_cleanups.discard)

    @staticmethod
    async def _detach(session: CDPSession) -> None:
        try:
            await asyncio.wait_for(session.detach(), timeout=UA_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - browser shutdown must not delay navigation
            pass

    async def close(self) -> None:
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        ua_tasks = list(self._user_agents.values())
        for task in ua_tasks:
            if not task.done():
                task.cancel()
        if ua_tasks:
            await asyncio.gather(*ua_tasks, return_exceptions=True)
        rereads = [task for _, task in self._ua_rereads.values()]
        for task in rereads:
            if not task.done():
                task.cancel()
        if rereads:
            await asyncio.gather(*rereads, return_exceptions=True)
        self._ua_rereads.clear()
        if self._ua_cleanups:
            await asyncio.gather(*list(self._ua_cleanups), return_exceptions=True)
        self._user_agents.clear()
        self._ua_retry_at.clear()


async def attach_robots_signal(service: RobotsSignals, result: ActionResult,
                               page: Optional[Page], guard: Optional[EgressGuard],
                               proxy: Optional[EgressProxy] = None) -> None:
    """Small MCP hook; only successful navigate/observe results carry signals."""
    if not result.success or page is None or guard is None:
        return
    try:
        signal = await service.signal(page.context, page.url, guard, proxy)
        if signal is not None:
            result.data["robots"] = signal
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not fail navigation/observation
        logger.debug("robots 신호 부착 실패 — 신호 없이 진행 (%s)", type(exc).__name__)  # WS-48: 흐름 불변, 진단용
        pass
