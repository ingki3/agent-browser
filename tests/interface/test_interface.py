"""WS-5 인터페이스 테스트 (Gate 3-B 항목 1).

MCP 툴 생성, A2UI 렌더링 보안, 관측성 마스킹, CLI를 검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts import (
    ACTION_INPUT_MAP,
    ActionResult,
    ActionType,
    ConfirmDialog,
    DangerLevel,
    ErrorCode,
    ExecutionMode,
    ObservedElement,
    ObserveResult,
)
from interface import (
    DashboardState,
    StepTracer,
    action_from_tool,
    build_all_tools,
    build_tool_schema,
    escape_markup,
    render_confirm_dialog,
    render_element_list,
    render_trace_line,
    tool_name,
)
from interface.cli import main as cli_main


# ---------------------------------------------------------------------------
# 1. MCP 툴 생성 — 계약과의 일치
# ---------------------------------------------------------------------------


def test_exactly_19_tools_generated():
    assert len(build_all_tools()) == len(ActionType) == 19


def test_every_action_has_a_tool():
    names = {spec["name"] for spec in build_all_tools()}
    for action in ActionType:
        assert tool_name(action) in names


def test_tool_names_are_prefixed():
    for spec in build_all_tools():
        assert spec["name"].startswith("browser_")


def test_tool_name_roundtrip():
    for action in ActionType:
        assert action_from_tool(tool_name(action)) is action


def test_unknown_tool_name_returns_none():
    assert action_from_tool("browser_not_a_real_action") is None
    assert action_from_tool("other_server_click") is None


def test_every_tool_has_description():
    for spec in build_all_tools():
        assert spec["description"], spec["name"]
        # 설명이 액션 이름의 단순 반복이면 클라이언트에 도움이 되지 않는다.
        assert spec["description"] != spec["name"]


def test_input_schema_comes_from_contract():
    """툴 스키마는 손으로 쓰지 않고 Pydantic 모델에서 생성되어야 한다."""
    spec = build_tool_schema(ActionType.CLICK)
    props = spec["inputSchema"]["properties"]
    model_fields = set(ACTION_INPUT_MAP[ActionType.CLICK].model_fields)
    assert set(props).issubset(model_fields | {"element_id", "epoch"})
    assert "element_id" in props


def test_navigate_url_is_required_in_schema():
    spec = build_tool_schema(ActionType.NAVIGATE)
    assert "url" in spec["inputSchema"].get("required", [])


def test_tool_count_tracks_contract_automatically():
    """액션이 추가되면 툴도 자동으로 늘어나야 한다 (수동 목록 금지)."""
    assert len(build_all_tools()) == len(list(ActionType))


# ---------------------------------------------------------------------------
# 2. A2UI 렌더링 보안 — 스크립트 실행 차단
# ---------------------------------------------------------------------------


def test_confirm_dialog_renders_all_schema_fields():
    dialog = ConfirmDialog(
        title="결제 승인 요청",
        message="항공권 결제 금액 342,000원을 최종 승인하시겠습니까?",
        confirm_label="결제 진행",
        cancel_label="작업 취소",
        danger_level=DangerLevel.HIGH,
    )
    out = render_confirm_dialog(dialog)
    assert "결제 승인 요청" in out
    assert "342,000원" in out
    assert "결제 진행" in out
    assert "작업 취소" in out


def test_markup_in_dialog_is_escaped():
    """웹에서 온 문자열이 Rich 마크업으로 해석되면 안 된다."""
    dialog = ConfirmDialog(
        title="[bold red]가짜 경고[/bold red]",
        message="[blink]클릭하세요[/blink]",
        confirm_label="확인",
        cancel_label="취소",
        danger_level=DangerLevel.LOW,
    )
    out = render_confirm_dialog(dialog)
    # 이스케이프되어 리터럴로 남아야 한다.
    assert r"\[bold red]" in out
    assert r"\[blink]" in out


def test_escape_markup_handles_empty():
    assert escape_markup("") == ""
    assert escape_markup(None) == ""  # type: ignore[arg-type]


def test_element_names_are_escaped_in_list():
    observation = ObserveResult(
        snapshot_epoch=1,
        url="http://x.test",
        title="t",
        axtree_summary="button [red]악성[/red]",
        token_count=8,
        elements=[
            ObservedElement(
                element_id="@e1",
                role="button",
                name="[red]악성[/red]",
                bbox={"x": 0, "y": 0, "width": 100, "height": 30},
                interactable=True,
                score=1.0,
            )
        ],
    )
    rows = render_element_list(observation)
    assert r"\[red]" in rows[0]


def test_trace_line_escapes_detail():
    line = render_trace_line(1, "click", False, "[bold]주입[/bold]")
    assert r"\[bold]" in line


def test_dashboard_header_escapes_url():
    state = DashboardState(url="http://x.test/[evil]", epoch=3)
    assert r"\[evil]" in state.header_line()


def test_dashboard_header_shows_mode():
    assert "무인" in DashboardState(mode=ExecutionMode.UNATTENDED).header_line()
    assert "대화형" in DashboardState(mode=ExecutionMode.INTERACTIVE).header_line()


# ---------------------------------------------------------------------------
# 3. 관측성 — 마스킹이 기록 시점에 적용되는가
# ---------------------------------------------------------------------------


def _result(**kw) -> ActionResult:
    base: dict = dict(
        success=True,
        action=ActionType.TYPE_TEXT,
        current_url="http://x.test",
        snapshot_epoch=1,
        tab_id="tab-1",
        healed=False,
        reobserve_required=False,
        retry_safe=True,
        data={"latency_ms": 12.5},
    )
    base.update(kw)
    return ActionResult(**base)


def test_tracer_masks_secrets_before_writing(tmp_path: Path):
    """디스크에 평문 비밀이 남으면 안 된다."""
    log = tmp_path / "trace.jsonl"
    tracer = StepTracer(log)
    tracer.record(
        _result(),
        action_input={"password": "hunter2", "text": "정상 입력"},
    )

    written = log.read_text(encoding="utf-8")
    assert "hunter2" not in written
    assert "정상 입력" in written


def test_tracer_masks_authorization_header(tmp_path: Path):
    log = tmp_path / "trace.jsonl"
    tracer = StepTracer(log)
    tracer.record(
        _result(error_message="요청 실패: Authorization: Bearer eyJsecret123"),
    )
    written = log.read_text(encoding="utf-8")
    assert "eyJsecret123" not in written


def test_tracer_writes_one_json_line_per_step(tmp_path: Path):
    log = tmp_path / "trace.jsonl"
    tracer = StepTracer(log)
    for _ in range(3):
        tracer.record(_result())

    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        payload = json.loads(line)  # 각 줄이 독립 JSON이어야 한다
        assert payload["correlation_id"] == tracer.correlation_id


def test_tracer_numbers_steps_sequentially():
    tracer = StepTracer()
    for _ in range(3):
        tracer.record(_result())
    assert [r.step for r in tracer.records] == [1, 2, 3]


def test_tracer_tracks_success_rate():
    tracer = StepTracer()
    tracer.record(_result(success=True))
    tracer.record(_result(success=False, error_code=ErrorCode.TIMEOUT))
    assert tracer.success_rate == 0.5


def test_tracer_records_error_code_as_string():
    tracer = StepTracer()
    record = tracer.record(_result(success=False, error_code=ErrorCode.TIMEOUT))
    assert record.error_code == "E_TIMEOUT"


def test_tracer_without_path_keeps_records_in_memory():
    tracer = StepTracer()
    tracer.record(_result())
    assert tracer.step_count == 1


# ---------------------------------------------------------------------------
# 4. CLI
# ---------------------------------------------------------------------------


def test_cli_tools_lists_all_19(capsys):
    assert cli_main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "19종" in out
    for action in ActionType:
        assert tool_name(action) in out


def test_cli_tools_json_is_valid(capsys):
    assert cli_main(["tools", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 19
    assert all("inputSchema" in spec for spec in payload)


def test_cli_requires_subcommand():
    with pytest.raises(SystemExit):
        cli_main([])


def test_cli_rejects_unknown_mode():
    with pytest.raises(SystemExit):
        cli_main(["serve", "--mode", "not-a-mode"])
