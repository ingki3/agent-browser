"""WS-57 R1 — 독립 검증 FAIL R1~R8 재현을 저장소 테스트로 고정.

R1 창 전환 보고가 다른 작업 공간 주소·탭 id 를 흘림 / R2 같은 작업 공간 동시 승인 호출이 heal 금지를 풂 /
R3 사람 조작권 반납이 다른 작업 공간 epoch 를 무효화하지 않음 / R4 남의 새 탭이 내 무효 클릭을 성공으로 /
R5 남의 탭이 살아 있으면 외부에서 닫힌 default 탭을 복구하지 못함 / R6 첫 동시 호출의 브라우저·프록시 중복 초기화 /
R7 붙은 쪽 요청 취소가 primary 의 프로필 잠금을 풂 / R8 붙기 통로의 숨은 64KiB 줄 제한.
로컬 http·임시 프로필·합성 값만.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MethodType
from typing import Any, Dict

import pytest

from browser.workspace import workspace_scope
from interface import attach
from interface.mcp_server import BrowserMCPServer, envelope_dict

from test_run_cli import requires_chromium
from ws29_helpers import hub_approve

PAGE = """<title>{name}</title>
<button id="noop">plain noop</button>
<button id="danger" onclick="this.dataset.done='yes'">삭제하기</button>
<p id="who">{name}</p>"""


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
            else:
                body = PAGE.format(name=self.path.strip("/") or "home").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: D401
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


@pytest.fixture
def short_root(monkeypatch):
    from browser.serve_profile import PROFILE_ROOT_ENV

    root = Path(tempfile.mkdtemp(prefix="ab57r"))
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(root))
    yield root
    shutil.rmtree(root, ignore_errors=True)


async def call(s: BrowserMCPServer, name: str, args: Dict[str, Any] = None, ws: str = None) -> Dict[str, Any]:
    args = dict(args or {})
    if ws is not None:
        args["workspace"] = ws
    if name.startswith("control") or name.startswith("approval"):
        return await s.call_server_tool("browser_" + name, args)
    return envelope_dict(await s.call_tool("browser_" + name, args))


async def _stop_hub_task(s: BrowserMCPServer) -> None:
    """자동 감시 태스크를 멈춘다 — 반납을 어느 작업 공간 문맥이 먼저 집는지 테스트가 정한다."""
    task, s._hub_task = s._hub_task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _take_release(s: BrowserMCPServer, ws: str, between=None) -> None:
    from interface.handoff import write_command

    with workspace_scope(ws):
        write_command(s.hub.root, s.hub.server_id, "take")
        await s._poll_handoff()
        if between is not None:
            await between()
        write_command(s.hub.root, s.hub.server_id, "release")
        await s._poll_handoff()


# ---------------------------------------------------------------------------------------------- R1


@requires_chromium
async def test_r1_window_report_shows_only_the_callers_tabs(site):
    """창 전환(재기동) 보고: 자기 작업 공간 탭만 + 다른 작업 공간은 개수만 — 두 작업 공간 모두 한 번씩 듣는다."""
    async with BrowserMCPServer(browser_mode="on-demand", recipes=False) as s:
        a = await call(s, "navigate", {"url": site.url + "/chat-private"})
        b = await call(s, "navigate", {"url": site.url + "/watch-private"}, "watch")
        assert a["success"] and b["success"]
        with workspace_scope("watch"):
            out = await s._switch_window(False, "recover", force=True)
        assert out["ok"], out
        ob = await call(s, "observe_page", {}, "watch")
        win = ob["data"]["window"]
        assert win["reopened"] is True
        assert [t["url"].rsplit("/", 1)[-1] for t in win["tabs"]] == ["watch-private"]
        assert win["other_workspace_tabs"] == 1
        assert "chat-private" not in json.dumps(ob, ensure_ascii=False)
        assert a["tab_id"] not in json.dumps(win)
        oa = await call(s, "observe_page", {})
        wa = oa["data"]["window"]
        assert wa["reopened"] is True  # 기본 작업 공간도 자기 전환 보고를 받는다
        assert [t["url"].rsplit("/", 1)[-1] for t in wa["tabs"]] == ["chat-private"]
        assert wa["other_workspace_tabs"] == 1
        assert "watch-private" not in json.dumps(oa, ensure_ascii=False)
        assert wa.get("snapshot_epoch", oa["snapshot_epoch"]) == oa["snapshot_epoch"]


@requires_chromium
async def test_r1_control_request_window_is_filtered(site, monkeypatch):
    async with BrowserMCPServer(browser_mode="on-demand", recipes=False) as s:
        await call(s, "navigate", {"url": site.url + "/chat-private"})
        b = await call(s, "navigate", {"url": site.url + "/watch-private"}, "watch")
        tabs = [{"tab_id": "tab-9", "was": "tab-1", "url": site.url + "/chat-private", "active": True,
                 "restored": True},
                {"tab_id": "tab-10", "was": b["tab_id"], "url": site.url + "/watch-private", "active": True,
                 "restored": True, "workspace": "watch"}]

        async def fake_open():
            return {"ok": True, "window": {"mode": "on-demand", "state": "headed", "reopened": True, "tabs": tabs}}

        monkeypatch.setattr(s, "_open_window_for_request", fake_open)
        r = await call(s, "control_request", {"reason": "확인"}, "watch")
        assert r["success"], r
        win = r["data"]["window"]
        assert [t["tab_id"] for t in win["tabs"]] == ["tab-10"] and win["other_workspace_tabs"] == 1
        assert "chat-private" not in json.dumps(r, ensure_ascii=False)


@requires_chromium
async def test_r1_tab_closed_notice_goes_to_the_owner_only(site):
    """사람이 닫은 탭 알림(tab_closed_by_human)은 그 탭 주인 작업 공간에만 — 남의 탭 id 를 알리지 않는다."""
    async with BrowserMCPServer(recipes=False) as s:
        s._human_can_see = lambda: True
        a = await call(s, "navigate", {"url": site.url + "/chat"})
        b = await call(s, "navigate", {"url": site.url + "/watch"}, "watch")
        await _stop_hub_task(s)

        async def close_watch_tab():
            with workspace_scope("watch"):
                await s._dispatcher.ctx.page.close()

        await _take_release(s, "default", close_watch_tab)
        st_chat = await call(s, "control_status", {})
        assert "tab_closed_by_human" not in st_chat["data"], st_chat
        st_watch = await call(s, "control_status", {}, "watch")
        notice = st_watch["data"]["tab_closed_by_human"]
        assert notice["closed_tab_id"] == b["tab_id"]
        assert a["tab_id"] not in json.dumps(notice)
        ob = await call(s, "observe_page", {}, "watch")
        assert ob["success"] and ob["tab_id"] == notice["active_tab_id"]
        oa = await call(s, "observe_page", {})
        assert oa["success"] and oa["tab_id"] == a["tab_id"]


# ---------------------------------------------------------------------------------------------- R2


@requires_chromium
async def test_r2_concurrent_approved_calls_keep_their_own_heal_guard(site):
    """같은 작업 공간의 두 승인 클릭이 겹쳐도, 먼저 끝난 쪽이 남은 쪽의 치유 금지를 풀지 않는다."""
    async with BrowserMCPServer(recipes=False) as s:
        s._human_can_see = lambda: True
        await s._page.set_content("""<title>local</title>
          <button id="a" onclick="this.dataset.done='yes'">결제하기</button>
          <button id="b" onclick="this.dataset.done='yes'">삭제하기</button>""")
        obs = await call(s, "observe_page", {})
        els = obs["data"]["observation"]["elements"]
        params = []
        for name in ("결제하기", "삭제하기"):
            eid = next(e["element_id"] for e in els if e["name"] == name)
            p = {"element_id": eid, "epoch": obs["snapshot_epoch"]}
            blocked = await call(s, "click", p)
            aid = blocked["data"]["approval"]["approval_id"]
            hub_approve(s.hub, aid)
            params.append(dict(p, approval_id=aid))
        original = s._pdf.dispatch
        a_enter, b_enter, a_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen: Dict[str, Any] = {}

        async def delayed(disp, action, p, *rest):
            if p["element_id"] == params[0]["element_id"]:
                a_enter.set()
                await b_enter.wait()
                seen["a"] = disp.heal_disabled
                return await original(disp, action, p, *rest)
            b_enter.set()
            await a_done.wait()
            seen["b"] = disp.heal_disabled
            # 승인 대상 재확인 뒤 원본을 같은 이름의 새 노드로 바꾼다 — 치유가 꺼져 있어야 누르지 않는다.
            await s._page.evaluate("""() => { const b=document.querySelector('#b'); b.remove();
               const x=document.createElement('button'); x.id='replacement'; x.textContent='삭제하기';
               x.onclick=()=>x.dataset.done='replacement-clicked'; document.body.appendChild(x); }""")
            return await original(disp, action, p, *rest)

        s._pdf.dispatch = delayed

        async def click(p):
            try:
                return await call(s, "click", p)
            finally:
                if p is params[0]:
                    a_done.set()

        ta = asyncio.create_task(click(params[0]))
        await a_enter.wait()
        tb = asyncio.create_task(click(params[1]))
        ra, rb = await asyncio.gather(ta, tb)
        assert seen == {"a": True, "b": True}, seen
        # A 는 승인한 그 요소를 눌렀다(data-* 변화는 사후조건 효과가 아니라 결과 봉투는 E_TIMEOUT 일 수 있다).
        assert ra["data"]["approval"]["status"] == "used", ra
        assert await s._page.locator("#a").get_attribute("data-done") == "yes"
        assert not rb["success"] and not rb.get("healed") and rb["data"].get("heal_disabled") is True, rb
        assert await s._page.locator("#replacement").get_attribute("data-done") is None
        # 호출 밖(다음 호출)은 치유가 다시 켜져 있다
        assert s._dispatcher.heal_disabled is False


async def test_r2_heal_guard_is_per_call_and_nests():
    from actions import ActionDispatcher, DispatchContext

    disp = ActionDispatcher(DispatchContext(page=None, engine=None))
    out: Dict[str, Any] = {}

    async def guarded(tag: str, hold: float) -> None:
        with disp.heal_disabled_scope():
            await asyncio.sleep(hold)
            out[tag] = disp.heal_disabled

    async def plain() -> None:
        await asyncio.sleep(0.01)
        out["plain"] = disp.heal_disabled

    await asyncio.gather(guarded("short", 0.005), guarded("long", 0.03), plain())
    assert out == {"short": True, "long": True, "plain": False}
    # 중첩(레시피 재생 안의 승인 호출): 안쪽이 끝나도 바깥 금지는 유지
    with disp.heal_disabled_scope():
        with disp.heal_disabled_scope():
            pass
        assert disp.heal_disabled is True
    assert disp.heal_disabled is False


# ---------------------------------------------------------------------------------------------- R3


@pytest.mark.parametrize("consumer", ["default", "watch"])
@requires_chromium
async def test_r3_release_invalidates_every_workspace(site, consumer):
    """반납을 어느 작업 공간 문맥이 먼저 집어도 모든 작업 공간의 epoch 가 오르고 반납 전 승인·요소 번호는 무효."""
    async with BrowserMCPServer(recipes=False) as s:
        s._human_can_see = lambda: True
        await call(s, "navigate", {"url": site.url + "/chat"})
        await call(s, "navigate", {"url": site.url + "/watch"}, "watch")
        ob = await call(s, "observe_page", {}, "watch")
        noop = next(e["element_id"] for e in ob["data"]["observation"]["elements"] if e["name"] == "plain noop")
        blocked = await call(s, "click", {"selector": "#danger"}, "watch")
        aid = blocked["data"]["approval"]["approval_id"]
        hub_approve(s.hub, aid)
        await _stop_hub_task(s)
        epochs = {}
        for ws in ("default", "watch"):
            with workspace_scope(ws):
                epochs[ws] = s._engine.epoch

        async def human_changes_watch_page():
            with workspace_scope("watch"):
                await s._page.locator("#danger").evaluate("(e) => e.dataset.changedByHuman = 'yes'")

        await _take_release(s, consumer, human_changes_watch_page)
        for ws in ("default", "watch"):
            with workspace_scope(ws):
                assert s._engine.epoch > epochs[ws], (ws, consumer)
        used = await call(s, "click", {"selector": "#danger", "approval_id": aid}, "watch")
        assert not used["success"], used
        with workspace_scope("watch"):
            assert await s._page.locator("#danger").get_attribute("data-done") is None
        stale = await call(s, "click", {"element_id": noop, "epoch": ob["snapshot_epoch"]}, "watch")
        assert not stale["success"], stale


# ---------------------------------------------------------------------------------------------- R4


@requires_chromium
async def test_r4_foreign_new_tab_is_not_my_click_effect(site):
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "navigate", {"url": site.url + "/chat"})
        await call(s, "navigate", {"url": site.url + "/watch"}, "watch")
        baseline = await call(s, "click", {"selector": "#noop"})
        assert not baseline["success"], baseline
        d = s._dispatcher
        original = d._execute_element_action

        async def interleave(self, action, handle, p):
            r = await call(s, "tab_control", {"command": "create", "url": site.url + "/watch-extra"}, "watch")
            assert r["success"], r
            return await original(action, handle, p)

        d._execute_element_action = MethodType(interleave, d)
        try:
            r = await call(s, "click", {"selector": "#noop"})
        finally:
            d._execute_element_action = original
        assert not r["success"], r
        assert not any(str(x).startswith("new_tab") for x in r.get("data", {}).get("signals", []) or []), r
        assert "new_tab" not in json.dumps(r)


# ---------------------------------------------------------------------------------------------- R5


@pytest.mark.parametrize("mode", ["on-demand", "headless"])
@requires_chromium
async def test_r5_default_tab_closed_externally_is_recovered(site, mode):
    async with BrowserMCPServer(browser_mode=mode, recipes=False) as s:
        await call(s, "navigate", {"url": site.url + "/chat"})
        b = await call(s, "navigate", {"url": site.url + "/watch"}, "watch")
        await s._dispatcher.ctx.page.close()  # 사람·사이트가 닫음(tab_control 경로 아님)
        r = await call(s, "navigate", {"url": site.url + "/chat-again"})
        assert r["success"], r
        assert r["current_url"].endswith("/chat-again")
        ob = await call(s, "observe_page", {}, "watch")
        assert ob["success"] and ob["tab_id"] == b["tab_id"] and ob["current_url"].endswith("/watch")


# ---------------------------------------------------------------------------------------------- R6


@requires_chromium
async def test_r6_first_concurrent_calls_start_one_browser_and_close_its_proxy(short_root, monkeypatch):
    from browser import BrowserCore
    from security.egress_runtime import EgressRuntime

    cores, runtimes = [], []
    oldc, oldr = BrowserCore.start, EgressRuntime.start

    async def trackc(self):
        cores.append(self)
        return await oldc(self)

    async def trackr(self):
        runtimes.append(self)
        return await oldr(self)

    monkeypatch.setattr(BrowserCore, "start", trackc)
    monkeypatch.setattr(EgressRuntime, "start", trackr)
    s = BrowserMCPServer(browser_mode="on-demand", profile="lazy", recipes=False)
    s.open_handoff()
    s.acquire_profile()  # run_stdio 와 같은 수명: 잠금 먼저, 브라우저는 첫 호출 때
    try:
        ra, rb = await asyncio.gather(call(s, "observe_page", {}), call(s, "observe_page", {}, "watch"))
        assert ra["success"] and rb["success"], (ra, rb)
        assert len(cores) == 1 and len(runtimes) == 1
        assert ra["tab_id"] != rb["tab_id"]
    finally:
        await s.close()
        for c in cores:
            if c is not s._core:
                try:
                    await c.close()
                except Exception:  # noqa: BLE001
                    pass
    assert all(not (r.proxy and r.proxy.running) for r in runtimes)


@requires_chromium
async def test_r6_new_workspace_first_calls_make_one_tab(site):
    async with BrowserMCPServer(recipes=False) as s:
        await call(s, "navigate", {"url": site.url + "/chat"})
        r1, r2 = await asyncio.gather(call(s, "observe_page", {}, "w2"), call(s, "observe_page", {}, "w2"))
        assert r1["success"] and r2["success"] and r1["tab_id"] == r2["tab_id"]
        with workspace_scope("w2"):
            assert len(s._core.tabs()) == 1


# ---------------------------------------------------------------------------------------------- R7


@requires_chromium
async def test_r7_cancelled_attached_start_keeps_the_primary_lock(short_root, site, monkeypatch):
    from browser import BrowserCore, serve_profile

    gate, entered = asyncio.Event(), asyncio.Event()
    oldc = BrowserCore.start

    async def slow(self):
        entered.set()
        await gate.wait()
        return await oldc(self)

    monkeypatch.setattr(BrowserCore, "start", slow)
    s = BrowserMCPServer(profile="p", recipes=False)
    s.open_handoff()
    s.acquire_profile()
    try:
        with workspace_scope("attach-1"):  # 붙은 연결의 첫 호출
            task = asyncio.create_task(call(s, "observe_page", {}))
        await asyncio.wait_for(entered.wait(), 30)
        primary = asyncio.create_task(call(s, "navigate", {"url": site.url + "/primary"}))  # 같은 시작을 기다림
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not primary.done()
        with pytest.raises(serve_profile.ProfileInUseError):
            serve_profile.acquire("p", server_id="competitor")  # 경쟁 non-attach serve 는 여전히 거부(exit 2)
        gate.set()
        rp = await asyncio.wait_for(primary, 60)  # 붙은 쪽 취소가 primary 가 기다리던 시작을 멈추지 않았다
        assert rp["success"], rp
        r = await call(s, "navigate", {"url": site.url + "/chat"})
        assert r["success"], r
        with pytest.raises(serve_profile.ProfileInUseError):
            serve_profile.acquire("p", server_id="competitor")
    finally:
        gate.set()
        await s.close()


async def test_r7_failed_start_does_not_release_a_lock_it_did_not_take(short_root, monkeypatch):
    from browser import serve_profile
    from security.egress_runtime import EgressRuntime

    async def boom(self):
        raise RuntimeError("synthetic proxy failure")

    monkeypatch.setattr(EgressRuntime, "start", boom)
    s = BrowserMCPServer(profile="p", recipes=False)
    s.open_handoff()
    s.acquire_profile()
    try:
        with pytest.raises(RuntimeError):
            await s.start()
        with pytest.raises(serve_profile.ProfileInUseError):
            serve_profile.acquire("p", server_id="competitor")
    finally:
        await s.close()
    lock = serve_profile.acquire("p", server_id="after")  # 서버가 끝나면 풀린다
    lock.release()


async def test_r7_start_without_prior_lock_still_releases_on_failure(short_root, monkeypatch):
    from browser import serve_profile
    from security.egress_runtime import EgressRuntime

    async def boom(self):
        raise RuntimeError("synthetic proxy failure")

    monkeypatch.setattr(EgressRuntime, "start", boom)
    s = BrowserMCPServer(profile="p", recipes=False)
    with pytest.raises(RuntimeError):
        await s.start()
    lock = serve_profile.acquire("p", server_id="after")  # 시작이 잡은 잠금은 시작 실패 때 푼다(기존 동작)
    lock.release()
    await s.close()


# ---------------------------------------------------------------------------------------------- R8


def _profile_dir(root: Path, name: str = "p") -> Path:
    d = root / f"serve-{name}"
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _in_thread(fn, *args):
    out: Dict[str, Any] = {}

    def run():
        try:
            out["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc
    t = threading.Thread(target=run)
    t.start()
    return t, out


async def _join(t, timeout=20.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while t.is_alive() and loop.time() < deadline:
        await asyncio.sleep(0.02)
    assert not t.is_alive()


async def test_r8_attach_stream_has_no_hidden_line_limit(short_root):
    """붙기 통로(StreamLines/StreamText + relay)로 64KiB 를 크게 넘는 줄이 양방향으로 그대로 오간다."""
    prof = _profile_dir(short_root)

    async def serve(reader, writer, workspace):
        out = attach.StreamText(writer)
        async for line in attach.StreamLines(reader):
            await out.write(line)  # 그대로 되돌림

    listener = attach.AttachListener(prof, serve, server_id="t-1")
    await listener.start()
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    try:
        t, out = _in_thread(attach.connect, prof, "watch")
        await _join(t)
        sock = out["value"][0]
        import io

        err = io.StringIO()
        rt, res = _in_thread(lambda: attach.relay(sock, stdin_fd=r_in, stdout_fd=w_out, stderr=err))
        big = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "x", "params": {"_meta": {"pad": "가" * 100_000}}},
                         ensure_ascii=False).encode("utf-8") + b"\n"
        small = b'{"id":3}\n'
        wt, _ = _in_thread(lambda: (os.write(w_in, big), os.write(w_in, small)))
        got = bytearray()

        def read_all():
            while len(got) < len(big) + len(small):
                chunk = os.read(r_out, 1 << 16)
                if not chunk:
                    break
                got.extend(chunk)
        reader_t, _ = _in_thread(read_all)
        await _join(reader_t)
        await _join(wt)
        assert bytes(got) == big + small
        os.close(w_in)
        await _join(rt)
        assert res["value"] == 0 and err.getvalue() == ""
    finally:
        await listener.close()
        for fd in (r_in, r_out, w_out):
            try:
                os.close(fd)
            except OSError:
                pass


@requires_chromium
async def test_r8_e2e_large_request_through_attached_serve(short_root):
    """실제 CLI primary + 붙은 serve: 70KB 넘는 정상 tools/list 요청에 붙은 쪽도 답한다(세션 종료 없음)."""
    import subprocess as sp
    from contextlib import AsyncExitStack

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "interface.cli", "serve", "--profile", "p", "--attach"],
                                   env=dict(os.environ))
    relay = None
    loop = asyncio.get_running_loop()
    try:
        async with AsyncExitStack() as stack:
            err1 = stack.enter_context(tempfile.TemporaryFile(mode="w+"))
            r1, w1 = await stack.enter_async_context(stdio_client(params, errlog=err1))
            chat_s = await stack.enter_async_context(ClientSession(r1, w1))
            await chat_s.initialize()
            path = attach.socket_path(short_root / "serve-p")
            for _ in range(600):
                if path.exists():
                    break
                await asyncio.sleep(0.05)
            relay = sp.Popen([sys.executable, "-m", "interface.cli", "serve", "--profile", "p", "--attach",
                              "--workspace", "watch"], stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE,
                             env=dict(os.environ))
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
            big = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {"pad": "x" * 70_000}}}
            for msg in (init, {"jsonrpc": "2.0", "method": "notifications/initialized"}, big):
                relay.stdin.write((json.dumps(msg) + "\n").encode())
            relay.stdin.flush()
            replies = {}
            while 2 not in replies:
                line = await loop.run_in_executor(None, relay.stdout.readline)
                assert line, relay.stderr.read().decode("utf-8", "replace") if relay.poll() is not None else "EOF"
                msg = json.loads(line)
                replies[msg.get("id")] = msg
            assert replies[2]["result"]["tools"], replies[2]
            assert relay.poll() is None  # 붙은 serve 는 살아 있다
            tools = await chat_s.list_tools()
            assert tools.tools  # primary 도 정상
    finally:
        if relay is not None and relay.poll() is None:
            relay.kill()
            relay.wait(timeout=10)
