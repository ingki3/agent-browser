"""WS-56 R1 — 승인 대상 결속(B1) · 동시 승인 호출의 호출 단위 상태(B2). 실제 Chromium(headless) + 로컬 http.

B1: 사람이 승인을 보는 동안 페이지가 승인 대상(#pay)을 **같은 id·같은 이름·같은 css 의 새 노드**로 바꾸면,
    이름·role·css 일치만으로는 통과하지 않는다 — 승인 요청 시점에 잡아 둔 노드 정체(JS 핸들)와 실행 직전 노드가
    같아야 한다. elicitation(element_id·selector) · 승인 증표(element_id·selector) · 서버 재확인 뒤 디스패처 직전
    교체(디스패처 결속) 각각.
B2: 같은 서버에 동시 승인 호출 둘 — 먼저 끝난 호출이 진행 중인 다른 승인 호출의 치유 금지(heal_disabled)·승인
    근거를 풀거나 가져가지 않는다. 실제로 눌린 대상이 각자 승인한 그 버튼뿐이어야 한다.

로컬 http(127.0.0.1)·임시 상태 폴더만. 공개 사이트 없음.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import anyio
import pytest

from contracts import ActionType, ErrorCode
from interface import handoff
from interface.mcp_server import BrowserMCPServer, create_server, tool_name
from test_run_cli import requires_chromium
from ws29_helpers import srv_approve

CLICK = tool_name(ActionType.CLICK)

PAGE = """<!doctype html><meta charset=utf-8><title>가게</title>
<p id=out>대기</p>
<div id=box><button id=pay onclick="hit('pay')">결제하기</button></div>
<div id=box2><button id=pay2 onclick="hit('pay2')">주문 확정</button></div>
<script>
function hit(b){fetch('/clicked?b='+b);document.getElementById('out').textContent='눌림 '+b;}
// 같은 id·같은 이름·같은 자리(같은 css)의 새 노드로 바꾼다 — 이름·role·css 로는 구별되지 않는다.
function swapSame(){var o=document.getElementById('pay');var n=document.createElement('button');n.id='pay';
n.textContent='결제하기';n.onclick=function(){hit('new')};o.replaceWith(n);}
// 비슷한 이름의 다른 노드(id 없음) — 관찰한 css 가 사라져 치유 사다리가 후보를 찾는 모양.
function swapSimilar(){var o=document.getElementById('pay');var n=document.createElement('button');
n.textContent='결제 하기';n.onclick=function(){hit('similar')};o.replaceWith(n);}
</script>
"""


@pytest.fixture
def site():
    hits: List[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/clicked"):
                hits.append(self.path.split("b=", 1)[-1])
                body = "ok"
            else:
                body = PAGE
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))


def _eid(observation: Dict[str, Any], name: str) -> str:
    return next(e["element_id"] for e in observation["elements"] if e["name"] == name)


async def _settle(backend: Any, hits: List[str]) -> str:
    await backend._dispatcher.ctx.page.wait_for_timeout(300)
    return await backend._dispatcher.ctx.page.text_content("#out")


async def _elicit_run(base: str, hits: List[str], args_of: Callable[[Dict[str, Any]], Dict[str, Any]],
                      during: Optional[Callable[[Any], Any]] = None,
                      prepare: Optional[Callable[[Any], None]] = None) -> Dict[str, Any]:
    """elicitation 지원 클라이언트로 click 한 번. during = 사람이 버튼을 보는 동안 페이지에 일어나는 일."""
    from mcp import types as t
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    server, backend = create_server(recipes=False)
    out: Dict[str, Any] = {}

    async def elicit(ctx: Any, params: Any) -> Any:
        if during is not None:
            await during(backend)
        return t.ElicitResult(action="accept", content={})

    try:
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(ss[0], ss[1], server.create_initialization_options()))
                async with ClientSession(*cs, elicitation_callback=elicit) as session:
                    await session.initialize()
                    await session.call_tool(tool_name(ActionType.NAVIGATE), {"url": base + "/"})
                    obs = await backend.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
                    if prepare is not None:
                        prepare(backend)
                    res = await session.call_tool(CLICK, args_of(obs.data["observation"]))
                    out["env"] = json.loads(res.content[0].text)
                    out["text"] = await _settle(backend, hits)
                tg.cancel_scope.cancel()
    finally:
        await backend.close()
        backend.hub.close()
    return out


def _by_eid(observation: Dict[str, Any]) -> Dict[str, Any]:
    return {"element_id": _eid(observation, "결제하기"), "epoch": observation["snapshot_epoch"]}


def _by_selector(observation: Dict[str, Any]) -> Dict[str, Any]:
    return {"selector": "#pay"}


async def _swap_same(backend: Any) -> None:
    await backend._dispatcher.ctx.page.evaluate("swapSame()")


# ======================================================================= B1 elicitation


@requires_chromium
@pytest.mark.parametrize("args_of", [_by_eid, _by_selector], ids=["element_id", "selector"])
async def test_elicitation_same_name_id_css_new_node_is_toctou(site, args_of):
    """사람이 보는 동안 #pay 가 같은 id·이름·css 의 새 노드로 바뀌면 accept 해도 누르지 않는다."""
    base, hits = site
    out = await _elicit_run(base, hits, args_of, during=_swap_same)
    env = out["env"]
    assert env.get("error_code") == ErrorCode.TOCTOU_MISMATCH.value, env
    assert env["data"]["approval"]["via"] == "elicitation"
    assert env["data"]["approval"]["reason"] == "target_changed"
    assert out["text"] == "대기" and hits == []


