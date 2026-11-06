"""WS-85 (#171 #218 #163): 판정 읽기를 격리 world 로도 — 페이지가 getter·prototype 을 바꿔 판정을 속이는 경로.

결합 규칙(리뷰 B1): #171 challenge = 격리 결과, 없으면 페이지 결과(OR). #218 민감 칸 = 페이지 OR 격리(입력 직후
격리 world activeElement — 실패 시 '민감 처리' 금지, B4). #163 클릭·selector·개행 = 두 판정을 각각 _gate_rank 로 분류해
높은 쪽(같으면 페이지), 판독할 수 있어야 하는데 실패하고 페이지 판정 < HIGH 면 승인 경로(B2). 실제 BrowserMCPServer
(헤드리스 Chromium) + 로컬 HTTP 만, 비밀값은 합성. 실제 효과(제출·눌림)는 페이지 world 를 거치지 않는 CDP 격리 판독으로
단정한다(_truth).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401

PAGES: Dict[str, str] = {}


def _serve(host: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            body = PAGES.get(path, "<!doctype html><meta charset=utf-8><title>x</title><p>x</p>")
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer((host, 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def site():
    srv = _serve("127.0.0.1")
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(server: BrowserMCPServer, action: ActionType, args: Dict[str, Any]):
    return await server.call_tool(tool_name(action), args)


def _page(server: BrowserMCPServer):
    return server._dispatcher.ctx.page  # noqa: SLF001


async def _truth(server: BrowserMCPServer, expression: str) -> Any:
    """실제 값 — 페이지 world getter 를 거치지 않는 CDP 격리 world 판독(위조가 닿지 않는다)."""
    return await server._dispatcher.isolated_top_evaluate(expression)  # noqa: SLF001


async def _status(server: BrowserMCPServer) -> str:
    await _page(server).wait_for_timeout(150)
    return str(await _truth(server, "document.getElementById('out').textContent"))


def _script(js: str) -> str:
    return f"<script>{js}</script>" if js else ""


HEAD = "<!doctype html><meta charset=utf-8><title>g</title><body style=margin:0><p id=out>대기</p>"
ON_SUBMIT = "onsubmit=\"document.getElementById('out').textContent='제출됨';return false\""
ON_CLICK = "onclick=\"document.getElementById('out').textContent='눌림'\""
SECRET = "SynthPw-7x9Q-ws85"

# ------------------------------------------------------------------ #171 challenge·handoff

T_CHAL = ("Object.defineProperty(HTMLElement.prototype,'innerText',{get(){return '환영합니다'},configurable:true});"
          "Object.defineProperty(Document.prototype,'title',{get(){return '홈'},configurable:true});"
          "Object.defineProperty(Node.prototype,'textContent',{get(){return '환영합니다'},configurable:true});")
CHAL_PAGE = ("<!doctype html><meta charset=utf-8><title>보안 확인</title><body><p>보안 확인을 완료해 주세요</p>")


@requires_chromium
async def test_171_hidden_challenge_detected_with_handoff(site):
    PAGES["/c0"] = CHAL_PAGE
    PAGES["/c1"] = CHAL_PAGE + _script(T_CHAL)
    async with BrowserMCPServer() as server:
        base = await _call(server, ActionType.NAVIGATE, {"url": site + "/c0"})
        assert base.data.get("challenge") is not None  # 전제: 위조 전에는 잡힌다
        # 전제(규칙 4): 페이지 world 프로브는 실제로 속는다.
        from browser.challenge import detect_challenge

        hidden = await _call(server, ActionType.NAVIGATE, {"url": site + "/c1"})
        fooled = await detect_challenge(_page(server))
    assert fooled.detected is False
    assert hidden.data.get("challenge") == base.data["challenge"]
    assert hidden.data.get("handoff") is not None
    assert hidden.data.get("handoff") == base.data.get("handoff")


@requires_chromium
async def test_171_normal_page_has_no_challenge(site):
    PAGES["/n"] = HEAD + "<h1>환영합니다</h1><p>평범한 본문</p>"
    async with BrowserMCPServer() as server:
        r = await _call(server, ActionType.NAVIGATE, {"url": site + "/n"})
    assert r.success and r.data.get("challenge") is None and "handoff" not in r.data


# ------------------------------------------------------------------ #218 민감 칸

T_TYPE = "Object.defineProperty(HTMLInputElement.prototype,'type',{get(){return 'text'},configurable:true});"


def _eid(obs, pred) -> str:
    for e in obs.data["observation"]["elements"]:
        if pred(e):
            return e["element_id"]
    raise AssertionError(obs.data["observation"]["elements"])


@requires_chromium
async def test_218_spoofed_password_type_text_hides_secret(site):
    PAGES["/p"] = HEAD + "<input id=p type=password aria-label='pw'>" + _script(T_TYPE)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/p"})
        assert await _page(server).evaluate("document.getElementById('p').type") == "text"  # 전제: 위조됨
        assert await _truth(server, "document.getElementById('p').type") == "password"
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.TYPE_TEXT, {
            "element_id": _eid(obs, lambda e: "pw" in (e.get("name") or "")), "text": SECRET,
            "epoch": obs.snapshot_epoch})
        dump = json.dumps(r.model_dump(mode="json"), ensure_ascii=False)
        typed = await _truth(server, "document.getElementById('p').value")
    assert typed == SECRET  # 실제로 입력됐다
    assert SECRET not in dump, dump


@requires_chromium
async def test_218_spoofed_password_press_key_is_sensitive(site):
    PAGES["/p"] = HEAD + "<input id=p type=password aria-label='pw'>" + _script(T_TYPE)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/p"})
        await _page(server).focus("#p")
        k = await _call(server, ActionType.PRESS_KEY, {"key": "z"})
        sensitive = server._dispatcher.last_key_sensitive  # noqa: SLF001
    assert k.success, k.error_message
    assert sensitive is True


@requires_chromium
async def test_218_plain_input_not_registered_as_secret(site):
    """과잉 0(B4): 일반 입력칸(검색어)은 비밀로 등록하지 않는다 — 결과에 값이 그대로 남는다."""
    PAGES["/s"] = HEAD + "<input id=q aria-label='검색어'>"
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/s"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.TYPE_TEXT, {
            "element_id": _eid(obs, lambda e: "검색어" in (e.get("name") or "")), "text": "plainword-ws85",
            "epoch": obs.snapshot_epoch})
        await _page(server).focus("#q")
        k = await _call(server, ActionType.PRESS_KEY, {"key": "a"})
        sensitive = server._dispatcher.last_key_sensitive  # noqa: SLF001
        from security.secret_registry import mask_registered

        masked = mask_registered("plainword-ws85")
    assert r.success and k.success
    assert sensitive is False
    assert masked == "plainword-ws85", masked
