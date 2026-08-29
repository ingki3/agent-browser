"""표준 에러 코드 택소노미 (Stage 0 동결).

PRD.md §4.1 액션 툴 명세표 및 §3.4 / §5.1 / §5.3 본문에 등장하는
모든 에러 코드를 단일 Enum으로 집약한다.

에이전트는 임의의 문자열 예외를 던지지 않고 반드시 본 Enum 멤버를
`ActionResult.error_code`에 바인딩해야 한다 (src/AGENTS.md §6-2).
"""

from enum import Enum


class ErrorCode(str, Enum):
    """액션 실행 및 런타임 가드에서 발생 가능한 표준 에러 코드."""

    # --- 요소 탐색 및 상호작용 (Element & Interaction) ---
    ELEMENT_NOT_FOUND = "E_ELEMENT_NOT_FOUND"
    ELEMENT_NOT_INTERACTABLE = "E_ELEMENT_NOT_INTERACTABLE"
    TOCTOU_MISMATCH = "E_TOCTOU_MISMATCH"
    OPTION_NOT_FOUND = "E_OPTION_NOT_FOUND"
    SCROLL_FAILED = "E_SCROLL_FAILED"
    KEY_PRESS_FAILED = "E_KEY_PRESS_FAILED"

    # --- 내비게이션 및 페이지 수명주기 (Navigation & Lifecycle) ---
    NAVIGATE_TIMEOUT = "E_NAVIGATE_TIMEOUT"
    INVALID_URL = "E_INVALID_URL"
    NO_HISTORY = "E_NO_HISTORY"
    PAGE_CRASHED = "E_PAGE_CRASHED"
    TIMEOUT = "E_TIMEOUT"

    # --- 프레임 / Shadow DOM / 탭 (Context Switching) ---
    FRAME_NOT_FOUND = "E_FRAME_NOT_FOUND"
    SHADOW_ROOT_NOT_FOUND = "E_SHADOW_ROOT_NOT_FOUND"
    TAB_NOT_FOUND = "E_TAB_NOT_FOUND"
    TAB_LIMIT_EXCEEDED = "E_TAB_LIMIT_EXCEEDED"

    # --- 다이얼로그 및 파일 I/O (Dialog & File I/O) ---
    NO_DIALOG_PRESENT = "E_NO_DIALOG_PRESENT"
    FILE_NOT_FOUND = "E_FILE_NOT_FOUND"
    DOWNLOAD_TIMEOUT = "E_DOWNLOAD_TIMEOUT"
    DOWNLOAD_FAILED = "E_DOWNLOAD_FAILED"
    SCREENSHOT_FAILED = "E_SCREENSHOT_FAILED"

    # --- 전역 루프 및 리소스 가드 (PRD §3.4) ---
    MAX_STEPS_EXCEEDED = "E_MAX_STEPS_EXCEEDED"
    COST_LIMIT_EXCEEDED = "E_COST_LIMIT_EXCEEDED"
    TIER2_BUDGET_EXCEEDED = "E_TIER2_BUDGET_EXCEEDED"

    # --- 세션 / 보안 / 정책 (PRD §5.1, §5.3, §3.3) ---
    AUTH_EXPIRED = "E_AUTH_EXPIRED"
    CAPTCHA_DETECTED = "E_CAPTCHA_DETECTED"
    HITL_UNATTENDED_BLOCKED = "E_HITL_UNATTENDED_BLOCKED"

    # --- 기능 게이팅 (v1.1 이연 기능 호출 시) ---
    FEATURE_NOT_IMPLEMENTED = "E_FEATURE_NOT_IMPLEMENTED"
