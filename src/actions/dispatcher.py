"""19종 액션 디스패처 (PRD §4.1, §4.3).

`contracts.ActionDispatcherProtocol` 구현체.

실행 흐름 (PRD §4.3):

    [1] Staleness 검증 (dispatch 이전)
         |- 통과 -> [2]
         `- 실패 -> 자가 치유 사다리 -> 대체 요소로 [2] (부작용 없으므로 안전)
    [2] CDP 이벤트 발송
    [3] 사후조건 검증
         |- 통과 -> success
         `- 미충족 -> retry_safe 판정
                      |- Yes -> 치유 후 1회 재시도
                      `- No  -> 즉시 중단 (이중 제출 방지)

핵심 원칙: dispatch 이후 실패에서 click/submit을 재시도하지 않는다.
결제가 두 번 실행되는 것보다 실패를 보고하는 편이 낫다.

`ActionResult`는 Stage 0에서 동결된 계약이므로 필수 필드
(`current_url`, `snapshot_epoch`, `tab_id`, `retry_safe`)를 항상 채운다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from contracts import ActionResult, ActionType, ErrorCode
from perception.engine import ElementHandle, PerceptionEngine

from actions.healing import (
    FailurePhase,
    HealingCandidate,
    HealingResult,
    heal,
    is_retry_safe,
)
from actions.verification import (
    capture_state,
    verify_post_condition,
    verify_staleness,
)

logger = logging.getLogger(__name__)

#: 요소를 대상으로 하지 않는 액션 (staleness 검증 대상 아님)
_ELEMENTLESS_ACTIONS = frozenset(
    {
        ActionType.OBSERVE_PAGE,
        ActionType.TAKE_SCREENSHOT,
        ActionType.NAVIGATE,
        ActionType.GO_BACK,
        ActionType.RELOAD,
        ActionType.SCROLL,
        ActionType.PRESS_KEY,
        ActionType.WAIT_FOR,
        ActionType.EXTRACT,
        ActionType.SWITCH_FRAME,
        ActionType.HANDLE_DIALOG,
        ActionType.TAB_CONTROL,
    }
)

#: 에포크를 증가시키는 액션 (PRD §4.2)
EPOCH_BUMPING_ACTIONS = frozenset(
    {ActionType.NAVIGATE, ActionType.GO_BACK, ActionType.RELOAD, ActionType.SWITCH_FRAME}
)


@dataclass
class DispatchContext:
    """액션 실행에 필요한 런타임 핸들."""

    page: Any
    engine: PerceptionEngine
    cdp: Any = None
    tab_id: str = "tab-1"
    #: BrowserCore 인스턴스. tab_control에 필요하며 미주입 시 해당 액션만 제한된다.
    core: Any = None


#: Playwright 키 이름 별칭.
#: Playwright는 'Enter'만 받고 'enter'/'Return'은 Unknown key로 거부한다.
#: LLM은 소문자나 별칭('return', 'esc')을 자주 쓰므로 정규화한다.
#: 실측 — TodoMVC에서 LLM이 Enter 입력에 실패해 항목 추가가 무산됐다.
_KEY_ALIASES: Dict[str, str] = {
    "enter": "Enter",
    "return": "Enter",
    "cr": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "Space",
    "spacebar": "Space",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "home": "Home",
    "end": "End",
}


def _normalize_key(key: str) -> str:
    """키 이름을 Playwright가 받는 형태로 정규화한다.

    조합 키('Control+A')는 각 파트를 개별 정규화한다.
    알 수 없는 이름은 그대로 두어 Playwright가 판단하게 한다
    (단일 문자 'a' 등은 유효한 입력이다).
    """
    raw = (key or "").strip()
    if not raw:
        return raw
    if "+" in raw:
        return "+".join(_normalize_key(part) for part in raw.split("+"))
    return _KEY_ALIASES.get(raw.lower(), raw)


class ActionDispatcher:
    """19종 액션 실행기."""

    def __init__(self, context: DispatchContext) -> None:
        self.ctx = context
        self._healing_attempts = 0
        self._healing_successes = 0

    # -- 통계 (하네스가 성공률 측정에 사용) ----------------------------------

    @property
    def healing_attempts(self) -> int:
        return self._healing_attempts

    @property
    def healing_successes(self) -> int:
        return self._healing_successes

    @property
    def healing_rate(self) -> float:
        if not self._healing_attempts:
            return 0.0
        return self._healing_successes / self._healing_attempts

    # -- 계약 필수 필드 채우기 -----------------------------------------------

    def _current_url(self) -> str:
        try:
            return self.ctx.page.url or ""
        except Exception:  # noqa: BLE001
            return ""

    def _result(
        self,
        *,
        success: bool,
        action: ActionType,
        retry_safe: bool,
        error_code: Optional[ErrorCode] = None,
        error_message: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        healed: bool = False,
        reobserve_required: bool = False,
        downloaded_path: Optional[str] = None,
        popup_tab_id: Optional[str] = None,
    ) -> ActionResult:
        """동결된 계약 형태로 결과를 구성한다."""
        return ActionResult(
            success=success,
            action=action,
            current_url=self._current_url(),
            snapshot_epoch=self.ctx.engine.epoch,
            tab_id=self.ctx.tab_id,
            healed=healed,
            reobserve_required=reobserve_required,
            retry_safe=retry_safe,
            downloaded_path=downloaded_path,
            popup_tab_id=popup_tab_id,
            error_code=error_code,
            error_message=error_message,
            data=data or {},
        )

    # -- 메인 진입점 ---------------------------------------------------------

    async def dispatch(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        """액션을 실행하고 `ActionResult`를 반환한다."""
        started = time.perf_counter()
        try:
            result = await self._dispatch_inner(action, params)
        except Exception as exc:  # noqa: BLE001 - 어떤 실패도 계약 형태로 반환
            logger.exception("액션 실행 중 예외: %s", action.value)
            result = self._result(
                success=False,
                action=action,
                retry_safe=is_retry_safe(action, FailurePhase.PRE_DISPATCH),
                error_code=ErrorCode.PAGE_CRASHED,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        result.data.setdefault(
            "latency_ms", round((time.perf_counter() - started) * 1000, 2)
        )
        return result

    async def _dispatch_inner(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        # 요소를 다루지 않는 액션은 곧바로 실행한다.
        if action in _ELEMENTLESS_ACTIONS:
            return await self._execute_elementless(action, params)

        element_id = params.get("element_id")
        if not element_id:
            return self._result(
                success=False,
                action=action,
                retry_safe=True,  # 발송 전이므로 안전
                error_code=ErrorCode.ELEMENT_NOT_FOUND,
                error_message="element_id가 필요합니다.",
            )

        handle = self.ctx.engine.get_handle(element_id)
        if handle is None:
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.TOCTOU_MISMATCH,
                error_message=f"{element_id}는 현재 에포크에서 유효하지 않습니다.",
                reobserve_required=True,
            )

        # --- [1] dispatch 이전 Staleness 검증 -------------------------------
        staleness = await verify_staleness(
            self.ctx.page,
            handle,
            self.ctx.engine.epoch,
            expected_role=params.get("expected_role"),
            expected_name=params.get("expected_name"),
        )

        healed_flag = False
        if not staleness.fresh:
            # 부작용이 없는 시점이므로 치유가 안전하다.
            healing = await self._attempt_heal(handle)
            if not healing.healed or healing.candidate is None:
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=staleness.error_code or ErrorCode.ELEMENT_NOT_FOUND,
                    error_message=(
                        f"Staleness 검증 실패({staleness.detail}) 및 자가 치유 실패"
                    ),
                    reobserve_required=True,
                    data={"healing_attempts": healing.attempts},
                )
            new_handle = self.ctx.engine.get_handle(healing.candidate.element_id)
            if new_handle is None:
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.ELEMENT_NOT_FOUND,
                    error_message="치유된 요소의 핸들을 찾을 수 없습니다.",
                    reobserve_required=True,
                )
            handle = new_handle
            healed_flag = True

        # --- [2] 이벤트 발송 -------------------------------------------------
        before = await capture_state(self.ctx.page, handle)
        try:
            await self._execute_element_action(action, handle, params)
        except Exception as exc:  # noqa: BLE001
            # 발송 자체가 실패했으므로 부작용이 없다 -> 재시도 안전
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_INTERACTABLE,
                error_message=f"이벤트 발송 실패: {exc}",
                healed=healed_flag,
            )

        # --- [3] 사후조건 검증 ------------------------------------------------
        after = await capture_state(self.ctx.page, handle)
        post = verify_post_condition(
            before,
            after,
            expected_value=params.get("text") if action is ActionType.TYPE_TEXT else None,
            expected_checked=(
                params.get("checked") if action is ActionType.CHECK_BOX else None
            ),
        )

        if post.satisfied:
            return self._result(
                success=True,
                action=action,
                retry_safe=is_retry_safe(action, FailurePhase.POST_DISPATCH),
                healed=healed_flag,
                data={"signals": post.signals, "element_id": handle.element_id},
            )

        # 사후조건 미충족 (Silent Failure)
        post_retry_safe = is_retry_safe(action, FailurePhase.POST_DISPATCH)
        if not post_retry_safe:
            # 이중 제출/결제 방지를 위해 재시도하지 않는다.
            return self._result(
                success=False,
                action=action,
                retry_safe=False,
                error_code=ErrorCode.TIMEOUT,
                error_message=(
                    f"사후조건 미충족({post.detail}). "
                    f"{action.value}는 재시도 시 부작용이 중복될 수 있어 중단합니다."
                ),
                reobserve_required=True,
                healed=healed_flag,
            )

        # 멱등 액션만 1회 재시도
        try:
            await self._execute_element_action(action, handle, params)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_INTERACTABLE,
                error_message=f"재시도 실패: {exc}",
                reobserve_required=True,
                healed=healed_flag,
            )

        retry_after = await capture_state(self.ctx.page, handle)
        retry_post = verify_post_condition(
            before,
            retry_after,
            expected_value=params.get("text") if action is ActionType.TYPE_TEXT else None,
            expected_checked=(
                params.get("checked") if action is ActionType.CHECK_BOX else None
            ),
        )
        return self._result(
            success=retry_post.satisfied,
            action=action,
            retry_safe=True,
            error_code=None if retry_post.satisfied else ErrorCode.TIMEOUT,
            error_message=(
                None if retry_post.satisfied else f"재시도 후에도 미충족({retry_post.detail})"
            ),
            data={"signals": retry_post.signals, "retried": True},
            healed=healed_flag,
            reobserve_required=not retry_post.satisfied,
        )

    # -- 치유 ---------------------------------------------------------------

    async def _attempt_heal(self, handle: ElementHandle) -> HealingResult:
        """재관찰 후 자가 치유 사다리를 가동한다."""
        self._healing_attempts += 1

        result = await self.ctx.engine.observe_page(
            page=self.ctx.page, cdp=self.ctx.cdp
        )
        candidates: List[HealingCandidate] = []
        for observed in result.elements:
            h = self.ctx.engine.get_handle(observed.element_id)
            candidates.append(
                HealingCandidate(
                    element_id=observed.element_id,
                    role=observed.role,
                    name=observed.name,
                    css_path=h.css_path if h else "",
                    testid=h.testid if h else None,
                    is_shadow=observed.is_shadow,
                )
            )

        healing = heal(handle, candidates)
        if healing.healed:
            self._healing_successes += 1
            logger.debug("치유 성공: %s (%s)", healing.strategy, healing.reason)
        return healing

    # -- 실행 ---------------------------------------------------------------

    async def _execute_element_action(
        self, action: ActionType, handle: ElementHandle, params: Dict[str, Any]
    ) -> None:
        """요소 대상 액션을 실제로 발송한다."""
        page = self.ctx.page
        selector = handle.css_path

        if action is ActionType.CLICK:
            await page.click(selector, button=params.get("button", "left"), timeout=5000)

        elif action is ActionType.TYPE_TEXT:
            if params.get("clear_before", True):
                await page.fill(selector, "", timeout=5000)
            await page.type(selector, params.get("text", ""), timeout=5000)
            if params.get("press_enter"):
                await page.press(selector, "Enter", timeout=5000)

        elif action is ActionType.SELECT_OPTION:
            if params.get("value") is not None:
                await page.select_option(selector, value=params["value"], timeout=5000)
            else:
                await page.select_option(
                    selector, index=params.get("index", 0), timeout=5000
                )

        elif action is ActionType.CHECK_BOX:
            if params.get("checked", True):
                await page.check(selector, timeout=5000)
            else:
                await page.uncheck(selector, timeout=5000)

        elif action is ActionType.HOVER:
            await page.hover(selector, timeout=5000)

        elif action is ActionType.UPLOAD_FILE:
            await page.set_input_files(
                selector, params.get("file_paths", []), timeout=5000
            )

        elif action is ActionType.DOWNLOAD_FILE:
            async with page.expect_download(
                timeout=params.get("timeout_ms", 30000)
            ) as dl:
                await page.click(selector, timeout=5000)
            download = await dl.value
            target = f"{params.get('save_dir', '.')}/{download.suggested_filename}"
            await download.save_as(target)
            params["_downloaded_path"] = target

        else:
            raise ValueError(f"요소 대상 액션이 아닙니다: {action.value}")

    async def _execute_elementless(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        """요소를 다루지 않는 액션을 실행한다."""
        page = self.ctx.page

        if action is ActionType.OBSERVE_PAGE:
            observed = await self.ctx.engine.observe_page(
                prune_top_n=params.get("prune_top_n", 20),
                force_full_tree=params.get("force_full_tree", False),
                page=page,
                cdp=self.ctx.cdp,
            )
            return self._result(
                success=True,
                action=action,
                retry_safe=True,
                data={"observation": observed.model_dump(mode="json")},
            )

        if action is ActionType.NAVIGATE:
            try:
                await page.goto(
                    params["url"],
                    wait_until=params.get("wait_until", "domcontentloaded"),
                    timeout=params.get("timeout_ms", 30000),
                )
            except Exception as exc:  # noqa: BLE001
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.NAVIGATE_TIMEOUT,
                    error_message=str(exc),
                )
            self.ctx.engine.bump_epoch("navigate")
            return self._result(
                success=True, action=action, retry_safe=True, data={"url": page.url}
            )

        if action is ActionType.GO_BACK:
            response = await page.go_back(timeout=params.get("timeout_ms", 10000))
            if response is None:
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.NO_HISTORY,
                    error_message="이전 히스토리가 없습니다.",
                )
            self.ctx.engine.bump_epoch("go_back")
            return self._result(
                success=True, action=action, retry_safe=True, data={"url": page.url}
            )

        if action is ActionType.RELOAD:
            await page.reload(timeout=params.get("timeout_ms", 30000))
            self.ctx.engine.bump_epoch("reload")
            return self._result(
                success=True, action=action, retry_safe=True, data={"url": page.url}
            )

        if action is ActionType.SCROLL:
            distance = params.get("distance", 500)
            delta = distance if params.get("direction", "down") == "down" else -distance
            before_height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate(f"window.scrollBy(0, {delta})")
            await page.wait_for_timeout(150)
            after_height = await page.evaluate("document.body.scrollHeight")
            return self._result(
                success=True,
                action=action,
                retry_safe=True,
                data={"scrolled": delta},
                # 동적 노드가 로드되었으면 재관찰이 필요하다.
                reobserve_required=after_height != before_height,
            )

        if action is ActionType.PRESS_KEY:
            raw_key = str(params.get("key", ""))
            key = _normalize_key(raw_key)
            try:
                await page.keyboard.press(key)
            except Exception as exc:  # noqa: BLE001
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=False,  # 키 입력은 발송 후 재시도 위험
                    error_code=ErrorCode.KEY_PRESS_FAILED,
                    error_message=f"{exc} (입력값: {raw_key!r} -> {key!r})",
                )
            return self._result(
                success=True, action=action, retry_safe=False, data={"key": key}
            )

        if action is ActionType.WAIT_FOR:
            return await self._wait_for(params)

        if action is ActionType.EXTRACT:
            return await self._extract(params)

        if action is ActionType.TAKE_SCREENSHOT:
            if params.get("annotate_som"):
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.FEATURE_NOT_IMPLEMENTED,
                    error_message="SoM 주석은 v1.1에서 활성화됩니다.",
                )
            try:
                shot = await page.screenshot(full_page=params.get("full_page", False))
            except Exception as exc:  # noqa: BLE001
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.SCREENSHOT_FAILED,
                    error_message=str(exc),
                )
            return self._result(
                success=True, action=action, retry_safe=True, data={"bytes": len(shot)}
            )

        if action is ActionType.SWITCH_FRAME:
            return await self._switch_frame(params)

        if action is ActionType.HANDLE_DIALOG:
            accept = params.get("accept", True)

            async def _handler(dialog) -> None:  # noqa: ANN001
                if accept:
                    await dialog.accept(params.get("prompt_text") or "")
                else:
                    await dialog.dismiss()

            page.once("dialog", _handler)
            return self._result(
                success=True, action=action, retry_safe=False, data={"accept": accept}
            )

        if action is ActionType.TAB_CONTROL:
            return await self._dispatch_tab_control(action, params)

    async def _dispatch_tab_control(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        """탭 생성/전환/종료/목록 (PRD §4.1 tab_control 4서브커맨드).

        `BrowserCore`가 주입되지 않은 경우(단위 테스트 등)에는 페이지의
        컨텍스트를 직접 사용해 최소 동작을 제공한다.
        """
        command = str(params.get("command", "")).lower()
        core = self.ctx.core

        if core is None:
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.FEATURE_NOT_IMPLEMENTED,
                error_message="tab_control에는 BrowserCore 주입이 필요합니다.",
            )

        try:
            if command == "list":
                tabs = core.tabs()
                return self._result(
                    success=True,
                    action=action,
                    retry_safe=True,
                    data={
                        "tabs": [
                            {"tab_id": t.tab_id, "url": t.page.url} for t in tabs
                        ],
                        "count": len(tabs),
                        "active": core.active_tab_id,
                    },
                )

            if command == "new":
                tab = await core.new_tab(
                    core.active_profile, url=params.get("url")
                )
                # 새 탭이 활성 대상이 되도록 디스패처 컨텍스트를 갱신한다.
                self.ctx.page = tab.page
                self.ctx.tab_id = tab.tab_id
                self.ctx.engine.bump_epoch("tab_new")
                return self._result(
                    success=True,
                    action=action,
                    retry_safe=False,
                    reobserve_required=True,
                    data={"tab_id": tab.tab_id, "url": tab.page.url},
                )

            if command == "switch":
                tab_id = params.get("tab_id")
                tab = core.get_tab(tab_id) if tab_id else None
                if tab is None:
                    return self._result(
                        success=False,
                        action=action,
                        retry_safe=True,
                        error_code=ErrorCode.TAB_NOT_FOUND,
                        error_message=f"탭을 찾을 수 없습니다: {tab_id}",
                    )
                core.set_active_tab(tab.tab_id)
                self.ctx.page = tab.page
                self.ctx.tab_id = tab.tab_id
                # 탭 전환은 컨텍스트 전환이므로 에포크를 올린다 (PRD §4.2).
                self.ctx.engine.bump_epoch("tab_switch")
                return self._result(
                    success=True,
                    action=action,
                    retry_safe=True,
                    reobserve_required=True,
                    data={"tab_id": tab.tab_id, "url": tab.page.url},
                )

            if command == "close":
                tab_id = params.get("tab_id") or self.ctx.tab_id
                if core.get_tab(tab_id) is None:
                    return self._result(
                        success=False,
                        action=action,
                        retry_safe=True,
                        error_code=ErrorCode.TAB_NOT_FOUND,
                        error_message=f"탭을 찾을 수 없습니다: {tab_id}",
                    )
                await core.close_tab(tab_id)
                remaining = core.tabs()
                if remaining:
                    core.set_active_tab(remaining[0].tab_id)
                    self.ctx.page = remaining[0].page
                    self.ctx.tab_id = remaining[0].tab_id
                self.ctx.engine.bump_epoch("tab_close")
                return self._result(
                    success=True,
                    action=action,
                    retry_safe=False,
                    reobserve_required=True,
                    data={"closed": tab_id, "remaining": len(remaining)},
                )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                success=False,
                action=action,
                retry_safe=False,
                error_code=ErrorCode.PAGE_CRASHED,
                error_message=f"탭 제어 실패: {exc}",
            )

        return self._result(
            success=False,
            action=action,
            retry_safe=True,
            error_code=ErrorCode.FEATURE_NOT_IMPLEMENTED,
            error_message=(
                f"알 수 없는 서브커맨드: {command!r} "
                "(new / switch / close / list 중 하나여야 합니다)"
            ),
        )

    async def _dispatch_unknown(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        return self._result(
            success=False,
            action=action,
            retry_safe=True,
            error_code=ErrorCode.FEATURE_NOT_IMPLEMENTED,
            error_message=f"미지원 액션: {action.value}",
        )

    async def _wait_for(self, params: Dict[str, Any]) -> ActionResult:
        page = self.ctx.page
        condition = params.get("condition", "stabilize")
        timeout = params.get("timeout_ms", 10000)
        try:
            if condition == "selector":
                await page.wait_for_selector(params["selector"], timeout=timeout)
            elif condition == "network_idle":
                await page.wait_for_load_state("networkidle", timeout=timeout)
            elif condition == "spa_route":
                start_url = page.url
                deadline = time.perf_counter() + timeout / 1000
                while page.url == start_url and time.perf_counter() < deadline:
                    await page.wait_for_timeout(100)
                if page.url == start_url:
                    raise TimeoutError("SPA 라우팅이 발생하지 않았습니다.")
            else:  # stabilize
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                await page.wait_for_timeout(200)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                success=False,
                action=ActionType.WAIT_FOR,
                retry_safe=True,
                error_code=ErrorCode.TIMEOUT,
                error_message=f"{condition} 대기 실패: {exc}",
            )
        return self._result(
            success=True,
            action=ActionType.WAIT_FOR,
            retry_safe=True,
            data={"condition": condition},
        )

    async def _extract(self, params: Dict[str, Any]) -> ActionResult:
        page = self.ctx.page
        selector = params["selector"]
        attributes: Sequence[str] = params.get("attributes", [])
        extract_all = params.get("extract_all", False)

        payload = await page.evaluate(
            """
            (args) => {
              const nodes = args.all
                ? Array.from(document.querySelectorAll(args.selector))
                : [document.querySelector(args.selector)].filter(Boolean);
              return nodes.map((el) => {
                const item = { text: (el.innerText || el.textContent || '').trim() };
                for (const attr of args.attrs) item[attr] = el.getAttribute(attr);
                return item;
              });
            }
            """,
            {"selector": selector, "attrs": list(attributes), "all": extract_all},
        )
        if not payload:
            return self._result(
                success=False,
                action=ActionType.EXTRACT,
                retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_FOUND,
                error_message=f"셀렉터에 해당하는 요소가 없습니다: {selector}",
            )
        return self._result(
            success=True,
            action=ActionType.EXTRACT,
            retry_safe=True,
            data={"items": payload if extract_all else payload[0]},
        )

    async def _switch_frame(self, params: Dict[str, Any]) -> ActionResult:
        page = self.ctx.page
        frame_selector = params.get("frame_selector")
        if frame_selector:
            element = await page.query_selector(frame_selector)
            frame = await element.content_frame() if element else None
            if frame is None:
                return self._result(
                    success=False,
                    action=ActionType.SWITCH_FRAME,
                    retry_safe=True,
                    error_code=ErrorCode.FRAME_NOT_FOUND,
                    error_message=f"프레임을 찾을 수 없습니다: {frame_selector}",
                )
            self.ctx.engine.bump_epoch("switch_frame")
            return self._result(
                success=True,
                action=ActionType.SWITCH_FRAME,
                retry_safe=True,
                data={"frame_url": frame.url},
            )

        shadow_selector = params.get("shadow_root_selector")
        exists = await page.evaluate(
            "(sel) => { const el = document.querySelector(sel); "
            "return !!(el && el.shadowRoot); }",
            shadow_selector,
        )
        if not exists:
            return self._result(
                success=False,
                action=ActionType.SWITCH_FRAME,
                retry_safe=True,
                error_code=ErrorCode.SHADOW_ROOT_NOT_FOUND,
                error_message=f"Shadow root를 찾을 수 없습니다: {shadow_selector}",
            )
        self.ctx.engine.bump_epoch("switch_shadow")
        return self._result(
            success=True,
            action=ActionType.SWITCH_FRAME,
            retry_safe=True,
            data={"shadow_root": shadow_selector},
        )
