"""본인 Chrome 연결 모드(WS-22 파트 3) — 전용 프로필 + 자동화 플래그 없음.

사람이 띄운 Chrome 에 connect_over_cdp 로 붙으면 navigator.webdriver 가 false 다.
`--enable-automation` 없이 띄운 브라우저이기 때문이지 위장이 아니다. 이 파일은
  - 실행 인자에 자동화/헤드리스/블링크 기능 끄기 플래그가 **없다**는 계약,
  - 사용자 평소 Chrome 프로필 경로를 거부하는 가드(Chrome 136+ 제약 + 평소 프로필 보호),
  - 전용 프로필 폴더 권한 700
을 고정한다. 실제 창이 뜨는 통합 테스트는 AB_USER_CHROME_TEST=1 일 때만 돈다(CI 기본 skip).
"""

from __future__ import annotations

import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from browser import user_chrome
from browser.user_chrome import (
    DEFAULT_PROFILE_DIR,
    UserChrome,
    build_chrome_args,
    find_chrome,
    prepare_profile_dir,
)

FORBIDDEN_SUBSTRINGS = (
    "--enable-automation",
    "--headless",
    "AutomationControlled",
    "--disable-blink-features",
    "--user-agent",
    "--proxy-server",
)


# ---------------------------------------------------------------- 인자 구성


def test_args_have_no_automation_flags(tmp_path):
    args = build_chrome_args(profile_dir=tmp_path / "p", port=9333)
    joined = " ".join(args)
    for bad in FORBIDDEN_SUBSTRINGS:
        assert bad not in joined, f"금지 플래그 {bad!r} 가 인자에 있음: {args}"


def test_args_exact_minimal_set(tmp_path):
    prof = tmp_path / "p"
    args = build_chrome_args(profile_dir=prof, port=9333)
    assert sorted(args) == sorted([
        "--remote-debugging-port=9333",
        f"--user-data-dir={prof.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
    ])


def test_args_user_data_dir_is_dedicated(tmp_path):
    prof = tmp_path / "dedicated"
    args = build_chrome_args(profile_dir=prof, port=9444)
    assert f"--user-data-dir={prof.resolve()}" in args


def test_port_zero_never_passed_to_chrome(tmp_path):
    """실측(Chrome 154): --remote-debugging-port=0 이면 navigator.webdriver=true 가 된다.

    port=0 은 우리가 127.0.0.1 빈 포트를 골라 구체 번호로 넘긴다.
    """
    with pytest.raises(ValueError):
        build_chrome_args(profile_dir=tmp_path / "p", port=0)
    port = user_chrome.pick_free_port()
    assert 1024 < port < 65536


async def test_launch_port_zero_uses_concrete_port(monkeypatch, tmp_path):
    seen = {}

    class FakeProc:
        pid = 999999
        returncode = None

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(user_chrome.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(user_chrome, "_version_info", lambda port, timeout=1.0: {})
    uc = await user_chrome.launch_user_chrome(
        profile_dir=tmp_path / "p", port=0, chrome_path=Path("/bin/false"))
    ports = [a for a in seen["cmd"] if a.startswith("--remote-debugging-port=")]
    assert len(ports) == 1 and ports[0] != "--remote-debugging-port=0"
    assert uc.port == int(ports[0].split("=")[1])
    for bad in FORBIDDEN_SUBSTRINGS:
        assert bad not in " ".join(seen["cmd"])
    uc.process = None  # 가짜 프로세스 — close 대상 아님


def test_default_profile_dir_is_agent_browser_folder():
    assert DEFAULT_PROFILE_DIR == Path.home() / ".agent-browser" / "chrome-profile"


@pytest.mark.parametrize("sub", ["", "Default", "Profile 1"])
def test_user_default_chrome_profile_rejected(sub):
    base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    target = base / sub if sub else base
    with pytest.raises(ValueError):
        build_chrome_args(profile_dir=target, port=9333)
    with pytest.raises(ValueError):
        prepare_profile_dir(target)


def test_linux_default_chrome_profile_rejected():
    with pytest.raises(ValueError):
        prepare_profile_dir(Path.home() / ".config" / "google-chrome")


async def test_launch_rejects_default_profile_before_spawning(monkeypatch):
    spawned = []
    monkeypatch.setattr(user_chrome.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError))
    base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    with pytest.raises(ValueError):
        await user_chrome.launch_user_chrome(profile_dir=base, chrome_path=Path("/bin/false"))
    assert spawned == []


