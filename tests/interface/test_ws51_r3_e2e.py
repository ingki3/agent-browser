"""WS-51 R3 E2E — 비밀 값 레지스트리의 실제 출구(실 Chromium + 로컬 페이지, headless).

수용 기준 1~4 를 실제 경로로 단언한다:

* 등록형(1): 값이 든 주소로 **실제 navigate** 한 뒤(깨끗한 페이지로 옮겨 응답의 주소 칸에는 값이 없게) 같은 문장을
  제목(observe → envelope)·error_message·fd2 stderr·logging(%s/%r/exc_text/stack_info/extra)으로 내보낸다 → 원문·
  퍼센트·JSON·HTML 변형 0. 리다이렉트로만 지나간 주소(최종 주소·이동 대상 어디에도 없음)의 값도 0.
* 미등록 인용형(2): `/clean` 제목만으로 `\\"`·JSON 인용·짝 없는 따옴표·긴 인용 → 0.
* 한계(3): 본 적 없는 값의 제목 전용 공백 꼬리는 남는다(고정).
* 의미(4): R2 CONTEXT 6쌍이 제목·error_message 에서 서로 다름.

값은 합성 값. 공개 사이트 없음(127.0.0.1 만).
"""

from __future__ import annotations

import html
import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest

from contracts import ActionResult, ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, envelope_json, install_public_log_filter, tool_name
from interface.server_common import _stderr_line
from security import secret_registry

from test_run_cli import requires_chromium

SECRET = "SYNTHr3E2Evalue"            # 8자 이상 — 이동 대상으로 본 값
REDIR = "SYNTHr3RedirOnly"            # 리다이렉트 중간 주소에만 있던 값
SPECIAL = 'SYN"T&H<é>R3+/=e2e'        # 인코딩 변형 확인용
PAGE = b"<!doctype html><meta charset=utf-8><title>clean</title><button>Go</button>"


def _samples(secret: str) -> dict:
    s = {
        "control_plain": "/a?token=" + secret,
        "control_quoted": '/a?token="' + secret + '"',
        "unmatched_double": '/a?token="' + secret,
        "unmatched_single": "/a?token='" + secret,
        "mismatched_quote": '/a?token="' + secret + "'",
        "backtick_space": "/a?token=`prefix " + secret + "`",
        "curly_double_space": "/a?token=“prefix " + secret + "”",
        "curly_single": "/a?token=‘prefix " + secret + "’",
        "corner_quote": "/a?token=「prefix " + secret + "」",
        "escaped_double": '/a?token=\\"' + secret + '\\"',
        "escaped_single": "/a?token=\\'" + secret + "\\'",
        "escaped_inner": '/a?token="prefix \\"' + secret + '\\""',
        "tab": "/a?token=prefix\t" + secret,
        "nbsp": "/a?token=prefix\u00a0" + secret,
        "fullwidth_space": "/a?token=prefix\u3000" + secret,
        "crlf": "/a?token=prefix\r\n" + secret,
        "quoted_crlf": '/a?token="prefix\r\n' + secret + '"',
        "after_space_quote": '/a?token=prefix "' + secret + '"',
        "empty_space_quote": '/a?token= "' + secret + '"',
        "multiple_secrets": '/a?token="' + secret + '"&session=prefix ' + secret + ';sid="' + secret + '"',
        "html_attribute": '<a href="/a?token=' + secret + '">next</a>',
        "json_url": json.dumps({"url": "/a?token=" + secret}),
        "json_quoted_url": json.dumps({"url": '/a?token="' + secret + '"'}),
        "json_space_url": json.dumps({"url": "/a?token=prefix " + secret}),
        "plain_sentence": "세션 " + secret + " 만료",
    }
    for offset in (1014, 1022, 1023, 1024, 1025, 1030):
        s[f"cap_space_{offset}"] = "/a?token=x " + ("w " * 600)[:offset] + secret
        s[f"cap_quote_{offset}"] = '/a?token="' + ("w " * 600)[:offset] + secret + '"'
    return s


QUOTED_UNREGISTERED = ["escaped_double", "escaped_single", "escaped_inner", "json_quoted_url",
                       "unmatched_double", "unmatched_single"] + [f"cap_quote_{o}" for o in (1014, 1024, 1025, 1030)]


def _forms(secret: str) -> set:
    return {secret, quote(secret, safe=""), json.dumps(secret)[1:-1],
            json.dumps(secret, ensure_ascii=False)[1:-1], html.escape(secret)}


def _leaks(text: str, secret: str) -> list:
    return [f for f in _forms(secret) if f in text]


