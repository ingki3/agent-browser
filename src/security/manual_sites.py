"""WS-64 — '직접 조작할 사이트'(사람이 정한 출처 목록): 판정·목록 파일·거부 응답 모양.

사용자가 `agent-browser manual-sites add <사이트>`(또는 serve `--manual-site`)로 넣은 사이트에서는 에이전트의
부작용 액션(누르기·입력·고르기·키·대화상자·파일)을 **실행하지 않는다**. 대신 서버가 대상을 창에 표시하고 사람이
직접 하게 한다(interface.server_manual). 매크로 방지·봇 탐지를 우회하지 않는다(사용자 결정 B).

* 목록은 사람만 바꾼다 — MCP 도구에는 읽기도 쓰기도 없다(판정만). 페이지 글자로 짐작하지 않는다.
* 출처 비교(R2-5): 스킴 무시(http·https 같음), 호스트 소문자·끝 점 제거·IDN punycode·IP 정규화, 포트 없는 항목은
  모든 포트, 80·443 은 '포트 없음'. `*.srail.kr` 은 srail.kr 과 그 하위 전부. `*`·`*.kr`·공개 접미사 전체는 거부.
* 차단 9종 / 허용 10종(R1-2). 허용 집합 밖은 전부 차단 — 새 ActionType 은 기본 차단(fail-closed).
* 판정 출처(R1-4): 대상 프레임 ∪ 최상위 문서 ∪ opener 탭. 좌표 클릭·press_key·handle_dialog 는 그 탭의 모든 프레임.
  주소를 읽지 못하면 차단(fail-closed).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import ipaddress
import json
import os
import secrets as _secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

from contracts import ActionType

#: 목록 파일(사람 CLI 가 쓰고 serve 가 읽는다). 프로필과 무관하게 이 사용자의 모든 serve 에 적용된다.
MANUAL_SITES_FILE = Path.home() / ".agent-browser" / "manual_sites.json"
#: 위치 바꾸기(시험·하위 프로세스 serve 격리 — STATE_ROOT_ENV·PROFILE_ROOT_ENV 와 같은 방식).
MANUAL_SITES_ENV = "AGENT_BROWSER_MANUAL_SITES_FILE"
FILE_VERSION = 1
#: 항목 수 상한(파일 손상·남용 방지).
MAX_ENTRIES = 256

#: 차단 9종 — 페이지에 실제 입력(포인터·키·값)을 보내거나 파일·대화상자를 다룬다.
MANUAL_GATED = frozenset({
    ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.SELECT_OPTION, ActionType.CHECK_BOX, ActionType.HOVER,
    ActionType.PRESS_KEY, ActionType.HANDLE_DIALOG, ActionType.UPLOAD_FILE, ActionType.DOWNLOAD_FILE,
})
#: 허용 10종 — 읽기·이동·탭/프레임 전환·JS 스크롤(입력 이벤트 없음).
MANUAL_ALLOWED = frozenset({
    ActionType.OBSERVE_PAGE, ActionType.TAKE_SCREENSHOT, ActionType.NAVIGATE, ActionType.GO_BACK, ActionType.RELOAD,
    ActionType.SCROLL, ActionType.WAIT_FOR, ActionType.EXTRACT, ActionType.SWITCH_FRAME, ActionType.TAB_CONTROL,
})
#: 대상 프레임을 확정할 수 없는 액션 — 그 탭의 어느 프레임이라도 목록이면 차단.
_WIDE_ACTIONS = frozenset({ActionType.PRESS_KEY, ActionType.HANDLE_DIALOG})

#: 고정 어휘(security.public_url CONTROL_VOCABULARY 에 등록).
REASON_MANUAL_SITE = "manual_site"
WAIT_TOOL = "browser_control_wait"
OUTCOME_CLICKED = "clicked"
OUTCOME_PAGE_CHANGED = "page_changed"
OUTCOME_DONE = "done"
OUTCOME_EXPIRED = "expired"
OUTCOMES = (OUTCOME_CLICKED, OUTCOME_PAGE_CHANGED, OUTCOME_DONE, OUTCOME_EXPIRED)
#: 사람 인계 대기 만료(초) — 넘으면 expired(실패).
HANDOFF_TTL_S = 15 * 60

MANUAL_SITE_BROKEN_MESSAGE = ("직접 조작할 사이트 목록 파일을 읽지 못해(손상·권한) 이 동작을 실행하지 않았습니다 — 판정할 수 "
                              "없어 모든 사이트에서 누르기·입력 등을 막고 있습니다. 사용자에게 목록 파일을 고쳐 달라고 "
                              "하세요(`agent-browser manual-sites list` 로 확인). data.handoff 를 따르세요.")
MANUAL_SITE_MESSAGE = ("이 사이트는 사용자가 '직접 조작할 사이트'로 정해 두어 이 동작을 자동으로 실행하지 않았습니다 "
                       "(매크로 방지를 우회하지 않음). data.handoff 를 따르세요.")
STEPS_AVAILABLE = (
    "자동으로 누르거나 입력하지 마세요 — 사용자가 이 사이트는 사람이 직접 조작하도록 정했습니다.",
    "창에 표시한 대상을 사람이 직접 조작해 달라고 사용자에게 알리고 browser_control_wait 로 기다리세요"
    "(clicked·page_changed·done·expired).",
    "끝나면 browser_observe_page 로 다시 관찰하세요(이전 element_id 무효). expired 면 사용자에게 다시 물으세요.",
)
STEPS_UNAVAILABLE = (
    "자동으로 누르거나 입력하지 마세요 — 사용자가 이 사이트는 사람이 직접 조작하도록 정했습니다.",
    "지금 사람이 볼 창이 없습니다 — 사용자에게 이 단계를 직접 하도록 안내하거나, browser_control_request 로 창을 "
    "연 뒤 다시 관찰하세요(현재 페이지 상태는 재시작으로 사라짐).",
)


class ManualSiteError(ValueError):
    """목록 항목·파일을 받을 수 없음."""


# ---------------------------------------------------------------------------- 정규화


def _public_suffixes() -> frozenset:
    from browser.chrome_cookies import PUBLIC_SUFFIXES

    return PUBLIC_SUFFIXES


#: R1 B5: IDNA2003(표준 라이브러리 idna 코덱)와 Chromium(UTS46 비전이 처리)이 다르게 바꾸는 글자 — 이 글자가 든
#: 항목은 받지 않는다(저장한 호스트와 브라우저 주소의 호스트가 달라져 보호가 빠진다: faß.de → fass.de ≠ xn--fa-hia.de).
_IDNA_DEVIATIONS = frozenset({"\u00df", "\u03c2", "\u200c", "\u200d"})  # ß · ς · ZWNJ · ZWJ


def _idn_to_ascii(host: str) -> str:
    """국제화 호스트 → punycode. Chromium 과 같은 결과를 보장할 수 없으면 ManualSiteError(받지 않음)."""
    import unicodedata

    if any(ch in _IDNA_DEVIATIONS for ch in host):
        raise ManualSiteError("이 국제화 도메인은 브라우저와 같은 이름으로 바꿀 수 없어 받지 않습니다(ß·ς·보이지 않는 연결 "
                              "문자) — 브라우저 주소창의 xn-- 형태로 넣으세요")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
        back = ascii_host.encode("ascii").decode("idna")
    except UnicodeError as exc:
        raise ManualSiteError("국제화 도메인을 변환할 수 없습니다") from exc
    # 되돌린 이름이 입력의 NFKC·소문자형과 다르면 IDNA2003 이 글자를 지우거나 바꾼 것이다(UTS46 과 어긋날 수 있음).
    if back != unicodedata.normalize("NFKC", host).lower():
        raise ManualSiteError("이 국제화 도메인은 브라우저와 같은 이름으로 바꿀 수 없어 받지 않습니다 — 브라우저 주소창의 "
                              "xn-- 형태로 넣으세요")
    return ascii_host


def _ipv4_number(part: str) -> Optional[int]:
    """WHATWG IPv4 number parser(0x 16진·0 8진·10진). 숫자가 아니면 None."""
    radix = 10
    if part[:2] in ("0x", "0X"):
        part, radix = part[2:], 16
    elif len(part) > 1 and part[0] == "0":
        part, radix = part[1:], 8
    if part == "":
        return 0
    digits = {10: "0123456789", 8: "01234567", 16: "0123456789abcdefABCDEF"}[radix]
    if not all(c in digits for c in part):
        return None
    return int(part, radix)


def _whatwg_ipv4(host: str) -> Optional[str]:
    """R1 B6: WHATWG URL 의 IPv4 판정(브라우저와 같은 규칙). 숫자로 끝나는 호스트면 정규 점 4개 형태, 아니면 None.

    숫자로 끝나는데 IPv4 로 읽을 수 없으면(브라우저가 주소를 거부) ManualSiteError. 127.1·2130706433·0x7f000001·
    0177.0.0.1 은 모두 127.0.0.1 이다.
    """
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts = parts[:-1]
    last = parts[-1]
    if not last or not (all(c in "0123456789" for c in last) or _ipv4_number(last) is not None):
        return None  # 숫자로 끝나지 않음 — 도메인
    if len(parts) > 4:
        raise ManualSiteError(f"IPv4 주소 형식이 아닙니다: {host!r}")
    numbers = []
    for part in parts:
        n = _ipv4_number(part) if part else None
        if n is None:
            raise ManualSiteError(f"IPv4 주소 형식이 아닙니다: {host!r}")
        numbers.append(n)
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        raise ManualSiteError(f"IPv4 주소 범위 밖입니다: {host!r}")
    value = numbers[-1]
    for i, n in enumerate(numbers[:-1]):
        value += n * 256 ** (3 - i)
    return str(ipaddress.IPv4Address(value))


def _norm_host(raw: str) -> str:
    """호스트 → 비교용 정규형(소문자·끝 점 제거·IDN punycode·IP 정규화). 받을 수 없으면 ManualSiteError.

    R1: 브라우저(Chromium·WHATWG URL)가 만드는 호스트와 같은 표현이어야 한다 — 국제화 이름은 Chromium 과 결과가 같을 때만
    받고(B5), 숫자로 끝나는 호스트는 WHATWG IPv4 규칙으로 정규화한다(B6). 페이지 주소의 호스트도 같은 함수를 지난다.
    """
    host = str(raw or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        try:
            return ipaddress.IPv6Address(host).compressed
        except ValueError as exc:
            raise ManualSiteError(f"IPv6 주소 형식이 아닙니다: {host!r}") from exc
    if host.endswith(".") and not host.endswith(".."):
        host = host[:-1]
    if not host:
        raise ManualSiteError("호스트가 비어 있습니다")
    if ":" in host:
        try:
            return ipaddress.IPv6Address(host).compressed
        except ValueError as exc:
            raise ManualSiteError(f"호스트 형식이 아닙니다: {host!r}") from exc
    if any(ord(ch) > 127 for ch in host):
        host = _idn_to_ascii(host)
    ipv4 = _whatwg_ipv4(host)
    if ipv4 is not None:
        return ipv4
    labels = host.split(".")
    for label in labels:
        if not label or len(label) > 63 or not all(c.isalnum() or c == "-" for c in label) \
                or label.startswith("-") or label.endswith("-"):
            raise ManualSiteError(f"호스트 형식이 아닙니다: {host!r}")
    return host


def _norm_port(port: Optional[int]) -> Optional[int]:
    if port in (None, 80, 443):
        return None
    if not 0 < int(port) < 65536:
        raise ManualSiteError("포트 범위 밖")
    return int(port)


@dataclass(frozen=True)
class SiteEntry:
    """목록 항목 하나(정규형). wildcard 면 host 와 그 하위 전부."""

    host: str
    port: Optional[int] = None
    wildcard: bool = False

    @property
    def text(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        out = ("*." if self.wildcard else "") + host
        return out + (f":{self.port}" if self.port is not None else "")

    def matches(self, host: str, port: Optional[int]) -> bool:
        if self.port is not None and port != self.port:
            return False
        if host == self.host:
            return True
        return self.wildcard and host.endswith("." + self.host)


def parse_entry(raw: Any) -> SiteEntry:
    """사람이 준 사이트 → SiteEntry. 받는 꼴: srail.kr · etk.srail.kr:8443 · https://srail.kr/ · *.srail.kr.

    경로·질의·사용자 정보가 붙은 주소, http(s) 밖 스킴, `*`·`*.kr`·공개 접미사 전체 항목은 거부한다.
    """
    text = str(raw if isinstance(raw, str) else "").strip()
    if not text or len(text) > 300:
        raise ManualSiteError("사이트가 비어 있거나 너무 깁니다")
    if any(ch.isspace() for ch in text):
        raise ManualSiteError("사이트에 공백이 있습니다")
    low = text.lower()
    if "://" in low:
        scheme = low.split("://", 1)[0]
        if scheme not in ("http", "https"):
            raise ManualSiteError("http/https 사이트만 넣을 수 있습니다")
    else:
        low = "https://" + low
    wildcard = False
    marker = low.split("://", 1)[1]
    if marker.startswith("*."):
        wildcard = True
        low = low.replace("*.", "", 1)
    elif marker.startswith("*"):
        raise ManualSiteError("'*' 하나로 모든 사이트를 넣을 수 없습니다")
    try:
        parts = urlsplit(low)
        port = parts.port
        hostname = parts.hostname
    except ValueError as exc:
        raise ManualSiteError("사이트 주소 형식이 아닙니다") from exc
    if parts.username or parts.password or "@" in parts.netloc:
        raise ManualSiteError("사용자 정보가 든 주소는 받지 않습니다")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ManualSiteError("경로 없이 사이트(호스트[:포트])만 넣으세요")
    if "*" in (hostname or ""):
        raise ManualSiteError("와일드카드는 맨 앞 '*.' 만 쓸 수 있습니다")
    host = _norm_host(hostname or "")
    is_ip = True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip = False
    if wildcard and is_ip:
        raise ManualSiteError("IP 주소에는 '*.' 를 붙일 수 없습니다")
    if not is_ip:
        labels = host.split(".")
        if host in _public_suffixes() or (wildcard and len(labels) < 2):
            raise ManualSiteError(f"{host} 는 여러 사이트가 함께 쓰는 공개 접미사라 통째로 넣을 수 없습니다 — "
                                  "그 아래 사이트 이름을 주세요")
    return SiteEntry(host=host, port=_norm_port(port), wildcard=wildcard)


def url_host_port(url: Any) -> Optional[Tuple[str, Optional[int]]]:
    """페이지 주소 → (정규 호스트, 정규 포트). http(s) 가 아니면(about:blank·data: 등) None.

    R1 B2: `blob:https://site/uuid` 는 만든 문서의 출처를 주소에 담는다 — 그 안쪽 주소로 판정한다.
    형식이 깨진 http(s) 주소는 ManualSiteError(호출자가 fail-closed 로 다룬다).
    """
    text = str(url or "")
    if text[:5].lower() == "blob:":
        text = text[5:]
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ManualSiteError("주소를 읽을 수 없음") from exc
    if parts.scheme.lower() not in ("http", "https"):
        return None
    try:
        port = parts.port
    except ValueError as exc:
        raise ManualSiteError("포트를 읽을 수 없음") from exc
    return _norm_host(parts.hostname or ""), _norm_port(port)


class ManualSites:
    """판정용 목록(불변). `current()` 는 자기 자신 — 저장소(ManualSiteStore)와 같은 꼴로 쓴다.

    R1 B3: `broken` 이 있으면(목록 파일을 처음부터 읽지 못함) 판정 불가 — 모든 부작용 액션을 막는다(manual_site_hit).
    """

    def __init__(self, entries: Iterable[SiteEntry] = (), broken: str = "") -> None:
        seen: Dict[str, SiteEntry] = {}
        for e in entries:
            seen.setdefault(e.text, e)
        self.entries: Tuple[SiteEntry, ...] = tuple(seen.values())
        self.broken = str(broken or "")

    @classmethod
    def of(cls, raws: Iterable[Any]) -> "ManualSites":
        return cls(parse_entry(r) for r in raws)

    @property
    def empty(self) -> bool:
        return not self.entries and not self.broken

    def current(self) -> "ManualSites":
        return self

    def texts(self) -> List[str]:
        return [e.text for e in self.entries]

    def match(self, url: Any) -> Optional[str]:
        """주소가 목록 사이트면 그 항목 문자열, 아니면 None. 주소를 읽지 못하면 ManualSiteError."""
        if not self.entries:
            return None
        hp = url_host_port(url)
        if hp is None:
            return None
        for e in self.entries:
            if e.matches(*hp):
                return e.text
        return None


# ---------------------------------------------------------------------------- 파일


def manual_sites_file() -> Path:
    """목록 파일 위치 — 환경변수가 있으면 그것, 없으면 MANUAL_SITES_FILE(호출 때 읽음)."""
    env = os.environ.get(MANUAL_SITES_ENV)
    return Path(env).expanduser() if env else Path(MANUAL_SITES_FILE)


def _check_file(path: Path) -> None:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ManualSiteError("목록 파일이 일반 파일이 아닙니다")
    if st.st_uid != os.geteuid():
        raise ManualSiteError("목록 파일 소유자가 다릅니다")
    if st.st_mode & 0o077:
        raise ManualSiteError("목록 파일 권한이 0600 이 아닙니다")


def read_entries(path: Path = MANUAL_SITES_FILE) -> List[Dict[str, Any]]:
    """파일의 항목 [{site, added_at}]. 파일이 없으면 []. 손상·권한 문제는 ManualSiteError."""
    return _load(Path(path))


#: 파일을 고치라는 안내(손상·권한 오류에 덧붙인다 — CLI·serve 경고·거부 응답).
FIX_HINT = ("목록 파일을 고치세요: `agent-browser manual-sites list` 로 확인하고, 손상됐으면 지우고 다시 add, 권한이면 "
            "`chmod 600` (그 전까지 판정할 수 없어 모든 사이트에서 누르기·입력 등을 실행하지 않습니다)")


def _load(path: Path) -> List[Dict[str, Any]]:
    path = Path(path)
    try:
        _check_file(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ManualSiteError(f"목록 파일을 읽을 수 없습니다({type(exc).__name__})") from exc
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManualSiteError("목록 파일이 손상됐습니다") from exc
    if not isinstance(data, dict) or data.get("version") != FILE_VERSION or not isinstance(data.get("sites"), list):
        raise ManualSiteError("목록 파일 형식이 아닙니다")
    rows = data["sites"][:MAX_ENTRIES]
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ManualSiteError("목록 파일 항목 형식이 아닙니다")
        entry = parse_entry(row.get("site"))
        out.append({"site": entry.text, "added_at": str(row.get("added_at") or "")})
    return out


def _write(path: Path, rows: List[Dict[str, Any]]) -> None:
    path = Path(path)
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    pst = os.lstat(parent)
    if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
        raise ManualSiteError("목록 폴더가 일반 폴더가 아닙니다")
    if path.exists() or path.is_symlink():
        _check_file(path)
    tmp = parent / f".tmp-manual-{_secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": FILE_VERSION, "sites": rows}, fh, ensure_ascii=True, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """R1 B4: 목록 편집(read-modify-write) 전체를 프로세스 간 배타 잠금(fcntl.flock) 안에서 한다.

    잠금 파일은 목록 옆 `<이름>.lock`(0600, 심볼릭 링크 거부). 원자적 교체(os.replace)는 찢어진 읽기만 막고 동시 add 의
    잃어버린 갱신은 막지 못한다(검증 B4).
    """
    import fcntl

    path = Path(path)
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = parent / (path.name + ".lock")
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise ManualSiteError(f"목록 잠금 파일을 열 수 없습니다({type(exc).__name__})") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def add_site(raw: Any, path: Path = MANUAL_SITES_FILE) -> Tuple[str, bool]:
    """항목 추가. 반환: (정규형, 새로 넣었는가). 읽기·추가·쓰기는 한 잠금 안의 트랜잭션(R1 B4)."""
    entry = parse_entry(raw)
    with _locked(path):
        rows = _load(path)
        if any(r["site"] == entry.text for r in rows):
            return entry.text, False
        if len(rows) >= MAX_ENTRIES:
            raise ManualSiteError(f"항목이 {MAX_ENTRIES}개를 넘을 수 없습니다")
        rows.append({"site": entry.text,
                     "added_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})
        _write(path, rows)
    return entry.text, True


def remove_site(raw: Any, path: Path = MANUAL_SITES_FILE) -> bool:
    entry = parse_entry(raw)
    with _locked(path):
        rows = _load(path)
        kept = [r for r in rows if r["site"] != entry.text]
        if len(kept) == len(rows):
            return False
        _write(path, kept)
    return True


class ManualSiteStore:
    """serve 가 쓰는 목록: 파일(판정마다 상태를 보고 바뀌었으면 다시 읽음) + 시작 플래그(`--manual-site`).

    파일을 읽지 못하면(손상·권한) 마지막으로 읽은 목록을 유지하고 error 에 사유를 둔다 — 목록은 막는 방향이라
    이전 값을 버리지 않는다(fail-closed). R1 B3: 한 번도 읽지 못했으면(cold start) 마지막 목록이 없다 — 빈 목록이 아니라
    판정 불가(broken)로 두어 모든 부작용 액션을 막는다. 파일을 고치면 다음 판정에서 풀린다.
    """

    def __init__(self, path: Optional[Path] = MANUAL_SITES_FILE, extra: Iterable[Any] = ()) -> None:
        self.path = Path(path) if path is not None else None
        self._extra = tuple(parse_entry(r) for r in extra)
        self._file: Tuple[SiteEntry, ...] = ()
        self._sig: Any = object()
        self.error = ""
        self._loaded = False
        self._cached = ManualSites(self._extra)

    def _signature(self) -> Any:
        if self.path is None:
            return None
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return ("error", type(exc).__name__)
        return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode, st.st_uid)

    def current(self) -> ManualSites:
        sig = self._signature()
        if sig != self._sig:
            self._sig = sig
            if self.path is not None:
                try:
                    self._file = tuple(parse_entry(r["site"]) for r in read_entries(self.path))
                    self.error = ""
                    self._loaded = True
                except ManualSiteError as exc:
                    self.error = str(exc)
            broken = self.error if (self.error and not self._loaded) else ""
            self._cached = ManualSites(self._extra + self._file, broken=broken)
        return self._cached

    @property
    def empty(self) -> bool:
        return self.current().empty


#: `agent-browser run`(고정 파일 run_cli.py) 경로의 디스패처 기본 목록 — cli.main 이 run 직전에만 넣는다.
#: 기본은 빈 목록(harness·시험이 홈 파일을 읽지 않는다).
_PROCESS_DEFAULT: Any = None


def set_process_default(store: Any) -> None:
    global _PROCESS_DEFAULT
    _PROCESS_DEFAULT = store


def process_default() -> Any:
    return _PROCESS_DEFAULT


# ---------------------------------------------------------------------------- 판정


def _url_of(obj: Any) -> str:
    return str(getattr(obj, "url", "") or "")


def is_gated(action: ActionType) -> bool:
    """이 액션은 목록 사이트에서 막는가(허용 집합 밖 = 차단)."""
    return action not in MANUAL_ALLOWED


def wide_target(action: ActionType, params: Dict[str, Any]) -> bool:
    """대상 프레임을 확정할 수 없는 호출인가(좌표 클릭·키·대화상자)."""
    if action in _WIDE_ACTIONS:
        return True
    return action is ActionType.CLICK and not params.get("element_id") and not params.get("selector")


#: R1 B2: 주소로 출처를 알 수 없는 문서(about:blank·about:srcdoc)의 실효 출처를 읽는 상한(초) — 대화상자가 열린 탭 등에서
#: 멈추지 않게. 넘거나 실패하면 판정 불가(그 문서의 프레임 사슬·최상위·opener 로만 판정).
EFFECTIVE_ORIGIN_TIMEOUT_S = 1.0


def refusal_message(sites: Any) -> str:
    """거부 응답 문구 — 목록 파일을 읽지 못해 막았으면(R1 B3) 파일을 고치라는 안내."""
    return MANUAL_SITE_BROKEN_MESSAGE if getattr(sites, "broken", "") else MANUAL_SITE_MESSAGE


def _ancestors(src: Any) -> List[Any]:
    """프레임의 부모 프레임 사슬(가까운 것부터). Page·가짜 객체는 []."""
    out: List[Any] = []
    cur = getattr(src, "parent_frame", None)
    while cur is not None and len(out) < 32:
        out.append(cur)
        cur = getattr(cur, "parent_frame", None)
    return out


async def _effective_origin(src: Any) -> Optional[str]:
    """주소에 출처가 없는 문서의 실효 출처(location.origin — Chromium 의 securityOrigin). 불투명('null')·실패면 None."""
    evaluate = getattr(src, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        origin = await asyncio.wait_for(evaluate("location.origin"), EFFECTIVE_ORIGIN_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — 시간 초과·떨어진 프레임: 판정 불가
        return None
    origin = str(origin or "")
    return origin if origin.lower().startswith(("http://", "https://")) else None


async def _source_hit(sites: "ManualSites", src: Any) -> Optional[str]:
    """한 문서(Page·Frame)의 실효 출처가 목록이면 그 항목. 주소를 읽지 못하면 '?'.

    http(s)·blob: 은 주소(blob 은 안쪽 출처)로, about:blank·about:srcdoc 은 실효 출처(location.origin)로 판정한다.
    data: 등 불투명 출처·판정 실패는 None — 호출자가 같은 사슬(부모 프레임·최상위·opener)을 함께 보므로 사슬에 목록
    출처가 있으면 막힌다.
    """
    try:
        url = _url_of(src)
        hp = url_host_port(url)
        if hp is not None:
            for e in sites.entries:
                if e.matches(*hp):
                    return e.text
            return None
    except Exception:  # noqa: BLE001 — 주소를 읽지 못함: fail-closed
        return "?"
    if not url.lower().startswith("about:"):
        return None
    origin = await _effective_origin(src)
    if origin is None:
        return None
    try:
        return sites.match(origin)
    except Exception:  # noqa: BLE001
        return "?"


async def manual_site_hit(ctx: Any, action: ActionType, params: Dict[str, Any], store: Any) -> Optional[str]:
    """이 호출을 목록 사이트 때문에 막는가 — 막으면 맞은 항목 문자열(주소를 못 읽었으면 '?'), 아니면 None.

    판정 출처: 대상 프레임(ctx.page — switch_frame 뒤면 Frame)과 그 부모 프레임 사슬 ∪ 최상위(ctx.root_page) ∪ 그 탭을
    연 opener 탭, 좌표 클릭·press_key·handle_dialog 는 그 탭의 모든 프레임도. 각 문서는 주소가 아니라 실효 출처로
    본다(R1 B2 — blob·about:blank·about:srcdoc 상속). 목록 파일을 처음부터 읽지 못했으면(R1 B3) 판정 불가 — '?'.
    """
    if store is None or not is_gated(action):
        return None
    sites = store.current()
    if sites.empty:
        return None
    if getattr(sites, "broken", ""):
        return "?"
    page = getattr(ctx, "page", None)
    top = getattr(ctx, "root_page", None) or page
    sources: List[Any] = [page]
    try:
        sources.extend(_ancestors(page))
    except Exception:  # noqa: BLE001 — 부모 프레임을 못 읽음: fail-closed
        return "?"
    sources.append(top)
    if wide_target(action, params or {}):
        try:
            sources.extend(list(getattr(top, "frames", []) or []))
        except Exception:  # noqa: BLE001 — 프레임 목록을 못 읽음: fail-closed
            return "?"
    try:
        opener_fn = getattr(top, "opener", None)
        opener = await opener_fn() if callable(opener_fn) else None
    except Exception:  # noqa: BLE001 — 닫힌 opener 등: 판정에서 뺀다(대상·최상위는 그대로 본다)
        opener = None
    if opener is not None:
        sources.append(opener)
    seen: List[int] = []
    for src in sources:
        if src is None or id(src) in seen:
            continue
        seen.append(id(src))
        hit = await _source_hit(sites, src)
        if hit is not None:
            return hit
    return None


def manual_hit_sync(sites: Any, page: Any) -> Optional[str]:
    """동기 판정(대화상자 처리기처럼 await 할 수 없는 곳): 그 탭과 모든 프레임의 주소(blob 안쪽 출처 포함).

    목록 파일을 읽지 못했으면 '?', 주소를 읽지 못하면 '?'. 주소에 출처가 없는 문서는 같은 탭의 다른 프레임(부모)으로 본다.
    """
    if sites is None or page is None:
        return None
    sites = sites.current()
    if sites.empty:
        return None
    if getattr(sites, "broken", ""):
        return "?"
    try:
        docs = [page] + list(getattr(page, "frames", []) or [])
        for doc in docs:
            hit = sites.match(_url_of(doc))
            if hit is not None:
                return hit
    except Exception:  # noqa: BLE001 — fail-closed
        return "?"
    return None


def handoff_data(action: ActionType, *, available: bool, handoff_id: Optional[str] = None,
                 target: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """거부 응답 data — data.handoff 는 고정 값만, 페이지 유래 대상 이름은 스캔되는 data.handoff_target."""
    handoff: Dict[str, Any] = {
        "suggested": True, "reason": REASON_MANUAL_SITE, "tool": WAIT_TOOL, "available": bool(available),
        "steps": list(STEPS_AVAILABLE if available else STEPS_UNAVAILABLE), "action": action.value,
    }
    if available and handoff_id:
        handoff["handoff_id"] = handoff_id
    data: Dict[str, Any] = {"handoff": handoff}
    if target:
        from actions.verification import safe_page_text

        data["handoff_target"] = {k: safe_page_text(v, 120) for k, v in target.items() if v}
    return data


def is_manual_handoff(data: Any) -> bool:
    """이 응답 data 가 목록 사이트 인계인가."""
    handoff = data.get("handoff") if isinstance(data, dict) else None
    return isinstance(handoff, dict) and handoff.get("reason") == REASON_MANUAL_SITE
