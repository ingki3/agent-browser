"""WS-64 R3 — R2 재검증 BLOCKING 2건 재현(실제 Chromium·로컬 서버·공개 call_tool 경로, 제품·Playwright monkeypatch 없음).

B1 단일 Playwright 호출 안 재해석: 대상이 비활성(또는 가려짐)이라 Playwright 가 actionability 를 기다리는 동안 바깥에서
문서를 목록 사이트로 바꾸면, Locator 는 같은 selector 의 **새 문서 노드**를 잡아 입력했다. 목록이 있으면 관문을 통과한
문서에 고정된 노드(ElementHandle)로만 입력 — 문서가 바뀌면 그 노드가 떨어져 실패('대상 바뀜' 계열), 새 노드로 넘어가지 않는다.
B2 lineage 과잉: 목록 A → 일반 B 이동을 마친 뒤 B 가 만든 새 불투명 팝업은 A 이력을 물려받지 않는다(생성 시점 살아 있던
문서만). 생성과 opener 이동이 겹치면(경합) 생성 순간의 문서(목록)로 막는다.
부작용은 로컬 서버 /effect 요청 수·페이지 기록(isTrusted)으로만 잰다. 실제 사이트에 접속하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlsplit

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer
from security.manual_sites import ManualSites

from test_run_cli import requires_chromium  # noqa: F401
from test_ws64_manual_server import _call, _eid, _epoch

pytestmark = requires_chromium

_SCRIPT = """<script>window.__ev=[];
function fx(kind,e){window.__ev.push({kind:kind,trusted:e.isTrusted,origin:location.origin});
 document.getElementById('out').textContent=kind+window.__ev.length;
 const q=new URLSearchParams(location.search);const i=new Image();
 i.src='/effect?case='+q.get('case')+'&kind='+kind;(window.__img=window.__img||[]).push(i);}
document.getElementById('go').addEventListener('click',e=>fx('click',e));
document.getElementById('q').addEventListener('input',e=>fx('input',e));
document.getElementById('c').addEventListener('input',e=>fx('check',e));
document.getElementById('s').addEventListener('input',e=>fx('select',e));
document.getElementById('h').addEventListener('mouseover',e=>fx('hover',e));
document.getElementById('f').addEventListener('input',e=>fx('upload',e));
document.getElementById('d').addEventListener('click',e=>{fx('download',e);
 const a=document.createElement('a');a.href='/file';a.download='r3.txt';document.body.appendChild(a);a.click();});
