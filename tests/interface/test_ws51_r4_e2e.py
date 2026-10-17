"""WS-51 R4 E2E — 실 Chromium(headless) + 로컬 http 대표 경로(계획 §3 수용 기준).

* R3-B1: `/seen?token=V` 이동 뒤 `/alias?ref=V`·`/V`·`http://v.localhost/`(호스트 라벨) navigate·observe 원문 0.
* R3-B5: 65,545자 값으로 이동 → 제목 0, 잘린 값·마지막 글자 변경 → 원문 연속 256자 이상 0.
* R3-B6: 실 observe + 최종 wire 1MiB < 1초(5회 중앙값, 매회 긴 문장·주소 칸 캐시 비움).
* R3-B7: `?token=navigate|observe_page|tab-1|E_INVALID_URL|approved` 각각 × navigate·observe·tab 생성·전환 —
  제어 필드 값이 원래 값과 같고 탭 전환이 성공한다(control_status·approval_wait 는 test_ws51_r4_server).
* P7: 다운로드 대상 주소의 값이 등록된다(컨텍스트 감시를 끄고 디스패처 경로만).

값은 합성 값. 공개 사이트 없음(127.0.0.1·*.localhost 만).
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, envelope_json, tool_name
from security import secret_registry

from test_run_cli import requires_chromium

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "security"))
from ws51_r4_leak import leaks_in_text, leaks_in_wire  # noqa: E402

V = "SYNTH83Secret"
PAGE = b"<!doctype html><meta charset=utf-8><title>clean</title><button>Go</button>"
R = "[REDACTED]"


@pytest.fixture(autouse=True)
def _fresh_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def lab():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/dl?"):
                body = b"col\n1\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", 'attachment; filename="r.csv"')
            elif self.path.startswith("/dlpage"):
                token = self.path.split("t=", 1)[1]
                body = (f'<!doctype html><title>dl</title><a href="/dl?token={token}" download>get file</a>').encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                body = PAGE
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
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


async def _nav(s, url, **kw):
    return await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": url, **kw})


async def _observe_wire(s, title=None):
    if title is not None:
        await s._page.evaluate("(t) => { document.title = t; }", title)
    raw = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
    assert raw.success, raw
    return envelope_json(raw)


@requires_chromium
async def test_b1_registered_value_not_in_address_fields(lab):
    port = lab
    origin = f"http://127.0.0.1:{port}"
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        assert (await _nav(s, f"{origin}/seen?token={V}")).success
        assert len(secret_registry.REGISTRY) == 1  # 조건 성립: 도구가 본 값
        for target in (f"{origin}/alias?ref={V}", f"{origin}/{V}", f"http://{V.lower()}.localhost:{port}/"):
            nav = await _nav(s, target)
            assert nav.success, (target, nav.error_message)
            assert V.lower() in s._page.url.lower()  # 조건 성립: 실제 주소에 값이 있다
            for wire in (envelope_json(nav), await _observe_wire(s)):
                assert leaks_in_wire(wire, V) == [], (target, wire[:600])
                assert V.lower() not in wire.lower(), (target, wire[:600])
                assert R in json.loads(wire)["current_url"]


@requires_chromium
async def test_b5_long_value_title(lab):
    origin = f"http://127.0.0.1:{lab}"
    value = "z" * 65536 + "SYNTHEND8"
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        nav = await _nav(s, f"{origin}/seen?token={value}")
        assert nav.success
        assert len(secret_registry.REGISTRY) == 1  # 64KiB 넘는 값도 등록(R3: 0)
        assert leaks_in_wire(envelope_json(nav), value) == []
        for title in (value, value[:40000] + "\u2026", value[:-1] + "X"):
            wire = await _observe_wire(s, title)
            assert leaks_in_wire(wire, value) == [], (len(title), wire[:300])


@requires_chromium
@pytest.mark.parametrize("shape", ["dense_a_8_15", "periodic256", "short256"])
async def test_b6_actual_observe_wire_one_mib(lab, shape):
    """실 observe + 최종 wire, 1MiB 제목, 5회 중앙값 < 1초(매회 캐시 비움). dense 는 실제 navigate 로 등록,
    256개 표본은 레지스트리에 직접 등록(256번 이동 대신 — 출구 경로는 실제)."""
    from security import public_url as _pu_mod  # noqa: F401

    pu = sys.modules["security.public_url"]
    origin = f"http://127.0.0.1:{lab}"
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        if shape == "dense_a_8_15":
            for n in range(8, 16):
                assert (await _nav(s, f"{origin}/seen?token={'a' * n}")).success
            title = "a" * (1 << 20)
            expect = 8
        elif shape == "periodic256":
            secret_registry.REGISTRY.add_values([("ab" * 200)[:n] for n in range(8, 264)])
            title = "ab" * (1 << 19)
            expect = 256
        else:
            values = ["k%03d" % i for i in range(256)]
            secret_registry.REGISTRY.add_values(values)
            title = " ".join(values[i % 256] for i in range(1 << 18))[: 1 << 20]
            expect = 256
        assert len(secret_registry.REGISTRY) == expect
        await s._page.evaluate("(t) => { document.title = t; }", title)
        runs = []
        for _ in range(5):
            pu._public_text_long.cache_clear()
            pu._public_url_field_long.cache_clear()
            t = time.perf_counter()
            raw = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
            wire = envelope_json(raw)
            runs.append(time.perf_counter() - t)
            assert raw.success
        print(json.dumps({"shape": shape, "runs": [round(x, 3) for x in runs]}))
        assert statistics.median(runs) < 1.0, runs
        assert R in json.loads(wire)["data"]["observation"]["title"]


@requires_chromium
@pytest.mark.parametrize("value", ["navigate", "observe_page", "tab-1", "E_INVALID_URL", "approved"])
async def test_b7_control_fields_after_protocol_like_value(lab, value):
    origin = f"http://127.0.0.1:{lab}"
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        steps = []
        nav = await _nav(s, f"{origin}/seen?token={value}")
        steps.append(nav)
        steps.append(await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {}))
        created = await s.call_tool(tool_name(ActionType.TAB_CONTROL), {"command": "create", "url": f"{origin}/x"})
        steps.append(created)
        listed = await s.call_tool(tool_name(ActionType.TAB_CONTROL), {"command": "list"})
        steps.append(listed)
        wire_list = json.loads(envelope_json(listed))
        first = wire_list["tab_id"] if created.tab_id != wire_list["tab_id"] else None
        ids = [t.get("tab_id") for t in (listed.data or {}).get("tabs", []) if isinstance(t, dict)]
        target = next(i for i in ids if i != created.tab_id) if first is None else first
        switched = await s.call_tool(tool_name(ActionType.TAB_CONTROL), {"command": "switch", "tab_id": target})
        steps.append(switched)
        assert switched.success, switched.error_message  # 탭 전환 동작
        for raw in steps:
            wire = json.loads(envelope_json(raw))
            assert wire["action"] == raw.action.value
            assert wire["tab_id"] == raw.tab_id
            if raw.error_code is not None:
                assert wire["error_code"] == raw.error_code.value
            for key in ("active_tab_id", "closed_tab_id", "popup_tab_id"):
                if key in (raw.data or {}):
                    assert wire["data"][key] == raw.data[key], (key, wire["data"][key])
            if "tabs" in (raw.data or {}):  # 탭 목록의 식별자(주소 칸은 token= 이라 가려지는 것이 맞다)
                assert [t["tab_id"] for t in wire["data"]["tabs"]] == [t["tab_id"] for t in raw.data["tabs"]]
        refused = await _nav(s, f"{origin}/x?token=[REDACTED]")
        assert json.loads(envelope_json(refused))["error_code"] == "E_INVALID_URL"


@requires_chromium
async def test_p7_download_target_registered(lab, tmp_path, monkeypatch):
    """다운로드 대상(`/dl?token=V2`)은 페이지 주소에 없다 — 컨텍스트 감시를 끄고 디스패처 등록만으로 가려진다."""
    monkeypatch.setattr(secret_registry, "watch_browser_context", lambda context: None)
    secret_v = "SYNTHdownloadV2x"
    origin = f"http://127.0.0.1:{lab}"
    async with BrowserMCPServer(headless=True, nav_settle=False, pre_approved_actions=("download_file:get file",)) as s:
        assert (await _nav(s, f"{origin}/dlpage?t={secret_v}")).success
        secret_registry.clear()  # 페이지 주소의 값(t=)은 비밀 키가 아니지만 혹시 모를 등록을 지운다
        obs = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        link = next(e for e in obs.data["observation"]["elements"] if "get file" in (e.get("name") or ""))
        dl = await s.call_tool(tool_name(ActionType.DOWNLOAD_FILE), {
            "trigger_element_id": link["element_id"], "epoch": obs.snapshot_epoch, "save_dir": str(tmp_path)})
        assert dl.success, dl.error_message
        assert (tmp_path / "r.csv").exists()
        wire = await _observe_wire(s, f"세션 {secret_v} 만료")
        assert leaks_in_text(json.loads(wire)["data"]["observation"]["title"], secret_v) == [], wire[:400]
