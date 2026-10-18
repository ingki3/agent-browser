"""WS-59 — 로그인 기록(login_ledger.json) 형식 (WS-58 이 판정에 사용)."""

from __future__ import annotations

import json
import os
import stat

import pytest

from browser import login_ledger as ll


def test_record_writes_version1_0600_file(tmp_path):
    ll.record(tmp_path, ["https://example.com"], source=ll.SOURCE_CHROME_IMPORT, at=1000.0)
    path = tmp_path / ll.LEDGER_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "origins": {"https://example.com": {"source": "chrome_import", "at": 1000.0}}}


def test_load_and_lookup(tmp_path):
    assert ll.load(tmp_path) == {}
    ll.record(tmp_path, ["https://a.test", "http://b.test:8080"], source=ll.SOURCE_LOGIN_FLOW, at=5.0)
    ll.record(tmp_path, ["https://a.test"], source=ll.SOURCE_CHROME_IMPORT, at=9.0)
    got = ll.load(tmp_path)
    assert got["https://a.test"] == {"source": "chrome_import", "at": 9.0}
    assert got["http://b.test:8080"] == {"source": "login_flow", "at": 5.0}
    assert ll.lookup(tmp_path, "https://A.test/") == {"source": "chrome_import", "at": 9.0}
    assert ll.lookup(tmp_path, "https://c.test") is None


@pytest.mark.parametrize("raw,want", [
    ("https://Example.COM", "https://example.com"),
    ("https://example.com:443/path?q=1", "https://example.com"),
    ("http://example.com:80", "http://example.com"),
    ("http://example.com:8080", "http://example.com:8080"),
])
def test_normalize_origin(raw, want):
    assert ll.normalize_origin(raw) == want


@pytest.mark.parametrize("raw", ["", "example.com", "ftp://example.com", "https://", "https://u:p@example.com",
                                 "javascript:alert(1)", "https://exa mple.com", "https://example.com:99999"])
def test_normalize_origin_rejects(raw):
    with pytest.raises(ll.LedgerError):
        ll.normalize_origin(raw)


def test_bad_source_rejected(tmp_path):
    with pytest.raises(ll.LedgerError):
        ll.record(tmp_path, ["https://a.test"], source="Bad Source!")


def test_symlinked_ledger_refused(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    (tmp_path / ll.LEDGER_FILE).symlink_to(target)
    with pytest.raises(ll.LedgerError):
        ll.load(tmp_path)
    with pytest.raises(ll.LedgerError):
        ll.record(tmp_path, ["https://a.test"], source=ll.SOURCE_CHROME_IMPORT)
    assert target.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("body", ["not json", "[]", '{"version": 2, "origins": {}}', '{"version": 1}',
                                  '{"version": 1, "origins": {"https://a.test": "x"}}'])
def test_corrupt_ledger_is_an_error_not_silently_empty(tmp_path, body):
    (tmp_path / ll.LEDGER_FILE).write_text(body, encoding="utf-8")
    with pytest.raises(ll.LedgerError):
        ll.load(tmp_path)
    with pytest.raises(ll.LedgerError):
        ll.record(tmp_path, ["https://a.test"], source=ll.SOURCE_CHROME_IMPORT)
    assert (tmp_path / ll.LEDGER_FILE).read_text(encoding="utf-8") == body  # 덮어쓰지 않는다


def test_record_is_atomic_no_temp_left(tmp_path):
    ll.record(tmp_path, ["https://a.test"], source=ll.SOURCE_HUMAN_CONTROL)
    assert sorted(os.listdir(tmp_path)) == [ll.LEDGER_FILE]


def test_origin_cap_drops_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(ll, "MAX_ORIGINS", 3)
    for i in range(5):
        ll.record(tmp_path, [f"https://s{i}.test"], source=ll.SOURCE_LOGIN_FLOW, at=float(i))
    assert sorted(ll.load(tmp_path)) == ["https://s2.test", "https://s3.test", "https://s4.test"]
