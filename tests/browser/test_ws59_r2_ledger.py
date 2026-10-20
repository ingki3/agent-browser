"""WS-59 R2 — 장부 OS 오류의 LedgerError 변환(B2)·짧은 쓰기(B3)·Chrome 사본 짧은 쓰기 회귀.

임시 폴더·합성 DB 만 쓴다. os.write/os.open 바꿔 끼우기는 장부 임시 파일·사본 descriptor 에만 적용한다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from browser import chrome_cookies as cc  # noqa: E402
from browser import login_ledger as ll  # noqa: E402
from ws59_chrome_fixture import TEST_PASSWORD, make_db  # noqa: E402

SECRETISH = "edge-cookie-secret-59-abc123"


def _profile(tmp_path: Path, name: str = "serve-t1") -> Path:
    prof = tmp_path / "profiles" / name
    prof.mkdir(parents=True)
    return prof


def _tmp_leftovers(prof: Path):
    return [p.name for p in prof.iterdir() if p.name.startswith(f".{ll.LEDGER_FILE}.")]


def _target_fds(monkeypatch, marker: str):
    """marker 가 경로에 든 새 파일(O_CREAT) descriptor 만 모은다 — 다른 descriptor 는 그대로."""
    real_open = os.open
    fds: set = set()

    def tracking_open(path, flags, *a, **k):
        fd = real_open(path, flags, *a, **k)
        if flags & os.O_CREAT and marker in str(path):
            fds.add(fd)
        return fd

    monkeypatch.setattr(os, "open", tracking_open)
    return fds


def _patch_write(monkeypatch, fds: set, behaviour):
    real_write = os.write

    def fake(fd, data):
        if fd in fds:
            return behaviour(real_write, fd, bytes(data))
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fake)


# ---------------------------------------------------------------------------- B3 짧은 쓰기


def test_short_writes_are_repeated_until_the_whole_ledger_is_written(tmp_path, monkeypatch):
    prof = _profile(tmp_path)
    ll.record(prof, ["https://old.test"], source=ll.SOURCE_LOGIN_FLOW, at=1)
    fds = _target_fds(monkeypatch, f".{ll.LEDGER_FILE}.")
    calls = []

    def twelve(real, fd, data):
        calls.append(len(data))
        return real(fd, data[:12])

    _patch_write(monkeypatch, fds, twelve)
    ll.record(prof, ["https://new.test"], source=ll.SOURCE_CHROME_IMPORT, at=2)
    monkeypatch.undo()
    assert len(calls) > 1  # 짧은 쓰기 조건 성립(측정 유효)
    data = ll.load(prof)
    assert set(data) == {"https://old.test", "https://new.test"}
    assert _tmp_leftovers(prof) == []


@pytest.mark.parametrize("mode", ["zero", "oserror"])
def test_failed_write_keeps_old_ledger_and_raises_ledger_error(tmp_path, monkeypatch, mode):
    prof = _profile(tmp_path)
    ll.record(prof, ["https://old.test"], source=ll.SOURCE_LOGIN_FLOW, at=1)
    old = (prof / ll.LEDGER_FILE).read_bytes()
    fds = _target_fds(monkeypatch, f".{ll.LEDGER_FILE}.")

    def fail(real, fd, data):
        if mode == "zero":
            return 0
        raise OSError(28, f"No space left: {SECRETISH}")

    _patch_write(monkeypatch, fds, fail)
    with pytest.raises(ll.LedgerError) as info:
        ll.record(prof, ["https://new.test"], source=ll.SOURCE_CHROME_IMPORT, at=2)
    monkeypatch.undo()
    assert SECRETISH not in str(info.value) and info.value.__cause__ is None
    assert (prof / ll.LEDGER_FILE).read_bytes() == old
    assert _tmp_leftovers(prof) == []


# ---------------------------------------------------------------------------- B2 OS 오류 → LedgerError


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="POSIX 권한 거부가 필요(root 아님)")
def test_tmp_create_permission_error_is_ledger_error_without_path(tmp_path):
    prof = _profile(tmp_path, "serve-" + SECRETISH)
    ll.record(prof, ["https://old.test"], source=ll.SOURCE_LOGIN_FLOW, at=1)
    old = (prof / ll.LEDGER_FILE).read_bytes()
    os.chmod(prof, 0o500)
    try:
        with pytest.raises(ll.LedgerError) as info:
            ll.record(prof, ["https://new.test"], source=ll.SOURCE_CHROME_IMPORT, at=2)
    finally:
        os.chmod(prof, 0o700)
    assert SECRETISH not in str(info.value) and info.value.__cause__ is None
    assert info.value.__suppress_context__
    assert (prof / ll.LEDGER_FILE).read_bytes() == old


@pytest.mark.parametrize("op", ["fstat", "fsync", "fchmod", "replace", "close"])
def test_every_record_os_step_failure_is_ledger_error(tmp_path, monkeypatch, op):
    prof = _profile(tmp_path)
    ll.record(prof, ["https://old.test"], source=ll.SOURCE_LOGIN_FLOW, at=1)
    old = (prof / ll.LEDGER_FILE).read_bytes()

    def boom(*a, **k):
        raise OSError(5, f"I/O error {SECRETISH}")

    if op == "fstat":
        monkeypatch.setattr(os, "fstat", boom)  # record 안의 load 단계
    elif op == "close":
        real_close = os.close
        fds = _target_fds(monkeypatch, f".{ll.LEDGER_FILE}.")

        def close(fd):
            real_close(fd)
            if fd in fds:
                raise OSError(5, f"I/O error {SECRETISH}")

        monkeypatch.setattr(os, "close", close)
    else:
        monkeypatch.setattr(os, op, boom)
    with pytest.raises(ll.LedgerError) as info:
        ll.record(prof, ["https://new.test"], source=ll.SOURCE_CHROME_IMPORT, at=2)
    monkeypatch.undo()
    assert SECRETISH not in str(info.value)
    assert (prof / ll.LEDGER_FILE).read_bytes() == old
    assert _tmp_leftovers(prof) == []


def test_cleanup_failure_is_ledger_error(tmp_path, monkeypatch):
    prof = _profile(tmp_path)
    ll.record(prof, ["https://old.test"], source=ll.SOURCE_LOGIN_FLOW, at=1)

    def boom(*a, **k):
        raise OSError(5, f"I/O error {SECRETISH}")

    monkeypatch.setattr(os, "fsync", boom)
    monkeypatch.setattr(os, "unlink", boom)
    with pytest.raises(ll.LedgerError) as info:
        ll.record(prof, ["https://new.test"], source=ll.SOURCE_CHROME_IMPORT, at=2)
    monkeypatch.undo()
    assert SECRETISH not in str(info.value)


@pytest.mark.parametrize("op", ["fstat", "read"])
def test_load_os_errors_are_ledger_error(tmp_path, monkeypatch, op):
    prof = _profile(tmp_path)
    ll.record(prof, ["https://old.test"], source=ll.SOURCE_LOGIN_FLOW, at=1)

    def boom(*a, **k):
        raise OSError(5, f"I/O error {SECRETISH}")

    if op == "fstat":
        monkeypatch.setattr(os, "fstat", boom)
    else:
        monkeypatch.setattr(os, "fdopen", lambda fd, mode: _BoomFile(fd))
    with pytest.raises(ll.LedgerError) as info:
        ll.load(prof)
    monkeypatch.undo()
    assert SECRETISH not in str(info.value)


class _BoomFile:
    def __init__(self, fd):
        self.fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        return False

    def read(self, n):
        raise OSError(5, f"I/O error {SECRETISH}")


# ---------------------------------------------------------------------------- Chrome 사본 짧은 쓰기


def _db(tmp_path: Path) -> Path:
    root = tmp_path / "Chrome"
    rows = [{"host": ".example.test", "name": f"n{i}", "value": f"synthetic-value-{i}"} for i in range(50)]
    make_db(root / "Default" / "Cookies", 24, rows)
    return root


def test_copy_short_writes_are_repeated(tmp_path, monkeypatch):
    root = _db(tmp_path)
    fds = _target_fds(monkeypatch, "ab-chrome-cookies-")
    calls = []

    def small(real, fd, data):
        calls.append(len(data))
        return real(fd, data[:1000])

    _patch_write(monkeypatch, fds, small)
    plan = cc.read_cookies(root / "Default" / "Cookies", ["example.test"], password_provider=lambda: TEST_PASSWORD,
                           tmp_root=tmp_path)
    monkeypatch.undo()
    assert len(calls) > 1
    assert len(plan.sites["example.test"].cookies) == 50


def test_copy_write_returning_zero_is_refused_and_cleaned(tmp_path, monkeypatch):
    root = _db(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    fds = _target_fds(monkeypatch, "ab-chrome-cookies-")
    _patch_write(monkeypatch, fds, lambda real, fd, data: 0)
    with pytest.raises(cc.CookieImportError):
        cc.read_cookies(root / "Default" / "Cookies", ["example.test"], password_provider=lambda: TEST_PASSWORD,
                        tmp_root=work)
    monkeypatch.undo()
    assert list(work.iterdir()) == []
