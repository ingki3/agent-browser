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


def _assert_held(lock, root: Path, name: str, server_id: str) -> None:
    """잠금이 실제로 쥐어졌고 잠금 파일·holder() 가 이 서버를 가리키는지."""
    assert lock.held and lock.name == name and lock.path == root / f"serve-{name}"
    body = json.loads((lock.path / sp.LOCK_FILE).read_text(encoding="utf-8"))
    assert body["server_id"] == server_id and body["pid"] == os.getpid()
    assert sp.holder(name) == {"server_id": server_id, "pid": os.getpid()}


def _assert_released_and_reacquirable(lock, name: str) -> None:
    """반납하면 잠금 파일이 비고 아무도 안 쥐며, 다른 서버가 다시 잡을 수 있다."""
    lock.release()
    assert not lock.held
    assert (lock.path / sp.LOCK_FILE).read_text(encoding="utf-8") == ""
    assert sp.holder(name) is None
    again = sp.acquire(name, server_id="9-re")
    try:
        assert sp.holder(name) == {"server_id": "9-re", "pid": os.getpid()}
    finally:
        again.release()


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
    # 서로의 잠금 파일을 덮어쓰지 않고 각자 자기 서버를 가리킨다
    _assert_held(a, _root, "t1", "1-a")
    _assert_held(b, _root, "t2", "2-b")
    _assert_released_and_reacquirable(a, "t1")
    _assert_held(b, _root, "t2", "2-b")  # t1 반납이 t2 를 건드리지 않는다
    _assert_released_and_reacquirable(b, "t2")


def test_live_chromium_singleton_lock_refused(_root):
    d = sp.prepare(sp.profile_dir("t1"))
    # 다른(살아 있는) Chromium 이 이 폴더를 쓰는 중: SingletonLock -> "<host>-<pid>"
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", d / "SingletonLock")
    with pytest.raises(sp.ProfileInUseError, match=str(os.getpid())):
        sp.acquire("t1", server_id="1-a")


def test_stale_chromium_singleton_lock_ignored(_root):
    d = sp.prepare(sp.profile_dir("t1"))
    os.symlink(f"{socket.gethostname()}-999999", d / "SingletonLock")
    lock = sp.acquire("t1", server_id="1-a")
    _assert_held(lock, _root, "t1", "1-a")
    _assert_released_and_reacquirable(lock, "t1")


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


# ------------------------------------------------------------------ R1 (독립 검증 NB)


def _hold_shared(path: Path) -> int:
    """다른 프로세스의 holder()(=`profile list`) 가 잠금을 잠깐 들여다보는 순간을 흉내낸다."""
    import fcntl

    fd = os.open(path / sp.LOCK_FILE, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    return fd


def test_nb1_holder_never_takes_exclusive_lock(_root, monkeypatch):
    """holder() 는 배타 잠금을 잡지 않는다 — 잡으면 그 순간 시작한 serve 가 거짓 거부된다."""
    import fcntl

    sp.acquire("t1", server_id="1-a").release()  # 잠금 파일이 있는 상태
    flags: list = []
    real = fcntl.flock
    monkeypatch.setattr(sp.fcntl, "flock", lambda fd, op: (flags.append(op), real(fd, op))[1])
    assert sp.holder("t1") is None
    assert flags and not any(op & fcntl.LOCK_EX for op in flags)


def test_nb1_holder_while_other_lister_peeks_is_not_in_use(_root):
    """두 `profile list` 가 겹쳐도 서로를 '사용 중' 으로 보지 않는다."""
    sp.acquire("t1", server_id="1-a").release()
    fd = _hold_shared(_root / "serve-t1")
    try:
        assert sp.holder("t1") is None
    finally:
        os.close(fd)


def test_nb1_acquire_absorbs_momentary_peek(_root):
    """holder() 가 잠깐 들여다보는 동안 시작한 serve 는 짧게 재시도해 잡는다(거짓 거부 0)."""
    import threading

    sp.acquire("t1", server_id="1-a").release()
    fd = _hold_shared(_root / "serve-t1")
    timer = threading.Timer(0.02, os.close, args=(fd,))
    timer.start()
    lock = sp.acquire("t1", server_id="2-b")
    timer.join()
    _assert_held(lock, _root, "t1", "2-b")
    _assert_released_and_reacquirable(lock, "t1")


def test_nb1_real_holder_still_refused_quickly(_root):
    """진짜 동시 사용은 그대로 거부 — 재시도가 거부를 오래 끌지 않는다."""
    import time as _t

    lock = sp.acquire("t1", server_id="1-a")
    try:
        t0 = _t.monotonic()
        with pytest.raises(sp.ProfileInUseError, match="1-a"):
            sp.acquire("t1", server_id="2-b")
        assert _t.monotonic() - t0 < 1.0
    finally:
        lock.release()


def test_nb2_lock_file_symlink_is_profile_error(_root, tmp_path):
    d = sp.prepare(sp.profile_dir("t1"))
    (tmp_path / "other").write_text("")
    os.symlink(tmp_path / "other", d / sp.LOCK_FILE)
    with pytest.raises(sp.ProfileError) as ei:
        sp.acquire("t1", server_id="1-a")
    assert str(tmp_path) not in str(ei.value)
    with pytest.raises(sp.ProfileError):
        sp.holder("t1")
    with pytest.raises(sp.ProfileError):
        sp.remove("t1")


def test_nb2_unwritable_root_is_profile_error(_root):
    _root.mkdir(parents=True)
    os.chmod(_root, 0o500)
    try:
        with pytest.raises(sp.ProfileError, match="권한") as ei:
            sp.acquire("t1", server_id="1-a")
        assert str(_root) not in str(ei.value)
    finally:
        os.chmod(_root, 0o700)


def test_nb2_list_survives_broken_lock_file(_root, tmp_path):
    d = sp.prepare(sp.profile_dir("t1"))
    (tmp_path / "other").write_text("")
    os.symlink(tmp_path / "other", d / sp.LOCK_FILE)
    rows = sp.list_profiles()
    assert [r["name"] for r in rows] == ["t1"]
    assert rows[0]["in_use"] is True  # 판정 못 하면 '비어 있음' 으로 가정하지 않는다


def test_nb8_existing_root_mode_untouched(_root):
    """루트가 이미 있으면(예: $HOME) 그 폴더 권한은 바꾸지 않는다 — 프로필 폴더만 0700."""
    _root.mkdir(parents=True)
    os.chmod(_root, 0o755)
    path = sp.prepare(sp.profile_dir("t1"))
    assert stat.S_IMODE(os.stat(_root).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700


def test_nb8_new_root_is_0700_even_with_loose_umask(_root):
    old = os.umask(0o022)
    try:
        sp.prepare(sp.profile_dir("t1"))
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(_root).st_mode) == 0o700


def test_safe_reason_strips_paths():
    exc = RuntimeError("Target closed: /Users/x/.agent-browser/profiles/serve-t1/Default\nmore /a/b")
    reason = sp.safe_reason(exc)
    assert reason.startswith("RuntimeError")
    assert "/Users" not in reason and "serve-t1" not in reason and "more" not in reason
