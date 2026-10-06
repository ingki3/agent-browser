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


def _refuse_symlink(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ProfileError(f"프로필 자리가 일반 폴더가 아닙니다(심볼릭 링크 등): {path.name}")


def prepare(path: Path) -> Path:
    """프로필 폴더(와 루트)를 0700 으로 만든다. 있으면 0700 으로 조인다. 심볼릭 링크는 거부."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _refuse_symlink(path.parent)
    os.chmod(path.parent, 0o700)
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


def holder(name: str, *, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """지금 이 프로필을 쓰는 쪽. 아무도 안 쓰면 None. {server_id?, pid?, chromium_pid?}."""
    path = profile_dir(name, root=root)
    if not path.is_dir() or path.is_symlink():
        return None
    if (path / LOCK_FILE).exists():
        fd = _try_flock(path)
        if fd is None:
            info = _read_lock_info(path)
            return {k: info[k] for k in ("server_id", "pid") if k in info} or {"server_id": None}
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
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


def acquire(name: str, *, server_id: str, root: Optional[Path] = None) -> ProfileLock:
    """프로필 폴더를 만들고(0700) 잠근다. 다른 serve·Chromium 이 쓰는 중이면 ProfileInUseError."""
    path = prepare(profile_dir(name, root=root))
    fd = _try_flock(path)
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
        who = holder(name, root=base)
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
    finally:
        lock.release() if path.exists() else _close_quietly(lock)


def _close_quietly(lock: ProfileLock) -> None:
    fd, lock.fd = lock.fd, None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
