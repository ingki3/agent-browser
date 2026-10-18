"""`agent-browser profile list|remove` — serve --profile 영속 프로필 관리 (WS-32).

출력은 이름·크기·마지막 사용·사용 중 여부(쓰는 서버 id)만. 폴더 경로·쿠키 내용은 출력하지 않는다.

`profile import-cookies` (WS-59, 사람 전용 — MCP·서버 도구에 없음): 일상 Chrome 에서 고른 사이트의 쿠키만
봇 프로필로 가져온다(browser.chrome_cookies). 출력은 사이트별 쿠키 개수·이름·건너뜀 이유 개수만.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def add_parser(sub: Any) -> None:
    prof = sub.add_parser("profile", help="serve --profile 영속 프로필(로그인 유지)을 보거나 지웁니다.")
    psub = prof.add_subparsers(dest="profile_action", required=True)
    p_list = psub.add_parser("list", help="영속 프로필 목록(이름·크기·마지막 사용·사용 중).")
    p_list.add_argument("--json", action="store_true", help="JSON 으로 출력")
    p_sites = psub.add_parser("sites", help="쿠키가 있는 사이트만 표시(로그인 여부를 보증하지 않음).")
    p_sites.add_argument("name", help="프로필 이름")
    p_sites.add_argument("--json", action="store_true", help="JSON 으로 출력")
    p_imp = psub.add_parser(
        "import-cookies",
        help="사람: 평소 Chrome 에서 고른 사이트의 로그인 쿠키만 봇 프로필로 가져옵니다(macOS 키체인 허용 창이 뜹니다).")
    p_imp.add_argument("--profile", required=True, metavar="NAME", help="쿠키를 넣을 봇 프로필 이름(serve --profile)")
    p_imp.add_argument("--site", required=True, action="append", metavar="DOMAIN",
                       help="가져올 사이트의 등록 도메인(예: example.com — 하위 도메인 쿠키 포함). 여러 번 줄 수 있음")
    p_imp.add_argument("--chrome-profile", default="Default", metavar="DIR",
                       help="평소 Chrome 프로필 폴더 이름(기본 Default, 예: 'Profile 1')")
    p_imp.add_argument("--chrome-dir", metavar="PATH",
                       help="평소 Chrome 데이터 폴더(기본 ~/Library/Application Support/Google/Chrome)")
    p_imp.add_argument("--dry-run", action="store_true", help="쓰지 않고 가져올 쿠키 이름·개수만 봅니다")
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


_NAMES_SHOWN = 50
#: 이번 import 가 읽은 모든 쿠키 값의 로컬 집합(chrome_cookies.ValueGuard) — 출력 직전 최종 검사(R1).
_GUARD: Any = None


def _import_say(line: str, *, err: bool = False) -> None:
    """출력 직전 살균(제어문자) + 이번 import 가 읽은 값 전부(로컬 집합) + 이 프로세스의 등록 값(WS-51 레지스트리) 가림."""
    from interface.handoff import display_safe
    from security.secret_registry import mask_registered

    text = display_safe(line, 4000)
    if _GUARD is not None:
        text = _GUARD.mask(text)
    print(mask_registered(text), file=sys.stderr if err else sys.stdout)


def _skip_text(skipped: "dict[str, int]") -> str:
    from browser.chrome_cookies import SKIP_LABELS

    return ", ".join(f"{SKIP_LABELS.get(k, k)} {n}개" for k, n in sorted(skipped.items()) if n)


def _shown_name(name: str) -> str:
    if not name:
        return "(이름 없음)"
    return _GUARD.name(name) if _GUARD is not None else name


def _names_text(names: "list[str]") -> str:
    shown = ", ".join(_shown_name(n) for n in names[:_NAMES_SHOWN])
    more = len(names) - _NAMES_SHOWN
    return shown + (f" 외 {more}개" if more > 0 else "")


def _cmd_import(args: argparse.Namespace) -> int:
    import asyncio
    import secrets

    from browser import chrome_cookies as cc
    from browser import login_ledger
    from browser import serve_profile as sp

    global _GUARD
    _GUARD = None
    name = args.profile
    try:
        sp.profile_dir(name)
        sites = list(dict.fromkeys(cc.validate_site(s) for s in args.site))
        if not args.dry_run:
            who = sp.holder(name)
            if who is not None:
                raise sp.ProfileInUseError(name, who)
        chrome_dir = Path(args.chrome_dir).expanduser() if args.chrome_dir else cc.DEFAULT_CHROME_DIR
        db = cc.find_cookie_db(chrome_dir, args.chrome_profile)
        _import_say("평소 Chrome 쿠키를 읽습니다 — macOS 키체인 'Chrome Safe Storage' 허용 창이 뜨면 "
                    "'허용'을 누르세요(값을 풀기 위해 이 명령이 묻습니다).")
        plan = cc.read_cookies(db, sites, password_provider=lambda: cc.keychain_password(), chrome_dir=chrome_dir)
        _GUARD = plan.guard()
    except (cc.CookieImportError, sp.ProfileError, sp.ProfileInUseError) as exc:
        _import_say(f"agent-browser profile import-cookies: 오류: {exc}", err=True)
        return 2
    except Exception as exc:  # noqa: BLE001 - 원 문구에 경로·값이 있을 수 있다
        _import_say(f"agent-browser profile import-cookies: 오류: 쿠키를 읽지 못했습니다({type(exc).__name__}).",
                    err=True)
        return 2

    if args.dry_run:
        _import_say("[dry-run] 봇 프로필에 쓰지 않았습니다 — 가져올 쿠키(이름만):")
        for site, sp_plan in plan.sites.items():
            names = sp_plan.names()
            _import_say(f"{site}: 가져올 쿠키 {len(sp_plan.cookies)}개"
                        + (f" — {_names_text(names)}" if names else " (평소 Chrome 에 이 사이트 쿠키가 없습니다)"))
            if sp_plan.skipped:
                _import_say(f"  건너뜀: {_skip_text(dict(sp_plan.skipped))}")
        return 0

    try:
        lock = sp.acquire(name, server_id="import-cookies-" + secrets.token_hex(4))
    except (sp.ProfileError, sp.ProfileInUseError) as exc:
        _import_say(f"agent-browser profile import-cookies: 오류: {exc}", err=True)
        return 2
    ledger_failed = False
    try:
        try:
            results = asyncio.run(cc.write_cookies(lock.path, plan))
        except cc.CookieImportError as exc:
            _import_say(f"agent-browser profile import-cookies: 오류: {exc}", err=True)
            return 2
        except Exception as exc:  # noqa: BLE001
            _import_say(f"agent-browser profile import-cookies: 오류: 쿠키를 넣지 못했습니다({type(exc).__name__}).",
                        err=True)
            return 2
        done = [s for s, r in results.items() if r.written]  # 저장소에서 값·속성까지 확인된 사이트만
        if done:
            try:
                login_ledger.record(lock.path, [f"https://{s}" for s in done],
                                    source=login_ledger.SOURCE_CHROME_IMPORT)
            except login_ledger.LedgerError as exc:
                ledger_failed = True
                _import_say(f"agent-browser profile import-cookies: 경고: 로그인 기록을 남기지 못했습니다: {exc}",
                            err=True)
    finally:
        lock.release()
    _import_say(f"봇 프로필 {name!r} 에 가져왔습니다(쿠키 이름만 표시):")
    for site, res in results.items():
        skipped = dict(plan.sites[site].skipped)
        if res.rejected - res.kept_existing:
            skipped[cc.SKIP_REJECTED] = res.rejected - res.kept_existing
        if res.kept_existing:
            skipped[cc.SKIP_KEPT_EXISTING] = res.kept_existing
        _import_say(f"{site}: 가져온 쿠키 {len(res.written)}개" + (f" — {_names_text(res.written)}" if res.written else ""))
        if skipped:
            _import_say(f"  건너뜀: {_skip_text(skipped)}")
    # 가져올 쿠키가 있었는데 하나도 들어가지 않은 사이트가 있으면 실패로 알린다(전부 브라우저가 거부).
    nothing = [s for s, r in results.items() if plan.sites[s].cookies and not r.written]
    if nothing:
        _import_say("agent-browser profile import-cookies: 오류: 브라우저가 이 사이트 쿠키를 하나도 받지 않았습니다: "
                    + ", ".join(nothing), err=True)
    return 1 if (ledger_failed or nothing) else 0


def run(args: argparse.Namespace) -> int:
    if args.profile_action == "import-cookies":
        return _cmd_import(args)
    if args.profile_action == "sites":
        from browser.serve_profile import ProfileError, bound_cookie_sites, cookie_sites
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
                rows, truncated = bound_cookie_sites(cookie_sites(args.name))
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