@pytest.fixture(autouse=True)
def _fresh_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def lab():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            if self.path == "/r":
                self.send_response(302)
                self.send_header("Location", f"/hop?token={REDIR}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.startswith("/hop"):
                self.send_response(302)
                self.send_header("Location", "/clean")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    finally:
        srv.shutdown()
        srv.server_close()


def _log_all(text: str) -> str:
    """같은 문장을 %s·%r·exc_text(예외)·stack_info(앞선 팩토리가 채움)·extra 로 내보낸 로그 전체."""
    lg = logging.getLogger("ws51.r3.e2e")
    prior = (lg.level, lg.propagate)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter("%(message)s | %(where)s"))
    lg.addHandler(handler)
    original = logging.getLogRecordFactory()

    def pre(*args, **kwargs):
        rec = original(*args, **kwargs)
        if rec.name == "ws51.r3.e2e" and rec.msg == "stack":
            rec.stack_info = f"Stack (most recent call last):\n  이동 {text}"
        return rec

    logging.setLogRecordFactory(pre)
    restore = install_public_log_filter()
    try:
        lg.warning("s %s", text, extra={"where": "s"})
        lg.warning("r %r", text, extra={"where": "r"})
        try:
            raise RuntimeError(f"Page.goto: net::ERR navigating to \"{text}\"")
        except RuntimeError:
            lg.exception("exc", extra={"where": "exc"})
        lg.warning("stack", extra={"where": "stack"})
        lg.warning("extra", extra={"where": text})
    finally:
        restore()
        logging.setLogRecordFactory(original)
        lg.removeHandler(handler)
        lg.setLevel(prior[0])
        lg.propagate = prior[1]
    return out.getvalue()


def _error_wire(text: str) -> str:
    return envelope_json(ActionResult(
        action=ActionType.NAVIGATE, success=False, error_code=ErrorCode.NAVIGATE_TIMEOUT,
        error_message="goto failed: " + text, snapshot_epoch=1, current_url="http://127.0.0.1:1/clean",
        tab_id="tab-1", retry_safe=True))


async def _title_wire(s, text: str) -> str:
    await s._page.evaluate("(t) => { document.title = t; }", text)
    raw = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
    assert raw.success
    return envelope_json(raw)


async def _emit_everywhere(s, text: str, capfd) -> dict:
    title = await _title_wire(s, text)
    capfd.readouterr()
    _stderr_line(text)
    err = capfd.readouterr().err
    assert err.strip(), "stderr 줄은 남아야 한다"
    logs = _log_all(text)
    assert logs.count(" | ") == 5, logs  # 로그 5줄 모두 남는다
    return {"title": title, "error": _error_wire(text), "stderr": err, "log": logs}


