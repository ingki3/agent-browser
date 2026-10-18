"""WS-52 R3: the settle tail ledgers only cookie writes the tab's own scripts provably made.

R0~R2 classified requests (which ones might have set a cookie) and every round an
independent check found another path — downloads, workers, requests issued before
the tail (B1~B11). R3 identifies script writes positively instead: after a
successful renamed navigate a disclosed observer wraps ``document.cookie`` (R4:
not ``CookieStore``) in that tab for at most 10 s and reports the
difference the native getter shows before/after each write. A cookie is ledgered
only when (1) the observer saw it appear, (2) in a same-origin (R4) execution context of
this tab, (3) it is the jar's only non-HttpOnly item with that name/value, (4) it is
the name's latest observation, (5) no Set-Cookie with that name/value was seen during
the tail and (6) the main-frame document is still the one the tail started on. The
server never reaches the ledger through any path: it writes no observation.

D (user decision): a key ledgered that way (``js``) may change its value on later
visits while it stays non-HttpOnly; any other change keeps the native name.

Synthetic loopback sites only (127.0.0.1 = the site, localhost = another site);
profiles under the per-test temporary AGENT_BROWSER_PROFILE_ROOT.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from interface.mcp_server import BrowserMCPServer
from test_run_cli import requires_chromium
from test_ws52_ledger_drift import VISITOR, present

#: Pages served to any user agent (subresources, frames, other-origin visits).
ANY_UA = ("/set", "/dl", "/frame", "/xwrite", "/worker.js", "/sw.js", "/plain", "/n2", "/other")


@dataclass
class R3Site:
    url: str = ""
    port: int = 0
    bodies: dict = field(default_factory=dict)
    default_body: bytes = b"<!doctype html><title>Read</title>"
    requests: list = field(default_factory=list)

    @property
    def other(self) -> str:
        """Another site on the same server (localhost ≠ 127.0.0.1)."""
        return f"http://localhost:{self.port}"

    def received(self, path):
        return [h for p, h in self.requests if p.split("?")[0] == path]

    def headless(self, path, index=0):
        return "HeadlessChrome" in self.received(path)[index]["user-agent"]


def page(js: str = "", extra: str = "") -> bytes:
    return f"<!doctype html><title>Read</title>{extra}<script>{js}</script>".encode()


@contextmanager
def r3_site():
    state = R3Site()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            if length:
                self.rfile.read(length)
            self.do_GET()

        def do_GET(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.requests.append((self.path, headers))
            path, query = urlsplit(self.path).path, parse_qs(urlsplit(self.path).query)
            cookies = []
            extra = {}
            kind = "text/html"
            if path == "/robots.txt":
                data, status, kind = b"User-agent: *\nAllow: /\n", 200, "text/plain"
            elif path.startswith(ANY_UA):
                status = 200
                for spec in query.get("c", []):
                    cookies.append(spec + "; Path=/; Max-Age=31536000")
                data = state.bodies.get(path, b"ok")
                if path == "/dl":
                    extra = {"Content-Disposition": "attachment; filename=synthetic.txt"}
                    kind = "application/octet-stream"
                elif path.endswith(".js"):
                    kind = "application/javascript"
                elif path == "/set":
                    kind = "text/plain"
                if path == "/sw.js":
                    extra = {"Service-Worker-Allowed": "/"}
            elif "HeadlessChrome" in headers.get("user-agent", ""):
                data, status = b"Forbidden", 403
            else:
                data, status = state.bodies.get(path, state.default_body), 200
                if "visitor=" not in headers.get("cookie", ""):
                    cookies.append(f"visitor={VISITOR}; Path=/; Max-Age=31536000")
            self.send_response_only(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state.port = httpd.server_address[1]
    state.url = f"http://127.0.0.1:{state.port}"
    try:
        yield state
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


@pytest.fixture
def site():
    with r3_site() as state:
        yield state


def server():
    return BrowserMCPServer(nav_settle=False, recipes=False)


async def navigate(srv, url):
    return await srv.call_tool("browser_navigate", {"url": url})


async def first_retry(srv, site, path="/one"):
    first = await navigate(srv, site.url + path)
    assert first.data["identity"]["original_status"] == 403, first.data
    assert srv._headless_retry._tail is not None, "the settle tail must be running"
    return first


async def jar(srv):
    return {c["name"]: c for c in await srv._page.context.cookies()}


async def plant(srv, site, name, value, http_only=False, url=None):
    """A cookie the browser got outside any observation (e.g. a download's Set-Cookie)."""
    await srv._page.context.add_cookies([{"name": name, "value": value, "url": url or site.url,
                                          "httpOnly": http_only}])


async def next_visit(srv, site, path="/next"):
    return await navigate(srv, site.url + path)


async def assert_native(srv, site, change, path="/next"):
    """WS-58 (intended change): the WS-52 invariant is checked on the ledger (strict comparison —
    the change was not merged); the next visit follows WS-58 rule (b)."""
    from ws58_helpers import assert_unmerged_next

    return await assert_unmerged_next(srv, site, change, navigate, path)


async def assert_renamed(srv, site, path="/next"):
    result = await next_visit(srv, site, path)
    assert result.data["last_http_status"] == 200, result.data.get("retry_skipped")
    assert result.data["identity"]["remembered"] is True
    assert not site.headless(path)
    return result


def ledgered(srv, site):
    return {e["name"]: e for e in srv._headless_retry.ledger.get(site.url, [])}


async def settle(seconds=1.2):
    await asyncio.sleep(seconds)  # > two 0.5 s samples


# -- 1. the purpose: the tab's own script writes are ledgered and disclosed ---------


@requires_chromium
async def test_identity_discloses_the_cookie_write_observer(site):
    site.default_body = page("setTimeout(() => { document.cookie = '_ga_R3=GS1.1.1; path=/; max-age=9999'; }, 300)")
    async with server() as srv:
        first = await first_retry(srv, site)
        assert first.data["identity"]["cookie_write_observer"] is True
        await settle()
        assert ledgered(srv, site)["_ga_R3"].get("js") is True
        second = await assert_renamed(srv, site, "/two")
        assert second.data["identity"]["cookie_write_observer"] is True  # the remembered visit observes too
        third = await navigate(srv, site.other + "/other")  # a visit that was never renamed
        assert "identity" not in third.data


# -- 2. server Set-Cookie through any path: no observation, never ledgered ------------


SERVER_PATHS = {
    "fetch": "fetch('/set?c=srv=S1')",
    "image": "new Image().src = '/set?c=srv=S1'",
    "beacon": "navigator.sendBeacon('/set?c=srv=S1')",
    "iframe": "(() => { const f = document.createElement('iframe'); f.src = '/frame?c=srv=S1'; document.body.append(f); })()",
    "download": "(() => { const a = document.createElement('a'); a.href = '/dl?c=srv=S1'; a.download = 'x.txt';"
                " document.body.append(a); a.click(); })()",
    "worker": "new Worker('/worker.js')",
    "service_worker": "navigator.serviceWorker.register('/sw.js').then(() => navigator.serviceWorker.ready)"
                      ".then((r) => r.active.postMessage('go'))",
}


@requires_chromium
@pytest.mark.parametrize("path", sorted(SERVER_PATHS))
async def test_server_cookie_through_any_path_is_never_merged(site, path):
    site.bodies["/worker.js"] = b"fetch('/set?c=srv=S1');"
    site.bodies["/sw.js"] = (b"self.addEventListener('install', () => self.skipWaiting());"
                             b"self.addEventListener('message', () => fetch('/set?c=srv=S1'));")
    async with server() as srv:
        await first_retry(srv, site)
        await srv._page.evaluate(SERVER_PATHS[path])
        for _ in range(40):
            if "srv" in await jar(srv):
                break
            await asyncio.sleep(0.1)
        assert "srv" in await jar(srv), "the server cookie never arrived (invalid trial)"
        await settle()
        assert "srv" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


# -- 3. review prototypes T5 / F1 / F2 / F3 as regressions ---------------------------


@requires_chromium
async def test_t5_blind_write_over_an_httponly_server_cookie_is_not_merged(site):
    async with server() as srv:
        await first_retry(srv, site)
        await srv._page.evaluate("fetch('/set?c=hsid=HLOGIN; HttpOnly').then((r) => r.text())")
        await srv._page.evaluate("document.cookie = 'hsid=HLOGIN'")  # refused by the browser, still a write
        await settle()
        assert (await jar(srv))["hsid"]["httpOnly"] is True
        assert "hsid" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


@requires_chromium
async def test_httponly_jar_item_is_never_merged_even_after_a_matching_write(site):
    # The script writes x=V (observed); the server then replaces it with an HttpOnly x=V
    # through a path nothing observes. Only the HttpOnly rule stops it.
    async with server() as srv:
        await first_retry(srv, site)
        await srv._page.evaluate("document.cookie = 'sid=LOGIN1; path=/'")
        await plant(srv, site, "sid", "LOGIN1", http_only=True)
        await settle()
        assert "sid" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


@requires_chromium
async def test_f1_payload_forged_with_patched_intrinsics_is_not_merged(site):
    async with server() as srv:
        await first_retry(srv, site)
        await plant(srv, site, "sid", "LOGIN1")  # readable server login cookie (unobserved path)
        await srv._page.evaluate("""() => {
            const real = JSON.stringify;
            JSON.stringify = (o) => real({a: {0: {n: 'sid', v: 'LOGIN1'}}, r: {}, n: o && o.n});
            String.prototype.split = function () { return ['sid=LOGIN1']; };
            document.cookie = 'harmless=1; path=/';
            JSON.stringify = real; }""")
        await settle()
        assert "harmless" in ledgered(srv, site) and "sid" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


GRABBER = page("""
const base = new Set(Object.getOwnPropertyNames(window)); window.__grabbed = [];
const tick = () => { for (const n of Object.getOwnPropertyNames(window)) {
  if (!base.has(n) && typeof window[n] === 'function' && !window.__grabbed.includes(window[n])) window.__grabbed.push(window[n]); } };
setInterval(tick, 0); const mc = new MessageChannel();
mc.port1.onmessage = () => { tick(); mc.port2.postMessage(0); }; mc.port2.postMessage(0);
""")


@requires_chromium
async def test_f2_binding_grabbed_by_the_page_cannot_forge_a_record(site):
    site.default_body = GRABBER
    async with server() as srv:
        await first_retry(srv, site)
        await asyncio.sleep(0.2)
        grabbed = await srv._page.evaluate("window.__grabbed.length")
        assert grabbed > 0, "the page did not grab the binding (invalid trial)"
        await plant(srv, site, "sid", "LOGIN1")
        await srv._page.evaluate("""() => { for (const f of window.__grabbed) { try {
            f(JSON.stringify({a: {0: {n: 'sid', v: 'LOGIN1'}}, r: {}}));
            f('{"a":{"0":{"n":"sid","v":"LOGIN1"}},"r":{},"k":"guess"}'); } catch (e) {} } }""")
        await settle()
        assert "sid" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


@requires_chromium
async def test_f3_cross_site_frame_write_is_not_attributed_to_the_site(site):
    # A third-party cookie must be SameSite=None; Secure (http://localhost is a secure context).
    site.bodies["/xwrite"] = page("document.cookie = 'sid=LOGIN1; path=/; SameSite=None; Secure'")
    site.default_body = page(f"setTimeout(() => {{ const f = document.createElement('iframe');"
                             f" f.src = '{site.other}/xwrite'; document.body.append(f); }}, 200)")
    async with server() as srv:
        await first_retry(srv, site)
        await plant(srv, site, "sid", "LOGIN1")  # the site's own (unobserved) login cookie, same name/value
        for _ in range(30):
            if any(c["name"] == "sid" and c["domain"] == "localhost" for c in await srv._page.context.cookies()):
                break
            await asyncio.sleep(0.1)
        assert any(c["name"] == "sid" and c["domain"] == "localhost" for c in await srv._page.context.cookies()), \
            ("the cross-site frame did not write (invalid trial)", site.received("/xwrite"))
        await settle()
        assert "sid" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


# -- 4. single-rule regressions (each one is the only defense in its trial) -----------


@requires_chromium
async def test_same_value_rewrite_of_a_server_cookie_is_no_visible_change(site):
    async with server() as srv:
        await first_retry(srv, site)
        await plant(srv, site, "sid", "LOGIN1")
        await srv._page.evaluate("document.cookie = 'sid=LOGIN1; path=/; max-age=999999'")  # expiry refresh
        await settle()
        assert "sid" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


@requires_chromium
async def test_only_the_latest_observation_of_a_name_counts(site):
    async with server() as srv:
        await first_retry(srv, site)
        await srv._page.evaluate("document.cookie = 'sid=A1; path=/'; document.cookie = 'sid=B2; path=/'")
        await plant(srv, site, "sid", "A1")  # the server brings back the older script value, unobserved
        await settle()
        assert "sid" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


@requires_chromium
async def test_script_copy_of_a_server_cookie_seen_during_the_tail_is_not_merged(site):
    # B9 variant: a same-tab frame document gets srv=S1 from the server and its script deletes
    # and rewrites the same value (visible changes) — the Set-Cookie seen during the tail excludes it.
    site.bodies["/frame"] = page("document.cookie = 'srv=; path=/; max-age=0'; document.cookie = 'srv=S1; path=/'")
    async with server() as srv:
        await first_retry(srv, site)
        await srv._page.evaluate(SERVER_PATHS["iframe"])
        await settle()
        assert (await jar(srv))["srv"]["value"] == "S1"
        assert "srv" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


@requires_chromium
async def test_other_tab_script_writing_the_same_name_and_value_is_not_merged(site):
    # B3 variant: the other tab's script writes; this tab writes nothing.
    site.bodies["/plain"] = page("document.cookie = 'shared=V1; path=/'")
    async with server() as srv:
        await first_retry(srv, site)
        other = await srv._page.context.new_page()
        await other.goto(site.url + "/plain")
        assert (await jar(srv))["shared"]["value"] == "V1"
        await settle()
        await other.close()
        assert "shared" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


@requires_chromium
async def test_new_main_frame_document_writes_are_not_merged(site):
    site.bodies["/n2"] = page("document.cookie = 'late=L1; path=/'")
    site.default_body = page("setTimeout(() => { location.href = '/n2'; }, 200)")
    async with server() as srv:
        await first_retry(srv, site)
        await settle()
        assert (await jar(srv))["late"]["value"] == "L1"
        assert "late" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


NATIVE_SETTER = ("Function.prototype.toString.call("
                 "Object.getOwnPropertyDescriptor(Document.prototype, 'cookie').set)")


@requires_chromium
@pytest.mark.parametrize("end", ["tool_call", "deadline"])
async def test_after_the_tail_new_documents_have_the_native_setter(site, end, monkeypatch):
    from browser import headless_retry as module

    if end == "deadline":
        monkeypatch.setattr(module, "TAIL_S", 0.6)
    async with server() as srv:
        await first_retry(srv, site)
        assert "[native code]" not in await srv._page.evaluate(NATIVE_SETTER)  # disclosed: observable
        if end == "tool_call":
            assert (await srv.call_tool("browser_scroll", {"direction": "down"})).success
        else:
            await asyncio.sleep(1.5)
        assert srv._headless_retry._tail is None
        await srv._page.goto(site.url + "/plain")  # a later document (same tab)
        assert "[native code]" in await srv._page.evaluate(NATIVE_SETTER)
        frame_setter = await srv._page.evaluate(
            "(() => { const f = document.createElement('iframe'); document.body.append(f);"
            " return Function.prototype.toString.call("
            "Object.getOwnPropertyDescriptor(f.contentWindow.Document.prototype, 'cookie').set); })()")
        assert "[native code]" in frame_setter


@requires_chromium
async def test_writes_after_a_tool_call_are_not_merged(site):
    async with server() as srv:
        await first_retry(srv, site)
        assert (await srv.call_tool("browser_scroll", {"direction": "down"})).success
        await srv._page.evaluate("document.cookie = 'after_tool=1; path=/'")
        await settle()
        assert "after_tool" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


@requires_chromium
async def test_record_overflow_stops_merging(site):
    from browser.headless_retry import OBSERVER_MAX_RECORDS

    async with server() as srv:
        await first_retry(srv, site)
        await srv._page.evaluate(f"for (let i = 0; i <= {OBSERVER_MAX_RECORDS}; i++) "
                                 "document.cookie = 'flood=' + i + '; path=/'")
        await srv._page.evaluate("document.cookie = 'final=1; path=/'")
        await settle()
        assert "flood" not in ledgered(srv, site) and "final" not in ledgered(srv, site)


# -- 5. D: later value changes of an identified (js) key --------------------------


GA_LIKE = ("setTimeout(() => { document.cookie = '_ga_R3=GS1.' + Date.now() + '; path=/; max-age=9999'; }, 300);"
           "addEventListener('pagehide', () => { document.cookie = '_ga_R3=GS2.' + Date.now() + '; path=/; max-age=9999'; });")


@requires_chromium
async def test_d_pagehide_write_and_other_origin_round_trip_keep_the_name(site):
    site.default_body = page(GA_LIKE)
    async with server() as srv:
        await first_retry(srv, site)
        await settle()
        before = (await jar(srv))["_ga_R3"]["value"]
        assert ledgered(srv, site)["_ga_R3"]["js"] is True
        await navigate(srv, site.other + "/other")  # pagehide rewrites _ga_R3 (never observed)
        assert (await jar(srv))["_ga_R3"]["value"] != before
        await assert_renamed(srv, site, "/back")


@requires_chromium
async def test_d_write_twelve_seconds_later_keeps_the_name(site):
    site.default_body = page("setTimeout(() => { document.cookie = '_ga_R3=GS1.1; path=/; max-age=9999'; }, 300);"
                             "setTimeout(() => { document.cookie = '_ga_R3=GS1.12; path=/; max-age=9999'; }, 12000);")
    async with server() as srv:
        await first_retry(srv, site)
        await asyncio.sleep(12.6)
        assert srv._headless_retry._tail is None
        assert (await jar(srv))["_ga_R3"]["value"] == "GS1.12"
        await assert_renamed(srv, site, "/later")


@requires_chromium
async def test_d_unidentified_key_change_after_the_tail_is_refused(site, monkeypatch):
    from browser import headless_retry as module

    monkeypatch.setattr(module, "TAIL_S", 0.6)
    site.default_body = page("setTimeout(() => { document.cookie = 'visitor=rewritten-late; path=/'; }, 1500)")
    async with server() as srv:
        await first_retry(srv, site)
        await asyncio.sleep(2.0)
        assert "js" not in ledgered(srv, site)["visitor"]  # ledgered by the navigate, never identified
        await assert_native(srv, site, "known_value_changed")


@requires_chromium
@pytest.mark.parametrize("change,expected", [("http_only", "known_value_changed"), ("new_name", "new_cookie"),
                                             ("non_js_value", "known_value_changed"), ("js_value", None)])
async def test_d_boundaries(site, change, expected):
    site.default_body = page("setTimeout(() => { document.cookie = '_ga_R3=GS1.1; path=/; max-age=9999'; }, 200)")
    async with server() as srv:
        await first_retry(srv, site)
        await settle()
        assert ledgered(srv, site)["_ga_R3"]["js"] is True
        assert (await srv.call_tool("browser_scroll", {"direction": "down"})).success  # tail over
        if change == "http_only":
            await plant(srv, site, "_ga_R3", "GS1.2", http_only=True)
        elif change == "new_name":
            await plant(srv, site, "session", "S-new")
        elif change == "non_js_value":
            await plant(srv, site, "visitor", "changed-by-login")
        else:
            await plant(srv, site, "_ga_R3", "GS1.2")
        if expected is None:
            await assert_renamed(srv, site)
        else:
            await assert_native(srv, site, expected)


# -- 6. the wrapper changes nothing a page relies on (but is observable — disclosed) ---


BEHAVIOUR = """() => {
  const d = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
  const out = {};
  out.assign_return = (document.cookie = 'b1=1; path=/');
  let n = 0; const o = {toString() { n++; return 'b2=' + n + '; path=/'; }}; document.cookie = o; out.to_string_calls = n;
  out.b2 = document.cookie.includes('b2=1');
  try { document.cookie = Symbol('x'); out.symbol = 'no-throw'; } catch (e) { out.symbol = e.constructor.name + ': ' + e.message; }
  try { d.set.call({}, 'b3=1'); out.illegal = 'no-throw'; } catch (e) { out.illegal = e.constructor.name + ': ' + e.message; }
  try { d.get.call({}); out.illegal_get = 'no-throw'; } catch (e) { out.illegal_get = e.constructor.name + ': ' + e.message; }
  out.getter_native = Function.prototype.toString.call(d.get).includes('[native code]');
  out.getter = document.cookie.includes('b1=1');
  out.length = d.set.length; out.enumerable = d.enumerable; out.configurable = d.configurable;
  out.prototype_prop = 'prototype' in d.set;
  out.globals = Object.getOwnPropertyNames(window).filter((k) => typeof window[k] === 'function' && /^_/.test(k));
  return out; }"""


@requires_chromium
async def test_wrapper_keeps_returns_errors_getter_and_conversions(site):
    async with server() as srv:
        await first_retry(srv, site)
        wrapped = await srv._page.evaluate(BEHAVIOUR)
        cs = await srv._page.evaluate(
            "cookieStore.set('cs1', 'v1').then((r) => typeof r + ':' + document.cookie.includes('cs1=v1'))")
        other = await srv._page.context.new_page()
        await other.goto(site.url + "/plain")
        native = await other.evaluate(BEHAVIOUR)
        native_cs = await other.evaluate(
            "cookieStore.set('cs2', 'v2').then((r) => typeof r + ':' + document.cookie.includes('cs2=v2'))")
        await other.close()
    assert wrapped == native
    assert wrapped["to_string_calls"] == 1 and wrapped["globals"] == []
    assert cs == native_cs.replace("cs2", "cs1") == "undefined:true"


# R4 (B4): cookieStore is no longer wrapped — its writes are not merged
# (tests/interface/test_ws52_r4_attribution.py::test_cookie_store_writes_are_not_observed_or_merged).


# -- 7. unit: observation rules (no browser) --------------------------------------


def _observer(retry, origin="http://example.test"):
    from browser.headless_retry import _Observer

    obs = _Observer(retry=retry, origin=origin, nonce="n0nce")
    obs.contexts[1] = "http://example.test"
    obs.contexts[2] = "http://cross.test"
    obs.contexts[3] = "http://sub.example.test"
    obs.contexts[4] = "http://example.test:8080"
    obs.contexts[5] = "https://example.test"
    return obs


def _payload(added=(), removed=(), nonce="n0nce"):
    return json.dumps({"k": nonce, "a": {str(i): {"n": n, "v": v} for i, (n, v) in enumerate(added)},
                       "r": {str(i): {"n": n, "v": v} for i, (n, v) in enumerate(removed)}})


def test_observer_attributes_records_to_same_origin_contexts_only():
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    obs = _observer(retry)
    obs.binding_called(1, _payload([("a", "1")]))
    obs.binding_called(2, _payload([("b", "2")]))  # cross-site frame
    obs.binding_called(3, _payload([("c", "3")]))  # same site, sub host: another origin (R4, B3)
    obs.binding_called(4, _payload([("e", "5")]))  # another port
    obs.binding_called(5, _payload([("f", "6")]))  # another scheme
    obs.binding_called(9, _payload([("d", "4")]))  # unknown / destroyed context
    assert set(obs.latest) == {"a"} and not obs.broken


def test_observer_needs_the_nonce_and_a_well_formed_payload():
    from browser.headless_retry import OBSERVER_MAX_PAYLOAD, HeadlessRetry

    retry = HeadlessRetry()
    obs = _observer(retry)
    for forged in (_payload([("a", "1")], nonce="guess"), "not json", "[]", json.dumps({"a": {}, "r": {}})):
        obs.binding_called(1, forged)
    assert obs.latest == {} and not obs.broken  # calls without the nonce are ignored
    for bad in (json.dumps({"k": "n0nce", "a": [["a", "1"]], "r": {}}),
                json.dumps({"k": "n0nce", "a": {"0": {"n": 1, "v": "x"}}, "r": {}}),
                json.dumps({"k": "n0nce", "a": {"0": {"n": "a"}}, "r": {}}),
                _payload([("a", "x" * OBSERVER_MAX_PAYLOAD)])):
        broken = _observer(retry)
        broken.binding_called(1, bad)
        assert broken.broken and broken.latest == {}


def test_observer_latest_value_deletion_and_multi_name_writes():
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    obs = _observer(retry)
    obs.binding_called(1, _payload([("a", "1")]))
    obs.binding_called(1, _payload([("a", "2")], [("a", "1")]))
    assert obs.latest["a"] == retry._digest("2")
    obs.binding_called(1, _payload([], [("a", "2")]))
    assert obs.latest["a"] is None  # deleted: nothing to merge
    obs.binding_called(1, _payload([("b", "1"), ("c", "1")]))  # one write cannot set two cookies
    assert obs.latest["b"] is None and obs.latest["c"] is None


def test_observer_record_cap():
    from browser.headless_retry import OBSERVER_MAX_RECORDS, HeadlessRetry

    retry = HeadlessRetry()
    obs = _observer(retry)
    for index in range(OBSERVER_MAX_RECORDS):
        obs.binding_called(1, _payload([("a", str(index))]))
    assert not obs.broken
    obs.binding_called(1, _payload([("a", "over")]))
    assert obs.broken


def test_observer_set_cookie_lines():
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    obs = _observer(retry)
    obs.response_headers({"Set-Cookie": "a=1; Path=/\nb = 2 ; HttpOnly", "content-type": "text/html"})
    obs.response_headers({"set-cookie": "c=3"})
    assert obs.server == {("a", retry._digest("1")), ("b", retry._digest("2")), ("c", retry._digest("3"))}


def test_ledger_file_accepts_js_marks_and_old_entries(tmp_path):
    from browser.headless_retry import HeadlessRetry

    retry = HeadlessRetry()
    retry.load(tmp_path / "headless_retry.json")
    entry = {"name": "_ga", "domain": "example.test", "path": "/", "value_digest": retry._digest("1")}
    retry.commit_success("http://example.test", [], [entry])
    retry._absorb_identified("http://example.test", [dict(entry, value_digest=retry._digest("2"))])
    again = HeadlessRetry()
    again.load(tmp_path / "headless_retry.json")
    assert again.ledger["http://example.test"] == [dict(entry, value_digest=retry._digest("2"), js=True)]
    raw = json.loads((tmp_path / "headless_retry.json").read_text())
    raw["origins"]["http://example.test"]["cookies"][0]["js"] = "yes"  # only true is a mark
    (tmp_path / "headless_retry.json").write_text(json.dumps(raw))
    broken = HeadlessRetry()
    broken.load(tmp_path / "headless_retry.json")
    assert broken.ledger == {}
