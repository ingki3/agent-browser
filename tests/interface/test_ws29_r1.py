"""WS-29 R1: 표시 살균(BLOCKING-1) · 승인 뒤 대상 고정(NB-1) · 확인 코드 · headless 증표 끔 ·
digest 비노출(NB-7) · V1/V2 뮤턴트(NB-2) · 증표 정리(NB-5) · 사람이 닫은 탭(NB-3) · pid 재사용(NB-6).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from contracts import ActionType, ErrorCode, ExecutionMode
from interface import cli, handoff, mcp_server
from interface.handoff import HandoffHub, display_safe, write_command
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401
from test_ws29_mcp_handoff import _blocked, _server
from ws29_helpers import code_from_banner, hub_approve, hub_show_code, srv_approve

ESC_NAME = "결제하기\x1b[2K\r  대상     : 장바구니 보기(무해)"


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


@pytest.fixture
def srv(tmp_path: Path):
    s = _server(tmp_path)
    yield s
    s.hub.close()


def _leaks(code: str, text: Any) -> bool:
    """text 에 code 가 독립된 숫자열로 있는가(hex digest·id 안의 우연한 일치는 제외)."""
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(code)}(?![0-9A-Za-z])", str(text or "")) is not None


def _all_state_text(root: Path) -> str:
    out = []
    for p in Path(root).rglob("*"):
        if p.is_file():
            try:
                out.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return "\n".join(out)


# ======================================================================= 1. 표시 살균 (BLOCKING-1)


@pytest.mark.parametrize("bad", [
    "\x1b", "\r", "\n", "\x07", "\x00", "\x7f", "\x85", "\u2028", "\u2029",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
    "\u200b", "\ufeff", "\u00ad",
])
def test_display_safe_neutralizes_each_control(bad: str):
    out = display_safe(f"가{bad}나")
    assert bad not in out
    assert out.startswith("가") and out.endswith("나") and len(out) > 2  # 보이는 표기로 남는다


def test_display_safe_keeps_plain_text_and_caps_length():
    assert display_safe("결제하기 (Pay) 3,000원") == "결제하기 (Pay) 3,000원"
    long = display_safe("가" * 10_000, limit=50)
    assert len(long) <= 50 and long.endswith("…")
    assert display_safe(None) == "None"


def _approval_file(**over: Any) -> Dict[str, Any]:
    data = {
        "approval_id": "ap_x", "server_id": "1-a", "status": "pending",
        "expires_at": "2026-10-04T00:00:00+00:00",
        "summary": {"target": ESC_NAME, "reason": "고위험\x1b[31m 키워드\r", "domain": "x"},
        "components": {"action": "click", "params": {"element_id": "@e1", "text": "a\u2028b"},
                       "gate_basis": {"name": ESC_NAME}, "tab_id": "tab-1\x1b[A",
                       "origin": "http://a\u202e.test", "snapshot_epoch": 1},
    }
    data.update(over)
    return data


def test_show_approval_has_no_control_chars_and_no_forged_line():
    """검증자 probe_esc 재현: 이름의 ESC[2K + CR 이 '대상' 줄을 지우고 가짜 줄을 만들면 안 된다."""
    text = handoff._show_approval(_approval_file())
    lines = text.split("\n")
    for ch in ("\x1b", "\r", "\u2028", "\u202e", "\x07"):
        assert ch not in text, repr(ch)
    target_lines = [ln for ln in lines if ln.lstrip().startswith("대상")]
    assert len(target_lines) == 1, lines
    # 진짜 대상 이름이 같은 줄에 보이고, 가짜 문구는 그 줄 안의 무해한 꼬리로만 남는다.
    assert "결제하기" in target_lines[0]


def test_cli_control_status_text_is_sanitized(hub: HandoffHub, root: Path, monkeypatch, capsys):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    hub.request("캡차\x1b[2K\r조작권 반납됨 — 안전\u202e")
    assert cli.main(["control", "status"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out and "\u202e" not in out
    assert out.count("\n") == 1


def test_stderr_line_is_sanitized(capfd):
    mcp_server._stderr_line("agent-browser serve: [요청] 캡차\x1b[2K\r가짜\u2028줄\u202e")
    err = capfd.readouterr().err
    assert "\x1b" not in err and "\r" not in err and "\u2028" not in err and "\u202e" not in err
    assert err.count("\n") == 1


async def test_control_request_stderr_and_banner_sanitized(srv, capfd):
    out = await srv.call_server_tool("browser_control_request",
                                     {"reason": "캡차\x1b[2K\r조작권 반납됨 — 안전"})
    assert out["success"]
    err = capfd.readouterr().err
    assert "\x1b" not in err and "\r" not in err
    assert srv.banners and all("\x1b" not in (b or "") and "\r" not in (b or "")
                               for b in srv.banners)


# ======================================================================= 3. 확인 코드 (hub)


def _cmd_approve(hub: HandoffHub, aid: str, **fields: Any) -> Dict[str, Any]:
    shown = handoff.read_pending_approval(hub.root, hub.server_id, aid)
    nonce = write_command(hub.root, hub.server_id, "approve", approval_id=aid,
                          action_digest=shown["action_digest"], **fields)
    hub.poll()
    return json.loads((hub.dir / f"ack-{nonce}.json").read_text())


def test_approve_without_code_is_rejected(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    ack = _cmd_approve(hub, ap.approval_id)
    assert ack["ok"] is False and "코드" in ack["message"]
    assert hub.approval_status(ap.approval_id)["status"] == "pending"
    # 표시 전에 아무 코드나 넣어도 안 된다
    ack = _cmd_approve(hub, ap.approval_id, code="123456")
    assert ack["ok"] is False
    assert hub.approval_status(ap.approval_id)["status"] == "pending"


def test_correct_code_approves(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    code = hub_show_code(hub, ap.approval_id)
    assert re.fullmatch(r"\d{6}", code)
    ack = _cmd_approve(hub, ap.approval_id, code=code)
    assert ack["ok"] is True
    assert hub.approval_status(ap.approval_id)["status"] == "approved"
    assert hub.consume_approval(ap.approval_id, _components())[0]


def test_three_wrong_codes_revoke(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    code = hub_show_code(hub, ap.approval_id)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for i in range(3):
        ack = _cmd_approve(hub, ap.approval_id, code=wrong)
        assert ack["ok"] is False
    assert hub.approval_status(ap.approval_id)["status"] == "revoked"
    # 폐기 뒤에는 맞는 코드도 소용없다
    ack = _cmd_approve(hub, ap.approval_id, code=code)
    assert ack["ok"] is False
    ok, why = hub.consume_approval(ap.approval_id, _components())
    assert not ok and "폐기" in why
    assert not hub.code_displayed()


def test_code_show_limit_revokes(hub: HandoffHub):
    """코드 표시를 반복 요청해 화면 캡처를 계속 막는 것(서비스 방해) — 표시 상한을 넘으면 폐기."""
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    for _ in range(handoff.MAX_CODE_SHOWS):
        hub_show_code(hub, ap.approval_id)
    shown = handoff.read_pending_approval(hub.root, hub.server_id, ap.approval_id)
    nonce = write_command(hub.root, hub.server_id, "show_code", approval_id=ap.approval_id,
                          action_digest=shown["action_digest"])
    hub.poll()
    assert hub.take_code_job() is None
    ack = json.loads((hub.dir / f"ack-{nonce}.json").read_text())
    assert ack["ok"] is False
    assert hub.approval_status(ap.approval_id)["status"] == "revoked"
    assert not hub.code_displayed()


def test_new_code_replaces_old_and_code_expires(root: Path):
    now = [1000.0]
    h = HandoffHub(root, browser_mode="human", clock=lambda: now[0])
    h.open()
    try:
        ap = h.issue_approval(_components(), {"target": "결제하기"})
        first = hub_show_code(h, ap.approval_id)
        second = hub_show_code(h, ap.approval_id)
        if first != second:
            assert not _cmd_approve(h, ap.approval_id, code=first)["ok"]
        assert h.code_displayed()
        now[0] += handoff.CODE_TTL_S + 1
        assert not h.code_displayed()
        assert not _cmd_approve(h, ap.approval_id, code=second)["ok"]
        assert h.approval_status(ap.approval_id)["status"] == "pending"
    finally:
        h.close()


def test_display_failure_withdraws_code(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    shown = handoff.read_pending_approval(hub.root, hub.server_id, ap.approval_id)
    nonce = write_command(hub.root, hub.server_id, "show_code", approval_id=ap.approval_id,
                          action_digest=shown["action_digest"])
    hub.poll()
    job = hub.take_code_job()
    hub.code_shown(job.nonce, False)
    ack = json.loads((hub.dir / f"ack-{nonce}.json").read_text())
    assert ack["ok"] is False and "창" in ack["message"]
    assert not hub.code_displayed()
    assert not _cmd_approve(hub, ap.approval_id, code=job.code)["ok"]


def test_code_never_written_to_disk_or_kept_in_clear(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    code = hub_show_code(hub, ap.approval_id)
    # 표시 직후(입력 전) 디스크 전수 + 서버 메모리(평문 없음, 해시만)
    assert not _leaks(code, _all_state_text(hub.root))
    assert not _leaks(code, repr(vars(hub))) and not _leaks(code, repr(hub.approvals))
    ack = _cmd_approve(hub, ap.approval_id, code=code)
    assert ack["ok"]
    assert not _leaks(code, json.dumps(ack, ensure_ascii=False))
    assert not _leaks(code, _all_state_text(hub.root))
    assert not _leaks(code, repr(vars(hub)))


def test_codes_use_secrets(monkeypatch, hub: HandoffHub):
    calls: List[int] = []
    real = handoff.secrets.randbelow

    def spy(n: int) -> int:
        calls.append(n)
        return real(n)

    monkeypatch.setattr(handoff.secrets, "randbelow", spy)
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    hub_show_code(hub, ap.approval_id)
    assert 1_000_000 in calls


# ======================================================================= 3. 확인 코드 (MCP 서버)


async def test_code_shown_only_on_overlay_and_screenshot_refused_while_shown(srv, capfd):
    r = await _blocked(srv)
    aid = r.data["approval"]["approval_id"]
    shown = handoff.read_pending_approval(srv.hub.root, srv.hub.server_id, aid)
    write_command(srv.hub.root, srv.hub.server_id, "show_code", approval_id=aid,
                  action_digest=shown["action_digest"])
    await srv._poll_handoff()
    code = code_from_banner(srv.banners)
    # 코드가 떠 있는 동안 화면 픽셀 경로는 거부(일반·SoM 모두)
    for args in ({}, {"annotate_som": True}, {"full_page": True}):
        shot = await srv.call_tool(tool_name(ActionType.TAKE_SCREENSHOT), dict(args))
        assert not shot.success
        assert shot.data["blocked_by"] == "approval_code_displayed"
        assert "retry_after_s" in shot.data
    assert ActionType.TAKE_SCREENSHOT not in srv._dispatcher.calls
    # 다른 응답·상태 파일·stderr 어디에도 코드가 없다
    texts = [mcp_server.envelope_json(r)]
    for name, args in [("browser_control_status", {}),
                       ("browser_approval_wait", {"approval_id": aid, "timeout_s": 0})]:
        texts.append(json.dumps(await srv.call_server_tool(name, args), ensure_ascii=False))
    write_command(srv.hub.root, srv.hub.server_id, "approve", approval_id=aid,
                  action_digest=shown["action_digest"], code=code)
    await srv._poll_handoff()
    ok = await _blocked(srv, approval_id=aid)
    assert ok.success, ok
    texts.append(mcp_server.envelope_json(ok))
    texts.append(_all_state_text(srv.hub.root))
    texts.append(capfd.readouterr().err)
    for t in texts:
        assert not _leaks(code, t)
    # 입력 뒤 오버레이에서 코드가 내려가고 스크린샷이 다시 된다
    assert not _leaks(code, (srv.banners[-1] or ""))
    shot = await srv.call_tool(tool_name(ActionType.TAKE_SCREENSHOT), {})
    assert shot.success


async def test_screenshot_refused_when_overlay_clear_failed(srv):
    """fail-closed: 오버레이를 내리지 못했으면(상태 불명) 코드가 만료돼도 화면 캡처를 거부한다."""
    r = await _blocked(srv)
    aid = r.data["approval"]["approval_id"]
    shown = handoff.read_pending_approval(srv.hub.root, srv.hub.server_id, aid)
    write_command(srv.hub.root, srv.hub.server_id, "show_code", approval_id=aid,
                  action_digest=shown["action_digest"])
    await srv._poll_handoff()

    async def broken(text):  # 지우기 실패
        return False

    srv._set_banner = broken
    srv.hub.approvals[aid].code_expires = 0  # 코드 만료
    await srv._poll_handoff()
    shot = await srv.call_tool(tool_name(ActionType.TAKE_SCREENSHOT), {})
    assert not shot.success and shot.data["blocked_by"] == "approval_code_displayed"


async def test_block_response_has_no_action_digest(srv):
    """NB-7: digest 는 사람 CLI 가 파일에서 읽는다 — 에이전트 응답에 싣지 않는다."""
    r = await _blocked(srv)
    assert "action_digest" not in r.data["approval"]
    assert r.data["approval"]["approval_id"]
    ap = srv.hub.approvals[r.data["approval"]["approval_id"]]
    assert ap.digest not in mcp_server.envelope_json(r)


async def test_headless_server_issues_no_approval(tmp_path):
    s = _server(tmp_path, visible=False)
    try:
        r = await _blocked(s)
        assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
        assert "approval" not in r.data
        assert "agent-browser approve" not in r.error_message
        assert "--pre-approve" in r.error_message and "--mode interactive" in r.error_message
        assert s.hub.approvals == {}
        # 들고 있는 아무 id 도 소용없다
        again = await _blocked(s, approval_id="ap_anything")
        assert not again.success and s._dispatcher.calls == []
    finally:
        s.hub.close()


async def test_headless_interactive_issues_no_approval(tmp_path):
    s = _server(tmp_path, visible=False, mode=ExecutionMode.INTERACTIVE)
    try:
        r = await _blocked(s)
        assert r.data["requires_confirmation"] is True
        assert "approval" not in r.data and s.hub.approvals == {}
    finally:
        s.hub.close()


def test_human_can_see_follows_browser_mode():
    assert BrowserMCPServer(browser_mode="headless")._human_can_see() is False
    assert BrowserMCPServer(browser_mode="human")._human_can_see() is True


# ======================================================================= 3. CLI


def _poller(hub: HandoffHub):
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            hub.poll()
            job = hub.take_code_job()
            if job is not None:
                shown_codes.append(job.code)
                hub.code_shown(job.nonce, True)
            stop.wait(0.02)

    shown_codes: List[str] = []
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return stop, t, shown_codes


def test_cli_approve_requires_code(hub: HandoffHub, root: Path, monkeypatch, capsys):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    stop, t, codes = _poller(hub)
    try:
        ap = hub.issue_approval(_components(), {"target": "결제하기"})
        aid = ap.approval_id
        monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
        # --yes 만으로는 승인 못 한다(코드 필수)
        assert cli.main(["approve", aid, "--yes"]) == 2
        assert hub.approval_status(aid)["status"] == "pending"
        # 코드 없이: 창에 코드를 띄우고 안내만, 승인 안 됨
        assert cli.main(["approve", aid]) == 2
        out = capsys.readouterr()
        assert "--code" in out.out + out.err
        assert codes and hub.approval_status(aid)["status"] == "pending"
        assert not _leaks(codes[-1], out.out + out.err)  # CLI 출력에도 코드가 없다
        # 틀린 코드 → 거부
        wrong = f"{(int(codes[-1]) + 1) % 1_000_000:06d}"
        assert cli.main(["approve", aid, "--code", wrong]) == 1
        assert hub.approval_status(aid)["status"] == "pending"
        # 맞는 코드 → 승인
        assert cli.main(["approve", aid, "--code", codes[-1]]) == 0
        assert hub.approval_status(aid)["status"] == "approved"
    finally:
        stop.set()
        t.join()


def test_cli_approve_prompt_reads_code_on_tty(hub: HandoffHub, root: Path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    stop, t, codes = _poller(hub)
    try:
        ap = hub.issue_approval(_components(), {"target": "결제하기"})
        monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: codes[-1])
        assert cli.main(["approve", ap.approval_id]) == 0
        assert hub.approval_status(ap.approval_id)["status"] == "approved"
        # 빈 입력 = 취소(승인 아님, 거절로도 기록하지 않음)
        ap2 = hub.issue_approval(_components(snapshot_epoch=5), {"target": "결제하기"})
        monkeypatch.setattr("builtins.input", lambda *_: "")
        assert cli.main(["approve", ap2.approval_id]) == 1
        assert hub.approval_status(ap2.approval_id)["status"] == "pending"
    finally:
        stop.set()
        t.join()


def test_cli_deny_needs_no_code(hub: HandoffHub, root: Path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    stop, t, _ = _poller(hub)
    try:
        ap = hub.issue_approval(_components(), {"target": "결제하기"})
        assert cli.main(["approve", ap.approval_id, "--deny"]) == 1
        assert hub.approval_status(ap.approval_id)["status"] == "denied"
    finally:
        stop.set()
        t.join()


# ======================================================================= 2·5. 대상 고정(NB-1) · V1/V2 (NB-2)


async def test_human_holder_blocks_even_with_approved_token(srv):
    """V1: approval_id 가 있어도 조작권 검사를 건너뛰면 안 된다."""
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    await srv_approve(srv, aid)
    write_command(srv.hub.root, srv.hub.server_id, "take")
    r = await _blocked(srv, approval_id=aid)
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED and "control" in r.data
    assert srv._dispatcher.calls == []
    assert srv.hub.approval_status(aid)["status"] == "approved"  # 소모되지 않음


async def test_mcp_digest_binds_target_name(srv):
    """V2: 같은 element_id·epoch 인데 대상 이름이 바뀌면(gate_basis) 거부 — MCP 층 digest 가 이름을 묶는다."""
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    await srv_approve(srv, aid)
    srv._engine.get_handle = lambda eid: type("H", (), {"name": "구독 해지하기"})()
    r = await _blocked(srv, approval_id=aid)
    assert not r.success
    assert r.data["approval"]["rejected"]["reason_code"] == "target_changed"
    assert srv._dispatcher.calls == []


async def test_approved_call_refuses_when_target_not_fresh(srv):
    """NB-1: 승인 증표 호출은 대상이 승인 때와 같은지(디스패처의 재확인) 통과해야만 실행."""
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    await srv_approve(srv, aid)

    async def stale(params):
        return {"fresh": False, "detail": "name '결제하기' -> '결제 하기'"}

    srv._dispatcher.approval_target_check = stale
    r = await _blocked(srv, approval_id=aid)
    assert not r.success
    assert r.data["approval"]["reason"] == "target_changed"
    assert srv._dispatcher.calls == []
    # 증표는 그대로(승인됨) — 같은 대상이 돌아오면 쓸 수 있다
    assert srv.hub.approval_status(aid)["status"] == "approved"


async def test_approved_call_disables_healing(srv):
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    await srv_approve(srv, aid)
    seen: List[Any] = []

    async def fresh(params):
        return {"fresh": True}

    srv._dispatcher.approval_target_check = fresh
    real = srv._dispatcher.dispatch

    async def spy(action, params):
        seen.append(getattr(srv._dispatcher, "heal_disabled", None))
        return await real(action, params)

    srv._dispatcher.dispatch = spy
    r = await _blocked(srv, approval_id=aid)
    assert r.success, r
    assert seen == [True]
    assert getattr(srv._dispatcher, "heal_disabled", False) is False  # 호출 뒤 원복


# ======================================================================= 6. 증표 정리 (NB-5)


def test_finished_approvals_are_purged_after_retention(root: Path):
    now = [1000.0]
    h = HandoffHub(root, browser_mode="human", approval_ttl_s=60, clock=lambda: now[0])
    h.open()
    try:
        used = h.issue_approval(_components(snapshot_epoch=1), {})
        hub_approve(h, used.approval_id)
        assert h.consume_approval(used.approval_id, _components(snapshot_epoch=1))[0]
        denied = h.issue_approval(_components(snapshot_epoch=2), {})
        shown = handoff.read_pending_approval(root, h.server_id, denied.approval_id)
        write_command(root, h.server_id, "deny", approval_id=denied.approval_id,
                      action_digest=shown["action_digest"])
        h.poll()
        expired = h.issue_approval(_components(snapshot_epoch=3), {})
        now[0] += 61
        h.poll()
        assert h.approval_status(expired.approval_id)["status"] == "expired"
        ids = [used.approval_id, denied.approval_id, expired.approval_id]
        assert all((h.dir / "approvals" / f"{i}.json").exists() for i in ids)
        now[0] += handoff.APPROVAL_RETAIN_S + 1
        h.poll()
        for i in ids:
            assert i not in h.approvals
            assert not (h.dir / "approvals" / f"{i}.json").exists()
    finally:
        h.close()


def test_approvals_are_capped(hub: HandoffHub):
    for i in range(handoff.MAX_APPROVALS + 50):
        hub.issue_approval(_components(snapshot_epoch=i), {})
    assert len(hub.approvals) <= handoff.MAX_APPROVALS
    assert len(list((hub.dir / "approvals").iterdir())) <= handoff.MAX_APPROVALS


# ======================================================================= 8. pid 재사용 (NB-6)


def test_server_json_records_uid_and_start(hub: HandoffHub):
    info = json.loads((hub.dir / "server.json").read_text())
    assert info["uid"] == os.geteuid()
    assert isinstance(info["started"], (int, float))


def test_pid_reused_by_other_user_counts_as_dead(root: Path, monkeypatch):
    root.mkdir(mode=0o700, parents=True)
    stale = root / "4242-dead"
    stale.mkdir(mode=0o700)
    handoff._write_private(stale / "server.json", {
        "server_id": "4242-dead", "pid": 4242, "uid": os.geteuid(), "started": 1.0})
    real_kill = os.kill

    def fake_kill(pid, sig):
        if pid == 4242:
            raise PermissionError(1, "Operation not permitted")
        return real_kill(pid, sig)

    monkeypatch.setattr(handoff.os, "kill", fake_kill)
    assert handoff.list_servers(root) == []
    h = HandoffHub(root)
    h.open()
    try:
        assert not stale.exists()
    finally:
        h.close()


# ======================================================================= 실 Chromium


PAGE = """<!doctype html><meta charset=utf-8><p id=out>대기</p>
<button id=b1 onclick="out.textContent='결제됨(승인한 버튼)'">결제하기</button>
<script>function swap(n){var b=document.getElementById('b1');var x=document.createElement('button');
x.textContent=n;x.onclick=function(){out.textContent='눌림: '+n};b.replaceWith(x);}</script>"""
ESC_PAGE = ("<!doctype html><meta charset=utf-8><p id=out>대기</p>"
            "<button aria-label=\"결제하기&#27;[2K&#13;  대상     : 장바구니 보기(무해)\" "
            "onclick=\"out.textContent='결제됨'\">x</button>")


@pytest.fixture
def site():
    pages = {"/": PAGE, "/esc": ESC_PAGE}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            d = pages.get(self.path.split("?")[0], PAGE).encode()
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
        yield f"http://127.0.0.1:{http.server_address[1]}"
    finally:
        http.shutdown()
        http.server_close()


def _visible(s: BrowserMCPServer) -> List[Any]:
    """headless 로 돌리되 사람이 볼 창이 있다고 두고, 창 오버레이 문구를 가로챈다(사람의 눈)."""
    s._human_can_see = lambda: True
    seen: List[Any] = []
    real = s._set_banner

    async def spy(text):
        seen.append(text)
        return await real(text)

    s._set_banner = spy
    s.banners = seen
    return seen


async def _human(*argv: str) -> int:
    return await asyncio.to_thread(cli.main, list(argv))


@requires_chromium
@pytest.mark.parametrize("new_name", ["결제하기", "결제 하기"])
async def test_real_heal_after_approval_is_refused(site, new_name, monkeypatch):
    """검증자 probe_heal 재현: 승인 뒤 같은/유사 이름의 다른 버튼으로 바뀌면 누르지 않는다."""
    monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
    async with BrowserMCPServer(handoff_root=Path(handoff.state_root())) as s:
        banners = _visible(s)
        await s.call_tool("browser_navigate", {"url": site + "/"})
        o = await s.call_tool("browser_observe_page", {})
        ep = o.data["observation"]["snapshot_epoch"]
        eid = [e["element_id"] for e in o.data["observation"]["elements"] if e["name"] == "결제하기"][0]
        r1 = await s.call_tool("browser_click", {"element_id": eid, "epoch": ep})
        aid = r1.data["approval"]["approval_id"]
        assert await _human("approve", aid) == 2  # 코드 띄우기
        assert await _human("approve", aid, "--code", code_from_banner(banners)) == 0
        await s._page.evaluate(f"swap({new_name!r})")
        r2 = await s.call_tool("browser_click", {"element_id": eid, "epoch": ep, "approval_id": aid})
        assert not r2.success and r2.healed is False
        assert r2.data["approval"]["reason"] == "target_changed"
        assert await s._page.text_content("#out") == "대기"


@requires_chromium
async def test_real_esc_name_render_and_code_flow(site, monkeypatch, capfd, caplog):
    """probe_esc 재현(렌더 결과에 가짜 '대상' 줄 없음) + 확인 코드 전체 흐름 + 코드 비노출 전수 검사
    (MCP 응답·stderr·stdout·상태 파일·로그)."""
    import logging

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
    async with BrowserMCPServer(handoff_root=Path(handoff.state_root())) as s:
        banners = _visible(s)
        await s.call_tool("browser_navigate", {"url": site + "/esc"})
        o = await s.call_tool("browser_observe_page", {})
        el = o.data["observation"]["elements"][0]
        r = await s.call_tool("browser_click", {"element_id": el["element_id"],
                                                "epoch": o.data["observation"]["snapshot_epoch"]})
        aid = r.data["approval"]["approval_id"]
        shown = handoff.read_pending_approval(Path(handoff.state_root()), s.hub.server_id, aid)
        text = handoff._show_approval(shown)
        assert "\x1b" not in text and "\r" not in text
        assert len([ln for ln in text.split("\n") if ln.lstrip().startswith("대상")]) == 1
        capfd.readouterr()
        assert await _human("approve", aid) == 2
        code = code_from_banner(banners)
        responses = []
        for name, args in [("browser_observe_page", {}), ("browser_extract", {"selector": "body"}),
                           ("browser_take_screenshot", {})]:
            res = await s.call_tool(name, args)
            responses.append(mcp_server.envelope_json(res))
        assert json.loads(responses[2])["error_code"] == "E_SCREENSHOT_FAILED"
        assert await _human("approve", aid, "--code", code) == 0
        ok = await s.call_tool("browser_click", {"element_id": el["element_id"],
                                                 "epoch": o.data["observation"]["snapshot_epoch"],
                                                 "approval_id": aid})
        assert ok.success, ok
        responses.append(mcp_server.envelope_json(ok))
        shot = await s.call_tool("browser_take_screenshot", {})
        assert shot.success
        cap = capfd.readouterr()
        for t in responses + [cap.out, cap.err, _all_state_text(Path(handoff.state_root())),
                              caplog.text]:
            assert not _leaks(code, t)


@requires_chromium
async def test_real_human_closes_agent_tab_then_release(site):
    """NB-3: 사람이 에이전트 활성 탭을 닫고 release → 남은 탭으로 바꾸고 알린다(PAGE_CRASHED 아님)."""
    async with BrowserMCPServer(handoff_root=Path(handoff.state_root())) as s:
        _visible(s)
        await s.call_tool("browser_navigate", {"url": site + "/"})
        first = s._core.active_tab_id
        await s.call_server_tool("browser_control_request", {"reason": "확인"})
        assert await _human("control", "take") == 0
        # 사람이 창에서 새 탭을 열고 에이전트 탭을 닫는다
        ctx = s._core.context_for("mcp-session")
        other = await ctx.new_page()
        await other.goto(site + "/esc")
        await s._page.close()
        waiter = asyncio.ensure_future(s.call_server_tool("browser_control_wait", {"timeout_s": 20}))
        await asyncio.sleep(0.2)
        assert await _human("control", "release") == 0
        got = await waiter
        notice = got["data"]["tab_closed_by_human"]
        assert notice["closed_tab_id"] == first and notice["active_tab_id"] != first
        assert "tab_control" in notice["hint"]
        obs = await s.call_tool("browser_observe_page", {})
        assert obs.success, obs
        assert obs.error_code is None and "/esc" in obs.current_url


# ======================================================================= 뮤테이션 보강 (R1)


def test_expired_code_rejected_without_status_probe(root: Path):
    """코드 만료 검사 자체(_code_live)가 승인 경로에서 돈다 — 다른 조회가 먼저 지워 주지 않아도."""
    now = [1000.0]
    h = HandoffHub(root, browser_mode="human", clock=lambda: now[0])
    h.open()
    try:
        ap = h.issue_approval(_components(), {"target": "결제하기"})
        code = hub_show_code(h, ap.approval_id)
        now[0] += handoff.CODE_TTL_S + 1
        assert not _cmd_approve(h, ap.approval_id, code=code)["ok"]
        assert h.approval_status(ap.approval_id)["status"] == "pending"
    finally:
        h.close()


def test_code_never_in_any_written_file(hub: HandoffHub, monkeypatch):
    """서버의 모든 파일 쓰기(_write_private: ack·control·approvals·server.json)를 가로채 코드가 없는지
    본다. 사람 CLI 가 쓰는 cmd-*.json(사람이 입력한 코드를 서버로 보내는 통로, 서버가 읽자마자 삭제)은
    제외한다."""
    written: List[str] = []
    real = handoff._write_private

    def spy(path, data):
        if not Path(path).name.startswith("cmd-"):
            written.append(json.dumps(data, ensure_ascii=False))
        return real(path, data)

    monkeypatch.setattr(handoff, "_write_private", spy)
    ap = hub.issue_approval(_components(), {"target": "결제하기"})
    code = hub_show_code(hub, ap.approval_id)
    _cmd_approve(hub, ap.approval_id, code=f"{(int(code) + 1) % 1_000_000:06d}")
    _cmd_approve(hub, ap.approval_id, code=code)
    assert written and not any(_leaks(code, w) for w in written)


async def test_approval_target_check_missing_is_fail_closed(srv):
    """대상 재확인 수단이 없으면(확인 불가) 실행하지 않는다."""
    aid = (await _blocked(srv)).data["approval"]["approval_id"]
    await srv_approve(srv, aid)
    srv._dispatcher.approval_target_check = None
    r = await _blocked(srv, approval_id=aid)
    assert not r.success and r.data["approval"]["reason"] == "target_changed"
    assert srv._dispatcher.calls == []


@requires_chromium
async def test_dispatcher_heal_disabled_never_substitutes(site):
    """디스패처 층(방어 겹): heal_disabled 면 이름이 바뀐 대상을 비슷한 요소로 치유하지 않는다."""
    async with BrowserMCPServer(handoff_root=Path(handoff.state_root())) as s:
        await s.call_tool("browser_navigate", {"url": site + "/"})
        o = await s.call_tool("browser_observe_page", {})
        ep = o.data["observation"]["snapshot_epoch"]
        eid = [e["element_id"] for e in o.data["observation"]["elements"] if e["name"] == "결제하기"][0]
        await s._page.evaluate("swap('결제 하기')")
        s._dispatcher.heal_disabled = True
        try:
            r = await s._dispatcher.dispatch(ActionType.CLICK, {"element_id": eid, "epoch": ep})
        finally:
            s._dispatcher.heal_disabled = False
        assert not r.success and r.healed is False and r.data.get("heal_disabled") is True
        assert await s._page.text_content("#out") == "대기"
        # 대조: 끄지 않으면 치유가 일어난다(검증자 probe_heal 의 원래 동작)
        r2 = await s._dispatcher.dispatch(ActionType.CLICK, {"element_id": eid, "epoch": ep})
        assert r2.healed is True
