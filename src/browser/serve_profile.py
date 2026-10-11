"""serve 의 이름 붙인 영속 프로필 (WS-32) — `agent-browser serve --profile NAME`.

serve 는 원래 매번 빈 컨텍스트로 시작해 서버를 다시 켜면 로그인이 사라졌다. 이 모듈은
이름 하나에 프로필 폴더 하나(<root>/serve-<NAME>)를 주고, Playwright
``launch_persistent_context`` 가 그 폴더를 통째로 쓰게 한다(BrowserCore.open_persistent).

왜 폴더 통째인가: site_session 모듈 주석의 실측 — 네이버는 쿠키만 옮겨 담은
storage_state 세션을 다음 실행에서 거부했다. 폴더에는 IndexedDB·기기 신뢰 등도 남는다.

재사용: 루트 기본값은 site_session.DEFAULT_PROFILE_ROOT(~/.agent-browser/profiles, 사이트별
폴더와 같은 곳 — 이름 앞에 `serve-` 를 붙여 섞이지 않게), 평소 Chrome 프로필 거부는
user_chrome._is_user_default_profile, 폴더 권한 700 은 site_session.open_site_context 와 같다.

잠금: 같은 프로필을 두 serve 가 동시에 쓰면 Chromium 이 늦게 연 쪽을 이상한 오류로 죽인다.
폴더 안 잠금 파일에 flock(배타)을 걸고 서버 id 를 적는다 — 프로세스가 죽으면(SIGKILL 포함)
OS 가 잠금을 푼다. 잠금이 없는데 Chromium 의 SingletonLock 이 살아 있는 pid 를 가리키면
(우리 잠금 밖의 Chromium) 그것도 사용 중으로 본다.

한계: 프로필 폴더의 쿠키는 평문이다(Playwright Chromium 은 OS 키체인 암호화를 쓰지 않는다).
폴더 권한 700 으로만 보호한다(README '로그인 유지' 절).
"""

from __future__ import annotations

import datetime as _dt
import errno
import fcntl
import json
import os
import re
import shutil
import socket
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 테스트·하네스가 임시 폴더를 쓰게 하는 환경변수(실제 홈 폴더 무접촉).
PROFILE_ROOT_ENV = "AGENT_BROWSER_PROFILE_ROOT"
#: serve 프로필 폴더 이름 앞머리 — 같은 루트의 사이트별 폴더(example.com)와 섞이지 않게.
DIR_PREFIX = "serve-"
#: 폴더 안 잠금 파일(flock + 서버 id).
LOCK_FILE = ".agent-browser-serve.lock"
_NAME_RE = re.compile(r"[a-z0-9-]{1,32}")


class ProfileError(ValueError):
    """프로필 이름·위치가 잘못됨(경로 탈출, 평소 Chrome 프로필 등)."""


class ProfileInUseError(RuntimeError):
    """같은 프로필을 다른 serve(또는 Chromium)가 쓰는 중."""

    def __init__(self, name: str, info: Dict[str, Any]) -> None:
        self.name = name
        self.info = dict(info)
        if info.get("server_id"):
            who = f"서버 {info['server_id']}"
        elif info.get("chromium_pid"):
            who = f"Chromium pid {info['chromium_pid']}"
        else:
            who = "다른 프로세스"
        super().__init__(
            f"프로필 {name!r} 를 {who} 가 쓰는 중입니다 — 같은 프로필은 한 서버만 쓸 수 있습니다. "
            "그 서버를 끝내거나 다른 --profile 이름을 쓰세요(`agent-browser profile list`)."
        )


def profile_root() -> Path:
    raw = os.environ.get(PROFILE_ROOT_ENV)
    if raw:
        return Path(raw).expanduser()
    from browser.site_session import DEFAULT_PROFILE_ROOT

    return DEFAULT_PROFILE_ROOT


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ProfileError(
            f"프로필 이름은 영문 소문자·숫자·하이픈 1~32자만 됩니다: {name!r}"
        )
    return name


