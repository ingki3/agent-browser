"""WS-51 R7 E2E — 실 Chromium(headless) + 로컬 http, R6-B1(frame 제어값) 검증자 재현 조건.

검증자 재현(`verify51-r6/identifiers/runtime.py` frame)과 같은 경로: `/outer` → 실제 `switch_frame('#inner')` →
observe 의 element_id 로 iframe 안 링크 click → `/inner2` 문서 이동(`data.nav_frame='current_frame'`). 별도 탭에서
`/seen?token=current_frame`(·`root`)을 관측해 실제 등록한 뒤 같은 동작을 반복 — 최종 JSON wire 의 제어값이 원래와 같다.

* `data.nav_frame` — 생산 actions/dispatcher.py `_NavWatch` (`info["nav_frame"] = "current_frame"`).
* `data.resolved_from` — 생산 actions/dispatcher.py `_switch_frame` (`"current_frame"` / `"root"`), 도구 안내
  interface/server_schema.py `data.resolved_from(current_frame|root)`.
* census 형제(R7): 프레임 안 take_screenshot 의 `data.captured='root_page'`, alert 의 `data.dialogs[].handled`.

값은 합성 값. 공개 사이트 없음(127.0.0.1 만).
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, envelope_json, tool_name
from security import secret_registry as sr
from security.public_url import public_text

from test_run_cli import requires_chromium

R = "[REDACTED]"
_PAGES = {
    "/outer": "<title>Outer</title><iframe id=inner src=/inner></iframe>",
    "/inner": "<title>Inner</title><a href=/inner2>Move frame</a>",
    "/inner2": "<title>Moved frame</title><input aria-label='Frame input'>",
    "/dialog": "<title>Dialog</title><button onclick=\"alert('synthetic')\">Open alert</button>",
}


@pytest.fixture(autouse=True)
def _fresh_registry():
    sr.clear()
    yield
    sr.clear()


@pytest.fixture
def origin():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = ("<!doctype html><meta charset=utf-8>"
                    + _PAGES.get(self.path.split("?", 1)[0], "<title>clean</title><p>clean</p>")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(s, action, params=None):
    res = await s.call_tool(tool_name(action), params or {})
    return res, json.loads(envelope_json(res))


async def _see(s, origin, tokens):
    """별도 실제 탭에서 `/seenN?token=값` 을 관측 — 실제 주소 관측 경로로 등록."""
    page = await s._page.context.new_page()
    try:
        for i, t in enumerate(tokens):
            await page.goto(f"{origin}/seen{i}?token={quote(t, safe='')}", wait_until="domcontentloaded")
        await asyncio.sleep(0.1)
    finally:
        await page.close()


async def _frame_round(s, origin):
    """메인 복귀 → /outer → switch_frame('#inner')(current_frame) → iframe 링크 click(nav_frame) →
    다시 switch_frame('#inner')(현재 프레임 안에 없음 → root). 세 wire 를 돌려준다."""
    await _call(s, ActionType.SWITCH_FRAME, {"to_main": True})
    r, _ = await _call(s, ActionType.NAVIGATE, {"url": origin + "/outer"})
    assert r.success, r.error_message
    r, sw_current = await _call(s, ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
    assert r.success, sw_current
    r, obs = await _call(s, ActionType.OBSERVE_PAGE)
    assert r.success, obs
    link = next(e["element_id"] for e in obs["data"]["observation"]["elements"]
                if e["role"] == "link" and e["name"] == "Move frame")
    r, click = await _call(s, ActionType.CLICK, {"element_id": link, "epoch": obs["snapshot_epoch"]})
    assert r.success, click
    r, sw_root = await _call(s, ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
    assert r.success, sw_root
    return sw_current, click, sw_root


@requires_chromium
async def test_frame_control_values_survive_real_registration(origin, tmp_path: Path, monkeypatch):
    """`root` 은 4자 알파벳 낱말이라 평소 등록되지 않는다(secret_registry._eligible) — 그 제외를 끈 조건에서도 제어 칸
    값이 같아야 하므로(키별 어휘 이중 방어) 등록 판정만 바꿔 실제 주소 관측으로 등록시킨다."""
    eligible = sr._eligible
    monkeypatch.setattr(sr, "_eligible", lambda base: False if base == "root" else eligible(base))
    async with BrowserMCPServer(headless=True, handoff_root=tmp_path / "hub") as s:
        sw_current, click, sw_root = await _frame_round(s, origin)
        # 조건 성립: 생산자가 실제로 세 값을 냈다(등록 전).
        assert sw_current["data"]["resolved_from"] == "current_frame", sw_current
        assert click["data"]["nav_frame"] == "current_frame", click
        assert sw_root["data"]["resolved_from"] == "root", sw_root
        assert len(sr.REGISTRY) == 0

        await _see(s, origin, ["current_frame", "root"])
        assert len(sr.REGISTRY) == 2, sr.REGISTRY  # 조건 성립: 실제 주소 관측으로 두 값이 등록됐다
        assert public_text("x current_frame y") == f"x {R} y"
        assert public_text("x root y") == f"x {R} y"

        sw_current, click, sw_root = await _frame_round(s, origin)
        got = {"nav_frame": click["data"]["nav_frame"], "resolved_from/current_frame": sw_current["data"]["resolved_from"],
               "resolved_from/root": sw_root["data"]["resolved_from"]}
        assert got == {"nav_frame": "current_frame", "resolved_from/current_frame": "current_frame",
                       "resolved_from/root": "root"}, "CONTROL_INVARIANT frame"


@requires_chromium
async def test_frame_screenshot_captured_and_dialog_handled_survive(origin, tmp_path: Path):
    async with BrowserMCPServer(headless=True, handoff_root=tmp_path / "hub") as s:
        async def shot():
            await _call(s, ActionType.SWITCH_FRAME, {"to_main": True})
            r, _ = await _call(s, ActionType.NAVIGATE, {"url": origin + "/outer"})
            assert r.success
            r, _ = await _call(s, ActionType.SWITCH_FRAME, {"frame_selector": "#inner"})
            assert r.success
            r, w = await _call(s, ActionType.TAKE_SCREENSHOT)
            assert r.success, w
            return w

        async def dialog():
            await _call(s, ActionType.SWITCH_FRAME, {"to_main": True})
            r, _ = await _call(s, ActionType.NAVIGATE, {"url": origin + "/dialog"})
            assert r.success
            r, obs = await _call(s, ActionType.OBSERVE_PAGE)
            btn = next(e["element_id"] for e in obs["data"]["observation"]["elements"] if e["name"] == "Open alert")
            r, w = await _call(s, ActionType.CLICK, {"element_id": btn, "epoch": obs["snapshot_epoch"]})
            assert w["data"].get("dialogs"), w
            return w

        before_shot, before_dialog = await shot(), await dialog()
        assert before_shot["data"]["captured"] == "root_page"
        handled = before_dialog["data"]["dialogs"][0]["handled"]
        assert handled in ("accepted", "dismissed")
        await _see(s, origin, ["root_page", handled])
        assert len(sr.REGISTRY) == 2 and public_text("x root_page y") == f"x {R} y"
        assert public_text(f"x {handled} y") == f"x {R} y"
        after_shot, after_dialog = await shot(), await dialog()
        assert after_shot["data"]["captured"] == "root_page", "CONTROL_INVARIANT captured"
        assert after_dialog["data"]["dialogs"][0]["handled"] == handled, "CONTROL_INVARIANT handled"
