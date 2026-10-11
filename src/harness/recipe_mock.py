"""레시피 재생(WS-38) Mock 사이트 — 로컬 HTTP 서버(외부 접속 없음).

페이지 묶음을 시나리오마다 바꿔 끼운다(`site.feed(rows, ...)`). 요청 경로를 모두 기록해(`site.log`)
재생이 무엇을 눌렀는지(오클릭)를 서버 쪽에서 독립적으로 판정한다.

경로
* /list            — 머리(검색칸·버튼) + 기사 목록(ul.items > li.item, 제목 /item?id=N)
* /search?q=...    — 검색 결과 목록(같은 틀) — params 치환 확인용
* /item?id=N       — 기사 페이지("장바구니 담기" → /cart)
* /cart            — "결제하기" 버튼(HITL 고위험) → /paid 요청이면 결제됨
* /login           — 아이디·비밀번호 칸 + 로그인 버튼(WS-38b 비밀 단계 자동 저장 제외 확인용)
"""

from __future__ import annotations

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

Rows = Sequence[Tuple[int, str]]

ROWS: List[Tuple[int, str]] = [(101, "Alpha"), (102, "Beta"), (103, "Gamma"), (104, "Delta"), (105, "Epsilon")]
NEW_ROWS: List[Tuple[int, str]] = [(201, "Zeta"), (202, "Eta"), (203, "Theta"), (204, "Iota"), (205, "Kappa")]

HEAD = "<!doctype html><html><head><meta charset=utf-8><title>{title}</title></head><body>"
HEADER = (
    "<header><nav><a href='/list'>home</a> <a href='/list?new=1'>new</a>"
    "<div role='search'><input id='q' type='search' aria-label='검색어'>"
    "<button type='button' onclick=\"location.href='/search?q='+encodeURIComponent("
    "document.getElementById('q').value)\">검색</button></div></nav></header>"
)
HEADER_B = (  # A/B 변형(머리 구조가 다름)
    "<div class='topbar'><div class='brand'><span>B</span></div>"
    "<div role='search'><input id='q' type='search' aria-label='검색어'>"
    "<button type='button' onclick=\"location.href='/search?q='+encodeURIComponent("
    "document.getElementById('q').value)\">검색</button></div></div>"
)


def items_html(rows: Rows, *, ad_first: bool = False, cls: str = "items") -> str:
    lis = []
    if ad_first:
        lis.append(
            "<li class='item'><span class='rank'>0.</span>"
            "<a class='title' href='https://ads.invalid/click?c=1'>오늘만 특가</a> "
            "<a class='sub' href='https://ads.invalid/click?c=2'>discuss</a></li>"
        )
    for i, (rid, title) in enumerate(rows, 1):
        lis.append(
            f"<li class='item'><span class='rank'>{i}.</span>"
            f"<a class='title' href='/item?id={rid}'>{html.escape(title)}</a> "
            f"<a class='sub' href='/item?id={rid}'>discuss</a></li>"
        )
    return f"<ul class='{cls}'>" + "".join(lis) + "</ul>"


def two_cols(rows: Rows, other: Rows, *, twin: bool) -> str:
    """같은 깊이의 목록 두 개(골격은 같음). twin=False 면 둘째 목록의 항목 틀이 다르고(li.post),
    True 면 첫째와 같은 틀(li.item) — '같은 틀 목록 2개'(모호) 시나리오."""
    second = items_html(other)
    if not twin:
        second = second.replace("class='item'", "class='post'")
    return f"<div class='col'>{items_html(rows)}</div><div class='col'>{second}</div>"


def list_page(rows: Rows = ROWS, *, header: str = HEADER, lists: Optional[str] = None,
              ad_first: bool = False, title: str = "목록") -> str:
    body = lists if lists is not None else items_html(rows, ad_first=ad_first)
    return (HEAD.format(title=title) + header + f"<main><section class='feed'>{body}</section></main>"
            "<footer><a href='/about'>about</a></footer></body></html>")


class RecipeSite:
    """시나리오별로 페이지를 바꿔 끼우는 Mock 사이트."""

    def __init__(self) -> None:
        self.rows: List[Tuple[int, str]] = list(ROWS)
        self.list_html: Callable[[], str] = lambda: list_page(self.rows)
        self.search_rows: Callable[[str], Rows] = lambda q: [(300 + i, f"{q} 결과 {i}") for i in range(1, 6)]
        self.log: List[str] = []
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()

    # -- 시나리오 조작 ------------------------------------------------------

    def feed(self, rows: Rows = ROWS, **kw: object) -> None:
        self.rows = list(rows)
        self.list_html = lambda: list_page(self.rows, **kw)  # type: ignore[arg-type]

    def clear_log(self) -> None:
        with self._lock:
            self.log.clear()

    def opened(self, prefix: str) -> List[str]:
        with self._lock:
            return [p for p in self.log if p.startswith(prefix)]

    # -- HTTP ---------------------------------------------------------------

    def _page(self, path: str) -> Tuple[int, str]:
        parts = urlsplit(path)
        q = parse_qs(parts.query)
        if parts.path == "/list":
            return 200, self.list_html()
        if parts.path == "/search":
            query = (q.get("q") or [""])[0]
            return 200, list_page(self.search_rows(query), title=f"검색 {html.escape(query)}")
        if parts.path == "/item":
            rid = (q.get("id") or ["?"])[0]
            return 200, (HEAD.format(title=f"item {html.escape(rid)}") + HEADER
                         + f"<main><h1 id='item'>item {html.escape(rid)}</h1>"
                         "<a class='cart' href='/cart'>장바구니 담기</a></main></body></html>")
        if parts.path == "/cart":
            return 200, (HEAD.format(title="장바구니") + HEADER
                         + "<main><form action='/paid'><button type='submit'>결제하기</button></form>"
                         "</main></body></html>")
        if parts.path == "/login":  # WS-38b: 비밀번호 칸·자격증명 치환 단계는 자동 저장하지 않는다
            return 200, (HEAD.format(title="로그인") + HEADER
                         + "<main><form onsubmit='return false'>"
                         "<input id='u' type='text' aria-label='아이디'>"
                         "<input id='p' type='password' aria-label='비밀번호'>"
                         "<button type='button' onclick=\"document.getElementById('msg').textContent="
                         "'확인 중'\">로그인</button><p id='msg'></p></form></main></body></html>")
        if parts.path == "/paid":
            return 200, HEAD.format(title="결제 완료") + "<p>paid</p></body></html>"
        return 404, HEAD.format(title="없음") + "<p>404</p></body></html>"

    def __enter__(self) -> "RecipeSite":
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                with site._lock:
                    site.log.append(self.path)
                status, body = site._page(self.path)
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 조용히
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def base(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def url(self, path: str) -> str:
        return self.base + path
