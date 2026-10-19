"""WS-59 — 일상 Chrome 쿠키 DB 읽기·복호화·사이트 필터 (합성 DB, 실제 Chrome·키체인 무접촉).

암호화 형식은 공개된 macOS Chrome 형식(v10, 도메인 해시 접두 meta.version ≥ 24)을 따른 합성 DB 로 검증한다.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from browser import chrome_cookies as cc
from ws59_chrome_fixture import TEST_PASSWORD, derive_test_key, encrypt, make_db

SECRET_A = "sekret-value-AAAA-1111"
SECRET_B = "sekret-value-BBBB-2222"
SECRET_C = "sekret-value-CCCC-3333"


def provider(calls=None):
    def _p() -> bytes:
        if calls is not None:
            calls.append(1)
        return TEST_PASSWORD
    return _p


# ---------------------------------------------------------------- 사이트 이름 검사


@pytest.mark.parametrize("site,want", [
    ("example.com", "example.com"),
    ("Example.COM", "example.com"),
    ("example.com.", "example.com"),
    ("example.co.uk", "example.co.uk"),
    ("someone.github.io", "someone.github.io"),
    ("example.test", "example.test"),
])
def test_validate_site_accepts_registrable_domains(site, want):
    assert cc.validate_site(site) == want


@pytest.mark.parametrize("site", [
    "github.io", "co.uk", "com", "co.kr", "herokuapp.com", "test",
    "127.0.0.1", "localhost", "::1",
    "www.example.com",          # 하위 도메인 — 등록 도메인으로 달라고 안내
    "", " ", "exa mple.com", "example.com/path", "*.example.com", "http://example.com",
    ".example.com", "example..com", "-example.com", "예시.com",
])
def test_validate_site_rejects_public_suffix_and_malformed(site):
    with pytest.raises(cc.CookieImportError):
        cc.validate_site(site)


@pytest.mark.parametrize("site", ["127.0.0.1", "10.0.0.1", "::1"])
def test_validate_site_names_ip_addresses(site):
    with pytest.raises(cc.CookieImportError, match="IP"):
        cc.validate_site(site)


# ---------------------------------------------------------------- 필터 경계


@pytest.mark.parametrize("host", [
    "example.com", ".example.com", "a.example.com", ".a.b.example.com", "EXAMPLE.com",
])
def test_host_in_site_matches_site_and_subdomains(host):
    assert cc.host_in_site(host, "example.com")


@pytest.mark.parametrize("host", [
    "evil-example.com", ".evil-example.com", "example.com.evil.test", ".example.com.evil.test",
    "notexample.com", "com", ".com", "", "..example.com", "example.co",
])
def test_host_in_site_rejects_lookalikes(host):
    assert not cc.host_in_site(host, "example.com")


# ---------------------------------------------------------------- 복호화


def test_derive_key_is_public_macos_chrome_kdf():
    assert cc.derive_key(TEST_PASSWORD) == derive_test_key()
    assert len(cc.derive_key(TEST_PASSWORD)) == 16


def _rows_for(site="example.test"):
    return [
        {"host": f".{site}", "name": "SID", "value": SECRET_A, "httponly": True, "secure": True, "samesite": 1},
        {"host": f"a.{site}", "name": "pref", "value": SECRET_B, "samesite": 2},
        {"host": f".evil-{site}", "name": "EVIL", "value": "evil-value-xxxx"},
        {"host": f"{site}.evil.zz", "name": "EVIL2", "value": "evil-value-yyyy"},
    ]


@pytest.mark.parametrize("meta_version", [24, 23])
def test_decrypts_v10_with_and_without_domain_hash_prefix(tmp_path, meta_version):
    db = make_db(tmp_path / "Default" / "Cookies", meta_version, _rows_for())
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
    site = plan.sites["example.test"]
    got = {c.name: c for c in site.cookies}
    assert set(got) == {"SID", "pref"}
    assert got["SID"].value == SECRET_A and got["pref"].value == SECRET_B
    assert got["SID"].http_only and got["SID"].secure and got["SID"].same_site == "Lax"
    assert got["pref"].same_site == "Strict" and not got["pref"].secure
    assert got["SID"].host_key == ".example.test" and got["pref"].host_key == "a.example.test"
    assert not site.skipped


def test_plaintext_legacy_rows(tmp_path):
    db = make_db(tmp_path / "Cookies", 24, [
        {"host": ".example.test", "name": "old", "value": SECRET_C, "enc": "plain"},
    ])
    calls = []
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider(calls))
    assert [c.value for c in plan.sites["example.test"].cookies] == [SECRET_C]
    assert calls == []  # 평문만 있으면 키체인을 묻지 않는다


def test_unknown_formats_are_skipped_and_counted(tmp_path):
    host = ".example.test"
    good = encrypt(SECRET_A, host, hash_prefix=True)
    rows = [
        {"host": host, "name": "ok", "value": SECRET_A},
        {"host": host, "name": "v11", "enc": b"v11" + good[3:]},
        {"host": host, "name": "v20", "enc": b"v20" + good[3:]},
        {"host": host, "name": "short", "enc": b"v10" + b"x" * 7},                        # 블록 길이 아님
        {"host": host, "name": "wrongkey", "enc": encrypt(SECRET_B, host, hash_prefix=True,
                                                          password=b"other-password")},
        {"host": host, "name": "nohash", "enc": encrypt(SECRET_B, host, hash_prefix=False)},  # v24 인데 접두 없음
        {"host": host, "name": "otherhash", "enc": encrypt(SECRET_B, ".other.test", hash_prefix=True)},
    ]
    db = make_db(tmp_path / "Cookies", 24, rows)
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
    site = plan.sites["example.test"]
    assert [c.name for c in site.cookies] == ["ok"]
    assert sum(site.skipped.values()) == 6
    assert set(site.skipped) <= {cc.SKIP_UNKNOWN, cc.SKIP_DECRYPT}
    assert site.skipped[cc.SKIP_UNKNOWN] >= 2  # v11·v20


def test_missing_meta_version_is_unknown_for_encrypted_rows(tmp_path):
    db = make_db(tmp_path / "Cookies", None, [
        {"host": ".example.test", "name": "enc", "value": SECRET_A},
        {"host": ".example.test", "name": "plain", "value": SECRET_B, "enc": "plain"},
    ])
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
    site = plan.sites["example.test"]
    assert [c.name for c in site.cookies] == ["plain"]
    assert site.skipped == {cc.SKIP_UNKNOWN: 1}


def test_expired_session_partitioned_and_none_insecure_are_skipped(tmp_path):
    host = ".example.test"
    db = make_db(tmp_path / "Cookies", 24, [
        {"host": host, "name": "keep", "value": SECRET_A, "expires": time.time() + 3600},
        {"host": host, "name": "expired", "value": SECRET_B, "expires": time.time() - 10},
        {"host": host, "name": "sess", "value": SECRET_B, "expires": None},
        {"host": host, "name": "chips", "value": SECRET_B, "partition": "https://top.test"},
        {"host": host, "name": "none_insecure", "value": SECRET_B, "samesite": 0, "secure": False},
        {"host": host, "name": "none_secure", "value": SECRET_C, "samesite": 0, "secure": True},
    ])
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
    site = plan.sites["example.test"]
    assert sorted(c.name for c in site.cookies) == ["keep", "none_secure"]
    assert site.skipped[cc.SKIP_EXPIRED] == 1
    assert site.skipped[cc.SKIP_SESSION] == 1
    assert site.skipped[cc.SKIP_PARTITIONED] == 1
    assert site.skipped[cc.SKIP_UNKNOWN] == 1
    keep = next(c for c in site.cookies if c.name == "keep")
    assert abs(keep.expires - (time.time() + 3600)) < 5
    assert next(c for c in site.cookies if c.name == "none_secure").same_site == "None"


def test_other_sites_are_never_decrypted(tmp_path):
    db = make_db(tmp_path / "Cookies", 24, [
        {"host": ".other.test", "name": "x", "value": SECRET_A},
        {"host": ".example.test", "name": "p", "value": SECRET_B, "enc": "plain"},
    ])
    calls = []
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider(calls))
    assert calls == []  # 다른 사이트의 암호 행 때문에 키체인을 묻지 않는다
    assert "other.test" not in plan.sites


def test_multiple_sites_grouped_and_empty_site_listed(tmp_path):
    db = make_db(tmp_path / "Cookies", 24, _rows_for("example.test") + _rows_for("second.test"))
    plan = cc.read_cookies(db, ["example.test", "second.test", "empty.test"], password_provider=provider())
    assert sorted(plan.sites) == ["empty.test", "example.test", "second.test"]
    assert plan.sites["empty.test"].cookies == []
    assert {c.name for c in plan.sites["second.test"].cookies} == {"SID", "pref"}


# ---------------------------------------------------------------- 읽기 전용 사본


def test_reads_a_private_copy_and_deletes_it(tmp_path, monkeypatch):
    db = make_db(tmp_path / "Chrome" / "Default" / "Cookies", 24, _rows_for())
    before = db.read_bytes()
    mtime = db.stat().st_mtime_ns
    tmp_root = tmp_path / "tmproot"
    tmp_root.mkdir()
    seen = {}
    real_connect = sqlite3.connect

    def spy(target, *a, **kw):
        text = str(target)
        seen.setdefault("targets", []).append(text)
        if text.startswith("file:"):
            path = Path(text[5:].split("?", 1)[0])
            seen["mode"] = stat.S_IMODE(os.stat(path.parent).st_mode)
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(cc.sqlite3, "connect", spy)
    cc.read_cookies(db, ["example.test"], password_provider=provider(), tmp_root=tmp_root)
    assert seen["targets"], "sqlite 를 열지 않았다"
    assert all(str(db) not in t for t in seen["targets"]), "원본 DB 를 sqlite 로 직접 열었다"
    assert all("mode=ro" in t for t in seen["targets"])
    assert seen["mode"] == 0o700
    assert list(tmp_root.iterdir()) == []  # 사본 폴더 삭제
    assert db.read_bytes() == before and db.stat().st_mtime_ns == mtime


def test_copy_removed_even_when_reading_fails(tmp_path):
    bad = tmp_path / "Cookies"
    bad.write_bytes(b"not a sqlite db" * 100)
    tmp_root = tmp_path / "tmproot"
    tmp_root.mkdir()
    with pytest.raises(cc.CookieImportError):
        cc.read_cookies(bad, ["example.test"], password_provider=provider(), tmp_root=tmp_root)
    assert list(tmp_root.iterdir()) == []


def test_works_while_chrome_holds_an_exclusive_lock(tmp_path):
    db = make_db(tmp_path / "Cookies", 24, _rows_for())
    holder = sqlite3.connect(db)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
    finally:
        holder.rollback()
        holder.close()
    assert {c.name for c in plan.sites["example.test"].cookies} == {"SID", "pref"}


def test_wal_contents_are_included(tmp_path):
    db = make_db(tmp_path / "Cookies", 24, [])
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    now = int((time.time() + 11644473600) * 1e6)
    conn.execute("INSERT INTO cookies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (now, ".example.test", "", "walcookie", SECRET_A, b"", "/", now + 86_400_000_000, 0, 0,
                  now, 1, 1, 1, -1, 2, 443, now, 0, 0))
    conn.commit()
    try:
        assert (tmp_path / "Cookies-wal").exists()
        plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
    finally:
        conn.close()
    assert [c.name for c in plan.sites["example.test"].cookies] == ["walcookie"]


def test_find_cookie_db_prefers_network_and_rejects_bad_profile_names(tmp_path):
    root = tmp_path / "Chrome"
    make_db(root / "Default" / "Cookies", 24, [])
    assert cc.find_cookie_db(root, "Default") == root / "Default" / "Cookies"
    make_db(root / "Default" / "Network" / "Cookies", 24, [])
    assert cc.find_cookie_db(root, "Default") == root / "Default" / "Network" / "Cookies"
    for bad in ("../Default", "Default/../x", "", "/abs", ".."):
        with pytest.raises(cc.CookieImportError):
            cc.find_cookie_db(root, bad)
    with pytest.raises(cc.CookieImportError):
        cc.find_cookie_db(root, "Profile 9")


def test_symlinked_cookie_db_is_refused(tmp_path):
    real = make_db(tmp_path / "elsewhere" / "Cookies", 24, [])
    (tmp_path / "Chrome" / "Default").mkdir(parents=True)
    (tmp_path / "Chrome" / "Default" / "Cookies").symlink_to(real)
    with pytest.raises(cc.CookieImportError):
        cc.read_cookies(cc.find_cookie_db(tmp_path / "Chrome", "Default"), ["example.test"],
                        password_provider=provider())


# ---------------------------------------------------------------- 값 비노출


def test_reprs_hide_values(tmp_path):
    db = make_db(tmp_path / "Cookies", 24, _rows_for())
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
    text = repr(plan) + str(plan) + "".join(repr(c) + str(c) for c in plan.sites["example.test"].cookies)
    assert SECRET_A not in text and SECRET_B not in text


def test_decrypt_errors_never_carry_values_or_keys(tmp_path, caplog):
    caplog.set_level("DEBUG")
    host = ".example.test"
    db = make_db(tmp_path / "Cookies", 24, [
        {"host": host, "name": "bad", "enc": encrypt(SECRET_A, host, hash_prefix=True, password=b"nope")},
    ])
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
    key_hex = derive_test_key().hex()
    blob = caplog.text + repr(plan)
    for needle in (SECRET_A, TEST_PASSWORD.decode(), key_hex):
        assert needle not in blob


def test_keychain_password_uses_security_cli_without_leaking(monkeypatch, capsys):
    """기본 키체인 함수: `security find-generic-password -w -s "Chrome Safe Storage"` (실제 실행 없음)."""
    seen = {}

    class R:
        returncode = 0
        stdout = b"fake-keychain-secret\n"
        stderr = b""

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        seen["kw"] = kw
        return R()

    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    assert cc.keychain_password() == b"fake-keychain-secret"
    assert seen["argv"][:3] == ["security", "find-generic-password", "-w"]
    assert "Chrome Safe Storage" in seen["argv"]
    assert seen["kw"].get("capture_output") is True
    assert "fake-keychain-secret" not in capsys.readouterr().out


def test_keychain_failure_is_a_plain_error(monkeypatch):
    class R:
        returncode = 51
        stdout = b"partial-secret-leak"
        stderr = b"security: SecKeychainSearchCopyNext: denied partial-secret-leak"

    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(cc.CookieImportError) as ei:
        cc.keychain_password()
    assert "partial-secret-leak" not in str(ei.value)
    assert ei.value.__cause__ is None


def test_keychain_refused_off_macos(monkeypatch):
    monkeypatch.setattr(cc.sys, "platform", "linux")
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: pytest.fail("security 를 부르면 안 됨"))
    with pytest.raises(cc.CookieImportError):
        cc.keychain_password()


def test_playwright_cookie_shape(tmp_path):
    db = make_db(tmp_path / "Cookies", 24, _rows_for())
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
    got = {c.name: c.to_playwright() for c in plan.sites["example.test"].cookies}
    assert got["SID"]["domain"] == ".example.test" and got["SID"]["httpOnly"] is True
    assert got["SID"]["secure"] is True and got["SID"]["sameSite"] == "Lax"
    assert got["pref"]["domain"] == "a.example.test" and got["pref"]["path"] == "/"
