"""`contracts` 패키지 최상위 Re-export (Stage 0 동결).

모든 워크스트림은 서브모듈 경로가 아닌 최상위에서 import한다:

    from contracts import ActionType, ActionResult, ClickInput, ErrorCode

Gate 0 검증 명령어(`import contracts; ... m.endswith('Input')`)가 본 네임스페이스를
기준으로 19종 입력 모델 존재를 확인하므로, 신규 모델 추가 시 `__all__`과
re-export를 함께 갱신해야 한다.
"""

from contracts import thresholds
from contracts.errors import ErrorCode
from contracts.inputs import (
    ACTION_INPUT_MAP,
    CheckBoxInput,
    ClickInput,
    DownloadFileInput,
    ExtractInput,
    GoBackInput,
    HandleDialogInput,
    HoverInput,
    NavigateInput,
    ObservePageInput,
    PressKeyInput,
    ReloadInput,
    ScrollInput,
    SelectOptionInput,
    SwitchFrameInput,
    TabControlInput,
    TakeScreenshotInput,
    TypeTextInput,
    UploadFileInput,
    WaitForInput,
)
from contracts.models import (
    ActionResult,
    ActionType,
    BBox,
    ConfirmDialog,
    DangerLevel,
    ExecutionMode,
    ObservedElement,
    ObserveResult,
)
from contracts.protocols import (
    ActionDispatcherProtocol,
    BrowserCoreProtocol,
    PerceptionEngineProtocol,
)

__all__ = [
    # 열거형 및 상수 모듈
    "ActionType",
    "ExecutionMode",
    "ErrorCode",
    "DangerLevel",
    "thresholds",
    # 관찰 반환 모델
    "BBox",
    "ObservedElement",
    "ObserveResult",
    # 액션 결과 모델
    "ActionResult",
    # A2UI 위젯
    "ConfirmDialog",
    # 19종 액션 입력 모델
    "ObservePageInput",
    "TakeScreenshotInput",
    "NavigateInput",
    "GoBackInput",
    "ReloadInput",
    "ClickInput",
    "TypeTextInput",
    "SelectOptionInput",
    "CheckBoxInput",
    "ScrollInput",
    "HoverInput",
    "PressKeyInput",
    "WaitForInput",
    "ExtractInput",
    "SwitchFrameInput",
    "HandleDialogInput",
    "UploadFileInput",
    "DownloadFileInput",
    "TabControlInput",
    # 매핑
    "ACTION_INPUT_MAP",
    # 모듈 간 호출 규약
    "PerceptionEngineProtocol",
    "BrowserCoreProtocol",
    "ActionDispatcherProtocol",
]
