"""WS-56 R2 — 검증 FAIL(B2·B3·B4) 재현을 저장소 테스트로. 실제 Chromium(headless) + 로컬 http(127.0.0.1)만.

B2: 좌표 클릭·press_key 도 승인 시점 노드(좌표 hit-test 노드 · 포커스 노드)에 결속 — 승인 대기 중 같은 id·이름·자리의
    새 노드로 바뀌면 실행하지 않는다(elicitation · 승인 증표). 결속 못 하면(다른 출처 프레임 등) 묻지도 실행하지도 않는다.
B3: 같은 노드의 실행 의미(a[href]·form action/formaction·method·target)가 승인 뒤 바뀌면 실행하지 않는다. 승인 문구에
    목적지를 보인다(가림 적용).
B4: 동시 승인 호출 — 재확인~실제 입력~효과 판정 구간 직렬화. 각자 승인한 버튼이 실제로 눌린다(입력 유실 없음).

원 재현: .hermes/verify/ws56-r1/test_independent.py (검증자 탐침) — 같은 시나리오를 옮겼다.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import anyio
import pytest

from contracts import ActionType
from interface import handoff
from interface.mcp_server import create_server, tool_name
from test_run_cli import requires_chromium
from test_ws56_r1_binding import _visible
from ws29_helpers import srv_approve

pytestmark = requires_chromium

HTML = """<!doctype html><meta charset=utf-8><title>로컬 가게</title>
<p id=out>대기</p><div id=box>
<button id=pay style="position:absolute;left:20px;top:80px;width:150px;height:50px" onclick="hit('original')">결제하기</button>
</div><div style="position:absolute;top:200px"><button id=pay2 onclick="hit('second')">주문 확정</button>
<button id=pay3 onclick="hit('third')">삭제 확정</button><button id=normal onclick="hit('normal')">열기</button></div>
<script>window.clientHits=[];function hit(b){clientHits.push(b);fetch('/clicked?b='+b+'&n='+clientHits.length,{cache:'no-store'});document.querySelector('#out').textContent=b;}
function swap(){let old=document.querySelector('#pay');let n=old.cloneNode(true);n.onclick=()=>hit('replacement');old.replaceWith(n);n.focus();}
</script>"""


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))


@pytest.fixture
def site():
    state: Dict[str, Any] = {"html": HTML, "hits": [], "frame": ""}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/clicked"):
                state["hits"].append(self.path)
                body = "<p id=out>done</p>"
            elif self.path.startswith("/frame"):
                body = state["frame"] or HTML
            else:
                body = state["html"]
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.do_GET()

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["port"] = srv.server_address[1]
    state["url"] = f"http://127.0.0.1:{state['port']}"
    try:
        yield state
    finally:
        srv.shutdown()
        srv.server_close()


async def _run(site: Dict[str, Any], mode: str = "selector", during: Any = None, token: bool = False,
               settle_ms: int = 350, prepare: Any = None) -> Dict[str, Any]:
    """한 번 실행. mode = selector | element_id | xy | key. during = 사람이 승인을 보는 동안 페이지에 일어나는 일."""
    from mcp import types as t
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    server, backend = create_server(recipes=False)
    out: Dict[str, Any] = {"messages": []}

    async def elicit(ctx: Any, params: Any) -> Any:
        out["messages"].append(params.message)
        if during:
            await during(backend)
        return t.ElicitResult(action="accept", content={})

    try:
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(*ss, server.create_initialization_options()))
                async with ClientSession(*cs, elicitation_callback=None if token else elicit) as session:
                    await session.initialize()
                    await session.call_tool(tool_name(ActionType.NAVIGATE), {"url": site["url"] + "/"})
                    obs = (await backend.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})).data["observation"]
                    if prepare is not None:
                        prepare(backend)
                    action = ActionType.PRESS_KEY if mode == "key" else ActionType.CLICK
                    if mode == "element_id":
                        eid = next(x["element_id"] for x in obs["elements"] if x["name"] == "결제하기")
                        args: Dict[str, Any] = {"element_id": eid, "epoch": obs["snapshot_epoch"]}
                    elif mode == "xy":
                        args = {"x": 70, "y": 100, "epoch": obs["snapshot_epoch"]}
                    elif mode == "key":
                        await backend._dispatcher.ctx.page.locator("#pay").focus()
                        args = {"key": "Enter"}
                    else:
                        args = {"selector": "#pay"}
                    if token:
                        _visible(backend)
                        blocked = await backend.call_tool(tool_name(action), args)
                        out["blocked"] = blocked.model_dump(mode="json")
                        aid = blocked.data["approval"]["approval_id"]
                        await srv_approve(backend, aid)
                        if during:
                            await during(backend)
                        out["env"] = (await backend.call_tool(tool_name(action), dict(args, approval_id=aid))
                                      ).model_dump(mode="json")
                    else:
                        r = await session.call_tool(tool_name(action), args)
                        out["env"] = json.loads(r.content[0].text)
                    await backend._page.wait_for_timeout(settle_ms)
                    out["hits"] = list(site["hits"])
                tg.cancel_scope.cancel()
    finally:
        await backend.close()
        backend.hub.close()
    return out


async def _swap(backend: Any) -> None:
    await backend._dispatcher.ctx.page.evaluate("swap()")


# ------------------------------------------------------------------ B2: 좌표·키도 노드 결속


@pytest.mark.parametrize("mode", ["xy", "key", "selector", "element_id"])
@pytest.mark.parametrize("token", [False, True], ids=["elicit", "approval_id"])
async def test_replaced_node_is_not_run(site, mode, token):
    """승인 대기 중 같은 id·이름·자리의 새 노드(키는 새 노드에 포커스) → 실행 안 함(TOCTOU)."""
    out = await _run(site, mode, _swap, token=token)
    assert not out["hits"], out
    assert out["env"]["success"] is False, out
    if _xy_not_elicited(mode, token, out):
        return
    assert out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out


def _xy_not_elicited(mode: str, token: bool, out: Dict[str, Any]) -> bool:
    """WS-56 R3 B1: 고위험 좌표 클릭은 버튼 승인(elicitation) 대상이 아니다 — 묻지도 실행하지도 않는다(터미널 승인만)."""
    if mode != "xy" or token:
        return False
    assert out["messages"] == [], out
    assert out["env"]["success"] is False and not out["hits"], out
    return True


@pytest.mark.parametrize("mode", ["xy", "key"])
@pytest.mark.parametrize("token", [False, True], ids=["elicit", "approval_id"])
async def test_unchanged_point_and_focus_still_run(site, mode, token):
    """대조: 노드가 그대로면 승인한 좌표 클릭·Enter 는 실제로 실행된다(과차단 아님)."""
    out = await _run(site, mode, None, token=token)
    if _xy_not_elicited(mode, token, out):
        return
    assert out["env"]["success"] is True, out
    assert [h.split("b=")[1].split("&")[0] for h in out["hits"]] == ["original"], out


async def test_xy_over_cross_origin_frame_is_not_elicited_or_run(site):
    """결속 불가(좌표가 다른 출처 iframe 위) → 묻지 않고 실행하지 않는다(fail-closed)."""
    other = f"http://localhost:{site['port']}/frame"
    site["frame"] = "<!doctype html><button onclick=\"fetch('/clicked?b=frame')\" style='width:300px;height:200px'>결제</button>"
    site["html"] = (f"<!doctype html><meta charset=utf-8><iframe src='{other}' "
                    "style='position:absolute;left:0;top:0;width:400px;height:300px;border:0'></iframe>")

    async def nothing(_b: Any) -> None:
        return None

    out = await _run(site, "xy", nothing)
    assert out["messages"] == [], out
    assert out["env"]["success"] is False, out
    assert not out["hits"], out


def _change_after_server_check(expr: str, token: bool) -> Any:
    """서버 재확인(elicitation accept 뒤 · 증표 사용)을 통과한 **뒤** 디스패처 직전에 페이지가 바뀐다."""

    def prepare(backend: Any) -> None:
        name = "_use_approval" if token else "_recheck_after_accept"
        real = getattr(backend, name)

        async def then_change(*a: Any, **kw: Any) -> Any:
            r = await real(*a, **kw)
            await backend._dispatcher.ctx.page.evaluate(expr)
            return r

        setattr(backend, name, then_change)

    return prepare


@pytest.mark.parametrize("mode", ["xy", "key", "href"])
@pytest.mark.parametrize("token", [False, True], ids=["elicit", "approval_id"])
async def test_change_after_server_recheck_is_caught_by_dispatcher(site, mode, token):
    """디스패처도 실행 직전에 같은 노드·같은 목적지인지 본다(좌표 hit-test·포커스·href)."""
    if mode == "href":
        site["html"] = _LINK
        expr, run_mode = "document.querySelector('#pay').href='/clicked?b=bob'", "selector"
    else:
        expr, run_mode = "swap()", mode
    out = await _run(site, run_mode, None, token=token, prepare=_change_after_server_check(expr, token))
    if _xy_not_elicited(run_mode, token, out):
        return
    assert out["env"]["success"] is False, out
    assert out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out
    assert not out["hits"], out


# ------------------------------------------------------------------ B3: 같은 노드의 목적지·동작 변경


_LINK = '<!doctype html><meta charset=utf-8><p id=out>대기</p><a id=pay href="/clicked?b=alice">결제하기</a>'
_FORM = ('<!doctype html><meta charset=utf-8><p id=out>대기</p><form method=post action="/clicked?b=alice">'
         '<button id=pay>결제하기</button></form>')
_MUTATIONS = {
    "href": (_LINK, "document.querySelector('#pay').href='/clicked?b=bob'"),
    "form-action": (_FORM, "document.querySelector('form').action='/clicked?b=bob'"),
    "formaction": (_FORM, "document.querySelector('#pay').setAttribute('formaction','/clicked?b=bob')"),
    "method": (_FORM, "document.querySelector('form').method='get'"),
    "target": (_FORM, "document.querySelector('form').target='_blank'"),
}


@pytest.mark.parametrize("mode", ["selector", "element_id"])
@pytest.mark.parametrize("kind", list(_MUTATIONS))
@pytest.mark.parametrize("token", [False, True], ids=["elicit", "approval_id"])
async def test_destination_or_method_change_is_not_run(site, mode, kind, token):
    html, expr = _MUTATIONS[kind]
    site["html"] = html

    async def mutate(b: Any) -> None:
        await b._dispatcher.ctx.page.evaluate(expr)

    out = await _run(site, mode, mutate, token=token)
    assert not out["hits"], out
    assert out["env"]["success"] is False, out
    assert out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out


@pytest.mark.parametrize("html", [_LINK, _FORM], ids=["link", "form"])
async def test_unchanged_destination_runs_and_message_shows_it(site, html):
    """대조: 목적지 그대로면 실행된다. 승인 문구에 목적지(절대 주소)가 보인다."""
    site["html"] = html
    out = await _run(site, "selector")
    assert out["env"]["success"] is True, out
    assert any("b=alice" in h for h in out["hits"]), out
    message = out["messages"][0]
    line = next(x for x in message.split("\n") if x.startswith("목적지: "))
    assert line.endswith(f":{site['port']}/clicked?b=alice"), message  # 절대 주소(호스트 숫자는 코드 흉내 가림)
    if html is _FORM:
        assert line.startswith("목적지: POST "), message


async def test_destination_in_message_is_masked(site):
    """목적지 표시도 가림(WS-51 public_url)·표시 살균을 거친다 — 비밀 계열 쿼리 값은 보이지 않는다."""
    site["html"] = ('<!doctype html><meta charset=utf-8><a id=pay href="/clicked?b=alice&token=s3cr3tv4lue9&x=%0a%1b">'
                    "결제하기</a>")
    out = await _run(site, "selector")
    message = out["messages"][0]
    line = next(x for x in message.split("\n") if x.startswith("목적지: "))
    assert "s3cr3tv4lue9" not in message, message
    assert "\x1b" not in line and "\r" not in line, line


# ------------------------------------------------------------------ B4: 동시 승인 — 실제 입력 유실 없음


@pytest.mark.parametrize("normal", [False, True], ids=["three-approvals", "approval-and-normal"])
async def test_concurrent_approvals_each_really_click(site, normal):
    from mcp import types as t
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    server, b = create_server(recipes=False)
    waiting = asyncio.Event()
    release = asyncio.Event()
    out: Dict[str, Any] = {}

    async def elicit(ctx: Any, params: Any) -> Any:
        if "결제하기" in params.message:
            waiting.set()
            await release.wait()
        return t.ElicitResult(action="accept", content={})

    try:
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(*ss, server.create_initialization_options()))
                async with ClientSession(*cs, elicitation_callback=elicit) as s:
                    await s.initialize()
                    await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": site["url"] + "/"})
                    await b.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})

                    async def call(label: str, selector: str) -> None:
                        r = await s.call_tool(tool_name(ActionType.CLICK), {"selector": selector})
                        out[label] = json.loads(r.content[0].text)

                    with anyio.fail_after(60):
                        async with anyio.create_task_group() as calls:
                            calls.start_soon(call, "a", "#pay")
                            await waiting.wait()
                            if normal:
                                await call("b", "#normal")
                            else:
                                await asyncio.gather(call("b", "#pay2"), call("c", "#pay3"))
                            await b._page.evaluate("swap()")
                            release.set()
                    await b._page.wait_for_timeout(350)
                    out["hits"] = list(site["hits"])
                    out["client_hits"] = await b._page.evaluate("window.clientHits")
                tg.cancel_scope.cancel()
    finally:
        await b.close()
        b.hub.close()
    assert out["a"]["success"] is False, out
    assert out["b"]["success"] is True, out
    assert sorted(out["client_hits"]) == (["normal"] if normal else ["second", "third"]), out
    assert len(out["hits"]) == (1 if normal else 2), out
    assert all("replacement" not in h for h in out["hits"]), out
    if not normal:
        assert out["c"]["success"] is True, out
        for label, want in (("b", "second"), ("c", "third")):
            assert out[label]["data"]["approval"]["via"] == "elicitation", out


async def test_approval_exec_lock_serializes_and_is_released(site):
    """락: 승인 호출이 끝나면(성공·거부 모두) 락이 풀린다 — 다음 승인 호출이 막히지 않는다."""
    first = await _run(site, "selector", _swap)
    assert first["env"]["success"] is False
    second = await _run(site, "selector")
    assert second["env"]["success"] is True, second
