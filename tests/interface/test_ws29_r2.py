"""WS-29 R2: 확인 코드 표시 ↔ 화면 캡처 레이스(BLOCKING) · VR 뮤턴트 4종 · 승인 화면은 서버 메모리 기준.

레이스(검증자 probe_race 실측): approve 로 코드 표시가 시작되기 **직전에 시작된** take_screenshot 이
표시 시작 **뒤에** 끝나며 오버레이 픽셀을 담아 에이전트에게 돌아갔다. 수정은 두 겹이다.
  (a)(b)(c) 표시 요청 → '표시 예정'(새 캡처 거부) → 진행 중 캡처가 끝나길 기다림(상한, 넘으면 표시 취소)
           → 그다음 오버레이.
  (d) 캡처 결과를 돌려주기 직전 '캡처 시작 이후 오버레이가 한 번이라도 켜졌는가'(세대 번호)를 보고
      켜졌으면 결과를 버리고 거부.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from contracts import ActionResult, ActionType
from interface import cli, handoff, mcp_server
from interface.handoff import HandoffHub, write_command
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401
from test_ws29_mcp_handoff import _blocked, _Dispatcher, _Page, _server
from ws29_helpers import CODE_RE, code_from_banner, hub_show_code

SHOT = tool_name(ActionType.TAKE_SCREENSHOT)


def _components(**over: Any) -> Dict[str, Any]:
    base = {
        "action": "click",
        "params": {"element_id": "@e3", "epoch": 2},
        "gate_basis": {"name": "결제하기", "matched_keyword": "결제", "source": "name"},
        "tab_id": "tab-1",
        "origin": "http://127.0.0.1:8000",
        "snapshot_epoch": 2,
    }
    base.update(over)
    return base


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "servers"


@pytest.fixture
def hub(root: Path):
    h = HandoffHub(root, browser_mode="human")
    h.open()
    yield h
    h.close()


def _has_code(text: Any) -> bool:
    return CODE_RE.search(str(text or "")) is not None


# ======================================================================= 1. 레이스 (가짜 느린 캡처)


class _SlowShots(_Dispatcher):
    """take_screenshot 이 gate 가 열릴 때까지 걸리는 디스패처(무거운 페이지의 수백 ms 캡처 흉내).

    '픽셀' = 캡처하는 동안 창 위에 있었던 오버레이 문구 전부(캡처는 순간이 아니다). 캡처 도중
    코드가 창에 올라갔으면 overlapped 에 남긴다(물리적으로 픽셀에 찍힐 수 있었다)."""

    def __init__(self, srv: BrowserMCPServer) -> None:
        super().__init__()
        self.srv = srv
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.overlapped = False

    async def dispatch(self, action: ActionType, params: Dict[str, Any]) -> ActionResult:
        if action is not ActionType.TAKE_SCREENSHOT:
            return await super().dispatch(action, params)
        self.calls.append(action)
        banners = self.srv.banners
        n0 = len(banners)
        on_screen = [banners[-1]] if banners else []
        self.started.set()
        await self.gate.wait()
        on_screen += banners[n0:]
        if any(_has_code(b) for b in on_screen):
            self.overlapped = True
        return ActionResult(
            success=True, action=action, current_url=_Page.url, snapshot_epoch=0,
            tab_id="tab-1", healed=False, reobserve_required=False, retry_safe=True,
            data={"image_b64": " | ".join(str(b) for b in on_screen)},
        )


def _slow_srv(tmp_path: Path) -> BrowserMCPServer:
    srv = _server(tmp_path)
    srv._dispatcher = _SlowShots(srv)
    return srv


def _request_code(srv: BrowserMCPServer, aid: str) -> str:
    shown = handoff.read_pending_approval(srv.hub.root, srv.hub.server_id, aid)
    return write_command(srv.hub.root, srv.hub.server_id, "show_code", approval_id=aid,
                         action_digest=shown["action_digest"])


def _ack(srv: BrowserMCPServer, nonce: str) -> Optional[Dict[str, Any]]:
    p = srv.hub.dir / f"ack-{nonce}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _leaked(res: ActionResult) -> bool:
    return bool(res.success) and _has_code((res.data or {}).get("image_b64"))


async def test_display_waits_for_inflight_capture_and_refuses_new(tmp_path):
    """(a)(b)(c): 진행 중 캡처가 있으면 표시는 기다리고, 그동안 새 캡처는 거부, 코드는 캡처가 끝난 뒤에만."""
    srv = _slow_srv(tmp_path)
    disp = srv._dispatcher
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        shot_task = asyncio.create_task(srv.call_tool(SHOT, {"annotate_som": True}))
        await disp.started.wait()
        nonce = _request_code(srv, aid)
        poll_task = asyncio.create_task(srv._poll_handoff())
        await asyncio.sleep(0.15)
        # (b) 아직 코드가 창에 없다 — 진행 중 캡처를 기다리는 중
        assert not any(_has_code(b) for b in srv.banners)
        assert not poll_task.done()
        # (a) 표시 예정 상태: 새 캡처는 디스패치 전에 거부
        calls_before = len(disp.calls)
        new = await srv.call_tool(SHOT, {})
        assert not new.success and new.data["blocked_by"] == "approval_code_displayed"
        assert len(disp.calls) == calls_before
        disp.gate.set()
        shot = await shot_task
        await poll_task
        # (c) 그다음에야 코드가 떴다. 진행 중이던 캡처의 픽셀에는 코드가 없다.
        code = code_from_banner(srv.banners)
        assert not disp.overlapped
        assert not _leaked(shot)
        assert _ack(srv, nonce)["ok"] is True
        assert code
    finally:
        disp.gate.set()
        srv.hub.close()


async def test_generation_check_alone_discards_capture(tmp_path, monkeypatch):
    """(d) 만으로도 막힌다: 기다림(b)을 없애도, 캡처 도중 오버레이가 켜졌으면 결과를 버리고 거부."""
    srv = _slow_srv(tmp_path)
    disp = srv._dispatcher

    async def no_wait(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(srv, "_wait_captures_idle", no_wait, raising=False)
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        shot_task = asyncio.create_task(srv.call_tool(SHOT, {"annotate_som": True}))
        await disp.started.wait()
        _request_code(srv, aid)
        await srv._poll_handoff()
        assert _has_code(srv.banners[-1])  # (b) 를 꺼서 코드가 캡처 도중 올라갔다
        disp.gate.set()
        shot = await shot_task
        assert disp.overlapped  # 픽셀에 찍힐 수 있었다
        assert not shot.success
        assert shot.data.get("blocked_by") == "approval_code_displayed"
        assert "image_b64" not in shot.data
        assert not _leaked(shot)
    finally:
        disp.gate.set()
        srv.hub.close()


async def test_generation_check_catches_overlay_on_then_off_during_capture(tmp_path, monkeypatch):
    """(d) 는 '지금 떠 있는가'가 아니라 '캡처 시작 뒤 한 번이라도 켜졌는가'를 본다 — 캡처 도중
    코드가 떴다가 입력돼 내려가도 결과를 버린다."""
    srv = _slow_srv(tmp_path)
    disp = srv._dispatcher

    async def no_wait(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(srv, "_wait_captures_idle", no_wait, raising=False)
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        shot_task = asyncio.create_task(srv.call_tool(SHOT, {}))
        await disp.started.wait()
        _request_code(srv, aid)
        await srv._poll_handoff()
        code = code_from_banner(srv.banners)
        shown = handoff.read_pending_approval(srv.hub.root, srv.hub.server_id, aid)
        write_command(srv.hub.root, srv.hub.server_id, "approve", approval_id=aid,
                      action_digest=shown["action_digest"], code=code)
        await srv._poll_handoff()
        assert not srv._pixels_blocked()  # 지금은 내려갔다
        disp.gate.set()
        shot = await shot_task
        assert disp.overlapped
        assert not shot.success and shot.data.get("blocked_by") == "approval_code_displayed"
        # 그 뒤 새 캡처는 정상
        disp.gate.set()
        assert (await srv.call_tool(SHOT, {})).success
    finally:
        disp.gate.set()
        srv.hub.close()


async def test_display_cancelled_when_capture_does_not_finish(tmp_path, monkeypatch):
    """(b) 상한: 진행 중 캡처가 상한 안에 끝나지 않으면 표시를 취소한다(fail-closed — 코드 무효)."""
    monkeypatch.setattr(mcp_server, "CAPTURE_DRAIN_TIMEOUT_S", 0.2, raising=False)
    srv = _slow_srv(tmp_path)
    disp = srv._dispatcher
    try:
        aid = (await _blocked(srv)).data["approval"]["approval_id"]
        shot_task = asyncio.create_task(srv.call_tool(SHOT, {}))
        await disp.started.wait()
        nonce = _request_code(srv, aid)
        await asyncio.wait_for(srv._poll_handoff(), timeout=3)
        assert not any(_has_code(b) for b in srv.banners)
        ack = _ack(srv, nonce)
        assert ack is not None and ack["ok"] is False
        assert not srv.hub.code_displayed()
        disp.gate.set()
        shot = await shot_task
        assert not disp.overlapped and not _leaked(shot)
        assert srv.hub.approval_status(aid)["status"] == "pending"
    finally:
        disp.gate.set()
        srv.hub.close()


async def test_concurrent_capture_counter_returns_to_zero_on_error(tmp_path):
    """캡처가 예외로 끝나도 진행 중 수가 줄어든다 — 안 줄면 다음 표시가 영영 기다린다(가용성)."""
    srv = _server(tmp_path)

    class Boom(_Dispatcher):
        async def dispatch(self, action, params):
            raise RuntimeError("boom")

    srv._dispatcher = Boom()
    try:
        with pytest.raises(RuntimeError):
            await srv.call_tool(SHOT, {})
        assert srv._captures_inflight == 0
    finally:
        srv.hub.close()


# ======================================================================= 2. VR 뮤턴트 4종


def test_vr_jobs_queued_code_job_counts_as_displayed(root: Path):
    """VR-jobs: 서버가 아직 꺼내지 않은 code_job 도 '표시 중'이다(표시 직전 창). 코드 수명이
    지나도 job 이 남아 있는 동안은 True, 꺼낼 때 죽은 job 은 버리고 실패 ack."""
    now = [1000.0]
    h = HandoffHub(root, browser_mode="human", clock=lambda: now[0])
    h.open()
    try:
        ap = h.issue_approval(_components(), {"target": "결제하기"})
        shown = handoff.read_pending_approval(h.root, h.server_id, ap.approval_id)
        nonce = write_command(h.root, h.server_id, "show_code", approval_id=ap.approval_id,
                              action_digest=shown["action_digest"])
        h.poll()
        now[0] += handoff.CODE_TTL_S + 1
        assert h.code_displayed()  # 대기 중 job — 서버가 곧 띄운다
        assert h.take_code_job() is None  # 죽은 코드는 띄우지 않는다
        assert not h.code_displayed()
        ack = json.loads((h.dir / f"ack-{nonce}.json").read_text())
        assert ack["ok"] is False
    finally:
        h.close()


def test_queued_job_of_finished_approval_is_dropped(hub: HandoffHub):
    """job 이 대기 중일 때 증표가 끝났으면(거절) 그 코드는 창에 띄우지 않는다."""
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    shown = handoff.read_pending_approval(hub.root, hub.server_id, ap.approval_id)
    nonce = write_command(hub.root, hub.server_id, "show_code", approval_id=ap.approval_id,
                          action_digest=shown["action_digest"])
    hub.poll()
    hub._apply({"op": "deny", "approval_id": ap.approval_id,
                "action_digest": shown["action_digest"]})
    assert hub.take_code_job() is None
    assert not hub.code_displayed()
    assert json.loads((hub.dir / f"ack-{nonce}.json").read_text())["ok"] is False


def test_vr_other_new_code_withdraws_previous_approvals_code(hub: HandoffHub):
    """VR-other: B 의 코드를 띄우면 A 의 코드는 즉시 무효 — 창에는 하나만."""
    a = hub.issue_approval(_components(), {"target": "결제하기"})
    b = hub.issue_approval(_components(snapshot_epoch=7), {"target": "삭제"})
    code_a = hub_show_code(hub, a.approval_id)
    hub_show_code(hub, b.approval_id)
    assert hub.approvals[a.approval_id].code_mac is None
    shown = handoff.read_pending_approval(hub.root, hub.server_id, a.approval_id)
    nonce = write_command(hub.root, hub.server_id, "approve", approval_id=a.approval_id,
                          action_digest=shown["action_digest"], code=code_a)
    hub.poll()
    assert json.loads((hub.dir / f"ack-{nonce}.json").read_text())["ok"] is False
    assert hub.approval_status(a.approval_id)["status"] == "pending"


def test_vr_reuse_correct_code_is_discarded_immediately(hub: HandoffHub):
    """VR-reuse: 맞은 코드는 그 자리에서 폐기 — 서버 메모리에 HMAC 도 남지 않는다."""
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    code = hub_show_code(hub, ap.approval_id)
    ok, _ = hub._check_code(ap, code)
    assert ok
    assert ap.code_mac is None  # 상태 전이 전에 이미 지워졌다
    ok2, _ = hub._check_code(ap, code)
    assert not ok2


async def test_vr_banner_san_overlay_target_and_action_sanitized(tmp_path):
    """VR-banner-san: 창 오버레이의 대상 이름도 살균(제어·양방향·개행) + 액션 종류 표시(항목 3)."""
    srv = _server(tmp_path)
    try:
        evil = "결제하기\x1b[2K\r\n\u202e기하제결\u2028x"
        ap = srv.hub.issue_approval(_components(gate_basis={"name": evil}), {"target": evil})
        _request_code(srv, ap.approval_id)
        await srv._poll_handoff()
        text = srv.banners[-1]
        assert _has_code(text)
        for ch in ("\x1b", "\r", "\n", "\u202e", "\u2028"):
            assert ch not in text
        assert "결제하기" in text
        assert "click" in text  # 액션 종류 — 사람이 터미널과 창을 대조
    finally:
        srv.hub.close()


# ======================================================================= 3. 승인 화면 = 서버 메모리


def _poller(hub: HandoffHub):
    stop = threading.Event()
    codes: List[str] = []

    def loop():
        while not stop.is_set():
            hub.poll()
            job = hub.take_code_job()
            if job is not None:
                codes.append(job.code)
                hub.code_shown(job.nonce, True)
            stop.wait(0.02)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return stop, t, codes


def _forge(hub: HandoffHub, aid: str, **changes: Any) -> None:
    path = hub.dir / "approvals" / f"{aid}.json"
    data = json.loads(path.read_text())
    for k, v in changes.items():
        if k == "target":
            data["summary"]["target"] = v
        elif k == "gate_name":
            data["components"]["gate_basis"]["name"] = v
        else:
            data[k] = v
    handoff._write_private(path, data)


def test_cli_shows_server_memory_not_file(hub: HandoffHub, root: Path, monkeypatch, capsys):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
    stop, t, codes = _poller(hub)
    try:
        ap = hub.issue_approval(_components(), {"target": "결제하기", "reason": "고위험 '결제'"})
        assert cli.main(["approve", ap.approval_id]) == 2  # 코드 띄우고 --code 안내
        out = capsys.readouterr().out
        assert "결제하기" in out and "서버 기록" in out
        assert codes
    finally:
        stop.set()
        t.join()


@pytest.mark.parametrize("field,value", [
    ("target", "장바구니 보기(무해)"),
    ("gate_name", "장바구니 보기(무해)"),
])
def test_cli_refuses_when_file_differs_from_server(hub: HandoffHub, root: Path, monkeypatch,
                                                   capsys, field, value):
    """probe_misc 파일위조: 같은 uid 가 approvals 파일을 고쳐도 사람 화면은 서버 기록을 보이고,
    불일치를 경고하며 승인 절차(코드 표시)를 시작하지 않는다."""
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
    stop, t, codes = _poller(hub)
    try:
        ap = hub.issue_approval(_components(), {"target": "결제하기"})
        _forge(hub, ap.approval_id, **{field: value})
        rc = cli.main(["approve", ap.approval_id])
        cap = capsys.readouterr()
        assert rc == 2
        assert "장바구니" not in cap.out  # 위조 내용은 사람 화면에 나오지 않는다
        assert "결제하기" in cap.out
        assert "불일치" in cap.err or "불일치" in cap.out
        assert codes == []  # 코드 표시를 요청하지 않았다
        assert hub.approval_status(ap.approval_id)["status"] == "pending"
    finally:
        stop.set()
        t.join()


def test_cli_refuses_without_server_view(hub: HandoffHub, root: Path, monkeypatch, capsys):
    """서버가 내용을 주지 않으면(응답 없음) 파일 내용으로 대신 보여 주지 않는다."""
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    monkeypatch.setattr(handoff, "DESCRIBE_TIMEOUT_S", 0.3, raising=False)
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    real_wait = handoff.wait_ack
    monkeypatch.setattr(handoff, "wait_ack",
                        lambda r, s, n, timeout_s=5.0: real_wait(r, s, n, timeout_s=min(timeout_s, 0.3)))
    rc = cli.main(["approve", ap.approval_id])
    cap = capsys.readouterr()
    assert rc == 2
    assert "결제하기" not in cap.out


def test_cli_rejects_tampered_describe_mac(hub: HandoffHub, root: Path, monkeypatch, capsys):
    """서버 응답(ack)의 내용이 HMAC 과 맞지 않으면 거부(요청마다 새 challenge 에 묶임)."""
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    real_ack = hub._ack

    def tamper(nonce: str, ok: bool, message: str, **extra: Any) -> None:
        if "view" in extra:
            extra["view"] = dict(extra["view"], summary={"target": "장바구니 보기(무해)"})
        real_ack(nonce, ok, message, **extra)

    hub._ack = tamper
    stop, t, codes = _poller(hub)
    try:
        rc = cli.main(["approve", ap.approval_id])
        cap = capsys.readouterr()
        assert rc == 2 and codes == []
        assert "장바구니" not in cap.out
    finally:
        stop.set()
        t.join()


def test_describe_view_never_contains_code(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    code = hub_show_code(hub, ap.approval_id)
    nonce = write_command(hub.root, hub.server_id, "describe", approval_id=ap.approval_id,
                          challenge="ab" * 16)
    hub.poll()
    ack = (hub.dir / f"ack-{nonce}.json").read_text()
    assert json.loads(ack)["ok"] is True
    assert re.search(rf"(?<![0-9A-Za-z]){code}(?![0-9A-Za-z])", ack) is None


# ======================================================================= 실 Chromium — probe_race / probe_race_chain


HEAVY = 1500
RACE_PAGE = ("<!doctype html><meta charset=utf-8><title>결제 시연</title><body>"
             "<h1>상점 — 결제 시연</h1><p id=out>대기</p>"
             "<button onclick=\"out.textContent='결제됨'\">결제하기</button><div id=big></div>"
             "<script>var h='';for(var i=0;i<HEAVY;i++)h+='<a href=\"#l'+i+'\">항목 '+i+'</a> ';"
             "document.getElementById('big').innerHTML=h;</script></body>").replace("HEAVY", str(HEAVY))


@pytest.fixture
def race_site():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            d = RACE_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(d)))
            self.end_headers()
            self.wfile.write(d)

        def log_message(self, *a):  # noqa: ANN002
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{http.server_address[1]}/"
    finally:
        http.shutdown()
        http.server_close()


@requires_chromium
async def test_real_race_heavy_page_no_leak_and_chain_fails(race_site, tmp_path):
    """검증자 probe_race(HEAVY=1500, 무거운 SoM 캡처 연타 + 표시 요청) + probe_race_chain(새어 나간
    캡처에서 코드를 읽어 승인 → 실행)을 20회. 누출(코드가 창에 있던 동안 찍힌 캡처의 성공 반환) 0,
    체인 실행 0('결제됨' 없음)."""
    s = BrowserMCPServer(som_enabled=True, handoff_root=tmp_path / "servers")
    events: List[tuple] = []  # (시각, 'banner', 문구) — 사람의 눈
    async with s:
        s._human_can_see = lambda: True
        real_banner = s._set_banner

        async def spy(text):
            events.append((time.monotonic(), text))
            return await real_banner(text)

        s._set_banner = spy
        real_dispatch = s._dispatcher.dispatch
        windows: List[tuple] = []

        async def timed(action, params):
            t0 = time.monotonic()
            try:
                return await real_dispatch(action, params)
            finally:
                if action is ActionType.TAKE_SCREENSHOT:
                    windows.append((t0, time.monotonic()))

        s._dispatcher.dispatch = timed
        await s.call_tool("browser_navigate", {"url": race_site})
        o = await s.call_tool("browser_observe_page", {})
        ep = o.data["observation"]["snapshot_epoch"]
        eid = [e["element_id"] for e in o.data["observation"]["elements"] if e["name"] == "결제하기"][0]
        root, sid = s.hub.root, s.hub.server_id
        leaks, chains, successes = 0, 0, 0
        for i in range(20):
            r = await s.call_tool("browser_click", {"element_id": eid, "epoch": ep})
            aid = r.data["approval"]["approval_id"]
            digest = handoff.read_pending_approval(root, sid, aid)["action_digest"]
            results: List[tuple] = []

            async def spam(n: int = 6) -> None:
                for _ in range(n):
                    w0 = len(windows)
                    res = await s.call_tool(SHOT, {"annotate_som": True})
                    results.append((res, windows[w0:]))

            task = asyncio.create_task(spam())
            await asyncio.sleep(0.02 * (i % 10))
            write_command(root, sid, "show_code", approval_id=aid, action_digest=digest)
            await task
            for _ in range(60):
                if any(_has_code(t) for _, t in events):
                    break
                await asyncio.sleep(0.05)
            # 창에 코드가 떠 있던 구간
            on: List[tuple] = []
            start = None
            for ts, text in events:
                if _has_code(text) and start is None:
                    start = ts
                elif not _has_code(text) and start is not None:
                    on.append((start, ts))
                    start = None
            if start is not None:
                on.append((start, float("inf")))
            for res, wins in results:
                if not res.success:
                    continue
                successes += 1
                if any(a < e and b > st for a, b in wins for st, e in on):
                    leaks += 1
                    # 체인: 새어 나간 캡처의 코드(사람 눈 대신 기록)를 읽어 승인 → 실행 시도
                    code = code_from_banner([t for _, t in events])
                    write_command(root, sid, "approve", approval_id=aid, action_digest=digest, code=code)
                    await asyncio.sleep(0.3)
                    ok = await s.call_tool("browser_click", {"element_id": eid, "epoch": ep,
                                                              "approval_id": aid})
                    if ok.success:
                        chains += 1
                    break
            for _ in range(3):  # 정리: 틀린 코드 3회 → 폐기, 다음 반복은 새 증표
                write_command(root, sid, "approve", approval_id=aid, action_digest=digest, code="000000")
                await asyncio.sleep(0.15)
            for _ in range(40):
                await asyncio.sleep(0.05)
                if not s._pixels_blocked():
                    break
            events.clear()
        assert leaks == 0, f"{leaks}/20 누출"
        assert chains == 0
        assert await s._page.text_content("#out") == "대기"
        # 공허 방지: 캡처는 실제로 성공도 했다(전부 거부해서 0 을 만든 것이 아님)
        assert successes > 0
