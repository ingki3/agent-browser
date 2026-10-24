"""WS-63 C: 보안 업체 차단·확인 화면 → 창 자동 재시도 없이 '사람 인계 안내'(data.handoff)만.

정책(사용자 결정 B·C): 풀지 않고 우회하지 않는다 — 창도 자동으로 열지 않는다. 사람이 직접 푸는 인계
(browser_control_request → 사람이 창에서 확인 통과 → browser_control_wait → 같은 탭 다시 관찰)를
**안내만** 한다. 로컬 HTTP 서버만 쓴다(공개 사이트 접속 없음).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from contracts import ActionResult, ActionType
from interface.mcp_server import BrowserMCPServer, envelope_json, tool_name
from interface.server_signals import (HANDOFF_HINT, HANDOFF_STEPS_AVAILABLE, HANDOFF_STEPS_UNAVAILABLE,
                                      challenge_handoff)

from test_run_cli import requires_chromium  # noqa: F401

CF = (b"<!doctype html><meta charset=utf-8><title>Just a moment...</title>"
      b"<p>Checking your browser before accessing the site.</p>")
CAPTCHA = (b"<!doctype html><meta charset=utf-8><title>check</title><p>robot?</p>"
           b"<iframe src='/recaptcha/api2/anchor?k=x' width=304 height=78></iframe>")
WIDGET = b"<!doctype html><meta charset=utf-8><p>widget</p>"
SHORT = b"<!doctype html><meta charset=utf-8><title>no</title><p>Forbidden</p>"
OK = (b"<!doctype html><meta charset=utf-8><title>ok</title><button>Read</button>"
      b"<p>" + "가격 표 본문 ".encode() * 120 + b"</p>")

#: 경로 → (상태, 추가 헤더, 본문)
PAGES = {
    "/cf": (403, {"Server": "cloudflare", "CF-RAY": "8a1b2c3d4e5f-ICN"}, CF),
    "/captcha": (200, {}, CAPTCHA),
    "/recaptcha/api2/anchor": (200, {}, WIDGET),
    "/deny": (403, {}, SHORT),
    "/login": (401, {"WWW-Authenticate": "Basic realm=x"}, SHORT),
    "/slow": (429, {"Retry-After": "30"}, SHORT),
    "/private": (200, {}, OK),
    "/ok": (200, {}, OK),
    "/private-cf": (403, {"Server": "cloudflare", "CF-RAY": "8a1b2c3d4e5f-ICN"}, CF),
    "/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private\n"),
}


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status, headers, body = PAGES.get(self.path.split("?", 1)[0], (404, {}, SHORT))
            self.send_response(status)
            ctype = "text/plain" if self.path.startswith("/robots") else "text/html; charset=utf-8"
            self.send_header("Content-Type", ctype)
            for key, value in headers.items():
                self.send_header(key, value)
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


def _no_window_calls(server: BrowserMCPServer) -> list:
    """창 관리자(전환·조작권용 창 열기) 호출을 센다 — 0 이어야 한다."""
    calls: list = []

    async def switch(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(("switch", args))
        return {"ok": False, "error": "test: 창 전환 금지"}

    async def open_for_request(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(("open", args))
        return {"ok": False, "error": "test: 창 열기 금지"}

    server._switch_window = switch  # type: ignore[method-assign]
    server._open_window_for_request = open_for_request  # type: ignore[method-assign]
    return calls


async def _nav(server: BrowserMCPServer, url: str) -> ActionResult:
    return await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": url})


def _assert_handoff(result: ActionResult, available: bool) -> None:
    h = result.data["handoff"]
    assert h["suggested"] is True
    assert h["tool"] == "browser_control_request"
    assert h["available"] is available
    assert h["reason"] == result.data["challenge"]["kind"]
    assert h["steps"] == list(HANDOFF_STEPS_AVAILABLE if available else HANDOFF_STEPS_UNAVAILABLE)
    hint = str(result.data.get("hint") or "")
    assert hint.count(HANDOFF_HINT) == 1, hint
    assert len(envelope_json(result)) <= 20_000
    assert "injection_suspected" not in result.data


def _assert_no_handoff(result: ActionResult) -> None:
    assert "handoff" not in result.data, result.data
    assert HANDOFF_HINT not in str(result.data.get("hint") or "")


# ------------------------------------------------------------------ 순수 판정


def test_pure_handoff_branches_and_exclusions():
    cf = {"kind": "captcha", "vendor": "cloudflare", "reason": "Cloudflare 브라우저 확인 화면"}
    on = challenge_handoff(cf, 403, available=True)
    off = challenge_handoff(cf, 403, available=False)
    assert on["available"] is True and on["steps"] == list(HANDOFF_STEPS_AVAILABLE)
    assert off["available"] is False and off["steps"] == list(HANDOFF_STEPS_UNAVAILABLE)
    assert on["steps"] != off["steps"]
    assert 2 <= len(on["steps"]) <= 3 and 2 <= len(off["steps"]) <= 3
    blocked = {"kind": "blocked", "vendor": "cloudflare", "reason": "Cloudflare 차단 화면"}
    assert challenge_handoff(blocked, 403, available=False)["reason"] == "blocked"
    # 차단 판정 없음·상태 코드만으로 잡은 일반 거부·401·429 → 안내 없음(각자 기존 안내).
    assert challenge_handoff(None, 403, available=True) is None
    generic = {"kind": "blocked", "vendor": "generic", "reason": "HTTP 403 + 짧은 본문(9자)"}
    assert challenge_handoff(generic, 403, available=True) is None
    for status in (401, 429):
        assert challenge_handoff(cf, status, available=True) is None
    # 고정 문장만 — 페이지 글자(이유 문장)는 싣지 않는다.
    assert cf["reason"] not in json.dumps(on, ensure_ascii=False)


def test_navigate_description_points_to_handoff():
    """차단 안내 전문(navigate 설명 한 곳)이 data.handoff 를 가리킨다 — 인계 도구 설명은 토큰 예산 때문에 그대로."""
    from interface.server_schema import CHALLENGE_NOTE, build_tool_schema

    assert "보안 확인·차단 화면이면 data.handoff 참고" in CHALLENGE_NOTE
    assert CHALLENGE_NOTE in build_tool_schema(ActionType.NAVIGATE)["description"]


# ------------------------------------------------------------------ 실제 브라우저(headless serve)


@requires_chromium
async def test_headless_serve_cloudflare_and_captcha_get_unavailable_handoff(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        calls = _no_window_calls(server)
        cf = await _nav(server, site + "/cf")
        obs = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        cap = await _nav(server, site + "/captcha")
        assert server._human_can_see() is False
    assert cf.data["challenge"]["vendor"] == "cloudflare"
    for r in (cf, obs, cap):
        _assert_handoff(r, available=False)
    assert cap.data["challenge"]["kind"] == "captcha"
    assert calls == []


@requires_chromium
async def test_other_responses_get_no_handoff(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        results = {p: await _nav(server, site + p) for p in ("/deny", "/login", "/slow", "/private", "/ok")}
        obs = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
    assert results["/deny"].data["last_http_status"] == 403
    # 조건 성립: 일반 거부·429 도 기존 challenge 는 붙는다 — 그래도 인계 안내는 없어야 한다.
    assert results["/deny"].data["challenge"]["vendor"] == "generic"
    assert results["/slow"].data["challenge"] is not None
    assert results["/login"].data["last_http_status"] == 401
    assert results["/slow"].data["last_http_status"] == 429
    assert results["/private"].data["robots"]["disallowed"] is True
    assert results["/ok"].data["challenge"] is None
    for r in (*results.values(), obs):
        _assert_no_handoff(r)


@requires_chromium
async def test_on_demand_serve_available_handoff_without_opening_window(site):
    async with BrowserMCPServer(browser_mode="on-demand", nav_settle=False, recipes=False) as server:
        calls = _no_window_calls(server)
        cf = await _nav(server, site + "/cf")
        again = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        state = server._window.state
    for r in (cf, again):
        _assert_handoff(r, available=True)
    assert state == "headless"
    assert calls == []


@requires_chromium
async def test_handoff_attached_once_per_response(site):
    """같은 응답에 판정이 두 번 지나가도(재시도 경로 등) 안내·hint 문장은 한 번만, 기존 값 유지."""
    async with BrowserMCPServer(browser_mode="on-demand", nav_settle=False, recipes=False) as server:
        _no_window_calls(server)
        cf = await _nav(server, site + "/cf")
        before = dict(cf.data["handoff"])
        cf.data["handoff"]["steps"] = ["기존"]
        await server._attach_challenge(cf)
        await server._attach_challenge(cf)
    assert cf.data["handoff"]["steps"] == ["기존"]
    assert before["steps"] == list(HANDOFF_STEPS_AVAILABLE)
    assert str(cf.data["hint"]).count(HANDOFF_HINT) == 1


# ------------------------------------------------------------------ R1: SPA 옛 상태가 현재 확인 화면 안내를 막지 않음


@requires_chromium
@pytest.mark.parametrize("path,status", [("/login", 401), ("/slow", 429)])
async def test_spa_current_captcha_after_old_401_429_gets_handoff(site, path, status):
    """401/429 문서 → pushState 다른 URL → 현재 CF 확인 화면: 판정용 상태(null)로 안내를 정한다(R1)."""
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        first = await _nav(server, site + path)
        assert first.data["last_http_status"] == status
        _assert_no_handoff(first)
        await server._page.evaluate(
            "history.pushState({}, '', '/spa-verify'); document.title = 'check';"
            "document.body.innerHTML = '<p>Verify you are human</p>'")
        obs = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        judge = server._page_status.judge_status_for(server._page)
    assert judge is None  # 조건 성립: 지금 URL 은 그 응답 URL 이 아니다
    assert obs.data["last_http_status"] == status  # 역사값은 그대로 보고
    assert obs.data["challenge"]["kind"] == "captcha"
    _assert_handoff(obs, available=False)


# ------------------------------------------------------------------ R2: robots 금지가 인계 안내보다 우선


@requires_chromium
async def test_robots_disallowed_challenge_gets_no_handoff_allowed_path_does(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        _no_window_calls(server)
        banned = await _nav(server, site + "/private-cf")
        banned_obs = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        allowed = await _nav(server, site + "/cf")
    for r in (banned, banned_obs):
        assert r.data["robots"]["disallowed"] is True
        assert r.data["challenge"]["kind"] == "captcha"  # 조건 성립: 확인 화면 판정은 그대로
        _assert_no_handoff(r)
    assert not (allowed.data.get("robots") or {}).get("disallowed")
    _assert_handoff(allowed, available=False)


def _robots_banned() -> dict:
    return {"disallowed": True, "ai_agents_disallowed": [], "scope": "all_bots"}


def _challenge_result() -> ActionResult:
    return ActionResult(success=True, action=ActionType.NAVIGATE, current_url="https://x.test/p",
                        snapshot_epoch=1, tab_id="tab-1", retry_safe=True,
                        data={"challenge": {"kind": "captcha", "vendor": "cloudflare", "reason": "r"}})


def test_settle_handoff_robots_wins_regardless_of_attach_order():
    """최종 응답 기준: 안내가 먼저 붙었든(기존 hint 유지) robots 가 먼저 붙었든 robots 금지면 안내 없음."""
    from interface.server_signals import settle_handoff

    handoff = challenge_handoff(_challenge_result().data["challenge"], 403, available=True)
    late = _challenge_result()
    late.data.update(handoff=handoff, hint="기존 안내 " + HANDOFF_HINT, robots=_robots_banned())
    settle_handoff(late.data)
    assert "handoff" not in late.data and late.data["hint"] == "기존 안내"
    only = _challenge_result()
    only.data.update(handoff=handoff, hint=HANDOFF_HINT, robots=_robots_banned())
    settle_handoff(only.data)
    assert "handoff" not in only.data and "hint" not in only.data
    kept = _challenge_result()
    kept.data.update(handoff=handoff, hint=HANDOFF_HINT, robots={"disallowed": False})
    settle_handoff(kept.data)
    assert kept.data["handoff"] == handoff and kept.data["hint"] == HANDOFF_HINT


@requires_chromium
async def test_attach_challenge_after_robots_skips_handoff(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await _nav(server, site + "/private-cf")
        r = _challenge_result()
        r.data = {"robots": _robots_banned()}
        await server._attach_challenge(r)
    assert r.data["challenge"]["kind"] == "captcha"
    _assert_no_handoff(r)


# ------------------------------------------------------------------ R3: 레시피 중단 응답이 안내를 보존


async def test_recipe_host_observe_projects_handoff_and_hint():
    from recipes.service import ServerHost

    handoff = {"suggested": True, "reason": "captcha", "tool": "browser_control_request", "available": False,
               "steps": ["a", "b"]}

    class FakeServer:
        _dispatcher = None

        async def call_tool(self, name, arguments):  # noqa: ANN001
            return ActionResult(success=True, action=ActionType.OBSERVE_PAGE, current_url="https://x.test/",
                                snapshot_epoch=1, tab_id="tab-1", retry_safe=True,
                                data={"challenge": {"kind": "captcha"}, "handoff": handoff,
                                      "hint": HANDOFF_HINT})

    out = await ServerHost(FakeServer()).observe()
    assert out["challenge"] == {"kind": "captcha"}
    assert out["handoff"] == handoff and out["hint"] == HANDOFF_HINT


@requires_chromium
async def test_actual_recipe_run_stop_preserves_handoff_and_hint():
    """실제 save → 같은 목록이 CF 확인 화면 → run: not_ready 중단 응답에 challenge·handoff·hint 모두(R3)."""
    from harness.recipe_mock import ROWS, RecipeSite

    with RecipeSite() as rsite:
        async with BrowserMCPServer(nav_settle=False) as server:
            assert (await _nav(server, rsite.url("/list"))).success
            obs = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
            el = next(e for e in obs.data["observation"]["elements"] if e["name"] == ROWS[0][1])
            click = await server.call_tool(tool_name(ActionType.CLICK),
                                           {"element_id": el["element_id"], "epoch": obs.snapshot_epoch})
            assert click.success, click.error_message
            saved = await server.call_server_tool("browser_recipe", {"op": "save", "name": "local open", "last_n": 1})
            assert saved["success"], saved
            rid = saved["data"]["recipe"]["id"]
            rsite.list_html = lambda: CF.decode()
            cf = await _nav(server, rsite.url("/list"))
            assert cf.data["handoff"]["reason"] == "captcha"
            out = await server.call_server_tool("browser_recipe", {"op": "run", "id": rid})
    data = out["data"]
    assert out["success"] is False and data["recipe"]["ran"] == 0
    assert "step_result" not in data  # pre-dispatch 중단 경로
    assert data["challenge"]["kind"] == "captcha"
    assert data["handoff"]["suggested"] is True and data["handoff"]["reason"] == "captcha"
    assert str(data["hint"]).count(HANDOFF_HINT) == 1
