"""WS-77 R1 — 독립 검증 결함 4종의 회귀 시험(#210).

F1 제어 창 버튼을 다른 문서(제어 창 main world 가 연 팝업)로 옮겨 에이전트 click 으로 누르기 → 반납 0.
F2 사람 활동 신호: secret 인계 origin 과 겹친 쿠키 변화가 반납 때 not_seen 으로 뒤집히지 않음 · 감시 CDP 설치 실패는
   unknown · 입력만 하고 제출 안 함은 not_seen(정의) 이어도 next_step 은 다시 관찰.
F3 제어 창·띠의 사유 표시에서 등록 비밀·토큰 주소 원문 0.
F4 human_handoff.tab_origins 는 호출한 작업 공간의 탭만.

사보타주(보고서 기록): F1 control_window._UI_JS 의 문서 검사(attached)를 끄면 adopt 시험이, F2 login_watch.finish 의
사건 누적을 끄면 쿠키 시험이, F4 server_control 의 작업 공간 필터를 끄면 경계 시험이 실패한다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from contracts import ActionType
from browser.login_watch import ACTIVITY_NOT_SEEN, ACTIVITY_SEEN, ACTIVITY_UNKNOWN, ControlLoginWatch, activity_of
from interface.server_control import NEXT_OBSERVE
from security import secret_registry

from test_run_cli import requires_chromium
from test_ws77_control_window import _server, _settle, _tool, _until, human_click, site  # noqa: F401 - 픽스처

DEFAULT_PORT = "http://127.0.0.1/"


async def _route_default_port(s: Any) -> None:
    """루프백 기본 포트 origin(http://127.0.0.1) — 리스너 없이 route 로 채운다(쿠키 origin 은 포트를 빼므로 겹침 재현)."""
    await s._page.context.route(DEFAULT_PORT + "**", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<!doctype html><meta charset=utf-8><p>합성 기본 포트</p>"))


# ------------------------------------------------------------------ F1


ADOPT_JS = """() => {
  const w = window.open('about:blank', 'exported-control');
  if (!w) return {opened: false};
  const b = document.querySelector('button');
  w.document.body.appendChild(w.document.adoptNode(b));
  return {opened: true, moved: b.ownerDocument === w.document};
}"""


@requires_chromium
async def test_button_adopted_into_popup_releases_nothing(tmp_path, site):  # noqa: F811
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        r = await s.call_server_tool("browser_control_request", {"reason": "main world 공략"})
        rid = r["data"]["request_id"]
        cw = s._control_window
        attack = await cw.page.evaluate(ADOPT_JS)  # 제어 창 main world 의 임의 JS(명세상 공격면)
        assert attack == {"opened": True, "moved": True}
        await asyncio.sleep(0.2)
        popup = next(t for t in s._core.all_tabs() if t.page is not s._page and t.page is not cw.page)
        assert (await _tool(s, ActionType.TAB_CONTROL, command="switch", tab_id=popup.tab_id))["success"]
        obs = await _tool(s, ActionType.OBSERVE_PAGE)
        button = next(e for e in obs["data"]["observation"]["elements"] if e["name"] == "다 했어요")
        await _tool(s, ActionType.CLICK, element_id=button["element_id"], epoch=obs["snapshot_epoch"])  # trusted CDP 입력
        await _settle(s)
        assert cw.accepted == 0, "다른 문서로 옮긴 버튼의 클릭이 [다 했어요] 로 받아들여졌다"
        assert s.hub.requested and s.hub.request_id == rid and s.hub.release_source is None
        # 버튼이 원 문서를 떠났다 → 그 창은 무효, 1회 다시 띄운다(완료로 보지 않음).
        assert cw.moved
        await _until(lambda: s._control_window is not None and s._control_window is not cw)
        fresh = s._control_window
        assert fresh.is_open and all(t.page is not fresh.page for t in s._core.all_tabs())
        await human_click(fresh)  # 진짜 사람 클릭은 그대로
        got = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        assert got["data"]["changed"] == "released"
        assert got["data"]["human_handoff"]["release_source"] == "window_done" and fresh.accepted == 1


@requires_chromium
async def test_binding_payload_requires_attached_button(tmp_path, site):  # noqa: F811
    """서버 쪽 확인: 우리 world·nonce·trusted 라도 버튼이 원 문서에 붙어 있지 않았다는 보고는 버린다."""
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        await s.call_server_tool("browser_control_request", {"reason": "확인"})
        cw = s._control_window

        def event(**payload: Any) -> Dict[str, Any]:
            return {"name": cw.binding, "executionContextId": cw.context_id, "payload": json.dumps(payload)}

        cw._on_binding(event(nonce=cw.request_id, trusted=True))  # attached 없음(옛 모양)
        cw._on_binding(event(nonce=cw.request_id, trusted=True, attached=False))
        cw._on_binding(event(nonce=cw.request_id, trusted=True, attached="true"))
        assert cw.accepted == 0 and cw.rejected == 3 and not cw.moved
        cw._on_binding(event(nonce="other", moved=True))  # 다른 요청의 '옮겨짐' 보고는 무시
        assert not cw.moved
        await human_click(cw)
        got = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        assert got["data"]["changed"] == "released" and cw.accepted == 1


@requires_chromium
async def test_ordinary_page_has_no_path_into_control_main_world(tmp_path, site):  # noqa: F811
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        await s.call_server_tool("browser_control_request", {"reason": "확인"})
        cw = s._control_window
        # 제어 창: opener 없음·opaque origin·창 이름 없음·main world 스크립트 0 — 남이 이름·origin 으로 잡을 손잡이가 없다.
        inside = await cw.page.evaluate("""() => ({opener: window.opener === null, origin: location.origin,
            name: window.name, scripts: document.scripts.length, frames: window.frames.length})""")
        assert inside == {"opener": True, "origin": "null", "name": "", "scripts": 0, "frames": 0}
        # 에이전트 탭의 일반 페이지: 이름으로 열어도 제어 창이 아닌 새 빈 창, opener 로도 닿지 않는다.
        probe = await s._page.evaluate("""() => {
            const w = window.open('', 'agent-browser');
            let text = null, sameDoc = false;
            try { text = w.document.body ? w.document.body.innerText : ''; sameDoc = true; } catch (e) {}
            try { w.close(); } catch (e) {}
            return {text, sameDoc, opener: window.opener === null};
        }""")
        assert probe["opener"] and "다 했어요" not in str(probe["text"])
        await _settle(s)
        assert cw.accepted == 0 and cw.is_open and s.hub.requested


# ------------------------------------------------------------------ F2


def test_finish_accumulates_every_event_kind():
    """origin 이 겹쳐도(secret_handoff 와 cookie_change) 종류별로 누적한다 — 기록용 dict 는 그대로."""

    class Ctx:
        pages: list = []

        async def cookies(self):
            return [{"name": "sid", "value": "new", "domain": "127.0.0.1", "path": "/", "httpOnly": False}]

    watch = ControlLoginWatch()
    watch.baseline = {}
    watch.note_secret("http://127.0.0.1/")
    found = asyncio.run(watch.finish(Ctx()))
    assert found == {"http://127.0.0.1": "secret_handoff"}  # 로그인 기록 dict 는 바뀌지 않는다
    assert watch.events == {"secret_handoff", "cookie_change"}
    assert activity_of(watch.events, degraded=False, baseline_known=True) == ACTIVITY_SEEN
    assert activity_of({"secret_handoff"}, degraded=False, baseline_known=True) == ACTIVITY_NOT_SEEN
    assert activity_of(set(), degraded=False, baseline_known=True, install_complete=False) == ACTIVITY_UNKNOWN


@requires_chromium
async def test_default_port_secret_cookie_change_stays_seen(tmp_path):
    async with _server(tmp_path, recipes=False) as s:
        await s.start()
        await _route_default_port(s)
        assert (await _tool(s, ActionType.NAVIGATE, url=DEFAULT_PORT))["success"]
        await s.call_server_tool("browser_control_request", {"reason": "기본 포트 비밀 입력", "secret_wanted": True})
        await s._page.evaluate("document.cookie='synthetic_login_default=changed;path=/'")
        live = await s.call_server_tool("browser_control_status", {})
        assert live["data"]["human_handoff"]["human_activity_seen"] == "seen"
        await human_click(s._control_window)
        end = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        hh = end["data"]["human_handoff"]
        assert hh["release_source"] == "window_done" and hh["human_activity_seen"] == "seen", hh


@requires_chromium
async def test_cli_release_secret_cookie_change_stays_seen(tmp_path):
    from interface import handoff

    async with _server(tmp_path, recipes=False) as s:
        await s.start()
        await _route_default_port(s)
        assert (await _tool(s, ActionType.NAVIGATE, url=DEFAULT_PORT))["success"]
        await s.call_server_tool("browser_control_request", {"reason": "비밀", "secret_wanted": True})
        await s._page.evaluate("document.cookie='synthetic_cli=changed;path=/'")
        handoff.write_command(s.hub.root, s.hub.server_id, "release")
        await _until(lambda: not s.hub.requested)
        await _settle(s)
        end = await s.call_server_tool("browser_control_status", {})
        assert end["data"]["human_handoff"]["release_source"] == "cli"
        assert end["data"]["human_handoff"]["human_activity_seen"] == "seen", end


@requires_chromium
async def test_login_watch_cdp_install_failure_is_unknown(tmp_path, site, monkeypatch):  # noqa: F811
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/home")
        context = s._page.context
        original = context.new_cdp_session
        failures = []

        async def session_for(page):
            session = await original(page)
            real = session.send

            async def send(method, params=None):
                if method == "Runtime.addBinding" and str((params or {}).get("name", "")).startswith("abw"):
                    failures.append(method)
                    raise RuntimeError("synthetic-login-watch-install-failure")
                return await real(method, params)

            session.send = send
            return session

        monkeypatch.setattr(context, "new_cdp_session", session_for)
        await s.call_server_tool("browser_control_request", {"reason": "감시 실패 대조"})
        live = await s.call_server_tool("browser_control_status", {})
        assert failures  # 조건 성립: 감시 설치가 실제로 실패했다
        assert live["data"]["human_handoff"]["human_activity_seen"] == "unknown", live
        await human_click(s._control_window)
        end = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        assert end["data"]["human_handoff"]["human_activity_seen"] == "unknown", end


@requires_chromium
async def test_input_without_submit_is_not_seen_but_next_step_reobserves(tmp_path, site):  # noqa: F811
    async with _server(tmp_path, recipes=False) as s:
        await _tool(s, ActionType.NAVIGATE, url=site + "/login")
        await s.call_server_tool("browser_control_request", {"reason": "입력 확인", "secret_wanted": True})
        await s._page.fill("#pw", "only-synthetic-input")
        await human_click(s._control_window)
        end = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        hh = end["data"]["human_handoff"]
        # 정의: 활동 = 네트워크 요청(POST/PUT)·비밀번호 칸 제출·쿠키 변화 — 제출 없는 입력은 활동이 아니다.
        assert hh["human_activity_seen"] == "not_seen"
        assert hh["next_step"] == NEXT_OBSERVE and "not_seen" in NEXT_OBSERVE


# ------------------------------------------------------------------ F3


@requires_chromium
async def test_reason_display_masks_registered_secret_and_token_url(tmp_path, site):  # noqa: F811
    token = "Ws77SyntheticCredentialCanaryZebra"
    secret_registry.clear()
    secret_registry.register_secret_values([token])
    try:
        async with _server(tmp_path, recipes=False) as s:
            await _tool(s, ActionType.NAVIGATE, url=site + "/home")
            reason = "확인 " + token + " https://example.test/cb?token=Zq81SyntheticUrlTokenValue"
            await s.call_server_tool("browser_control_request", {"reason": reason})
            ui = await s._control_window.page.inner_text("body")
            banner = str(s._control_banner)
            for shown in (ui, banner):
                assert "확인" in shown
                assert token not in shown and token[:12] not in shown, shown
                assert "Zq81SyntheticUrlTokenValue" not in shown and "Zq81Synthetic" not in shown, shown
    finally:
        secret_registry.clear()


# ------------------------------------------------------------------ F4


@requires_chromium
async def test_tab_origins_only_from_caller_workspace(tmp_path, site):  # noqa: F811
    from browser.workspace import workspace_scope

    async with _server(tmp_path, recipes=False) as s:
        await s.start()
        await _route_default_port(s)
        assert (await _tool(s, ActionType.NAVIGATE, url=site + "/home"))["success"]
        assert (await _tool(s, ActionType.NAVIGATE, url=DEFAULT_PORT, workspace="watch"))["success"]
        other = "http://127.0.0.1"
        assert other != site and any(str(t.page.url).startswith(DEFAULT_PORT) for t in s._core.all_tabs())
        listed = await _tool(s, ActionType.TAB_CONTROL, command="list")
        assert listed["data"]["other_workspace_tabs"] == 1  # 개수 신호는 그대로
        req = await s.call_server_tool("browser_control_request", {"reason": "작업 공간 경계"})
        st = await s.call_server_tool("browser_control_status", {})
        for hh in (req["data"]["human_handoff"], st["data"]["human_handoff"]):
            assert hh["tab_origins"]["before"] == [site] and hh["tab_origins"]["now"] == [site], hh
        with workspace_scope("watch"):  # 다른 작업 공간이 보면 자기 탭만
            mine = s._handoff_view()["tab_origins"]
        assert mine["before"] == [other] and mine["now"] == [other]
        await human_click(s._control_window)
        end = await s.call_server_tool("browser_control_wait", {"timeout_s": 10})
        ended = end["data"]["human_handoff"]["tab_origins"]
        assert ended["before"] == [site] and ended["now"] == [site], end
        assert '"' + other + '"' not in json.dumps(end["data"]["human_handoff"])  # 다른 작업 공간 origin 0
