"""WS-73 browser_batch — 실제 구조 fixture(하나투어 캡처 + 재렌더 스크립트)와 멈춤 조건.

모든 시험은 로컬 HTTP + Chromium(공개 사이트 접속 없음 — 캡처의 외부 <link> 는 CSP 로 막는다).
합격선(계획 §9·review-r4): (a) [10/19, 11/6] 묶음 통과·다른 달 클릭 0, (b) 모달 '확인' target_new, (c) SPA 같은 이름 새 단추
멈춤, (d) 이름이 바뀌는 확정 단추는 묶음 3단계에서 멈추고 응답 관찰 id 로 단건 click 완료, 호출 수 묶음 경로 ≤ 3/5.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402
from ws73_pages import (GATES_HTML, IPI_HTML, MANY_HTML, MODAL_HTML, OTHER_HTML, SPA_HTML, TWIN_HTML,  # noqa: E402
                        hana_html)

from contracts import ErrorCode  # noqa: E402

BATCH = "browser_batch"
DEP = {"action": "click", "target": {"role": "link", "name": "19", "group": "2026년 10월"}}
ARR = {"action": "click", "target": {"role": "link", "name": "6", "group": "2026년 11월"}}
OLD_CONFIRM = "2026.10.24(토) ~ 2026.10.28(수) 선택완료"
NEW_CONFIRM = "2026.10.19(월) ~ 2026.11.06(금) 선택완료"
INPUTS_JS = "() => [...document.querySelectorAll('#select_tripstartdate .option_wrap input')].map(x => x.value)"
CLICKS_JS = "() => window.__clicks"
LOG_JS = "() => window.__log"
#: 호출 수 비교 조건(메인 승인): 응답 상한 200,000자 + prune_top_n 800 — 기본 상한(20,000)에서는 날짜·확정 단추가 어떤
#: 관찰에도 실리지 않는다(기존 관찰 한계, 보고서에 기록).
BIG = 200_000
WIDE = {"prune_top_n": 800}


def _click(name: str, role: str = "button", **extra: Any) -> Dict[str, Any]:
    target = {"role": role, "name": name}
    target.update(extra)
    return {"action": "click", "target": target}


@pytest.fixture
def hana():
    yield from _serve({"/": hana_html()})


@pytest.fixture
def pages():
    yield from _serve({"/": GATES_HTML, "/gates": GATES_HTML, "/other": OTHER_HTML, "/modal": MODAL_HTML,
                       "/spa": SPA_HTML, "/twin": TWIN_HTML, "/ipi": IPI_HTML, "/many": MANY_HTML})


def _server(tmp_path: Path, **kw: Any) -> Any:
    from interface.mcp_server import BrowserMCPServer

    kw.setdefault("recipes", False)
    return BrowserMCPServer(handoff_root=tmp_path / "s", **kw)


async def _open(s: Any, url: str) -> None:
    r = await s.call_tool("browser_navigate", {"url": url})
    assert r.success, r.error_message


def _batch_of(out: Dict[str, Any]) -> Dict[str, Any]:
    return out["data"]["batch"]


def _obs_id(out_data: Dict[str, Any], name: str) -> str:
    return next(e["element_id"] for e in out_data["observation"]["elements"] if e["name"] == name)


# ---------------------------------------------------------------------------- (a)~(d) 실제 구조 fixture


@requires_chromium
async def test_fixture_a_batch_two_dates_completes(hana, tmp_path):
    """[10/19, 11/6] 묶음이 끝까지 — 달력 하위 트리가 클릭마다 다시 그려져도(같은 구조) 통과, 다른 달 클릭 0."""
    async with _server(tmp_path) as s:
        await _open(s, hana + "/")
        out = await s.call_server_tool(BATCH, {"steps": [DEP, ARR]})
        clicks = await s._page.evaluate(CLICKS_JS)
        values = await s._page.evaluate(INPUTS_JS)
        rerenders = await s._page.evaluate("() => window.__rerenders")
    assert out["success"] is True, out.get("data", {}).get("batch")
    batch = _batch_of(out)
    assert batch["ran"] == 2 and batch["of"] == 2 and [x["ok"] for x in batch["steps"]] == [True, True]
    assert rerenders == 2, "fixture 가 재렌더를 일으키지 않으면 기준 노드 규칙을 시험하지 못한다"
    assert clicks == [["2026년 10월", 19, 0], ["2026년 11월", 6, 1]]
    assert values == ["2026.10.19(월)", "2026.11.06(금)"]
    assert "앞 단계 효과는 이미" in out["data"]["next"] and "@eN" in out["data"]["next"]
    assert "observation" in out["data"]


@requires_chromium
async def test_fixture_b_modal_confirm_is_target_new(pages, tmp_path):
    """1단계가 띄운 결제 확인 모달의 '확인' 은 묶음 시작 때 없던 노드 → target_new, 모달 단추 안 눌림."""
    async with _server(tmp_path) as s:
        await _open(s, pages + "/modal")
        out = await s.call_server_tool(BATCH, {"steps": [_click("옵션 보기"), _click("확인")]})
        log = await s._page.evaluate(LOG_JS)
    assert out["success"] is False
    b = _batch_of(out)
    assert (b["reason"], b["ran"], b["stopped_at"]) == ("target_new", 1, 1), b
    assert log == ["next"]
    assert any(e["name"] == "확인" for e in out["data"]["observation"]["elements"])  # 에이전트가 보고 판단


@requires_chromium
async def test_fixture_c_spa_same_name_new_node_stops(pages, tmp_path):
    """클릭 뒤 같은 이름 '확인' 이 다른 위치에 새로 생김(옛 단추 사라짐) → 멈춤, 새 단추 안 눌림."""
    async with _server(tmp_path) as s:
        await _open(s, pages + "/spa")
        out = await s.call_server_tool(BATCH, {"steps": [_click("다음"), _click("확인")]})
        log = await s._page.evaluate(LOG_JS)
    b = _batch_of(out)
    assert out["success"] is False and b["reason"] == "target_new" and b["ran"] == 1, b
    assert log == ["next"]


@requires_chromium
async def test_same_heading_new_path_is_target_new(pages, tmp_path):
    """표제·이름은 같고 css_path 만 다른 새 노드(패널을 한 겹 더 감싸 다시 그림) → target_new(메인 조건 c)."""
    async with _server(tmp_path) as s:
        await _open(s, pages + "/twin")
        out = await s.call_server_tool(BATCH, {"steps": [
            _click("새로 그리기"), _click("3", role="link", group="2026년 10월")]})
        log = await s._page.evaluate(LOG_JS)
        # 대조: 다시 그리지 않으면 같은 대상이 통과한다(표제 계산·해석이 이 페이지에서 동작함).
        await _open(s, pages + "/twin")
        ok = await s.call_server_tool(BATCH, {"steps": [_click("3", role="link", group="2026년 10월"),
                                                         _click("4", role="link", group="2026년 11월")]})
        log2 = await s._page.evaluate(LOG_JS)
    b = _batch_of(out)
    assert out["success"] is False and (b["reason"], b["ran"]) == ("target_new", 1), b
    assert log == ["redraw"]
    assert ok["success"] is True, _batch_of(ok)
    assert log2 == ["o3", "n4"]


@requires_chromium
async def test_fixture_d_renamed_confirm_stops_then_single_click(hana, tmp_path):
    """이름이 앞 단계로 바뀌는 확정 단추를 3단계로 넣으면 멈추고 아무것도 더 누르지 않음 — 응답 관찰의 새 이름 id 로
    단건 click 이 정상 완료(NB-D: 응답 관찰에 단추 id 가 있어야 한다)."""
    async with _server(tmp_path, max_result_chars=BIG) as s:
        await _open(s, hana + "/")
        out = await s.call_server_tool(BATCH, {"steps": [DEP, ARR, _click(OLD_CONFIRM)], "observe": WIDE})
        after_stop = await s._page.evaluate("() => [window.__confirmed, window.__clicks.length]")
        b = _batch_of(out)
        eid = _obs_id(out["data"], NEW_CONFIRM)
        epoch = out["data"]["observation"]["snapshot_epoch"]
        single = await s.call_tool("browser_click", {"element_id": eid, "epoch": epoch})
        confirmed = await s._page.evaluate("() => window.__confirmed")
    assert out["success"] is False and (b["reason"], b["ran"], b["stopped_at"]) == ("target_not_found", 2, 2), b
    assert after_stop == [None, 2]
    assert single.success, (single.error_code, single.error_message)
    assert confirmed == ["2026.10.19(월)", "2026.11.06(금)"]


# ---------------------------------------------------------------------------- 호출 수·시간


class Counter:
    def __init__(self, s: Any) -> None:
        self.s = s
        self.calls = 0
        self.ms: List[float] = []

    async def tool(self, name: str, args: Dict[str, Any]) -> Any:
        t0 = time.perf_counter()
        r = await self.s.call_tool(name, args)
        self.ms.append((time.perf_counter() - t0) * 1000)
        self.calls += 1
        return r

    async def server(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.perf_counter()
        r = await self.s.call_server_tool(name, args)
        self.ms.append((time.perf_counter() - t0) * 1000)
        self.calls += 1
        return r


PANEL_JS = ("(css) => { const a = document.querySelector(css);"
            " return [...document.querySelectorAll('#select_tripstartdate .inr')].indexOf(a.closest('.inr')); }")


async def _day_id(s: Any, obs: Any, panel: int, day: str) -> str:
    """단건 경로의 날짜 id. 실제 캡처에서는 넓은 관찰도 data.repeated 에 달력 묶음이 빠진다(omitted — 기존 표시 예산
    한계, docs/features.md WS-62 R11) — 그래서 어느 달의 '19' 인지는 호출 없이 페이지 구조로 고른다(oracle, 호출 수 0)."""
    for e in obs.data["observation"]["elements"]:
        if e["name"] == day and e["role"] == "link":
            h = s._dispatcher.ctx.engine.get_handle(e["element_id"])
            if await s._page.evaluate(PANEL_JS, h.css_path) == panel:
                return e["element_id"]
    raise AssertionError(f"{panel} {day} 없음")


async def _single_path(s: Any) -> Counter:
    """단건 최소 유효 경로(5호출): observe → click → click(같은 관찰 id) → observe → click."""
    c = Counter(s)
    obs = await c.tool("browser_observe_page", WIDE)
    assert obs.success, obs.error_message
    for panel, day in ((0, "19"), (1, "6")):
        r = await c.tool("browser_click", {"element_id": await _day_id(s, obs, panel, day), "epoch": obs.snapshot_epoch})
        assert r.success, (panel, day, r.error_code, r.error_message)
    obs2 = await c.tool("browser_observe_page", WIDE)
    r = await c.tool("browser_click", {"element_id": _obs_id(obs2.data, NEW_CONFIRM), "epoch": obs2.snapshot_epoch})
    assert r.success, (r.error_code, r.error_message)
    return c


async def _batch_path(s: Any) -> Counter:
    """묶음 경로: observe → batch(응답 관찰) → click. 응답 관찰에 확정 단추가 없으면 실패(observe 추가 금지)."""
    c = Counter(s)
    obs = await c.tool("browser_observe_page", WIDE)  # 에이전트가 화면을 본다(달 이름은 사용자 의도·페이지 글자)
    assert obs.success
    out = await c.server(BATCH, {"steps": [DEP, ARR], "observe": WIDE})
    assert out["success"] is True, _batch_of(out)
    data = out["data"]
    r = await c.tool("browser_click", {"element_id": _obs_id(data, NEW_CONFIRM),
                                        "epoch": data["observation"]["snapshot_epoch"]})
    assert r.success, (r.error_code, r.error_message)
    return c


@requires_chromium
async def test_call_count_and_time_report(hana, tmp_path, capsys):
    """호출 수 합격: 묶음 경로 실제 호출 수 ≤ 3/5 × 단건. 시간은 보고 전용(p50 5회), 단 묶음 1회 ≤ 10초."""
    rows = {"single": [], "batch": [], "batch_call": []}
    counts = {}
    finals = {}
    async with _server(tmp_path, max_result_chars=BIG) as s:
        for _ in range(5):
            for kind, run in (("single", _single_path), ("batch", _batch_path)):
                await _open(s, hana + "/")
                c = await run(s)
                counts[kind] = c.calls
                rows[kind].append(sum(c.ms))
                if kind == "batch":
                    rows["batch_call"].append(c.ms[1])
                finals[kind] = (await s._page.evaluate("() => window.__confirmed"),
                                await s._page.evaluate(CLICKS_JS))
    assert counts == {"single": 5, "batch": 3}
    assert counts["batch"] * 5 <= counts["single"] * 3
    assert finals["single"] == finals["batch"]
    assert finals["batch"][0] == ["2026.10.19(월)", "2026.11.06(금)"]
    assert [c[:2] for c in finals["batch"][1]] == [["2026년 10월", 19], ["2026년 11월", 6]]
    assert max(rows["batch_call"]) <= 10_000, rows["batch_call"]
    with capsys.disabled():
        print("\nWS-73 time report (tool ms, p50 of 5):",
              {k: round(statistics.median(v), 1) for k, v in rows.items()}, "calls", counts)


# ---------------------------------------------------------------------------- 해석 규칙


@requires_chromium
async def test_same_name_other_groups_without_group_is_ambiguous(hana, tmp_path):
    async with _server(tmp_path) as s:
        await _open(s, hana + "/")
        out = await s.call_server_tool(BATCH, {"steps": [_click("19", role="link")]})
        clicks = await s._page.evaluate(CLICKS_JS)
    b = _batch_of(out)
    assert (b["reason"], b["ran"]) == ("target_ambiguous", 0), b
    assert clicks == []


@requires_chromium
async def test_group_mismatch_and_not_found(hana, tmp_path):
    async with _server(tmp_path) as s:
        await _open(s, hana + "/")
        miss = await s.call_server_tool(BATCH, {"steps": [_click("19", role="link", group="2027년 1월")]})
        part = await s.call_server_tool(BATCH, {"steps": [_click("항공권", role="link")]})  # 부분 일치 금지
        clicks = await s._page.evaluate(CLICKS_JS)
    assert _batch_of(miss)["reason"] == "group_mismatch"
    assert _batch_of(part)["reason"] == "target_not_found"
    assert clicks == []


@requires_chromium
async def test_target_outside_top20_resolves(pages, tmp_path):
    """기본 관찰 상위 20 밖 대상도 자르기 전 전체 후보로 해석해 실행한다."""
    async with _server(tmp_path) as s:
        await _open(s, pages + "/many")
        obs = await s.call_tool("browser_observe_page", {})
        assert all(e["name"] != "맨 끝 확인 단추" for e in obs.data["observation"]["elements"])
        out = await s.call_server_tool(BATCH, {"steps": [_click("맨 끝 확인 단추")]})
        log = await s._page.evaluate(LOG_JS)
    assert out["success"] is True, _batch_of(out)
    assert log == ["target"]


FORGE_JS = """() => { const sw = v => typeof v === 'string' ? v.replace(/10월|11월/g, m => m === '10월' ? '11월' : '10월') : v;
for (const [P, k] of [[Node.prototype, 'textContent'], [HTMLElement.prototype, 'innerText'], [Node.prototype, 'nodeValue'],
                      [CharacterData.prototype, 'data']]) {
  const d = Object.getOwnPropertyDescriptor(P, k); if (!d || !d.get) continue;
  Object.defineProperty(P, k, {get() { return sw(d.get.call(this)); }, set: d.set, configurable: true}); } }"""


@requires_chromium
async def test_main_world_heading_forgery_has_no_effect(hana, tmp_path):
    """probe_forge 재사용: 페이지 main world 가 글자 getter 를 바꿔 10월/11월 표제를 뒤바꿔도, 묶음은 격리 world 표제로
    진짜 10월 패널(0번)의 19 를 누른다."""
    async with _server(tmp_path) as s:
        await _open(s, hana + "/")
        await s._page.evaluate(FORGE_JS)
        forged = await s._page.evaluate("() => document.querySelector('#select_tripstartdate .inr .header span').textContent")
        out = await s.call_server_tool(BATCH, {"steps": [DEP]})
        clicks = await s._page.evaluate(CLICKS_JS)
    assert forged == "2026년 11월"  # 위조가 실제로 main world 에 걸렸다
    assert out["success"] is True, _batch_of(out)
    assert [c[2] for c in clicks] == [0], clicks


# ---------------------------------------------------------------------------- 멈춤 조건


@requires_chromium
async def test_approval_button_stops_same_as_single(pages, tmp_path):
    """승인 필요 단추(결제하기) → approval_required, 남은 단계 실행 0 — 단건과 같은 관문 결과(차등)."""
    async with _server(tmp_path) as s:
        await _open(s, pages + "/gates")
        out = await s.call_server_tool(BATCH, {"steps": [_click("결제하기"), _click("다음")]})
        log = await s._page.evaluate(LOG_JS)
        obs = await s.call_tool("browser_observe_page", {})
        single = await s.call_tool("browser_click", {"element_id": _obs_id(obs.data, "결제하기"),
                                                      "epoch": obs.snapshot_epoch})
    b = _batch_of(out)
    assert (b["reason"], b["ran"]) == ("approval_required", 1), b
    assert log == []
    step = out["data"]["step_result"]
    assert step["error_code"] == single.error_code.value == ErrorCode.HITL_UNATTENDED_BLOCKED.value
    assert ("approval" in step.get("data", {})) == ("approval" in single.data)


@requires_chromium
async def test_manual_site_stops_same_as_single(pages, tmp_path):
    from security.manual_sites import ManualSites

    host = pages.split("//", 1)[1]
    async with _server(tmp_path, manual_sites=ManualSites.of([host])) as s:
        await _open(s, pages + "/gates")
        out = await s.call_server_tool(BATCH, {"steps": [_click("다음"), _click("다른 단추")]})
        log = await s._page.evaluate(LOG_JS)
        obs = await s.call_tool("browser_observe_page", {})
        single = await s.call_tool("browser_click", {"element_id": _obs_id(obs.data, "다음"),
                                                      "epoch": obs.snapshot_epoch})
    b = _batch_of(out)
    assert b["reason"] == "manual_site" and b["ran"] == 1, b
    assert log == []
    assert out["data"]["step_result"]["error_code"] == single.error_code.value
    assert out["data"]["step_result"]["data"]["handoff"]["reason"] == single.data["handoff"]["reason"]


@requires_chromium
async def test_ipi_page_stops_before_any_step(pages, tmp_path):
    async with _server(tmp_path) as s:
        await _open(s, pages + "/ipi")
        out = await s.call_server_tool(BATCH, {"steps": [_click("다음")]})
        log = await s._page.evaluate(LOG_JS)
    b = _batch_of(out)
    assert (b["reason"], b["ran"]) == ("injection_suspected", 0), b
    assert log == []
    assert "injection_suspected" in out["data"]


@requires_chromium
@pytest.mark.parametrize("first,reason", [("경고", "dialog"), ("늦은 경고", "dialog")])
async def test_dialog_stops(pages, tmp_path, first, reason):
    """다이얼로그 — 그 단계 결과 또는 늦게(다음 단계 해석 전) 실린 것 모두 멈춤, 다음 단계 실행 0."""
    async with _server(tmp_path) as s:
        await _open(s, pages + "/gates")
        out = await s.call_server_tool(BATCH, {"steps": [_click(first), _click("다음")]})
        await asyncio.sleep(0.1)
        log = await s._page.evaluate(LOG_JS)
    b = _batch_of(out)
    assert b["reason"] == reason and b["ran"] == 1, b
    assert "next" not in log


@requires_chromium
async def test_new_tab_and_navigation_stop_fragment_does_not(pages, tmp_path):
    async with _server(tmp_path) as s:
        await _open(s, pages + "/gates")
        tab = await s.call_server_tool(BATCH, {"steps": [_click("새 탭 열기", role="link"), _click("다음")]})
        tab_log = await s._page.evaluate(LOG_JS)
        await _open(s, pages + "/gates")
        nav = await s.call_server_tool(BATCH, {"steps": [_click("다른 페이지", role="link"), _click("다음")]})
        await _open(s, pages + "/gates")
        frag = await s.call_server_tool(BATCH, {"steps": [_click("아래로", role="link"), _click("주소만 바꾸기"),
                                                           _click("다음")]})
        frag_log = await s._page.evaluate(LOG_JS)
    assert (_batch_of(tab)["reason"], _batch_of(tab)["ran"]) == ("new_tab", 1)
    assert "next" not in tab_log
    assert (_batch_of(nav)["reason"], _batch_of(nav)["ran"]) == ("navigated", 1)
    assert frag["success"] is True, _batch_of(frag)  # URL 만 바뀐 fragment·pushState 는 멈춤 아님
    assert frag_log == ["frag", "push", "next"]


@requires_chromium
async def test_press_key_focus_then_key_and_focus_failure_stops(pages, tmp_path):
    async with _server(tmp_path) as s:
        await _open(s, pages + "/gates")
        ok = await s.call_server_tool(BATCH, {"steps": [
            {"action": "type_text", "target": {"role": "textbox", "name": "검색어"}, "args": {"text": "부산"}},
            {"action": "press_key", "target": {"role": "textbox", "name": "검색어"}, "args": {"key": "Enter"}}]})
        bad = await s.call_server_tool(BATCH, {"steps": [
            {"action": "press_key", "target": {"role": "button", "name": "포커스 거부"}, "args": {"key": "Enter"}},
            _click("다음")]})
        log = await s._page.evaluate(LOG_JS)
    assert ok["success"] is True, _batch_of(ok)
    b = _batch_of(bad)
    assert (b["reason"], b["ran"]) == ("focus_failed", 0), b
    assert log == ["enter:부산"]


@requires_chromium
async def test_time_limit_between_steps_never_cancels_running_step(pages, tmp_path, monkeypatch):
    """시간 상한은 단계 사이에서만 — 진행 중(승인 대기처럼 오래 걸리는) 단계는 끝까지 가고 다음 단계 전에 멈춤."""
    import interface.server_batch as sb

    monkeypatch.setattr(sb, "TIME_LIMIT_S", 0.5)
    async with _server(tmp_path) as s:
        await _open(s, pages + "/gates")
        real = s.call_tool

        async def slow(name, args):
            res = await real(name, args)
            if name == "browser_click":
                await asyncio.sleep(1.0)  # 사람 승인 대기처럼 오래 걸린 단계
            return res

        s.call_tool = slow
        out = await s.call_server_tool(BATCH, {"steps": [_click("다음"), _click("다른 단추")]})
        log = await s._page.evaluate(LOG_JS)
    b = _batch_of(out)
    assert (b["reason"], b["ran"], b["stopped_at"]) == ("time_limit", 1, 1), b
    assert b["steps"][0]["ok"] is True
    assert log == ["next"]


@requires_chromium
async def test_same_workspace_call_cannot_interleave(pages, tmp_path):
    """같은 작업 공간의 다른 호출은 묶음이 끝날 때까지 기다린다(단계 사이에 끼지 않음)."""
    async with _server(tmp_path) as s:
        await _open(s, pages + "/gates")
        obs = await s.call_tool("browser_observe_page", {})
        other_id, epoch = _obs_id(obs.data, "다른 단추"), obs.snapshot_epoch
        real = s._dispatcher.dispatch
        started = asyncio.Event()

        async def slow_dispatch(action, params):
            res = await real(action, params)
            if getattr(action, "value", "") == "click":
                started.set()
                await asyncio.sleep(0.3)
            return res

        s._dispatcher.dispatch = slow_dispatch
        batch = asyncio.ensure_future(s.call_server_tool(BATCH, {"steps": [_click("다음"), _click("경고")]}))
        await started.wait()
        single = await s.call_tool("browser_click", {"element_id": other_id, "epoch": epoch})
        out = await batch
        log = await s._page.evaluate(LOG_JS)
    assert _batch_of(out)["ran"] == 2
    assert log[:2] == ["next", "alert"] and log[-1] == "other", log
    assert single is not None


@requires_chromium
async def test_heal_disabled_during_steps(pages, tmp_path):
    async with _server(tmp_path) as s:
        await _open(s, pages + "/gates")
        seen: List[bool] = []
        real = s._dispatcher.dispatch
        disp = s._dispatcher

        async def spy(action, params):
            if getattr(action, "value", "") == "click":
                seen.append(disp.heal_disabled)
            return await real(action, params)

        disp.dispatch = spy
        out = await s.call_server_tool(BATCH, {"steps": [_click("다음"), _click("다른 단추")]})
        disp.dispatch = real
        obs = await s.call_tool("browser_observe_page", {})
        seen_after: List[bool] = []

        async def spy2(action, params):
            seen_after.append(disp.heal_disabled)
            return await real(action, params)

        disp.dispatch = spy2
        await s.call_tool("browser_click", {"element_id": _obs_id(obs.data, "다음"), "epoch": obs.snapshot_epoch})
    assert out["success"] is True
    assert seen == [True, True]
    assert seen_after == [False]  # 묶음 밖 단건 호출은 원래대로


@requires_chromium
async def test_recipe_recording_once_per_step(pages, tmp_path):
    """레시피 기록 pre/post 는 단계당 1회 — 자동 저장 표시는 batch.steps[i].recipe_saved 로 한 번만."""
    async with _server(tmp_path, recipes=True) as s:
        await _open(s, pages + "/gates")
        pre_calls: List[str] = []
        real_pre = s._recipes.pre

        async def pre(action, params):
            pre_calls.append(action.value)
            return await real_pre(action, params)

        s._recipes.pre = pre
        out = await s.call_server_tool(BATCH, {"steps": [_click("다음"), _click("다른 단추")]})
    assert out["success"] is True, _batch_of(out)
    assert [a for a in pre_calls if a != "observe_page"] == ["click", "click"]
    saved = [x for x in _batch_of(out)["steps"] if "recipe_saved" in x]
    assert len(saved) == 1, _batch_of(out)["steps"]  # 구간(앞 navigate 포함)이 처음 저장된 단계 한 번만
    assert "recipe_saved" not in out["data"].get("step_result", {}).get("data", {})


@requires_chromium
async def test_elicitation_approved_step_still_stops(pages, tmp_path, monkeypatch):
    """대역 밖 승인(elicitation accept)으로 1단계가 성공해도 data.approval 이 붙었으므로 멈춤 — 2단계 실행 0(B3)."""
    import json

    import anyio
    from mcp import types as t
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from interface import handoff
    from interface.mcp_server import create_server

    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))
    server, backend = create_server(recipes=False)
    asked: List[str] = []

    async def elicit(ctx: Any, params: Any) -> Any:
        asked.append(params.message)
        return t.ElicitResult(action="accept", content={})

    out: Dict[str, Any] = {}
    try:
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(ss[0], ss[1], server.create_initialization_options()))
                async with ClientSession(*cs, elicitation_callback=elicit) as session:
                    await session.initialize()
                    await session.call_tool("browser_navigate", {"url": pages + "/gates"})
                    res = await session.call_tool(BATCH, {"steps": [_click("결제하기"), _click("다음")]})
                    out = json.loads(res.content[0].text)
                    out["log"] = await backend._dispatcher.ctx.page.evaluate(LOG_JS)
                tg.cancel_scope.cancel()
    finally:
        await backend.close()
        backend.hub.close()
    b = out["data"]["batch"]
    assert len(asked) == 1 and "결제하기" in asked[0]
    assert out["success"] is False and (b["reason"], b["ran"]) == ("approval_required", 1), b
    assert b["steps"][0]["ok"] is True  # 승인된 단계는 실제로 눌렸다(되돌리지 않음)
    assert out["data"]["step_result"]["data"]["approval"]["via"] == "elicitation"
    assert out["log"] == ["pay"]
