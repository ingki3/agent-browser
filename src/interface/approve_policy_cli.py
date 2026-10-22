"""`agent-browser approve-policy add|list|remove` — 사이트별 상시 허용 정책 (WS-56, 사람 전용).

정책 = (origin, 액션, [대상 이름]). 맞으면 serve 가 그 고위험 액션을 승인 없이 실행한다(`data.approval.via=policy`).
최고위험 범주(돈 이동·계정 해지·권한 변경·판정 불가·upload_file)는 추가 단계에서 거부하고, 서버도 실행 때 다시
거부한다(security.approve_policy). MCP·서버 도구에는 이 기능이 없다.

`add` 는 터미널(TTY)에서 origin 을 한 번 더 손으로 입력해야 한다 — 에이전트가 셸 도구로 이 명령을 그냥 실행해
정책을 만드는 길을 좁힌다. **보안 경계는 아니다**(pty 로 흉내 낼 수 있다 — 같은 OS 사용자 한계, README).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def add_parser(sub: Any) -> None:
    pol = sub.add_parser("approve-policy",
                         help="사람: serve --profile 의 사이트별 고위험 동작 상시 허용 정책을 관리합니다.")
    psub = pol.add_subparsers(dest="policy_action", required=True)
    p_add = psub.add_parser("add", help="정책 추가(터미널에서 origin 재입력 확인).")
    p_list = psub.add_parser("list", help="정책 목록.")
    p_rm = psub.add_parser("remove", help="정책 삭제(origin·액션·대상 이름이 모두 같은 것).")
    for p in (p_add, p_list, p_rm):
        p.add_argument("--profile", required=True, metavar="NAME", help="serve --profile 이름")
    for p in (p_add, p_rm):
        p.add_argument("--origin", required=True, help="https://호스트[:포트] (경로 없이)")
        p.add_argument("--action", required=True, help="액션 이름(click·press_key·type_text …)")
        p.add_argument("--target-name", default=None,
                       help="대상 요소 이름(생략하면 그 사이트의 그 액션 전부 — 최고위험 범주는 늘 제외)")
    p_list.add_argument("--json", action="store_true", help="JSON 으로 출력")


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _err(msg: str) -> int:
    from interface.handoff import display_safe

    print(f"agent-browser approve-policy: 오류: {display_safe(msg, 400)}", file=sys.stderr)
    return 2


def _policy_path(name: str, create: bool) -> Path:
    from browser import serve_profile as sp
    from security.approve_policy import POLICY_FILE

    folder = sp.profile_dir(name)
    sp._refuse_symlink(folder)
    if create and not folder.exists():
        folder.mkdir(mode=0o700, parents=True)
        os.chmod(folder, 0o700)
    return folder / POLICY_FILE


def _row_text(row: dict) -> str:
    from interface.handoff import display_safe

    target = row.get("target_name")
    return (f"{display_safe(row['origin'])}  {row['action']}  "
            + (f"대상={display_safe(target)}" if target is not None else "대상=(이름 무관, 최고위험 제외)"))


def run(args: argparse.Namespace) -> int:
    from browser.serve_profile import ProfileError
    from security import approve_policy as ap

    try:
        if args.policy_action == "list":
            rows = ap.read_policies(_policy_path(args.profile, create=False))
            if args.json:
                print(json.dumps({"profile": args.profile, "policies": rows}, ensure_ascii=False, indent=2))
            elif not rows:
                print(f"프로필 {args.profile!r} 에 상시 허용 정책이 없습니다.")
            else:
                for i, row in enumerate(rows, 1):
                    print(f"{i}. {_row_text(row)}")
            return 0
        if args.policy_action == "remove":
            if ap.remove_policy(_policy_path(args.profile, create=False), args.origin, args.action,
                                args.target_name):
                print("정책을 지웠습니다. 실행 중인 serve 도 다음 승인 판정부터 따릅니다.")
                return 0
            print("같은 정책이 없습니다(`agent-browser approve-policy list --profile …`).")
            return 1
        return _add(args, ap)
    except ap.PolicyError as exc:
        if args.policy_action == "list":
            return _err(f"{exc} — 이 파일의 정책은 적용되지 않습니다(fail-closed). 파일을 지우고 다시 추가하세요.")
        return _err(str(exc))
    except ProfileError as exc:
        return _err(str(exc))


def _add(args: argparse.Namespace, ap: Any) -> int:
    from security.hitl import normalize_gate_text
    from contracts import ActionType

    # 값 검사를 먼저(최고위험·형식) — 확인을 묻기 전에 거부 사유를 알린다.
    origin = ap.normalize_origin(args.origin)
    try:
        action = ActionType(str(args.action).strip())
    except ValueError:
        raise ap.PolicyError(f"알 수 없는 액션: {args.action!r}") from None
    if action in ap.FORBIDDEN_ACTIONS:
        raise ap.PolicyError(f"이 액션은 정책으로 열 수 없습니다(범주 {ap.FORBIDDEN_ACTIONS[action]}) — 매번 승인하세요")
    if args.target_name is not None:
        from security import ActionContext

        cat = ap.forbidden_category(ActionContext(action=action, element_name=normalize_gate_text(args.target_name)))
        if cat is not None:
            raise ap.PolicyError(f"이 대상은 정책으로 열 수 없습니다(범주 {cat}) — 매번 승인하세요")
    path = _policy_path(args.profile, create=False)
    if not _stdin_is_tty():
        print("agent-browser approve-policy: 확인이 필요합니다 — 사람이 터미널에서 직접 실행하세요"
              "(에이전트·스크립트로는 추가하지 않습니다).", file=sys.stderr)
        return 2
    draft = {"origin": origin, "action": action.value,
             "target_name": normalize_gate_text(args.target_name) if args.target_name is not None else None}
    sys.stdout.write(
        f"프로필 {args.profile!r} 에서 다음 고위험 동작을 앞으로 묻지 않고 실행합니다:\n  {_row_text(draft)}\n"
        "확인하려면 origin 을 그대로 다시 입력하세요: ")
    sys.stdout.flush()
    typed = (sys.stdin.readline() or "").strip()
    try:
        same = ap.normalize_origin(typed) == origin
    except ap.PolicyError:
        same = False
    if not same:
        print("입력한 origin 이 달라 추가하지 않았습니다.")
        return 1
    path = _policy_path(args.profile, create=True)
    entry = ap.add_policy(path, origin, action.value, args.target_name)
    print(f"정책을 추가했습니다: {_row_text(entry)}\n실행 중인 serve 도 다음 승인 판정부터 따릅니다.")
    return 0