@requires_chromium
async def test_registered_values_never_leak_on_any_exit(lab, capfd):
    """수용 1: 도구가 `?token=SECRET` 으로 이동한 뒤 R2 표본 전부 — 4 출구 × 변형 0."""
    origin, _ = lab
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        assert (await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{origin}/seen?token={SECRET}"})).success
        assert (await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{origin}/clean"})).success
        assert SECRET not in s._page.url  # 응답의 주소 칸에는 값이 없다(등록은 이동 경로에서만)
        leaks = {}
        for name, text in _samples(SECRET).items():
            for exit_name, out in (await _emit_everywhere(s, text, capfd)).items():
                found = _leaks(out, SECRET)
                if found:
                    leaks[f"{name}/{exit_name}"] = found
        assert leaks == {}, leaks


@requires_chromium
async def test_real_goto_error_message_masks_target(lab):
    """실제 Playwright 이동 실패 문구(`navigating to "URL"`)에 실린 이동 대상의 값 — 공백 꼬리 포함."""
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        url = "http://127.0.0.1:1/a?token=" + quote(f"prefix {SECRET}", safe="")
        raw = await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": url, "timeout_ms": 5000})
        assert not raw.success
        assert SECRET in (raw.error_message or "")  # 조건 성립: 원 오류 문구에 값이 있다
        wire = envelope_json(raw)
        assert _leaks(wire, SECRET) == [], wire[:800]
        assert "[REDACTED]" in json.loads(wire)["error_message"]


@requires_chromium
async def test_encoded_variants_of_seen_value(lab, capfd):
    """특수 문자 값: 이동 주소의 퍼센트 꼴을 보고, 문장에는 원문·퍼센트·JSON·HTML 꼴로 나와도 0."""
    origin, _ = lab
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        assert (await s.call_tool(tool_name(ActionType.NAVIGATE),
                                  {"url": f"{origin}/seen?token=" + quote(SPECIAL, safe="")})).success
        assert (await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{origin}/clean"})).success
        for form in sorted(_forms(SPECIAL)):
            outs = await _emit_everywhere(s, f"세션 {form} 만료", capfd)
            for exit_name, out in outs.items():
                assert _leaks(out, SPECIAL) == [], (form, exit_name, out[:600])


@requires_chromium
async def test_redirect_only_value_is_registered(lab, capfd):
    """리다이렉트 중간 주소에만 있던 값 — 이동 대상(/r)에도 최종 주소(/clean)에도 없다."""
    origin, hits = lab
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        nav = await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{origin}/r"})
        assert nav.success and s._page.url.endswith("/clean")
        assert any(h.startswith("/hop?token=") for h in hits)  # 조건 성립: 리다이렉트를 실제로 거쳤다
        assert REDIR not in envelope_json(nav)
        for text in (f"Search: {REDIR} 다음", f"/x?q=1 prefix {REDIR} 끝", json.dumps({"t": REDIR})):
            outs = await _emit_everywhere(s, text, capfd)
            for exit_name, out in outs.items():
                assert REDIR not in out, (text, exit_name, out[:600])
            assert "다음" in outs["title"] or "끝" in outs["title"] or "{" in text  # 뒤 문장은 남는다


@requires_chromium
async def test_navigate_and_tab_create_targets_registered_before_goto(lab, monkeypatch):
    """등록 지점 (b) 단독: 컨텍스트 감시(c)를 끄고도 navigate·tab create 대상의 값은 goto 전에 등록된다 —
    이동이 실패해도(포트 1) 그 값이 뒤의 제목 문장에서 가려진다."""
    monkeypatch.setattr(secret_registry, "watch_browser_context", lambda context: None)
    origin, _ = lab
    nav_secret, tab_secret = "SYNTHr3NavOnly01", "SYNTHr3TabOnly01"
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        failed = await s.call_tool(tool_name(ActionType.NAVIGATE),
                                   {"url": f"http://127.0.0.1:1/a?token={nav_secret}", "timeout_ms": 5000})
        assert not failed.success
        created = await s.call_tool(tool_name(ActionType.TAB_CONTROL),
                                    {"command": "create", "url": f"{origin}/seen?sid={tab_secret}"})
        assert created.success
        page = s._dispatcher.ctx.page
        for secret in (nav_secret, tab_secret):
            await page.evaluate("(t) => { document.title = t; }", f"세션 {secret} 만료")
            wire = envelope_json(await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {}))
            assert secret not in wire, (secret, wire[:600])
            assert "세션 [REDACTED] 만료" in wire


@requires_chromium
async def test_unregistered_quoted_titles_masked(lab):
    """수용 2: 레지스트리가 빈 채 /clean 제목만 — 인용형은 URL 문법 계층이 가린다."""
    origin, _ = lab
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        assert (await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{origin}/clean"})).success
        assert len(secret_registry.REGISTRY) == 0
        samples = _samples(SECRET)
        for name in QUOTED_UNREGISTERED:
            wire = await _title_wire(s, samples[name])
            assert SECRET not in wire, (name, wire[:600])
            assert SECRET not in _error_wire(samples[name]), name


@requires_chromium
async def test_limit_unregistered_title_only_space_tail(lab):
    """수용 3(한계 고정): 도구가 본 적 없는 값이 제목에만 공백 뒤로 이어지면 원문이 남는다."""
    origin, _ = lab
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        assert (await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{origin}/clean"})).success
        title = json.loads(await _title_wire(s, f"/a?token=prefix {SECRET}"))["data"]["observation"]["title"]
        assert title == f"/a?token=[REDACTED] {SECRET}"


CONTEXT = [
    'Request /a?token=abc expired, please retry',
    'Request /a?token=abc succeeded, continue checkout',
    'Request /a?token=abc failed: connection refused',
    'Request /a?token=abc failed: certificate expired',
    'Request /a?token=abc completed: payment confirmed',
    'Request /a?token=abc failed: payment declined',
    'Request /a?token=abc HTTP 401 Unauthorized',
    'Request /a?token=abc HTTP 200 OK',
    'Search: /a?token=abc Booking confirmed',
    'Search: /a?token=abc Booking cancelled',
    '이동 /a?token=abc 예약에 성공했습니다',
    '이동 /a?token=abc 예약에 실패했습니다',
]


@requires_chromium
@pytest.mark.parametrize("value,seen", [("abc", False), ("SYNTHmeaningE2E1", True)])
async def test_meaning_pairs_distinct_on_real_title_and_error(lab, value, seen):
    """수용 4: R2 CONTEXT 6쌍(meaning_gate) — 제목·error_message 에서 서로 다르고 값 뒤 문구는 원문 그대로."""
    origin, _ = lab
    async with BrowserMCPServer(headless=True, nav_settle=False) as s:
        if seen:
            assert (await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{origin}/seen?token={value}"})).success
        assert (await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": f"{origin}/clean"})).success
        rows = []
        for src in CONTEXT:
            text = src.replace("abc", value)
            title = json.loads(await _title_wire(s, text))["data"]["observation"]["title"]
            error = json.loads(_error_wire(text))["error_message"]
            for out in (title, error):
                assert value not in out
                assert out.split("[REDACTED]", 1)[1] == text.split(value, 1)[1]
            rows.append((title, error))
        for a in range(0, len(rows), 2):
            assert rows[a][0] != rows[a + 1][0] and rows[a][1] != rows[a + 1][1], rows[a]