</script>"""


def _markup(wait: bool, moved: bool, checked: bool) -> str:
    dis = " disabled" if wait else ""
    chk = " checked" if checked else ""
    # 자가 치유 경로: moved 면 버튼 id·구조가 바뀐다(관찰한 css 경로로 못 찾음 → 이름으로 치유).
    button = (f"<div class=wrap><button id=go2 aria-label='Local button'{dis}>Local button</button></div>"
              if moved else f"<button id=go aria-label='Local button'{dis}>Local button</button>")
    # 가리개(hover 대기): 대상 위를 덮어 hit-target 을 막는다 — ready 문서에는 없다.
    cover = ("<div id=cover style='position:fixed;left:0;top:0;width:100vw;height:100vh;z-index:9'></div>"
             if wait else "")
    return ("<!doctype html><meta charset=utf-8><title>R3 local</title>" + button
            + f"<input id=q aria-label='Local text' value=PRESERVE{dis}>"
            + f"<label><input id=c type=checkbox aria-label='Local check'{dis}{chk}>Local check</label>"
            + f"<select id=s aria-label='Local select'{dis}><option value=a>A</option><option value=b>B</option></select>"
            + "<div id=h role=button aria-label='Local hover' style='width:120px;height:30px'>hover</div>"
            + f"<input id=f type=file aria-label='Local file'{dis}>"
            + f"<button id=d aria-label='Local download'{dis}>Local download</button>"
            + "<div id=out></div>" + cover + _SCRIPT.replace("getElementById('go')", "getElementById('" + ("go2" if moved else "go") + "')"))


class _Local:
    """목록 출처 = 127.0.0.1:port, 목록 밖 = localhost:port (같은 서버, 다른 출처)."""

    def __enter__(self) -> "_Local":
        self.rows: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                q = parse_qs(parts.query)
                with owner.lock:
                    owner.rows.append({"path": self.path, "host": self.headers.get("Host")})
                if parts.path == "/robots.txt":
                    body, ctype = b"User-agent: *\nAllow: /\n", "text/plain"
                elif parts.path == "/effect":
                    body, ctype = b"ok", "text/plain"
                elif parts.path == "/file":
                    body, ctype = b"r3", "application/octet-stream"
                else:
                    body = _markup(parts.path == "/wait", "moved" in q, "checked" in q).encode()
                    ctype = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                if parts.path == "/file":
                    self.send_header("Content-Disposition", "attachment; filename=r3.txt")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *_e: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, listed: bool, path: str) -> str:
        return f"http://{'127.0.0.1' if listed else 'localhost'}:{self.port}{path}"

    def effects(self, case: str) -> List[Dict[str, Any]]:
        with self.lock:
            return [r for r in self.rows if r["path"].startswith(f"/effect?case={case}&")]


def _server(tmp_path: Any, local: _Local, listed: bool = True) -> BrowserMCPServer:
    sites = ManualSites.of([f"127.0.0.1:{local.port}"]) if listed else ManualSites()
    return BrowserMCPServer(handoff_root=tmp_path / "hub", manual_sites=sites, recipes=False)


#: 사람 승인(HITL) 대상 액션은 서버 게이트가 먼저 멈추므로 디스패처(백스톱 관문 포함)로 직접 부른다.
_DIRECT = (ActionType.UPLOAD_FILE, ActionType.DOWNLOAD_FILE)


async def _act(srv: BrowserMCPServer, action: ActionType, **args: Any) -> Any:
    if action in _DIRECT:
        return await srv._dispatcher.dispatch(action, dict(args))
    return await _call(srv, action, **args)


def _reason(r: Any) -> Any:
    return ((r.data or {}).get("handoff") or {}).get("reason")


# (액션, 대상 이름, 인자, 대기 문서 쿼리) — 대기: 비활성(값·포인터 입력) 또는 가리개(hover)
_RACE = [
    ("click-selector", ActionType.CLICK, None, {"selector": "#go"}, ""),
    ("click-element", ActionType.CLICK, "Local button", {}, ""),
    ("type_text", ActionType.TYPE_TEXT, "Local text", {"text": "new"}, ""),
    ("check_box", ActionType.CHECK_BOX, "Local check", {"checked": True}, ""),
    ("uncheck", ActionType.CHECK_BOX, "Local check", {"checked": False}, "&checked=1"),
    ("select_option", ActionType.SELECT_OPTION, "Local select", {"value": "b"}, ""),
    ("hover", ActionType.HOVER, "Local hover", {}, ""),
    ("download", ActionType.DOWNLOAD_FILE, "Local download", {}, ""),
]


async def _race(tmp_path: Any, local: _Local, srv: BrowserMCPServer, case: str, action: ActionType,
                name: Any, params: Dict[str, Any], extra: str, dest_listed: bool, path: str = "/wait") -> Any:
    page = srv._page
    nav = await _call(srv, ActionType.NAVIGATE, url=local.url(False, f"{path}?case={case}{extra}"))
    assert nav.success, nav.error_message
    obs = await _call(srv, ActionType.OBSERVE_PAGE)
    assert obs.success, obs.error_message
    args = dict(params)
    if name is not None:
        key = "trigger_element_id" if action is ActionType.DOWNLOAD_FILE else "element_id"
        args[key] = _eid(obs, name)
    if action is ActionType.DOWNLOAD_FILE:
        args["save_dir"] = str(tmp_path / "dl")
    args["epoch"] = _epoch(obs)
    pending = asyncio.create_task(_act(srv, action, **args))
    try:
        await asyncio.sleep(0.6)
        # 조건 성립: 공개 호출이 Playwright 대기 안에 있고 아직 아무 효과가 없다.
        assert not pending.done(), (await pending).model_dump(mode="json")
        assert local.effects(case) == [] and await page.evaluate("window.__ev") == []
        await page.goto(local.url(dest_listed, f"/ready?case={case}{extra}"))
        result = await asyncio.wait_for(pending, 15)
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    await asyncio.sleep(0.3)
    state = await page.evaluate("({origin:location.origin,ev:window.__ev,q:document.querySelector('#q').value,"
                                "c:document.querySelector('#c').checked,s:document.querySelector('#s').value})")
    return result, state


@pytest.mark.parametrize("case,action,name,params,extra", _RACE, ids=[c[0] for c in _RACE])
async def test_single_call_wait_cannot_cross_into_listed_document(tmp_path, case, action, name, params, extra):
    """B1: 대기 중 문서가 목록 사이트로 바뀌면 새 문서에 아무 입력도 가지 않는다(값 보존·서버 요청 0)."""
    with _Local() as local:
        srv = _server(tmp_path, local)
        await srv.start()
        try:
            result, state = await _race(tmp_path, local, srv, case, action, name, params, extra, dest_listed=True)
            out = {"response": result.model_dump(mode="json"), "state": state}
            assert state["origin"] == local.url(True, "")
            assert state["ev"] == [] and local.effects(case) == [], out
            assert state["q"] == "PRESERVE" and state["s"] == "a" and state["c"] is (case == "uncheck"), out
            assert result.success is False, out
            assert result.error_code in (ErrorCode.TOCTOU_MISMATCH, ErrorCode.HITL_UNATTENDED_BLOCKED), out
            assert result.reobserve_required or _reason(result) == "manual_site", out
        finally:
            await srv.close()


@pytest.mark.parametrize("case,action,name,params,extra", _RACE[:3], ids=[c[0] for c in _RACE[:3]])
async def test_fixed_node_with_list_fails_on_document_swap_to_unlisted(tmp_path, case, action, name, params, extra):
    """목록이 있으면 목록 밖 사이트도 고정 노드 — 대기 중 문서가 바뀌면 새 문서 노드로 넘어가지 않고 '대상 바뀜'."""
    with _Local() as local:
        srv = _server(tmp_path, local)
        await srv.start()
        try:
            result, state = await _race(tmp_path, local, srv, case, action, name, params, extra, dest_listed=False)
            out = {"response": result.model_dump(mode="json"), "state": state}
            assert state["ev"] == [] and local.effects(case) == [], out
            assert result.success is False and result.error_code is ErrorCode.TOCTOU_MISMATCH, out
            assert result.reobserve_required is True and result.retry_safe is True, out
        finally:
            await srv.close()


async def test_typing_stops_when_fixed_node_document_swapped_to_unlisted(tmp_path, monkeypatch):
    """type_text 의 글자 단계: 세 번째 글자를 보내는 동안 문서가 (목록 밖) 새 문서로 바뀌면 고정 노드가 떨어졌으니 남은
    글자를 새 문서의 포커스로 보내지 않는다(키 0) — 관문은 목록 밖이라 통과하므로 고정 노드 확인만이 막는다."""
    with _Local() as local:
        srv = _server(tmp_path, local)
        await srv.start()
        try:
            page = srv._page
            assert (await _call(srv, ActionType.NAVIGATE, url=local.url(False, "/ready?case=keys"))).success
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            keyboard = page.keyboard
            real_type = keyboard.type
            sent: List[str] = []

            async def type_then_maybe_move(text: str, **kw: Any) -> None:
                await real_type(text, **kw)
                sent.append(text)
                if len(sent) == 3:  # 세 번째 글자 직후 바깥에서 이동이 끝남(관문·제품 함수는 그대로)
                    await page.goto(local.url(False, "/ready?case=keys2"))
                    await page.evaluate("()=>{window.__keys=0;document.addEventListener('keydown',()=>window.__keys++);"
                                        "document.querySelector('#q').focus();}")

            monkeypatch.setattr(keyboard, "type", type_then_maybe_move)
            r = await _call(srv, ActionType.TYPE_TEXT, element_id=_eid(obs, "Local text"), epoch=_epoch(obs),
                            text="abcdef", clear_before=False)
            assert sent == ["a", "b", "c"], sent
            assert r.success is False and r.error_code is ErrorCode.TOCTOU_MISMATCH, r.model_dump(mode="json")
            assert await page.evaluate("[document.querySelector('#q').value, window.__keys]") == ["PRESERVE", 0]
        finally:
            await srv.close()


async def test_empty_list_keeps_locator_path(tmp_path):
    """빈 목록은 기존 Locator 경로 그대로(회귀 대조): 대기 중 바뀐 목록 밖 문서의 같은 노드를 누른다."""
    with _Local() as local:
        srv = _server(tmp_path, local, listed=False)
        await srv.start()
        try:
            result, state = await _race(tmp_path, local, srv, "empty", ActionType.CLICK, None, {"selector": "#go"},
                                        "", dest_listed=False)
            assert result.success, result.error_message
            assert [e["kind"] for e in state["ev"]] == ["click"] and state["ev"][0]["trusted"]
            assert len(local.effects("empty")) == 1
        finally:
            await srv.close()


@pytest.mark.parametrize("dest_listed", [True, False])
async def test_healed_handle_is_gated_and_fixed(tmp_path, dest_listed):
    """자가 치유 경로: 관찰 뒤 버튼이 바뀌어(id·구조) 이름으로 치유한 새 핸들도 그 노드에 고정·관문 — 대기 중 문서가
    바뀌면 새 문서로 넘어가지 않는다(목록이면 효과 0)."""
    with _Local() as local:
        srv = _server(tmp_path, local)
        await srv.start()
        try:
            page = srv._page
            assert (await _call(srv, ActionType.NAVIGATE, url=local.url(False, "/wait?case=heal"))).success
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            eid = _eid(obs, "Local button")
            # 관찰한 노드를 다른 구조·id 의 같은 이름 버튼으로 바꾼다(비활성 유지) — 치유가 새 핸들을 고른다.
            await page.evaluate("()=>{const b=document.getElementById('go');const w=document.createElement('div');"
                                "w.className='wrap';const n=b.cloneNode(true);n.id='go2';w.appendChild(n);"
                                "b.replaceWith(w);}")
            pending = asyncio.create_task(_call(srv, ActionType.CLICK, element_id=eid, epoch=_epoch(obs)))
            try:
                await asyncio.sleep(0.6)
                assert not pending.done(), (await pending).model_dump(mode="json")
                await page.goto(local.url(dest_listed, "/ready?case=heal&moved=1"))
                result = await asyncio.wait_for(pending, 15)
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            await asyncio.sleep(0.3)
            assert await page.evaluate("window.__ev") == [] and local.effects("heal") == []
            assert result.success is False, result.model_dump(mode="json")
        finally:
            await srv.close()


async def test_healed_handle_normal_success(tmp_path):
    """치유 대조: 문서가 그대로면 치유한 새 핸들(고정 노드)로 정상 클릭된다."""
    with _Local() as local:
        srv = _server(tmp_path, local)
        await srv.start()
        try:
            page = srv._page
            assert (await _call(srv, ActionType.NAVIGATE, url=local.url(False, "/ready?case=heal-ok"))).success
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            eid = _eid(obs, "Local button")
            await page.evaluate("()=>{const b=document.getElementById('go');const w=document.createElement('div');"
                                "w.className='wrap';const n=b.cloneNode(true);n.id='go2';"
                                "n.addEventListener('click',e=>fx('click',e));w.appendChild(n);b.replaceWith(w);}")
            result = await _call(srv, ActionType.CLICK, element_id=eid, epoch=_epoch(obs))
            assert result.success and result.healed, result.model_dump(mode="json")
            await asyncio.sleep(0.2)
            assert [e["kind"] for e in await page.evaluate("window.__ev")] == ["click"]
        finally:
            await srv.close()


@pytest.mark.parametrize("listed", [True, False])
async def test_fixed_node_actions_still_succeed_on_unlisted_site(tmp_path, listed):
    """회귀 대조: 목록이 있어도(고정 노드) 목록 밖 사이트의 보통 입력(문서 교체 없음)은 빈 목록(Locator)과 똑같이 성공한다.
    (다운로드 저장은 서버 컨텍스트의 다운로드 정리 경로가 빈 목록에서도 취소해 여기서 재지 않는다 — 경합 시험만.)"""
    with _Local() as local:
        srv = _server(tmp_path, local, listed=listed)
        await srv.start()
        try:
            page = srv._page
            upload = tmp_path / "up.txt"
            upload.write_text("x")
            assert (await _call(srv, ActionType.NAVIGATE, url=local.url(False, "/ready?case=ok"))).success
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            ep = _epoch(obs)
            steps = [
                (ActionType.CLICK, {"element_id": _eid(obs, "Local button")}),
                (ActionType.CLICK, {"selector": "#go"}),
                (ActionType.TYPE_TEXT, {"element_id": _eid(obs, "Local text"), "text": "abc"}),
                (ActionType.CHECK_BOX, {"element_id": _eid(obs, "Local check"), "checked": True}),
                (ActionType.CHECK_BOX, {"element_id": _eid(obs, "Local check"), "checked": False}),
                (ActionType.SELECT_OPTION, {"element_id": _eid(obs, "Local select"), "value": "b"}),
                (ActionType.HOVER, {"element_id": _eid(obs, "Local hover")}),
                (ActionType.UPLOAD_FILE, {"element_id": _eid(obs, "Local file"), "file_paths": [str(upload)]}),
            ]
            for action, args in steps:
                r = await _act(srv, action, epoch=ep, **args)
                assert r.success, (action, r.model_dump(mode="json"))
                if r.data.get("snapshot_epoch") is not None:
                    ep = r.data["snapshot_epoch"]
                else:
                    o = await _call(srv, ActionType.OBSERVE_PAGE)
                    ep = _epoch(o)
            kinds = [e["kind"] for e in await page.evaluate("window.__ev")]
            assert {"click", "input", "check", "select", "hover", "upload"} <= set(kinds), kinds
            assert await page.evaluate("[document.querySelector('#q').value, document.querySelector('#s').value]") == \
                ["abc", "b"]
        finally:
            await srv.close()


# ======================================================================= B2 lineage 과잉


def _opaque_creator(effect_url: str, after: str = "") -> str:
    markup = ("<button id=go>Local button</button><div id=out></div><script>window.__effects=[];"
              "document.querySelector('#go').onclick=e=>{window.__effects.push({trusted:e.isTrusted,origin:self.origin});"
              "document.querySelector('#out').textContent='clicked';const i=new Image();i.src=" + json.dumps(effect_url) + ";window.__i=i;};</script>")
    return ("<script>const b=new Blob([" + json.dumps(markup).replace("<", "\\u003c")
            + "],{type:'text/html'});window.open(URL.createObjectURL(b),'r3-popup');" + after + "</script>")


async def _click_popup(srv: BrowserMCPServer, popup: Any) -> Any:
    tabs = await _call(srv, ActionType.TAB_CONTROL, command="list")
    tab = next(t for t in tabs.data["tabs"] if t["url"] == popup.url)
    assert (await _call(srv, ActionType.TAB_CONTROL, command="switch", tab_id=tab["tab_id"])).success
    obs = await _call(srv, ActionType.OBSERVE_PAGE)
    return await _call(srv, ActionType.CLICK, selector="#go", epoch=_epoch(obs))


@pytest.mark.parametrize("visited_listed", [True, False])
async def test_new_popup_of_unlisted_document_not_tainted_by_old_visit(tmp_path, visited_listed):
    """B2: 목록 A → 일반 B 이동 완료 뒤 B 가 만든 새 불투명 팝업은 A 이력 없이 정상 클릭(과거 방문 무관)."""
    with _Local() as local:
        srv = _server(tmp_path, local)
        await srv.start()
        try:
            if visited_listed:
                assert (await _call(srv, ActionType.NAVIGATE, url=local.url(True, "/ready?case=old"))).success
            assert (await _call(srv, ActionType.NAVIGATE, url=local.url(False, "/ready?case=b"))).success
            page = srv._page
            assert len(page.frames) == 1
            effect = local.url(False, "/effect?case=popup&kind=click")
            async with page.context.expect_page(timeout=5000) as opened:
                await page.evaluate("s=>{const f=document.createElement('iframe');f.sandbox='allow-scripts allow-popups';"
                                    "f.srcdoc=s;document.body.appendChild(f);}", _opaque_creator(effect))
            popup = await opened.value
            await popup.wait_for_selector("#go")
            assert popup.url.startswith("blob:null/") and await popup.evaluate("self.origin") == "null"
            r = await _click_popup(srv, popup)
            await asyncio.sleep(0.3)
            assert r.success, r.model_dump(mode="json")
            assert await popup.evaluate("window.__effects") == [{"trusted": True, "origin": "null"}]
            assert len(local.effects("popup")) == 1
        finally:
            await srv.close()


#: None: 연 뒤 이동 요청 / 0: 이동 요청 직후 같은 틱 뒤에 연다 — 실측 약 1/3 에서 opener 이동 이벤트가 팝업 이벤트보다 먼저
#: 온다(경합 성립, 오류 메시지의 order). 2ms 이상은 A 가 내려간 뒤라 팝업이 안 생겨 쓰지 않는다.
@pytest.mark.parametrize("delay_ms", [None, 0])
async def test_popup_created_by_listed_document_racing_navigation_is_blocked(tmp_path, delay_ms):
    """B2 경합: 목록 문서 A 의 sandbox iframe 이 팝업을 열자마자 A 가 일반 B 로 떠나도(opener 이동 이벤트가 팝업 이벤트보다
    먼저 올 수 있음) 생성 순간 살아 있던 A 의 lineage 로 막는다."""
    with _Local() as local:
        srv = _server(tmp_path, local)
        await srv.start()
        try:
            assert (await _call(srv, ActionType.NAVIGATE, url=local.url(True, "/ready?case=a"))).success
            page = srv._page
            effect = local.url(True, "/effect?case=race&kind=click")
            dest = local.url(False, "/ready?case=b")
            # 열자마자 부모에게 알림 → 부모(목록 A)가 곧바로 목록 밖 B 로 최상위 이동.
            await page.evaluate("d=>{window.addEventListener('message',()=>{location.href=d;});}", dest)
            order: List[str] = []
            page.on("framenavigated", lambda f: order.append("nav:" + f.url) if f == page.main_frame else None)
            page.context.on("page", lambda p: order.append("popup"))
            if delay_ms is None:  # 열고 나서 이동 요청
                creator = _opaque_creator(effect, "parent.postMessage('opened','*');")
            else:  # 이동 요청을 먼저 보내고 delay_ms 뒤 연다(A 가 아직 살아 있는 동안 — 이동 확정과 팝업 이벤트가 겹침)
                creator = _opaque_creator(effect).replace(
                    "window.open(", "parent.postMessage('go','*');setTimeout(()=>window.open(").replace(
                    ",'r3-popup');", ",'r3-popup')," + str(delay_ms) + ");")
            async with page.context.expect_page(timeout=5000) as opened:
                await page.evaluate("s=>{const f=document.createElement('iframe');f.sandbox='allow-scripts allow-popups';"
                                    "f.srcdoc=s;document.body.appendChild(f);}", creator)
            popup = await opened.value
            await popup.wait_for_selector("#go")
            await page.wait_for_url(dest)
            r = await _click_popup(srv, popup)
            await asyncio.sleep(0.3)
            assert _reason(r) == "manual_site", (order, r.model_dump(mode="json"))
            assert await popup.evaluate("window.__effects") == [] and local.effects("race") == []
        finally:
            await srv.close()
