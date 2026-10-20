"""WS-56 R3 — 재검증 FAIL(BLOCKING-1·2·3·6) 재현을 저장소 테스트로. 실제 Chromium(headless) + 로컬 http(127.0.0.1)만.

B1: 순수 CSS hover(·pointermove·pointer-events) 오버레이가 좌표 클릭을 승인 안 한 노드로 보냄 — 고위험 좌표 클릭은
    elicitation·정책 대상에서 빼고(묻지도 실행하지도 않음), 터미널 승인 증표 실행은 이동 뒤 실제 대상을 다시 보고
    실제 눌림 대상(이벤트)도 감시해 승인 노드가 아니면 실행하지 않는다.
B2: Enter 암묵 제출 — 포커스 입력칸 폼의 기본 제출 버튼(첫 submit) formaction/formtarget/formmethod 도 실행 의미.
B3: 정책 통과 뒤 실행 락을 기다리는 동안 대상이 바뀌면 — 락 획득 뒤 대상·문맥·의미·정책·금지 범주를 다시 판정.
B6: 일반(비승인) 액션도 같은 서버 입력/효과 락에 참여 — 승인된 무반응 클릭이 동시 일반 클릭의 alert 로 성공 오보고 안 함.

원 재현: .hermes/verify/ws56-r2/binding/test_natural.py · policy/test_policy_wait_uninstrumented.py ·
concurrency/test_dialog_attribution.py.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import anyio
import pytest

from contracts import ActionType
from interface import handoff
from interface.mcp_server import BrowserMCPServer, create_server, tool_name
from interface.server_elicit import CURRENT_ELICITOR
from security import approve_policy as ap
from test_run_cli import requires_chromium
from test_ws56_r2_binding import _run, site  # noqa: F401 — site 는 pytest 픽스처

pytestmark = requires_chromium


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))


def _labels(hits: Any) -> list:
    return [h.split("b=")[1].split("&")[0] for h in hits]


# ------------------------------------------------------------------ B1: 좌표 hover 오버레이

_OVERLAY_BASE = """<!doctype html><meta charset=utf-8><title>로컬</title><p id=out>대기</p>
<style>#zone{position:absolute;left:20px;top:80px;width:150px;height:50px}
#pay,#evil{position:absolute;inset:0;width:150px;height:50px}#pay{z-index:1}#evil{z-index:2;pointer-events:none}</style>
<div id=zone><button id=pay onclick="hit('original')">결제하기</button><button id=evil onclick="hit('evil')">결제하기</button></div>
<script>function hit(x){fetch('/clicked?b='+x,{cache:'no-store'});document.querySelector('#out').textContent=x;}</script>"""

_OVERLAYS = {
    "css-hover": _OVERLAY_BASE + "<style>#zone:hover #evil{pointer-events:auto}</style>",
    "pointermove": _OVERLAY_BASE + ("<script>document.querySelector('#pay').addEventListener('pointermove',"
                                    "()=>document.querySelector('#evil').style.pointerEvents='auto',{once:true});</script>"),
    "pointer-events-none": _OVERLAY_BASE.replace("#evil{z-index:2;pointer-events:none}",
                                                 "#evil{z-index:0;pointer-events:auto}")
    + ("<script>document.querySelector('#pay').addEventListener('pointermove',"
       "e=>e.target.style.pointerEvents='none',{once:true});</script>"),
    # 이동 뒤 hit-test 는 그대로인데 누르는 순간(pointerdown)에 덮개가 생김 — 이벤트 감시가 잡아야 한다.
    "pointerdown-swap": _OVERLAY_BASE.replace("#evil{z-index:2;pointer-events:none}", "#evil{z-index:2;display:none}")
    + ("<script>addEventListener('pointerdown',()=>{const e=document.querySelector('#evil');"
       "e.style.display='block';},{capture:true,once:true});</script>"),
}


_HOVER_HREF = ("<!doctype html><meta charset=utf-8><p id=out>대기</p>"
               "<a id=pay href=\"/clicked?b=alice\" style=\"position:absolute;left:20px;top:80px;width:150px;height:50px;"
               "display:block\">결제하기</a><script>document.querySelector('#pay').addEventListener('pointermove',"
               "e=>{e.currentTarget.href='/clicked?b=bob'},{once:true});</script>")


async def test_pointermove_href_change_on_same_node_is_not_run(site):
    """같은 노드라도 이동(hover)에 목적지가 바뀌면 — 이동 뒤 다시 본 실행 의미가 달라 누르지 않는다(이벤트 감시로는 못 잡는 경우)."""
    site["html"] = _HOVER_HREF
    out = await _run(site, "xy", None, token=True)
    assert "bob" not in _labels(out["hits"]), out
    assert out["env"]["success"] is False and out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out


@pytest.mark.parametrize("case", list(_OVERLAYS))
async def test_hover_overlay_xy_with_approval_id_is_not_run(site, case):
    """터미널 승인 증표로 연 좌표 클릭: 이동 뒤 실제 대상이 승인 노드가 아니면 실행하지 않는다."""
    site["html"] = _OVERLAYS[case]
    out = await _run(site, "xy", None, token=True)
    assert "evil" not in _labels(out["hits"]), out
    assert out["env"]["success"] is False, out
    assert out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out


@pytest.mark.parametrize("case", list(_OVERLAYS))
async def test_high_risk_xy_is_not_elicited(site, case):
    """버튼 승인(elicitation)은 고위험 좌표 클릭을 묻지 않는다(결속을 끝까지 증명할 수 없어 터미널 승인만)."""
    site["html"] = _OVERLAYS[case]
    out = await _run(site, "xy", None, token=False)
    assert out["messages"] == [], out
    assert out["env"]["success"] is False, out
    assert not out["hits"], out


async def test_unchanged_xy_with_approval_id_still_runs(site):
    """대조: 오버레이 없는 같은 표본은 터미널 승인으로 실제 원 노드가 눌린다(과차단 아님)."""
    site["html"] = _OVERLAY_BASE
    out = await _run(site, "xy", None, token=True)
    assert out["env"]["success"] is True, out
    assert _labels(out["hits"]) == ["original"], out


# ------------------------------------------------------------------ B2: Enter 암묵 제출의 기본 제출 버튼

_IMPLICIT = ('<!doctype html><meta charset=utf-8><p id=out>대기</p><form method=post action="/clicked?b=alice">'
             '<input id=pay aria-label="결제하기" value=x>'
             '<button id=submitter formaction="/clicked?b=alice" formtarget=_self>결제하기</button></form>')
_IMPLICIT_MUTATIONS = {
    "formaction": "document.querySelector('#submitter').formAction='/clicked?b=bob'",
    "formtarget": "document.querySelector('#submitter').formTarget='_blank'",
    "formmethod": "document.querySelector('#submitter').formMethod='get'",
    "new-default-submitter": ("(()=>{const b=document.createElement('button');b.setAttribute('formaction','/clicked?b=bob');"
                              "b.textContent='결제하기';document.querySelector('form').prepend(b);})()"),
}


@pytest.mark.parametrize("kind", list(_IMPLICIT_MUTATIONS))
@pytest.mark.parametrize("token", [False, True], ids=["elicit", "approval_id"])
async def test_enter_default_submitter_change_is_not_run(site, kind, token):
    site["html"] = _IMPLICIT
    expr = _IMPLICIT_MUTATIONS[kind]

    async def mutate(b: Any) -> None:
        await b._dispatcher.ctx.page.evaluate(expr)

    out = await _run(site, "key", mutate, token=token)
    assert not out["hits"], out
    assert out["env"]["success"] is False, out
    assert out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out


async def test_enter_unchanged_runs_and_message_shows_submitter_destination(site):
    """대조: 그대로면 실행된다. 승인 문구 목적지는 기본 제출 버튼의 formaction(폼 action 아님)."""
    site["html"] = _IMPLICIT.replace('formaction="/clicked?b=alice"', 'formaction="/clicked?b=carol"')
    out = await _run(site, "key")
    assert out["env"]["success"] is True, out
    assert _labels(out["hits"]) == ["carol"], out
    line = next(x for x in out["messages"][0].split("\n") if x.startswith("목적지: "))
    assert line.startswith("목적지: POST ") and line.endswith("/clicked?b=carol"), line


# ------------------------------------------------------------------ B3: 정책 통과 뒤 락 대기 → 재판정

_WAIT_PAGE = '<!doctype html><meta charset=utf-8><p id=out>대기</p><a id=b href="/clicked?b=safe">제출</a>'


async def _policy_server(site: Dict[str, Any], tmp_path: Any, monkeypatch: Any, rows: Any = (("click", None),)) -> Any:
    from browser import serve_profile as sp

    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    folder = tmp_path / "profiles" / "serve-wait"
    folder.mkdir(parents=True)
    for action, name in rows:
        ap.add_policy(folder / ap.POLICY_FILE, site["url"], action, name)
    b = BrowserMCPServer(profile="wait", recipes=False)
    await b.call_tool(tool_name(ActionType.NAVIGATE), {"url": site["url"] + "/"})
    return b, folder / ap.POLICY_FILE


async def _while_lock_held(b: Any, call: Any, during: Any) -> Any:
    """다른 호출이 실행 락을 쥔 동안 call 이 락을 기다리게 하고, 그사이 during(페이지·정책 변경) 뒤 락을 놓는다."""
    lock = b._approval_exec_lock()
    await lock.acquire()
    try:
        task = asyncio.create_task(call())
        with anyio.fail_after(20):
            while not getattr(lock, "_waiters", None):
                if task.done():
                    break
                await asyncio.sleep(0.005)
        assert not task.done(), "호출이 락을 기다리지 않고 끝남"
        await during()
    finally:
        lock.release()
    return await task


@pytest.mark.parametrize("change", ["href-to-transfer", "policy-removed", "nothing"])
async def test_policy_pass_is_rechecked_after_lock_wait(site, tmp_path, monkeypatch, change):
    site["html"] = _WAIT_PAGE
    b, path = await _policy_server(site, tmp_path, monkeypatch)
    try:
        async def call() -> Any:
            return await b.call_tool(tool_name(ActionType.CLICK), {"selector": "#b"})

        async def during() -> None:
            if change == "href-to-transfer":
                await b._page.evaluate("document.querySelector('#b').href='/clicked?b=transfer'")
            elif change == "policy-removed":
                ap.remove_policy(path, site["url"], "click")

        r = await _while_lock_held(b, call, during)
        await b._page.wait_for_timeout(300)
    finally:
        await b.close()
        b.hub.close()
    hits = _labels(site["hits"])
    if change == "nothing":
        assert r.success is True and r.data["approval"]["via"] == "policy", r
        assert hits == ["safe"], hits
    else:
        assert r.success is False, r
        assert hits == [], hits
        assert r.data.get("approval", {}).get("via") != "policy" or r.error_code is not None, r


async def test_elicitation_accept_is_rechecked_after_lock_wait(site, tmp_path, monkeypatch):
    """elicitation accept 경로도 같다 — 락을 기다리는 동안 목적지가 바뀌면 실행하지 않는다."""
    site["html"] = _WAIT_PAGE
    b = create_server(recipes=False)[1]

    class Accept:
        def supported(self) -> bool:
            return True

        async def ask(self, *a: Any) -> str:
            return "accept"

    try:
        await b.call_tool(tool_name(ActionType.NAVIGATE), {"url": site["url"] + "/"})

        async def call() -> Any:
            tok = CURRENT_ELICITOR.set(Accept())
            try:
                return await b.call_tool(tool_name(ActionType.CLICK), {"selector": "#b"})
            finally:
                CURRENT_ELICITOR.reset(tok)

        async def during() -> None:
            await b._page.evaluate("document.querySelector('#b').href='/clicked?b=transfer'")

        r = await _while_lock_held(b, call, during)
        await b._page.wait_for_timeout(300)
    finally:
        await b.close()
        b.hub.close()
    assert r.success is False and r.error_code.value == "E_TOCTOU_MISMATCH", r
    assert site["hits"] == [], site["hits"]


async def test_high_risk_xy_is_not_opened_by_policy(site, tmp_path, monkeypatch):
    """정책(이름 무관 click)이 있어도 고위험 좌표 클릭은 정책으로 실행하지 않는다(B1 — 터미널 승인만)."""
    site["html"] = _OVERLAY_BASE.replace("결제하기", "제출").replace("pay", "sub")
    b, _path = await _policy_server(site, tmp_path, monkeypatch)
    try:
        obs = (await b.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})).data["observation"]
        r = await b.call_tool(tool_name(ActionType.CLICK), {"x": 70, "y": 100, "epoch": obs["snapshot_epoch"]})
        await b._page.wait_for_timeout(300)
    finally:
        await b.close()
        b.hub.close()
    assert r.success is False, r
    assert (r.data.get("approval") or {}).get("via") != "policy", r
    assert site["hits"] == [], site["hits"]


# ------------------------------------------------------------------ B6: 일반 실행도 입력/효과 락에 참여

_DIALOG = """<!doctype html><meta charset=utf-8>
<button id=pay2 onclick="console.log('approved-noop-fired')">주문 확정</button>
<button id=normal onclick="alert('normal-only-dialog')">열기</button>"""


async def test_approved_noop_does_not_take_normal_click_dialog(site):
    from mcp import types as t
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    site["html"] = _DIALOG
    server, b = create_server(recipes=False)

    async def elicit(ctx: Any, params: Any) -> Any:
        return t.ElicitResult(action="accept", content={})

    out: Dict[str, Any] = {}
    try:
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(*ss, server.create_initialization_options()))
                async with ClientSession(*cs, elicitation_callback=elicit) as s:
                    await s.initialize()
                    await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": site["url"] + "/"})
                    await b.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})

                    async def click(selector: str) -> Dict[str, Any]:
                        r = await s.call_tool(tool_name(ActionType.CLICK), {"selector": selector})
                        return json.loads(r.content[0].text)

                    fired = asyncio.Event()
                    b._page.on("console", lambda m: fired.set() if m.text == "approved-noop-fired" else None)
                    with anyio.fail_after(60):
                        task = asyncio.create_task(click("#pay2"))
                        await fired.wait()
                        out["normal"] = await click("#normal")
                        out["noop"] = await task
                tg.cancel_scope.cancel()
    finally:
        await b.close()
        b.hub.close()
    assert out["noop"]["success"] is False, out
    assert "dialogs" not in out["noop"]["data"], out
    assert out["normal"]["success"] is True, out
    assert out["normal"]["data"]["dialogs"][0]["message"] == "normal-only-dialog", out
