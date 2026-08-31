"""WS-14 MCP SDK 바인딩 및 epoch 검증 테스트.

외부 사용자 테스트에서 발견된 결함들의 재발을 막는다.

배경:
`harness.mcp_smoke`는 `BrowserMCPServer.call_tool`을 직접 호출해 SDK
바인딩 계층을 통째로 우회했다. 그래서 19/19를 통과하면서도
`agent-browser serve`가 AttributeError로 즉사하는 것을 잡지 못했다.
README의 Claude Desktop 연동 경로 전체가 막힌 상태였다.
"""

from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# 1. SDK 메이저 호환
# ---------------------------------------------------------------------------


def test_create_server_does_not_crash():
    """create_server()가 어느 SDK 메이저에서도 살아야 한다.

    mcp 2.x의 lowlevel Server에는 list_tools/call_tool 데코레이터가 없다.
    데코레이터만 쓰면 AttributeError로 즉사한다.
    """
    from interface.mcp_server import create_server

    server, backend = create_server()
    assert server is not None
    assert backend is not None


def test_schema_field_is_resolved_dynamically():
    """Tool 스키마 필드명을 하드코딩하면 안 된다.

    mcp 1.x는 inputSchema, 2.x는 input_schema다. 한쪽으로 고정하면
    다른 메이저에서 tools/list가 통째로 실패한다.
    """
    from interface import mcp_server

    src = inspect.getsource(mcp_server.create_server)
    assert "Tool.model_fields" in src, (
        "스키마 필드명이 하드코딩되어 있습니다 — SDK 메이저 간 호환이 깨집니다"
    )


def test_both_registration_paths_exist():
    """1.x 데코레이터와 2.x 생성자 콜백 경로가 모두 있어야 한다."""
    from interface import mcp_server

    src = inspect.getsource(mcp_server.create_server)
    assert "on_list_tools" in src, "2.x 생성자 콜백 경로 없음"
    assert "list_tools()" in src, "1.x 데코레이터 경로 없음"


def test_tools_carry_input_schema():
    """생성된 Tool에 스키마가 실제로 담겨야 한다."""
    from mcp.types import Tool

    from interface.mcp_server import build_all_tools

    field = "input_schema" if "input_schema" in Tool.model_fields else "inputSchema"
    for spec in build_all_tools():
        tool = Tool(
            **{
                "name": spec["name"],
                "description": spec["description"],
                field: spec["inputSchema"],
            }
        )
        schema = getattr(tool, "input_schema", None) or getattr(
            tool, "inputSchema", None
        )
        assert isinstance(schema, dict) and schema.get("type") == "object", (
            f"{spec['name']}의 스키마가 비어 있습니다"
        )


# ---------------------------------------------------------------------------
# 2. 실사용 경로가 검증되는가 (게이트 미탐 방지)
# ---------------------------------------------------------------------------


def test_binding_harness_exists_and_uses_client_session():
    """SDK를 우회하지 않는 하네스가 있어야 한다.

    mcp_smoke는 backend.call_tool을 직접 부르므로 바인딩 결함을 못 잡는다.
    실제 ClientSession으로 왕복하는 하네스가 필요하다.
    """
    from harness import mcp_binding

    src = inspect.getsource(mcp_binding)
    assert "ClientSession" in src, "실제 MCP 클라이언트를 쓰지 않습니다"
    assert "create_server" in src, "SDK 바인딩 경로를 호출하지 않습니다"
    assert "list_tools" in src and "call_tool" in src


def test_binding_harness_is_registered_for_coverage():
    """커버리지 검사기에 등록되어야 한다."""
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "check_harness_coverage.py"
    assert "mcp_binding" in script.read_text(encoding="utf-8"), (
        "check_harness_coverage.py에 mcp_binding이 등록되지 않았습니다"
    )


# ---------------------------------------------------------------------------
# 3. epoch 검증 (계약상 필수 입력)
# ---------------------------------------------------------------------------

HTML = "<button id='b' onclick=\"document.title='clicked'\">클릭</button>"


async def _dispatch_with_epoch(epoch_value, omit=False):
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from contracts import ActionType
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.set_content(HTML)
        engine = PerceptionEngine()
        dispatcher = ActionDispatcher(DispatchContext(page=page, engine=engine))

        obs = await engine.observe_page(page=page, prune_top_n=10)
        params = {"element_id": obs.elements[0].element_id}
        if not omit:
            params["epoch"] = epoch_value
        result = await dispatcher.dispatch(ActionType.CLICK, params)
        title = await page.title()
        await browser.close()
        return result, title


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_correct_epoch_is_accepted():
    result, title = await _dispatch_with_epoch(0)
    assert result.success, result.error_message
    assert title == "clicked"


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_stale_epoch_is_rejected():
    """오래된 epoch은 거부되어야 한다 (계약상 필수 입력)."""
    from contracts import ErrorCode

    result, title = await _dispatch_with_epoch(999)
    assert not result.success, "틀린 epoch으로 액션이 성공했습니다"
    assert result.error_code is ErrorCode.TOCTOU_MISMATCH
    assert title != "clicked", "차단됐는데 부작용이 발생했습니다"


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_negative_epoch_is_rejected():
    from contracts import ErrorCode

    result, _ = await _dispatch_with_epoch(-1)
    assert not result.success
    assert result.error_code is ErrorCode.TOCTOU_MISMATCH


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_non_integer_epoch_is_rejected():
    """정수가 아닌 값도 명확히 거부한다 (예외로 터지면 안 된다)."""
    from contracts import ErrorCode

    result, _ = await _dispatch_with_epoch("무효한값")
    assert not result.success
    assert result.error_code is ErrorCode.TOCTOU_MISMATCH


# ---------------------------------------------------------------------------
# 4. selfcheck 기본값
# ---------------------------------------------------------------------------


def test_selfcheck_default_matches_actual_sites():
    """기본값이 실제 사이트 수와 어긋나면 CI가 인자로 우회하게 된다."""
    import harness.selfcheck as selfcheck
    from harness.mock_sites import MOCK_SITES

    src = inspect.getsource(selfcheck)
    assert "default=len(MOCK_SITES)" in src, (
        "기본값이 상수로 고정되어 있습니다 — 사이트 추가 시 어긋납니다"
    )
    assert len(MOCK_SITES) > 0


def test_ci_does_not_bypass_selfcheck_default():
    """CI가 --mock-sites로 우회하고 있으면 기본값이 죽은 설정이다."""
    from pathlib import Path

    ci = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8")
    assert "selfcheck --mock-sites" not in text, (
        "CI가 기본값을 우회하고 있습니다 — 기본값을 고치십시오"
    )
