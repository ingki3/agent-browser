"""사이트별 영속 프로필 + 사람 먼저 로그인 + 사이트 밖 이동 차단.

다른 브라우저 에이전트의 방식을 따른다(2026-09 조사):
  - Playwright MCP·Browserbase: 브라우저 프로필 폴더를 통째로 유지(기본값)
  - ChatGPT agent·Meta Muse: 로그인은 사람이 직접, 모델은 비밀번호를 보지 않음
  - browser-use: allowed_domains로 사이트 밖 이동 차단

실측(2026-09-24 네이버)으로 바꾼 것:
  - 쿠키만 옮겨 담은 세션(storage_state)은 다음 실행에서 거부됐다 → 프로필 폴더
  - 자동 제출은 매번 캡차 → 기본은 칸만 채우고 제출은 사람이
  - 로그인 직후 곧바로 이동·확인하다 '실패'로 끝냈다 → 화면 안정 대기 후 **새 탭**
    으로 보호 페이지 확인(원래 탭의 리다이렉트를 끊지 않음), 확인 실패는 계속 대기
  - 캡차 화면에서 폼이 새로 그려져 칸이 비었다 → 빈 칸은 대기 중 다시 채운다

한계: 프로필 폴더에는 쿠키가 평문으로 남는다(Playwright Chromium은 OS 키체인
암호화를 쓰지 않는다). 폴더 권한 700과 사이트별 분리로만 보호한다.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from browser.login_flow import (
    HandoffInfo,
    LoginState,
    fill_login_fields,
    looks_like_login_url,
    read_login_state,
)
from security.credentials import Credential, _host, _matches

DEFAULT_PROFILE_ROOT = Path.home() / ".agent-browser" / "profiles"
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


@dataclass
class SiteSession:
    source: str  # "profile" | "human" | "auto" | "failed"
    state: LoginState
    note: str = ""
    checks: int = 0
    elapsed_s: float = 0.0
    handoff: Optional[HandoffInfo] = field(default=None, repr=False)


def profile_dir_for(domain: str, *, root: Optional[Path] = None) -> Path:
    d = (domain or "").strip().lower()
    if not _DOMAIN_RE.match(d):
        raise ValueError(f"프로필 도메인 형식이 잘못됐습니다: {domain!r}")
    return Path(root or DEFAULT_PROFILE_ROOT) / d


async def open_site_context(pw: Any, domain: str, *, root: Optional[Path] = None,
                            headless: bool = False, **kwargs: Any) -> Any:
    """사이트 전용 영속 프로필로 브라우저를 연다(폴더 권한 700)."""
    path = profile_dir_for(domain, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return await pw.chromium.launch_persistent_context(str(path), headless=headless, **kwargs)


async def install_navigation_guard(context: Any, allowed_domains: Sequence[str]) -> List[str]:
    """최상위 문서 이동만 사이트 안으로 제한한다. 막힌 주소 목록을 돌려준다.

    하위 리소스(CDN 스크립트·이미지)는 막지 않는다 — 막으면 사이트가 깨진다.
    막힌 최상위 이동은 Chromium이 오류 페이지(chrome-error://)를 띄우므로, 직전
    허용 주소로 되돌린다. 안 그러면 에이전트가 오류 페이지에 갇힌다.
    """
    allowed = [d.lower().lstrip(".") for d in allowed_domains]
    blocked: List[str] = []
    last_ok: dict = {}

    async def guard(route: Any, request: Any) -> None:
        try:
            frame = request.frame
            top = request.is_navigation_request() and frame.parent_frame is None
        except Exception:  # noqa: BLE001 - 서비스 워커 요청 등 frame 없음
            frame, top = None, False
        host = _host(request.url)
        if top and host and not any(_matches(host, d) for d in allowed):
            blocked.append(request.url.split("?")[0])
            await route.abort("blockedbyclient")
            page = frame.page if frame is not None else None
            back = last_ok.get(id(page)) if page is not None else None
            if page is not None and back:
                async def restore() -> None:
                    # 오류 페이지가 뜬 뒤에 되돌린다. 'chrome-error://' 전환을 기다리는
                    # 방식은 경쟁이 있어(이미 떴으면 못 봄) 주소를 직접 확인한다.
                    for _ in range(30):
                        await asyncio.sleep(0.1)
                        try:
                            if page.is_closed():
                                return
                            if page.url.startswith("chrome-error://"):
                                await page.goto(back, wait_until="domcontentloaded")
                                return
                        except Exception:  # noqa: BLE001
                            return
                asyncio.get_running_loop().create_task(restore())
            return
        if top and host and frame is not None:
            last_ok[id(frame.page)] = request.url
        await route.fallback()

    await context.route("**/*", guard)
    return blocked


async def wait_settled(page: Any, *, stable_ms: int = 2000, poll_ms: int = 250,
                       timeout_ms: int = 20000) -> LoginState:
    """주소와 로그인 상태가 stable_ms 동안 바뀌지 않을 때까지 기다린다."""
    deadline = time.monotonic() + timeout_ms / 1000
    last = None
    since = time.monotonic()
    state = await read_login_state(page)
    while time.monotonic() < deadline:
        state = await read_login_state(page)
        key = (page.url, state)
        if key != last:
            last, since = key, time.monotonic()
        elif (time.monotonic() - since) * 1000 >= stable_ms:
            return state
        await asyncio.sleep(poll_ms / 1000)
    return state


async def verify_logged_in(context: Any, check_url: str, *, stable_ms: int = 2000) -> bool:
    """새 탭으로 보호 페이지를 열어 로그인 여부를 확인한다(원래 탭은 건드리지 않음)."""
    probe = await context.new_page()
    try:
        await probe.goto(check_url, wait_until="domcontentloaded")
        state = await wait_settled(probe, stable_ms=stable_ms)
        return state is LoginState.LOGGED_IN
    except Exception:  # noqa: BLE001
        return False
    finally:
        await probe.close()


async def check_remember_me(page: Any) -> Optional[bool]:
    """'로그인 상태 유지' 체크박스를 찾아 체크한다(이미 체크돼 있으면 그대로).

    반환: True 체크됨 / False 찾았지만 체크 실패 / None 못 찾음.
    라벨 문구로만 고른다 — 'IP 보안' 같은 옆 스위치나 약관 동의는 건드리지 않는다.
    실측(2026-09-24 네이버): input#loginStay[name=nvlong] + label '로그인 상태 유지'.
    이 칸이 꺼진 채 로그인하면 핵심 쿠키(NID_AUT·NID_SES)가 세션 쿠키로 와서
    브라우저를 닫는 순간 사라졌다 — 프로필 폴더를 유지해도 로그인이 남지 않는다.
    """
    found = await page.evaluate(_MARK_REMEMBER_JS)
    if not found:
        return None
    sel = "[data-ab-remember]"
    # 1) 사람처럼 누르기(보이는 요소) → 2) 라벨 누르기(숨은 입력 + 라벨로 그린 UI)
    # → 3) DOM click(크기 0 등 누를 수 없는 요소). 3도 같은 click 이벤트라 페이지
    # 핸들러가 그대로 돈다(값만 바꾸는 방식과 다름).
    attempts = [
        lambda: page.click(sel, timeout=2000),
        lambda: page.click("[data-ab-remember-label]", timeout=2000),
        lambda: page.evaluate("document.querySelector('[data-ab-remember]').click()"),
    ]
    for attempt in attempts:
        if await page.evaluate(_REMEMBER_STATE_JS):
            return True
        try:
            await attempt()
        except Exception:  # noqa: BLE001 - 다음 방법으로
            continue
    return bool(await page.evaluate(_REMEMBER_STATE_JS))


_REMEMBER_TEXT = r"(로그인\s*상태\s*유지|자동\s*로그인|로그인\s*유지|keep\s+me\s+(signed|logged)\s+in|remember\s+me|stay\s+signed\s+in)"

_MARK_REMEMBER_JS = r"""
() => {
  const re = new RegExp(%s, 'i');
  document.querySelectorAll('[data-ab-remember],[data-ab-remember-label]').forEach(e => {
    e.removeAttribute('data-ab-remember'); e.removeAttribute('data-ab-remember-label'); });
  const text = el => {
    const lab = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    return [(lab && lab.innerText) || '', (el.closest('label') || {}).innerText || '',
            el.getAttribute('aria-label') || '', el.getAttribute('title') || ''].join(' ');
  };
  for (const el of document.querySelectorAll('input[type=checkbox]')) {
    if (!re.test(text(el))) continue;
    el.setAttribute('data-ab-remember', '1');
    const lab = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : el.closest('label');
    if (lab) lab.setAttribute('data-ab-remember-label', '1');
    return 'input';
  }
  for (const el of document.querySelectorAll('[role=checkbox]:not(input)')) {
    if (!re.test(text(el) + ' ' + (el.innerText || ''))) continue;
    el.setAttribute('data-ab-remember', '1');
    return 'aria';
  }
  return null;
}
""" % repr(_REMEMBER_TEXT)

_REMEMBER_STATE_JS = r"""
() => { const e = document.querySelector('[data-ab-remember]');
  if (!e) return false;
  return e.tagName === 'INPUT' ? e.checked : e.getAttribute('aria-checked') === 'true'; }
"""


async def auth_cookie_report(context: Any, domain: str, names: Sequence[str]) -> dict:
    """로그인 쿠키가 영속인지(브라우저를 닫아도 남는지)와 남은 기간. 값은 담지 않는다."""
    now = time.time()
    out: dict = {n: None for n in names}
    for c in await context.cookies():
        dom = (c.get("domain") or "").lstrip(".").lower()
        if c.get("name") in out and (dom == domain or dom.endswith("." + domain)):
            exp = c.get("expires")
            persistent = exp is not None and exp > 0
            out[c["name"]] = {
                "persistent": persistent,
                "days_left": round((exp - now) / 86400, 2) if exp is not None and exp > 0 else None,
                "domain": c.get("domain"),
            }
    return out


async def _try_remember(page: Any) -> Optional[bool]:
    try:
        return await check_remember_me(page)
    except Exception:  # noqa: BLE001 - 페이지 전환 중
        return False


async def ensure_logged_in(
    context: Any,
    page: Any,
    cred: Credential,
    *,
    login_url: str,
    check_url: str,
    on_handoff: Optional[Callable[[HandoffInfo], Any]] = None,
    done_event: Optional[asyncio.Event] = None,
    human_timeout_s: float = 300,
    poll_ms: int = 1000,
    stable_ms: int = 2000,
    recheck_s: float = 3.0,
    remember_me: bool = True,
) -> SiteSession:
    """로그인된 상태로 page를 check_url에 둔다. 제출은 사람이 한다.

    remember_me: '로그인 상태 유지'를 체크해 둔다. 폼이 새로 그려지면(캡차 화면)
    다시 체크하지만, 같은 칸을 사람이 끄면 존중한다(되돌리지 않음).
    """
    started = time.monotonic()
    checks = 0

    def done(source: str, state: LoginState, note: str, info=None) -> SiteSession:
        return SiteSession(source, state, note, checks, time.monotonic() - started, info)

    # 1) 프로필에 로그인이 남아 있나
    await page.goto(check_url, wait_until="domcontentloaded")
    if await wait_settled(page, stable_ms=min(stable_ms, 2000)) is LoginState.LOGGED_IN:
        return done("profile", LoginState.LOGGED_IN, "프로필에 로그인이 남아 있음")

    # 2) 로그인 화면으로 가서 칸만 채운다(제출하지 않음)
    if not looks_like_login_url(page.url) or await read_login_state(page) is LoginState.LOGGED_IN:
        await page.goto(login_url, wait_until="domcontentloaded")
    state = await read_login_state(page)
    try:
        report = await fill_login_fields(page, cred)
    except Exception:  # noqa: BLE001 - 다른 도메인·칸 없음
        from browser.login_flow import FillReport
        report = FillReport(False, False)
    remembered = await _try_remember(page) if remember_me else None
    info = HandoffInfo(state=state, url=page.url.split("?")[0],
                       username_filled=report.username, password_filled=report.password,
                       remember_checked=remembered)
    if on_handoff is not None:
        maybe = on_handoff(info)
        if asyncio.iscoroutine(maybe):
            await maybe

    # 3) 사람을 기다린다 — 빈 칸 다시 채우기, 자동 감지 또는 '완료' 신호로 확인.
    #    자동 감지의 '화면 안정'은 폴링 안에서 센다. 따로 막아 두고 기다리면
    #    그 사이에 온 완료 신호를 못 본다.
    deadline = time.monotonic() + human_timeout_s
    last_check = 0.0
    stable_key = None
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_ms / 1000)
        try:
            state = await read_login_state(page)
            if state is not LoginState.LOGGED_IN:
                await fill_login_fields(page, cred, only_empty=True)
                # 표시가 사라졌다 = 폼이 새로 그려졌다(캡차 화면 등) → 다시 체크.
                # 표시가 남아 있으면 같은 칸이니 사람이 끈 것을 되돌리지 않는다.
                if remember_me and not await page.evaluate(
                        "() => !!document.querySelector('[data-ab-remember]')"):
                    await _try_remember(page)
        except Exception:  # noqa: BLE001 - 페이지 전환 중·다른 도메인
            continue

        key = (page.url, state)
        if key != stable_key:
            stable_key, stable_since = key, time.monotonic()
        settled = (time.monotonic() - stable_since) * 1000 >= stable_ms

        signaled = done_event is not None and done_event.is_set()
        auto = state is LoginState.LOGGED_IN and settled
        if not (signaled or auto):
            continue
        if not signaled and time.monotonic() - last_check < recheck_s:
            continue
        checks += 1
        last_check = time.monotonic()
        # 확인 탭은 짧게 본다 — 자동 감지의 안정 대기(stable_ms)를 그대로 쓰면
        # 사람이 '완료'를 알린 뒤에도 오래 기다린다.
        if await verify_logged_in(context, check_url, stable_ms=min(stable_ms, 2000)):
            await page.goto(check_url, wait_until="domcontentloaded")
            await wait_settled(page, stable_ms=min(stable_ms, 1500))
            return done("human", LoginState.LOGGED_IN, f"사람이 로그인(확인 {checks}회)", info)
        if done_event is not None:
            done_event.clear()  # 확인 실패 — 다시 알려 줄 때까지 계속 기다린다

    return done("failed", await read_login_state(page),
                f"{human_timeout_s:.0f}초 안에 로그인이 확인되지 않음(확인 {checks}회)", info)
