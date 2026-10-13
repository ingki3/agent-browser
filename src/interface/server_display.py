"""BrowserMCPServer 의 확인 코드 오버레이·안내 띠·화면 캡처 차단(WS-29 R1·R2).

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from contracts import ActionResult, ActionType, ErrorCode
from interface.handoff import SITE_NAME_LABEL
from interface.server_common import _iso

#: 분리 전과 같은 로거 이름을 쓴다(로그 레코드 불변).
logger = logging.getLogger("interface.mcp_server")


def code_overlay_text(code: str, kind: str, target: str, ttl_s: int) -> str:
    """창 오버레이에 띄우는 확인 코드 문구. target 은 사이트가 붙인 이름(살균된 것) — 6자리 이상 숫자열은
    여기서도 가린다(R1 NB-4)."""
    from interface.handoff import mask_code_like

    return (f"agent-browser 승인 확인 코드 {code}  (액션: {kind}, 대상: {SITE_NAME_LABEL} "
            f"{mask_code_like(target)}, {int(ttl_s)}초) — 터미널의 approve 화면과 대조해 입력")


class DisplayMixin:
    """BrowserMCPServer 의 확인 코드 오버레이·안내 띠·화면 캡처 차단(WS-29 R1·R2) (WS-48 믹스인 — 상태는 BrowserMCPServer.__init__)."""

    async def _sync_code_overlay(self) -> None:
        """hub 가 내준 확인 코드를 창 오버레이에 띄우고, 만료·입력·폐기되면 내린다.

        코드는 **창 오버레이에만** 간다 — MCP 응답·stderr·로그·상태 파일에는 쓰지 않는다. 표시가
        실패하면 hub 에 알려 코드를 무효로 한다(창에 없는 코드로는 승인할 수 없다).
        """
        if self._banner is not None and self._banner_gone():
            # R3 NB-2: 오버레이를 띄운 탭이 닫혔다 — 오버레이는 탭과 함께 사라졌다. 오버레이는 이
            # 세션 하나에만 띄우므로(_set_banner) 다른 탭에 코드가 남았을 수 없다. 창에 없는 코드는
            # 무효로 한다(사람은 approve 를 다시 실행해 지금 탭에 새 코드를 띄운다).
            self._banner = None
            if self._code_on_overlay and self._code_where != "window":
                self.hub.withdraw_codes()
        job = self.hub.take_code_job()
        if job is not None:
            shown = False
            busy = False
            if self._human_can_see():
                from interface.handoff import CODE_TTL_S, display_safe, mask_code_like

                ap = self.hub.approvals.get(job.approval_id)
                raw_target = (ap.summary or {}).get("target", "") if ap else ""
                target = display_safe(mask_code_like(str(raw_target or "")), 40)
                kind = display_safe((ap.components or {}).get("action", "") if ap else "", 20)
                # (a) 표시 예정: 표시 시도 전에 켠다 — 새 화면 캡처는 이때부터 거부(fail-closed).
                self._code_on_overlay = True
                # (b) 이미 진행 중인 캡처가 끝나야 띄운다. 상한을 넘으면 표시 취소(코드 무효).
                if await self._wait_captures_idle():
                    # (c) 오버레이를 켜기 직전 세대를 올린다 — 그 사이 끝나지 않은 캡처는 (d) 에서 버린다.
                    self._pixel_gen += 1
                    if self._code_in_window():
                        # WS-34 D1: on-demand 가 headless 면 페이지는 그대로 두고 별도 작은 창에.
                        self._code_where = "window"
                        shown = await self._show_code_window(job, ap, kind, target)
                    else:
                        self._code_where = "overlay"
                        shown = bool(await self._set_banner(
                            code_overlay_text(job.code, kind, target, int(CODE_TTL_S))))
                else:
                    busy = True
                    logger.warning("진행 중 화면 캡처가 끝나지 않아 확인 코드 표시를 취소함")
            self.hub.code_shown(job.nonce, shown, reason="capture_busy" if busy else None)
            del job  # 평문을 붙잡지 않는다
        if (self._code_on_overlay and self._code_where == "window" and self._code_window is not None
                and self._code_window.closed_externally):
            # R1 NB-3: 사람이 별도 코드 창을 닫았다 — 창에 없는 코드는 무효(오버레이 탭이 닫힐 때와 같은
            # 원칙). 아래에서 캡처 거부를 푼다. 사람은 approve 를 다시 실행해 새 창·새 코드를 받는다.
            self.hub.withdraw_codes()
        if self._code_on_overlay and not self.hub.code_displayed():
            if self._code_where == "window":
                # 별도 창을 닫았음이 확인돼야 화면 캡처 거부를 푼다(fail-closed).
                if await self._close_code_window():
                    self._code_on_overlay = False
                    self._code_where = None
            # 조작권 안내로 되돌린다. 그게 안 되면(예: 띄울 활성 탭이 없음) 코드만이라도 지운다 —
            # 지운 것이 확인돼야(또는 오버레이 탭이 사라져야) 화면 캡처 거부를 푼다(fail-closed).
            elif await self._set_banner(self._control_banner) or (
                    self._control_banner and await self._set_banner(None)):
                self._code_on_overlay = False
                self._code_where = None

    def _code_in_window(self) -> bool:
        """확인 코드를 별도 창에 띄우는가(WS-34 D1: on-demand 이고 지금 창이 없을 때)."""
        return self._window is not None and self._window.state != "headed"

    async def _show_code_window(self, job: Any, ap: Any, kind: str, target: str) -> bool:
        from interface.code_window import ApprovalCodeWindow
        from interface.handoff import CODE_TTL_S, display_safe

        if self._code_window is None:
            self._code_window = ApprovalCodeWindow()
        comps = (ap.components or {}) if ap is not None else {}
        return await self._code_window.show(
            getattr(self._core, "_playwright", None),
            action=kind, target=target or "(이름 없음)",
            origin=display_safe(comps.get("origin") or "", 80),
            expires_at=_iso(ap.expires) if ap is not None else "",
            code=job.code, ttl_s=int(CODE_TTL_S),
        )

    async def _close_code_window(self) -> bool:
        win = self._code_window
        return True if win is None else await win.close()

    def _pixels_blocked(self) -> bool:
        """확인 코드가 창에 떠 있을 수 있는 동안 화면 픽셀을 에이전트에게 주지 않는다."""
        return self._code_on_overlay or self.hub.code_displayed()

    def _pixels_refused(self, action: ActionType) -> ActionResult:
        from interface.handoff import CODE_TTL_S

        return self._error_result(
            action,
            ErrorCode.SCREENSHOT_FAILED,
            "사람이 승인 확인 중이라(창에 확인 코드 표시) 잠시 화면 캡처를 할 수 없습니다. "
            "observe_page·extract 는 쓸 수 있습니다. 잠시 뒤 다시 시도하세요.",
            data={"blocked_by": "approval_code_displayed", "retry_after_s": int(CODE_TTL_S)},
        )

    async def _show_control_banner(self, text: Optional[str]) -> None:
        self._control_banner = text
        if not self._code_on_overlay:  # 코드가 떠 있으면 코드를 덮지 않는다(내린 뒤 다시 띄움)
            await self._set_banner(text)

    def _human_can_see(self) -> bool:
        """사람이 볼 브라우저 창이 있는가(headless 면 없다). on-demand 는 필요할 때 창을 연다(WS-34)."""
        if self._window is not None:
            return True
        return self.browser_mode != "headless" or not self.headless

    def _banner_gone(self) -> bool:
        """오버레이를 띄운 탭이 닫혔음이 **확인**되는가(R3 NB-2). 페이지를 모르거나 확인이 안 되면
        False — 오버레이가 남았을 수 있다고 본다(fail-closed)."""
        page = self._banner[2] if self._banner is not None and len(self._banner) > 2 else None
        if page is None:
            return False
        try:
            return bool(page.is_closed())
        except Exception:  # noqa: BLE001
            return False

    async def _set_banner(self, text: Optional[str]) -> bool:
        """창 위 안내 띠(DevTools 오버레이 — 페이지 DOM 을 바꾸지 않고 페이지 스크립트가 읽거나
        누를 수 없다). 성공하면 True. 창이 없거나 실패하면 False(안내는 stderr 에도 나간다).

        지울 때는 띄웠던 그 탭의 세션으로 지운다(활성 탭이 바뀌었어도 옛 탭에 남지 않게). 띄우기 전
        DOM.enable 이 필요하다(Chromium: 'DOM should be enabled first').
        주의: headed 창에서는 이 오버레이가 Page 스크린샷에 찍힌다(R1 실측) — 확인 코드가 떠 있는
        동안 화면 캡처를 거부하는 이유(_pixels_blocked).

        R3 NB-2: 띄운 탭이 닫혔으면(전송 실패 뒤 페이지 닫힘 확인) 오버레이는 탭과 함께 사라졌다 —
        지우기는 성공, 띄우기는 현재 활성 탭의 새 세션으로 (한 번 더) 시도한다. 탭이 살아 있는데 전송이
        실패하면 여전히 False(오버레이가 남았을 수 있다).
        """
        if not self._human_can_see() or self._core is None:
            return False
        if self._window is not None and self._window.state != "headed":
            return False  # WS-34: on-demand 가 headless(또는 전환 중)면 띄울 창이 없다
        for _attempt in range(2):
            try:
                if not text:
                    if self._banner is not None:
                        cdp = self._banner[1]
                        await cdp.send("Overlay.setPausedInDebuggerMessage", {})
                        await cdp.send("Overlay.disable")
                    return True
                tab_id = self._core.active_tab_id
                if self._banner is not None and self._banner[0] != tab_id:
                    old = self._banner[1]
                    await old.send("Overlay.setPausedInDebuggerMessage", {})
                    self._banner = None
                if self._banner is None:
                    get_tab = getattr(self._core, "get_tab", None)
                    tab = get_tab(tab_id) if callable(get_tab) else None
                    self._banner = (tab_id, await self._core.new_cdp_session(tab_id),
                                    getattr(tab, "page", None))
                cdp = self._banner[1]
                await cdp.send("DOM.enable")
                await cdp.send("Overlay.enable")
                await cdp.send("Overlay.setPausedInDebuggerMessage", {"message": text})
                return True
            except Exception:  # noqa: BLE001
                logger.debug("안내 띠 표시 실패", exc_info=True)
                if self._banner is not None and self._banner_gone():
                    # 보내는 도중 탭이 닫혔다 — 오버레이도 함께 사라졌다.
                    self._banner = None
                    if not text:
                        return True
                    continue  # 띄우기: 현재 활성 탭으로 한 번 더
                return False
        return False
