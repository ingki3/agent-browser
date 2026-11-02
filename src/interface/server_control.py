"""BrowserMCPServer 의 사람용 제어 창(WS-77, #210): 창 안 [다 했어요] → 기존 반납 흐름 + 인계 응답 신호.

* 제어 창(interface.control_window)은 조작권 요청(창이 있는 serve) 때 **관문+drain 구간 안에서** 만들고 코어 관리 탭에서
  뺀다(B1). 에이전트 도구는 관리 탭만 대상으로 삼으므로 이 창에 닿지 못한다.
* [다 했어요] 는 hub 대기 사건 큐 → poll() 'released'(release_source=window_done) → ``_poll_handoff`` 의 기존 반납
  흐름(epoch·관문·로그인 기록·on-demand headless 복귀·레시피 리셋·띠 내림) 그대로(B4). 자동 반납은 없다(B7).
* 사람이 창을 닫거나(NB2) 창 문서를 다른 곳으로 옮기면(B3) 완료로 보지 않고 1회 다시 띄운다. 그 뒤에는 띠+CLI 안내.
* 모든 반납·만료·창 전환·서버 종료에서 창을 닫는다.
* 응답의 ``data.human_handoff``: expires_at·human_activity_seen(seen/not_seen/unknown — 브라우저 수준 신호)·탭 origin
  변화·next_step·고정 문구 사용자 안내(tell_user — 페이지 유래 글자 없음, NB5).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from interface.server_common import _iso, _stderr_line

#: 분리 전과 같은 로거 이름을 쓴다(로그 레코드 불변).
logger = logging.getLogger("interface.mcp_server")

#: 텔레그램 등 사람에게 그대로 전할 고정 문구(NB5 — 페이지·사유 글자를 넣지 않는다).
TELL_WINDOW = "브라우저 옆에 뜬 작은 'agent-browser' 창에서, 로그인·확인을 모두 마친 뒤 [다 했어요]를 누르세요."
#: NB4: user-chrome 은 사용자의 Chrome 에 창을 만든다.
TELL_WINDOW_USER_CHROME = ("사용 중인 Chrome 에 뜬 작은 'agent-browser' 창에서, 로그인·확인을 모두 마친 뒤 "
                           "[다 했어요]를 누르세요.")
TELL_CLI = "브라우저 창에서 해결한 뒤 맥 터미널에서 `agent-browser control release` 를 실행하세요."
TELL_HEADLESS = ("지금 서버는 사람이 볼 브라우저 창이 없습니다 — 운영자에게 창을 띄울 수 있는 방식"
                 "(`--browser on-demand`)으로 다시 실행해 달라고 하세요.")
#: WS-78 T4(#215): 끝난 인계의 tell_user — 진행 중 문구(TELL_*)를 다시 쓰지 않는 종료용 고정 문구.
TELL_ENDED = {
    "released": "사람의 조작이 끝났습니다 — 더 하실 일은 없습니다. 결과를 확인해 알려 드리겠습니다.",
    "request_expired": "조작 요청이 만료됐습니다 — 지금은 하실 일이 없습니다. 필요하면 다시 요청하겠습니다.",
}
NEXT_WAIT = "browser_control_wait 로 기다리세요(사람이 [다 했어요]를 누르면 바로 돌아옵니다)."
NEXT_OBSERVE = ("browser_observe_page 로 다시 관찰해 결과(로그인·해결 여부)를 확인하세요 — human_activity_seen 이 "
                "not_seen 이어도 확인하세요(제출 없는 입력·클릭은 활동으로 잡히지 않습니다).")
#: NB1: DevTools 띠의 ▶·⤼ 는 눌러도 아무 일 없이 그 아래 페이지가 눌린다(probe1b).
BANNER_NOT_BUTTON = "(이 띠의 ▶·⤼ 버튼 아님 — 누르면 띠 아래 페이지가 눌립니다)"
#: 탭 origin 변화 보고 상한(개).
MAX_ORIGINS = 5


def _origins(urls: List[str]) -> List[str]:
    from browser.login_watch import _origin

    out = sorted({o for o in (_origin(u) for u in urls) if o})
    return out


def _reason_display(reason: Any, limit: int) -> str:
    """사람에게 보이는 사유(제어 창·띠·stderr) — 에이전트 응답과 같은 가림(R1 F3: 등록 비밀·토큰 주소 →
    public_url.public_text) 뒤 display_safe. 가린 뒤 자른다(자른 조각이 가림을 피하지 않게)."""
    from interface.handoff import display_safe
    from security.public_url import public_text

    return display_safe(public_text(str(reason or "")), limit)


class ControlWindowMixin:
    """BrowserMCPServer 의 사람용 제어 창·인계 응답 신호 (WS-77 믹스인 — 상태는 BrowserMCPServer.__init__)."""

    # -- 상태 ---------------------------------------------------------------

    def _control_where(self) -> str:
        return "user_chrome" if self.browser_mode == "user-chrome" else "agent_browser"

    def _control_window_possible(self) -> bool:
        """지금 제어 창을 띄울 수 있는가 — 사람이 볼 창이 있고, on-demand 면 창이 열려 있을 때."""
        if not self._human_can_see() or not self._started or self._core is None:
            return False
        return self._window is None or self._window.state == "headed"

    def _handoff_live(self) -> bool:
        from interface.handoff import HOLDER_HUMAN

        return bool(self.hub.requested or self.hub.holder == HOLDER_HUMAN)

    def _control_window_state(self) -> str:
        cw = self._control_window
        if cw is not None and cw.request_id == self.hub.request_id and cw.is_open:
            return "open"
        if self._control_spawning:
            return "opening"
        if self._handoff_live() and self._control_gave_up == self.hub.request_id:
            return "unavailable"
        return "none"

    def _tell_user(self) -> str:
        if not self._human_can_see():
            return TELL_HEADLESS
        if self._control_window_state() in ("open", "opening"):
            return TELL_WINDOW_USER_CHROME if self._control_where() == "user_chrome" else TELL_WINDOW
        return TELL_CLI

    def _request_expires_at(self) -> Optional[str]:
        """on-demand 가 요청으로 연 창의 만료 시각(그 밖의 방식은 요청이 만료되지 않는다 → None)."""
        from interface import on_demand

        w = self._window
        if w is None or w.opened_for_request is None or not self.hub.requested:
            return None
        left = max(0.0, w.opened_for_request + on_demand.REQUEST_WINDOW_TTL_S - time.monotonic())
        return _iso(time.time() + left)

    def _tab_origins_now(self) -> List[Tuple[str, str]]:
        """모든 작업 공간 탭의 (작업 공간, origin) — 보여 줄 때 호출한 작업 공간으로 거른다(R1 F4)."""
        from browser.workspace import DEFAULT_WORKSPACE
        from interface.server_window import _all_tabs

        try:
            tabs = _all_tabs(self._core) if self._core is not None else []
            return sorted({(str(getattr(t, "workspace", None) or DEFAULT_WORKSPACE), o)
                           for t in tabs for o in _origins([str(getattr(t.page, "url", "") or "")])})
        except Exception:  # noqa: BLE001 - 탭을 못 읽음(가짜 코어·닫히는 중): 변화 판정 없음
            return []

    def _tab_origin_view(self, now_pairs: Optional[List[Tuple[str, str]]] = None) -> Dict[str, Any]:
        """호출한 작업 공간 탭의 origin 만(R1 F4 — 다른 작업 공간 탭은 tab list 의 other_workspace_tabs 개수로만)."""
        from browser.workspace import current_workspace

        ws = current_workspace()
        pairs = self._tab_origins_now() if now_pairs is None else now_pairs
        now = sorted({o for w, o in pairs if w == ws})
        before = sorted({o for w, o in self._request_origins if w == ws})
        return {"changed": set(now) != set(before), "before": before[:MAX_ORIGINS], "now": now[:MAX_ORIGINS]}

    def _handoff_view(self, activity: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """응답에 싣는 인계 신호(data.human_handoff). 요청·조작권·막 끝난 인계가 없으면 None."""
        from browser.login_watch import ACTIVITY_UNKNOWN

        end = self._handoff_end
        if self._handoff_live():
            from interface.handoff import HOLDER_HUMAN

            out: Dict[str, Any] = {
                "request_id": self.hub.request_id,
                "state": "human_holding" if self.hub.holder == HOLDER_HUMAN else "waiting_human",
                "expires_at": self._request_expires_at(),
                "control_window": self._control_window_state(),
                "tell_user": self._tell_user(),
                "human_activity_seen": activity or ACTIVITY_UNKNOWN,
                "tab_origins": self._tab_origin_view(),
                "next_step": NEXT_WAIT,
            }
            return out
        if end is None:
            return None
        out = {"request_id": end.get("request_id"), "state": end.get("state"),
               # WS-78 T4: 진행 중 응답과 같은 필드 — 끝난 요청은 만료가 없고(null), 창 상태는 지금 것, 고정 종료 문구.
               "expires_at": None, "control_window": self._control_window_state(),
               "tell_user": TELL_ENDED.get(str(end.get("state")))}
        out.update({k: v for k, v in end.items() if k not in out and k != "_tab_pairs"})
        out["tab_origins"] = self._tab_origin_view(end.get("_tab_pairs") or [])  # 끝날 때 탭, 이 작업 공간 것만
        out["next_step"] = NEXT_OBSERVE
        return out

    def _live_activity_sync(self) -> str:
        from browser.login_watch import ACTIVITY_UNKNOWN

        watch = self._login_watch
        return watch.live_activity() if watch is not None else ACTIVITY_UNKNOWN

    async def _live_activity(self) -> str:
        from browser.login_watch import ACTIVITY_UNKNOWN

        watch = self._login_watch
        if watch is None:
            return ACTIVITY_UNKNOWN
        try:
            context = self._core.context_for("mcp-session") if self._started and self._core else None
        except Exception:  # noqa: BLE001
            context = None
        return await watch.peek(context)

    async def _handoff_view_async(self) -> Optional[Dict[str, Any]]:
        return self._handoff_view(await self._live_activity() if self._handoff_live() else None)

    def _note_handoff_start(self, request_id: str) -> None:
        """새 요청 — 지난 인계 기록을 지우고 이 요청의 기준(탭 origin)을 잡는다."""
        self._handoff_end = None
        self._handoff_end_unreported = False
        self._last_activity = None
        self._control_recreated = None
        self._control_gave_up = None
        self._request_origins = self._tab_origins_now()

    def _note_handoff_end(self, event: str) -> None:
        """인계가 끝났다(released|request_expired) — 다음 wait/status 응답에 그 신호를 싣는다."""
        from browser.login_watch import ACTIVITY_UNKNOWN

        self._handoff_end = {
            "request_id": self.hub.request_id,
            "state": event,
            "release_source": self.hub.release_source if event == "released" else None,
            "human_activity_seen": self._last_activity or ACTIVITY_UNKNOWN,
            "tab_origins": None,  # 보여 줄 때 호출한 작업 공간으로 거른다(_handoff_view)
            "_tab_pairs": self._tab_origins_now(),
            "ended_at": _iso(time.time()),
        }
        self._handoff_end_unreported = True

    # -- 창 ----------------------------------------------------------------

    def _agent_root_page(self) -> Any:
        ctx = getattr(self._dispatcher, "ctx", None)
        return getattr(ctx, "root_page", None) or getattr(ctx, "page", None) or self._page

    async def _open_control_window(self, request_id: str, reason: str) -> bool:
        """관문+drain 구간 안에서 제어 창을 만들고 코어 관리 탭에서 뺀다(B1). 띄웠으면 True."""
        from interface.control_window import ControlWindow

        if not self._control_window_possible():
            return False
        self._hold_gate()
        try:
            if not await self._drain_calls():
                return False
            if self.hub.request_id != request_id or not self._handoff_live() or not self._control_window_possible():
                return False
            await self._close_control_window()
            page = self._agent_root_page()
            if page is None:
                return False
            cw = ControlWindow(request_id, self.hub.queue_window_done)
            if not await cw.open(self._core, page, reason=_reason_display(reason, 80)):
                return False
            self._control_window = cw
            return True
        finally:
            self._release_gate()

    def _spawn_control_window(self, request_id: str, reason: str) -> None:
        """감시 경로에서 다시 띄운다(NB2·B3) — 관문은 지금 닫는다(이 뒤 도구 호출은 창이 생기고 빠질 때까지 기다림)."""
        self._hold_gate()
        self._control_spawning = True

        async def _run() -> None:
            try:
                if not await self._open_control_window(request_id, reason):
                    await self._control_window_gave_up(request_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("제어 창 다시 띄우기 실패", exc_info=True)
            finally:
                self._control_spawning = False
                self._release_gate()

        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            self._control_spawning = False
            self._release_gate()
            return
        self._window_tasks.add(task)
        task.add_done_callback(self._window_tasks.discard)

    async def _control_window_gave_up(self, request_id: str) -> None:
        if self.hub.request_id != request_id or not self._handoff_live():
            return
        self._control_gave_up = request_id
        from interface.handoff import HOLDER_HUMAN

        if self.hub.holder == HOLDER_HUMAN:
            await self._show_control_banner(f"사람이 조작 중 — 끝나면 `agent-browser control release` {BANNER_NOT_BUTTON}")
        else:
            await self._show_control_banner(self._request_banner(self.hub.reason, window=False))
        _stderr_line("agent-browser serve: 제어 창을 다시 띄우지 않습니다 — 끝나면 `agent-browser control release`")

    async def _close_control_window(self) -> None:
        cw, self._control_window = self._control_window, None
        if cw is not None:
            await cw.close()

    async def _sync_control_window(self) -> None:
        """poll 마다: 인계가 끝났으면 닫고, 사람이 닫았거나(NB2) 다른 문서로 옮겨졌으면(B3) 1회 다시 띄운다."""
        cw = self._control_window
        if cw is None:
            return
        if not self._handoff_live() or cw.request_id != self.hub.request_id:
            await self._close_control_window()
            return
        if not (cw.gone or cw.navigated or cw.moved):
            return
        why = "closed" if cw.gone else "navigated"
        await self._close_control_window()
        rid = cw.request_id
        if self._control_recreated != rid and self._control_window_possible():
            self._control_recreated = rid
            _stderr_line(f"agent-browser serve: 제어 창이 {'닫혀' if why == 'closed' else '다른 문서로 바뀌어'} "
                         "다시 띄웁니다(완료로 보지 않음)")
            self._spawn_control_window(rid, self.hub.reason)
        else:
            await self._control_window_gave_up(rid)

    def _taken_banner(self) -> str:
        if self._control_window_state() == "open":
            return f"사람이 조작 중 — 끝나면 옆 작은 창의 [다 했어요] 또는 `agent-browser control release` {BANNER_NOT_BUTTON}"
        return f"사람이 조작 중 — 끝나면 `agent-browser control release` {BANNER_NOT_BUTTON}"

    def _request_banner(self, reason: str, *, window: bool) -> str:
        head = f"에이전트가 사람 조작을 요청: {_reason_display(reason, 80)} — "
        if window:
            return head + f"끝나면 옆의 작은 'agent-browser' 창에서 [다 했어요] {BANNER_NOT_BUTTON}"
        return head + f"`agent-browser control take` {BANNER_NOT_BUTTON}"
