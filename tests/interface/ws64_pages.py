"""WS-64 시험용 로컬 모형 — SRT 구조(읽기 전용 출발일 칸 + window.open 달력 팝업 + 조회 버튼 + confirm 예매).

실제 SRT(etk.srail.kr)에 접속하지 않는다. 같은 서버를 두 출처로 쓴다: http://127.0.0.1:<port>(목록 사이트로 넣는 쪽)와
http://localhost:<port>(목록 밖). 조회 버튼은 isTrusted 가 아니면 '매크로 차단'을 적는다(매크로 방지 흉내 — 우회하지 않음).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator

SRT = """<!doctype html><meta charset=utf-8><title>SRT 모형</title>
<label>출발일 <input id=dptDt aria-label="출발일" readonly value="20261024"></label>
<button id=cal onclick="window.open('/cal','calendar','width=320,height=320')">달력 열기</button>
<button id=search>조회하기</button>
<button id=book>예매</button>
<input id=name aria-label="이름">
<input id=pw type=password aria-label="비밀번호">
<select id=seat aria-label="좌석"><option value=a>일반실</option><option value=b>특실</option></select>
<label><input type=checkbox id=agree aria-label="약관 동의"> 약관 동의</label>
<input type=file id=attach aria-label="첨부 파일">
<button id=dl>표 내려받기</button>
<a href="/srt2">다음 화면</a>
<div id=out></div>
<script>
window.__mut = 0; window.__clicks = 0; window.__hover = 0;
new MutationObserver(m => { window.__mut += m.length; }).observe(document.documentElement,
  {subtree: true, childList: true, attributes: true, characterData: true});
document.getElementById('search').addEventListener('click', e => {
  window.__clicks++;
  document.getElementById('out').textContent = e.isTrusted ? '조회됨' : '매크로 차단';
});
document.getElementById('search').addEventListener('mouseover', () => { window.__hover++; });
document.getElementById('book').addEventListener('click', () => {
  document.getElementById('out').textContent = confirm('예매하시겠습니까?') ? '예매확정' : '예매취소';
});
document.getElementById('dl').addEventListener('click', () => {
  const a = document.createElement('a'); a.href = '/ticket.csv'; a.download = 'ticket.csv'; a.click();
});
</script>"""

CAL = """<!doctype html><meta charset=utf-8><title>달력</title>
<button id=d1101 onclick="opener.document.getElementById('dptDt').value='20261101'; window.close()">11월 1일</button>
<button id=ask onclick="document.title = confirm('이 날짜로 할까요?') ? '수락' : '거절'">확인 묻기</button>"""

SRT2 = """<!doctype html><meta charset=utf-8><title>다음 화면</title><p>조회 결과</p><button>뒤로</button>"""

#: 목록 밖 출처(localhost)가 목록 출처(127.0.0.1) iframe 을 품는 페이지 — 좌표·키는 탭의 모든 프레임 기준.
HOST = """<!doctype html><meta charset=utf-8><title>바깥</title>
<button id=outer onclick="document.getElementById('o').textContent='바깥 눌림'">바깥 버튼</button><div id=o></div>
<iframe id=f src="{inner}" width=600 height=300></iframe>"""

PLAIN = """<!doctype html><meta charset=utf-8><title>목록 밖</title>
<button id=go onclick="document.getElementById('o').textContent='눌림'">일반 버튼</button><div id=o></div>
<button id=ask onclick="document.getElementById('o').textContent = confirm('확인?') ? '수락' : '거절'">확인 묻기</button>
<input id=q aria-label="검색어">"""


class _Handler(BaseHTTPRequestHandler):
    pages: Dict[str, str] = {}

    def log_message(self, *a: Any) -> None:  # noqa: D401 - 조용히
        pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/ticket.csv":
            body, ctype = b"a,b\n1,2\n", "text/csv"
        elif path == "/robots.txt":
            body, ctype = b"User-agent: *\nAllow: /\n", "text/plain"
        else:
            html = self.pages.get(path)
            if html is None:
                self.send_response(404)
                self.end_headers()
                return
            body, ctype = html.replace("{inner}", self.server.inner_url).encode("utf-8"), "text/html; charset=utf-8"  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SrtMock:
    """with SrtMock() as m: m.listed('/srt') (목록 출처) · m.unlisted('/plain') (목록 밖) · m.entry (목록 항목)."""

    def __enter__(self) -> "SrtMock":
        handler = type("H", (_Handler,), {"pages": {"/srt": SRT, "/cal": CAL, "/srt2": SRT2, "/host": HOST,
                                                    "/plain": PLAIN}})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.httpd.inner_url = f"http://127.0.0.1:{self.port}/srt"  # type: ignore[attr-defined]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def entry(self) -> str:
        return f"127.0.0.1:{self.port}"

    def listed(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def unlisted(self, path: str) -> str:
        return f"http://localhost:{self.port}{path}"


def srt_mock() -> Iterator[SrtMock]:
    with SrtMock() as m:
        yield m


async def human_click(page: Any, selector: str) -> None:
    """사람 흉내(시험에서만): 실제 CDP 마우스 입력(isTrusted=true)으로 요소 가운데를 누른다."""
    box = await page.locator(selector).bounding_box()
    assert box is not None, selector
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    cdp = await page.context.new_cdp_session(page)
    try:
        await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for kind in ("mousePressed", "mouseReleased"):
            await cdp.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                        "clickCount": 1})
    finally:
        try:
            await cdp.detach()
        except Exception:  # noqa: BLE001 - 누른 결과로 창이 닫힘(달력 팝업)
            pass
