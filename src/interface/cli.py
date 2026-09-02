"""CLI 진입점 (PRD §3.3 실행 모드).

    agent-browser serve   [--mode] [--allow-domain] [--secrets]  # MCP 서버 (stdio)
    agent-browser tui     [--mode]                     # Textual 대시보드
    agent-browser tools                                # 노출 툴 목록 확인

`--mode`는 PRD §3.3의 실행 모드 정책을 결정한다. 무인 모드가 기본값이며,
고위험 액션은 `--pre-approve`로 명시한 것만 통과한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import List, Optional, Sequence

from contracts import ExecutionMode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-browser",
        description="AI 에이전트 네이티브 헤드리스 브라우징 런타임",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- serve ---
    serve = sub.add_parser("serve", help="MCP 서버를 stdio로 구동합니다.")
    serve.add_argument(
        "--mode",
        choices=[m.value for m in ExecutionMode],
        default=ExecutionMode.UNATTENDED.value,
        help="실행 모드 (기본: unattended)",
    )
    serve.add_argument(
        "--allow-domain",
        action="append",
        default=[],
        metavar="DOMAIN",
        help="Egress 허용 도메인. 반복 지정 가능. 미지정 시 open_sandbox.",
    )
    serve.add_argument(
        "--pre-approve",
        action="append",
        default=[],
        metavar="ACTION:NAME",
        help="무인 모드에서 사전 승인할 고위험 액션 (예: click:결제 진행).",
    )
    serve.add_argument(
        "--secrets",
        metavar="PATH",
        help=(
            "자격증명 파일 경로 (dotenv 형식, 권한 0600 필수). "
            "type_text의 text가 등록된 키와 일치하면 실제 값으로 치환한다. "
            "LLM에는 키 이름만 노출된다."
        ),
    )

    # --- tui ---
    tui = sub.add_parser("tui", help="Textual 대시보드를 실행합니다.")
    tui.add_argument(
        "--mode",
        choices=[m.value for m in ExecutionMode],
        default=ExecutionMode.INTERACTIVE.value,
        help="실행 모드 (기본: interactive)",
    )

    # --- tools ---
    tools = sub.add_parser("tools", help="노출되는 MCP 툴 목록을 출력합니다.")
    tools.add_argument(
        "--json", action="store_true", help="JSON 스키마 전문을 출력합니다."
    )

    # --- llm-check ---
    llm = sub.add_parser(
        "llm-check",
        help="OpenRouter 설정과 자격증명을 확인합니다 (최소 비용 호출).",
    )
    llm.add_argument(
        "--model", default=None, help="확인할 모델 (미지정 시 .env의 OPENROUTER_MODEL)"
    )
    llm.add_argument(
        "--no-call",
        action="store_true",
        help="실제 API를 호출하지 않고 설정만 확인합니다 (비용 0).",
    )

    return parser


def _cmd_tools(as_json: bool) -> int:
    from interface.mcp_server import build_all_tools

    specs = build_all_tools()
    if as_json:
        print(json.dumps(specs, ensure_ascii=False, indent=2))
        return 0

    print(f"노출 툴 {len(specs)}종:")
    for spec in specs:
        required = spec["inputSchema"].get("required", [])
        hint = f" (필수: {', '.join(required)})" if required else ""
        print(f"  {spec['name']:28}{hint}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from interface.mcp_server import run_stdio

    try:
        asyncio.run(
            run_stdio(
                mode=ExecutionMode(args.mode),
                allowed_domains=tuple(args.allow_domain),
                secrets_path=args.secrets,
            )
        )
    except KeyboardInterrupt:
        return 130
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
    from interface.tui import DashboardState, build_app

    state = DashboardState(mode=ExecutionMode(args.mode))
    app = build_app(state)
    app.run()
    return 0


def _cmd_llm_check(args: argparse.Namespace) -> int:
    """OpenRouter 설정과 자격증명을 확인한다.

    실환경 검증 전에 키가 유효한지 먼저 알아야 한다. 태스크를 다 돌린 뒤
    401을 받으면 시간과 비용을 낭비한다.
    """
    from llm import load_config, probe_connection

    config = load_config(model_override=args.model)
    print(f"설정: {config.summary()}")

    if not config.configured:
        print()
        if config.has_placeholder_key:
            print("[-] .env의 OPENROUTER_API_KEY가 아직 플레이스홀더입니다.")
            print("    .env를 열어 실제 키로 교체하십시오.")
            print("    키 발급: https://openrouter.ai/keys")
        else:
            print("[-] OPENROUTER_API_KEY가 없습니다.")
            print("    1) cp .env.example .env")
            print("    2) .env를 열어 OPENROUTER_API_KEY를 채우십시오.")
            print("    또는: export OPENROUTER_API_KEY='sk-or-v1-...'")
        return 1

    if args.no_call:
        print("[+] 키가 설정되어 있습니다 (--no-call: 실제 호출은 생략).")
        return 0

    result = asyncio.run(probe_connection(config))
    if result["ok"]:
        print(f"[+] 연결 성공  model={result['model']}")
        print(
            f"    응답={result['reply']!r}  "
            f"토큰={result['prompt_tokens']}+{result['completion_tokens']}  "
            f"비용=${result['cost_usd']:.6f}"
        )
        return 0

    print(f"[-] 연결 실패: {result['reason']}")
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "tools":
        return _cmd_tools(args.json)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "tui":
        return _cmd_tui(args)
    if args.command == "llm-check":
        return _cmd_llm_check(args)

    parser.error(f"알 수 없는 명령: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
