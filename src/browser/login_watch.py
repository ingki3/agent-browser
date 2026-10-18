"""WS-58 — 사람 조작권(control take~release) 동안 생긴 로그인 사건을 알아본다.

사람이 창에서 직접 로그인하면 도구가 그 사실을 모른다. 조작권 구간 동안만 다음 둘을 본다:

1. **비밀번호 칸 제출** — 각 탭의 문서에 *격리 world*(페이지 스크립트와 전역을 공유하지 않는 CDP isolated world)
   로 작은 감시 스크립트를 넣는다. DOM 이벤트(capture)만 듣고, 값이 들어 있는 ``input[type=password]`` 가 있는
   폼 제출·버튼 클릭·비밀번호 칸 Enter 가 있으면 그 문서의 origin 만 바인딩으로 보고한다(값·이름은 보내지 않는다).
   페이지는 격리 world 의 함수·바인딩을 볼 수 없어 보고를 위조할 수 없다. 이후 문서(같은 탭의 이동)에는
   ``Page.addScriptToEvaluateOnNewDocument(worldName)`` 로 들어간다. 조작권이 끝나면 스크립트·바인딩·세션을 걷는다.
   한계: shadow DOM 안 비밀번호 칸, 설치 전에 끝난 제출, 다른 프로세스 iframe(OOPIF)은 보지 못한다.
2. **새 서버 전용(HttpOnly) 쿠키** — 조작권을 가져갈 때의 쿠키 키(name/domain/path)와 반납할 때를 비교해
   새로 생긴(또는 HttpOnly 로 바뀐) HttpOnly 키의 *쿠키 도메인*을 기록 대상으로 둔다. 비밀번호 제출이 있었으면
   그 구간에 새로 생기거나 값이 바뀐 쿠키 도메인 전부를 기록한다(로그인 하위 도메인이 상위 도메인에 쿠키를
   심는 경우 — 기록 범위는 '그 호스트와 하위').

결과는 origin 집합(쿠키 도메인은 Secure 면 https, 아니면 http — 판정은 scheme 을 보지 않는다). 기록·판정은
browser.login_records 몫이다. 감시 설치·해제 실패는 조용히 넘어간다 — 이 감시는 기록을 *더할* 뿐, 판정 규칙 (b)
(새 HttpOnly 키 거부)는 이 감시와 무관하게 늘 적용된다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from typing import Any, Dict, Optional, Set, Tuple

from browser import login_ledger

logger = logging.getLogger(__name__)

#: 감시 설치·해제 한 단계의 상한(초) — 사람 조작 흐름을 막지 않는다.
STEP_TIMEOUT_S = 2.0
#: 한 조작권 구간에서 받는 보고 상한(넘으면 더 받지 않는다 — 이미 받은 것은 남는다).
MAX_REPORTS = 64

CookieKey = Tuple[str, str, str]

_WATCH_JS = r"""(() => {
  const B = globalThis[__BINDING__];
  if (typeof B !== 'function') return;
  try { delete globalThis[__BINDING__]; } catch (e) {}
  const filled = (root) => {
    try {
      const all = root.querySelectorAll('input[type=password]');
      for (let i = 0; i < all.length; i++) if (all[i].value) return true;
    } catch (e) {}
    return false;
  };
  let sent = 0;
  const report = () => { if (sent++ < 8) { try { B(JSON.stringify({o: location.origin})); } catch (e) {} } };
  document.addEventListener('submit', (e) => { const f = e.target; if (f && filled(f)) report(); }, true);
  document.addEventListener('keydown', (e) => {
    const t = e.target;
    if (e.key === 'Enter' && t && t.type === 'password' && t.value) report();
  }, true);
  document.addEventListener('click', (e) => {
    const t = e.target && e.target.closest && e.target.closest('button,input[type=submit],input[type=image],[role=button]');
    if (t && filled(document)) report();
  }, true);
})();"""


def _source(binding: str) -> str:
    return _WATCH_JS.replace("__BINDING__", json.dumps(binding))


def cookie_origin(cookie: Dict[str, Any]) -> Optional[str]:
    """쿠키 도메인 → 기록할 origin(Secure 면 https). 형식이 안 맞으면 None."""
    host = str(cookie.get("domain") or "").lstrip(".")
    if not host:
        return None
    try:
        return login_ledger.normalize_origin(("https://" if cookie.get("secure") else "http://") + host)
    except login_ledger.LedgerError:
        return None


def _snapshot(cookies: Any) -> Dict[CookieKey, Tuple[str, bool]]:
    out: Dict[CookieKey, Tuple[str, bool]] = {}
    for c in cookies or []:
        key = (str(c.get("name", "")), str(c.get("domain", "")), str(c.get("path") or "/"))
        digest = hashlib.sha256(str(c.get("value", "")).encode("utf-8", "surrogatepass")).hexdigest()
        out[key] = (digest, c.get("httpOnly") is not False)
    return out


class ControlLoginWatch:
    """한 조작권 구간의 감시(조작권마다 새로 만든다)."""

    def __init__(self) -> None:
        self.world = "ab_login_watch_" + secrets.token_hex(6)
        self.binding = "abw" + secrets.token_hex(8)
        #: 비밀번호 칸 제출이 보고된 origin.
        self.submitted: Set[str] = set()
        #: secret_wanted 구간에 사람이 보던(에이전트 활성 탭) origin.
        self.secret_origins: Set[str] = set()
        self.baseline: Optional[Dict[CookieKey, Tuple[str, bool]]] = None
        self._contexts: list = []
        #: page → (CDP 세션, 추가한 스크립트 id 또는 None)
        self._pages: Dict[Any, Tuple[Any, Optional[str]]] = {}
        self._pending: Set[asyncio.Task] = set()
        self._reports = 0
        self.closed = False

    # -- 시작·유지 -----------------------------------------------------------

    async def ensure(self, context: Any) -> None:
        """이 컨텍스트의 모든 탭(이후 새 탭 포함)에 감시를 건다. 처음 부를 때 쿠키 기준점을 잡는다."""
        if self.closed or context is None:
            return
        if self.baseline is None:
            try:
                self.baseline = _snapshot(await context.cookies())
            except Exception:  # noqa: BLE001 - 기준점이 없으면 쿠키 비교는 하지 않는다(판정 규칙 b 는 그대로)
                self.baseline = {}
        if context not in self._contexts:
            self._contexts.append(context)

            def opened(page: Any) -> None:
                if self.closed:
                    return
                task = asyncio.get_running_loop().create_task(self._attach(page))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)

            try:
                context.on("page", opened)
            except Exception:  # noqa: BLE001
                pass
        for page in list(getattr(context, "pages", []) or []):
            await self._attach(page)

    async def _attach(self, page: Any) -> None:
        if self.closed or page in self._pages:
            return
        try:
            if page.is_closed():
                return
        except Exception:  # noqa: BLE001
            return
        self._pages[page] = (None, None)
        try:
            session = await asyncio.wait_for(page.context.new_cdp_session(page), STEP_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - 설치 실패: 이 탭은 쿠키 비교만
            return
        self._pages[page] = (session, None)

        def called(event: Dict[str, Any]) -> None:
            if event.get("name") == self.binding:
                self._reported(event.get("payload"))

        try:
            session.on("Runtime.bindingCalled", called)
            send = session.send
            # Page 도메인을 켜야 이 세션의 addScriptToEvaluateOnNewDocument 가 다음 문서에 들어간다(실측).
            await asyncio.wait_for(send("Page.enable"), STEP_TIMEOUT_S)
            await asyncio.wait_for(send("Runtime.enable"), STEP_TIMEOUT_S)
            await asyncio.wait_for(send("Runtime.addBinding", {"name": self.binding,
                                                                "executionContextName": self.world}), STEP_TIMEOUT_S)
            source = _source(self.binding)
            added = await asyncio.wait_for(send("Page.addScriptToEvaluateOnNewDocument",
                                                {"source": source, "worldName": self.world}), STEP_TIMEOUT_S)
            self._pages[page] = (session, str(added.get("identifier")))
            tree = await asyncio.wait_for(send("Page.getFrameTree"), STEP_TIMEOUT_S)
            frames = []
            stack = [tree.get("frameTree") or {}]
            while stack:
                node = stack.pop()
                frame = node.get("frame") or {}
                if frame.get("id"):
                    frames.append(frame["id"])
                stack.extend(node.get("childFrames") or [])
            for frame_id in frames:
                try:
                    world = await asyncio.wait_for(send("Page.createIsolatedWorld",
                                                        {"frameId": frame_id, "worldName": self.world}), STEP_TIMEOUT_S)
                    await asyncio.wait_for(send("Runtime.evaluate", {"expression": source,
                                                                     "contextId": world["executionContextId"]}),
                                           STEP_TIMEOUT_S)
                except Exception:  # noqa: BLE001 - 사라진 프레임
                    pass
        except Exception:  # noqa: BLE001 - 닫힌 탭 등: 쿠키 비교만 남는다
            logger.debug("조작권 로그인 감시 설치 실패", exc_info=True)
        if self.closed:
            await self._detach(page)

    def _reported(self, payload: Any) -> None:
        if self.closed or not isinstance(payload, str) or len(payload) > 512 or self._reports >= MAX_REPORTS:
            return
        self._reports += 1
        try:
            origin = json.loads(payload).get("o")
            self.submitted.add(login_ledger.normalize_origin(str(origin)))
        except Exception:  # noqa: BLE001 - opaque origin('null') 등은 기록하지 않는다
            return

    def note_secret(self, url: str) -> None:
        """secret_wanted 인계 중 사람이 보던 문서(에이전트 활성 탭)."""
        try:
            self.secret_origins.add(login_ledger.normalize_origin(url))
        except login_ledger.LedgerError:
            pass

    # -- 끝 -----------------------------------------------------------------

    async def finish(self, context: Any) -> Dict[str, str]:
        """감시를 걷고 기록할 origin → 사건 이름(``password_submit``·``server_cookie``·``secret_handoff``)."""
        self.closed = True
        for task in list(self._pending):
            task.cancel()
        out: Dict[str, str] = {}
        for origin in self.submitted:
            out.setdefault(origin, "password_submit")
        for origin in self.secret_origins:
            out.setdefault(origin, "secret_handoff")
        if context is not None and self.baseline is not None:
            try:
                cookies = list(await context.cookies())
            except Exception:  # noqa: BLE001
                cookies = []
            now = _snapshot(cookies)
            for cookie in cookies:
                key = (str(cookie.get("name", "")), str(cookie.get("domain", "")), str(cookie.get("path") or "/"))
                digest, http_only = now[key]
                before = self.baseline.get(key)
                new_server = http_only and (before is None or not before[1])
                changed = before is None or before[0] != digest
                if new_server or (self.submitted and changed):
                    origin = cookie_origin(cookie)
                    if origin is not None:
                        out.setdefault(origin, "server_cookie")
        for page in list(self._pages):
            await self._detach(page)
        return out

    async def _detach(self, page: Any) -> None:
        session, script = self._pages.pop(page, (None, None))
        if session is None:
            return
        steps = []
        if script:
            steps.append(("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script}))
        steps.append(("Runtime.removeBinding", {"name": self.binding}))
        for method, params in steps:
            try:
                await asyncio.wait_for(session.send(method, params), STEP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - 닫힌 탭
                pass
        try:
            await asyncio.wait_for(session.detach(), STEP_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            pass
