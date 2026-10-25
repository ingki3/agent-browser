"""WS-68 R1 (#140) — 검증 BLOCKING 3건 회귀 시험.

* B1: 엄격 판정(detect_challenge_strict)은 **격리 world** 에서 읽는다 — 페이지 JS 가 querySelectorAll·innerText·
  prototype 을 바꿔도 실제 캡차를 정상으로 보지 않는다. 격리 world 를 못 쓰면 판정 불가 = 저장 거부.
* B2: 저장은 문서 결속 트랜잭션 — 판정 전 주 프레임 결속값(loaderId·방문 기록 항목·주소)을 적고, 판정·상태·egress 뒤
  같은 결속값 + 두 번째 엄격 판정을 통과해야 저장. 저장 직후 다시 보고 바뀌었으면 방금 쓴 항목을 되돌린다.
* B3: 반복 묶음 사전 맞춤(fit_repeated)도 최종 자르기와 같은 표시용 limit 으로 잰다 — hint 길이까지 상한 안.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from contracts import ActionType
from interface.server_envelope import envelope_json
from perception.repeated import HOW

from test_ws61_r1 import URL, _route_server
from test_ws61_r2 import LAST_WINDOW, WINDOW_INFO
from test_ws68_trunc_numbers import _obs


# ---------------------------------------------------------------------------
# B3 — 반복 묶음 사전 맞춤의 표시용 limit
# ---------------------------------------------------------------------------


async def _boundary(limit: int, n_groups: int = 4, n_elements: int = 20, saved: bool = True) -> Any:
    from interface.mcp_server import tool_name

    s, _, res = _route_server()
    s.max_result_chars = limit
    s._window = NS(info=lambda: dict(WINDOW_INFO))
    s._window_unreported = True
    s._recovered_notice = False
    s._last_window_result = dict(LAST_WINDOW)
    s._on_demand_enter = AsyncMock(return_value=None)
    s._note_tabs = lambda: None
    block = {"how": HOW, "groups": [{"group": f"묶음{i}", "group_source": "text", "x": i * 100, "y": 0,
                                     "members": [{"id": f"@e{i * 20 + k}", "role": "link", "name": str(k + 1)}
                                                 for k in range(10)]} for i in range(n_groups)]}
    data: Dict[str, Any] = {"observation": _obs(n_elements), "repeated": block}
    if saved:
        data["saved_pages"] = {"how": "목록", "candidates": [{"id": "p_control", "name": "주문", "url": URL}]}
    s._call_tool = AsyncMock(return_value=res(ActionType.OBSERVE_PAGE, **data))
    return await s._route_tool_call(tool_name(ActionType.OBSERVE_PAGE), {})


@pytest.mark.asyncio
async def test_b3_verifier_boundary_1309_stays_within_configured_limit():
    """검증 재현(reproduce_boundary.py): 설정 1309 — 이전엔 1330(21자 초과)."""
    out = await _boundary(1309)
    final = len(envelope_json(out))
    assert final <= 1309, final
    assert out.data["truncated"]["returned_chars"] == final
    assert "saved_pages" not in out.data


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", range(1150, 1700, 7))
async def test_b3_final_size_never_exceeds_configured_limit_near_boundary(limit):
    """요소가 다 잘린(0개) 구간을 포함한 경계 주변 — 최종 봉투 ≤ 설정 상한(묶음을 더 빼서)."""
    out = await _boundary(limit)
    final = len(envelope_json(out))
    assert final <= limit, (limit, final)
    t = out.data.get("truncated")
    if t is not None:
        assert t["returned_chars"] == final


@pytest.mark.asyncio
async def test_b3_no_window_report_keeps_previous_bytes():
    """창 보고가 없으면 limit == 실효 예산 — hint 는 단일 숫자, 묶음 유지 판단도 이전과 같다."""
    from interface.mcp_server import tool_name

    s, _, res = _route_server()
    s.max_result_chars = 1309
    s._note_tabs = lambda: None
    block = {"how": HOW, "groups": [{"group": f"묶음{i}", "group_source": "text", "x": i * 100, "y": 0,
                                     "members": [{"id": f"@e{i * 20 + k}", "role": "link", "name": str(k + 1)}
                                                 for k in range(10)]} for i in range(4)]}
    s._call_tool = AsyncMock(return_value=res(ActionType.OBSERVE_PAGE, observation=_obs(20), repeated=block))
    out = await s._route_tool_call(tool_name(ActionType.OBSERVE_PAGE), {})
    assert len(envelope_json(out)) <= 1309
    assert "설정" not in out.data["truncated"]["hint"]
    assert json.loads(envelope_json(out))  # 직렬화 정상


# ---------------------------------------------------------------------------
# B1·B2 — 단위(가짜 디스패처): 격리 world 판독기·문서 결속
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from browser import challenge as ch  # noqa: E402
from recipes import saved_pages as sp  # noqa: E402
from recipes.saved_pages import SavedPagesService, SavedPageStore  # noqa: E402

PAGE_URL = "https://shop.test/orders"
CLEAN = {"title": "주문", "text": "주문 목록", "len": 5, "widget": "", "url": PAGE_URL}
CAPTCHA = dict(CLEAN, widget="cloudflare")


class _Doc:
    """가짜 최상위 문서 — 격리 world 평가값과 결속값을 순서대로 돌려준다(호출마다 다음 값, 마지막 값 유지)."""

    def __init__(self, probes: Any = (CLEAN,), bindings: Any = None, url: str = PAGE_URL) -> None:
        self.url = url
        self.probes = list(probes)
        self.bindings = list(bindings or [("F", "L1", 1, url)])
        self.evals = 0
        self.binds = 0
        self.main_world_reads = 0

    async def evaluate(self, _js: str) -> Any:  # 페이지 world — 엄격 판정이 쓰면 안 된다
        self.main_world_reads += 1
        return CLEAN

    async def isolated_top_evaluate(self, expression: str) -> Any:
        assert expression == ch.STRICT_PROBE_EXPRESSION
        v = self.probes[min(self.evals, len(self.probes) - 1)]
        self.evals += 1
        if isinstance(v, BaseException):
            raise v
        return v

    async def top_document_binding(self) -> Any:
        v = self.bindings[min(self.binds, len(self.bindings) - 1)]
        self.binds += 1
        if isinstance(v, BaseException):
            raise v
        return v


class _Server:
    def __init__(self, doc: _Doc, dispatcher_extra: bool = True) -> None:
        self.profile = None
        self.doc = doc
        self._recipes = NS(running=False)
        self._egress = None
        self._page_status = None
        self.extra = dispatcher_extra

    @property
    def _dispatcher(self) -> Any:
        ns = NS(ctx=NS(page=self.doc, root_page=None))
        if self.extra:
            ns.isolated_top_evaluate = self.doc.isolated_top_evaluate
            ns.top_document_binding = self.doc.top_document_binding
        return ns


def _svc(doc: _Doc, store: Any = None, extra: bool = True) -> SavedPagesService:
    svc = SavedPagesService(_Server(doc, extra))
    svc._store = store if store is not None else SavedPageStore()
    return svc


def _run(svc: SavedPagesService, name: str = "주문") -> Dict[str, Any]:
    return asyncio.run(svc.tool({"op": "remember_page", "name": name}))


def test_b1_strict_probe_reads_isolated_world_not_page_world():
    doc = _Doc(probes=(CAPTCHA,))  # 페이지 world 는 정상(위조), 격리 world 는 캡차
    svc = _svc(doc)
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_CHALLENGE
    assert len(svc.store) == 0 and doc.main_world_reads == 0


def test_b1_no_isolated_reader_is_unjudged():
    svc = _svc(_Doc(), extra=False)
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_UNJUDGED and len(svc.store) == 0


@pytest.mark.parametrize("probe", [RuntimeError("createIsolatedWorld"), None, "x", {"title": "t"}])
def test_b1_isolated_world_failure_is_unjudged(probe):
    svc = _svc(_Doc(probes=(probe,)))
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_UNJUDGED and len(svc.store) == 0


def test_b1_strict_without_isolated_reader_returns_none():
    page = _Doc()
    assert asyncio.run(ch.detect_challenge_strict(page)) is None
    assert page.main_world_reads == 0
    found = asyncio.run(ch.detect_challenge_strict(page, isolated=page.isolated_top_evaluate))
    assert found is not None and not found.detected


@pytest.mark.parametrize("bindings", [
    [RuntimeError("getFrameTree")],
    [None],
    [("F", "L1", 1, PAGE_URL), RuntimeError("gone")],
])
def test_b2_binding_unreadable_is_unjudged(bindings):
    svc = _svc(_Doc(bindings=bindings))
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_UNJUDGED and len(svc.store) == 0


@pytest.mark.parametrize("second", [
    ("F", "L2", 2, PAGE_URL),                        # 새 문서(loaderId)
    ("F", "L1", 2, "https://shop.test/masked-home"),  # pushState(새 항목)
    ("F", "L1", 1, "https://shop.test/replaced"),     # replaceState(같은 항목, 주소만)
    ("G", "L1", 1, PAGE_URL),                        # 다른 주 프레임
])
def test_b2_document_changed_between_probe_and_save_is_refused(second):
    svc = _svc(_Doc(bindings=[("F", "L1", 1, PAGE_URL), second]))
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_CHANGED and len(svc.store) == 0
    assert "shop.test" not in sp.E_CHANGED


def test_b2_binding_url_must_match_page_url():
    """Playwright 의 page.url 과 실제 문서(CDP 방문 기록) 주소가 다르면 저장할 주소를 믿지 않는다."""
    svc = _svc(_Doc(bindings=[("F", "L1", 1, "https://shop.test/elsewhere")]))
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_CHANGED and len(svc.store) == 0


def test_b2_second_strict_probe_catches_same_url_dom_swap():
    svc = _svc(_Doc(probes=(CLEAN, CAPTCHA)))
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_CHALLENGE and len(svc.store) == 0


def test_b2_change_right_after_save_rolls_back_new_entry(tmp_path):
    path = tmp_path / sp.FILE_NAME
    doc = _Doc(bindings=[("F", "L1", 1, PAGE_URL)] * 2 + [("F", "L9", 3, PAGE_URL)])
    svc = _svc(doc, SavedPageStore(path))
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_CHANGED
    assert len(svc.store) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["pages"] == []
    assert doc.binds == 3


def test_b2_swap_after_save_rolls_back_name_update(tmp_path):
    path = tmp_path / sp.FILE_NAME
    store = SavedPageStore(path)
    entry, _ = store.add("default", PAGE_URL, "원래 이름")
    store.note_use("default", entry["id"])
    doc = _Doc(probes=(CLEAN, CLEAN, CAPTCHA))
    svc = _svc(doc, store)
    out = _run(svc, "바뀐 이름")
    assert not out["success"] and out["error_message"] == sp.E_CHALLENGE
    kept = store.get("default", entry["id"])
    assert kept["name"] == "원래 이름" and kept["uses"] == 1
    disk = json.loads(path.read_text(encoding="utf-8"))["pages"]
    assert [p["name"] for p in disk] == ["원래 이름"]


def test_b2_stable_document_saves_with_three_bindings_and_three_probes():
    doc = _Doc()
    svc = _svc(doc)
    out = _run(svc)
    assert out["success"] and len(svc.store) == 1
    assert doc.binds == 3 and doc.evals == 3 and doc.main_world_reads == 0


# ---------------------------------------------------------------------------
# B1·B2 — E2E(실제 Chromium + 로컬 http, 외부 접속 없음): 검증 재현 편입
# ---------------------------------------------------------------------------

from test_ws68_remember_strict import _PAGES, _Site  # noqa: E402

_CAPTCHA_HTML = ("<title>Orders</title><p>verify you are human</p>"
                 "<div class='cf-turnstile' style='width:300px;height:65px'></div>")
_PAGES.setdefault("/r1-captcha", (200, _CAPTCHA_HTML))
_PAGES.setdefault("/r1-clean", (200, "<title>Orders</title><h1>Order list</h1><a href='/r1-other'>other</a>"))
_PAGES.setdefault("/r1-other", (200, "<title>Other</title><h1>Other page</h1>"))

_FORGE_WIDGET = """() => {
  const native = document.querySelectorAll.bind(document);
  document.querySelectorAll = s => s.includes('captcha') ? [] : native(s);
  Object.defineProperty(document.body, 'innerText', {get: () => 'Order list'});
}"""
_FORGE_LENGTH = """() => {
  const native = document.querySelectorAll.bind(document);
  document.querySelectorAll = s => s.includes('captcha') ? [] : native(s);
  Object.defineProperty(document.body, 'innerText', {get: () => 'verify you are human' + 'x'.repeat(4000)});
}"""
_FORGE_PROTO = """() => {
  Document.prototype.querySelectorAll = function () { return []; };
  Object.defineProperty(HTMLElement.prototype, 'innerText', {get() { return 'Order list'; }});
  Element.prototype.getBoundingClientRect = () => ({width: 0, height: 0});
  window.getComputedStyle = () => ({visibility: 'hidden', display: 'none', opacity: '0'});
}"""
_SWAP_GETTER = """({html, move}) => {
  const body = document.body;
  Object.defineProperty(body, 'innerText', {configurable: true, get() {
    delete body.innerText;
    queueMicrotask(() => { if (move) history.pushState({}, '', '/masked-home'); document.body.innerHTML = html; });
    return 'Order list';
  }});
}"""


async def _real(page: Any) -> Dict[str, Any]:
    """독립 CDP 격리 world 로 실제 화면을 읽는다(위조된 페이지 속성과 무관)."""
    cdp = await page.context.new_cdp_session(page)
    try:
        tree = await cdp.send("Page.getFrameTree")
        world = await cdp.send("Page.createIsolatedWorld",
                               {"frameId": tree["frameTree"]["frame"]["id"], "worldName": "ws68-r1-test"})
        res = await cdp.send("Runtime.evaluate", {
            "expression": f"({ch._PROBE_JS.strip()})()", "contextId": world["executionContextId"],
            "returnByValue": True})
        return res["result"]["value"]
    finally:
        await cdp.detach()


async def _remember_and_list(srv: Any, name: str = "검증") -> tuple:
    out = await srv.call_server_tool("browser_recipe", {"op": "remember_page", "name": name})
    pages = (await srv.call_server_tool("browser_recipe", {"op": "pages"}))["data"]["pages"]
    return out, pages


def _fresh(srv: Any) -> None:
    srv._saved_pages._store = SavedPageStore()


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", [_FORGE_WIDGET, _FORGE_LENGTH, _FORGE_PROTO],
                         ids=["forge_widget", "forge_length", "forge_proto"])
async def test_b1_e2e_page_js_forgery_cannot_save_real_captcha(forge):
    from interface.mcp_server import BrowserMCPServer

    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            page = srv._dispatcher.ctx.page
            await page.goto(site.url("/r1-captcha"))  # 외부(에이전트 밖) 이동 — navigate 신호 없음
            await page.evaluate(forge)
            # 표본 확인: 페이지 world 판정은 실제로 속는다(위조가 효과 있음), 실제 화면은 캡차
            assert not (await ch.detect_challenge(page)).detected
            real = await _real(page)
            assert ch.classify(real["title"], real["text"], real["len"], real["widget"]).detected
            out, pages = await _remember_and_list(srv)
            assert not out["success"] and out["error_message"] == sp.E_CHALLENGE, out
            assert pages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("move", [False, True], ids=["swap_same_url", "swap_new_url"])
async def test_b2_e2e_getter_swap_never_saves_a_captcha(move):
    """검증 재현 swap_*: 페이지 world 읽기에 걸린 교체 — 저장됐다면 실제 화면은 저장 주소의 정상 화면이어야 한다."""
    from interface.mcp_server import BrowserMCPServer

    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            page = srv._dispatcher.ctx.page
            await page.goto(site.url("/r1-clean"))
            await page.evaluate(_SWAP_GETTER, {"html": _CAPTCHA_HTML, "move": move})
            out, pages = await _remember_and_list(srv)
            await page.evaluate("() => new Promise(r => setTimeout(r, 50))")
            real = await _real(page)
            if out["success"]:
                assert pages and pages[0]["url"] == real["url"] == site.url("/r1-clean")
                assert not ch.classify(real["title"], real["text"], real["len"], real["widget"]).detected
            else:
                assert pages == []


def _inject_after(srv: Any, nth: int, script: str, method: str = "isolated_top_evaluate") -> None:
    """디스패처 판독기의 nth 번째 호출이 끝난 직후(다음 단계 전) 페이지 JS 로 화면을 바꾼다 — await 경계 주입."""
    disp = srv._dispatcher
    native = getattr(disp, method)
    calls = {"n": 0}

    async def wrapped(*a: Any, **k: Any) -> Any:
        out = await native(*a, **k)
        calls["n"] += 1
        if calls["n"] == nth:
            page = disp.ctx.page
            await page.evaluate(script)
            await page.wait_for_timeout(30)
        return out

    setattr(disp, method, wrapped)


_TO_CAPTCHA = "() => { document.body.innerHTML = " + json.dumps(_CAPTCHA_HTML) + "; }"
_PUSH_CAPTCHA = ("() => { history.pushState({}, '', '/boundary-swapped'); document.body.innerHTML = "
                 + json.dumps(_CAPTCHA_HTML) + "; }")
_PUSH_CLEAN = "() => { history.pushState({}, '', '/masked-home'); document.body.innerHTML = '<h1>Home</h1>'; }"
_NAV_AWAY = "() => { location.href = '/r1-other'; }"


@pytest.mark.asyncio
@pytest.mark.parametrize("nth,script,message", [
    (1, _TO_CAPTCHA, sp.E_CHALLENGE),    # 첫 판정 뒤 같은 주소 DOM 교체 — 두 번째 엄격 판정
    (1, _PUSH_CAPTCHA, sp.E_CHANGED),    # 검증 probe_paths: await 경계 pushState + 캡차
    (1, _PUSH_CLEAN, sp.E_CHANGED),      # 정상처럼 보이는 다른 주소 — 결속값만 잡는다
    (1, _NAV_AWAY, sp.E_CHANGED),        # 새 문서(loaderId)
], ids=["dom_swap", "push_captcha", "push_clean", "navigate_away"])
async def test_b2_e2e_swap_between_probe_and_save_is_refused(nth, script, message):
    from interface.mcp_server import BrowserMCPServer

    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            page = srv._dispatcher.ctx.page
            await page.goto(site.url("/r1-clean"))
            _inject_after(srv, nth, script)
            out, pages = await _remember_and_list(srv)
            assert not out["success"] and out["error_message"] == message, out
            assert pages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("script,message", [(_TO_CAPTCHA, sp.E_CHALLENGE), (_PUSH_CLEAN, sp.E_CHANGED)],
                         ids=["dom_swap", "push_clean"])
async def test_b2_e2e_swap_right_after_save_rolls_back(script, message, tmp_path):
    """저장 직후(파일 기록 뒤) 바뀌면 방금 쓴 항목을 되돌린다 — 파일에도 남지 않는다."""
    from interface.mcp_server import BrowserMCPServer

    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            page = srv._dispatcher.ctx.page
            await page.goto(site.url("/r1-clean"))
            path = tmp_path / sp.FILE_NAME
            store = SavedPageStore(path)
            srv._saved_pages._store = store
            native_add = store.add

            def add_then_swap(*a: Any, **k: Any) -> Any:
                out = native_add(*a, **k)
                assert len(json.loads(path.read_text(encoding="utf-8"))["pages"]) == 1  # 실제로 기록됨
                asyncio.get_running_loop().create_task(page.evaluate(script))
                return out

            store.add = add_then_swap  # type: ignore[method-assign]
            disp = srv._dispatcher
            native_bind = disp.top_document_binding
            calls = {"n": 0}

            async def bind_after_swap() -> Any:
                calls["n"] += 1
                if calls["n"] == 3:  # 저장 뒤 확인 — 교체가 끝난 뒤 읽게
                    await page.wait_for_timeout(50)
                return await native_bind()

            disp.top_document_binding = bind_after_swap
            out, pages = await _remember_and_list(srv)
            assert not out["success"] and out["error_message"] == message, out
            assert pages == [] and json.loads(path.read_text(encoding="utf-8"))["pages"] == []


@pytest.mark.asyncio
async def test_b2_e2e_no_over_refusal_normal_redirect_spa_hash_revisit():
    """과잉 거부 0: 정상·리다이렉트·pushState SPA·해시 주소·재방문 uses."""
    from interface.mcp_server import BrowserMCPServer, tool_name

    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            page = srv._dispatcher.ctx.page
            assert (await srv.call_tool(tool_name(ActionType.NAVIGATE), {"url": site.url("/r1-clean")})).success
            out, _ = await _remember_and_list(srv, "정상")
            assert out["success"], out
            await page.evaluate("() => { history.pushState({}, '', '/spa-orders'); "
                                "document.body.innerHTML = '<h1>Order list</h1>'; }")
            out, _ = await _remember_and_list(srv, "SPA")
            assert out["success"] and out["data"]["saved_page"]["url"].endswith("/spa-orders"), out
            await page.goto(site.url("/r1-other#section-2"))
            out, _ = await _remember_and_list(srv, "해시")
            assert out["success"] and out["data"]["saved_page"]["url"].endswith("#section-2"), out
            # 같은 문서에서 다시 기억(이름 갱신)도 그대로
            out, pages = await _remember_and_list(srv, "해시 다시")
            assert out["success"] and out["data"]["saved_page"]["updated"], out
            assert len(pages) == 3
            pid = [p for p in pages if p["name"] == "정상"][0]["id"]
            assert (await srv.call_tool(tool_name(ActionType.NAVIGATE), {"url": site.url("/r1-clean")})).success
            await srv.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
            pages = (await srv.call_server_tool("browser_recipe", {"op": "pages"}))["data"]["pages"]
            assert [p for p in pages if p["id"] == pid][0]["uses"] == 1


@pytest.mark.asyncio
async def test_b2_e2e_no_over_refusal_manual_sites_and_redirect():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from interface.mcp_server import BrowserMCPServer, tool_name
    from security.manual_sites import ManualSites

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/orders")
                self.end_headers()
                return
            data = b"<title>Orders</title><h1>Order list</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:  # noqa: ANN002
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        async with BrowserMCPServer(headless=True, manual_sites=ManualSites.of([origin])) as srv:
            for path in ("/orders", "/redirect"):
                _fresh(srv)
                await srv.call_tool(tool_name(ActionType.NAVIGATE), {"url": origin + path})
                out, pages = await _remember_and_list(srv)
                assert out["success"] and pages[0]["url"] == origin + "/orders", (path, out)
    finally:
        httpd.shutdown()
        httpd.server_close()
