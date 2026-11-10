"""WS-90 (#245, 리뷰 B3): 목적 신호(focus_target·target_selected)는 효과가 아니다 — 레시피 기대 신호에서 거른다.

기록(포커스만) → 재생(효과 있음) 통과, 기록(효과) → 재생(포커스만)은 기존대로 '신호 종류가 다름'으로 멈춘다.
제품 경로: BrowserMCPServer + 로컬 http(127.0.0.1), 실제 네이버지도 캡처 구조의 합성 페이지.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from interface.mcp_server import BrowserMCPServer
from recipes.recorder import build_expect, signal_kinds
from recipes.replay import expect_ok


def _page(open_layer: bool) -> str:
    on = ' onclick="document.getElementById(\'lay\').hidden=false"' if open_layer else ""
    return ("<!doctype html><html><head><meta charset=utf-8><title>길찾기</title></head><body>"
            '<label for="input_search_r_6_">출발지 입력</label>'
            f'<input id="input_search_r_6_" class="input_search" role="combobox" aria-expanded="false" type="text"{on}>'
            '<label for="input_search_r_7_">도착지 입력</label>'
            '<input id="input_search_r_7_" class="input_search" role="combobox" aria-expanded="false" type="text">'
            '<div id="lay" hidden><button type="button">최근 검색 1</button></div>'
            "<script>document.getElementById('input_search_r_6_').focus()</script></body></html>")


class _R:
    def __init__(self, signals):
        self.current_url = "http://127.0.0.1/p"
        self.data = {"signals": signals}


def test_signal_kinds_drop_purpose_signals():
    assert signal_kinds(["focus_target"]) == []
    assert signal_kinds(["focus_moved: BODY# -> INPUT#", "target_selected"]) == []
    assert signal_kinds(["text_changed", "focus_target"]) == ["text_changed"]
    exp = build_expect("click", "http://127.0.0.1/p", "http://127.0.0.1/p", {"signals": ["focus_target"]})
    assert exp["signals"] == [] and exp["nav"] == "none"
    step = {"action": "click", "expect": exp}
    assert expect_ok(step, "http://127.0.0.1/p", _R(["text_changed"])) is None
    eff = {"action": "click", "expect": {"nav": "none", "url_pat": None, "signals": ["text_changed"]}}
    assert expect_ok(eff, "http://127.0.0.1/p", _R(["focus_target"])) == "사후 확인 신호 종류가 기록과 다름"


@pytest.fixture
def site():
    state: Dict[str, Any] = {"html": _page(False)}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = (b"User-agent: *\nAllow: /\n" if self.path == "/robots.txt" else state["html"].encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", state
    finally:
        srv.shutdown()
        srv.server_close()


async def _record(srv: BrowserMCPServer, url: str):
    assert (await srv.call_tool("browser_navigate", {"url": url})).success
    obs = await srv.call_tool("browser_observe_page", {})
    el = next(e for e in obs.data["observation"]["elements"] if e["role"] == "combobox" and "출발지" in e["name"])
    res = await srv.call_tool("browser_click", {"element_id": el["element_id"], "epoch": obs.snapshot_epoch})
    assert res.success, res.model_dump(mode="json")
    saved = await srv.call_server_tool("browser_recipe", {"op": "save", "name": "출발지", "last_n": 1})
    assert saved["success"], saved
    rid = saved["data"]["recipe"]["id"]
    return res, rid, srv._recipes.store.get(rid)["steps"][0]  # noqa: SLF001


@pytest.mark.asyncio
async def test_record_focus_only_then_replay_with_effect_passes(site):
    base, state = site
    async with BrowserMCPServer(headless=True, profile="ws90") as srv:
        res, rid, step = await _record(srv, base + "/dir")
        assert "focus_target" in res.data["signals"], res.data
        assert step["expect"]["signals"] == [], step
        state["html"] = _page(True)
        assert (await srv.call_tool("browser_navigate", {"url": base + "/dir"})).success
        out = await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid})
        hidden = await srv._dispatcher.ctx.page.evaluate("document.getElementById('lay').hidden")  # noqa: SLF001
    assert out["success"], out
    assert hidden is False  # 재생 때 효과(목록 열림)가 실제로 있었다


@pytest.mark.asyncio
async def test_record_effect_then_replay_focus_only_stops(site):
    base, state = site
    state["html"] = _page(True)
    async with BrowserMCPServer(headless=True, profile="ws90") as srv:
        res, rid, step = await _record(srv, base + "/dir")
        assert "focus_target" not in res.data["signals"], res.data
        assert step["expect"]["signals"], step
        state["html"] = _page(False)
        assert (await srv.call_tool("browser_navigate", {"url": base + "/dir"})).success
        out = await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid})
    assert out["success"] is False and out["data"]["recipe"]["reason"] == "expect_mismatch", out
