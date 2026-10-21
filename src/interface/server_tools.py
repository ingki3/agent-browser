"""BrowserMCPServer 의 도구 라우팅: call_tool·_call_tool·서버 도구·레시피 도구·오류 결과.

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any, Dict, Optional

from contracts import ACTION_INPUT_MAP, ActionResult, ActionType, ErrorCode
from interface.server_common import _wait_timeout
from interface.server_schema import (CHALLENGE_CHECK_ACTIONS, RECIPE_TOOL, SERVER_TOOLS, TOOL_PREFIX, YOUTUBE_TOOL,
                                      action_from_tool)
from interface.server_envelope import cap_result_size
from interface.server_workspace import WorkspaceNameError, pop_workspace_arg

logger = logging.getLogger("interface.mcp_server")


class ToolsMixin:
    """BrowserMCPServer 의 도구 라우팅: call_tool·_call_tool·서버 도구·레시피 도구·오류 결과 (WS-48 믹스인 — 상태는 BrowserMCPServer.__init__)."""

    #: WS-49 YouTube 검색 — 할당량 추정 장부(첫 호출 때 만듦)와 **테스트 전용** API 주소(루프백 Mock).
    #: 운영에서는 None → interface.youtube_search.API_BASE(www.googleapis.com 고정). 환경변수·CLI 로 바꾸는 길은 없다.
    _youtube_ledger: Any = None
    _youtube_api_base: Optional[str] = None
    #: WS-52 R2: the passive reads a running settle tail survives (only as the sole call in flight).
    _TAIL_PASSIVE_ACTIONS = frozenset({ActionType.OBSERVE_PAGE, ActionType.EXTRACT, ActionType.TAKE_SCREENSHOT,
                                       ActionType.WAIT_FOR})

    def tool_entry(self, name: str) -> Any:
        """WS-52 도구 시작 장벽 — 모든 도구 진입점이 await 전에 동기적으로 든다(with).

        R2: 모든 호출(액션·서버 도구·레시피·YouTube, 중첩 포함)은 진행 중인 다른 호출을 '겹침'으로 표시하고
        재시도 장부 꼬리를 영구 중단한다(세대 번호를 올려 진행 중 쿠키 읽기도 합치기 직전에 버린다).
        예외는 수동 읽기(observe_page·extract·take_screenshot·wait_for)가 유일한 진행 호출일 때뿐 — scroll·hover 는
        페이지 스크립트를 깨울 수 있어 중단한다.
        """
        from contextlib import nullcontext

        retry = getattr(self, "_headless_retry", None)
        if retry is None:
            return nullcontext()  # no retry memory → no tail to close (bare test servers)
        return retry.tool_call(read_only=action_from_tool(name) in self._TAIL_PASSIVE_ACTIONS)

    async def _retry_settled(self) -> None:
        """WS-52 R3: the tail this call just ended is torn down (init script removed) before the
        call acts — a document it opens never gets the cookie-write observer."""
        retry = getattr(self, "_headless_retry", None)
        if retry is not None:
            await retry.settled()

    async def call_server_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """계약 밖 서버 도구(WS-29). 결과는 dict(JSON 응답).

        WS-51: 응답의 페이지/탭 주소(window.tabs[].url·탭 알림·레시피 단계 결과 …)는 비밀 값을 가린 새 dict 로
        돌려준다(security.public_url). 서버 내부 상태(last_tabs 등)는 원문 그대로 둔다.
        """
        from security.public_url import public_payload

        with self.tool_entry(name):
            try:
                # WS-57: 작업 공간 인자(계약 밖) — 레시피만 쓰고, 조작권 도구는 브라우저 전체 단위라 무시한다.
                arguments, workspace = pop_workspace_arg(arguments)
            except WorkspaceNameError as exc:
                return {"success": False, "error_code": ErrorCode.TAB_NOT_FOUND.value, "error_message": str(exc)}
            with self._call_scope(workspace):
                await self._retry_settled()
                return public_payload(await self._server_tool_payload(name, arguments))

    async def _server_tool_payload(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """call_server_tool 본문(가리기 전 원문 응답)."""
        args = dict(arguments or {})
        if name not in SERVER_TOOLS:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": f"알 수 없는 툴: {name}"}
        if name == RECIPE_TOOL:
            # WS-57 R2(B1): 레시피 재생은 이 작업 공간의 액션 여러 개 — 단계 사이에 같은 작업 공간의 다른 호출이 끼지 않게
            # 재생 전체를 직렬화 구간으로(안쪽 call_tool 은 같은 흐름이라 다시 잡지 않는다).
            async with self._dispatch_serial():
                if self._started:
                    await self._ensure_workspace()
                return await self._recipe_tool(args)
        if name == YOUTUBE_TOOL:
            return await self._youtube_tool(args)
        if not self.hub.opened:
            self.open_handoff()
        if not self.hub.opened:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": "사람 인계 통로(상태 디렉터리)를 열 수 없습니다 — 서버 stderr 참조."}
        await self._poll_handoff()
        short = name[len(TOOL_PREFIX):]
        if short == "control_status":
            data: Dict[str, Any] = {"control": self.hub.status()}
            data["control"].pop("window", None)
            if self.profile is not None:
                data["profile"] = self._profile_info()
            if self._window is not None:
                data["window"] = self._window.info()
            if self._tab_notice is not None:
                data["tab_closed_by_human"] = self._tab_notice
            return {"success": True, "data": data}
        if short == "control_request":
            result = await self._control_request(args)
            result.setdefault("data", {}).setdefault("login_hint", self._login_hint())
            return result
        timeout = _wait_timeout(args.get("timeout_s"))
        if short == "control_wait":
            return await self._control_wait(timeout)
        return await self._approval_wait(str(args.get("approval_id") or ""), timeout)

    async def _recipe_tool(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """browser_recipe(WS-38). 응답 data 도 IPI 신호 경로를 거친다(레시피 이름·중단 사유·관찰)."""
        if self._recipes is None:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": "레시피가 꺼져 있습니다(serve --no-recipes)."}
        saved = getattr(self, "_saved_pages", None)
        from recipes.saved_pages import OPS as SAVED_PAGE_OPS

        if saved is not None and str(args.get("op") or "") in SAVED_PAGE_OPS:
            payload = saved.tool(args)  # WS-61 도착 페이지 기억
        else:
            payload = await self._recipes.tool(args)
        data = payload.get("data")
        if isinstance(data, dict) and "injection_suspected" not in data:
            from security.injection_signal import injection_signal

            signal = injection_signal(data, None, 10 * self.max_result_chars)
            if signal is not None:
                data["injection_suspected"] = signal
        return payload

    async def _youtube_tool(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """browser_youtube_search(WS-49). 브라우저·사람 인계 통로 없이 동작한다.

        Egress: 서버가 이미 시작했으면 그 가드·검증 프록시를, 아니면 같은 운영자 정책(allowlist·사설망·루프백)
        으로 이 호출만의 프록시를 띄워 지난다(robots 조회와 같은 방식). 할당량 장부는 서버 프로세스 단위이고
        `--profile` 로 프로필을 잡은 뒤에는 그 폴더 파일(0600)에도 남는다.
        """
        from interface import youtube_search as yt

        try:
            return await self._youtube_call(args, yt)
        except Exception as exc:  # noqa: BLE001 - 서버 도구 경계의 마지막 안전망(예외 문구는 키·외부 원문을 담을 수 있다)
            logger.warning("browser_youtube_search 처리 중 예상하지 못한 오류 (%s)", type(exc).__name__)
            return yt.internal_error_result(self._youtube_ledger)

    async def _youtube_call(self, args: Dict[str, Any], yt: Any) -> Dict[str, Any]:
        from pathlib import Path

        QuotaLedger, youtube_search, API_BASE = yt.QuotaLedger, yt.youtube_search, yt.API_BASE
        lock = getattr(self, "_profile_lock", None)
        path = (Path(lock.path) / "youtube_quota.json"
                if self.profile is not None and lock is not None and getattr(lock, "held", False) else None)
        if self._youtube_ledger is None:
            self._youtube_ledger = QuotaLedger(path=path)
        elif path is not None and self._youtube_ledger.path != path:
            self._youtube_ledger.path = path  # 메모리 추정치와 파일 기록 중 큰 쪽을 이어 간다
        runtime = None
        guard, proxy = self._egress, self._egress_proxy
        if guard is None or proxy is None or not getattr(proxy, "running", False):
            from security.egress_runtime import EgressRuntime

            runtime = await EgressRuntime(
                allowed_domains=self.allowed_domains,
                allow_private_network=self.allow_private_network,
                block_loopback=self.block_loopback,
            ).start()
            guard, proxy = runtime.guard, runtime.proxy
        try:
            return await youtube_search(args, guard=guard, proxy=proxy, ledger=self._youtube_ledger,
                                        api_base=self._youtube_api_base or API_BASE,
                                        max_result_chars=self.max_result_chars)
        finally:
            if runtime is not None:
                await runtime.close()

    def _recipe_page(self) -> Any:
        """레시피 기록·재생 대상 페이지. 프레임 안이면 None(지원하지 않음)."""
        ctx = getattr(self._dispatcher, "ctx", None)
        if ctx is None or getattr(ctx, "root_page", None) is not None:
            return None
        return getattr(ctx, "page", None)

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> ActionResult:
        """MCP 툴 호출을 디스패처로 라우팅한다(_call_tool). 화면 캡처는 확인 코드 표시와 직렬화한다.

        WS-29 R2 (d): 캡처를 시작한 뒤 확인 코드 오버레이가 한 번이라도 켜졌으면(세대 번호가 바뀜)
        캡처 결과를 버리고 거부한다 — 코드 표시 쪽의 기다림(b)이 없어도 막히는 겹 방어.
        """
        # WS-52 R1: anything that may change the page (login included) closes the settle tail
        # synchronously, before the first await (tool_entry).
        with self.tool_entry(name):
            try:
                # WS-57: 작업 공간 인자(계약 밖 서버 메타) — 계약 입력 검증 전에 떼어 낸다.
                arguments, workspace = pop_workspace_arg(arguments)
            except WorkspaceNameError as exc:
                return self._workspace_error(action_from_tool(name) or ActionType.OBSERVE_PAGE, exc)
            with self._call_scope(workspace):
                await self._retry_settled()
                return await self._route_tool_call(name, arguments)

    async def _route_tool_call(self, name: str, arguments: Dict[str, Any]) -> ActionResult:
        from interface.on_demand import SWITCH_WAIT_S

        capture: Dict[str, Any] = {}
        if self._window is None and not await self._await_gate(SWITCH_WAIT_S):
            early = self._error_result(action_from_tool(name) or ActionType.OBSERVE_PAGE,
                                       ErrorCode.TIMEOUT, "로그인 창을 준비 중입니다; 잠시 뒤 다시 호출하세요.")
            self._note_saved_pages(action_from_tool(name), arguments, early)
            return early
        if self._window is not None:
            # WS-34: 창 전환과 겹치지 않게 — 전환 중이면 기다리고, 실패 상태면 headless 로 다시 연다.
            blocked = await self._on_demand_enter(action_from_tool(name) or ActionType.OBSERVE_PAGE)
            if blocked is not None:
                self._note_saved_pages(action_from_tool(name), arguments, blocked)
                return blocked
        # 관문 검사와 등록 사이에 await 없음 — 전환은 등록된 호출이 끝나길 기다린다(_drain_calls).
        self._inflight += 1
        navigation_scope = AsyncExitStack()
        navigation_token = self._navigation_scope.set(navigation_scope)
        try:
            if self._window is not None and action_from_tool(name) is ActionType.WAIT_FOR:
                result = await self._call_wait_tool(name, arguments, capture)
            else:
                result = await self._call_tool(name, arguments, capture)
            if capture and capture["gen"] != self._pixel_gen:
                return self._pixels_refused(ActionType.TAKE_SCREENSHOT)
            action = action_from_tool(name)
            # WS-61 R4: 조작권·HITL·검증 거부 같은 조기 반환을 포함한 모든 결과 경로에서 pending 초기화·소비.
            self._note_saved_pages(action, arguments, result)
            if action is ActionType.TAB_CONTROL and result.success and "tabs" in result.data:
                self._note_other_workspaces(result)  # WS-57: 다른 작업 공간 탭은 개수만
            if result.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED:
                result.data["login_hint"] = self._login_hint()
            if action is not None:
                await self._attach_observation_guidance(action, arguments or {}, result)
            if action in {ActionType.NAVIGATE, ActionType.OBSERVE_PAGE}:
                await self._attach_robots(result)
            if action in CHALLENGE_CHECK_ACTIONS and "document" not in result.data:
                await self._attach_block_hint(result)
            # WS-36: 결과에 실린 웹 유래 텍스트(관찰 요소 이름·제목, 추출 텍스트·속성, 다이얼로그
            # 문구, 차단 결과의 대상 이름·오류 문구 …)에 주입 문구가 있으면 data.injection_suspected
            # 신호를 붙인다 — 차단·수정하지 않는다. 모든 반환 경로(HITL 차단 등 조기 반환 포함)를
            # 덮으려고 여기서 한 번 한다. 자르기 전에 붙여 신호까지 크기 상한 안에 들게 한다.
            from security.injection_signal import attach_injection_signal

            # 검사 입력 상한 = 결과 크기 상한의 10배(WS-36 R1 NB2) — 자르기가 앞쪽을 남기므로 돌려주는
            # 부분은 늘 검사 범위 안이다.
            scan_chars = 10 * self.max_result_chars
            if action is ActionType.OBSERVE_PAGE and "saved_pages" in result.data:
                from recipes.saved_pages import fit_saved_pages

                # WS-61: 기억한 페이지 후보가 크기 상한에서 페이지 요소를 밀어내지 않게 — 넘으면 후보를 뺀다.
                # R3: 주입 신호까지 붙인 최종 크기로 잰다(신호는 후보를 뺀 뒤 원본에 다시 계산).
                fit_saved_pages(result, self.max_result_chars,
                                finalize=lambda r: attach_injection_signal(r, max_scan_chars=scan_chars))

            attach_injection_signal(result, max_scan_chars=scan_chars)
            # WS-30b: 큰 페이지 결과가 클라이언트 도구 결과 한도를 넘지 않게 항목 경계에서 자른다.
            cap_result_size(result, self.max_result_chars)
            if self._window is not None and (self._window_unreported or self._recovered_notice):
                # WS-50: 꺼진 브라우저 복구는 '창 전환 실패 뒤 복구' 와 다른 알림(browser_recovered)으로 싣는다.
                browser_recovered = self._recovered_notice == self._BROWSER_RECOVERED
                if browser_recovered:
                    self._recovered_notice = False
                self._attach_window_report(result)
                if browser_recovered:
                    self._mark_browser_recovered(result)
            if self._window is not None:
                self._note_tabs()  # WS-50: 브라우저가 꺼지면 복원할 마지막 탭 URL
            return result
        finally:
            try:
                # Same lifetime as the recursive first retry, including MCP postprocessing.
                await navigation_scope.aclose()
            finally:
                self._navigation_scope.reset(navigation_token)
                self._inflight -= 1
                if capture:
                    self._captures_inflight -= 1

    async def _call_wait_tool(self, name: str, arguments: Dict[str, Any],
                              capture: Dict[str, Any]) -> ActionResult:
        """on-demand 의 wait_for — 창 전환이 시작되면 대기를 끝내고 E_TIMEOUT 으로 돌려준다(R1 NB-5).

        wait_for 의 timeout_ms 는 상한이 없어 전환이 진행 중 호출을 기다리는 상한(DRAIN_TIMEOUT_S)보다 길 수
        있다 — 긴 대기 때문에 사람 호출(control_request)이 실패하지 않게, 전환(_drain_calls)이 이 대기를
        취소한다. 바깥 취소(클라이언트 취소·종료)는 그대로 올린다.
        """
        task = asyncio.get_running_loop().create_task(self._call_tool(name, arguments, capture))
        self._wait_calls.add(task)
        try:
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                raise
            if task.cancelled():
                w = self._window
                return self._error_result(
                    ActionType.WAIT_FOR, ErrorCode.TIMEOUT,
                    "브라우저 창 전환(사람 호출 등) 때문에 대기를 멈췄습니다 — 전환 뒤 다시 관찰하고 "
                    "필요하면 다시 기다리세요.",
                    data={"window": w.info() if w is not None else {}},
                )
            return task.result()
        finally:
            self._wait_calls.discard(task)

    async def _call_tool(self, name: str, arguments: Dict[str, Any],
                         capture: Dict[str, Any]) -> ActionResult:
        """MCP 툴 호출을 디스패처로 라우팅한다.

        실패는 예외가 아니라 `ActionResult`로 반환해 클라이언트가
        구조화된 정보를 받도록 한다.
        """
        action = action_from_tool(name)
        if action is None:
            return self._error_result(
                ActionType.OBSERVE_PAGE,
                ErrorCode.FEATURE_NOT_IMPLEMENTED,
                f"알 수 없는 툴: {name}",
            )

        if not self._started:
            await self.start()
        await self._ensure_workspace()  # WS-57: 기본 외 작업 공간은 자기 탭·엔진·디스패처

        params: Dict[str, Any] = dict(arguments or {})
        # WS-29: 승인 증표 id 는 계약 밖 인자 — 계약 입력 검증 전에 떼어 낸다.
        approval_id = params.pop("approval_id", None)
        refused = await self._refuse_before_validation(action, params, capture)
        if refused is not None:
            return refused
        checked = self._validated_params(action, params)
        if isinstance(checked, ActionResult):
            return checked
        params = checked

        # WS-57 R2(B1): 같은 작업 공간의 게이트·디스패치는 한 번에 하나 — 디스패처가 호출 임시 상태(_dialogs·_nav_info·
        # _opened_pages·_top_attribution)를 인스턴스에 두고 dispatch 입구에서 초기화하므로, 겹치면 남의 alert·이동이 내
        # 효과로 읽혔다. 다른 작업 공간끼리는 동시. 조작권·확인 코드 거부는 위에서 기다리지 않고 바로 돌려준다
        # (캡처는 이미 등록돼 코드 표시가 기다린다). 기다렸다면 그사이 사람이 조작권을 잡았을 수 있어 다시 본다.
        serial = self._dispatch_serial()
        async with serial:
            if serial.waited:
                if self.hub.opened:
                    await self._poll_handoff()
                refused = self._control_refusal(action, params)
                if refused is not None:
                    return refused
            self._observation_state()

            # HITL 게이트: 고위험 액션은 모드에 따라 차단하거나 승인을 요구한다.
            self._pending_gate_basis = None
            self._used_approval = None
            self._approval_via = None
            self._approval_pin = None
            # WS-56 R4(WS-56 R2 B4·R3 B6 를 이 구간으로 통일): 승인 증표·정책·elicitation 으로 연 호출과 일반 호출 모두
            # 재확인 → 실제 입력 → 효과 판정을 이 작업 공간 직렬화 구간 안에서 한다(서버 단위 별도 락 없음). 사람 응답
            # 대기(elicitation)만 구간 밖 — _out_of_band 가 잠시 놓았다가(_SerialPause) 응답 뒤 다시 잡고 재판정한다.
            blocked = await self._check_hitl(action, params, approval_id=approval_id)
            if blocked is not None:
                await self._drop_approval_pin()
                return blocked
            return await self._run_gated_action(name, arguments, action, params)

    async def _refuse_before_validation(self, action: ActionType, params: Dict[str, Any],
                                        capture: Dict[str, Any]) -> Optional[ActionResult]:
        """조작권(사람)·확인 코드 표시 중 화면 캡처 거부. 통과한 스크린샷은 캡처로 등록한다."""
        # WS-29: 사람이 조작권을 가진 동안(또는 비밀 입력 요청 중) 에이전트 액션 거부.
        if self.hub.opened:
            await self._poll_handoff()
        refused = self._control_refusal(action, params)
        if refused is not None:
            return refused
        if action is ActionType.TAKE_SCREENSHOT:
            if self._pixels_blocked():
                # WS-29 R1: 사람용 확인 코드가 창 오버레이에 떠 있다(또는 띄울 예정). headed 창에서는
                # 오버레이가 스크린샷에 찍히므로(실측) 그동안 화면 픽셀(일반·전체·SoM)을 주지 않는다.
                return self._pixels_refused(action)
            # WS-29 R2: 이 캡처를 등록한다(검사와 등록 사이에 await 없음). 코드 표시는 등록된 캡처가
            # 끝나길 기다리고, call_tool 은 반환 직전 세대 번호로 캡처 도중 오버레이가 켜졌는지 본다.
            self._captures_inflight += 1
            capture["gen"] = self._pixel_gen
        return None

    def _control_refusal(self, action: ActionType, params: Dict[str, Any]) -> Optional[ActionResult]:
        """사람이 조작권을 가진 동안(또는 비밀 입력 요청 중) 에이전트 액션 거부 결과. 막지 않으면 None."""
        control = self.hub.control_blocks(action, params)
        if control is None:
            return None
        if self._recipes is not None:
            self._recipes.reset("human_control")
        what = "관찰을 포함한 모든 액션" if control["secret_wanted"] else "조작 액션"
        return self._error_result(
            action,
            ErrorCode.HITL_UNATTENDED_BLOCKED,
            f"사람이 브라우저를 다루는 중이라 {what}을 거부했습니다({control['reason']}). "
            "browser_control_wait 로 반납을 기다린 뒤 다시 관찰하세요.",
            data={"control": control},
        )

    def _validated_params(self, action: ActionType, params: Dict[str, Any]) -> Any:
        """계약 입력 검증 + 디스패처 키 정규화. 반환: 검증된 params(dict) 또는 실패 ActionResult."""
        # 입력 검증: 계약 모델로 파싱해 잘못된 인자를 조기 차단한다.
        model = ACTION_INPUT_MAP.get(action)
        to_main = False
        if action is ActionType.SWITCH_FRAME and "to_main" in params:
            # WS-30 추가 B: 디스패처의 메인 복귀(to_main)는 계약(동결)에 없는 키라 검증에서
            # 버려지고 '정확히 하나' 검증에 걸려 MCP 로는 복귀할 수 없었다 — 경계 특례.
            to_main = bool(params.pop("to_main"))
            if to_main and any(params.get(k) for k in ("frame_selector", "shadow_root_selector")):
                return self._error_result(
                    action,
                    ErrorCode.FRAME_NOT_FOUND,
                    "to_main 과 frame_selector/shadow_root_selector 는 함께 지정할 수 없습니다.",
                )
        if to_main:
            params = {"to_main": True}
        elif model is not None:
            try:
                validated = model(**params)
                params = validated.model_dump(exclude_none=True)
            except Exception as exc:  # noqa: BLE001 - Pydantic ValidationError 등
                return self._error_result(
                    action,
                    ErrorCode.ELEMENT_NOT_FOUND
                    if "element_id" in str(exc)
                    else ErrorCode.INVALID_URL,
                    f"입력 검증 실패: {exc}",
                )
        # WS-30 항목 2: 계약 키(trigger_element_id 등)를 디스패처 키로 — 게이트도 같은 키를 본다.
        from actions.dispatcher import normalize_action_params

        return normalize_action_params(action, params)

    def _note_saved_pages(self, action: Optional[ActionType], arguments: Dict[str, Any], result: ActionResult) -> None:
        """WS-61: 외부 navigate/observe 결과마다(성공·거부 무관) uses pending 초기화·소비 + 관찰 후보 부착.

        uses 는 navigate 1회당 첫 observe 에서 최대 +1. 헤드리스 재시도의 내부 호출(_retry_recipe_boundary)은 건너뛴다
        — 바깥 navigate 의 최종 결과(재시도 결과 포함)로 한 번만 판정한다.
        """
        saved = getattr(self, "_saved_pages", None)
        if saved is None or self._retry_recipe_boundary.get():
            return
        if action is ActionType.NAVIGATE:
            saved.note_navigate(dict(arguments or {}), result)
        elif action is ActionType.OBSERVE_PAGE:
            saved.observe(result)

    async def _run_gated_action(self, name: str, arguments: Dict[str, Any], action: ActionType,
                                params: Dict[str, Any]) -> ActionResult:
        """게이트를 통과한 액션 실행 → 신호 부착 → 헤드리스 재시도 → 레시피 기록."""
        # WS-38: 레시피 궤적 기록용 스냅숏(기록하는 동작만, 한 번의 evaluate).
        retry_boundary = action is ActionType.NAVIGATE and self._retry_recipe_boundary.get()
        recipe_pre = await self._recipes.pre(action, params) if self._recipes is not None and not retry_boundary else None
        # 누적 수로 이번 호출의 신규분을 센다(기록은 상한 deque 라 길이로는 못 센다, WS-29b R1).
        blocks_before = self._egress.blocked_total if self._egress is not None else 0
        upstream_before = self._egress.upstream_total if self._egress is not None else 0
        # WS-38 R1 NB-3: 클릭한 링크의 주소(실행 전 핸들) — 클릭으로 나간 이동의 차단을 알아보는 데 쓴다.
        target_href = self._handle_href(params)
        used, self._used_approval = self._used_approval, None
        # WS-56: 정책·elicitation 으로 연 호출도 승인한 그 요소만 — 치유 끔, 결과에 via·outcome.
        via, self._approval_via = self._approval_via, None
        # WS-58 R1 B2: 입력 대상 문서(활성 frame)의 주소를 실행 전에 잡는다 — 디스패처가 비밀 치환을 허용한 문서.
        input_url = (self._agent_input_url() if action is ActionType.TYPE_TEXT and hasattr(self, "_agent_input_url")
                     else "")
        result, remembered = await self._dispatch_with_approval(action, params, used, heal_off=via is not None)
        if (action is ActionType.TYPE_TEXT and result.success and result.data.get("secret_resolved") is True
                and hasattr(self, "_record_login")):
            # WS-58: 자격증명 채우기(디스패처가 키를 값으로 치환해 입력함)는 도구가 아는 로그인 사건.
            from browser.login_ledger import SOURCE_LOGIN_FLOW

            self._record_login(input_url or self._agent_input_url() or self._agent_page_url(), SOURCE_LOGIN_FLOW)
        if used is not None:
            self._attach_approval_outcome(used, result)
        elif via is not None:
            self._attach_via_outcome(via, result)
        result = self._attach_egress_block(action, params, result, blocks_before, target_href=target_href)
        result = self._attach_upstream_failure(action, params, result, upstream_before)
        if self._pending_gate_basis is not None:
            result.data.setdefault("gate_basis", self._pending_gate_basis)
            self._pending_gate_basis = None
        if action in CHALLENGE_CHECK_ACTIONS and "document" not in result.data:
            await self._attach_challenge(result)
        self._attach_remembered_identity(remembered, result)
        if action is ActionType.NAVIGATE and "document" not in result.data:
            retried = await self._retry_headless_navigation(name, arguments, params, result)
            if retried is not None:
                result = retried  # original pre-snapshot + final result form one recipe boundary
        if self._recipes is not None and not retry_boundary:
            # WS-38: 성공 + 사후 확인 통과만 궤적에(승인 재생·실패·치유는 끊음), 관찰에는 후보.
            if action is ActionType.NAVIGATE and result.data.get("challenge") is not None:
                self._recipes.reset("navigation_blocked")
            else:
                self._recipes.post(action, params, recipe_pre, result, used is not None or via is not None)
            if action is ActionType.OBSERVE_PAGE:
                await self._recipes.observe(result)
        # (WS-61 uses·관찰 후보는 _route_tool_call 의 _note_saved_pages — 조기 반환 경로까지 덮는다.)
        # (WS-36: 크기 상한은 call_tool 에서 — 주입 신호를 붙인 뒤 자른다.)
        return result

    async def _dispatch_with_approval(self, action: ActionType, params: Dict[str, Any],
                                      used: Optional[str], heal_off: bool = False) -> tuple:
        """디스패처 실행(승인 증표·정책·elicitation 호출이면 치유 끔). 반환: (결과, 기억된 이름 재시도 시도 또는 None)."""
        no_heal = used is not None or heal_off
        # NB-1: 승인한 그 요소만 — 자가 치유(유사 이름 대체)를 이 호출 동안 끈다. WS-56 R1 B2: 호출 문맥 단위
        # (heal_disabled_scope) — 같은 디스패처의 다른 동시 호출이 끝나며 이 호출의 금지를 풀지 않는다.
        # B1: 사람 승인 때 잡은 그 노드에만 동작(pinned_scope) — 같은 css 의 새 노드로 바뀌면 디스패처가 거부.
        heal_guard = self._heal_guard(no_heal)
        pin = self._approval_pin if no_heal else None
        pin_guard = self._pin_guard(pin)
        try:
            with heal_guard, pin_guard:
                scope = self._navigation_scope.get()
                remembered = (await scope.enter_async_context(self._present_remembered_navigation(action, params))
                              if scope is not None else None)
                result = await self._pdf.dispatch(self._dispatcher, action, params, self._egress,
                                                 self._egress_proxy, self._robots, self.max_result_chars)
        except BaseException:
            if used is not None:
                self.hub.set_outcome(used, "outcome_unknown")
            raise
        finally:
            await self._drop_approval_pin()
        return result, remembered

    def _heal_guard(self, on: bool) -> Any:
        """승인 호출 동안 자가 치유 끄기(호출 문맥 단위, WS-57 R1 R2 와 같은 모양). 디스패처가 scope 를 모르면 예전 방식."""
        import contextlib

        disp = self._dispatcher
        if not on or disp is None:
            return contextlib.nullcontext()
        scope = getattr(disp, "heal_disabled_scope", None)
        if callable(scope):
            return scope()

        @contextlib.contextmanager
        def legacy() -> Any:
            disp.heal_disabled = True
            try:
                yield
            finally:
                disp.heal_disabled = False

        return legacy()

    def _pin_guard(self, pin: Any) -> Any:
        """승인 때 잡은 노드로 디스패처 실행을 묶는다(WS-56 R1 B1). 잡은 노드가 없거나 디스패처가 모르면 그대로."""
        import contextlib

        scope = getattr(self._dispatcher, "pinned_scope", None)
        if pin is None or not callable(scope):
            return contextlib.nullcontext()
        return scope(pin)

    def _error_result(
        self,
        action: ActionType,
        code: ErrorCode,
        message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> ActionResult:
        return ActionResult(
            success=False,
            action=action,
            current_url=self._page.url if self._page else "",
            snapshot_epoch=self._engine.epoch if self._engine else 0,
            # 유일한 탭이 닫히면 활성 탭이 None 이다(R3 NB-2) — 계약은 str.
            tab_id=(self._core.active_tab_id if self._core else "") or "",
            healed=False,
            reobserve_required=False,
            retry_safe=True,
            error_code=code,
            error_message=message,
            data=data or {},
        )
