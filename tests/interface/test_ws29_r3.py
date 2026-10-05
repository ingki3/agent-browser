"""WS-29 R3: R2 재검증 지적(NB-1~4) 마무리.

NB-2 오버레이를 띄운 탭을 사람이 닫으면 코드 표시 상태가 고정돼 화면 캡처·승인이 영구히 막혔다
     (회복은 서버 재시작뿐) + 탭 복구 전 거부 결과가 tab_id=None 으로 ValidationError.
NB-1 외톨이 서로게이트 이름으로 승인 발급 시 UnicodeEncodeError.
NB-4 진행 중 캡처 상한 초과로 표시 취소 시 ack 가 "창이 닫혔거나…" 로 원인을 잘못 안내.
NB-3 뮤턴트 생존 2종(take_code_job 의 _expire 생략, _server_view 의 id·server 대조 생략).
"""

from __future__ import annotations

import asyncio
import io
import json
import contextlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from contracts import ActionResult, ActionType, ErrorCode
from interface import handoff, mcp_server
from interface.handoff import HandoffHub, action_digest, write_command
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401
from test_ws29_mcp_handoff import _blocked, _Dispatcher, _Engine, _Handle, _Page, _server
from ws29_helpers import CODE_RE

SHOT = tool_name(ActionType.TAKE_SCREENSHOT)
CLICK = tool_name(ActionType.CLICK)


# ======================================================================= 가짜 브라우저(탭·CDP 오버레이)


class _FPage:
    url = _Page.url

    def __init__(self) -> None:
        self.closed = False
        self.overlay: Optional[str] = None
        #: 닫히지 않았는데 CDP 전송이 실패하는 탭(세션 끊김 등 — 오버레이가 남았는지 모른다).
        self.broken = False
        #: 다음 전송 도중 닫힌다(전송 중 사람이 탭을 닫음).
        self.close_on_send = False

    def is_closed(self) -> bool:
        return self.closed

    async def bring_to_front(self) -> None:
        return None


class _FCDP:
    def __init__(self, page: _FPage) -> None:
        self.page = page

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.page.close_on_send:
            self.page.close_on_send = False
            self.page.closed = True
            self.page.overlay = None
        if self.page.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        if self.page.broken:
            raise RuntimeError("Session closed")
        if method == "Overlay.setPausedInDebuggerMessage":
            self.page.overlay = (params or {}).get("message")
        return {}


class _FTab:
    def __init__(self, tab_id: str) -> None:
        self.tab_id = tab_id
        self.page = _FPage()
        self.profile_name = "mcp-session"


class _FCore:
    """BrowserCore 의 탭 규칙을 흉내: 닫힌 탭은 목록에서 빠지고 활성 탭은 남은 첫 탭(없으면 None)."""

    def __init__(self) -> None:
        self._tabs: Dict[str, _FTab] = {}
        self.all: List[_FTab] = []
        self.active_tab_id: Optional[str] = None
        self._n = 0
        self.add()

    def add(self) -> _FTab:
        self._n += 1
        tab = _FTab(f"tab-{self._n}")
        self._tabs[tab.tab_id] = tab
        self.all.append(tab)
        self.active_tab_id = tab.tab_id
        return tab

    def get_tab(self, tab_id: Optional[str]) -> Optional[_FTab]:
        return self._tabs.get(tab_id) if tab_id else None

    def tabs(self) -> List[_FTab]:
        return list(self._tabs.values())

    async def new_tab(self, profile_name: str) -> _FTab:
        return self.add()

    def set_active_tab(self, tab_id: str) -> None:
        self.active_tab_id = tab_id

    async def new_cdp_session(self, tab_id: Optional[str] = None) -> _FCDP:
        tab = self._tabs.get(tab_id or self.active_tab_id or "")
        if tab is None:
            raise RuntimeError(f"탭을 찾을 수 없습니다: {tab_id}")
        return _FCDP(tab.page)

    def human_close(self, tab_id: str) -> None:
        """사람이 창에서 탭을 닫는다 — 오버레이는 탭과 함께 사라진다."""
        tab = self._tabs.pop(tab_id)
        tab.page.closed = True
        tab.page.overlay = None
        if self.active_tab_id == tab_id:
            self.active_tab_id = next(iter(self._tabs), None)

    def visible_codes(self) -> List[str]:
        """사람의 눈: 아직 열린 탭 위에 보이는 확인 코드."""
        return [m.group(1) for t in self._tabs.values() if t.page.overlay
                for m in [CODE_RE.search(t.page.overlay)] if m]


