"""자격증명 플레이스홀더 치환 (PRD 5.3).

목적은 단 하나다 — **자격증명 평문이 LLM 컨텍스트에 유입되는 것을 막는다.**

    LLM이 보는 것      type_text(text="X-PASSWORD")
    실제 실행되는 것    page.fill(..., "실제값")
    트레이스에 남는 것  text="X-PASSWORD"

WS-19에서 넣은 마스킹은 사후 방어다. 값이 이미 프롬프트로 전송된 뒤
기록할 때만 가린다. 이 모듈은 전송 자체를 없앤다.

한계(중요):
치환된 값은 페이지 DOM에 존재하므로 이후 관찰·스크린샷에 노출될 수
있다. 이 기능은 "LLM 컨텍스트 유입 차단"만 보장하며 종단 간 기밀성을
보장하지 않는다. Playwright MCP도 같은 한계를 명시한다 — 응답 마스킹을
넣고 1년 뒤에야 콘솔 로그 경로를 막았고 트레이스/HAR은 미해결이다.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

#: 시크릿 키로 인정할 형식. 우연한 일치를 막기 위해 좁게 잡는다.
#: 일반 텍스트가 키로 오인되면 사용자 입력이 조용히 바뀌므로 위험하다.
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_-]{2,63}$")

#: 파일 권한 요건. 그룹/기타 사용자에게 읽기 권한이 있으면 거부한다.
REQUIRED_MODE = 0o600


class SecretsError(Exception):
    """시크릿 파일 로드 실패."""


@dataclass(frozen=True)
class SecretResolution:
    """치환 결과.

    resolved가 False면 원본 문자열을 그대로 쓴다. **조용한 실패를 만들지
    않기 위해** 등록되지 않은 키는 치환하지 않고 입력값을 그대로 전달한다.
    """

    value: str
    resolved: bool
    key: Optional[str] = None


class SecretStore:
    """dotenv 형식 시크릿 파일을 읽어 플레이스홀더를 해석한다."""

    def __init__(self, secrets: Optional[Dict[str, str]] = None) -> None:
        self._secrets: Dict[str, str] = dict(secrets or {})

    # -- 로드 -----------------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path) -> "SecretStore":
        """파일에서 로드한다. 권한이 느슨하면 거부한다."""
        p = Path(path).expanduser()
        if not p.is_file():
            raise SecretsError(f"시크릿 파일이 없습니다: {p}")

        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            raise SecretsError(
                f"시크릿 파일 권한이 느슨합니다: {p} (현재 {mode:04o}, "
                f"필요 {REQUIRED_MODE:04o}). "
                f"`chmod 600 {p}` 후 다시 실행하십시오."
            )

        return cls(cls._parse(p.read_text(encoding="utf-8")))

    @staticmethod
    def _parse(text: str) -> Dict[str, str]:
        """dotenv 형식을 파싱한다. 유효하지 않은 키는 건너뛴다."""
        out: Dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not KEY_PATTERN.match(key):
                continue
            value = value.strip()
            # 따옴표로 감싼 값은 벗긴다
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key] = value
        return out

    @classmethod
    def from_env(cls, prefix: str = "AGENT_BROWSER_SECRET_") -> "SecretStore":
        """환경변수에서 로드한다 (컨테이너/CI용)."""
        out = {
            k[len(prefix):]: v
            for k, v in os.environ.items()
            if k.startswith(prefix) and KEY_PATTERN.match(k[len(prefix):])
        }
        return cls(out)

    # -- 해석 -----------------------------------------------------------

    def resolve(self, text: str) -> SecretResolution:
        """입력이 등록된 키면 실제 값으로 바꾼다.

        키가 아니거나 등록되지 않았으면 **원본을 그대로** 반환한다.
        치환 실패를 조용히 넘기면 사용자가 빈 값이 들어간 줄 모른다.
        """
        if not text or not KEY_PATTERN.match(text):
            return SecretResolution(value=text, resolved=False)
        if text not in self._secrets:
            return SecretResolution(value=text, resolved=False)
        return SecretResolution(
            value=self._secrets[text], resolved=True, key=text
        )

    def is_key(self, text: str) -> bool:
        """등록된 시크릿 키인가."""
        return bool(text) and text in self._secrets

    def __len__(self) -> int:
        return len(self._secrets)

    def __repr__(self) -> str:  # pragma: no cover - 디버깅용
        # 값은 절대 노출하지 않는다.
        return f"SecretStore(keys={sorted(self._secrets)})"
