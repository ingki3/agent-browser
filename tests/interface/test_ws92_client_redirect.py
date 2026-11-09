"""WS-92 (#247) — navigate 중 주 프레임 문서가 다시 커밋돼도(클라이언트 이동·같은 주소 reload) 열린 문서면 부분 성공.

실제 사례(봇 호출 25224): navigate(corp.kaltura.com/newsroom/, networkidle) → 같은 주소 404 문서가 load 뒤 자기 자신을
다시 커밋하고 폴링으로 networkidle 에 못 닿아 14.5s E_NAVIGATE_TIMEOUT + '이동 중 다른 문서로 바뀜'. 기준(4fc4d82)은
커밋이 정확히 1회일 때만 부분 성공(WS-69 R2)이었다.

규칙(리뷰 v2 B1~B5): 2단계 시간 초과에서 커밋 수 n 이 2..3 이고, readyState 판독 전후 n 이 같고(판독 값이 지금 문서의
것), 마지막 await 뒤 동기적으로 page.url 이 http(s)·오류 문서 아님, readyState ∈ {interactive, complete} 이면 #170 과
같은 부분 성공 + load_state.documents=n. 그 밖(고리 4+, 판독 중 커밋, 오류 문서, blob:/about: 등)은 기준 실패 그대로.
로컬 HTTP(127.0.0.1, 빈 포트)만 쓴다. 비밀 값은 합성값이다.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urlparse

import pytest

from contracts import ActionType, ErrorCode


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

POLL_JS = "<script>setInterval(()=>fetch('/ping?'+Math.random()).catch(()=>0),150)</script>"
POLL = "<!doctype html><meta charset=utf-8><title>poll</title><h1 id=t>FINAL</h1>" + POLL_JS
STATIC = "<!doctype html><meta charset=utf-8><title>static</title><h1 id=t>STATIC</h1>"
NAVER = ("<!doctype html><meta charset=utf-8><title>네이버</title>"
         "<h2>보안 확인을 완료해 주세요</h2><p>아래 문제에 답해 주세요.</p>" + POLL_JS)
LINK_PAGE = ("<!doctype html><meta charset=utf-8><title>목록</title><main><h1>목록</h1>"
             "<a href='/static'>다음 글</a></main>" + POLL_JS)
SYNTH_TOKEN = "SYNTH_SECRET_92"  # 합성 비밀(테스트 전용)


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _onload(js: str, poll: bool = True) -> str:
    """load 50ms 뒤 js — 첫 문서는 폴링(networkidle 미도달)."""
    return ("<!doctype html><meta charset=utf-8><title>first</title><h1>FIRST</h1>"
            + (POLL_JS if poll else "") + f"<script>addEventListener('load',()=>setTimeout(()=>{{{js}}},50))</script>")


def _dcl_hang(js: str) -> str:
    """끝나지 않는 이미지(load 미도달) + DCL 70ms 뒤 js."""
    return ("<!doctype html><meta charset=utf-8><h1>FIRST</h1><img src='/hang'>"
            f"<script>document.addEventListener('DOMContentLoaded',()=>setTimeout(()=>{{{js}}},70))</script>")


HANG_IMG = "<!doctype html><meta charset=utf-8><title>이미지</title><p id=t>둘째 문서</p><img src='/hang'>"


@pytest.fixture
def site():
    stop = threading.Event()
    closed = _closed_port()
    state: Dict[str, Any] = {}

    def page(path: str):
        port = state["port"]
        table = {
            "/poll": (200, POLL),
            "/static": (200, STATIC),
            "/img": (200, HANG_IMG),
            "/img2": (200, HANG_IMG.replace("둘째", "셋째")),
            "/js_poll": (200, _onload("location.replace('/poll?final=1')")),
            "/js_static": (200, _onload("location.replace('/static')")),
            "/meta0": (200, "<!doctype html><meta http-equiv=refresh content='0;url=/poll?m=0'>" + POLL_JS),
            "/meta1": (200, "<!doctype html><meta http-equiv=refresh content='1;url=/poll?m=1'><h1>M1</h1>" + POLL_JS),
            "/push": (200, _onload("history.pushState({},'','/pushed?x=1')")),
            "/hop1": (200, _onload("location.href='/hop2'")),
            "/hop2": (200, "<!doctype html><meta http-equiv=refresh content='0;url=/poll?h=2'>" + POLL_JS),
            "/hop3a": (200, _onload("location.href='/hop3b'")),
            "/hop3b": (200, _onload("location.href='/hop3c'")),
            "/hop3c": (200, _onload("location.href='/poll?h=4'")),
            "/cloopa": (200, _onload("location.replace('/cloopb')")),
            "/cloopb": (200, _onload("location.replace('/cloopa')")),
            "/to_closed": (200, _onload(f"location.href='http://127.0.0.1:{closed}/x'")),
            "/to_closed_https": (200, _onload(f"location.href='https://127.0.0.1:{closed}/x'")),
            "/to_private": (200, _onload("location.href='http://10.255.255.1/x'")),
            "/to_nxdns": (200, _onload("location.href='http://nx-ws92-zz.invalid/x'")),
            "/to_404": (200, _onload("location.replace('/nf')")),
            "/nf": (404, "<!doctype html><title>404</title><h1>Not found</h1>" + POLL_JS),
            # Kaltura 모양: 같은 주소 404 문서가 load 뒤 1회 자기 reload + 폴링
            "/k404": (404, "<!doctype html><title>404</title><h1>Not found</h1>" + POLL_JS
                      + "<script>addEventListener('load',()=>{if(!sessionStorage.r){sessionStorage.r=1;"
                        "setTimeout(()=>location.reload(),50)}})</script>"),
            "/k404_replace": (404, "<!doctype html><title>404</title><h1>NF</h1>" + POLL_JS
                              + "<script>addEventListener('load',()=>{if(!sessionStorage.q){sessionStorage.q=1;"
                                "setTimeout(()=>location.replace(location.href),50)}})</script>"),
            "/docopen": (200, _onload("document.open();document.write('<h1>W</h1>'+%r);document.close()" % POLL_JS)),
            "/to_token": (200, _onload(f"location.replace('/poll?access_token={SYNTH_TOKEN}&sid=SYNTHSID&code=SYNTHCODE#frag')")),
            "/to_dl": (200, _onload("location.href='/dl'")),
            "/to_never": (200, _onload("location.href='/hangdoc'")),
            "/to_naver": (200, _onload("location.replace('/naver')")),
            "/naver": (200, NAVER),
            "/to_list": (200, _onload("location.replace('/list')")),
            "/list": (200, LINK_PAGE),
            "/js_to_127": (200, _onload(f"location.href='http://127.0.0.1:{port}/poll'")),
            "/to_blob": (200, _dcl_hang(
                f"location.href=URL.createObjectURL(new Blob([\"<p>blob</p><img src='http://127.0.0.1:{port}/hang'>\"],"
                "{type:'text/html'}))")),
            "/to_data": (200, _dcl_hang("location.href='data:text/html,<p>d</p>'")),
        }
        return table.get(path, (200, STATIC))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a: Any) -> None:
            pass

        def _redir(self, to: str) -> None:
            self.send_response(302)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            try:
                if u.path == "/hang":
                    stop.wait(20)
                    return
                if u.path == "/hangdoc":
                    stop.wait(20)
                    return
                if u.path == "/robots.txt":
                    body = b"User-agent: *\nAllow: /\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if u.path == "/s302":
                    return self._redir("/poll?s=302")
                if u.path == "/s302loop":
                    return self._redir("/s302loop")
                if u.path == "/dl":
                    body = b"x" * 1000
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", "attachment; filename=a.bin")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                status, text = page(u.path)
                body = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.block_on_close = False
    state["port"] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{state['port']}"
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()


def _dispatcher(page):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    return ActionDispatcher(DispatchContext(page=page, engine=PerceptionEngine()))


async def _direct(url: str, wait_until: str = "networkidle", timeout_ms: int = 1500, hook=None):
    """디스패처 단독(egress 프록시 없음). hook(d, page) 로 판독을 바꿔 끼울 수 있다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            page = await (await b.new_context()).new_page()
            d = _dispatcher(page)
            extra = hook(d, page) if hook else None
            t0 = time.monotonic()
            r = await asyncio.wait_for(d.dispatch(ActionType.NAVIGATE, {
                "url": url, "wait_until": wait_until, "timeout_ms": timeout_ms}), 15.0)
            return r, page.url, time.monotonic() - t0, extra
        finally:
            await b.close()


