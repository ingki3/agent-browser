"""WS-32: serve --profile NAME — 이름 붙인 영속 프로필(폴더·이름 검사·잠금·목록·삭제).

테스트는 AGENT_BROWSER_PROFILE_ROOT 로 임시 폴더만 쓴다(실제 홈 폴더 무접촉).
"""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

import pytest

from browser import serve_profile as sp
from browser import user_chrome


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(root))
    return root


def test_root_comes_from_env(_root):
    assert sp.profile_root() == _root


def test_default_root_is_shared_site_session_root(monkeypatch):
    from browser.site_session import DEFAULT_PROFILE_ROOT

    monkeypatch.delenv(sp.PROFILE_ROOT_ENV, raising=False)
    assert sp.profile_root() == DEFAULT_PROFILE_ROOT


@pytest.mark.parametrize("name", ["t1", "work", "a-b-c", "0", "x" * 32])
def test_valid_names(name, _root):
    assert sp.profile_dir(name) == _root / f"serve-{name}"


@pytest.mark.parametrize("name", [
    "", "x" * 33, "../x", "a/b", "A", "Work", "a_b", "a.b", "..", ".", "a b", "~", "é",
    "/etc", "a\n", "-" * 33, None,
])
def test_invalid_names_rejected(name):
    with pytest.raises(sp.ProfileError):
        sp.profile_dir(name)


def test_user_default_chrome_profile_root_rejected(monkeypatch):
    bad = user_chrome._USER_PROFILE_ROOTS[0]
    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(bad))
    with pytest.raises(sp.ProfileError, match="평소 Chrome"):
        sp.profile_dir("t1")


def test_user_chrome_dedicated_profile_rejected(monkeypatch):
    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(user_chrome.DEFAULT_PROFILE_DIR))
    with pytest.raises(sp.ProfileError, match="user-chrome"):
        sp.profile_dir("t1")


def test_prepare_makes_0700(_root):
    path = sp.prepare(sp.profile_dir("t1"))
    assert path.is_dir()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(_root).st_mode) == 0o700


def test_prepare_tightens_existing_dir(_root):
    d = _root / "serve-t1"
    d.mkdir(parents=True)
    os.chmod(d, 0o755)
    sp.prepare(d)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_prepare_refuses_symlink(_root, tmp_path):
    _root.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (_root / "serve-t1").symlink_to(target)
    with pytest.raises(sp.ProfileError, match="심볼릭"):
        sp.prepare(_root / "serve-t1")


# ------------------------------------------------------------------ 잠금


def test_acquire_and_release(_root):
    lock = sp.acquire("t1", server_id="111-aaa")
    try:
        assert lock.path == _root / "serve-t1"
        info = sp.holder("t1")
        assert info is not None and info["server_id"] == "111-aaa"
    finally:
        lock.release()
    assert sp.holder("t1") is None


def test_second_acquire_refused_with_server_id(_root):
    lock = sp.acquire("t1", server_id="111-aaa")
    try:
        with pytest.raises(sp.ProfileInUseError) as ei:
            sp.acquire("t1", server_id="222-bbb")
        msg = str(ei.value)
        assert "111-aaa" in msg and "t1" in msg
        assert str(_root) not in msg  # 경로 대신 이름
    finally:
        lock.release()
    # 반납 뒤에는 다시 잡힌다
    sp.acquire("t1", server_id="222-bbb").release()


def test_other_profile_not_blocked(_root):
    a = sp.acquire("t1", server_id="1-a")
    b = sp.acquire("t2", server_id="2-b")
    a.release()
    b.release()


def test_live_chromium_singleton_lock_refused(_root):
    d = sp.prepare(sp.profile_dir("t1"))
    # 다른(살아 있는) Chromium 이 이 폴더를 쓰는 중: SingletonLock -> "<host>-<pid>"
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", d / "SingletonLock")
    with pytest.raises(sp.ProfileInUseError, match=str(os.getpid())):
        sp.acquire("t1", server_id="1-a")


def test_stale_chromium_singleton_lock_ignored(_root):
    d = sp.prepare(sp.profile_dir("t1"))
    os.symlink(f"{socket.gethostname()}-999999", d / "SingletonLock")
    sp.acquire("t1", server_id="1-a").release()


def test_lock_file_is_private(_root):
    lock = sp.acquire("t1", server_id="1-a")
    try:
        f = lock.path / sp.LOCK_FILE
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
        assert json.loads(f.read_text())["server_id"] == "1-a"
    finally:
        lock.release()


# ------------------------------------------------------------------ 목록·삭제


def test_list_profiles(_root):
    d = sp.prepare(sp.profile_dir("t1"))
    (d / "Default").mkdir()
    (d / "Default" / "Cookies").write_bytes(b"x" * 100)
    sp.prepare(sp.profile_dir("t2"))
    (_root / "example.com").mkdir()  # site_session 폴더는 serve 프로필이 아니다
    lock = sp.acquire("t2", server_id="9-z")
    try:
        rows = {r["name"]: r for r in sp.list_profiles()}
    finally:
        lock.release()
    assert set(rows) == {"t1", "t2"}
    assert rows["t1"]["size_bytes"] >= 100 and rows["t1"]["in_use"] is False
    assert rows["t2"]["in_use"] is True and rows["t2"]["server_id"] == "9-z"
    assert rows["t1"]["last_used"]
    assert all("path" not in r for r in rows.values())


def test_list_empty_root():
    assert sp.list_profiles() == []


def test_remove_profile(_root):
    sp.prepare(sp.profile_dir("t1"))
    sp.remove("t1")
    assert not (_root / "serve-t1").exists()


def test_remove_in_use_refused(_root):
    lock = sp.acquire("t1", server_id="1-a")
    try:
        with pytest.raises(sp.ProfileInUseError):
            sp.remove("t1")
        assert (_root / "serve-t1").exists()
    finally:
        lock.release()


def test_remove_missing():
    with pytest.raises(sp.ProfileError, match="없습니다"):
        sp.remove("nope")


def test_remove_symlink_refused(_root, tmp_path):
    _root.mkdir(parents=True)
    target = tmp_path / "keep"
    target.mkdir()
    (target / "f").write_text("x")
    (_root / "serve-t1").symlink_to(target)
    with pytest.raises(sp.ProfileError):
        sp.remove("t1")
    assert (target / "f").exists()
