"""WS-62 R5 사용성 — 실제 MCP 서버 + 로컬 HTTP + Chromium.

흐름: 관찰 → 다음달 → 가는 날 12/19 선택(오는 날 표시가 '오는 날' 로 돌아감 = 미선택) → 재관찰 → 다음달 같은 id 두 번
→ 2027년 1월 6일. 합격: 거부 ≤1(달 이동 직후 옛 날짜 id 사용만), 다음달 거부 0, 입력칸 값이 고른 날짜.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402
from test_ws62_r4_hanatour import page_html  # noqa: E402
from test_ws62_r4_usability import FULL, MAX_CHARS, _day, _named  # noqa: E402


@pytest.fixture
def site():
    yield from _serve({"/": page_html(True)})


@requires_chromium
async def test_pending_return_flow_dec19_then_jan6_at_most_one_refusal(site, tmp_path):
    from interface.mcp_server import BrowserMCPServer
    from interface.server_envelope import envelope_json

    refusals: List[Tuple[str, str]] = []
    sizes: List[int] = []

    async def observe(s):
        r = await s.call_tool("browser_observe_page", FULL)
        assert r.success, r.error_message
        sizes.append(len(envelope_json(r)))
        return r

    async def click(s, what: str, eid: str, epoch: int):
        res = await s.call_tool("browser_click", {"element_id": eid, "epoch": epoch})
        if not res.success:
            refusals.append((what, str(res.error_code)))
        return res

    async def pick(s, obs, month: str, day: str):
        for _attempt in range(3):
            try:
                eid = _day(obs, month, day)
            except AssertionError:
                eid = None
            if eid is not None:
                res = await click(s, f"{month} {day}", eid, obs.snapshot_epoch)
                if res.success:
                    return obs
                assert res.reobserve_required, (res.error_code, res.error_message)
            obs = await observe(s)
        raise AssertionError(f"{month} {day} 를 고르지 못함: {refusals}")

    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        assert (await s.call_tool("browser_navigate", {"url": site + "/"})).success
        obs = await observe(s)
        assert (await click(s, "다음달", _named(obs, "다음달"), obs.snapshot_epoch)).success
        obs = await pick(s, obs, "2026년 12월", "19")
        ret_label = await s._page.text_content("#retset")
        obs = await observe(s)   # 오는 날 미선택 상태의 관찰
        nxt = _named(obs, "다음달")
        for _ in range(2):
            await click(s, "다음달", nxt, obs.snapshot_epoch)
        months = await s._page.evaluate("() => [...document.querySelectorAll('#cal > .inr .header span')].map(x => x.textContent)")
        await pick(s, obs, "2027년 1월", "6")
        values = [await s._page.input_value("#dep"), await s._page.input_value("#ret")]
    assert ret_label == "오는 날"
    assert months == ["2027년 1월", "2027년 2월"], (months, refusals)
    assert values == ["2026.12.19", "2027.01.06"], (values, refusals)
    assert len(refusals) <= 1, refusals
    assert all(what.split()[0].startswith("20") for what, _code in refusals), refusals
    assert max(sizes) <= MAX_CHARS, sizes
