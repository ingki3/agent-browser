"""WS-59 R2 — 재검증 B1(짧은·등록 제외 값의 정확한 이름 복사)·B2(장부 OS 오류·예상 밖 예외의 traceback) CLI 회귀.

합성 DB·임시 프로필·키체인 바꿔 끼움·가짜 쿠키 저장소만 쓴다(실 Chrome·키체인·네이버 무접촉).
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "browser"))

from browser import chrome_cookies as cc  # noqa: E402
from browser import login_ledger as ll  # noqa: E402
from interface import cli  # noqa: E402
from security import secret_registry  # noqa: E402
from ws59_chrome_fixture import TEST_PASSWORD, make_db  # noqa: E402

#: 레지스트리(WS-51) 등록 제외 대상 — 짧은 값(<4)·순수 알파벳 4~7자·도구 어휘.
EXEMPT_VALUES = ["x9z", "abcd", "abcdefg", "browser_navigate"]
LONG_VALUE = "edge-cookie-secret-59-abc123"


@pytest.fixture(autouse=True)
def _clear_secret_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def keychain(monkeypatch):
    monkeypatch.setattr(cc, "keychain_password", lambda: TEST_PASSWORD)
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: pytest.fail("실제 security 를 부르면 안 됨"))


@pytest.fixture
def fake_store(monkeypatch):
    """브라우저 대신 받은 대로 저장하는 가짜 컨텍스트."""

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
    return store


def _run(argv):
    return cli.main(["profile", "import-cookies", *argv])


def _exact_copy_db(tmp_path: Path, value: str) -> Path:
    """쿠키 A 의 값(value)을 쿠키 B 의 이름 칸에 그대로 복사."""
    root = tmp_path / "Chrome"
    make_db(root / "Default" / "Cookies", 24, [
        {"host": ".example.test", "name": value, "value": "synthetic-holder"},
        {"host": ".example.test", "name": "zz_source", "value": value},
        {"host": ".example.test", "name": value + "q7", "value": "synthetic-other"},
    ])
    return root


def _shown(out: str, value: str) -> bool:
    """출력의 이름 목록에 value 가 이름 하나로 그대로 나왔는가."""
    for line in out.splitlines():
        if " — " in line:
            names = line.split(" — ", 1)[1].split(", ")
            if value in names:
                return True
    return False


@pytest.mark.parametrize("value", EXEMPT_VALUES)
@pytest.mark.parametrize("dry_run", [True, False])
def test_exact_name_copy_of_exempt_value_is_redacted(tmp_path, keychain, fake_store, capsys, value, dry_run):
    """B1: 등록 제외(짧은 값·알파벳·도구 어휘) 값이어도 이름 전체가 정확히 같으면 이름을 통째로 가린다."""
    root = _exact_copy_db(tmp_path, value)
    argv = ["--profile", "t1", "--site", "example.test", "--chrome-dir", str(root)]
    code = _run(argv + (["--dry-run"] if dry_run else []))
    out, err = capsys.readouterr()
    assert code == 0, err
    assert not _shown(out + err, value), out
    assert "[REDACTED]" in out
    assert "zz_source" in out  # 값이 아닌 이름은 그대로
    if len(value) < 8:
        # 정확 일치만 넓혔다 — 짧은 값의 부분 일치는 기존 정책 그대로(이름에 값+꼬리는 보인다).
        assert value + "q7" in out


@pytest.mark.parametrize("value", EXEMPT_VALUES)
def test_global_registry_policy_is_unchanged(value):
    """WS-51 전역 정책(등록 제외)은 바꾸지 않는다 — import 로컬 집합만 정확 일치를 더한다."""
    secret_registry.REGISTRY.add_values([value])
    assert secret_registry.mask_registered(f"name {value} here") == f"name {value} here"


def test_value_guard_exact_and_empty_values():
    guard = cc.ValueGuard(["x9z", "", "abcd", "AA_SYNTHETIC_COOKIE_VALUE_59_abc987654321"])
    assert guard.name("x9z") == "[REDACTED]"
    assert guard.name("abcd") == "[REDACTED]"
    assert guard.name("") == ""  # 빈 값은 집합에 넣지 않는다(빈 이름을 가리지 않음)
    assert guard.name("x9z_other") == "x9z_other"  # 짧은 값 부분 일치는 기존 정책 그대로
    assert guard.name("pre-AA_SYNTHETIC_COOKIE_VALUE_59_abc987654321") == "[REDACTED]"  # 긴 값 부분 일치 유지
    assert "x9z" not in repr(guard) and "abcd" not in repr(guard)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="POSIX 권한 거부가 필요(root 아님)")
def test_ledger_permission_error_is_warning_not_traceback(tmp_path, keychain, fake_store, monkeypatch, capsys,
                                                          _isolated_profile_root):
    """B2: 쓰기 뒤 프로필 폴더가 0500 이면 장부 임시 파일 생성이 실제 OS 권한 오류 — 비밀 없는 경고·비 0 종료."""
    real_writer = cc.write_cookies
    seen = {}

    async def write_then_deny(path, plan):
        result = await real_writer(path, plan)
        seen["path"] = path
        os.chmod(path, 0o500)
        return result

    monkeypatch.setattr(cc, "write_cookies", write_then_deny)
    root = tmp_path / "Chrome"
    make_db(root / "Default" / "Cookies", 24, [
        {"host": ".example.test", "name": "ordinary", "value": "synthetic-holder"},
        {"host": ".example.test", "name": "zz_source", "value": LONG_VALUE},
    ])
    try:
        code = _run(["--profile", LONG_VALUE, "--site", "example.test", "--chrome-dir", str(root)])
    finally:
        if "path" in seen:
            os.chmod(seen["path"], 0o700)
    out, err = capsys.readouterr()
    assert code == 1
    assert LONG_VALUE not in out + err
    assert "Traceback" not in out + err and "Errno" not in out + err and "Permission" not in out + err
    assert "로그인 기록을 남기지 못했습니다" in err
    assert not (seen["path"] / ll.LEDGER_FILE).exists()
    assert not [p for p in seen["path"].iterdir() if p.name.startswith(f".{ll.LEDGER_FILE}.")]


@pytest.mark.parametrize("where", ["ledger", "summary"])
def test_unexpected_exception_ends_with_fixed_text_and_type_only(tmp_path, keychain, fake_store, monkeypatch, capsys,
                                                                 where):
    """B2: 예상 밖 예외는 원문·traceback 대신 고정 문구 + 예외 종류 이름만, 비 0 종료."""
    from interface import profile_cli

    class OddError(Exception):
        pass

    def boom(*a, **k):
        raise OddError(f"{LONG_VALUE} /secret/path")

    if where == "ledger":
        monkeypatch.setattr(ll, "record", boom)
    else:
        monkeypatch.setattr(profile_cli, "_skip_text", boom)
    root = tmp_path / "Chrome"
    make_db(root / "Default" / "Cookies", 24, [
        {"host": ".example.test", "name": "ordinary", "value": LONG_VALUE},
        {"host": ".example.test", "name": "skipme", "value": "session-synthetic", "expires": None},
    ])
    code = _run(["--profile", "t1", "--site", "example.test", "--chrome-dir", str(root)])
    out, err = capsys.readouterr()
    assert code not in (0, None)
    assert LONG_VALUE not in out + err and "/secret/path" not in out + err
    assert "Traceback" not in out + err
    assert "OddError" in err
