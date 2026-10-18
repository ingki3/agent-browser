"""일상 Chrome 에서 고른 사이트의 로그인 쿠키만 읽기 (WS-59) — 사람 CLI `profile import-cookies` 전용.

에이전트(MCP)·서버 도구에는 노출하지 않는다. 일상 Chrome 프로필에 **연결하지 않는다**(user_chrome 의 평소 프로필
거부 규칙은 그대로) — 쿠키 DB 파일(`Cookies`, 있으면 곁 파일 `Cookies-journal`·`Cookies-wal`)만 바이트 복사해 읽는다.

읽기
  `<chrome-dir>/<chrome-profile>/Network/Cookies`(없으면 `<…>/Cookies`)와 곁 파일을 임시 0700 폴더로 복사 → 원본을
  같은 순서로 다시 읽어 내용이 같을 때만(한 시점의 일관된 묶음) 받아들이고, 아니면 짧게 다시 시도한 뒤 거부 →
  사본을 sqlite 로 연다(곁 파일이 있으면 SQLite 가 hot rollback journal 을 되돌리고 WAL 의 커밋만 읽도록 사본을
  읽기·쓰기로, 없으면 `mode=ro`) → 끝나면(실패해도) 폴더째 지운다. sqlite 로 원본을 열지 않으므로 Chrome 이 실행
  중이어도(Chrome 은 쿠키 DB 를 배타 잠금으로 연다) 동작하고 원본을 바꾸지 않는다. 비밀번호·기록 등 다른 파일은
  열지 않는다. `--chrome-dir`(기준점, 포함)부터 DB·곁 파일까지 경로 구성요소가 하나라도 심볼릭 링크면 거부한다
  (기준점 위의 OS 링크 — 예: macOS `/var` → `/private/var` — 는 검사하지 않는다).

복호화(macOS, 공개된 형식)
  키체인 'Chrome Safe Storage' 비밀 → PBKDF2-HMAC-SHA1(salt `saltysalt`, 1003회, 16바이트) → AES-128-CBC
  (IV 공백 16, PKCS7), 값 앞 `v10`. DB `meta.version` ≥ 24 이면 평문 앞 32바이트가 SHA256(host_key) 이고 그것을
  확인해 떼어 낸다. 접두 `v10` 이 아닌 값(v11·v20 등)·meta 버전 불명·해시 불일치·복호화 실패는 그 쿠키만 건너뛰고
  이유별 개수만 보고한다(fail-closed). 키체인은 고른 사이트에 암호화된 쿠키가 있을 때만 한 번 묻는다.

필터
  `--site` 는 등록 도메인(공용 접미사 아님, 하위 도메인 아님)만. host_key 가 그 도메인과 같거나 `.`+도메인으로
  끝날 때만(라벨 경계) — `evil-example.com`·`example.com.evil.test` 는 제외. 만료·세션(브라우저를 닫으면 사라짐)·
  파티션(CHIPS)·SameSite=None 인데 secure 아님은 건너뛴다.

쓰기
  봇 프로필(serve_profile.profile_dir)을 serve 가 쓰는 중이면 거부. 아니면 프로필 잠금을 쥐고 Playwright 영속
  컨텍스트를 headless 로 잠깐 열어 add_cookies(만료·secure·httpOnly·sameSite 보존) → 저장소를 다시 읽어 name/domain/
  path 와 값·secure·httpOnly·sameSite·만료(1초 오차, Chromium 400일 상한)가 요청과 같은 것만 성공으로 센다(같은 키의
  기존 쿠키가 남은 거부는 따로 센다) → 닫는다. 확인된 쿠키가 있는 사이트만 로그인 기록(login_ledger, source
  `chrome_import`)에 `https://<site>` 로 적는다.

비노출
  쿠키 값·키체인 비밀·복호화 키는 출력·로그·예외 문구에 싣지 않는다. 값은 repr 에서 숨기고, 읽은 값은 프로세스
  비밀 값 레지스트리(WS-51)에 등록해 CLI 출력 줄을 한 번 더 가린다. 레지스트리는 값 256개 상한이라, CLI 는 이번
  import 가 읽은 **모든** 값의 로컬 집합(ValueGuard)으로 출력 직전에 다시 검사한다 — 값이 든 쿠키 이름은 통째로
  `[REDACTED]`. 예외는 종류만 남기고 원 예외 연쇄를 끊는다.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

#: 사람 평소 Chrome 데이터 폴더(macOS 기본).
DEFAULT_CHROME_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
DEFAULT_CHROME_PROFILE = "Default"
KEYCHAIN_SERVICE = "Chrome Safe Storage"

#: 1601-01-01 → 1970-01-01 (초) — Chrome 시각은 1601 기준 마이크로초.
_EPOCH_DELTA_S = 11644473600
#: 이 meta 버전부터 평문 앞에 SHA256(host_key) 32바이트가 붙는다.
DOMAIN_HASH_META_VERSION = 24

SKIP_UNKNOWN = "unknown_format"
SKIP_DECRYPT = "decrypt_failed"
SKIP_EXPIRED = "expired"
SKIP_SESSION = "session"
SKIP_PARTITIONED = "partitioned"
SKIP_REJECTED = "rejected"
SKIP_KEPT_EXISTING = "rejected_kept_existing"
#: 사람에게 보일 건너뜀 이유(고정 문구).
SKIP_LABELS: Dict[str, str] = {
    SKIP_UNKNOWN: "알 수 없는 형식",
    SKIP_DECRYPT: "복호화 실패",
    SKIP_EXPIRED: "만료",
    SKIP_SESSION: "세션 쿠키(브라우저를 닫으면 사라져 가져오지 않음)",
    SKIP_PARTITIONED: "파티션 쿠키",
    SKIP_REJECTED: "브라우저가 거부",
    SKIP_KEPT_EXISTING: "브라우저가 거부 — 같은 이름의 기존 쿠키가 그대로 남음",
}

#: 등록 도메인으로 받지 않는 공용 접미사(근사 — 공용 접미사 목록 라이브러리가 의존성에 없다).
#: 한 라벨(`com`·`test`)은 라벨 수 규칙으로 거부한다.
PUBLIC_SUFFIXES = frozenset({
    # 국가 2단계
    "co.kr", "or.kr", "go.kr", "ac.kr", "ne.kr", "re.kr", "pe.kr", "ms.kr", "hs.kr", "es.kr", "sc.kr",
    "kg.kr", "mil.kr", "seoul.kr",
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "ltd.uk", "plc.uk", "sch.uk",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "ad.jp", "ed.jp", "gr.jp", "lg.jp",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "com.br", "net.br", "org.br", "com.cn", "net.cn",
    "org.cn", "gov.cn", "com.tw", "org.tw", "com.hk", "org.hk", "co.nz", "org.nz", "co.in", "net.in",
    "org.in", "com.sg", "com.my", "co.id", "co.th", "com.vn", "com.mx", "com.ar", "com.tr", "co.za",
    # 사설(사용자가 하위 도메인을 받는 호스팅)
    "github.io", "gitlab.io", "herokuapp.com", "vercel.app", "netlify.app", "pages.dev", "workers.dev",
    "web.app", "firebaseapp.com", "appspot.com", "blogspot.com", "cloudfront.net", "azurewebsites.net",
    "glitch.me", "fly.dev", "onrender.com", "ngrok.io", "ngrok-free.app", "trycloudflare.com",
    "s3.amazonaws.com", "elasticbeanstalk.com", "readthedocs.io", "surge.sh", "now.sh", "myshopify.com",
    "wixsite.com", "repl.co", "replit.dev", "deno.dev", "cloudflareaccess.com",
})

_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_CHROME_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_SAMESITE = {0: "None", 1: "Lax", 2: "Strict"}


class CookieImportError(ValueError):
    """가져오기를 진행할 수 없음 — 문구에는 경로·값·비밀을 싣지 않는다."""


# ---------------------------------------------------------------------------- 사이트 이름·필터


def validate_site(site: object) -> str:
    """`--site` → 소문자 등록 도메인. 공용 접미사·하위 도메인·IP·한 라벨·잘못된 형식은 CookieImportError."""
    raw = site if isinstance(site, str) else ""
    text = raw.strip().lower()
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    shown = raw.strip()[:80]
    if not text or not text.isascii():
        raise CookieImportError(f"--site 는 영문 도메인(국제화 도메인은 xn-- 형식)이어야 합니다: {shown!r}")
    try:
        ipaddress.ip_address(text)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        raise CookieImportError(f"--site 에 IP 주소는 쓸 수 없습니다: {shown!r}")
    labels = text.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.fullmatch(lb) for lb in labels) or labels[-1].isdigit():
        raise CookieImportError(f"--site 는 example.com 같은 도메인 이름이어야 합니다: {shown!r}")
    if text in PUBLIC_SUFFIXES:
        raise CookieImportError(
            f"{text} 는 여러 사이트가 함께 쓰는 공용 접미사라 가져올 수 없습니다 — 그 아래 사이트 이름을 주세요.")
    parent = ".".join(labels[1:])
    if len(labels) >= 3 and parent not in PUBLIC_SUFFIXES:
        raise CookieImportError(
            f"--site 는 등록 도메인으로 주세요(하위 도메인 쿠키도 함께 가져옵니다): {text} → {parent}")
    return text


def host_in_site(host_key: object, site: str) -> bool:
    """Chrome host_key(`example.com`·`.example.com`·`a.example.com`)가 site 와 같거나 그 하위인가(라벨 경계)."""
    if not isinstance(host_key, str):
        return False
    host = host_key.lower()
    if host.startswith("."):
        host = host[1:]
    if not host or host.startswith("."):
        return False
    return host == site or host.endswith("." + site)


# ---------------------------------------------------------------------------- 쿠키 모델


@dataclass
class ChromeCookie:
    host_key: str
    name: str
    value: str = field(repr=False)
    path: str
    #: unix 초, 세션은 -1(가져오지 않지만 형식상).
    expires: float
    secure: bool
    http_only: bool
    same_site: Optional[str]

    def __str__(self) -> str:
        return repr(self)

    def to_playwright(self) -> Dict[str, Union[str, float, bool]]:
        out: Dict[str, Union[str, float, bool]] = {
            "name": self.name, "value": self.value, "domain": self.host_key, "path": self.path,
            "expires": self.expires, "secure": self.secure, "httpOnly": self.http_only,
        }
        if self.same_site is not None:
            out["sameSite"] = self.same_site
        return out


@dataclass
class SitePlan:
    site: str
    cookies: List[ChromeCookie] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)

    def names(self) -> List[str]:
        return sorted({c.name for c in self.cookies})


@dataclass
class ImportPlan:
    sites: Dict[str, SitePlan]
    #: 이번 가져오기가 읽은(복호화·평문) **모든** 값 — 건너뛴 쿠키 값 포함. 출력 최종 검사(ValueGuard) 몫.
    read_values: List[str] = field(default_factory=list)

    def values(self) -> List[str]:
        return [c.value for p in self.sites.values() for c in p.cookies]

    def guard(self) -> "ValueGuard":
        return ValueGuard(list(self.read_values) + self.values())

    def __repr__(self) -> str:
        parts = ", ".join(f"{s}: {len(p.cookies)} cookies" for s, p in self.sites.items())
        return f"<ImportPlan {parts} (values hidden)>"

    __str__ = __repr__


# ---------------------------------------------------------------------------- 키체인·복호화


def keychain_password() -> bytes:
    """macOS 키체인의 'Chrome Safe Storage' 비밀. 사람에게 키체인 허용 창이 뜨는 것이 정상이다.

    실패 문구에는 `security` 의 출력·오류를 싣지 않는다.
    """
    if sys.platform != "darwin":
        raise CookieImportError("Chrome 쿠키 가져오기는 macOS 에서만 됩니다(키체인 'Chrome Safe Storage').")
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise CookieImportError("키체인을 열지 못했습니다(`security` 명령 실행 실패).") from None
    secret = (res.stdout or b"").rstrip(b"\r\n")
    if res.returncode != 0 or not secret:
        raise CookieImportError(
            f"키체인에서 '{KEYCHAIN_SERVICE}' 비밀을 읽지 못했습니다 — 허용 창에서 '거부'했거나 Chrome 이 "
            "설치돼 있지 않습니다. 다시 실행해 '허용'을 누르세요.")
    return secret


def derive_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)


class _Skip(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _decrypt_v10(blob: bytes, key: bytes, host_key: str, meta_version: Optional[int]) -> str:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if meta_version is None:
        raise _Skip(SKIP_UNKNOWN)
    body = blob[3:]
    if not body or len(body) % 16:
        raise _Skip(SKIP_DECRYPT)
    try:
        dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        padded = dec.update(body) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
    except Exception:  # noqa: BLE001 - 잘못된 키·패딩: 값·키를 싣지 않고 이유만
        raise _Skip(SKIP_DECRYPT) from None
    if meta_version >= DOMAIN_HASH_META_VERSION:
        if len(plain) < 32 or plain[:32] != hashlib.sha256(host_key.encode("utf-8")).digest():
            raise _Skip(SKIP_UNKNOWN)
        plain = plain[32:]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        raise _Skip(SKIP_DECRYPT) from None


# ---------------------------------------------------------------------------- DB 찾기·복사·읽기


def _refuse_symlink(path: Path, what: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise CookieImportError(f"{what} 이(가) 없습니다.") from None
    except OSError:
        raise CookieImportError(f"{what} 을(를) 확인할 수 없습니다.") from None
    if stat.S_ISLNK(st.st_mode):
        raise CookieImportError(f"{what} 이(가) 심볼릭 링크라 읽지 않습니다.")


def _anchor(chrome_dir: Path) -> Path:
    """기준점 — 사용자가 준(또는 기본) `--chrome-dir` 그 자체(절대 경로, 링크를 풀지 않음)."""
    return Path(os.path.abspath(Path(chrome_dir).expanduser()))


def _refuse_link_chain(chrome_dir: Path, target: Path) -> None:
    """기준점(`--chrome-dir`, 포함)부터 target(포함)까지 모든 경로 구성요소를 lstat — 링크면 거부(R4).

    기준점 위(예: 홈·macOS `/var` → `/private/var`·`/tmp` 같은 OS 링크)는 사용자가 고른 경로라 검사하지 않는다.
    target 이 기준점 아래가 아니면 거부한다.
    """
    anchor = _anchor(chrome_dir)
    full = Path(os.path.abspath(target))
    try:
        rel = full.relative_to(anchor)
    except ValueError:
        raise CookieImportError("Chrome 쿠키 DB 가 --chrome-dir 아래에 있지 않습니다.") from None
    if any(part in ("", ".", "..") for part in rel.parts):
        raise CookieImportError("Chrome 쿠키 DB 경로 형식이 잘못됐습니다.")
    _refuse_symlink(anchor, "Chrome 데이터 폴더(--chrome-dir)")
    cur = anchor
    for part in rel.parts:
        cur = cur / part
        _refuse_symlink(cur, "Chrome 쿠키 DB 경로의 폴더·파일")


def find_cookie_db(chrome_dir: Path, chrome_profile: str = DEFAULT_CHROME_PROFILE) -> Path:
    """`<chrome_dir>/<profile>/Network/Cookies` 또는 `<…>/Cookies`. 프로필 이름은 폴더 이름 한 칸만.

    기준점(`chrome_dir`)부터 DB 파일까지 경로 구성요소가 하나라도 심볼릭 링크면 거부한다(R4).
    """
    if not isinstance(chrome_profile, str) or not _CHROME_PROFILE_RE.fullmatch(chrome_profile) \
            or chrome_profile.strip() != chrome_profile:
        raise CookieImportError("--chrome-profile 은 'Default'·'Profile 1' 같은 폴더 이름 하나여야 합니다.")
    base = _anchor(chrome_dir) / chrome_profile
    for cand in (base / "Network" / "Cookies", base / "Cookies"):
        if os.path.lexists(cand):
            _refuse_link_chain(chrome_dir, cand)
            return cand
    raise CookieImportError(
        f"Chrome 쿠키 DB(Cookies)를 찾지 못했습니다 — --chrome-dir·--chrome-profile({chrome_profile})을 확인하세요.")


def _copy_nofollow(src: Path, dst: Path) -> str:
    """src 를 dst(0600, 새 파일)로 바이트 복사하고 내용의 SHA256 을 돌려준다(링크·일반 파일 아님은 거부)."""
    digest = hashlib.sha256()
    fd = os.open(src, os.O_RDONLY | _NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CookieImportError("Chrome 쿠키 DB 가 일반 파일이 아닙니다.")
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        try:
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                os.write(out, chunk)
        finally:
            os.close(out)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _hash_file(src: Path) -> Optional[str]:
    """원본을 다시 읽어 SHA256(없으면 None). 링크·일반 파일 아님은 거부."""
    try:
        fd = os.open(src, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CookieImportError("Chrome 쿠키 DB 가 일반 파일이 아닙니다.")
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


#: 원본 DB 와 함께 복사하는 SQLite 곁 파일 — rollback journal(R2)·WAL.
_SIDE_SUFFIXES = ("-journal", "-wal")
#: 일관된 사본을 못 얻으면 이 간격으로 다시 시도한다(합계 약 0.75초) — 그래도 안 되면 거부.
_SNAPSHOT_DELAYS = (0.05, 0.1, 0.2, 0.4)


def _snapshot(db_path: Path, tmp: Path) -> bool:
    """원본 DB 와 곁 파일(-journal·-wal)을 tmp 로 복사해 **한 시점의 일관된 묶음**을 만든다(R2).

    복사(DB → journal → wal) 직후 같은 순서로 원본을 다시 읽어 내용(SHA256)이 모두 같을 때만 받아들인다 — 복사 중
    Chrome 이 쓰면(커밋·체크포인트·cache spill) 다시 시도하고, 끝내 안정되지 않으면 CookieImportError.
    곁 파일까지 함께 복사한 사본은 SQLite 가 열 때 스스로 처리한다: 살아 있는(hot) rollback journal 은 되돌려
    **커밋된 내용만** 남기고, WAL 은 커밋된 프레임만 읽는다. 원본은 열기만 하고(읽기 전용·O_NOFOLLOW) 바꾸지 않는다.
    곁 파일을 하나라도 복사했으면 True(사본을 읽기·쓰기로 열어야 되돌리기가 된다).
    """
    sides = [(s, db_path.with_name(db_path.name + s)) for s in _SIDE_SUFFIXES]
    for attempt in range(len(_SNAPSHOT_DELAYS) + 1):
        first: Dict[str, Optional[str]] = {"": _copy_nofollow(db_path, tmp / "Cookies")}
        for suffix, src in sides:
            if os.path.lexists(src):
                _refuse_symlink(src, "Chrome 쿠키 DB 곁 파일(-journal·-wal)")
                try:
                    first[suffix] = _copy_nofollow(src, tmp / ("Cookies" + suffix))
                except FileNotFoundError:
                    first[suffix] = None
            else:
                first[suffix] = None
        second: Dict[str, Optional[str]] = {"": _hash_file(db_path)}
        for suffix, src in sides:
            second[suffix] = _hash_file(src)
        if first == second:
            return any(first[s] is not None for s, _ in sides)
        for child in list(tmp.iterdir()):
            child.unlink()
        if attempt < len(_SNAPSHOT_DELAYS):
            time.sleep(_SNAPSHOT_DELAYS[attempt])
    raise CookieImportError(
        "Chrome 이 쿠키 DB 를 계속 쓰고 있어 일관된 사본을 얻지 못했습니다 — 잠시 뒤 다시 실행하세요.")


def _columns(db: sqlite3.Connection) -> List[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(cookies)").fetchall()]


_WANTED = ("host_key", "name", "value", "encrypted_value", "path", "expires_utc", "is_secure",
           "is_httponly", "has_expires", "is_persistent", "samesite", "top_frame_site_key")
_REQUIRED = ("host_key", "name", "value", "encrypted_value", "path", "expires_utc", "is_secure", "is_httponly")
Row = Tuple[object, ...]


def _read_rows(db_path: Path, tmp_root: Optional[Path],
               chrome_dir: Optional[Path] = None) -> Tuple[Optional[int], List[str], List[Row]]:
    if chrome_dir is None:
        # 기준점을 주지 않으면 Chrome 배치(`<chrome-dir>/<profile>/[Network/]Cookies`)에서 데이터 폴더를 기준점으로 본다.
        full = Path(os.path.abspath(db_path))
        chrome_dir = full.parents[2] if full.parent.name == "Network" else full.parents[1]
    _refuse_link_chain(chrome_dir, db_path)  # find_cookie_db 뒤 바뀌었을 수 있다 — 복사 직전 다시
    tmp = Path(tempfile.mkdtemp(prefix="ab-chrome-cookies-", dir=str(tmp_root) if tmp_root else None))
    try:
        os.chmod(tmp, 0o700)
        copy = tmp / "Cookies"
        try:
            with_sides = _snapshot(db_path, tmp)
        except OSError:
            raise CookieImportError("Chrome 쿠키 DB 를 복사하지 못했습니다(권한·파일 상태 확인).") from None
        # 곁 파일이 있으면 사본(우리 0700 폴더 안)을 읽기·쓰기로 연다 — SQLite 가 hot journal 을 되돌리고 WAL 을
        # 읽게. 원본은 sqlite 로 열지 않는다.
        mode = "rw" if with_sides else "ro"
        try:
            with sqlite3.connect(copy.as_uri() + f"?mode={mode}", uri=True, timeout=0.5) as db:
                meta_version: Optional[int]
                try:
                    row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                    meta_version = int(row[0]) if row and str(row[0]).isdigit() else None
                except sqlite3.Error:
                    meta_version = None
                cols = _columns(db)
                if any(c not in cols for c in _REQUIRED):
                    raise CookieImportError("Chrome 쿠키 DB 형식을 알 수 없습니다(필수 칸 없음).")
                present = [c for c in _WANTED if c in cols]
                rows = db.execute(f"SELECT {', '.join(present)} FROM cookies").fetchall()
            db.close()
        except sqlite3.Error:
            raise CookieImportError("Chrome 쿠키 DB 를 읽지 못했습니다(형식·상태 확인).") from None
        return meta_version, present, rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_cookies(db_path: Path, sites: Sequence[str], *, password_provider: Callable[[], bytes],
                 now: Optional[float] = None, tmp_root: Optional[Path] = None,
                 chrome_dir: Optional[Path] = None) -> ImportPlan:
    """고른 사이트의 쿠키만 복호화해 계획으로. 다른 사이트 행은 복호화하지 않는다.

    chrome_dir(기준점)을 주면 그곳부터 DB 까지 경로 구성요소의 심볼릭 링크를 복사 직전에 다시 거부한다(R4).
    """
    picked = [validate_site(s) for s in sites]
    plan = ImportPlan(sites={s: SitePlan(site=s) for s in dict.fromkeys(picked)})
    meta_version, cols, rows = _read_rows(Path(db_path), tmp_root, chrome_dir)
    when = time.time() if now is None else now
    key: Optional[bytes] = None
    for raw in rows:
        r = dict(zip(cols, raw))
        host = r.get("host_key")
        site = next((s for s in plan.sites if host_in_site(host, s)), None)
        if site is None or not isinstance(host, str):
            continue
        target = plan.sites[site]
        try:
            if r.get("top_frame_site_key"):
                raise _Skip(SKIP_PARTITIONED)
            enc_raw = r.get("encrypted_value") or b""
            if not isinstance(enc_raw, (bytes, bytearray, memoryview)):
                raise _Skip(SKIP_UNKNOWN)
            enc = bytes(enc_raw)
            if enc:
                if not enc.startswith(b"v10"):
                    raise _Skip(SKIP_UNKNOWN)
                if key is None:
                    key = derive_key(password_provider())
                value = _decrypt_v10(enc, key, host, meta_version)
                plan.read_values.append(value)
            else:
                plain = r.get("value")
                if not isinstance(plain, str):
                    raise _Skip(SKIP_UNKNOWN)
                value = plain
            plan.read_values.append(value)
            name, path = r.get("name"), r.get("path")
            if not isinstance(name, str) or not isinstance(path, str) or not path.startswith("/"):
                raise _Skip(SKIP_UNKNOWN)
            expires_utc = r.get("expires_utc")
            persistent = bool(r.get("has_expires", 1)) and bool(r.get("is_persistent", 1))
            if not isinstance(expires_utc, int) or expires_utc <= 0 or not persistent:
                raise _Skip(SKIP_SESSION)
            expires = expires_utc / 1_000_000 - _EPOCH_DELTA_S
            if expires <= when:
                raise _Skip(SKIP_EXPIRED)
            secure = bool(r.get("is_secure"))
            raw_same_site = r.get("samesite")
            same_site = _SAMESITE.get(raw_same_site) if isinstance(raw_same_site, int) else None
            if same_site == "None" and not secure:
                raise _Skip(SKIP_UNKNOWN)  # Chrome 스스로 저장하지 않는 조합
            target.cookies.append(ChromeCookie(
                host_key=host, name=name, value=value, path=path, expires=expires, secure=secure,
                http_only=bool(r.get("is_httponly")), same_site=same_site))
        except _Skip as skip:
            target.skipped[skip.reason] += 1
    key = None
    _register_values(plan.values())
    return plan


class ValueGuard:
    """이번 가져오기가 읽은 **모든** 쿠키 값의 로컬 집합 — CLI 출력 직전 최종 검사(R1).

    전역 레지스트리(WS-51, 값 256개 LRU)는 쿠키가 많으면 앞 값을 잊는다. 이 객체는 이번 import 의 값만 상한 없이
    기억한다(같은 판정 규칙을 쓰려고 SecretRegistry 를 **별도 인스턴스**로 쓴다 — 전역 상한·정책은 그대로):
    ≥ 8자는 부분 일치, 4~7자(순수 알파벳 제외)는 경계 일치, 도구 어휘·식별자 형식은 제외.
    값은 밖으로 내보내지 않는다(repr 은 개수만).
    """

    __slots__ = ("_reg",)

    def __init__(self, values: Iterable[str]) -> None:
        from security.secret_registry import SecretRegistry

        unique = list(dict.fromkeys(v for v in values if isinstance(v, str)))
        self._reg = SecretRegistry(cap=max(1, len(unique)), max_chars=sys.maxsize)
        self._reg.add_values(unique)

    def __repr__(self) -> str:
        return f"<ValueGuard {len(self._reg)} values (values hidden)>"

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise TypeError("ValueGuard 는 직렬화하지 않는다")

    def mask(self, text: str) -> str:
        """문장 속 값(표현)을 `[REDACTED]` 로."""
        return self._reg.mask(text)

    def name(self, name: str) -> str:
        """쿠키 이름에 값(전체·부분)이 들어 있으면 이름을 통째로 `[REDACTED]`."""
        from security.masking import MASK

        return MASK if self._reg.mask(name) != name else name


def _register_values(values: Iterable[str]) -> None:
    """읽은 값을 이 프로세스의 비밀 값 레지스트리(WS-51)에 등록 — CLI 출력 줄을 한 번 더 가린다."""
    try:
        from security import secret_registry

        secret_registry.REGISTRY.add_values(list(values))
    except Exception:  # noqa: BLE001 - 등록 실패가 가져오기를 막지 않는다(출력에는 원래 값이 없다)
        return


# ---------------------------------------------------------------------------- 쓰기


@dataclass
class SiteResult:
    site: str
    #: 저장소에서 값·속성까지 요청과 같다고 확인된 쿠키 이름.
    written: List[str] = field(default_factory=list)
    #: 실패 전부 — 브라우저가 받지 않았거나(없음) 다른 값·속성으로 저장됐거나 기존 쿠키가 남음.
    rejected: int = 0
    #: rejected 중, 같은 name/domain/path 의 **기존** 쿠키가 바뀌지 않고 그대로 남은 것(R3 — 성공으로 세지 않는다).
    kept_existing: int = 0

    @property
    def failed(self) -> int:
        return self.rejected


@asynccontextmanager
async def _playwright_context(path: Path) -> AsyncIterator[object]:
    """봇 프로필을 headless 영속 컨텍스트로 잠깐 연다(배경 Google 접속 끔). 페이지는 열지 않는다."""
    from playwright.async_api import async_playwright

    from browser.core import background_network_off_args

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(path), headless=True, args=background_network_off_args())
        try:
            yield ctx
        finally:
            await ctx.close()


def _key(c: Dict[str, object]) -> Tuple[str, str, str]:
    return (str(c.get("name")), str(c.get("domain")), str(c.get("path")))


#: 만료 비교 허용 오차(초) — Chromium 은 마이크로초로 저장한다.
EXPIRES_TOLERANCE_S = 1.0
#: Chromium 의 쿠키 만료 상한(400일) — 이보다 먼 만료는 저장할 때 잘린다.
CHROMIUM_MAX_EXPIRY_S = 400 * 86400


def _matches(want: ChromeCookie, got: Dict[str, object], t0: float, t1: float) -> bool:
    """저장된 쿠키가 요청(값·secure·httpOnly·sameSite·만료)과 같은가(R3). t0·t1 은 주입 전후 시각."""
    if got.get("value") != want.value or bool(got.get("secure")) != want.secure \
            or bool(got.get("httpOnly")) != want.http_only:
        return False
    if want.same_site is not None and got.get("sameSite") != want.same_site:
        return False
    exp = got.get("expires")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return False
    if abs(float(exp) - want.expires) <= EXPIRES_TOLERANCE_S:
        return True
    # 400일보다 먼 만료는 Chromium 이 '주입 시각 + 400일'로 자른다 — 그 범위 안이면 같은 쿠키다.
    return (want.expires > t0 + CHROMIUM_MAX_EXPIRY_S
            and t0 + CHROMIUM_MAX_EXPIRY_S - EXPIRES_TOLERANCE_S <= float(exp) <= t1 + CHROMIUM_MAX_EXPIRY_S
            + EXPIRES_TOLERANCE_S)


async def write_cookies(profile_path: Path, plan: ImportPlan) -> Dict[str, SiteResult]:
    """잠금을 쥔 프로필 폴더에 쿠키를 넣고, 주입 뒤 저장소를 다시 읽어 **값·속성까지 요청과 같은 것만** 성공으로
    사이트별로 돌려준다(R3). 같은 키의 기존 쿠키가 남은 거부는 kept_existing 으로 따로 센다.

    실패는 CookieImportError(예외 종류만 — Playwright 문구에는 쿠키 값·경로가 들어갈 수 있다).
    """
    results = {s: SiteResult(site=s) for s in plan.sites}
    try:
        async with _playwright_context(profile_path) as ctx:
            before = {_key(c): c for c in await ctx.cookies()}  # type: ignore[attr-defined]
            t0 = time.time()
            for site, sp in plan.sites.items():
                if not sp.cookies:
                    continue
                batch = [c.to_playwright() for c in sp.cookies]
                try:
                    await ctx.add_cookies(batch)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 - 하나라도 형식이 틀리면 묶음 전체가 실패한다
                    for one in batch:  # 하나씩 — 거부된 것만 빠진다(아래 저장소 대조가 센다)
                        try:
                            await ctx.add_cookies([one])  # type: ignore[attr-defined]
                        except Exception:  # noqa: BLE001
                            continue
            t1 = time.time()
            stored = {_key(c): c for c in await ctx.cookies()}  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise CookieImportError(
            f"봇 프로필에 쿠키를 넣지 못했습니다({type(exc).__name__}). 다른 프로그램이 그 프로필을 쓰는지 확인하세요."
        ) from None
    for site, sp in plan.sites.items():
        for c in sp.cookies:
            want = (c.name, c.host_key, c.path)
            got = stored.get(want)
            if got is not None and _matches(c, got, t0, t1):
                results[site].written.append(c.name)
            else:
                results[site].rejected += 1
                if got is not None and before.get(want) == got:  # 기존 쿠키가 바뀌지 않고 남았다
                    results[site].kept_existing += 1
        results[site].written.sort()
    return results
