"""WS-59 R1 — 독립 검증 FAIL(R2·R3·R4·R5) 회귀 테스트 (합성 DB·가짜 브라우저 컨텍스트, 실제 Chrome·키체인 무접촉).

* R2: rollback journal(`Cookies-journal`)이 살아 있는 DB — 커밋된 값만 읽는다(원본 무변경).
* R3: 주입 뒤 저장소의 값·속성이 요청과 같을 때만 성공(같은 키의 기존 쿠키가 남은 거부를 구분).
* R4: `--chrome-dir` 부터 DB 까지, 봇 프로필 루트부터 ledger 까지 경로 구성요소의 심볼릭 링크 거부.
* R5: 의미적으로 손상된 v1 장부(version true·비정규 origin·NaN/Infinity 시각·모르는 source) fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncio
import pytest

from browser import chrome_cookies as cc
from browser import login_ledger as ll
from ws59_chrome_fixture import TEST_PASSWORD, make_db


def provider():
    return lambda: TEST_PASSWORD


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- R2 rollback journal


def _hot_journal_db(tmp_path: Path):
    rows = [{"host": ".example.test", "name": f"N{i}", "value": f"oldvalue-{i}", "enc": "plain"} for i in range(1000)]
    db = make_db(tmp_path / "Chrome" / "Default" / "Cookies", 24, rows)
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA cache_size=1")
    writer.execute("PRAGMA cache_spill=ON")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE cookies SET value='uncommitted-value-987654321' WHERE name='N0'")
    writer.execute("UPDATE cookies SET value=value || 'x'")
    journal = db.with_name("Cookies-journal")
    assert journal.exists() and journal.stat().st_size > 0, "hot journal 조건이 성립하지 않음(측정 무효)"
    return db, writer, journal


def test_hot_rollback_journal_reads_committed_values_only(tmp_path):
    db, writer, journal = _hot_journal_db(tmp_path)
    try:
        before_db, before_j = _sha(db), _sha(journal)
        plan = cc.read_cookies(db, ["example.test"], password_provider=provider())
        assert _sha(db) == before_db and _sha(journal) == before_j  # 원본·journal 무변경
    finally:
        writer.rollback()
        writer.close()
    values = {c.name: c.value for c in plan.sites["example.test"].cookies}
    assert len(values) == 1000
    assert values["N0"] == "oldvalue-0"
    assert all(v == f"oldvalue-{n[1:]}" for n, v in values.items())


def test_unstable_source_is_refused_after_retries(tmp_path, monkeypatch):
    """복사하는 동안 원본이 계속 바뀌면(일관된 사본 불가) 짧게 다시 시도한 뒤 명확한 오류로 거부한다."""
    db = make_db(tmp_path / "Cookies", 24, [{"host": ".example.test", "name": "a", "value": "x" * 12, "enc": "plain"}])
    counter = {"n": 0}
    real = cc._hash_file

    def changing(path):
        counter["n"] += 1
        got = real(path)
        return None if got is None else got + str(counter["n"])  # 두 번 읽을 때마다 다른 내용처럼

    monkeypatch.setattr(cc, "_hash_file", changing)
    monkeypatch.setattr(cc, "_SNAPSHOT_DELAYS", (0.0, 0.0))
    tmp_root = tmp_path / "tmproot"
    tmp_root.mkdir()
    with pytest.raises(cc.CookieImportError, match="일관된 사본"):
        cc.read_cookies(db, ["example.test"], password_provider=provider(), tmp_root=tmp_root)
    assert list(tmp_root.iterdir()) == []
    assert counter["n"] >= 3


def test_symlinked_journal_is_refused(tmp_path):
    db = make_db(tmp_path / "Cookies", 24, [])
    other = tmp_path / "other-journal"
    other.write_bytes(b"\0" * 512)
    db.with_name("Cookies-journal").symlink_to(other)
    with pytest.raises(cc.CookieImportError):
        cc.read_cookies(db, ["example.test"], password_provider=provider())


# ---------------------------------------------------------------- R4 경로 구성요소 심볼릭 링크


def _chrome(tmp_path: Path, network: bool = True) -> Path:
    root = tmp_path / "Chrome"
    sub = ("Default", "Network", "Cookies") if network else ("Default", "Cookies")
    make_db(root.joinpath(*sub), 24, [{"host": ".example.test", "name": "a", "value": "v" * 12, "enc": "plain"}])
    return root


def test_chrome_dir_itself_symlink_is_refused(tmp_path):
    root = _chrome(tmp_path)
    alias = tmp_path / "chrome-link"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(cc.CookieImportError, match="심볼릭 링크"):
        cc.find_cookie_db(alias)
    # 찾은 뒤 바뀐 경우(read 직전 재확인)
    db = cc.find_cookie_db(root)
    with pytest.raises(cc.CookieImportError, match="심볼릭 링크"):
        cc.read_cookies(alias / "Default" / "Network" / "Cookies", ["example.test"], password_provider=provider(),
                        chrome_dir=alias)
    with pytest.raises(cc.CookieImportError, match="심볼릭 링크"):  # 기준점을 안 주면 배치에서 추론
        cc.read_cookies(alias / "Default" / "Network" / "Cookies", ["example.test"], password_provider=provider())
    assert cc.read_cookies(db, ["example.test"], password_provider=provider(), chrome_dir=root).sites


@pytest.mark.parametrize("component", ["Default", "Network"])
def test_every_component_below_chrome_dir_is_checked(tmp_path, component):
    root = _chrome(tmp_path)
    real = root / "Default" if component == "Default" else root / "Default" / "Network"
    moved = tmp_path / f"real-{component}"
    real.rename(moved)
    real.symlink_to(moved, target_is_directory=True)
    with pytest.raises(cc.CookieImportError, match="심볼릭 링크"):
        cc.find_cookie_db(root)


def test_chrome_dir_parent_above_anchor_is_not_checked(tmp_path):
    """기준점(--chrome-dir) 위의 링크(예: macOS /var → /private/var)는 검사하지 않는다."""
    real_parent = tmp_path / "real-parent"
    _chrome(real_parent)
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    db = cc.find_cookie_db(link_parent / "Chrome")
    plan = cc.read_cookies(db, ["example.test"], password_provider=provider(), chrome_dir=link_parent / "Chrome")
    assert [c.name for c in plan.sites["example.test"].cookies] == ["a"]


def test_ledger_load_and_record_refuse_symlinked_profile_and_root(tmp_path):
    real = tmp_path / "real-profile"
    real.mkdir()
    ll.record(real, ["https://example.test"], source=ll.SOURCE_CHROME_IMPORT, at=1.0)
    link = tmp_path / "profile-link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ll.LedgerError):
        ll.load(link)
    with pytest.raises(ll.LedgerError):
        ll.lookup(link, "https://example.test")
    with pytest.raises(ll.LedgerError):
        ll.record(link, ["https://other.test"], source=ll.SOURCE_CHROME_IMPORT)

    real_root = tmp_path / "real-root"
    (real_root / "serve-x").mkdir(parents=True)
    ll.record(real_root / "serve-x", ["https://example.test"], source=ll.SOURCE_CHROME_IMPORT, at=1.0)
    root_link = tmp_path / "root-link"
    root_link.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ll.LedgerError):
        ll.load(root_link / "serve-x")
    with pytest.raises(ll.LedgerError):
        ll.record(root_link / "serve-x", ["https://other.test"], source=ll.SOURCE_CHROME_IMPORT)
    assert set(ll.load(real_root / "serve-x")) == {"https://example.test"}  # 정상 경로는 그대로


# ---------------------------------------------------------------- R5 장부 의미 검증


@pytest.mark.parametrize("version,origin,source,at", [
    (True, "https://example.test", "chrome_import", 1),
    (1.0, "https://example.test", "chrome_import", 1),
    (1, "not-an-origin", "chrome_import", 1),
    (1, "https://Example.test", "chrome_import", 1),
    (1, "https://example.test:443", "chrome_import", 1),
    (1, "https://example.test/", "chrome_import", 1),
    (1, "ftp://example.test", "chrome_import", 1),
    (1, "https://example.test", "chrome_import", float("nan")),
    (1, "https://example.test", "chrome_import", float("inf")),
    (1, "https://example.test", "chrome_import", float("-inf")),
    (1, "https://example.test", "chrome_import", True),
    (1, "https://example.test", "made_up_source", 1),
])
def test_semantically_corrupt_ledger_is_refused(tmp_path, version, origin, source, at):
    body = json.dumps({"version": version, "origins": {origin: {"source": source, "at": at}}})
    (tmp_path / ll.LEDGER_FILE).write_text(body, encoding="utf-8")
    with pytest.raises(ll.LedgerError):
        ll.load(tmp_path)
    with pytest.raises(ll.LedgerError):
        ll.lookup(tmp_path, "https://example.test")
    with pytest.raises(ll.LedgerError):
        ll.record(tmp_path, ["https://a.test"], source=ll.SOURCE_CHROME_IMPORT)
    assert (tmp_path / ll.LEDGER_FILE).read_text(encoding="utf-8") == body  # 덮어쓰지 않는다


@pytest.mark.parametrize("at", [float("nan"), float("inf"), True, "1"])
def test_record_refuses_non_finite_time(tmp_path, at):
    with pytest.raises(ll.LedgerError):
        ll.record(tmp_path, ["https://a.test"], source=ll.SOURCE_CHROME_IMPORT, at=at)
    assert not (tmp_path / ll.LEDGER_FILE).exists()


def test_record_refuses_unknown_source_vocabulary(tmp_path):
    with pytest.raises(ll.LedgerError):
        ll.record(tmp_path, ["https://a.test"], source="made_up_source")
    assert ll.KNOWN_SOURCES == {ll.SOURCE_CHROME_IMPORT, ll.SOURCE_LOGIN_FLOW, ll.SOURCE_HUMAN_CONTROL}


def test_valid_ledger_with_int_time_and_port_still_loads(tmp_path):
    body = {"version": 1, "origins": {"http://b.test:8080": {"source": "login_flow", "at": 5},
                                      "https://[::1]:8443": {"source": "human_control", "at": 2.5}}}
    (tmp_path / ll.LEDGER_FILE).write_text(json.dumps(body), encoding="utf-8")
    got = ll.load(tmp_path)
    assert got["http://b.test:8080"] == {"source": "login_flow", "at": 5.0}
    assert math.isfinite(got["https://[::1]:8443"]["at"])


# ---------------------------------------------------------------- R3 주입 성공 판정(가짜 컨텍스트)


class FakeStore:
    """Chromium 쿠키 저장소 흉내: 키(name, domain, path) 하나에 쿠키 하나. reject(c) 가 참이면 그 쿠키를 거부한다.

    묶음(add_cookies)은 하나라도 거부되면 통째로 예외(Playwright 와 같다)."""

    def __init__(self, existing=(), reject=lambda c: False, mutate=lambda c: c):
        self.cookies_by_key = {(c["name"], c["domain"], c["path"]): dict(c) for c in existing}
        self.reject = reject
        self.mutate = mutate
        self.calls = []

    async def add_cookies(self, cookies):
        self.calls.append(len(cookies))
        if any(self.reject(c) for c in cookies):
            raise RuntimeError("Invalid cookie fields")
        for c in cookies:
            stored = self.mutate(dict(c))
            stored.setdefault("sameSite", "Lax")
            self.cookies_by_key[(c["name"], c["domain"], c["path"])] = stored

    async def cookies(self):
        return [dict(c) for c in self.cookies_by_key.values()]


def _use(monkeypatch, store):
    @asynccontextmanager
    async def ctx(path):
        yield store

    monkeypatch.setattr(cc, "_playwright_context", ctx)


def _cookie(name, value, *, secure=True, http_only=True, same_site="Lax", expires=None, host=".example.test",
            path="/"):
    return cc.ChromeCookie(host_key=host, name=name, value=value, path=path,
                           expires=time.time() + 3600 if expires is None else expires, secure=secure,
                           http_only=http_only, same_site=same_site)


def _plan(*cookies, site="example.test"):
    return cc.ImportPlan(sites={site: cc.SitePlan(site=site, cookies=list(cookies))})


def test_rejected_replacement_with_existing_key_is_not_success(tmp_path, monkeypatch):
    old = _cookie("__Secure-test", "old-synthetic-value").to_playwright()
    store = FakeStore(existing=[old], reject=lambda c: c["name"].startswith("__Secure-") and not c["secure"])
    _use(monkeypatch, store)
    bad = _cookie("__Secure-test", "new-synthetic-value", secure=False)
    res = asyncio.run(cc.write_cookies(tmp_path, _plan(bad)))["example.test"]
    assert res.written == []
    assert res.rejected == 1 and res.kept_existing == 1  # 실패 1, 그중 기존 같은 키가 남은 거부 1(구분)
    assert res.failed == 1
    assert store.calls == [1, 1]  # 묶음 실패 → 하나씩 다시


def test_batch_failure_then_individual_retry_counts_only_verified(tmp_path, monkeypatch):
    store = FakeStore(reject=lambda c: c["name"] == "bad")
    _use(monkeypatch, store)
    plan = _plan(_cookie("good1", "value-good-0001"), _cookie("bad", "value-bad-00001"),
                 _cookie("good2", "value-good-0002", same_site=None, secure=False, http_only=False))
    res = asyncio.run(cc.write_cookies(tmp_path, plan))["example.test"]
    assert res.written == ["good1", "good2"]
    assert res.rejected == 1 and res.kept_existing == 0
    assert store.calls == [3, 1, 1, 1]


@pytest.mark.parametrize("field,changed", [
    ("value", "other-value-xxxx"), ("secure", False), ("httpOnly", False), ("sameSite", "Strict"),
    ("expires", "+5"),
])
def test_stored_attribute_mismatch_is_rejected(tmp_path, monkeypatch, field, changed):
    def mutate(c):
        c[field] = c["expires"] + 5 if changed == "+5" else changed
        return c

    _use(monkeypatch, FakeStore(mutate=mutate))
    res = asyncio.run(cc.write_cookies(tmp_path, _plan(_cookie("SID", "value-sid-00001"))))["example.test"]
    assert res.written == [] and res.rejected == 1, field


def test_expiry_within_one_second_and_400_day_cap_are_accepted(tmp_path, monkeypatch):
    def mutate(c):
        if c["name"] == "long":
            c["expires"] = time.time() + 400 * 86400  # Chromium 만료 상한(400일)
        else:
            c["expires"] = c["expires"] + 0.4
        return c

    _use(monkeypatch, FakeStore(mutate=mutate))
    plan = _plan(_cookie("near", "value-near-0001"), _cookie("long", "value-long-0001",
                                                             expires=time.time() + 800 * 86400))
    res = asyncio.run(cc.write_cookies(tmp_path, plan))["example.test"]
    assert res.written == ["long", "near"] and res.failed == 0


def test_reimport_of_identical_cookie_into_existing_profile_is_success(tmp_path, monkeypatch):
    same = _cookie("SID", "value-sid-00001")
    store = FakeStore(existing=[same.to_playwright()])
    _use(monkeypatch, store)
    res = asyncio.run(cc.write_cookies(tmp_path, _plan(same)))["example.test"]
    assert res.written == ["SID"] and res.failed == 0
