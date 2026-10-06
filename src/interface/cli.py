"""CLI 진입점 (PRD §3.3 실행 모드).

    agent-browser serve   [--mode] [--allow-domain] [--secrets] [--som-vision]  # MCP 서버 (stdio)
                          [--browser {headless,human,user-chrome}] [--chrome-profile] [--keep-open]
                          [--nav-settle {on,off}] [--max-result-chars N]
                          [--allow-private-network] [--block-loopback] [--profile NAME]
    agent-browser tui     [--mode]                     # Textual 대시보드
    agent-browser tools                                # 노출 툴 목록 확인
    agent-browser session login <프로파일> --url <주소>  # 사람이 직접 로그인
    agent-browser run --url <주소> --goal <목표> [--human|--user-chrome] [--handoff]  # 목표 1개 실행
    agent-browser control take|release|status [--server ID]   # 사람: serve 의 조작권 (WS-29)
    agent-browser approve <approval_id> [--server ID] [--code N|--deny]  # 사람: 고위험 행동 승인(창의 확인 코드)
    agent-browser profile list|remove NAME [--yes]     # serve --profile 영속 프로필 관리 (WS-32)

`--mode`는 PRD §3.3의 실행 모드 정책을 결정한다. 무인 모드가 기본값이며,
고위험 액션은 `--pre-approve`로 명시한 것만 통과한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
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
    serve.add_argument(
        "--som-vision",
        action="store_true",
        help=(
            "Tier-2 SoM 시각 폴백 활성화 (v1.1). take_screenshot(annotate_som=True)가 "
            "태그 오버레이 스크린샷을 반환한다. 미지정 시 E_FEATURE_NOT_IMPLEMENTED."
        ),
    )
    serve.add_argument(
        "--browser",
        choices=["headless", "human", "user-chrome"],
        default="headless",
        help=(
            "브라우저 방식 (기본 headless). human: 창 보이는 Chromium(위장 없음). "
            "user-chrome: 설치된 Chrome 을 전용 프로필로 띄워 붙음(차단 사이트용)."
        ),
    )
    serve.add_argument(
        "--chrome-profile",
        default=None,
        metavar="PATH",
        help="--browser user-chrome 전용: 전용 Chrome 프로필 폴더 "
        "(기본 ~/.agent-browser/chrome-profile, 평소 Chrome 프로필은 거부)",
    )
    serve.add_argument(
        "--keep-open",
        action="store_true",
        help="--browser user-chrome 전용: 서버 종료 시 띄운 Chrome 을 닫지 않음",
    )
    serve.add_argument(
        "--nav-settle",
        choices=["on", "off"],
        default="on",
        help=(
            "이동 대기 (기본 on). on: click·press_key 등 뒤 새 문서가 뜰 때까지 기다림 — "
            "대가로 이동 없는 click/press_key 가 약 0.2초 느려짐. off: 기다리지 않음 — "
            "이동 뒤 새 문서 확인은 부르는 쪽이 wait_for/observe_page 로 해야 함."
        ),
    )
    serve.add_argument(
        "--max-result-chars",
        type=_max_result_chars,
        default=None,
        metavar="N",
        help=(
            "observe_page·extract 응답 크기 상한(글자, 기본 20000 — Claude Code MCP 결과 한도 "
            "25,000토큰, 한글 1자≈1토큰 기준). 넘으면 항목 경계에서 자르고 data.truncated 로 알림."
        ),
    )

    serve.add_argument(
        "--allow-private-network",
        action="store_true",
        help=(
            "사설·링크로컬·CGNAT(100.64/10)·IPv6 ULA 대역 접속 허용(기본 차단). 로컬 NAS·사내망용. "
            "클라우드 메타데이터·0.0.0.0 은 이 옵션과 무관하게 차단."
        ),
    )
    serve.add_argument(
        "--block-loopback",
        action="store_true",
        help="루프백(127/8·::1·localhost)도 차단(기본 허용 — 로컬 Mock·개발 서버용).",
    )
    serve.add_argument(
        "--approval-ttl",
        type=_approval_ttl,
        default=None,
        metavar="SEC",
        help="고위험 행동 승인 증표 수명(초, 기본 1800). 사람이 `agent-browser approve` 로 승인.",
    )
    serve.add_argument(
        "--profile",
        default=None,
        metavar="NAME",
        help=(
            "이름 붙인 영속 프로필로 시작(로그인 유지, WS-32). 폴더 ~/.agent-browser/profiles/"
            "serve-NAME(권한 700). NAME 은 영문 소문자·숫자·하이픈 1~32자. 미지정 시 매번 빈 브라우저. "
            "--browser human 으로 한 번 로그인하면 이후 headless 에서도 유지."
        ),
    )

    # --- control / approve (WS-29: 사람 쪽 신호 통로) ---
    control = sub.add_parser("control", help="사람: 실행 중인 serve 의 조작권을 가져오거나 돌려줍니다.")
    control.add_argument("op", choices=["take", "release", "status"])
    control.add_argument("--server", default=None, metavar="ID",
                         help="서버 id (serve 시작 때 stderr 에 출력). 하나만 떠 있으면 생략 가능.")
    control.add_argument("--json", action="store_true", help="status 를 JSON 으로 출력")
    approve = sub.add_parser("approve", help="사람: 에이전트가 요청한 고위험 행동 하나를 승인합니다.")
    approve.add_argument("approval_id")
    approve.add_argument("--server", default=None, metavar="ID", help="서버 id (생략 시 자동 탐색)")
    approve.add_argument("--code", default=None, metavar="N",
                         help="브라우저 창에 뜬 6자리 확인 코드(생략하면 창에 코드를 띄우고 입력받음)")
    approve.add_argument("--yes", action="store_true",
                         help="(호환용) 코드를 대신하지 못함 — 승인에는 --code 가 필요")
    approve.add_argument("--deny", action="store_true", help="승인하지 않고 거절로 기록")

    # --- profile (WS-32) ---
    from interface import profile_cli

    profile_cli.add_parser(sub)

    # --- tui ---
    tui = sub.add_parser("tui", help="Textual 대시보드를 실행합니다.")
    tui.add_argument(
        "--mode",
        choices=[m.value for m in ExecutionMode],
        default=ExecutionMode.INTERACTIVE.value,
        help="실행 모드 (기본: interactive)",
    )

    # --- session ---
    session = sub.add_parser(
        "session",
        help="로그인 세션을 저장·조회합니다 (비밀번호는 저장하지 않습니다).",
    )
    session_sub = session.add_subparsers(dest="session_action", required=True)

    s_login = session_sub.add_parser(
        "login",
        help="브라우저를 띄워 사람이 직접 로그인하고 세션을 암호화 저장합니다.",
    )
    s_login.add_argument("profile", help="프로파일 이름 (예: naver)")
    s_login.add_argument(
        "--url", required=True, metavar="URL", help="로그인 페이지 주소"
    )
    s_login.add_argument(
        "--auth-dir", default=None, metavar="DIR", help="세션 저장 디렉터리"
    )
    s_login.add_argument(
        "--timeout",
        type=int,
        default=600,
        metavar="SEC",
        help="로그인 대기 상한 (기본 600초)",
    )

    s_list = session_sub.add_parser("list", help="저장된 세션 목록을 봅니다.")
    s_list.add_argument(
        "--auth-dir", default=None, metavar="DIR", help="세션 저장 디렉터리"
    )
    s_list.add_argument("--json", action="store_true", help="JSON으로 출력합니다.")

    s_check = session_sub.add_parser(
        "check", help="세션이 아직 유효한지 확인합니다."
    )
    s_check.add_argument("profile", help="프로파일 이름")
    s_check.add_argument(
        "--url",
        default=None,
        metavar="URL",
        help="유효성을 확인할 보호된 페이지 (미지정 시 파일 검사만)",
    )
    s_check.add_argument(
        "--auth-dir", default=None, metavar="DIR", help="세션 저장 디렉터리"
    )

    s_remove = session_sub.add_parser("remove", help="저장된 세션을 삭제합니다.")
    s_remove.add_argument("profile", help="프로파일 이름")
    s_remove.add_argument(
        "--auth-dir", default=None, metavar="DIR", help="세션 저장 디렉터리"
    )
    s_remove.add_argument(
        "-f", "--force", action="store_true", help="확인 없이 삭제합니다."
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

    # --- run ---
    from interface import run_cli

    run_cli.add_parser(sub)

    return parser


def _default_max_result_chars() -> int:
    from interface.mcp_server import DEFAULT_MAX_RESULT_CHARS

    return DEFAULT_MAX_RESULT_CHARS


def _max_result_chars(raw: str) -> int:
    """serve --max-result-chars 값(정수, 하한 MIN_MAX_RESULT_CHARS)."""
    from interface.mcp_server import MIN_MAX_RESULT_CHARS

    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"정수가 아닙니다: {raw}") from exc
    if value < MIN_MAX_RESULT_CHARS:
        raise argparse.ArgumentTypeError(f"{MIN_MAX_RESULT_CHARS} 이상이어야 합니다: {value}")
    return value


def _approval_ttl(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"정수가 아닙니다: {raw}") from exc
    if value < 10:
        raise argparse.ArgumentTypeError(f"10 이상이어야 합니다: {value}")
    return value


def _cmd_tools(as_json: bool) -> int:
    from interface.mcp_server import build_all_tools, build_server_tools

    specs = build_all_tools()
    extra = build_server_tools()
    if as_json:
        # tools/list 와 같은 목록(액션 툴 + 계약 밖 서버 도구).
        print(json.dumps(specs + extra, ensure_ascii=False, indent=2))
        return 0

    print(f"노출 툴 {len(specs)}종:")
    for spec in specs:
        required = spec["inputSchema"].get("required", [])
        hint = f" (필수: {', '.join(required)})" if required else ""
        print(f"  {spec['name']:28}{hint}")
    print(f"서버 도구(계약 밖, 사람 인계) {len(extra)}개:")
    for spec in extra:
        print(f"  {spec['name']}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from browser.serve_profile import ProfileInUseError
    from interface.mcp_server import run_stdio

    try:
        asyncio.run(
            run_stdio(
                mode=ExecutionMode(args.mode),
                allowed_domains=tuple(args.allow_domain),
                # WS-30: 파싱만 되고 전달되지 않던 결함 — 서버의 HITL 게이트까지 넘긴다.
                pre_approved_actions=tuple(args.pre_approve),
                secrets_path=args.secrets,
                som_enabled=args.som_vision,
                browser_mode=args.browser,
                chrome_profile=(
                    Path(args.chrome_profile).expanduser() if args.chrome_profile else None
                ),
                keep_open=bool(args.keep_open),
                nav_settle=args.nav_settle == "on",
                max_result_chars=(
                    args.max_result_chars
                    if args.max_result_chars is not None
                    else _default_max_result_chars()
                ),
                allow_private_network=bool(args.allow_private_network),
                block_loopback=bool(args.block_loopback),
                **({"approval_ttl_s": args.approval_ttl} if args.approval_ttl else {}),
                profile=args.profile,
            )
        )
    except KeyboardInterrupt:
        return 130
    except ProfileInUseError as exc:
        print(f"agent-browser serve: 오류: {exc}", file=sys.stderr)
        return 2
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
        if (args.chrome_profile or args.keep_open) and args.browser != "user-chrome":
            parser.error("--chrome-profile / --keep-open 은 --browser user-chrome 과 함께 씁니다")
        if args.browser == "user-chrome":
            # 평소 Chrome 프로필 가드 — 서버·브라우저를 만들기 전에, stdout 없이 stderr 한 줄.
            from browser.user_chrome import DEFAULT_PROFILE_DIR, _guard_profile

            try:
                _guard_profile(Path(args.chrome_profile).expanduser()
                               if args.chrome_profile else DEFAULT_PROFILE_DIR)
            except ValueError as exc:
                parser.exit(2, f"agent-browser serve: 오류: {exc}\n")
        if args.profile is not None:
            if args.browser == "user-chrome":
                parser.exit(2, "agent-browser serve: 오류: --profile 과 --browser user-chrome 은 "
                               "함께 쓸 수 없습니다 — user-chrome 은 이미 전용 영속 프로필"
                               "(--chrome-profile)을 씁니다. 둘 중 하나만 쓰세요.\n")
            from browser.serve_profile import ProfileError, profile_dir

            try:
                profile_dir(args.profile)  # 이름·위치 검사(경로 탈출·평소 Chrome 프로필 거부)
            except ProfileError as exc:
                parser.exit(2, f"agent-browser serve: 오류: {exc}\n")
        return _cmd_serve(args)
    if args.command == "tui":
        return _cmd_tui(args)
    if args.command == "profile":
        from interface import profile_cli

        return profile_cli.run(args)
    if args.command == "control":
        from interface import handoff

        return handoff.cli_control(args.op, args.server, as_json=args.json)
    if args.command == "approve":
        from interface import handoff

        if args.yes and args.deny:
            parser.error("--yes 와 --deny 는 함께 쓸 수 없습니다")
        if args.code is not None and args.deny:
            parser.error("--code 와 --deny 는 함께 쓸 수 없습니다")
        return handoff.cli_approve(args.approval_id, args.server, args.yes, deny=args.deny,
                                   code=args.code)
    if args.command == "llm-check":
        return _cmd_llm_check(args)
    if args.command == "session":
        from interface import session_cli

        return session_cli.run(args)
    if args.command == "run":
        if (args.chrome_profile or args.keep_open) and not args.user_chrome:
            parser.error("--chrome-profile / --keep-open 은 --user-chrome 과 함께 씁니다")
        if args.human and args.user_chrome:
            parser.error("--human 과 --user-chrome 은 함께 쓸 수 없습니다(둘 중 하나만)")
        if args.user_chrome:
            # 평소 Chrome 프로필 가드 — 브라우저·폴더를 만들기 전에, 트레이스백 없이 한 줄.
            from browser.user_chrome import DEFAULT_PROFILE_DIR, _guard_profile

            try:
                _guard_profile(Path(args.chrome_profile).expanduser()
                               if args.chrome_profile else DEFAULT_PROFILE_DIR)
            except ValueError as exc:
                parser.exit(2, f"agent-browser run: 오류: {exc}\n")
        from interface import run_cli

        return run_cli.run(args)

    parser.error(f"알 수 없는 명령: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