class _FCtx:
    def __init__(self, core: _FCore) -> None:
        self.tab_id = core.active_tab_id
        self.page = core.get_tab(self.tab_id).page
        self.root_page = None
        self.cdp = None


class _FDispatcher(_Dispatcher):
    def __init__(self, core: _FCore) -> None:
        super().__init__()
        self.ctx = _FCtx(core)

    def _set_active_page(self, page: Any, tab_id: str) -> None:
        self.ctx.page = page
        self.ctx.tab_id = tab_id


def _tab_srv(tmp_path: Path, tabs: int = 1) -> BrowserMCPServer:
    """진짜 _set_banner(오버레이 경로)를 쓰고 브라우저만 가짜인 서버."""
    srv = _server(tmp_path)
    del srv._set_banner  # 인스턴스 가로채기 해제 → BrowserMCPServer._set_banner
    core = _FCore()
    for _ in range(tabs - 1):
        core.add()
    core.set_active_tab("tab-1")
    srv._core = core
    srv._page = core.get_tab("tab-1").page
    srv._dispatcher = _FDispatcher(core)
    return srv


def _cmd(srv: BrowserMCPServer, op: str, aid: str = "", **extra: Any) -> str:
    fields: Dict[str, Any] = dict(extra)
    if aid:
        fields["approval_id"] = aid
        fields["action_digest"] = handoff.read_pending_approval(
            srv.hub.root, srv.hub.server_id, aid)["action_digest"]
    return write_command(srv.hub.root, srv.hub.server_id, op, **fields)


def _ack(srv: BrowserMCPServer, nonce: str) -> Optional[Dict[str, Any]]:
    p = srv.hub.dir / f"ack-{nonce}.json"
    return json.loads(p.read_text()) if p.exists() else None


async def _show(srv: BrowserMCPServer, aid: str) -> Dict[str, Any]:
    nonce = _cmd(srv, "show_code", aid)
    await srv._poll_handoff()
    ack = _ack(srv, nonce)
    assert ack is not None
    return ack


async def _approve(srv: BrowserMCPServer, aid: str, code: str) -> Dict[str, Any]:
    nonce = _cmd(srv, "approve", aid, code=code)
    await srv._poll_handoff()
    ack = _ack(srv, nonce)
    assert ack is not None
    return ack


async def _shot(srv: BrowserMCPServer) -> ActionResult:
    res = await srv.call_tool(SHOT, {})
    assert isinstance(res, ActionResult)  # 예외(ValidationError)가 아니라 결과
    return res


def _refused(res: ActionResult) -> bool:
    return not res.success and (res.data or {}).get("blocked_by") == "approval_code_displayed"


async def _release(srv: BrowserMCPServer) -> None:
    _cmd(srv, "take")
    await srv._poll_handoff()
    _cmd(srv, "release")
    await srv._poll_handoff()


async def _new_approval_works(srv: BrowserMCPServer) -> None:
    """새 승인: 막힘 → 코드 표시(보이는 탭 위) → 그 코드로 승인 → 실행."""
    core: _FCore = srv._core
    r = await _blocked(srv)
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    aid = r.data["approval"]["approval_id"]
    ack = await _show(srv, aid)
    assert ack["ok"] is True, ack
    codes = core.visible_codes()
    assert len(codes) == 1
    assert _refused(await _shot(srv))
    assert (await _approve(srv, aid, codes[0]))["ok"] is True
    assert core.visible_codes() == []  # 승인 뒤 코드는 창에서 내려간다
    calls = len(srv._dispatcher.calls)
    ok = await _blocked(srv, approval_id=aid)
    assert ok.success, ok
    assert srv._dispatcher.calls[calls:] == [ActionType.CLICK]
    assert (await _shot(srv)).success


# ======================================================================= NB-2 탭 닫힘


