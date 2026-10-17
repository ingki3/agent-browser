"""WS-51 R9 E2E — 실 Chromium(headless) + 로컬 http, R8-B1(`data.healing_attempts` 복합 코드)·R8-B2(`data.signals` 순수 코드)
검증자 재현 조건(`.hermes/verify/ws51-r8/report.md` '실사용 재현').

* R8-B1: `/notes` 를 관찰한 뒤 그 ID 의 `Open notes` 버튼을 지우고 `Open notes!`(또는 `Open notes!`·`Open notes?` 둘)로 바꿔 옛 ID 로 click.
  실제 디스패처가 요소 사라짐(NODE_DETACHED) → 실제 heal() 사다리 → 신원 변경 거부(또는 모호함)로 실패하고
  `data.healing_attempts` 에 `text_similarity(identity_changed)`(또는 `text_similarity(ambiguous)`)를 싣는다. 별도 탭에서
  `/seen?token=text_similarity`(기본 전략명)·`…token=text_similarity(identity_changed)`(전체 복합 코드)를 관측해 실제 등록한
  뒤에도 최종 JSON wire 의 목록이 내부 원래 값과 같다.
* R8-B2: `/tc` 에서 본문 텍스트를 바꾸는 버튼·aria-pressed 를 토글하는 버튼을 observe 반환 ID 로 click — 실제
  verify_post_condition 이 `text_changed`·`element_attr_changed` 를 낸다. `text_changed`·`element_attr_changed` 를 실제 등록한
  뒤에도 wire 의 그 코드는 같고, 같은 응답의 페이지 유래 자유 문장(`focus_moved: … -> BUTTON#<페이지 id>`)에 든 등록 값은
  가려진다(R4-B3 재발 금지 — 목록 통째 면제가 아님).

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
_PAGE_SECRET = "SyntheticSecret88"
_PAGES = {
    "/notes": "<title>Notes</title><button id=pay>Open notes</button><p>total 10</p>",
    "/tc": ("<title>Text</title><p id=t>old words</p>"
            f"<button id={_PAGE_SECRET} onclick=\"document.getElementById('t').textContent='new words'\">"
            "Change text</button>"
            "<button aria-pressed=false onclick=\"this.setAttribute('aria-pressed',"
            "this.getAttribute('aria-pressed')=='true'?'false':'true')\">Toggle mode</button>"),
}
_SWAP_ONE = ("() => { document.getElementById('pay').remove(); const b = document.createElement('button');"
             " b.textContent = 'Open notes!'; document.body.appendChild(b); }")
_SWAP_TWO = ("() => { document.getElementById('pay').remove(); for (const t of ['Open notes!', 'Open notes?']) {"
             " const b = document.createElement('button'); b.textContent = t; document.body.appendChild(b); } }")


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


async def _observe_id(s, name):
    r, obs = await _call(s, ActionType.OBSERVE_PAGE)
    assert r.success, obs
    eid = next(e["element_id"] for e in obs["data"]["observation"]["elements"] if e["name"] == name)
    return eid, obs["snapshot_epoch"]


async def _swapped_click(s, origin, swap):
    r, w = await _call(s, ActionType.NAVIGATE, {"url": origin + "/notes"})
    assert r.success, w
    eid, epoch = await _observe_id(s, "Open notes")
    await s._page.evaluate(swap)
    return await _call(s, ActionType.CLICK, {"element_id": eid, "epoch": epoch})


async def _signal_click(s, origin, name):
    r, w = await _call(s, ActionType.NAVIGATE, {"url": origin + "/tc"})
    assert r.success, w
    eid, epoch = await _observe_id(s, name)
    return await _call(s, ActionType.CLICK, {"element_id": eid, "epoch": epoch})


# ------------------------------------------------------------------ R8-B1 healing_attempts


@requires_chromium
@pytest.mark.parametrize("token", ["text_similarity", "text_similarity(identity_changed)"])
async def test_identity_refusal_healing_attempts_survive_registration(origin, tmp_path: Path, token):
    async with BrowserMCPServer(headless=True, handoff_root=tmp_path / "hub") as s:
        r, before = await _swapped_click(s, origin, _SWAP_ONE)
        # 조건 성립: 실제 heal() 이 신원 변경을 거부했다(누르지 않음) — 복합 코드가 생산됐다.
        assert not r.success and r.error_code is not None and r.error_code.value == "E_TOCTOU_MISMATCH", before
        assert "text_similarity(identity_changed)" in r.data["healing_attempts"], r.data
        assert before["data"]["healing_attempts"] == r.data["healing_attempts"]
        assert len(sr.REGISTRY) == 0

        await _see(s, origin, [token])
        assert len(sr.REGISTRY) == 1, sr.REGISTRY
        assert public_text(f"x {token} y") == f"x {R} y"

        r, after = await _swapped_click(s, origin, _SWAP_ONE)
        assert not r.success and r.error_code.value == "E_TOCTOU_MISMATCH"  # 원래 신원 거부 유지
        assert "text_similarity(identity_changed)" in r.data["healing_attempts"]
        assert after["data"]["healing_attempts"] == r.data["healing_attempts"], \
            "CONTROL_INVARIANT healing_attempts/" + token


@requires_chromium
async def test_ambiguous_healing_attempts_survive_registration(origin, tmp_path: Path):
    async with BrowserMCPServer(headless=True, handoff_root=tmp_path / "hub") as s:
        r, before = await _swapped_click(s, origin, _SWAP_TWO)
        assert not r.success and "text_similarity(ambiguous)" in r.data["healing_attempts"], before
        assert before["data"]["healing_attempts"] == r.data["healing_attempts"]

        await _see(s, origin, ["text_similarity(ambiguous)", "text_similarity"])
        assert len(sr.REGISTRY) == 2, sr.REGISTRY

        r, after = await _swapped_click(s, origin, _SWAP_TWO)
        assert not r.success and "text_similarity(ambiguous)" in r.data["healing_attempts"]
        assert after["data"]["healing_attempts"] == r.data["healing_attempts"], \
            "CONTROL_INVARIANT healing_attempts/ambiguous"


# ------------------------------------------------------------------ R8-B2 signals


@requires_chromium
@pytest.mark.parametrize(("button", "code"), [("Change text", "text_changed"), ("Toggle mode", "element_attr_changed")])
async def test_pure_signal_code_survives_registration(origin, tmp_path: Path, button, code):
    async with BrowserMCPServer(headless=True, handoff_root=tmp_path / "hub") as s:
        r, before = await _signal_click(s, origin, button)
        assert r.success, before
        assert code in r.data["signals"], r.data  # 조건 성립: 실제 verify_post_condition 이 순수 코드를 냈다
        assert before["data"]["signals"] == r.data["signals"]
        assert len(sr.REGISTRY) == 0

        await _see(s, origin, [code])
        assert len(sr.REGISTRY) == 1, sr.REGISTRY
        assert public_text(f"x {code} y") == f"x {R} y"

        r, after = await _signal_click(s, origin, button)
        assert r.success and code in r.data["signals"]
        assert after["data"]["signals"] == r.data["signals"], "CONTROL_INVARIANT signals/" + code


@requires_chromium
async def test_page_detail_in_signals_still_masked(origin, tmp_path: Path):
    """같은 signals 목록의 페이지 유래 상세(`focus_moved: … -> BUTTON#<페이지 id>`)는 등록 값이 가려진다."""
    async with BrowserMCPServer(headless=True, handoff_root=tmp_path / "hub") as s:
        r, before = await _signal_click(s, origin, "Change text")
        focus = [x for x in r.data["signals"] if x.startswith("focus_moved:")]
        assert focus and _PAGE_SECRET in focus[0], r.data  # 조건 성립: 페이지 id 가 상세 문장에 실렸다

        await _see(s, origin, [_PAGE_SECRET, "text_changed"])
        assert len(sr.REGISTRY) == 2, sr.REGISTRY

        r, after = await _signal_click(s, origin, "Change text")
        wire = after["data"]["signals"]
        assert "text_changed" in wire, wire
        detail = [x for x in wire if x.startswith("focus_moved:")]
        assert detail and _PAGE_SECRET not in json.dumps(after) and R in detail[0], wire
