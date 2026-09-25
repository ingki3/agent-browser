"""도메인별 로그인 자격증명 (YAML).

파일 형식 — 권한 600, 도메인·아이디·비밀번호를 짝으로 둔다::

    credentials:
      - domain: naver.com
        username: myid
        password: "p@ss"

모델에게는 실제 값 대신 **고정 키 이름**만 알려 준다::

    아이디 입력칸   → type_text(text="LOGIN_USERNAME")
    비밀번호 입력칸 → type_text(text="LOGIN_PASSWORD")

디스패처가 입력 직전에 치환한다(`secrets.SecretStore`와 같은 경로). 이때
치환기는 **발급된 도메인의 페이지에서만** 값을 내준다(`allowed_for`). 에이전트가
다른 사이트로 이동한 뒤 같은 키를 입력해도 실제 값이 새 나가지 않는다.

YAML 함정: 따옴표 없는 `123456`·`0012`·`no`·`2026-09-23`은 숫자·불리언·날짜로
읽힌다. `str()`로 되돌리면 `0012`→`10`(8진수), `no`→`False`처럼 **다른 값이
조용히 입력되고** 로그인 실패 원인을 알 수 없다. 문자열이 아니면 거부한다.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from security.secrets import SecretStore

#: 모델이 쓰는 고정 키 이름. 사이트마다 다르게 두면 모델이 키를 지어내다 틀린다.
USERNAME_KEY = "LOGIN_USERNAME"
PASSWORD_KEY = "LOGIN_PASSWORD"

#: 기본 파일 위치
DEFAULT_PATH = Path("~/.config/agent-browser/credentials.yaml")

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class CredentialError(Exception):
    """자격증명 파일 로드·검증 실패. 메시지에 값은 절대 넣지 않는다."""


@dataclass(frozen=True)
class Credential:
    domain: str
    username: str = field(repr=False)
    password: str = field(repr=False)

    def __repr__(self) -> str:  # pragma: no cover - 디버깅용
        return f"Credential(domain={self.domain!r})"


def _host(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    return (parsed.hostname or "").lower().rstrip(".") or None


def _matches(host: str, domain: str) -> bool:
    """host가 domain 자신이거나 그 하위 도메인인가 (점 경계 기준)."""
    return host == domain or host.endswith("." + domain)


class BoundSecrets(SecretStore):
    """한 도메인에 묶인 치환기. 그 도메인 페이지에서만 값을 내준다."""

    def __init__(self, credential: Credential) -> None:
        super().__init__({
            USERNAME_KEY: credential.username,
            PASSWORD_KEY: credential.password,
        })
        self.domain = credential.domain

    def allowed_for(self, url: str) -> bool:
        host = _host(url)
        return bool(host) and _matches(host, self.domain)

    def __repr__(self) -> str:  # pragma: no cover
        return f"BoundSecrets(domain={self.domain!r})"


class CredentialStore:
    """도메인 → 자격증명. 값은 repr·예외 메시지에 나오지 않는다."""

    def __init__(self, credentials: List[Credential]) -> None:
        self._creds = list(credentials)

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_PATH) -> "CredentialStore":
        import yaml

        p = Path(path).expanduser()
        if not p.is_file():
            raise CredentialError(f"자격증명 파일이 없습니다: {p}")
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            raise CredentialError(
                f"자격증명 파일 권한이 느슨합니다: {p} (현재 {mode:04o}, 필요 0600). "
                f"`chmod 600 {p}` 후 다시 실행하십시오."
            )
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            # 파서 메시지에 값 조각이 들어갈 수 있어 위치만 남긴다.
            mark = getattr(exc, "problem_mark", None)
            where = f" ({mark.line + 1}행)" if mark else ""
            raise CredentialError(f"YAML 형식 오류{where}") from None
        return cls(cls._parse(data))

    @staticmethod
    def _parse(data: object) -> List[Credential]:
        if not isinstance(data, dict) or "credentials" not in data:
            raise CredentialError("최상위에 'credentials:' 목록이 필요합니다.")
        items = data["credentials"]
        if not isinstance(items, list) or not items:
            raise CredentialError("'credentials' 목록이 비어 있습니다.")
        out: List[Credential] = []
        seen: set[str] = set()
        for i, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise CredentialError(f"{i}번째 항목이 domain/username/password 짝이 아닙니다.")
            for key in ("domain", "username", "password"):
                if key not in item:
                    raise CredentialError(f"{i}번째 항목에 '{key}'가 없습니다.")
                if not isinstance(item[key], str):
                    kind = "빈 값(null)" if item[key] is None else type(item[key]).__name__
                    raise CredentialError(
                        f"{i}번째 항목의 '{key}'가 문자열이 아닙니다"
                        f"(YAML이 {kind}로 읽음). "
                        "값을 따옴표로 감싸십시오 — 예: password: \"0012\""
                    )
            domain = item["domain"].strip().lower().rstrip(".")
            if not _DOMAIN_RE.match(domain):
                raise CredentialError(
                    f"{i}번째 항목의 도메인 형식이 잘못됐습니다: {domain!r} "
                    "(https://나 경로 없이 naver.com 처럼 적으십시오)"
                )
            if domain in seen:
                raise CredentialError(f"도메인이 중복됐습니다: {domain}")
            seen.add(domain)
            if not item["username"] or not item["password"]:
                raise CredentialError(f"{i}번째 항목({domain})의 아이디나 비밀번호가 비어 있습니다.")
            out.append(Credential(domain=domain, username=item["username"],
                                  password=item["password"]))
        return out

    def domains(self) -> List[str]:
        return [c.domain for c in self._creds]

    def for_url(self, url: str) -> Optional[Credential]:
        """URL에 맞는 자격증명. 여러 개가 맞으면 가장 구체적인(긴) 도메인."""
        host = _host(url)
        if not host:
            return None
        hits = [c for c in self._creds if _matches(host, c.domain)]
        return max(hits, key=lambda c: len(c.domain)) if hits else None

    def secrets_for(self, url: str) -> Optional[BoundSecrets]:
        cred = self.for_url(url)
        return BoundSecrets(cred) if cred else None

    def __repr__(self) -> str:  # pragma: no cover
        return f"CredentialStore(domains={self.domains()})"


def placeholders() -> Dict[str, str]:
    """모델 지시문에 넣을 키 이름."""
    return {"username": USERNAME_KEY, "password": PASSWORD_KEY}
