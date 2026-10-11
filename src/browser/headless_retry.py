"""One disclosed headless-name retry; no browser fingerprint/JS modifications.

Fetch.continueRequest changes only this paused request's headers, unlike a
target-wide UA override or Playwright route headers inherited by redirects.
Client Hint lists remove only the additional headless brand and use Chromium's
native version-seeded ordering; Client Hint versions, GREASE and platform stay
native. The UA uses headed Chromium's native major-only version reduction.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from itertools import permutations
import re
from typing import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page

from actions.pdf_document import cookie_matches
from security.egress import EgressGuard
from security.egress_proxy import EgressProxy
from security.robots_signal import RobotsSignals, _origin


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


async def cookie_free_origin(context: BrowserContext, url: str) -> bool:
    """Check domain/secure/expiry for every path on the origin, without logging values."""
    parts = urlsplit(url)
    try:
        for cookie in await context.cookies():
            # Cookies scoped to another path still indicate a potentially logged-in origin.
            destination = urlunsplit((parts.scheme, parts.netloc, cookie.get("path") or "/", "", ""))
            if cookie_matches(cookie, destination):
                return False
        return True
    except Exception:  # noqa: BLE001 - unavailable cookies never authorize a changed name
        return False


class HeadlessRetry:
    def __init__(self) -> None:
        # Server lifetime, independent of tab/context replacement (on-demand).
        self.attempted: set[str] = set()

    @asynccontextmanager
    async def present(self, page: Page, url: str, original: str, robots: RobotsSignals,
                      guard: EgressGuard, proxy: EgressProxy | None) -> AsyncIterator[RetryRequest]:
        """Present the native headed Chromium name and reduced UA during this retry.

        Every request/redirect is separately scoped; all existing egress routes
        and the pinned proxy remain installed. Cookie changes fail closed, and
        robots-disallowed subpaths keep the native name. Detach restores the
        native request path even when navigation is cancelled or fails.
        """
        destination = _origin(url)
        version = re.search(r"HeadlessChrome/([0-9]+)\.", original)
        if version is None:
            raise ValueError("unavailable native headless version")
        major = int(version[1])
        session = await page.context.new_cdp_session(page)
        attempt = RetryRequest()

        async def paused(event: dict) -> None:
            request = event["request"]
            target = request["url"]
            params: dict = {"requestId": event["requestId"]}
            try:
                headers = request["headers"]
                ua_key = next((key for key in headers if key.lower() == "user-agent"), None)
                if (_origin(target) == destination and ua_key is not None and headers[ua_key] == original
                        and await cookie_free_origin(page.context, target)
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


@dataclass
class RetryRequest:
    """Whether a renamed main-document request actually left interception."""

    document_requested: bool = False
