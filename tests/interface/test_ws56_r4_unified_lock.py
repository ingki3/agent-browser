"""WS-56 R4 — WS-57 병합 뒤 실행 락 하나(작업 공간 직렬화 구간)로 통일. 실제 Chromium(headless) + 로컬 http 만.

WS-56 R2 B4·R3 B6 의 서버 단위 승인 실행 락(_approval_exec_lock·_EXEC_HELD)을 없애고 WS-57 R2 의 작업 공간 락
(_dispatch_serial)으로 합쳤다. 지키는 것:

U1 사람 응답 대기(elicitation)는 락 밖 — 기다리는 동안 같은 작업 공간의 다른 호출이 끝난다.
U2 accept 뒤 락을 다시 잡은 **뒤** 재확인(R3 B3) — 그사이 바뀌지 않았으면 실행, 바뀌었으면 R3 테스트가 막는다.
U3 다른 작업 공간은 동시 — 한 작업 공간이 실행 중(일반 액션 포함)이어도 다른 작업 공간의 승인 실행이 기다리지 않는다
   (서버 단위 락이 남아 있으면 RED).
U4 취소 안전 — 사람 대기 중 취소·다시 잡기를 기다리다 취소돼도 락이 남지 않고 남의 락을 풀지 않는다.
U5 공유 브라우저 입력 — 두 작업 공간의 동시 type_text·click(alert) 이 각자 자기 탭에 그대로 들어가고 효과(다이얼로그)가
   자기 호출에만 귀속된다(브라우저 전역 입력 락이 필요 없다는 근거: 입력은 CDP 대상 세션별, 디스패처·효과 기록은 작업
   공간별).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import anyio
import pytest

from interface import handoff
from interface.mcp_server import BrowserMCPServer, envelope_dict
from interface.server_elicit import CURRENT_ELICITOR
from test_run_cli import requires_chromium

pytestmark = requires_chromium

HTML = """<!doctype html><meta charset=utf-8><title>로컬 가게</title><p id=out>대기</p>
<button id=pay2 onclick="hit('second')">주문 확정</button>
<button id=normal onclick="hit('normal')">열기</button>
<button id=notice onclick="alert('NOTICE-'+location.hash.slice(1))">알림 보기</button>
<input id=memo aria-label="메모">
<script>window.clientHits=[];function hit(b){clientHits.push(b);fetch('/clicked?b='+b,{cache:'no-store'});
document.querySelector('#out').textContent=b;}</script>"""


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))


@pytest.fixture
def site():
    state: Dict[str, Any] = {"hits": [], "slow": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/clicked"):
                state["hits"].append(self.path)
                body = "<p>done</p>"
            elif self.path.startswith("/slow"):
                state["slow"].set()
                time.sleep(2.5)
                body = "<title>slow</title><p>slow</p>"
            else:
                body = HTML
            data = body.encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *a):  # noqa: A002,ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield state
    finally:
        srv.shutdown()


class Gate:
    """사람 응답을 테스트가 정하는 elicitor — ask 가 불리면 asked, answer 를 넣으면 그 응답."""

    def __init__(self, reply: str = "accept") -> None:
        self.reply = reply
        self.asked = asyncio.Event()
        self.go = asyncio.Event()
        self.messages: List[str] = []

    def supported(self) -> bool:
        return True

    async def ask(self, message: str, timeout_s: float) -> str:
        self.messages.append(message)
        self.asked.set()
        await self.go.wait()
        return self.reply


async def call(s: BrowserMCPServer, name: str, args: Optional[Dict[str, Any]] = None, ws: Optional[str] = None,
               elicitor: Any = None) -> Dict[str, Any]:
    args = dict(args or {})
    if ws is not None:
        args["workspace"] = ws
    tok = CURRENT_ELICITOR.set(elicitor) if elicitor is not None else None
    try:
        return envelope_dict(await s.call_tool("browser_" + name, args))
    finally:
        if tok is not None:
            CURRENT_ELICITOR.reset(tok)


async def _hits(s: BrowserMCPServer, ws: Optional[str] = None) -> list:
    page = s._workspace_runtime(ws).page if ws else s._page
    return await page.evaluate("window.clientHits")


async def _wait_waiters(lock: Any, task: "asyncio.Task") -> None:
    with anyio.fail_after(20):
        while not getattr(lock, "_waiters", None):
            assert not task.done(), task
            await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------------------------- U1


async def test_u1_human_wait_is_outside_the_workspace_lock(site):
    """버튼 승인을 기다리는 동안 같은 작업 공간의 일반 클릭이 끝나고, accept 뒤 승인 클릭이 실행된다(교착 없음)."""
    async with BrowserMCPServer(recipes=False) as s:
        assert (await call(s, "navigate", {"url": site["url"] + "/"}))["success"]
        gate = Gate()
        approved = asyncio.create_task(call(s, "click", {"selector": "#pay2"}, elicitor=gate))
        with anyio.fail_after(20):
            await gate.asked.wait()
        assert not s._dispatch_lock("default").locked(), "사람 대기 중에 작업 공간 락을 쥐고 있음"
        normal = await asyncio.wait_for(call(s, "click", {"selector": "#normal"}), 15)
        assert normal["success"], normal
        assert not approved.done()
        gate.go.set()
        r = await asyncio.wait_for(approved, 20)
        assert r["success"], r
        assert r["data"]["approval"]["via"] == "elicitation", r
        assert await _hits(s) == ["normal", "second"]


# ---------------------------------------------------------------------------------------------- U2


async def test_u2_accept_reacquires_lock_then_runs_when_unchanged(site):
    """대조(R3 B3 표본의 '안 바뀜'): accept 뒤 락을 기다렸다가 다시 잡고, 바뀐 것이 없으면 실행한다(과차단 아님)."""
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "navigate", {"url": site["url"] + "/"})
        gate = Gate()
        task = asyncio.create_task(call(s, "click", {"selector": "#pay2"}, elicitor=gate))
        lock = s._dispatch_lock("default")
        with anyio.fail_after(20):
            await gate.asked.wait()
            await lock.acquire()
        try:
            gate.go.set()
            await _wait_waiters(lock, task)
        finally:
            lock.release()
        r = await asyncio.wait_for(task, 20)
        assert r["success"] and r["data"]["approval"]["via"] == "elicitation", r
        assert await _hits(s) == ["second"]


async def test_u2_control_taken_during_human_wait_is_refused_after_reacquire(site):
    """사람 대기 중 사람이 조작권을 잡으면, 락을 다시 잡은 뒤 조작권 거부(누르지 않음)."""
    from interface.handoff import write_command

    async with BrowserMCPServer(recipes=False) as s:
        s._human_can_see = lambda: True
        s.open_handoff()
        await call(s, "navigate", {"url": site["url"] + "/"})
        gate = Gate()
        task = asyncio.create_task(call(s, "click", {"selector": "#pay2"}, elicitor=gate))
        with anyio.fail_after(20):
            await gate.asked.wait()
        write_command(s.hub.root, s.hub.server_id, "take")  # 사람이 조작권을 잡는다(버튼 승인 대기 중)
        gate.go.set()
        r = await asyncio.wait_for(task, 20)
        assert r["error_code"] == "E_HITL_UNATTENDED_BLOCKED" and r["data"]["control"]["holder"] != "agent", r
        assert await _hits(s) == []


# ---------------------------------------------------------------------------------------------- U3


async def test_u3_other_workspace_approval_runs_while_this_workspace_executes(site):
    """default 가 실행 중(느린 navigate — 일반 액션도 락 참여)이어도 watch 의 승인 클릭은 기다리지 않는다.

    서버 단위 승인 실행 락이 남아 있으면 watch 가 default 의 실행이 끝날 때까지 기다려 RED."""
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "navigate", {"url": site["url"] + "/"})
        await call(s, "navigate", {"url": site["url"] + "/"}, "watch")
        site["slow"].clear()
        slow = asyncio.create_task(call(s, "navigate", {"url": site["url"] + "/slow", "timeout_ms": 9000}))
        assert await asyncio.to_thread(site["slow"].wait, 10)
        gate = Gate()
        gate.go.set()
        t0 = time.monotonic()
        r = await asyncio.wait_for(call(s, "click", {"selector": "#pay2"}, "watch", elicitor=gate), 20)
        took = time.monotonic() - t0
        assert r["success"] and r["data"]["approval"]["via"] == "elicitation", r
        assert not slow.done() and took < 2.0, took
        assert (await slow)["success"]
        assert await _hits(s, "watch") == ["second"]


async def test_u3_same_workspace_waits_for_the_one_lock(site):
    """같은 작업 공간 일반 호출은 승인 실행(=같은 락) 중에 기다린다 — 락 하나가 승인·일반 모두를 줄 세운다."""
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "navigate", {"url": site["url"] + "/"})
        lock = s._dispatch_lock("default")
        await lock.acquire()
        try:
            gate = Gate()
            gate.go.set()
            approved = asyncio.create_task(call(s, "click", {"selector": "#pay2"}, elicitor=gate))
            normal = asyncio.create_task(call(s, "click", {"selector": "#normal"}))
            await asyncio.sleep(0.3)
            assert not approved.done() and not normal.done()
            assert not gate.asked.is_set(), "락 밖에서 게이트 판정을 시작함"
        finally:
            lock.release()
        ra, rn = await asyncio.wait_for(asyncio.gather(approved, normal), 30)
        assert ra["success"] and rn["success"], (ra, rn)
        assert sorted(await _hits(s)) == ["normal", "second"]


# ---------------------------------------------------------------------------------------------- U4


async def test_u4_cancel_during_human_wait_leaves_no_lock(site):
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "navigate", {"url": site["url"] + "/"})
        gate = Gate()
        task = asyncio.create_task(call(s, "click", {"selector": "#pay2"}, elicitor=gate))
        with anyio.fail_after(20):
            await gate.asked.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not s._dispatch_lock("default").locked()
        r = await asyncio.wait_for(call(s, "click", {"selector": "#normal"}), 15)
        assert r["success"], r
        assert await _hits(s) == ["normal"]


async def test_u4_cancel_while_reacquiring_does_not_release_others_lock(site):
    """accept 뒤 락을 다시 기다리다 취소돼도, 지금 락을 쥔 다른 호출의 락을 풀지 않는다."""
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "navigate", {"url": site["url"] + "/"})
        gate = Gate()
        task = asyncio.create_task(call(s, "click", {"selector": "#pay2"}, elicitor=gate))
        lock = s._dispatch_lock("default")
        with anyio.fail_after(20):
            await gate.asked.wait()
            await lock.acquire()
        try:
            gate.go.set()
            await _wait_waiters(lock, task)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert lock.locked(), "취소된 호출이 남(테스트)의 락을 풀었음"
        finally:
            lock.release()
        assert not lock.locked()
        r = await asyncio.wait_for(call(s, "click", {"selector": "#normal"}), 15)
        assert r["success"], r
        assert await _hits(s) == ["normal"]


# ---------------------------------------------------------------------------------------------- U5


async def _ids(s: BrowserMCPServer, ws: str) -> Dict[str, Any]:
    obs = await call(s, "observe_page", {}, ws)
    els = obs["data"]["observation"]["elements"]
    return {e["name"]: {"element_id": e["element_id"], "epoch": obs["snapshot_epoch"]} for e in els}


async def test_u5_two_workspaces_concurrent_input_lands_in_own_tab(site):
    """공유 브라우저에서 두 작업 공간의 동시 입력·클릭: 글자 유실·섞임 없음, alert 는 자기 호출에만."""
    async with BrowserMCPServer(recipes=False) as s:
        names = ("default", "watch")
        for ws in names:
            assert (await call(s, "navigate", {"url": site["url"] + "/#" + ws}, ws))["success"]
        texts = {ws: (ws.upper() + "-가나다-0123456789-") * 4 for ws in names}
        for _round in range(3):
            ids = {ws: await _ids(s, ws) for ws in names}

            async def work(ws: str) -> Dict[str, Any]:
                typed = await call(s, "type_text", dict(ids[ws]["메모"], text=texts[ws]), ws)
                clicked = await call(s, "click", ids[ws]["알림 보기"], ws)
                return {"typed": typed, "clicked": clicked}

            out = dict(zip(names, await asyncio.wait_for(asyncio.gather(*(work(ws) for ws in names)), 60)))
            for ws in names:
                page = s._workspace_runtime(ws).page
                assert out[ws]["typed"]["success"], out[ws]
                assert await page.evaluate("document.querySelector('#memo').value") == texts[ws]
                clicked = out[ws]["clicked"]
                assert clicked["success"], clicked
                msgs = [d["message"] for d in clicked["data"].get("dialogs", [])]
                assert msgs == [f"NOTICE-{ws}"], (ws, msgs)
                other = [n for n in names if n != ws][0]
                assert f"NOTICE-{other}" not in json.dumps(clicked, ensure_ascii=False)