async def test_nb2_a_close_tab_while_code_displayed_recovers(tmp_path):
    """시나리오 A: 코드가 떠 있는 탭을 사람이 닫음 → 캡처 거부가 풀리고(코드는 창에 없으니 무효),
    탭 복구 전에도 거부 결과가 ValidationError 로 터지지 않고, release 뒤 새 승인이 된다."""
    srv = _tab_srv(tmp_path)
    core: _FCore = srv._core
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        assert (await _show(srv, aid))["ok"] is True
        old_code = core.visible_codes()[0]
        assert _refused(await _shot(srv))
        core.human_close("tab-1")  # 유일한 탭 — 활성 탭 없음
        assert core.active_tab_id is None
        # 캡처 호출이 먼저 감시를 한 번 돌린다 — 닫힘을 알아채 바로 풀린다(예외 아님)
        assert not _refused(await _shot(srv))
        await srv._poll_handoff()
        assert srv._code_on_overlay is False and srv._banner is None
        assert not srv.hub.code_displayed()  # 창에 없는 코드는 무효(R1 원칙)
        assert not _refused(await _shot(srv))
        # 사라진 탭의 코드로는 승인되지 않는다
        assert (await _approve(srv, aid, old_code))["ok"] is False
        # 창이 없으면 띄울 수 없다(정직한 실패) — 그래도 캡처 거부가 고정되지 않는다
        ack = await _show(srv, aid)
        assert ack["ok"] is False and "창" in ack["message"]
        assert not _refused(await _shot(srv))
        await _release(srv)  # 탭 복구(남은 탭 없음 → 새 탭)
        assert core.active_tab_id == "tab-2"
        assert srv._tab_notice is not None
        await _new_approval_works(srv)
    finally:
        srv.hub.close()


async def test_nb2_b_close_tab_after_code_cleared_then_show_again(tmp_path):
    """시나리오 B: 코드를 내린 뒤(틀린 코드 3회 → 폐기) 탭을 닫고 다시 show_code → 영구 거부 없음."""
    srv = _tab_srv(tmp_path)
    core: _FCore = srv._core
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        assert (await _show(srv, aid))["ok"] is True
        for _ in range(3):
            await _approve(srv, aid, "000000")
        assert srv._code_on_overlay is False and core.visible_codes() == []
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        core.human_close("tab-1")
        ack = await _show(srv, aid)
        assert ack["ok"] is False
        assert srv._code_on_overlay is False and srv._banner is None
        assert not srv.hub.code_displayed()
        assert not _refused(await _shot(srv))
        await _release(srv)
        await _new_approval_works(srv)
    finally:
        srv.hub.close()


async def test_nb2_close_tab_while_human_holds_control(tmp_path):
    """사람이 조작권을 쥔 동안(조작권 안내 문구가 있음) 코드 탭을 닫음 → 안내를 띄울 탭이 없어도
    코드는 창에 없으므로 캡처 거부가 풀린다(안내 복원 실패가 거부를 고정하지 않는다)."""
    srv = _tab_srv(tmp_path)
    core: _FCore = srv._core
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        _cmd(srv, "take")
        await srv._poll_handoff()
        assert srv._control_banner
        assert (await _show(srv, aid))["ok"] is True
        assert core.visible_codes()
        core.human_close("tab-1")
        await srv._poll_handoff()
        assert srv._code_on_overlay is False and not srv.hub.code_displayed()
        assert not _refused(await _shot(srv))
        _cmd(srv, "release")
        await srv._poll_handoff()
        await _new_approval_works(srv)
    finally:
        srv.hub.close()


async def test_nb2_show_retries_on_current_active_tab(tmp_path):
    """오버레이 탭이 닫히고 다른 탭이 남아 있으면 그 탭(현재 활성 탭)의 새 세션에 띄운다."""
    srv = _tab_srv(tmp_path, tabs=2)
    core: _FCore = srv._core
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        assert (await _show(srv, aid))["ok"] is True
        core.human_close("tab-1")
        assert core.active_tab_id == "tab-2"
        await srv._poll_handoff()
        assert not _refused(await _shot(srv))
        ack = await _show(srv, aid)
        assert ack["ok"] is True, ack
        assert srv._banner[0] == "tab-2"
        code = core.visible_codes()
        assert len(code) == 1 and core.get_tab("tab-2").page.overlay
        assert _refused(await _shot(srv))
        assert (await _approve(srv, aid, code[0]))["ok"] is True
        assert core.visible_codes() == [] and not _refused(await _shot(srv))
    finally:
        srv.hub.close()


