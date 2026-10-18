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

import asyncio
import io
import json
import logging
import numbers
import os
import re
import signal
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
from weakref import WeakSet

from playwright.async_api import Frame
from browser.headless_retry import RetryRequest

from contracts import (
    ACTION_INPUT_MAP,
    ActionResult,
    ActionType,
    ErrorCode,
    ExecutionMode,
    thresholds,
)

logger = logging.getLogger(__name__)

# WS-48: 책임별 모듈로 옮긴 이름 — 기존 `from interface.mcp_server import X` 를 그대로 지원한다.
from interface.server_common import (  # noqa: E402,F401
    _iso,
    _stderr_line,
    _wait_timeout,
)
from interface.server_schema import (  # noqa: E402,F401
    CHALLENGE_CHECK_ACTIONS,
    CHALLENGE_NOTE,
    CHALLENGE_NOTE_HOME,
    CHALLENGE_REF,
    OBSERVE_INSTRUCTIONS,
    RECIPE_INSTRUCTIONS,
    RECIPE_TOOL,
    SERVER_TOOLS,
    TOOL_PREFIX,
    YOUTUBE_INSTRUCTIONS,
    YOUTUBE_TOOL,
    _DESCRIPTIONS,
    _SCHEMA_VALUE_KEYS,
    _WAIT_SCHEMA,
    _describe,
    action_from_tool,
    build_all_tools,
    build_listed_tools,
    build_server_tools,
    build_tool_schema,
    compact_schema,
    tool_name,
)
from interface.server_envelope import (  # noqa: E402,F401
    DEFAULT_MAX_RESULT_CHARS,
    ENVELOPE_ALWAYS,
    ENVELOPE_RULE,
    MIN_MAX_RESULT_CHARS,
    _TRUNC_HINT_EXTRACT,
    _TRUNC_HINT_OBSERVE,
    _TRUNC_HINT_TEXT,
    _compact_observation,
    _field_default,
    _safe_cut,
    cap_result_size,
    envelope_dict,
    envelope_json,
)
from interface.server_signals import (  # noqa: E402,F401
    SignalsMixin,
    UNOBSERVED_HINT,
    _PageObservation,
)
from interface.server_hitl import (  # noqa: E402,F401
    HitlMixin,
    _BUTTON_INPUT_TYPES,
    _ENTER_SUBMITS_INPUT_TYPES,
    _UNCERTAIN_CODES,
    _signals_of,
    approve_hint_text,
    blocked_hint_text,
    pre_approve_hint,
)
from interface.server_display import (  # noqa: E402,F401
    DisplayMixin,
    code_overlay_text,
)
from interface.server_handoff import (  # noqa: E402,F401
    HandoffMixin,
    _RELEASE_HINT,
)
from interface.server_window import (  # noqa: E402,F401
    WindowMixin,
)
from interface.server_lifecycle import (  # noqa: E402,F401
    LifecycleMixin,
)
from interface.server_tools import (  # noqa: E402,F401
    ToolsMixin,
)
#: 분리 전 모듈 __annotations__ 를 그대로 둔다(값 바인딩 없는 주석 — SERVER_TOOLS 는 위 재수출).
SERVER_TOOLS: Dict[str, Dict[str, Any]]


from interface.handoff import DEFAULT_APPROVAL_TTL_S, WAIT_DEFAULT_S, WAIT_MAX_S  # noqa: E402
from interface.handoff import SITE_NAME_LABEL  # noqa: E402


#: WS-29 R2: 확인 코드를 띄우기 전, 이미 진행 중인 화면 캡처가 끝나길 기다리는 상한(초). 넘으면
#: 표시를 취소한다(코드 무효 — fail-closed). 무거운 페이지 SoM 캡처 실측 수백 ms 의 10배 이상.
CAPTURE_DRAIN_TIMEOUT_S = 5.0
#: WS-37: 조작권 반납 때 진행 중인 닫힌 탭 복구(CDP 세션 생성 등)를 기다리는 상한(초).
RELEASE_RECOVERY_WAIT_S = 10.0


