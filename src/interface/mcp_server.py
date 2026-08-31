"""MCP 서버 (PRD §6.3, Gate 3-B 항목 3).

19종 액션 툴을 RFC 표준 MCP(JSON-RPC 2.0)로 외부 에이전트에 노출한다.

설계 원칙:
* **툴 목록을 손으로 쓰지 않는다.** `ACTION_INPUT_MAP`에서 자동 생성하므로
  계약에 액션이 추가되면 툴도 자동으로 늘어난다. 수동 목록은 계약과
  어긋나도 아무도 모르게 되므로 금지한다.
* **입력 스키마도 Pydantic에서 생성한다.** 각 액션의 Input 모델이
  `model_json_schema()`로 JSON Schema를 제공한다.
* **에러는 예외가 아니라 ActionResult로 반환한다.** MCP 클라이언트가
  구조화된 실패 정보를 받아야 재시도 판단이 가능하다.

세션 수명주기:
MCP 서버는 stdio로 구동되며 클라이언트 연결당 하나의 브라우저 세션을
유지한다. 첫 툴 호출 시 지연 초기화하고, 서버 종료 시 정리한다.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from contracts import (
    ACTION_INPUT_MAP,
    ActionResult,
    ActionType,
    ErrorCode,
    ExecutionMode,
    thresholds,
)

logger = logging.getLogger(__name__)

#: 툴 이름 접두사. MCP 클라이언트에서 다른 서버와 충돌하지 않도록 한다.
TOOL_PREFIX = "browser_"


def tool_name(action: ActionType) -> str:
    """ActionType -> MCP 툴 이름."""
    return f"{TOOL_PREFIX}{action.value}"


def action_from_tool(name: str) -> Optional[ActionType]:
    """MCP 툴 이름 -> ActionType (역변환)."""
    if not name.startswith(TOOL_PREFIX):
        return None
    raw = name[len(TOOL_PREFIX) :]
    try:
        return ActionType(raw)
    except ValueError:
        return None


def build_tool_schema(action: ActionType) -> Dict[str, Any]:
    """단일 액션의 MCP 툴 정의를 계약에서 생성한다."""
    model = ACTION_INPUT_MAP.get(action)
    if model is None:
        # 입력이 없는 액션도 빈 스키마로 노출한다.
        schema: Dict[str, Any] = {"type": "object", "properties": {}}
    else:
        schema = model.model_json_schema()

    return {
        "name": tool_name(action),
        "description": _describe(action),
        "inputSchema": schema,
    }


def build_all_tools() -> List[Dict[str, Any]]:
    """19종 툴 정의 전체를 생성한다.

    `ActionType`을 순회하므로 계약에 액션이 추가되면 자동 반영된다.
    """
    return [build_tool_schema(action) for action in ActionType]


_DESCRIPTIONS: Dict[ActionType, str] = {
    ActionType.OBSERVE_PAGE: (
        "현재 페이지를 관찰해 상호작용 가능한 요소 목록을 반환합니다. "
        "각 요소에는 이후 액션에서 사용할 element_id가 부여됩니다."
    ),
    ActionType.TAKE_SCREENSHOT: "현재 페이지의 스크린샷을 캡처합니다.",
    ActionType.NAVIGATE: "지정한 URL로 이동합니다. snapshot_epoch가 증가합니다.",
    ActionType.GO_BACK: "브라우저 히스토리에서 이전 페이지로 이동합니다.",
    ActionType.RELOAD: "현재 페이지를 새로고침합니다.",
    ActionType.CLICK: "지정한 요소를 클릭합니다. 실행 전 요소 유효성을 검증합니다.",
    ActionType.TYPE_TEXT: "입력 필드에 텍스트를 입력합니다.",
    ActionType.SELECT_OPTION: "드롭다운에서 옵션을 선택합니다.",
    ActionType.CHECK_BOX: "체크박스나 라디오 버튼을 토글합니다.",
    ActionType.SCROLL: "뷰포트를 스크롤합니다. 동적 로딩 시 재관찰이 필요할 수 있습니다.",
    ActionType.HOVER: "요소 위에 마우스를 올려 툴팁이나 서브메뉴를 노출시킵니다.",
    ActionType.PRESS_KEY: "키보드 이벤트를 발생시킵니다 (Enter, Tab, Escape 등).",
    ActionType.WAIT_FOR: "지정한 조건이 만족될 때까지 대기합니다.",
    ActionType.EXTRACT: "CSS 셀렉터로 텍스트와 속성을 추출합니다.",
    ActionType.SWITCH_FRAME: "iframe 또는 Shadow DOM 컨텍스트로 전환합니다.",
    ActionType.HANDLE_DIALOG: "Alert/Confirm/Prompt 다이얼로그를 처리합니다.",
    ActionType.UPLOAD_FILE: "파일 입력 필드에 파일을 바인딩합니다.",
    ActionType.DOWNLOAD_FILE: "다운로드를 트리거하고 파일을 저장합니다.",
    ActionType.TAB_CONTROL: "탭을 생성/전환/종료하거나 목록을 조회합니다.",
}


def _describe(action: ActionType) -> str:
    return _DESCRIPTIONS.get(action, f"{action.value} 액션을 실행합니다.")


class BrowserMCPServer:
    """19종 툴을 노출하는 MCP 서버.

    실제 MCP SDK 바인딩은 `create_server()`에서 수행하고, 본 클래스는
    툴 호출을 디스패처로 라우팅하는 순수 로직만 담당한다. 그래야
    MCP 런타임 없이도 단위 테스트가 가능하다.
    """

    def __init__(
        self,
        *,
        mode: ExecutionMode = ExecutionMode.UNATTENDED,
        allowed_domains: tuple = (),
        pre_approved_actions: tuple = (),
        headless: bool = True,
    ) -> None:
        self.mode = mode
        self.allowed_domains = allowed_domains
        self.pre_approved_actions = pre_approved_actions
        self.headless = headless

        self._core: Any = None
        self._engine: Any = None
        self._dispatcher: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._hitl: Any = None
        self._egress: Any = None
        self._started = False

    # -- 수명주기 -----------------------------------------------------------

    async def start(self) -> None:
        """브라우저 세션과 파이프라인을 초기화한다."""
        if self._started:
            return

        from actions import ActionDispatcher, DispatchContext
        from browser import BrowserCore
        from perception import PerceptionEngine
        from security import EgressGuard, EgressPolicy, HITLGate

        self._core = await BrowserCore(headless=self.headless).start()
        await self._core.new_context("mcp-session")
        tab = await self._core.new_tab("mcp-session")
        self._page = tab.page
        self._cdp = await self._core.new_cdp_session(tab.tab_id)

        self._engine = PerceptionEngine()
        self._dispatcher = ActionDispatcher(
            DispatchContext(
                page=self._page,
                engine=self._engine,
                cdp=self._cdp,
                tab_id=tab.tab_id,
                core=self._core,  # tab_control이 탭 수명주기에 접근하려면 필요
            )
        )

        self._egress = EgressGuard(
            allowed_domains=self.allowed_domains,
            policy=(
                EgressPolicy.STRICT
                if self.allowed_domains
                else EgressPolicy.OPEN_SANDBOX
            ),
            allow_loopback=True,  # 로컬 Mock/개발 서버 허용
        )
        await self._egress.install(self._core.context_for("mcp-session"))

        self._hitl = HITLGate(
            mode=self.mode, pre_approved_actions=self.pre_approved_actions
        )
        self._started = True
        logger.info("MCP 브라우저 세션 시작 (mode=%s)", self.mode.value)

    async def close(self) -> None:
        if self._core is not None:
            await self._core.close()
        self._started = False

    async def __aenter__(self) -> "BrowserMCPServer":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        await self.close()

    # -- 툴 호출 ------------------------------------------------------------

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> ActionResult:
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

        # 입력 검증: 계약 모델로 파싱해 잘못된 인자를 조기 차단한다.
        model = ACTION_INPUT_MAP.get(action)
        params: Dict[str, Any] = dict(arguments or {})
        if model is not None:
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

        # HITL 게이트: 고위험 액션은 모드에 따라 차단하거나 승인을 요구한다.
        blocked = await self._check_hitl(action, params)
        if blocked is not None:
            return blocked

        return await self._dispatcher.dispatch(action, params)

    async def _check_hitl(
        self, action: ActionType, params: Dict[str, Any]
    ) -> Optional[ActionResult]:
        """고위험 액션 승인 게이트. 차단 시 ActionResult를 반환한다."""
        from security import ActionContext

        element_name = ""
        element_id = params.get("element_id")
        if element_id and self._engine is not None:
            handle = self._engine.get_handle(element_id)
            if handle is not None:
                element_name = handle.name

        decision = self._hitl.evaluate(
            ActionContext(
                action=action,
                element_name=element_name,
                selector=params.get("selector", "") or "",
                domain=self._current_domain(),
                submits_form=bool(params.get("press_enter")),
            )
        )
        if decision.allowed:
            return None

        message = decision.reason
        if decision.requires_confirmation and decision.dialog is not None:
            # 대화형 모드: 클라이언트가 렌더링할 정형 모달을 함께 전달한다.
            message = decision.dialog.message

        return self._error_result(
            action,
            decision.error_code or ErrorCode.HITL_UNATTENDED_BLOCKED,
            message,
            data={
                "requires_confirmation": decision.requires_confirmation,
                "risk": decision.risk.value,
                "dialog": (
                    decision.dialog.model_dump(mode="json") if decision.dialog else None
                ),
            },
        )

    def _current_domain(self) -> str:
        try:
            from urllib.parse import urlparse

            return urlparse(self._page.url).hostname or ""
        except Exception:  # noqa: BLE001
            return ""

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
            tab_id=self._core.active_tab_id if self._core else "",
            healed=False,
            reobserve_required=False,
            retry_safe=True,
            error_code=code,
            error_message=message,
            data=data or {},
        )

    # -- 조회 ---------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    def list_tools(self) -> List[Dict[str, Any]]:
        return build_all_tools()


def create_server(
    *,
    mode: ExecutionMode = ExecutionMode.UNATTENDED,
    allowed_domains: tuple = (),
    pre_approved_actions: tuple = (),
):
    """MCP SDK에 바인딩된 서버 인스턴스를 생성한다.

    `mcp` 패키지가 없는 환경에서도 나머지 모듈이 import되도록
    지연 import한다.
    """
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    backend = BrowserMCPServer(
        mode=mode,
        allowed_domains=allowed_domains,
        pre_approved_actions=pre_approved_actions,
    )

    def _build_tools() -> List[Tool]:
        """툴 정의를 SDK 타입으로 변환한다.

        `Tool`의 스키마 필드는 SDK 메이저에 따라 다르다.
        - mcp 1.x: `inputSchema`
        - mcp 2.x: `input_schema` (JSON alias는 inputSchema)

        한쪽만 지원하면 다른 메이저에서 tools/list가 통째로 실패한다.
        실제 필드를 조회해 맞춘다 — 버전 문자열 분기는 프리릴리스나
        포크에서 어긋난다.
        """
        field = "input_schema" if "input_schema" in Tool.model_fields else "inputSchema"
        out: List[Tool] = []
        for spec in build_all_tools():
            kwargs = {
                "name": spec["name"],
                "description": spec["description"],
                field: spec["inputSchema"],
            }
            out.append(Tool(**kwargs))
        return out

    async def _list_tools_impl() -> List[Tool]:
        return _build_tools()

    async def _call_tool_impl(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
        result = await backend.call_tool(name, arguments)
        return [TextContent(type="text", text=result.model_dump_json())]

    # SDK 메이저별 등록 방식이 다르다. 2.x의 lowlevel Server에는
    # list_tools/call_tool 데코레이터가 없고 생성자 콜백을 받는다.
    # 실측 — 데코레이터만 쓰면 create_server()가 AttributeError로 즉사해
    # `agent-browser serve` 경로 전체가 막힌다.
    if hasattr(Server("__probe__"), "list_tools"):
        # mcp 1.x — 데코레이터 등록
        server = Server("agent-browser")
        server.list_tools()(_list_tools_impl)  # type: ignore[attr-defined]
        server.call_tool()(_call_tool_impl)  # type: ignore[attr-defined]
        return server, backend

    # mcp 2.x — 생성자 콜백 등록
    from mcp import types as mcp_types

    async def _on_list_tools(ctx: Any, params: Any) -> Any:
        return mcp_types.ListToolsResult(tools=_build_tools())

    async def _on_call_tool(ctx: Any, params: Any) -> Any:
        content = await _call_tool_impl(params.name, params.arguments or {})
        return mcp_types.CallToolResult(content=list(content))

    server = Server(
        "agent-browser",
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )
    return server, backend


async def run_stdio(
    *,
    mode: ExecutionMode = ExecutionMode.UNATTENDED,
    allowed_domains: tuple = (),
) -> None:
    """stdio 트랜스포트로 MCP 서버를 구동한다."""
    from mcp.server.stdio import stdio_server

    server, backend = create_server(mode=mode, allowed_domains=allowed_domains)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )
    finally:
        await backend.close()
