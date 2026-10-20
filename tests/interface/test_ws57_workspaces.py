"""WS-57 작업 공간 — 같은 serve 를 대화·감시(정기 작업)가 동시에 써도 서로의 활성 탭·epoch·탭 닫기·navigate 영향 0.

실사용 2026-10-14: Hermes 는 프로세스당 MCP 연결 하나를 대화·cron 이 공유하고 호출을 동시에 보낸다. 감시 cron 이
대화가 사람에게 열어 준 창의 마지막 탭을 닫았다. 로컬 http·임시 프로필·합성 값만.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from interface.mcp_server import BrowserMCPServer, envelope_dict

from test_run_cli import requires_chromium


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str, headers: Dict[str, str] = None) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            cookies = self.headers.get("Cookie") or ""
            if self.path.startswith("/login"):
                self._send("<title>로그인됨</title><p>ok</p>",
                           {"Set-Cookie": "auth=synthetic; Max-Age=3600; Path=/"})
            elif self.path.startswith("/whoami"):
                who = "로그인" if "auth=synthetic" in cookies else "손님"
                self._send(f"<title>{who}</title><p id=who>{who}</p>")
            else:
                name = self.path.strip("/") or "home"
                self._send(f"<title>{name}</title><button onclick=\"this.textContent='눌림'\">{name} 버튼</button>")

        def log_message(self, *a):  # noqa: D401
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


def _caller(s: BrowserMCPServer, workspace: str = None):
    async def call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        args = dict(args)
        if workspace is not None:
            args["workspace"] = workspace
        if name.startswith("browser_control") or name.startswith("browser_approval") or name == "browser_recipe":
            return await s.call_server_tool(name, args)
        return envelope_dict(await s.call_tool(name, args))
    return call


def _names(obs: Dict[str, Any]):
    return [e["name"] for e in obs["data"]["observation"]["elements"]]


@requires_chromium
async def test_two_workspaces_concurrently_keep_their_own_tab_and_epoch(site):
    async with BrowserMCPServer() as s:
        chat, watch = _caller(s), _caller(s, "watch")
        a, b = await asyncio.gather(chat("browser_navigate", {"url": site.url + "/chat"}),
                                    watch("browser_navigate", {"url": site.url + "/watch"}))
        assert a["success"] and b["success"], (a, b)
        assert a["tab_id"] != b["tab_id"]
        oa, ob = await asyncio.gather(chat("browser_observe_page", {}), watch("browser_observe_page", {}))
        assert _names(oa) == ["chat 버튼"] and _names(ob) == ["watch 버튼"]
        epoch_a = oa.get("snapshot_epoch", 0)
        # 감시가 여러 번 이동해도 대화 쪽 epoch·페이지는 그대로
        for path in ("/w1", "/w2", "/w3"):
            assert (await watch("browser_navigate", {"url": site.url + path}))["success"]
        oa2 = await chat("browser_observe_page", {})
        assert oa2.get("snapshot_epoch", 0) == epoch_a
        assert oa2["current_url"].endswith("/chat")
        # 대화가 받은 요소 id 로 그대로 누를 수 있다(감시 쪽 이동이 요소를 낡게 하지 않음)
        el = oa["data"]["observation"]["elements"][0]["element_id"]
        click = await chat("browser_click", {"element_id": el, "epoch": epoch_a})
        assert click["success"], click


@requires_chromium
async def test_tab_close_and_list_stay_inside_the_workspace(site):
    async with BrowserMCPServer() as s:
        chat, watch = _caller(s), _caller(s, "watch")
        a = await chat("browser_navigate", {"url": site.url + "/chat"})
        b = await watch("browser_navigate", {"url": site.url + "/watch"})
        # 감시 목록: 자기 탭만 + 다른 작업 공간 탭은 개수만(주소 비노출)
        lb = await watch("browser_tab_control", {"command": "list"})
        assert [t["tab_id"] for t in lb["data"]["tabs"]] == [b["tab_id"]]
        assert lb["data"]["other_workspace_tabs"] == 1
        assert "/chat" not in str(lb["data"])
        # 남의 탭은 전환·닫기 불가
        for cmd in ("switch", "close"):
            r = await watch("browser_tab_control", {"command": cmd, "tab_id": a["tab_id"]})
            assert not r["success"] and r["error_code"] == "E_TAB_NOT_FOUND", r
        # 감시가 습관적으로 자기 마지막 탭을 닫아도(10/14) 대화 탭은 그대로
        closed = await watch("browser_tab_control", {"command": "close", "tab_id": b["tab_id"]})
        assert closed["success"], closed
        oa = await chat("browser_observe_page", {})
        assert oa["success"] and oa["current_url"].endswith("/chat") and oa["tab_id"] == a["tab_id"]
        la = await chat("browser_tab_control", {"command": "list"})
        assert [t["tab_id"] for t in la["data"]["tabs"]] == [a["tab_id"]]
        # 단일 작업 공간만 쓰면 other_workspace_tabs 는 없다(기존 응답 그대로) — 감시 탭이 남아 있어 여기선 1
        assert la["data"]["other_workspace_tabs"] == 1


@requires_chromium
async def test_window_reopen_restores_each_workspace_tabs_with_owner(site):
    """on-demand 창 전환(브라우저 재기동)이 감시 쪽 호출에서 일어나도 탭은 주인째 복원된다."""
    from browser.workspace import workspace_scope

    async with BrowserMCPServer(browser_mode="on-demand") as s:
        chat, watch = _caller(s), _caller(s, "watch")
        a = await chat("browser_navigate", {"url": site.url + "/chat"})
        b = await watch("browser_navigate", {"url": site.url + "/watch"})
        assert a["success"] and b["success"]
        epoch_b = b.get("snapshot_epoch", 0)
        with workspace_scope("watch"):
            out = await s._switch_window(False, "recover", force=True)
        assert out["ok"], out
        restored = {t["url"].rsplit("/", 1)[-1]: t for t in out["window"]["tabs"]}
        assert "workspace" not in restored["chat"] and restored["watch"]["workspace"] == "watch"
        oa = await chat("browser_observe_page", {})
        ob = await watch("browser_observe_page", {})
        assert oa["current_url"].endswith("/chat") and _names(oa) == ["chat 버튼"]
        assert ob["current_url"].endswith("/watch") and _names(ob) == ["watch 버튼"]
        assert ob.get("snapshot_epoch", 0) > epoch_b  # 다시 열었으니 감시 쪽 관찰도 낡음
        lb = await watch("browser_tab_control", {"command": "list"})
        assert len(lb["data"]["tabs"]) == 1 and lb["data"]["other_workspace_tabs"] == 1


@requires_chromium
async def test_login_cookies_are_shared(site):
    async with BrowserMCPServer() as s:
        chat, watch = _caller(s), _caller(s, "watch")
        assert (await chat("browser_navigate", {"url": site.url + "/login"}))["success"]
        r = await watch("browser_navigate", {"url": site.url + "/whoami"})
        assert r["success"]
        ex = await watch("browser_extract", {"selector": "#who"})
        assert "로그인" in str(ex["data"]), ex


@requires_chromium
async def test_single_session_has_no_workspace_traces(site):
    async with BrowserMCPServer() as s:
        chat = _caller(s)
        await chat("browser_navigate", {"url": site.url + "/one"})
        lst = await chat("browser_tab_control", {"command": "list"})
        assert "other_workspace_tabs" not in lst["data"]
        assert set(s._workspace_runtimes()) == {"default"}


async def test_bad_workspace_name_is_refused_before_anything():
    s = BrowserMCPServer()
    r = envelope_dict(await s.call_tool("browser_navigate", {"url": "http://127.0.0.1:1/", "workspace": "../x"}))
    assert not r["success"] and r["error_code"] == "E_TAB_NOT_FOUND"
    assert not s.started  # 브라우저를 띄우지 않았다
    p = await s.call_server_tool("browser_recipe", {"command": "list", "workspace": "Bad Name"})
    assert p["success"] is False and p["error_code"] == "E_TAB_NOT_FOUND"


async def test_per_call_state_does_not_mix_between_concurrent_calls():
    """_used_approval·_pending_gate_basis 는 호출(태스크)마다 따로 — 같은 작업 공간의 동시 호출도."""
    s = BrowserMCPServer()

    async def one(tag: str, delay: float) -> tuple:
        with s._call_scope(None):
            s._used_approval = f"ap-{tag}"
            s._pending_gate_basis = {"who": tag}
            await asyncio.sleep(delay)
            # 하위 태스크(wait_for 등)에서 쓴 값도 같은 호출 안에서는 보인다
            async def child():
                s._pending_gate_basis = {"who": tag, "child": True}
            await asyncio.wait_for(child(), 1)
            await asyncio.sleep(delay)
            return s._used_approval, s._pending_gate_basis

    r1, r2 = await asyncio.gather(one("a", 0.02), one("b", 0.01))
    assert r1 == ("ap-a", {"who": "a", "child": True})
    assert r2 == ("ap-b", {"who": "b", "child": True})


def test_recipe_trajectories_are_per_workspace_but_share_the_store():
    """두 작업 공간의 동작이 한 궤적에 섞이면 엉터리 레시피가 저장된다 — 기록기는 따로, 저장소는 하나."""
    from browser.workspace import workspace_scope

    s = BrowserMCPServer()
    base = s._recipes
    with workspace_scope("watch"):
        mine = s._recipes
        assert mine is not None and mine is not base
        assert mine.trajectory is not base.trajectory
        assert mine.store is base.store
        assert s._recipes is mine  # 한 번 만든 것을 계속 쓴다
    assert s._recipes is base
    s._reset_all_recipes("human_control")
    s._close_recipes()


def test_no_recipes_means_no_recipes_in_any_workspace():
    from browser.workspace import workspace_scope

    s = BrowserMCPServer(recipes=False)
    with workspace_scope("watch"):
        assert s._recipes is None