def profile_dir(name: Any, *, root: Optional[Path] = None) -> Path:
    """이름 → 폴더(<root>/serve-<NAME>). 만들지 않는다. 평소 Chrome 프로필·user-chrome 폴더는 거부."""
    from browser import user_chrome

    validate_name(name)
    path = Path(root or profile_root()).expanduser() / f"{DIR_PREFIX}{name}"
    if user_chrome._is_user_default_profile(path):
        raise ProfileError(
            "사용자 평소 Chrome 프로필 아래는 쓸 수 없습니다(쿠키·비밀번호 보호). "
            f"{PROFILE_ROOT_ENV} 를 확인하세요."
        )
    dedicated = user_chrome._fold(user_chrome.DEFAULT_PROFILE_DIR.expanduser().resolve())
    if user_chrome._fold(path.resolve())[: len(dedicated)] == dedicated:
        raise ProfileError(
            "user-chrome 전용 프로필(~/.agent-browser/chrome-profile) 아래는 쓸 수 없습니다 — "
            "그 폴더는 `--browser user-chrome` 몫입니다."
        )
    return path


_ABS_PATH_RE = re.compile(r"(?<![\w.~-])(?:~|/)[^\s'\"()\[\],;]*")


def safe_reason(exc: BaseException, limit: int = 160) -> str:
    """로그·오류 문구용 짧은 이유: 예외 종류 + 첫 줄, 경로(절대·~ 경로)는 지운다.

    Playwright·OS 예외 문자열에는 프로필 폴더 경로(--user-data-dir=...)가 들어 있을 수 있다.
    """
    first = (str(exc).strip().splitlines() or [""])[0]
    first = _ABS_PATH_RE.sub("<경로>", first).strip()
    if len(first) > limit:
        first = first[:limit] + "…"
    name = type(exc).__name__
    return f"{name}: {first}" if first else name


def _os_reason(exc: OSError) -> str:
    """OSError → 사람이 읽을 짧은 이유(경로 없음 — filename 은 쓰지 않는다)."""
    if exc.errno == errno.ELOOP:
        return "심볼릭 링크가 끼어 있습니다"
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return f"권한이 없습니다({errno.errorcode.get(exc.errno, exc.errno)})"
    code = errno.errorcode.get(exc.errno or 0, "")
    text = exc.strerror or type(exc).__name__
    return f"{text}({code})" if code else text


