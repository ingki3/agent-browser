"""Playwright CDP 코어 및 BrowserContext 풀 (PRD §5.2, §3.4).

`contracts.BrowserCoreProtocol` 구현체.

책임:
* 프로파일별 독립 `BrowserContext` 격리 (세션/쿠키 유출 차단)
* 탭 수명주기 관리 및 탭별 태스크 격리
* 리소스 상한 강제 (탭 10개 / 컨텍스트 5개)
* 암호화된 `storageState` 주입 및 회수
* Direct CDP 세션 제공

동시성 주의: Playwright API는 스레드 안전하지 않다. 본 코어는 단일
asyncio 이벤트 루프 내에서만 사용해야 하며, 컨텍스트별 `asyncio.Lock`으로
동일 컨텍스트에 대한 동시 조작을 직렬화한다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from contracts import ErrorCode, thresholds

from browser.session_store import SessionStore

logger = logging.getLogger(__name__)


class BrowserCoreError(RuntimeError):
    """브라우저 코어 처리 실패. 표준 에러 코드를 동반한다."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ManagedTab:
    """코어가 추적하는 단일 탭."""

    tab_id: str
    page: Any  # playwright.async_api.Page
    profile_name: str


@dataclass
class ManagedContext:
    """프로파일 단위로 격리된 BrowserContext."""

    profile_name: str
    context: Any  # playwright.async_api.BrowserContext
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tabs: Dict[str, ManagedTab] = field(default_factory=dict)


