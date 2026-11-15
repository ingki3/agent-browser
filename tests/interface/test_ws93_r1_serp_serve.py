"""WS-93 R1 (#270 검증 R1) — 서버 경로(BrowserMCPServer.call_tool navigate, networkidle)에서 짧은 정상 Bing SERP 는
challenge null·인계 없음, 실제 과제 화면은 그대로 captcha/bing.

검증 재현(reproduce_R1.py → extra_public.py)의 합성 화면 6종을 경로만 바꿔 옮기고, 결과 0개 SERP(검색 입력칸·제목·URL q
반영)와 결과 구조만으로 걸러지는 SERP(따옴표·검색어 출처 없음)를 더한다. 로컬 HTTP(127.0.0.1)만 — bing 벤더 도메인을
127.0.0.1 로 바꿔(monkeypatch) 호스트 한정 규칙을 통과시킨다.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import quote_plus, urlparse

import pytest


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

PAIR = "One last step Please solve the challenge below to continue"
KO_PAIR = "마지막 한 단계 계속하려면 아래 과제 해결"
REASON = "Bing 과제 화면(계속하려면 과제 해결 요구)"
CAPTCHA = {"kind": "captcha", "vendor": "bing", "reason": REASON}

# 검증 extra_public.py PAGES 그대로(이름 → 화면, 기대 captcha 여부)
PAGES = {
    "serp-short": f'<title>{PAIR} - Search</title><form><input name=q value="{PAIR}"><button>Search</button></form><nav>All Images Videos News</nav><main id=b_results><ol><li class=b_algo><h2><a href="/article">Troubleshooting Bing</a></h2><cite>example.test/help</cite><p>Search results for "{PAIR}": this guide explains the message.</p></li></ol></main>',
    "serp-long": f'<title>{PAIR} - Search</title><form><input name=q value="{PAIR}"><button>Search</button></form><nav>All Images Videos News</nav><main id=b_results><ol><li class=b_algo><h2><a href="/article">Troubleshooting Bing</a></h2><cite>example.test/help</cite><p>Search results for "{PAIR}": this guide explains the message. ' + ("Ordinary search snippet. " * 50) + "</p></li></ol></main>",
    "login": "<title>Bing</title><h1>One last step</h1><p>Sign in to your account</p><input type=email><button>Continue</button>",
    "rewards": "<title>Rewards</title><h1>One last step</h1><p>Join Rewards to earn points.</p><button>Join</button>",
    "challenge": "<title>Bing</title><h1>One last step</h1><p>Please solve the challenge below to continue</p>",
    "case-nbsp-newline": "<title>Bing</title><h1>ONE\u00a0LAST<br>STEP</h1><p>PLEASE SOLVE THE CHALLENGE BELOW TO CONTINUE</p>",
    # R1 추가: 결과 0개 SERP — 검색 입력칸 값·제목·본문이 검색어(두 문장)를 반영, 결과 항목 없음, 따옴표 없음
    "zero-en": f"<title>{PAIR} - Search</title><form><input id=sb_form_q name=q value=\"{PAIR}\"><button>Search</button></form>"
               f"<nav>All Images Videos News</nav><main id=b_results><ol><li class=b_no><h1>There are no results for {PAIR}</h1>"
               "<p>Check your spelling or try different keywords</p></li></ol></main>",
    "zero-ko": f"<title>{KO_PAIR} - 검색</title><form><textarea id=sb_form_q name=q>{KO_PAIR}</textarea><button>검색</button></form>"
               f"<nav>전체 이미지 동영상 뉴스</nav><main id=b_results><ol><li class=b_no><h1>{KO_PAIR}에 대한 결과가 없습니다</h1>"
               "</li></ol></main>",
    # R1 추가: 결과 구조만으로 걸러지는 SERP — 제목·검색칸·URL 에 문장 없음, 따옴표 없음, 결과 항목(.b_algo) 1개
    "serp-structure-only": "<title>Bing</title><nav>All Images Videos News</nav><main id=b_results><ol><li class=b_algo>"
                           "<h2><a href=\"/article\">Troubleshooting Bing</a></h2><cite>example.test/help</cite>"
                           f"<p>{PAIR}: this guide explains the message.</p></li></ol></main>",
    # 실제 과제 화면 모양: 검색어 site:…(두 문장 없음), 결과 항목 없음
    "real-ko": "<title>Bing</title><a href='#m'>콘텐츠로 건너뛰기</a><a>접근성 피드백</a><span>Rewards</span>"
               "<h1>마지막 한 단계</h1><p>계속하려면 아래 과제 해결</p><div style='width:300px;height:65px'></div>",
}
EXPECTED_CAPTCHA = {"challenge", "case-nbsp-newline", "real-ko"}
QUERY = {  # URL 검색어(q)
    "zero-en": PAIR, "zero-ko": KO_PAIR, "real-ko": "site:example.com 뉴스",
}


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/robots.txt":
                body, ct = b"User-agent: *\nAllow: /\n", "text/plain"
            else:
                html = "<!doctype html><meta charset=utf-8>" + PAGES.get(u.path.rsplit("/", 1)[-1], "<p>normal</p>")
                body, ct = html.encode(), "text/html; charset=utf-8"
            try:
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.block_on_close = False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def bing_is_local(monkeypatch):
    from browser import challenge

    monkeypatch.setitem(challenge.SITE_VENDOR_DOMAINS, "bing", ("127.0.0.1",))


async def _navigate(url: str) -> Dict[str, Any]:
    from interface.mcp_server import BrowserMCPServer

    s = BrowserMCPServer()
    try:
        r = await asyncio.wait_for(s.call_tool("browser_navigate", {
            "url": url, "wait_until": "networkidle", "timeout_ms": 2500}), 40.0)
        assert r.success is True, (r.error_code, r.error_message, r.data)
        return r.data
    finally:
        await s.close()


@pytest.mark.parametrize("name", sorted(PAGES))
async def test_r1_serp_vs_challenge_on_navigate(site, bing_is_local, name):
    from interface.server_signals import HANDOFF_HINT

    url = f"{site}/search/{name}" + (f"?q={quote_plus(QUERY[name])}" if name in QUERY else "")
    data = await _navigate(url)
    if name in EXPECTED_CAPTCHA:
        assert data["challenge"] == CAPTCHA, data
        assert data["handoff"]["suggested"] is True and data["handoff"]["reason"] == "captcha"
        assert data["hint"].count(HANDOFF_HINT) == 1
    else:
        assert data["challenge"] is None, (name, data)
        assert "handoff" not in data, (name, data)
        assert HANDOFF_HINT not in (data.get("hint") or "")
