"""로그인 기록(login ledger) — 봇 프로필 안 `login_ledger.json`(0600) (WS-59 정의, WS-58 이 판정에 사용).

도구가 아는 로그인 사건만 origin 단위로 적는다. 형식(version 1)::

    {"version": 1,
     "origins": {"https://example.com": {"source": "chrome_import", "at": 1760000000.0}}}

* origin 은 `scheme://host[:port]`(기본 포트 생략, 호스트 소문자). 경로·사용자 정보는 담지 않는다.
* source 는 사건 종류(`chrome_import`·`login_flow`·`human_control`·… — 영문 소문자·밑줄 1~32자).
* at 은 기록 시각(epoch 초). 같은 origin 을 다시 적으면 source·at 을 새것으로 바꾼다.
* 쿠키 이름·값·계정은 적지 않는다. 판정(재시도 거부 등)은 이 모듈 몫이 아니다(WS-58).

안전: 파일·자리가 심볼릭 링크면 거부, 쓰기는 같은 폴더 임시 파일(0600) → fsync → rename(원자적).
깨진 파일은 빈 기록으로 보지 않고 LedgerError — 판정하는 쪽이 보수적으로 처리하게 하고, 덮어써서 기록을 잃지
않는다. 상한 MAX_ORIGINS 를 넘으면 오래된(at 작은) origin 부터 잊는다.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Union
from urllib.parse import urlsplit

LEDGER_FILE = "login_ledger.json"
VERSION = 1
#: 기록하는 origin 수 상한(넘으면 오래된 것부터 잊는다).
MAX_ORIGINS = 1000
#: 읽기 상한(바이트) — 이보다 크면 깨진 것으로 본다.
MAX_BYTES = 1024 * 1024

SOURCE_CHROME_IMPORT = "chrome_import"
SOURCE_LOGIN_FLOW = "login_flow"
SOURCE_HUMAN_CONTROL = "human_control"

#: 기록 한 항목 — {"source": str, "at": float}.
Entry = Dict[str, Union[str, float]]

_SOURCE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_HOST_RE = re.compile(r"[a-z0-9.\-\[\]:]+")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class LedgerError(ValueError):
    """로그인 기록을 읽거나 쓸 수 없음(깨진 파일·심볼릭 링크·잘못된 origin/source)."""


def normalize_origin(origin: str) -> str:
    """`https://Example.com:443/path` → `https://example.com`. http/https·호스트 필수·사용자 정보 거부."""
    try:
        parts = urlsplit(str(origin or "").strip())
        port = parts.port
    except ValueError:
        raise LedgerError("origin 형식이 잘못됐습니다.") from None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host or parts.username or parts.password:
        raise LedgerError("origin 은 사용자 정보 없는 http/https 주소여야 합니다.")
    if not _HOST_RE.fullmatch(host):
        raise LedgerError("origin 호스트 형식이 잘못됐습니다.")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _path(profile: Path) -> Path:
    return Path(profile) / LEDGER_FILE


def _validate(data: object) -> Dict[str, Entry]:
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("origins"), dict):
        raise LedgerError("로그인 기록 형식이 맞지 않습니다(version 1).")
    out: Dict[str, Entry] = {}
    for origin, entry in data["origins"].items():
        if (not isinstance(origin, str) or not isinstance(entry, dict)
                or not isinstance(entry.get("source"), str) or not _SOURCE_RE.fullmatch(entry["source"])
                or not isinstance(entry.get("at"), (int, float)) or isinstance(entry.get("at"), bool)):
            raise LedgerError("로그인 기록 항목 형식이 맞지 않습니다.")
        out[origin] = {"source": entry["source"], "at": float(entry["at"])}
    return out


def load(profile: Path) -> Dict[str, Entry]:
    """origin → {source, at}. 파일이 없으면 빈 dict, 깨졌거나 심볼릭 링크면 LedgerError."""
    path = _path(profile)
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError:
        raise LedgerError("로그인 기록 파일을 열 수 없습니다(심볼릭 링크·권한).") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            raise LedgerError("로그인 기록 파일이 일반 파일이 아니거나 너무 큽니다.")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw = fh.read(MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LedgerError("로그인 기록 파일이 깨졌습니다.") from None
    return _validate(data)


def lookup(profile: Path, origin: str) -> Optional[Entry]:
    """그 origin(정규화)의 기록, 없으면 None."""
    return load(profile).get(normalize_origin(origin))


def record(profile: Path, origins: Iterable[str], *, source: str, at: Optional[float] = None) -> None:
    """origins 를 source 로 기록한다(원자적 교체, 0600). 기존 파일이 깨졌으면 덮어쓰지 않고 LedgerError."""
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise LedgerError("source 는 영문 소문자·숫자·밑줄 1~32자여야 합니다.")
    keys = [normalize_origin(o) for o in origins]
    when = float(time.time() if at is None else at)
    profile = Path(profile)
    try:
        st = os.lstat(profile)
    except OSError:
        raise LedgerError("프로필 폴더가 없습니다.") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise LedgerError("프로필 자리가 일반 폴더가 아닙니다.")
    current = load(profile)
    for key in keys:
        current.pop(key, None)
        current[key] = {"source": source, "at": when}
    if len(current) > MAX_ORIGINS:
        ordered = sorted(current.items(), key=lambda kv: float(kv[1]["at"]))  # 안정 정렬 — 같은 시각은 삽입 순
        current = dict(ordered[len(current) - MAX_ORIGINS:])
    body = json.dumps({"version": VERSION, "origins": current}, ensure_ascii=True, indent=1).encode("ascii")
    tmp = profile / f".{LEDGER_FILE}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, _path(profile))
    except OSError:
        raise LedgerError("로그인 기록을 쓰지 못했습니다.") from None
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
