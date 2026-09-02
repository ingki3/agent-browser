"""세션 관리 CLI (PRD 2절 Persona A, 5.1).

**비밀번호를 저장하지 않는다.** 사람이 실제 브라우저에서 직접 로그인하고,
그 결과물인 쿠키/localStorage만 암호화해 보관한다.

    agent-browser session login <프로파일> --url <로그인 페이지>
    agent-browser session list
    agent-browser session check <프로파일> --url <보호된 페이지>
    agent-browser session remove <프로파일>

이 방식이 근본적인 이유:

- 비밀번호가 에이전트 경로를 지나가지 않는다. LLM 프롬프트에도,
  트레이스에도, 디스크에도 남지 않는다.
- 소셜 로그인(구글 등)과 2FA를 지원하는 **유일한** 방법이다. 제공자들이
  헤드리스 자동화를 능동 탐지해 차단하기 때문에 사람이 직접 하는 수밖에
  없다. Playwright `storageState`, Browserbase Contexts, Steel 세션
  영속화가 모두 같은 접근이다.

저장물 보호 (PRD 5.1-1):
    AES-256-GCM + Argon2id(salt 16B, iter 3, mem 64MiB, lanes 4), 0600.
    패스프레이즈는 keyring -> 프롬프트 -> CI 환경변수 순으로 해석한다.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from browser import (
    CI_ENV_VAR,
    KeyUnavailableError,
    SessionStore,
    SessionStoreError,
    resolve_passphrase,
)
from browser.session_probe import PageSignals, ProfileProbeConfig, detect_expiry
from browser.session_store import KEYRING_SERVICE

logger = logging.getLogger(__name__)

#: 로그인 완료를 기다리는 최대 시간. 2FA나 CAPTCHA를 사람이 처리할
#: 여유를 준다. 무인 실행이 아니므로 넉넉해도 된다.
LOGIN_TIMEOUT_S = 600

#: 저장된 세션에 함께 기록하는 메타데이터 키. storage_state 자체는
#: Playwright 스키마라 건드리지 않고, 별도 키로 감싼다.
META_KEY = "_agent_browser_meta"


class SessionCLIError(Exception):
    """세션 CLI 실패."""


# ---------------------------------------------------------------------------
# 패스프레이즈
# ---------------------------------------------------------------------------


def _prompt_passphrase(label: str) -> str:
    """에코 없이 패스프레이즈를 입력받는다."""
    return getpass.getpass(label)


def _resolve_for_read(profile: str) -> str:
    """기존 프로파일을 열기 위한 패스프레이즈를 얻는다."""
    try:
        resolution = resolve_passphrase(profile, prompt_fn=_prompt_passphrase)
    except KeyUnavailableError as exc:
        raise SessionCLIError(str(exc)) from None
    if not resolution.passphrase:
        raise SessionCLIError("패스프레이즈를 얻지 못했습니다.")
    return resolution.passphrase


def _resolve_for_create(profile: str) -> Tuple[str, bool]:
    """새 프로파일을 만들기 위한 패스프레이즈를 얻는다.

    키체인에 이미 있으면 그대로 쓰고, 없으면 두 번 입력받아 오타를
    막는다. 오타가 나면 다음 실행에서 세션을 못 열고 원인도 알기 어렵다.

    반환: (패스프레이즈, 새로 입력받았는가)
    """
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, profile)
        if stored:
            print(f"  키체인에 등록된 패스프레이즈를 사용합니다 (service={KEYRING_SERVICE}).")
            return stored, False
    except Exception as exc:  # noqa: BLE001 - keyring 백엔드 부재 등
        logger.debug("Keyring 조회 실패: %s", exc)

    ci_value = os.environ.get(CI_ENV_VAR)
    if ci_value:
        print(f"  {CI_ENV_VAR} 환경변수를 사용합니다 (보안 등급이 낮습니다).")
        return ci_value, False

    first = _prompt_passphrase(f"[{profile}] 새 마스터 패스프레이즈: ")
    if not first:
        raise SessionCLIError("패스프레이즈가 비어 있습니다.")
    second = _prompt_passphrase(f"[{profile}] 한 번 더 입력: ")
    if first != second:
        raise SessionCLIError("두 입력이 일치하지 않습니다.")
    return first, True


def _offer_keyring_save(profile: str, passphrase: str) -> None:
    """무인 실행을 위해 패스프레이즈를 키체인에 저장할지 묻는다."""
    try:
        import keyring
    except ImportError:
        print("  keyring 미설치 — 다음 실행 때 패스프레이즈를 다시 입력해야 합니다.")
        return

    answer = input("  이 패스프레이즈를 OS 키체인에 저장할까요? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        print("  저장하지 않았습니다. 무인 실행 시 매번 입력이 필요합니다.")
        return
    try:
        keyring.set_password(KEYRING_SERVICE, profile, passphrase)
        print(f"  키체인에 저장했습니다 (service={KEYRING_SERVICE}, account={profile}).")
    except Exception as exc:  # noqa: BLE001
        print(f"  키체인 저장 실패: {exc}")


# ---------------------------------------------------------------------------
# 페이지 신호 수집
# ---------------------------------------------------------------------------


#: 로그인 페이지로 리다이렉트됐는지 판정할 경로 조각 (PRD 5.1 3순위).
LOGIN_PATH_HINTS = ("/login", "/signin", "/sign-in", "/auth", "/nidlogin")


async def _collect_signals(page, http_status: Optional[int]) -> PageSignals:
    """세션 프로브(PRD 5.1)에 넣을 신호를 실제 페이지에서 모은다."""
    visible_password_inputs = await page.evaluate(
        """() => {
            const inputs = Array.from(
                document.querySelectorAll('input[type=password]')
            );
            return inputs.filter(el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0
                    && s.visibility !== 'hidden' && s.display !== 'none';
            }).length;
        }"""
    )
    current = (page.url or "").lower()
    return PageSignals(
        url=page.url,
        http_status=http_status,
        visible_password_inputs=int(visible_password_inputs),
        has_authenticated_markers=False,
        custom_probe_status=None,
        redirected_to_login=any(hint in current for hint in LOGIN_PATH_HINTS),
    )


def _origins_of(storage_state: Dict[str, Any]) -> List[str]:
    """저장된 세션이 어떤 도메인을 담고 있는지 요약한다.

    **쿠키 값은 절대 출력하지 않는다.** 도메인 이름만 센다.
    """
    domains = set()
    for cookie in storage_state.get("cookies", []) or []:
        domain = cookie.get("domain")
        if domain:
            domains.add(str(domain).lstrip("."))
    for origin in storage_state.get("origins", []) or []:
        url = origin.get("origin")
        if url:
            domains.add(str(url))
    return sorted(domains)


# ---------------------------------------------------------------------------
# 명령: login
# ---------------------------------------------------------------------------


async def _run_login(
    profile: str,
    url: str,
    *,
    auth_dir: Optional[str],
    timeout_s: int,
) -> int:
    from playwright.async_api import async_playwright

    store = SessionStore(auth_dir=_as_path(auth_dir))
    passphrase, newly_entered = _resolve_for_create(profile)

    overwriting = store.exists(profile)
    if overwriting:
        print(f"  기존 프로파일 '{profile}'을 덮어씁니다.")

    print()
    print("  브라우저 창이 열립니다. 직접 로그인하십시오.")
    print("  2단계 인증이나 CAPTCHA가 있으면 그것도 창에서 처리하면 됩니다.")
    print("  로그인이 끝나면 이 터미널로 돌아와 Enter를 누르십시오.")
    print()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        response = None
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            await browser.close()
            raise SessionCLIError(f"페이지를 열지 못했습니다: {exc}") from None

        try:
            input("  로그인을 마쳤으면 Enter: ")
        except (EOFError, KeyboardInterrupt):
            await browser.close()
            print()
            print("  취소했습니다. 저장하지 않았습니다.")
            return 130

        # 로그인이 실제로 됐는지 확인한다. 확인 없이 저장하면 빈 세션이
        # 저장되고, 그 사실을 무인 실행 중에야 알게 된다.
        status = response.status if response is not None else None
        signals = await _collect_signals(page, status)
        probe = detect_expiry(signals)
        # Playwright는 TypedDict를 반환한다. 메타데이터를 얹기 위해
        # 평범한 dict로 바꾼다(SessionStore는 JSON으로 직렬화한다).
        storage_state: Dict[str, Any] = dict(await context.storage_state())
        await browser.close()

    cookie_count = len(storage_state.get("cookies", []) or [])
    origin_count = len(storage_state.get("origins", []) or [])

    if probe.expired:
        print()
        print(f"  [경고] 아직 로그인되지 않은 것으로 보입니다 ({probe.reason}).")
        answer = input("  그래도 저장할까요? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("  저장하지 않았습니다.")
            return 1

    if cookie_count == 0 and origin_count == 0:
        print()
        print("  [경고] 쿠키도 localStorage도 없습니다. 저장할 의미가 없습니다.")
        return 1

    storage_state[META_KEY] = {
        "profile": profile,
        "login_url": url,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    path = store.save(profile, storage_state, passphrase)

    print()
    print(f"  저장 완료: {path}")
    print(f"  쿠키 {cookie_count}개 / origin {origin_count}개")
    print(f"  도메인: {', '.join(_origins_of(storage_state)[:6]) or '(없음)'}")
    print(f"  암호화: AES-256-GCM + Argon2id, 권한 {oct(path.stat().st_mode & 0o777)}")

    if newly_entered:
        print()
        _offer_keyring_save(profile, passphrase)

    return 0


# ---------------------------------------------------------------------------
# 명령: list
# ---------------------------------------------------------------------------


def _run_list(auth_dir: Optional[str], as_json: bool) -> int:
    store = SessionStore(auth_dir=_as_path(auth_dir))
    directory = store.auth_dir

    if not directory.exists():
        if as_json:
            print(json.dumps({"auth_dir": str(directory), "profiles": []}))
        else:
            print(f"저장된 세션이 없습니다 ({directory}).")
        return 0

    entries = []
    for path in sorted(directory.glob("*.enc")):
        stat_result = path.stat()
        entries.append(
            {
                "profile": path.stem,
                "path": str(path),
                "bytes": stat_result.st_size,
                "mode": oct(stat_result.st_mode & 0o777),
                "modified": datetime.fromtimestamp(
                    stat_result.st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds"),
                "permissions_ok": (stat_result.st_mode & 0o077) == 0,
            }
        )

    if as_json:
        print(
            json.dumps(
                {"auth_dir": str(directory), "profiles": entries},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not entries:
        print(f"저장된 세션이 없습니다 ({directory}).")
        return 0

    print(f"저장 위치: {directory}")
    print()
    print(f"  {'프로파일':<20} {'크기':>8}  {'권한':<6} {'최종 수정':<22}")
    for entry in entries:
        warn = "" if entry["permissions_ok"] else "  [권한 확인 필요]"
        print(
            f"  {entry['profile']:<20} {entry['bytes']:>7}B  "
            f"{entry['mode']:<6} {entry['modified']:<22}{warn}"
        )
    print()
    print("  내용을 보려면 패스프레이즈가 필요합니다 (session check).")
    return 0


# ---------------------------------------------------------------------------
# 명령: check
# ---------------------------------------------------------------------------


async def _run_check(
    profile: str, url: Optional[str], *, auth_dir: Optional[str]
) -> int:
    store = SessionStore(auth_dir=_as_path(auth_dir))
    if not store.exists(profile):
        raise SessionCLIError(f"프로파일이 없습니다: {profile}")

    perm_ok = store.verify_permissions(profile)
    passphrase = _resolve_for_read(profile)

    try:
        storage_state = store.load(profile, passphrase)
    except SessionStoreError as exc:
        raise SessionCLIError(f"복호화 실패: {exc}") from None

    meta = storage_state.get(META_KEY, {}) or {}
    cookie_count = len(storage_state.get("cookies", []) or [])
    origin_count = len(storage_state.get("origins", []) or [])

    print(f"프로파일: {profile}")
    print(f"  파일 권한 0600: {'예' if perm_ok else '아니오 (확인 필요)'}")
    print(f"  복호화: 성공")
    print(f"  쿠키 {cookie_count}개 / origin {origin_count}개")
    if meta.get("saved_at"):
        print(f"  저장 시각: {meta['saved_at']}")
    domains = _origins_of(storage_state)
    if domains:
        print(f"  도메인: {', '.join(domains[:6])}")

    expiring = _expiry_summary(storage_state)
    if expiring:
        print(f"  쿠키 만료 임박: {expiring}")

    if not url:
        print()
        print("  실제 유효성을 보려면 --url로 보호된 페이지를 지정하십시오.")
        return 0

    from playwright.async_api import async_playwright

    print()
    print(f"  {url} 접속해 세션 유효성을 확인합니다...")

    # 메타데이터는 Playwright 스키마가 아니므로 주입 전에 제거한다.
    state_for_browser: Any = {
        k: v for k, v in storage_state.items() if k != META_KEY
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=state_for_browser)
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            await browser.close()
            raise SessionCLIError(f"페이지를 열지 못했습니다: {exc}") from None

        status = response.status if response is not None else None
        signals = await _collect_signals(page, status)
        final_url = page.url
        await browser.close()

    probe = detect_expiry(signals)
    print(f"  HTTP {status} -> {final_url}")
    print(f"  판정 계층: {probe.tier.name}")

    if probe.expired:
        print(f"  [만료] {probe.reason}")
        print("  다시 로그인하십시오: agent-browser session login " + profile)
        return 1

    print("  [유효] 세션이 살아 있습니다.")
    return 0


def _expiry_summary(storage_state: Dict[str, Any]) -> str:
    """가장 이른 쿠키 만료 시각을 요약한다. 값은 출력하지 않는다."""
    now = datetime.now(timezone.utc).timestamp()
    soonest = None
    for cookie in storage_state.get("cookies", []) or []:
        expires = cookie.get("expires")
        if not isinstance(expires, (int, float)) or expires <= 0:
            continue  # 세션 쿠키
        if expires < now:
            return "이미 만료된 쿠키가 있습니다"
        if soonest is None or expires < soonest:
            soonest = expires
    if soonest is None:
        return ""
    remaining_days = (soonest - now) / 86400
    if remaining_days > 14:
        return ""
    return f"{remaining_days:.1f}일 후"


# ---------------------------------------------------------------------------
# 명령: remove
# ---------------------------------------------------------------------------


def _run_remove(profile: str, *, auth_dir: Optional[str], force: bool) -> int:
    store = SessionStore(auth_dir=_as_path(auth_dir))
    if not store.exists(profile):
        raise SessionCLIError(f"프로파일이 없습니다: {profile}")

    if not force:
        answer = input(f"  '{profile}' 세션을 삭제할까요? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("  취소했습니다.")
            return 1

    store.delete(profile)
    print(f"  삭제했습니다: {profile}")

    try:
        import keyring

        if keyring.get_password(KEYRING_SERVICE, profile):
            print(
                f"  참고: 키체인에 패스프레이즈가 남아 있습니다 "
                f"(service={KEYRING_SERVICE}, account={profile})."
            )
    except Exception:  # noqa: BLE001
        pass
    return 0


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def _as_path(value: Optional[str]):
    from pathlib import Path

    return Path(value).expanduser() if value else None


def run(args) -> int:
    """`agent-browser session <액션>`을 실행한다."""
    import asyncio

    action = args.session_action
    try:
        if action == "login":
            return asyncio.run(
                _run_login(
                    args.profile,
                    args.url,
                    auth_dir=args.auth_dir,
                    timeout_s=args.timeout,
                )
            )
        if action == "list":
            return _run_list(args.auth_dir, args.json)
        if action == "check":
            return asyncio.run(
                _run_check(args.profile, args.url, auth_dir=args.auth_dir)
            )
        if action == "remove":
            return _run_remove(
                args.profile, auth_dir=args.auth_dir, force=args.force
            )
    except SessionCLIError as exc:
        print(f"[-] {exc}")
        return 1
    except KeyboardInterrupt:
        print()
        return 130

    print(f"[-] 알 수 없는 동작: {action}")
    return 2
