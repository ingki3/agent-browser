"""WS-94(#272): extract_all — 렌더되지 않는 항목 제외(B1·B2) + 상한을 넘을 때 열린 오버레이 안 항목 먼저(B3·B4).

실사용(하나투어 날짜 칸 클릭 뒤 extract_all)에서 숨은 전체메뉴 글자가 20,000자 상한을 채워 방금 열린 달력이 0개 실렸다. 리뷰
fixture(ws94_extract_pages — 합성 값만, 로컬 HTTP)로 서버 경로를 잰다. 단일 extract·숨은 요소 없는 페이지·selector 오류·
못 찾음·전부 숨은 선택·격리 판독 실패는 기준과 바이트 동일.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from contracts import ActionResult, ActionType
from interface.mcp_server import DEFAULT_MAX_RESULT_CHARS, BrowserMCPServer, envelope_json, tool_name

from test_run_cli import requires_chromium  # noqa: E402,F401 - Chromium 유무 판정 재사용
import ws94_extract_pages as P

PAGES = P.pages()
EXTRACT = tool_name(ActionType.EXTRACT)
NAVIGATE = tool_name(ActionType.NAVIGATE)
CLICK = tool_name(ActionType.CLICK)
WAIT_FOR = tool_name(ActionType.WAIT_FOR)


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
    """격리 판독을 실패시킨다 — NN2: 실패 = 기준 호출 그대로(필터·재배치 없음). 기준 바이트의 출처로 쓴다."""
    from actions.dispatcher import ActionDispatcher

    async def fail(self, expression):  # noqa: ANN001
        raise RuntimeError("격리 world 없음(시험)")

    monkeypatch.setattr(ActionDispatcher, "_doc_isolated_eval", fail)


def _env(r: ActionResult) -> Dict[str, Any]:
    out = json.loads(envelope_json(r))
    out.get("data", {}).pop("latency_ms", None)
    out.pop("snapshot_epoch", None)  # 비교마다 다시 navigate 한다(미관찰 안내 1회 조건을 맞추려고)
    return out


def _texts(r: ActionResult) -> List[str]:
    items = r.data.get("items")
    return [i.get("text", "") for i in items] if isinstance(items, list) else []


def _fares(texts: List[str]) -> int:
    return sum(1 for t in texts if re.fullmatch(r"\d{2}만", t))


async def _after_date_click(server, url: str) -> ActionResult:
    nav = await server.call_tool(NAVIGATE, {"url": url})
    assert nav.success, nav.error_message
    clk = await server.call_tool(CLICK, {"selector": "#date"})
    assert clk.success, clk.error_message
    return await server.call_tool(EXTRACT, {"selector": P.SEL, "extract_all": True})


# ---------------------------------------------------------------- 실사용 재현: 달력 전부·숨은 글자 0


@requires_chromium
@pytest.mark.parametrize("path", ["/air", "/air_big?pos=end", "/air_layer?pos=end"])
async def test_open_calendar_is_returned_and_hidden_menu_is_not(site, path):
    """달력 중간/끝·여러 겹(기존 covering 판 + 달력): 62칸 운임 전부, 두 달 표제, 숨은 메뉴 글자 0, 잘림 안내는 레이어 우선 문구."""
    async with BrowserMCPServer() as server:
        r = await _after_date_click(server, site + path)
    texts = _texts(r)
    assert r.success and len(envelope_json(r)) <= DEFAULT_MAX_RESULT_CHARS
    assert _fares(texts) == 62, _fares(texts)
    assert "2026.12" in texts and "2027.01" in texts
    assert not any(P.MEGA_MARK in t for t in texts)
    assert isinstance(r.data.get("hidden_omitted"), int) and r.data["hidden_omitted"] >= 645
    from interface.mcp_server import _TRUNC_HINT_EXTRACT, _TRUNC_HINT_EXTRACT_LAYER

    trunc = r.data.get("truncated")
    if trunc is not None:
        assert trunc["hint"] == _TRUNC_HINT_EXTRACT_LAYER.format(limit=DEFAULT_MAX_RESULT_CHARS)
        assert trunc["hint"] != _TRUNC_HINT_EXTRACT.format(limit=DEFAULT_MAX_RESULT_CHARS)
    if "layer" in path:
        assert any(t.startswith("기획전") for t in texts), "기존 레이어도 앞에 온다(NN7)"


@requires_chromium
async def test_baseline_reproduces_calendar_zero(site, monkeypatch):
    """재현 고정: 기준 경로(격리 판독 실패)에서는 숨은 메뉴가 상한을 채워 달력 0 — 위 시험이 무엇을 고쳤는지의 근거."""
    _baseline(monkeypatch)
    async with BrowserMCPServer() as server:
        r = await _after_date_click(server, site + "/air_big?pos=end")
    texts = _texts(r)
    assert r.data.get("truncated") and _fares(texts) == 0
    assert any(P.MEGA_MARK in t for t in texts) and "hidden_omitted" not in r.data


# ---------------------------------------------------------------- B3: 주입 신호 where 는 재배치 뒤 항목을 가리킨다


@requires_chromium
async def test_injection_where_points_at_reordered_items(site):
    async with BrowserMCPServer() as server:
        r = await _after_date_click(server, site + "/air_inj?pos=end")
    sig = r.data.get("injection_suspected")
    assert sig, r.data.keys()
    items = r.data["items"]
    hits = 0
    for w in sig["where"]:
        m = re.fullmatch(r"items\[(\d+)\]\.text", w)
        assert m, w
        k = int(m.group(1))
        if k < len(items):
            assert P.INJ_TEXT in items[k]["text"], (w, items[k]["text"][:80])
            hits += 1
    assert hits >= 1
    assert _fares(_texts(r)) == 62


# ---------------------------------------------------------------- B4: extract 가 다음 동작의 overlay_opened 를 바꾸지 않는다


@requires_chromium
async def test_extract_does_not_change_next_overlay_opened(site, monkeypatch):
    async def seq(server, with_extract: bool) -> Any:
        await server.call_tool(NAVIGATE, {"url": site + "/air_auto"})
        w = await server.call_tool(WAIT_FOR, {"condition": "selector", "selector": "#cal"})
        assert w.success, w.error_message
        if with_extract:
            await server.call_tool(EXTRACT, {"selector": P.SEL, "extract_all": True})
        clk = await server.call_tool(CLICK, {"selector": "#go"})
        assert clk.success, clk.error_message
        return clk.data.get("overlay_opened")

    async with BrowserMCPServer() as server:
        base = await seq(server, False)
        after = await seq(server, True)
    assert base == {"kinds": ["covering"]}, base
    assert after == base


# ---------------------------------------------------------------- B1·B2: census·숨은 것만 일치·섞인 선택


def _ids(r: ActionResult) -> List[str]:
    return [i.get("id") for i in r.data["items"]]


@requires_chromium
async def test_visibility_census(site):
    async with BrowserMCPServer() as server:
        await server.call_tool(NAVIGATE, {"url": site + "/census"})
        r = await server.call_tool(EXTRACT, {"selector": ".t", "extract_all": True,
                                             "attributes": ["id", "value", "content"]})
    ids = _ids(r)
    assert len(ids) + r.data["hidden_omitted"] == 36
    assert r.data["hidden_omitted"] == len(P.CENSUS_OMITTED)
    for gone in P.CENSUS_OMITTED:
        assert gone not in ids, gone
    for kept in P.CENSUS_LIMITS:
        assert kept in ids, f"문서화한 한계는 반환 유지: {kept}"
    texts = " ".join(_texts(r))
    for v in re.findall(r"(?<![A-Z])V-[A-Z0-9-]+", P.CENSUS):
        assert v in texts, f"보이는 것 누락: {v}"
    assert "tin" in ids and "VV-TEXT-INPUT" in [i.get("value") for i in r.data["items"]]


@requires_chromium
@pytest.mark.parametrize("args", [
    {"selector": "select option", "attributes": ["value"]},
    {"selector": "meta", "attributes": ["content"]},
    {"selector": "input[type=hidden]", "attributes": ["value"]},
    {"selector": "#gnbAll a", "attributes": ["href"]},
])
async def test_only_hidden_matches_are_byte_identical(site, monkeypatch, args):
    path = "/air" if args["selector"].startswith("#gnbAll") else "/census"
    call = dict(args, extract_all=True)
    async with BrowserMCPServer() as server:
        await server.call_tool(NAVIGATE, {"url": site + path})
        got = await server.call_tool(EXTRACT, call)
        await server.call_tool(NAVIGATE, {"url": site + path})  # 미관찰 안내(1회)도 같은 조건으로
        with monkeypatch.context() as m:
            _baseline(m)
            base = await server.call_tool(EXTRACT, call)
    assert got.success and base.success
    assert _env(got) == _env(base)
    assert "hidden_omitted" not in got.data


@requires_chromium
async def test_mixed_links_report_hidden_omitted(site):
    async with BrowserMCPServer() as server:
        await server.call_tool(NAVIGATE, {"url": site + "/air"})
        r = await server.call_tool(EXTRACT, {"selector": "a", "attributes": ["href"], "extract_all": True})
    hrefs = [i["href"] for i in r.data["items"]]
    assert r.data["hidden_omitted"] == len(P.REGIONS) * 22
    assert not any(h.startswith("/m/") for h in hrefs)
    assert "/k/0" in hrefs and "/p/0" in hrefs


@requires_chromium
async def test_hidden_injection_drops_with_item(site):
    """NN5: display:none 문단의 주입 문구는 섞인 extract_all 에서 항목과 신호가 함께 빠진다(문서화)."""
    async with BrowserMCPServer() as server:
        await server.call_tool(NAVIGATE, {"url": site + "/inj"})
        r = await server.call_tool(EXTRACT, {"selector": "p", "extract_all": True})
    assert _texts(r) == ["정상 본문 문단입니다 하나", "정상 본문 문단입니다 둘"]
    assert r.data["hidden_omitted"] == 1 and "injection_suspected" not in r.data


@requires_chromium
async def test_switch_frame_reads_the_frame_document(site):
    """NN2: switch_frame 한 프레임은 그 프레임 문서(격리 world)를 읽는다 — 바깥 문서 항목이 섞이지 않는다."""
    async with BrowserMCPServer() as server:
        await server.call_tool(NAVIGATE, {"url": site + "/framed"})
        await server.call_tool(WAIT_FOR, {"condition": "selector", "selector": "#f"})
        sw = await server.call_tool(tool_name(ActionType.SWITCH_FRAME), {"frame_selector": "#f"})
        assert sw.success, sw.error_message
        r = await server.call_tool(EXTRACT, {"selector": "p", "extract_all": True})
    assert _texts(r) == ["정상 본문 문단입니다 하나", "정상 본문 문단입니다 둘"]
    assert r.data["hidden_omitted"] == 1


# ---------------------------------------------------------------- 불변: 기준과 바이트 동일


@requires_chromium
@pytest.mark.parametrize("path,args", [
    ("/plain", {"selector": "li", "extract_all": True}),
    ("/plain", {"selector": "a", "attributes": ["href"], "extract_all": True}),
    ("/census", {"selector": "#dn"}),
    ("/census", {"selector": "#hin", "attributes": ["value"]}),
    ("/census", {"selector": "#det"}),
    ("/air", {"selector": P.SEL}),
    ("/plain", {"selector": "li[", "extract_all": True}),
    ("/plain", {"selector": "#nope", "extract_all": True}),
    ("/plain", {"selector": "li[", "extract_all": False}),
])
async def test_unaffected_calls_are_byte_identical(site, monkeypatch, path, args):
    async with BrowserMCPServer() as server:
        await server.call_tool(NAVIGATE, {"url": site + path})
        got = await server.call_tool(EXTRACT, args)
        await server.call_tool(NAVIGATE, {"url": site + path})  # 미관찰 안내(1회)도 같은 조건으로
        with monkeypatch.context() as m:
            _baseline(m)
            base = await server.call_tool(EXTRACT, args)
    assert _env(got) == _env(base)
    assert "hidden_omitted" not in (got.data or {})


@requires_chromium
async def test_large_plain_page_keeps_document_order(site):
    """오버레이가 없으면 잘려도 기준 순서·기준 hint(_TRUNC_HINT_EXTRACT) 그대로."""
    PAGES["/rows"] = "<!doctype html><meta charset=utf-8><title>rows</title><ul>" + "".join(
        f"<li>{i}번째 기사 제목입니다</li>" for i in range(3000)) + "</ul>"
    from interface.mcp_server import _TRUNC_HINT_EXTRACT

    async with BrowserMCPServer() as server:
        await server.call_tool(NAVIGATE, {"url": site + "/rows"})
        r = await server.call_tool(EXTRACT, {"selector": "li", "extract_all": True})
    texts = _texts(r)
    assert texts[:3] == ["0번째 기사 제목입니다", "1번째 기사 제목입니다", "2번째 기사 제목입니다"]
    assert r.data["truncated"]["hint"] == _TRUNC_HINT_EXTRACT.format(limit=DEFAULT_MAX_RESULT_CHARS)


# ---------------------------------------------------------------- 순수 함수·가림 재시도 순서 일관


def _extract_result(n: int) -> ActionResult:
    items = [{"text": f"항목 {i} " + "가" * 200} for i in range(n)]
    return ActionResult(success=True, action=ActionType.EXTRACT, current_url="http://x/", snapshot_epoch=1,
                        tab_id="tab-1", retry_safe=True, data={"items": items, "latency_ms": 1.0})


def test_layer_first_order_is_pure_and_stable():
    from actions.extract_visible import layer_first_order

    assert layer_first_order([False, True, False, True]) == [1, 3, 0, 2]
    assert layer_first_order([False, False]) is None
    assert layer_first_order([True, True]) is None
    assert layer_first_order([True, False]) is None


def test_fit_reorders_only_over_cap_and_is_consistent_across_retries():
    from interface.mcp_server import _TRUNC_HINT_EXTRACT_LAYER
    from security.injection_signal import attach_injection_signal
    from security.public_url import content_view

    n, limit = 200, 5000
    s = BrowserMCPServer(max_result_chars=limit)
    marks = [i >= n - 5 for i in range(n)]
    result = _extract_result(n)
    original = result.data
    outs = []
    for _ in range(2):  # 가림 재시도처럼 같은 원본에서 다시
        result.data = content_view(original)
        s._fit_result(result, ActionType.EXTRACT, limit, limit * 10, attach_injection_signal,  # noqa: SLF001
                      layers=marks)
        outs.append(envelope_json(result))
    assert outs[0] == outs[1]
    assert [i["text"].split(" ")[1] for i in original["items"][:2]] == ["0", "1"], "원본은 그대로"
    got = json.loads(outs[0])["data"]
    assert [i["text"].split(" ")[1] for i in got["items"][:5]] == [str(i) for i in range(n - 5, n)]
    assert got["truncated"]["hint"] == _TRUNC_HINT_EXTRACT_LAYER.format(limit=limit)
    small = _extract_result(3)
    small.data = content_view(small.data)
    before = envelope_json(small)
    s._fit_result(small, ActionType.EXTRACT, limit, limit * 10, attach_injection_signal,  # noqa: SLF001
                  layers=[False, False, True])
    assert envelope_json(small) == before, "상한 안이면 순서 그대로"


def test_batch_refitter_keeps_layer_order():
    from security.public_url import content_view

    n, limit = 200, 5000
    s = BrowserMCPServer(max_result_chars=limit)
    marks = [i >= n - 5 for i in range(n)]
    result = _extract_result(n)
    original = result.data
    result.data = content_view(original)
    refit = s._batch_refitter(result, original, ActionType.EXTRACT, limit, limit * 10,  # noqa: SLF001
                              layers=marks)
    got = refit(1000).data
    assert got["items"][0]["text"].startswith(f"항목 {n - 5} ")