class BrowserMCPServer(ToolsMixin, HitlMixin, SignalsMixin, DisplayMixin, HandoffMixin, WindowMixin, LifecycleMixin):
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
        secrets: Any = None,
        som_enabled: bool = False,
        browser_mode: str = "headless",
        chrome_profile: Any = None,
        keep_open: bool = False,
        nav_settle: bool = True,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
        allow_private_network: bool = False,
        block_loopback: bool = False,
        approval_ttl_s: float = DEFAULT_APPROVAL_TTL_S,
        handoff_root: Any = None,
        profile: Optional[str] = None,
        recipes: bool = True,
        headless_retry: bool = True,
    ) -> None:
        #: 이름 붙인 영속 프로필(WS-32, serve --profile). None 이면 기존 동작(빈 컨텍스트).
        if profile is not None:
            from browser import serve_profile

            serve_profile.validate_name(profile)
            if browser_mode == "user-chrome":
                raise ValueError(
                    "--profile 과 --browser user-chrome 은 함께 쓸 수 없습니다 — user-chrome 은 "
                    "이미 전용 영속 프로필(--chrome-profile)을 씁니다."
                )
        self.profile = profile
        self._profile_lock: Any = None
        #: WS-38 동작 캐시(레시피). 기본 켬(serve --no-recipes 로 끔). --profile 이면 프로필 폴더의
        #: recipes.json(0600), 없으면 메모리만.
        self.recipes_enabled = bool(recipes)
        self._recipes: Any = None
        if self.recipes_enabled:
            from recipes.service import RecipeService

            self._recipes = RecipeService(self)
        self._init_window_fields(browser_mode)
        self._init_handoff_fields(browser_mode, profile, handoff_root, approval_ttl_s)
        #: Egress 대역 옵션 (WS-29b). 기본: 루프백 허용, 사설·링크로컬·CGNAT·ULA 차단.
        self.allow_private_network = allow_private_network
        self.block_loopback = block_loopback
        #: 이동 대기 스위치 (WS-28). DispatchContext.nav_settle 로 전달된다.
        self.nav_settle = nav_settle
        #: observe_page·extract 응답 봉투 크기 상한(글자, WS-30b). cap_result_size 참조.
        self.max_result_chars = max_result_chars
        #: 자격증명 플레이스홀더 해석기 (PRD 5.3). 디스패처에 주입되어
        #: type_text의 키를 실제 값으로 바꾼다. LLM에는 키만 노출된다.
        self.secrets = secrets
        #: Tier-2 SoM 게이트 (PRD §8-2). 기본 OFF — 레거시 클라이언트는
        #: annotate_som=True에 E_FEATURE_NOT_IMPLEMENTED를 그대로 받는다.
        self.som_enabled = som_enabled
        self.mode = mode
        self.allowed_domains = allowed_domains
        self.pre_approved_actions = pre_approved_actions
        self.headless = headless
        self.headless_retry = bool(headless_retry)
        from browser.headless_retry import HeadlessRetry

        self._headless_retry = HeadlessRetry()
        self._retry_recipe_boundary: ContextVar[bool] = ContextVar("headless_retry_recipe_boundary", default=False)
        self._navigation_scope: ContextVar[AsyncExitStack | None] = ContextVar("headless_navigation_scope", default=None)
        #: 브라우저 방식 (WS-27): headless / human / user-chrome. 가드·문서 상태·HITL 은
        #: 방식과 무관하게 같다 — 브라우저를 여는 방법만 다르다.
        self.browser_mode = browser_mode
        self.chrome_profile = chrome_profile
        self.keep_open = keep_open

        self._init_session_fields()

    def _init_window_fields(self, browser_mode: str) -> None:
        """__init__ 1부(WS-48 분해): on-demand 창·전환 관문·확인 코드 창 상태."""
        #: WS-34 on-demand 창 상태(다른 방식이면 None — 기존 동작 그대로).
        from interface.on_demand import ON_DEMAND, WindowState

        self._window: Optional[WindowState] = (
            WindowState() if browser_mode == ON_DEMAND else None)
        #: on-demand 를 --profile 없이 띄우면 쓰는 서버 전용 임시 프로필 잠금(종료 때 폴더 삭제).
        self._ephemeral_lock: Any = None
        #: 창 전환 직렬화(WS-34): 전환 동안 새 도구 호출은 관문에서 기다리고, 전환은 진행 중인
        #: 호출(_inflight)이 끝나길 기다린다. 전환끼리는 _switch_lock 으로 한 번에 하나.
        self._gate = asyncio.Event()
        self._gate.set()
        self._gate_holds = 0
        self._inflight = 0
        self._switch_lock = asyncio.Lock()
        self._window_tasks: set = set()
        #: 마지막 전환 결과(window 정보)·전환 횟수 — control_wait 가 그사이 전환을 알아챈다.
        self._last_window_result: Optional[Dict[str, Any]] = None
        self._switch_seq = 0
        #: 에이전트가 아직 듣지 못한 창 전환·반납(WS-34) — 다음 control_wait/도구 결과에 한 번 싣는다.
        self._window_unreported = False
        self._release_unreported = False
        #: 다음 도구 결과에 실을 '실패 뒤 복구함' 표시.
        self._recovered_notice = False
        #: 조작권 요청 때의 탭 URL(창에서 해결한 사이트 판정용).
        self._request_urls: List[str] = []
        #: 현재 컨텍스트가 닫혔음(브라우저 종료 등 — 사람이 창을 닫은 경우 포함).
        self._ctx_closed = False
        #: 승인 확인 코드 별도 창(D1)과 코드가 떠 있는 곳("window"|"overlay").
        self._code_window: Any = None
        #: R1 NB-5: 진행 중인 wait_for 호출(창 전환이 취소해 E_TIMEOUT 으로 끝낸다).
        self._wait_calls: "set[asyncio.Task]" = set()
        self._code_where: Optional[str] = None

    def _init_handoff_fields(self, browser_mode: str, profile: Optional[str], handoff_root: Any,
                             approval_ttl_s: float) -> None:
        """__init__ 2부(WS-48 분해): 사람 인계 통로·안내 띠·승인 증표·화면 캡처 차단 상태."""
        from interface.handoff import HandoffHub, state_root

        #: 사람 인계 통로(조작권·승인 증표, WS-29). 디스크 상태는 open_handoff() 에서 만든다.
        self.hub = HandoffHub(
            root=handoff_root if handoff_root is not None else state_root(),
            browser_mode=browser_mode,
            profile=profile,
            approval_ttl_s=approval_ttl_s,
        )
        self._hub_task: Any = None
        #: 안내 띠를 띄운 (탭 id, CDP 세션) — 같은 세션으로 지운다.
        self._banner: Any = None
        #: 이번 호출에서 쓴 승인 증표 id(결과에 outcome 을 붙인다).
        self._used_approval: Optional[str] = None
        #: 조작권 안내 띠 문구(코드 표시 중에는 미뤄 두었다가 코드를 내린 뒤 다시 띄운다).
        self._control_banner: Optional[str] = None
        #: 창 오버레이에 확인 코드가 떠 있다(또는 내렸는지 확인하지 못했다) — 그동안 화면 캡처 거부.
        #: 코드 평문은 여기에도 두지 않는다(표시 직후 버린다).
        self._code_on_overlay = False
        #: WS-29 R2: 확인 코드 오버레이를 켤 때마다 1 씩(세대 번호) · 진행 중인 화면 캡처 수.
        self._pixel_gen = 0
        self._captures_inflight = 0
        #: 사람이 에이전트 활성 탭을 닫았음(조작권 반납 때 확인, NB-3) — control_wait/status 로 알린다.
        self._tab_notice: Optional[Dict[str, Any]] = None
        #: WS-37: 진행 중인 닫힌 탭 복구(반납 처리). 감시 태스크가 반납을 먼저 집어 복구하는 동안
        #: 다른 호출(control_wait 등)이 복구가 끝나기 전에 '반납됨' 을 알리지 않도록 기다리는 데 쓴다.
        self._release_recovery: Optional["asyncio.Future[None]"] = None

    def _init_session_fields(self) -> None:
        """__init__ 3부(WS-48 분해): 브라우저 세션 핸들·문서 상태·신호 캐시(start() 가 채운다)."""
        self._core: Any = None
        self._engine: Any = None
        self._dispatcher: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._hitl: Any = None
        #: 통과한 좌표 클릭의 판정 근거(WS-31) — 디스패치 결과 data.gate_basis 로 옮긴다.
        self._pending_gate_basis: Optional[Dict[str, Any]] = None
        self._egress: Any = None
        #: 검증 프록시 묶음(WS-29b, security.egress_runtime.EgressRuntime)
        self._egress_runtime: Any = None
        self._started = False
        #: 세션 전역 메인 문서 상태(WS-26). 탭별 기록(_page_status)이 없을 때만 쓴다
        #: (브라우저 없이 디스패처를 바꿔 끼운 단위 테스트 등).
        self._http_status: Dict[str, Any] = {"last_http_status": None}
        #: 탭(페이지)별 메인 문서 상태(WS-26b). start() 가 context 에 단다.
        self._page_status: Any = None
        #: WS-40: 탭별 마지막 문서/프레임 관찰 여부와 한 번만 내보내는 안내.
        self._page_observations: Dict[str, _PageObservation] = {}
        self._observation_watches: WeakSet = WeakSet()
        from security.robots_signal import RobotsSignals

        self._robots = RobotsSignals()  # WS-41: server-memory intent signal cache
        from actions.pdf_document import PdfDocuments

        self._pdf = PdfDocuments()

    async def _await_release_recovery(self) -> None:
        """진행 중인 닫힌 탭 복구가 있으면 끝날 때까지(상한 RELEASE_RECOVERY_WAIT_S) 기다린다."""
        deadline = time.monotonic() + RELEASE_RECOVERY_WAIT_S
        while True:
            fut = self._release_recovery
            if fut is None or fut.done():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("닫힌 탭 복구가 %.0f초 안에 끝나지 않음", RELEASE_RECOVERY_WAIT_S)
                return
            try:
                await asyncio.wait_for(asyncio.shield(fut), remaining)
            except asyncio.TimeoutError:
                logger.warning("닫힌 탭 복구가 %.0f초 안에 끝나지 않음", RELEASE_RECOVERY_WAIT_S)
                return

    async def _wait_captures_idle(self) -> bool:
        """진행 중인 화면 캡처가 모두 끝날 때까지 기다린다(상한 CAPTURE_DRAIN_TIMEOUT_S). 끝나면 True."""
        deadline = time.monotonic() + CAPTURE_DRAIN_TIMEOUT_S
        while self._captures_inflight > 0:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    @property
    def started(self) -> bool:
        return self._started

    def list_tools(self) -> List[Dict[str, Any]]:
        return build_all_tools()


