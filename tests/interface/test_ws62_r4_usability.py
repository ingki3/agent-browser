"""WS-62 R4 사용성 합격선 — 실제 MCP 서버 + 로컬 HTTP + Chromium, 봇이 실제로 부른 순서(2026-10-23 실사용).

시험 자료: tests/fixtures/ws62/hanatour_calendar.html(실제 여행 예약 페이지 구조만 합성). 순서: observe() →
observe(prune 15) → observe(prune 20) → observe(force_full_tree, prune_top_n 50) → 다음달×3 → 이전달 → 12/19·1/6 선택
→ 항공권 검색·TOP. 합격: 거부 0~1회(달 이동 직후 옛 날짜 id 사용만), 다음달·이전달·항공권 검색·TOP 거부 0, repeated 에 두
달 표제와 해당 날짜 id, 관찰 응답 ≤ 20,000자, 입력칸 값이 고른 날짜.
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

FULL = {"force_full_tree": True, "prune_top_n": 50}
MAX_CHARS = 20000


@pytest.fixture
def site():
    yield from _serve({"/": page_html(True), "/nocss": page_html(False)})


def _named(obs, name: str) -> str:
    return next(e["element_id"] for e in obs.data["observation"]["elements"] if e["name"] == name)


def _day(obs, month: str, day: str) -> str:
    for g in obs.data["repeated"]["groups"]:
        if g["group"] == month:
            for m in g["members"]:
                if m["name"].split()[0] == day:
                    return m["id"]
    raise AssertionError(f"{month}/{day} 없음")


@requires_chromium
async def test_bot_sequence_two_dates_with_at_most_one_refusal(site, tmp_path):
    from interface.mcp_server import BrowserMCPServer
    from interface.server_envelope import envelope_json

    refusals: List[Tuple[str, str]] = []
    sizes: List[int] = []

    async def observe(s, args):
        r = await s.call_tool("browser_observe_page", args)
        assert r.success, r.error_message
        sizes.append(len(envelope_json(r)))
        return r

    async def click(s, what: str, eid: str, epoch: int):
        res = await s.call_tool("browser_click", {"element_id": eid, "epoch": epoch})
        if not res.success:
            refusals.append((what, str(res.error_code)))
        return res

    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        assert (await s.call_tool("browser_navigate", {"url": site + "/"})).success
        await observe(s, {})
        await observe(s, {"prune_top_n": 15})
        await observe(s, {"prune_top_n": 20})
        obs = await observe(s, FULL)
        heads = [g["group"] for g in obs.data["repeated"]["groups"]]
        assert heads[:2] == ["2026년 10월", "2026년 11월"], heads
        _day(obs, "2026년 10월", "19"), _day(obs, "2026년 11월", "19")
        epoch = obs.snapshot_epoch
        nxt, prv = _named(obs, "다음달"), _named(obs, "이전달")
        for _ in range(3):
            assert (await click(s, "다음달", nxt, epoch)).success
        assert (await click(s, "이전달", prv, epoch)).success
        months = await s._page.evaluate("() => [...document.querySelectorAll('#cal > .inr .header span')].map(x => x.textContent)")
        assert months == ["2026년 12월", "2027년 1월"]
        # 봇처럼: 마지막 관찰의 묶음에서 고르고, 거부되면 재관찰해 다시 고른다.
        for month, day in (("2026년 12월", "19"), ("2027년 1월", "6")):
            for _attempt in range(3):
                try:
                    eid = _day(obs, month, day)
                except AssertionError:
                    eid = None
                if eid is not None:
                    res = await click(s, f"{month} {day}", eid, obs.snapshot_epoch)
                    if res.success:
                        break
                    assert res.reobserve_required, (res.error_code, res.error_message)
                obs = await observe(s, FULL)
            else:
                raise AssertionError(f"{month} {day} 를 고르지 못함: {refusals}")
        search = await click(s, "항공권 검색", _named(obs, "항공권 검색"), obs.snapshot_epoch)
        top = await click(s, "TOP", _named(obs, "TOP"), obs.snapshot_epoch)
        values = [await s._page.input_value("#dep"), await s._page.input_value("#ret")]
        clicks = await s._page.evaluate("window.clicks")
    assert values == ["2026.12.19", "2027.01.06"], (values, refusals)
    assert search.success and top.success and clicks == ["search", "top"], refusals
    # 거부는 달 이동 직후 옛 날짜 id 한 번까지 — 다음달·이전달·검색·TOP 거부 0
    assert len(refusals) <= 1, refusals
    assert all(what.split()[0].startswith("20") for what, _code in refusals), refusals
    assert max(sizes) <= MAX_CHARS, sizes


def test_server_instructions_say_same_nav_button_id_is_reusable():
    """R1-7: 행 고정(R1-3)·대상 단위 국소성(R2) 시험이 통과한 뒤의 지침 — 같은 다음달 id 재사용 + 날짜는 묶음에서 고르고 확인."""
    from interface.mcp_server import create_server

    server, _backend = create_server(recipes=False)
    instructions = server.create_initialization_options().instructions
    assert "이전/다음 달 같은 버튼은 같은 id 로 다시 눌러도 된다" in instructions
    assert "날짜는 data.repeated 묶음(group 표제)에서 고르고 누른 뒤 입력칸 값으로 확인" in instructions
