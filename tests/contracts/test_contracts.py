"""Stage 0 계약 동결 검증 테스트.

Gate 0의 기계 검증 항목을 pytest로 재현한다. 계약이 PRD.md §4 / §1.5와
일치하는지, validator가 실제로 동작하는지를 확인한다.
"""

import pytest
from pydantic import ValidationError

import contracts
from contracts import (
    ACTION_INPUT_MAP,
    ActionResult,
    ActionType,
    BBox,
    CheckBoxInput,
    ClickInput,
    ConfirmDialog,
    DangerLevel,
    ErrorCode,
    NavigateInput,
    ObservedElement,
    ObserveResult,
    SelectOptionInput,
    SwitchFrameInput,
    TabControlInput,
    TypeTextInput,
    UploadFileInput,
    WaitForInput,
    thresholds,
)


# ---------------------------------------------------------------------------
# 1. 계약 완전성 (Gate 0 기계 검증 항목)
# ---------------------------------------------------------------------------


def test_action_type_has_19_members():
    """PRD §4.1 표와 동일하게 19종이어야 한다."""
    assert len(ActionType) == 19


def test_exactly_19_input_models_exported():
    """Gate 0 검증 명령어와 동일한 방식으로 19종 입력 모델을 센다."""
    input_models = [m for m in dir(contracts) if m.endswith("Input")]
    assert len(input_models) == 19, f"발견된 모델: {sorted(input_models)}"


def test_action_input_map_covers_every_action():
    """모든 ActionType이 입력 모델과 1:1 매핑되어야 한다."""
    assert set(ACTION_INPUT_MAP.keys()) == set(ActionType)
    assert len(ACTION_INPUT_MAP) == 19


def test_action_input_map_values_are_unique():
    """서로 다른 액션이 같은 입력 모델을 공유하지 않아야 한다."""
    models = list(ACTION_INPUT_MAP.values())
    assert len(set(models)) == len(models)


def test_error_code_has_at_least_20_members():
    """Gate 0은 20종 이상을 요구한다."""
    assert len(ErrorCode) >= 20


def test_error_code_values_follow_prefix_convention():
    for code in ErrorCode:
        assert code.value.startswith("E_"), code


# ---------------------------------------------------------------------------
# 2. KPI 상수 정합성 (PRD §1.5)
# ---------------------------------------------------------------------------


def test_threshold_constants_match_prd():
    assert thresholds.RECALL_AT_20 == 0.95
    assert thresholds.ACTION_SUCCESS_RATE == 0.92
    assert thresholds.SELF_HEALING_RATE == 0.80
    assert thresholds.FLAKY_RATE == 0.02
    assert thresholds.TASK_SUCCESS_RATE == 0.60
    assert thresholds.OBSERVATION_TOKENS_P50 == 2_500
    assert thresholds.OBSERVATION_TOKENS_P95 == 6_500
    assert thresholds.COMPLEX_LATENCY_MS_P95 == 2_200


def test_step_latency_budget_is_internally_consistent():
    """관찰(300ms) + 액션(500ms) = 스텝 지연(800ms) 이어야 한다."""
    assert (
        thresholds.OBSERVE_LATENCY_MS_P50 + thresholds.ACTION_LATENCY_MS_P50
        == thresholds.STEP_LATENCY_MS_P50
    )


def test_tier2_unattended_budget_respects_trigger_rate():
    """무인 3회 / 30스텝 = 10%로 발동 비율 상한과 일치해야 한다."""
    ratio = thresholds.TIER2_MAX_CALLS_UNATTENDED / thresholds.MAX_STEPS_PER_TASK
    assert ratio == pytest.approx(thresholds.TIER2_TRIGGER_RATE)


# ---------------------------------------------------------------------------
# 3. ClickInput validator (REVIEW_12 P1-1 / P1-2)
# ---------------------------------------------------------------------------


def test_click_requires_a_target():
    with pytest.raises(ValidationError):
        ClickInput(epoch=1)


def test_click_rejects_both_targets():
    """'적어도 하나'가 아니라 '정확히 하나'여야 한다."""
    with pytest.raises(ValidationError):
        ClickInput(element_id="@e1", selector="#btn", epoch=1)


def test_click_with_element_id_requires_epoch():
    with pytest.raises(ValidationError):
        ClickInput(element_id="@e1")


def test_click_with_selector_does_not_require_epoch():
    """selector 경로는 관찰을 거치지 않으므로 epoch가 불필요하다."""
    action = ClickInput(selector="#submit")
    assert action.epoch is None
    assert action.button == "left"


def test_click_with_element_id_and_epoch_is_valid():
    action = ClickInput(element_id="@e1", epoch=7, expected_role="button")
    assert action.element_id == "@e1"


# ---------------------------------------------------------------------------
# 4. 그 외 입력 모델 validator
# ---------------------------------------------------------------------------


def test_element_target_inputs_require_epoch():
    """element_id 기반 액션은 epoch 없이 생성될 수 없다."""
    for model in (TypeTextInput, CheckBoxInput, UploadFileInput):
        with pytest.raises(ValidationError):
            model(element_id="@e1")  # type: ignore[call-arg]


def test_type_text_defaults():
    action = TypeTextInput(element_id="@e2", epoch=3, text="hello")
    assert action.clear_before is True
    assert action.press_enter is False


