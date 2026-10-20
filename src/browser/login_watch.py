"""WS-58 — 사람 조작권(control take~release) 동안 생긴 로그인 사건을 알아본다.

사람이 창에서 직접 로그인하면 도구가 그 사실을 모른다. 조작권 구간 동안 다음을 본다(보수 방향 — 사람 조작 중의
POST·쿠키 변화는 로그인으로 간주한다, 과차단은 허용):

1. **본문을 싣는 요청(POST/PUT)** — 컨텍스트의 ``request`` 사건(Playwright 가 브라우저 CDP Network 계층
   ``Network.requestWillBeSent`` 로 받는 것 — 모든 탭·iframe·팝업, 문서·fetch·XHR 공통)으로 메서드와 대상 origin
   만 본다(본문·주소 경로·머리는 읽지도 저장하지도 않는다). 페이지 스크립트는 DOM 이벤트 전파를 끊을 수 있어도
   이미 나간 요청을 이 계층에서 지울 수 없다(WS-58 R1 B1 — window capture ``stopImmediatePropagation`` 우회).
2. **쿠키 저장소 변화** — 조작권을 가져갈 때의 쿠키(name/domain/path → 값 지문)와 반납할 때를 비교해 새로 생기거나
   값이 바뀐(또는 HttpOnly 로 바뀐) 쿠키의 *쿠키 도메인*을 기록 대상으로 둔다(이름·값은 남기지 않는다).
3. **비밀번호 칸 제출(보조)** — 각 탭의 문서에 *격리 world* 로 작은 감시 스크립트를 넣어, 값이 든
   ``input[type=password]`` 가 있는 폼 제출·버튼 클릭·Enter 를 보고받는다(실제 요청 없는 가짜 이벤트도 기록 쪽).
   페이지가 이벤트를 숨길 수 있으므로 1·2 의 보조일 뿐이다.

감시 설치가 실패하면(요청 사건 등록 실패, 탭의 CDP 세션 불가, 쿠키 기준점 읽기 실패) 그 구간은 **fail-closed** —
조작권 동안 방문한(문서를 연) origin 전부를 기록한다. 결과는 origin 집합(쿠키 도메인은 Secure 면 https, 아니면
http — 판정은 scheme 을 보지 않는다). 기록·판정은 browser.login_records 몫이다. 판정 규칙 (b)(새 HttpOnly 키
거부)는 이 감시와 무관하게 늘 적용된다.
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
#: 본문을 싣는 요청 — 로그인 제출로 간주한다(메서드·origin 만 본다).
BODY_METHODS = frozenset({"POST", "PUT"})

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


def _origin(url: Any) -> Optional[str]:
    try:
        return login_ledger.normalize_origin(str(url or ""))
    except login_ledger.LedgerError:
        return None  # about:blank·data:·blob: 등 — 기록할 origin 이 아니다


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
        #: 본문을 싣는 요청(POST/PUT)이 간 origin(Network 계층).
        self.posted: Set[str] = set()
        #: 이 구간에 문서를 연 origin — 감시가 불완전(degraded)할 때 fail-closed 로 기록한다.
        self.visited: Set[str] = set()
        #: 감시 설치 일부가 실패했는가(요청 사건·CDP 세션·쿠키 기준점).
        self.degraded = False
        #: secret_wanted 구간에 사람이 보던(에이전트 활성 frame) origin.
        self.secret_origins: Set[str] = set()
        self.baseline: Optional[Dict[CookieKey, Tuple[str, bool]]] = None
        self._contexts: list = []
        #: page → (CDP 세션, 추가한 스크립트 id 또는 None)
        self._pages: Dict[Any, Tuple[Any, Optional[str]]] = {}
        self._pending: Set[asyncio.Task] = set()
        self._reports = 0
        self.closed = False

    # -- 시작·유지 -----------------------------------------------------------

    def arm(self, context: Any) -> None:
        """요청 감시(Network 계층 ``request``)·새 탭 사건을 **await 없이** 붙인다(WS-58 R2 NB1).

        조작권을 사람이 가져간 그 동기 구간에서 부른다 — 이후 어떤 await(쿠키 기준점 읽기·안내 띠·탭별 CDP
        설치) 동안 나간 요청도 기록된다. 같은 컨텍스트에 두 번 붙이지 않는다."""
        if self.closed or context is None or context in self._contexts:
            return
        self._contexts.append(context)

        def opened(page: Any) -> None:
            if self.closed:
                return
            task = asyncio.get_running_loop().create_task(self._attach(page))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

        try:
            context.on("request", self._on_request)
            context.on("page", opened)
        except Exception:  # noqa: BLE001 - 요청 신호를 받을 수 없다 → 방문 origin 전부 기록(fail-closed)
            self.degraded = True

    async def ensure(self, context: Any) -> None:
        """이 컨텍스트의 모든 탭(이후 새 탭 포함)에 감시를 건다. 요청 감시를 먼저 붙인 뒤(arm) 처음 부를 때
        쿠키 기준점을 잡는다 — 기준점 읽기(await) 동안의 요청도 놓치지 않는다."""
        if self.closed or context is None:
            return
        self.arm(context)
        if self.baseline is None:
            try:
                self.baseline = _snapshot(await context.cookies())
            except Exception:  # noqa: BLE001 - 기준점이 없으면 지금 쿠키 전부가 '새 쿠키'로 보인다 + fail-closed
                self.baseline = {}
                self.degraded = True
        if self.closed:
            return
        self._note_pages(context)
        for page in list(getattr(context, "pages", []) or []):
            await self._attach(page)

    def _on_request(self, request: Any) -> None:
        """컨텍스트 요청 사건 — 메서드·대상 origin 만 본다(본문·경로·머리 미열람)."""
        if self.closed:
            return
        try:
            method = str(request.method or "").upper()
            url = request.url
            navigation = bool(request.is_navigation_request())
        except Exception:  # noqa: BLE001 - 요청을 읽지 못함: 신호를 잃었으니 fail-closed
            self.degraded = True
            return
        origin = _origin(url)
        if origin is None:
            return
        if method in BODY_METHODS:
            self.posted.add(origin)
        if navigation:
            self.visited.add(origin)

    def note_visited(self, url: Any) -> None:
        """사람이 보던 문서(fail-closed 기록 후보)."""
        origin = _origin(url)
        if origin is not None and not self.closed:
            self.visited.add(origin)

    def mark_degraded(self) -> None:
        """감시를 이 구간에 걸 수 없었다(서버 쪽 실패) — 반납 때 방문 origin 전부 기록."""
        self.degraded = True

    def _note_pages(self, context: Any) -> None:
        try:
            pages = list(getattr(context, "pages", []) or [])
        except Exception:  # noqa: BLE001
            return
        for page in pages:
            self.note_visited(getattr(page, "url", ""))
            for frame in list(getattr(page, "frames", []) or []):
                self.note_visited(getattr(frame, "url", ""))

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
        except Exception:  # noqa: BLE001 - 설치 실패: 이 구간은 방문 origin 전부 기록(fail-closed)
            self.mark_degraded()
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
        """감시를 걷고 기록할 origin → 사건 이름(``network_request``·``password_submit``·``cookie_change``·
        ``secret_handoff``·``watch_unavailable``)."""
        if context is not None:
            self._note_pages(context)
        self.closed = True
        for task in list(self._pending):
            task.cancel()
        for ctx in self._contexts:
            try:
                ctx.remove_listener("request", self._on_request)
            except Exception:  # noqa: BLE001 - 닫힌 컨텍스트
                pass
        out: Dict[str, str] = {}
        for origin in self.posted:
            out.setdefault(origin, "network_request")
        for origin in self.submitted:
            out.setdefault(origin, "password_submit")
        for origin in self.secret_origins:
            out.setdefault(origin, "secret_handoff")
        if context is not None and self.baseline is not None:
            try:
                cookies = list(await context.cookies())
            except Exception:  # noqa: BLE001 - 반납 때 쿠키를 못 읽음: fail-closed
                cookies = []
                self.degraded = True
            now = _snapshot(cookies)
            for cookie in cookies:
                key = (str(cookie.get("name", "")), str(cookie.get("domain", "")), str(cookie.get("path") or "/"))
                digest, http_only = now[key]
                before = self.baseline.get(key)
                new_server = http_only and (before is None or not before[1])
                changed = before is None or before[0] != digest
                if new_server or changed:
                    origin = cookie_origin(cookie)
                    if origin is not None:
                        out.setdefault(origin, "cookie_change")
        if self.degraded:
            for origin in self.visited:
                out.setdefault(origin, "watch_unavailable")
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
