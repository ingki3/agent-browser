"""`agent-browser manual-sites add|remove|list` — 직접 조작할 사이트 목록(WS-64, 사람 전용).

목록 사이트에서는 serve 가 에이전트의 누르기·입력·고르기·키·대화상자·파일 동작을 실행하지 않고, 대상을 창에 표시해
사람이 직접 하게 한다(매크로 방지를 우회하지 않음). MCP 도구로는 목록을 바꿀 수 없다.

* add 는 비대화형도 받는다(막는 방향이라 안전). remove 는 대화형 터미널(TTY)에서 사이트를 한 번 더 입력해야 한다 —
  셸을 가진 에이전트가 목록을 지워 우회하는 길을 좁힌다(보안 경계는 아님 — 같은 OS 사용자 한계, README).
* 실행 중인 serve 는 다음 판정부터 파일을 다시 읽는다(재시작 불필요).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def add_parser(sub: Any) -> None:
    p = sub.add_parser("manual-sites", help="사람: 에이전트가 직접 조작하지 않을 사이트(사람이 직접) 목록을 관리합니다.")
    psub = p.add_subparsers(dest="manual_action", required=True)
    p_add = psub.add_parser("add", help="사이트 추가(예: srail.kr · https://etk.srail.kr · *.srail.kr).")
    p_add.add_argument("site")
    p_rm = psub.add_parser("remove", help="사이트 삭제(터미널에서 사이트 재입력 확인).")
    p_rm.add_argument("site")
    p_list = psub.add_parser("list", help="목록.")
    p_list.add_argument("--json", action="store_true", help="JSON 으로 출력")
    hp = sub.add_parser("handoff", help="사람: 직접 조작할 사이트에서 표시된 단계를 마쳤다고 에이전트에게 알립니다.")
    hp.add_argument("op", choices=["done"])
    hp.add_argument("--server", default=None, metavar="ID", help="서버 id (하나만 떠 있으면 생략 가능)")


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _err(msg: str) -> int:
    from interface.handoff import display_safe

    print(f"agent-browser manual-sites: 오류: {display_safe(msg, 400)}", file=sys.stderr)
    return 2


def run(args: argparse.Namespace) -> int:
    from security import manual_sites as ms

    path = ms.manual_sites_file()
    try:
        if args.manual_action == "list":
            rows = ms.read_entries(path)
            if args.json:
                print(json.dumps({"sites": rows}, ensure_ascii=False, indent=2))
            elif not rows:
                print("직접 조작할 사이트가 없습니다.")
            else:
                for i, row in enumerate(rows, 1):
                    print(f"{i}. {row['site']}")
            return 0
        if args.manual_action == "add":
            text, added = ms.add_site(args.site, path)
            if added:
                print(f"추가했습니다: {text}\n실행 중인 serve 도 다음 동작부터 따릅니다(에이전트는 이 사이트를 누르거나 "
                      "입력하지 않고 대상을 창에 표시합니다).")
            else:
                print(f"이미 있습니다: {text}")
            return 0
        entry = ms.parse_entry(args.site)
        if not _stdin_is_tty():
            print("agent-browser manual-sites: 삭제는 사람이 터미널에서 직접 실행하세요(확인 입력 필요 — 에이전트·"
                  "스크립트로는 지우지 않습니다).", file=sys.stderr)
            return 2
        sys.stdout.write(f"직접 조작할 사이트에서 뺍니다: {entry.text}\n확인하려면 사이트를 그대로 다시 입력하세요: ")
        sys.stdout.flush()
        typed = (sys.stdin.readline() or "").strip()
        try:
            same = ms.parse_entry(typed).text == entry.text
        except ms.ManualSiteError:
            same = False
        if not same:
            print("입력한 사이트가 달라 지우지 않았습니다.")
            return 1
        if ms.remove_site(entry.text, path):
            print(f"지웠습니다: {entry.text}")
            return 0
        print("목록에 없는 사이트입니다(`agent-browser manual-sites list`).")
        return 1
    except ms.ManualSiteError as exc:
        if args.manual_action == "list":
            # WS-64 R1 B3: 목록을 읽지 못하면 오류 종료 + 고치는 방법(serve 는 그동안 모든 부작용 액션을 막는다).
            return _err(f"{exc} — {ms.FIX_HINT}")
        return _err(str(exc))


def run_handoff(args: argparse.Namespace) -> int:
    """`agent-browser handoff done` — 조작권·창·비밀 입력 보호는 바꾸지 않고 '다시 관찰' 신호만 보낸다."""
    from interface import handoff

    return handoff.cli_control(args.op, args.server)
