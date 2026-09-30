"""WS-30b 항목 4(a): challenge vendor 오탐 — 문구만으로 vendor 를 단정하지 않는다.

비교 시험: 로컬 목업 "보안 확인을 완료해 주세요" 가 `vendor:"naver"` 로 판정돼 에이전트가
사용자에게 "네이버 캡차" 라고 전했다. 사이트 고유 문구(네이버)는 **페이지 도메인이 그 벤더일
때만** vendor 를 채우고, 아니면 `generic`. kind·reason 은 그대로.

Cloudflare·Akamai 문구는 사이트가 아니라 앞단(CDN) 벤더의 화면이라 어느 도메인에서나 나온다
(G마켓의 Cloudflare 화면 실측) — 도메인 확인 대상이 아니다.

`classify` 는 MCP(`_attach_challenge`)와 `run` 루프(`agent.loop`)가 공유한다 — 두 경로 모두
회귀로 확인한다(루프 쪽은 tests/agent/test_loop_challenge.py, MCP 쪽은 tests/interface).
실제 네이버에는 접속하지 않는다 — 네이버 주소는 page.route 로 가짜 응답을 준다.
"""

from __future__ import annotations

import pytest

from browser.challenge import ChallengeKind, classify, detect_challenge, vendor_domain_matches

from test_challenge import NAVER_LIMIT, NAVER_SECURITY, requires_chromium  # noqa: F401

SEC = "보안 확인을 완료해 주세요"
LIMIT = "쇼핑 서비스 접속이 일시적으로 제한되었습니다."


@pytest.mark.parametrize(
    "url,vendor",
    [
        ("https://search.shopping.naver.com/search/all?q=x", "naver"),
        ("https://nid.naver.com/nidlogin.login", "naver"),
        ("https://naver.com/", "naver"),
        ("http://127.0.0.1:8080/x/captcha", "generic"),
        ("https://evil-naver.com/", "generic"),
        ("https://naver.com.evil.test/", "generic"),
        ("about:blank", "generic"),
        ("", "generic"),
        (None, "generic"),
    ],
)
def test_naver_phrase_vendor_depends_on_domain(url, vendor):
    for text, kind in ((SEC, ChallengeKind.CAPTCHA), (LIMIT, ChallengeKind.BLOCKED)):
        got = classify("t", text, len(text), "", None, url=url)
        assert got.kind is kind
        assert got.vendor == vendor, (url, text)
        assert got.reason in ("네이버 보안 확인 화면", "네이버 접속 일시 제한"), "reason 유지"


def test_cdn_vendor_phrases_are_domain_independent():
    got = classify("Just a moment...", "x", 10, "", None, url="https://www.gmarket.co.kr/")
    assert got.vendor == "cloudflare"
    got = classify("Access Denied", "access denied reference #1", 30, "", None, url="http://127.0.0.1/")
    assert got.vendor == "akamai"


def test_vendor_domain_matches():
    assert vendor_domain_matches("naver", "https://m.naver.com/")
    assert not vendor_domain_matches("naver", "https://naver.co.evil/")
    assert vendor_domain_matches("cloudflare", "http://anything/")  # 도메인 무관 벤더


@requires_chromium
@pytest.mark.parametrize("html", [NAVER_SECURITY, NAVER_LIMIT], ids=["security", "limit"])
async def test_detect_uses_page_url(html):
    """detect_challenge 는 페이지 주소로 판정한다(가짜 네이버 주소는 route 로 로컬 응답)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page()
        await page.route("https://search.shopping.naver.com/**",
                         lambda r: r.fulfill(status=200, content_type="text/html; charset=utf-8",
                                             body=html))
        await page.goto("https://search.shopping.naver.com/search/all?q=x")
        on_naver = await detect_challenge(page)
        other = await b.new_page()
        await other.set_content(html)  # about:blank — 네이버 도메인이 아님
        local = await detect_challenge(other)
        await b.close()
    assert on_naver.vendor == "naver"
    assert local.vendor == "generic"
    assert on_naver.kind is local.kind is not ChallengeKind.NONE
    assert on_naver.reason == local.reason


@requires_chromium
async def test_mcp_path_vendor_by_domain():
    """MCP 경로(_attach_challenge): 네이버 주소면 naver, 로컬 목업이면 generic — kind·reason 같다."""
    from contracts import ActionType
    from interface.mcp_server import BrowserMCPServer, tool_name

    async with BrowserMCPServer() as server:
        await server._page.context.route(  # noqa: SLF001 - 실제 네이버에 가지 않는다
            "https://nid.naver.com/**",
            lambda r: r.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body=NAVER_SECURITY),
        )
        naver = await server.call_tool(tool_name(ActionType.NAVIGATE),
                                       {"url": "https://nid.naver.com/nidlogin.login"})
        await server._page.goto("about:blank")  # noqa: SLF001
        await server._page.set_content(NAVER_SECURITY)  # noqa: SLF001
        local = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
    assert naver.data["challenge"]["vendor"] == "naver"
    assert local.data["challenge"]["vendor"] == "generic"
    assert naver.data["challenge"]["kind"] == local.data["challenge"]["kind"] == "captcha"
    assert naver.data["challenge"]["reason"] == local.data["challenge"]["reason"]