async def _serve(url: str, wait_until: str = "networkidle", timeout_ms: int = 1500, **kw):
    from interface.mcp_server import BrowserMCPServer

    s = BrowserMCPServer(**kw)
    try:
        t0 = time.monotonic()
        r = await asyncio.wait_for(s.call_tool("browser_navigate", {
            "url": url, "wait_until": wait_until, "timeout_ms": timeout_ms}), 40.0)
        return r, time.monotonic() - t0
    finally:
        await s.close()


def _assert_partial(r, documents: int, requested: str = "networkidle") -> None:
    assert r.success is True, (r.error_code, r.error_message, r.data)
    assert r.data["load_state"] == {"requested": requested, "reached": "domcontentloaded", "timed_out": True,
                                    "documents": documents}, r.data
    assert "문서는 열렸" in r.data["hint"]
    assert "nav_failure" not in r.data


def _assert_doc_changed_failure(r) -> None:
    assert r.success is False, r.data
    assert r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in r.data
    assert r.data["nav_failure"] == {"kind": "timeout"}
    assert "이동 중 다른 문서로 바뀜" in r.data["hint"]


# -- 1. 합격 RED: Kaltura 모양(같은 주소 404 + 폴링 + load 뒤 1회 reload) — 서버 경로 ----------------------


