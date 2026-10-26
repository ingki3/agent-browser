"""WS-67 (#141): 페이지가 기본 기능(form getter·matches·closest)을 지워도 위험 제출 판정이 비지 않는다.

ws56-r5 NB-1 재현: 입력칸의 `HTMLInputElement.prototype.form` 을 지우면 페이지 world 판정이
`in_form=false` 가 되어 press_key Enter(암묵적 폼 제출)가 승인 없이 통과했다. 판정을 CDP 격리
world(Page.createIsolatedWorld)에서도 하고, 두 판정을 각각 위험 분류해 높은 쪽을 쓴다.

범위(R3): press_key 포커스 경로와 좌표 클릭 경로. 격리 판정이 실패하거나 생략되면(OOPIF·IFRAME
위 좌표) 기존 판정 그대로 — 과잉 거부 0. 실제 BrowserMCPServer(헤드리스 Chromium) + 로컬 HTTP 만.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401

PAGES: Dict[str, str] = {}


def _serve(host: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            body = PAGES.get(path, "<!doctype html><meta charset=utf-8><title>x</title><p>x</p>")
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer((host, 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def site():
    srv = _serve("127.0.0.1")
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def other_origin():
    """같은 기계의 다른 출처(다른 사이트라 Chromium 은 다른 프로세스 iframe=OOPIF 로 띄운다)."""
    srv = _serve("127.0.0.1")
    try:
        yield f"http://localhost:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(server: BrowserMCPServer, action: ActionType, args: Dict[str, Any]):
    return await server.call_tool(tool_name(action), args)


def _page(server: BrowserMCPServer):
    return server._dispatcher.ctx.page  # noqa: SLF001


def _dispatcher(server: BrowserMCPServer):
    return server._dispatcher  # noqa: SLF001


_HEAD = "<!doctype html><meta charset=utf-8><title>g</title><body style=margin:0><p id=out>대기</p>"
#: 제출이 일어나면 표시만 하고 이동하지 않는다 — '제출 0' 을 #out 으로 잰다.
_ON_SUBMIT = "onsubmit=\"document.getElementById('out').textContent='제출됨';return false\""

#: 페이지가 기본 기능을 지우거나 바꾸는 방식(NB-1 + 리뷰 N5).
TAMPER = {
    "delete_proto_form": "delete HTMLInputElement.prototype.form; delete HTMLButtonElement.prototype.form;",
    "null_proto_form": (
        "Object.defineProperty(HTMLInputElement.prototype,'form',{get(){return null},configurable:true});"
        "Object.defineProperty(HTMLButtonElement.prototype,'form',{get(){return null},configurable:true});"
    ),
    "null_instance_form": (
        "document.querySelectorAll('input,button').forEach(function(e){"
        "Object.defineProperty(e,'form',{get(){return null}});});"
    ),
}
_KILL_TREE = "delete Element.prototype.matches; delete Element.prototype.closest;"


def _script(js: str) -> str:
    return f"<script>{js}</script>"


async def _status(server: BrowserMCPServer) -> str:
    return str(await _page(server).text_content("#out"))


# ------------------------------------------------------------------ NB-1 press_key Enter


def _search_form(tamper: str) -> str:
    return (_HEAD + f"<form action=\"/search\" {_ON_SUBMIT}>"
            "<input id=q name=q aria-label=\"검색어\"></form>" + _script(tamper))


def _image_form(tamper: str) -> str:
    return (_HEAD + f"<form action=\"/s\" {_ON_SUBMIT}><input id=q name=q aria-label=\"검색어\">"
            "<input id=img type=image alt=\"검색\" style=\"width:60px;height:30px\"></form>"
            + _script(tamper))


@requires_chromium
@pytest.mark.parametrize("tamper", sorted(TAMPER))
@pytest.mark.parametrize("form", ["search", "image"])
async def test_nb1_enter_in_tampered_form_requires_approval(site, form, tamper):
    PAGES["/f"] = (_search_form if form == "search" else _image_form)(TAMPER[tamper])
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#q")
        # 전제(규칙 4): 페이지 world 판정은 실제로 속는다 — 이 시험이 공백을 재현한다.
        existing = await _dispatcher(server).focused_target()
        assert existing["in_form"] is False and existing["tag"] == "INPUT"
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _page(server).wait_for_timeout(150)
        status = await _status(server)
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.success, r.error_message)
    assert r.data["gate_basis"]["source"] == "form_submit"
    assert status == "대기"


@requires_chromium
async def test_nb1_enter_on_focused_image_button_requires_approval(site):
    """폼 안 image 버튼에 포커스 + Enter = 폼 제출. 페이지 world 는 in_form=false 라 이름 '검색' 판정만 했다."""
    PAGES["/f"] = _image_form(TAMPER["delete_proto_form"])
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#img")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _page(server).wait_for_timeout(150)
        status = await _status(server)
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.success, r.error_message)
    assert status == "대기"


@requires_chromium
async def test_isolated_focus_reads_true_form_owner(site):
    PAGES["/f"] = _search_form(TAMPER["null_proto_form"] + _KILL_TREE)
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#q")
        info = await _dispatcher(server).isolated_focus_target()
    assert info is not None
    assert info["tag"] == "INPUT" and info["in_form"] is True and info["inside_form"] is True


# ------------------------------------------------------------------ 같은 출처 iframe · OOPIF (포커스)


def _frame_top(src: str) -> str:
    return (_HEAD + f"<iframe id=fr src=\"{src}\" style=\"position:absolute;left:0;top:100px;"
            "width:400px;height:200px;border:0\"></iframe>")


def _frame_inner(tamper: str) -> str:
    return ("<!doctype html><meta charset=utf-8><body style=margin:0><p id=out>대기</p>"
            f"<form action=\"/fdone\" {_ON_SUBMIT}><input id=fq name=fq aria-label=\"프레임 검색\">"
            "<button id=fb style=\"width:120px;height:40px\">다음</button></form>"
            "<input id=loose aria-label=\"프레임 메모\">"
            "<button id=plain style=\"width:120px;height:40px\" "
            "onclick=\"document.getElementById('out').textContent='눌림'\">보기</button>"
            + _script(tamper))


@requires_chromium
async def test_same_origin_iframe_enter_is_hardened(site):
    PAGES["/inner"] = _frame_inner(TAMPER["delete_proto_form"])
    PAGES["/top"] = _frame_top("/inner")
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        frame = _page(server).frame_locator("#fr")
        await frame.locator("#fq").focus()
        existing = await _dispatcher(server).focused_target()
        assert existing["in_form"] is False  # 전제: 페이지 world 는 속는다
        iso = await _dispatcher(server).isolated_focus_target()
        # 같은 출처 iframe: 격리 world 의 원본 contentDocument.activeElement 로 내려간다(실측).
        assert iso is not None and iso["in_form"] is True and iso["name"] == "프레임 검색"
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
        await _page(server).wait_for_timeout(150)
        status = await frame.locator("#out").text_content()
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.success, r.error_message)
    assert status == "대기"


@requires_chromium
async def test_same_origin_iframe_enter_outside_form_not_overblocked(site):
    PAGES["/inner"] = _frame_inner(TAMPER["delete_proto_form"] + _KILL_TREE)
    PAGES["/top"] = _frame_top("/inner")
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        await _page(server).frame_locator("#fr").locator("#loose").focus()
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert r.success, (r.error_code, r.error_message)


@requires_chromium
async def test_oopif_focus_keeps_existing_behaviour(site, other_origin):
    """다른 출처(OOPIF) 안 포커스: 격리 판정은 생략(None) — 기존 판정(프레임 안 읽기) 그대로."""
    PAGES["/inner"] = _frame_inner("")
    PAGES["/top"] = _frame_top(other_origin + "/inner")
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        await _page(server).frame_locator("#fr").locator("#loose").focus()
        iso = await _dispatcher(server).isolated_focus_target()
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert iso is None
    assert r.success, (r.error_code, r.error_message)


# ------------------------------------------------------------------ 과잉 거부 0 (press_key)


@requires_chromium
async def test_enter_outside_form_with_tampering_passes(site):
    PAGES["/f"] = (_HEAD + "<input id=q aria-label=\"메모\">"
                   + _script(TAMPER["delete_proto_form"] + _KILL_TREE))
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#q")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert r.success, (r.error_code, r.error_message)


@requires_chromium
async def test_enter_on_plain_link_passes(site):
    PAGES["/f"] = _HEAD + "<a id=l href=\"#here\">보기</a>"
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#l")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert r.success, (r.error_code, r.error_message)


@requires_chromium
async def test_normal_get_search_enter_verdict_unchanged(site):
    """정상 GET 검색 폼 Enter 는 원래도 '폼 제출 액션'(WS-30) — 격리 판정이 사유를 바꾸지 않는다."""
    PAGES["/f"] = _search_form("")
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/f"})
        await _page(server).focus("#q")
        r = await _call(server, ActionType.PRESS_KEY, {"key": "Enter"})
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert r.data["gate_basis"]["source"] == "form_submit"


# ------------------------------------------------------------------ 좌표 클릭


async def _coord_click(server: BrowserMCPServer, url: str, selector: str, frame: str = ""):
    await _call(server, ActionType.NAVIGATE, {"url": url})
    obs = await _call(server, ActionType.OBSERVE_PAGE, {})
    epoch = obs.data["observation"]["snapshot_epoch"]
    loc = (_page(server).frame_locator(frame).locator(selector) if frame
           else _page(server).locator(selector))
    box = await loc.bounding_box()
    x, y = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
    r = await _call(server, ActionType.CLICK, {"x": x, "y": y, "epoch": epoch})
    return r, x, y


def _checkout_form(tamper: str) -> str:
    return (_HEAD + f"<form action=\"/order/checkout\" method=post {_ON_SUBMIT}>"
            "<input id=sb type=submit value=\"다음\" style=\"width:120px;height:40px\">"
            "<input id=ib type=image alt=\"다음\" style=\"width:120px;height:40px\">"
            "<button id=bb style=\"width:120px;height:40px\">다음</button></form>" + _script(tamper))


@requires_chromium
@pytest.mark.parametrize("target", ["#sb", "#ib", "#bb"])
async def test_coordinates_tampered_form_button_requires_approval(site, target):
    """matches·closest·form getter 를 모두 지우면 페이지 world 는 '폼 밖 비상호작용'으로 통과시켰다."""
    PAGES["/c"] = _checkout_form(TAMPER["delete_proto_form"] + _KILL_TREE)
    async with BrowserMCPServer() as server:
        r, x, y = await _coord_click(server, site + "/c", target)
        existing = await _dispatcher(server).describe_point(x, y)
        iso = await _dispatcher(server).isolated_point_target(x, y)
        status = await _status(server)
    # 전제: 페이지 world 판정은 속는다(상호작용 아님·폼 밖).
    assert existing["info"]["interactive"] is False and existing["info"]["inside_form"] is False
    # 격리 판정: image·submit·button 각각 in_form=true, 목적지 신호.
    assert iso is not None and iso["info"]["in_form"] is True and iso["info"]["interactive"] is True
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.success, r.error_message)
    assert r.data["gate_basis"]["source"] == "form_action"
    assert status == "대기"


@requires_chromium
@pytest.mark.parametrize("target", ["#sb", "#ib"])
async def test_coordinates_matches_closest_deleted_requires_approval(site, target):
    PAGES["/c"] = _checkout_form(_KILL_TREE)
    async with BrowserMCPServer() as server:
        r, _x, _y = await _coord_click(server, site + "/c", target)
        status = await _status(server)
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (r.success, r.error_message)
    assert status == "대기"


@requires_chromium
async def test_coordinates_get_search_button_not_overblocked(site):
    PAGES["/c"] = (_HEAD + f"<form action=\"/search\" method=get {_ON_SUBMIT}><input name=q>"
                   "<button id=go style=\"width:120px;height:40px\">검색</button></form>")
    async with BrowserMCPServer() as server:
        r, _x, _y = await _coord_click(server, site + "/c", "#go")
    assert r.success, (r.error_code, r.error_message)


@requires_chromium
@pytest.mark.parametrize("tamper", ["", TAMPER["delete_proto_form"] + _KILL_TREE])
async def test_coordinates_plain_link_and_button_not_overblocked(site, tamper):
    PAGES["/c"] = (_HEAD + "<a id=l href=\"#here\" style=\"display:inline-block;width:120px;height:40px\">보기</a>"
                   "<button id=b style=\"width:120px;height:40px\" "
                   "onclick=\"document.getElementById('out').textContent='눌림'\">열기</button>"
                   + _script(tamper))
    async with BrowserMCPServer() as server:
        r1, _x, _y = await _coord_click(server, site + "/c", "#l")
        r2, _x, _y = await _coord_click(server, site + "/c", "#b")
    assert r1.success, (r1.error_code, r1.error_message)
    assert r2.success, (r2.error_code, r2.error_message)


@requires_chromium
async def test_coordinates_same_origin_iframe_hardened_and_plain_passes(site):
    PAGES["/inner"] = _frame_inner(TAMPER["delete_proto_form"] + _KILL_TREE).replace(
        "/fdone", "/order/checkout")
    PAGES["/top"] = _frame_top("/inner")
    async with BrowserMCPServer() as server:
        risky, _x, _y = await _coord_click(server, site + "/top", "#fb", frame="#fr")
        plain, _x, _y = await _coord_click(server, site + "/top", "#plain", frame="#fr")
        pressed = await _page(server).frame_locator("#fr").locator("#out").text_content()
    assert risky.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (risky.success, risky.error_message)
    assert plain.success, (plain.error_code, plain.error_message)
    assert pressed == "눌림"


@requires_chromium
async def test_coordinates_on_cross_origin_frame_keep_existing_verdict(site, other_origin):
    """좌표가 다른 출처 iframe 위: 기존 판정(판정 불가·차단) 그대로 — 격리 판정은 낮출 수 없다."""
    PAGES["/inner"] = _frame_inner("")
    PAGES["/top"] = _frame_top(other_origin + "/inner")
    async with BrowserMCPServer() as server:
        r, _x, _y = await _coord_click(server, site + "/top", "#plain", frame="#fr")
    assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
    assert r.data["gate_basis"]["source"] == "unresolved"


@requires_chromium
async def test_coordinates_hitting_iframe_element_skip_isolated(site):
    """맞힌 노드가 IFRAME 자신(OOPIF 소유자와 같은 모양 — 여기선 테두리)이면 iframe 을 판정하지 않는다.

    폼 안 iframe 을 '폼 안 비상호작용'으로 읽으면 과잉 거부가 생긴다(리뷰 R2 B2) — 격리 판정 생략(None).
    """
    PAGES["/inner"] = _frame_inner("")
    PAGES["/top"] = (_HEAD + "<form action=\"/order/checkout\"><iframe id=fr src=\"/inner\" "
                     "style=\"position:absolute;left:0;top:100px;width:300px;height:150px;"
                     "border:30px solid #ccc\"></iframe></form>")
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/top"})
        await _page(server).frame_locator("#fr").locator("#plain").wait_for()
        iso = await _dispatcher(server).isolated_point_target(10, 110)  # 위 테두리
    assert iso is None


@requires_chromium
async def test_isolated_point_after_scroll(site):
    """DOM.getNodeForLocation 은 문서 좌표 — 스크롤된 페이지에서도 뷰포트 좌표의 같은 요소를 맞힌다."""
    PAGES["/c"] = (_HEAD + "<div style=\"height:1500px\"></div>"
                   "<form action=\"/order/checkout\" method=post>"
                   "<button id=bb style=\"width:120px;height:40px\">다음</button></form>"
                   "<div style=\"height:1500px\"></div>")
    async with BrowserMCPServer() as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/c"})
        await _page(server).locator("#bb").scroll_into_view_if_needed()
        box = await _page(server).locator("#bb").bounding_box()
        x, y = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        iso = await _dispatcher(server).isolated_point_target(x, y)
    assert iso is not None and iso["info"]["tag"] == "BUTTON" and iso["info"]["in_form"] is True


# ------------------------------------------------------------------ 격리 world 캐시 (frameId, loaderId)


@requires_chromium
async def test_isolated_world_follows_navigation(site):
    """이동하면 같은 frameId 에서 loaderId 가 바뀐다 → 새 격리 world. 이동 직후 판정이 새 문서를 본다."""
    PAGES["/a"] = _HEAD + "<input id=q aria-label=\"첫 문서\">"
    PAGES["/b"] = _search_form(TAMPER["delete_proto_form"])
    async with BrowserMCPServer() as server:
        d = _dispatcher(server)
        await _call(server, ActionType.NAVIGATE, {"url": site + "/a"})
        await _page(server).focus("#q")
        first = await d.isolated_focus_target()
        keys_a = set(d._gate_worlds)  # noqa: SLF001
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b"})
        await _page(server).focus("#q")
        second = await d.isolated_focus_target()
        keys_b = set(d._gate_worlds)  # noqa: SLF001
    assert first["name"] == "첫 문서" and first["in_form"] is False
    assert second["name"] == "검색어" and second["in_form"] is True
    (_, frame_a, loader_a), = keys_a
    (_, frame_b, loader_b), = keys_b  # 이전 문서 것은 버렸다
    assert frame_a == frame_b and loader_a != loader_b


@requires_chromium
async def test_isolated_world_reads_rewritten_document_same_loader(site):
    """document.open/write(loaderId 그대로): 격리 world 판정이 이전 내용이 아니라 지금 문서를 본다."""
    PAGES["/a"] = _HEAD + "<input id=q aria-label=\"첫 문서\">"
    async with BrowserMCPServer() as server:
        d = _dispatcher(server)
        await _call(server, ActionType.NAVIGATE, {"url": site + "/a"})
        await _page(server).focus("#q")
        assert (await d.isolated_focus_target())["name"] == "첫 문서"
        await _page(server).evaluate(
            "() => { document.open(); document.write('<form action=/s><input id=q aria-label=\"새 문서\">"
            "</form><scr' + 'ipt>delete HTMLInputElement.prototype.form</scr' + 'ipt>'); document.close(); }")
        await _page(server).focus("#q")
        info = await d.isolated_focus_target()
    assert info is not None and info["name"] == "새 문서" and info["in_form"] is True


class _FakeCdp:
    """격리 world 컨텍스트가 사라지는 상황을 흉내 낸 CDP 세션(재시도 경로 단위 시험)."""

    def __init__(self, fail_recreated: bool = False) -> None:
        self.live = None
        self.created = 0
        self.fail_recreated = fail_recreated

    def on(self, *_a: Any) -> None:
        pass

    async def send(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F", "loaderId": "L"}}}
        if method == "Page.createIsolatedWorld":
            self.created += 1
            self.live = 100 + self.created
            return {"executionContextId": self.live}
        if method == "Runtime.evaluate":
            if params["contextId"] != self.live or (self.fail_recreated and self.created > 1):
                raise RuntimeError("Protocol error (Runtime.evaluate): Cannot find context with specified id")
            return {"result": {"value": {"tag": "INPUT", "type": "text", "name": "q", "in_form": True}}}
        raise AssertionError(method)


@pytest.mark.parametrize("fail_recreated", [False, True])
async def test_isolated_world_recreated_once_when_context_gone(fail_recreated):
    from actions.dispatcher import ActionDispatcher

    d = ActionDispatcher.__new__(ActionDispatcher)
    fake = _FakeCdp(fail_recreated=fail_recreated)

    async def gate_cdp(_page: Any) -> Any:
        return fake

    d._gate_cdp_for = gate_cdp  # noqa: SLF001
    d._top_page = lambda: object()  # noqa: SLF001
    assert (await d.isolated_focus_target())["in_form"] is True
    assert fake.created == 1
    fake.live = 999  # 같은 loaderId 인데 컨텍스트가 사라짐(캐시는 101 을 들고 있다)
    info = await d.isolated_focus_target()
    assert fake.created == 2  # 'context 없음' → 한 번만 새로 만든다
    if fail_recreated:
        assert info is None  # 재생성도 실패 = 격리 판정 불가 → 기존 판정만
    else:
        assert info is not None and info["in_form"] is True


# ------------------------------------------------------------------ 높은 쪽 고르기(순수)


def test_risk_rank_order():
    from interface.server_hitl import _gate_rank

    def rank(**kw):
        base = dict(name="", submits_form=False, unresolved="", signals=())
        base.update(kw)
        return _gate_rank(ActionType.PRESS_KEY, "example.test", **base)

    none = rank()
    signal = rank(signals=(("form_action", "/order/checkout"),))
    submit = rank(submits_form=True)
    unresolved = rank(unresolved="판정 불가")
    assert unresolved > submit > signal > none
