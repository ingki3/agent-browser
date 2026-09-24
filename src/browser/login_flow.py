"""로그인: 코드로 1회 자동 시도 → 실패하면 칸을 채운 채 사람에게 넘긴다.

흐름::

    1. 로그인 페이지에서 아이디·비밀번호 칸을 코드로 채우고 Enter (1회만)
    2. settle_ms 뒤 상태 판정
         LOGGED_IN  → 끝 (method="auto")
         그 밖      → 3
    3. 칸이 보이면 다시 채워 두고 on_handoff(info) 호출 → 사람이 창에서
       캡차·2단계 인증을 처리하고 로그인 버튼을 누른다
    4. poll_ms 간격으로 LOGGED_IN이 될 때까지 기다린다 (human_timeout_s 상한)

AI(LLM)를 쓰지 않는다. 실측(2026-09-23 네이버) — 에이전트가 아이디 칸에 두 번
입력해 값이 5자로 망가졌다. 칸 찾기·채우기·제출은 결정적인 코드가 맡고,
로그인 뒤의 작업만 에이전트에게 준다.

값은 `page.fill`에만 전달된다. 결과 객체·로그·예외 메시지에 싣지 않는다.

한계: 최상위 문서만 본다(iframe 안 로그인 폼 미지원). 자동 시도는 사이트의
자동화 탐지에 걸릴 수 있다 — 그래서 사람 인계가 기본 경로다.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from security.credentials import Credential, _host, _matches


class LoginState(str, Enum):
    LOGGED_IN = "logged_in"
    LOGIN_FORM = "login_form"  # 비밀번호 칸이 보이거나 로그인 주소
    CAPTCHA = "captcha"  # 자동입력 방지 문자 요구


@dataclass
class HandoffInfo:
    state: LoginState
    url: str
    username_filled: bool
    password_filled: bool


@dataclass
class FillReport:
    username: bool
    password: bool


@dataclass
class LoginOutcome:
    method: str  # "auto" | "human" | "failed"
    state: LoginState
    reason: str = ""
    elapsed_s: float = 0.0
    handoff: Optional[HandoffInfo] = field(default=None, repr=False)


_LOGIN_URL_RE = re.compile(r"(log-?in|sign-?in|signon|/auth\b|nidlogin)", re.I)

# 페이지 안에서 로그인 칸을 찾아 표시한다. 값은 여기로 넘기지 않는다.
_MARK_FIELDS_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  document.querySelectorAll('[data-ab-login]').forEach(e => e.removeAttribute('data-ab-login'));
  const pw = [...document.querySelectorAll('input[type=password]')].find(vis);
  if (!pw) return {password: false, username: false};
  pw.setAttribute('data-ab-login', 'password');
  const textish = el => ['text', 'email', 'tel', ''].includes((el.getAttribute('type') || '').toLowerCase());
  const scope = pw.form || document;
  const cands = [...scope.querySelectorAll('input')].filter(el => el !== pw && textish(el) && vis(el));
  // 1순위: autocomplete=username  2순위: 비밀번호 칸 바로 앞의 텍스트 칸
  let user = cands.find(el => (el.getAttribute('autocomplete') || '').includes('username'));
  if (!user) {
    const before = cands.filter(el => el.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING);
    user = before[before.length - 1];
  }
  if (user) user.setAttribute('data-ab-login', 'username');
  return {password: true, username: !!user};
}
"""

_STATE_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  // 보이는 것만 캡차로 본다. 실측(2026-09-24 네이버) — 평범한 로그인 화면에도
  // 숨은 input#ncaptchaSplit이 있어, 존재만 보면 자동 로그인을 건너뛰었다.
  const captcha = [...document.querySelectorAll(
    "input[id*=captcha i], input[name*=captcha i], " +
    "iframe[src*=captcha i], iframe[src*=recaptcha i], iframe[src*=hcaptcha i], " +
    "[class*=g-recaptcha], [class*=h-captcha]")]
    .some(el => (el.getAttribute('type') || '').toLowerCase() !== 'hidden' && vis(el));
  const pw = [...document.querySelectorAll('input[type=password]')].some(vis);
  return {captcha, pw};
}
"""


def looks_like_login_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return bool(_LOGIN_URL_RE.search(f"{p.path}?{p.query}"))


async def read_login_state(page: Any) -> LoginState:
    try:
        s = await page.evaluate(_STATE_JS)
    except Exception:  # noqa: BLE001 - 페이지 전환 중이면 아직 로그인 전으로 본다
        return LoginState.LOGIN_FORM
    if s.get("captcha"):
        return LoginState.CAPTCHA
    if s.get("pw") or looks_like_login_url(page.url):
        return LoginState.LOGIN_FORM
    return LoginState.LOGGED_IN


async def fill_login_fields(page: Any, cred: Credential) -> FillReport:
    """보이는 로그인 칸을 채운다. 현재 페이지가 cred 도메인이 아니면 거부."""
    host = _host(page.url)
    if not host or not _matches(host, cred.domain):
        raise PermissionError(
            f"현재 페이지가 {cred.domain} 도메인이 아니라 자격증명을 입력하지 않습니다."
        )
    found = await page.evaluate(_MARK_FIELDS_JS)
    if found.get("username"):
        await page.fill("[data-ab-login=username]", cred.username)
    if found.get("password"):
        await page.fill("[data-ab-login=password]", cred.password)
    return FillReport(username=bool(found.get("username")), password=bool(found.get("password")))


async def _submit(page: Any) -> None:
    await page.press("[data-ab-login=password]", "Enter")


async def login_with_handoff(
    page: Any,
    cred: Credential,
    *,
    settle_ms: int = 3000,
    human_timeout_s: float = 300,
    poll_ms: int = 1000,
    on_handoff: Optional[Callable[[HandoffInfo], Any]] = None,
) -> LoginOutcome:
    started = time.monotonic()

    def done(method: str, state: LoginState, reason: str, info=None) -> LoginOutcome:
        return LoginOutcome(method=method, state=state, reason=reason,
                            elapsed_s=time.monotonic() - started, handoff=info)

    state = await read_login_state(page)
    if state is LoginState.LOGGED_IN:
        return done("auto", state, "이미 로그인돼 있음")

    # 1) 자동 시도 — 제출은 이 한 번뿐이다. 캡차가 이미 떠 있으면 건너뛴다.
    if state is LoginState.LOGIN_FORM:
        report = await fill_login_fields(page, cred)
        if report.password:
            await _submit(page)
            await page.wait_for_timeout(settle_ms)
            state = await read_login_state(page)
            if state is LoginState.LOGGED_IN:
                return done("auto", state, "자동 로그인 성공")

    # 2) 사람에게 넘긴다 — 보이는 칸은 다시 채워 둔다(제출은 하지 않음).
    try:
        report = await fill_login_fields(page, cred)
    except PermissionError:
        report = FillReport(username=False, password=False)
    except Exception:  # noqa: BLE001 - 페이지 전환 중
        report = FillReport(username=False, password=False)
    info = HandoffInfo(state=state, url=page.url.split("?")[0],
                       username_filled=report.username, password_filled=report.password)
    if on_handoff is not None:
        maybe = on_handoff(info)
        if asyncio.iscoroutine(maybe):
            await maybe

    deadline = time.monotonic() + human_timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_ms / 1000)
        state = await read_login_state(page)
        if state is LoginState.LOGGED_IN:
            return done("human", state, "사람이 로그인을 마침", info)
    return done("failed", state, f"{human_timeout_s:.0f}초 안에 로그인이 끝나지 않음", info)
