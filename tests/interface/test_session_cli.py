"""세션 CLI 테스트 (WS-21).

`agent-browser session login`은 **비밀번호를 저장하지 않는다.** 사람이
실제 브라우저에서 로그인하고, 결과물인 쿠키/localStorage만 암호화해
보관한다.

이 방식이 소셜 로그인과 2FA를 지원하는 유일한 길이다. 제공자들이
헤드리스 자동화를 능동 탐지해 차단하므로 사람이 직접 하는 수밖에 없다.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from browser import SessionStore
from browser.session_probe import detect_expiry
from interface import cli, session_cli
from interface.session_cli import (
    LOGIN_PATH_HINTS,
    META_KEY,
    SessionCLIError,
    _expiry_summary,
    _origins_of,
    _run_list,
    _run_remove,
)

PASS = "test-passphrase-1234"
COOKIE_VALUE = "sensitive-session-token-xyz"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Chromium 바이너리 없음"
)


@pytest.fixture()
def auth_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    for f in d.glob("*"):
        f.unlink()
    d.rmdir()


def _sample_state(expires_days: float = 30.0):
    return {
        "cookies": [
            {
                "name": "session_id",
                "value": COOKIE_VALUE,
                "domain": "example.com",
                "path": "/",
                "expires": datetime.now(timezone.utc).timestamp()
                + 86400 * expires_days,
            }
        ],
        "origins": [],
    }


# ---------------------------------------------------------------------------
# 1. 저장물 보호
# ---------------------------------------------------------------------------


def test_saved_session_is_encrypted_on_disk(auth_dir):
    """디스크에 쿠키 값이 평문으로 남으면 안 된다."""
    store = SessionStore(auth_dir=auth_dir)
    path = store.save("p1", _sample_state(), PASS)

    raw = path.read_bytes()
    assert COOKIE_VALUE.encode() not in raw, "쿠키 값이 평문으로 저장됐습니다"


def test_saved_session_has_0600_permissions(auth_dir):
    store = SessionStore(auth_dir=auth_dir)
    path = store.save("p1", _sample_state(), PASS)
    assert (path.stat().st_mode & 0o077) == 0, "그룹/타인이 읽을 수 있습니다"


def test_wrong_passphrase_rejected(auth_dir):
    from browser import SessionStoreError

    store = SessionStore(auth_dir=auth_dir)
    store.save("p1", _sample_state(), PASS)
    with pytest.raises(SessionStoreError):
        store.load("p1", "wrong-passphrase")


# ---------------------------------------------------------------------------
# 2. 요약 출력이 값을 노출하지 않는다
# ---------------------------------------------------------------------------


def test_origin_summary_excludes_cookie_values():
    """도메인만 보여주고 쿠키 값은 절대 출력하지 않는다."""
    summary = _origins_of(_sample_state())
    assert summary == ["example.com"]
    assert COOKIE_VALUE not in str(summary)


def test_list_output_never_decrypts(auth_dir, capsys):
    """list는 패스프레이즈 없이 동작하므로 내용을 볼 수 없어야 한다."""
    store = SessionStore(auth_dir=auth_dir)
    store.save("p1", _sample_state(), PASS)

    rc = _run_list(str(auth_dir), as_json=False)
    out = capsys.readouterr().out

    assert rc == 0
    assert "p1" in out
    assert COOKIE_VALUE not in out, "list가 쿠키 값을 노출했습니다"


def test_list_json_reports_permissions(auth_dir, capsys):
    store = SessionStore(auth_dir=auth_dir)
    store.save("p1", _sample_state(), PASS)

    _run_list(str(auth_dir), as_json=True)
    data = json.loads(capsys.readouterr().out)

    assert data["profiles"][0]["profile"] == "p1"
    assert data["profiles"][0]["permissions_ok"] is True
    assert COOKIE_VALUE not in json.dumps(data)


def test_list_flags_loose_permissions(auth_dir, capsys):
    """권한이 느슨해지면 사용자에게 알려야 한다."""
    store = SessionStore(auth_dir=auth_dir)
    path = store.save("p1", _sample_state(), PASS)
    os.chmod(path, 0o644)

    _run_list(str(auth_dir), as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data["profiles"][0]["permissions_ok"] is False


def test_empty_dir_reports_nothing(auth_dir, capsys):
    rc = _run_list(str(auth_dir), as_json=False)
    assert rc == 0
    assert "없습니다" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 3. 만료 판정
# ---------------------------------------------------------------------------


def test_expiry_summary_warns_when_soon():
    assert "3.0일 후" in _expiry_summary(_sample_state(expires_days=3))


def test_expiry_summary_silent_when_far():
    assert _expiry_summary(_sample_state(expires_days=90)) == ""


def test_expiry_summary_detects_already_expired():
    assert "이미 만료" in _expiry_summary(_sample_state(expires_days=-1))


def test_session_cookies_ignored_in_expiry():
    """expires가 없는 세션 쿠키는 만료 계산에서 제외한다."""
    state = {"cookies": [{"name": "c", "value": "v", "expires": -1}]}
    assert _expiry_summary(state) == ""


# ---------------------------------------------------------------------------
# 4. 로그인 여부 판정 (실브라우저)
# ---------------------------------------------------------------------------


@requires_chromium
async def test_detects_logged_out_page():
    """비밀번호 칸이 보이면 미로그인으로 판정해야 한다.

    이 판정이 없으면 로그인 실패 상태의 빈 세션을 저장하고,
    무인 실행 중에야 그 사실을 알게 된다.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto("https://example.com/login", wait_until="domcontentloaded")
        await page.set_content("<form><input type='password'></form>")

        signals = await session_cli._collect_signals(page, 200)
        await browser.close()

    assert signals.visible_password_inputs == 1
    assert signals.redirected_to_login is True
    assert detect_expiry(signals).expired is True