async def test_nb2_tab_closed_mid_send_retries_on_active_tab(tmp_path):
    """표시 도중 오버레이 탭이 닫힘 → 남은 활성 탭의 새 세션으로 한 번 더 시도."""
    srv = _tab_srv(tmp_path, tabs=2)
    core: _FCore = srv._core
    try:
        assert await srv._set_banner("안내 1")
        assert srv._banner[0] == "tab-1"
        tab1 = core.get_tab("tab-1")
        tab1.page.close_on_send = True
        core._tabs.pop("tab-1")
        core.active_tab_id = "tab-2"
        # 활성 탭이 바뀌어 옛 탭을 지우려다 닫힘을 만난다 → 새 활성 탭에 띄운다
        assert await srv._set_banner("안내 2")
        assert srv._banner[0] == "tab-2"
        assert core.get_tab("tab-2").page.overlay == "안내 2"
    finally:
        srv.hub.close()


async def test_nb2_closing_other_tab_keeps_refusal(tmp_path):
    """fail-closed: 코드가 떠 있는 탭이 아닌 다른 탭을 닫으면 코드는 그대로 — 거부 유지."""
    srv = _tab_srv(tmp_path, tabs=2)
    core: _FCore = srv._core
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        assert (await _show(srv, aid))["ok"] is True
        core.human_close("tab-2")
        await srv._poll_handoff()
        assert core.visible_codes() and srv.hub.code_displayed()
        assert _refused(await _shot(srv))
    finally:
        srv.hub.close()


async def test_nb2_send_failure_on_live_tab_keeps_refusal(tmp_path):
    """fail-closed: 탭은 살아 있는데 지우기 전송이 실패하면(코드가 남았을 수 있음) 거부 유지."""
    srv = _tab_srv(tmp_path)
    core: _FCore = srv._core
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        assert (await _show(srv, aid))["ok"] is True
        core.get_tab("tab-1").page.broken = True
        for _ in range(3):
            await _approve(srv, aid, "000000")  # 코드 폐기 → 지우기 시도 → 전송 실패
        assert not srv.hub.code_displayed()
        assert core.visible_codes()  # 창에는 아직 남아 있다
        assert srv._code_on_overlay is True
        assert _refused(await _shot(srv))
        # 전송이 다시 되면 지우고 풀린다
        core.get_tab("tab-1").page.broken = False
        await srv._poll_handoff()
        assert core.visible_codes() == [] and not _refused(await _shot(srv))
    finally:
        srv.hub.close()


async def test_nb2_unknown_page_keeps_refusal(tmp_path):
    """fail-closed: 탭이 닫혔는지 확인할 수 없는 코어(get_tab 없음)에서는 전송 실패를 성공으로 치지 않는다."""
    srv = _tab_srv(tmp_path)
    core: _FCore = srv._core

    class _Blind:
        active_tab_id = "tab-1"

        async def new_cdp_session(self, tab_id: Optional[str] = None) -> _FCDP:
            return await core.new_cdp_session(tab_id)

    srv._core = _Blind()
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        assert (await _show(srv, aid))["ok"] is True
        core.get_tab("tab-1").page.closed = True
        for _ in range(3):
            await _approve(srv, aid, "000000")  # 코드 폐기 → 지우기 시도 → 전송 실패(닫힘 확인 불가)
        assert not srv.hub.code_displayed()
        assert srv._code_on_overlay is True
        assert _refused(await _shot(srv))
    finally:
        srv.hub.close()


def test_nb2_error_result_without_active_tab(tmp_path):
    """활성 탭이 없을 때(유일한 탭이 닫힘) _error_result 가 ValidationError 를 내지 않는다."""
    srv = _server(tmp_path)
    srv._core.active_tab_id = None
    try:
        res = srv._pixels_refused(ActionType.TAKE_SCREENSHOT)
        assert res.tab_id == "" and res.error_code is ErrorCode.SCREENSHOT_FAILED
    finally:
        srv._core.active_tab_id = "tab-1"
        srv.hub.close()


