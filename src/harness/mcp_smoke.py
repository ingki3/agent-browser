"""19종 툴 MCP 클라이언트 E2E 왕복 스모크 (Gate 3-B 항목 3).

`python -m harness.mcp_smoke`

MCP 툴 정의가 계약과 일치하는지, 각 툴이 실제로 왕복 호출되는지 검증한다.

**커버리지 요건 (AGENTS.md §5 규칙 1)**:
19종 전수를 호출해야 한다. 일부만 호출하면 나머지가 파손돼도 통과하므로
미호출 툴이 있으면 exit 2로 측정을 무효화한다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Set

from contracts import ACTION_INPUT_MAP, ActionType

from harness.mock_sites import MockServer
from harness.result import MetricResult, emit, emit_error


def check_tool_definitions() -> List[str]:
    """툴 정의가 계약과 일치하는지 정적 검증한다."""
    from interface import build_all_tools, tool_name

    problems: List[str] = []
    specs = build_all_tools()
    names = {s["name"] for s in specs}

    for action in ActionType:
        expected = tool_name(action)
        if expected not in names:
            problems.append(f"툴 누락: {expected}")

    if len(specs) != len(ActionType):
        problems.append(
            f"툴 수 불일치: 정의 {len(specs)}종, 계약 {len(ActionType)}종"
        )

    for spec in specs:
        schema = spec["inputSchema"]
        if not isinstance(schema, dict) or "type" not in schema:
            problems.append(f"{spec['name']}: inputSchema가 유효한 JSON Schema가 아님")
        if not spec.get("description"):
            problems.append(f"{spec['name']}: description 누락")

    # 입력 모델이 있는 액션은 스키마에 properties가 있어야 한다.
    for action, model in ACTION_INPUT_MAP.items():
        spec = next((s for s in specs if s["name"] == tool_name(action)), None)
        if spec is None:
            continue
        props = spec["inputSchema"].get("properties", {})
        if model.model_fields and not props:
            problems.append(f"{spec['name']}: 입력 필드가 스키마에 반영되지 않음")

    return problems


#: 각 툴의 왕복 호출 인자. Mock 사이트에서 결정론적으로 동작해야 한다.
#: `_element` 키는 실행 시 관찰로 element_id를 채운다.
CALL_PLAN: Dict[ActionType, Dict[str, Any]] = {
    ActionType.OBSERVE_PAGE: {},
    ActionType.TAKE_SCREENSHOT: {},
    ActionType.NAVIGATE: {"_navigate_to": "s01_login"},
    ActionType.GO_BACK: {"_needs_history": True},
    ActionType.RELOAD: {},
    ActionType.CLICK: {"_element": ("button", "로그인")},
    ActionType.TYPE_TEXT: {"_element": ("textbox", "아이디"), "text": "tester"},
    ActionType.SELECT_OPTION: {
        "_site": "s21_widgets",
        "_element": ("combobox", "배송 방법"),
        "value": "express",
    },
    ActionType.CHECK_BOX: {
        "_site": "s21_widgets",
        "_element": ("checkbox", "약관 동의"),
        "checked": True,
    },
    ActionType.SCROLL: {"direction": "down", "amount": 300},
    ActionType.HOVER: {"_site": "s21_widgets", "_element": ("button", "도움말")},
    ActionType.PRESS_KEY: {"key": "Tab"},
    ActionType.WAIT_FOR: {"condition": "selector", "selector": "body", "timeout_ms": 1000},
    ActionType.EXTRACT: {"selector": "h1", "attributes": ["textContent"]},
    ActionType.SWITCH_FRAME: {"_site": "s07_iframe", "frame_selector": "iframe"},
    ActionType.HANDLE_DIALOG: {"_site": "s10_dialog", "accept": True},
    ActionType.UPLOAD_FILE: {
        "_site": "s21_widgets",
        "_element": ("textbox", "첨부 파일"),
        "_needs_temp_file": True,
    },
    ActionType.DOWNLOAD_FILE: {
        "_site": "s04_download",
        "_element": ("link", "CSV 내려받기"),
        "_needs_temp_dir": True,
    },
    ActionType.TAB_CONTROL: {"command": "list"},
}


async def _run_smoke() -> Dict[str, Any]:
    import tempfile
    from pathlib import Path

    from interface import BrowserMCPServer, tool_name

    called: Set[ActionType] = set()
    failures: List[str] = []
    schema_problems = check_tool_definitions()

    tmpdir = tempfile.mkdtemp(prefix="mcp_smoke_")
    upload = Path(tmpdir) / "sample.txt"
    upload.write_text("스모크 테스트 파일", encoding="utf-8")

    with MockServer() as server:
        srv = BrowserMCPServer(headless=True)
        await srv.start()
        try:
            for action in ActionType:
                plan = dict(CALL_PLAN.get(action, {}))
                site = plan.pop("_site", "s01_login")

                await srv._page.goto(  # noqa: SLF001 - 스모크 전용 접근
                    server.site_url(site), wait_until="domcontentloaded"
                )
                await srv._page.wait_for_timeout(300)  # noqa: SLF001

                if plan.pop("_needs_history", False):
                    await srv._page.goto(  # noqa: SLF001
                        server.site_url("s02_twofactor"), wait_until="domcontentloaded"
                    )
                if "_navigate_to" in plan:
                    plan["url"] = server.site_url(plan.pop("_navigate_to"))
                if plan.pop("_needs_temp_file", False):
                    plan["file_paths"] = [str(upload)]
                if plan.pop("_needs_temp_dir", False):
                    plan["save_dir"] = tmpdir

                target = plan.pop("_element", None)
                if target is not None:
                    role, name = target
                    observation = await srv._engine.observe_page(  # noqa: SLF001
                        page=srv._page, cdp=srv._cdp  # noqa: SLF001
                    )
                    match = next(
                        (
                            e
                            for e in observation.elements
                            if e.role == role and e.name == name
                        ),
                        None,
                    )
                    if match is None:
                        failures.append(f"{action.value}: 대상 요소 미발견 ({role}/{name})")
                        continue
                    plan["element_id"] = match.element_id
                    plan["epoch"] = observation.snapshot_epoch

                result = await srv.call_tool(tool_name(action), plan)
                called.add(action)

                # 왕복 자체가 성공해야 한다. 액션 실패는 허용하되,
                # 계약 위반(스키마 파싱 실패 등)은 실패로 본다.
                if result.error_code is not None and result.error_code.value in (
                    "E_FEATURE_NOT_IMPLEMENTED",
                    "E_INVALID_URL",
                ):
                    failures.append(
                        f"{action.value}: {result.error_code.value} — {result.error_message}"
                    )
        finally:
            await srv.close()

    return {
        "called": called,
        "failures": failures,
        "schema_problems": schema_problems,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP 툴 E2E 왕복 스모크")
    parser.add_argument(
        "--allow-partial-coverage",
        action="store_true",
        help="19종 전수 호출 검사를 생략한다 (디버깅 전용).",
    )
    args = parser.parse_args()

    try:
        import interface  # noqa: F401
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "mcp_tool_roundtrip",
                    "인터페이스(WS-5)가 아직 구현되지 않아 측정할 수 없습니다.",
                )
            )
        )

    try:
        metrics = asyncio.run(_run_smoke())
    except Exception as exc:  # noqa: BLE001
        sys.exit(int(emit_error("mcp_tool_roundtrip", f"스모크 실행 실패: {exc}")))

    for problem in metrics["schema_problems"]:
        print(f"[-] 스키마 결함: {problem}", file=sys.stderr)
    for failure in metrics["failures"][:10]:
        print(f"[-] {failure}", file=sys.stderr)

    if metrics["schema_problems"]:
        sys.exit(
            int(
                emit_error(
                    "mcp_tool_roundtrip",
                    f"툴 정의가 계약과 불일치합니다 ({len(metrics['schema_problems'])}건).",
                )
            )
        )

    called = metrics["called"]
    missing = sorted(a.value for a in ActionType if a not in called)
    if missing and not args.allow_partial_coverage:
        for name in missing:
            print(f"[-] 미호출 툴: {name}", file=sys.stderr)
        sys.exit(
            int(
                emit_error(
                    "mcp_tool_roundtrip",
                    f"19종 중 {len(missing)}종이 한 번도 호출되지 않았습니다. "
                    "해당 툴이 파손돼도 스모크가 통과하므로 신뢰할 수 없습니다.",
                )
            )
        )

    success = len(called) - len(metrics["failures"])
    result = MetricResult(
        metric="mcp_tool_roundtrip",
        value=round(success / len(ActionType), 4),
        threshold=1.0,
        samples=len(called),
        comparison="gte",
        extra={
            "tools_called": len(called),
            "tools_required": len(ActionType),
            "failures": metrics["failures"][:10] or None,
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
