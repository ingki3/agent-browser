"""WS-40 behavior through MCP call_tool, Chromium and original loopback pages."""

from __future__ import annotations

import json

import pytest

from contracts import ActionType, ErrorCode
from harness.ws40_mock import StructuredMockServer
from interface.mcp_server import BrowserMCPServer, create_server
from test_run_cli import requires_chromium


@pytest.fixture(scope="module")
def site():
    with StructuredMockServer() as mock:
        yield mock.base_url


async def call(server, action, **args):
    return await server.call_tool("browser_" + action, args)


async def missing(server):
    return await call(server, "extract", selector="#missing")


@requires_chromium
async def test_missing_hint_once_and_observation_suppresses_it(site):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + "/product")
        first = await missing(server)
        assert not first.success and first.error_code is ErrorCode.ELEMENT_NOT_FOUND
        assert "browser_observe_page" in first.data["hint"]
        assert "element_id" in first.data["hint"] and len(first.data["hint"]) <= 100
        assert "hint" not in (await missing(server)).data
        await call(server, "navigate", url=site + "/product")
        assert "hint" in (await missing(server)).data
        await call(server, "navigate", url=site + "/product")
        assert (await call(server, "observe_page")).success
        assert "hint" not in (await missing(server)).data


@requires_chromium
@pytest.mark.parametrize("selector,all_items,want_hint", [
    ("#normal", False, False), (".poor", True, True), ("#short", False, True),
    ("#boundary", False, False), ("#normal, .poor", True, True),
])
async def test_sparse_extraction_thresholds(site, selector, all_items, want_hint):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + "/product")
        result = await call(server, "extract", selector=selector, extract_all=all_items)
        assert result.success
        assert ("hint" in result.data) is want_hint
        if not want_hint:
            assert "hint" in (await missing(server)).data, "normal success must not consume hint"


@requires_chromium
async def test_observed_sparse_extraction_has_no_hint(site):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + "/product")
        await call(server, "observe_page")
        assert "hint" not in (await call(server, "extract", selector=".poor", extract_all=True)).data


@requires_chromium
async def test_reload_back_and_external_url_change_reset_observation(site):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + "/product")
        await call(server, "observe_page")
        await call(server, "reload")
        assert "hint" in (await missing(server)).data
        await call(server, "navigate", url=site + "/flight")
        await call(server, "observe_page")
        await call(server, "go_back")
        assert "hint" in (await missing(server)).data
        await call(server, "observe_page")
        await server._dispatcher.ctx.page.evaluate("history.pushState({}, '', '/route')")
        assert "hint" in (await missing(server)).data
        await call(server, "observe_page")
        await server._dispatcher.ctx.page.reload()
        assert "hint" in (await missing(server)).data


@requires_chromium
async def test_tab_switch_resets_target_but_list_does_not(site):
    async with BrowserMCPServer(recipes=False) as server:
        nav = await call(server, "navigate", url=site + "/product")
        await call(server, "observe_page")
        await call(server, "tab_control", command="list")
        assert "hint" not in (await missing(server)).data
        await call(server, "tab_control", command="create", url=site + "/flight")
        assert "hint" in (await missing(server)).data
        await call(server, "tab_control", command="switch", tab_id=nav.tab_id)
        assert "hint" in (await missing(server)).data


@requires_chromium
async def test_ambiguous_selector_failure_gets_hint(site):
    async with BrowserMCPServer(recipes=False, nav_settle=False) as server:
        await call(server, "navigate", url=site + "/product")
        result = await call(server, "click", selector=".duplicate")
        assert not result.success
        assert "hint" in result.data


@requires_chromium
async def test_product_graph_offers_origin_and_rating(site):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + "/product")
        result = await call(server, "observe_page")
    assert result.success
    product = next(x for x in result.data["page_data"] if x["@type"] == "Product")
    assert product["name"] == "Mock Laptop" and product["sku"] == "MOCK-40"
    assert product["brand"] == "Mock Computing"
    assert str(product["offers"][0]["price"]) == "3340000"
    assert product["offers"][0]["priceCurrency"] == "KRW"
    assert product["offers"][0]["url"] == site + "/buy"
    assert product["offers"][1]["lowPrice"] == "3200000"
    assert "url" not in product["offers"][1]
    assert str(product["aggregateRating"]["ratingValue"]) == "4.8"
    assert "other.test" not in json.dumps(result.data["page_data"])


@requires_chromium
async def test_array_flight_article_event_and_breadcrumb_names(site):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + "/flight")
        result = await call(server, "observe_page")
    data = {x["@type"]: x for x in result.data["page_data"]}
    assert data["Flight"]["offers"][0]["priceCurrency"] == "KRW"
    assert data["Flight"]["offers"][0]["url"] == site + "/book"
    assert data["NewsArticle"]["name"] == "Mock flight announcement"
    assert data["Event"]["name"] == "Mock Travel Fair"
    assert data["BreadcrumbList"]["names"] == ["Travel", "Flights"]
    assert "other.test" not in json.dumps(data)


@requires_chromium
@pytest.mark.parametrize("path", ["/none", "/broken"])
async def test_absent_or_broken_json_omits_page_data(site, path):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + path)
        result = await call(server, "observe_page")
    assert result.success and "page_data" not in result.data


@requires_chromium
@pytest.mark.parametrize("path", ["/huge", "/many", "/nested", "/controls"])
async def test_bounded_clean_summary_and_nested_traversal(site, path):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + path)
        result = await call(server, "observe_page")
    assert result.success
    data = result.data["page_data"]
    assert 0 < len(data) <= 5
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    text.encode("utf-8")  # A length cap must not leave half of an emoji surrogate pair.
    assert len(text) <= 1200
    assert all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in text)
    assert max(len(x.get("name", "")) for x in data) <= 128
    assert all("\ufffd" not in x.get("name", "") for x in data)
    if path in ("/huge", "/nested", "/controls"):
        assert data[0]["name"] == "Mock Laptop"


@requires_chromium
async def test_standalone_offer_urls_are_same_origin_only(site):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + "/urls")
        result = await call(server, "observe_page")
    data = result.data["page_data"]
    assert len(data) == 5
    assert [x.get("url") for x in data] == [site + "/buy", site + "/same", None, None, None]


@requires_chromium
async def test_structured_text_is_scanned_for_injection(site):
    async with BrowserMCPServer(recipes=False) as server:
        await call(server, "navigate", url=site + "/injection")
        result = await call(server, "observe_page")
    signal = result.data["injection_suspected"]
    assert "prior_instruction_override_en" in signal["patterns"]
    assert "page_data[0].name" in signal["where"]


@pytest.mark.parametrize("recipes", [True, False])
def test_initialize_instructions_reach_sdk_client(recipes):
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async def run():
        server, backend = create_server(recipes=recipes)
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(ss[0], ss[1], server.create_initialization_options()))
                async with ClientSession(*cs) as session:
                    result = await session.initialize()
                tg.cancel_scope.cancel()
        await backend.close()
        return result.instructions

    instructions = anyio.run(run)
    assert "페이지를 열면 먼저 browser_observe_page" in instructions
    assert "data.page_data" in instructions
    assert ("browser_recipe" in instructions) is recipes