async def test_k404_self_reload_is_partial_success_with_page_not_found(site):
    r, _ = await _serve(site + "/k404")
    _assert_partial(r, 2)
    assert r.data["url"] == site + "/k404"  # 같은 주소 — 최종 주소는 기존 url 칸
    assert r.data["last_http_status"] == 404
    assert r.data["page_not_found"]["status"] == 404
    assert "redirected_from" not in r.data


async def test_k404_self_replace_is_partial_success(site):
    r, _ = await _serve(site + "/k404_replace")
    _assert_partial(r, 2)
    assert r.data["page_not_found"]["status"] == 404


# -- 2. 실패 → 부분 성공(디스패처 단독) ------------------------------------------------------------------


@pytest.mark.parametrize("path,final,docs,timeout_ms", [
    ("/js_poll", "/poll?final=1", 2, 1500),
    ("/meta0", "/poll?m=0", 2, 1500),
    ("/meta1", "/poll?m=1", 2, 2600),
    ("/hop1", "/poll?h=2", 3, 2000),  # 2홉 클라이언트(3커밋 = 상한)
    ("/k404", "/k404", 2, 1500),
    ("/k404_replace", "/k404_replace", 2, 1500),
    ("/to_404", "/nf", 2, 1500),
])
async def test_client_hops_become_partial_success_direct(site, path, final, docs, timeout_ms):
    r, url, _, _ = await _direct(site + path, timeout_ms=timeout_ms)
    _assert_partial(r, docs)
    assert url == site + final and r.data["url"] == site + final


async def test_token_query_hop_is_partial_success_and_url_is_redacted(site):
    """최종 url 의 access_token·sid·code 는 기존 주소 칸 정책(public_url)으로 가려지고 fragment 는 남는다."""
    from interface.server_envelope import envelope_json

    r, _ = await _serve(site + "/to_token")
    _assert_partial(r, 2)
    text = envelope_json(r)
    assert SYNTH_TOKEN not in text and "SYNTHSID" not in text and "SYNTHCODE" not in text, text
    payload = json.loads(text)
    url = payload["data"]["url"]
    assert url.startswith(site + "/poll?access_token=") and url.endswith("#frag"), url
    assert "documents" in payload["data"]["load_state"]
    assert payload["data"]["load_state"]["documents"] == 2