def test_select_option_requires_exactly_one_of_value_or_index():
    with pytest.raises(ValidationError):
        SelectOptionInput(element_id="@e1", epoch=1)
    with pytest.raises(ValidationError):
        SelectOptionInput(element_id="@e1", epoch=1, value="a", index=0)
    assert SelectOptionInput(element_id="@e1", epoch=1, value="a").index is None


def test_wait_for_selector_condition_requires_selector():
    with pytest.raises(ValidationError):
        WaitForInput(condition="selector")
    assert WaitForInput(condition="network_idle").selector is None


def test_switch_frame_requires_exactly_one_target():
    with pytest.raises(ValidationError):
        SwitchFrameInput()
    with pytest.raises(ValidationError):
        SwitchFrameInput(frame_selector="iframe", shadow_root_selector="#host")
    assert SwitchFrameInput(frame_selector="iframe#main").shadow_root_selector is None


def test_tab_control_switch_and_close_require_tab_id():
    for command in ("switch", "close"):
        with pytest.raises(ValidationError):
            TabControlInput(command=command)  # type: ignore[arg-type]
    assert TabControlInput(command="list").tab_id is None


def test_upload_file_requires_non_empty_paths():
    with pytest.raises(ValidationError):
        UploadFileInput(element_id="@e1", epoch=1, file_paths=[])


def test_navigate_accepts_commit_wait_until():
    """Playwright가 지원하는 4종 wait_until을 모두 수용해야 한다."""
    for state in ("domcontentloaded", "load", "networkidle", "commit"):
        assert NavigateInput(url="https://example.com", wait_until=state).wait_until == state


def test_navigate_rejects_unknown_wait_until():
    with pytest.raises(ValidationError):
        NavigateInput(url="https://example.com", wait_until="idle")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. 반환 모델
# ---------------------------------------------------------------------------


def test_observe_result_round_trip():
    payload = {
        "title": "테스트 페이지",
        "url": "https://example.com/",
        "snapshot_epoch": 104,
        "elements": [
            {
                "element_id": "@e1",
                "role": "button",
                "name": "가는 날 선택",
                "value": "2026.09.01",
                "bbox": {"x": 10, "y": 20, "width": 120, "height": 40},
                "interactable": True,
                "is_shadow": False,
                "score": 0.93,
            }
        ],
        "axtree_summary": "button 가는 날 선택",
        "token_count": 1820,
    }
    result = ObserveResult.model_validate(payload)
    assert result.elements[0].bbox.width == 120
    assert result.model_dump()["elements"][0]["element_id"] == "@e1"


def test_observed_element_defaults():
    element = ObservedElement(
        element_id="@e9",
        role="link",
        name="다음",
        bbox=BBox(x=0, y=0, width=10, height=10),
        interactable=True,
        score=0.5,
    )
    assert element.is_shadow is False
    assert element.value is None


def test_action_result_binds_error_code_enum():
    result = ActionResult(
        success=False,
        action=ActionType.CLICK,
        current_url="https://example.com/",
        snapshot_epoch=12,
        tab_id="tab-1",
        retry_safe=True,
        error_code=ErrorCode.TOCTOU_MISMATCH,
    )
    assert result.error_code is ErrorCode.TOCTOU_MISMATCH
    assert result.model_dump()["error_code"] == "E_TOCTOU_MISMATCH"
    assert result.data == {}


def test_action_result_rejects_unknown_error_code():
    with pytest.raises(ValidationError):
        ActionResult(
            success=False,
            action=ActionType.CLICK,
            current_url="https://example.com/",
            snapshot_epoch=1,
            tab_id="tab-1",
            retry_safe=False,
            error_code="E_TYPO_NOT_A_REAL_CODE",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 6. A2UI ConfirmDialog (PRD §6.1 스크립트 주입 배제)
# ---------------------------------------------------------------------------


def test_confirm_dialog_matches_prd_example():
    dialog = ConfirmDialog(
        title="결제 승인 요청",
        message="항공권 결제 금액 342,000원을 최종 승인하시겠습니까?",
        confirm_label="결제 진행",
        cancel_label="작업 취소",
        danger_level=DangerLevel.HIGH,
    )
    assert dialog.widget_type == "ConfirmDialog"
    assert dialog.model_dump()["danger_level"] == "high"


def test_confirm_dialog_forbids_extra_fields():
    """정형 스키마 외 필드 주입을 거부해야 한다 (스크립트 주입 방어)."""
    with pytest.raises(ValidationError):
        ConfirmDialog.model_validate(
            {
                "title": "t",
                "message": "m",
                "on_click": "alert('xss')",
            }
        )


# ---------------------------------------------------------------------------
# 7. Protocol 계약
# ---------------------------------------------------------------------------


def test_protocols_are_runtime_checkable():
    """구조적 서브타이핑으로 구현체를 검증할 수 있어야 한다."""

    class _StubDispatcher:
        async def dispatch(self, action, params, epoch):  # noqa: ANN001, D102
            return None

    assert isinstance(_StubDispatcher(), contracts.ActionDispatcherProtocol)


def test_protocol_rejects_incomplete_implementation():
    class _Missing:
        pass

    assert not isinstance(_Missing(), contracts.PerceptionEngineProtocol)
