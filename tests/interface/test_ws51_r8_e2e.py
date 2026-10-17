"""WS-51 R8 E2E — 실 Chromium(headless) + 로컬 http, R7-B1(`data.dialogs[].type`) 검증자 재현 조건.

검증자 재현(`verify51-r7/census/runtime_siblings.py dialogs`)과 같은 경로: `/bu` 에서 observe 반환 ID 로 버튼을 눌러
beforeunload 리스너(사용자 활성화 포함)를 건 뒤, observe 반환 링크 ID 로 `/target` 이동 click — 디스패처가 실제
Playwright Dialog 를 받아 `data.dialogs[0].type='beforeunload'`(Dialog.type) 를 싣는다. 별도 탭에서
`/seen?token=beforeunload` 를 관측해 실제 등록한 뒤 같은 동작을 반복 — 최종 JSON wire 의 type 이 원래와 같다.

* 생산: actions/dispatcher.py `_on_dialog` (`kind = str(dialog.type)` → `{"type": kind, …}`) → `_result` 의 data.dialogs.
* alert·confirm·prompt 는 4~7자 순수 알파벳이라 평소 등록되지 않는다(secret_registry._eligible) — 그 제외를 끈 조건
  (이중 방어)에서도 type 은 같고, 자유 문장 칸 message 의 같은 낱말은 가려진다(면제가 키 경로에만 걸린다).

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
_SHORT_TYPES = ("alert", "confirm", "prompt")
_PAGES = {
    "/bu": ("<title>Beforeunload</title><button onclick=\"window.addEventListener('beforeunload',"
            "e=>{e.preventDefault();e.returnValue='';});this.textContent='Armed'\">Arm exit</button>"
            "<a href=/target>Leave page</a>"),
    **{f"/{t}": f"<title>{t}</title><button onclick=\"{t}('note {t} here')\">Show box {i}</button>"
       for i, t in enumerate(_SHORT_TYPES)},
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


async def _click_named(s, name):
    r, obs = await _call(s, ActionType.OBSERVE_PAGE)
    assert r.success, obs
    eid = next(e["element_id"] for e in obs["data"]["observation"]["elements"] if e["name"] == name)
    return await _call(s, ActionType.CLICK, {"element_id": eid, "epoch": obs["snapshot_epoch"]})


async def _beforeunload(s, origin):
    r, w = await _call(s, ActionType.NAVIGATE, {"url": origin + "/bu"})
    assert r.success, w
    r, w = await _click_named(s, "Arm exit")
    assert r.success, w
    r, w = await _click_named(s, "Leave page")
    assert r.success, w
    assert s._page.url.endswith("/target"), s._page.url  # 실제로 문서가 떠났다(수락)
    return r, w


async def _short(s, origin, kind):
    r, w = await _call(s, ActionType.NAVIGATE, {"url": f"{origin}/{kind}"})
    assert r.success, w
    r, w = await _click_named(s, f"Show box {_SHORT_TYPES.index(kind)}")
    assert w["data"].get("dialogs"), w
    return r, w


@requires_chromium
async def test_beforeunload_dialog_type_survives_real_registration(origin, tmp_path: Path):
    async with BrowserMCPServer(headless=True, handoff_root=tmp_path / "hub") as s:
        r, before = await _beforeunload(s, origin)
        # 조건 성립: 실제 Dialog.type 이 생산됐다(등록 전).
        assert before["data"]["dialogs"][0]["type"] == "beforeunload", before
        assert before["data"]["dialogs"][0]["handled"] == "accepted"
        assert len(sr.REGISTRY) == 0

        await _see(s, origin, ["beforeunload"])
        assert len(sr.REGISTRY) == 1, sr.REGISTRY  # 실제 주소 관측으로 등록(12자 — 평소에도 등록 대상)
        assert public_text("x beforeunload y") == f"x {R} y"

        r, after = await _beforeunload(s, origin)
        assert r.data["dialogs"][0]["type"] == "beforeunload"  # 내부 원래 값
        assert after["data"]["dialogs"][0]["type"] == "beforeunload", "CONTROL_INVARIANT dialogs[].type/beforeunload"
        assert after["data"]["dialogs"][0]["handled"] == "accepted"


@requires_chromium
async def test_short_dialog_types_survive_with_eligibility_off(origin, tmp_path: Path, monkeypatch):
    """alert·confirm·prompt — 등록 제외(_eligible)를 끈 이중 방어 조건. message 의 같은 낱말은 가린다."""
    eligible = sr._eligible
    monkeypatch.setattr(sr, "_eligible", lambda base: False if base in _SHORT_TYPES else eligible(base))
    async with BrowserMCPServer(headless=True, handoff_root=tmp_path / "hub") as s:
        before = {k: (await _short(s, origin, k))[1]["data"]["dialogs"][0] for k in _SHORT_TYPES}
        assert {k: d["type"] for k, d in before.items()} == {k: k for k in _SHORT_TYPES}, before
        assert all(d["message"] == f"note {k} here" for k, d in before.items()), before
        assert len(sr.REGISTRY) == 0

        await _see(s, origin, list(_SHORT_TYPES))
        assert len(sr.REGISTRY) == 3, sr.REGISTRY
        assert all(public_text(f"x {k} y") == f"x {R} y" for k in _SHORT_TYPES)

        after = {k: (await _short(s, origin, k))[1]["data"]["dialogs"][0] for k in _SHORT_TYPES}
        assert {k: d["type"] for k, d in after.items()} == {k: k for k in _SHORT_TYPES}, "CONTROL_INVARIANT dialogs[].type"
        assert {k: d["handled"] for k, d in after.items()} == {k: d["handled"] for k, d in before.items()}
        assert all(d["message"] == f"note {R} here" for d in after.values()), after  # 자유 문장은 가림
