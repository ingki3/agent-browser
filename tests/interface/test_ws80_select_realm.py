"""WS-80 R1 (검증 B1): 프레임·shadow 안 select 도 페이지가 오염할 수 없는 곳(격리 world)에서 해석·확인한다.

페이지 main world 가 HTMLSelectElement.options getter·Array.prototype.push·Object.prototype.then 을 바꿔도
B 요청에 A 를 고르고 성공을 보고하면 안 된다(실제 B 선택 또는 실패). 공용 응답 경로(BrowserMCPServer.call_tool)로
부르고, 실제 선택값은 응답·페이지 평가 디코더와 분리된 별도 CDP 세션(Runtime.evaluate)으로 읽는다.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401

SELECT = '<select id=s aria-label="지역"><option value=A>A</option><option value=B>B</option></select>'
TAMPER: Dict[str, str] = {
    "none": "",
    "options": ("const rd=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'options').get;"
                "Object.defineProperty(HTMLSelectElement.prototype,'options',{get(){const x=rd.call(this);return ["
                "{value:'B',label:'B',selected:x[0].selected,disabled:false,parentElement:null},"
                "{value:'A',label:'A',selected:x[1].selected,disabled:false,parentElement:null}];}});"),
    "array": ("const push=Array.prototype.push;Array.prototype.push=function(...a){for(const o of a){"
              "if(o && typeof o==='object' && Object.hasOwn(o,'label') && Object.hasOwn(o,'value')){"
              "if(o.value==='A'){o.value='B';o.label='B'}else if(o.value==='B'){o.value='A';o.label='A'}}}"
              "return Reflect.apply(push,this,a)};"),
    "object": ("Object.defineProperty(Object.prototype,'then',{configurable:true,value:function(resolve){"
               "Object.defineProperty(this,'then',{value:undefined,configurable:true});"
               "if(this.verdict==='ok'){this.index=0;this.value='A';this.label='A'}"
               "if(this.verdict==='match'||this.verdict==='differ'){this.verdict='match'}resolve(this)}});"),
}
MODES = ("top", "frame", "shadow")


def _body(mode: str, tamper: str) -> str:
    body = SELECT if mode != "shadow" else (
        "<div id=host></div><script>host.attachShadow({mode:'open'}).innerHTML=" + json.dumps(SELECT) + "</script>")
    return body + "<script>" + TAMPER[tamper] + "</script>"


PAGES: Dict[str, str] = {}
for _m in MODES:
    for _t in TAMPER:
        if _m == "frame":
            PAGES[f"inner-{_m}-{_t}"] = _body(_m, _t)
            PAGES[f"{_m}-{_t}"] = f'<iframe id=f src="/inner-{_m}-{_t}"></iframe>'
        else:
            PAGES[f"{_m}-{_t}"] = _body(_m, _t)
# 프레임 안 shadow 도 같은 규칙(같은 출처 프레임).
for _t in TAMPER:
    PAGES[f"inner-frameshadow-{_t}"] = _body("shadow", _t)
    PAGES[f"frameshadow-{_t}"] = f'<iframe id=f src="/inner-frameshadow-{_t}"></iframe>'
# value·라벨 충돌([value=A label=B][value=B label=A]) — value=B 는 value B(격리 world 해석이 실제로 쓰였다는 증거: 기준
# Playwright valueOrLabel 첫 일치면 option 0 = A). 중복 라벨은 해석만 거부할 수 있다.
COLLIDE = '<select id=s aria-label="지역"><option value=A>B</option><option value=B>A</option></select>'
DUP = '<select id=s aria-label="지역"><option value=A>서울</option><option value=B>서울</option></select>'
for _k, _sel in (("collide", COLLIDE), ("dup", DUP)):
    _sh = "<div id=host></div><script>host.attachShadow({mode:'open'}).innerHTML=" + json.dumps(_sel) + "</script>"
    PAGES[f"top-{_k}"] = _sel
    PAGES[f"shadow-{_k}"] = _sh
    PAGES[f"inner-frame-{_k}"] = _sel
    PAGES[f"frame-{_k}"] = f'<iframe id=f src="/inner-frame-{_k}"></iframe>'
    PAGES[f"inner-frameshadow-{_k}"] = _sh
    PAGES[f"frameshadow-{_k}"] = f'<iframe id=f src="/inner-frameshadow-{_k}"></iframe>'

# 다른 출처 iframe(같은 서버를 localhost 로 — 출처가 다르다). 이 Chromium 에서는 같은 프로세스 프레임으로 뜬다.
for _t in ("none", "options", "collide"):
    PAGES[f"xframe-{_t}"] = f'<iframe id=f src="http://localhost:__PORT__/inner-frame-{_t}"></iframe>'

ACTUAL = {
    "top": "document.querySelector('#s').value",
    "frame": "document.querySelector('#f').contentDocument.querySelector('#s').value",
    "shadow": "document.querySelector('#host').shadowRoot.querySelector('#s').value",
    "xframe": "",
    "frameshadow": "document.querySelector('#f').contentDocument.querySelector('#host').shadowRoot"
                   ".querySelector('#s').value",
}


@pytest.fixture(scope="module")
def site():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            page = PAGES.get(self.path.strip("/"), "<p>x</p>").replace("__PORT__", str(self.server.server_address[1]))
            body = ("<!doctype html><meta charset=utf-8>" + page).encode()
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


async def _actual(s: BrowserMCPServer, mode: str) -> Any:
    """실제 선택값 — 응답·제품 평가 경로와 분리된 새 CDP 세션(최상위 main world 의 원 DOM 접근; 다른 출처 프레임은 그
    프레임 컨텍스트의 원 DOM)."""
    if mode == "xframe":
        return await _actual_xframe(s)
    root = s._dispatcher.ctx.root_page or s._dispatcher.ctx.page  # noqa: SLF001
    cdp = await root.context.new_cdp_session(root)
    try:
        res = await cdp.send("Runtime.evaluate", {"expression": ACTUAL[mode], "returnByValue": True})
    finally:
        await cdp.detach()
    return res["result"]["value"]


async def _actual_xframe(s: BrowserMCPServer) -> Any:
    root = s._dispatcher.ctx.root_page  # noqa: SLF001
    cdp = await root.context.new_cdp_session(root)
    try:
        tree = (await cdp.send("Page.getFrameTree"))["frameTree"]
        frame_id = tree["childFrames"][0]["frame"]["id"]
        world = await cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "ws80-test-probe"})
        res = await cdp.send("Runtime.evaluate", {"expression": "document.querySelector('#s').value",
                                                  "contextId": world["executionContextId"], "returnByValue": True})
    finally:
        await cdp.detach()
    return res["result"]["value"]


async def _run(site: str, tmp_path, mode: str, tamper: str, value: str = "B") -> Dict[str, Any]:
    async with BrowserMCPServer(recipes=False, nav_settle=False, handoff_root=tmp_path / f"{mode}-{tamper}") as s:
        n = await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{site}/{mode}-{tamper}"})
        assert n.success, n.error_message
        if "frame" in mode:
            f = await s.call_tool(tool_name(ActionType.SWITCH_FRAME), {"frame_selector": "#f"})
            assert f.success, f.error_message
        o = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        assert o.success, o.error_message
        e = next(e for e in o.data["observation"]["elements"] if e["role"] == "combobox")
        r = await s.call_tool(tool_name(ActionType.SELECT_OPTION),
                              {"element_id": e["element_id"], "epoch": o.snapshot_epoch, "value": value})
        actual = await _actual(s, mode)
    return {"obs": o, "r": r, "actual": actual}


@requires_chromium
@pytest.mark.parametrize("mode", MODES + ("frameshadow",))
@pytest.mark.parametrize("tamper", list(TAMPER))
async def test_tampered_page_world_never_yields_wrong_success(site, tmp_path, mode, tamper):
    """B 요청 → 실제 B 선택 또는 실패. 성공+A(오선택 후 성공 보고)는 0."""
    out = await _run(site, tmp_path, mode, tamper)
    r, actual = out["r"], out["actual"]
    assert not (r.success and actual != "B"), (mode, tamper, actual, r.model_dump(mode="json"))
    if tamper == "none":  # 비공격: 정상 선택 유지
        assert r.success and actual == "B", (mode, actual, r.model_dump(mode="json"))


@requires_chromium
@pytest.mark.parametrize("tamper", list(TAMPER))
async def test_frame_display_labels_not_forged(site, tmp_path, tamper):
    """프레임 관찰의 data.select_options — 오염 가능한 world 에서 읽은 라벨 순서는 싣지 않는다(실으면 실제 순서)."""
    out = await _run(site, tmp_path, "frame", tamper)
    block = out["obs"].data.get("select_options")
    if tamper == "none":
        assert block, out["obs"].data
    if block:
        assert [o["label"] for o in block[0]["options"]] == ["A", "B"], block


@requires_chromium
@pytest.mark.parametrize("mode", MODES + ("frameshadow",))
async def test_scoped_resolution_really_used(site, tmp_path, mode):
    """비공격 frame/shadow 도 WS-80 해석을 쓴다(격리 world 를 실제로 얻음): value 우선·중복 라벨 즉시 거부."""
    out = await _run(site, tmp_path, mode, "collide")
    assert out["r"].success and out["actual"] == "B", (mode, out["actual"], out["r"].model_dump(mode="json"))
    dup = await _run(site, tmp_path, mode, "dup", value="서울")
    assert dup["r"].success is False and dup["r"].error_code is ErrorCode.OPTION_NOT_FOUND, dup["r"].model_dump(mode="json")
    assert dup["r"].data["select_candidates"] == {"reason": "ambiguous", "indices": [0, 1]}
    assert dup["actual"] == "A"


@requires_chromium
@pytest.mark.parametrize("tamper", ["none", "options", "collide"])
async def test_cross_origin_frame(site, tmp_path, tamper):
    """다른 출처 iframe: 위조 → 성공+A 0, 비공격·충돌은 WS-80 해석으로 value B."""
    out = await _run(site, tmp_path, "xframe", tamper)
    r, actual = out["r"], out["actual"]
    assert not (r.success and actual != "B"), (tamper, actual, r.model_dump(mode="json"))
    if tamper != "options":
        assert r.success and actual == "B", (tamper, actual, r.model_dump(mode="json"))


@requires_chromium
@pytest.mark.parametrize("mode", ["frame", "shadow", "frameshadow"])
async def test_no_isolated_world_fails_closed(site, tmp_path, monkeypatch, mode):
    """격리 world 를 못 얻으면 페이지 world 로 내려가지 않는다 — 표시 없음, 해석 없이 기준 호출(위조 무관하게 B)."""
    from actions.dispatcher import ActionDispatcher

    async def no_world(self):
        return None

    monkeypatch.setattr(ActionDispatcher, "_select_frame_world", no_world)
    out = await _run(site, tmp_path, mode, "options")
    assert "select_options" not in out["obs"].data, out["obs"].data
    r, actual = out["r"], out["actual"]
    assert not (r.success and actual != "B"), (mode, actual, r.model_dump(mode="json"))
    assert r.success and actual == "B", (mode, actual, r.model_dump(mode="json"))
