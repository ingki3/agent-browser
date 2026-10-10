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

#: 확인 코드(WS-29 R1): 사람이 `approve` 를 실행하면 서버가 6자리 숫자를 만들어 **브라우저 창
#: 오버레이에만** 띄운다. 사람이 그 코드를 입력해야 승인된다. 서버는 코드의 HMAC 만 기억한다.
CODE_DIGITS = 6
#: 코드 수명(초). 지나면 오버레이에서 내리고 무효.
CODE_TTL_S = 120.0
#: 연속으로 틀릴 수 있는 횟수 — 이만큼 틀리면 그 증표를 폐기(revoked)한다(무차별 대입 방지:
#: 6자리 1/10^6 × 3회).
MAX_CODE_FAILS = 3
#: 증표 하나당 코드 표시 횟수 상한. 코드가 떠 있는 동안 화면 캡처를 거부하므로, 표시를 끝없이
#: 반복시켜 캡처를 영구히 막는 것(서비스 방해)을 막는다 — 넘으면 폐기.
MAX_CODE_SHOWS = 5
#: 끝난 증표(used·denied·expired·revoked)를 메모리·파일에 남겨 두는 시간(초) — 그 뒤 삭제.
APPROVAL_RETAIN_S = 10 * 60
#: 메모리에 두는 증표 수 상한(넘으면 끝난 것 → 오래된 대기 순으로 지운다).
MAX_APPROVALS = 200
#: 끝난 상태.
_FINISHED = frozenset({"used", "denied", "expired", "revoked"})

