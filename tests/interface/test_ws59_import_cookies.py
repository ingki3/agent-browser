"""WS-59 — `agent-browser profile import-cookies` (사람 CLI) 통합 테스트.

합성 Chrome Cookies DB(테스트용 키) → 사이트 필터 → 임시 봇 프로필에 실제 Chromium 으로 주입 → 로컬 http 요청에
쿠키가 실리는지 확인한다. 실제 일상 Chrome·키체인은 건드리지 않는다(키체인 함수는 바꿔 끼운다).
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "browser"))

from browser import chrome_cookies as cc  # noqa: E402
from browser import login_ledger as ll  # noqa: E402
from browser import serve_profile as sp  # noqa: E402
from interface import cli  # noqa: E402
from test_run_cli import requires_chromium  # noqa: E402
from ws59_chrome_fixture import TEST_PASSWORD, derive_test_key, make_db  # noqa: E402

V_SID = "val-SID-9f8e7d6c5b4a"
V_DOM = "val-DOM-1a2b3c4d5e6f"
V_PREF = "val-PREF-0011223344"
V_OLD = "val-OLD-plaintext-77"
V_EVIL = "val-EVIL-shouldnotgo"
V_EVIL2 = "val-EVIL2-shouldnot"
V_OTHER = "val-OTHER-site-0000"
V_HOSTBAD = "val-HOSTBAD-rejected1"
ALL_VALUES = (V_SID, V_DOM, V_PREF, V_OLD, V_EVIL, V_EVIL2, V_OTHER, V_HOSTBAD)


@pytest.fixture(autouse=True)
def _clear_secret_registry():
    from security import secret_registry

    yield
    secret_registry.clear()


@pytest.fixture
def chrome_dir(tmp_path):
    root = tmp_path / "Chrome"
    make_db(root / "Default" / "Network" / "Cookies", 24, [
        {"host": ".example.test", "name": "SID", "value": V_SID, "secure": True, "httponly": True, "samesite": 1},
        {"host": ".example.test", "name": "dom", "value": V_DOM},
        {"host": "a.example.test", "name": "pref", "value": V_PREF, "samesite": 2},
        {"host": ".example.test", "name": "old", "value": V_OLD, "enc": "plain"},
        {"host": ".example.test", "name": "v20cookie", "enc": b"v20" + b"\x00" * 32},
        {"host": ".example.test", "name": "sess", "value": V_DOM, "expires": None},
        {"host": ".evil-example.test", "name": "EVIL", "value": V_EVIL},
        {"host": "example.test.evil.zz", "name": "EVIL2", "value": V_EVIL2},
        {"host": ".other.test", "name": "OTHER", "value": V_OTHER},
        # __Host- 는 호스트 전용이어야 한다 — Chromium 이 거부(묶음 전체 실패 → 하나씩 넣기로 이것만 빠짐)
        {"host": ".example.test", "name": "__Host-bad", "value": V_HOSTBAD, "secure": True},
    ])
    return root


@pytest.fixture
def keychain(monkeypatch):
    calls = []

    def fake() -> bytes:
        calls.append(1)
        return TEST_PASSWORD

    monkeypatch.setattr(cc, "keychain_password", fake)
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: pytest.fail("실제 security 를 부르면 안 됨"))
    return calls


def _run(argv):
    return cli.main(["profile", "import-cookies", *argv])


def _assert_no_secrets(text: str) -> None:
    for v in ALL_VALUES + (TEST_PASSWORD.decode(), derive_test_key().hex()):
        assert v not in text, f"비밀 값이 출력에 있다: {v[:8]}…"


def test_dry_run_lists_names_and_writes_nothing(chrome_dir, keychain, capsys, caplog,
                                                _isolated_profile_root):
    caplog.set_level("DEBUG")
    code = _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(chrome_dir), "--dry-run"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "example.test: 가져올 쿠키 5개" in out
    for name in ("SID", "dom", "pref", "old", "__Host-bad"):
        assert name in out
    assert "EVIL" not in out and "OTHER" not in out
    assert "dry-run" in out.lower() or "쓰지 않" in out
    _assert_no_secrets(out + err + caplog.text)
    assert not (_isolated_profile_root / "serve-t1").exists()
    assert keychain == [1]


def test_refuses_public_suffix_and_subdomain_sites_before_reading(chrome_dir, keychain, capsys):
    for site in ("github.io", "co.uk", "www.example.test", "127.0.0.1"):
        code = _run(["--profile", "t1", "--site", site, "--chrome-dir", str(chrome_dir), "--dry-run"])
        assert code == 2, site
    assert keychain == []
    _assert_no_secrets("".join(capsys.readouterr()))


def test_refuses_when_serve_holds_the_profile(chrome_dir, keychain, capsys, _isolated_profile_root):
    lock = sp.acquire("t1", server_id="srv-ws59")
    try:
        code = _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(chrome_dir)])
    finally:
        lock.release()
    out, err = capsys.readouterr()
    assert code == 2
    assert "쓰는 중" in err and "srv-ws59" in err
    assert keychain == []  # 일상 Chrome 을 읽기(키체인 창) 전에 거부
    assert not (_isolated_profile_root / "serve-t1" / ll.LEDGER_FILE).exists()
    _assert_no_secrets(out + err)


def test_missing_chrome_db_is_a_clear_error(tmp_path, keychain, capsys):
    code = _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(tmp_path / "nope")])
    out, err = capsys.readouterr()
    assert code == 2 and "Cookies" in err
    assert keychain == []


def test_keychain_denied_is_a_clear_error(chrome_dir, monkeypatch, capsys):
    def denied() -> bytes:
        raise cc.CookieImportError("키체인에서 'Chrome Safe Storage' 비밀을 읽지 못했습니다.")

    monkeypatch.setattr(cc, "keychain_password", denied)
    code = _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(chrome_dir)])
    out, err = capsys.readouterr()
    assert code == 2 and "키체인" in err


def test_write_failure_paths_never_leak_values(chrome_dir, keychain, monkeypatch, capsys, caplog,
                                               _isolated_profile_root):
    caplog.set_level("DEBUG")

    class FakeCtx:
        async def add_cookies(self, cookies):
            raise RuntimeError(f"Invalid cookie fields: {json.dumps(cookies)} {V_SID}")

        async def cookies(self):
            return []

    @asynccontextmanager
    async def fake_ctx(path):
        yield FakeCtx()

    monkeypatch.setattr(cc, "_playwright_context", fake_ctx)
    code = _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(chrome_dir)])
    out, err = capsys.readouterr()
    assert code != 0
    _assert_no_secrets(out + err + caplog.text)

    @asynccontextmanager
    async def boom(path):
        raise RuntimeError(f"launch failed {V_DOM} --user-data-dir={path}")
        yield  # pragma: no cover

    monkeypatch.setattr(cc, "_playwright_context", boom)
    code = _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(chrome_dir)])
    out, err = capsys.readouterr()
    assert code == 2
    _assert_no_secrets(out + err + caplog.text)
    assert str(_isolated_profile_root) not in err
    # 실패했으면 로그인 기록도 남기지 않는다
    assert not (_isolated_profile_root / "serve-t1" / ll.LEDGER_FILE).exists()


def test_output_lines_mask_values_read_in_this_process(chrome_dir, keychain, capsys):
    """WS-51 레지스트리 가림: 읽은 값이 어떤 경로로 출력 줄에 섞여도 가려진다(방어선 직접 확인)."""
    from interface import profile_cli

    assert _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(chrome_dir), "--dry-run"]) == 0
    capsys.readouterr()
    profile_cli._import_say(f"leak? {V_SID} {V_PREF}")
    profile_cli._import_say(f"leak? {V_DOM}", err=True)
    out, err = capsys.readouterr()
    _assert_no_secrets(out + err)
    assert "[REDACTED]" in out and "[REDACTED]" in err


def test_import_cookies_not_exposed_to_mcp():
    src = Path(__file__).resolve().parents[2] / "src" / "interface"
    for path in sorted(src.glob("*.py")):
        if path.name in ("profile_cli.py", "cli.py"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "chrome_cookies" not in text and "import_cookies" not in text, path.name


@pytest.fixture
def echo_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = (self.headers.get("Cookie") or "").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


async def _visit(profile: Path, port: int):
    from playwright.async_api import async_playwright

    rules = "MAP example.test 127.0.0.1,MAP *.example.test 127.0.0.1,MAP evil-example.test 127.0.0.1"
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile), headless=True, args=[f"--host-resolver-rules={rules}"])
        try:
            page = await ctx.new_page()
            sent = {}
            for host in ("a.example.test", "b.example.test", "evil-example.test"):
                resp = await page.goto(f"http://{host}:{port}/echo")
                sent[host] = await resp.text()
            return sent, await ctx.cookies()
        finally:
            await ctx.close()


@requires_chromium
def test_import_injects_into_real_chromium_and_records_ledger(chrome_dir, keychain, echo_site, capsys, caplog,
                                                             _isolated_profile_root):
    caplog.set_level("DEBUG")
    code = _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(chrome_dir)])
    out, err = capsys.readouterr()
    assert code == 0, err
    _assert_no_secrets(out + err + caplog.text)
    assert "example.test: 가져온 쿠키 4개" in out and "SID" in out
    assert "브라우저가 거부 1개" in out and "__Host-bad" not in out
    profile = _isolated_profile_root / "serve-t1"
    assert sp.holder("t1") is None  # 잠금 반납

    ledger = ll.load(profile)
    assert ledger["https://example.test"]["source"] == ll.SOURCE_CHROME_IMPORT
    assert list(ledger) == ["https://example.test"]

    sent, cookies = asyncio.run(_visit(profile, echo_site))
    by_name = {c["name"]: c for c in cookies}
    assert set(by_name) == {"SID", "dom", "pref", "old"}
    assert by_name["SID"]["value"] == V_SID and by_name["SID"]["httpOnly"] and by_name["SID"]["secure"]
    assert by_name["SID"]["domain"] == ".example.test" and by_name["SID"]["sameSite"] == "Lax"
    assert by_name["pref"]["domain"] == "a.example.test" and by_name["pref"]["sameSite"] == "Strict"
    assert by_name["dom"]["expires"] > 0
    # 실제 요청에 실린다 — 비보안 도메인 쿠키는 모든 하위에, 호스트 전용 쿠키는 그 호스트에만
    assert f"dom={V_DOM}" in sent["a.example.test"] and f"pref={V_PREF}" in sent["a.example.test"]
    assert f"old={V_OLD}" in sent["b.example.test"] and "pref=" not in sent["b.example.test"]
    assert "SID=" not in sent["a.example.test"]  # secure 쿠키는 http 로 보내지 않는다(속성 보존)
    assert sent["evil-example.test"] == ""


@requires_chromium
def test_second_import_into_running_profile_merges(chrome_dir, keychain, capsys, _isolated_profile_root):
    assert _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(chrome_dir)]) == 0
    capsys.readouterr()
    other = chrome_dir.parent / "Chrome2"
    make_db(other / "Default" / "Cookies", 23, [
        {"host": ".second.test", "name": "S2", "value": V_OTHER},
    ])
    assert _run(["--profile", "t1", "--site", "second.test", "--chrome-dir", str(other)]) == 0
    ledger = ll.load(_isolated_profile_root / "serve-t1")
    assert set(ledger) == {"https://example.test", "https://second.test"}
