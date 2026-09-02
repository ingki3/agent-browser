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


# ---------------------------------------------------------------------------
# 7. 비대화형 환경 (WS-21 실사용 검증에서 발견)
# ---------------------------------------------------------------------------


def test_confirm_returns_default_when_not_interactive(monkeypatch):
    """파이프 환경에서 EOFError로 죽지 않아야 한다.

    실측 — CLI를 파이프로 돌렸더니 getpass가 EOFError를 던지고
    스택트레이스가 그대로 노출됐다.
    """
    monkeypatch.setattr(session_cli, "_is_interactive", lambda: False)
    assert session_cli._confirm("삭제할까요? ") is False
    assert session_cli._confirm("저장할까요? ", default=True) is True


def test_confirm_survives_eof(monkeypatch):
    """대화형이라고 판단됐는데 입력이 끊긴 경우도 방어한다."""
    monkeypatch.setattr(session_cli, "_is_interactive", lambda: True)

    def _raise(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    assert session_cli._confirm("계속? ") is False


def test_ci_env_used_when_not_interactive(auth_dir, monkeypatch):
    """비대화형이면 프롬프트 없이 환경변수로 풀려야 한다.

    실측 — AGENT_AUTH_KEY_CI를 설정해뒀는데도 프롬프트가 먼저 떠서
    무인 실행이 막혔다. PRD 5.1-1 우선순위상 프롬프트가 2순위라
    CLI가 대화형 여부를 판단해줘야 한다.
    """
    from browser import CI_ENV_VAR

    monkeypatch.setattr(session_cli, "_is_interactive", lambda: False)
    monkeypatch.setenv(CI_ENV_VAR, PASS)

    store = SessionStore(auth_dir=auth_dir)
    store.save("p1", _sample_state(), PASS)

    resolved = session_cli._resolve_for_read("p1")
    assert resolved == PASS


def test_non_interactive_without_key_reports_how_to_fix(
    auth_dir, monkeypatch
):
    """비대화형에서 키가 없으면 원인과 해결책을 알려줘야 한다."""
    from browser import CI_ENV_VAR

    monkeypatch.setattr(session_cli, "_is_interactive", lambda: False)
    monkeypatch.delenv(CI_ENV_VAR, raising=False)
    monkeypatch.setattr(
        session_cli, "resolve_passphrase",
        lambda *a, **k: (_ for _ in ()).throw(
            __import__("browser").KeyUnavailableError("키 없음")
        ),
    )

    with pytest.raises(SessionCLIError) as exc:
        session_cli._resolve_for_read("p1")

    message = str(exc.value)
    assert "비대화형" in message
    assert CI_ENV_VAR in message


def test_remove_without_force_is_safe_when_not_interactive(
    auth_dir, monkeypatch
):
    """확인할 수 없으면 삭제하지 않는다 (파괴적 동작의 기본값)."""
    monkeypatch.setattr(session_cli, "_is_interactive", lambda: False)
    store = SessionStore(auth_dir=auth_dir)
    store.save("p1", _sample_state(), PASS)

    rc = _run_remove("p1", auth_dir=str(auth_dir), force=False)
    assert rc == 1
    assert store.exists("p1"), "확인 없이 삭제됐습니다"


def test_is_interactive_reads_actual_tty(monkeypatch):
    """_is_interactive가 실제 tty 상태를 읽어야 한다.

    monkeypatch로 이 함수를 대체하는 테스트만 있으면 함수 자체가
    망가져도 드러나지 않는다(사보타주로 확인된 미탐 경로).
    """
    import io

    class _FakeStream(io.StringIO):
        def __init__(self, tty: bool):
            super().__init__()
            self._tty = tty

        def isatty(self):
            return self._tty

    monkeypatch.setattr("sys.stdin", _FakeStream(True))
    monkeypatch.setattr("sys.stdout", _FakeStream(True))
    assert session_cli._is_interactive() is True

    monkeypatch.setattr("sys.stdin", _FakeStream(False))
    assert session_cli._is_interactive() is False, (
        "stdin이 파이프인데 대화형으로 판단했습니다"
    )

    monkeypatch.setattr("sys.stdin", _FakeStream(True))
    monkeypatch.setattr("sys.stdout", _FakeStream(False))
    assert session_cli._is_interactive() is False


def test_resolve_for_read_disables_prompt_when_piped(monkeypatch, auth_dir):
    """비대화형이면 resolve_passphrase에 프롬프트를 넘기지 않아야 한다.

    넘기면 getpass가 EOFError를 던져 무인 실행이 죽는다.
    """
    from browser import CI_ENV_VAR

    captured = {}

    def _spy(profile, *, prompt_fn=None, allow_prompt=True):
        captured["prompt_fn"] = prompt_fn
        captured["allow_prompt"] = allow_prompt
        from browser import KeyResolution

        return KeyResolution(passphrase=PASS, source="ci_env")

    monkeypatch.setattr(session_cli, "resolve_passphrase", _spy)
    monkeypatch.setattr(session_cli, "_is_interactive", lambda: False)

    session_cli._resolve_for_read("p1")

    assert captured["allow_prompt"] is False, (
        "비대화형인데 프롬프트를 허용했습니다"
    )
    assert captured["prompt_fn"] is None, (
        "비대화형인데 프롬프트 함수를 넘겼습니다"
    )
