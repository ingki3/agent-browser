"""WS-29: 사람 인계 통로(조작권) + 승인 증표 — 서버 쪽 상태기계·신호 통로 단위/보안 테스트.

브라우저 없음. 상태 디렉터리는 tmp_path 아래(실제 ~/.agent-browser 를 건드리지 않는다).
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from contracts import ActionType
from interface import handoff
from interface.handoff import HandoffHub, action_digest, write_command


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "servers"


@pytest.fixture
def hub(root: Path):
    h = HandoffHub(root, browser_mode="human")
    h.open()
    yield h
    h.close()


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


def _approve_via_cli_file(hub: HandoffHub, approval_id: str) -> None:
    """사람(CLI)이 승인 파일을 보고 그 digest 로 승인 명령을 쓴다 — CLI 와 같은 함수."""
    shown = handoff.read_pending_approval(hub.root, hub.server_id, approval_id)
    write_command(hub.root, hub.server_id, "approve",
                  approval_id=approval_id, action_digest=shown["action_digest"])
    hub.poll()


# ------------------------------------------------------------------ 상태 디렉터리


def test_state_dirs_and_files_are_private(hub: HandoffHub, root: Path):
    assert _mode(root) == 0o700
    assert _mode(hub.dir) == 0o700
    for name in ("server.json", "control.json"):
        assert _mode(hub.dir / name) == 0o600, name
    ap = hub.issue_approval(_components(), {"action": "click"})
    f = hub.dir / "approvals" / f"{ap.approval_id}.json"
    assert _mode(f.parent) == 0o700 and _mode(f) == 0o600


def test_close_removes_server_dir(root: Path):
    h = HandoffHub(root)
    h.open()
    d = h.dir
    assert d.is_dir()
    h.close()
    assert not d.exists()


def test_stale_server_dirs_are_cleaned_on_open(root: Path):
    root.mkdir(mode=0o700, parents=True)
    stale = root / "999999-dead"
    stale.mkdir(mode=0o700)
    handoff._write_private(stale / "server.json", {"server_id": "999999-dead", "pid": 2**22 + 12345})
    h = HandoffHub(root)
    h.open()
    try:
        assert not stale.exists()
        assert [s["server_id"] for s in handoff.list_servers(root)] == [h.server_id]
    finally:
        h.close()


def test_server_id_resolution_single_vs_many(root: Path):
    a = HandoffHub(root)
    a.open()
    try:
        info, err = handoff.resolve_server(root, None)
        assert err is None and info["server_id"] == a.server_id
        b = HandoffHub(root)
        b.open()
        try:
            info, err = handoff.resolve_server(root, None)
            assert info is None and a.server_id in err and b.server_id in err
            info, err = handoff.resolve_server(root, b.server_id)
            assert err is None and info["server_id"] == b.server_id
        finally:
            b.close()
    finally:
        a.close()
    info, err = handoff.resolve_server(root, None)
    assert info is None and err


# ------------------------------------------------------------------ 조작권 상태기계


def test_initial_status(hub: HandoffHub):
    st = hub.status()
    assert st["holder"] == "agent" and st["requested"] is False and st["request_id"] is None


def test_request_take_release_cycle(hub: HandoffHub):
    req = hub.request("캡차", secret_wanted=False)
    assert req["holder"] == "agent" and req["request_id"]
    assert hub.status()["requested"] is True and hub.status()["reason"] == "캡차"
    # 파일에도 반영(CLI status 가 읽는다)
    on_disk = json.loads((hub.dir / "control.json").read_text())
    assert on_disk["requested"] is True

    write_command(hub.root, hub.server_id, "take")
    assert hub.poll() == ["taken"]
    st = hub.status()
    assert st["holder"] == "human" and st["requested"] is False

    write_command(hub.root, hub.server_id, "release")
    assert hub.poll() == ["released"]
    st = hub.status()
    assert st["holder"] == "agent" and st["secret_wanted"] is False


MANIPULATION = [
    ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.NAVIGATE, ActionType.PRESS_KEY,
    ActionType.SELECT_OPTION, ActionType.CHECK_BOX, ActionType.SCROLL, ActionType.HOVER,
    ActionType.UPLOAD_FILE, ActionType.DOWNLOAD_FILE, ActionType.HANDLE_DIALOG,
    ActionType.SWITCH_FRAME, ActionType.RELOAD, ActionType.GO_BACK,
]
OBSERVATION = [ActionType.OBSERVE_PAGE, ActionType.TAKE_SCREENSHOT, ActionType.EXTRACT,
               ActionType.WAIT_FOR]


def test_classification_covers_all_19():
    covered = set(MANIPULATION) | set(OBSERVATION) | {ActionType.TAB_CONTROL}
    assert covered == set(ActionType)


@pytest.mark.parametrize("action", MANIPULATION)
def test_human_holder_blocks_manipulation(hub: HandoffHub, action: ActionType):
    assert hub.control_blocks(action, {}) is None
    write_command(hub.root, hub.server_id, "take")
    hub.poll()
    blocked = hub.control_blocks(action, {})
    assert blocked is not None and blocked["holder"] == "human"
    assert blocked["how_to_wait"] == "browser_control_wait"


@pytest.mark.parametrize("command", ["create", "switch", "close"])
def test_human_holder_blocks_tab_changes(hub: HandoffHub, command: str):
    write_command(hub.root, hub.server_id, "take")
    hub.poll()
    assert hub.control_blocks(ActionType.TAB_CONTROL, {"command": command}) is not None


@pytest.mark.parametrize("action,args", [(a, {}) for a in OBSERVATION]
                         + [(ActionType.TAB_CONTROL, {"command": "list"})])
def test_human_holder_allows_observation(hub: HandoffHub, action, args):
    write_command(hub.root, hub.server_id, "take")
    hub.poll()
    assert hub.control_blocks(action, args) is None


@pytest.mark.parametrize("action", list(ActionType))
def test_secret_wanted_blocks_everything(hub: HandoffHub, action: ActionType):
    hub.request("비밀번호 입력", secret_wanted=True)
    args = {"command": "list"} if action is ActionType.TAB_CONTROL else {}
    # 요청 직후(사람이 아직 take 전)부터 막는다 — 사람이 take 없이 바로 입력할 수 있다.
    blocked = hub.control_blocks(action, args)
    assert blocked is not None and blocked["secret_wanted"] is True
    write_command(hub.root, hub.server_id, "take")
    hub.poll()
    assert hub.control_blocks(action, args) is not None
    write_command(hub.root, hub.server_id, "release")
    hub.poll()
    assert hub.control_blocks(action, args) is None


# ------------------------------------------------------------------ 명령 파일 위조


def _forge(hub: HandoffHub, body: Dict[str, Any], *, mode: int = 0o600, name: str = "") -> Path:
    nonce = body.get("nonce") or "forged1"
    body.setdefault("nonce", nonce)
    p = hub.dir / (name or f"cmd-{nonce}.json")
    p.write_text(json.dumps(body))
    os.chmod(p, mode)
    return p


def test_command_with_loose_permissions_rejected(hub: HandoffHub):
    _forge(hub, {"op": "take", "server_id": hub.server_id}, mode=0o644)
    assert hub.poll() == []
    assert hub.status()["holder"] == "agent"
    assert hub.rejected and "권한" in hub.rejected[-1][1]


def test_command_for_other_server_rejected(hub: HandoffHub):
    _forge(hub, {"op": "take", "server_id": "1-other"})
    assert hub.poll() == []
    assert hub.status()["holder"] == "agent"
    assert "server_id" in hub.rejected[-1][1]


def test_command_owned_by_other_user_ignored(hub: HandoffHub):
    write_command(hub.root, hub.server_id, "take")
    hub._uid = os.geteuid() + 1  # 파일 소유자(나) ≠ 서버가 믿는 uid → 다른 사용자 파일로 보인다
    try:
        assert hub.poll() == []
    finally:
        hub._uid = os.geteuid()
    assert hub.status()["holder"] == "agent"
    assert "소유자" in hub.rejected[-1][1]


def test_symlinked_command_rejected(hub: HandoffHub, tmp_path: Path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"op": "take", "server_id": hub.server_id, "nonce": "sym1"}))
    os.chmod(target, 0o600)
    (hub.dir / "cmd-sym1.json").symlink_to(target)
    assert hub.poll() == []
    assert hub.status()["holder"] == "agent"
    # 이름 규칙에 맞는 파일이 실제로 검사됐다(이름 때문에 무시된 것이 아님).
    assert hub.rejected and hub.rejected[-1][0] == "cmd-sym1.json"


def test_unknown_op_and_nonce_mismatch_rejected(hub: HandoffHub):
    _forge(hub, {"op": "sudo", "server_id": hub.server_id})
    _forge(hub, {"op": "take", "server_id": hub.server_id, "nonce": "zzzz"}, name="cmd-aaaa.json")
    assert hub.poll() == []
    assert hub.status()["holder"] == "agent"
    assert {name for name, _ in hub.rejected} == {"cmd-forged1.json", "cmd-aaaa.json"}


def test_processed_commands_are_removed_and_acked(hub: HandoffHub):
    nonce = write_command(hub.root, hub.server_id, "take")
    hub.poll()
    assert not (hub.dir / f"cmd-{nonce}.json").exists()
    ack = json.loads((hub.dir / f"ack-{nonce}.json").read_text())
    assert ack["ok"] is True
    assert _mode(hub.dir / f"ack-{nonce}.json") == 0o600


# ------------------------------------------------------------------ 승인 증표


def test_digest_is_stable_and_sensitive():
    base = action_digest(_components())
    assert base == action_digest(_components())
    assert len(base) == 64


@pytest.mark.parametrize("change", [
    {"params": {"element_id": "@e4", "epoch": 2}},           # 파라미터 1글자
    {"gate_basis": {"name": "결제하기!", "matched_keyword": "결제", "source": "name"}},  # 대상
    {"tab_id": "tab-2"},
    {"origin": "http://127.0.0.1:8001"},
    {"snapshot_epoch": 3},
    {"action": "press_key"},
])
def test_digest_tamper_rejected(hub: HandoffHub, change):
    ap = hub.issue_approval(_components(), {"action": "click"})
    _approve_via_cli_file(hub, ap.approval_id)
    ok, why = hub.consume_approval(ap.approval_id, _components(**change))
    assert not ok and why
    # 거부는 증표를 소모하지 않는다 — 같은 행동이면 여전히 쓸 수 있다.
    ok, why = hub.consume_approval(ap.approval_id, _components())
    assert ok, why


def test_mismatch_reason_names_component(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"action": "click"})
    _approve_via_cli_file(hub, ap.approval_id)
    for key, val, word in [("tab_id", "tab-9", "탭"), ("origin", "http://x", "origin"),
                           ("snapshot_epoch", 7, "epoch")]:
        ok, why = hub.consume_approval(ap.approval_id, _components(**{key: val}))
        assert not ok and word in why, why


def test_agent_alone_cannot_approve(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"action": "click"})
    ok, why = hub.consume_approval(ap.approval_id, _components())
    assert not ok and "승인" in why
    assert hub.approval_status(ap.approval_id)["status"] == "pending"


def test_forged_approval_file_does_not_approve(hub: HandoffHub):
    """approvals/<id>.json 을 approved 로 고쳐 써도 서버는 메모리 상태만 믿는다."""
    ap = hub.issue_approval(_components(), {"action": "click"})
    f = hub.dir / "approvals" / f"{ap.approval_id}.json"
    data = json.loads(f.read_text())
    data["status"] = "approved"
    f.write_text(json.dumps(data))
    hub.poll()
    ok, _ = hub.consume_approval(ap.approval_id, _components())
    assert not ok


def test_approve_command_with_wrong_digest_rejected(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"action": "click"})
    write_command(hub.root, hub.server_id, "approve", approval_id=ap.approval_id,
                  action_digest="0" * 64)
    hub.poll()
    assert hub.approval_status(ap.approval_id)["status"] == "pending"
    assert not hub.consume_approval(ap.approval_id, _components())[0]


def test_approve_command_with_loose_perms_rejected(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"action": "click"})
    _forge(hub, {"op": "approve", "server_id": hub.server_id, "approval_id": ap.approval_id,
                 "action_digest": ap.digest}, mode=0o640)
    hub.poll()
    assert hub.approval_status(ap.approval_id)["status"] == "pending"


def test_other_server_approval_id_invalid(root: Path):
    a, b = HandoffHub(root), HandoffHub(root)
    a.open()
    b.open()
    try:
        ap = a.issue_approval(_components(), {"action": "click"})
        _approve_via_cli_file(a, ap.approval_id)
        ok, why = b.consume_approval(ap.approval_id, _components())
        assert not ok and "알 수 없는" in why
        # 다른 서버 디렉터리에 같은 id 로 승인 명령을 써도 b 에는 그 증표가 없다.
        write_command(root, b.server_id, "approve", approval_id=ap.approval_id,
                      action_digest=ap.digest)
        b.poll()
        assert not b.consume_approval(ap.approval_id, _components())[0]
        assert a.consume_approval(ap.approval_id, _components())[0]
    finally:
        a.close()
        b.close()


def test_expiry(root: Path):
    now = [1000.0]
    h = HandoffHub(root, approval_ttl_s=60, clock=lambda: now[0])
    h.open()
    try:
        ap = h.issue_approval(_components(), {"action": "click"})
        _approve_via_cli_file(h, ap.approval_id)
        now[0] += 61
        ok, why = h.consume_approval(ap.approval_id, _components())
        assert not ok and "만료" in why
    finally:
        h.close()


def test_approve_after_expiry_rejected(root: Path):
    now = [1000.0]
    h = HandoffHub(root, approval_ttl_s=60, clock=lambda: now[0])
    h.open()
    try:
        ap = h.issue_approval(_components(), {"action": "click"})
        shown = handoff.read_pending_approval(root, h.server_id, ap.approval_id)
        now[0] += 120
        write_command(root, h.server_id, "approve", approval_id=ap.approval_id,
                      action_digest=shown["action_digest"])
        h.poll()
        assert h.approval_status(ap.approval_id)["status"] == "expired"
    finally:
        h.close()


def test_single_use(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"action": "click"})
    _approve_via_cli_file(hub, ap.approval_id)
    assert hub.consume_approval(ap.approval_id, _components())[0]
    ok, why = hub.consume_approval(ap.approval_id, _components())
    assert not ok and "사용" in why


def test_outcome_unknown_marks_and_stays_unusable(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"action": "click"})
    _approve_via_cli_file(hub, ap.approval_id)
    assert hub.consume_approval(ap.approval_id, _components())[0]
    hub.set_outcome(ap.approval_id, "outcome_unknown")
    assert hub.approval_status(ap.approval_id)["outcome"] == "outcome_unknown"
    assert not hub.consume_approval(ap.approval_id, _components())[0]


def test_deny(hub: HandoffHub):
    ap = hub.issue_approval(_components(), {"action": "click"})
    shown = handoff.read_pending_approval(hub.root, hub.server_id, ap.approval_id)
    write_command(hub.root, hub.server_id, "deny", approval_id=ap.approval_id,
                  action_digest=shown["action_digest"])
    hub.poll()
    assert hub.approval_status(ap.approval_id)["status"] == "denied"
    assert not hub.consume_approval(ap.approval_id, _components())[0]


def test_same_pending_action_reuses_id(hub: HandoffHub):
    a = hub.issue_approval(_components(), {"action": "click"})
    b = hub.issue_approval(_components(), {"action": "click"})
    c = hub.issue_approval(_components(snapshot_epoch=9), {"action": "click"})
    assert a.approval_id == b.approval_id != c.approval_id


def test_approval_ids_are_unguessable(hub: HandoffHub):
    ids = {hub.issue_approval(_components(snapshot_epoch=i), {}).approval_id for i in range(50)}
    assert len(ids) == 50
    assert all(len(i) >= 16 for i in ids)


# ------------------------------------------------------------------ CLI (사람 쪽)


def _start_poller(hub: HandoffHub):
    """서버 쪽 감시 루프 흉내(실서버는 asyncio 태스크로 같은 hub.poll 을 돈다)."""
    import threading

    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            hub.poll()
            time.sleep(0.02)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return stop, t


def test_cli_control_take_status_release(hub: HandoffHub, root: Path, monkeypatch, capsys):
    from interface import cli

    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    stop, t = _start_poller(hub)
    try:
        assert cli.main(["control", "take"]) == 0
        assert hub.status()["holder"] == "human"
        assert cli.main(["control", "status", "--json"]) == 0
        out = capsys.readouterr().out
        assert json.loads(out.strip().splitlines()[-1])["holder"] == "human"
        assert cli.main(["control", "release", "--server", hub.server_id]) == 0
        assert hub.status()["holder"] == "agent"
    finally:
        stop.set()
        t.join()


def test_cli_approve_yes_and_prompt(hub: HandoffHub, root: Path, monkeypatch, capsys):
    from interface import cli

    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(root))
    stop, t = _start_poller(hub)
    try:
        ap = hub.issue_approval(_components(), {"action": "click", "target": "결제하기"})
        # 대화형: N 이면 승인하지 않는다(그리고 거절로 기록)
        monkeypatch.setattr("builtins.input", lambda *_: "n")
        monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: True)
        assert cli.main(["approve", ap.approval_id]) == 1
        assert hub.approval_status(ap.approval_id)["status"] == "denied"
        ap2 = hub.issue_approval(_components(snapshot_epoch=5), {"action": "click"})
        monkeypatch.setattr("builtins.input", lambda *_: "y")
        assert cli.main(["approve", ap2.approval_id]) == 0
        assert hub.approval_status(ap2.approval_id)["status"] == "approved"
        out = capsys.readouterr().out
        assert "click" in out  # 내용을 보여 준다
        ap3 = hub.issue_approval(_components(snapshot_epoch=6), {"action": "click"})
        monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
        # TTY 아님 + --yes 없음 → 거부(스크립트가 실수로 승인하지 않게)
        assert cli.main(["approve", ap3.approval_id]) == 2
        assert hub.approval_status(ap3.approval_id)["status"] == "pending"
        assert cli.main(["approve", ap3.approval_id, "--yes"]) == 0
        assert hub.approval_status(ap3.approval_id)["status"] == "approved"
    finally:
        stop.set()
        t.join()


def test_cli_without_server_fails_cleanly(tmp_path: Path, monkeypatch, capsys):
    from interface import cli

    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "none"))
    assert cli.main(["control", "status"]) == 2
    assert cli.main(["approve", "ap_x"]) == 2


def test_agent_cannot_lower_secret_wanted_by_rerequesting(hub: HandoffHub):
    hub.request("비밀번호 입력", secret_wanted=True)
    hub.request("다시", secret_wanted=False)
    assert hub.status()["secret_wanted"] is True
    assert hub.control_blocks(ActionType.OBSERVE_PAGE, {}) is not None
    write_command(hub.root, hub.server_id, "release")
    hub.poll()
    assert hub.control_blocks(ActionType.OBSERVE_PAGE, {}) is None