async def test_client_hop_to_challenge_page_is_success_with_challenge_signal(site):
    """NN2: 클라이언트 홉 뒤 보안 확인 화면 — 직접 이동처럼 success + challenge(서버 층이 최종 page.url 로 판정)."""
    r, _ = await _serve(site + "/to_naver")
    _assert_partial(r, 2)
    assert r.data["url"] == site + "/naver"
    assert r.data["challenge"] == {"kind": "captcha", "vendor": "generic", "reason": "네이버 보안 확인 화면"}


# -- 3. 실패 유지 ---------------------------------------------------------------------------------------


async def test_client_redirect_loop_stays_failure(site):
    """B4: A↔B location.replace 고리(커밋 4회 이상) — 기존 실패 그대로."""
    r, url, _, _ = await _direct(site + "/cloopa", timeout_ms=1500)
    _assert_doc_changed_failure(r)


async def test_four_commit_hops_stay_failure(site):
    """3홉 클라이언트(커밋 4회) — 상한(3) 초과는 고리와 같게 실패."""
    r, url, _, _ = await _direct(site + "/hop3a", timeout_ms=2500)
    assert url == site + "/poll?h=4"  # 전제: 실제로 4번째 문서까지 갔다
    _assert_doc_changed_failure(r)


async def test_commit_during_ready_state_read_stays_failure(site, monkeypatch):
    """B3: readyState 판독(마지막 await) 동안 정상 http 문서가 커밋되면 그 판독 값은 옛 문서의 것 — 실패."""
    import actions.dispatcher as dispatcher_mod

    monkeypatch.setattr(dispatcher_mod, "_NAV_READ_GRACE_S", 3.0)  # 주입이 판독 예산에 취소되지 않게(시험 전제)

    def hook(d, page):
        real = d._main_ready_state
        seen = {"calls": 0, "value": None, "committed": False}

        async def slow(p):
            seen["calls"] += 1
            value = await real(p)
            seen["value"] = value
            await page.evaluate("location.href='/img2'")
            deadline = time.monotonic() + 2.0
            while not page.url.endswith("/img2") and time.monotonic() < deadline:
                await asyncio.sleep(0.002)
            seen["committed"] = page.url.endswith("/img2")
            return value

        d._main_ready_state = slow
        return seen

    r, url, _, seen = await _direct(site + "/img", "load", 300, hook=hook)
    assert seen["calls"] == 1 and seen["value"] in ("interactive", "complete") and seen["committed"], seen
    assert url.startswith("http") and url.endswith("/img2")
    _assert_doc_changed_failure(r)


async def test_blob_final_document_stays_failure(site):
    """B5: 다중 커밋 뒤 http(s) 가 아닌 문서(blob:)로 끝나면 '오류 문서 아님'이어도 부분 성공이 아니다."""
    r, url, _, _ = await _direct(site + "/to_blob", "load", 1200)
    assert url.startswith("blob:"), url  # 전제: 실제로 blob 문서가 최상위에 커밋됐다
    _assert_doc_changed_failure(r)


async def test_data_url_hop_is_blocked_and_unchanged(site):
    """대조: 최상위 data: 이동은 Chromium 이 막는다 — 커밋 1회, 기존(#170) 부분 성공 그대로(documents 키 없음)."""
    r, url, _, _ = await _direct(site + "/to_data", "load", 1000)
    assert url == site + "/to_data"
    assert r.success is True and r.data["load_state"] == {
        "requested": "load", "reached": "domcontentloaded", "timed_out": True}


