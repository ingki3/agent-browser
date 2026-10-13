"""BrowserMCPServer 의 수명주기·세션 연결·영속 프로필·닫힌 탭 복구.

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import logging
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from interface.server_common import _stderr_line

#: 분리 전과 같은 로거 이름을 쓴다(로그 레코드 불변).
logger = logging.getLogger("interface.mcp_server")


class LifecycleMixin:
    """BrowserMCPServer 의 수명주기·세션 연결·영속 프로필·닫힌 탭 복구 (WS-48 믹스인 — 상태는 BrowserMCPServer.__init__)."""

    async def start(self) -> None:
        """브라우저 세션과 파이프라인을 초기화한다."""
        if self._started:
            return

        from browser import BrowserCore
        from security.egress_runtime import EgressRuntime

        # WS-32: 영속 프로필이면 먼저 잠근다 — 다른 serve 가 쓰는 중이면 아무것도 띄우지 않고 거부.
        persistent = self.acquire_profile()
        if self.profile is not None and persistent is not None and self._headless_retry.path is None:
            # WS-47: --profile 이면 재시도 기억·쿠키 장부도 프로필 폴더에(0600). 없으면 메모리만.
            from browser.headless_retry import LEDGER_FILE

            self._headless_retry.load(Path(persistent) / LEDGER_FILE)
        # WS-29b: 브라우저보다 검증 프록시를 먼저 띄운다 — 브라우저는 프록시로만 나간다.
        runtime = EgressRuntime(
            allowed_domains=self.allowed_domains,
            allow_private_network=self.allow_private_network,
            block_loopback=self.block_loopback,
            tokenless=self.browser_mode == "user-chrome",
        )
        try:
            await runtime.start()
        except BaseException:
            self.release_profile()
            raise
        self._egress_runtime = runtime
        try:
            core_kw: Dict[str, Any] = {}
            if persistent is not None:
                core_kw["persistent_profile"] = persistent
            # WS-34: on-demand 는 headless 로 시작하는 영속 컨텍스트 — 창은 전환 때 다시 열어 띄운다.
            on_demand_mode = self._window is not None
            core = BrowserCore(
                headless=True if on_demand_mode else self.headless,
                browser_mode="headless" if on_demand_mode else self.browser_mode,
                chrome_profile=self.chrome_profile,
                keep_open=self.keep_open,
                egress=runtime,
                **core_kw,
            )
            self._core = await core.start()
        except BaseException:
            await self._close_egress()
            self.release_profile()
            raise
        try:
            await self._init_session()
        except BaseException:
            # 시작 도중 실패하면 띄운 브라우저(user-chrome 이면 Chrome 프로세스)를 남기지 않는다.
            # keep_open 은 정상 종료 때만 존중한다 — 시작 실패면 옵션과 무관하게 닫는다(README).
            self._core.keep_open = False
            try:
                await self._core.close()
            except Exception:  # noqa: BLE001
                logger.warning("시작 실패 뒤 브라우저 정리 실패", exc_info=True)
            self._core = None
            await self._close_egress()
            self.release_profile()
            raise
        self._started = True
        self.open_handoff()
        self._sync_window()
        logger.info(
            "MCP 브라우저 세션 시작 (mode=%s, browser=%s)", self.mode.value, self.browser_mode
        )

    def acquire_profile(self) -> Optional[Path]:
        """--profile 이면 프로필 폴더를 만들고(0700) 이 서버 id 로 잠근다. 폴더 경로를 돌려준다.

        이미 잡았으면 그대로. 다른 serve(또는 Chromium)가 쓰는 중이면 ProfileInUseError —
        사람이 읽을 이유(어느 서버 id)가 담긴다. --profile 이 없으면 None(기존 동작).
        WS-34: on-demand 를 --profile 없이 띄우면 서버 전용 임시 프로필(0700, 종료 때 삭제)을 쓴다 —
        그 전에 비정상 종료로 남은 임시 프로필(잠금 풀린 것)을 지운다.
        """
        if self.profile is None:
            if self._window is None:
                return None
            if self._ephemeral_lock is not None and self._ephemeral_lock.held:
                return self._ephemeral_lock.path
            from browser import serve_profile

            serve_profile.cleanup_ephemeral()
            self._ephemeral_lock = serve_profile.acquire_ephemeral(server_id=self.hub.server_id)
            return self._ephemeral_lock.path
        if self._profile_lock is not None and self._profile_lock.held:
            return self._profile_lock.path
        from browser import serve_profile

        # R1 NB-6: --profile 서버도 비정상 종료로 남은 임시 프로필(잠금 풀린 것만)을 지운다.
        serve_profile.cleanup_ephemeral()
        self._profile_lock = serve_profile.acquire(self.profile, server_id=self.hub.server_id)
        return self._profile_lock.path

    def release_profile(self) -> None:
        lock, self._profile_lock = self._profile_lock, None
        if lock is not None:
            lock.release()
        eph, self._ephemeral_lock = self._ephemeral_lock, None
        if eph is not None:
            from browser import serve_profile

            serve_profile.remove_ephemeral(eph)

    def _profile_info(self) -> Optional[Dict[str, Any]]:
        """에이전트에게 보이는 프로필 상태 — 이름만(경로는 넣지 않는다)."""
        if self.profile is None:
            return None
        return {"name": self.profile, "persistent": True}

    async def _recover_closed_tab_serialized(self) -> None:
        """닫힌 탭 복구를 Future 로 감싸 진행 중임을 알린다(WS-37 반납 알림 경쟁 조건).

        Future 는 첫 await 전에 만든다 — hub.poll() 이 'released' 를 돌려준 같은 동기 구간 안이라,
        hub.version 이 바뀐 것을 본 다른 태스크는 반드시 이 Future 도 본다.
        """
        fut: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        self._release_recovery = fut
        try:
            await self._recover_closed_tab()
        finally:
            if not fut.done():
                fut.set_result(None)
            if self._release_recovery is fut:
                self._release_recovery = None

    async def _recover_closed_tab(self) -> None:
        """조작권 반납 때 에이전트 활성 탭이 살아 있는지 확인(NB-3). 사람이 닫았으면 남은 탭(없으면
        새 탭)으로 바꾸고 알린다 — 닫힌 탭을 관찰해 PAGE_CRASHED 가 나지 않게."""
        core, dispatcher = self._core, self._dispatcher
        ctx = getattr(dispatcher, "ctx", None)
        if core is None or ctx is None or not hasattr(core, "get_tab"):
            return
        closed_id = getattr(ctx, "tab_id", None)
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None)
        alive = core.get_tab(closed_id) is not None
        try:
            alive = alive and not page.is_closed()
        except Exception:  # noqa: BLE001
            pass
        if alive:
            return
        try:
            tabs = core.tabs()
            tab = tabs[0] if tabs else await core.new_tab("mcp-session")
            core.set_active_tab(tab.tab_id)
            dispatcher._set_active_page(tab.page, tab.tab_id)
            ctx.cdp = await core.new_cdp_session(tab.tab_id)
            if self._page is None or self._page.is_closed():
                self._page = tab.page
                self._cdp = ctx.cdp
            if self._engine is not None:
                self._engine.bump_epoch("tab_closed_by_human")
        except Exception:  # noqa: BLE001
            logger.warning("사람이 닫은 탭 복구 실패", exc_info=True)
            return
        self._tab_notice = {
            "closed_tab_id": closed_id,
            "active_tab_id": tab.tab_id,
            "hint": ("사람이 에이전트가 쓰던 탭을 닫았습니다 — 남은 탭으로 바꿨습니다. "
                     "browser_tab_control(command=\"list\") 로 탭을 확인하고 다시 관찰하세요."),
        }

    async def _init_session(self) -> None:
        """컨텍스트·첫 탭·디스패처·Egress 가드·HITL 을 준비한다(세 방식 공통)."""
        from perception import PerceptionEngine
        from security import HITLGate

        self._engine = PerceptionEngine()
        await self._wire_session()
        self._hitl = HITLGate(
            mode=self.mode, pre_approved_actions=self.pre_approved_actions
        )

    async def _wire_session(self, restore: Optional[List[Dict[str, Any]]] = None,
                            cookies: Optional[List[Dict[str, Any]]] = None) -> tuple:
        """열린 브라우저에 서버 연결을 단다 — 처음 시작과 창 전환 뒤(WS-34)가 **같은 경로**.

        컨텍스트 채택 → 문서 상태 추적(PageDocumentStatus) → 첫 탭(about:blank)·CDP·디스패처 → Egress
        route 가드(어떤 페이지 이동보다 먼저) → (전환이면) 탭 복원. 검증 프록시는 브라우저 실행 인자
        (BrowserCore._launch_kwargs)로 이미 묶였다.
        restore(창 전환) 가 있으면 그 탭 URL 들을 다시 연다(활성 탭 먼저, http(s) 만).
        반환: (탭 복원 내역, 건너뛴 탭 수).
        """
        from actions import ActionDispatcher, DispatchContext
        from browser.doc_status import PageDocumentStatus

        await self._core.new_context("mcp-session")
        context = self._core.context_for("mcp-session")
        # 새 탭·팝업 문서 응답도 잡도록 context 단위로 달되, 응답을 온 탭에 묶는다
        # (WS-26b: 팝업 403 이 원래 탭 판정에 새지 않게).
        self._page_status = PageDocumentStatus()
        self._page_status.attach(context)
        tab = await self._core.new_tab("mcp-session")
        self._page = tab.page
        self._cdp = await self._core.new_cdp_session(tab.tab_id)

        self._dispatcher = ActionDispatcher(
            DispatchContext(
                page=self._page,
                engine=self._engine,
                cdp=self._cdp,
                tab_id=tab.tab_id,
                core=self._core,  # tab_control이 탭 수명주기에 접근하려면 필요
                secrets=self.secrets,  # 자격증명 플레이스홀더 해석 (PRD 5.3)
                som_enabled=self.som_enabled,  # Tier-2 SoM 게이트 (PRD §8-2)
                nav_settle=self.nav_settle,  # 이동 대기 스위치 (WS-28)
            )
        )
        # WS-29b: 루프백만 기본 허용(이전 allow_loopback=True 는 사설 대역 전체를 열었다).
        # 같은 가드를 프록시(리다이렉트 홉·WebSocket·재바인딩)와 route(조기 차단)가 공유한다.
        # WS-34: 어떤 페이지 이동(탭 복원 포함)보다 먼저 단다 — 다시 연 브라우저도 처음부터 가드 안.
        self._egress = self._egress_runtime.guard
        await self._egress.install(context)
        if self._window is not None:
            self._watch_context_close(context)
        if cookies:
            try:
                await context.add_cookies(cookies)
            except Exception:  # noqa: BLE001 - 세션 쿠키를 못 넣어도 전환은 계속(영속 쿠키는 남음)
                logger.warning("세션 쿠키 복원 실패", exc_info=True)
        if restore is None:
            return [], 0
        return await self._restore_tabs(tab, restore)

    async def _restore_tabs(self, first: Any, snapshot: List[Dict[str, Any]]) -> tuple:
        """창 전환 뒤 탭 URL 복원(WS-34). 활성 탭을 첫 탭에 먼저, 나머지는 새 탭에. about:blank·
        chrome:// 등(http(s) 아님)은 건너뛴다. 이동 실패는 그 탭만 restored=false(이유)로 알린다."""
        from browser.serve_profile import safe_reason
        from interface import on_demand
        from urllib.parse import urlsplit

        order = sorted(snapshot, key=lambda t: not t.get("active"))
        tabs_info: List[Dict[str, Any]] = []
        skipped = 0
        for item in order:
            url = str(item.get("url") or "")
            if not on_demand.restorable_url(url):
                skipped += 1
                continue
            tab = first if not tabs_info else await self._core.new_tab("mcp-session")
            entry: Dict[str, Any] = {"tab_id": tab.tab_id, "was": item.get("tab_id"), "url": url,
                                     "active": not tabs_info, "restored": False}
            egress = self._egress
            up_before = egress.upstream_total if egress is not None else 0
            try:
                resp = await tab.page.goto(url, wait_until="domcontentloaded",
                                           timeout=on_demand.RESTORE_NAV_TIMEOUT_MS)
                status = getattr(resp, "status", None)
                if status is not None:
                    entry["http_status"] = status
                fails = egress.upstream_failures_since(up_before) if egress is not None else []
                parts = urlsplit(url)
                key = ((parts.hostname or "").lower(),
                       parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower()))
                fails = [f for f in fails if (f.host.lower().strip("[]"), f.port) == key]
                if fails:
                    # 프록시가 업스트림에 닿지 못해 만든 502 문서 — 복원한 것이 아니다(WS-29b R1 과 같은 판정).
                    entry["error"] = f"업스트림 접속 실패({fails[-1].to_agent().get('code')})"
                else:
                    entry["restored"] = True
            except Exception as exc:  # noqa: BLE001 - 한 탭 실패로 전환 전체를 실패시키지 않는다
                entry["error"] = safe_reason(exc)
            tabs_info.append(entry)
        self._core.set_active_tab(first.tab_id)
        return tabs_info, skipped

    async def _session_cookies(self) -> List[Dict[str, Any]]:
        ctx = self._core.context_for("mcp-session") if self._core is not None else None
        if ctx is None or self._ctx_closed:
            return []
        try:
            cookies = await ctx.cookies()
        except Exception:  # noqa: BLE001 - 창이 이미 닫힘 등 — 영속 쿠키는 폴더에 남는다
            return []
        return [c for c in cookies if float(c.get("expires", -1) or -1) < 0]

    async def _fail_closed(self, reason: str, snapshot: List[Dict[str, Any]]) -> None:
        """전환 실패: 보호가 덜 걸렸을 수 있는 브라우저를 남기지 않는다(닫음). state=failed."""
        w = self._window
        assert w is not None
        w.state = "failed"
        w.error = reason
        if snapshot:
            w.last_tabs = list(snapshot)
        self._banner = None
        try:
            await self._core.close_persistent()
        except BaseException:  # noqa: BLE001
            logger.warning("전환 실패 뒤 브라우저 닫기 실패", exc_info=True)
        self._sync_window()
        _stderr_line(f"agent-browser serve: 창 전환 실패 — 브라우저를 닫았습니다(다음 도구 호출 때 "
                     f"headless 로 다시 엽니다): {reason}")

    def _watch_context_close(self, context: Any) -> None:
        on = getattr(context, "on", None)
        if not callable(on):
            return

        def _closed(*_a: Any) -> None:
            if self._core is not None and self._core.context_for("mcp-session") is context:
                self._ctx_closed = True

        on("close", _closed)

    async def close(self) -> None:
        await self._robots.close()  # WS-41: join background requests before closing their context
        if self._recipes is not None:
            self._recipes.close()  # 모아 둔 실행 통계 마지막 쓰기(WS-38)
        task, self._hub_task = self._hub_task, None
        if task is not None:
            task.cancel()
        # WS-34: 진행 중인 창 전환을 멈추고(전환은 취소되면 스스로 브라우저를 닫는다) 코드 창을 닫는다.
        pending = [t for t in self._window_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5.0)
        if self._code_window is not None:
            try:
                await self._code_window.close()
            except Exception:  # noqa: BLE001
                logger.warning("승인 코드 창 정리 실패", exc_info=True)
        try:
            if self._core is not None:
                await self._core.close()
            await self._close_egress()
        finally:
            # 브라우저(영속 컨텍스트)를 닫은 뒤에 잠금을 푼다 — 닫히기 전에 다른 serve 가 잡지 않게.
            self.release_profile()
            self.hub.close()  # 상태 디렉터리 정리(사람 CLI 가 죽은 서버를 고르지 않게)
            self._started = False

    async def _close_egress(self) -> None:
        runtime, self._egress_runtime = self._egress_runtime, None
        if runtime is not None:
            try:
                await runtime.close()
            except Exception:  # noqa: BLE001
                logger.warning("Egress 프록시 정리 실패", exc_info=True)

    @property
    def _egress_proxy(self) -> Any:
        return self._egress_runtime.proxy if self._egress_runtime is not None else None

    async def __aenter__(self) -> "BrowserMCPServer":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        await self.close()
