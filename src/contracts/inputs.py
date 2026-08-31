"""19종 액션 입력 모델 및 ACTION_INPUT_MAP (Stage 0 동결).

PRD.md §4.1 "19종 액션 툴 전수 명세표"의 '주요 입력 필드' 열을 1:1로 코드화한다.
`ACTION_INPUT_MAP`은 `ActionType` → 입력 모델 클래스 매핑으로, 디스패처와
MCP 툴 스키마 자동 생성의 단일 진실 공급원이다.

공통 규약:
* `element_id`를 사용하는 액션은 `epoch`(스냅샷 버전)를 함께 요구한다.
  `element_id`는 `observe_page`가 발급한 특정 스냅샷에서만 유효하기 때문이다.
* `element_id`와 `selector`를 모두 받는 액션은 **정확히 하나만** 지정해야 한다.
"""

from typing import List, Literal, Optional, Type

from pydantic import BaseModel, Field, model_validator

from contracts.models import ActionType
from contracts.thresholds import DEFAULT_PRUNE_TOP_N


class _ElementTargetInput(BaseModel):
    """`element_id` + `epoch` 조합을 요구하는 액션의 공통 베이스.

    element_id는 특정 snapshot_epoch에서 발급된 핸들이므로,
    실행 직전 staleness 검증(PRD §4.2)을 위해 epoch가 반드시 동반되어야 한다.
    """

    element_id: str
    epoch: int


# ---------------------------------------------------------------------------
# 1. 관찰 & 캡처 (Observation Primitives)
# ---------------------------------------------------------------------------


class ObservePageInput(BaseModel):
    tab_id: Optional[str] = None
    prune_top_n: int = DEFAULT_PRUNE_TOP_N
    force_full_tree: bool = False


class TakeScreenshotInput(BaseModel):
    tab_id: Optional[str] = None
    #: v1.1 활성화 전까지 True 지정 시 E_FEATURE_NOT_IMPLEMENTED 반환 (PRD §8-2)
    annotate_som: bool = False
    full_page: bool = False


# ---------------------------------------------------------------------------
# 2. 내비게이션 (Navigation)
# ---------------------------------------------------------------------------


class NavigateInput(BaseModel):
    url: str
    wait_until: Literal[
        "domcontentloaded", "load", "networkidle", "commit"
    ] = "domcontentloaded"
    timeout_ms: int = 15_000


class GoBackInput(BaseModel):
    timeout_ms: int = 10_000


class ReloadInput(BaseModel):
    ignore_cache: bool = False


# ---------------------------------------------------------------------------
# 3. 상호작용 (Interaction)
# ---------------------------------------------------------------------------


class ClickInput(BaseModel):
    element_id: Optional[str] = None
    selector: Optional[str] = None
    epoch: Optional[int] = None
    expected_role: Optional[str] = None
    expected_name: Optional[str] = None
    button: Literal["left", "right", "middle"] = "left"

    @model_validator(mode="after")
    def check_target_and_epoch(self) -> "ClickInput":
        # 1. element_id와 selector 중 정확히 하나만 지정 강제
        if bool(self.element_id) == bool(self.selector):
            raise ValueError("element_id와 selector 중 정확히 하나만 지정해야 합니다.")
        # 2. element_id 사용 시에만 epoch 필수 검증
        if self.element_id and self.epoch is None:
            raise ValueError("element_id를 지정할 경우 snapshot epoch는 필수입니다.")
        return self


class TypeTextInput(_ElementTargetInput):
    text: str
    clear_before: bool = True
    press_enter: bool = False


class SelectOptionInput(_ElementTargetInput):
    value: Optional[str] = None
    index: Optional[int] = None

    @model_validator(mode="after")
    def check_selector_target(self) -> "SelectOptionInput":
        if (self.value is None) == (self.index is None):
            raise ValueError("value와 index 중 정확히 하나만 지정해야 합니다.")
        return self


class CheckBoxInput(_ElementTargetInput):
    checked: bool


class ScrollInput(BaseModel):
    direction: Literal["up", "down"]
    distance: int = 500
    target_element_id: Optional[str] = None


class HoverInput(_ElementTargetInput):
    pass


class PressKeyInput(BaseModel):
    key: str = Field(..., examples=["Enter", "Tab", "Escape"])


# ---------------------------------------------------------------------------
# 4. 대기 및 추출 (Sync & Extraction)
# ---------------------------------------------------------------------------


class WaitForInput(BaseModel):
    condition: Literal["selector", "network_idle", "spa_route", "stabilize"]
    selector: Optional[str] = None
    timeout_ms: int = 10_000

    @model_validator(mode="after")
    def check_selector_required(self) -> "WaitForInput":
        if self.condition == "selector" and not self.selector:
            raise ValueError('condition="selector"인 경우 selector는 필수입니다.')
        return self


class ExtractInput(BaseModel):
    selector: str
    attributes: List[str] = Field(default_factory=list)
    extract_all: bool = False


# ---------------------------------------------------------------------------
# 5. 복합 웹 제어 (Complex Primitives)
# ---------------------------------------------------------------------------


class SwitchFrameInput(BaseModel):
    frame_selector: Optional[str] = None
    shadow_root_selector: Optional[str] = None

    @model_validator(mode="after")
    def check_exactly_one_target(self) -> "SwitchFrameInput":
        if bool(self.frame_selector) == bool(self.shadow_root_selector):
            raise ValueError(
                "frame_selector와 shadow_root_selector 중 정확히 하나만 지정해야 합니다."
            )
        return self


class HandleDialogInput(BaseModel):
    accept: bool = True
    prompt_text: Optional[str] = None


class UploadFileInput(_ElementTargetInput):
    file_paths: List[str] = Field(..., min_length=1)


class DownloadFileInput(BaseModel):
    trigger_element_id: str
    save_dir: str
    epoch: int


class TabControlInput(BaseModel):
    command: Literal["create", "switch", "close", "list"]
    tab_id: Optional[str] = None
    url: Optional[str] = None

    @model_validator(mode="after")
    def check_command_args(self) -> "TabControlInput":
        if self.command in ("switch", "close") and not self.tab_id:
            raise ValueError(f'command="{self.command}"인 경우 tab_id는 필수입니다.')
        return self


# ---------------------------------------------------------------------------
# ActionType → 입력 모델 매핑 (디스패처 및 MCP 스키마 생성의 단일 공급원)
# ---------------------------------------------------------------------------

ACTION_INPUT_MAP: dict[ActionType, Type[BaseModel]] = {
    ActionType.OBSERVE_PAGE: ObservePageInput,
    ActionType.TAKE_SCREENSHOT: TakeScreenshotInput,
    ActionType.NAVIGATE: NavigateInput,
    ActionType.GO_BACK: GoBackInput,
    ActionType.RELOAD: ReloadInput,
    ActionType.CLICK: ClickInput,
    ActionType.TYPE_TEXT: TypeTextInput,
    ActionType.SELECT_OPTION: SelectOptionInput,
    ActionType.CHECK_BOX: CheckBoxInput,
    ActionType.SCROLL: ScrollInput,
    ActionType.HOVER: HoverInput,
    ActionType.PRESS_KEY: PressKeyInput,
    ActionType.WAIT_FOR: WaitForInput,
    ActionType.EXTRACT: ExtractInput,
    ActionType.SWITCH_FRAME: SwitchFrameInput,
    ActionType.HANDLE_DIALOG: HandleDialogInput,
    ActionType.UPLOAD_FILE: UploadFileInput,
    ActionType.DOWNLOAD_FILE: DownloadFileInput,
    ActionType.TAB_CONTROL: TabControlInput,
}