_CMD_RE = re.compile(r"^cmd-([A-Za-z0-9_-]{4,64})\.json$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CODE_RE = re.compile(r"^\d{%d}$" % CODE_DIGITS)
_OPS = frozenset({"take", "release", "approve", "deny", "show_code", "describe", "login", "login_finish", "cookie_sites"})
#: describe 요청의 challenge(사람 CLI 가 요청마다 새로 만든 16바이트 hex).
_CHALLENGE_RE = re.compile(r"^[0-9a-f]{32,128}$")

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
    """사람 CLI 의 UX 판단용(프롬프트를 띄울 수 있는가). **보안 근거가 아니다** — pty 로 흉내 낼 수
    있다(WS-29 검증 실측). 승인의 보안 근거는 창 오버레이에만 뜨는 확인 코드다."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------------------- 표시 살균

#: 사람에게 보이는 한 칸의 기본 길이 상한(글자).
DISPLAY_LIMIT = 200
#: 양방향 제어(LRE·RLE·PDF·LRO·RLO, LRI·RLI·FSI·PDI) — 범주로도 Cf 지만 명시해 둔다.
_BIDI = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
#: 터미널·창에서 보이지 않거나 화면을 조작하는 범주: 제어(Cc: 개행·CR·ESC·BEL·NUL·DEL·C1),
#: 서식(Cf: 양방향·폭 0·BOM·soft hyphen), 줄/문단 구분(Zl·Zp: U+2028/2029), 대리쌍(Cs).
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})


def display_safe(value: Any, limit: int = DISPLAY_LIMIT) -> str:
    """외부 유래 문자열(요소 이름·판정 근거·reason·URL·호스트)을 사람에게 보여 주기 직전에 살균한다.

    보이지 않거나 화면을 조작하는 문자는 보이는 표기(`\\x1b`, `\\u202e`)로 바꾸고 길이를 자른다.
    판정용 원문(digest 구성요소)은 건드리지 않는다 — 표시에만 쓴다(WS-29 R1 BLOCKING-1).
    """
    import unicodedata

    text = value if isinstance(value, str) else str(value)
    out: List[str] = []
    size = 0
    for ch in text:
        if ch in _BIDI or unicodedata.category(ch) in _HIDDEN_CATEGORIES:
            code = ord(ch)
            piece = f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"
        else:
            piece = ch
        size += len(piece)
        if size > limit:
            # 잘린 표시 — 마지막 칸에 말줄임표.
            while out and sum(map(len, out)) > limit - 1:
                out.pop()
            out.append("…")
            break
        out.append(piece)
    return "".join(out)


#: R1 NB-4: 승인 코드 화면에서 사이트가 붙인 대상 이름 앞에 붙이는 표기.
SITE_NAME_LABEL = "(사이트가 붙인 이름)"
#: 코드처럼 보이는 숫자열 — 6자리 이상(공백·하이픈·점 한 칸 사이 허용, 전각 숫자 포함).
_CODE_LIKE_RE = re.compile(r"\d(?:[\s\-.·_/]?\d){5,}")


def mask_code_like(value: str) -> str:
    """대상 이름의 6자리 이상 숫자열을 `••••••` 로 가린다(R1 NB-4).

    사이트가 요소 이름에 "확인 코드 123456" 을 넣어 진짜 확인 코드와 헷갈리게 하지 못하게 —
    진짜 코드는 별도 라벨·큰 글씨로만 보인다. 판정용 원문은 건드리지 않는다(표시 전용).
    """
    return _CODE_LIKE_RE.sub("••••••", value or "")


# ---------------------------------------------------------------------------- 파일 도구


def _write_private(path: Path, data: Dict[str, Any]) -> None:
    """0600 파일로 원자적으로 쓴다(임시 파일 → rename). 기존 파일을 따라가지 않는다."""
    tmp = path.parent / f".tmp-{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # ensure_ascii: 외부 유래 문자열의 외톨이 서로게이트(\ud800)도 손실 없이 \uXXXX 로 쓴다
            # (ensure_ascii=False 면 UTF-8 인코딩에서 UnicodeEncodeError — R3 NB-1).
            json.dump(data, fh, ensure_ascii=True)
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
    """내(같은 uid) 서버 프로세스가 살아 있는가.

    서버 디렉터리는 내 uid 소유이고 server.json 에 내 uid 를 적는다 — 내 프로세스면 신호를 보낼 수
    있다. PermissionError 는 그 pid 를 **다른 uid 프로세스**가 쓰고 있다는 뜻(pid 재사용)이므로
    죽은 서버로 본다(WS-29 R1 NB-6: 예전엔 True 로 봐 죽은 디렉터리가 영구히 남았다).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OverflowError:
        return False
    return True


def _process_start_time(pid: int) -> Optional[float]:
    """pid 프로세스의 시작 시각(epoch 초). 못 읽으면 None(판정에 쓰지 않는다)."""
    import subprocess

    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True,
                             text=True, timeout=2, env={**os.environ, "LC_ALL": "C"})
        raw = out.stdout.strip()
        if out.returncode != 0 or not raw:
            return None
        return time.mktime(time.strptime(" ".join(raw.split()), "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _server_alive(info: Optional[Dict[str, Any]], uid: int) -> bool:
    """server.json 기준 생존 판정: 기록된 uid 가 나와 같고(있으면) pid 가 내 프로세스로 살아 있고,
    그 프로세스가 서버 기록 시각 **뒤에** 시작된 것이 아님(같은 uid 의 pid 재사용 — 시작 시각 비교)."""
    if not info:
        return False
    recorded = info.get("uid")
    if recorded is not None and recorded != uid:
        return False
    pid = info.get("pid")
    if not (isinstance(pid, int) and _pid_alive(pid)):
        return False
    started = info.get("started")
    if isinstance(started, (int, float)):
        proc_start = _process_start_time(pid)
        # 프로세스는 server.json 을 쓰기 전에 시작한다. 기록보다 늦게 시작했다면 다른 프로세스.
        if proc_start is not None and proc_start > float(started) + 2.0:
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
    # surrogatepass: 외톨이 서로게이트(요소 이름은 페이지가 정한다)도 예외 없이, 서로 다른 문자열은
    # 서로 다른 바이트로(단사) — 정상 문자열의 바이트는 strict UTF-8 과 같다(R3 NB-1).
    return hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()


# ---------------------------------------------------------------------------- 상태


@dataclass
class Approval:
    approval_id: str
    digest: str
    components: Dict[str, Any]
    summary: Dict[str, Any]
    created: float
    expires: float
    status: str = "pending"  # pending | approved | denied | expired | used | revoked
    outcome: Optional[str] = None
    #: 끝난 시각(used·denied·expired·revoked) — 보존 기간 뒤 삭제(NB-5).
    finished: Optional[float] = None
    #: 확인 코드: HMAC(서버 비밀키, 코드) 만 둔다 — 평문은 메모리에도 남기지 않는다.
    code_mac: Optional[bytes] = field(default=None, repr=False)
    code_expires: float = 0.0
    code_shows: int = 0
    code_fails: int = 0


@dataclass
class CodeJob:
    """창 오버레이에 띄울 코드 한 건(서버가 꺼내 표시하고 즉시 버린다). 디스크에 쓰지 않는다."""

    approval_id: str
    nonce: str
    code: str = field(repr=False)
    expires: float = 0.0


@dataclass
class LoginJob:
    """A human CLI request; URL remains in memory, never in status output."""

    url: str
    login_id: str
    deadline: float
    nonce: str
    status: str = "pending"


@dataclass
class HandoffHub:
    """서버 한 개의 조작권·승인 상태 + 명령 디렉터리 감시."""

    root: Path = field(default_factory=state_root)
    browser_mode: str = "headless"
    approval_ttl_s: float = DEFAULT_APPROVAL_TTL_S
    clock: Callable[[], float] = time.time
    profile: Optional[str] = None

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
        #: 확인 코드 HMAC 키(프로세스 메모리에만).
        self._code_key = secrets.token_bytes(32)
        #: 창에 띄울 코드(서버가 take_code_job 으로 꺼낸다) · 표시 결과를 기다리는 ack nonce.
        self._code_jobs: List[CodeJob] = []
        self._code_pending_ack: Dict[str, str] = {}
        #: 창 상태(WS-34 on-demand). None 이면 상태에 싣지 않는다(다른 방식은 그대로).
        self.window: Optional[Dict[str, Any]] = None
        self.login: Optional[LoginJob] = None
        self._finished_logins: Dict[str, None] = {}
        self.site_jobs: List[str] = []

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
            # NB-6: 소유 uid·시작 시각 — pid 가 다른 프로세스에 재사용됐는지 가린다.
            "uid": self._uid,
            "started": time.time(),
            "browser_mode": self.browser_mode,
            "profile": self.profile,
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
            if _server_alive(info, self._uid):
                continue
            if info is None and self.clock() - st.st_mtime < 60:
                continue  # 막 만들어지는 중일 수 있다
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
        return removed

    # -- 조작권 --------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        out = {
            "holder": self.holder,
            "requested": self.requested,
            "reason": self.reason,
            "secret_wanted": self.secret_wanted,
            "since": _now_iso(self.since),
            "request_id": self.request_id,
            "server_id": self.server_id,
        }
        window = getattr(self, "window", None)
        if window is not None:
            out["window"] = dict(window)
        if self.login is not None:
            out["login"] = {"status": self.login.status}
        return out

    def set_window(self, info: Optional[Dict[str, Any]]) -> None:
        """창 상태(WS-34 on-demand: headless/headed/sticky)를 사람용 상태 파일에도 싣는다."""
        self.window = dict(info) if info is not None else None
        if self._opened:
            self._write_control()

    def cancel_request(self, event: str = "request_expired") -> bool:
        """사람이 가져가지 않은 조작권 요청을 서버가 거둔다(WS-34: 창을 연 요청의 만료)."""
        if not self.requested:
            return False
        self.requested = False
        self.since = self.clock()
        self._changed(event)
        return True

    def release_by_server(self) -> None:
        """서버가 조작권을 에이전트에게 돌린다(WS-34: 사람이 창을 직접 닫음 = 창이 없음).

        사람의 release 와 같은 상태로 만든다(비밀 입력 보호도 해제 — 비밀을 넣을 창이 없다)."""
        self._clear_login()
        self.holder = HOLDER_AGENT
        self.requested = False
        self.secret_wanted = False
        self.since = self.clock()
        self._changed("released")

    def _clear_login(self) -> None:
        job, self.login = self.login, None
        if job is None:
            return
        self._finished_logins[job.login_id] = None
        if len(self._finished_logins) > 2048:
            self._finished_logins.pop(next(iter(self._finished_logins)))
        if job.status != "ready":
            self._ack(job.nonce, False, "로그인 창 요청 취소/시간 만료")

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
        self.sweep()
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
        self._enforce_cap()
        self._write_approval(ap)
        self._changed("approval_issued")
        return ap

    def _write_approval(self, ap: Approval) -> None:
        """사람 CLI 가 볼 표시용 파일. 확인 코드(평문·HMAC)는 절대 쓰지 않는다."""
        if not self._opened or ap.approval_id not in self.approvals:
            return
        try:
            _write_private(self.dir / "approvals" / f"{ap.approval_id}.json", self.approval_view(ap))
        except OSError:
            pass

    def _finish(self, ap: Approval, status: str) -> None:
        ap.status = status
        ap.finished = self.clock()
        ap.code_mac = None  # 끝난 증표의 코드는 즉시 무효
        self._write_approval(ap)

    def _forget(self, approval_id: str) -> None:
        self.approvals.pop(approval_id, None)
        try:
            os.unlink(self.dir / "approvals" / f"{approval_id}.json")
        except OSError:
            pass

    def sweep(self) -> None:
        """만료 처리 + 끝난 지 APPROVAL_RETAIN_S 지난 증표를 메모리·파일에서 지운다(NB-5)."""
        now = self.clock()
        for ap in list(self.approvals.values()):
            self._expire(ap)
            if ap.status in _FINISHED and ap.finished is not None \
                    and now - ap.finished >= APPROVAL_RETAIN_S:
                self._forget(ap.approval_id)

    def _enforce_cap(self) -> None:
        """증표 수 상한 — 끝난 것부터, 그다음 오래된 대기 순으로 지운다."""
        if len(self.approvals) <= MAX_APPROVALS:
            return
        order = sorted(self.approvals.values(),
                       key=lambda a: (a.status not in _FINISHED, a.created))
        for ap in order[: len(self.approvals) - MAX_APPROVALS]:
            self._forget(ap.approval_id)

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
            self._finish(ap, "expired")

    def consume_approval(self, approval_id: str, components: Dict[str, Any]) -> Tuple[bool, str]:
        """(승인됨 ∧ 만료 전 ∧ digest 일치 ∧ 1회용)일 때만 True. 성공하면 used 로 바뀐다."""
        ok, why, _ = self.check_approval(approval_id, components)
        if not ok:
            return False, why
        ap = self.approvals[str(approval_id)]
        self._finish(ap, "used")
        self._changed("approval_used")
        return True, ""

    def check_approval(self, approval_id: str, components: Dict[str, Any]
                       ) -> Tuple[bool, str, str]:
        """consume 없이 판정만: (통과, 사유, 사유 코드). 사유 코드 target_changed = 대상·문맥이 바뀜."""
        ap = self.approvals.get(str(approval_id or ""))
        if ap is None:
            return False, "알 수 없는 승인 id(이 서버가 발급하지 않았거나 서버가 재시작됨)", "unknown"
        self._expire(ap)
        if ap.status == "expired":
            return False, "승인 증표가 만료됨", "expired"
        if ap.status == "used":
            return False, "이미 사용한 승인 증표(1회용)", "used"
        if ap.status == "denied":
            return False, "사람이 거절한 행동", "denied"
        if ap.status == "revoked":
            return False, "확인 코드 오류·표시 상한으로 폐기된 증표", "revoked"
        if ap.status != "approved":
            return False, "아직 사람이 승인하지 않음(`agent-browser approve` 필요)", "not_approved"
        current = normalize_components(components)
        if action_digest(current) != ap.digest:
            diff = [k for k in DIGEST_KEYS if current.get(k) != ap.components.get(k)]
            labels = ", ".join(_KEY_LABEL[k] for k in diff) or "내용"
            code = "target_changed" if set(diff) & {"gate_basis", "tab_id", "origin",
                                                    "snapshot_epoch"} else "mismatch"
            return False, f"승인한 행동과 다름({labels} 불일치) — 다시 승인받아야 함", code
        return True, "", ""

    def set_outcome(self, approval_id: str, outcome: str) -> None:
        ap = self.approvals.get(approval_id)
        if ap is None:
            return
        ap.outcome = outcome
        self._write_approval(ap)

    # -- 확인 코드 ------------------------------------------------------------

    def _code_mac(self, code: str) -> bytes:
        import hmac

        return hmac.new(self._code_key, code.encode("ascii", "replace"), hashlib.sha256).digest()

    def _code_live(self, ap: Approval) -> bool:
        if ap.code_mac is None:
            return False
        if ap.status != "pending" or self.clock() >= ap.code_expires:
            ap.code_mac = None
            return False
        return True

    def code_displayed(self) -> bool:
        """지금 창에 띄워 둔(유효한) 확인 코드가 있는가 — 있는 동안 화면 캡처를 거부한다."""
        if self._code_jobs:
            return True
        live = False
        for ap in self.approvals.values():
            self._expire(ap)
            live = self._code_live(ap) or live
        return live

    def take_code_job(self) -> Optional[CodeJob]:
        """서버가 창에 띄울 코드 한 건을 꺼낸다(꺼낸 뒤 hub 는 평문을 갖지 않는다).

        대기하는 동안 코드가 죽었으면(만료·증표 종료·다른 증표가 거둬들임) 띄우지 않고 실패 ack."""
        while self._code_jobs:
            job = self._code_jobs.pop(0)
            ap = self.approvals.get(job.approval_id)
            if ap is not None:
                self._expire(ap)
            if ap is not None and self._code_live(ap):
                return job
            self.code_shown(job.nonce, False)
        return None

    def code_shown(self, nonce: str, ok: bool, reason: Optional[str] = None) -> None:
        """서버가 코드 표시 결과를 알린다. 실패면 코드를 거둬들인다(창에 없는 코드는 무효).

        reason(실패 사유): "capture_busy" = 진행 중 화면 캡처가 상한 안에 끝나지 않아 표시를 취소함
        (창은 있다 — 잠시 뒤 다시). 그 밖(None) = 창이 없거나 오버레이를 띄우지 못함(R3 NB-4)."""
        approval_id = self._code_pending_ack.pop(nonce, None)
        ap = self.approvals.get(approval_id or "")
        if not ok and ap is not None:
            ap.code_mac = None
        if ok and ap is not None and ap.code_mac is not None:
            message = (f"브라우저 창 위에 {approval_id} 의 확인 코드({CODE_DIGITS}자리)를 띄웠습니다 "
                       f"({int(CODE_TTL_S)}초 유효). 창에서 보고 "
                       f"`agent-browser approve {approval_id} --code <코드>` 로 입력하세요.")
            self._ack(nonce, True, message)
        elif reason == "capture_busy":
            self._ack(nonce, False, "진행 중인 화면 캡처가 끝나지 않아 확인 코드 표시를 취소했습니다"
                                    "(창은 그대로입니다). 잠시 뒤 `agent-browser approve "
                                    f"{approval_id or ''}` 를 다시 실행하세요.")
        else:
            self._ack(nonce, False, "브라우저 창에 확인 코드를 띄울 수 없습니다 — 승인할 수 없습니다"
                                    "(창이 닫혔거나 오버레이를 지원하지 않는 브라우저).")

    def withdraw_codes(self) -> None:
        """창에 떠 있던 확인 코드를 모두 거둬들인다 — 코드를 띄운 탭이 닫혀 창에 코드가 없을 때
        (R3 NB-2). 창에 없는 코드로는 승인할 수 없다(code_shown 실패와 같은 원칙)."""
        for ap in self.approvals.values():
            ap.code_mac = None

    def _ack(self, nonce: str, ok: bool, message: str, **extra: Any) -> None:
        try:
            _write_private(self.dir / f"ack-{nonce}.json",
                           {"ok": ok, "message": message, "control": self.status(), **extra})
        except OSError:
            pass

    def approval_view(self, ap: Approval) -> Dict[str, Any]:
        """사람이 볼 승인 내용 — **서버 메모리 기준**(R2). 확인 코드(평문·HMAC)는 넣지 않는다.
        디스크의 approvals/<id>.json 과 같은 모양이다(CLI 가 둘을 대조한다)."""
        return {
            "approval_id": ap.approval_id,
            "server_id": self.server_id,
            "action_digest": ap.digest,
            "status": ap.status,
            "outcome": ap.outcome,
            "created_at": _now_iso(ap.created),
            "expires_at": _now_iso(ap.expires),
            "summary": ap.summary,
            "components": ap.components,
        }

    def _describe(self, ap: Approval, data: Dict[str, Any], nonce: str
                  ) -> Tuple[Optional[bool], str, Optional[str]]:
        """사람 CLI 의 승인 화면용 내용 요청. 응답을 요청마다 새 challenge 로 HMAC 해 묶는다."""
        challenge = str(data.get("challenge") or "")
        if not _CHALLENGE_RE.match(challenge):
            return False, "challenge 형식 오류", None
        view = self.approval_view(ap)
        self._ack(nonce, True, "", view=view, view_mac=view_mac(challenge, view))
        return None, "", None

    def _show_code(self, ap: Approval, nonce: str) -> Tuple[Optional[bool], str, Optional[str]]:
        if ap.code_shows >= MAX_CODE_SHOWS:
            self._finish(ap, "revoked")
            self._changed("revoked")
            return False, (f"코드 표시 상한({MAX_CODE_SHOWS}회)을 넘어 이 승인 요청을 폐기했습니다. "
                           "에이전트가 같은 행동을 다시 요청하면 새 id 가 나옵니다."), "revoked"
        ap.code_shows += 1
        code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        ap.code_mac = self._code_mac(code)
        ap.code_expires = self.clock() + CODE_TTL_S
        # 다른 증표의 코드는 거둬들인다(창에는 하나만 뜬다).
        for other in self.approvals.values():
            if other is not ap:
                other.code_mac = None
        self._code_jobs = [CodeJob(ap.approval_id, nonce, code, ap.code_expires)]
        self._code_pending_ack[nonce] = ap.approval_id
        return None, "", "code_requested"  # ack 는 서버가 표시한 뒤(code_shown)

    def _check_code(self, ap: Approval, given: Any) -> Tuple[bool, str]:
        import hmac

        if not self._code_live(ap):
            return False, ("창에 떠 있는 확인 코드가 없습니다(만료·미표시). `agent-browser approve "
                           f"{ap.approval_id}` 를 코드 없이 실행해 창에 코드를 띄우세요.")
        text = str(given or "")
        if _CODE_RE.match(text) and hmac.compare_digest(self._code_mac(text), ap.code_mac or b""):
            ap.code_mac = None
            ap.code_fails = 0
            return True, ""
        ap.code_fails += 1
        if ap.code_fails >= MAX_CODE_FAILS:
            self._finish(ap, "revoked")
            self._changed("revoked")
            return False, (f"확인 코드가 {MAX_CODE_FAILS}회 틀려 이 승인 요청을 폐기했습니다.")
        left = MAX_CODE_FAILS - ap.code_fails
        return False, f"확인 코드가 틀렸습니다(남은 시도 {left}회)."

    # -- 명령 처리 ------------------------------------------------------------

    def poll(self) -> List[str]:
        """명령 파일을 처리하고 일어난 사건 목록(taken/released/approved/denied/…)을 돌려준다."""
        if not self._opened:
            return []
        self.sweep()
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        events: List[str] = []
        if self.login and time.monotonic() >= self.login.deadline:
            _, _, event = self._apply({"op": "login_finish", "login_id": self.login.login_id})
            if event:
                events.append(event)
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
            ok, message, event = self._apply(data, m.group(1))
            if event:
                events.append(event)
            if ok is not None:
                self._ack(m.group(1), ok, message)
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

    def _apply(self, data: Dict[str, Any], nonce: str = ""
               ) -> Tuple[Optional[bool], str, Optional[str]]:
        """명령 적용. ok=None 이면 ack 를 미룬다(show_code — 서버가 창에 띄운 뒤 code_shown)."""
        op = data["op"]
        if op == "cookie_sites":
            self.site_jobs.append(nonce)
            return None, "쿠키 메타데이터 요청", "cookie_sites"
        if op == "login":
            from interface.login_cli import MAX_LOGIN_TIMEOUT_S, validate_url

            try:
                url = validate_url(data.get("url", ""))
                duration = float(data.get("timeout", 600))
                import math

                if not math.isfinite(duration) or not 0 < duration <= MAX_LOGIN_TIMEOUT_S:
                    raise ValueError
                login_id = data.get("login_id")
                if not isinstance(login_id, str) or not _ID_RE.fullmatch(login_id):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                return False, "로그인 요청 형식 오류(http/https URL·timeout·login_id)", None
            if self.login or self.holder == HOLDER_HUMAN:
                return False, "이미 사람이 조작 중입니다.", None
            if login_id in self._finished_logins:
                return False, "이미 취소한 로그인 요청입니다.", None
            self.login = LoginJob(url=url, login_id=login_id, deadline=time.monotonic() + duration,
                                  nonce=nonce)
            self._write_control()
            return None, "로그인 창 준비 중", "login"
        if op == "login_finish":
            login_id = data.get("login_id")
            if not isinstance(login_id, str) or not _ID_RE.fullmatch(login_id):
                return False, "login_id 형식 오류", None
            # Command filenames contain random nonces, not arrival order. Remember
            # cancellation even if poll sees finish BEFORE the matching login.
            self._finished_logins[login_id] = None
            if len(self._finished_logins) > 2048:
                self._finished_logins.pop(next(iter(self._finished_logins)))
            job = self.login
            if not job or job.login_id != login_id:
                return True, "이미 반납했습니다.", None
            self._clear_login()
            if self.holder == HOLDER_HUMAN:
                self.release_by_server()
                return True, "로그인 조작권 반납", "released"
            self._write_control()
            return True, "로그인 요청 취소", None
        if op == "take":
            self.holder = HOLDER_HUMAN
            self.requested = False
            self.since = self.clock()
            self._changed("taken")
            return True, "조작권을 사람이 가져갔습니다. 해결 뒤 release 하세요.", "taken"
        if op == "release":
            self.release_by_server()
            return True, "조작권을 에이전트에게 돌려줬습니다.", "released"
        approval_id = str(data.get("approval_id") or "")
        ap = self.approvals.get(approval_id)
        if ap is None:
            return False, "알 수 없는 승인 id", None
        self._expire(ap)
        if op == "describe":  # 상태와 무관하게 서버 기록을 보여 준다(사람 CLI 가 상태도 확인)
            return self._describe(ap, data, nonce)
        if ap.status != "pending":
            return False, f"승인 대기 상태가 아님({ap.status})", None
        if not secrets.compare_digest(str(data.get("action_digest") or ""), ap.digest):
            return False, "action_digest 불일치 — 보여 준 내용과 다른 행동", None
        if op == "show_code":
            return self._show_code(ap, nonce)
        if op == "approve":
            ok, why = self._check_code(ap, data.get("code"))
            if not ok:
                return False, why, ("revoked" if ap.status == "revoked" else None)
            ap.status = "approved"
            self._write_approval(ap)
            self._changed("approved")
            return True, f"{approval_id}: approved", "approved"
        self._finish(ap, "denied")
        self._changed("denied")
        return True, f"{approval_id}: denied", "denied"


# ---------------------------------------------------------------------------- 사람 쪽(CLI)


def view_mac(challenge: str, view: Dict[str, Any]) -> str:
    """서버가 승인 화면 내용(view)을 요청의 challenge 로 HMAC-SHA256 한 값(hex).

    challenge 는 사람 CLI 가 요청마다 새로 만든다 — 응답이 이 요청에 대한 것이고 전송 중 바뀌지
    않았음을 CLI 가 확인한다. 같은 uid 는 명령 파일의 challenge 를 읽을 수 있으므로 같은 uid 에 대한
    인증은 아니다(README 한계 ①) — 사람이 대조할 독립 채널은 창 오버레이(액션·대상)다."""
    import hmac

    body = json.dumps(view, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hmac.new(challenge.encode("ascii"), body.encode("ascii"), hashlib.sha256).hexdigest()



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
        if not _server_alive(info, uid):
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
        print(f"agent-browser control: {display_safe(err, 1000)}", file=sys.stderr)
        return 2
    sid = info["server_id"]
    if op == "status":
        st = read_control(root, sid)
        if st is None:
            print("agent-browser control: 상태 파일을 읽을 수 없습니다.", file=sys.stderr)
            return 2
        if as_json:
            # JSON 은 제어문자를 이스케이프하고 비ASCII(양방향 제어 포함)도 u-표기로 낸다.
            print(json.dumps(st, ensure_ascii=True))
        else:
            # reason 은 에이전트 입력 — 사람 터미널에 그대로 내보내지 않는다(BLOCKING-1).
            print(f"server {display_safe(sid)}: holder={display_safe(st.get('holder'))} "
                  f"requested={display_safe(st.get('requested'))} "
                  f"secret_wanted={display_safe(st.get('secret_wanted'))} "
                  f"reason={display_safe(st.get('reason'), 500)} "
                  f"since={display_safe(st.get('since'))}")
            window = st.get("window")
            if isinstance(window, dict):  # WS-34 on-demand: 창 상태·sticky
                line = (f"  window={display_safe(window.get('state'))} "
                        f"sticky={display_safe(window.get('sticky'))}")
                if window.get("sticky_reason"):
                    line += f" sticky_reason={display_safe(window.get('sticky_reason'), 300)}"
                pending = window.get("sticky_pending")
                if isinstance(pending, dict):
                    line += (f" sticky_pending={display_safe(pending.get('domain'))}"
                             " (다음 조작권 요청 때 창을 열고 유지)")
                print(line)
        return 0
    nonce = write_command(root, sid, op)
    ack = wait_ack(root, sid, nonce)
    if ack is None:
        print("agent-browser control: 서버 응답 없음(5초)", file=sys.stderr)
        return 2
    print(display_safe(ack.get("message", ""), 1000))
    return 0 if ack.get("ok") else 1


def _show_approval(data: Dict[str, Any]) -> str:
    """사람이 볼 승인 내용. 페이지·에이전트 유래 문자열은 모두 display_safe 를 거친다(BLOCKING-1)."""
    s = data.get("summary") or {}
    c = data.get("components") or {}
    basis = c.get("gate_basis") or {}
    d = display_safe
    target = s.get("target") or basis.get("name") or "(이름 없음)"
    lines = [
        f"승인 요청 {d(data.get('approval_id'))} (서버 {d(data.get('server_id'))})",
        f"  액션     : {d(c.get('action'))}",
        f"  대상     : {d(target)}",
        f"  판정 근거: {d(s.get('reason', ''))}",
        f"  문서     : {d(c.get('origin'))}  탭 {d(c.get('tab_id'))}  "
        f"epoch {d(c.get('snapshot_epoch'))}",
        f"  파라미터 : {d(json.dumps(c.get('params'), ensure_ascii=False), 500)}",
        f"  만료     : {d(data.get('expires_at'))}",
        f"  digest   : {d(data.get('action_digest'))}",
    ]
    return "\n".join(lines)


def _send(root: Path, sid: str, op: str, timeout_s: float = 5.0, **fields: Any
          ) -> Optional[Dict[str, Any]]:
    nonce = write_command(root, sid, op, **fields)
    return wait_ack(root, sid, nonce, timeout_s=timeout_s)


#: 상태 파일과 서버 기록을 대조하는 칸(어느 하나라도 다르면 승인 절차를 시작하지 않는다).
_VIEW_KEYS = ("approval_id", "server_id", "action_digest", "summary", "components")


def _server_view(root: Path, sid: str, approval_id: str
                 ) -> Tuple[Optional[Dict[str, Any]], str]:
    """서버 메모리의 승인 내용을 받아 온다(요청마다 새 challenge, 응답 HMAC 확인). 실패하면 (None, 사유)
    — 파일 내용으로 대신 보여 주지 않는다(fail-closed)."""
    import hmac

    challenge = secrets.token_hex(16)
    ack = _send(root, sid, "describe", approval_id=approval_id, challenge=challenge)
    if ack is None:
        return None, "서버 응답 없음(5초) — 서버 기록을 받을 수 없어 승인하지 않습니다."
    if not ack.get("ok"):
        return None, str(ack.get("message") or "서버가 승인 내용을 주지 않았습니다.")
    view = ack.get("view")
    if not isinstance(view, dict) or not hmac.compare_digest(
            str(ack.get("view_mac") or ""), view_mac(challenge, view)):
        return None, "서버 응답 검증 실패(내용과 서명이 맞지 않음) — 승인하지 않습니다."
    if view.get("approval_id") != approval_id or view.get("server_id") != sid:
        return None, "서버 응답이 요청한 승인 id·서버와 다릅니다 — 승인하지 않습니다."
    return view, ""


def cli_approve(approval_id: str, server_id: Optional[str], yes: bool, deny: bool = False,
                code: Optional[str] = None) -> int:
    """사람의 승인. 승인에는 **브라우저 창 오버레이에만 뜨는 확인 코드**가 필요하다(WS-29 R1).

    * 코드 없이 실행 → 서버가 창에 6자리 코드를 띄운다. 터미널이면 그 자리에서 입력받고,
      아니면 `--code` 로 다시 실행하라고 안내한다.
    * `--code <코드>` → 그 코드로 승인. 틀리면 거부, 연속 3회 틀리면 그 요청은 폐기.
    * `--yes` 는 더 이상 코드를 대신하지 못한다(코드 없이는 승인 불가).
    TTY 여부는 프롬프트를 띄울지 정하는 UX 판단일 뿐 보안 근거가 아니다(pty 로 흉내 가능).
    """
    root = state_root()
    if server_id:
        info, err = resolve_server(root, server_id)
        sid = info["server_id"] if info else None
    else:
        sid, err = find_approval_server(root, approval_id)
    if sid is None:
        print(f"agent-browser approve: {display_safe(err, 1000)}", file=sys.stderr)
        return 2
    data = read_pending_approval(root, sid, approval_id)
    if data is None:
        print(f"agent-browser approve: 승인 요청 {display_safe(approval_id)} 를 찾을 수 없습니다.",
              file=sys.stderr)
        return 2
    # R2: 사람이 보는 내용은 디스크 파일이 아니라 서버 메모리 기준(파일은 같은 uid 가 고칠 수 있다).
    view, problem = _server_view(root, sid, approval_id)
    if view is None:
        print(f"agent-browser approve: {display_safe(problem, 1000)}", file=sys.stderr)
        return 2
    print("(서버 기록 기준 — 실행 중인 서버의 메모리에서 받은 내용입니다. 창의 확인 코드 옆 "
          "액션·대상과 대조하세요)")
    print(_show_approval(view))
    diff = [k for k in _VIEW_KEYS if data.get(k) != view.get(k)]
    if diff:
        print(f"agent-browser approve: 상태 파일과 서버 기록이 불일치합니다({', '.join(diff)}) — "
              "파일이 바뀌었을 수 있어 승인 절차를 시작하지 않습니다. 위 서버 기록을 확인하세요.",
              file=sys.stderr)
        return 2
    data = view
    if data.get("status") != "pending":
        print(f"승인 대기 상태가 아닙니다: {display_safe(data.get('status'))}")
        return 1
    digest = data.get("action_digest")
    if deny:
        ack = _send(root, sid, "deny", approval_id=approval_id, action_digest=digest)
        if ack is None:
            print("agent-browser approve: 서버 응답 없음(5초)", file=sys.stderr)
            return 2
        print(display_safe(ack.get("message", ""), 1000))
        return 1
    if code is None:
        if yes:
            print("agent-browser approve: --yes 만으로는 승인할 수 없습니다 — 브라우저 창에 뜨는 "
                  "확인 코드가 필요합니다. 코드 없이 실행해 창에 코드를 띄운 뒤 --code 로 "
                  "입력하세요.", file=sys.stderr)
            return 2
        ack = _send(root, sid, "show_code", timeout_s=10.0, approval_id=approval_id,
                    action_digest=digest)
        if ack is None:
            print("agent-browser approve: 서버 응답 없음(10초)", file=sys.stderr)
            return 2
        print(display_safe(ack.get("message", ""), 1000))
        if not ack.get("ok"):
            return 1
        if not _stdin_is_tty():
            print(f"agent-browser approve: 창의 코드를 보고 `agent-browser approve "
                  f"{display_safe(approval_id)} --code <코드>` 로 다시 실행하세요.", file=sys.stderr)
            return 2
        code = input("브라우저 창에 뜬 확인 코드(빈칸=취소): ").strip()
        if not code:
            print("취소했습니다(승인하지 않음).")
            return 1
    ack = _send(root, sid, "approve", approval_id=approval_id, action_digest=digest,
                code=str(code).strip())
    if ack is None:
        print("agent-browser approve: 서버 응답 없음(5초)", file=sys.stderr)
        return 2
    print(display_safe(ack.get("message", ""), 1000))
    return 0 if ack.get("ok") else 1
