"""JSON-LD boundaries with Chromium; no public HTML or remote context loading."""

from __future__ import annotations

import json

import pytest
from playwright.async_api import async_playwright

from harness.ws40_mock import StructuredMockServer
from perception.page_data import summarize_page_data


@pytest.mark.parametrize("scripts,expected", [
    (["{broken", '{"@type":["Thing","Article"],"headline":"Mock article"}'],
     [{"@type": "Article", "name": "Mock article"}]),
    (['null', '42', '"string"', '{"@type":"WebSite","name":"Ignore"}', '{"@type":"Product"}'], []),
    ([json.dumps({"container": {"@graph": [{"@type": "Product", "name": "Nested item"}]}})],
     [{"@type": "Product", "name": "Nested item"}]),
    ([json.dumps([{"@type": "Event", "name": f"Event {i}"} for i in range(12)])],
     [{"@type": "Event", "name": f"Event {i}"} for i in range(5)]),
])
async def test_independent_scripts_supported_types_nesting_and_item_limit(scripts, expected):
    with StructuredMockServer() as mock:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(mock.base_url + "/none")
                await page.evaluate("""scripts => {
                    for (const raw of scripts) {
                        const s = document.createElement('script');
                        s.type = 'application/ld+json'; s.textContent = raw;
                        document.head.append(s);
                    }
                }""", scripts)
                assert await summarize_page_data(page) == expected
            finally:
                await browser.close()


async def test_origin_uses_scheme_host_port_and_rejects_credentials():
    with StructuredMockServer() as mock:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(mock.base_url + "/none")
                urls = [mock.base_url.replace("http:", "https:") + "/x",
                        "http://127.0.0.1:1/x", "http://synthetic@" + mock.base_url[7:] + "/x",
                        "//other.test/x", "/same"]
                payload = [{"@type": "Offer", "price": 1, "url": url} for url in urls]
                await page.evaluate("""raw => {
                    const s = document.createElement('script'); s.type = 'application/ld+json';
                    s.textContent = raw; document.head.append(s);
                }""", json.dumps(payload))
                data = await summarize_page_data(page)
                assert [x.get("url") for x in data] == [None, None, None, None, mock.base_url + "/same"]
            finally:
                await browser.close()
