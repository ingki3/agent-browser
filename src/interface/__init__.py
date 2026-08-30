"""WS-5 인터페이스 패키지 (PRD §6.1, §6.2, §6.3).

* `BrowserMCPServer` — 19종 툴을 노출하는 MCP 서버
* `StepTracer`       — 스텝 JSONL 트레이스 (마스킹 적용)
* `TraceSession`     — Playwright Trace 연계 리플레이
* `render_confirm_dialog` — A2UI ConfirmDialog 렌더러 (스크립트 실행 차단)
* `main`             — CLI 진입점
"""

from interface.cli import main
from interface.mcp_server import (
    TOOL_PREFIX,
    BrowserMCPServer,
    action_from_tool,
    build_all_tools,
    build_tool_schema,
    create_server,
    run_stdio,
    tool_name,
)
from interface.observability import StepRecord, StepTracer, TraceSession
from interface.tui import (
    DANGER_STYLES,
    DashboardState,
    build_app,
    escape_markup,
    render_confirm_dialog,
    render_element_list,
    render_trace_line,
)

__all__ = [
    # MCP
    "BrowserMCPServer",
    "build_all_tools",
    "build_tool_schema",
    "create_server",
    "run_stdio",
    "tool_name",
    "action_from_tool",
    "TOOL_PREFIX",
    # 관측성
    "StepTracer",
    "StepRecord",
    "TraceSession",
    # TUI / A2UI
    "DashboardState",
    "render_confirm_dialog",
    "render_element_list",
    "render_trace_line",
    "escape_markup",
    "build_app",
    "DANGER_STYLES",
    # CLI
    "main",
]
