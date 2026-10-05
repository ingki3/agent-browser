"""WS-29: MCP 경로의 사람 인계(조작권) + 승인 증표.

* 브라우저 없는 단위: call_tool 앞단의 조작권 거부(19종 각각), 관찰 허용, secret_wanted, approval_id 떼기,
  headless 거부, 승인 증표 발급·검증·outcome_unknown, 안내 문구, tools/list.
* 시연 통합(로컬 Mock + 실제 Chromium):
  ① 캡차 → control_request → (사람 역할) CLI take → 에이전트 조작 거부 → 사람이 해결 → CLI release →
    control_wait 반환 → 에이전트가 관찰·이어서 답.
  ② 결제 버튼 → 차단+approval_id → 승인 전 재호출 거부 → CLI approve → 재호출 성공 → 재사용 거부 →
    페이지가 바뀐 뒤(epoch/대상 변경) 이전 id 거부.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from contracts import ActionResult, ActionType, ErrorCode, ExecutionMode
from interface import cli, handoff, mcp_server
from interface.mcp_server import SERVER_TOOLS, BrowserMCPServer, tool_name

from test_run_cli import fake_llm, requires_chromium, server  # noqa: F401 - 픽스처·판정 재사용
from ws29_helpers import code_from_banner, srv_approve

MANIPULATION_ARGS: Dict[ActionType, Dict[str, Any]] = {
    ActionType.CLICK: {"element_id": "@e1", "epoch": 0},
    ActionType.TYPE_TEXT: {"element_id": "@e1", "epoch": 0, "text": "x"},
    ActionType.NAVIGATE: {"url": "http://127.0.0.1/"},
    ActionType.PRESS_KEY: {"key": "Enter"},
    ActionType.SELECT_OPTION: {"element_id": "@e1", "epoch": 0, "value": "a"},
    ActionType.CHECK_BOX: {"element_id": "@e1", "epoch": 0, "checked": True},
    ActionType.SCROLL: {"direction": "down"},
    ActionType.HOVER: {"element_id": "@e1", "epoch": 0},
    ActionType.UPLOAD_FILE: {"element_id": "@e1", "epoch": 0, "file_paths": ["/x"]},
    ActionType.DOWNLOAD_FILE: {"element_id": "@e1", "epoch": 0, "save_dir": "/tmp"},
    ActionType.HANDLE_DIALOG: {"accept": True},
    ActionType.SWITCH_FRAME: {"frame_selector": "iframe"},
    ActionType.RELOAD: {},
    ActionType.GO_BACK: {},
}
TAB_CHANGES = [{"command": "create"}, {"command": "switch", "tab_id": "t"},
               {"command": "close", "tab_id": "t"}]
OBSERVE_ARGS: Dict[ActionType, Dict[str, Any]] = {
    ActionType.OBSERVE_PAGE: {},
    ActionType.TAKE_SCREENSHOT: {},
    ActionType.EXTRACT: {"selector": "p"},
    ActionType.WAIT_FOR: {"condition": "stabilize"},
}


# ------------------------------------------------------------------ 브라우저 없는 서버


class _Handle:
    def __init__(self, name: str) -> None:
        self.name = name


class _Engine:
    def __init__(self) -> None:
        self.epoch = 0

    def get_handle(self, element_id: str) -> Any:
        return _Handle({"@e1": "결제하기"}.get(element_id, "보기"))

    def bump_epoch(self, reason: str = "") -> int:
        self.epoch += 1
        return self.epoch


class _Page:
    url = "http://127.0.0.1:9/shop"

    async def bring_to_front(self) -> None:
        return None


class _Core:
    active_tab_id = "tab-1"


class _Ctx:
    page = _Page()
    root_page = None


class _Dispatcher:
    def __init__(self) -> None:
        self.calls: List[ActionType] = []
        self.ctx = _Ctx()
        self.next_error: Any = None

    async def describe_element_for_gate(self, element_id: str) -> Dict[str, Any]:
        return {"info": {"signals": []}}

    async def approval_target_check(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"fresh": True, "detail": ""}

    async def dispatch(self, action: ActionType, params: Dict[str, Any]) -> ActionResult:
        self.calls.append(action)
        err = self.next_error
        return ActionResult(
            success=err is None, action=action, current_url=_Page.url, snapshot_epoch=0,
            tab_id="tab-1", healed=False, reobserve_required=False, retry_safe=True,
            error_code=err, error_message="x" if err else None, data={},
        )


def _server(tmp_path: Path, visible: bool = True, **kw: Any) -> BrowserMCPServer:
    """브라우저 없는 서버. visible=True 면 '사람이 볼 창이 있다'(승인 증표 켜짐, R1)로 두고
    창 오버레이 문구를 srv.banners 로 가로챈다(사람의 눈 역할 — 확인 코드를 여기서 읽는다)."""
    from security import HITLGate

    srv = BrowserMCPServer(handoff_root=tmp_path / "servers", **kw)
    srv.banners = []
    if visible:
        srv._human_can_see = lambda: True

        async def _banner(text: Any) -> bool:
            srv.banners.append(text)
            return True

        srv._set_banner = _banner
    srv._started = True
    srv._engine = _Engine()
    srv._page = _Page()
    srv._core = _Core()
    srv._dispatcher = _Dispatcher()
    srv._hitl = HITLGate(mode=srv.mode, pre_approved_actions=srv.pre_approved_actions)
    srv.hub.open()
    return srv


@pytest.fixture
def srv(tmp_path: Path):
    s = _server(tmp_path)
    yield s
    s.hub.close()


def _take(s: BrowserMCPServer) -> None:
    handoff.write_command(s.hub.root, s.hub.server_id, "take")


def _release(s: BrowserMCPServer) -> None:
    handoff.write_command(s.hub.root, s.hub.server_id, "release")


@pytest.mark.parametrize("action", list(MANIPULATION_ARGS))
async def test_human_holder_rejects_each_manipulation(srv, action):
    _take(srv)
    r = await srv.call_tool(tool_name(action), dict(MANIPULATION_ARGS[action]))
    assert not r.success
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    c = r.data["control"]
    assert c["holder"] == "human" and c["how_to_wait"] == "browser_control_wait"
    assert set(c) >= {"holder", "reason", "since", "how_to_wait"}
    assert srv._dispatcher.calls == []  # 디스패처까지 가지 않는다


@pytest.mark.parametrize("args", TAB_CHANGES)
async def test_human_holder_rejects_tab_changes(srv, args):
    _take(srv)
    r = await srv.call_tool(tool_name(ActionType.TAB_CONTROL), dict(args))
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED and "control" in r.data
    assert srv._dispatcher.calls == []


@pytest.mark.parametrize("action,args", list(OBSERVE_ARGS.items())
                         + [(ActionType.TAB_CONTROL, {"command": "list"})])
async def test_human_holder_allows_observation(srv, action, args, monkeypatch):
    async def _no_challenge(result: Any) -> None:
        return None

    monkeypatch.setattr(srv, "_attach_challenge", _no_challenge)
    _take(srv)
    r = await srv.call_tool(tool_name(action), dict(args))
    assert r.success, r
    assert srv._dispatcher.calls == [action]


@pytest.mark.parametrize("action,args", list(OBSERVE_ARGS.items())
                         + [(ActionType.TAB_CONTROL, {"command": "list"})])
async def test_secret_wanted_rejects_observation(srv, action, args, monkeypatch):
    monkeypatch.setattr(srv, "_human_can_see", lambda: True)
    out = await srv.call_server_tool("browser_control_request",
                                     {"reason": "비밀번호 입력", "secret_wanted": True})
    assert out["success"], out
    r = await srv.call_tool(tool_name(action), dict(args))
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert r.data["control"]["secret_wanted"] is True
    assert srv._dispatcher.calls == []


async def test_control_check_runs_before_input_validation(srv):
    """잘못된 인자여도 조작권 거부가 먼저 — 사람 조작 중에는 어떤 조작도 시도하지 않는다."""
    _take(srv)
    r = await srv.call_tool(tool_name(ActionType.CLICK), {"bogus": 1})
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED


async def test_release_bumps_epoch_and_wait_reports(srv, monkeypatch):
    monkeypatch.setattr(srv, "_human_can_see", lambda: True)
    await srv.call_server_tool("browser_control_request", {"reason": "캡차"})
    _take(srv)
    await srv._poll_handoff()
    before = srv._engine.epoch
    waiter = asyncio.ensure_future(srv.call_server_tool("browser_control_wait", {"timeout_s": 5}))
    await asyncio.sleep(0.1)
    _release(srv)
    out = await waiter
    assert out["data"]["changed"] == "released"
    assert out["data"]["control"]["holder"] == "agent"
    assert "observe_page" in out["data"]["hint"]
    assert srv._engine.epoch == before + 1


async def test_control_wait_timeout_and_cap(srv, monkeypatch):
    monkeypatch.setattr(srv, "_human_can_see", lambda: True)
    await srv.call_server_tool("browser_control_request", {"reason": "캡차"})
    out = await srv.call_server_tool("browser_control_wait", {"timeout_s": 0.2})
    assert out["data"]["changed"] == "timeout"
    assert mcp_server._wait_timeout(10_000) == handoff.WAIT_MAX_S
    assert mcp_server._wait_timeout(-5) == 0.0


async def test_headless_control_request_refused(tmp_path):
    srv = _server(tmp_path, visible=False)
    try:
        out = await srv.call_server_tool("browser_control_request", {"reason": "캡차"})
        assert not out["success"]
        assert "--browser human" in out["error_message"] and "user-chrome" in out["error_message"]
        assert srv.hub.status()["requested"] is False
    finally:
        srv.hub.close()


async def test_control_status_tool(srv):
    out = await srv.call_server_tool("browser_control_status", {})
    assert out["data"]["control"]["holder"] == "agent"


# ------------------------------------------------------------------ 승인 증표 (단위)


async def _blocked(srv: BrowserMCPServer, **extra: Any) -> ActionResult:
    return await srv.call_tool(tool_name(ActionType.CLICK),
                               {"element_id": "@e1", "epoch": 0, **extra})


async def test_block_carries_approval_and_message(srv):
    r = await _blocked(srv)
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    ap = r.data["approval"]
    assert set(ap) >= {"approval_id", "expires_at", "how_to_approve"}
    assert "action_digest" not in ap  # R1 NB-7
    assert f"agent-browser approve {ap['approval_id']}" in r.error_message
    assert r.data["pre_approve_hint"] == "click:결제하기"  # 기존 안내 유지
    # 우회 안내 없음
    for word in ("press_key", "press_enter", "evaluate", "browser_control"):
        assert word not in r.error_message


async def test_interactive_mode_also_carries_approval(tmp_path):
    s = _server(tmp_path, mode=ExecutionMode.INTERACTIVE)
    try:
        r = await _blocked(s)
        assert r.data["requires_confirmation"] is True
        assert r.data["approval"]["approval_id"] in r.error_message
    finally:
        s.hub.close()


async def test_approval_id_alone_never_executes(srv):
    """핵심: 에이전트가 들고 있는 approval_id 만으로는 절대 실행되지 않는다."""
    r = await _blocked(srv)
    aid = r.data["approval"]["approval_id"]
    for _ in range(3):
        again = await _blocked(srv, approval_id=aid)
        assert again.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
        assert again.data["approval"]["rejected"]["approval_id"] == aid
    # 에이전트 쪽 도구로는 승인할 수 없다(서버 도구는 요청·조회·대기뿐).
    assert not any("approve" in name and "wait" not in name for name in SERVER_TOOLS)
    out = await srv.call_server_tool("browser_approval_wait", {"approval_id": aid, "timeout_s": 0.1})
    assert out["data"]["approval"]["status"] == "pending"
    assert srv._dispatcher.calls == []


async def _approve(srv: BrowserMCPServer, aid: str) -> None:
    """사람: 코드 표시 요청 → 창 오버레이의 코드를 읽어 승인(R1)."""
    await srv_approve(srv, aid)


async def test_approved_then_single_use(srv):
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    await _approve(srv, aid)
    ok = await _blocked(srv, approval_id=aid)
    assert ok.success, ok
    assert ok.data["approval"] == {"approval_id": aid, "status": "used", "outcome": "succeeded"}
    again = await _blocked(srv, approval_id=aid)
    assert not again.success and "1회용" in again.data["approval"]["rejected"]["reason"]
    assert srv._dispatcher.calls == [ActionType.CLICK]


@pytest.mark.parametrize("mutate", ["epoch", "tab", "origin", "param"])
async def test_digest_bound_to_context(srv, mutate):
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    await _approve(srv, aid)
    args: Dict[str, Any] = {"approval_id": aid}
    if mutate == "epoch":
        srv._engine.epoch = 1
        args["epoch"] = 1
    elif mutate == "tab":
        srv._core.active_tab_id = "tab-2"
    elif mutate == "origin":
        srv._dispatcher.ctx = type("C", (), {"page": type("P", (), {"url": "http://evil.test/"})(),
                                             "root_page": None})()
    else:
        args["expected_name"] = "결제하기"
    try:
        r = await _blocked(srv, **args)
        assert not r.success
        assert "불일치" in r.data["approval"]["rejected"]["reason"]
        assert srv._dispatcher.calls == []
    finally:
        srv._core.active_tab_id = "tab-1"


@pytest.mark.parametrize("code", [ErrorCode.TIMEOUT, ErrorCode.PAGE_CRASHED,
                                  ErrorCode.NAVIGATE_TIMEOUT])
async def test_uncertain_result_is_outcome_unknown(srv, code):
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    await _approve(srv, aid)
    srv._dispatcher.next_error = code
    r = await _blocked(srv, approval_id=aid)
    assert r.data["approval"]["outcome"] == "outcome_unknown"
    assert r.retry_safe is False and "다시 시도하지" in r.data["approval"]["note"]
    srv._dispatcher.next_error = None
    again = await _blocked(srv, approval_id=aid)
    assert not again.success  # 같은 증표 재사용 불가
    assert srv.hub.approval_status(aid)["outcome"] == "outcome_unknown"


async def test_approval_wait_returns_on_approve(srv):
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    waiter = asyncio.ensure_future(srv.call_server_tool(
        "browser_approval_wait", {"approval_id": aid, "timeout_s": 5}))
    await asyncio.sleep(0.1)
    await _approve(srv, aid)
    out = await waiter
    assert out["data"]["approval"]["status"] == "approved"


async def test_approval_id_is_stripped_before_contract_validation(srv, monkeypatch):
    """저위험 액션에 approval_id 를 붙여도 계약 입력 검증에 걸리지 않는다(경계에서 떼어 냄)."""
    async def _no_challenge(result: Any) -> None:
        return None

    monkeypatch.setattr(srv, "_attach_challenge", _no_challenge)
    r = await srv.call_tool(tool_name(ActionType.SCROLL), {"direction": "down", "approval_id": "ap_x"})
    assert r.success, r


def test_tools_list_has_server_tools_separately():
    names = [t["name"] for t in mcp_server.build_listed_tools()]
    assert len(mcp_server.build_all_tools()) == 19  # 액션 툴 의미 유지
    assert names[19:] == list(SERVER_TOOLS)
    assert all(n.startswith("browser_") for n in SERVER_TOOLS)
    assert all(mcp_server.action_from_tool(n) is None for n in SERVER_TOOLS)


def test_server_tools_token_cost_small():
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    extra = len(enc.encode(json.dumps(mcp_server.build_server_tools(), ensure_ascii=False)))
    assert extra <= 300, extra  # 실측 293 (WS-29 보고서)


async def test_close_removes_state_dir(tmp_path):
    s = _server(tmp_path)
    d = s.hub.dir
    s._core = None
    await s.close()
    assert not d.exists()


def test_cli_serve_passes_approval_ttl(monkeypatch):
    seen: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    assert cli.main(["serve", "--approval-ttl", "600"]) == 0
    assert seen["approval_ttl_s"] == 600
    seen.clear()
    assert cli.main(["serve"]) == 0
    assert "approval_ttl_s" not in seen  # 기본값은 run_stdio 쪽(1800)


# ------------------------------------------------------------------ 시연 통합 (실제 Chromium)

SHOP = """<!doctype html><meta charset=utf-8><title>상점</title>
<h1>사과</h1><p id=price>가격 3,000원</p>"""
CAPTCHA = """<!doctype html><meta charset=utf-8><title>확인</title><p>로봇이 아닙니다</p>
<iframe src='/recaptcha/api2/anchor?k=x' width=304 height=78></iframe>
<button id=solve onclick="fetch('/solve').then(()=>location.replace('/shop'))">사람 확인 완료</button>"""
PAY = """<!doctype html><meta charset=utf-8><title>결제</title><p id=out>대기</p>
<button onclick="document.getElementById('out').textContent='결제됨 '+(+(this.dataset.n=(+this.dataset.n||0)+1))">결제하기</button>
<button onclick="document.getElementById('out').textContent='보기'">보기</button>"""


@pytest.fixture
def mock_site():
    state = {"solved": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/solve"):
                state["solved"] = True
                body = "ok"
            elif self.path.startswith("/recaptcha/"):
                body = "<p>위젯</p>"
            elif self.path.startswith("/shop"):
                body = SHOP if state["solved"] else CAPTCHA
            elif self.path.startswith("/pay"):
                body = PAY
            else:
                body = SHOP
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
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _human_cli(*argv: str) -> int:
    """사람 역할: 별도 스레드에서 실제 CLI(`agent-browser control …`/`approve …`)를 실행한다."""
    return await asyncio.to_thread(cli.main, list(argv))


def _by_name(obs: ActionResult) -> Dict[str, str]:
    return {e["name"]: e["element_id"] for e in obs.data["observation"]["elements"]}


@requires_chromium
async def test_demo_captcha_handoff(mock_site, tmp_path, monkeypatch):
    s = BrowserMCPServer(handoff_root=Path(handoff.state_root()))
    # 테스트는 headless 로 돌리되 '사람이 볼 창이 있다'고 둔다(시연은 serve --browser human).
    monkeypatch.setattr(s, "_human_can_see", lambda: True)
    async with s:
        # 에이전트: 가격 확인 → 캡차 신호
        nav = await s.call_tool("browser_navigate", {"url": mock_site + "/shop"})
        assert nav.data["challenge"] is not None, nav.data
        # 에이전트: 사람에게 조작권 요청
        req = await s.call_server_tool("browser_control_request", {"reason": "캡차 확인 필요"})
        assert req["success"] and req["data"]["holder"] == "agent"
        assert s.hub.server_id in req["data"]["how_to_respond"]
        # 사람: take (실제 CLI)
        assert await _human_cli("control", "take") == 0
        # 에이전트: 조작은 거부, 관찰은 허용
        blocked = await s.call_tool("browser_navigate", {"url": mock_site + "/shop"})
        assert blocked.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
        assert blocked.data["control"]["holder"] == "human"
        seen = await s.call_tool("browser_observe_page", {})
        assert seen.success
        epoch_before = seen.data["observation"]["snapshot_epoch"]
        stale_id = _by_name(seen)["사람 확인 완료"]
        # 에이전트: 반납을 기다린다
        waiter = asyncio.ensure_future(s.call_server_tool("browser_control_wait", {"timeout_s": 20}))
        # 사람: 창에서 캡차를 푼다(에이전트 도구가 아니라 페이지를 직접 조작) → release
        await s._page.click("#solve")
        await s._page.wait_for_url("**/shop")
        await s._page.wait_for_selector("#price")
        assert await _human_cli("control", "release") == 0
        got = await waiter
        assert got["data"]["changed"] == "released"
        # 에이전트: 이전 element_id 는 무효 → 다시 관찰하고 이어서 답
        stale = await s.call_tool("browser_click", {"element_id": stale_id, "epoch": epoch_before})
        assert not stale.success
        obs = await s.call_tool("browser_observe_page", {})
        assert obs.data["challenge"] is None
        assert obs.data["observation"]["snapshot_epoch"] > epoch_before
        price = await s.call_tool("browser_extract", {"selector": "#price"})
        assert "3,000원" in json.dumps(price.data, ensure_ascii=False)


def _watch_overlay(s: BrowserMCPServer) -> List[Any]:
    """headless 로 돌리되 사람이 볼 창이 있다고 두고 창 오버레이 문구를 가로챈다(사람의 눈)."""
    s._human_can_see = lambda: True
    seen: List[Any] = []
    real = s._set_banner

    async def spy(text: Any) -> bool:
        seen.append(text)
        return await real(text)

    s._set_banner = spy
    return seen


@requires_chromium
async def test_demo_payment_approval(mock_site, monkeypatch):
    monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
    async with BrowserMCPServer(handoff_root=Path(handoff.state_root())) as s:
        banners = _watch_overlay(s)
        await s.call_tool("browser_navigate", {"url": mock_site + "/pay"})
        obs = await s.call_tool("browser_observe_page", {})
        ep = obs.data["observation"]["snapshot_epoch"]
        pay = {"element_id": _by_name(obs)["결제하기"], "epoch": ep}
        out_text = lambda: s._page.text_content("#out")  # noqa: E731

        # 1) 차단 + 증표
        r1 = await s.call_tool("browser_click", dict(pay))
        assert r1.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
        aid = r1.data["approval"]["approval_id"]
        assert f"agent-browser approve {aid}" in r1.error_message
        # 2) 승인 전 같은 id 로 재호출 → 거부
        r2 = await s.call_tool("browser_click", {**pay, "approval_id": aid})
        assert not r2.success and r2.data["approval"]["rejected"]["approval_id"] == aid
        assert await out_text() == "대기"
        # 3) 사람: CLI 승인 — 창에 코드 띄우기 → 창에서 읽은 코드 입력(서버 감시 태스크가 처리)
        assert await _human_cli("approve", aid, "--yes") == 2  # --yes 만으로는 안 됨(R1)
        assert await _human_cli("approve", aid) == 2  # 코드 표시, 비TTY 라 --code 로 다시
        assert await _human_cli("approve", aid, "--code", code_from_banner(banners)) == 0
        # 4) 재호출 성공
        r4 = await s.call_tool("browser_click", {**pay, "approval_id": aid})
        assert r4.success, r4
        assert r4.data["approval"]["outcome"] == "succeeded"
        assert await out_text() == "결제됨 1"
        # 5) 같은 id 재사용 거부
        r5 = await s.call_tool("browser_click", {**pay, "approval_id": aid})
        assert not r5.success and "1회용" in r5.data["approval"]["rejected"]["reason"]
        assert await out_text() == "결제됨 1"
        # 6) 새 증표를 승인해 두고 페이지가 바뀐 뒤(epoch 변경) 이전 id 로 → 거부
        r6 = await s.call_tool("browser_click", dict(pay))
        aid2 = r6.data["approval"]["approval_id"]
        assert aid2 != aid
        assert await _human_cli("approve", aid2) == 2
        assert await _human_cli("approve", aid2, "--code", code_from_banner(banners)) == 0
        await s.call_tool("browser_reload", {})
        obs2 = await s.call_tool("browser_observe_page", {})
        ep2 = obs2.data["observation"]["snapshot_epoch"]
        assert ep2 > ep
        r7 = await s.call_tool("browser_click", {"element_id": _by_name(obs2)["결제하기"],
                                                 "epoch": ep2, "approval_id": aid2})
        assert not r7.success
        assert "불일치" in r7.data["approval"]["rejected"]["reason"]
        assert await out_text() == "대기"  # 새로고침 뒤 아무것도 결제되지 않음


# ------------------------------------------------------------------ run --handoff 회귀 (동작 변화 0)


@requires_chromium
def test_run_handoff_unchanged_and_does_not_open_server_state(server, fake_llm, tmp_path):
    """`run --handoff` 는 기존 신호 파일 경로 그대로 — 서버 상태 디렉터리(WS-29)를 만들지 않는다."""
    import time

    url, state = server
    state["blocked"] = True
    done = tmp_path / "sig" / "handoff.done"
    out = tmp_path / "r.json"

    def human():
        time.sleep(1.5)
        state["blocked"] = False
        time.sleep(1.0)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.touch()

    th = threading.Thread(target=human, daemon=True)
    th.start()
    code = cli.main(["run", "--url", url, "--goal", "사과 가격을 알려 줘", "--out", str(out),
                     "--handoff", "--handoff-wait", "20", "--handoff-file", str(done)])
    th.join(5)
    assert code == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["completed"] is True and rec["handoffs"][0]["via"] == "file"
    assert not handoff.state_root().exists(), "run 경로는 serve 상태 디렉터리를 만들지 않는다"


# ------------------------------------------------------------------ 실제 serve 프로세스(MCP stdio)


async def test_real_serve_prints_server_id_and_serves_control_tools(tmp_path):
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = handoff.state_root()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    errlog = tmp_path / "stderr.txt"
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "interface.cli", "serve"], env=env)
    with open(errlog, "w") as err:
        async with stdio_client(params, errlog=err) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = [t.name for t in listed.tools]
                assert len(names) == 23 and set(SERVER_TOOLS) <= set(names)
                res = await session.call_tool("browser_control_status", {})
                st = json.loads(res.content[0].text)
                assert st["data"]["control"]["holder"] == "agent"
                sid = st["data"]["control"]["server_id"]
                assert [s["server_id"] for s in handoff.list_servers(root)] == [sid]
                # 사람 쪽 CLI 가 이 서버를 자동 선택한다
                assert await _human_cli("control", "status") == 0
                res = await session.call_tool("browser_control_request", {"reason": "캡차"})
                assert "--browser human" in json.loads(res.content[0].text)["error_message"]
    text = errlog.read_text(encoding="utf-8")
    assert f"server_id={sid}" in text and "agent-browser control take" in text
    # 정상 종료(stdin EOF) 뒤 상태 디렉터리가 정리된다
    for _ in range(50):
        if not (root / sid).exists():
            break
        await asyncio.sleep(0.1)
    assert not (root / sid).exists()