@requires_chromium
@pytest.mark.parametrize("args_of", [_by_eid, _by_selector], ids=["element_id", "selector"])
async def test_elicitation_unchanged_node_still_clicks(site, args_of):
    """대조: 노드가 그대로면 accept 로 실제로 눌린다(결속이 정상 승인을 막지 않는다)."""
    base, hits = site
    out = await _elicit_run(base, hits, args_of)
    assert out["env"]["success"] is True, out["env"]
    assert out["env"]["data"]["approval"]["via"] == "elicitation"
    assert hits == ["pay"]


@requires_chromium
@pytest.mark.parametrize("args_of", [_by_eid, _by_selector], ids=["element_id", "selector"])
async def test_swap_after_server_recheck_is_caught_by_dispatcher(site, args_of):
    """서버 재확인을 통과한 **뒤** 디스패처가 누르기 직전에 바뀌어도(같은 id·이름·css) 새 노드를 누르지 않는다 —
    디스패처가 잡아 둔 그 노드에만 동작한다."""
    base, hits = site

    def prepare(backend: Any) -> None:
        real = backend._recheck_after_accept

        async def recheck_then_swap(*a: Any, **kw: Any) -> Any:
            r = await real(*a, **kw)
            await backend._dispatcher.ctx.page.evaluate("swapSame()")
            return r

        backend._recheck_after_accept = recheck_then_swap

    out = await _elicit_run(base, hits, args_of, prepare=prepare)
    env = out["env"]
    assert env["success"] is False, env
    assert env.get("error_code") == ErrorCode.TOCTOU_MISMATCH.value, env
    assert out["text"] == "대기" and hits == []


@requires_chromium
@pytest.mark.parametrize("args_of", [_by_eid, _by_selector], ids=["element_id", "selector"])
async def test_swap_between_dispatcher_check_and_click_never_clicks_new_node(site, args_of):
    """디스패처의 노드 확인을 지난 **뒤**, 실제 클릭 직전에 바뀌어도 새 노드는 눌리지 않는다 — 실행은 잡아 둔 그
    노드 핸들로만 한다(로케이터를 다시 풀지 않음). 떨어진 노드라 발송이 실패하고 아무것도 눌리지 않는다."""
    base, hits = site

    def prepare(backend: Any) -> None:
        disp = backend._dispatcher
        real = disp._send_element_event

        async def swap_then_send(*a: Any, **kw: Any) -> Any:
            await disp.ctx.page.evaluate("swapSame()")
            return await real(*a, **kw)

        disp._send_element_event = swap_then_send

    out = await _elicit_run(base, hits, args_of, prepare=prepare)
    assert out["env"]["success"] is False, out["env"]
    assert out["text"] == "대기" and hits == []


# ======================================================================= B1 승인 증표(census → 같은 규칙)


def _visible(s: BrowserMCPServer) -> None:
    """headless 로 돌리되 사람이 볼 창이 있다고 두고(증표 발급), 창 오버레이 문구를 가로챈다(사람의 눈)."""
    s._human_can_see = lambda: True
    seen: List[Any] = []
    real = s._set_banner

    async def spy(text):
        seen.append(text)
        return await real(text)

    s._set_banner = spy
    s.banners = seen