def _refuse_symlink(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ProfileError(f"프로필 자리가 일반 폴더가 아닙니다(심볼릭 링크 등): {path.name}")


def prepare(path: Path) -> Path:
    """프로필 폴더를 0700 으로 만든다(있으면 0700 으로 조인다). 심볼릭 링크는 거부.

    루트(부모)는 우리가 새로 만들 때만 0700 — 이미 있는 루트(예: $HOME 을 루트로 준 경우)의
    권한은 바꾸지 않는다(R1 NB-8). 보호 단위는 프로필 폴더 자체(0700)다.
    """
    path = Path(path)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        created_root = True
    except FileExistsError:
        created_root = False
    _refuse_symlink(path.parent)
    if created_root:
        os.chmod(path.parent, 0o700)  # umask 가 mode 를 깎았을 수 있다
    _refuse_symlink(path)
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


# ---------------------------------------------------------------------------- 잠금


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 다른 uid 가 살아 있다 — 사용 중으로 본다(보수적)
    except OverflowError:
        return False
    return True


def _chromium_holder(path: Path) -> Optional[int]:
    """Chromium SingletonLock(심볼릭 링크 '<host>-<pid>')이 이 컴퓨터의 살아 있는 pid 면 그 pid."""
    try:
        target = os.readlink(path / "SingletonLock")
    except OSError:
        return None
    host, _, pid_s = target.rpartition("-")
    if host != socket.gethostname() or not pid_s.isdigit():
        return None
    pid = int(pid_s)
    return pid if _pid_alive(pid) else None


def _read_lock_info(path: Path) -> Dict[str, Any]:
    try:
        fd = os.open(path / LOCK_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _try_flock(path: Path) -> Optional[int]:
    """잠금 파일을 열고 배타 flock 을 건다. 성공하면 fd, 남이 쥐고 있으면 None."""
    fd = os.open(path / LOCK_FILE, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    os.fchmod(fd, 0o600)
    return fd


def _lock_held_by_other(path: Path) -> bool:
    """잠금 파일에 배타 잠금(=serve)이 걸려 있는지 — 공유 잠금으로만 들여다본다.

    R1 NB-1: 예전에는 배타 잠금을 잠깐 쥐었다 → 그 순간 시작한 serve 가 '사용 중' 으로 거짓
    거부됐다(검증 실측 179/3000). 공유 잠금은 다른 들여다보기(`profile list`)와 겹치지 않고,
    serve 의 acquire() 는 짧게 재시도해 이 순간을 흡수한다.
    """
    try:
        fd = os.open(path / LOCK_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProfileError(f"프로필 {path.name[len(DIR_PREFIX):]!r} 의 잠금 파일을 열 수 없습니다: "
                           f"{_os_reason(exc)}") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return True
        raise ProfileError(f"프로필 잠금을 확인할 수 없습니다: {_os_reason(exc)}") from None
    finally:
        os.close(fd)  # 닫으면 공유 잠금도 풀린다
    return False


def holder(name: str, *, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """지금 이 프로필을 쓰는 쪽. 아무도 안 쓰면 None. {server_id?, pid?, chromium_pid?}.

    잠금 파일이 심볼릭 링크 등이라 판정할 수 없으면 ProfileError.
    """
    path = profile_dir(name, root=root)
    if not path.is_dir() or path.is_symlink():
        return None
    if _lock_held_by_other(path):
        info = _read_lock_info(path)
        return {k: info[k] for k in ("server_id", "pid") if k in info} or {"server_id": None}
    pid = _chromium_holder(path)
    return {"chromium_pid": pid} if pid else None


@dataclass
class ProfileLock:
    """serve 가 쥐는 프로필 잠금. release() 하거나 프로세스가 끝나면 풀린다."""

    name: str
    path: Path
    fd: Optional[int]

    @property
    def held(self) -> bool:
        return self.fd is not None

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)  # 다음 사람이 옛 서버 id 를 읽지 않게
            os.utime(self.path / LOCK_FILE)  # '마지막 사용' = 반납 시각
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


#: 배타 잠금 재시도 간격(초) — `profile list` 가 잠깐 들여다보는 순간(공유 잠금)을 흡수한다.
#: 진짜 사용 중(다른 serve)이면 합계 약 0.15초 뒤 그대로 거부된다.
_ACQUIRE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08)


def acquire(name: str, *, server_id: str, root: Optional[Path] = None) -> ProfileLock:
    """프로필 폴더를 만들고(0700) 잠근다. 다른 serve·Chromium 이 쓰는 중이면 ProfileInUseError.

    폴더·잠금 파일을 준비하지 못하면(심볼릭 링크·권한 등) 경로 없는 한 줄 이유의 ProfileError.
    """
    try:
        path = prepare(profile_dir(name, root=root))
        fd = _try_flock(path)
        for delay in _ACQUIRE_RETRY_DELAYS:
            if fd is not None:
                break
            time.sleep(delay)
            fd = _try_flock(path)
    except OSError as exc:
        raise ProfileError(f"프로필 {name!r} 을 준비하지 못했습니다: {_os_reason(exc)}") from None
    if fd is None:
        raise ProfileInUseError(name, holder(name, root=root) or {})
    try:
        pid = _chromium_holder(path)
        if pid is not None:
            raise ProfileInUseError(name, {"chromium_pid": pid})
        body = json.dumps({"server_id": server_id, "pid": os.getpid(), "started": time.time()})
        os.ftruncate(fd, 0)
        os.pwrite(fd, body.encode("utf-8"), 0)
    except BaseException:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise
    return ProfileLock(name=name, path=path, fd=fd)


# ---------------------------------------------------------------------------- 목록·삭제


def cookie_sites(name: str) -> List[Dict[str, object]]:
    """Read only domain/expiry metadata via a consistent SQLite backup (includes WAL).

    No cookie name/value column is selected. A 0700 scratch directory and 0600
    snapshot are deleted even on failure; a running Chromium keeps its own lock.
    """
    import sqlite3
    import tempfile

    path = profile_dir(name)
    _refuse_symlink(path.parent)
    _refuse_symlink(path)
    if not path.exists():
        raise ProfileError(f"프로필 {name!r} 이 없습니다.")
    candidates = [path / "Default" / "Network" / "Cookies", path / "Default" / "Cookies"]
    source = next((p for p in candidates if p.exists()), None)
    if source is None:
        return []
    for parent in (path / "Default", source.parent):
        _refuse_symlink(parent)
    if source.is_symlink() or not source.is_file():
        raise ProfileError("Cookies DB 가 일반 파일이 아닙니다.")
    scratch = Path.home() / ".hermes" / "cache" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ab-cookie-sites-", dir=scratch) as tmp:
        snapshot = Path(tmp) / "cookies.sqlite"
        snapshot.touch(mode=0o600)
        deadline = time.monotonic() + 2

        def progress(status: int, remaining: int, total: int) -> None:
            # SQLite's backup retries BUSY/LOCKED indefinitely even when the
            # connection timeout is set. Bound retries as well as elapsed time.
            nonlocal retries
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                retries += 1
            if retries >= 20 or time.monotonic() >= deadline:
                raise ProfileError("쿠키 DB 잠금 또는 복사 시간이 초과됐습니다; 잠시 뒤 다시 실행하세요.")

        retries = 0
        try:
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
                with sqlite3.connect(snapshot) as copy:
                    db.backup(copy, pages=256, progress=progress, sleep=0.05)
            with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as copy:
                metadata = copy.execute("SELECT host_key, expires_utc FROM cookies").fetchall()
        except sqlite3.Error:
            raise ProfileError("쿠키 메타데이터를 읽을 수 없습니다(Chromium DB 상태 확인).") from None
    return summarize_cookie_sites(metadata)


def bound_cookie_sites(sites: List[Dict[str, object]]) -> tuple[List[Dict[str, object]], bool]:
    """Use the same 12KB serialized rows limit in Hub and SQLite CLI responses."""
    bounded: List[Dict[str, object]] = []
    size = 0
    for row in sites:
        size += len(json.dumps(row, ensure_ascii=True).encode("ascii")) + 2
        if size > 12000:
            break
        bounded.append(row)
    return bounded, len(bounded) < len(sites)


def summarize_cookie_sites(metadata: List[tuple[str, int]]) -> List[Dict[str, object]]:
    """Domain/expiry only; never accepts cookie names or values."""
    from interface.on_demand import registrable_domain

    grouped: Dict[str, List[int]] = {}
    for host, expires in metadata:
        domain = registrable_domain(str(host).lstrip("."))
        if domain:
            grouped.setdefault(domain, []).append(int(expires))
    rows: List[Dict[str, object]] = []
    for domain, expiries in sorted(grouped.items()):
        latest = max(expiries)
        # Chromium microseconds since 1601-01-01, zero means session cookie.
        try:
            expiry = _dt.datetime.fromtimestamp(latest / 1_000_000 - 11644473600,
                                               _dt.timezone.utc).isoformat() if latest > 0 else None
        except (ValueError, OverflowError, OSError):
            expiry = None
        rows.append({"domain": domain, "expires_at": expiry,
                     "session_only": all(e <= 0 for e in expiries)})
    return rows


def _size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                st = os.lstat(os.path.join(dirpath, f))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def list_profiles(*, root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """serve 프로필 목록: 이름·크기·마지막 사용·사용 중 여부. 경로·쿠키 내용은 담지 않는다."""
    base = Path(root or profile_root()).expanduser()
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for d in entries:
        if not d.name.startswith(DIR_PREFIX):
            continue
        name = d.name[len(DIR_PREFIX):]
        if not _NAME_RE.fullmatch(name) or d.is_symlink() or not d.is_dir():
            continue
        lock = d / LOCK_FILE
        try:
            last = os.stat(lock if lock.exists() else d).st_mtime
        except OSError:
            last = 0.0
        try:
            who = holder(name, root=base)
        except ProfileError:
            # 잠금을 판정할 수 없다(잠금 파일이 심볼릭 링크 등) — '비어 있음' 으로 가정하지 않는다.
            who = {"server_id": None}
        rows.append({
            "name": name,
            "size_bytes": _size(d),
            "last_used": _iso(last) if last else "",
            "in_use": who is not None,
            "server_id": (who or {}).get("server_id"),
            "chromium_pid": (who or {}).get("chromium_pid"),
        })
    return rows


def remove(name: str, *, root: Optional[Path] = None) -> None:
    """프로필 폴더를 지운다. 사용 중이면 ProfileInUseError, 없으면 ProfileError."""
    path = profile_dir(name, root=root)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ProfileError(f"프로필 {name!r} 이 없습니다.") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ProfileError(f"프로필 자리가 일반 폴더가 아닙니다(심볼릭 링크 등): {name!r}")
    # 지우는 동안 다른 serve 가 잡지 못하게 잠금을 쥔 채 지운다.
    lock = acquire(name, server_id="profile-remove", root=root)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise ProfileError(f"프로필 {name!r} 을 다 지우지 못했습니다: {_os_reason(exc)}") from None
    finally:
        lock.release() if path.exists() else _close_quietly(lock)


# ---------------------------------------------------------------------------- 서버 전용 임시 프로필 (WS-34)

#: `serve --browser on-demand` 를 --profile 없이 띄울 때의 서버 전용 임시 프로필 폴더 앞머리.
#: headless ↔ 창 전환은 같은 폴더를 닫고 다시 여는 방식이라(BrowserCore.open_persistent) 이름 붙은
#: 프로필이 없어도 폴더가 필요하다. 서버 종료 때 지우고, 비정상 종료로 남은 것은 다음 기동 때
#: 잠금이 풀린 것만 지운다. `serve-` 가 아니므로 `profile list` 에는 보이지 않는다.
EPHEMERAL_PREFIX = "ondemand-"
_EPHEMERAL_ID_RE = re.compile(r"[0-9]{1,10}-[0-9a-f]{1,32}")


def acquire_ephemeral(*, server_id: str, root: Optional[Path] = None) -> ProfileLock:
    """서버 전용 임시 프로필(<root>/ondemand-<server_id>, 0700)을 만들고 잠근다."""
    if not isinstance(server_id, str) or not _EPHEMERAL_ID_RE.fullmatch(server_id):
        raise ProfileError(f"임시 프로필 서버 id 형식 오류: {server_id!r}")
    base = Path(root or profile_root()).expanduser()
    path = base / f"{EPHEMERAL_PREFIX}{server_id}"
    from browser import user_chrome

    if user_chrome._is_user_default_profile(path):
        raise ProfileError("사용자 평소 Chrome 프로필 아래는 쓸 수 없습니다(쿠키·비밀번호 보호).")
    try:
        prepare(path)
        fd = _try_flock(path)
    except OSError as exc:
        raise ProfileError(f"임시 프로필을 준비하지 못했습니다: {_os_reason(exc)}") from None
    if fd is None:
        raise ProfileInUseError(path.name, {})
    return ProfileLock(name=path.name, path=path, fd=fd)


def remove_ephemeral(lock: ProfileLock) -> None:
    """임시 프로필 폴더를 지우고 잠금을 푼다(브라우저를 닫은 뒤에 부른다). 실패는 조용히 —
    남은 폴더는 다음 기동의 cleanup_ephemeral 이 지운다."""
    try:
        if lock.held:
            shutil.rmtree(lock.path, ignore_errors=True)
    finally:
        _close_quietly(lock)


def cleanup_ephemeral(*, root: Optional[Path] = None) -> List[str]:
    """비정상 종료로 남은 임시 프로필(잠금이 풀린 것)을 지운다. 지운 폴더 이름 목록.

    심볼릭 링크·일반 폴더가 아닌 것·다른 서버가 잠근 것·Chromium 이 쓰는 것은 건드리지 않는다.
    """
    base = Path(root or profile_root()).expanduser()
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    removed: List[str] = []
    for d in entries:
        if not d.name.startswith(EPHEMERAL_PREFIX):
            continue
        if not _EPHEMERAL_ID_RE.fullmatch(d.name[len(EPHEMERAL_PREFIX):]):
            continue
        try:
            st = os.lstat(d)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            continue
        try:
            fd = _try_flock(d)
        except OSError:
            continue
        if fd is None:
            continue  # 살아 있는 서버가 쓰는 중
        try:
            if _chromium_holder(d) is not None:
                continue
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                removed.append(d.name)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    return removed


def _close_quietly(lock: ProfileLock) -> None:
    fd, lock.fd = lock.fd, None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
