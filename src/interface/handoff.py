"""사람 인계 통로(조작권) + 승인 증표 (WS-29) — MCP 계약 밖 서버 기능.

두 가지를 한 곳에서 다룬다.

* **조작권(control)** — holder ∈ {agent, human}. 에이전트가 `browser_control_request` 로 요청하면
  사람이 `agent-browser control take|release` 로 가져가고 돌려준다. holder=human 이면 에이전트의 조작
  액션은 거부되고 관찰만 허용된다. `secret_wanted` 요청(비밀번호 입력 등) 동안은 관찰도 막는다.
* **승인 증표(approval)** — 고위험 액션이 게이트에 막히면 행동 내용의 해시(action_digest)에 묶인
  1회용 증표를 발급한다. 사람이 `agent-browser approve <id>` 로 승인해야만 같은 행동이 통과한다.

신호 통로(사람 → 서버)는 서버별 상태 디렉터리다::

    ~/.agent-browser/servers/<server_id>/      0700
        server.json      서버 정보(pid, 브라우저 방식)          0600
        control.json     조작권 상태(사람 CLI 가 읽는다)         0600
        approvals/<id>.json  승인 대기 내용(사람이 볼 표시용)    0600
        cmd-<nonce>.json     사람 CLI 가 쓰는 명령(take/release/approve/deny)
        ack-<nonce>.json     서버의 처리 결과

신뢰 경계:
* 서버는 **자기 메모리의 상태만** 믿는다. 디스크의 control.json·approvals/*.json 은 사람이 볼 표시용이며,
  고쳐 써도 서버 판정이 바뀌지 않는다.
* 명령 파일은 이 프로세스와 같은 uid 소유·일반 파일(심볼릭 링크 아님)·권한 0600(그룹/기타 비트 없음)·
  server_id 일치·파일 이름 nonce 일치일 때만 처리한다. 승인 명령은 서버가 기억하는 action_digest 와
  같아야 한다(사람이 본 내용과 실행할 내용이 같음).
* 에이전트(MCP 클라이언트)에게는 승인 도구가 없다. 증표 id 만으로는 실행되지 않는다.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from contracts import ActionType

#: 상태 디렉터리 루트를 바꾸는 환경변수(테스트·여러 사용자 환경용).
STATE_ROOT_ENV = "AGENT_BROWSER_SERVERS_DIR"
DEFAULT_STATE_ROOT = Path.home() / ".agent-browser" / "servers"

#: 승인 증표 기본 수명(초) — OpenBot 행동 제안과 같은 30분.
DEFAULT_APPROVAL_TTL_S = 30 * 60
#: browser_control_wait / browser_approval_wait 의 timeout_s 상한·기본(초).
WAIT_MAX_S = 120.0
WAIT_DEFAULT_S = 60.0
#: 서버의 명령 디렉터리 감시 주기(초).
POLL_INTERVAL_S = 0.1
#: 명령 파일 크기 상한(바이트) — 그 이상은 읽지 않는다.
_MAX_CMD_BYTES = 16 * 1024

_CMD_RE = re.compile(r"^cmd-([A-Za-z0-9_-]{4,64})\.json$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_OPS = frozenset({"take", "release", "approve", "deny"})

HOLDER_AGENT = "agent"
HOLDER_HUMAN = "human"

#: holder=human 이어도 허용하는 관찰 액션(사람이 하는 걸 에이전트가 보고 이어받을 수 있게).
OBSERVE_ACTIONS = frozenset({
    ActionType.OBSERVE_PAGE,
    ActionType.TAKE_SCREENSHOT,
    ActionType.EXTRACT,
    ActionType.WAIT_FOR,
})


class HandoffError(RuntimeError):
    """상태 디렉터리를 안전하게 쓸 수 없음(소유자·심볼릭 링크 등)."""


def state_root() -> Path:
    raw = os.environ.get(STATE_ROOT_ENV)
    return Path(raw).expanduser() if raw else DEFAULT_STATE_ROOT


def _now_iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds")


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------------------- 파일 도구


def _write_private(path: Path, data: Dict[str, Any]) -> None:
    """0600 파일로 원자적으로 쓴다(임시 파일 → rename). 기존 파일을 따라가지 않는다."""
    tmp = path.parent / f".tmp-{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _check_private_file(path: Path, uid: int) -> Optional[str]:
    """신뢰 가능한 파일인가. 문제가 있으면 사유, 없으면 None."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return f"읽을 수 없음({exc.__class__.__name__})"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return "일반 파일이 아님(심볼릭 링크 등)"
    if st.st_uid != uid:
        return f"소유자 불일치(uid {st.st_uid})"
    if st.st_mode & 0o077:
        return f"권한이 느슨함({oct(stat.S_IMODE(st.st_mode))}, 0600 이어야 함)"
    if st.st_size > _MAX_CMD_BYTES:
        return "파일이 너무 큼"
    return None


