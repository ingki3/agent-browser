"""BrowserMCPServer 의 사람 인계 통로(WS-29)·로그인 안내·조작권 요청/대기·승인 대기.

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import logging
import asyncio
import time
from typing import Any, Dict, List, Optional

from contracts import ActionResult, ErrorCode
from interface.server_common import _stderr_line

#: 분리 전과 같은 로거 이름을 쓴다(로그 레코드 불변).
logger = logging.getLogger("interface.mcp_server")


#: 조작권 반납 뒤 안내.
_RELEASE_HINT = "사람이 화면을 바꿨을 수 있습니다 — 이전 element_id 는 무효. browser_observe_page 로 다시 관찰하세요."


class HandoffMixin:
    """BrowserMCPServer 의 사람 인계 통로(WS-29)·로그인 안내·조작권 요청/대기·승인 대기 (WS-48 믹스인 — 상태는 BrowserMCPServer.__init__)."""

    def open_handoff(self) -> Optional[str]:
        """서버 상태 디렉터리를 열고 감시 태스크를 띄운다. 실패하면 None(통로 없음 — fail-closed:
        사람이 승인·조작권을 줄 수 없으므로 고위험 액션은 계속 막힌다)."""
        if not self.hub.opened:
            try:
                self.hub.open()
            except Exception as exc:  # noqa: BLE001
                logger.warning("사람 인계 상태 디렉터리를 열 수 없음: %r", exc)
                return None
        if self._hub_task is None:
            try:
                self._hub_task = asyncio.get_running_loop().create_task(self._watch_handoff())
            except RuntimeError:
                self._hub_task = None
        return self.hub.server_id

    async def _watch_handoff(self) -> None:
        from interface.handoff import POLL_INTERVAL_S

        while True:
            try:
                await self._poll_handoff()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 감시는 죽지 않는다
                logger.warning("사람 인계 감시 실패", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _poll_handoff(self) -> List[str]:
        events = self.hub.poll()
        if "taken" in events:
            # WS-58 R2 NB1: poll 이 holder 를 human 으로 바꾼 그 동기 구간에서 요청 감시를 붙인다 — 아래 어떤
            # await(쿠키 사이트 응답·안내 띠)보다 먼저.
            self._login_watch_begin()
        if events and self._recipes is not None and ("taken" in events or "released" in events):
            self._recipes.reset("human_control")  # 사람 조작 구간에서 궤적을 끊는다(WS-38)
        for event in events:
            if event == "cookie_sites":
                from browser.serve_profile import bound_cookie_sites, cookie_sites, summarize_cookie_sites
                from interface.handoff import _write_private

                jobs, self.hub.site_jobs = self.hub.site_jobs, []
                try:
                    context = self._core.context_for("mcp-session") if self._core else None
                    cookies = await context.cookies() if context else []
                    sites = (cookie_sites(self.profile) if context is None and self.profile else
                             summarize_cookie_sites([(c["domain"], int((c["expires"] + 11644473600) * 1_000_000)
                             if c["expires"] > 0 else 0) for c in cookies]))
                    bounded, truncated = bound_cookie_sites(sites)
                    for nonce in jobs:
                        # No control/reason/URL metadata in this response: sites only.
                        _write_private(self.hub.dir / f"ack-{nonce}.json",
                                       {"ok": True, "sites": bounded, "truncated": truncated})
                except Exception:
                    for nonce in jobs:
                        _write_private(self.hub.dir / f"ack-{nonce}.json", {"ok": False})
            if event == "login":
                self._spawn_login()
            if event == "released":
                self._release_unreported = True
                # 사람이 화면을 바꿨을 수 있다 — 기존 element_id 를 모두 무효로.
                if self._engine is not None:
                    self._engine.bump_epoch("control_release")
                # WS-58: 조작권 구간의 로그인 사건을 기록하는 동안 새 도구 호출(관문)을 세운다 — 기록 전에
                # 이름 재시도가 그 사이트로 가지 않게.
                self._hold_gate()
                try:
                    if self._window is not None:
                        # 창 전환이 창 컨텍스트를 닫기 전에(쿠키를 읽을 수 있을 때) 기록한다.
                        await self._login_watch_finish()
                        # WS-34 D2: 창을 닫고 headless 로 다시 연다(sticky 면 창 유지). 탭은 다시 열려
                        # 복원되므로 닫힌 탭 복구(NB-3)는 필요 없다.
                        self._on_demand_released()
                    else:
                        # 복구 Future 는 poll 과 같은 동기 구간에서 만들어야 한다(WS-37) — 기록은 그 뒤.
                        await self._recover_closed_tab_serialized()
                        await self._login_watch_finish()
                finally:
                    self._release_gate()
                await self._show_control_banner(None)
                _stderr_line("agent-browser serve: 조작권 반납됨 — 에이전트가 이어서 진행합니다")
            elif event == "taken":
                self._login_watch_begin()
                if self._window is not None:
                    self._window.opened_for_request = None
                    if self._window.state == "headless" and self._started:
                        # 요청 없이 사람이 바로 take — 조작할 창이 필요하다.
                        self._spawn_switch(True, "taken")
                await self._show_control_banner(
                    "사람이 조작 중 — 끝나면 `agent-browser control release`")
                _stderr_line("agent-browser serve: 조작권을 사람이 가져감")
        await self._login_watch_tick()
        if self._window is not None:
            await self._watch_window()
        await self._sync_code_overlay()
        # WS-37: 다른 태스크(감시 태스크)가 반납을 먼저 집어 복구 중이면 끝날 때까지 기다린다 —
        # 이 함수가 돌아온 뒤 호출자가 보는 상태(반납·탭 알림)가 어긋나지 않게.
        await self._await_release_recovery()
        return events

    # -- WS-58: 도구가 아는 로그인 사건 → 로그인 기록(헤드리스 이름 재시도 판정) -------------------

    def _record_login(self, url: str, source: str) -> None:
        """그 origin 을 로그인 기록에 남긴다(값·이름 없음). 실패는 판정이 fail-closed 로 다룬다."""
        retry = getattr(self, "_headless_retry", None)
        if retry is not None and url:
            retry.logins.record(url, source)

    def _login_watch_begin(self) -> None:
        """조작권 구간 시작 — 감시를 만들고 요청 감시를 **이 동기 구간에서** 붙인다(WS-58 R2 NB1: holder 가
        human 이 된 뒤 첫 await 전에). 쿠키 기준점·탭별 설치는 다음 tick(컨텍스트가 정해진 뒤)."""
        from browser.login_watch import ControlLoginWatch

        if self._login_watch is None:
            self._login_watch = ControlLoginWatch()
        self._login_watch_arm()

    def _login_watch_arm(self, context: Any = None) -> None:
        """현재(또는 주어진 — on-demand 창 전환 직후 새) 컨텍스트에 요청 감시를 await 없이 붙인다."""
        watch = self._login_watch
        if watch is None or self._core is None or (context is None and not self._started):
            return
        try:
            watch.arm(context if context is not None else self._core.context_for("mcp-session"))
        except Exception:  # noqa: BLE001 - 붙일 수 없음: 반납 때 방문 origin 전부 기록(fail-closed)
            watch.mark_degraded()

    def _agent_page_url(self) -> str:
        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None) or self._page
        return str(getattr(page, "url", "") or "")

    def _agent_input_url(self) -> str:
        """입력 사건(자격증명 치환·secret_wanted)의 대상 문서 — 에이전트의 *활성 frame*(switch_frame 뒤면 그 iframe).

        디스패처가 비밀 치환 허용을 판단하는 문서(``ctx.page``)와 같다. ``_agent_page_url`` 의 root 우선 의미는
        그대로 둔다(WS-58 R1 B2)."""
        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "page", None) or getattr(ctx, "root_page", None) or self._page
        try:
            return str(getattr(page, "url", "") or "")
        except Exception:  # noqa: BLE001 - 떨어진 frame
            return ""

    async def _login_watch_tick(self) -> None:
        """사람이 조작 중이면 현재 컨텍스트의 모든 탭에 감시를 건다(on-demand 창 전환 뒤 새 컨텍스트 포함)."""
        from interface.handoff import HOLDER_HUMAN

        watch = self._login_watch
        if watch is not None and self.hub.holder != HOLDER_HUMAN:
            # 'released' 사건 없이 끝난 구간(사람이 창을 닫아 서버가 반납 등)도 마무리한다.
            await self._login_watch_finish()
            return
        if watch is None and self.hub.holder == HOLDER_HUMAN:
            self._login_watch_begin()  # take 를 다른 경로(로그인 작업·직접 _apply)로 받은 경우
            watch = self._login_watch
        if watch is None or self.hub.holder != HOLDER_HUMAN or not self._started or self._core is None:
            return
        if self.hub.secret_wanted:
            watch.note_secret(self._agent_input_url())
        watch.note_visited(self._agent_page_url())
        watch.note_visited(self._agent_input_url())
        try:
            await watch.ensure(self._core.context_for("mcp-session"))
        except Exception:  # noqa: BLE001 - 감시를 걸 수 없음: 조작권 흐름은 그대로, 반납 때 방문 origin 전부 기록
            watch.mark_degraded()
            logger.debug("조작권 로그인 감시 실패", exc_info=True)

    async def _login_watch_finish(self) -> None:
        """조작권 반납 — 감시 결과·로그인 작업(agent-browser login)을 로그인 기록에 남긴다."""
        from browser.login_ledger import SOURCE_HUMAN_CONTROL, SOURCE_LOGIN_FLOW

        watch, self._login_watch = self._login_watch, None
        job_url, self._login_job_url = self._login_job_url, None
        if job_url:
            self._record_login(job_url, SOURCE_LOGIN_FLOW)
        if watch is None:
            return
        context = None
        try:
            if self._started and self._core is not None:
                context = self._core.context_for("mcp-session")
        except Exception:  # noqa: BLE001
            context = None
        try:
            found = await watch.finish(context)
        except Exception:  # noqa: BLE001
            logger.debug("조작권 로그인 감시 정리 실패", exc_info=True)
            return
        for origin in found:
            self._record_login(origin, SOURCE_HUMAN_CONTROL)

    def _login_hint(self) -> str:
        if self.profile is None:
            return "맥에서: --profile 로 서버를 띄워야 로그인이 유지됨 (이후 agent-browser login URL --profile 이름)."
        from urllib.parse import urlsplit
        import re
        import shlex
        from browser.serve_profile import ProfileError, validate_name

        origin = None
        try:
            parts = urlsplit(self._current_origin())
            if parts.scheme in ("http", "https") and parts.hostname:
                host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
                port = parts.port
                if (re.fullmatch(r"[a-z0-9.-]+|\[[0-9a-f:.]+\]", host)
                        and (port is None or isinstance(port, int))):
                    origin = f"{parts.scheme}://{host}"
                    if port is not None:
                        origin += f":{port}"
        except ValueError:
            pass
        try:
            profile = validate_name(self.profile)
        except ProfileError:
            profile = "<프로필 이름>"
        if origin is None:
            return f"맥에서: 페이지 주소를 확인한 뒤 agent-browser login <페이지 주소> --profile {profile}"
        return f"맥에서: agent-browser login {shlex.quote(origin)} --profile {profile}"

    def _spawn_login(self) -> None:
        """Human CLI job: same drain/window/release paths as control take, no MCP action added."""
        job = self.hub.login
        if job is None or job.status != "pending":
            return
        job.status = "opening"
        self._hold_gate()

        async def run() -> None:
            from interface.login_cli import display_available
            from interface.handoff import write_command

            try:
                if not self._human_can_see() or not display_available():
                    raise ValueError("창 없는 환경(headless 전용 또는 DISPLAY 없음). 맥 앞에서 실행하세요.")
                self.hub._apply({"op": "take"})
                self._login_watch_begin()  # WS-58: 이 take 는 poll 사건으로 오지 않는다
                self.hub.secret_wanted = True  # Human login contents stay out of observations.
                self.hub._write_control()
                if self._recipes is not None:
                    self._recipes.reset("human_control")
                if not await self._drain_calls():
                    raise ValueError("진행 중인 도구 호출이 끝나지 않았습니다.")
                if self.hub.login is not job:
                    return
                # run_stdio reserves the profile but starts Chromium lazily.
                if not self._started:
                    await self.start()
                if self.hub.login is not job:
                    return
                if self._window is not None:
                    out = await self._switch_window(True, "taken")
                    if not out.get("ok"):
                        raise ValueError("로그인 창 전환 실패")
                if self.hub.login is not job:
                    return
                context = self._core.context_for("mcp-session")
                # context.new_page guarantees a NEW tab (core.new_tab may adopt a blank one).
                page = await context.new_page()
                await page.goto(job.url, wait_until="domcontentloaded")
                await page.bring_to_front()
                if self.hub.login is not job:
                    return
                # Reuse take's window-only banner on the new human login tab.
                # Preserve the agent's active tab; its dispatcher resumes there.
                active = self._core.active_tab_id
                tab = self._core.tab_for_page(page)
                if tab is not None:
                    self._core.set_active_tab(tab.tab_id)
                    try:
                        await self._show_control_banner("사람이 로그인 중 — 끝나면 터미널에서 Enter 또는 로그인 탭 닫기")
                    finally:
                        if self._core.get_tab(active) is not None:
                            self._core.set_active_tab(active)
                job.status = "ready"
                # WS-58: 사람이 이 작업을 끝내면(반납) 그 origin 을 login_flow 로 기록한다.
                self._login_job_url = job.url
                await self._login_watch_tick()
                page.on("close", lambda: write_command(self.hub.root, self.hub.server_id,
                        "login_finish", login_id=job.login_id) if self.hub.opened and self.hub.login is job else None)
                self.hub._write_control()
                self.hub._ack(job.nonce, True, "로그인 창을 열었습니다.")
            except Exception:
                # Never echo a navigation exception (may contain a URL query/token).
                self.hub._ack(job.nonce, False, "로그인 창을 열 수 없습니다. 맥 앞에서 서버/화면/egress 정책을 확인하세요.")
                if self.hub.login is job:
                    write_command(self.hub.root, self.hub.server_id, "login_finish", login_id=job.login_id)
            finally:
                self._release_gate()

        task = asyncio.create_task(run())
        self._window_tasks.add(task)
        task.add_done_callback(self._window_tasks.discard)

    async def _control_request(self, args: Dict[str, Any]) -> Dict[str, Any]:
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": "reason 이 필요합니다."}
        if not self._human_can_see():
            # 사람이 볼 창이 없다 — 운영자가 창 보이는 방식으로 띄워야 한다(우회 아님).
            message = (
                "headless 서버라 사람이 볼 창이 없습니다. 운영자에게 `agent-browser serve "
                "--browser on-demand`(필요할 때만 창) · `--browser human` · `--browser user-chrome` "
                "중 하나로 다시 띄우도록 요청하세요."
            )
            data: Dict[str, Any] = {"control": self.hub.status(), "browser_mode": self.browser_mode}
            data["login_hint"] = self._login_hint()
            if self.profile is not None:
                # WS-32: 영속 프로필이면 창 있는 서버로 한 번 로그인해 두면 headless 에서도 유지된다.
                message += (
                    f" 이 서버는 영속 프로필 {self.profile!r} 을 씁니다 — 운영자가 이 서버를 끝내고 "
                    f"`agent-browser serve --browser human --profile {self.profile}` 으로 한 번 "
                    "로그인하면 이후 headless 에서도 로그인이 유지됩니다."
                )
                data["profile"] = self._profile_info()
            return {
                "success": False,
                "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                "error_message": message,
                "data": data,
            }
        if not self._started:
            await self.start()
        window_out: Optional[Dict[str, Any]] = None
        if self._window is not None:
            window_out = await self._open_window_for_request()
            if not window_out.get("ok"):
                return {
                    "success": False,
                    "error_code": ErrorCode.PAGE_CRASHED.value,
                    "error_message": (
                        "사람에게 보일 창을 열지 못해 조작권을 요청하지 않았습니다"
                        f"({window_out.get('error') or '창 전환 실패'}). "
                        + ("브라우저를 닫았습니다 — 다음 도구 호출 때 headless 로 다시 엽니다. "
                           if self._window.state == "failed" else "")
                        + "잠시 뒤 다시 요청하거나 운영자에게 알리세요."),
                    "data": {"window": window_out.get("window") or self._window.info()},
                }
        out = self.hub.request(reason, secret_wanted=bool(args.get("secret_wanted")))
        out["login_hint"] = self._login_hint()
        out["how_to_respond"] += " " + out["login_hint"]
        page = getattr(getattr(self._dispatcher, "ctx", None), "page", None) or self._page
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            # 의도된 fail-safe(WS-48 점검): 창을 앞으로 가져오기는 편의 기능 — 실패해도 조작권 요청은 유지된다.
            pass
        from interface.handoff import display_safe

        await self._show_control_banner(f"에이전트가 사람 조작을 요청: {display_safe(reason, 80)} — "
                                        f"`agent-browser control take`")
        _stderr_line(f"agent-browser serve: [사람 조작 요청] {display_safe(reason, 500)} "
                     f"(secret_wanted={out['secret_wanted']}) — {out['how_to_respond']}")
        if window_out is not None:
            out = dict(out, window=window_out["window"])
        return {"success": True, "data": out}

    async def _open_window_for_request(self) -> Dict[str, Any]:
        """on-demand: 조작권 요청 때 창을 연다(이미 창이면 그대로). sticky_pending 이면 sticky 로 확정."""
        from interface.on_demand import HEADED_HINT

        w = self._window
        assert w is not None
        w.take_sticky_for_request()
        self._request_urls = [str(getattr(t.page, "url", "") or "") for t in self._core.tabs()] \
            if self._core is not None else []
        if w.state == "headed":
            self._sync_window()
            info = dict(w.info(), reopened=False)
            return {"ok": True, "window": info}
        out = await self._switch_window(True, "control_request")
        if out.get("ok"):
            self._window_unreported = False  # 이 응답으로 알린다
            w.opened_for_request = time.monotonic()
            w.notice = None
            info = dict(out["window"])
            info.update(w.info())
            info.setdefault("hint", HEADED_HINT)
            out["window"] = info
            self._sync_window()
        return out

    async def _control_wait(self, timeout: float) -> Dict[str, Any]:
        st = self._control_view()
        # WS-34: on-demand 에서는 감시 태스크가 반납을 먼저 처리하고 창 전환을 시작했을 수 있다 —
        # 에이전트가 아직 듣지 못한 반납이면 그 반납(과 전환 결과)을 알린다.
        pre_released = self._window is not None and self._release_unreported
        if st["holder"] == "agent" and not st["requested"] and not pre_released:
            data0: Dict[str, Any] = {"control": st, "changed": "none_pending", "hint": _RELEASE_HINT}
            if self._window is not None:
                data0["window"] = self._take_window_report()
            return {"success": True, "data": data0}
        start_version = self.hub.version
        deadline = time.monotonic() + timeout
        changed = "released" if pre_released else "timeout"
        while changed == "timeout" and time.monotonic() < deadline:
            events = await self._poll_handoff()
            hit = [e for e in events if e in ("taken", "released")]
            if hit:
                changed = hit[-1]
                break
            if self.hub.version != start_version and self.hub.last_event in ("taken", "released"):
                changed = self.hub.last_event
                break
            await asyncio.sleep(0.05)
        window: Optional[Dict[str, Any]] = None
        if self._window is not None:
            # WS-34: 반납(또는 take)으로 창 전환이 시작됐으면 끝날 때까지 기다린다 — 다시 연 브라우저를
            # 에이전트가 바로 쓸 수 있게.
            from interface.on_demand import SWITCH_WAIT_S

            await self._await_gate(SWITCH_WAIT_S)
            window = self._take_window_report()
        data: Dict[str, Any] = {"control": self._control_view(), "changed": changed}
        if changed == "released":
            # WS-37: 감시 태스크가 복구 중이었다면 위 _poll_handoff 가 그 복구를 기다린 뒤 돌아왔다
            # (복구 Future) — 창이 없는(복구를 하는) 경로에서는 그 뒤로 await 가 없으므로 아래
            # tab_closed_by_human 은 빠지지 않는다.
            self._release_unreported = False
            data["hint"] = _RELEASE_HINT
            data["snapshot_epoch"] = self._engine.epoch if self._engine else 0
            if self._tab_notice is not None:
                data["tab_closed_by_human"], self._tab_notice = self._tab_notice, None
        if window is not None:
            data["window"] = window
        return {"success": True, "data": data}

    def _attach_window_report(self, result: ActionResult) -> None:
        """도구 결과에 아직 알리지 않은 창 전환(다시 열림·실패 뒤 복구)을 싣는다 — control_wait 를
        부르지 않는 에이전트도 '페이지가 새로 열렸으니 다시 관찰' 을 듣게."""
        from interface.on_demand import HEADLESS_HINT

        report = self._take_window_report()
        if self._recovered_notice:
            self._recovered_notice = False
            report.update(recovered=True,
                          hint="창 전환이 실패해 브라우저를 headless 로 다시 열었습니다. " + HEADLESS_HINT)
        existing = result.data.get("window")
        result.data["window"] = {**report, **existing} if isinstance(existing, dict) else report

    def _control_view(self) -> Dict[str, Any]:
        """에이전트에게 보이는 조작권 상태(창 상태는 data.window 로 따로 싣는다)."""
        st = self.hub.status()
        st.pop("window", None)
        return st

    def _take_window_report(self) -> Dict[str, Any]:
        """에이전트가 아직 듣지 못한 창 전환이 있으면 그 결과(reopened=true, 탭 복원 내역)를 한 번
        돌려주고, 없으면 현재 상태(reopened=false)."""
        assert self._window is not None
        if self._window_unreported and self._last_window_result is not None:
            self._window_unreported = False
            out = dict(self._last_window_result)
            out.update(self._window.info())
            return out
        return dict(self._window.info(), reopened=False)

    async def _approval_wait(self, approval_id: str, timeout: float) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            st = self.hub.approval_status(approval_id)
            if st["status"] != "pending" or time.monotonic() >= deadline:
                break
            await self._poll_handoff()
            await asyncio.sleep(0.05)
        out = dict(st)
        if out["status"] == "approved":
            out["next"] = "같은 툴을 같은 인자 + approval_id 로 다시 호출하세요(1회용)."
        return {"success": out["status"] != "unknown", "data": {"approval": out}}
