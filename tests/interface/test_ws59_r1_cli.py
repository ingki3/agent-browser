"""WS-59 R1 — CLI 출력 경계(R1)·실제 Chromium 교체 거부(R3) 회귀 테스트 (합성 DB·임시 프로필·키체인 바꿔 끼움)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "browser"))

from browser import chrome_cookies as cc  # noqa: E402
from browser import login_ledger as ll  # noqa: E402
from browser import serve_profile as sp  # noqa: E402
from interface import cli  # noqa: E402
from security import secret_registry  # noqa: E402
from test_run_cli import requires_chromium  # noqa: E402
from ws59_chrome_fixture import TEST_PASSWORD, make_db  # noqa: E402

SENTINEL = "AA_SYNTHETIC_COOKIE_VALUE_59_abc987654321"
SHORT = "ab12"  # 4~7자, 순수 알파벳 아님 — 레지스트리 짧은 값 규칙(경계 일치)으로 보호 대상


@pytest.fixture(autouse=True)
def _clear_secret_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def keychain(monkeypatch):
    monkeypatch.setattr(cc, "keychain_password", lambda: TEST_PASSWORD)
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: pytest.fail("실제 security 를 부르면 안 됨"))


def _run(argv):
    return cli.main(["profile", "import-cookies", *argv])


def _overflow_db(tmp_path: Path, per_site: int) -> Path:
    """두 사이트 × per_site 쿠키. 첫 쿠키의 값(SENTINEL)을 다른 쿠키의 이름 칸에 복사(정렬상 표시 상한 안)."""
    rows = [{"host": ".example.test", "name": SENTINEL, "value": "name-holder-synthetic-000"},
            {"host": ".example.test", "name": f"pre-{SHORT}-x", "value": "short-holder-synthetic-1"}]
    rows += [{"host": ".example.test", "name": f"z{i}",
              "value": SENTINEL if i == 0 else (SHORT if i == 1 else f"first-site-synthetic-value-{i}")}
             for i in range(per_site - 2)]
    rows += [{"host": ".second.test", "name": f"q{i}", "value": f"second-site-synthetic-value-{i}"}
             for i in range(per_site)]
    root = tmp_path / "Chrome"
    make_db(root / "Default" / "Cookies", 24, rows)
    return root


@pytest.mark.parametrize("per_site", [50, 150])
def test_dry_run_never_prints_a_cookie_value_copied_into_a_name(tmp_path, keychain, capsys, per_site,
                                                                _isolated_profile_root):
    """전역 레지스트리(256개 상한)가 넘쳐도 이번 import 가 읽은 값의 로컬 집합으로 이름을 가린다."""
    root = _overflow_db(tmp_path, per_site)
    code = _run(["--profile", "t1", "--site", "example.test", "--site", "second.test", "--chrome-dir", str(root),
                 "--dry-run"])
    out, err = capsys.readouterr()
    assert code == 0, err
    if per_site == 150:
        assert per_site * 2 > secret_registry.MAX_VALUES  # 용량 초과 조건 성립(측정 유효)
    assert SENTINEL not in out + err
    assert f"pre-{SHORT}-x" not in out + err  # 짧은 값(경계 일치)이 든 이름도 통째로 가린다
    assert "pre-[REDACTED]" not in out  # 부분만 가린 이름이 아니라 이름 전체가 표지
    assert "[REDACTED]" in out
    assert "z10" in out  # 값이 없는 이름은 그대로(정렬상 표시 상한 안)


def test_final_line_check_masks_any_value_read_in_this_import(tmp_path, keychain, capsys, _isolated_profile_root):
    """요약·오류 줄의 최종 검사 — 전역 레지스트리를 비워도 이번 import 의 값은 가린다."""
    from interface import profile_cli

    root = _overflow_db(tmp_path, 150)
    assert _run(["--profile", "t1", "--site", "example.test", "--site", "second.test", "--chrome-dir", str(root),
                 "--dry-run"]) == 0
    capsys.readouterr()
    secret_registry.clear()
    profile_cli._import_say(f"oops {SENTINEL} and second-site-synthetic-value-7")
    profile_cli._import_say(f"err {SENTINEL}", err=True)
    out, err = capsys.readouterr()
    assert SENTINEL not in out + err and "second-site-synthetic-value-7" not in out
    assert out.count("[REDACTED]") >= 2 and "[REDACTED]" in err


def test_write_summary_names_are_checked_too(tmp_path, keychain, monkeypatch, capsys, _isolated_profile_root):
    """실제 가져오기 요약(가져온 이름 목록)도 같은 경계를 지난다 — 가짜 컨텍스트(모두 그대로 저장)."""
    from contextlib import asynccontextmanager

    class Store:
        def __init__(self):
            self.rows = []

        async def add_cookies(self, cookies):
            self.rows.extend(dict(c, sameSite=c.get("sameSite", "Lax")) for c in cookies)

        async def cookies(self):
            return list(self.rows)

    store = Store()

    @asynccontextmanager
    async def ctx(path):
        yield store

    monkeypatch.setattr(cc, "_playwright_context", ctx)
    root = _overflow_db(tmp_path, 150)
    code = _run(["--profile", "t1", "--site", "example.test", "--site", "second.test", "--chrome-dir", str(root)])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert SENTINEL not in out + err and f"pre-{SHORT}-x" not in out + err
    assert "example.test: 가져온 쿠키 150개" in out and "[REDACTED]" in out


async def _seed(path: Path) -> None:
    old = cc.ChromeCookie(".example.test", "__Secure-test", "old-synthetic-value", "/", time.time() + 3600,
                          True, True, "Lax")
    async with cc._playwright_context(path) as ctx:
        await ctx.add_cookies([old.to_playwright()])  # type: ignore[attr-defined]


async def _stored(path: Path):
    async with cc._playwright_context(path) as ctx:
        return await ctx.cookies()  # type: ignore[attr-defined]


@requires_chromium
def test_real_chromium_rejected_replacement_is_failure_and_not_recorded(tmp_path, keychain, capsys,
                                                                        _isolated_profile_root):
    lock = sp.acquire("t1", server_id="seed-ws59")
    profile = lock.path
    try:
        asyncio.run(_seed(profile))
    finally:
        lock.release()
    root = tmp_path / "Chrome"
    make_db(root / "Default" / "Cookies", 24, [
        {"host": ".example.test", "name": "__Secure-test", "value": "new-synthetic-value", "secure": False,
         "httponly": True, "samesite": 1},
    ])
    code = _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(root)])
    out, err = capsys.readouterr()
    assert code == 1, out + err
    assert "가져온 쿠키 0개" in out and "가져온 쿠키 1개" not in out
    assert "기존 쿠키" in out  # 같은 이름의 기존 쿠키가 남은 거부를 구분해 보인다
    assert ll.load(profile) == {}  # 실제 성공 사이트만 로그인 기록
    rows = asyncio.run(_stored(profile))
    assert [(r["name"], r["value"], r["secure"]) for r in rows] == [("__Secure-test", "old-synthetic-value", True)]


@requires_chromium
def test_real_chromium_reimport_into_existing_profile_succeeds(tmp_path, keychain, capsys, _isolated_profile_root):
    root = tmp_path / "Chrome"
    make_db(root / "Default" / "Cookies", 24, [
        {"host": ".example.test", "name": "SID", "value": "value-sid-00001", "secure": True, "httponly": True,
         "samesite": 1},
        {"host": "a.example.test", "name": "pref", "value": "value-pref-0001", "samesite": 2},
    ])
    argv = ["--profile", "t1", "--site", "example.test", "--chrome-dir", str(root)]
    assert _run(argv) == 0
    capsys.readouterr()
    assert _run(argv) == 0  # 같은 쿠키가 이미 있는 프로필에 다시 — 값·속성이 같으니 성공
    out, err = capsys.readouterr()
    assert "example.test: 가져온 쿠키 2개" in out, out + err
    assert set(ll.load(_isolated_profile_root / "serve-t1")) == {"https://example.test"}
