"""WS-1 브라우저 코어 패키지.

* `BrowserCore`   — Playwright CDP 코어, 컨텍스트 풀, 탭 수명주기
* `SessionStore`  — AES-256-GCM + Argon2id 세션 스토리지
* `detect_expiry` — 3단계 세션 만료 감지 프로브
"""

from browser.core import (
    BrowserCore,
    BrowserCoreError,
    ManagedContext,
    ManagedTab,
)
from browser.session_probe import (
    AUTH_FAILURE_STATUSES,
    PageSignals,
    ProbeResult,
    ProbeTier,
    ProfileProbeConfig,
    detect_expiry,
)
from browser.session_store import (
    CI_ENV_VAR,
    FILE_MODE,
    DecryptionError,
    KeyResolution,
    KeyUnavailableError,
    SessionStore,
    SessionStoreError,
    resolve_passphrase,
)

__all__ = [
    # 코어
    "BrowserCore",
    "BrowserCoreError",
    "ManagedContext",
    "ManagedTab",
    # 세션 스토리지
    "SessionStore",
    "SessionStoreError",
    "KeyUnavailableError",
    "DecryptionError",
    "KeyResolution",
    "resolve_passphrase",
    "CI_ENV_VAR",
    "FILE_MODE",
    # 만료 프로브
    "detect_expiry",
    "PageSignals",
    "ProbeResult",
    "ProbeTier",
    "ProfileProbeConfig",
    "AUTH_FAILURE_STATUSES",
]
