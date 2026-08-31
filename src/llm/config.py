"""LLM 설정 및 `.env` 로더 (WS-7).

`.env` 파일 또는 환경변수에서 OpenRouter 자격증명을 읽는다.

    OPENROUTER_API_KEY=sk-or-v1-...
    OPENROUTER_MODEL=anthropic/claude-3.5-sonnet

설계 원칙:
* **키 값은 절대 로그·예외 메시지·`__repr__`에 노출하지 않는다.** 실환경
  검증 중 트레이스가 저장되므로, 한 번이라도 새면 디스크에 남는다.
* 외부 의존(python-dotenv)을 쓰지 않는다. stdlib만으로 충분하고,
  의존성이 적을수록 공급망 위험이 줄어든다.
* 환경변수가 `.env`보다 우선한다. CI에서 시크릿으로 주입하는 값이
  로컬 파일에 덮이면 안 된다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

#: 기본 `.env` 탐색 경로 (레포지토리 루트)
DEFAULT_ENV_PATH = Path(".env")

#: OpenRouter API 베이스 URL
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: 모델 미지정 시 기본값.
#: 저비용·구조화 출력 지원 모델을 기본으로 둔다. 태스크당 $0.75 상한
#: (contracts.thresholds.MAX_USD_PER_TASK) 안에서 30스텝을 돌 수 있어야 한다.
DEFAULT_MODEL = "openai/gpt-4o-mini"

#: `KEY=VALUE` 파싱. 값의 따옴표와 인라인 주석을 제거한다.
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env_text(text: str) -> Dict[str, str]:
    """`.env` 내용을 딕셔너리로 파싱한다.

    지원: `KEY=value`, `export KEY=value`, 따옴표, `#` 주석, 빈 줄.
    """
    result: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()

        # 따옴표로 감싼 값은 내부를 그대로 사용한다(주석 기호 포함 가능).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # 따옴표가 없을 때만 인라인 주석을 제거한다.
            hash_pos = value.find(" #")
            if hash_pos >= 0:
                value = value[:hash_pos].rstrip()

        result[key] = value
    return result


def load_env_file(path: Optional[Path] = None) -> Dict[str, str]:
    """`.env` 파일을 읽어 반환한다. 없으면 빈 딕셔너리."""
    target = Path(path) if path else DEFAULT_ENV_PATH
    if not target.exists():
        return {}
    try:
        return parse_env_text(target.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _redact(secret: str) -> str:
    """키를 사람이 식별만 가능한 형태로 축약한다 (앞 6자 + 길이)."""
    if not secret:
        return "<없음>"
    if len(secret) <= 10:
        return f"<{len(secret)}자>"
    return f"{secret[:6]}...<{len(secret)}자>"


#: `.env.example`의 플레이스홀더를 실제 키로 오인하지 않기 위한 표식.
#: `cp .env.example .env` 후 값을 채우지 않으면 "설정됨"으로 보고되어
#: 이후 단계에서 401을 받고서야 원인을 알게 된다.
_PLACEHOLDER_MARKERS = (
    "여기에",
    "your_",
    "YOUR_",
    "xxx",
    "XXX",
    "<",
    "changeme",
    "replace",
    "example",
)


def is_placeholder(value: str) -> bool:
    """플레이스홀더(미기입) 값인지 판정한다."""
    if not value:
        return True
    lowered = value.lower()
    return any(marker.lower() in lowered for marker in _PLACEHOLDER_MARKERS)


@dataclass
class LLMConfig:
    """OpenRouter 호출에 필요한 설정.

    `api_key`는 `repr=False`로 선언해 `print(config)`나 예외 트레이스백에
    노출되지 않게 한다. 확인이 필요하면 `masked_key`를 사용한다.
    """

    api_key: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    base_url: str = OPENROUTER_BASE_URL
    #: OpenRouter 순위 페이지에 표시될 앱 정보 (선택)
    app_url: str = ""
    app_title: str = "agent-browser"
    timeout_s: float = 60.0
    max_retries: int = 2

    @property
    def configured(self) -> bool:
        """실제 사용 가능한 키가 있는가.

        플레이스홀더는 미설정으로 취급한다. `cp .env.example .env` 후
        값을 채우지 않은 상태를 "설정됨"으로 보고하면, 실제 호출에서
        401을 받고서야 원인을 알게 된다.
        """
        return bool(self.api_key) and not is_placeholder(self.api_key)

    @property
    def has_placeholder_key(self) -> bool:
        """값은 있으나 플레이스홀더인 상태 (안내 메시지 분기용)."""
        return bool(self.api_key) and is_placeholder(self.api_key)

    @property
    def masked_key(self) -> str:
        if self.has_placeholder_key:
            return "<플레이스홀더 — 실제 키로 교체 필요>"
        return _redact(self.api_key)

    def summary(self) -> str:
        """로그에 안전하게 남길 수 있는 설정 요약."""
        if self.configured:
            state = "설정됨"
        elif self.has_placeholder_key:
            state = "플레이스홀더"
        else:
            state = "미설정"
        return f"OpenRouter[{state}] model={self.model} key={self.masked_key}"

    def __str__(self) -> str:  # noqa: D105
        return self.summary()


def load_config(
    env_path: Optional[Path] = None,
    *,
    model_override: Optional[str] = None,
) -> LLMConfig:
    """환경변수와 `.env`에서 설정을 로드한다.

    우선순위: 명시적 인자 > 환경변수 > `.env` > 기본값.
    CI에서 시크릿으로 주입한 환경변수가 로컬 `.env`에 덮이면 안 된다.
    """
    file_env = load_env_file(env_path)

    def pick(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_env.get(key) or default

    return LLMConfig(
        api_key=pick("OPENROUTER_API_KEY"),
        model=model_override or pick("OPENROUTER_MODEL", DEFAULT_MODEL),
        base_url=pick("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
        app_url=pick("OPENROUTER_APP_URL"),
        app_title=pick("OPENROUTER_APP_TITLE", "agent-browser"),
        timeout_s=float(pick("OPENROUTER_TIMEOUT_S", "60") or 60),
        max_retries=int(pick("OPENROUTER_MAX_RETRIES", "2") or 2),
    )
