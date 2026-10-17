"""WS-51 R5 E2E — 실 Chromium(headless) + 로컬 http, R4 검증 BLOCKING 4건의 검증자 재현 조건.

* R4-B1: 실제 `/seen?token=98765432` 이동 뒤 표준 Formatter(`%(message)s %(metadata)s`) — 문자열·int·중첩 int extra 원문 0.
* R4-B2: 실제 이동으로 330,000자 값 등록 → `representations(v)[16]`(R2∘R3, 930,000자)을 제목으로 → 실 observe+wire 원문 0.
* R4-B3: 실제 이동으로 `synthsecret99`·`synth_secret_123` 등록 → hub.request(같은 값) → 실제 browser_control_status 의
  data.control.reason == '[REDACTED]'.
* R4-B4: 실제 browser_recipe save → 그 id 를 token 주소에서 봄 → list 의 id 가 원래와 같고 run·delete 가 그 id 로 동작.

값은 합성 값. 공개 사이트 없음(127.0.0.1 만).
"""

from __future__ import annotations

import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, envelope_json, install_public_log_filter, tool_name
from security import secret_registry as sr
from security.public_url import public_text

from test_run_cli import requires_chromium

R = "[REDACTED]"
PAGE = b"<!doctype html><meta charset=utf-8><title>clean</title><button>Go</button>"


@pytest.fixture(autouse=True)
def _fresh_registry():
    sr.clear()
    yield
    sr.clear()


@pytest.fixture
def origin():
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


async def _nav(s, url):
    return await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": url})


@requires_chromium
async def test_b1_numeric_extra_after_real_navigate(origin):
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        v = "98765432"
        assert (await _nav(s, origin + "/seen?token=" + v)).success
        assert len(sr.REGISTRY) == 1 and public_text(v) == R  # 조건 성립
        out = io.StringIO()
        h = logging.StreamHandler(out)
        h.setFormatter(logging.Formatter("%(message)s %(metadata)s"))
        log = logging.getLogger("ws51.r5.e2e.numeric")
        log.handlers, log.propagate = [h], False
        log.setLevel(logging.INFO)
        restore = install_public_log_filter()
        try:
            for meta in (v, int(v), {"secret": int(v)}, {int(v): "key"}, float(v)):
                log.info("retained", extra={"metadata": meta})
        finally:
            restore()
            log.handlers = []
            h.close()
        lines = out.getvalue().splitlines()
        assert len(lines) == 5
        assert [line for line in lines if any(r in line for r in sr.representations(v))] == [], lines


@requires_chromium
async def test_b2_retained_value_keeps_all_forms_real_wire(origin):
    v = "SYNTH\u00e9<&\"'/" * 30000
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        nav = await _nav(s, origin + "/seen?token=" + quote(v, safe=""))
        assert nav.success
        assert len(sr.REGISTRY) == 1 and len(sr.REGISTRY) < sr.REGISTRY.cap
        form = sr.representations(v)[16]
        assert form == quote(json.dumps(v)[1:-1], safe="")  # 조건 성립: R2∘R3 표현
        await s._page.evaluate("(t) => { document.title = t; }", form)
        raw = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        assert raw.success
        title = json.loads(envelope_json(raw))["data"]["observation"]["title"]
        assert len(title) < 1000 and form not in title, (len(title), title[:100])


@requires_chromium
async def test_b4_recipe_id_survives_save_list_run_delete(origin, tmp_path: Path):
    async with BrowserMCPServer(headless=True, nav_settle=False, handoff_root=tmp_path / "servers") as s:
        for path in ("/first", "/second"):
            assert (await _nav(s, origin + path)).success
        saved = await s.call_server_tool("browser_recipe", {"op": "save", "name": "합성정상레시피", "last_n": 1})
        assert saved["success"], saved
        rid = saved["data"]["recipe"]["id"]
        assert (await _nav(s, origin + "/seen?token=" + rid)).success  # 도구 식별자를 주소에서 봄
        listed = await s.call_server_tool("browser_recipe", {"op": "list"})
        returned = next(r for r in listed["data"]["recipes"] if r["name"] == "합성정상레시피")["id"]
        assert returned == rid, (returned, rid)
        ran = await s.call_server_tool("browser_recipe", {"op": "run", "id": returned})
        assert ran["data"]["recipe"]["id"] == rid, ran
        deleted = await s.call_server_tool("browser_recipe", {"op": "delete", "id": returned})
        assert deleted["success"], deleted
        after = await s.call_server_tool("browser_recipe", {"op": "list"})
        assert all(r["id"] != rid for r in after["data"]["recipes"])


@requires_chromium
async def test_b3_control_status_free_text_reason_masked(origin, tmp_path: Path):
    async with BrowserMCPServer(headless=True, nav_settle=False, handoff_root=tmp_path / "servers") as s:
        for v in ("synthsecret99", "synth_secret_123"):
            sr.clear()
            assert (await _nav(s, origin + "/seen?token=" + v)).success
            assert len(sr.REGISTRY) == 1
            s.hub.request(v)
            status = await s.call_server_tool("browser_control_status", {})
            assert status["data"]["control"]["reason"] == R, status["data"]["control"]
            assert v not in json.dumps(status, ensure_ascii=False)
            assert status["data"]["control"]["holder"] == "agent"  # 제어 값은 그대로
