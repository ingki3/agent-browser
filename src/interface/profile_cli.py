"""`agent-browser profile list|remove` — serve --profile 영속 프로필 관리 (WS-32).

출력은 이름·크기·마지막 사용·사용 중 여부(쓰는 서버 id)만. 폴더 경로·쿠키 내용은 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def add_parser(sub: Any) -> None:
    prof = sub.add_parser("profile", help="serve --profile 영속 프로필(로그인 유지)을 보거나 지웁니다.")
    psub = prof.add_subparsers(dest="profile_action", required=True)
    p_list = psub.add_parser("list", help="영속 프로필 목록(이름·크기·마지막 사용·사용 중).")
    p_list.add_argument("--json", action="store_true", help="JSON 으로 출력")
    p_sites = psub.add_parser("sites", help="쿠키가 있는 사이트만 표시(로그인 여부를 보증하지 않음).")
    p_sites.add_argument("name", help="프로필 이름")
    p_sites.add_argument("--json", action="store_true", help="JSON 으로 출력")
    p_rm = psub.add_parser("remove", help="영속 프로필을 지웁니다(로그인이 사라집니다).")
    p_rm.add_argument("name", help="프로필 이름")
    p_rm.add_argument("--yes", "-y", action="store_true", help="확인 없이 지웁니다")


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def _cmd_list(as_json: bool) -> int:
    from browser import serve_profile as sp

    try:
        rows = sp.list_profiles()
    except sp.ProfileError as exc:
        print(f"agent-browser profile: 오류: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("영속 프로필이 없습니다. `agent-browser serve --profile NAME` 으로 만듭니다.")
        return 0
    print(f"{'이름':<20} {'크기':>8}  {'마지막 사용':<26} 사용 중")
    for r in rows:
        if r["in_use"]:
            who = f"예 (서버 {r['server_id']})" if r.get("server_id") else (
                f"예 (Chromium pid {r['chromium_pid']})" if r.get("chromium_pid") else "예")
        else:
            who = "아니오"
        print(f"{r['name']:<20} {_human_size(r['size_bytes']):>8}  {r['last_used']:<26} {who}")
    return 0


def _cmd_remove(name: str, yes: bool) -> int:
    from browser import serve_profile as sp

    try:
        sp.profile_dir(name)
        who = sp.holder(name)
    except sp.ProfileError as exc:
        print(f"agent-browser profile: 오류: {exc}", file=sys.stderr)
        return 2
    if who is not None:
        print(f"agent-browser profile: 오류: {sp.ProfileInUseError(name, who)}", file=sys.stderr)
        return 2
    if not yes:
        if not _stdin_is_tty():
            print("agent-browser profile: 확인이 필요합니다 — 터미널에서 실행하거나 --yes 를 붙이세요.",
                  file=sys.stderr)
            return 2
        sys.stdout.write(f"프로필 {name!r} 을 지웁니다(이 프로필의 로그인이 사라집니다). 계속할까요? [y/N] ")
        sys.stdout.flush()
        answer = (sys.stdin.readline() or "").strip().lower()
        if answer not in ("y", "yes"):
            print("취소했습니다.")
            return 1
    try:
        sp.remove(name)
    except sp.ProfileInUseError as exc:
        print(f"agent-browser profile: 오류: {exc}", file=sys.stderr)
        return 2
    except sp.ProfileError as exc:
        print(f"agent-browser profile: 오류: {exc}", file=sys.stderr)
        return 2
    print(f"프로필 {name!r} 을 지웠습니다.")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.profile_action == "sites":
        from browser.serve_profile import ProfileError, cookie_sites
        from interface.handoff import display_safe
        from interface import handoff

        try:
            truncated = False
            from browser.serve_profile import validate_name

            validate_name(args.name)
            live = [s for s in handoff.list_servers() if s.get("profile") == args.name]
            if live:
                sid = live[0]["server_id"]
                nonce = handoff.write_command(None, sid, "cookie_sites")
                ack = handoff.wait_ack(None, sid, nonce)
                if not ack or not ack.get("ok"):
                    raise ProfileError("서버의 쿠키 메타데이터 응답이 없습니다; 잠시 뒤 다시 실행하세요.")
                rows = ack["sites"]
                truncated = bool(ack.get("truncated"))
            else:
                rows = cookie_sites(args.name)
        except ProfileError as exc:
            print(f"agent-browser profile sites: {display_safe(str(exc))}", file=sys.stderr)
            return 2
        notice = "쿠키 있음 표시이며 로그인 상태를 보증하지 않습니다."
        if args.json:
            print(json.dumps({"sites": rows, "notice": notice, "truncated": truncated}, ensure_ascii=True))
        else:
            print(notice)
            if truncated:
                print("사이트 목록이 응답 크기 상한으로 잘렸습니다.")
            for row in rows:
                print(f"{display_safe(row['domain'])}: 만료={row['expires_at'] or '없음'} "
                      f"세션 쿠키만={'예' if row['session_only'] else '아니오'}")
        return 0
    if args.profile_action == "list":
        return _cmd_list(bool(args.json))
    return _cmd_remove(args.name, bool(args.yes))
