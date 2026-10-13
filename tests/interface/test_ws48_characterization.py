"""WS-48 특성화(골든) 테스트 — 리팩터링 전후 MCP 응답 봉투가 한 바이트도 바뀌지 않음을 고정한다.

로컬 루프백 Mock 사이트(공개 사이트 없음)로 BrowserMCPServer 를 띄워 대표 도구 호출 시나리오의
응답 봉투(envelope_dict)를 정규화(포트·시각·id·임시 경로 마스킹)한 뒤 골든 파일과 비교한다.
`list_tools`·tools/list 스키마 전체도 골든이다.

골든 갱신(의도된 동작 변경 때만): WS48_UPDATE_GOLDEN=1 pytest tests/interface/test_ws48_characterization.py
리팩터링 라운드에서는 갱신하지 않는다 — 골든이 바뀌면 동작이 바뀐 것이다.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from interface import handoff
from interface.handoff import write_command
from interface.mcp_server import (
    BrowserMCPServer,
    build_all_tools,
    build_listed_tools,
    build_server_tools,
    cap_result_size,
    envelope_dict,
)
from test_run_cli import requires_chromium
from ws29_helpers import code_from_banner

GOLDEN = Path(__file__).parent / "golden" / "ws48_characterization.json"
UPDATE = os.environ.get("WS48_UPDATE_GOLDEN") == "1"

HOME = """<!doctype html><meta charset=utf-8><title>WS48 홈</title>
<h1>특성화 홈</h1>
<form id=f action=/next method=get><input aria-label='검색어' name=q id=q>
<button type=submit>검색</button></form>
<button id=pay onclick="document.getElementById('out').textContent='결제됨'">결제하기</button>
<p id=out>대기</p>
<a href='/next'>다음 페이지</a>
<a id=dl href='/file.csv' download>CSV 받기</a>
<button class=dup>더보기</button><button class=dup>더보기</button>
<iframe id=fr src='/frame' width=300 height=80></iframe>
"""
NEXT = "<!doctype html><meta charset=utf-8><title>다음</title><p class=row>둘째 페이지</p><a href='/'>홈</a>"
FRAME = "<!doctype html><meta charset=utf-8><title>프레임</title><button>프레임 버튼</button>"
BIG = ("<!doctype html><meta charset=utf-8><title>큰 페이지</title>"
       + "".join(f"<button>항목 버튼 {i:04d} 길게 늘인 이름으로 결과 크기를 키운다</button>" for i in range(300)))
SPARSE = "<!doctype html><meta charset=utf-8><title>빈칸</title><div class=e> </div><div class=e></div>"
OK_PAGE = "<!doctype html><meta charset=utf-8><title>Read</title><a href='/next'>Next</a>"
CLOUDFLARE = b"<!doctype html><title>Just a moment...</title>Checking your browser before accessing"


class _Site:
    def __init__(self, robots: bytes) -> None:
        self.robots = robots
        self.url = ""


@contextmanager
def _local_site(robots: bytes = b"User-agent: *\nDisallow: /private\n"):
    state = _Site(robots)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            ua = self.headers.get("User-Agent", "")
            headless = "HeadlessChrome" in ua
            path = self.path.split("?", 1)[0]
            status, ctype, body, extra = 200, "text/html; charset=utf-8", b"", {}
            if path == "/robots.txt":
                ctype, body = "text/plain", state.robots
            elif path == "/":
                body = HOME.encode()
            elif path in ("/next", "/private"):
                body = NEXT.encode()
            elif path == "/frame":
                body = FRAME.encode()
            elif path == "/big":
                body = BIG.encode()
            elif path == "/sparse":
                body = SPARSE.encode()
            elif path == "/file.csv":
                ctype, body = "text/csv", b"a,b\n1,2\n"
                extra["Content-Disposition"] = "attachment; filename=file.csv"
            elif path in ("/read", "/read2", "/blocked"):
                # 헤드리스 이름만 막는 사이트(WS-44 재시도 대상): 짧은 403 본문.
                if headless:
                    status, body = 403, b"Forbidden"
                else:
                    body = OK_PAGE.encode()
            elif path == "/cf":
                status, body = 403, CLOUDFLARE
            elif path == "/slow429":
                status, body = 429, b"Too many"
                extra["Retry-After"] = "120"
            else:
                status, body = 404, b"not found"
            self.send_response_only(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield state
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------- 정규화

_MASK_KEYS = {"approval_id", "expires_at", "latency_ms", "elapsed_ms", "duration_ms", "server_id",
              "created_at", "updated_at", "at", "ts", "last_used", "id", "since",
              # 브라우저 결과의 글자 수는 latency_ms 자릿수에 따라 ±1 흔들린다 — 정확한 값은 정적 cap 골든이 고정.
              "returned_chars", "total_chars"}
_APPROVAL_RE = re.compile(r"\bap[-_][0-9A-Za-z_-]{6,}\b")
_TAB_RE = re.compile(r"\btab-[0-9a-f]{6,}\b")


class _Norm:
    def __init__(self) -> None:
        self.subs: List[tuple] = []
        self.tabs: Dict[str, str] = {}

    def add(self, real: str, alias: str) -> None:
        if real:
            self.subs.append((real, alias))

    def _tab(self, m: "re.Match[str]") -> str:
        return self.tabs.setdefault(m.group(0), f"TAB{len(self.tabs)}")

    def text(self, s: str) -> str:
        for real, alias in sorted(self.subs, key=lambda x: -len(x[0])):
            s = s.replace(real, alias)
        s = _APPROVAL_RE.sub("APPROVAL", s)
        return _TAB_RE.sub(self._tab, s)

    def value(self, v: Any, key: str = "") -> Any:
        if key in _MASK_KEYS and v not in (None, "", 0, False):
            return "<masked>"
        if key in ("tab_id", "popup_tab_id", "active_tab_id") and isinstance(v, str) and v:
            return self.tabs.setdefault(v, f"TAB{len(self.tabs)}")
        if isinstance(v, dict):
            return {k: self.value(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x, key) for x in v]
        if isinstance(v, str):
            return self.text(v)
        return v


def _env(norm: _Norm, result: Any) -> Any:
    raw = envelope_dict(result) if not isinstance(result, dict) else result
    return norm.value(json.loads(json.dumps(raw, ensure_ascii=False)))


def _check(section: str, actual: Any) -> None:
    golden: Dict[str, Any] = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if UPDATE:
        golden[section] = actual
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        return
    assert section in golden, f"골든 없음: {section} (WS48_UPDATE_GOLDEN=1 로 생성)"
    expected = golden[section]
    if actual != expected:
        a = json.dumps(actual, ensure_ascii=False, indent=1, sort_keys=True).splitlines()
        e = json.dumps(expected, ensure_ascii=False, indent=1, sort_keys=True).splitlines()
        import difflib

        diff = "\n".join(list(difflib.unified_diff(e, a, "golden", "actual", lineterm=""))[:80])
        pytest.fail(f"{section}: 응답 봉투가 골든과 다름\n{diff}")


def _eid(obs_result: Any, name: str) -> str:
    for el in obs_result.data["observation"]["elements"]:
        if el["name"] == name:
            return el["element_id"]
    raise AssertionError(f"요소 없음: {name}")


def _epoch(obs_result: Any) -> int:
    return obs_result.data["observation"]["snapshot_epoch"]


@pytest.fixture
def sites():
    with ExitStack() as stack:
        yield (stack.enter_context(_local_site()),
               stack.enter_context(_local_site()),
               stack.enter_context(_local_site(b"User-agent: *\nDisallow: /blocked\n")))


def _norm_for(sites, tmp_path: Path) -> _Norm:
    n = _Norm()
    for alias, site in zip(("SITE_A", "SITE_B", "SITE_C"), sites):
        n.add(site.url, f"http://{alias}")
        n.add(site.url.split("://", 1)[1], alias)
    n.add(str(tmp_path), "TMP")
    return n


# --------------------------------------------------------------------------- 정적 골든


def test_tool_schemas_golden():
    tools = {
        "build_all_tools": build_all_tools(),
        "build_server_tools": build_server_tools(True),
        "build_server_tools_no_recipes": build_server_tools(False),
        "build_listed_tools": build_listed_tools(True),
        "list_tools": BrowserMCPServer(recipes=False).list_tools(),
    }
    _check("tool_schemas", json.loads(json.dumps(tools, ensure_ascii=False)))


def test_cap_result_size_golden():
    from contracts import ActionResult, ActionType

    out = {}
    for limit in (2000, 5000):
        r = ActionResult(success=True, action=ActionType.EXTRACT, current_url="http://x/", snapshot_epoch=1,
                         tab_id="t", healed=False, reobserve_required=False, retry_safe=True, data={"items": [{"text": "가" * 300 + str(i)} for i in range(40)]})
        cap_result_size(r, limit)
        out[str(limit)] = envelope_dict(r)
    _check("cap_result_size", out)


async def test_error_results_golden():
    norm = _Norm()
    async with BrowserMCPServer(recipes=False) as server:
        out = {
            "unknown_tool": _env(norm, await server.call_tool("browser_nope", {})),
            "bad_navigate": _env(norm, await server.call_tool("browser_navigate", {})),
            "bad_click": _env(norm, await server.call_tool("browser_click", {"epoch": "x"})),
            "frame_to_main_conflict": _env(norm, await server.call_tool(
                "browser_switch_frame", {"to_main": True, "frame_selector": "#fr"})),
            "unknown_server_tool": _env(norm, await server.call_server_tool("browser_nope", {})),
            "recipe_disabled": _env(norm, await server.call_server_tool("browser_recipe", {"op": "list"})),
        }
    _check("error_results", out)


# --------------------------------------------------------------------------- 브라우저 시나리오


@requires_chromium
async def test_core_actions_golden(sites, tmp_path):
    a, _, _ = sites
    norm = _norm_for(sites, tmp_path)
    out: Dict[str, Any] = {}
    async with BrowserMCPServer(nav_settle=False, recipes=False,
                                pre_approved_actions=("download_file:*",)) as s:
        out["navigate"] = _env(norm, await s.call_tool("browser_navigate", {"url": a.url + "/"}))
        obs = await s.call_tool("browser_observe_page", {})
        out["observe"] = _env(norm, obs)
        ep = _epoch(obs)
        out["extract_h1"] = _env(norm, await s.call_tool("browser_extract", {"selector": "h1"}))
        out["type"] = _env(norm, await s.call_tool(
            "browser_type_text", {"element_id": _eid(obs, "검색어"), "text": "사과", "epoch": ep}))
        out["hover"] = _env(norm, await s.call_tool(
            "browser_hover", {"element_id": _eid(obs, "다음 페이지"), "epoch": ep}))
        out["press_tab"] = _env(norm, await s.call_tool("browser_press_key", {"key": "Tab"}))
        out["scroll"] = _env(norm, await s.call_tool("browser_scroll", {"direction": "down"}))
        out["ambiguous_selector"] = _env(norm, await s.call_tool("browser_click", {"selector": "button.dup"}))
        out["download"] = _env(norm, await s.call_tool("browser_download_file", {
            "trigger_element_id": _eid(obs, "CSV 받기"), "save_dir": str(tmp_path), "epoch": ep}))
        obs = await s.call_tool("browser_observe_page", {})
        out["click_link"] = _env(norm, await s.call_tool(
            "browser_click", {"element_id": _eid(obs, "다음 페이지"), "epoch": _epoch(obs)}))
        out["go_back"] = _env(norm, await s.call_tool("browser_go_back", {}))
        out["reload"] = _env(norm, await s.call_tool("browser_reload", {}))
        out["wait_selector"] = _env(norm, await s.call_tool(
            "browser_wait_for", {"condition": "selector", "selector": "h1", "timeout_ms": 2000}))
        out["frame_in"] = _env(norm, await s.call_tool("browser_switch_frame", {"frame_selector": "#fr"}))
        out["frame_observe"] = _env(norm, await s.call_tool("browser_observe_page", {}))
        out["frame_out"] = _env(norm, await s.call_tool("browser_switch_frame", {"to_main": True}))
        out["tab_create"] = _env(norm, await s.call_tool(
            "browser_tab_control", {"command": "create", "url": a.url + "/next"}))
        out["tab_list"] = _env(norm, await s.call_tool("browser_tab_control", {"command": "list"}))
        out["screenshot_keys"] = sorted((await s.call_tool("browser_take_screenshot", {})).data)
    _check("core_actions", out)


@requires_chromium
async def test_observation_guidance_and_cap_golden(sites, tmp_path):
    a, _, _ = sites
    norm = _norm_for(sites, tmp_path)
    out: Dict[str, Any] = {}
    async with BrowserMCPServer(nav_settle=False, recipes=False, max_result_chars=2000) as s:
        await s.call_tool("browser_navigate", {"url": a.url + "/sparse"})
        out["sparse_extract_hint"] = _env(norm, await s.call_tool(
            "browser_extract", {"selector": ".e", "extract_all": True}))
        out["missing_selector_hint"] = _env(norm, await s.call_tool("browser_click", {"selector": "#nope"}))
        await s.call_tool("browser_navigate", {"url": a.url + "/big"})
        out["big_observe_capped"] = _env(norm, await s.call_tool("browser_observe_page", {}))
        out["big_extract_capped"] = _env(norm, await s.call_tool(
            "browser_extract", {"selector": "button", "extract_all": True}))
    _check("observation_guidance_cap", out)


@requires_chromium
async def test_hitl_unattended_golden(sites, tmp_path):
    a, _, _ = sites
    norm = _norm_for(sites, tmp_path)
    out: Dict[str, Any] = {}
    async with BrowserMCPServer(nav_settle=False, recipes=False) as s:
        await s.call_tool("browser_navigate", {"url": a.url + "/"})
        obs = await s.call_tool("browser_observe_page", {})
        ep = _epoch(obs)
        out["click_pay_blocked"] = _env(norm, await s.call_tool(
            "browser_click", {"element_id": _eid(obs, "결제하기"), "epoch": ep}))
        out["type_enter_blocked"] = _env(norm, await s.call_tool(
            "browser_type_text", {"element_id": _eid(obs, "검색어"), "text": "x", "press_enter": True,
                                  "epoch": ep}))
        await s.call_tool("browser_type_text", {"element_id": _eid(obs, "검색어"), "text": "x", "epoch": ep})
        out["press_enter_blocked"] = _env(norm, await s.call_tool("browser_press_key", {"key": "Enter"}))
        out["coordinate_click"] = _env(norm, await s.call_tool("browser_click", {"x": 5, "y": 5, "epoch": ep}))
        out["selector_pay_blocked"] = _env(norm, await s.call_tool("browser_click", {"selector": "#pay"}))
        out["control_status"] = _env(norm, await s.call_server_tool("browser_control_status", {}))
        out["control_request_headless"] = _env(norm, await s.call_server_tool(
            "browser_control_request", {"reason": "캡차"}))
        out["approval_wait_unknown"] = _env(norm, await s.call_server_tool(
            "browser_approval_wait", {"approval_id": "nope", "timeout_s": 0}))
        out["page_out"] = await s._page.text_content("#out")
    _check("hitl_unattended", out)


def _visible(s: BrowserMCPServer) -> List[Any]:
    """headless 로 돌리되 사람이 볼 창이 있다고 두고, 창 오버레이 문구를 가로챈다(ws29 R1 방식)."""
    s._human_can_see = lambda: True
    seen: List[Any] = []
    real = s._set_banner

    async def spy(text):
        seen.append(text)
        return await real(text)

    s._set_banner = spy
    return seen


@requires_chromium
async def test_hitl_approval_flow_golden(sites, tmp_path, monkeypatch):
    a, _, _ = sites
    monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
    norm = _norm_for(sites, tmp_path)
    out: Dict[str, Any] = {}
    async with BrowserMCPServer(nav_settle=False, recipes=False,
                                handoff_root=Path(handoff.state_root())) as s:
        banners = _visible(s)
        await s.call_tool("browser_navigate", {"url": a.url + "/"})
        obs = await s.call_tool("browser_observe_page", {})
        args = {"element_id": _eid(obs, "결제하기"), "epoch": _epoch(obs)}
        first = await s.call_tool("browser_click", args)
        out["approval_required"] = _env(norm, first)
        aid = first.data["approval"]["approval_id"]
        out["approval_bogus_id"] = _env(norm, await s.call_tool("browser_click", dict(args, approval_id="ap-bogus000")))
        out["approval_unapproved"] = _env(norm, await s.call_tool("browser_click", dict(args, approval_id=aid)))
        # 사람: 코드 표시 요청 → 창에서 코드 읽기 → 승인.
        aid2 = (await s.call_tool("browser_click", args)).data["approval"]["approval_id"]
        root, sid = s.hub.root, s.hub.server_id
        shown = handoff.read_pending_approval(root, sid, aid2)
        write_command(root, sid, "show_code", approval_id=aid2, action_digest=shown["action_digest"])
        await s._poll_handoff()
        code = code_from_banner(banners)
        out["screenshot_while_code"] = _env(norm, await s.call_tool("browser_take_screenshot", {}))
        write_command(root, sid, "approve", approval_id=aid2, action_digest=shown["action_digest"], code=code)
        await s._poll_handoff()
        out["approval_wait_approved"] = _env(norm, await s.call_server_tool(
            "browser_approval_wait", {"approval_id": aid2, "timeout_s": 0}))
        out["approval_used"] = _env(norm, await s.call_tool("browser_click", dict(args, approval_id=aid2)))
        out["approval_reuse"] = _env(norm, await s.call_tool("browser_click", dict(args, approval_id=aid2)))
        out["page_out"] = await s._page.text_content("#out")
    _check("hitl_approval_flow", out)


@requires_chromium
async def test_navigation_signals_golden(sites, tmp_path):
    a, b, c = sites
    norm = _norm_for(sites, tmp_path)
    closed = _closed_port()
    norm.add(f"127.0.0.1:{closed}", "127.0.0.1:CLOSED")
    out: Dict[str, Any] = {}
    async with BrowserMCPServer(nav_settle=False, recipes=False) as s:
        out["robots_disallowed"] = _env(norm, await s.call_tool("browser_navigate", {"url": a.url + "/private"}))
        out["robots_observe"] = _env(norm, await s.call_tool("browser_observe_page", {}))
        out["retry_success"] = _env(norm, await s.call_tool("browser_navigate", {"url": a.url + "/read"}))
        out["remembered"] = _env(norm, await s.call_tool("browser_navigate", {"url": a.url + "/read2"}))
        out["cloudflare_skip"] = _env(norm, await s.call_tool("browser_navigate", {"url": b.url + "/cf"}))
        out["rate_limited"] = _env(norm, await s.call_tool("browser_navigate", {"url": b.url + "/slow429"}))
        out["robots_skip"] = _env(norm, await s.call_tool("browser_navigate", {"url": c.url + "/blocked"}))
        out["egress_private"] = _env(norm, await s.call_tool("browser_navigate", {"url": "http://10.255.255.1/"}))
        out["upstream_fail"] = _env(norm, await s.call_tool(
            "browser_navigate", {"url": f"http://127.0.0.1:{closed}/"}))
    _check("navigation_signals", out)


@requires_chromium
async def test_retry_disabled_and_recipe_golden(sites, tmp_path):
    a, _, _ = sites
    norm = _norm_for(sites, tmp_path)
    out: Dict[str, Any] = {}
    async with BrowserMCPServer(nav_settle=False, headless_retry=False) as s:
        out["retry_disabled"] = _env(norm, await s.call_tool("browser_navigate", {"url": a.url + "/read"}))
        await s.call_tool("browser_navigate", {"url": a.url + "/"})
        obs = await s.call_tool("browser_observe_page", {})
        await s.call_tool("browser_type_text", {"element_id": _eid(obs, "검색어"), "text": "사과",
                                                "epoch": _epoch(obs)})
        await s.call_tool("browser_click", {"element_id": _eid(obs, "다음 페이지"), "epoch": _epoch(obs)})
        listed = await s.call_server_tool("browser_recipe", {"op": "list"})
        out["recipe_list"] = _env(norm, listed)
        out["recipe_bad_op"] = _env(norm, await s.call_server_tool("browser_recipe", {"op": "zzz"}))
        out["recipe_run_missing"] = _env(norm, await s.call_server_tool(
            "browser_recipe", {"op": "run", "id": "missing"}))
    _check("retry_disabled_recipe", out)
