"""BrowserMCPServer 의 '직접 조작할 사이트'(WS-64): 관문·대상 표시·사람 인계 대기·대화상자 보류·미리 창.

목록(security.manual_sites)은 사람만 바꾼다 — MCP 에는 바꾸는 도구가 없다(control_status 에서 읽기만).
목록 사이트의 부작용 액션은 실행하지 않고(HITL_UNATTENDED_BLOCKED, retry_safe=false, data.handoff.reason=manual_site)
사람이 볼 창이 있으면 대상을 CDP 오버레이로 표시 + 안내 띠 + 격리 world 수동 감지기를 건다(페이지 DOM 무변경).
에이전트는 browser_control_wait 로 clicked|page_changed|done|expired 를 기다린다(입력값은 읽지 않는다).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from contracts import ActionResult, ActionType, ErrorCode
from interface.server_common import _stderr_line

logger = logging.getLogger("interface.mcp_server")

_VERBS = {
    ActionType.CLICK: "누르기", ActionType.TYPE_TEXT: "입력", ActionType.SELECT_OPTION: "고르기",
    ActionType.CHECK_BOX: "체크", ActionType.HOVER: "마우스 올리기", ActionType.PRESS_KEY: "키 누르기",
    ActionType.HANDLE_DIALOG: "대화상자 처리", ActionType.UPLOAD_FILE: "파일 올리기", ActionType.DOWNLOAD_FILE: "내려받기",
}
#: 값 변경으로 끝나는 액션 — 감지 이벤트는 change(누르기만으로는 끝난 게 아니다).
_CHANGE_ACTIONS = frozenset({ActionType.TYPE_TEXT, ActionType.SELECT_OPTION, ActionType.UPLOAD_FILE})
#: 이름으로 보는 민감 칸(값을 안내 띠에 쓰지 않는다).
_SENSITIVE_WORDS = ("password", "passwd", "비밀번호", "암호", "카드", "card", "cvc", "cvv", "주민", "계좌", "pin",
                    "otp", "인증번호", "보안코드")
_HIGHLIGHT = {"showInfo": False, "contentColor": {"r": 255, "g": 140, "b": 0, "a": 0.18},
              "borderColor": {"r": 255, "g": 120, "b": 0, "a": 1.0}}
#: 대화상자 보류 중인지 확인하는 짧은 상한(초) — 허용 액션이 멈추지 않게.
DIALOG_PROBE_S = 0.5
DIALOG_OPEN_MESSAGE = ("사람에게 보여 둔 대화상자가 이 탭에 열려 있어 실행하지 않았습니다 — 사람이 창에서 닫은 뒤 다시 "
                       "시도하세요(browser_control_wait 로 기다릴 수 있음).")
PRE_SWITCH_HINT = ("직접 조작할 사이트라 이동 전에 창을 미리 열었습니다(서버 수명 동안 유지). 브라우저를 다시 열어 이전 페이지 "
                   "상태(입력값 등)는 사라졌습니다 — 다시 관찰하세요.")


@dataclass
class _ManualHandoff:
    handoff_id: str
    key: tuple
    action: ActionType
    page: Any
    frame: Any = None
    started: float = field(default_factory=time.monotonic)
    deadline: float = 0.0
    ended: Optional[str] = None
    banner: Optional[str] = None
    watch: Any = None
    highlighted: bool = False
    _subs: List[tuple] = field(default_factory=list)

    def mark(self, how: str) -> None:
        if self.ended is None:
            self.ended = how


class ManualSiteMixin:
    """BrowserMCPServer 의 직접 조작할 사이트(WS-64) — 상태는 BrowserMCPServer.__init__(_manual_store) 와 지연 생성."""

    # ------------------------------------------------------------------ 목록·판정

    def _manual_list(self) -> Any:
        store = getattr(self, "_manual_store", None)
        if store is None:
            from security.manual_sites import ManualSites

            return ManualSites()
        return store.current()

    def _manual_available(self) -> bool:
        """지금 사람이 볼 창이 있는가(on-demand 는 headed 일 때만, 고정 headless 는 없음)."""
        if not self._human_can_see():
            return False
        w = self._window
        return w is None or w.state == "headed"

    def _manual_state(self) -> Dict[str, Any]:
        return self.__dict__.setdefault("_manual_st", {"pending": None, "held": {}, "pages": [], "contexts": []})

    async def _manual_site_gate(self, action: ActionType, params: Dict[str, Any]) -> Optional[ActionResult]:
        """_call_tool 직렬 구간의 관문(입력 검증 뒤·HITL 앞). 막으면 거부 결과, 아니면 None."""
        from security.manual_sites import MANUAL_ALLOWED, handoff_data, manual_site_hit, refusal_message

        sites = self._manual_list()
        if sites.empty:
            return None
        self._manual_watch_dialogs()
        ctx = getattr(self._dispatcher, "ctx", None)
        if action in MANUAL_ALLOWED:
            return await self._manual_dialog_probe(action, ctx)
        hit = await manual_site_hit(ctx, action, params, sites)
        if hit is None:
            return None
        if self._recipes is not None:
            self._recipes.reset("manual_site")  # 사람 단계가 빠진 궤적을 레시피로 남기지 않는다
        target = None
        manual_target = getattr(self._dispatcher, "manual_target", None)
        if callable(manual_target):
            target = manual_target(params)
        available = self._manual_available()
        handoff_id = await self._manual_begin(action, params, target, ctx) if available else None
        result = self._error_result(action, ErrorCode.HITL_UNATTENDED_BLOCKED, refusal_message(sites),
                                    data=handoff_data(action, available=available, handoff_id=handoff_id,
                                                      target=target))
        result.retry_safe = False
        return result

    # ------------------------------------------------------------------ 인계 시작·끝

    def _manual_key(self, action: ActionType, params: Dict[str, Any]) -> tuple:
        handle = None
        element_id = params.get("element_id")
        if element_id and self._engine is not None:
            handle = self._engine.get_handle(element_id)
        node = getattr(handle, "backend_node_id", None) or (getattr(handle, "css_path", None) if handle else None)
        tab = self._core.active_tab_id if self._core is not None else ""
        return (tab, action.value, node or element_id, params.get("selector"), params.get("x"), params.get("y"),
                params.get("key"))

    async def _manual_begin(self, action: ActionType, params: Dict[str, Any], target: Optional[Dict[str, str]],
                            ctx: Any) -> str:
        """대기 인계 하나를 만든다(같은 대상이면 같은 handoff_id, 다른 대상이면 기존 인계를 대체)."""
        from security.manual_sites import HANDOFF_TTL_S, OUTCOME_PAGE_CHANGED

        st = self._manual_state()
        key = self._manual_key(action, params)
        cur: Optional[_ManualHandoff] = st["pending"]
        if cur is not None:
            self._manual_tick(cur)
            if cur.ended is None and cur.key == key:
                return cur.handoff_id
            await self._manual_end()
        top = getattr(ctx, "root_page", None) or getattr(ctx, "page", None)
        frame = getattr(ctx, "page", None) if getattr(ctx, "root_page", None) is not None else None
        h = _ManualHandoff(handoff_id=f"mh-{secrets.token_hex(4)}", key=key, action=action, page=top, frame=frame)
        h.deadline = h.started + HANDOFF_TTL_S
        st["pending"] = h

        def on_nav(fr: Any) -> None:
            main = getattr(top, "main_frame", None)
            if fr is main or (frame is not None and fr is frame):
                h.mark(OUTCOME_PAGE_CHANGED)

        def on_close(*_a: Any) -> None:
            h.mark(OUTCOME_PAGE_CHANGED)

        for event, fn in (("framenavigated", on_nav), ("close", on_close)):
            try:
                top.on(event, fn)
                h._subs.append((event, fn))
            except Exception:  # noqa: BLE001 - 가짜 페이지
                pass
        handle = None
        element_id = params.get("element_id")
        if element_id and self._engine is not None:
            handle = self._engine.get_handle(element_id)
        h.banner = await self._manual_banner_text(action, params, target, handle)
        try:
            await self._show_control_banner(h.banner)
        except Exception:  # noqa: BLE001 - 표시 실패: 인계는 유지(stderr 안내)
            logger.debug("직접 조작 안내 띠 실패", exc_info=True)
        await self._manual_watch_and_highlight(h, action, params, handle)
        _stderr_line(f"agent-browser serve: [직접 조작] {_VERBS.get(action, action.value)} — 사람이 창에서 표시된 대상을 "
                     f"직접 조작하세요(끝나면 `agent-browser handoff done`). id={h.handoff_id}")
        return h.handoff_id

    async def _manual_watch_and_highlight(self, h: _ManualHandoff, action: ActionType, params: Dict[str, Any],
                                          handle: Any) -> None:
        from browser.manual_watch import ManualClickWatch

        page = h.page
        if page is None or not hasattr(page, "context"):
            return
        backend = getattr(handle, "backend_node_id", None) if handle is not None else None
        css = None
        if not backend:
            css = (getattr(handle, "css_path", None) if handle is not None else None) or params.get("selector")
        watch = ManualClickWatch()
        h.watch = watch
        if backend or css:
            events = ("change",) if action in _CHANGE_ACTIONS else ("click",)
            frame_url = str(getattr(h.frame, "url", "") or "") if h.frame is not None else None
            await watch.start(page, frame_url=frame_url, backend_node_id=backend, css=css, events=events)
        node = watch.backend_node_id or backend
        banner = getattr(self, "_banner", None)
        if node and banner is not None and h.frame is None:
            try:
                await asyncio.wait_for(banner[1].send("Overlay.highlightNode", {
                    "highlightConfig": _HIGHLIGHT, "backendNodeId": int(node)}), 2.0)
                h.highlighted = True
            except Exception:  # noqa: BLE001 - 표시 실패: 안내 띠만
                logger.debug("직접 조작 대상 표시 실패", exc_info=True)

    async def _manual_banner_text(self, action: ActionType, params: Dict[str, Any],
                                  target: Optional[Dict[str, str]], handle: Any) -> str:
        """안내 띠 문구(사람 창에만). 비밀 자리표시자·민감 칸 값은 가린다 — 응답·로그에는 값을 쓰지 않는다."""
        from interface.handoff import display_safe, mask_code_like

        name = display_safe(mask_code_like(str((target or {}).get("name") or "")), 40) or "(표시한 대상)"
        value = ""
        if action is ActionType.TYPE_TEXT:
            value = await self._manual_value_label(str(params.get("text") or ""), name, handle)
        elif action is ActionType.SELECT_OPTION:
            # WS-64 R1 B7: 고를 값도 type_text 와 같은 민감 판정(자리표시자·민감 이름·민감 칸) — 민감이면 가린다.
            raw = params.get("value") if params.get("value") is not None else params.get("index")
            value = await self._manual_value_label(str(raw if raw is not None else ""), name, handle)
        elif action is ActionType.CHECK_BOX:
            value = "체크" if params.get("checked", True) else "체크 해제"
        elif action is ActionType.PRESS_KEY:
            value = display_safe(str(params.get("key") or ""), 20)
        text = f"직접 해 주세요: {_VERBS.get(action, action.value)} {name}"
        if value:
            text += f" [{value}]"
        return text + " — 끝나면 `agent-browser handoff done`"

    async def _manual_value_label(self, text: str, name: str, handle: Any) -> str:
        from interface.handoff import display_safe
        from security.secrets import KEY_PATTERN

        if not text:
            return ""
        store = getattr(self, "secrets", None)
        resolved = False
        if store is not None:
            try:
                resolved = bool(store.resolve(text).resolved)
            except Exception:  # noqa: BLE001
                resolved = True
        if resolved or KEY_PATTERN.match(text):
            return f"저장된 자격증명 {display_safe(text, 40)}"
        if any(w in name.lower() for w in _SENSITIVE_WORDS):
            return "(민감 칸 — 값 가림)"
        check = getattr(self._dispatcher, "is_sensitive_field", None)
        if handle is not None and callable(check):
            try:
                if await asyncio.wait_for(check(handle), 2.0):
                    return "(민감 칸 — 값 가림)"
            except Exception:  # noqa: BLE001 - 판정 불가: 가린다
                return "(민감 칸 — 값 가림)"
        from security.secret_registry import mask_registered

        return mask_registered(display_safe(text, 40))

    def _manual_tick(self, h: _ManualHandoff) -> None:
        from security.manual_sites import OUTCOME_CLICKED, OUTCOME_EXPIRED

        if h.ended is not None:
            return
        if h.watch is not None and h.watch.triggered:
            h.mark(OUTCOME_CLICKED)
        elif time.monotonic() >= h.deadline:
            h.mark(OUTCOME_EXPIRED)

    def _manual_note_done(self) -> None:
        """사람이 `agent-browser handoff done` — 대기 인계를 done 으로(조작권·창·비밀 입력 보호는 그대로)."""
        from security.manual_sites import OUTCOME_DONE

        h = self._manual_state()["pending"]
        if h is not None:
            h.mark(OUTCOME_DONE)

    async def _manual_end(self) -> None:
        st = self._manual_state()
        h: Optional[_ManualHandoff] = st["pending"]
        st["pending"] = None
        if h is None:
            return
        for event, fn in h._subs:
            try:
                h.page.remove_listener(event, fn)
            except Exception:  # noqa: BLE001
                pass
        if h.watch is not None:
            await h.watch.stop()
        banner = getattr(self, "_banner", None)
        if h.highlighted and banner is not None:
            try:
                await asyncio.wait_for(banner[1].send("Overlay.hideHighlight"), 2.0)
            except Exception:  # noqa: BLE001 - 닫힌 탭: 표시도 사라졌다
                pass
        if h.banner is not None and getattr(self, "_control_banner", None) == h.banner:
            try:
                await self._show_control_banner(None)
            except Exception:  # noqa: BLE001
                pass

    def _manual_pending_view(self) -> Optional[Dict[str, Any]]:
        """control_status 에 싣는 대기 인계(고정 값만)."""
        h: Optional[_ManualHandoff] = self._manual_state()["pending"]
        if h is None:
            return None
        self._manual_tick(h)
        return {"handoff_id": h.handoff_id, "reason": "manual_site", "action": h.action.value,
                "expires_in_s": max(0, int(h.deadline - time.monotonic()))}

    async def _manual_wait(self, timeout: float) -> Optional[Dict[str, Any]]:
        """browser_control_wait: 대기 인계가 있으면 결과(clicked|page_changed|done|expired|timeout). 없으면 None."""
        from interface.server_handoff import _RELEASE_HINT

        h: Optional[_ManualHandoff] = self._manual_state()["pending"]
        if h is None:
            return None
        deadline = time.monotonic() + timeout
        while True:
            self._manual_tick(h)
            if h.ended is not None or time.monotonic() >= deadline:
                break
            await self._poll_handoff()
            self._manual_tick(h)
            if h.ended is not None:
                break
            await asyncio.sleep(0.05)
        handoff = {"handoff_id": h.handoff_id, "reason": "manual_site", "action": h.action.value}
        data: Dict[str, Any] = {"control": self._control_view(), "changed": h.ended or "timeout",
                                "handoff": handoff}
        if h.ended is not None:
            await self._manual_end()
            self._bump_all_epochs("manual_handoff")  # 사람이 화면을 바꿨을 수 있다 — 이전 element_id 무효
            data["hint"] = _RELEASE_HINT
            data["snapshot_epoch"] = self._engine.epoch if self._engine else 0
        if self._window is not None:
            data["window"] = self._take_window_report()
        return {"success": True, "data": data}

    # ------------------------------------------------------------------ 대화상자 보류(R2-3)

    def _manual_dialog_hold(self, page: Any) -> bool:
        """이 탭의 대화상자를 사람에게 둘까 — 사람이 볼 창 + 목록 출처 프레임을 가진 탭에서만."""
        from security.manual_sites import manual_hit_sync

        sites = self._manual_list()
        if sites.empty or page is None or not self._manual_available():
            return False
        try:
            hit = manual_hit_sync(sites, page) is not None  # blob 안쪽 출처·목록 파일 판정 불가(R1) 포함
        except Exception:  # noqa: BLE001 - 판정 불가: 지금처럼 처리
            return False
        if hit:
            self._manual_state()["held"][id(page)] = time.monotonic()
        return hit

    def _manual_watch_dialogs(self) -> None:
        """디스패처 리스너가 없는 탭(팝업 등)도 대화상자를 Playwright 자동 닫기 대신 이 서버가 판정한다."""
        core = self._core
        if core is None or not self._started:
            return
        st = self._manual_state()
        try:
            context = core.context_for("mcp-session")
        except Exception:  # noqa: BLE001
            context = None
        if context is not None and not any(c is context for c in st["contexts"]):
            try:
                context.on("page", self._manual_attach_dialog)
                st["contexts"].append(context)
            except Exception:  # noqa: BLE001
                pass
        from interface.server_window import _all_tabs

        for tab in _all_tabs(core):
            self._manual_attach_dialog(getattr(tab, "page", None))

    def _manual_attach_dialog(self, page: Any) -> None:
        st = self._manual_state()
        if page is None or any(p is page for p in st["pages"]):
            return
        try:
            page.on("dialog", self._manual_on_dialog)
        except Exception:  # noqa: BLE001
            return
        st["pages"].append(page)

    def _manual_on_dialog(self, dialog: Any) -> None:
        page = getattr(dialog, "page", None)
        for runtime in self._workspace_runtimes().values():
            disp = getattr(runtime, "dispatcher", None)
            if any(p is page for p in getattr(disp, "_dialog_pages", []) or []):
                return  # 디스패처 리스너가 처리한다(보류 판정 포함)
        if self._manual_dialog_hold(page):
            return  # 사람에게 둔다
        try:
            kind = str(dialog.type)
        except Exception:  # noqa: BLE001
            kind = ""

        async def _settle() -> None:  # Playwright 기본 동작과 같게(beforeunload 수락, 나머지 거절)
            try:
                await (dialog.accept() if kind == "beforeunload" else dialog.dismiss())
            except Exception:  # noqa: BLE001
                pass

        asyncio.ensure_future(_settle())

    async def _manual_dialog_probe(self, action: ActionType, ctx: Any) -> Optional[ActionResult]:
        """보류한 대화상자가 이 탭에 열려 있으면 허용 액션을 짧은 시간 안에 dialog_open 신호로 돌려준다(멈춤 없음)."""
        top = getattr(ctx, "root_page", None) or getattr(ctx, "page", None)
        held = self._manual_state()["held"]
        if top is None or id(top) not in held:
            return None
        try:
            await asyncio.wait_for(top.evaluate("1"), DIALOG_PROBE_S)
        except asyncio.TimeoutError:
            return self._error_result(action, ErrorCode.TIMEOUT, DIALOG_OPEN_MESSAGE,
                                      data={"dialog_open": True})
        except Exception:  # noqa: BLE001 - 닫힌 탭 등: 보류 기록만 지운다
            pass
        held.pop(id(top), None)
        return None

    # ------------------------------------------------------------------ on-demand 미리 창(R2-2)

    async def _manual_pre_switch(self, action: Optional[ActionType], arguments: Dict[str, Any]) -> None:
        """목록 사이트로 가는 navigate·tab_control create 를 **실행하기 전**에만 창을 미리 연다(sticky).

        이미 headed 거나 목록이 비면 아무것도 하지 않는다. 디스플레이가 없으면 열지 않는다(인계는 available=false).
        """
        from interface.handoff import HOLDER_HUMAN

        w = self._window
        if w is None or action not in (ActionType.NAVIGATE, ActionType.TAB_CONTROL) or self.hub.holder == HOLDER_HUMAN:
            return
        args = arguments or {}
        if action is ActionType.TAB_CONTROL and args.get("command") != "create":
            return
        url = args.get("url")
        sites = self._manual_list()
        if not url or sites.empty or getattr(sites, "broken", "") or w.state != "headless":
            return  # 목록 파일을 읽지 못함(R1 B3): 모든 이동에 창을 열지 않는다(부작용 액션은 관문이 막는다)
        try:
            hit = sites.match(str(url))
        except Exception:  # noqa: BLE001 - 깨진 주소: 이동이 실패할 것 — 창을 열지 않는다
            hit = None
        if hit is None:
            return
        from interface.login_cli import display_available

        if not display_available():
            return
        if not self._started:
            await self.start()
        w.sticky = True
        w.sticky_reason = f"직접 조작할 사이트 {hit}"
        out = await self._switch_window(True, "manual_site")
        if out.get("ok"):
            w.notice = {"reason": "manual_site", "hint": PRE_SWITCH_HINT}
            _stderr_line("agent-browser serve: 직접 조작할 사이트로 이동 — 창을 미리 열었습니다(서버 수명 동안 유지)")
            self._sync_window()
