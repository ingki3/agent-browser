"""WS-64 R4 — R3 재검증 BLOCKING 2건 재현(실제 Chromium·로컬 서버/route fulfill·공개 call_tool 경로, 제품·Playwright
monkeypatch 없음).

B1 같은 문서 SPA 재렌더 과잉 거부: 목록이 있을 때 대기 중 같은 Document 안에서 비활성 노드를 같은 selector·이름의 활성
노드로 바꾸면(SPA 재렌더) 정상 입력이어야 한다 — 다른 Document 로 바뀐 경우(R3 TOCTOU)와 구별한다.
B2 adoptNode: 최상위 문서는 그대로 두고 대기 중 대상 노드를 목록 사이트 iframe 문서로 옮기면(document.domain 완화) 연결은
유지되지만 소유 문서·프레임이 바뀐다 — 그 노드에 입력하면 목록 사이트에 입력한 것이다(효과 0이어야 함).
구조: 해석(노드) → 효과 없는 대기(trial·상태 확인) → 소유 프레임 관문·결속 → 대기 없는 즉시 입력.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer
from security.manual_sites import ManualSites

from test_run_cli import requires_chromium  # noqa: F401
from test_ws64_manual_server import _call, _eid, _epoch
from test_ws64_r3_server import _Local, _act, _reason, _server

pytestmark = requires_chromium

_SPA = [
    (ActionType.CLICK, "Local button", {}, "#go"),
    (ActionType.TYPE_TEXT, "Local text", {"text": "abc"}, "#q"),
    (ActionType.CHECK_BOX, "Local check", {"checked": True}, "#c"),
    (ActionType.SELECT_OPTION, "Local select", {"value": "b"}, "#s"),
]


@pytest.mark.parametrize("listed", [True, False], ids=["list", "empty"])
@pytest.mark.parametrize("change", ["replace", "enable"])
@pytest.mark.parametrize("action,name,params,selector", _SPA, ids=[a[0].value for a in _SPA])
async def test_same_document_rerender_succeeds(tmp_path, listed, change, action, name, params, selector):
    """B1: 대기 중 같은 Document 안 재렌더(새 노드)·활성화 — 목록 유무와 무관하게 정상 성공, 입력은 새 노드 한 번."""
    with _Local() as local:
        srv = _server(tmp_path, local, listed=listed)
        await srv.start()
        pending = None
        try:
            page = srv._page
            assert (await _call(srv, ActionType.NAVIGATE, url=local.url(False, "/wait?case=spa"))).success
            await page.evaluate("()=>{document.querySelector('#cover').remove();window.__docmark={};window.__spa=[];"
                                "for(const kind of ['input','click'])document.addEventListener(kind,e=>"
                                "{window.__spa.push({kind,trusted:e.isTrusted,id:e.target.id});"
                                "document.querySelector('#out').textContent=kind+':'+e.target.id;});}")
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            pending = asyncio.create_task(_act(srv, action, element_id=_eid(obs, name), epoch=_epoch(obs), **params))
            await asyncio.sleep(0.6)
            assert not pending.done(), (await pending).model_dump(mode="json")
            await page.evaluate("([sel,replace])=>{const old=document.querySelector(sel);let n=old;"
                                "if(replace){n=old.cloneNode(true);old.replaceWith(n);}n.disabled=false;}",
                                [selector, change == "replace"])
            r = await asyncio.wait_for(pending, 12)
            state = await page.evaluate("({mark:!!window.__docmark,q:document.querySelector('#q').value,"
                                        "c:document.querySelector('#c').checked,s:document.querySelector('#s').value,"
                                        "spa:window.__spa})")
            out = {"response": r.model_dump(mode="json"), "state": state}
            assert state["mark"], out
            assert r.success, out
            if action is ActionType.TYPE_TEXT:
                assert state["q"] == "abc", out
            elif action is ActionType.CHECK_BOX:
                assert state["c"] is True, out
            elif action is ActionType.SELECT_OPTION:
                assert state["s"] == "b", out
            else:
                assert [e["kind"] for e in state["spa"]] == ["click"] and state["spa"][0]["trusted"], out
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await srv.close()


async def test_same_document_rerender_to_different_button_refused(tmp_path):
    """B1 대조: 같은 문서 재해석에도 기존 신원 검사 — 같은 selector 의 노드가 다른 이름(다른 버튼)이면 누르지 않는다."""
    with _Local() as local:
        srv = _server(tmp_path, local)
        await srv.start()
        pending = None
        try:
            page = srv._page
            assert (await _call(srv, ActionType.NAVIGATE, url=local.url(False, "/wait?case=swap"))).success
            await page.evaluate("()=>{document.querySelector('#cover').remove();window.__clicks=0;"
                                "document.addEventListener('click',()=>window.__clicks++,true);}")
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            pending = asyncio.create_task(_call(srv, ActionType.CLICK, element_id=_eid(obs, "Local button"),
                                                epoch=_epoch(obs)))
            await asyncio.sleep(0.6)
            assert not pending.done(), (await pending).model_dump(mode="json")
            await page.evaluate("()=>{const old=document.querySelector('#go');const n=document.createElement('button');"
                                "n.id='go';n.textContent='Delete account';n.setAttribute('aria-label','Delete account');"
                                "old.replaceWith(n);}")
            r = await asyncio.wait_for(pending, 12)
            assert r.success is False and r.error_code is ErrorCode.TOCTOU_MISMATCH, r.model_dump(mode="json")
            assert r.reobserve_required is True
            assert await page.evaluate("window.__clicks") == 0
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await srv.close()


_ADOPT = [
    (ActionType.CLICK, "#go", "Local button", {}),
    (ActionType.TYPE_TEXT, "#q", "Local text", {"text": "abc"}),
    (ActionType.CHECK_BOX, "#c", "Local check", {"checked": True}),
    (ActionType.SELECT_OPTION, "#s", "Local select", {"value": "b"}),
]

_FRAME_BODY = "<!doctype html><script>document.domain='local.test';window.__adopt=[];</script><div id=out></div>"
_TOP_BODY = (
    "<!doctype html><script>document.domain='local.test';window.__docmark={};</script>"
    "<button id=go disabled>Local button</button><input id=q aria-label='Local text' disabled value=PRESERVE>"
    "<label><input id=c type=checkbox aria-label='Local check' disabled>Local check</label>"
    "<select id=s aria-label='Local select' disabled><option value=a>A</option><option value=b>B</option></select>"
    "<iframe id=frame src='http://b.local.test/embed' style='width:800px;height:400px'></iframe><div id=out></div>"
)
_ADOPT_JS = (
    "([sel,fx])=>{const f=document.querySelector('iframe');const doc=f.contentDocument;"
    "const node=doc.adoptNode(document.querySelector(sel));doc.body.appendChild(node);"
    "for(const kind of ['click','input'])node.addEventListener(kind,e=>{doc.defaultView.__adopt.push({kind,"
    "trusted:e.isTrusted});const i=new doc.defaultView.Image();i.src=fx;doc.defaultView.__img=i;});"
    "node.disabled=false;return {top:!!window.__docmark,connected:node.isConnected,owner:node.ownerDocument.URL};}"
)


@pytest.mark.parametrize("frame_listed", [True, False], ids=["listed-frame", "unlisted-frame"])
@pytest.mark.parametrize("action,selector,name,params", _ADOPT, ids=[a[0].value for a in _ADOPT])
async def test_adopted_into_listed_frame_gets_no_input(tmp_path, action, selector, name, params, frame_listed):
    """B2: 대기 중 다른 프레임 문서로 adoptNode 된 노드(연결 유지·최상위 문서 동일)에는 입력하지 않는다(효과·요청 0) —
    그 프레임이 목록이면 실제 소유 프레임 관문으로 manual_site, 목록 밖이면 보수적으로 '대상 바뀜'(E_TOCTOU_MISMATCH)."""
    with _Local() as local:
        srv = BrowserMCPServer(manual_sites=ManualSites.of(["b.local.test" if frame_listed else "c.local.test"]),
                               recipes=False, handoff_root=tmp_path / "hub")
        await srv.start()
        pending = None
        try:
            page = srv._page

            async def route(r: Any) -> None:
                url = r.request.url
                if url.endswith("/robots.txt"):
                    body = "User-agent: *\nAllow: /\n"
                elif url.startswith("http://b.local.test"):
                    body = _FRAME_BODY
                else:
                    body = _TOP_BODY
                await r.fulfill(status=200, headers={"Content-Type": "text/html", "Origin-Agent-Cluster": "?0"},
                                body=body)

            await page.context.route("http://*.local.test/**", route)
            assert (await _call(srv, ActionType.NAVIGATE, url="http://a.local.test/main")).success
            await page.wait_for_function("document.querySelector('iframe').contentDocument && "
                                         "document.querySelector('iframe').contentDocument.domain==='local.test'")
            frame = page.frames[1]
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            eid = _eid(obs, name)
            pending = asyncio.create_task(_call(srv, action, element_id=eid, epoch=_epoch(obs), **params))
            await asyncio.sleep(0.6)
            assert not pending.done(), (await pending).model_dump(mode="json")
            case = "adopt-" + action.value
            adopt = await page.evaluate(_ADOPT_JS, [selector, local.url(True, f"/effect?case={case}&kind=input")])
            assert adopt == {"top": True, "connected": True, "owner": "http://b.local.test/embed"}, adopt
            r = await asyncio.wait_for(pending, 15)
            await asyncio.sleep(0.3)
            out: Dict[str, Any] = {"response": r.model_dump(mode="json")}
            assert await frame.evaluate("window.__adopt") == [] and local.effects(case) == [], out
            assert r.success is False, out
            if frame_listed:
                assert _reason(r) == "manual_site", out
            else:
                assert r.error_code is ErrorCode.TOCTOU_MISMATCH and r.reobserve_required is True, out
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await srv.close()


async def test_delayed_enable_same_node_listed(tmp_path):
    """유지: 목록이 있어도 같은 노드가 늦게 활성화되면(대기 → 활성) 정상 클릭 — 효과 한 번."""
    with _Local() as local:
        srv = _server(tmp_path, local)
        await srv.start()
        pending = None
        try:
            page = srv._page
            assert (await _call(srv, ActionType.NAVIGATE, url=local.url(False, "/wait?case=late"))).success
            await page.evaluate("()=>document.querySelector('#cover').remove()")
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            pending = asyncio.create_task(_call(srv, ActionType.CLICK, element_id=_eid(obs, "Local button"),
                                                epoch=_epoch(obs)))
            await asyncio.sleep(0.6)
            assert not pending.done()
            await page.evaluate("()=>{document.querySelector('#go').disabled=false;}")
            r = await asyncio.wait_for(pending, 12)
            assert r.success, r.model_dump(mode="json")
            await asyncio.sleep(0.3)
            assert [e["kind"] for e in await page.evaluate("window.__ev")] == ["click"]
            assert len(local.effects("late")) == 1
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await srv.close()


_SHADOW_BODY = (
    "<!doctype html><div id=host></div><script>const r=document.getElementById('host').attachShadow({mode:'open'});"
    "r.innerHTML='<button id=sb>Shadow note</button>';r.getElementById('sb').addEventListener('click',e=>"
    "{document.body.setAttribute('data-result',e.isTrusted?'ok':'untrusted');"
    "const p=document.createElement('p');p.textContent='clicked';document.body.appendChild(p);});</script>"
)


async def test_open_shadow_button_hit_target_with_list(tmp_path):
    """유지: 목록이 있을 때 효과 없는 actionability 확인의 hit target 이 open shadow 안 노드를 맞게 본다(문서
    elementFromPoint 는 host 를 돌려준다) — 클릭이 기다리다 시간 초과로 끝나지 않고 효과가 난다."""
    srv = BrowserMCPServer(manual_sites=ManualSites.of(["b.local.test"]), recipes=False, handoff_root=tmp_path / "hub")
    await srv.start()
    try:
        page = srv._page

        async def route(r: Any) -> None:
            body = "User-agent: *\nAllow: /\n" if r.request.url.endswith("/robots.txt") else _SHADOW_BODY
            await r.fulfill(status=200, headers={"Content-Type": "text/html"}, body=body)

        await page.context.route("http://*.local.test/**", route)
        assert (await _call(srv, ActionType.NAVIGATE, url="http://a.local.test/shadow")).success
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r = await _call(srv, ActionType.CLICK, element_id=_eid(obs, "Shadow note"), epoch=_epoch(obs))
        assert await page.evaluate("document.body.getAttribute('data-result')") == "ok", (r.error_code, r.error_message)
        assert r.success, r.model_dump(mode="json")
    finally:
        await srv.close()