@requires_chromium
@pytest.mark.parametrize("args_of", [_by_eid, _by_selector], ids=["element_id", "selector"])
async def test_approval_token_same_name_id_css_new_node_is_toctou(site, args_of):
    """승인 증표 경로도: 발급 뒤 사람이 승인하는 사이 같은 id·이름·css 의 새 노드로 바뀌면 누르지 않는다."""
    base, hits = site
    async with BrowserMCPServer(handoff_root=Path(handoff.state_root())) as s:
        _visible(s)
        await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": base + "/"})
        obs = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        args = args_of(obs.data["observation"])
        r1 = await s.call_tool(CLICK, dict(args))
        aid = r1.data["approval"]["approval_id"]
        await srv_approve(s, aid)
        await s._page.evaluate("swapSame()")
        r2 = await s.call_tool(CLICK, dict(args, approval_id=aid))
        assert not r2.success and r2.error_code is ErrorCode.TOCTOU_MISMATCH, r2
        assert r2.data["approval"]["reason"] == "target_changed"
        await s._page.wait_for_timeout(300)
        assert await s._page.text_content("#out") == "대기" and hits == []


@requires_chromium
@pytest.mark.parametrize("args_of", [_by_eid, _by_selector], ids=["element_id", "selector"])
async def test_approval_token_unchanged_node_still_clicks(site, args_of):
    base, hits = site
    async with BrowserMCPServer(handoff_root=Path(handoff.state_root())) as s:
        _visible(s)
        await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": base + "/"})
        obs = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        args = args_of(obs.data["observation"])
        r1 = await s.call_tool(CLICK, dict(args))
        aid = r1.data["approval"]["approval_id"]
        await srv_approve(s, aid)
        r2 = await s.call_tool(CLICK, dict(args, approval_id=aid))
        assert r2.success, r2
        await s._page.wait_for_timeout(300)
        assert hits == ["pay"]


# ======================================================================= B2 동시 승인 호출


@requires_chromium
async def test_concurrent_accepts_keep_their_own_heal_ban_and_targets(site):
    """A(결제하기)·B(주문 확정) 동시 elicitation 승인. A 가 디스패처 안(치유 금지 켠 뒤)에서 멈춘 사이 B 가 끝난다.
    B 가 끝나며 A 의 치유 금지를 풀면 A 는 비슷한 이름 노드로 치유를 시도한다. 호출 단위면 A 는 치유 없이 거부.
    실제로 눌린 것은 B 의 버튼 하나뿐이어야 한다."""
    from mcp import types as t
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    base, hits = site
    server, backend = create_server(recipes=False)
    a_in_dispatch = asyncio.Event()
    b_done = asyncio.Event()
    out: Dict[str, Any] = {}

    async def elicit(ctx: Any, params: Any) -> Any:
        if "주문 확정" in params.message:
            await a_in_dispatch.wait()  # B 는 A 가 디스패처 안에 들어간 뒤에 승인
        return t.ElicitResult(action="accept", content={})

    try:
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(ss[0], ss[1], server.create_initialization_options()))
                async with ClientSession(*cs, elicitation_callback=elicit) as session:
                    await session.initialize()
                    await session.call_tool(tool_name(ActionType.NAVIGATE), {"url": base + "/"})
                    obs = (await backend.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})).data["observation"]
                    a_id, b_id = _eid(obs, "결제하기"), _eid(obs, "주문 확정")
                    disp = backend._dispatcher

                    real_recheck = backend._recheck_after_accept

                    async def recheck(action: Any, params: Dict[str, Any], components: Any) -> Any:
                        r = await real_recheck(action, params, components)
                        if params.get("element_id") == a_id:
                            # A: 서버 재확인 뒤 대상이 비슷한 이름의 다른 노드로 — 디스패처에서 stale.
                            await disp.ctx.page.evaluate("swapSimilar()")
                        return r

                    backend._recheck_after_accept = recheck
                    real_fresh = disp._fresh_or_healed_handle

                    async def fresh(action: Any, handle: Any, params: Dict[str, Any]) -> Any:
                        if params.get("element_id") == a_id:
                            out["a_heal_before"] = disp.heal_disabled
                            a_in_dispatch.set()
                            await b_done.wait()  # B 가 끝날 때까지 A 를 디스패처 안에 붙잡는다
                            out["a_heal_after_b"] = disp.heal_disabled
                        return await real_fresh(action, handle, params)

                    disp._fresh_or_healed_handle = fresh
                    ep = obs["snapshot_epoch"]

                    async def call_a() -> None:
                        res = await session.call_tool(CLICK, {"element_id": a_id, "epoch": ep})
                        out["a"] = json.loads(res.content[0].text)

                    async def call_b() -> None:
                        res = await session.call_tool(CLICK, {"element_id": b_id, "epoch": ep})
                        out["b"] = json.loads(res.content[0].text)
                        b_done.set()

                    with anyio.fail_after(60):
                        async with anyio.create_task_group() as calls:
                            calls.start_soon(call_a)
                            calls.start_soon(call_b)
                    out["text"] = await _settle(backend, hits)
                tg.cancel_scope.cancel()
    finally:
        await backend.close()
        backend.hub.close()

    assert out["a_heal_before"] is True and out["a_heal_after_b"] is True, out
    a, b = out["a"], out["b"]
    assert b["success"] is True and b["data"]["approval"]["via"] == "elicitation", b
    assert a["success"] is False and not a.get("healed"), a
    assert a["data"].get("heal_disabled") is True, a  # 치유 금지가 살아 있어 사다리를 돌지 않았다
    assert a["data"]["approval"]["via"] == "elicitation", a  # A 의 근거를 B 가 가져가지 않았다
    assert hits == ["pay2"], hits
    assert backend._dispatcher.heal_disabled is False  # 호출 밖에서는 꺼져 있다


