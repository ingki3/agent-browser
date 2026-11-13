"""WS-95a(#275): 열린 레이어(달력) 구성원을 반복 묶음(data.repeated) 입력에 — 기본 관찰에서 두 달 전부.

실사용(하나투어 날짜 칸 클릭 뒤 기본 관찰)에서 elements(top-20) 안 날짜만 묶음에 들어 두 달이 7·3 칸으로 보였다. 반복 묶음 입력 =
반환 elements + 열린 레이어 구성원(반환 안 된 것도 핸들·v1160 고정 유지, 기존 두 번째 고정 호출에 합쳐 호출 수 2). 최종 응답 묶음에
남은 id 만 등록 유지. elements 내용·순서 불변. 크기 예산은 레이어 묶음 먼저(표제 null 이어도). 흐름 안 달력(오버레이 미감지)은 한계.
fixture: ws95_layer_pages(합성 값만, 로컬 HTTP).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set

import pytest

from contracts import ActionResult, ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, envelope_json, tool_name

from test_run_cli import requires_chromium  # noqa: E402,F401 - Chromium 유무 판정 재사용
import ws95_layer_pages as P

PAGES = P.pages()
OBSERVE = tool_name(ActionType.OBSERVE_PAGE)
NAVIGATE = tool_name(ActionType.NAVIGATE)
CLICK = tool_name(ActionType.CLICK)


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = PAGES.get(self.path.split("?", 1)[0], "<p>x</p>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _baseline(monkeypatch) -> None:
    """기준 동작: 레이어 범위(추가 구성원·묶음 우선)를 끈다 — 기준 바이트·7·3 재현의 출처."""
    from actions.dispatcher import ActionDispatcher

    def none(self, els, members, final_idx):  # noqa: ANN001
        return [], None

    monkeypatch.setattr(ActionDispatcher, "_layer_scope", none)


def _spy_extras(monkeypatch) -> List[Set[str]]:
    """_layer_scope 가 고른 추가 구성원 id(관찰마다 하나) — 등록 해제 확인용(동작은 그대로)."""
    from actions.dispatcher import ActionDispatcher

    real = ActionDispatcher._layer_scope
    seen: List[Set[str]] = []

    def spy(self, els, members, final_idx):  # noqa: ANN001
        extras, ids = real(self, els, members, final_idx)
        seen.append({e.element_id for e in extras})
        return extras, ids

    monkeypatch.setattr(ActionDispatcher, "_layer_scope", spy)
    return seen


def _env(r: ActionResult) -> Dict[str, Any]:
    out = json.loads(envelope_json(r))
    out.get("data", {}).pop("latency_ms", None)
    return out


def _elements(r: ActionResult) -> List[Dict[str, Any]]:
    return r.data["observation"]["elements"]


def _groups(r: ActionResult) -> List[Dict[str, Any]]:
    rep = r.data.get("repeated") or {}
    return rep.get("groups") or []


def _member_ids(r: ActionResult) -> Set[str]:
    return {m["id"] for g in _groups(r) for m in g.get("members") or []}


def _day_counts(r: ActionResult) -> List[int]:
    return [len(g["members"]) for g in _groups(r)
            if all(m["name"].isdigit() for m in g["members"])]


def _registered(server) -> Set[str]:
    engine = server._dispatcher.ctx.engine
    return {eid for eid in engine._handles if engine.get_handle(eid) is not None}


async def _open_layer(server, url: str) -> ActionResult:
    nav = await server.call_tool(NAVIGATE, {"url": url})
    assert nav.success, nav.error_message
    first = await server.call_tool(OBSERVE, {})
    assert first.success, first.error_message
    if "/hana" in url:
        clk = await server.call_tool(CLICK, {"selector": "#d1"})
        assert clk.success, clk.error_message
    r = await server.call_tool(OBSERVE, {})
    assert r.success, r.error_message
    return r


async def _run(url: str, then=None, **kw):
    async with BrowserMCPServer(**kw) as server:
        r = await _open_layer(server, url)
        extra = await then(server, r) if then else None
    return r, extra


# ---------------------------------------------------------------- 재현·정탐: 두 달 전부(30·31)


@requires_chromium
@pytest.mark.parametrize("shape", P.SHAPES)
async def test_baseline_reproduces_partial_months(site, shape, monkeypatch):
    """재현 고정(RED 근거): 기준(레이어 범위 끔)은 elements 안 날짜만 묶어 두 달이 7·3 칸."""
    _baseline(monkeypatch)
    r, _ = await _run(f"{site}/hana_{shape}")
    assert r.data.get("overlay", {}).get("kinds") == ["covering"], r.data.get("overlay")
    assert _day_counts(r) == [7, 3], _day_counts(r)


@requires_chromium
@pytest.mark.parametrize("shape", P.SHAPES)
async def test_layer_months_complete_and_elements_unchanged(site, shape, monkeypatch):
    """기본 관찰 repeated 에 두 달 전부(30·31). elements 내용·순서·overlay 는 기준과 같다(바이트)."""
    on, _ = await _run(f"{site}/hana_{shape}")
    assert _day_counts(on) == [30, 31], _day_counts(on)
    elem_ids = {e["element_id"] for e in _elements(on)}
    assert len(_member_ids(on) - elem_ids) >= 50  # 반환 안 된 레이어 구성원이 묶음에 실렸다
    _baseline(monkeypatch)
    off, _ = await _run(f"{site}/hana_{shape}")
    a, b = _env(on), _env(off)
    assert a["data"]["observation"] == b["data"]["observation"]
    assert a["data"]["overlay"] == b["data"]["overlay"]
    print(f"\nWS95-SIZE hana_{shape} base={len(envelope_json(off))} cand={len(envelope_json(on))}")


# ---------------------------------------------------------------- 추가 id: 클릭 반영 · 표제 바뀐 뒤 거부


def _dec_day(r: ActionResult, day: str) -> str:
    dec = next(g for g in _groups(r) if len(g["members"]) == 31)
    return next(m["id"] for m in dec["members"] if m["name"] == day)


@requires_chromium
@pytest.mark.parametrize("shape", P.SHAPES)
async def test_extra_id_click_selects_december_25(site, shape):
    """elements 에 없는 12월 25일 id(묶음에서 얻음)를 누르면 그 칸이 눌린다(foot 표시 = 12월 25)."""

    async def click(server, r):
        eid = _dec_day(r, "25")
        assert eid not in {e["element_id"] for e in _elements(r)}
        res = await server.call_tool(CLICK, {"element_id": eid, "epoch": r.snapshot_epoch})
        page = server._dispatcher.ctx.page
        return res, await page.inner_text("#cal .foot .d")

    _r, (res, shown) = await _run(f"{site}/hana_{shape}", then=click)
    assert res.success, (res.error_code, res.error_message)
    assert shown == "2026년 12월 25", shown


@requires_chromium
@pytest.mark.parametrize("shape", ["div", "p"])
async def test_extra_id_after_heading_change_is_toctou(site, shape):
    """'다음'으로 표제가 바뀐 뒤 옛 관찰의 추가 id(12월 25일)는 누르지 않는다 — E_TOCTOU_MISMATCH, 표시 그대로."""

    async def flow(server, r):
        eid = _dec_day(r, "25")
        nxt = await server.call_tool(CLICK, {"selector": "#cal .next"})
        assert nxt.success, nxt.error_message
        page = server._dispatcher.ctx.page
        before = await page.inner_text("#cal .foot .d")
        res = await server.call_tool(CLICK, {"element_id": eid, "epoch": r.snapshot_epoch})
        return res, before, await page.inner_text("#cal .foot .d")

    _r, (res, before, after) = await _run(f"{site}/hana_{shape}", then=flow)
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH, (res.error_code, res.error_message)
    assert before == after


@requires_chromium
async def test_sephead_extra_id_same_as_returned_day(site):
    """한계(별도 WS — group null·_panel_heads): 표제가 달 본문 밖 머리 행이면 반환된 날짜 id 도 표제 변경을 못 잡는다.
    추가 id 는 반환된 id 와 같은 판정을 받는다(더 느슨하지도 엄격하지도 않음)."""

    async def flow(server, r, pick):
        eid = pick(r)
        nxt = await server.call_tool(CLICK, {"selector": "#cal .next"})
        assert nxt.success, nxt.error_message
        res = await server.call_tool(CLICK, {"element_id": eid, "epoch": r.snapshot_epoch})
        return res.success, res.error_code

    def returned_day(r):
        elems = {e["element_id"] for e in _elements(r)}
        dec = next(g for g in _groups(r) if len(g["members"]) == 31)
        return next(m["id"] for m in dec["members"] if m["id"] in elems)

    _r, extra = await _run(f"{site}/hana_sephead", then=lambda s, r: flow(s, r, lambda x: _dec_day(x, "25")))
    _r, returned = await _run(f"{site}/hana_sephead", then=lambda s, r: flow(s, r, returned_day))
    assert extra == returned, (extra, returned)


# ---------------------------------------------------------------- 레이어 없는 페이지·모달·쿠키 배너


@requires_chromium
@pytest.mark.parametrize("name", ["products", "cookie_bar", "modal"])
async def test_no_extra_members_bytes_identical(site, name, monkeypatch):
    """레이어 없음(상품 행·쿠키 띠 — 감지 안 됨)·구성원이 전부 반환되는 모달: 응답 바이트 = 기준."""
    on, _ = await _run(f"{site}/{name}")
    if name != "modal":
        assert "overlay" not in on.data
    _baseline(monkeypatch)
    off, _ = await _run(f"{site}/{name}")
    assert json.dumps(_env(on), ensure_ascii=False) == json.dumps(_env(off), ensure_ascii=False)


@requires_chromium
async def test_cookie_center_size_reported(site, monkeypatch):
    """중앙 쿠키 설정창(covering): 반환 안 된 구성원이 묶음에 실려 커질 수 있다 — 크기를 보고하고 상한 안·elements 불변."""
    on, _ = await _run(f"{site}/cookie_center")
    _baseline(monkeypatch)
    off, _ = await _run(f"{site}/cookie_center")
    a, b = len(envelope_json(on)), len(envelope_json(off))
    print(f"\nWS95-SIZE cookie_center base={b} cand={a} delta={a - b}")
    assert _env(on)["data"]["observation"] == _env(off)["data"]["observation"]
    assert a >= b


# ---------------------------------------------------------------- B4: 예산 압박에서 레이어 묶음 우선


@requires_chromium
async def test_layer_sets_first_under_budget(site, monkeypatch):
    """표제 null(div) 달력 + base 안 더 큰·표제 있는 쌍둥이 세트: 레이어 묶음이 먼저(예산에서 안 빠짐)."""
    on, _ = await _run(f"{site}/hana_big")
    assert _day_counts(on) == [30, 31], (_day_counts(on), on.data.get("repeated", {}).get("omitted"))
    heads = [len(g["members"]) for g in _groups(on)][:2]
    assert heads == [30, 31], heads


@requires_chromium
@pytest.mark.parametrize("cap", [3000, 3500, 4000])
async def test_layer_sets_survive_fit_repeated(site, cap, monkeypatch):
    """작은 응답 상한: fit_repeated 는 뒤(우선순위 낮은 쪽)부터 뺀다 — 레이어 밖 세트가 남아 있으면 레이어 묶음도 남는다.
    뺀 묶음의 추가 구성원 id 는 등록 해제(응답 묶음에 실린 것만 살아 있다)."""
    seen = _spy_extras(monkeypatch)

    async def regs(server, r):
        return _registered(server)

    r, registered = await _run(f"{site}/hana_big", then=regs, max_result_chars=cap)
    days = _day_counts(r)
    others = [g for g in _groups(r) if not all(m["name"].isdigit() for m in g["members"])]
    assert days == [30, 31] or not others, (days, len(others))
    extras = seen[-1]
    assert extras and registered & extras == _member_ids(r) & extras, sorted((registered & extras) - _member_ids(r))
    print(f"\nWS95-CAP {cap} days={days} others={len(others)} omitted={(r.data.get('repeated') or {}).get('omitted')}")


# ---------------------------------------------------------------- v1160 고정 호출 2회 · 등록 해제


@requires_chromium
async def test_v1160_two_calls_second_includes_layer_extras(site, monkeypatch):
    """v1160 고정 1차 = 엔진 원래 top-N, 2차 = 최종 elements + 반환 안 된 레이어 구성원(호출 수 2 유지)."""
    import perception.repeated_v1160 as v

    real = v.annotate_repeated
    calls: List[List[str]] = []

    async def spy(page, engine, elements):
        calls.append([e.element_id for e in elements])
        return await real(page, engine, elements)

    monkeypatch.setattr(v, "annotate_repeated", spy)

    async def mark(server, r):
        calls.append(["--"])

    r, _ = await _run(f"{site}/hana_p", then=mark)
    last = calls[calls.index(["--"]) - 2:calls.index(["--"])]
    elems = [e["element_id"] for e in _elements(r)]
    assert len(last) == 2 and last[1][:len(elems)] == elems, last
    assert len(last[1]) > len(elems) and _member_ids(r) - set(elems) <= set(last[1])


@requires_chromium
async def test_only_returned_ids_stay_registered(site, monkeypatch):
    """레이어 안·묶음 밖 구성원(안내 링크 10개)은 반환되지 않으면 등록 해제 — 등록 id = elements ∪ 응답 묶음 id."""
    seen = _spy_extras(monkeypatch)

    async def regs(server, r):
        return _registered(server)

    r, registered = await _run(f"{site}/hana_notes", then=regs)
    assert _day_counts(r) == [30, 31]
    shown = {e["element_id"] for e in _elements(r)} | _member_ids(r)
    assert seen[-1] - shown, "조건: 묶음에 안 드는 추가 구성원이 있어야 해제를 잰다"
    assert registered == shown, sorted(registered - shown)


@requires_chromium
async def test_repeated_dropped_by_cap_unregisters_extras(site, monkeypatch):
    """상한이 묶음(12월)을 통째로 빼면 그 추가 구성원 id 도 해제 — 옛 묶음에서 얻은 id 를 보내도 누르지 않는다."""
    seen = _spy_extras(monkeypatch)

    async def flow(server, r):
        regs = _registered(server)
        eid = sorted(seen[-1] - _member_ids(r))[0]
        res = await server.call_tool(CLICK, {"element_id": eid, "epoch": r.snapshot_epoch})
        return regs, res, await server._dispatcher.ctx.page.inner_text("#cal .foot .d")

    r, (registered, res, shown) = await _run(f"{site}/hana_p", then=flow, max_result_chars=3000)
    assert (r.data.get("repeated") or {}).get("omitted"), r.data.get("repeated")
    dropped = seen[-1] - _member_ids(r)
    assert dropped and not registered & dropped, sorted(registered & dropped)
    assert res.success is False and shown == "2026.11.13(금)", (res.error_code, shown)


@requires_chromium
async def test_hidden_control_ids_do_not_collide_with_layer_ids(site):
    """WS-81 추가 요소(숨긴 체크박스 라벨) id 는 묶음의 추가 구성원 id 와 겹치지 않는다(핸들 덮어쓰기 0)."""

    async def handles(server, r):
        engine = server._dispatcher.ctx.engine
        return {m["id"]: (engine.get_handle(m["id"]).name if engine.get_handle(m["id"]) else None)
                for g in _groups(r) for m in g["members"]}

    r, names = await _run(f"{site}/hana_hidden", then=handles)
    assert _day_counts(r) == [30, 31]
    hc_ids = {e["element_id"] for e in _elements(r)} - {e["element_id"] for e in _elements(r)[:20]}
    assert hc_ids and not hc_ids & _member_ids(r), (hc_ids, sorted(_member_ids(r))[:5])
    for g in _groups(r):
        for m in g["members"]:
            assert names[m["id"]] is not None and names[m["id"]].startswith(m["name"]), (m, names[m["id"]])