class BrowserCore:
    """Playwright 기반 브라우저 수명주기 관리자."""

    def __init__(
        self,
        *,
        headless: bool = True,
        session_store: Optional[SessionStore] = None,
        max_contexts: int = thresholds.MAX_ACTIVE_CONTEXTS,
        max_tabs: int = thresholds.MAX_TABS_PER_SESSION,
        viewport_width: int = thresholds.VIEWPORT_WIDTH,
        viewport_height: int = thresholds.VIEWPORT_HEIGHT,
    ) -> None:
        self.headless = headless
        self.session_store = session_store or SessionStore()
        self.max_contexts = max_contexts
        self.max_tabs = max_tabs
        self.viewport = {"width": viewport_width, "height": viewport_height}

        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: Dict[str, ManagedContext] = {}
        self._tab_index: Dict[str, ManagedTab] = {}
        self._active_tab_id: Optional[str] = None
        self._tab_counter = 0

    # -- 수명주기 -----------------------------------------------------------

    async def start(self) -> "BrowserCore":
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        return self

    async def close(self) -> None:
        for managed in list(self._contexts.values()):
            try:
                await managed.context.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("컨텍스트 종료 실패 (%s): %s", managed.profile_name, exc)
        self._contexts.clear()
        self._tab_index.clear()
        self._active_tab_id = None

        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> "BrowserCore":
        return await self.start()

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        await self.close()

    # -- 컨텍스트 (BrowserCoreProtocol) --------------------------------------

    async def new_context(self, profile_name: str) -> Any:
        """프로파일 전용 격리 컨텍스트를 생성한다.

        동일 프로파일을 재요청하면 기존 컨텍스트를 반환한다(중복 생성 방지).
        """
        if self._browser is None:
            raise BrowserCoreError(
                ErrorCode.PAGE_CRASHED, "브라우저가 시작되지 않았습니다. start()를 먼저 호출하십시오."
            )

        existing = self._contexts.get(profile_name)
        if existing:
            return existing.context

        if len(self._contexts) >= self.max_contexts:
            raise BrowserCoreError(
                ErrorCode.TAB_LIMIT_EXCEEDED,
                f"활성 컨텍스트 상한({self.max_contexts})을 초과했습니다.",
            )

        options: Dict[str, Any] = {"viewport": dict(self.viewport)}
        context = await self._browser.new_context(**options)
        self._contexts[profile_name] = ManagedContext(
            profile_name=profile_name, context=context
        )
        logger.debug("컨텍스트 생성: %s", profile_name)
        return context

    async def new_context_with_session(
        self, profile_name: str, passphrase: str
    ) -> Any:
        """저장된 암호화 storageState를 주입해 컨텍스트를 생성한다."""
        if self._browser is None:
            raise BrowserCoreError(ErrorCode.PAGE_CRASHED, "브라우저가 시작되지 않았습니다.")
        if len(self._contexts) >= self.max_contexts:
            raise BrowserCoreError(
                ErrorCode.TAB_LIMIT_EXCEEDED,
                f"활성 컨텍스트 상한({self.max_contexts})을 초과했습니다.",
            )

        storage_state = self.session_store.load(profile_name, passphrase)
        context = await self._browser.new_context(
            viewport=dict(self.viewport), storage_state=storage_state
        )
        self._contexts[profile_name] = ManagedContext(
            profile_name=profile_name, context=context
        )
        return context

    async def save_session(self, profile_name: str, passphrase: str) -> str:
        """현재 컨텍스트의 storageState를 암호화 저장한다."""
        managed = self._contexts.get(profile_name)
        if managed is None:
            raise BrowserCoreError(
                ErrorCode.TAB_NOT_FOUND, f"컨텍스트를 찾을 수 없습니다: {profile_name}"
            )
        state = await managed.context.storage_state()
        path = self.session_store.save(profile_name, state, passphrase)
        return str(path)

    # -- 탭 -----------------------------------------------------------------

    async def new_tab(self, profile_name: str, url: Optional[str] = None) -> ManagedTab:
        managed = self._contexts.get(profile_name)
        if managed is None:
            await self.new_context(profile_name)
            managed = self._contexts[profile_name]

        if len(self._tab_index) >= self.max_tabs:
            raise BrowserCoreError(
                ErrorCode.TAB_LIMIT_EXCEEDED,
                f"세션 탭 상한({self.max_tabs})을 초과했습니다.",
            )

        async with managed.lock:
            page = await managed.context.new_page()

        self._tab_counter += 1
        tab_id = f"tab-{self._tab_counter}"
        tab = ManagedTab(tab_id=tab_id, page=page, profile_name=profile_name)
        managed.tabs[tab_id] = tab
        self._tab_index[tab_id] = tab
        self._active_tab_id = tab_id

        if url:
            await page.goto(url, wait_until="domcontentloaded")
        return tab

    async def get_active_page(self, tab_id: Optional[str] = None) -> Any:
        """활성 탭(또는 지정 탭)의 Page를 반환한다 (BrowserCoreProtocol)."""
        target_id = tab_id or self._active_tab_id
        if target_id is None:
            raise BrowserCoreError(ErrorCode.TAB_NOT_FOUND, "활성 탭이 없습니다.")
        tab = self._tab_index.get(target_id)
        if tab is None:
            raise BrowserCoreError(
                ErrorCode.TAB_NOT_FOUND, f"탭을 찾을 수 없습니다: {target_id}"
            )
        return tab.page

    async def close_tab(self, tab_id: str) -> None:
        tab = self._tab_index.pop(tab_id, None)
        if tab is None:
            raise BrowserCoreError(
                ErrorCode.TAB_NOT_FOUND, f"탭을 찾을 수 없습니다: {tab_id}"
            )
        managed = self._contexts.get(tab.profile_name)
        if managed:
            managed.tabs.pop(tab_id, None)
        await tab.page.close()
        if self._active_tab_id == tab_id:
            self._active_tab_id = next(iter(self._tab_index), None)

    def switch_tab(self, tab_id: str) -> None:
        if tab_id not in self._tab_index:
            raise BrowserCoreError(
                ErrorCode.TAB_NOT_FOUND, f"탭을 찾을 수 없습니다: {tab_id}"
            )
        self._active_tab_id = tab_id

    def list_tabs(self) -> Dict[str, str]:
        """tab_id → profile_name 매핑을 반환한다."""
        return {tid: tab.profile_name for tid, tab in self._tab_index.items()}

    # -- CDP ----------------------------------------------------------------

    async def new_cdp_session(self, tab_id: Optional[str] = None) -> Any:
        """지정 탭에 대한 Direct CDP 세션을 연다."""
        target_id = tab_id or self._active_tab_id
        if target_id is None:
            raise BrowserCoreError(ErrorCode.TAB_NOT_FOUND, "활성 탭이 없습니다.")
        tab = self._tab_index.get(target_id)
        if tab is None:
            raise BrowserCoreError(
                ErrorCode.TAB_NOT_FOUND, f"탭을 찾을 수 없습니다: {target_id}"
            )
        managed = self._contexts[tab.profile_name]
        return await managed.context.new_cdp_session(tab.page)

    # -- 상태 조회 -----------------------------------------------------------

    @property
    def context_count(self) -> int:
        return len(self._contexts)

    @property
    def tab_count(self) -> int:
        return len(self._tab_index)

    @property
    def active_tab_id(self) -> Optional[str]:
        return self._active_tab_id

    def context_for(self, profile_name: str) -> Optional[Any]:
        managed = self._contexts.get(profile_name)
        return managed.context if managed else None
