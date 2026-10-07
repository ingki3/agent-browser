"""WS-34: serve --browser on-demand 의 서버 전용 임시 프로필(--profile 없을 때).

같은 '닫고 다시 열기' 메커니즘(영속 컨텍스트)을 쓰되 폴더는 서버 하나 전용 — 0700, 서버 종료 때
삭제, 비정상 종료로 남은 것은 다음 기동 때 정리(잠금이 풀린 것만). `profile list` 에는 안 보인다.
테스트는 AGENT_BROWSER_PROFILE_ROOT 로 임시 폴더만 쓴다.
"""

from __future__ import annotations

import os
import stat

import pytest

from browser import serve_profile as sp


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(root))
    return root


def test_acquire_ephemeral_makes_private_dir_under_root(_root):
    lock = sp.acquire_ephemeral(server_id="123-abcdef")
    try:
        assert lock.held
        assert lock.path.parent == _root
        assert lock.path.name == f"{sp.EPHEMERAL_PREFIX}123-abcdef"
        assert stat.S_IMODE(os.stat(lock.path).st_mode) == 0o700
    finally:
        sp.remove_ephemeral(lock)
    assert not lock.path.exists()
    assert not lock.held


def test_ephemeral_not_listed_as_named_profile(_root):
    lock = sp.acquire_ephemeral(server_id="1-aa")
    try:
        assert sp.list_profiles() == []
    finally:
        sp.remove_ephemeral(lock)


@pytest.mark.parametrize("sid", ["../x", "a/b", "", "x" * 200, "ABC"])
def test_ephemeral_server_id_validated(sid):
    with pytest.raises(sp.ProfileError):
        sp.acquire_ephemeral(server_id=sid)


def test_cleanup_removes_only_unlocked_leftovers(_root):
    live = sp.acquire_ephemeral(server_id="1-aa")
    dead = sp.acquire_ephemeral(server_id="2-bb")
    (dead.path / "Cookies").write_text("x")
    dead.release()  # 프로세스가 죽은 것처럼: 잠금만 풀리고 폴더가 남음
    named = sp.acquire("keep", server_id="3-cc")
    named.release()
    other = _root / "example.com"  # 사이트별 폴더(다른 기능) — 건드리지 않는다
    other.mkdir()
    try:
        removed = sp.cleanup_ephemeral()
        assert removed == [f"{sp.EPHEMERAL_PREFIX}2-bb"]
        assert not dead.path.exists()
        assert live.path.exists()
        assert (_root / "serve-keep").exists() and other.exists()
    finally:
        sp.remove_ephemeral(live)


def test_cleanup_skips_symlinked_entry(_root, tmp_path):
    _root.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    os.symlink(target, _root / f"{sp.EPHEMERAL_PREFIX}9-ff")
    assert sp.cleanup_ephemeral() == []
    assert (target / "keep.txt").exists()


def test_cleanup_without_root_is_noop(_root):
    assert sp.cleanup_ephemeral() == []
