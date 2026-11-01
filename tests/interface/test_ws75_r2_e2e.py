"""WS-75 R2 (#199 B3)·R3(사용자 결정 A — 끝 앞부분 가림 제거) E2E. 실 Chromium(headless) + 127.0.0.1 http, 합성 값만.

검증 재현(parent-proof): 실제 MCP `?password=2026PrivateKey` 등록 뒤 깨끗한 페이지의 안 잘린 일반 이름
`Order # 2026`(12)·`Order details for ordinary shipment 2026`(40)·이모지 120 단위 이름은 원문 그대로 보이고 정상 레시피가
저장된다(기준 ae4bd2d 와 같음). 묶음 표제(정확히 40자, 안 잘림)도 원문.
전체 일치: 같은 등록 값 전체가 든 이름은 wire 에서 가려지고 레시피는 저장되지 않는다.
문서화된 한계: 등록 값이 실제 120 자르기 경계에 걸치면 남은 앞부분은 원문으로 보인다(기준 ae4bd2d·v1.18.1 과 같음).
"""

from __future__ import annotations

import html
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_cli import requires_chromium  # noqa: E402

from browser.serve_profile import PROFILE_ROOT_ENV  # noqa: E402
from interface.handoff import STATE_ROOT_ENV  # noqa: E402
from interface.mcp_server import BrowserMCPServer, envelope_json  # noqa: E402
from security import secret_registry  # noqa: E402

V = "2026PrivateKey"
PAD = "Ordinary shipment details "
EMOJI = "\U0001F600"
NAMES = {"short12": "Order # 2026", "short40": "Order details for ordinary shipment 2026",
         "emoji120": EMOJI + (PAD * 6)[:114] + "2026", "near11": "Order: 2026", "plain": "Activate ordinary",
         "cut120": "x" * 113 + " " + V + " tail",  # 실제로 잘린다(앞 6자 `2026Pr` 이 경계에 남는다 — 한계)
         "whole": "Order " + V + " details"}
HEADING40 = NAMES["short40"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path / "servers"))
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def site():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            key = self.path.split("?")[0][1:]
            if key == "robots.txt":
                text = "User-agent: *\nAllow: /\n"
            elif key == "groups":
                members = "".join(f'<li><button onclick="this.dataset.clicked=1">{i}</button></li>' for i in range(1, 5))
                text = (f"<!doctype html><meta charset=utf-8><title>groups</title><main style=\"display:flex;gap:50px\">"
                        f"<section><h2>{html.escape(HEADING40)}</h2><ul>{members}</ul></section>"
                        f"<section><h2>Separate control</h2><ul>{members}</ul></section></main>")
            else:
                text = ("<!doctype html><meta charset=utf-8><title>ordinary provenance</title><button id=target "
                        "onclick=\"document.querySelector('#effect').textContent='right'\">"
                        + html.escape(NAMES.get(key, "Registration control")) + "</button><output id=effect></output>")
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _prefix_leaks(value: str, text: str):
    return [k for k in range(4, len(value) + 1) if value[:k] in text]


async def _run(site, tmp_path, key):
    async with BrowserMCPServer(headless=True, profile="ordinary-" + key, recipes=True, nav_settle=False,
                                handoff_root=tmp_path / ("h-" + key), pre_approved_actions=("click:*",)) as s:
        reg = await s.call_tool("browser_navigate", {"url": site + "/register?password=" + V})
        assert reg.success and len(secret_registry.REGISTRY) > 0
        nav = await s.call_tool("browser_navigate", {"url": site + "/" + key})
        assert nav.success, nav.error_message
        src = await s._page.evaluate("() => ({raw: target.textContent, cut: target.textContent.slice(0, 120)})")
        obs = await s.call_tool("browser_observe_page", {"force_full_tree": True})
        assert obs.success
        b = next(e for e in obs.data["observation"]["elements"] if e["role"] == "button")
        click = await s.call_tool("browser_click", {"element_id": b["element_id"], "epoch": obs.snapshot_epoch})
        effect = await s._page.locator("#effect").text_content()
        save = await s.call_server_tool("browser_recipe", {"op": "save", "name": "ordinary proof", "last_n": 2})
        path = s._recipes.store.path
        disk = path.read_text(encoding="utf-8") if path and path.exists() else ""
    assert click.success and effect == "right"
    return src, b["name"], envelope_json(obs), save, disk


@requires_chromium
@pytest.mark.parametrize("key", ["short12", "short40", "emoji120", "near11", "plain"])
async def test_uncut_ordinary_name_shown_raw_and_recipe_saved(site, tmp_path, key):
    src, shown, wire, save, disk = await _run(site, tmp_path, key)
    assert src["raw"] == src["cut"] == NAMES[key]  # 시험 조건: 원문이 자르기 길이 안 — 잘리지 않았다
    assert shown == NAMES[key], (key, shown)
    assert save["success"] is True, save
    assert "ordinary proof" in disk and " ".join(NAMES[key].split()).lower() in disk


@requires_chromium
async def test_whole_registered_value_masked_in_wire_and_recipe_refused(site, tmp_path):
    src, shown, wire, save, disk = await _run(site, tmp_path, "whole")
    assert src["raw"] == src["cut"]  # 시험 조건: 잘리지 않았다
    assert shown == "Order [REDACTED] details", shown
    assert V not in wire and V.casefold() not in disk.casefold()
    assert save["success"] is False


@requires_chromium
async def test_documented_limit_cut_prefix_stays_like_baseline(site, tmp_path):
    """문서화된 한계: producer 가 자른 경계에 걸친 등록 값은 앞부분이 원문으로 남는다(끝 앞부분 가림 없음 — 기준과 같음)."""
    src, shown, wire, save, disk = await _run(site, tmp_path, "cut120")
    assert src["cut"] != src["raw"] and src["cut"].endswith(V[:6])  # 시험 조건: 실제로 잘려 앞부분이 끝에 남는다
    assert shown == src["cut"], shown
    assert V not in wire and "[REDACTED]" not in json.dumps(json.loads(wire)["data"]["observation"]["elements"])


@requires_chromium
async def test_uncut_heading_40_shown_raw(site, tmp_path):
    async with BrowserMCPServer(headless=True, nav_settle=False, recipes=False, handoff_root=tmp_path / "h") as s:
        reg = await s.call_tool("browser_navigate", {"url": site + "/register?password=" + V})
        assert reg.success and len(secret_registry.REGISTRY) > 0
        assert (await s.call_tool("browser_navigate", {"url": site + "/groups"})).success
        out = await s.call_tool("browser_observe_page", {"force_full_tree": True})
    groups = (json.loads(envelope_json(out))["data"].get("repeated") or {}).get("groups", [])
    assert len(HEADING40) == 40
    assert HEADING40 in [g.get("group") for g in groups], groups
