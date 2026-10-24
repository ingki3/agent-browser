"""WS-64 R2-4 — 레시피 재생이 직접 조작할 사이트 단계에서 멈춘다: 승인 판정보다 먼저, 실패로 세지 않음(꺼지지 않음).

실제 Chromium + 로컬 Mock(harness.recipe_mock). 기록은 목록 없는 서버, 재생은 같은 프로필 + 목록 서버.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from contracts import ActionType
from harness.recipe_mock import RecipeSite
from interface.mcp_server import BrowserMCPServer
from security.manual_sites import ManualSites

from test_ws38_server import call, record_first_article, recipe


@pytest.fixture
def site():
    with RecipeSite() as s:
        yield s


@pytest.mark.asyncio
async def test_replay_stops_manual_site_without_counting_failure(site):
    async with BrowserMCPServer(headless=True, profile="mh") as srv:
        rid = (await record_first_article(srv, site))["recipe"]["id"]
    entry = urlsplit(site.url("/list")).netloc
    async with BrowserMCPServer(headless=True, profile="mh", manual_sites=ManualSites.of([entry])) as srv2:
        for _ in range(4):  # 연속 실패 3회면 disabled — manual_site 는 세지 않는다
            await call(srv2, ActionType.NAVIGATE, url=site.url("/list"))
            site.clear_log()
            out = await recipe(srv2, op="run", id=rid)
            assert not out["success"]
            info = out["data"]["recipe"]
            assert info["reason"] == "manual_site", info
            assert info["disabled"] is False
            assert out["data"]["step_result"]["data"]["handoff"]["reason"] == "manual_site"
            assert site.opened("/item") == []  # 누르지 않았다
        listed = await recipe(srv2, op="list")
        entry_row = next(r for r in listed["data"]["recipes"] if r["id"] == rid)
        assert entry_row["disabled"] is False


def test_manual_stop_checked_before_approval():
    """_is_approval 은 HITL_UNATTENDED_BLOCKED 를 승인 필요로 본다 — manual_site 가 먼저여야 한다(소스 순서 고정)."""
    import inspect

    from recipes import replay

    src = inspect.getsource(replay.run_recipe)
    assert src.index("is_manual_handoff") < src.index("_is_approval(result)")
    assert "manual_site" in replay.REASONS
