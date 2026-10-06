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
from pathlib import Path
from typing import Any, Dict, List, Optional

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


#: 브라우저 방식 (WS-27). serve --browser 와 같은 이름.
BROWSER_MODES = ("headless", "human", "user-chrome")


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
        human_like: bool = False,
        browser_mode: str = "headless",
        chrome_profile: Optional[Path] = None,
        keep_open: bool = False,
        egress: Any = None,
        persistent_profile: Optional[Path] = None,
    ) -> None:
        """
        persistent_profile (WS-32): 영속 프로필 폴더(serve --profile, browser.serve_profile).
        주면 chromium.launch 대신 launch_persistent_context(그 폴더) 로 연다 — 쿠키·로컬
        저장소·IndexedDB 가 폴더에 남아 다음 실행에서도 로그인이 유지된다. launch 인자(egress
        프록시·QUIC 끔·WebRTC 정책)와 context 옵션(viewport/locale)은 기존과 같은 것을 한 번에
        넘긴다. 컨텍스트는 1개(영속 컨텍스트 채택), 저장 세션 주입은 지원하지 않는다.
        open_persistent()/close_persistent() 로 같은 프로필을 닫고 다시 열 수 있다(WS-34 기반).
        user-chrome 과는 함께 쓸 수 없다(그 방식은 이미 전용 Chrome 프로필을 쓴다).

        egress (WS-29b): security.egress_runtime.EgressRuntime(시작된 것). 주면 브라우저를
        검증 프록시에 묶어 띄운다(Playwright Chromium: launch(proxy=, args=), user-chrome:
        Chrome 명령줄 인자). None 이면 기존 동작(프록시 없음).

        human_like=True 이면 사람이 쓰는 브라우저와 같은 기본값을 쓴다(위장 없음):
          - viewport 고정 대신 no_viewport (창 크기 = 실제 창, screen 위장 없음)
          - locale="ko-KR" (Accept-Language 헤더와 navigator.languages 채움)
        UA 변경·navigator.webdriver 숨기기 같은 위장은 하지 않는다.
        headless 여부는 바꾸지 않는다 — 창 보이는 브라우저가 필요하면 호출자가
        headless=False 를 함께 줘야 한다.

        browser_mode (WS-27):
          - "headless": 기존 동작(headless/human_like 인자 그대로).
          - "human": headless=False + human_like=True (창 보임, 위장 없음).
          - "user-chrome": 설치된 Chrome 을 자동화 플래그 없이 전용 프로필
            (chrome_profile, 기본 user_chrome.DEFAULT_PROFILE_DIR)로 띄워
            connect_over_cdp 로 붙고, 그 기본 컨텍스트를 채택한다(쿠키·로그인 유지).
            컨텍스트는 1개만, 저장 세션 주입은 지원하지 않는다. keep_open=True 면
            close() 때 띄운 Chrome 을 남긴다.
        """
        if browser_mode not in BROWSER_MODES:
            raise ValueError(
                f"알 수 없는 브라우저 방식: {browser_mode!r} (가능: {', '.join(BROWSER_MODES)})"
            )
        if browser_mode == "human":
            headless, human_like = False, True
        if persistent_profile is not None and browser_mode == "user-chrome":
            raise ValueError(
                "영속 프로필(--profile)은 user-chrome 방식과 함께 쓸 수 없습니다 — user-chrome 은 "
                "이미 전용 Chrome 프로필(--chrome-profile)을 씁니다."
            )
        self.persistent_profile = Path(persistent_profile) if persistent_profile else None
        self.browser_mode = browser_mode
        self.chrome_profile = chrome_profile
        self.keep_open = keep_open
        self.egress = egress
        self.headless = headless
        self.human_like = human_like
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
        #: user-chrome: 우리가 띄운 Chrome(UserChrome)과 아직 탭으로 채택하지 않은 첫 페이지
        self._user_chrome: Any = None
        self._adoptable_page: Any = None
        #: 영속 프로필 모드(WS-32): launch_persistent_context 가 돌려준 컨텍스트
        self._persistent: Any = None

    @property
    def is_user_chrome(self) -> bool:
        return self.browser_mode == "user-chrome"

    @property
    def is_persistent(self) -> bool:
        return self.persistent_profile is not None

    # -- 수명주기 -----------------------------------------------------------

    async def start(self) -> "BrowserCore":
        if self.is_user_chrome:
            return await self._start_user_chrome()

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        if self.is_persistent:
            try:
                await self.open_persistent()
            except BaseException:
                await self._playwright.stop()
                self._playwright = None
                raise
            return self
        self._browser = await self._playwright.chromium.launch(**self._launch_kwargs())
        return self

    def _launch_kwargs(self) -> Dict[str, Any]:
        """chromium.launch / launch_persistent_context 공통 실행 인자(headless + egress)."""
        launch_kw: Dict[str, Any] = {"headless": self.headless}
        if self.egress is not None:
            launch_kw.update(self.egress.launch_kwargs())
        return launch_kw

    async def open_persistent(self, *, headless: Optional[bool] = None) -> Any:
        """영속 프로필 폴더로 브라우저+컨텍스트를 연다 — 다시 열 수 있는 단위(WS-32/WS-34).

        headless 를 주면 그 값으로 바꿔 연다(같은 프로필을 headless ↔ 창 있는 브라우저로).
        launch 인자(egress 프록시·플래그)와 context 옵션을 기존 launch+new_context 경로와 같게
        넘긴다. 컨텍스트 채택·route·이벤트 연결은 new_context(profile) 가 한다(부르는 쪽 몫).
        """
        if not self.is_persistent:
            raise RuntimeError("영속 프로필 모드가 아닙니다(persistent_profile 없음).")
        if self._persistent is not None:
            raise RuntimeError("영속 컨텍스트가 이미 열려 있습니다 — close_persistent() 먼저.")
        if self._playwright is None:
            raise RuntimeError("playwright 가 시작되지 않았습니다 — start() 먼저.")
        if headless is not None:
            self.headless = bool(headless)
        kw = self._launch_kwargs()
        kw.update(self._context_options())
        try:
            self._persistent = await self._playwright.chromium.launch_persistent_context(
                str(self.persistent_profile), **kw
            )
        except Exception as exc:  # noqa: BLE001
            from browser.serve_profile import safe_reason

            # R1 NB-3: Playwright 오류에는 실행 명령(--user-data-dir=<프로필 경로>)이 담긴다 —
            # 예외 종류·첫 줄만, 경로는 지운다(원 예외 연쇄도 끊는다: traceback 로 새지 않게).
            raise BrowserCoreError(
                ErrorCode.PAGE_CRASHED,
                f"영속 프로필 브라우저를 열지 못했습니다: {safe_reason(exc)}",
            ) from None
        return self._persistent

    async def close_persistent(self) -> None:
        """영속 컨텍스트(=브라우저)를 닫는다. 쿠키를 디스크에 남기려면 닫아야 한다.

        탭·컨텍스트 기록을 비운다. playwright 는 그대로 둔다(open_persistent 로 다시 열 수 있게).
        """
        ctx, self._persistent = self._persistent, None
        self._contexts.clear()
        self._tab_index.clear()
        self._active_tab_id = None
        self._adoptable_page = None
        if ctx is not None:
            await ctx.close()

    async def _start_user_chrome(self) -> "BrowserCore":
        """전용 프로필 Chrome 을 띄우고 CDP(127.0.0.1)로 붙는다.

        순서: 평소 프로필 가드 → Chrome 실행 → playwright 시작 → connect_over_cdp.
        가드에 걸리면 프로세스도 playwright 도 만들지 않는다. 실행 뒤 어느 단계든
        실패하면 띄운 Chrome 을 keep_open 과 무관하게 정리한다(고아 Chrome 방지).
        """
        from browser import user_chrome

        profile = self.chrome_profile or user_chrome.DEFAULT_PROFILE_DIR
        user_chrome._guard_profile(Path(profile))  # ValueError: 평소 프로필
        if self.egress is not None:
            uc = await user_chrome.launch_user_chrome(
                profile_dir=profile, extra_args=self.egress.chrome_args()
            )
        else:
            uc = await user_chrome.launch_user_chrome(profile_dir=profile)
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(uc.endpoint)
        except BaseException:
            try:
                uc.close()
            finally:
                if self._playwright is not None:
                    try:
                        await self._playwright.stop()
                    except Exception:  # noqa: BLE001
                        logger.debug("playwright 정리 실패", exc_info=True)
                    self._playwright = None
                self._browser = None
            raise
        self._user_chrome = uc
        return self

    async def close(self) -> None:
        if self.is_persistent:
            try:
                await self.close_persistent()
            except Exception as exc:  # noqa: BLE001
                from browser.serve_profile import safe_reason

                # R1 NB-3: 예외 문자열에 프로필 경로가 들어 있을 수 있다 — 종류·짧은 이유만.
                logger.warning("영속 컨텍스트 종료 실패: %s", safe_reason(exc))
        for managed in list(self._contexts.values()):
            if self.is_user_chrome:
                # 채택한 기본 컨텍스트를 닫으면 Chrome 창이 통째로 사라진다 — 닫지 않는다.
                continue
            try:
                await managed.context.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("컨텍스트 종료 실패 (%s): %s", managed.profile_name, exc)
        self._contexts.clear()
        self._tab_index.clear()
        self._active_tab_id = None
        self._adoptable_page = None

        if self._browser:
            try:
                # connect_over_cdp 로 붙은 브라우저는 close() 가 연결만 끊는다.
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001
                if not self.is_user_chrome:
                    raise
                logger.debug("CDP 연결 종료 실패: %s", exc)
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        if self._user_chrome is not None:
            uc, self._user_chrome = self._user_chrome, None
            if not self.keep_open:
                uc.close()

    async def __aenter__(self) -> "BrowserCore":
        return await self.start()

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        await self.close()

    # -- 컨텍스트 (BrowserCoreProtocol) --------------------------------------

    def _context_options(self) -> Dict[str, Any]:
        if self.human_like:
            return {"no_viewport": True, "locale": "ko-KR"}
        return {"viewport": dict(self.viewport)}

    async def new_context(self, profile_name: str) -> Any:
        """프로파일 전용 격리 컨텍스트를 생성한다.

        동일 프로파일을 재요청하면 기존 컨텍스트를 반환한다(중복 생성 방지).
        """
        if self._browser is None and self._persistent is None:
            raise BrowserCoreError(
                ErrorCode.PAGE_CRASHED, "브라우저가 시작되지 않았습니다. start()를 먼저 호출하십시오."
            )

        existing = self._contexts.get(profile_name)
        if existing:
            return existing.context

        if self.is_user_chrome:
            return self._adopt_default_context(profile_name)
        if self.is_persistent:
            return self._adopt_default_context(profile_name, self._persistent)

        if len(self._contexts) >= self.max_contexts:
            raise BrowserCoreError(
                ErrorCode.TAB_LIMIT_EXCEEDED,
                f"활성 컨텍스트 상한({self.max_contexts})을 초과했습니다.",
            )

        context = await self._browser.new_context(**self._context_options())
        self._contexts[profile_name] = ManagedContext(
            profile_name=profile_name, context=context
        )
        self._watch_context_pages(profile_name, context)
        logger.debug("컨텍스트 생성: %s", profile_name)
        return context

    async def new_context_with_session(
        self, profile_name: str, passphrase: str
    ) -> Any:
        """저장된 암호화 storageState를 주입해 컨텍스트를 생성한다."""
        if self.is_persistent:
            raise BrowserCoreError(
                ErrorCode.FEATURE_NOT_IMPLEMENTED,
                "영속 프로필 방식은 저장 세션 주입을 지원하지 않습니다 — 프로필 폴더 자체가 "
                "세션 저장소입니다(한 번 로그인하면 유지됩니다).",
            )
        if self._browser is None:
            raise BrowserCoreError(ErrorCode.PAGE_CRASHED, "브라우저가 시작되지 않았습니다.")
        if self.is_user_chrome:
            raise BrowserCoreError(
                ErrorCode.FEATURE_NOT_IMPLEMENTED,
                "user-chrome 방식은 저장 세션 주입을 지원하지 않습니다 — 전용 Chrome 프로필 "
                "자체가 세션 저장소입니다(그 창에서 한 번 로그인하면 유지됩니다).",
            )
        if len(self._contexts) >= self.max_contexts:
            raise BrowserCoreError(
                ErrorCode.TAB_LIMIT_EXCEEDED,
                f"활성 컨텍스트 상한({self.max_contexts})을 초과했습니다.",
            )

        storage_state = self.session_store.load(profile_name, passphrase)
        context = await self._browser.new_context(
            **self._context_options(), storage_state=storage_state
        )
        self._contexts[profile_name] = ManagedContext(
            profile_name=profile_name, context=context
        )
        self._watch_context_pages(profile_name, context)
        return context

    def _adopt_default_context(self, profile_name: str, context: Any = None) -> Any:
        """user-chrome: 연결된 Chrome 의 기본 컨텍스트를 profile_name 으로 채택한다.

        새 incognito 컨텍스트를 만들면 전용 프로필의 쿠키·로그인을 못 쓴다 — 그래서
        browser.contexts[0] 을 쓴다. 한 Chrome 에 기본 컨텍스트는 하나라 컨텍스트도
        1개만 허용한다(두 번째 프로필 요청은 오류). 이미 열린 about:blank 페이지는
        첫 new_tab 이 새 탭 대신 채택한다(빈 탭이 창에 남지 않게).
        """
        kind = "영속 프로필" if context is not None else "user-chrome"
        if self._contexts:
            held = next(iter(self._contexts))
            raise BrowserCoreError(
                ErrorCode.TAB_LIMIT_EXCEEDED,
                f"{kind} 방식은 컨텍스트를 1개만 씁니다(사용 중: {held!r}, "
                f"요청: {profile_name!r}).",
            )
        if context is None:
            contexts = list(getattr(self._browser, "contexts", None) or [])
            if not contexts:
                raise BrowserCoreError(
                    ErrorCode.PAGE_CRASHED, "연결된 Chrome 에 기본 컨텍스트가 없습니다."
                )
            context = contexts[0]
        self._contexts[profile_name] = ManagedContext(
            profile_name=profile_name, context=context
        )
        self._watch_context_pages(profile_name, context)
        pages = list(getattr(context, "pages", None) or [])
        if len(pages) == 1 and getattr(pages[0], "url", "") == "about:blank":
            self._adoptable_page = pages[0]
        logger.debug("user-chrome 기본 컨텍스트 채택: %s", profile_name)
        return context

    def _take_adoptable_page(self) -> Any:
        page, self._adoptable_page = self._adoptable_page, None
        if page is None:
            return None
        is_closed = getattr(page, "is_closed", None)
        try:
            if callable(is_closed) and is_closed():
                return None
        except Exception:  # noqa: BLE001
            return None
        return page

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

    def _watch_context_pages(self, profile_name: str, context: Any) -> None:
        """컨텍스트에서 새로 생긴 페이지(팝업 포함)를 관리 탭으로 등록한다 (WS-26b).

        target=_blank·window.open 팝업은 코어를 거치지 않고 생겨 tabs() 에 없었다
        (검증 NB-3). context 'page' 이벤트는 new_page() 로 만든 페이지에도 오므로
        같은 페이지는 한 번만 등록한다(_register_page 가 페이지 동일성으로 확인).
        팝업은 활성 탭을 바꾸지 않는다 — 옮기는 것은 부르는 쪽(tab_control switch) 몫.
        """
        on = getattr(context, "on", None)
        if not callable(on):
            return

        def _on_page(page: Any) -> None:
            try:
                self._register_page(profile_name, page, popup=True)
            except Exception:  # noqa: BLE001 - 등록 실패로 브라우저 이벤트 처리를 막지 않는다
                logger.warning("새 페이지 탭 등록 실패", exc_info=True)

        on("page", _on_page)

    def _find_tab_by_page(self, page: Any) -> Optional[ManagedTab]:
        for tab in self._tab_index.values():
            if tab.page is page:
                return tab
        return None

    def _register_page(
        self, profile_name: str, page: Any, *, popup: bool
    ) -> Optional[ManagedTab]:
        """page 를 관리 탭으로 등록한다. 이미 등록돼 있으면 그 탭을 돌려준다.

        popup=True(이벤트 경로)이면 탭 상한을 넘을 때 등록하지 않고 로그만 남긴다
        (예외를 던지면 Playwright 이벤트 처리 중이라 받을 사람이 없다).
        """
        existing = self._find_tab_by_page(page)
        if existing is not None:
            return existing
        managed = self._contexts.get(profile_name)
        if managed is None:
            return None
        if len(self._tab_index) >= self.max_tabs:
            if popup:
                logger.warning(
                    "세션 탭 상한(%d)이라 새 페이지를 탭으로 등록하지 않습니다: %s",
                    self.max_tabs,
                    getattr(page, "url", ""),
                )
                return None
            raise BrowserCoreError(
                ErrorCode.TAB_LIMIT_EXCEEDED,
                f"세션 탭 상한({self.max_tabs})을 초과했습니다.",
            )

        self._tab_counter += 1
        tab_id = f"tab-{self._tab_counter}"
        tab = ManagedTab(tab_id=tab_id, page=page, profile_name=profile_name)
        managed.tabs[tab_id] = tab
        self._tab_index[tab_id] = tab
        try:
            page.on("close", lambda _p=None, _id=tab_id: self._on_page_closed(_id))
        except Exception:  # noqa: BLE001 - 가짜 페이지
            pass
        return tab

    def _forget_tab(self, tab_id: str) -> Optional[ManagedTab]:
        """탭 목록에서 뺀다. 활성 탭이었으면 남은 첫 탭으로(close_tab 과 같은 규칙)."""
        tab = self._tab_index.pop(tab_id, None)
        if tab is None:
            return None
        managed = self._contexts.get(tab.profile_name)
        if managed:
            managed.tabs.pop(tab_id, None)
        if self._active_tab_id == tab_id:
            self._active_tab_id = next(iter(self._tab_index), None)
        return tab

    def _on_page_closed(self, tab_id: str) -> None:
        """페이지가 (사이트 JS·사용자·close_tab 등으로) 닫히면 탭 목록에서 뺀다."""
        self._forget_tab(tab_id)

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
            adopt = self.is_user_chrome or self.is_persistent
            page = self._take_adoptable_page() if adopt else None
            if page is None:
                page = await managed.context.new_page()

        # context 'page' 이벤트가 먼저 등록했으면 그 탭을 그대로 쓴다(이중 등록 없음).
        tab = self._register_page(profile_name, page, popup=False)
        assert tab is not None
        self._active_tab_id = tab.tab_id

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
        tab = self._forget_tab(tab_id)
        if tab is None:
            raise BrowserCoreError(
                ErrorCode.TAB_NOT_FOUND, f"탭을 찾을 수 없습니다: {tab_id}"
            )
        await tab.page.close()

    def switch_tab(self, tab_id: str) -> None:
        if tab_id not in self._tab_index:
            raise BrowserCoreError(
                ErrorCode.TAB_NOT_FOUND, f"탭을 찾을 수 없습니다: {tab_id}"
            )
        self._active_tab_id = tab_id

    def set_active_tab(self, tab_id: str) -> None:
        """switch_tab 별칭. 디스패처 tab_control 이 이 이름으로 부른다."""
        self.switch_tab(tab_id)

    def list_tabs(self) -> Dict[str, str]:
        """tab_id → profile_name 매핑을 반환한다."""
        return {tid: tab.profile_name for tid, tab in self._tab_index.items()}

    def get_tab(self, tab_id: Optional[str]) -> Optional["ManagedTab"]:
        """tab_id로 관리 탭을 조회한다. 없으면 None."""
        if not tab_id:
            return None
        return self._tab_index.get(tab_id)

    def tab_for_page(self, page: Any) -> Optional["ManagedTab"]:
        """Page 로 관리 탭을 조회한다. 등록되지 않았으면 None."""
        return self._find_tab_by_page(page)

    def tabs(self) -> List["ManagedTab"]:
        """생성 순서대로 관리 탭 목록을 반환한다."""
        return list(self._tab_index.values())

    @property
    def active_profile(self) -> Optional[str]:
        """활성 탭이 속한 프로파일 이름."""
        tab = self.get_tab(self._active_tab_id)
        return tab.profile_name if tab else None

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
