"""WS-52 R4: narrower attribution of observed script writes (R3 verification B2·B3·B4).

- B2: a record is made only when the ``document.cookie`` setter's receiver is the very
  Document the wrapper was installed in — a popup that borrows the opener's wrapped
  setter for its own document is not the opener tab's write.
- B3: a record counts only from an execution context of the **same origin** (scheme,
  host, port) as the retried origin. Parent/child host strings are no same-site proof
  (``github.io``/``evil.github.io``, ``co.uk``/``evil.co.uk`` are cross-site); a sub-domain
  frame of the same site is no longer merged either (fewer merges, README).
- B4: ``CookieStore.prototype.set/delete`` are not wrapped (a rejection handler on the
  returned Promise would swallow ``unhandledrejection``); cookieStore writes are not
  observed, so they are never merged.

Local loopback sites and offline-fulfilled URLs only (no request leaves the browser for
``*.github.io``/``*.co.uk``/``*.example.test``); synthetic cookie values.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from test_run_cli import requires_chromium
from test_ws52_r3_script_writes import (assert_native, assert_renamed, first_retry, jar, ledgered, page,
                                        r3_site, server, settle, site)  # noqa: F401 - fixture

# -- B2: a popup borrowing the opener's wrapped setter --------------------------------

BORROW = {
    "document_cookie_set": "opener.Object.getOwnPropertyDescriptor(opener.Document.prototype, 'cookie')"
                           ".set.call(document, 'sid=LOGIN1; path=/')",
    "cookie_store_set": "opener.CookieStore.prototype.set.call(cookieStore, 'sid', 'LOGIN1')",
}


@requires_chromium
@pytest.mark.parametrize("api", sorted(BORROW))
async def test_b2_popup_borrowing_the_openers_wrapper_is_not_the_openers_write(site, api):
    site.bodies["/plain"] = page("window.borrowed = false; Promise.resolve(" + BORROW[api]
                                 + ").then(() => { window.borrowed = true; });")
    async with server() as srv:
        await first_retry(srv, site)
        async with srv._page.expect_popup() as info:
            await srv._page.evaluate("window.open('/plain')")
        popup = await info.value
        await popup.wait_for_load_state()
        await popup.wait_for_function("window.borrowed === true")
        assert (await jar(srv))["sid"]["value"] == "LOGIN1", "the popup did not write (invalid trial)"
        await settle()
        await popup.close()
        assert "sid" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")


@requires_chromium
async def test_b2_the_installed_document_is_still_observed_through_call(site):
    # The receiver check must not drop the tab's own writes through an explicit receiver.
    async with server() as srv:
        await first_retry(srv, site)
        await srv._page.evaluate("Object.getOwnPropertyDescriptor(Document.prototype, 'cookie')"
                                 ".set.call(document, '_ga_R4=GS1.1; path=/')")
        await settle()
        assert ledgered(srv, site)["_ga_R4"]["js"] is True
        await assert_renamed(srv, site)


# -- B3 / same origin only: offline-fulfilled frames ---------------------------------


async def _frame_trial(parent: str, child: str, child_js: str, server_cookie: str | None) -> dict:
    """Retried ``parent`` document + a ``child`` iframe whose script runs ``child_js``.

    ``server_cookie`` (a Set-Cookie value) is given to ``parent`` by another tab's real
    response — invisible to the observed CDP session. Returns the eligibility and the
    status of the next visit (403 to the native name, 200 to the renamed one).
    """
    from playwright.async_api import async_playwright

    from browser.headless_retry import HeadlessRetry

    root = "https://" + parent
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()

        async def route(handle):
            url = handle.request.url
            if url == root + "/set" and server_cookie:
                await handle.fulfill(status=200, content_type="text/html", body="<title>set</title>",
                                     headers={"Set-Cookie": server_cookie})
            elif url == "https://" + child + "/frame":
                await handle.fulfill(status=200, content_type="text/html",
                                     body="<script>" + child_js + "; window.done = true</script>")
            elif url == root + "/next":
                native = "HeadlessChrome" in (await handle.request.all_headers()).get("user-agent", "")
                await handle.fulfill(status=403 if native else 200, content_type="text/html", body="<title>n</title>")
            else:
                await handle.fulfill(status=200, content_type="text/html", body="<title>offline</title>")

        await context.route("**/*", route)
        tab = await context.new_page()
        await tab.goto(root + "/one")
        retry = HeadlessRetry()
        retry.commit_success(root, [], [])
        try:
            with retry.tool_call(False):
                assert await retry.start_tail(tab, root + "/one", root, lambda: True)
            if server_cookie:
                other = await context.new_page()
                await other.goto(root + "/set")
                await other.close()
            await tab.evaluate("(u) => { const f = document.createElement('iframe'); f.src = u;"
                               " document.body.append(f); }", "https://" + child + "/frame")
            frame = None
            for _ in range(50):
                frame = next((f for f in tab.frames if f.url.startswith("https://" + child)), None)
                if frame is not None and await frame.evaluate("window.done === true"):
                    break
                await asyncio.sleep(0.05)
            assert frame is not None and await frame.evaluate("window.done === true"), "no child frame (invalid trial)"
            await settle()
            jar_names = [c["name"] for c in await context.cookies(root)]
            ledger = retry.ledger.get(root)
            eligible = await retry.cookies_cleared(context, root + "/next")
            await retry.end_tail()
            if eligible:
                class Robots:
                    async def allowed(self, *args):
                        return True

                original = await tab.evaluate("navigator.userAgent")
                async with retry.present(tab, root + "/next", original, Robots(), None, None):
                    response = await tab.goto(root + "/next")
            else:
                response = await tab.goto(root + "/next")
            return {"eligible": eligible, "status": response.status, "jar": jar_names, "ledger": ledger}
        finally:
            await retry.aclose()
            await context.close()
            await browser.close()


@requires_chromium
@pytest.mark.parametrize("http_only", [False, True], ids=["script_visible", "http_only"])
@pytest.mark.parametrize("parent,child", [("github.io", "evil.github.io"), ("co.uk", "evil.co.uk")])
async def test_b3_public_suffix_child_frame_is_cross_site(parent, child, http_only):
    cookie = "sid=LOGIN1; Path=/; Secure; SameSite=None" + ("; HttpOnly" if http_only else "")
    result = await _frame_trial(parent, child, "document.cookie = " + json.dumps(cookie), cookie)
    assert "sid" in result["jar"], ("the server cookie never arrived (invalid trial)", result)
    print("B3=" + json.dumps({"parent": parent, "eligible": result["eligible"], "status": result["status"]}))
    assert result["ledger"] is None, result  # WS-52 invariant: the cross-site write is never merged
    if http_only:
        assert result["eligible"] is False and result["status"] == 403, result
    else:
        # WS-58 (intended change): a non-HttpOnly cookie no longer keeps the native name.
        assert result["eligible"] is True and result["status"] == 200, result


@requires_chromium
async def test_same_site_sub_domain_frame_write_is_no_longer_merged():
    # Impact of same-origin only: sub.example.test writes a cookie visible to example.test.
    # R3 merged it (parent/child host); R4 refuses — the next visit honestly reports it.
    result = await _frame_trial("example.test", "sub.example.test",
                                "document.cookie = 'pref=dark; Domain=example.test; Path=/; Secure'", None)
    assert "pref" in result["jar"], ("the sub-domain frame did not write (invalid trial)", result)
    assert result["ledger"] is None, result  # WS-52 R4 invariant: not merged
    # WS-58 (intended change): the non-HttpOnly cookie no longer keeps the native name.
    assert result["eligible"] is True and result["status"] == 200, result


@requires_chromium
async def test_same_origin_frame_write_is_merged(site):
    site.bodies["/plain"] = page("document.cookie = '_ga_FR=GS1.1; path=/; max-age=9999'")
    async with server() as srv:
        await first_retry(srv, site)
        await srv._page.evaluate("(() => { const f = document.createElement('iframe'); f.src = '/plain';"
                                 " document.body.append(f); })()")
        for _ in range(30):
            if "_ga_FR" in await jar(srv):
                break
            await asyncio.sleep(0.1)
        await settle()
        assert ledgered(srv, site)["_ga_FR"]["js"] is True
        await assert_renamed(srv, site)


def test_same_origin_needs_scheme_host_and_port():
    from browser.headless_retry import _same_origin

    assert _same_origin("http://127.0.0.1:8000", "http://127.0.0.1:8000")
    assert _same_origin("https://example.test", "https://example.test:443")
    for other in ("http://127.0.0.1:8001", "https://127.0.0.1:8000", "http://localhost:8000",
                  "http://sub.127.0.0.1:8000", "null", ""):
        assert not _same_origin(other, "http://127.0.0.1:8000"), other
    for parent, child in (("github.io", "evil.github.io"), ("co.uk", "evil.co.uk"),
                          ("example.test", "sub.example.test")):
        assert not _same_origin("https://" + child, "https://" + parent)
        assert not _same_origin("https://" + parent, "https://" + child)


# -- B4: cookieStore is not wrapped -------------------------------------------------

REJECTIONS = """async () => {
  const seen = [];
  const on = (e) => { seen.push(e.reason.name + ': ' + e.reason.message); e.preventDefault(); };
  window.addEventListener('unhandledrejection', on);
  cookieStore.set({name: 'bad;name', value: 'x'});
  await new Promise((r) => setTimeout(r, 200));
  window.removeEventListener('unhandledrejection', on);
  return seen; }"""

NATIVE_STORE = ("['set', 'delete'].map((m) => Function.prototype.toString.call(CookieStore.prototype[m])"
                ".includes('[native code]'))")


@requires_chromium
async def test_b4_unhandled_rejection_is_delivered_as_natively(site):
    async with server() as srv:
        await first_retry(srv, site)
        wrapped = await srv._page.evaluate(REJECTIONS)
        store = await srv._page.evaluate(NATIVE_STORE)
        other = await srv._page.context.new_page()
        await other.goto(site.url + "/plain")
        native = await other.evaluate(REJECTIONS)
        await other.close()
    assert len(native) == 1, ("no native rejection (invalid trial)", native)
    assert wrapped == native
    assert store == [True, True]


@requires_chromium
async def test_cookie_store_writes_are_not_observed_or_merged(site):
    async with server() as srv:
        await first_retry(srv, site)
        await srv._page.evaluate("cookieStore.set({name: 'cs_pref', value: 'dark', path: '/'})")
        await settle()
        assert (await jar(srv))["cs_pref"]["value"] == "dark"
        assert "cs_pref" not in ledgered(srv, site)
        await assert_native(srv, site, "new_cookie")
