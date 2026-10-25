"""WS-68 (#140-2): remember_page 는 지금 화면을 엄격히 다시 판정해 확인·차단·오류 화면을 저장하지 않는다.

* 엄격 판정(detect_challenge_strict): detect_challenge 와 같은 프로브·규칙, 평가 예외·모양 틀림 = 판정 불가 → 거부.
* judge_status_for ≥ 400 → 거부. None(URL 바뀜·기록 없음)이면 엄격 판정만으로(통과 시에만 저장).
* egress 불허(판정 예외 포함) → 거부. 마지막 navigate 결과는 보조 — 같은 주소에서 실패였으면 거부(OR).
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace as NS
from typing import Any, Dict, Optional

import pytest

from browser import challenge as ch
from contracts import ActionResult, ActionType
from recipes import saved_pages as sp
from recipes.saved_pages import SavedPagesService, SavedPageStore

URL = "https://shop.test/orders"
NORMAL = {"title": "주문", "text": "주문 목록", "len": 5, "widget": "", "url": URL}


class FakePage:
    def __init__(self, url: str = URL, probe: Any = None, boom: bool = False) -> None:
        self.url = url
        self.probe = dict(NORMAL, url=url) if probe is None else probe
        self.boom = boom

    async def evaluate(self, _js: str) -> Any:
        if self.boom:
            raise RuntimeError("Execution context was destroyed")
        return self.probe


class FakeGuard:
    def __init__(self, allowed: bool = True, boom: bool = False) -> None:
        self.allowed, self.boom = allowed, boom

    def evaluate(self, url: str) -> Any:
        if self.boom:
            raise RuntimeError("guard")
        return NS(allowed=self.allowed)


class FakeStatus:
    def __init__(self, status: Optional[int]) -> None:
        self.status = status

    def judge_status_for(self, page: Any) -> Optional[int]:
        return self.status


class FakeServer:
    def __init__(self, page: Any) -> None:
        self.profile = None
        self.page = page
        self._recipes = NS(running=False)
        self._egress = FakeGuard()
        self._page_status = FakeStatus(200)

    @property
    def _dispatcher(self) -> Any:
        return NS(ctx=NS(page=self.page, root_page=None))


def _svc(page: Any = None) -> tuple:
    srv = FakeServer(page or FakePage())
    svc = SavedPagesService(srv)
    svc._store = SavedPageStore()
    return srv, svc


def _remember(svc: SavedPagesService) -> Dict[str, Any]:
    return asyncio.run(svc.tool({"op": "remember_page", "name": "주문"}))


def _refused(out: Dict[str, Any], message: str, svc: SavedPagesService) -> None:
    assert not out["success"], out
    assert out["error_code"] == "E_FEATURE_NOT_IMPLEMENTED" and out["error_message"] == message
    assert len(svc.store) == 0


def test_normal_page_is_saved():
    _, svc = _svc()
    out = _remember(svc)
    assert out["success"] and out["data"]["saved_page"]["url"] == URL and len(svc.store) == 1


@pytest.mark.parametrize("probe", [
    dict(NORMAL, widget="cloudflare"),
    dict(NORMAL, widget="recaptcha"),
    dict(NORMAL, title="Just a moment...", text="Checking your browser before accessing", len=40),
    dict(NORMAL, text="Sorry, you have been blocked. Cloudflare Ray ID", len=50),
    dict(NORMAL, text="보안 확인을 완료해 주세요", len=20),
])
def test_challenge_screen_is_refused(probe):
    found = ch.classify(probe["title"], probe["text"], probe["len"], probe["widget"])
    assert found.detected  # 표본이 실제로 같은 탐지 규칙에 걸리는 화면인지 먼저 확인
    _, svc = _svc(FakePage(probe=probe))
    _refused(_remember(svc), sp.E_CHALLENGE, svc)


@pytest.mark.parametrize("page", [
    FakePage(boom=True),            # 평가 예외(전환 중·컨텍스트 파괴)
    FakePage(probe="x"),             # dict 아님
    FakePage(probe={"title": "t"}),  # 필드 누락
    FakePage(probe=dict(NORMAL, len="5")),
    FakePage(probe=dict(NORMAL, len=True)),
])
def test_unjudgeable_screen_is_refused(page):
    _, svc = _svc(page)
    _refused(_remember(svc), sp.E_UNJUDGED, svc)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418, 429, 500, 502, 503])
def test_error_status_is_refused(status):
    """긴 본문(차단 문구 규칙 밖) 오류 페이지 — 상태 규칙으로 거부."""
    srv, svc = _svc(FakePage(probe=dict(NORMAL, text="안내 " * 2000, len=6000)))
    srv._page_status = FakeStatus(status)
    _refused(_remember(svc), sp.E_STATUS, svc)


def test_short_body_with_block_status_is_challenge_even_before_status_rule():
    """403 + 짧은 본문은 엄격 판정(classify)에도 걸린다 — 둘 중 어느 쪽으로든 거부."""
    srv, svc = _svc(FakePage(probe=dict(NORMAL, text="denied", len=6)))
    srv._page_status = FakeStatus(403)
    out = _remember(svc)
    assert not out["success"] and len(svc.store) == 0


def test_status_none_saves_only_when_strict_probe_passes():
    srv, svc = _svc()
    srv._page_status = FakeStatus(None)  # URL 바뀜·기록 없음
    assert _remember(svc)["success"]
    srv2, svc2 = _svc(FakePage(probe=dict(NORMAL, widget="hcaptcha")))
    srv2._page_status = FakeStatus(None)
    _refused(_remember(svc2), sp.E_CHALLENGE, svc2)
    srv3, svc3 = _svc(FakePage(boom=True))
    srv3._page_status = FakeStatus(None)
    _refused(_remember(svc3), sp.E_UNJUDGED, svc3)


def test_status_lookup_failure_is_refused():
    srv, svc = _svc()

    class Boom:
        def judge_status_for(self, page: Any) -> Optional[int]:
            raise RuntimeError("status")

    srv._page_status = Boom()
    _refused(_remember(svc), sp.E_UNJUDGED, svc)


@pytest.mark.parametrize("guard", [FakeGuard(allowed=False), FakeGuard(boom=True)])
def test_egress_disallowed_is_refused(guard):
    srv, svc = _svc()
    srv._egress = guard
    _refused(_remember(svc), sp.E_EGRESS, svc)


def _nav(url: str, success: bool = True, **data: Any) -> ActionResult:
    return ActionResult(success=success, action=ActionType.NAVIGATE, current_url=url, snapshot_epoch=1,
                        tab_id="tab-1", retry_safe=True, data=dict(data))


def test_last_navigate_failure_on_same_url_is_refused():
    """보조: 마지막 navigate 가 같은 주소에서 challenge 로 끝났으면 지금 프로브가 정상이어도 거부(OR)."""
    _, svc = _svc()
    svc.note_navigate({"url": URL}, _nav(URL, last_http_status=200,
                                         challenge={"kind": "captcha", "vendor": "generic", "reason": "x"}))
    _refused(_remember(svc), sp.E_LAST_NAV, svc)


def test_last_navigate_status_alone_is_left_to_judge_status():
    """보조 판정은 상태를 보지 않는다 — 403 뒤 pushState 로 바뀐 화면은 judge_status_for(None)+엄격 판정이 맡는다."""
    _, svc = _svc()
    svc.note_navigate({"url": URL}, _nav(URL, last_http_status=403))
    assert _remember(svc)["success"]


def test_last_navigate_failure_elsewhere_does_not_block():
    """클릭·JS 이동으로 다른 주소에 와 있으면 낡은 navigate 결과는 쓰지 않는다(지금 페이지 판정이 주)."""
    _, svc = _svc()
    svc.note_navigate({"url": "https://shop.test/login"},
                      _nav("https://shop.test/login", last_http_status=429))
    assert _remember(svc)["success"]


def test_url_checks_still_come_first():
    """주소 검사(기존 고정 문구)는 그대로 — 엄격 판정보다 먼저."""
    _, svc = _svc(FakePage(url="https://shop.test/?q=010+1234+5678", boom=True))
    _refused(_remember(svc), sp.E_PII, svc)


def test_revisit_flow_after_strict_save_still_counts_uses():
    srv, svc = _svc()
    pid = _remember(svc)["data"]["saved_page"]["id"]
    svc.note_navigate({"url": URL}, _nav(URL, last_http_status=200))
    svc.observe(ActionResult(success=True, action=ActionType.OBSERVE_PAGE, current_url=URL, snapshot_epoch=2,
                             tab_id="tab-1", retry_safe=True, data={}))
    assert svc.store.get("default", pid)["uses"] == 1


def test_strict_probe_vs_lenient_probe_on_exception():
    """같은 규칙 — 다른 점은 판정 불가뿐(detect_challenge 는 NONE, 엄격판은 None)."""
    page = FakePage(boom=True)
    assert asyncio.run(ch.detect_challenge(page)) is ch.NO_CHALLENGE
    assert asyncio.run(ch.detect_challenge_strict(page)) is None
    ok = FakePage(probe=dict(NORMAL, widget="cloudflare"))
    assert asyncio.run(ch.detect_challenge_strict(ok)) == asyncio.run(ch.detect_challenge(ok))


# ---------------------------------------------------------------------------
# E2E — 실제 Chromium + 로컬 http (외부 접속 없음)
# ---------------------------------------------------------------------------

_PAGES = {
    "/orders": (200, "<title>주문</title><h1>주문 목록</h1><a href='/p/1'>상품</a>"),
    "/home": (200, "<title>홈</title><h1>홈</h1>"),
    "/turnstile": (200, "<title>확인</title><p>계속하려면 확인하세요</p>"
                        "<div class='cf-turnstile' style='width:300px;height:65px'></div>"),
    "/cf-block": (403, "<title>Attention Required! | Cloudflare</title>"
                       "<h1>Sorry, you have been blocked</h1><p>Cloudflare Ray ID: 1</p>"),
    "/forbidden": (403, "<title>금지</title>" + "<p>긴 안내 문단입니다.</p>" * 400),
    "/limited": (429, "<title>잠시</title>" + "<p>요청이 많습니다. 잠시 뒤 다시 시도하세요.</p>" * 300),
    "/oops": (500, "<title>오류</title><h1>서버 오류</h1>" + "<p>죄송합니다.</p>" * 400),
    "/spa403": (403, "<title>앱</title>" + "<p>본문</p>" * 900
                     + "<script>history.pushState({}, '', '/spa-next')</script>"),
}


class _Site:
    def __enter__(self) -> "_Site":
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, body = _PAGES.get(self.path.split("?", 1)[0], (404, "<h1>없음</h1>"))
                data = f"<html><head><meta charset='utf-8'></head><body>{body}</body></html>".encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:  # noqa: ANN002
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"


@pytest.fixture
def site():
    with _Site() as s:
        yield s


async def _nav_to(srv: Any, url: str) -> ActionResult:
    from interface.mcp_server import tool_name

    return await srv.call_tool(tool_name(ActionType.NAVIGATE), {"url": url})


async def _recipe(srv: Any, **args: Any) -> Dict[str, Any]:
    return await srv.call_server_tool("browser_recipe", args)


@pytest.mark.asyncio
async def test_e2e_confirmation_and_error_screens_are_not_saved(site):
    from interface.mcp_server import BrowserMCPServer

    async with BrowserMCPServer(headless=True) as srv:
        for path, message in (("/turnstile", sp.E_CHALLENGE), ("/cf-block", None), ("/forbidden", sp.E_STATUS),
                              ("/limited", sp.E_STATUS), ("/oops", sp.E_STATUS)):
            await _nav_to(srv, site.url(path))
            out = await _recipe(srv, op="remember_page", name="x")
            assert not out["success"], (path, out)
            if message is not None:
                assert out["error_message"] == message, (path, out)
        assert (await _recipe(srv, op="pages"))["data"]["pages"] == []
        # 과잉 거부 0: 정상 페이지는 저장, 재방문 흐름(후보·uses) 유지
        assert (await _nav_to(srv, site.url("/orders"))).success
        saved = await _recipe(srv, op="remember_page", name="주문")
        assert saved["success"], saved
        assert (await _nav_to(srv, site.url("/home"))).success
        from interface.mcp_server import tool_name

        obs = await srv.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        assert obs.data["saved_pages"]["candidates"][0]["id"] == saved["data"]["saved_page"]["id"]
        assert (await _nav_to(srv, site.url("/orders"))).success
        await srv.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        assert (await _recipe(srv, op="pages"))["data"]["pages"][0]["uses"] == 1


@pytest.mark.asyncio
async def test_e2e_url_changed_after_403_saves_only_on_strict_pass(site):
    """403 문서 뒤 pushState 로 URL 이 바뀌면 judge_status_for 는 None — 엄격 판정(긴 본문, 위젯 없음)을 통과해 저장."""
    from interface.mcp_server import BrowserMCPServer

    async with BrowserMCPServer(headless=True) as srv:
        await _nav_to(srv, site.url("/spa403"))
        page = srv._dispatcher.ctx.page
        assert page.url.endswith("/spa-next")
        assert srv._page_status.judge_status_for(page) is None
        out = await _recipe(srv, op="remember_page", name="앱")
        assert out["success"], out

        async def boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("context destroyed")

        page.evaluate = boom  # 탐지 중 예외 주입 → 판정 불가 → 거부
        out = await _recipe(srv, op="remember_page", name="앱 다시")
        assert not out["success"] and out["error_message"] == sp.E_UNJUDGED


@pytest.mark.asyncio
async def test_e2e_egress_disallowed_is_not_saved(site, monkeypatch):
    from interface.mcp_server import BrowserMCPServer

    async with BrowserMCPServer(headless=True) as srv:
        assert (await _nav_to(srv, site.url("/orders"))).success
        monkeypatch.setattr(srv._egress, "evaluate", lambda url: NS(allowed=False))
        out = await _recipe(srv, op="remember_page", name="주문")
        assert not out["success"] and out["error_message"] == sp.E_EGRESS
        assert (await _recipe(srv, op="pages"))["data"]["pages"] == []
