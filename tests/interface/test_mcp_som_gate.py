"""MCP 서버 Tier-2 SoM 게이트 (PRD §8-2, Stage 4 Task 9).

`--som-vision` 없이 기동한 서버는 레거시 클라이언트 보호를 위해
`annotate_som=True`에 계속 `E_FEATURE_NOT_IMPLEMENTED`를 돌려야 하고,
켜면 태그 오버레이 스크린샷을 돌려야 한다. 게이트가 실제로 디스패처까지
전달되는지를 MCP 백엔드 경로로 확인한다.
"""

from __future__ import annotations

import pytest


async def _screenshot_via_server(som_enabled: bool):
    from contracts import ActionType
    from interface.mcp_server import BrowserMCPServer, tool_name

    async with BrowserMCPServer(som_enabled=som_enabled) as server:
        await server.call_tool(
            tool_name(ActionType.NAVIGATE),
            {"url": "data:text/html,<button id='b'>확인</button>"},
        )
        return await server.call_tool(
            tool_name(ActionType.TAKE_SCREENSHOT), {"annotate_som": True}
        )


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_som_gate_off_by_default_returns_not_implemented():
    from contracts import ErrorCode

    result = await _screenshot_via_server(som_enabled=False)
    assert result.success is False
    assert result.error_code is ErrorCode.FEATURE_NOT_IMPLEMENTED


@pytest.mark.requires_chromium
@pytest.mark.asyncio
async def test_som_gate_on_returns_tagged_screenshot():
    result = await _screenshot_via_server(som_enabled=True)
    assert result.success is True, result.error_message
    assert result.data["candidate_count"] >= 1
    assert result.data["som_tags"][0]["tag"] == "A1"
    assert result.data["image_tokens"] == 1600


def test_cli_serve_exposes_som_vision_flag():
    from interface.cli import _build_parser

    args = _build_parser().parse_args(["serve", "--som-vision"])
    assert args.som_vision is True
    args = _build_parser().parse_args(["serve"])
    assert args.som_vision is False