async def test_js_to_never_answering_document_stays_failure_direct(site):
    r, _, elapsed, _ = await _direct(site + "/to_never", timeout_ms=1500)
    assert r.success is False and r.error_code is ErrorCode.NAVIGATE_TIMEOUT
    assert "load_state" not in r.data
    assert elapsed < 1.5 + 0.5, elapsed


@pytest.mark.parametrize("path,code,egress_code,kind", [
    ("/s302loop", ErrorCode.NAVIGATE_TIMEOUT, None, "other"),
    ("/cloopa", ErrorCode.NAVIGATE_TIMEOUT, None, "timeout"),
    ("/to_closed", ErrorCode.NAVIGATE_TIMEOUT, "connect_failed", "connect"),  # 서버 층 502 재구성
    ("/to_closed_https", ErrorCode.NAVIGATE_TIMEOUT, None, "connect"),  # chrome-error(TUNNEL)
    ("/to_private", ErrorCode.INVALID_URL, "egress_blocked", "blocked"),
    ("/to_nxdns", ErrorCode.NAVIGATE_TIMEOUT, "resolve_failed", "dns"),
])
async def test_neighbour_failures_unchanged_serve(site, path, code, egress_code, kind):
    r, _ = await _serve(site + path)
    assert r.success is False, r.data
    assert r.error_code is code
    assert "load_state" not in r.data
    assert r.data["nav_failure"]["kind"] == kind, r.data
    if egress_code:
        assert r.data["egress"]["code"] == egress_code
    else:
        assert "egress" not in r.data


async def test_allowlist_outside_client_hop_stays_blocked_serve(site):
    port = urlparse(site).port
    r, _ = await _serve(f"http://localhost:{port}/js_to_127", allowed_domains=("localhost",))
    assert r.success is False and r.error_code is ErrorCode.INVALID_URL
    assert r.data["egress"]["code"] == "egress_blocked"
    assert r.data["egress"]["category"] == "not_in_allowlist"
    assert "load_state" not in r.data


# -- 4. 대조(1커밋·문서 교체 없음) — documents 키 없음, 기존 모양 그대로 ---------------------------------


@pytest.mark.parametrize("path", ["/s302", "/push", "/docopen", "/to_dl"])
async def test_single_commit_controls_have_no_documents_key(site, path):
    r, url, _, _ = await _direct(site + path, timeout_ms=1500)
    assert r.success is True, r.data
    assert r.data["load_state"] == {"requested": "networkidle", "reached": "domcontentloaded", "timed_out": True}


async def test_js_to_static_completes_without_load_state(site):
    r, url, _, _ = await _direct(site + "/js_static", timeout_ms=3000)
    assert r.success is True and "load_state" not in r.data
    assert url == site + "/static"


# -- 5. 레시피 기록·재생(NN6) ----------------------------------------------------------------------------


async def test_recipe_records_and_replays_multi_commit_navigate(site):
    from interface.mcp_server import BrowserMCPServer, tool_name

    async def call(srv, action, **args):
        return await srv.call_tool(tool_name(action), args)

    async with BrowserMCPServer(headless=True, profile="ws92") as srv:
        nav = await call(srv, ActionType.NAVIGATE, url=site + "/to_list", wait_until="networkidle", timeout_ms=1500)
        _assert_partial(nav, 2)
        obs = await call(srv, ActionType.OBSERVE_PAGE)
        eid = next(e["element_id"] for e in obs.data["observation"]["elements"] if e["name"] == "다음 글")
        clicked = await call(srv, ActionType.CLICK, element_id=eid, epoch=obs.snapshot_epoch)
        assert clicked.success, clicked.error_message
        saved = await srv.call_server_tool("browser_recipe", {"op": "save", "name": "ws92 목록", "last_n": 2})
        assert saved["success"], saved
        rid = saved["data"]["recipe"]["id"]
    async with BrowserMCPServer(headless=True, profile="ws92") as srv2:
        out = await srv2.call_server_tool("browser_recipe", {"op": "run", "id": rid})
        assert out["success"], out
        assert srv2._page.url == site + "/static"
