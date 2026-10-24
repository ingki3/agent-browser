"""WS-62 R4 사용성 합격선(R12 개정) — 실제 MCP 서버 + 로컬 HTTP + Chromium, 봇이 실제로 부른 순서(2026-10-23 실사용).

시험 자료: tests/fixtures/ws62/hanatour_calendar.html(실제 여행 예약 페이지 구조만 합성). 순서: observe() →
observe(prune 15) → observe(prune 20) → observe(force_full_tree, prune_top_n 50) → 다음달×3 → 이전달 → 12/19·1/6 선택
→ 항공권 검색·TOP.

R12(사용자 결정 B): 고정·실행 직전 확인은 v1.16.0 원문이라 같은 '다음달' id 2회째는 거부된다. 봇 정책 = 거부 응답의 hint 대로
browser_observe_page → 같은 이름 버튼을 새 id 로. 합격: 다음달 클릭당 호출 ≤3, 포기 0, 날짜는 관찰한 data.repeated 에서, 응답
≤ 20,000자, repeated 에 두 달 표제, 입력칸 값이 고른 날짜.
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
from ws62_corpus import page_html  # noqa: E402

FULL = {"force_full_tree": True, "prune_top_n": 50}
MAX_CHARS = 20000
HINT = ("관찰 뒤 이 버튼이 속한 화면 부분이 바뀌어(data.group_changed.changed) 누르지 않았습니다 — "
        "observe_page 로 다시 관찰한 뒤 같은 버튼을 새 id 로 누르세요.")


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


class Bot:
    """거부 → (hint 대로) browser_observe_page → 같은 이름 버튼 새 id. 호출 수·거부·응답 크기를 센다."""

    def __init__(self, s) -> None:
        self.s = s
        self.calls = 0
        self.refusals: List[Tuple[str, str]] = []
        self.sizes: List[int] = []
        self.changed: List[dict] = []

    async def observe(self, args=None):
        from interface.server_envelope import envelope_json

        r = await self.s.call_tool("browser_observe_page", dict(FULL if args is None else args))
        self.calls += 1
        assert r.success, r.error_message
        self.sizes.append(len(envelope_json(r)))
        return r

    async def click(self, what: str, eid: str, epoch: int):
        res = await self.s.call_tool("browser_click", {"element_id": eid, "epoch": epoch})
        self.calls += 1
        if not res.success:
            self.refusals.append((what, str(res.error_code)))
        return res

    async def press(self, obs, name: str):
        """이름으로 누르기. 거부면 hint 를 확인하고 다시 관찰한 뒤 같은 이름 새 id 로(한 번). (관찰, 결과, 호출 수)."""
        start = self.calls
        res = await self.click(name, _named(obs, name), obs.snapshot_epoch)
        if not res.success:
            assert res.reobserve_required and res.data.get("hint") == HINT, (res.error_code, res.data)
            assert "group_changed" in res.data and "retry_element_id" not in res.data["group_changed"]
            if "changed" in res.data["group_changed"]:
                from interface.server_envelope import envelope_dict

                # 응답 봉투(공개 정제 뒤)에도 그대로 — 'changed' 는 제어 키 이름과 같지만 값이 dict 라 제어 값 판정 밖.
                env = envelope_dict(res)
                assert env["data"]["group_changed"]["changed"] == res.data["group_changed"]["changed"], env["data"]
                self.changed.append(res.data["group_changed"]["changed"])
            obs = await self.observe()
            res = await self.click(name, _named(obs, name), obs.snapshot_epoch)
        return obs, res, self.calls - start

    async def pick(self, obs, month: str, day: str):
        for _attempt in range(3):
            try:
                eid = _day(obs, month, day)
            except AssertionError:
                eid = None
            if eid is not None:
                res = await self.click(f"{month} {day}", eid, obs.snapshot_epoch)
                if res.success:
                    return obs
                assert res.reobserve_required, (res.error_code, res.error_message)
            obs = await self.observe()
        raise AssertionError(f"{month} {day} 를 고르지 못함: {self.refusals}")


@requires_chromium
async def test_bot_sequence_reobserve_after_refusal_completes(site, tmp_path):
    """MCP 흐름(css — 실사용 순서). 무 CSS 진행성은 tests/actions/test_ws62_r12.py(디스패처 경로)."""
    path = "/"
    from interface.mcp_server import BrowserMCPServer

    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        bot = Bot(s)
        assert (await s.call_tool("browser_navigate", {"url": site + path})).success
        await bot.observe({})
        await bot.observe({"prune_top_n": 15})
        await bot.observe({"prune_top_n": 20})
        obs = await bot.observe(FULL)
        heads = [g["group"] for g in obs.data["repeated"]["groups"]]
        assert heads[:2] == ["2026년 10월", "2026년 11월"], heads
        _day(obs, "2026년 10월", "19"), _day(obs, "2026년 11월", "19")
        per_next = []
        for name in ("다음달", "다음달", "다음달", "이전달"):
            obs, res, n = await bot.press(obs, name)
            assert res.success, (name, res.error_code, res.error_message)
            if name == "다음달":
                per_next.append(n)
        months = await s._page.evaluate("() => [...document.querySelectorAll('#cal > .inr .header span')].map(x => x.textContent)")
        assert months == ["2026년 12월", "2027년 1월"]
        # 날짜는 관찰한 data.repeated 묶음에서 고른다(달을 넘긴 뒤 옛 관찰의 날짜 id 는 쓰지 않는다).
        obs = await bot.observe(FULL)
        assert [g["group"] for g in obs.data["repeated"]["groups"]][:2] == ["2026년 12월", "2027년 1월"]
        for month, day in (("2026년 12월", "19"), ("2027년 1월", "6")):
            obs = await bot.pick(obs, month, day)
        obs, search, _n = await bot.press(obs, "항공권 검색")
        obs, top, _n = await bot.press(obs, "TOP")
        values = [await s._page.input_value("#dep"), await s._page.input_value("#ret")]
        clicks = await s._page.evaluate("window.clicks")
    print(f"\nWS62R12-MCP {path} per_next={per_next} refusals={bot.refusals} max_size={max(bot.sizes)}")
    assert values == ["2026.12.19", "2027.01.06"], (values, bot.refusals)
    assert search.success and top.success and clicks == ["search", "top"], bot.refusals
    assert all(n <= 3 for n in per_next), per_next
    assert max(bot.sizes) <= MAX_CHARS, bot.sizes
    # 다음달 거부의 바뀐 부분(순서열) 앞/뒤가 응답에 보인다(조정자 결정 1)
    assert bot.changed and all(c["items"]["before"] != c["items"]["after"] for c in bot.changed if "items" in c), bot.changed


def test_server_instructions_do_not_promise_same_nav_button_id():
    """R12: 같은 다음달 id 재사용 약속(R4 지침)을 지웠다 — 기준(v1.16.0)은 2회째를 거부하고, 거부 응답 hint 가 재관찰을 안내한다."""
    from interface.mcp_server import create_server

    server, _backend = create_server(recipes=False)
    instructions = server.create_initialization_options().instructions
    assert "같은 id 로 다시 눌러도 된다" not in instructions
    assert "data.repeated 의 group" in instructions
