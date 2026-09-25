"""`agent-browser run` — 목표 1개 실행 진입점 (WS-22).

로컬 서버 페이지 + 가짜 LLM 으로만 돈다(외부 사이트·과금 없음).
  - 결과 JSON 키가 모두 채워지는지,
  - --out 파일 권한이 0600 인지(페이지 본문이 들어간다),
  - --handoff 가 신호 파일로 재개되는지(차단 화면 → 사람 해결 → 신호 → 계속),
  - 신호가 없으면 대기 상한 후 E_CAPTCHA_DETECTED 로 끝나는지.
--user-chrome 경로는 AB_USER_CHROME_TEST=1 일 때만 돈다(실제 Chrome 창이 뜸).
캡차를 풀거나 차단을 우회하지 않는다 — 사람이 해결했다는 신호만 기다린다.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent import loop as loop_mod
from contracts import ErrorCode
from interface import cli, run_cli
from llm import LLMConfig
from llm.client import LLMResponse

RESULT_KEYS = {
    "goal", "start_url", "final_url", "completed", "terminal_reason", "step_count",
    "steps", "decided_by", "decided_by_counts", "usd", "tokens", "challenge",
    "handoffs", "human_wait_s", "http_status", "final_answer", "final_page_text",
}


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

NORMAL = """<!doctype html><meta charset=utf-8><title>검색</title>
<input aria-label='검색어' id=q><button>검색</button><p>상품 목록 사과 3000원</p>"""

# 사람이 창에서 해결하면 페이지가 바뀌는 상황을 흉내 낸다: 차단 화면은 /state 를
# 폴링하다가 서버 상태가 풀리면(=사람이 해결) 정상 페이지로 한 번 이동한다.
BLOCKED = """<!doctype html><meta charset=utf-8><title>네이버쇼핑</title>
<h2>쇼핑 서비스 접속이 일시적으로 제한되었습니다.</h2><a href='#x'>고객센터</a>
<script>setInterval(async () => {
  const r = await fetch('/state'); if ((await r.text()) === 'ok') location.replace('/');
}, 200)</script>"""


class FakeChat:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def close(self):
        return None

    async def complete(self, messages, **kw):
        self.calls += 1
        return LLMResponse(content=self.bodies.pop(0), model="m",
                           prompt_tokens=10, completion_tokens=5, cost_usd=0.001)


def _cfg():
    return LLMConfig(api_key="sk-or-v1-" + "k" * 40, model="z-ai/glm-5.3-flash",
                     base_url="https://openrouter.ai/api/v1", decider="llm")


@pytest.fixture
def server():
    state = {"blocked": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/state":
                body = (b"blocked" if state["blocked"] else b"ok")
            else:
                body = (BLOCKED if state["blocked"] else NORMAL).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/", state
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def fake_llm(monkeypatch):
    chat = FakeChat(['{"action":"finish","reason":"상품 목록에 사과 3000원이 보인다"}'])
    monkeypatch.setattr(loop_mod, "OpenRouterClient", lambda *a, **k: chat)
    monkeypatch.setattr(run_cli, "_load_config", _cfg)
    return chat


# ---------------------------------------------------------------- 인자


def test_run_subcommand_parses_all_options(tmp_path):
    args = cli._build_parser().parse_args([
        "run", "--url", "http://127.0.0.1:1/", "--goal", "g", "--max-steps", "3",
        "--out", str(tmp_path / "o.json"), "--human", "--user-chrome",
        "--chrome-profile", str(tmp_path / "p"), "--keep-open", "--handoff",
        "--handoff-wait", "5", "--handoff-file", str(tmp_path / "d"),
    ])
    assert args.command == "run"
    assert (args.url, args.goal, args.max_steps) == ("http://127.0.0.1:1/", "g", 3)
    assert args.human and args.user_chrome and args.keep_open and args.handoff
    assert args.handoff_wait == 5.0
    assert Path(args.handoff_file) == tmp_path / "d"


def test_run_defaults():
    args = cli._build_parser().parse_args(["run", "--url", "u", "--goal", "g"])
    assert args.max_steps == 15 and args.out == "" and not args.handoff
    assert args.handoff_wait == 300
    assert Path(args.handoff_file) == Path.home() / ".agent-browser" / "handoff.done"


def test_chrome_options_require_user_chrome(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--url", "u", "--goal", "g", "--keep-open"])


def test_write_result_is_0600_even_if_file_existed(tmp_path):
    out = tmp_path / "r.json"
    out.write_text("old")
    os.chmod(out, 0o644)
    run_cli.write_result(out, {"a": "본문"})
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert json.loads(out.read_text(encoding="utf-8")) == {"a": "본문"}


# ---------------------------------------------------------------- 실행 (로컬)


@requires_chromium
def test_run_fills_all_keys_and_out_is_0600(server, fake_llm, tmp_path, capsys):
    url, _ = server
    out = tmp_path / "result.json"
    code = cli.main(["run", "--url", url, "--goal", "사과 가격을 알려 줘",
                     "--max-steps", "3", "--out", str(out)])
    assert code == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    rec = json.loads(out.read_text(encoding="utf-8"))
    missing = RESULT_KEYS - rec.keys()
    assert not missing, f"빠진 키: {missing}"
    assert rec["goal"] == "사과 가격을 알려 줘" and rec["start_url"] == url
    assert rec["completed"] is True, rec["terminal_reason"]
    assert rec["http_status"] == 200
    assert rec["step_count"] == 1 and rec["decided_by"] == ["llm"]
    assert rec["decided_by_counts"] == {"llm": 1}
    assert "사과 3000원" in rec["final_answer"]
    assert "사과 3000원" in rec["final_page_text"]
    assert rec["handoffs"] == [] and rec["human_wait_s"] == 0.0
    assert rec["challenge"] == "" or rec["challenge"] is None
    # 가짜 LLM 은 BudgetGuard 에 기록하지 않는다(실제 클라이언트가 기록) — 형식만 본다.
    assert isinstance(rec["usd"], (int, float)) and isinstance(rec["tokens"], int)
    printed = json.loads(capsys.readouterr().out)
    assert "final_page_text" not in printed and printed["completed"] is True


@requires_chromium
def test_final_page_text_truncated_to_6000(server, fake_llm, tmp_path, monkeypatch):
    url, _ = server
    big = "가" * 9000
    monkeypatch.setattr(
        run_cli, "_read_body_text",
        lambda page: asyncio.sleep(0, result=big))
    out = tmp_path / "r.json"
    assert cli.main(["run", "--url", url, "--goal", "g", "--out", str(out)]) == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))["final_page_text"]) == 6000


@requires_chromium
def test_handoff_resumes_on_signal_file(server, fake_llm, tmp_path):
    url, state = server
    state["blocked"] = True
    done = tmp_path / "sig" / "handoff.done"
    out = tmp_path / "r.json"

    def human():
        # 사람이 창에서 해결(서버가 정상 페이지로) → 신호 파일
        import time
        time.sleep(1.5)
        state["blocked"] = False
        time.sleep(1.0)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.touch()

    th = threading.Thread(target=human, daemon=True)
    th.start()
    code = cli.main(["run", "--url", url, "--goal", "사과 가격을 알려 줘", "--out", str(out),
                     "--handoff", "--handoff-wait", "20", "--handoff-file", str(done)])
    th.join(5)
    assert code == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["completed"] is True, rec["terminal_reason"]
    assert rec["challenge"] == "blocked"
    assert len(rec["handoffs"]) == 1
    h = rec["handoffs"][0]
    assert h["kind"] == "blocked" and h["resolved_signal"] is True and h["via"] == "file"
    assert rec["human_wait_s"] > 0
    assert fake_llm.calls == 1
    assert not done.exists(), "신호 파일은 쓰고 나면 지운다"


@requires_chromium
def test_handoff_times_out_without_signal(server, fake_llm, tmp_path):
    url, state = server
    state["blocked"] = True
    out = tmp_path / "r.json"
    code = cli.main(["run", "--url", url, "--goal", "g", "--out", str(out), "--handoff",
                     "--handoff-wait", "1", "--handoff-file", str(tmp_path / "none.done")])
    assert code == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["completed"] is False
    assert rec["terminal_reason"].startswith(ErrorCode.CAPTCHA_DETECTED.value)
    assert rec["handoffs"][0]["resolved_signal"] is False
    assert fake_llm.calls == 0, "차단 화면에서 LLM 을 부르지 않는다"


def test_handoff_wait_resumes_on_enter_when_tty(tmp_path):
    """표준입력이 TTY 면 Enter 로도 재개한다(파일과 먼저 오는 것)."""
    r, w = os.pipe()

    class TTYReader:
        def __init__(self, fd):
            self._f = os.fdopen(fd, "r")

        def isatty(self):
            return True

        def fileno(self):
            return self._f.fileno()

        def readline(self):
            return self._f.readline()

    reader = TTYReader(r)

    async def go():
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, os.write, w, b"\n")
        return await run_cli.wait_for_human(tmp_path / "never.done", 5, stdin=reader)

    try:
        assert asyncio.run(go()) == "enter"
    finally:
        os.close(w)
        reader._f.close()


def test_handoff_wait_ignores_non_tty_stdin(tmp_path):
    r, w = os.pipe()

    class Pipe:
        def __init__(self, fd):
            self._f = os.fdopen(fd, "r")

        def isatty(self):
            return False

        def fileno(self):
            return self._f.fileno()

        def readline(self):
            return self._f.readline()

    reader = Pipe(r)
    os.write(w, b"\n")

    try:
        assert asyncio.run(run_cli.wait_for_human(tmp_path / "x.done", 0.6, stdin=reader)) is None
    finally:
        os.close(w)
        reader._f.close()


# ---------------------------------------------------------------- 본인 Chrome (opt-in)

_USER_CHROME = os.environ.get("AB_USER_CHROME_TEST") == "1"


@pytest.mark.skipif(not _USER_CHROME, reason="AB_USER_CHROME_TEST=1 + 설치된 Chrome 필요(창이 뜸)")
def test_run_user_chrome_closes_our_chrome(server, fake_llm, tmp_path):
    from browser.user_chrome import find_chrome
    if find_chrome() is None:
        pytest.skip("Chrome 없음")
    url, _ = server
    out = tmp_path / "r.json"
    prof = tmp_path / "prof"
    code = cli.main(["run", "--url", url, "--goal", "사과 가격을 알려 줘", "--out", str(out),
                     "--user-chrome", "--chrome-profile", str(prof)])
    assert code == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["completed"] is True, rec["terminal_reason"]
    assert RESULT_KEYS <= rec.keys()
    assert rec["user_chrome"]["port"] > 0
    left = subprocess.run(["pgrep", "-f", str(prof)], capture_output=True, text=True).stdout
    assert left.strip() == "", "우리가 띄운 Chrome 이 남아 있음"
