"""WS-51 R2 E2E — 재검증 B6(자유 문장 값 경계)·B7(Unicode 키) 의 실제 출구(실 Chromium + 로컬 페이지, headless).

* B6: 깨끗한 페이지의 `document.title` 에 `/a?token="X"`·`/a?token=prefix X` 를 넣고 observe — 제목
  (`data.observation.title`)은 내용 제외 경로가 아니라 가려져야 한다. 오류 봉투(error_message)도 같은 표본.
* B7: 전각 `ｔｏｋｅｎ`·long-s `ſeſſion` 키(Chromium 이 퍼센트 인코딩)로 이동한 navigate/observe 응답.

값은 짧은 합성 값. 공개 사이트 없음.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from contracts import ActionResult, ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, envelope_json, tool_name

from test_run_cli import requires_chromium

SHORT = "SYNTHr2E"
PAGE = b"<!doctype html><meta charset=utf-8><title>clean</title><button>Go</button>"

B6_TITLES = [f'/a?token="{SHORT}"', f"/a?token='{SHORT}'", f"/a?token=prefix {SHORT}",
             f'goto failed: http://127.0.0.1:1/a?token="{SHORT}"']


@pytest.fixture
def lab():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@requires_chromium
async def test_b6_observe_title_masks_quoted_and_spaced_values(lab):
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        assert (await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{lab}/clean"})).success
        for title in B6_TITLES:
            await s._page.evaluate("(t) => { document.title = t; }", title)
            raw = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
            assert raw.success
            assert SHORT in json.dumps(raw.model_dump(), ensure_ascii=False)  # 조건 성립: 가리기 전엔 원문
            wire = envelope_json(raw)
            assert SHORT not in wire, (title, wire[:600])
            assert "[REDACTED]" in json.loads(wire)["data"]["observation"]["title"]


@pytest.mark.parametrize("message", B6_TITLES)
def test_b6_error_envelope_masks(message):
    r = ActionResult(action=ActionType.NAVIGATE, success=False, snapshot_epoch=1,
                     current_url="http://127.0.0.1:1/", tab_id="t", retry_safe=True,
                     error_code=ErrorCode.TIMEOUT, error_message=message)
    wire = envelope_json(r)
    assert SHORT not in wire, wire
    assert "[REDACTED]" in json.loads(wire)["error_message"]


@requires_chromium
@pytest.mark.parametrize("key", ["ｔｏｋｅｎ", "ſeſſion", "ＧａｔｅＴｏｋｅｎ"])
async def test_b7_unicode_keys_never_reach_the_agent(lab, key):
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        nav = envelope_json(await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{lab}/n?{key}={SHORT}"}))
        obs = envelope_json(await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {}))
        assert json.loads(nav)["success"] and json.loads(obs)["success"]
        assert SHORT in s._page.url  # 실제 이동·내부 상태는 원문
        for text in (nav, obs):
            assert SHORT not in text, text[:600]
