"""WS-57 R3 — 재검증 FAIL B3 를 저장소 테스트로 고정(실 Chromium, 로컬 http·합성 값만).

B3 R2 의 귀속 조건(차단 기록의 리다이렉트 앞 홉 호스트 하나라도 이동 대상과 같으면 문서 이동 차단)이 같은 페이지의
하위 요청(이미지·스크립트·iframe)이 비허용 주소로 302 돼 막힌 것까지 골라, 정상(200·로딩 완료) 문서의 navigate 를
E_INVALID_URL 로 바꿨다 → navigate 오류에는 그 탭 메인 프레임 문서 요청(과 그 리다이렉트 체인)의 차단만 붙인다.
검증 보고 ws57-r2 §B3 의 image_document_probe(다른 포트 이미지 302 → 비허용 주소, HTML 을 나눠 보내 이미지 차단이
문서 로딩 중에 기록되게)를 옮긴 것과 메인 문서 리다이렉트가 진짜 막히는 대조군.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from interface.mcp_server import BrowserMCPServer, envelope_dict

from test_run_cli import requires_chromium


class _Pair:
    """허용 `localhost:A`(문서) + `localhost:B`(하위 자원 302 → 비허용 `127.0.0.1:B`)."""

    def __init__(self) -> None:
        self.hits: List[Tuple[int, str]] = []
        self.endpoints: Dict[str, str] = {}
        pair = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                pair.hits.append((self.server.server_port, self.path))
                try:
                    if self.path.startswith("/res-redirect"):
                        self.send_response(302)
                        self.send_header("Location", pair.endpoints["denied"])
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if self.path.startswith("/doc-redirect"):
                        self.send_response(302)
                        self.send_header("Location", pair.endpoints["denied_doc"])
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if self.path.startswith(("/stream-", "/stream404-")):
                        kind = self.path.split("-", 1)[1]
                        res = pair.endpoints["res"]
                        tag = {"img": f'<img src="{res}">',
                               "script": f'<script src="{res}"></script>',
                               "iframe": f'<iframe src="{res}"></iframe>'}[kind]
                        prefix = ("<title>Healthy document</title><h1>HEALTHY_DOCUMENT</h1>" + tag).encode()
                        tail = b"<p>HTML_COMPLETE</p><button>still usable</button>"
                        self.send_response(404 if self.path.startswith("/stream404-") else 200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(prefix) + len(tail)))
                        self.end_headers()
                        self.wfile.write(prefix)
                        self.wfile.flush()
                        time.sleep(0.3)  # 하위 자원 차단이 문서 로딩 중(호출 안)에 기록되게
                        self.wfile.write(tail)
                        return
                    body = b"User-agent: *\nAllow: /\n" if self.path == "/robots.txt" else b"FORBIDDEN_ENDPOINT"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):  # noqa: D401
                pass

        self.servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        for srv in self.servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        a, b = (srv.server_address[1] for srv in self.servers)
        self.doc = f"http://localhost:{a}"
        self.endpoints.update(res=f"http://localhost:{b}/res-redirect",
                              denied=f"http://127.0.0.1:{b}/denied-res",
                              denied_doc=f"http://127.0.0.1:{a}/denied-doc")

    def close(self) -> None:
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def denied_hits(self) -> List[Tuple[int, str]]:
        return [h for h in self.hits if h[1].startswith("/denied")]


@pytest.fixture
def pair():
    p = _Pair()
    yield p
    p.close()


async def _navigate(s: BrowserMCPServer, url: str) -> Dict[str, Any]:
    return envelope_dict(await s.call_tool("browser_navigate", {"url": url, "timeout_ms": 8000}))


@requires_chromium
@pytest.mark.parametrize("kind", ["img", "script", "iframe"])
async def test_b3_subresource_redirect_block_keeps_healthy_document_success(pair, kind):
    """하위 자원(이미지·스크립트·iframe 하위 문서)이 비허용 주소로 302 돼 막혀도 정상 문서 navigate 는 성공."""
    async with BrowserMCPServer(recipes=False, headless_retry=False, allowed_domains=("localhost",)) as s:
        for trial in range(2):
            before = s._egress.blocked_total
            r = await _navigate(s, f"{pair.doc}/stream-{kind}")
            state = await s._dispatcher.ctx.page.evaluate(
                "({ready: document.readyState, body: document.body.innerText})")
            # 측정 조건(규칙 4): 이 호출 안에서 그 탭에 귀속되고 앞 홉이 이동 대상 호스트(localhost)인 차단 기록이 있다
            mine = [d for d in s._egress.blocked_since(before)
                    if d.host == "127.0.0.1" and s._egress_record_mine(d) and "localhost" in d.chain_hosts]
            assert mine, (kind, trial, s._egress.blocked_requests)
            assert state["ready"] == "complete" and "HTML_COMPLETE" in state["body"], state
            assert r["success"] is True, (kind, trial, r)
            assert r["data"]["last_http_status"] == 200, r
            assert "egress" not in r["data"] and "127.0.0.1" not in json.dumps(r, ensure_ascii=False), r
    assert not pair.denied_hits(), pair.hits


@requires_chromium
async def test_b3_subresource_block_on_non_200_document_is_not_a_navigate_block(pair):
    """사이트가 준 404 문서(정상 로딩)의 이미지가 막혀도 egress 이동 실패가 아니다 — 200 성공 유지 판정에 기대지 않고
    요청 종류(메인 프레임 문서인가)로 거른다."""
    async with BrowserMCPServer(recipes=False, headless_retry=False, allowed_domains=("localhost",)) as s:
        before = s._egress.blocked_total
        r = await _navigate(s, f"{pair.doc}/stream404-img")
        mine = [d for d in s._egress.blocked_since(before)
                if d.host == "127.0.0.1" and s._egress_record_mine(d) and "localhost" in d.chain_hosts]
        assert mine, s._egress.blocked_requests
        assert r["data"].get("last_http_status") == 404, r
        assert r.get("error_code") != "E_INVALID_URL", r
        assert "egress" not in r["data"] and "127.0.0.1" not in json.dumps(r, ensure_ascii=False), r


@requires_chromium
async def test_b3_main_document_redirect_block_still_fails_navigate(pair):
    """대조군: 메인 문서가 허용 localhost 에서 비허용 127.0.0.1 로 302 되면 여전히 egress_blocked 이동 실패."""
    async with BrowserMCPServer(recipes=False, headless_retry=False, allowed_domains=("localhost",)) as s:
        for _ in range(2):
            r = await _navigate(s, f"{pair.doc}/doc-redirect")
            assert r["success"] is False and r["error_code"] == "E_INVALID_URL", r
            assert r["data"]["egress"]["host"] == "127.0.0.1", r
            assert "egress_blocked" in r["error_message"], r
    assert not pair.denied_hits(), pair.hits


# ------------------------------------------------------------------------------------- 단위: 출처 판별


class _Frame:
    def __init__(self, page: Any, parent: Any = None) -> None:
        self.page = page
        self.parent_frame = parent


class _Req:
    def __init__(self, url: str, frame: Any, *, nav: bool, rtype: str, prev: Any = None) -> None:
        self.url = url
        self.frame = frame
        self._nav = nav
        self.resource_type = rtype
        self.redirected_from = prev

    def is_navigation_request(self) -> bool:
        return self._nav


def test_request_origin_marks_only_main_frame_document_requests():
    from security.egress import request_origin

    page = SimpleNamespace(url="http://a.test/")
    main = _Frame(page)
    child = _Frame(page, parent=main)
    hop = _Req("http://a.test/r", main, nav=True, rtype="document")
    cases = {
        "main_doc_redirect": (_Req("http://b.test/x", main, nav=True, rtype="document", prev=hop), True),
        "image": (_Req("http://b.test/i", main, nav=False, rtype="image", prev=hop), False),
        "script": (_Req("http://b.test/s", main, nav=False, rtype="script"), False),
        "fetch": (_Req("http://b.test/f", main, nav=False, rtype="fetch"), False),
        "iframe_doc": (_Req("http://b.test/d", child, nav=True, rtype="document", prev=hop), False),
    }
    for name, (req, expected) in cases.items():
        got_page, hosts, main_doc = request_origin(req)
        assert got_page is page, name
        assert main_doc is expected, name
    assert request_origin(cases["main_doc_redirect"][0])[1] == ("a.test",)


# ------------------------------------------------------------------------------------- 단위: 부착 판정


class _StatusStub:
    def __init__(self, status: Any) -> None:
        self.status = status

    def judge_status_for(self, page: Any) -> Any:
        return self.status


def _attach(status: Any, *, main_document: bool, success: bool = True) -> Any:
    from contracts import ActionResult, ActionType
    from security import EgressGuard, EgressPolicy
    from security.egress import EgressDecision

    srv = BrowserMCPServer()
    page = SimpleNamespace(url="http://a.test/doc")
    srv._page = page
    srv._dispatcher = SimpleNamespace(ctx=SimpleNamespace(page=page))
    srv._page_status = _StatusStub(status)
    srv._egress = EgressGuard(policy=EgressPolicy.OPEN_SANDBOX, allow_loopback=True)
    before = srv._egress.blocked_total
    srv._egress.record_block(EgressDecision(allowed=False, url="http://b.test/x", host="b.test", category="c"),
                             (page, ("a.test",), main_document))
    ok = ActionResult(success=success, action=ActionType.NAVIGATE, current_url="http://a.test/doc",
                      snapshot_epoch=0, tab_id="t", retry_safe=True, data={})
    return srv._attach_egress_block(ActionType.NAVIGATE, {"url": "http://a.test/doc"}, ok, before)


@pytest.mark.parametrize("status", [None, 403, 404, 200])
def test_attach_ignores_subresource_records_whatever_the_document_status(status):
    r = _attach(status, main_document=False)
    assert r.success is True and "egress" not in r.data


@pytest.mark.parametrize("status", [None, 403, 502])
def test_attach_main_document_redirect_record_fails_navigate(status):
    from contracts import ErrorCode

    r = _attach(status, main_document=True)
    assert r.success is False and r.error_code is ErrorCode.INVALID_URL and r.data["egress"]["host"] == "b.test"


def test_attach_keeps_success_when_tab_then_loaded_a_200_document():
    r = _attach(200, main_document=True)
    assert r.success is True and "egress" not in r.data
    failed = _attach(200, main_document=True, success=False)  # 이동 자체가 실패했으면 이유를 붙여 실패
    assert failed.success is False and failed.data["egress"]["host"] == "b.test"
