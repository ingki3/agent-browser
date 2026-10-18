"""BrowserMCPServer 의 결과 신호 부착: 관찰 안내·헤드리스 재시도·차단/egress/업스트림/challenge.

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional

from playwright.async_api import Frame
from browser.headless_retry import RetryRequest

from contracts import ActionResult, ActionType, ErrorCode
from interface.server_common import _stderr_line

#: 분리 전과 같은 로거 이름을 쓴다(로그 레코드 불변).
logger = logging.getLogger("interface.mcp_server")


UNOBSERVED_HINT = (
    "이 페이지를 아직 관찰하지 않았습니다 — browser_observe_page 로 요소를 확인한 뒤 element_id 로 고르세요"
)


@dataclass
class _PageObservation:
    context_id: int
    url: str
    observed: bool = False
    hinted: bool = False


class SignalsMixin:
    """BrowserMCPServer 의 결과 신호 부착: 관찰 안내·헤드리스 재시도·차단/egress/업스트림/challenge (WS-48 믹스인 — 상태는 BrowserMCPServer.__init__)."""

    def _observation_state(self) -> Optional[_PageObservation]:
        """Track active tab/context and URL without a browser round trip."""
        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "page", None)
        if page is None:
            return None
        tab_id = str(getattr(ctx, "tab_id", ""))
        url = str(getattr(page, "url", ""))
        state = self._page_observations.get(tab_id)
        if state is None or state.context_id != id(page) or state.url != url:
            state = self._page_observations[tab_id] = _PageObservation(id(page), url)
        top = getattr(ctx, "root_page", None) or page
        # Page events also catch external/same-URL reloads. Frame contexts share
        # their root's listener; unrelated subframe navigation does not reset it.
        if hasattr(top, "on") and top not in self._observation_watches:
            self._observation_watches.add(top)

            def moved(frame: Frame) -> None:
                current = self._page_observations.get(tab_id)
                if current is not None and (frame.parent_frame is None
                                            or current.context_id == id(frame)):
                    current.observed = current.hinted = False
                    current.url = frame.url

            top.on("framenavigated", moved)
            top.on("close", lambda: self._page_observations.pop(tab_id, None))
        return state

    @staticmethod
    def _sparse_extract(result: ActionResult) -> bool:
        """Sparse = >= half blank text rows, or < eight non-whitespace chars.

        Count text only: raw attributes do not turn empty page text into a useful
        extraction. Evaluate before the output cap so truncation cannot fake sparsity.
        """
        items = result.data.get("items", [])
        rows = [items] if isinstance(items, dict) else items if isinstance(items, list) else []
        texts = [str(row.get("text", "") or "") for row in rows if isinstance(row, dict)]
        compact = ["".join(text.split()) for text in texts]
        return not compact or 2 * sum(not text for text in compact) >= len(compact) or sum(
            len(text) for text in compact) < 8

    async def _attach_observation_guidance(
        self, action: ActionType, params: Dict[str, Any], result: ActionResult,
    ) -> None:
        if "document" in result.data:
            return  # PDF is a virtual document; the previous DOM is not its observation.
        state = self._observation_state()
        if state is None:
            return
        moved = action in {ActionType.NAVIGATE, ActionType.GO_BACK, ActionType.RELOAD,
                           ActionType.SWITCH_FRAME} or (
            action is ActionType.TAB_CONTROL and params.get("command") != "list")
        if result.success and moved:
            state.observed = state.hinted = False
        if action is ActionType.OBSERVE_PAGE and result.success:
            state.observed = True
            from perception.page_data import summarize_page_data

            summary = await summarize_page_data(self._dispatcher.ctx.page)
            if summary:
                result.data["page_data"] = summary
        selector_failed = not result.success and result.error_code in {
            ErrorCode.ELEMENT_NOT_FOUND, ErrorCode.ELEMENT_NOT_INTERACTABLE,
            ErrorCode.FRAME_NOT_FOUND, ErrorCode.SHADOW_ROOT_NOT_FOUND,
        }
        # The existing HITL gate refuses ambiguous/unresolved selectors before
        # dispatch. Its decision stays intact; guidance still helps resolve them.
        basis = result.data.get("gate_basis")
        selector_failed = selector_failed or (
            not result.success and result.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
            and isinstance(basis, dict) and basis.get("source") == "unresolved")
        sparse = action is ActionType.EXTRACT and result.success and self._sparse_extract(result)
        if (params.get("selector") or params.get("frame_selector")) and (
                selector_failed or sparse) and not state.observed and not state.hinted:
            if "hint" not in result.data:
                result.data["hint"] = UNOBSERVED_HINT
                state.hinted = True

    @asynccontextmanager
    async def _present_remembered_navigation(
        self, action: ActionType, params: Dict[str, Any],
    ) -> AsyncIterator[RetryRequest | None]:
        """Reuse success only during this navigate and with current eligibility.

        Temporarily ineligible origins retain memory: the per-request checks
        still prevent renamed requests while cookies or REP restrictions exist.
        """
        from security.robots_signal import _origin

        url = params.get("url", "") if action is ActionType.NAVIGATE else ""
        origin = _origin(url)
        page = self._recipe_page()
        if (origin is None or self._headless_retry.state(origin) != "succeeded"
                or self._retry_recipe_boundary.get() or not self.headless_retry
                or not getattr(self._core, "headless", False) or page is None or self._egress is None):
            yield None
            return
        if (await self._robots.allowed(page.context, url, self._egress, self._egress_proxy) is not True
                or not await self._headless_retry.cookies_cleared(page.context, url)):
            yield None
            return
        original = await self._robots._context_user_agent(page.context)
        if (original is None or "HeadlessChrome" not in original
                or self._headless_retry.state(origin) != "succeeded"):
            yield None
            return
        async with self._headless_retry.present(page, url, original, self._robots,
                                                self._egress, self._egress_proxy,
                                                self._retry_tail_guard()) as attempt:
            yield attempt

    def _retry_tail_guard(self) -> Any:
        """WS-52: the settle tail may merge only while no human could have touched the browser.

        Captured before the navigate: any handoff event since then (take/release/request/
        login — hub.version moves), a human holder, a pending request or login job, a
        headed window, or the feature/headless turning off ends the tail without merging.
        """
        from interface.handoff import HOLDER_AGENT

        hub = self.hub
        version = hub.version

        def guard() -> bool:
            window = self._window
            return bool(self.headless_retry and getattr(self._core, "headless", False) and self._started
                        and (window is None or window.state == "headless")
                        and hub.holder == HOLDER_AGENT and not hub.requested and hub.login is None
                        and hub.version == version)

        return guard

    async def _attach_robots(self, result: ActionResult) -> None:
        """navigate·observe_page 결과에 robots.txt 의사 표시 신호(data.robots, WS-41)를 싣는다."""
        from security.robots_signal import attach_robots_signal

        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None) or self._page
        document = result.data.get("document")
        if isinstance(document, dict) and document.get("url") and page is not None and self._egress is not None:
            signal = await self._robots.signal(page.context, document["url"], self._egress, self._egress_proxy)
            if signal:
                result.data["robots"] = signal
        else:
            await attach_robots_signal(self._robots, result, page, self._egress, self._egress_proxy)

    def _attach_remembered_identity(self, remembered: Any, result: ActionResult) -> None:
        """기억된 성공으로 처음부터 일반 Chrome 이름으로 연 navigate 결과에 data.identity 를 싣는다."""
        from browser.headless_retry import FAILED_NOTE, REMEMBERED_NOTE
        from security.robots_signal import _origin

        if (remembered is not None and remembered.document_requested and "document" not in result.data
                and _origin(result.current_url) == remembered.origin):
            identity = {"presented_as": "chrome", "reason": "headless_ua_blocked", "remembered": True,
                        "note": REMEMBERED_NOTE}
            # WS-52 R3: the scope exit adds cookie_write_observer when the observer starts.
            remembered.identity = identity
            status = result.data.get("last_http_status")
            if status == 403:
                self._headless_retry.mark_failed(remembered.origin or "")
                identity["retry_failed"] = True
                identity["note"] = FAILED_NOTE
            elif isinstance(status, int) and 200 <= status < 300 and result.data.get("challenge") is None:
                remembered.succeeded = True  # ledger cookies this navigate caused (on scope exit)
            result.data["identity"] = identity

    async def _retry_headless_navigation(
        self, name: str, arguments: Dict[str, Any], params: Dict[str, Any], result: ActionResult,
    ) -> Optional[ActionResult]:
        """WS-44: short generic 403, confirmed REP Allow, empty-cookie origin, once.

        WS-47: "empty-cookie" means no cookie, or only cookies our earlier
        successful retries caused (exact ledger match). A 403 challenge that is
        not retried gets data.retry_skipped with a fixed reason code + note, so
        the caller does not explain it as a temporary block.
        """
        from browser.headless_retry import FAILED_NOTE, SUCCESS_NOTE, skip_reason
        from security.robots_signal import _origin

        page = self._recipe_page()
        if page is None or self._egress is None or self._retry_recipe_boundary.get():
            return None  # the nested retry call itself is reported by its caller
        status = result.data.get("last_http_status")
        challenge = result.data.get("challenge")
        if (status != 403 or not isinstance(challenge, dict) or not result.success
                or "identity" in result.data):
            return None

        def skip(code: str) -> None:
            result.data["retry_skipped"] = skip_reason(code)
            return None

        if not getattr(self._core, "headless", False):
            return skip("headed")
        if not self.headless_retry:
            return skip("disabled")
        if not (challenge.get("kind") == "blocked" and challenge.get("vendor") == "generic"
                and str(challenge.get("reason") or "").startswith(f"HTTP {status} + 짧은 본문")):
            return skip("vendor_challenge")
        if self._page_status is not None and self._page_status.confirmation_for(page):
            return skip("vendor_challenge")
        origin = _origin(page.url)
        if origin is None or _origin(params.get("url", "")) != origin:
            return None
        allowed = await self._robots.allowed(page.context, page.url, self._egress, self._egress_proxy)
        # Also confirm the original navigate path when it redirected within this origin.
        if (allowed is not True or await self._robots.allowed(
                page.context, params["url"], self._egress, self._egress_proxy) is not True):
            return skip("robots_not_confirmed")
        if self._headless_retry.state(origin) == "failed":
            return skip("already_failed")
        if not await self._headless_retry.cookies_cleared(page.context, page.url):
            # WS-52: say whether an unknown cookie (maybe a login) or only a known value changed.
            result.data["retry_skipped"] = skip_reason(
                "cookies_present", await self._headless_retry.cookie_change(page.context, page.url))
            return None
        if self._headless_retry.state(origin) is not None:
            return None  # remembered success that did not apply to this navigate
        original = await self._robots._context_user_agent(page.context)
        if original is None or "HeadlessChrome" not in original:
            return None
        if self._headless_retry.state(origin) is not None:
            return None  # another caller can reserve the origin while UA discovery awaits
        self._headless_retry.reserve(origin)  # reserve before any await/recursive navigation
        token = self._retry_recipe_boundary.set(True)
        attempt = None
        retry_arguments = dict(arguments)
        retry_arguments["url"] = params["url"].split("#", 1)[0]
        failed = False
        try:
            async with self._headless_retry.present(page, page.url, original, self._robots,
                                                    self._egress, self._egress_proxy,
                                                    self._retry_tail_guard()) as attempt:
                retried = await self.call_tool(name, retry_arguments)
                retry_status = retried.data.get("last_http_status")
                if (attempt.document_requested and "document" not in retried.data
                        and _origin(retried.current_url) == origin):
                    failed = (not retried.success or retried.data.get("challenge") is not None
                              or not isinstance(retry_status, int) or not 200 <= retry_status < 300)
                    # Success commits state + the cookies this navigate caused when present() exits.
                    attempt.succeeded = not failed
        finally:
            self._retry_recipe_boundary.reset(token)
            if attempt is None or not attempt.document_requested:
                self._headless_retry.release(origin)
        if not attempt.document_requested:
            # Preserve genuine gate failures, but a same-document no-op is not a retry.
            return retried if not retried.success else None
        if "document" in retried.data or _origin(retried.current_url) != origin:
            # PDF bytes use native httpx UA; a different origin's final status
            # cannot establish success/failure of the reserved source's retry.
            return retried
        identity = {"presented_as": "chrome", "reason": "headless_ua_blocked", "original_status": 403,
                    "note": SUCCESS_NOTE}
        if failed:
            self._headless_retry.mark_failed(origin)
            identity["retry_failed"] = True
            identity["note"] = FAILED_NOTE
        elif attempt.observing:
            identity["cookie_write_observer"] = True  # WS-52 R3: disclosed, never hidden
        retried.data["identity"] = identity
        return retried

    async def _attach_block_hint(self, result: ActionResult) -> None:
        """Explain server denial separately from REP, and report rate-limit timing."""
        from actions.verification import safe_page_text

        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None) or self._page
        if page is None:
            return
        status = result.data.get("last_http_status")
        if self._page_status is not None and self._page_status.judge_status_for(page) != status:
            return  # stale SPA response status is not this document's status
        hint = ""
        if status == 429:
            retry_after = self._page_status.retry_after_for(page) if self._page_status is not None else ""
            hint = "속도 제한 — 잠시 뒤에 다시 요청하세요."
            if retry_after:
                # Keep web-derived header text in a separately scanned field:
                # IPI intentionally skips the server's fixed hint prose.
                result.data["retry_after"] = safe_page_text(retry_after, 128)
                hint += " Retry-After: " + result.data["retry_after"]
        else:
            challenge = result.data.get("challenge")
            short_denial = (isinstance(challenge, dict)
                            and str(challenge.get("reason") or "").startswith(f"HTTP {status} + 짧은 본문"))
            if (self._egress is not None and (status == 401 or status == 403 and short_denial)
                    and await self._robots.allowed(page.context, page.url, self._egress, self._egress_proxy) is True):
                hint = "robots 때문이 아님 — 사이트 서버가 이 브라우저를 막음(헤드리스 이름 차단 가능)"
                if status == 401:
                    hint += "; 401은 로그인·인증이 필요하며 이름 재시도 대상이 아닙니다."
        if hint and hint not in str(result.data.get("hint") or ""):
            result.data["hint"] = (str(result.data.get("hint") or "") + " " + hint).strip()

    def _handle_href(self, params: Dict[str, Any]) -> Optional[str]:
        """element_id 대상 핸들의 href(링크가 아니거나 핸들이 없으면 None)."""
        eid = params.get("element_id")
        if not eid or self._engine is None:
            return None
        try:
            handle = self._engine.get_handle(eid)
        except Exception:  # noqa: BLE001 - 진단용 보조 정보일 뿐
            return None
        href = getattr(handle, "href", None) if handle is not None else None
        return href if isinstance(href, str) and href else None

    def _attach_egress_block(
        self, action: ActionType, params: Dict[str, Any], result: ActionResult, before: int,
        *, target_href: Optional[str] = None,
    ) -> ActionResult:
        """이번 호출 중 Egress 가 막은 이동을 에이전트에게 알린다 (WS-29b).

        활성 탭의 문서 이동(요청 주소·리다이렉트 홉·현재 주소의 호스트)이 막혔으면
        data.egress 에 이유(code=egress_blocked, host, category, reason, open_with)를 싣는다.
        navigate 가 막혔으면 실패(E_INVALID_URL)로 돌려준다 — 막힌 문서를 성공으로 보고하지
        않는다. 하위 요청(이미지·비콘 등) 차단은 싣지 않는다(문서 이동만).
        클릭한 링크의 호스트(target_href, WS-38 R1 NB-3)도 이동 대상으로 본다 — 막히면 탭이
        chrome-error:// 로 바뀌어 현재 주소로는 호스트를 알 수 없기 때문이다.
        """
        if self._egress is None:
            return result
        new = self._egress.blocked_since(before)
        if not new:
            return result
        from urllib.parse import urlparse

        hosts = set()
        for raw in (params.get("url"), getattr(self._page, "url", None), result.current_url, target_href):
            try:
                h = urlparse(raw or "").hostname
            except ValueError:
                h = None
            if h:
                hosts.add(h.lower())
        hit = next((d for d in reversed(new) if d.host and d.host.lower() in hosts), None)
        if hit is None and action is ActionType.NAVIGATE and not result.success:
            hit = new[-1]
        if hit is None:
            return result
        info = hit.to_agent()
        if action is ActionType.NAVIGATE:
            return self._error_result(
                action,
                ErrorCode.INVALID_URL,
                f"egress_blocked: {info['host']} ({info['category']})",
                data={"egress": info, "url": getattr(self._page, "url", "")},
            )
        result.data["egress"] = info
        return result

    def _attach_upstream_failure(
        self, action: ActionType, params: Dict[str, Any], result: ActionResult, before: int
    ) -> ActionResult:
        """프록시가 업스트림에 닿지 못해 스스로 만든 502 문서를 이동 실패로 알린다 (WS-29b R1).

        프록시 없이는 브라우저가 이름 해석·접속 실패로 이동 자체를 실패시킨다(base 실측:
        E_NAVIGATE_TIMEOUT). 프록시를 거치면 그 실패가 502 문서로 바뀌어 '열렸다'로 보였다.
        이번 호출 중 프록시가 기록한 업스트림 실패(호스트·포트)가 이동 대상과 같고, 탭의 메인
        문서가 502 일 때만 해당한다 — 사이트가 실제로 준 502 는 프록시 기록이 없어 그대로 둔다.
        이미 실패한 이동(https CONNECT 실패 등)에는 이유만 싣는다.
        """
        if self._egress is None or self._dispatcher is None:
            return result
        new = self._egress.upstream_failures_since(before)
        if not new:
            return result
        if result.data.get("egress") is not None:
            return result  # 차단 이유가 먼저다
        from urllib.parse import urlsplit

        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "page", None) or self._page
        targets = set()
        for raw in (params.get("url") if action is ActionType.NAVIGATE else None,
                    getattr(page, "url", None), result.current_url):
            try:
                parts = urlsplit(raw or "")
                host = (parts.hostname or "").lower()
                port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())
            except ValueError:
                continue
            if host:
                targets.add((host, port))
        hit = next((f for f in reversed(new)
                    if (f.host.lower().strip("[]"), f.port) in targets), None)
        if hit is None:
            return result
        if result.success:
            status: Optional[int] = None
            try:
                if self._page_status is not None:
                    status = self._page_status.status_for(page)
            except Exception:  # noqa: BLE001
                status = None
            if status != 502:
                return result  # 메인 문서는 정상 — 하위 요청 실패일 뿐
        info = hit.to_agent()
        if action is ActionType.NAVIGATE:
            return self._error_result(
                action,
                ErrorCode.NAVIGATE_TIMEOUT,
                f"이동 실패: {info['code']}: {info['host']}",
                data={"egress": info, "url": getattr(page, "url", "")},
            )
        result.data["egress"] = info
        return result

    async def _attach_challenge(self, result: ActionResult) -> None:
        """활성 탭 화면의 차단·캡차 판정을 result.data 에 병합한다 (WS-26).

        판정만 한다 — 풀거나 우회하지 않는다. 판정 실패는 challenge=None 으로 둔다.
        기존 data 키는 덮어쓰지 않는다.

        판정 페이지는 root_page(프레임 진입 전 최상위) → page → self._page 순이다 —
        최상위가 캡차인데 그 안 정상 iframe 에 들어가 있어도 캡차를 놓치지 않는다.
        WS-26b: 상태는 **그 페이지가** 받은 마지막 메인 문서 응답이고, 판정에는 지금
        URL 이 그 응답 URL 과 같을 때만 넘긴다(pushState 로 옮긴 SPA 화면 오판 방지).
        """
        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None)
        if page is None:
            page = self._page
        tracker = self._page_status
        last_status: Optional[int] = None
        judge_status: Optional[int] = None
        try:
            if tracker is not None:
                last_status = tracker.status_for(page)
                judge_status = tracker.judge_status_for(page)
            else:
                last_status = self._http_status.get("last_http_status")
                judge_status = last_status
        except Exception:  # noqa: BLE001 - 상태 조회 실패로 결과를 망가뜨리지 않는다
            logger.warning("문서 상태 조회 실패 — null 로 둔다", exc_info=True)
        challenge: Optional[Dict[str, str]] = None
        try:
            from browser import challenge as challenge_mod

            found = await challenge_mod.detect_challenge(page, last_status=judge_status)
            if found.detected:
                challenge = {
                    "kind": found.kind.value,
                    "vendor": found.vendor,
                    "reason": found.reason,
                }
        except Exception:  # noqa: BLE001 - 판정 실패로 결과를 망가뜨리지 않는다
            logger.warning("차단 판정 실패 — challenge=None 으로 둔다", exc_info=True)
        result.data.setdefault("challenge", challenge)
        result.data.setdefault("last_http_status", last_status)
        if self._window is not None and challenge is not None:
            # WS-34 D3: 창에서 해결했던 사이트에서 headless 복귀 뒤 다시 차단/캡차 → 알리고, 다음
            # 조작권 요청 때 창을 서버 수명 동안 유지(sticky).
            from interface.on_demand import STICKY_PENDING_HINT

            pending = self._window.note_challenge(str(getattr(page, "url", "") or ""), challenge)
            if pending is not None:
                self._sync_window()
                _stderr_line(f"agent-browser serve: {pending['domain']} 에서 창으로 해결한 뒤 "
                             "headless 에서 다시 차단/캡차 — 다음 조작권 요청 때 창을 열고 유지합니다")
            if self._window.sticky_pending is not None:
                result.data.setdefault("window", dict(self._window.info(), hint=STICKY_PENDING_HINT))