# ======================================================================= fail-closed · 호출 단위 상태 (브라우저 없음)


@pytest.fixture
def fake_pair(tmp_path: Path, monkeypatch):
    from test_ws56_elicitation import _fake

    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))
    server, backend = create_server(recipes=False)
    _fake(backend)
    yield server, backend
    backend.hub.close()


async def test_unpinnable_target_is_not_elicited_and_not_run(fake_pair):
    """승인 요청 때 대상 노드를 잡지 못하면(결속 불가) 묻지도 실행하지도 않는다 — 기존 경로(증표 안내)."""
    from test_ws56_elicitation import PAY, _Asker, _sdk_call

    server, backend = fake_pair

    async def unresolved(params: Dict[str, Any]) -> Dict[str, Any]:
        return {"unresolved": "대상 노드 없음"}

    backend._dispatcher.pin_target = unresolved
    asker = _Asker("accept")
    env = (await _sdk_call(server, CLICK, dict(PAY), asker))["json"]
    assert env["success"] is False and asker.messages == []
    assert env["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value
    assert backend._dispatcher.calls == []


async def test_pinned_mismatch_after_accept_is_toctou(fake_pair):
    from test_ws56_elicitation import PAY, _Asker, _sdk_call

    server, backend = fake_pair

    async def other(pin: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"fresh": False, "detail": "승인한 노드가 아님"}

    backend._dispatcher.pinned_target_check = other
    env = (await _sdk_call(server, CLICK, dict(PAY), _Asker("accept")))["json"]
    assert env["error_code"] == ErrorCode.TOCTOU_MISMATCH.value, env
    assert env["data"]["approval"]["reason"] == "target_changed"
    assert backend._dispatcher.calls == []


async def test_token_without_pinned_node_is_refused(fake_pair):
    """증표 발급 때 노드를 잡지 못했으면 승인돼도 실행하지 않는다(장부에 없음 = 대상 바뀜)."""
    from ws29_helpers import srv_approve

    server, backend = fake_pair
    backend._approval_pins().clear()
    blocked = await backend.call_tool(CLICK, {"element_id": "@e1", "epoch": 0})
    aid = blocked.data["approval"]["approval_id"]
    assert aid in backend._approval_pins()
    backend._approval_pins().pop(aid)
    await srv_approve(backend, aid)
    r = await backend.call_tool(CLICK, {"element_id": "@e1", "epoch": 0, "approval_id": aid})
    assert not r.success and r.error_code is ErrorCode.TOCTOU_MISMATCH
    assert r.data["approval"]["reason"] == "target_changed"
    assert backend._dispatcher.calls == []


async def test_per_call_state_is_not_shared_between_concurrent_calls():
    """B2: 승인 증표 표지·게이트 근거·잡은 노드는 호출(태스크) 문맥 단위 — 다른 호출이 지우거나 가져가지 않는다."""
    srv = BrowserMCPServer()
    a_set, b_cleared = asyncio.Event(), asyncio.Event()
    seen: Dict[str, Any] = {}

    async def call_a() -> None:
        srv._used_approval, srv._pending_gate_basis, srv._approval_pin = "ap_a", {"name": "a"}, "pin-a"
        a_set.set()
        await b_cleared.wait()
        seen["a"] = (srv._used_approval, srv._pending_gate_basis, srv._approval_pin)

    async def call_b() -> None:
        await a_set.wait()
        srv._used_approval = srv._pending_gate_basis = srv._approval_pin = None  # call 입구의 초기화
        b_cleared.set()

    await asyncio.gather(call_a(), call_b())
    assert seen["a"] == ("ap_a", {"name": "a"}, "pin-a")