@requires_chromium
async def test_detects_logged_in_page():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto("https://example.com/", wait_until="domcontentloaded")

        signals = await session_cli._collect_signals(page, 200)
        await browser.close()

    assert signals.visible_password_inputs == 0
    assert signals.redirected_to_login is False
    assert detect_expiry(signals).expired is False


@requires_chromium
async def test_hidden_password_field_not_counted():
    """숨겨진 password 입력은 미로그인 근거가 아니다.

    많은 사이트가 로그인 후에도 숨은 폼을 DOM에 남긴다.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto("https://example.com/", wait_until="domcontentloaded")
        await page.set_content(
            "<form style='display:none'><input type='password'></form>"
        )

        signals = await session_cli._collect_signals(page, 200)
        await browser.close()

    assert signals.visible_password_inputs == 0


@pytest.mark.parametrize("path", LOGIN_PATH_HINTS)
def test_login_path_hints_are_lowercase(path):
    """대소문자 비교 실수를 막는다 (URL을 소문자로 변환해 비교한다)."""
    assert path == path.lower()


# ---------------------------------------------------------------------------
# 5. remove
# ---------------------------------------------------------------------------


def test_remove_deletes_file(auth_dir, capsys):
    store = SessionStore(auth_dir=auth_dir)
    store.save("p1", _sample_state(), PASS)

    rc = _run_remove("p1", auth_dir=str(auth_dir), force=True)
    assert rc == 0
    assert not store.exists("p1")


def test_remove_missing_profile_errors(auth_dir):
    with pytest.raises(SessionCLIError):
        _run_remove("nope", auth_dir=str(auth_dir), force=True)


# ---------------------------------------------------------------------------
# 6. CLI 배선
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["session", "login", "naver", "--url", "https://naver.com"],
        ["session", "list"],
        ["session", "check", "naver"],
        ["session", "remove", "naver"],
    ],
)
def test_cli_parses_session_commands(argv):
    args = cli._build_parser().parse_args(argv)
    assert args.command == "session"
    assert args.session_action == argv[1]


def test_login_requires_url():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["session", "login", "naver"])


def test_meta_key_is_namespaced():
    """Playwright storage_state 스키마와 충돌하지 않아야 한다."""
    assert META_KEY.startswith("_")
    assert META_KEY not in {"cookies", "origins"}
