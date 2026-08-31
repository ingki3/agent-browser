"""MCP SDK 바인딩 실사용 경로 검증 (WS-14).

`harness.mcp_smoke`는 `BrowserMCPServer.call_tool`을 직접 호출해
**SDK 바인딩 계층을 통째로 우회한다.** 그래서 19/19를 통과해도
`agent-browser serve`가 죽는 것을 잡지 못했다.

실제로 발생한 사고:
* mcp 2.x에는 `Server.list_tools` 데코레이터가 없어 `create_server()`가
  AttributeError로 즉사했다. Claude Desktop 연동 경로 전체가 막혔다.
* `Tool(input_schema=...)`는 1.x에서 필드명이 달라 tools/list가 실패한다.

이 하네스는 **실제 MCP 클라이언트 세션**으로 initialize -> tools/list ->
tools/call 왕복을 수행한다. SDK를 우회하지 않는다.

    python -m harness.mcp_binding
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List

from harness.result import MetricResult, emit, emit_error

#: 왕복 검증에 쓰는 툴. 브라우저를 띄우지 않는 관찰 계열만 사용한다.
#: (바인딩 검증이 목적이므로 액션 커버리지는 mcp_smoke가 담당한다)
PROBE_TOOLS = ("browser_observe_page",)


async def _run_session() -> Dict[str, Any]:
    """인메모리 스트림으로 실제 클라이언트-서버 세션을 연다."""
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from interface.mcp_server import create_server

    server, backend = create_server()
    findings: Dict[str, Any] = {
        "initialized": False,
        "tools_listed": 0,
        "tool_names": [],
        "schemas_valid": 0,
        "call_ok": False,
        "errors": [],
    }

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:

            async def _serve() -> None:
                try:
                    await server.run(
                        server_read,
                        server_write,
                        server.create_initialization_options(),
                    )
                except Exception as exc:  # noqa: BLE001
                    findings["errors"].append(f"서버 실행: {type(exc).__name__}: {exc}")

            tg.start_soon(_serve)

            try:
                async with ClientSession(client_read, client_write) as session:
                    await session.initialize()
                    findings["initialized"] = True

                    listed = await session.list_tools()
                    findings["tools_listed"] = len(listed.tools)
                    findings["tool_names"] = [t.name for t in listed.tools]

                    # 스키마가 실제로 전달됐는가 (필드명이 틀리면 비어 온다)
                    for tool in listed.tools:
                        schema = getattr(tool, "input_schema", None) or getattr(
                            tool, "inputSchema", None
                        )
                        if isinstance(schema, dict) and schema.get("type") == "object":
                            findings["schemas_valid"] += 1

                    # 실제 호출 왕복 — 인자 검증 오류라도 응답이 오면 바인딩은 정상
                    for name in PROBE_TOOLS:
                        if name not in findings["tool_names"]:
                            findings["errors"].append(f"툴 미노출: {name}")
                            continue
                        res = await session.call_tool(name, {})
                        if res.content:
                            findings["call_ok"] = True
                        else:
                            findings["errors"].append(f"{name}: 응답 본문 없음")
            except Exception as exc:  # noqa: BLE001
                findings["errors"].append(f"클라이언트: {type(exc).__name__}: {exc}")
            finally:
                tg.cancel_scope.cancel()

    await backend.close()
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MCP SDK 바인딩 실사용 경로 검증 (SDK를 우회하지 않음)"
    )
    parser.add_argument("--tools", type=int, default=19, help="기대 툴 수")
    args = parser.parse_args()

    try:
        findings = asyncio.run(_run_session())
    except ModuleNotFoundError as exc:
        return emit_error(
            "mcp_binding_roundtrip",
            f"MCP SDK를 불러올 수 없습니다: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        return emit_error(
            "mcp_binding_roundtrip",
            f"세션 수립 실패: {type(exc).__name__}: {exc}",
        )

    violations: List[str] = list(findings["errors"])
    if not findings["initialized"]:
        violations.append("initialize 실패")
    if findings["tools_listed"] != args.tools:
        violations.append(
            f"툴 수 불일치: {findings['tools_listed']} != {args.tools}"
        )
    if findings["schemas_valid"] != findings["tools_listed"]:
        violations.append(
            "입력 스키마가 전달되지 않은 툴이 있습니다 "
            f"({findings['schemas_valid']}/{findings['tools_listed']}) "
            "— Tool 스키마 필드명이 SDK 메이저와 어긋났을 수 있습니다"
        )
    if not findings["call_ok"]:
        violations.append("tools/call 왕복 실패")

    passed = not violations
    result = MetricResult(
        metric="mcp_binding_roundtrip",
        value=1.0 if passed else 0.0,
        threshold=1.0,
        samples=findings["tools_listed"],
        extra={
            "initialized": findings["initialized"],
            "tools_listed": findings["tools_listed"],
            "tools_required": args.tools,
            "schemas_valid": findings["schemas_valid"],
            "call_roundtrip": findings["call_ok"],
            "violations": violations or None,
            "note": "SDK 바인딩을 우회하지 않는 실사용 경로 검증",
        },
    )
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