async def _wait_ack(srv: BrowserMCPServer, nonce: str, t: float = 8.0) -> Dict[str, Any]:
    for _ in range(int(t / 0.05)):
        ack = _ack(srv, nonce)
        if ack is not None:
            return ack
        await asyncio.sleep(0.05)
    raise AssertionError("ack 없음")


async def _until(cond: Any, t: float = 5.0) -> bool:
    for _ in range(int(t / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return bool(cond())


@requires_chromium
@pytest.mark.parametrize("scen", ["A", "B"])
async def test_nb2_real_overlay_tab_closed(scen, tmp_path):
    """검증자 probe_tab_close(시나리오 A·B)를 실 Chromium 으로: 탭을 닫은 뒤 영구 거부가 없고,
    release(탭 복구) 뒤 새 승인이 된다. 감시 태스크(서버 내장)가 명령을 처리한다."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page = ("<!doctype html><meta charset=utf-8><body><h1>shop</h1>"
            "<button onclick=\"document.title='paid'\">결제하기</button>").encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *a):  # noqa: ANN002
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{http.server_address[1]}/"
    seen: List[Any] = []
    try:
        async with BrowserMCPServer(som_enabled=True, handoff_root=tmp_path / "servers") as s:
            s._human_can_see = lambda: True
            real = s._set_banner

            async def spy(text):
                seen.append(text)
                return await real(text)

            s._set_banner = spy

            async def click() -> tuple:
                o = await s.call_tool("browser_observe_page", {})
                obs = o.data["observation"]
                eid = [e["element_id"] for e in obs["elements"] if e["name"] == "결제하기"][0]
                r = await s.call_tool(CLICK, {"element_id": eid, "epoch": obs["snapshot_epoch"]})
                return r.data["approval"]["approval_id"], eid, obs["snapshot_epoch"]

            async def cmd(op: str, aid: str = "", **kw: Any) -> Dict[str, Any]:
                return await _wait_ack(s, _cmd(s, op, aid, **kw))

            await s.call_tool("browser_navigate", {"url": url})
            aid, _, _ = await click()
            assert (await cmd("show_code", aid))["ok"] is True
            if scen == "B":
                for _ in range(3):
                    await cmd("approve", aid, code="000000")
                assert await _until(lambda: s._code_on_overlay is False)
                aid, _, _ = await click()
            await s._page.close()
            await asyncio.sleep(0.3)
            res = await _shot(s)  # 복구 전: 예외(ValidationError)가 아니라 결과
            assert isinstance(res.tab_id, str)
            await cmd("show_code", aid)  # 활성 탭이 없어 실패 — 그래도 거부가 고정되지 않는다
            assert await _until(lambda: s._code_on_overlay is False and not s.hub.code_displayed())
            assert not _refused(await _shot(s))
            await cmd("take")
            await cmd("release")
            assert await _until(lambda: s._tab_notice is not None)
            await s.call_tool("browser_navigate", {"url": url})
            assert (await _shot(s)).success
            aid2, eid, epoch = await click()
            n = len(seen)
            assert (await cmd("show_code", aid2))["ok"] is True
            codes = [m.group(1) for t in seen[n:] for m in [CODE_RE.search(t or "")] if m]
            assert codes
            assert _refused(await _shot(s))
            assert (await cmd("approve", aid2, code=codes[-1]))["ok"] is True
            done = await s.call_tool(CLICK, {"element_id": eid, "epoch": epoch,
                                             "approval_id": aid2})
            assert done.success, done
            assert await _until(lambda: not s._pixels_blocked())
            assert (await _shot(s)).success
    finally:
        http.shutdown()
        http.server_close()


# ======================================================================= NB-1 외톨이 서로게이트


SURROGATE_NAME = "결제하기\ud800"


def _components(name: str) -> Dict[str, Any]:
    return {
        "action": "click",
        "params": {"element_id": "@e1", "epoch": 0},
        "gate_basis": {"name": name, "matched_keyword": "결제", "source": "name"},
        "tab_id": "tab-1",
        "origin": "http://127.0.0.1:9",
        "snapshot_epoch": 0,
    }


def test_nb1_digest_accepts_lone_surrogate_and_stays_distinct():
    d = action_digest(_components(SURROGATE_NAME))
    assert len(d) == 64
    assert d != action_digest(_components("결제하기"))
    assert d != action_digest(_components("결제하기\ufffd"))
    assert d != action_digest(_components("결제하기\\ud800"))
    assert d != action_digest(_components("결제하기\udfff"))


def test_nb1_hub_issues_and_checks_surrogate_approval(tmp_path):
    hub = HandoffHub(tmp_path / "servers", browser_mode="human")
    hub.open()
    try:
        ap = hub.issue_approval(_components(SURROGATE_NAME), {"target": SURROGATE_NAME})
        data = handoff.read_pending_approval(hub.root, hub.server_id, ap.approval_id)
        assert data is not None and data["action_digest"] == ap.digest
        assert data["components"]["gate_basis"]["name"] == SURROGATE_NAME  # 원문 보존(판정용)
        # 사람이 볼 화면은 살균
        shown = handoff._show_approval(data)
        assert "\ud800" not in shown and "\\ud800" in shown
        shown.encode("utf-8")
        hub._finish(ap, "approved")
        assert hub.check_approval(ap.approval_id, _components(SURROGATE_NAME))[0]
        assert not hub.check_approval(ap.approval_id, _components("결제하기"))[0]
    finally:
        hub.close()


async def test_nb1_surrogate_click_blocked_with_approval(tmp_path):
    """판정은 막힌 그대로(고위험) · 증표는 정상 발급 · 응답은 직렬화 가능 · 디스패치 0."""
    srv = _server(tmp_path)

    class E(_Engine):
        def get_handle(self, element_id: str) -> Any:
            return _Handle(SURROGATE_NAME)

    srv._engine = E()
    try:
        r = await srv.call_tool(CLICK, {"element_id": "@e1", "epoch": 0})
        assert not r.success and r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
        assert r.data["approval"]["approval_id"] in srv.hub.approvals
        assert srv._dispatcher.calls == []
    finally:
        srv.hub.close()


async def test_nb1_surrogate_approval_full_cli_flow(tmp_path, monkeypatch):
    """사람 CLI(describe → 서버 기록 대조 → 코드 표시)가 서로게이트 이름에서도 동작한다."""
    srv = _server(tmp_path)

    class E(_Engine):
        def get_handle(self, element_id: str) -> Any:
            return _Handle(SURROGATE_NAME)

    srv._engine = E()
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(srv.hub.root))
    monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
    stop = asyncio.Event()

    async def bg() -> None:
        while not stop.is_set():
            await srv._poll_handoff()
            await asyncio.sleep(0.03)

    task = asyncio.create_task(bg())
    try:
        aid = (await srv.call_tool(CLICK, {"element_id": "@e1", "epoch": 0})
               ).data["approval"]["approval_id"]
        so, se = io.StringIO(), io.StringIO()

        def run() -> int:
            with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                return handoff.cli_approve(aid, srv.hub.server_id, yes=False)

        rc = await asyncio.to_thread(run)
        out = so.getvalue() + se.getvalue()
        assert "불일치" not in out and "검증 실패" not in out, out
        assert "\\ud800" in out and "\ud800" not in out
        assert srv.banners and CODE_RE.search(srv.banners[-1] or "")
        assert rc in (0, 1, 2)
        out.encode("utf-8")
    finally:
        stop.set()
        await task
        srv.hub.close()


# ======================================================================= NB-4 취소 사유 문구


def test_nb4_capture_busy_message_not_window_closed(tmp_path):
    hub = HandoffHub(tmp_path / "servers", browser_mode="human")
    hub.open()
    try:
        ap = hub.issue_approval(_components("결제하기"), {"target": "결제하기"})
        msgs = {}
        for reason in ("capture_busy", None):
            write_command(hub.root, hub.server_id, "show_code", approval_id=ap.approval_id,
                          action_digest=ap.digest)
            hub.poll()
            job = hub.take_code_job()
            assert job is not None
            if reason:
                hub.code_shown(job.nonce, False, reason=reason)
            else:
                hub.code_shown(job.nonce, False)
            msgs[reason] = json.loads((hub.dir / f"ack-{job.nonce}.json").read_text())
        busy, gone = msgs["capture_busy"], msgs[None]
        assert busy["ok"] is False and gone["ok"] is False
        assert "캡처" in busy["message"] and "창이 닫혔" not in busy["message"]
        assert "다시" in busy["message"]
        assert "창이 닫혔" in gone["message"]
    finally:
        hub.close()


async def test_nb4_server_reports_capture_busy(tmp_path, monkeypatch):
    """상한 초과로 표시 취소 시 ack 는 '진행 중 화면 캡처' 사유(창이 닫혔다는 오안내 아님)."""
    monkeypatch.setattr(mcp_server, "CAPTURE_DRAIN_TIMEOUT_S", 0.1, raising=False)
    srv = _server(tmp_path)
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        srv._captures_inflight = 1  # 끝나지 않는 캡처
        nonce = _cmd(srv, "show_code", aid)
        await asyncio.wait_for(srv._poll_handoff(), timeout=3)
        ack = _ack(srv, nonce)
        assert ack is not None and ack["ok"] is False
        assert "캡처" in ack["message"] and "창이 닫혔" not in ack["message"]
        srv._captures_inflight = 0
        assert not srv.hub.code_displayed()
        await srv._poll_handoff()
        assert srv._code_on_overlay is False
    finally:
        srv._captures_inflight = 0
        srv.hub.close()


# ======================================================================= NB-3 뮤턴트 공백


def test_nb3_take_code_job_drops_code_when_approval_expired_first(tmp_path):
    """승인 만료(ap.expires) < 코드 만료(code_expires) 인 창: 꺼낼 때 승인 만료를 따져 띄우지 않는다
    (take_code_job 의 _expire 생략 뮤턴트를 죽인다)."""
    now = [1000.0]
    hub = HandoffHub(tmp_path / "servers", browser_mode="human", approval_ttl_s=30.0,
                     clock=lambda: now[0])
    hub.open()
    try:
        ap = hub.issue_approval(_components("결제하기"), {"target": "결제하기"})
        nonce = write_command(hub.root, hub.server_id, "show_code", approval_id=ap.approval_id,
                              action_digest=ap.digest)
        hub.poll()
        assert ap.code_expires > ap.expires  # 코드 TTL(120s) > 남은 승인 TTL(30s)
        now[0] = ap.expires + 1.0  # 승인은 만료, 코드 시각은 아직 안 지남
        assert ap.status == "pending"  # 아직 아무도 만료 처리하지 않았다
        assert hub.take_code_job() is None
        ack = json.loads((hub.dir / f"ack-{nonce}.json").read_text())
        assert ack["ok"] is False
        assert ap.status == "expired"
    finally:
        hub.close()


def test_nb3_server_view_rejects_other_approval_id(tmp_path, monkeypatch):
    """서명은 맞지만 다른 승인 id·서버의 내용이면 거부한다(_server_view id 대조 생략 뮤턴트를 죽인다)."""
    sid = "123-abcdef"

    def fake_view(aid: str, server: str) -> Dict[str, Any]:
        return {"approval_id": aid, "server_id": server, "action_digest": "d" * 64,
                "status": "pending", "summary": {}, "components": {}}

    for view in (fake_view("ap_other", sid), fake_view("ap_mine", "999-ffffff")):
        def fake_send(root, s, op, timeout_s=5.0, _v=view, **fields):  # noqa: ANN001
            return {"ok": True, "view": _v, "view_mac": handoff.view_mac(fields["challenge"], _v)}

        monkeypatch.setattr(handoff, "_send", fake_send)
        got, why = handoff._server_view(tmp_path, sid, "ap_mine")
        assert got is None and "다릅니다" in why
    # 대조: 맞는 id·서버면 통과(테스트가 공허하지 않음)
    good = fake_view("ap_mine", sid)
    monkeypatch.setattr(handoff, "_send", lambda root, s, op, timeout_s=5.0, **f: {
        "ok": True, "view": good, "view_mac": handoff.view_mac(f["challenge"], good)})
    got, why = handoff._server_view(tmp_path, sid, "ap_mine")
    assert got == good
