"""사이트별 영속 프로필 + 사람 먼저 로그인 + 사이트 밖 이동 차단.

다른 브라우저 에이전트의 방식을 따른다(2026-09 조사):
  - Playwright MCP·Browserbase: 브라우저 프로필 폴더를 통째로 유지(기본값)
  - ChatGPT agent·Meta Muse: 로그인은 사람이 직접, 모델은 비밀번호를 보지 않음
  - browser-use: allowed_domains로 사이트 밖 이동 차단

실측(2026-09-24 네이버)으로 바꾼 것:
  - 쿠키만 옮겨 담은 세션(storage_state)은 다음 실행에서 거부됐다 → 프로필 폴더
  - 자동 제출은 매번 캡차 → 기본은 칸만 채우고 제출은 사람이
  - 로그인 직후 곧바로 이동·확인하다 '실패'로 끝냈다 → 화면 안정 대기 후 확인,
    확인 실패는 계속 대기
  - 3초마다 새 탭으로 메일함을 열어 확인하다 메일함↔로그인 화면이 1분간 반복됐다
    → 새 탭·반복 이동 없이 **사람이 보는 탭**이 보호 페이지에서 안정되면 완료.
    사람이 '완료'를 알렸는데 다른 페이지에 있으면 그 탭만 한 번 이동해 본다.
  - 실패 원인을 가를 기록이 없었다 → LoginTracer(탭·주소·상태·쿠키 수명, 값 없음)
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
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import parse_qsl, urlsplit

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


def _same_page(url: str, target: str) -> bool:
    """같은 호스트이고 target 경로로 시작하면 보호 페이지에 있는 것으로 본다."""
    u, t = urlsplit(url), urlsplit(target)
    if (u.hostname or "").lower() != (t.hostname or "").lower():
        return False
    return (u.path or "/").startswith((t.path or "/").rstrip("/") or "/")


def redact_url(url: str) -> str:
    """주소에서 쿼리 값을 지운다(키 이름만 남김). 토큰·아이디가 기록에 남지 않게."""
    try:
        u = urlsplit(url)
    except ValueError:
        return "<bad-url>"
    if u.scheme not in ("http", "https"):
        return u.scheme + ":"
    keys = sorted({k for k, _ in parse_qsl(u.query, keep_blank_values=True)})
    return f"{u.scheme}://{u.netloc}{u.path}" + (f" [{','.join(keys)}]" if keys else "")


class LoginTracer:
    """로그인 과정 진단 기록 — 탭 열림/닫힘, 최상위 이동, 로그인 상태, 로그인 쿠키 수명.

    값은 담지 않는다: 주소는 쿼리 키 이름만, 쿠키는 있음/영속 여부/남은 일수만.
    sink에 dict를 하나씩 넘긴다(예: 0600 JSONL 파일에 쓰기).
    """

    def __init__(self, context: Any, domain: str, cookie_names: Sequence[str],
                 *, sink: Callable[[Dict[str, Any]], Any]) -> None:
        self.context = context
        self.domain = domain
        self.cookie_names = list(cookie_names)
        self.sink = sink
        self._t0 = time.monotonic()
        self._tabs: Dict[int, int] = {}
        self._cookies: Dict[str, Any] = {}
        self._last_state: Optional[str] = None
        self._handlers: List[Any] = []

    def _emit(self, kind: str, **kw: Any) -> None:
        ev = {"t": round(time.monotonic() - self._t0, 2), "wall": time.strftime("%H:%M:%S"),
              "kind": kind, **kw}
        try:
            self.sink(ev)
        except Exception:  # noqa: BLE001 - 기록 실패가 로그인을 막으면 안 된다
            pass

    def _tab(self, page: Any) -> int:
        if id(page) not in self._tabs:
            self._tabs[id(page)] = len(self._tabs)
        return self._tabs[id(page)]

    def _watch(self, page: Any) -> None:
        tab = self._tab(page)

        def on_nav(frame: Any) -> None:
            if frame == page.main_frame:
                self._emit("nav", tab=tab, url=redact_url(frame.url))

        page.on("framenavigated", on_nav)
        page.on("close", lambda *_: self._emit("tab_close", tab=tab))

    async def start(self) -> None:
        for pg in list(self.context.pages):
            self._watch(pg)

        def on_page(pg: Any) -> None:
            self._emit("tab_open", tab=self._tab(pg))
            self._watch(pg)

        self.context.on("page", on_page)
        self._handlers.append(on_page)
        await self.poll()

    def stop(self) -> None:
        for h in self._handlers:
            try:
                self.context.remove_listener("page", h)
            except Exception:  # noqa: BLE001
                pass
        self._handlers.clear()

    def state(self, page: Any, state: LoginState) -> None:
        if state.value != self._last_state:
            self._last_state = state.value
            self._emit("state", tab=self._tab(page), state=state.value)

    def note(self, what: str, **kw: Any) -> None:
        self._emit("note", what=what, **kw)

    async def poll(self) -> None:
        """로그인 쿠키가 생기거나 사라지거나 수명이 바뀌면 기록한다."""
        try:
            rep = await auth_cookie_report(self.context, self.domain, self.cookie_names)
        except Exception:  # noqa: BLE001
            return
        for name, r in rep.items():
            cur = None if r is None else (r["persistent"], None if r["days_left"] is None
                                          else round(r["days_left"]))
            if name in self._cookies and self._cookies[name] == cur:
                continue
            self._cookies[name] = cur
            self._emit("cookie", name=name, present=r is not None,
                       persistent=bool(r and r["persistent"]),
                       days_left=(r or {}).get("days_left"))


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
    remember_me: bool = True,
    tracer: Optional[LoginTracer] = None,
) -> SiteSession:
    """로그인된 상태로 page를 check_url에 둔다. 제출은 사람이 한다.

    완료 판정은 사람이 보는 탭(page)만 본다 — 보호 페이지(check_url)에서 로그인
    화면이 아닌 상태로 stable_ms 동안 머물면 완료. 새 탭을 열거나 반복해서
    이동하지 않는다. done_event가 오면: 이미 보호 페이지면 그대로 판정하고,
    아니면 그 탭을 한 번만 check_url로 이동해 본다.

    remember_me: '로그인 상태 유지'를 체크해 둔다. 폼이 새로 그려지면(캡차 화면)
    다시 체크하지만, 같은 칸을 사람이 끄면 존중한다(되돌리지 않음).
    """
    started = time.monotonic()
    checks = 0

    def done(source: str, state: LoginState, note: str, info=None) -> SiteSession:
        if tracer is not None:
            tracer._emit("result", source=source, state=state.value, checks=checks)
        return SiteSession(source, state, note, checks, time.monotonic() - started, info)

    async def observe() -> LoginState:
        st = await read_login_state(page)
        if tracer is not None:
            tracer.state(page, st)
            await tracer.poll()
        return st

    # 1) 프로필에 로그인이 남아 있나
    await page.goto(check_url, wait_until="domcontentloaded")
    if await wait_settled(page, stable_ms=min(stable_ms, 2000)) is LoginState.LOGGED_IN \
            and _same_page(page.url, check_url):
        await observe()
        return done("profile", LoginState.LOGGED_IN, "프로필에 로그인이 남아 있음")

    # 2) 로그인 화면으로 가서 칸만 채운다(제출하지 않음)
    if not looks_like_login_url(page.url) or await read_login_state(page) is LoginState.LOGGED_IN:
        await page.goto(login_url, wait_until="domcontentloaded")
    state = await observe()
    try:
        report = await fill_login_fields(page, cred)
    except Exception:  # noqa: BLE001 - 다른 도메인·칸 없음
        from browser.login_flow import FillReport
        report = FillReport(False, False)
    remembered = await _try_remember(page) if remember_me else None
    info = HandoffInfo(state=state, url=page.url.split("?")[0],
                       username_filled=report.username, password_filled=report.password,
                       remember_checked=remembered)
    if tracer is not None:
        tracer.note("handoff", remember_checked=remembered)
    if on_handoff is not None:
        maybe = on_handoff(info)
        if asyncio.iscoroutine(maybe):
            await maybe

    # 3) 사람을 기다린다 — 빈 칸 다시 채우기, 사람이 보는 탭만 지켜본다.
    #    '화면 안정'은 폴링 안에서 센다. 따로 막아 두고 기다리면 그 사이에 온
    #    완료 신호를 못 본다.
    deadline = time.monotonic() + human_timeout_s
    stable_key = None
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_ms / 1000)
        try:
            state = await observe()
            if state is not LoginState.LOGGED_IN:
                await fill_login_fields(page, cred, only_empty=True)
                # 표시가 사라졌다 = 폼이 새로 그려졌다(캡차 화면 등) → 다시 체크.
                # 표시가 남아 있으면 같은 칸이니 사람이 끈 것을 되돌리지 않는다.
                if remember_me and not await page.evaluate(
                        "() => !!document.querySelector('[data-ab-remember]')"):
                    await _try_remember(page)
        except Exception:  # noqa: BLE001 - 페이지 전환 중·다른 도메인
            continue

        on_target = state is LoginState.LOGGED_IN and _same_page(page.url, check_url)
        key = (page.url, state)
        if key != stable_key:
            stable_key, stable_since = key, time.monotonic()
        settled = (time.monotonic() - stable_since) * 1000 >= stable_ms

        if on_target and settled:
            checks += 1
            return done("human", LoginState.LOGGED_IN, f"사람이 로그인(확인 {checks}회)", info)

        if done_event is not None and done_event.is_set():
            done_event.clear()
            checks += 1
            if tracer is not None:
                tracer.note("done_signal", on_target=on_target)
            if not on_target:
                # 사람이 끝났다고 했는데 보호 페이지가 아니다 → 그 탭을 한 번만 이동
                try:
                    await page.goto(check_url, wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass
            st = await wait_settled(page, stable_ms=min(stable_ms, 1500))
            if tracer is not None:
                tracer.state(page, st)
                await tracer.poll()
            if st is LoginState.LOGGED_IN and _same_page(page.url, check_url):
                return done("human", LoginState.LOGGED_IN, f"사람이 로그인(확인 {checks}회)", info)
            # 아직 아니다 — 다시 알려 줄 때까지 기다린다(반복 이동하지 않음)
            stable_key, stable_since = None, time.monotonic()

    return done("failed", await read_login_state(page),
                f"{human_timeout_s:.0f}초 안에 로그인이 확인되지 않음(확인 {checks}회)", info)
