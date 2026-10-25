"""BrowserMCPServer 의 작업 공간(WS-57) — 호출자마다 자기 탭·활성 탭·관찰 epoch·디스패처.

같은 serve 를 대화와 정기 작업이 함께 쓰면(Hermes 는 프로세스당 MCP 연결 하나를 대화·cron 이 공유하고 호출을
동시에 보낸다) 활성 탭·snapshot_epoch·탭 닫기·navigate 가 서로에게 샜다. 작업 공간 이름은
(1) 도구 인자 `workspace`(계약 밖 서버 메타 — 검증 전에 떼어 낸다), 없으면 (2) 연결 기본값(attach 연결의
`--workspace`, interface.attach), 없으면 `default`(예전 단일 세션 그대로)다.

작업 공간마다 따로: 탭 묶음·활성 탭(BrowserCore), PerceptionEngine(epoch·요소), ActionDispatcher(page·cdp).
공유: 영속 컨텍스트(쿠키·로그인), egress 가드, 문서 상태, 창(on-demand)·조작권·HITL 정책·승인 증표.
호출별 임시 상태(_pending_gate_basis·_used_approval)는 호출(요청 태스크)마다 따로 — 같은 작업 공간 안
동시 호출도 섞이지 않는다.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from browser.workspace import (
    DEFAULT_WORKSPACE,
    WorkspaceNameError,
    current_workspace,
    validate_workspace,
    workspace_scope,
)

#: 분리 전과 같은 로거 이름을 쓴다(로그 레코드 불변).
logger = logging.getLogger("interface.mcp_server")

#: 도구 인자 이름(계약 밖 서버 메타, approval_id 와 같은 방식으로 검증 전에 뗀다).
WORKSPACE_ARG = "workspace"
#: 작업 공간 인자를 받는 서버 도구(액션 19종은 모두 받는다). 조작권 도구는 브라우저 전체 단위라 받지 않는다.
WORKSPACE_SERVER_TOOLS = frozenset({"browser_recipe"})

#: 호출(요청 태스크)별 임시 상태 묶음 — call_tool 입구에서 새 dict 를 단다. 그 호출 안에서 만든 하위
#: 태스크(asyncio.wait_for 등)도 같은 dict 를 물려받아 쓰기가 보이고, 다른 호출과는 섞이지 않는다.
_CALL_STATE: ContextVar[Optional[Dict[str, Any]]] = ContextVar("agent_browser_call_state", default=None)
#: WS-56 R4: 승인 경로의 호출별 상태(_approval_via·_oob_note·_approval_pin)도 같은 묶음 하나(예전 ContextVar 5개를 합침).
_PER_CALL = ("_pending_gate_basis", "_used_approval", "_approval_via", "_oob_note", "_approval_pin")


@dataclass
class WorkspaceRuntime:
    """작업 공간 하나의 관찰·실행 핸들."""

    name: str
    engine: Any = None
    dispatcher: Any = None
    page: Any = None
    cdp: Any = None
    #: 레시피 기록(궤적·구간) — 작업 공간마다 따로, 저장소(recipes.json)는 기본 작업 공간 것을 함께 쓴다.
    recipes: Any = None
    #: WS-57 R1: 사람이 닫은 이 작업 공간 탭 알림(tab_closed_by_human) — 주인 작업 공간에만 알린다.
    tab_notice: Optional[Dict[str, Any]] = None
    #: WS-57 R1: 이 작업 공간이 아직 듣지 못한 창 전환(재기동) — 작업 공간마다 한 번씩 알린다.
    window_unreported: bool = False


def _runtime_field(field: str) -> property:
    def fget(self: Any) -> Any:
        return getattr(self._workspace_runtime(), field)

    def fset(self: Any, value: Any) -> None:
        setattr(self._workspace_runtime(), field, value)

    return property(fget, fset, doc=f"현재 작업 공간의 {field} (WS-57)")


def _call_field(key: str) -> property:
    def fget(self: Any) -> Any:
        return self._call_state().get(key)

    def fset(self: Any, value: Any) -> None:
        self._call_state()[key] = value

    return property(fget, fset, doc=f"호출별 임시 상태 {key} (WS-57)")


def pop_workspace_arg(arguments: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """arguments 에서 workspace 인자를 떼어 (나머지, 이름|None). 이름 규칙 위반이면 WorkspaceNameError."""
    params: Dict[str, Any] = dict(arguments or {})
    if WORKSPACE_ARG not in params:
        return params, None
    raw = params.pop(WORKSPACE_ARG)
    if raw is None or raw == "":
        return params, None
    return params, validate_workspace(raw)


class WorkspaceMixin:
    """BrowserMCPServer 의 작업 공간 (WS-57 믹스인 — 상태는 인스턴스 __dict__ 에 지연 생성)."""

    _engine = _runtime_field("engine")
    _dispatcher = _runtime_field("dispatcher")
    _page = _runtime_field("page")
    _cdp = _runtime_field("cdp")
    _pending_gate_basis = _call_field("_pending_gate_basis")
    _used_approval = _call_field("_used_approval")
    _tab_notice = _runtime_field("tab_notice")
    _window_unreported = _runtime_field("window_unreported")

    @property
    def _recipes(self) -> Any:
        """현재 작업 공간의 레시피 기록기(WS-57: 두 작업 공간의 궤적이 섞여 엉터리 레시피가 되지 않게)."""
        runtime = self._workspace_runtime()
        if runtime.recipes is None and runtime.name != DEFAULT_WORKSPACE:
            base = self._workspace_runtime(DEFAULT_WORKSPACE).recipes
            if base is not None:
                from recipes.service import RecipeService

                runtime.recipes = RecipeService(self)
                runtime.recipes._store = base.store  # 같은 저장소(파일 하나)
        return runtime.recipes

    @_recipes.setter
    def _recipes(self, value: Any) -> None:
        self._workspace_runtime().recipes = value

    def _recipe_services(self) -> list:
        return [r.recipes for r in self._workspace_runtimes().values() if r.recipes is not None]

    def _reset_all_recipes(self, why: str) -> None:
        """사람 조작은 브라우저 전체 — 모든 작업 공간의 궤적을 끊는다."""
        for svc in self._recipe_services():
            svc.reset(why)

    def _close_recipes(self) -> None:
        """종료: 다른 작업 공간의 미뤄 둔 구간을 먼저 쓰고, 공유 저장소는 기본 작업 공간이 마지막에 닫는다."""
        base = self._workspace_runtime(DEFAULT_WORKSPACE).recipes
        for svc in self._recipe_services():
            if svc is not base:
                svc._end_segment()
        if base is not None:
            base.close()

    # ------------------------------------------------------------------ 상태

    def _workspace_runtimes(self) -> Dict[str, WorkspaceRuntime]:
        return self.__dict__.setdefault("_ws_runtimes", {})

    def _workspace_runtime(self, name: Optional[str] = None) -> WorkspaceRuntime:
        name = name or current_workspace()
        runtimes = self._workspace_runtimes()
        runtime = runtimes.get(name)
        if runtime is None:
            runtime = runtimes[name] = WorkspaceRuntime(name)
        return runtime

    def _call_state(self) -> Dict[str, Any]:
        state = _CALL_STATE.get()
        if state is None:
            # 호출 밖(단위 테스트가 속성을 직접 쓰는 경우 등): 인스턴스 하나짜리 묶음.
            state = self.__dict__.setdefault("_call_state_fallback", {})
        return state

    def _call_scope(self, workspace: Optional[str]) -> "_CallScope":
        """한 도구 호출의 범위: 작업 공간(없으면 연결 기본) + 호출별 임시 상태 새 묶음(with 로 쓴다)."""
        return _CallScope(workspace or current_workspace())

    def _all_engines(self) -> list:
        return [r.engine for r in self._workspace_runtimes().values() if r.engine is not None]

    def _bump_all_epochs(self, why: str) -> None:
        """브라우저 전체 사건(사람 조작권 반납·사람이 창을 닫음·창 재기동): 모든 작업 공간의 관찰·승인 근거를
        무효로 — 사건을 어느 작업 공간 문맥이 먼저 처리해도 결과가 같다(WS-57 R1 R3)."""
        for engine in self._all_engines():
            engine.bump_epoch(why)

    def _mark_window_unreported(self) -> None:
        """창 전환(재기동)을 모든 작업 공간이 한 번씩 듣게 한다(각자 자기 탭 복원 내역만, WS-57 R1)."""
        for runtime in self._workspace_runtimes().values():
            runtime.window_unreported = True

    def _window_view(self, info: Any) -> Any:
        """호출자에게 보낼 창 보고(WS-57 R1 R1): 복원 내역(tabs)은 이 작업 공간 탭만, 다른 작업 공간은 개수만.
        서버가 쥔 원본(_last_window_result·last_tabs — 복원용 전체 기록)은 그대로 둔다."""
        if not isinstance(info, dict) or not isinstance(info.get("tabs"), list):
            return info
        ws = current_workspace()
        tabs = info["tabs"]
        mine = [t for t in tabs if not isinstance(t, dict) or t.get("workspace", DEFAULT_WORKSPACE) == ws]
        out = dict(info, tabs=mine)
        others = len(tabs) - len(mine)
        if others:
            out["other_workspace_tabs"] = others
        if "snapshot_epoch" in out and self._engine is not None:
            out["snapshot_epoch"] = self._engine.epoch  # 전환을 일으킨 작업 공간이 아니라 이 작업 공간의 epoch
        return out

    def _forget_workspace_handles(self) -> None:
        """창 전환으로 브라우저를 다시 열었다: 기본 외 작업 공간의 페이지·CDP·디스패처는 옛 브라우저 것 —
        다음 호출 입구(_ensure_workspace)가 복원된 자기 활성 탭으로 다시 만든다. epoch(엔진)은 유지."""
        for name, runtime in self._workspace_runtimes().items():
            if name == DEFAULT_WORKSPACE:
                continue
            runtime.dispatcher = runtime.page = runtime.cdp = None

    # ------------------------------------------------------------------ 입구

    def _workspace_error(self, action: Any, exc: WorkspaceNameError) -> Any:
        from contracts import ErrorCode

        return self._error_result(action, ErrorCode.TAB_NOT_FOUND, str(exc))

    def _workspace_lock(self, name: str) -> Any:
        import asyncio

        locks = self.__dict__.setdefault("_ws_locks", {})
        lock = locks.get(name)
        if lock is None:
            lock = locks[name] = asyncio.Lock()
        return lock

    def _dispatch_serial(self) -> "_WorkspaceSerial":
        """현재 작업 공간의 액션 직렬화 구간(async with). WS-57 R2(B1): 디스패처는 호출 임시 상태(_dialogs·_nav_info·
        _opened_pages·_top_attribution)를 인스턴스에 두고 dispatch 입구에서 초기화한다 — 같은 작업 공간의 두 호출이
        겹치면 한쪽 alert 가 다른 쪽 효과로 읽혔다. 다른 작업 공간끼리는 락이 달라 지금처럼 동시에 돈다.
        같은 호출 흐름 안의 중첩 호출(헤드리스 재시도·레시피 재생 단계)은 이미 쥔 락을 다시 잡지 않는다."""
        return _WorkspaceSerial(self, current_workspace())

    def _serial_pause(self) -> "_SerialPause":
        """사람 응답 대기(elicitation) 동안 이 호출 흐름이 쥔 작업 공간 직렬화 구간을 잠시 놓는다(async with, WS-56 R4).
        나오면 다시 잡는다 — 그사이 같은 작업 공간의 다른 호출이 돌았을 수 있으니 부른 쪽이 대상·조작권을 다시 본다.
        구간을 쥐지 않은 흐름(단위 테스트의 직접 호출)에서는 아무것도 하지 않는다."""
        return _SerialPause()

    def _dispatch_lock(self, name: str) -> Any:
        import asyncio

        # _workspace_lock(첫 호출 탭 만들기)과 다른 락 — 직렬화 구간 안에서 _ensure_workspace 가 그 락을 잡는다.
        locks = self.__dict__.setdefault("_ws_dispatch_locks", {})
        lock = locks.get(name)
        if lock is None:
            lock = locks[name] = asyncio.Lock()
        return lock

    async def _ensure_workspace(self) -> None:
        """기본 외 작업 공간의 첫 호출: 자기 탭(같은 영속 컨텍스트 — 로그인 공유)·엔진·디스패처를 만든다.
        자기 활성 탭이 사라졌으면(사람·사이트가 닫음) 남은 자기 탭(없으면 새 탭)으로 바꾸고 epoch 를 올린다.
        WS-57 R1: 같은 작업 공간의 동시 첫 호출은 직렬화(탭 하나, R6). 기본 작업 공간도 자기 탭이 외부에서 닫혔는데
        다른 작업 공간 탭이 살아 있으면(브라우저가 꺼진 것으로 보지 않는다) 같은 경로로 복구한다(R5)."""
        name = current_workspace()
        if self._core is None:
            return
        if name == DEFAULT_WORKSPACE and not self._default_needs_recovery():
            return
        async with self._workspace_lock(name):
            await self._ensure_workspace_locked(name)

    def _default_needs_recovery(self) -> bool:
        """기본 작업 공간의 디스패처 탭이 닫혔고, 다른 작업 공간 탭이 살아 있는가(그 경우만 — 혼자일 때의 기존
        경로[on-demand 브라우저 복구·기존 오류]는 그대로)."""
        core = self._core
        ctx = getattr(self._dispatcher, "ctx", None)
        counter = getattr(core, "foreign_tab_count", None)
        if ctx is None or not callable(counter) or not counter():
            return False
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None)
        return core.get_tab(getattr(ctx, "tab_id", None)) is None or _closed(page)

    async def _ensure_workspace_locked(self, name: str) -> None:
        core = self._core
        if core is None or (name == DEFAULT_WORKSPACE and not self._default_needs_recovery()):
            return
        from actions import ActionDispatcher, DispatchContext
        from perception import PerceptionEngine

        runtime = self._workspace_runtime(name)
        if runtime.engine is None:
            runtime.engine = PerceptionEngine()
        tab = core.get_tab(core.active_tab_id)
        page_alive = tab is not None
        if tab is not None:
            try:
                page_alive = not tab.page.is_closed()
            except Exception:  # noqa: BLE001 - 닫힘 여부를 못 읽으면 코어 기록만 믿는다
                page_alive = True
        if not page_alive:
            tabs = [t for t in core.tabs() if not _closed(t.page)]
            tab = tabs[0] if tabs else await core.new_tab("mcp-session")
            core.set_active_tab(tab.tab_id)
        ctx = getattr(runtime.dispatcher, "ctx", None)
        if ctx is not None and ctx.tab_id == tab.tab_id and runtime.page is not None and not _closed(ctx.page):
            return
        cdp = await core.new_cdp_session(tab.tab_id)
        if ctx is not None:
            # 디스패처는 그대로(그 작업 공간의 레시피·자가 치유 상태 유지), 활성 탭만 바꾼다.
            runtime.dispatcher._set_active_page(tab.page, tab.tab_id)
            ctx.cdp = cdp
            runtime.engine.bump_epoch("workspace_tab_restored")
        else:
            had_page = runtime.page is not None
            runtime.dispatcher = ActionDispatcher(
                DispatchContext(
                    page=tab.page,
                    engine=runtime.engine,
                    cdp=cdp,
                    tab_id=tab.tab_id,
                    core=core,
                    secrets=self.secrets,
                    som_enabled=self.som_enabled,
                    nav_settle=self.nav_settle,
                    manual_sites=getattr(self, "_manual_store", None),  # WS-64 거부 백스톱(같은 판정)
                    dialog_hold=getattr(self, "_manual_dialog_hold", None),  # WS-64 R2-3 대화상자 보류
                )
            )
            if had_page:
                runtime.engine.bump_epoch("workspace_tab_restored")
        runtime.page = tab.page
        runtime.cdp = cdp

    def _note_other_workspaces(self, result: Any) -> None:
        """tab_control list: 다른 작업 공간의 탭은 개수만(주소 비노출). 없으면 아무것도 싣지 않는다."""
        core = self._core
        counter = getattr(core, "foreign_tab_count", None)
        if not callable(counter):
            return
        others = counter()
        if others:
            result.data["other_workspace_tabs"] = others


class _CallScope:
    """_call_scope 의 컨텍스트 관리자(WS-48 전역 이전이 데코레이터 contextmanager 를 받지 않아 클래스로)."""

    def __init__(self, workspace: str) -> None:
        self.workspace = validate_workspace(workspace)
        self._tokens: Any = None

    def __enter__(self) -> str:
        self._tokens = (_CALL_STATE.set({}), workspace_scope(self.workspace))
        self._tokens[1].__enter__()
        return self.workspace

    def __exit__(self, *exc: Any) -> None:
        state_token, scope = self._tokens
        try:
            scope.__exit__(*exc)
        finally:
            _CALL_STATE.reset(state_token)


#: 이 호출 흐름(태스크와 그 하위 태스크)이 이미 쥔 작업 공간 직렬화 락 이름들 — 중첩 호출의 자기 교착 방지.
_SERIAL_HELD: ContextVar[frozenset] = ContextVar("agent_browser_serial_held", default=frozenset())
#: 이 호출 흐름이 실제로 잡은 직렬화 구간들(바깥→안) — 사람 응답 대기 동안 잠시 놓기(_SerialPause)에 쓴다(WS-56 R4).
_SERIAL_STACK: ContextVar[tuple] = ContextVar("agent_browser_serial_stack", default=())


class _WorkspaceSerial:
    """_dispatch_serial 의 async 컨텍스트 관리자(_CallScope 와 같은 이유로 클래스). 기다리다 취소되면 아무것도 쥐지
    않고, 쥔 뒤 취소·예외면 __aexit__ 가 놓는다."""

    def __init__(self, owner: Any, name: str) -> None:
        self.owner = owner
        self.name = name
        self._lock: Any = None
        self._token: Any = None
        #: 다른 호출이 쥐고 있어 기다렸는가(기다린 사이 바뀐 상태를 다시 볼지 정한다).
        self.waited = False

    async def __aenter__(self) -> None:
        held = _SERIAL_HELD.get()
        if self.name in held:
            return  # 같은 호출 흐름의 중첩 호출 — 바깥이 쥐고 있다
        lock = self.owner._dispatch_lock(self.name)
        self.waited = lock.locked()
        await lock.acquire()
        self._lock = lock
        self._token = (_SERIAL_HELD.set(held | {self.name}), _SERIAL_STACK.set(_SERIAL_STACK.get() + (self,)))

    async def __aexit__(self, *exc: Any) -> None:
        lock, self._lock = self._lock, None
        if self._token is None:
            return
        held_token, stack_token = self._token
        self._token = None
        try:
            _SERIAL_STACK.reset(stack_token)
            _SERIAL_HELD.reset(held_token)
        finally:
            if lock is not None:  # 잠시 놓은 뒤 다시 잡다 취소됐으면(_SerialPause) 쥔 것이 없다
                lock.release()


class _SerialPause:
    """_serial_pause 의 async 컨텍스트 관리자(WS-56 R4). 들어가며 이 흐름이 쥔 구간을 모두 놓고(안→바깥), 나오며
    같은 순서(바깥→안)로 다시 잡는다. 다시 잡기를 기다리다 취소되면 못 잡은 구간은 쥔 것이 없는 상태로 남아
    바깥 __aexit__ 가 놓지 않는다(남의 락을 풀지 않음)."""

    def __init__(self) -> None:
        self._paused: tuple = ()
        self._token: Any = None
        #: 실제로 놓았다가 다시 잡았는가(놓지 않았으면 False).
        self.paused = False

    async def __aenter__(self) -> "_SerialPause":
        paused = tuple(s for s in _SERIAL_STACK.get() if s._lock is not None)
        if not paused:
            return self
        self._token = _SERIAL_HELD.set(_SERIAL_HELD.get() - {s.name for s in paused})
        for serial in reversed(paused):
            lock, serial._lock = serial._lock, None
            lock.release()
        self._paused = paused
        self.paused = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        paused, self._paused = self._paused, ()
        if not paused:
            return
        try:
            for serial in paused:
                lock = serial.owner._dispatch_lock(serial.name)
                await lock.acquire()
                serial._lock = lock
                serial.waited = True
        finally:
            _SERIAL_HELD.reset(self._token)


def _closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:  # noqa: BLE001
        return False
