"""로그인 세션 재사용: 저장된 세션이 유효하면 로그인을 건너뛰고, 아니면 로그인 후 저장한다.

실측(2026-09-23 네이버) — 자동 로그인은 매번 캡차에 막혀 사람이 풀어야 했다.
한 번 로그인한 세션(쿠키)을 암호화 저장해 두면 다음 실행은 캡차 없이 바로 쓴다.

저장 형식은 기존 `SessionStore`(AES-256-GCM, 0600) 그대로 — `agent-browser
session list/check/remove` 명령으로 같이 관리된다. 비밀번호는 저장하지 않는다.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from browser.login_flow import HandoffInfo, LoginState, login_with_handoff, read_login_state
from browser.session_store import DecryptionError, SessionStore, SessionStoreError
from security.credentials import Credential

logger = logging.getLogger(__name__)

#: storage_state에 얹는 메타데이터 키 (session_cli와 같은 이름)
META_KEY = "_agent_browser_meta"


@dataclass
class LoggedIn:
    source: str  # "session" | "login" | "failed"
    state: LoginState
    context: Any = field(repr=False)
    page: Any = field(repr=False)
    note: str = ""


async def _new_context(browser: Any, setup, **kw) -> Any:
    ctx = await browser.new_context(**kw)
    if setup is not None:
        maybe = setup(ctx)
        if inspect.isawaitable(maybe):
            await maybe
    return ctx


async def open_logged_in(
    browser: Any,
    *,
    profile: str,
    cred: Credential,
    store: SessionStore,
    passphrase: str,
    login_url: str,
    check_url: str,
    context_kwargs: Optional[dict] = None,
    context_setup: Optional[Callable[[Any], Optional[Awaitable[Any]]]] = None,
    on_handoff: Optional[Callable[[HandoffInfo], Any]] = None,
    settle_ms: int = 3000,
    human_timeout_s: float = 300,
    poll_ms: int = 1000,
) -> LoggedIn:
    """로그인된 컨텍스트와 페이지(check_url 위)를 돌려준다.

    context_setup: 새 컨텍스트마다 호출(테스트의 route 설치 등).
    """
    kw = dict(context_kwargs or {})
    note = ""

    # 1) 저장된 세션
    if store.exists(profile):
        try:
            state = store.load(profile, passphrase)
        except (DecryptionError, SessionStoreError) as exc:
            # 키체인 교체·파일 손상 — 로그인으로 폴백하고 성공 시 덮어쓴다.
            note = f"저장된 세션 복호화 실패({type(exc).__name__}) — 다시 로그인"
            logger.warning(note)
        else:
            state = {k: v for k, v in state.items() if k != META_KEY}
            ctx = await _new_context(browser, context_setup, storage_state=state, **kw)
            page = await ctx.new_page()
            await page.goto(check_url, wait_until="domcontentloaded")
            if await read_login_state(page) is LoginState.LOGGED_IN:
                return LoggedIn("session", LoginState.LOGGED_IN, ctx, page, "저장된 세션 사용")
            note = "저장된 세션 만료 — 다시 로그인"
            await ctx.close()

    # 2) 로그인
    ctx = await _new_context(browser, context_setup, **kw)
    page = await ctx.new_page()
    await page.goto(login_url, wait_until="domcontentloaded")
    outcome = await login_with_handoff(
        page, cred, settle_ms=settle_ms, human_timeout_s=human_timeout_s,
        poll_ms=poll_ms, on_handoff=on_handoff,
    )
    if outcome.state is not LoginState.LOGGED_IN:
        return LoggedIn("failed", outcome.state, ctx, page,
                        "; ".join(x for x in (note, outcome.reason) if x))

    # 로그인 직후 페이지가 목적지가 아닐 수 있다 — 확인 주소로 가서 다시 본다.
    await page.goto(check_url, wait_until="domcontentloaded")
    if await read_login_state(page) is not LoginState.LOGGED_IN:
        return LoggedIn("failed", LoginState.LOGIN_FORM, ctx, page,
                        "로그인 뒤 확인 주소에서 다시 로그인 화면이 나옴 — 저장하지 않음")

    storage: dict = dict(await ctx.storage_state())
    storage[META_KEY] = {
        "profile": profile,
        "login_url": login_url.split("?")[0],
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "via": outcome.method,
    }
    store.save(profile, storage, passphrase)
    return LoggedIn("login", LoginState.LOGGED_IN, ctx, page,
                    "; ".join(x for x in (note, f"로그인({outcome.method}) 후 세션 저장") if x))
