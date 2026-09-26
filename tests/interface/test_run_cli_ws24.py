"""`agent-browser run` 결과 보강 (WS-24).

F1-c: 실패한 스텝 기록에 오류 메시지 앞부분을 붙인다 — "scroll -> FAIL E_PAGE_CRASHED"
      만으로는 원인을 추적할 수 없었다(G마켓 실측). type_text 는 붙이지 않는다
      (예외 메시지에 입력값·치환된 비밀값이 섞일 수 있다).
F2:   last_http_status — 실행 중 마지막 메인 프레임 문서 응답의 상태. 첫 이동만
      기록하던 http_status 는 검색 페이지에서 403 으로 막혀도 200 이었다.
로컬 서버 + 가짜 LLM 만 쓴다(외부 사이트·과금 없음).
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.loop import StepOutcome
from agent.policy import Decision
from contracts import ActionResult, ActionType, ErrorCode
from interface import cli, run_cli

from test_run_cli import _cfg, fake_llm, requires_chromium  # noqa: F401 - 픽스처 재사용

HOME = """<!doctype html><meta charset=utf-8><title>쇼핑</title>
<input aria-label='검색어' id=q><button>검색</button><p>상품 목록 사과 3000원</p>"""
# 첫 화면은 200, 곧바로 검색 페이지로 옮겨 가는데 그 페이지가 403 차단 화면(G마켓 실측 흐름).
HOME_THEN_BLOCK = """<!doctype html><meta charset=utf-8><title>쇼핑</title><p>잠시만요</p>
<script>setTimeout(() => location.href = '/search?q=k', 50)</script>"""
BLOCK = "<!doctype html><meta charset=utf-8><title>막힘</title><p>요청이 거부되었습니다.</p>"
# 하위 프레임의 403 은 메인 문서 상태가 아니다.
HOME_WITH_BAD_IFRAME = HOME + "<iframe src='/search?q=frame' width=10 height=10></iframe>"
# 새 탭(R1): 첫 화면은 200 이고, 곧바로 새 탭이 열린다. 새 탭의 첫 문서 응답은
# 프레임이 생기기 전에 요청돼 response.frame 이 예외를 낸다(실측).
HOME_POPUP_LINK = HOME + """<a id=pop href='/search?q=tab' target=_blank>새 창</a>
<script>setTimeout(() => document.getElementById('pop').click(), 50)</script>"""
HOME_POPUP_OPEN = HOME + "<script>setTimeout(() => window.open('/search?q=win'), 50)</script>"
HOME_POPUP_FRAMED = HOME + "<script>setTimeout(() => window.open('/framed'), 50)</script>"
# 새 탭 문서는 200, 그 안의 iframe 은 403 — iframe 은 여전히 무시해야 한다.
FRAMED = """<!doctype html><meta charset=utf-8><title>새 탭</title><p>상품 목록 사과 3000원</p>
<iframe src='/search?q=inner' width=10 height=10></iframe>"""


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status, body = 200, HOME
            if self.path.startswith("/search"):
                status, body = 403, BLOCK
            elif self.path.startswith("/redirect-home"):
                body = HOME_THEN_BLOCK
            elif self.path.startswith("/iframe-home"):
                body = HOME_WITH_BAD_IFRAME
            elif self.path.startswith("/popup-link"):
                body = HOME_POPUP_LINK
            elif self.path.startswith("/popup-open"):
                body = HOME_POPUP_OPEN
            elif self.path.startswith("/popup-framed"):
                body = HOME_POPUP_FRAMED
            elif self.path.startswith("/framed"):
                body = FRAMED
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- F1-c 스텝 기록


def _outcome(action: str, code: ErrorCode, message: str, **dkw) -> StepOutcome:
    result = ActionResult(
        success=False, action=ActionType(action), current_url="http://x/",
        snapshot_epoch=0, tab_id="t", retry_safe=True,
        error_code=code, error_message=message,
    )
    return StepOutcome(step=1, decision=Decision(action=action, **dkw), result=result)


def test_step_line_appends_error_message_head():
    msg = ("Error: Page.evaluate: Execution context was destroyed, most likely because of "
           "a navigation\n   and a very long tail " + "x" * 200)
    line = run_cli.step_line(_outcome("scroll", ErrorCode.PAGE_CRASHED, msg))
    assert line.startswith("scroll -> FAIL E_PAGE_CRASHED")
    assert "Execution context was destroyed" in line
    assert "\n" not in line, "한 줄로 접는다"
    tail = line.split("E_PAGE_CRASHED", 1)[1]
    assert len(tail) <= run_cli.STEP_ERROR_CHARS + 6, "앞부분만 붙인다"


def test_step_line_folds_newlines_within_head():
    """개행이 앞 80자 안에 있어도 한 줄로 접는다(R1-2)."""
    msg = "Error: Page.evaluate:\n  Execution context\r\n\twas destroyed"
    line = run_cli.step_line(_outcome("scroll", ErrorCode.PAGE_CRASHED, msg))
    assert "\n" not in line and "\r" not in line and "\t" not in line
    assert line.endswith("— Error: Page.evaluate: Execution context was destroyed"), line


def test_step_line_hides_url_query_and_fragment():
    """오류 메시지 안 URL 의 쿼리·fragment 는 가린다(R1-3) — 콘솔 요약에도 나온다."""
    msg = ("Page.goto: net::ERR_UNSAFE_PORT at http://127.0.0.1:9/login?user=alice&token=QUE"
           "#frag")
    line = run_cli.step_line(_outcome("navigate", ErrorCode.NAVIGATE_TIMEOUT, msg,
                                      url="http://127.0.0.1:9/login"))
    assert "alice" not in line and "token" not in line and "frag" not in line, line
    assert line.endswith("at http://127.0.0.1:9/login?…"), line
    # fragment 만 있어도 가리고, http(s) 가 아닌 토큰은 건드리지 않는다.
    line2 = run_cli.step_line(_outcome("click", ErrorCode.TIMEOUT,
                                       "at https://ex.com/a#tab=secret, sel a?b=1",
                                       element_id="@e1"))
    assert line2.endswith("at https://ex.com/a?… sel a?b=1"), line2


def test_step_line_hides_query_before_truncation():
    """쿼리를 먼저 가린 뒤 자른다 — 80자 경계에 걸린 쿼리 일부도 남지 않는다."""
    msg = "x" * 60 + " http://h/p?secret=" + "S" * 40
    line = run_cli.step_line(_outcome("click", ErrorCode.TIMEOUT, msg, element_id="@e1"))
    assert "secret" not in line and "SS" not in line, line
    # 먼저 가리므로 긴 쿼리 뒤의 원인 문구가 80자 안으로 들어온다.
    msg2 = "Page.goto: at http://h/p?" + "q=" + "Q" * 120 + " net::ERR_ABORTED"
    line2 = run_cli.step_line(_outcome("navigate", ErrorCode.NAVIGATE_TIMEOUT, msg2, url="http://h/p"))
    assert line2.endswith("at http://h/p?… net::ERR_ABORTED"), line2


def test_step_line_never_appends_type_text_error():
    """type_text 실패 메시지에는 입력값(치환된 비밀값 포함)이 섞일 수 있다 — 붙이지 않는다."""
    o = _outcome("type_text", ErrorCode.ELEMENT_NOT_INTERACTABLE,
                 "Locator.fill: 실패 - fill(\"hunter2-secret\")",
                 element_id="@e1", text="LOGIN_PASSWORD")
    line = run_cli.step_line(o)
    assert "hunter2" not in line and "fill(" not in line
    assert line == o.summary()


def test_step_line_success_is_unchanged():
    ok = StepOutcome(step=1, decision=Decision(action="scroll"), result=ActionResult(
        success=True, action=ActionType.SCROLL, current_url="http://x/", snapshot_epoch=0,
        tab_id="t", retry_safe=True))
    assert run_cli.step_line(ok) == ok.summary() == "scroll -> OK"


@requires_chromium
def test_run_steps_carry_failure_message(server, fake_llm, tmp_path):
    """실제 루프·디스패처: 없는 키 입력이 실패하면 steps 에 그 이유가 남는다."""
    fake_llm.bodies = ['{"action":"press_key","key":"NoSuchKeyZ","reason":"눌러 본다"}',
                       '{"action":"give_up","reason":"그만"}']
    out = tmp_path / "r.json"
    assert cli.main(["run", "--url", server + "/", "--goal", "사과 가격", "--max-steps", "3",
                     "--out", str(out)]) == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    first = rec["steps"][0]
    assert first.startswith("press_key -> FAIL E_KEY_PRESS_FAILED"), first
    # 요약의 "(키 NoSuchKeyZ)" 말고 디스패처 오류 메시지("… (입력값: …)")가 붙어야 한다.
    assert " — " in first and "입력값" in first, first


# ---------------------------------------------------------------- F2 last_http_status


@requires_chromium
def test_last_http_status_tracks_later_main_document(server, fake_llm, tmp_path):
    """첫 이동은 200, 이후 메인 문서가 403 이면 http_status=200 · last_http_status=403."""
    out = tmp_path / "r.json"
    assert cli.main(["run", "--url", server + "/redirect-home", "--goal", "사과 가격",
                     "--max-steps", "3", "--out", str(out)]) == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["http_status"] == 200, "첫 이동 상태는 호환을 위해 그대로"
    assert rec["last_http_status"] == 403, rec
    assert "/search" in rec["final_url"]


@requires_chromium
def test_last_http_status_ignores_subframe_documents(server, fake_llm, tmp_path):
    out = tmp_path / "r.json"
    assert cli.main(["run", "--url", server + "/iframe-home", "--goal", "사과 가격",
                     "--max-steps", "3", "--out", str(out)]) == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["completed"] is True, rec["terminal_reason"]
    assert rec["http_status"] == 200 and rec["last_http_status"] == 200


@requires_chromium
def test_last_http_status_normal_run_equals_first(server, fake_llm, tmp_path):
    out = tmp_path / "r.json"
    assert cli.main(["run", "--url", server + "/", "--goal", "사과 가격",
                     "--max-steps", "3", "--out", str(out)]) == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["http_status"] == 200 and rec["last_http_status"] == 200


def _run_rec(server, path, tmp_path):
    out = tmp_path / "r.json"
    assert cli.main(["run", "--url", server + path, "--goal", "사과 가격",
                     "--max-steps", "3", "--out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


@requires_chromium
def test_last_http_status_catches_target_blank_new_tab(server, fake_llm, tmp_path):
    """target=_blank 로 연 새 탭의 첫 문서가 403 이면 last_http_status=403 (R1-1)."""
    rec = _run_rec(server, "/popup-link", tmp_path)
    assert rec["http_status"] == 200
    assert rec["last_http_status"] == 403, rec


@requires_chromium
def test_last_http_status_catches_window_open_new_tab(server, fake_llm, tmp_path):
    """window.open 으로 연 새 탭의 첫 문서 403 도 잡는다 (R1-1)."""
    rec = _run_rec(server, "/popup-open", tmp_path)
    assert rec["http_status"] == 200
    assert rec["last_http_status"] == 403, rec


@requires_chromium
def test_last_http_status_new_tab_ignores_its_iframe(server, fake_llm, tmp_path):
    """새 탭 문서는 200, 그 안 iframe 403 은 무시 — 결과는 200 (R1-1)."""
    rec = _run_rec(server, "/popup-framed", tmp_path)
    assert rec["http_status"] == 200
    assert rec["last_http_status"] == 200, rec


def test_last_http_status_key_present_on_error_path(monkeypatch):
    """루프 생성 전에 죽어도 last_http_status 키는 있다(None)."""
    import agent

    class Stop(Exception):
        pass

    def fake_loop(**kw):
        raise Stop

    class Page:
        url = "about:blank"

        async def goto(self, *a, **k):
            return None

        async def wait_for_timeout(self, ms):
            return None

    class Ctx:
        async def new_cdp_session(self, page):
            return object()

        async def close(self):
            return None

    class Br:
        async def close(self):
            return None

    async def fake_open(pw, args, record):
        return Br(), Ctx(), Page(), None

    async def body(page):
        return ""

    monkeypatch.setattr(agent, "AgentLoop", fake_loop)
    monkeypatch.setattr(run_cli, "_load_config", _cfg)
    monkeypatch.setattr(run_cli, "_open_browser", fake_open)
    monkeypatch.setattr(run_cli, "_read_body_text", body)
    args = cli._build_parser().parse_args(["run", "--url", "http://x/", "--goal", "g"])
    rec = asyncio.run(run_cli.run_goal(args))
    assert rec["error"].startswith("Stop")
    assert "last_http_status" in rec and rec["last_http_status"] is None
