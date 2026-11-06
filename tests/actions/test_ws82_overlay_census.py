"""WS-82(#198) 측정 B1-a: 감지가 꺼진 쪽 관찰 봉투가 감지를 끈 관찰(= dev 동작)과 바이트 동일 — mock 25 + ws62 corpus 전 쪽.

같은 페이지에서 (감지 켬, 감지 끔, 감지 끔) 세 번 관찰한다. 끔 두 번이 같은(정적인) 쪽에서만 바이트 비교를 한다(광고 로테이션처럼
스스로 바뀌는 쪽은 감지 상태만 본다 — 몇 쪽인지 보고). 감지가 켜진 쪽은 이름만 모아 보고하고, mock 25 는 모두 꺼져야 한다(리뷰 F3:
mock 사이트에는 오버레이가 없다). 모든 시험은 로컬(set_content·로컬 Mock 서버, 외부 요청 차단).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import pytest

from contracts import ActionType

from test_scroll_navigation import requires_chromium  # noqa: F401
import ws62_corpus as corpus

FAMILIES = ("captures", "prior", "corpus", "independent", "symmetric", "symname", "b45")


def _env(r) -> str:
    from interface.server_envelope import envelope_json

    c = r.model_copy(deep=True)
    c.data.pop("latency_ms", None)
    return envelope_json(c)


async def _triple(page, args: Dict[str, Any]) -> Tuple[Any, str, str, str]:
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async def none(self):
        return None

    on_d = ActionDispatcher(DispatchContext(page=page, engine=PerceptionEngine()))
    on = await on_d.dispatch(ActionType.OBSERVE_PAGE, dict(args))
    outs = []
    for _ in range(2):
        d = ActionDispatcher(DispatchContext(page=page, engine=PerceptionEngine()))
        d._overlay_detect = none.__get__(d)  # 이 디스패처만 감지 끔(= dev)
        outs.append(_env(await d.dispatch(ActionType.OBSERVE_PAGE, dict(args))))
    return on, _env(on), outs[0], outs[1]


class Tally:
    def __init__(self) -> None:
        self.pages = 0
        self.compared = 0
        self.unstable: List[str] = []
        self.on: List[str] = []
        self.mismatch: List[str] = []

    def add(self, name: str, on, e_on: str, off1: str, off2: str) -> None:
        self.pages += 1
        if "overlay" in on.data:
            self.on.append(name)
            return
        if off1 != off2:
            self.unstable.append(name)
            return
        self.compared += 1
        if e_on != off1:
            self.mismatch.append(name)

    def line(self) -> str:
        return (f"pages={self.pages} compared={self.compared} unstable={len(self.unstable)} on={len(self.on)} "
                f"mismatch={len(self.mismatch)} on_pages={self.on[:10]} unstable_pages={self.unstable[:5]}")


@requires_chromium
async def test_mock_sites_detection_off_and_bytes_identical():
    from playwright.async_api import async_playwright

    from harness.mock_sites import MOCK_SITES, MockServer

    t = Tally()
    with MockServer() as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                for site in MOCK_SITES:
                    ctx = await browser.new_context(viewport={"width": 1280, "height": 720})
                    try:
                        page = await ctx.new_page()
                        await page.goto(server.site_url(site.site_id), wait_until="domcontentloaded")
                        await page.wait_for_timeout(100)
                        for args in ({}, {"force_full_tree": True, "prune_top_n": 2000}):
                            t.add(f"{site.site_id}:{'full' if args else 'top'}", *await _triple(page, args))
                    finally:
                        await ctx.close()
            finally:
                await browser.close()
    print(f"\nWS82-B1 mock {t.line()}")
    assert len(MOCK_SITES) >= 25 and t.pages == 2 * len(MOCK_SITES)
    assert not t.on, t.on  # mock 사이트에는 오버레이가 없다 — 켜지면 오탐
    assert not t.mismatch, t.mismatch
    assert t.compared >= 40, t.line()  # 측정 조건: 대부분 정적 — 바이트 비교가 실제로 돈다


@requires_chromium
@pytest.mark.parametrize("family", FAMILIES)
async def test_ws62_corpus_detection_off_side_bytes_identical(family):
    from playwright.async_api import async_playwright

    t = Tally()
    pages = corpus.family_pages(family)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for name, html in pages:
                ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
                try:
                    await ctx.route("**/*", lambda route: route.abort())
                    page = await ctx.new_page()
                    await page.set_content(html)
                    t.add(name, *await _triple(page, {}))
                finally:
                    await ctx.close()
        finally:
            await browser.close()
    print(f"\nWS82-B1 corpus:{family} {t.line()}")
    assert t.pages == len(pages) and t.compared, t.line()
    assert not t.mismatch, json.dumps(t.mismatch[:10], ensure_ascii=False)
