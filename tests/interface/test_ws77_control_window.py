"""WS-77 실 서버 경로 — 창 안 [다 했어요] 제어 창(#210, 리뷰 설계 (b)).

서버는 ``browser_mode="human"`` 으로 띄우되 Chromium 은 headless(창 없는 CI 에서도 같은 CDP 경로 — probe2 headless
실측과 같음). '사람' 은 제어 창 대상에 직접 보낸 CDP Input(시험 전용 경로 — 에이전트 도구가 아니다)으로 흉내 낸다.
창이 실제로 뜨는 on-demand 경로는 test_ws77_on_demand_e2e.py.

사보타주(보고서 기록): ``BrowserCore.exclude_page`` 를 무력화하면 위조 매트릭스의 에이전트 경로(tab_control switch →
click)가 반납을 일으켜 ``test_forgery_matrix_releases_nothing`` 이 실패하고, isTrusted 검사를 끄면 같은 시험의 main world
click() 이 반납을 일으켜 실패한다.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode
from interface import handoff
from interface.mcp_server import BrowserMCPServer, envelope_dict, tool_name
from interface.server_control import NEXT_OBSERVE, NEXT_WAIT, TELL_CLI, TELL_HEADLESS, TELL_WINDOW

from test_run_cli import requires_chromium

LOGIN = """<!doctype html><meta charset=utf-8><title>로그인</title>
<form method=post action=/login><input id=user aria-label='아이디' name=u>
<input id=pw type=password aria-label='비밀번호' name=p><button id=go type=submit>로그인</button></form>"""
HOME = "<!doctype html><meta charset=utf-8><title>홈</title><p id=who>{who}</p>"
#: 에이전트 탭의 적대 페이지 — 제어 창·바인딩을 찾아 누르려 한다(다른 탭·iframe 포함).
FORGE_JS = r"""
window.forge = [];
const tryAll = () => {
  for (const k of Object.getOwnPropertyNames(window)) {
    if (/^ab|done|control/i.test(k) && typeof window[k] === 'function') { try { window[k](JSON.stringify({nonce: 'x', trusted: true})); forge.push('called:' + k); } catch (e) {} }
  }
  try { const w = window.open('', 'agent-browser'); forge.push('named:' + (w ? w.location.href : null)); if (w) w.close(); } catch (e) { forge.push('named-err'); }
  try { new BroadcastChannel('agent-browser').postMessage('done'); } catch (e) {}
  for (const b of document.querySelectorAll('button')) { b.click(); b.dispatchEvent(new MouseEvent('click', {bubbles: true})); }
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
};
tryAll(); setTimeout(tryAll, 200);
"""
HOSTILE = ("<!doctype html><meta charset=utf-8><title>적대</title><button id=fake>다 했어요</button>"
           "<iframe src='/frame'></iframe><script>" + FORGE_JS + "</script>")
FRAME = "<!doctype html><meta charset=utf-8><button>다 했어요</button><script>" + FORGE_JS + "</script>"
IN_CONTROL = ("<!doctype html><meta charset=utf-8><title>옮겨진 창</title><button id=b>다 했어요</button><script>"
              + FORGE_JS + "</script>")


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str, headers: Dict[str, Any] = None) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._send(303, "", {"Location": "/home", "Set-Cookie": "sid=synthetic; Path=/; HttpOnly; Max-Age=600"})

        def do_GET(self):  # noqa: N802
            who = "로그인됨" if "sid=" in (self.headers.get("Cookie") or "") else "비로그인"
            body = {"/login": LOGIN, "/hostile": HOSTILE, "/frame": FRAME, "/in_control": IN_CONTROL}.get(
                self.path, HOME.format(who=who))
            self._send(200, body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _server(tmp_path: Path, **kw: Any) -> BrowserMCPServer:
    kw.setdefault("browser_mode", "human")
    return BrowserMCPServer(handoff_root=tmp_path / "servers", nav_settle=False, **kw)


async def _tool(s: BrowserMCPServer, action: ActionType, **args: Any) -> Dict[str, Any]:
    return envelope_dict(await s.call_tool(tool_name(action), args))


async def human_click(cw: Any) -> None:
    """사람 흉내: 제어 창 대상에 직접 CDP 마우스 입력(시험 전용 — 에이전트 도구 경로가 아니다)."""
    page = cw.page
    session = await page.context.new_cdp_session(page)
    x, y = cw.button_center
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        await session.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "clickCount": 1,
                                                        "button": "none" if kind == "mouseMoved" else "left"})
    await session.detach()


async def _settle(s: BrowserMCPServer, rounds: int = 6) -> None:
    for _ in range(rounds):
        await s._poll_handoff()
        await asyncio.sleep(0.05)


async def _until(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "시간 초과"
        await asyncio.sleep(0.05)


def _ledger(s: BrowserMCPServer) -> Dict[str, str]:
    return dict(s._headless_retry.logins.memory)


# ------------------------------------------------------------------ 진짜 1회


@requires_chromium
async def test_window_done_releases_and_agent_observes_immediately(tmp_path, site):
    async with _server(tmp_path, recipes=False) as s:
        assert (await _tool(s, ActionType.NAVIGATE, url=site + "/login"))["success"]
        epoch0 = s._engine.epoch
        r = await s.call_server_tool("browser_control_request", {"reason": "로그인해 주세요", "secret_wanted": True})
        assert r["success"], r
        hh = r["data"]["human_handoff"]
        assert hh["control_window"] == "open" and hh["tell_user"] == TELL_WINDOW and hh["next_step"] == NEXT_WAIT
        assert hh["expires_at"] is None  # human 방식은 요청이 만료되지 않는다(on-demand 만)
        assert "[다 했어요]" in r["data"]["how_to_respond"]
        cw = s._control_window
        assert cw is not None and cw.is_open
        # 제어 창 내용: 고정 문구 + display_safe 사유, 코드·페이지 글자 없음(NB3·NB5).
        text = await cw.page.evaluate("document.body.innerText")
        assert "다 했어요" in text and "로그인해 주세요" in text and "에이전트가 사람의 조작을" in text
        # 잠금 중 관찰 거부 응답에도 안내가 실린다.
        refused = await _tool(s, ActionType.OBSERVE_PAGE)
        assert refused["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value
        assert refused["data"]["human_handoff"]["tell_user"] == TELL_WINDOW
        st = await s.call_server_tool("browser_control_status", {})
        assert st["data"]["human_handoff"]["human_activity_seen"] == "not_seen"
        # 사람: take 없이 창에서 바로 로그인(이번 사고의 흐름).
        page = s._page
        await page.fill("#user", "someone")
        await page.fill("#pw", "synthetic-human-pass")
        async with page.expect_navigation():
            await page.click("#go")
        await human_click(cw)
        got = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        assert got["data"]["changed"] == "released", got
        hh = got["data"]["human_handoff"]
        assert hh["state"] == "released" and hh["release_source"] == "window_done"
        assert hh["human_activity_seen"] == "seen" and hh["next_step"] == NEXT_OBSERVE
        assert cw.accepted == 1 and not cw.is_open and s._control_window is None
        assert s.hub.holder == "agent" and not s.hub.requested and not s.hub.secret_wanted
        assert s._engine.epoch > epoch0
        obs = await _tool(s, ActionType.OBSERVE_PAGE)  # 즉시 관찰 가능
        assert obs["success"], obs
        assert _ledger(s).get(site) == "human_control"  # B5: take 없이도 로그인 기록(본문 요청 origin)
        # 한 번만 알린다.
        again = await s.call_server_tool("browser_control_wait", {"timeout_s": 1})
        assert again["data"]["changed"] == "none_pending"


@requires_chromium
async def test_take_then_window_done(tmp_path, site):
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        r = await s.call_server_tool("browser_control_request", {"reason": "캡차"})
        cw = s._control_window
        handoff.write_command(s.hub.root, s.hub.server_id, "take")
        await _until(lambda: s.hub.holder == "human")
        await _settle(s)
        assert s._control_window is cw and cw.is_open  # 사람이 쥔 동안에도 창은 남는다
        st = await s.call_server_tool("browser_control_status", {})
        assert st["data"]["human_handoff"]["state"] == "human_holding"
        await human_click(cw)
        got = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        assert got["data"]["changed"] == "released"
        assert got["data"]["human_handoff"]["release_source"] == "window_done"
        assert r["data"]["human_handoff"]["human_activity_seen"] in ("unknown", "not_seen")


# ------------------------------------------------------------------ 위조 0


async def _forge_main_world(cw: Any) -> None:
    await cw.page.evaluate("""() => { const b = document.querySelector('button');
        b.click(); b.dispatchEvent(new MouseEvent('click', {bubbles: true}));
        b.focus(); b.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
        for (const k of Object.getOwnPropertyNames(window)) if (/^ab/.test(k)) { try { window[k]('{}'); } catch (e) {} }
    }""")


@requires_chromium
async def test_forgery_matrix_releases_nothing(tmp_path, site):
    async with _server(tmp_path) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        r = await s.call_server_tool("browser_control_request", {"reason": "봐 주세요"})  # 비밀 보호 없음: 에이전트 도구가 돈다
        rid = r["data"]["request_id"]
        cw = s._control_window
        assert cw is not None and cw.is_open
        v0 = s.hub.version
        # (1) 다른 탭·iframe 의 적대 페이지(같은 탭 JS click/dispatch/키 합성·named open·BroadcastChannel).
        assert (await _tool(s, ActionType.NAVIGATE, url=site + "/hostile"))["success"]
        hostile_tab = await _tool(s, ActionType.TAB_CONTROL, command="create", url=site + "/hostile")
        assert hostile_tab["success"], hostile_tab
        await asyncio.sleep(0.6)
        forge = await s._page.evaluate("window.forge")
        assert not any(str(f).startswith("called:") for f in forge), forge  # main world 에 바인딩 없음
        # (2) 제어 창 main world 의 untrusted click·dispatchEvent·키 합성(바인딩은 불리지만 trusted=false 로 버림).
        await _forge_main_world(cw)
        await _settle(s)
        assert cw.accepted == 0
        assert cw.rejected >= 2  # 조건 성립: main world click 이 바인딩까지 갔고(trusted=false) 버려졌다
        # (3) 에이전트 도구: tab_control list 에 제어 창 없음, 어떤 tab id 로도 그 창에 갈 수 없음.
        listed = await _tool(s, ActionType.TAB_CONTROL, command="list")
        assert listed["success"]
        ids = [t["tab_id"] for t in listed["data"]["tabs"]]
        reached = []
        for n in range(1, 12):
            out = await _tool(s, ActionType.TAB_CONTROL, command="switch", tab_id=f"tab-{n}")
            if out["success"]:
                reached.append(s._core.get_tab(f"tab-{n}").page)
                # 갈 수 있는 탭마다 누르기 시도(selector·좌표·키) — 제어 창이 아니므로 반납이 일어나지 않는다.
                # (사보타주: 코어 제외를 끄면 여기서 제어 창 탭에 닿아 selector 클릭이 반납을 일으킨다.)
                await _tool(s, ActionType.CLICK, selector="button")
                obs = await _tool(s, ActionType.OBSERVE_PAGE)
                ep = obs.get("snapshot_epoch", 0)
                x, y = (int(c) for c in cw.button_center)
                await _tool(s, ActionType.CLICK, x=x, y=y, epoch=ep)
                await _tool(s, ActionType.PRESS_KEY, key="Enter")
        await _tool(s, ActionType.TAB_CONTROL, command="switch", tab_id=ids[0])
        obs = await _tool(s, ActionType.OBSERVE_PAGE)
        by = {e["name"]: e["element_id"] for e in obs["data"]["observation"]["elements"]}
        if "다 했어요" in by:
            await _tool(s, ActionType.CLICK, element_id=by["다 했어요"], epoch=obs["snapshot_epoch"])
        batch = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "click", "target": {"role": "button", "name": "다 했어요"}},
            {"action": "press_key", "args": {"key": "Enter"}, "target": {"role": "button", "name": "다 했어요"}}]})
        assert isinstance(batch, dict)
        # (4) 레시피 재생: 방금 통과한 클릭을 저장해 다시 실행.
        saved = await s.call_server_tool("browser_recipe", {"op": "save", "name": "누르기", "last_n": 1})
        if saved.get("success"):
            await s.call_server_tool("browser_recipe", {"op": "run", "id": saved["data"]["recipe"]["id"]})
        await _settle(s, 10)
        assert cw.accepted == 0, "위조가 [다 했어요] 로 받아들여졌다"
        assert s.hub.requested and s.hub.request_id == rid and s.hub.release_source is None
        assert s.hub.last_event == "requested" and s.hub.version == v0
        assert len(reached) >= 2 and all(p is not cw.page for p in reached)  # 탭 전환은 실제로 돌았다
        assert all(s._core.get_tab(i).page is not cw.page for i in ids)  # 목록에 제어 창 없음
        # 진짜 사람 클릭은 그대로 통한다(위 시험이 살아 있는 창에서 돌았다는 증거).
        await human_click(s._control_window)
        got = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        assert got["data"]["changed"] == "released"
        assert got["data"]["human_handoff"]["release_source"] == "window_done"


@requires_chromium
async def test_stale_nonce_late_click_and_other_world_rejected(tmp_path, site):
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        first = (await s.call_server_tool("browser_control_request", {"reason": "하나"}))["data"]["request_id"]
        old = s._control_window
        second = (await s.call_server_tool("browser_control_request", {"reason": "둘"}))["data"]["request_id"]
        assert first != second and not old.is_open  # 새 요청이면 옛 창은 닫힌다
        cw = s._control_window

        def event(nonce: Any, trusted: Any, context_id: Any = None, name: Any = None) -> Dict[str, Any]:
            # 서버가 받는 Runtime.bindingCalled 모양 그대로(①~③ 검사 대상).
            return {"name": name or cw.binding, "executionContextId": cw.context_id if context_id is None else context_id,
                    "payload": json.dumps({"nonce": nonce, "trusted": trusted})}

        for bad in (event(first, True), event(second, False), event(second, "true"), event(None, True),
                    event(second, True, context_id=cw.context_id + 999), event(second, True, name="other")):
            cw._on_binding(bad)
        cw._on_binding({"name": cw.binding, "executionContextId": cw.context_id, "payload": "x" * 600})
        assert cw.accepted == 0 and cw.rejected == 6  # 다른 바인딩 이름은 세지 않는다
        # 우리 world 안에서도 바인딩은 그리기 직후 지워져 있다(창 스크립트가 다시 부를 수 없음) — 다른 CDP 세션은
        # 그 world 에 바인딩 자체를 받지 못한다.
        session = await cw.page.context.new_cdp_session(cw.page)
        expr = f"typeof globalThis[{json.dumps(cw.binding)}]"
        mine = await session.send("Runtime.evaluate", {"expression": expr, "contextId": cw.context_id,
                                                         "returnByValue": True})
        assert mine["result"]["value"] == "undefined"
        await session.detach()
        await _settle(s)
        assert cw.accepted == 0 and s.hub.requested and s.hub.request_id == second
        await human_click(cw)
        got = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        assert got["data"]["changed"] == "released"
        # 반납 뒤 같은 창 객체로 들어온 늦은 호출은 버린다(창은 닫혔다).
        cw._on_binding({"name": cw.binding, "executionContextId": cw.context_id,
                        "payload": json.dumps({"nonce": second, "trusted": True})})
        assert cw.accepted == 1
        s.hub.queue_window_done(second)
        assert await s._poll_handoff() == []  # 요청이 끝났다


# ------------------------------------------------------------------ 수명


@requires_chromium
async def test_moving_control_window_elsewhere_recreates_once_then_cli(tmp_path, site):
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        await s.call_server_tool("browser_control_request", {"reason": "확인", "secret_wanted": True})
        cw1 = s._control_window
        await cw1.page.goto(site + "/in_control")  # 사람이 창 주소를 적대 페이지로 옮김
        await asyncio.sleep(0.5)
        assert cw1.navigated and cw1.accepted == 0
        await _settle(s)
        await _until(lambda: s._control_window is not None and s._control_window is not cw1)
        cw2 = s._control_window
        assert cw2.is_open and s.hub.requested
        assert all(t.page is not cw2.page for t in s._core.all_tabs())  # 다시 띄운 창도 관리 탭 밖
        await cw2.page.goto(site + "/in_control")
        await asyncio.sleep(0.3)
        await _settle(s)
        await _until(lambda: not s._control_spawning)
        assert s._control_window is None and s.hub.requested and s.hub.secret_wanted
        st = await s.call_server_tool("browser_control_status", {})
        hh = st["data"]["human_handoff"]
        assert hh["control_window"] == "unavailable" and hh["tell_user"] == TELL_CLI
        handoff.write_command(s.hub.root, s.hub.server_id, "release")
        await _until(lambda: not s.hub.requested)
        assert s.hub.release_source == "cli"


@requires_chromium
async def test_human_closing_control_window_is_not_done(tmp_path, site):
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        await s.call_server_tool("browser_control_request", {"reason": "확인", "secret_wanted": True})
        cw1 = s._control_window
        n_tabs = s._core.tab_count
        await cw1.page.close()  # 정리하려고 먼저 닫음(NB2) — 완료가 아니다
        await _settle(s)
        await _until(lambda: s._control_window is not None and s._control_window is not cw1)
        assert s.hub.requested and s.hub.secret_wanted and s.hub.release_source is None
        assert s._core.tab_count == n_tabs  # 제어 창 닫힘은 탭·창 감시('탭 0개=창 닫힘')와 무관
        await s._control_window.page.close()
        await _settle(s)
        await _until(lambda: not s._control_spawning)
        assert s._control_window is None and s.hub.requested
        refused = await _tool(s, ActionType.OBSERVE_PAGE)
        assert refused["data"]["human_handoff"]["tell_user"] == TELL_CLI


@requires_chromium
async def test_cli_release_without_take_records_login_and_closes_window(tmp_path, site):
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/login")
        await s.call_server_tool("browser_control_request", {"reason": "로그인", "secret_wanted": True})
        cw = s._control_window
        page = s._page
        await page.fill("#user", "someone")
        await page.fill("#pw", "synthetic-human-pass")
        async with page.expect_navigation():
            await page.click("#go")
        handoff.write_command(s.hub.root, s.hub.server_id, "release")
        await _until(lambda: not s.hub.requested)
        await _settle(s)
        assert not cw.is_open and s._control_window is None
        assert _ledger(s).get(site) == "human_control"


@requires_chromium
async def test_server_close_closes_control_window(tmp_path, site):
    s = _server(tmp_path, recipes=False)
    await s.start()
    try:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        await s.call_server_tool("browser_control_request", {"reason": "확인"})
        cw = s._control_window
        assert cw.is_open
    finally:
        await s.close()
    assert s._control_window is None and not cw.is_open


@requires_chromium
async def test_headless_server_unchanged_with_fixed_notice(tmp_path):
    async with BrowserMCPServer(handoff_root=tmp_path / "servers", nav_settle=False, recipes=False) as s:
        r = await s.call_server_tool("browser_control_request", {"reason": "로그인"})
        assert not r["success"] and r["error_code"] == ErrorCode.FEATURE_NOT_IMPLEMENTED.value
        assert r["data"]["human_handoff"] == {"state": "no_window", "control_window": "none",
                                              "tell_user": TELL_HEADLESS}
        assert s._control_window is None and not s.hub.requested
