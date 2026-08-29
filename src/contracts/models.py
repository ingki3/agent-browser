"""핵심 데이터 모델 (Stage 0 동결).

PRD.md §4의 Pydantic V2 모델 정의를 그대로 구현한다.
Stage 0 Gate 0 통과 이후 본 모듈은 읽기 전용으로 동결되며,
수정이 필요한 경우 사람 감독자 승인 하에 Stage 0 재동결 절차를 거친다.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from contracts.errors import ErrorCode


class ActionType(str, Enum):
    """에이전트가 호출 가능한 19종 액션 툴 (PRD §4.1)."""

    # 1. 관찰 & 캡처 (Observation Primitives)
    OBSERVE_PAGE = "observe_page"
    TAKE_SCREENSHOT = "take_screenshot"
    # 2. 내비게이션 (Navigation)
    NAVIGATE = "navigate"
    GO_BACK = "go_back"
    RELOAD = "reload"
    # 3. 상호작용 (Interaction)
    CLICK = "click"
    TYPE_TEXT = "type_text"
    SELECT_OPTION = "select_option"
    CHECK_BOX = "check_box"
    SCROLL = "scroll"
    HOVER = "hover"
    PRESS_KEY = "press_key"
    # 4. 대기 및 추출 (Sync & Extraction)
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    # 5. 복합 웹 제어 (Complex Primitives)
    SWITCH_FRAME = "switch_frame"
    HANDLE_DIALOG = "handle_dialog"
    UPLOAD_FILE = "upload_file"
    DOWNLOAD_FILE = "download_file"
    TAB_CONTROL = "tab_control"


class ExecutionMode(str, Enum):
    """실행 모드 (PRD §3.3)."""

    INTERACTIVE = "interactive"
    UNATTENDED = "unattended"


# ---------------------------------------------------------------------------
# 1. 관찰 반환 모델 (Perception Return Models)
# ---------------------------------------------------------------------------


class BBox(BaseModel):
    """요소의 뷰포트 기준 바운딩 박스."""

    x: int
    y: int
    width: int
    height: int


class ObservedElement(BaseModel):
    """프루닝을 통과한 단일 후보 요소."""

    element_id: str  # "@e1", "@e2"
    role: str
    name: str
    value: Optional[str] = None
    bbox: BBox
    interactable: bool
    is_shadow: bool = False
    score: float


class ObserveResult(BaseModel):
    """`observe_page` 액션의 반환 페이로드 (PRD §4.2)."""

    title: str
    url: str
    snapshot_epoch: int
    elements: List[ObservedElement]
    axtree_summary: str
    token_count: int


# ---------------------------------------------------------------------------
# 2. 액션 결과 모델 (Action Result Model)
# ---------------------------------------------------------------------------


class ActionResult(BaseModel):
    """모든 액션 실행의 표준 반환 모델."""

    success: bool
    action: ActionType
    current_url: str
    snapshot_epoch: int
    tab_id: str
    healed: bool = False  # 자가 치유 적용 여부
    reobserve_required: bool = False  # 스크롤/TOCTOU 등으로 재관찰 필요 여부
    retry_safe: bool  # 멱등성/재시도 안전 여부
    downloaded_path: Optional[str] = None  # 다운로드 완료된 로컬 파일 경로
    popup_tab_id: Optional[str] = None  # 액션으로 생성된 신규 탭 ID
    error_code: Optional[ErrorCode] = None
    error_message: Optional[str] = None
    # 액션별 가변 페이로드. 계약상 불가피한 Any 예외 (src/AGENTS.md §6-1)
    data: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 3. A2UI 선언형 위젯 (PRD §6.1)
# ---------------------------------------------------------------------------


class DangerLevel(str, Enum):
    """HITL 승인 요청의 위험 등급."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConfirmDialog(BaseModel):
    """TUI 네이티브 모달로 렌더링되는 승인 요청 위젯.

    임의의 스크립트 실행을 원천 차단하기 위해 정형 스키마만 허용하며,
    `extra="forbid"`로 미정의 필드 주입을 거부한다.
    """

    model_config = {"extra": "forbid"}

    widget_type: str = Field(default="ConfirmDialog", frozen=True)
    title: str
    message: str
    confirm_label: str = "확인"
    cancel_label: str = "취소"
    danger_level: DangerLevel = DangerLevel.MEDIUM
