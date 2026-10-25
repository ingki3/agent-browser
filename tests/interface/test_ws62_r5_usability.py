"""WS-62 R5 사용성(R12 개정) — 실제 MCP 서버 + 로컬 HTTP + Chromium.

흐름: 관찰 → 다음달 → 가는 날 12/19 선택(오는 날 표시가 '오는 날' 로 돌아감 = 미선택) → 재관찰 → 다음달 두 번
→ 2027년 1월 6일. R12(사용자 결정 B): 같은 다음달 id 2회째는 기준대로 거부 → 봇은 hint 대로 다시 관찰해 새 id.
합격: 다음달 클릭당 호출 ≤3, 포기 0, 입력칸 값이 고른 날짜, 응답 ≤ 20,000자.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402
from test_ws62_r4_usability import FULL, MAX_CHARS, Bot  # noqa: E402
from ws62_corpus import page_html  # noqa: E402


@pytest.fixture
def site():
    yield from _serve({"/": page_html(True)})


@requires_chromium
async def test_pending_return_flow_dec19_then_jan6_progress(site, tmp_path):
    from interface.mcp_server import BrowserMCPServer

    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        bot = Bot(s)
        assert (await s.call_tool("browser_navigate", {"url": site + "/"})).success
        obs = await bot.observe(FULL)
        obs, res, _n = await bot.press(obs, "다음달")
        assert res.success
        obs = await bot.observe(FULL)
        obs = await bot.pick(obs, "2026년 12월", "19")
        ret_label = await s._page.text_content("#retset")
        obs = await bot.observe(FULL)   # 오는 날 미선택 상태의 관찰
        per_next = []
        for _ in range(2):
            obs, res, n = await bot.press(obs, "다음달")
            assert res.success, (res.error_code, res.error_message)
            per_next.append(n)
        months = await s._page.evaluate("() => [...document.querySelectorAll('#cal > .inr .header span')].map(x => x.textContent)")
        obs = await bot.observe(FULL)
        await bot.pick(obs, "2027년 1월", "6")
        values = [await s._page.input_value("#dep"), await s._page.input_value("#ret")]
    assert ret_label == "오는 날"
    assert months == ["2027년 1월", "2027년 2월"], (months, bot.refusals)
    assert values == ["2026.12.19", "2027.01.06"], (values, bot.refusals)
    assert all(n <= 3 for n in per_next), per_next
    assert max(bot.sizes) <= MAX_CHARS, bot.sizes