def public_tool_error(exc: BaseException) -> RuntimeError:
    """도구 처리 중 새는 예외(backend·봉투 직렬화)를 모델에 보낼 문구로(R4 P9 — MCP SDK 예외 경로, §1.2 (d)).

    고정 문구 + 가린 예외 종류·메시지. 원 예외는 연결하지 않는다(`from None` — traceback 에 원문 문맥이 붙지 않게).
    """
    from security.public_url import public_text

    try:
        detail = f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001 - str(exc) 자체가 실패
        detail = type(exc).__name__
    return RuntimeError("도구 처리 중 내부 오류 — " + public_text(detail[:LOG_MAX_STR]))


def create_server(
    *,
    mode: ExecutionMode = ExecutionMode.UNATTENDED,
    allowed_domains: tuple = (),
    pre_approved_actions: tuple = (),
    secrets: Any = None,
    som_enabled: bool = False,
    browser_mode: str = "headless",
    chrome_profile: Any = None,
    keep_open: bool = False,
    nav_settle: bool = True,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    allow_private_network: bool = False,
    block_loopback: bool = False,
    approval_ttl_s: float = DEFAULT_APPROVAL_TTL_S,
    profile: Optional[str] = None,
    recipes: bool = True,
    headless_retry: bool = True,
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
        secrets=secrets,
        som_enabled=som_enabled,
        browser_mode=browser_mode,
        chrome_profile=chrome_profile,
        keep_open=keep_open,
        nav_settle=nav_settle,
        max_result_chars=max_result_chars,
        allow_private_network=allow_private_network,
        block_loopback=block_loopback,
        approval_ttl_s=approval_ttl_s,
        profile=profile,
        recipes=recipes,
        headless_retry=headless_retry,
    )
    instructions = _server_instructions(recipes)

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
        for spec in build_listed_tools(recipes):  # 액션 툴 19종 + 서버 도구(WS-29·WS-38)
            kwargs = {
                "name": spec["name"],
                "description": spec["description"],
                field: spec["inputSchema"],
            }
            out.append(Tool(**kwargs))
        return out

    async def _list_tools_impl() -> List[Tool]:
        return _build_tools()

    async def _call_tool_body(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
        if name in SERVER_TOOLS and (recipes or name != RECIPE_TOOL):
            import json

            payload = await backend.call_server_tool(name, arguments)
            return [TextContent(type="text", text=json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")))]
        result = await backend.call_tool(name, arguments)
        # WS-30b: 기본값 필드를 뺀 봉투(빠진 필드 = 계약 기본값, ENVELOPE_RULE).
        return [TextContent(type="text", text=envelope_json(result))]

    async def _call_tool_impl(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
        # WS-52 R1: one barrier for every MCP request, before the branch and any await.
        with backend.tool_entry(name):
            try:
                return await _call_tool_body(name, arguments)
            except Exception as exc:  # noqa: BLE001 - R4 P9: SDK 가 str(exc) 를 isError 문구로 모델에 보낸다
                raise public_tool_error(exc) from None

    # SDK 메이저별 등록 방식이 다르다. 2.x의 lowlevel Server에는
    # list_tools/call_tool 데코레이터가 없고 생성자 콜백을 받는다.
    # 실측 — 데코레이터만 쓰면 create_server()가 AttributeError로 즉사해
    # `agent-browser serve` 경로 전체가 막힌다.
    if hasattr(Server("__probe__"), "list_tools"):
        # mcp 1.x — 데코레이터 등록. instructions 인자가 없는 옛 1.x 는 속성으로 넣는다
        # (create_initialization_options 가 self.instructions 를 읽는다).
        try:
            server = Server("agent-browser", instructions=instructions)
        except TypeError:
            server = Server("agent-browser")
            server.instructions = instructions  # type: ignore[attr-defined]
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
        instructions=instructions,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )
    return server, backend


def _server_instructions(recipes: bool) -> str:
    """MCP initialize 의 서버 instructions(create_server 에서 분리, WS-48)."""
    #: Independent instruction fragments: recipes optional, observation (WS-40) and robots (WS-41) always present.
    from security.robots_signal import ROBOTS_INSTRUCTIONS
    from actions.pdf_document import PDF_INSTRUCTIONS
    from browser.headless_retry import IDENTITY_INSTRUCTIONS

    return ((RECIPE_INSTRUCTIONS + "\n" if recipes else "") + OBSERVE_INSTRUCTIONS + "\n"
            + ROBOTS_INSTRUCTIONS + "\n" + PDF_INSTRUCTIONS + "\n" + IDENTITY_INSTRUCTIONS
            + "\n" + YOUTUBE_INSTRUCTIONS)


async def run_stdio(
    *,
    mode: ExecutionMode = ExecutionMode.UNATTENDED,
    allowed_domains: tuple = (),
    pre_approved_actions: tuple = (),
    secrets_path: Optional[str] = None,
    som_enabled: bool = False,
    browser_mode: str = "headless",
    chrome_profile: Any = None,
    keep_open: bool = False,
    nav_settle: bool = True,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    allow_private_network: bool = False,
    block_loopback: bool = False,
    approval_ttl_s: float = DEFAULT_APPROVAL_TTL_S,
    profile: Optional[str] = None,
    recipes: bool = True,
    headless_retry: bool = True,
) -> None:
    """stdio 트랜스포트로 MCP 서버를 구동한다.

    stdout 은 MCP 프로토콜 전용이다 — 시작 로그는 stderr 로 한 줄만 쓴다.
    profile(WS-32): 영속 프로필 이름. MCP 를 열기 전에 잠근다 — 다른 serve 가 쓰는 중이면
    serve_profile.ProfileInUseError 를 올린다(CLI 가 stderr 한 줄 + exit 2).
    """
    from mcp.server.stdio import stdio_server

    secrets = None
    if secrets_path:
        # 권한이 느슨하면 SecretsError로 기동을 거부한다(PRD 5.3).
        from security import SecretStore

        secrets = SecretStore.from_file(secrets_path)

    server, backend = create_server(
        mode=mode,
        allowed_domains=allowed_domains,
        pre_approved_actions=tuple(pre_approved_actions),
        secrets=secrets,
        som_enabled=som_enabled,
        browser_mode=browser_mode,
        chrome_profile=chrome_profile,
        keep_open=keep_open,
        nav_settle=nav_settle,
        max_result_chars=max_result_chars,
        allow_private_network=allow_private_network,
        block_loopback=block_loopback,
        approval_ttl_s=approval_ttl_s,
        **({"profile": profile} if profile is not None else {}),
        **({} if recipes else {"recipes": False}),
        **({} if headless_retry else {"headless_retry": False}),
    )
    # WS-29: 사람 인계 통로(상태 디렉터리)를 브라우저보다 먼저 연다 — 사람이 server_id 로 고른다.
    server_id = backend.open_handoff() if hasattr(backend, "open_handoff") else None
    if profile is not None:
        # WS-32: 브라우저는 첫 툴 호출 때 뜨지만 잠금은 지금 잡는다 — 동시 사용이면 바로 실패.
        try:
            backend.acquire_profile()
        except BaseException:
            if hasattr(backend, "hub"):
                backend.hub.close()
            raise
    _announce_serve(mode, browser_mode, chrome_profile, keep_open, profile, recipes,
                    allow_private_network, block_loopback, server_id)

    async def _serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    restore_log = install_public_log_filter()  # WS-51: 로그 줄의 페이지 주소 비밀 값 가리기
    try:
        await _serve_until_stop(_serve, backend)
    finally:
        restore_log()


#: install_public_log_filter 의 겹친 설치 수(R2 NB-5c) — 마지막 설치가 끝날 때만 원래대로 돌린다.
_PUBLIC_LOG_STATE: Dict[str, Any] = {"count": 0, "uninstall": None}
_PUBLIC_LOG_LOCK = threading.Lock()


def install_public_log_filter() -> Callable[[], None]:
    """이 프로세스의 로그·예외 출력에서 도구가 본 비밀 값과 주소의 비밀 값을 가린다(WS-51, R4 P1).

    **가린 뒤 표현**(R3-B2·B3·B4): 레코드 팩토리가 **포맷 전에** args 를 같은 모양으로 재귀 정제한다 — 문자열 리프는
    public_text, dict(키 포함)·list·tuple·namedtuple·set 은 같은 모양의 새 객체, 그 밖의 객체(예외·모델·URL 객체 …)는
    str·repr 이 각각 public_text(원 str·repr) 인 안전 래퍼(`_LogSafe`). 깊이·개수·길이 상한을 넘으면 안전 표지.
    그 뒤 완성된 메시지에 public_text 를 한 번 더(백스톱 — f-string 으로 미리 만든 문장, 임의 깊이 이스케이프 인용).
    `extra=` 속성은 팩토리 **뒤에** 붙으므로(Logger.makeRecord) makeRecord 도 감싸 같은 규칙으로 정제한다(formatter 의
    `%(meta)s`). exc_text·stack_info 와 `sys.excepthook`·`threading.excepthook` 의 traceback 도 가린다.

    겹쳐 설치하면 횟수를 세어 마지막 restore 때만 원래대로 돌린다 — 먼저 끝난 쪽이 남은 설치의 가림을 벗기지 않는다.
    반환: 이 설치를 끝내는 함수(두 번 불러도 한 번만 센다).
    """
    with _PUBLIC_LOG_LOCK:
        if _PUBLIC_LOG_STATE["count"] == 0:
            _PUBLIC_LOG_STATE["uninstall"] = _install_public_log_hooks()
        _PUBLIC_LOG_STATE["count"] += 1
    done = [False]

    def restore() -> None:
        with _PUBLIC_LOG_LOCK:
            if done[0]:
                return
            done[0] = True
            _PUBLIC_LOG_STATE["count"] -= 1
            if _PUBLIC_LOG_STATE["count"] == 0:
                uninstall = _PUBLIC_LOG_STATE["uninstall"]
                _PUBLIC_LOG_STATE["uninstall"] = None
                if uninstall is not None:
                    uninstall()

    return restore


#: logging 패키지 디렉터리 — handleError 의 'Call stack' 이 logging 내부 프레임을 건너뛰는 기준(logging 과 같다).
_LOGGING_DIR = os.path.dirname(logging.__file__)

#: 로그 인자 정제 상한(R4 §8 — 루트 로거가 DEBUG 면 버려질 레코드도 정제되므로 비용을 묶는다).
LOG_MAX_STR = 64 * 1024
LOG_MAX_ITEMS = 1000
LOG_MAX_DEPTH = 8


class _LogSafe:
    """로그 인자 자리의 비문자열 객체 — str·repr 이 각각 public_text(원 str·repr)(R4 P1). 숫자 변환(`%d` 등)은 원
    객체에 맡기되, str·repr 이 가려지는 객체면 숫자로 바꾸지 않는다(R5 B1 — 원 숫자가 `%x`·`%05d` 꼴로 새지 않게).
    원 str/repr 이 실패하면 그 예외를 그대로 낸다(logging 의 형식 오류 경로 — 원문 없이 진단)."""

    __slots__ = ("_obj",)

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def __str__(self) -> str:
        return _log_text(str(self._obj))

    def __repr__(self) -> str:
        return _log_text(repr(self._obj))

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def masked(self) -> bool:
        """원 str·repr 중 하나라도 가려지는가(실패하면 False — 형식 오류 경로에 맡긴다)."""
        try:
            text, rep = str(self._obj), repr(self._obj)
        except Exception:  # noqa: BLE001
            return False
        return _log_text(text) != text or _log_text(rep) != rep

    def _number_source(self) -> Any:
        if self.masked():
            raise TypeError("가린 로그 값은 숫자로 바꾸지 않습니다")
        return self._obj

    def __int__(self) -> int:
        return int(self._number_source())

    def __float__(self) -> float:
        return float(self._number_source())

    def __index__(self) -> int:
        return self._number_source().__index__()


def _log_text(text: str) -> str:
    from security.public_url import public_text

    if len(text) > LOG_MAX_STR:
        return f"[로그 값 생략: {len(text)}자 — 상한 {LOG_MAX_STR}]"
    return public_text(text)


def _masked_number(obj: Any) -> Optional[str]:
    """숫자(int·float·complex·Decimal·Fraction·IntEnum …)의 str·repr 이 가려지면 가린 문자열, 아니면 None(R5 B1).

    가려지지 않은 숫자는 원 객체 그대로 둔다 — `%d`·`%5.2f`·`%x` 출력이 훅이 없을 때와 같다. 가려진 숫자는 숫자가
    아니라 표지 문자열이 된다(원 숫자를 다른 진법·자릿수로 찍어 새는 길이 없다). str/repr 이 실패하면(자릿수 상한을
    넘는 거대 정수 등) None — 원 객체의 형식 오류 경로에 맡긴다."""
    try:
        text, rep = str(obj), repr(obj)
    except Exception:  # noqa: BLE001
        return None
    safe = _log_text(text)
    if safe != text:
        return safe
    safe_rep = _log_text(rep)
    return safe_rep if safe_rep != rep else None


def log_safe_value(obj: Any, depth: int = 0) -> Any:
    """로그 인자·extra 값 하나를 같은 모양으로 정제한다(바뀐 것이 없으면 같은 객체). R4 P1 · R5 B1.

    비문자열 리프 전 유형: None·bool 은 그대로, 숫자는 가려질 때만 표지 문자열(아니면 같은 객체), 그 밖의 객체
    (bytes·datetime·UUID·Enum·사용자 객체 …)는 _LogSafe(str·repr·format 이 가린 값)."""
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, str):
        out = _log_text(obj)
        return obj if out == obj else out
    if isinstance(obj, numbers.Number):
        masked = _masked_number(obj)
        return obj if masked is None else masked
    if depth >= LOG_MAX_DEPTH:
        return "[REDACTED-DEPTH]"
    if isinstance(obj, dict):
        if len(obj) > LOG_MAX_ITEMS:
            return f"[로그 값 생략: 원소 {len(obj)}개 — 상한 {LOG_MAX_ITEMS}]"
        pairs = [(_log_safe_key(k, depth + 1), log_safe_value(v, depth + 1)) for k, v in list(obj.items())]
        if all(a is k and b is v for (a, b), (k, v) in zip(pairs, obj.items())):
            return obj
        return OrderedDict(pairs) if isinstance(obj, OrderedDict) else dict(pairs)
    if isinstance(obj, (list, tuple, set, frozenset)):
        if len(obj) > LOG_MAX_ITEMS:
            return f"[로그 값 생략: 원소 {len(obj)}개 — 상한 {LOG_MAX_ITEMS}]"
        items = [log_safe_value(v, depth + 1) for v in obj]
        if all(a is b for a, b in zip(items, obj)):
            return obj
        if isinstance(obj, tuple) and hasattr(obj, "_fields"):  # namedtuple — 같은 형으로
            try:
                return type(obj)._make(items)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 생성자가 다르면 일반 tuple
                return tuple(items)
        if isinstance(obj, list):
            return items
        if isinstance(obj, tuple):
            return tuple(items)
        return frozenset(items) if isinstance(obj, frozenset) else set(items)
    return _LogSafe(obj)


def _log_safe_key(key: Any, depth: int) -> Any:
    """dict 키(중첩 dict 와 extra 의 top-level 비문자열 이름 공통 — R6 F1) — 값과 같은 규칙(숫자 키가 가려지면 표지
    문자열 키 — JSON 직렬화도 된다). 가려지지 않는 객체 키는 원 객체 그대로(안전 래퍼로 바꾸지 않는다)."""
    safe = log_safe_value(key, depth)
    if isinstance(safe, _LogSafe) and not safe.masked():
        return key
    return safe


#: printf 형식 지정자(logging 의 `%` 스타일). `%%` 는 인자를 쓰지 않는다.
_PCT_SPEC = re.compile(r"%(?:\((?P<key>[^)]*)\))?(?P<flags>[#0\- +]*)(?P<width>\*|\d+)?"
                       r"(?:\.(?P<prec>\*|\d+))?[hlL]?(?P<conv>[diouxXeEfFgGcrsa%])")
_NUMERIC_CONV = frozenset("diouxXeEfFgGc")


def _was_masked(original: Any, safe: Any) -> bool:
    """인자 하나가 숫자 형식으로 찍힐 수 없게 가려졌는가(가린 숫자 → 표지 문자열, 가린 _LogSafe)."""
    if isinstance(safe, str) and not isinstance(original, str):
        return True
    return isinstance(safe, _LogSafe) and safe.masked()


def _neutralize_masked_specs(msg: str, original: Any, safe: Any) -> Optional[str]:
    """가린 인자 자리의 숫자 형식 지정자(`%d`·`%5.2f`·`%x` …)를 `%s` 로 바꾼 형식 문자열(R5 B1 — 형식 오류 없이
    표지). 가려지지 않은 인자 자리는 그대로(원래 출력과 같다). 바꿀 자리가 없으면 msg, 다룰 수 없으면 None."""
    mapping = isinstance(safe, dict)
    if not mapping and not isinstance(safe, tuple):
        return msg
    out: List[str] = []
    prev = 0
    index = 0
    changed = False
    for m in _PCT_SPEC.finditer(msg):
        conv = m.group("conv")
        if conv == "%":
            continue
        if not mapping:
            stars = (m.group("width") == "*") + (m.group("prec") == "*")
            index += stars
            if index >= len(safe):
                return None
            hit = _was_masked(original[index], safe[index])
            index += 1
        else:
            key = m.group("key")
            if key is None or key not in safe:
                return None
            hit = _was_masked(original.get(key), safe[key])
        if not hit or conv not in _NUMERIC_CONV:
            continue
        if m.group("prec") == "*":
            return None  # `%.*d` 의 정밀도 인자를 버리면 인자 수가 어긋난다 — 형식 오류 경로(원문 없음)
        key_part = f"({m.group('key')})" if mapping else ""
        flags = "-" if "-" in m.group("flags") else ""
        out.append(msg[prev:m.start()])
        out.append(f"%{key_part}{flags}{m.group('width') or ''}s")
        prev = m.end()
        changed = True
    if not changed:
        return msg
    out.append(msg[prev:])
    return "".join(out)


def _install_public_log_hooks() -> Callable[[], None]:
    """레코드 팩토리·Logger.makeRecord·sys/threading excepthook 을 감싸고, 원래대로 돌리는 함수를 돌려준다
    (refcount 는 호출자)."""
    import sys
    import traceback

    from security.public_url import public_text
    from security.secret_registry import mask_registered

    previous = logging.getLogRecordFactory()
    original_make = logging.Logger.makeRecord
    original_handler_format = logging.Handler.format
    original_handle_error = logging.Handler.handleError
    if getattr(previous, "_ws51_public", False):  # 다른 경로(모듈 재적재 등)로 이미 감싸져 있다
        return lambda: None

    def _mask(record: logging.LogRecord) -> None:
        original_msg, original_args = record.msg, record.args
        safe_args = log_safe_value(original_args) if original_args else original_args
        safe_msg = original_msg if isinstance(original_msg, str) else log_safe_value(original_msg)
        format_msg = safe_msg
        if isinstance(safe_msg, str) and safe_args and safe_args is not original_args:
            format_msg = _neutralize_masked_specs(safe_msg, original_args, safe_args)
            if format_msg is None:
                format_msg = safe_msg
            elif format_msg is not safe_msg:
                safe_msg = format_msg  # 아래 '바뀜' 판정 — 완성 문장으로 바꿔 둔다
        record.msg, record.args = format_msg, safe_args
        try:
            message = record.getMessage()
            failed = False
        except Exception as exc:  # noqa: BLE001 - 형식이 깨진 호출(`'bad %d' % url`)
            # R1 B4: 원 record 를 그대로 두면 handler 의 'Logging error … Arguments: (…)' 진단이 원문 args 를
            # stderr 로 낸다. 형식 실패 사실은 남기고(조용히 버리지 않는다) msg·args 를 가린 문자열로 바꾼다.
            try:
                args_text = repr(safe_args)
            except Exception:  # noqa: BLE001
                args_text = "<args repr 실패>"
            message = f"[로그 형식 오류 {type(exc).__name__}: {exc}] msg={safe_msg!r} args={args_text}"
            failed = True
        masked = public_text(message)  # 백스톱(완성된 문장 — 미리 만든 f-string·임의 깊이 이스케이프 인용)
        if failed or masked != message or safe_args is not original_args or safe_msg is not original_msg:
            record.msg, record.args = masked, None
        else:
            record.msg, record.args = original_msg, original_args
        if record.exc_text:  # R2 NB-5a: 앞선 팩토리가 이미 채운 traceback 문자열
            record.exc_text = public_text(record.exc_text)
        elif record.exc_info and record.exc_info[0] is not None:
            text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            record.exc_text = public_text(text)  # Formatter 는 exc_text 가 있으면 다시 만들지 않는다
        if record.stack_info:
            record.stack_info = public_text(record.stack_info)

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            _mask(record)
        except Exception as exc:  # noqa: BLE001 - 가림이 실패해도 로그는 남기되 원문은 내보내지 않는다
            record.msg = f"[로그 가림 실패 {type(exc).__name__} — 원문 생략] logger={record.name}"
            record.args, record.exc_info, record.exc_text, record.stack_info = None, None, None, None
        return record

    def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = original_make(self, *args, **kwargs)
        extra = kwargs.get("extra", args[8] if len(args) > 8 else None)
        if extra:
            for key in list(extra):  # R2 NB-5b · R4 P1 · R5 B1: extra 속성(값의 모든 리프 + 속성 이름)
                if key not in record.__dict__:
                    continue
                try:
                    value = log_safe_value(record.__dict__[key])
                except Exception as exc:  # noqa: BLE001 - 속성은 남기되 원문은 내보내지 않는다
                    value = f"[로그 가림 실패 {type(exc).__name__} — 원문 생략]"
                # R6 F2: 문자열 속성 이름은 그대로 둔다 — 기존 Formatter 의 `%(name)s`·`{name}`·`${name}` 조회가 훅이
                # 없을 때와 같아야 한다. 이름이 비밀이면 출력 단계(handler_format·handle_error)가 가린다.
                # R6 F1: 비문자열 이름(숫자·bytes·tuple …)은 Formatter 가 이름으로 조회할 수 없다 — 중첩 dict 키와
                # 같은 정책(_log_safe_key)으로 가린 이름으로 옮긴다.
                safe_key = key if isinstance(key, str) else _log_safe_key(key, 0)
                if safe_key is key or safe_key == key:
                    record.__dict__[key] = value
                    continue
                del record.__dict__[key]
                name, n = safe_key, 2
                while name in record.__dict__:
                    name, n = f"{safe_key}#{n}", n + 1
                record.__dict__[name] = value
        return record

    def handler_format(self: logging.Handler, record: logging.LogRecord) -> str:
        """R6 F2: 모든 handler 출력(표준·JSON 형 Formatter — 속성 이름·값을 어떻게 찍든)의 최종 문장에서 도구가 본 비밀
        값(레지스트리 — §1.1 S 의 모든 표현 R)을 가린다. public_text 의 첫 층만 쓴다: URL 문법·JWT 층은 메시지·extra 값
        단계에서 이미 거쳤고, Formatter 가 이어 붙인 한 줄 전체에 다시 걸면 인용 값 판정이 칸 경계를 넘어 비밀 아닌
        출력(`… | %(where)s`)을 삼킨다(dd9d3aa 출력 동일성 — 2026-10-17 메인 승인)."""
        text = original_handler_format(self, record)
        try:
            return mask_registered(text) if isinstance(text, str) and text else text
        except Exception as exc:  # noqa: BLE001 - 가림 실패 — 원문 대신 표지
            return f"[로그 가림 실패 {type(exc).__name__} — 원문 생략] logger={record.name}"

    def handle_error(self: logging.Handler, record: logging.LogRecord) -> None:
        """R6 F2: logging.Handler.handleError 와 같은 진단(같은 조건·같은 모양)을 버퍼에 만든 뒤 가려서 stderr 로.

        Formatter 오류(KeyError·'Formatting field not found …')·emit 예외의 traceback·`Message:`/`Arguments:` 줄이
        속성 이름·값 원문을 담을 수 있다. raiseExceptions 가 False 이거나 stderr 가 없으면 아무것도 쓰지 않는다(원래와 같다).
        """
        if not (logging.raiseExceptions and sys.stderr):
            return
        t, v, tb = sys.exc_info()
        buf = io.StringIO()
        try:
            buf.write("--- Logging error ---\n")
            traceback.print_exception(t, v, tb, None, buf)
            buf.write("Call stack:\n")
            frame = tb.tb_frame if tb is not None else None
            while frame and os.path.dirname(frame.f_code.co_filename) == _LOGGING_DIR:
                frame = frame.f_back
            if frame:
                traceback.print_stack(frame, file=buf)
            else:
                buf.write("Logged from file %s, line %s\n" % (record.filename, record.lineno))
            try:
                buf.write("Message: %r\nArguments: %s\n" % (record.msg, record.args))
            except RecursionError:
                raise
            except Exception:  # noqa: BLE001 - logging 원래 문구
                buf.write("Unable to print the message and arguments - possible formatting error.\n"
                          "Use the traceback above to help find the error.\n")
            try:
                text = mask_registered(buf.getvalue())  # handler_format 과 같은 층(레지스트리)
            except Exception as exc:  # noqa: BLE001
                text = f"--- Logging error --- [진단 가림 실패 {type(exc).__name__} — 원문 생략]\n"
            sys.stderr.write(text)
        except OSError:  # logging 과 같다(issue 5971)
            pass
        finally:
            del t, v, tb

    def _write_exception(header: str, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            text = header + "".join(traceback.format_exception(exc_type, exc, tb))
            sys.stderr.write(public_text(text))
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 - 예외 출력 실패는 조용히(원문을 대신 내지 않는다)
            pass

    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def excepthook(exc_type: Any, exc: Any, tb: Any) -> None:
        _write_exception("", exc_type, exc, tb)

    def thread_excepthook(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, "name", None) or threading.get_ident()
        _write_exception(f"Exception in thread {name}:\n", args.exc_type, args.exc_value, args.exc_traceback)

    factory._ws51_public = True  # type: ignore[attr-defined]
    make_record._ws51_public = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]
    logging.Handler.format = handler_format  # type: ignore[method-assign]
    logging.Handler.handleError = handle_error  # type: ignore[method-assign]
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook

    def uninstall() -> None:
        if logging.getLogRecordFactory() is factory:
            logging.setLogRecordFactory(previous)
        if logging.Logger.makeRecord is make_record:
            logging.Logger.makeRecord = original_make  # type: ignore[method-assign]
        if logging.Handler.format is handler_format:
            logging.Handler.format = original_handler_format  # type: ignore[method-assign]
        if logging.Handler.handleError is handle_error:
            logging.Handler.handleError = original_handle_error  # type: ignore[method-assign]
        if sys.excepthook is excepthook:
            sys.excepthook = previous_hook
        if threading.excepthook is thread_excepthook:
            threading.excepthook = previous_thread_hook

    return uninstall


def _announce_serve(mode: ExecutionMode, browser_mode: str, chrome_profile: Any, keep_open: bool,
                    profile: Optional[str], recipes: bool, allow_private_network: bool,
                    block_loopback: bool, server_id: Optional[str]) -> None:
    """serve 시작 로그(stderr, run_stdio 에서 분리 — WS-48). stdout 은 MCP 전용."""
    import sys

    extra = ""
    if browser_mode == "user-chrome":
        extra = f" chrome_profile={chrome_profile or '(기본)'} keep_open={bool(keep_open)}"
    if profile is not None:
        extra += f" profile={profile}(영속)"
    if browser_mode == "on-demand":
        extra += (" [평소 headless — 사람 인계·승인 코드 때만 창"
                  + ("" if profile is not None else ", 서버 전용 임시 프로필(종료 때 삭제)") + "]")
    extra += " recipes=" + (("on(profile)" if profile is not None else "on(memory)") if recipes else "off")
    extra += (
        f" egress(loopback={'blocked' if block_loopback else 'allowed'},"
        f" private={'allowed' if allow_private_network else 'blocked'})"
    )
    if browser_mode == "user-chrome":
        # WS-29b: 우리가 띄운 Chrome 에만 프록시 플래그를 줄 수 있다 — 한 줄로 알린다.
        extra += " [egress 프록시는 우리가 띄운 Chrome 에만 적용, README 보안 절]"
    print(
        f"agent-browser serve: browser={browser_mode} mode={mode.value}{extra}"
        " (브라우저는 첫 툴 호출 때 시작)",
        file=sys.stderr,
        flush=True,
    )
    if server_id:
        print(
            f"agent-browser serve: server_id={server_id} — 사람 인계: "
            f"`agent-browser control take|release --server {server_id}`, "
            f"승인: `agent-browser approve <approval_id>`",
            file=sys.stderr,
            flush=True,
        )


async def _serve_until_stop(_serve: Any, backend: Any) -> None:
    """서버를 돌리고 종료 신호·정상 종료를 처리한다(run_stdio 에서 분리 — WS-48)."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    received: List[int] = []
    previous = _install_shutdown_signals(loop, stop, received)
    serve_task = asyncio.ensure_future(_serve())
    stop_task = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        serve_task.cancel()
        stop_task.cancel()
        _restore_signals(previous)
        await _bounded_close(backend)
        raise

    if not received:
        # 정상 종료(stdin EOF 등): 기존과 같다 — 정리 뒤 반환(종료 코드 0), 서버 예외는 다시 올린다.
        stop_task.cancel()
        try:
            await serve_task
        finally:
            _restore_signals(previous)
            await _bounded_close(backend)
        return

    # 종료 신호: 서버를 취소하고(기다리지 않음) 브라우저를 정리한 뒤 프로세스를 끝낸다.
    # stdin 리더는 스레드에서 막혀 있어 취소가 끝나지 않을 수 있으므로(실측: SIGINT 로
    # asyncio.run 이 무한 대기) 정리가 끝나면 os._exit 로 바로 종료한다.
    serve_task.cancel()
    # 진행 중인 툴 호출(브라우저 시작 등)이 취소 정리를 마칠 시간을 짧게 준다(상한 있음 —
    # stdin 리더 스레드 때문에 서버 태스크 자체는 끝나지 않을 수 있다).
    await asyncio.wait({serve_task}, timeout=min(SHUTDOWN_TASK_GRACE_S, SHUTDOWN_CLOSE_TIMEOUT_S))
    signum = received[0]
    ok = await _bounded_close(backend)
    _stderr_line(
        f"agent-browser serve: {'정리 완료' if ok else '정리 미완료'} — 종료 코드 {128 + signum}"
    )
    os._exit(128 + signum)


#: 종료 신호 뒤 backend.close() 상한(초). UserChrome.close 의 기본 timeout(10초)과 같다.
SHUTDOWN_CLOSE_TIMEOUT_S = 10.0
#: 정리가 이벤트 루프를 막는(동기 대기) 경우의 감시 스레드 여유(초). 상한+여유 뒤 강제 종료.
SHUTDOWN_WATCHDOG_GRACE_S = 5.0
#: 신호 뒤 취소된 서버 태스크가 자기 정리를 마치기를 기다리는 시간(초).
SHUTDOWN_TASK_GRACE_S = 2.0
#: serve 가 처리하는 종료 신호. SIGKILL 은 잡을 수 없다(README 참조).
SHUTDOWN_SIGNALS = tuple(
    getattr(signal, n) for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n)
)


def _install_shutdown_signals(loop: Any, stop: Any, received: List[int]) -> Dict[int, Any]:
    """SIGTERM·SIGHUP·SIGINT 처리기를 단다. 이전 처리기를 돌려준다.

    loop.add_signal_handler 가 아니라 signal.signal 을 쓴다 — 정리가 이벤트 루프를 동기로
    막고 있을 때(UserChrome.close 의 proc.wait 등)도 두 번째 신호로 바로 끝낼 수 있게.
    첫 신호: stop 이벤트(루프 스레드 안전) + 감시 스레드 시작. 두 번째 신호: 즉시 종료.
    """
    import threading

    def _handler(signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        if received:
            _stderr_line(f"agent-browser serve: 신호 {name} 다시 받음 — 정리를 기다리지 않고 종료")
            os._exit(128 + signum)
        received.append(signum)
        _stderr_line(f"agent-browser serve: 신호 {name} — 브라우저 정리 후 종료")
        cap = SHUTDOWN_CLOSE_TIMEOUT_S + SHUTDOWN_WATCHDOG_GRACE_S

        def _watchdog() -> None:
            time.sleep(cap)
            _stderr_line(f"agent-browser serve: 정리가 {cap:g}s 안에 끝나지 않아 강제 종료")
            os._exit(128 + signum)

        threading.Thread(target=_watchdog, name="serve-shutdown-watchdog", daemon=True).start()
        loop.call_soon_threadsafe(stop.set)

    previous: Dict[int, Any] = {}
    for sig in SHUTDOWN_SIGNALS:
        try:
            previous[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):  # 메인 스레드가 아니면 등록 불가 — 기존 동작 유지
            continue
    # 신호 마스크는 부모에게서 상속되고 signal.signal 은 마스크를 풀지 않는다 — 부모가 막아 둔
    # 채로 띄우면 처리기를 달아도 신호가 전달되지 않아 serve 가 끝나지 않았다(WS-29b R1 실측).
    # 처리기를 단 신호만 이 스레드에서 푼다(stdin 리더 등 이후 만든 스레드도 이 마스크를 받는다).
    if previous and hasattr(signal, "pthread_sigmask"):
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, set(previous))
        except (ValueError, OSError):
            pass
    return previous


def _restore_signals(previous: Dict[int, Any]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            pass


async def _bounded_close(backend: Any) -> bool:
    """backend.close() 를 상한 시간 안에서 돌린다. 끝까지 정리했으면 True."""
    try:
        await asyncio.wait_for(backend.close(), timeout=SHUTDOWN_CLOSE_TIMEOUT_S)
        return True
    except asyncio.TimeoutError:
        _stderr_line(
            f"agent-browser serve: 브라우저 정리가 상한({SHUTDOWN_CLOSE_TIMEOUT_S:g}s)을 넘어 중단"
        )
        _kill_owned_chrome(backend)
    except Exception as exc:  # noqa: BLE001
        _stderr_line(f"agent-browser serve: 브라우저 정리 실패: {exc!r}")
        _kill_owned_chrome(backend)
    return False


def _kill_owned_chrome(backend: Any) -> None:
    """정리가 막혔을 때 마지막 수단: 우리가 띄운 Chrome 만 닫는다(keep_open 이면 둔다).

    UserChrome.close 는 attach 로 붙은(소유하지 않은) Chrome 은 건드리지 않는다.
    """
    core = getattr(backend, "_core", None)
    uc = getattr(core, "_user_chrome", None)
    if uc is None or getattr(core, "keep_open", False):
        return
    try:
        uc.close(timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        _stderr_line(f"agent-browser serve: Chrome 종료 실패: {exc!r}")


# --------------------------------------------------------------------------- WS-48 R1: 모듈 전역 호환
#: 책임별로 옮긴 모듈. 그 소스에 정의된 함수는 아래 `_adopt_split_globals` 로 이 모듈 네임스페이스를 전역으로 쓴다.
_SPLIT_MODULES = (
    "interface.server_common",
    "interface.server_display",
    "interface.server_envelope",
    "interface.server_handoff",
    "interface.server_hitl",
    "interface.server_lifecycle",
    "interface.server_schema",
    "interface.server_signals",
    "interface.server_tools",
    "interface.server_window",
)


def _adopt_split_globals() -> None:
    """옮긴 함수들이 전역 이름을 분리 전처럼 **이 모듈(interface.mcp_server)** 에서 호출 시점에 찾게 한다.

    분리 전에는 서버 코드가 이 모듈 하나에 있어 `mcp_server.TOOL_PREFIX = ...`·`monkeypatch.setattr(mcp_server,
    "_wait_timeout", ...)` 같은 재바인딩이 모든 사용처에 그대로 전달됐다. 옮긴 뒤에는 함수의 `__globals__` 가 새
    모듈이고 `from … import` 캐시까지 생겨 그 효과가 사라졌다(WS-48 R1). 같은 코드 객체(`__code__`)로 전역만
    이 모듈 dict 인 함수 객체를 다시 만들어 분리 모듈·믹스인 클래스·재수출 속성을 그것으로 바꾼다 — 본문·기본값·
    클로저·annotation 은 그대로이고 호출 비용도 그대로다(조회 대상 dict 만 바뀜). annotation 문자열도 이 모듈에서
    해석되므로 `typing.get_type_hints(BrowserMCPServer.__aenter__)` 가 분리 전처럼 성공한다.

    결과: 옮긴 코드의 패치 지점은 분리 전과 같은 `interface.mcp_server.<이름>` 이다. 분리 모듈 쪽 이름
    (`server_schema.TOOL_PREFIX` 등)을 바꿔도 옮긴 함수에는 전달되지 않는다(분리 전에 없던 지점). 모르는
    데코레이터가 붙은 함수는 조용히 넘기지 않고 import 단계에서 실패시킨다.
    """
    import contextlib
    import inspect
    import sys
    import types

    namespace = globals()

    @contextlib.asynccontextmanager
    async def _acm_probe():  # noqa: ANN202 - asynccontextmanager 래퍼의 코드 객체 식별용
        yield

    acm_code = _acm_probe.__code__
    swapped: Dict[int, tuple] = {}  # id(옛 함수) -> (옛 함수, 새 함수) — 옛 객체를 살려 둬 id 재사용을 막는다

    def rebind(fn: Any) -> Any:
        if id(fn) in swapped:
            return swapped[id(fn)][1]
        new = types.FunctionType(fn.__code__, namespace, fn.__name__, fn.__defaults__, fn.__closure__)
        new.__kwdefaults__ = fn.__kwdefaults__
        new.__annotations__ = dict(fn.__annotations__)
        new.__dict__.update(fn.__dict__)
        new.__doc__ = fn.__doc__
        new.__qualname__ = fn.__qualname__
        new.__module__ = fn.__module__
        swapped[id(fn)] = (fn, new)
        return new

    def adopt(value: Any, source: str) -> Any:
        """옮긴 소스에 정의된 함수(또는 그 래퍼)면 새 객체, 아니면 None."""
        if isinstance(value, (staticmethod, classmethod)):
            inner = adopt(value.__func__, source)
            return None if inner is None else type(value)(inner)
        if isinstance(value, property):
            parts = [value.fget, value.fset, value.fdel]
            new_parts = [None if p is None else (adopt(p, source) or p) for p in parts]
            if all(a is b for a, b in zip(parts, new_parts)):
                return None
            return property(*new_parts, value.__doc__)
        if not inspect.isfunction(value):
            return None
        if value.__code__.co_filename == source:
            return rebind(value)
        wrapped = getattr(value, "__wrapped__", None)
        if inspect.isfunction(wrapped) and wrapped.__code__.co_filename == source:
            if value.__code__ is not acm_code:
                raise RuntimeError(f"WS-48: 전역 이전을 모르는 데코레이터 — {value.__qualname__}")
            return contextlib.asynccontextmanager(rebind(wrapped))
        return None

    modules = [sys.modules[name] for name in _SPLIT_MODULES]
    for mod in modules:
        source = mod.__file__
        for key, value in list(vars(mod).items()):
            if inspect.isclass(value) and value.__module__ == mod.__name__:
                for attr_name, attr in list(vars(value).items()):
                    new_attr = adopt(attr, source)
                    if new_attr is not None:
                        setattr(value, attr_name, new_attr)
            else:
                new_value = adopt(value, source)
                if new_value is not None:
                    setattr(mod, key, new_value)
    # 다른 분리 모듈의 `from … import` 캐시와 이 모듈의 재수출도 새 객체로 맞춘다.
    for ns in [vars(mod) for mod in modules] + [namespace]:
        for key, value in list(ns.items()):
            pair = swapped.get(id(value))
            if pair is not None and pair[0] is value:
                ns[key] = pair[1]


_adopt_split_globals()
