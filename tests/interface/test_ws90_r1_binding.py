"""WS-90 R1 (검증 FAIL B1): 목적 신호는 **실제로 클릭한 그 노드**에서만.

검증 재현(.hermes/verify/ws90 runner.py binding-contract · repro_binding.py)을 저장소 회귀 시험으로 옮겼다.
클릭한 입력·탭이 onclick 에서 같은 selector(#t)의 clone 으로 교체되고 새 노드가 포커스·aria-selected 를 받으면,
옛 노드(클릭한 노드)는 끊겼고(isConnected=false) 포커스도 받지 않았다 — focus_target·target_selected 로 성공하면
안 된다(기준 E_TIMEOUT 그대로). 사후 CSS 재조회로 찾은 새 후보를 쓰면 이 시험이 RED.
독립 oracle: 시험 자신의 CDP 세션(main world)에서 클릭 전에 잡아 둔 window.orig 를 클릭 뒤 읽는다.
경로: element_id · selector · batch(role+name) · 레시피 재생(포커스만 기록 → 재생 때 교체).
제품 경로: 실 Chromium(headless) + 로컬 http(127.0.0.1), 합성 페이지만.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from interface.mcp_server import BrowserMCPServer, envelope_json

_SWAP_INPUT = "const n=this.cloneNode(true);this.replaceWith(n);n.focus()"
_SWAP_TAB = "const n=this.cloneNode(true);n.setAttribute('aria-selected','true');this.replaceWith(n);n.focus()"

PAGES: Dict[str, str] = {
    "/input": f'<input id=t aria-label="확인칸" onclick="{_SWAP_INPUT}">',
    "/tab": f'<div role=tablist><button id=t role=tab aria-selected=false onclick="{_SWAP_TAB}">보기</button></div>',
    "/plain": '<input id=t aria-label="확인칸">',
    "/tab_on": '<div role=tablist><button id=t role=tab aria-selected=true>보기</button></div>',
}
CASES = [("/input", "textbox", "확인칸"), ("/tab", "tab", "보기")]
ROUTES = ["id", "selector", "batch"]
#: 게이트 묶음 없이 디스패처 직접(묶음 정체 재사용이 없는 경로 — 클릭 직전 새 결속).
DIRECT = ["direct-id", "direct-selector"]


def _has_capture() -> bool:
    from actions.dispatcher import ActionDispatcher

    return hasattr(ActionDispatcher, "_capture_clicked_node")


_HAS_CAPTURE = _has_capture()

_TRUTH = ("JSON.stringify({oldConnected: orig.isConnected, activeIsClicked: document.activeElement === orig,"
          " sameNode: document.getElementById('t') === orig,"
          " currentSelected: document.getElementById('t').getAttribute('aria-selected'),"
          " activeIsCurrent: document.activeElement === document.getElementById('t')})")


@pytest.fixture(scope="module")
def site():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            p = self.path.split("?")[0]
            body = (b"User-agent: *\nAllow: /\n" if p == "/robots.txt" else
                    ("<!doctype html><meta charset=utf-8><title>경계</title>" + PAGES.get(p, "<p>nf")).encode())
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
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _truth(s: BrowserMCPServer) -> Dict[str, Any]:
    cdp = await s._page.context.new_cdp_session(s._page)  # noqa: SLF001 — 독립 oracle(제품 세션 아님)
    try:
        res = await cdp.send("Runtime.evaluate", {"expression": _TRUTH, "returnByValue": True})
        return json.loads(res["result"]["value"])
    finally:
        await cdp.detach()


async def _open(s: BrowserMCPServer, url: str) -> Any:
    nav = await s.call_tool("browser_navigate", {"url": url})
    assert nav.success, nav.model_dump(mode="json")
    return await s.call_tool("browser_observe_page", {})


def _find(obs: Any, role: str, name: str) -> Dict[str, Any]:
    hit = [e for e in obs.data["observation"]["elements"] if e.get("role") == role and e.get("name") == name]
    assert hit, [(e.get("role"), e.get("name")) for e in obs.data["observation"]["elements"]]
    return hit[0]


async def _click(s: BrowserMCPServer, route: str, role: str, name: str, obs: Any) -> Dict[str, Any]:
    if route.startswith("direct-"):
        from contracts import ActionType

        args = ({"selector": "#t"} if route == "direct-selector" else
                {"element_id": _find(obs, role, name)["element_id"], "epoch": obs.snapshot_epoch})
        return json.loads(envelope_json(await s._dispatcher.dispatch(ActionType.CLICK, args)))  # noqa: SLF001
    if route == "batch":
        return await s.call_server_tool("browser_batch", {"steps": [
            {"action": "click", "target": {"role": role, "name": name}}]})
    if route == "selector":
        res = await s.call_tool("browser_click", {"selector": "#t"})
    else:
        el = _find(obs, role, name)
        res = await s.call_tool("browser_click", {"element_id": el["element_id"], "epoch": obs.snapshot_epoch})
    return json.loads(envelope_json(res))


@pytest.fixture(scope="module")
def rows(site) -> List[Dict[str, Any]]:
    import asyncio

    from actions.dispatcher import ActionDispatcher

    out: List[Dict[str, Any]] = []
    reused: List[bool] = []  # 클릭 직전 정체를 잡을 때 게이트 묶음 정체가 있었나(경로 고유성 확인)
    capture = getattr(ActionDispatcher, "_capture_clicked_node", None)

    async def spy(self, *a: Any, **k: Any) -> Any:  # noqa: ANN001
        reused.append(self.__dict__.get("_fence_bound") is not None)
        return await capture(self, *a, **k)

    async def main() -> None:
        async with BrowserMCPServer(headless=True, nav_settle=False) as s:
            for page, role, name in CASES:
                for route in ROUTES + DIRECT:
                    obs = await _open(s, site + page)
                    await s._page.evaluate("() => { window.orig = document.getElementById('t') }")  # noqa: SLF001
                    reused.clear()
                    wire = await _click(s, route, role, name, obs)
                    out.append({"case": page, "route": route, "wire": wire, "truth": await _truth(s),
                                "reused": list(reused)})
            # 대조(같은 노드 유지): 직접 경로도 클릭한 노드의 포커스·선택을 성공으로 읽는다.
            for page, role, name in [("/plain", "textbox", "확인칸"), ("/tab_on", "tab", "보기")]:
                for route in DIRECT:
                    obs = await _open(s, site + page)
                    reused.clear()
                    wire = await _click(s, route, role, name, obs)
                    out.append({"case": page, "route": route, "wire": wire, "reused": list(reused)})
            # 포커스만 기록(expect.signals=[]) → 재생 때 클릭한 노드가 clone 으로 교체된다.
            obs = await _open(s, site + "/plain")
            el = _find(obs, "textbox", "확인칸")
            rec = await s.call_tool("browser_click", {"element_id": el["element_id"], "epoch": obs.snapshot_epoch})
            if not rec.success:
                out.append({"case": "/plain", "route": "record", "wire": json.loads(envelope_json(rec))})
                return
            saved = await s.call_server_tool("browser_recipe", {"op": "save", "name": "칸 확인", "last_n": 1})
            assert saved["success"], saved
            rid = saved["data"]["recipe"]["id"]
            step = s._recipes.store.get(rid)["steps"][0]  # noqa: SLF001
            await s._page.evaluate(  # noqa: SLF001
                "() => { const t = document.getElementById('t'); window.orig = t;"
                " t.onclick = () => { const n = t.cloneNode(true); t.replaceWith(n); n.focus() } }")
            ran = await s.call_server_tool("browser_recipe", {"op": "run", "id": rid})
            out.append({"case": "/input", "route": "recipe", "step": step, "wire": ran, "truth": await _truth(s)})

    with pytest.MonkeyPatch.context() as mp:
        if capture is not None:
            mp.setattr(ActionDispatcher, "_capture_clicked_node", spy)
        asyncio.run(main())
    return out


@pytest.mark.parametrize("case", ["/input", "/tab"])
@pytest.mark.parametrize("route", ROUTES + DIRECT)
def test_replaced_node_is_not_clicked_node(rows, case, route):
    row = next(r for r in rows if r["case"] == case and r["route"] == route)
    truth = row["truth"]
    # 조건 성립: 클릭한 노드는 끊겼고, 새 노드(같은 selector)가 포커스를 받았다(탭은 aria-selected=true 도).
    assert truth["oldConnected"] is False and truth["sameNode"] is False, row
    assert truth["activeIsClicked"] is False and truth["activeIsCurrent"] is True, row
    if case == "/tab":
        assert truth["currentSelected"] == "true", row
    wire = row["wire"]
    assert wire["success"] is False, row
    signals = ((wire.get("data") or {}).get("signals") or [])
    assert "focus_target" not in signals and "target_selected" not in signals, row
    if route != "batch":
        assert wire["error_code"] == "E_TIMEOUT", row  # 기준 실패 그대로
    if _HAS_CAPTURE:  # 경로 고유성: 묶음 경로는 게이트 대조 정체 재사용, 직접 경로는 클릭 직전 새 결속
        assert row["reused"] == ([False] if route in DIRECT else [True]), row


@pytest.mark.parametrize("case,signal", [("/plain", "focus_target"), ("/tab_on", "target_selected")])
@pytest.mark.parametrize("route", DIRECT)
def test_direct_same_node_purpose_still_succeeds(rows, case, signal, route):
    row = next(r for r in rows if r["case"] == case and r["route"] == route)
    assert row["wire"]["success"] is True and signal in row["wire"]["data"]["signals"], row
    # 이 경로는 게이트 묶음 정체 재사용 없이 클릭 직전에 새로 결속했다.
    assert row["reused"] == [False], row


def test_recorded_focus_replay_rejects_replacement(rows):
    row = next((r for r in rows if r["route"] == "recipe"), None)
    if row is None:  # 기준(WS-90 이전)은 포커스만 하는 클릭을 기록하지 못한다 — 위 직접 결속 대조는 그대로 돈다
        pytest.skip("baseline cannot record a focus-only click; direct node-binding controls above remain active")
    assert row["step"]["expect"]["signals"] == []
    assert row["truth"]["oldConnected"] is False and row["truth"]["activeIsClicked"] is False
    assert row["wire"]["success"] is False, row