def _read_private_json(path: Path, uid: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    problem = _check_private_file(path, uid)
    if problem:
        return None, problem
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"읽기 실패({exc.__class__.__name__})"
    if not isinstance(data, dict):
        return None, "형식 오류"
    return data, None


def _ensure_private_dir(path: Path, uid: int) -> None:
    """디렉터리를 0700 으로 만들거나 확인한다. 남의 소유·심볼릭 링크면 거부."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise HandoffError(f"상태 디렉터리 자리에 다른 파일이 있음: {path}") from exc
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise HandoffError(f"상태 디렉터리가 일반 디렉터리가 아님: {path}")
    if st.st_uid != uid:
        raise HandoffError(f"상태 디렉터리 소유자가 다름(uid {st.st_uid}): {path}")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError:
        return False
    return True


# ---------------------------------------------------------------------------- digest


def _canon(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


#: digest 를 이루는 구성요소(순서는 불일치 사유 보고 순서).
DIGEST_KEYS = ("action", "params", "gate_basis", "tab_id", "origin", "snapshot_epoch")
_KEY_LABEL = {
    "action": "액션 종류",
    "params": "파라미터",
    "gate_basis": "대상 요소(이름·문맥)",
    "tab_id": "탭",
    "origin": "문서 origin",
    "snapshot_epoch": "snapshot_epoch",
}


def normalize_components(components: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _canon(components.get(k)) for k in DIGEST_KEYS}


def action_digest(components: Dict[str, Any]) -> str:
    """액션 종류 + 정규화 파라미터 + 대상 근거 + 탭 + origin + epoch 의 SHA-256(hex)."""
    body = json.dumps({"v": 1, **normalize_components(components)},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------- 상태


@dataclass
class Approval:
    approval_id: str
    digest: str
    components: Dict[str, Any]
    summary: Dict[str, Any]
    created: float
    expires: float
    status: str = "pending"  # pending | approved | denied | expired | used
    outcome: Optional[str] = None


@dataclass
class HandoffHub:
    """서버 한 개의 조작권·승인 상태 + 명령 디렉터리 감시."""

    root: Path = field(default_factory=state_root)
    browser_mode: str = "headless"
    approval_ttl_s: float = DEFAULT_APPROVAL_TTL_S
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self._uid = os.geteuid()
        self.server_id = f"{os.getpid()}-{secrets.token_hex(3)}"
        self.dir = self.root / self.server_id
        self._opened = False
        self.holder = HOLDER_AGENT
        self.requested = False
        self.reason = ""
        self.secret_wanted = False
        self.since = self.clock()
        self.request_id: Optional[str] = None
        self.approvals: Dict[str, Approval] = {}
        #: 처리하지 않은 명령 (파일 이름, 사유) — 진단·테스트용.
        self.rejected: List[Tuple[str, str]] = []
        self._seen_bad: set = set()
        #: 상태가 바뀔 때마다 1 씩 — wait 가 변화를 알아챈다.
        self.version = 0
        self.last_event: Optional[str] = None

    # -- 수명주기 ------------------------------------------------------------

    def open(self) -> "HandoffHub":
        if self._opened:
            return self
        _ensure_private_dir(self.root, self._uid)
        self.cleanup_stale()
        self.dir.mkdir(mode=0o700)
        os.chmod(self.dir, 0o700)
        (self.dir / "approvals").mkdir(mode=0o700)
        os.chmod(self.dir / "approvals", 0o700)
        _write_private(self.dir / "server.json", {
            "server_id": self.server_id,
            "pid": os.getpid(),
            "browser_mode": self.browser_mode,
            "started_at": _now_iso(self.clock()),
        })
        self._opened = True
        self._write_control()
        return self

    @property
    def opened(self) -> bool:
        return self._opened

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        shutil.rmtree(self.dir, ignore_errors=True)

    def cleanup_stale(self) -> List[str]:
        """프로세스가 없는 서버 디렉터리(내 소유만)를 지운다."""
        removed: List[str] = []
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return removed
        for d in entries:
            try:
                st = os.lstat(d)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != self._uid:
                continue  # 남의 것·링크는 건드리지 않는다
            info, _ = _read_private_json(d / "server.json", self._uid)
            pid = info.get("pid") if info else None
            if isinstance(pid, int) and _pid_alive(pid):
                continue
            if info is None and self.clock() - st.st_mtime < 60:
                continue  # 막 만들어지는 중일 수 있다
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
        return removed

    # -- 조작권 --------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "holder": self.holder,
            "requested": self.requested,
            "reason": self.reason,
            "secret_wanted": self.secret_wanted,
            "since": _now_iso(self.since),
            "request_id": self.request_id,
            "server_id": self.server_id,
        }

    def how_to_respond(self) -> str:
        sid = self.server_id
        return (
            f"사람: `agent-browser control take --server {sid}` → 브라우저 창에서 직접 해결 → "
            f"`agent-browser control release --server {sid}`. 에이전트: browser_control_wait 로 기다리세요."
        )

    def request(self, reason: str, secret_wanted: bool = False) -> Dict[str, Any]:
        self.request_id = f"rq_{secrets.token_urlsafe(9)}"
        self.requested = self.holder == HOLDER_AGENT
        self.reason = str(reason or "")[:500]
        # 비밀 입력 보호는 사람이 release 할 때만 풀린다 — 에이전트가 다시 요청해 낮출 수 없다.
        self.secret_wanted = self.secret_wanted or bool(secret_wanted)
        self.since = self.clock()
        self._changed("requested")
        return {
            "request_id": self.request_id,
            "holder": self.holder,
            "secret_wanted": self.secret_wanted,
            "server_id": self.server_id,
            "how_to_respond": self.how_to_respond(),
        }

    def control_blocks(self, action: ActionType, args: Optional[Dict[str, Any]] = None
                       ) -> Optional[Dict[str, Any]]:
        """이 액션을 지금 막아야 하면 data.control 내용, 아니면 None."""
        if self.holder == HOLDER_AGENT and not (self.requested and self.secret_wanted):
            return None
        if not self.secret_wanted:
            if action in OBSERVE_ACTIONS:
                return None
            if action is ActionType.TAB_CONTROL and (args or {}).get("command") == "list":
                return None
        return {
            "holder": self.holder,
            "reason": self.reason,
            "since": _now_iso(self.since),
            "secret_wanted": self.secret_wanted,
            "request_id": self.request_id,
            "how_to_wait": "browser_control_wait",
        }

    def _changed(self, event: str) -> None:
        self.version += 1
        self.last_event = event
        if self._opened:
            self._write_control()

    def _write_control(self) -> None:
        try:
            _write_private(self.dir / "control.json", self.status())
        except OSError:
            pass

    # -- 승인 증표 ------------------------------------------------------------

    def issue_approval(self, components: Dict[str, Any], summary: Dict[str, Any]) -> Approval:
        digest = action_digest(components)
        now = self.clock()
        for ap in self.approvals.values():
            if ap.digest == digest and ap.status == "pending" and ap.expires > now:
                return ap
        ap = Approval(
            approval_id=f"ap_{secrets.token_urlsafe(12)}",
            digest=digest,
            components=normalize_components(components),
            summary=_canon(summary),
            created=now,
            expires=now + float(self.approval_ttl_s),
        )
        self.approvals[ap.approval_id] = ap
        self._write_approval(ap)
        self._changed("approval_issued")
        return ap

    def _write_approval(self, ap: Approval) -> None:
        if not self._opened:
            return
        try:
            _write_private(self.dir / "approvals" / f"{ap.approval_id}.json", {
                "approval_id": ap.approval_id,
                "server_id": self.server_id,
                "action_digest": ap.digest,
                "status": ap.status,
                "outcome": ap.outcome,
                "created_at": _now_iso(ap.created),
                "expires_at": _now_iso(ap.expires),
                "summary": ap.summary,
                "components": ap.components,
            })
        except OSError:
            pass

    def approval_status(self, approval_id: str) -> Dict[str, Any]:
        ap = self.approvals.get(str(approval_id or ""))
        if ap is None:
            return {"approval_id": approval_id, "status": "unknown"}
        self._expire(ap)
        return {
            "approval_id": ap.approval_id,
            "status": ap.status,
            "outcome": ap.outcome,
            "expires_at": _now_iso(ap.expires),
        }

    def _expire(self, ap: Approval) -> None:
        if ap.status in ("pending", "approved") and self.clock() >= ap.expires:
            ap.status = "expired"
            self._write_approval(ap)

    def consume_approval(self, approval_id: str, components: Dict[str, Any]) -> Tuple[bool, str]:
        """(승인됨 ∧ 만료 전 ∧ digest 일치 ∧ 1회용)일 때만 True. 성공하면 used 로 바뀐다."""
        ap = self.approvals.get(str(approval_id or ""))
        if ap is None:
            return False, "알 수 없는 승인 id(이 서버가 발급하지 않았거나 서버가 재시작됨)"
        self._expire(ap)
        if ap.status == "expired":
            return False, "승인 증표가 만료됨"
        if ap.status == "used":
            return False, "이미 사용한 승인 증표(1회용)"
        if ap.status == "denied":
            return False, "사람이 거절한 행동"
        if ap.status != "approved":
            return False, "아직 사람이 승인하지 않음(`agent-browser approve` 필요)"
        current = normalize_components(components)
        if action_digest(current) != ap.digest:
            diff = [k for k in DIGEST_KEYS if current.get(k) != ap.components.get(k)]
            labels = ", ".join(_KEY_LABEL[k] for k in diff) or "내용"
            return False, f"승인한 행동과 다름({labels} 불일치) — 다시 승인받아야 함"
        ap.status = "used"
        self._write_approval(ap)
        self._changed("approval_used")
        return True, ""

    def set_outcome(self, approval_id: str, outcome: str) -> None:
        ap = self.approvals.get(approval_id)
        if ap is None:
            return
        ap.outcome = outcome
        self._write_approval(ap)

    # -- 명령 처리 ------------------------------------------------------------

    def poll(self) -> List[str]:
        """명령 파일을 처리하고 일어난 사건 목록(taken/released/approved/denied)을 돌려준다."""
        if not self._opened:
            return []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        events: List[str] = []
        for name in sorted(names):
            m = _CMD_RE.match(name)
            if not m:
                continue
            path = self.dir / name
            if name in self._seen_bad:
                continue
            data, problem = _read_private_json(path, self._uid)
            if problem is None:
                problem = self._validate_command(data, m.group(1))
            if problem is not None or data is None:
                self._reject(name, path, problem or "형식 오류")
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
            ok, message, event = self._apply(data)
            if event:
                events.append(event)
            try:
                _write_private(self.dir / f"ack-{m.group(1)}.json",
                               {"ok": ok, "message": message, "control": self.status()})
            except OSError:
                pass
        return events

    def _validate_command(self, data: Any, nonce: str) -> Optional[str]:
        if data.get("server_id") != self.server_id:
            return "server_id 불일치"
        if data.get("nonce") != nonce:
            return "nonce 불일치"
        if data.get("op") not in _OPS:
            return f"알 수 없는 명령 {data.get('op')!r}"
        return None

    def _reject(self, name: str, path: Path, problem: str) -> None:
        self.rejected.append((name, problem))
        self._seen_bad.add(name)
        try:
            st = os.lstat(path)
            if st.st_uid == self._uid:
                os.unlink(path)  # 내 것만 지운다 — 남의 파일은 무시만
        except OSError:
            pass

    def _apply(self, data: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
        op = data["op"]
        if op == "take":
            self.holder = HOLDER_HUMAN
            self.requested = False
            self.since = self.clock()
            self._changed("taken")
            return True, "조작권을 사람이 가져갔습니다. 해결 뒤 release 하세요.", "taken"
        if op == "release":
            self.holder = HOLDER_AGENT
            self.requested = False
            self.secret_wanted = False
            self.since = self.clock()
            self._changed("released")
            return True, "조작권을 에이전트에게 돌려줬습니다.", "released"
        approval_id = str(data.get("approval_id") or "")
        ap = self.approvals.get(approval_id)
        if ap is None:
            return False, "알 수 없는 승인 id", None
        self._expire(ap)
        if ap.status != "pending":
            return False, f"승인 대기 상태가 아님({ap.status})", None
        if not secrets.compare_digest(str(data.get("action_digest") or ""), ap.digest):
            return False, "action_digest 불일치 — 보여 준 내용과 다른 행동", None
        ap.status = "approved" if op == "approve" else "denied"
        self._write_approval(ap)
        self._changed(ap.status)
        return True, f"{approval_id}: {ap.status}", ap.status


# ---------------------------------------------------------------------------- 사람 쪽(CLI)


def list_servers(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    root = Path(root) if root is not None else state_root()
    uid = os.geteuid()
    out: List[Dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for d in entries:
        try:
            st = os.lstat(d)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
            continue
        info, _ = _read_private_json(d / "server.json", uid)
        if not info or info.get("server_id") != d.name:
            continue
        if not _pid_alive(int(info.get("pid") or 0)):
            continue
        out.append(info)
    return out


def resolve_server(root: Optional[Path], server_id: Optional[str]
                   ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    servers = list_servers(root)
    if server_id:
        for s in servers:
            if s["server_id"] == server_id:
                return s, None
        return None, f"실행 중인 서버 {server_id} 가 없습니다."
    if not servers:
        return None, "실행 중인 agent-browser serve 가 없습니다."
    if len(servers) > 1:
        listing = ", ".join(f"{s['server_id']}({s.get('browser_mode')})" for s in servers)
        return None, f"서버가 여러 개입니다 — --server 로 고르세요: {listing}"
    return servers[0], None


def write_command(root: Optional[Path], server_id: str, op: str, **fields: Any) -> str:
    root = Path(root) if root is not None else state_root()
    if not _ID_RE.match(server_id):
        raise HandoffError("server_id 형식 오류")
    nonce = secrets.token_hex(8)
    body = {"op": op, "server_id": server_id, "nonce": nonce, **fields}
    _write_private(root / server_id / f"cmd-{nonce}.json", body)
    return nonce


def wait_ack(root: Optional[Path], server_id: str, nonce: str, timeout_s: float = 5.0
             ) -> Optional[Dict[str, Any]]:
    root = Path(root) if root is not None else state_root()
    path = root / server_id / f"ack-{nonce}.json"
    deadline = time.monotonic() + timeout_s
    uid = os.geteuid()
    while time.monotonic() < deadline:
        if path.exists():
            data, problem = _read_private_json(path, uid)
            try:
                os.unlink(path)
            except OSError:
                pass
            return data if problem is None else None
        time.sleep(0.03)
    return None


def read_control(root: Optional[Path], server_id: str) -> Optional[Dict[str, Any]]:
    root = Path(root) if root is not None else state_root()
    data, _ = _read_private_json(root / server_id / "control.json", os.geteuid())
    return data


def read_pending_approval(root: Optional[Path], server_id: str, approval_id: str
                          ) -> Optional[Dict[str, Any]]:
    root = Path(root) if root is not None else state_root()
    if not _ID_RE.match(str(approval_id or "")):
        return None
    data, _ = _read_private_json(root / server_id / "approvals" / f"{approval_id}.json",
                                 os.geteuid())
    if not data or data.get("approval_id") != approval_id or data.get("server_id") != server_id:
        return None
    return data


def find_approval_server(root: Optional[Path], approval_id: str
                         ) -> Tuple[Optional[str], Optional[str]]:
    """approval_id 를 가진 실행 중 서버를 찾는다(--server 생략 시)."""
    hits = [s["server_id"] for s in list_servers(root)
            if read_pending_approval(root, s["server_id"], approval_id)]
    if not hits:
        return None, f"승인 id {approval_id} 를 가진 실행 중 서버가 없습니다."
    if len(hits) > 1:
        return None, f"여러 서버에 같은 id — --server 로 고르세요: {', '.join(hits)}"
    return hits[0], None


def cli_control(op: str, server_id: Optional[str], as_json: bool = False) -> int:
    root = state_root()
    info, err = resolve_server(root, server_id)
    if info is None:
        print(f"agent-browser control: {err}", file=sys.stderr)
        return 2
    sid = info["server_id"]
    if op == "status":
        st = read_control(root, sid)
        if st is None:
            print("agent-browser control: 상태 파일을 읽을 수 없습니다.", file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(st, ensure_ascii=False))
        else:
            print(f"server {sid}: holder={st.get('holder')} requested={st.get('requested')} "
                  f"secret_wanted={st.get('secret_wanted')} reason={st.get('reason')!r} "
                  f"since={st.get('since')}")
        return 0
    nonce = write_command(root, sid, op)
    ack = wait_ack(root, sid, nonce)
    if ack is None:
        print("agent-browser control: 서버 응답 없음(5초)", file=sys.stderr)
        return 2
    print(ack.get("message", ""))
    return 0 if ack.get("ok") else 1


def _show_approval(data: Dict[str, Any]) -> str:
    s = data.get("summary") or {}
    c = data.get("components") or {}
    basis = c.get("gate_basis") or {}
    lines = [
        f"승인 요청 {data.get('approval_id')} (서버 {data.get('server_id')})",
        f"  액션     : {c.get('action')}",
        f"  대상     : {s.get('target') or basis.get('name') or '(이름 없음)'}",
        f"  판정 근거: {s.get('reason', '')}",
        f"  문서     : {c.get('origin')}  탭 {c.get('tab_id')}  epoch {c.get('snapshot_epoch')}",
        f"  파라미터 : {json.dumps(c.get('params'), ensure_ascii=False)}",
        f"  만료     : {data.get('expires_at')}",
        f"  digest   : {data.get('action_digest')}",
    ]
    return "\n".join(lines)


def cli_approve(approval_id: str, server_id: Optional[str], yes: bool, deny: bool = False) -> int:
    root = state_root()
    if server_id:
        info, err = resolve_server(root, server_id)
        sid = info["server_id"] if info else None
    else:
        sid, err = find_approval_server(root, approval_id)
    if sid is None:
        print(f"agent-browser approve: {err}", file=sys.stderr)
        return 2
    data = read_pending_approval(root, sid, approval_id)
    if data is None:
        print(f"agent-browser approve: 승인 요청 {approval_id} 를 찾을 수 없습니다.", file=sys.stderr)
        return 2
    print(_show_approval(data))
    if data.get("status") != "pending":
        print(f"승인 대기 상태가 아닙니다: {data.get('status')}")
        return 1
    op = "deny" if deny else "approve"
    if not deny and not yes:
        if not _stdin_is_tty():
            print("agent-browser approve: 터미널이 아니면 --yes 가 필요합니다.", file=sys.stderr)
            return 2
        answer = input("이 행동을 실행하도록 승인할까요? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            op = "deny"
    nonce = write_command(root, sid, op, approval_id=approval_id,
                          action_digest=data.get("action_digest"))
    ack = wait_ack(root, sid, nonce)
    if ack is None:
        print("agent-browser approve: 서버 응답 없음(5초)", file=sys.stderr)
        return 2
    print(ack.get("message", ""))
    return 0 if ack.get("ok") and op == "approve" else 1