def test_profile_dir_created_with_0700(tmp_path):
    prof = tmp_path / "a" / "chrome-profile"
    out = prepare_profile_dir(prof)
    assert out == prof.resolve()
    assert stat.S_IMODE(prof.stat().st_mode) == 0o700


def test_existing_profile_dir_tightened_to_0700(tmp_path):
    prof = tmp_path / "loose"
    prof.mkdir(mode=0o755)
    os.chmod(prof, 0o755)
    prepare_profile_dir(prof)
    assert stat.S_IMODE(prof.stat().st_mode) == 0o700


def test_find_chrome_returns_existing_or_none(monkeypatch, tmp_path):
    fake = tmp_path / "Google Chrome"
    fake.write_text("")
    monkeypatch.setattr(user_chrome, "CHROME_CANDIDATES", (tmp_path / "nope", fake))
    assert find_chrome() == fake
    monkeypatch.setattr(user_chrome, "CHROME_CANDIDATES", (tmp_path / "nope",))
    assert find_chrome() is None


def test_attached_close_does_not_touch_foreign_process():
    uc = UserChrome(port=9, process=None, profile_dir=None)
    assert uc.owned is False
    uc.close()  # 사용자가 띄운 Chrome: 아무것도 하지 않는다


# ---------------------------------------------------------------- 통합 (opt-in)

_INTEGRATION = os.environ.get("AB_USER_CHROME_TEST") == "1" and find_chrome() is not None
integration = pytest.mark.skipif(
    not _INTEGRATION, reason="AB_USER_CHROME_TEST=1 + 설치된 Chrome 필요(창이 뜸)")


@pytest.fixture
def local_server():
    seen: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = b"<!doctype html><meta charset=utf-8><h1>ok</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", seen
    finally:
        server.shutdown()
        server.server_close()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@integration
async def test_real_chrome_not_webdriver_and_close_kills_only_ours(tmp_path, local_server):
    from playwright.async_api import async_playwright

    url, seen = local_server
    uc = await user_chrome.launch_user_chrome(profile_dir=tmp_path / "prof")
    pid = uc.process.pid
    try:
        assert uc.owned is True
        assert stat.S_IMODE((tmp_path / "prof").stat().st_mode) == 0o700
        import shutil
        import subprocess
        if shutil.which("lsof"):
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{uc.port}", "-sTCP:LISTEN"],
                capture_output=True, text=True).stdout
            listens = [ln.split()[8] for ln in out.splitlines()[1:] if len(ln.split()) > 8]
            print(f"[integration] listen={listens}")
            assert listens and all(a.startswith(("127.0.0.1:", "[::1]:")) for a in listens)
        async with async_playwright() as pw:
            browser, context, page = await user_chrome.connect_user_chrome(pw, uc)
            await page.goto(url)
            webdriver = await page.evaluate("navigator.webdriver")
            print(f"[integration] navigator.webdriver={webdriver!r} ua={seen[-1].get('user-agent')}")
            assert webdriver is False
            assert "HeadlessChrome" not in seen[-1].get("user-agent", "")
            # 붙은 쪽(attach)은 소유가 아니므로 close 해도 프로세스가 살아 있어야 한다
            attached = await user_chrome.attach(uc.port)
            assert attached.owned is False
            attached.close()
            assert _alive(pid)
            await browser.close()  # CDP 연결만 끊는다(connect_over_cdp)
    finally:
        uc.close()
    assert not _alive(pid), "우리가 띄운 Chrome 이 남아 있음"
