"""BrowserMCPServer 의 필요할 때만 창(WS-34): 전환 관문·창 전환·on-demand 진입.

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import logging
import asyncio
import time
from typing import Any, Dict, List, Optional

from contracts import ActionResult, ActionType, ErrorCode
from interface.server_common import _stderr_line

#: 분리 전과 같은 로거 이름을 쓴다(로그 레코드 불변).
logger = logging.getLogger("interface.mcp_server")


class WindowMixin:
    """BrowserMCPServer 의 필요할 때만 창(WS-34): 전환 관문·창 전환·on-demand 진입 (WS-48 믹스인 — 상태는 BrowserMCPServer.__init__)."""

    def _sync_window(self) -> None:
        """창 상태를 사람용 상태 파일(control.json — `agent-browser control status`)에도 싣는다."""
        if self._window is not None:
            self.hub.set_window(self._window.info())

    def _hold_gate(self) -> None:
        self._gate_holds += 1
        self._gate.clear()

    def _release_gate(self) -> None:
        self._gate_holds = max(0, self._gate_holds - 1)
        if self._gate_holds == 0:
            self._gate.set()

    async def _await_gate(self, timeout: Optional[float] = None) -> bool:
        """창 전환이 끝날 때까지 기다린다. 상한 안에 끝나면 True."""
        if self._gate.is_set():
            return True
        try:
            await asyncio.wait_for(self._gate.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def _drain_calls(self, timeout: Optional[float] = None) -> bool:
        """진행 중인 도구 호출이 끝나길 기다린다(전환과 겹치지 않게). 상한 안에 끝나면 True."""
        from interface import on_demand

        limit = on_demand.DRAIN_TIMEOUT_S if timeout is None else timeout
        deadline = time.monotonic() + limit
        # R1 NB-5: wait_for 는 timeout_ms 상한이 없다 — 잠깐(WAIT_CANCEL_AFTER_S, 상한의 절반 이하) 기다려도
        # 남아 있으면 멈춘다(E_TIMEOUT 결과로 돌아감). 다른 호출은 지금처럼 끝나길 기다린다.
        cancel_at = time.monotonic() + min(on_demand.WAIT_CANCEL_AFTER_S, limit / 2)
        while self._inflight > 0:
            now = time.monotonic()
            if now >= deadline:
                return False
            if now >= cancel_at:
                for task in list(self._wait_calls):
                    task.cancel()
            await asyncio.sleep(0.01)
        return True

    def _spawn_switch(self, headed: bool, reason: str,
                      snapshot: Optional[List[Dict[str, Any]]] = None) -> None:
        """감시 경로(사건 처리)에서 전환을 시작한다. 관문은 **지금** 닫는다 — 이 뒤에 들어오는 도구
        호출은 전환이 끝날 때까지 기다린다(태스크가 늦게 돌아도)."""
        self._hold_gate()

        async def _run() -> None:
            try:
                await self._switch_window(headed, reason, snapshot=snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 실패는 _switch_window 가 상태로 남긴다
                logger.warning("창 전환 실패(%s)", reason, exc_info=True)
            finally:
                self._release_gate()

        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            self._release_gate()
            return
        self._window_tasks.add(task)
        task.add_done_callback(self._window_tasks.discard)

    def _tab_snapshot(self) -> List[Dict[str, Any]]:
        core = self._core
        tabs = core.tabs() if core is not None else []
        if not tabs:
            return list(self._window.last_tabs) if self._window is not None else []
        active = core.active_tab_id
        return [{"tab_id": t.tab_id, "url": str(getattr(t.page, "url", "") or ""),
                 "active": t.tab_id == active} for t in tabs]

    async def _switch_window(self, headed: bool, reason: str, *, force: bool = False,
                             snapshot: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """같은 프로필을 닫고 headless=not headed 로 다시 연 뒤 서버 연결을 모두 다시 단다(WS-34).

        순서: 관문 닫기(새 호출 대기) → 진행 중 호출 끝나길 대기 → 탭 URL·활성 탭 기록 → 영속 컨텍스트
        닫기 → 같은 프로필로 다시 열기(검증 프록시 실행 인자 포함) → 연결 재설치(_wire_session: 문서 상태·
        route 가드·탭·CDP·디스패처) → 탭 복원 → epoch 올림 → (창이면) 안내 띠. 프로필 잠금은 내내 유지.
        실패하면 fail-closed: 브라우저를 닫고 state=failed — 다음 도구 호출이 headless 로 다시 연다.
        반환: {ok, reopened, window, error?}.
        """
        from browser.serve_profile import safe_reason
        from interface.on_demand import HEADED_HINT, HEADLESS_HINT

        w = self._window
        assert w is not None
        target = "headed" if headed else "headless"
        self._hold_gate()
        try:
            async with self._switch_lock:
                if w.state == target and not force:
                    return {"ok": True, "reopened": False, "window": dict(w.info(), reopened=False)}
                if self._core is None:
                    return {"ok": False, "reopened": False, "error": "브라우저가 시작되지 않음",
                            "window": w.info()}
                if not await self._drain_calls():
                    return {"ok": False, "reopened": False,
                            "error": "진행 중인 도구 호출이 끝나지 않아 창을 전환하지 못함",
                            "window": w.info()}
                snap = snapshot if snapshot is not None else self._tab_snapshot()
                w.state = "switching"
                self._sync_window()
                t0 = time.perf_counter()
                try:
                    tabs_info, skipped = await self._reopen_browser(not headed, snap)
                except BaseException as exc:
                    await self._fail_closed(safe_reason(exc), snap)
                    if not isinstance(exc, Exception):
                        raise  # 취소(SIGTERM 등) — 정리했으니 그대로 올린다
                    return {"ok": False, "reopened": False, "error": safe_reason(exc),
                            "window": w.info()}
                ms = round((time.perf_counter() - t0) * 1000, 1)
                w.state = target
                w.error = ""
                w.last_tabs = [] if headed else w.last_tabs
                if self._engine is not None:
                    self._engine.bump_epoch("window_reopen")
                self._switch_seq += 1
                info = dict(w.info(), reopened=True, reason=reason, switch_ms=ms,
                            tabs=tabs_info, skipped=skipped,
                            hint=HEADED_HINT if headed else HEADLESS_HINT)
                if self._engine is not None:
                    info["snapshot_epoch"] = self._engine.epoch
                self._last_window_result = info
                self._window_unreported = True
                self._sync_window()
                if headed and self._control_banner:
                    await self._set_banner(self._control_banner)
                _stderr_line(f"agent-browser serve: 창 {'열림' if headed else '닫힘(headless 복귀)'} "
                             f"({reason}, {ms:g}ms, 탭 {len(tabs_info)}개 복원)")
                return {"ok": True, "reopened": True, "window": info}
        finally:
            self._release_gate()

    async def _reopen_browser(self, headless: bool, snapshot: List[Dict[str, Any]]) -> tuple:
        core = self._core
        # R1 NB-2: 다시 열 실행 인자를 먼저 만든다 — 검증 프록시가 실제로 듣고 있지 않으면 여기서
        # 예외(전환하지 않음 → _switch_window 가 fail-closed: 브라우저 닫고 state=failed + 이유).
        core._launch_kwargs()
        # 창을 닫으면 그 창의 오버레이(확인 코드·안내 띠)도 함께 사라진다 — 창에 없는 코드는 무효.
        self._banner = None
        if self._code_where == "overlay":
            self.hub.withdraw_codes()
            self._code_on_overlay = False
            self._code_where = None
        self._ctx_closed = False
        # 세션 쿠키(만료 없음)는 Chromium 이 브라우저를 다시 열 때 버린다(WS-32 실측) — 이 서버가 이미
        # 가진 세션 쿠키만 그대로 다시 넣는다(사이트가 준 값 그대로, 수명·값을 바꾸지 않음).
        session_cookies = await self._session_cookies()
        await core.close_persistent()
        await core.open_persistent(headless=headless)
        return await self._wire_session(restore=snapshot, cookies=session_cookies)

    def _on_demand_released(self) -> None:
        """사람이 조작권을 돌려줬다(D2): 창에서 해결한 사이트를 기록하고, sticky 가 아니면 headless 로."""
        w = self._window
        assert w is not None
        w.opened_for_request = None
        if w.state not in ("headed", "switching"):
            return
        urls = [str(getattr(t.page, "url", "") or "") for t in self._core.tabs()] \
            if self._core is not None else []
        w.note_solved(urls + list(self._request_urls))
        if not w.keep_window_after_release():
            self._spawn_switch(False, "released")
        else:
            self._sync_window()

    async def _watch_window(self) -> None:
        """창이 열린 동안: 마지막 탭 URL 기록, 사람이 창을 닫았는지, 요청 만료를 본다."""
        from interface import on_demand

        w = self._window
        if w is None or w.state != "headed" or self._switch_lock.locked() or self._core is None:
            return
        if self._gate_holds > 0:
            # R1 NB-5: 이미 시작한 전환(태스크가 아직 첫 틱 전)이 있다 — 다시 판정·spawn 하지 않는다.
            return
        tabs = self._core.tabs()
        if tabs and not self._ctx_closed:
            active = self._core.active_tab_id
            w.last_tabs = [{"tab_id": t.tab_id, "url": str(getattr(t.page, "url", "") or ""),
                            "active": t.tab_id == active} for t in tabs]
            opened = w.opened_for_request
            st = self.hub.status()
            if (opened is not None and st["holder"] == "agent" and st["requested"]
                    and time.monotonic() - opened > on_demand.REQUEST_WINDOW_TTL_S):
                # 사람이 take 하지 않았다 — 요청을 거두고 창도 닫는다.
                w.opened_for_request = None
                self.hub.cancel_request()
                w.notice = {"reason": "request_expired",
                            "hint": "사람이 조작권을 가져가지 않아 요청을 거두고 창을 닫았습니다."}
                _stderr_line("agent-browser serve: 조작권 요청 만료 — 창을 닫습니다")
                if not w.keep_window_after_release():
                    self._spawn_switch(False, "request_expired")
                else:
                    self._sync_window()
            return
        # 사람이 창을 직접 닫았다(창의 탭이 모두 닫힘·브라우저 종료) — 창이 없으니 조작권을 돌리고
        # headless 로 다시 열어 마지막 탭을 복원한다.
        w.opened_for_request = None
        w.notice = {"reason": "window_closed",
                    "hint": ("사람이 창을 닫았습니다 — 조작권을 에이전트에게 돌리고 headless 로 다시 열어 "
                             "마지막 탭을 복원했습니다. 다시 관찰하세요.")}
        self.hub.release_by_server()
        if self._engine is not None:
            self._engine.bump_epoch("window_closed_by_human")
        self._control_banner = None
        _stderr_line("agent-browser serve: 사람이 창을 닫음 — headless 로 돌아갑니다")
        self._spawn_switch(False, "window_closed", snapshot=list(w.last_tabs))

    async def _on_demand_enter(self, action: ActionType) -> Optional[ActionResult]:
        """on-demand 도구 호출 입구: 전환 중이면 기다리고, 실패 상태면 headless 로 다시 연다."""
        from interface import on_demand

        w = self._window
        assert w is not None
        if self._started and self.hub.opened:
            await self._poll_handoff()
        if not await self._await_gate(on_demand.SWITCH_WAIT_S):
            return self._error_result(
                action, ErrorCode.TIMEOUT,
                f"브라우저 창 전환 중이라 {int(on_demand.SWITCH_WAIT_S)}초 안에 실행하지 못했습니다 — "
                "잠시 뒤 다시 호출하세요.",
                data={"window": w.info()},
            )
        if self._started and w.state == "failed":
            out = await self._switch_window(False, "recover", force=True)
            if not out.get("ok"):
                return self._error_result(
                    action, ErrorCode.PAGE_CRASHED,
                    "브라우저를 다시 열지 못했습니다(" + str(out.get("error") or "") + ") — "
                    "운영자에게 serve 재시작을 요청하세요.",
                    data={"window": w.info()},
                )
            self._recovered_notice = True
        return None
