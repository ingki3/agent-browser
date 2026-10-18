"""WS-58 — 헤드리스 이름 재시도 판정이 보는 '로그인 기록' (login_ledger 파일 + 서버 메모리).

* 저장: ``serve --profile`` 이면 프로필 폴더의 ``login_ledger.json``(WS-59 형식, browser.login_ledger) — 서버를
  다시 띄워도, 사람 CLI(``profile import-cookies``)가 서버 밖에서 적은 기록도 판정에 들어간다. 프로필이 없으면
  서버 수명 동안 메모리에만(로그인 기록도 쿠키도 서버와 함께 사라진다).
* 범위: 기록된 origin 의 호스트 L 이 페이지 호스트 H 를 덮는다 ⇔ ``H == L`` 또는 H 가 L 의 하위 도메인(점 경계).
  WS-59 가져오기 사이트 필터(chrome_cookies)·자격증명 도메인(security.credentials._matches)과 같은 규칙이다.
  scheme·포트는 보지 않는다 — 쿠키는 포트를 가리지 않고 Secure 아닌 쿠키는 http/https 가 공유한다(보수 쪽).
  ``evil-example.com``·``example.com.evil.test`` 는 ``example.com`` 기록에 덮이지 않는다.
* 손상: 파일을 읽을 수 없으면 ``covers`` 가 LedgerError 를 그대로 올린다 — 판정하는 쪽이 재시도하지 않는다
  (fail-closed). 쓰기 실패는 메모리 기록으로 남기고(이 서버 동안은 판정에 들어감) 경고만 남긴다(경로·값 없음).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit

from browser import login_ledger

logger = logging.getLogger(__name__)


def _host(origin_or_url: str) -> str:
    try:
        return (urlsplit(origin_or_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def host_covered(host: str, recorded: str) -> bool:
    """recorded 호스트 기록이 host 를 덮는가 — 같거나 점 경계 하위 도메인."""
    host, recorded = host.lower().rstrip("."), recorded.lower().rstrip(".")
    return bool(host and recorded) and (host == recorded or host.endswith("." + recorded))


class LoginRecords:
    """도구가 아는 로그인 사건(origin → source). 판정은 ``covers``, 기록은 ``record``."""

    def __init__(self) -> None:
        self.profile: Optional[Path] = None
        self.memory: Dict[str, str] = {}

    def attach(self, profile: Path) -> None:
        """serve --profile 의 프로필 폴더(login_ledger.json 이 놓이는 곳)."""
        self.profile = Path(profile)

    def record(self, origin: str, source: str) -> bool:
        """origin(URL 도 됨)을 source 로 기록한다. 파일에 썼으면 True(메모리에는 늘 남는다)."""
        try:
            key = login_ledger.normalize_origin(origin)
        except login_ledger.LedgerError:
            return False  # about:blank·data: 등 — 기록할 origin 이 아니다
        if source not in login_ledger.KNOWN_SOURCES:
            raise ValueError("unknown login source")
        self.memory[key] = source
        if self.profile is None:
            return False
        try:
            login_ledger.record(self.profile, [key], source=source)
            return True
        except login_ledger.LedgerError:
            # 경로·origin 은 남기지 않는다. 깨진 파일이면 판정이 어차피 fail-closed 다.
            logger.warning("로그인 기록 파일에 쓰지 못했습니다 — 이 서버 동안은 메모리 기록으로 판정합니다")
            return False

    def entries(self) -> Dict[str, str]:
        """메모리 + 파일 기록(origin → source). 파일이 깨졌으면 LedgerError."""
        out = dict(self.memory)
        if self.profile is not None:
            for origin, entry in login_ledger.load(self.profile).items():
                out.setdefault(origin, str(entry["source"]))
        return out

    def covers(self, url: str) -> bool:
        """url 의 호스트가 로그인 기록 범위 안인가. 기록을 읽을 수 없으면 LedgerError(fail-closed)."""
        host = _host(url)
        if not host:
            return False
        return any(host_covered(host, _host(origin)) for origin in self.entries())
