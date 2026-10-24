"""WS-64 R1 — 검증 BLOCKING 재현(브라우저 없음): 손상 목록 cold-start(B3)·동시 편집(B4)·IDN(B5)·IPv4(B6)·실효 출처(B2).

Chromium 과의 일치(new URL 실제 평가)와 실제 MCP 경로 재현은 tests/interface/test_ws64_r1_server.py.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import threading
import time
from typing import Any, List, Optional

import pytest

from contracts import ActionType
from security import manual_sites as ms
from security.manual_sites import ManualSiteError, ManualSites, ManualSiteStore, parse_entry


class _P:
    def __init__(self, url: str, frames: Optional[List[Any]] = None, opener: Any = None, origin: Any = None,
                 parent: Any = None) -> None:
        self.url = url
        self.frames = frames or []
        self._opener = opener
        self._origin = origin
        self.parent_frame = parent

    async def opener(self) -> Any:
        return self._opener

    async def evaluate(self, expr: str) -> Any:
        assert expr == "location.origin"
        if isinstance(self._origin, Exception):
            raise self._origin
        return self._origin


class _Ctx:
    def __init__(self, page: Any, root: Any = None) -> None:
        self.page = page
        self.root_page = root


CLICK = {"element_id": "@e1"}


# ----------------------------------------------------------------- B3 손상 목록 cold-start = 판정 불가(전부 차단)


def _broken(tmp_path, fault: str):
    path = tmp_path / "manual_sites.json"
    ms.add_site("guarded.example", path)
    if fault == "invalid-json":
        path.write_text("{broken")
    else:
        path.chmod(0o644)
    return path


@pytest.mark.parametrize("fault", ["invalid-json", "loose-permissions"])
async def test_cold_error_blocks_every_gated_action_everywhere(tmp_path, fault):
    store = ManualSiteStore(_broken(tmp_path, fault))
    sites = store.current()
    assert store.error and sites.broken and not sites.empty
    for url in ("https://guarded.example/", "https://unrelated.example/", "about:blank"):
        for action in ms.MANUAL_GATED:
            assert await ms.manual_site_hit(_Ctx(_P(url)), action, CLICK, store) == "?", (url, action)
    for action in ms.MANUAL_ALLOWED:  # 읽기·이동은 그대로
        assert await ms.manual_site_hit(_Ctx(_P("https://guarded.example/")), action, {}, store) is None
    assert "고쳐" in ms.refusal_message(sites) and ms.refusal_message(ManualSites()) == ms.MANUAL_SITE_MESSAGE


async def test_cold_error_with_extra_flags_still_blocks_and_fix_unblocks(tmp_path):
    path = _broken(tmp_path, "invalid-json")
    store = ManualSiteStore(path, extra=["srail.kr"])
    assert await ms.manual_site_hit(_Ctx(_P("https://other.example/")), ActionType.CLICK, CLICK, store) == "?"
    path.unlink()
    ms.add_site("guarded.example", path)  # 사람이 고침 → 다음 판정부터 정상
    assert not store.current().broken and not store.error
    assert await ms.manual_site_hit(_Ctx(_P("https://other.example/")), ActionType.CLICK, CLICK, store) is None
    assert await ms.manual_site_hit(_Ctx(_P("https://guarded.example/")), ActionType.CLICK, CLICK, store)


async def test_warm_error_keeps_last_list_not_broken(tmp_path):
    path = tmp_path / "manual_sites.json"
    ms.add_site("guarded.example", path)
    store = ManualSiteStore(path)
    assert store.current().match("https://guarded.example/")
    path.write_text("{broken")
    sites = store.current()
    assert store.error and not sites.broken and sites.match("https://guarded.example/")
    assert await ms.manual_site_hit(_Ctx(_P("https://other.example/")), ActionType.CLICK, CLICK, store) is None


@pytest.mark.parametrize("fault", ["invalid-json", "loose-permissions"])
def test_cli_list_exits_with_fix_hint_on_broken_file(tmp_path, monkeypatch, capsys, fault):
    from interface import manual_sites_cli

    monkeypatch.setenv(ms.MANUAL_SITES_ENV, str(_broken(tmp_path, fault)))
    code = manual_sites_cli.run(argparse.Namespace(manual_action="list", json=False))
    err = capsys.readouterr().err
    assert code != 0 and "고치세요" in err


def test_serve_store_warns_with_fix_hint(tmp_path, monkeypatch, capsys):
    from interface import cli

    monkeypatch.setenv(ms.MANUAL_SITES_ENV, str(_broken(tmp_path, "invalid-json")))
    store = cli._manual_store(argparse.Namespace(manual_site=[]))
    err = capsys.readouterr().err
    assert store.current().broken and "고치세요" in err and "--manual-site 만" not in err


# ----------------------------------------------------------------- B4 동시 편집 = 잠금 안 트랜잭션


def _slow_write(monkeypatch):
    real = ms._write

    def slow(path, rows):
        time.sleep(0.05)  # 읽기와 쓰기 사이를 넓혀 잠금이 없으면 잃어버린 갱신이 반드시 나게 한다
        real(path, rows)

    monkeypatch.setattr(ms, "_write", slow)


def test_concurrent_adds_all_kept(tmp_path, monkeypatch):
    path = tmp_path / "manual_sites.json"
    ms.add_site("base.example", path)
    _slow_write(monkeypatch)
    names = [f"s{i}.example" for i in range(6)]
    start = threading.Barrier(len(names))

    def add(name):
        start.wait(timeout=5)
        return ms.add_site(name, path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
        outcomes = list(pool.map(add, names))
    assert all(added for _, added in outcomes)
    assert {r["site"] for r in ms.read_entries(path)} == {"base.example", *names}


def test_concurrent_add_and_remove_both_apply(tmp_path, monkeypatch):
    path = tmp_path / "manual_sites.json"
    ms.add_site("base.example", path)
    ms.add_site("gone.example", path)
    _slow_write(monkeypatch)
    start = threading.Barrier(2)

    def run(fn):
        start.wait(timeout=5)
        return fn()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(run, [lambda: ms.add_site("new.example", path),
                                  lambda: ms.remove_site("gone.example", path)]))
    assert out == [("new.example", True), True]
    assert {r["site"] for r in ms.read_entries(path)} == {"base.example", "new.example"}


def test_lock_file_is_private_and_not_symlink_followed(tmp_path):
    path = tmp_path / "manual_sites.json"
    ms.add_site("base.example", path)
    lock = tmp_path / "manual_sites.json.lock"
    assert lock.exists() and (os.stat(lock).st_mode & 0o077) == 0
    lock.unlink()
    lock.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ManualSiteError):
        ms.add_site("x.example", path)
    assert not (tmp_path / "elsewhere").exists()


# ----------------------------------------------------------------- B5 IDN = Chromium(UTS46 비전이)과 같은 표현만


@pytest.mark.parametrize("raw", ["faß.de", "straße.de", "ςa.gr", "a\u200db.com", "a\u200cb.com"])
def test_idn_deviation_characters_rejected(raw):
    with pytest.raises(ManualSiteError):
        parse_entry(raw)


@pytest.mark.parametrize("raw,text", [
    ("한국.kr", "xn--3e0b707e.kr"), ("Bücher.de", "xn--bcher-kva.de"), ("ｅｘａｍｐｌｅ.com", "example.com"),
    ("xn--fa-hia.de", "xn--fa-hia.de"),  # 브라우저 주소창의 punycode 그대로는 받는다
])
def test_idn_accepted_forms(raw, text):
    assert parse_entry(raw).text == text


def test_punycode_entry_matches_browser_host():
    assert ManualSites.of(["xn--fa-hia.de"]).match("https://xn--fa-hia.de/") == "xn--fa-hia.de"


# ----------------------------------------------------------------- B6 IPv4 = WHATWG 규칙


@pytest.mark.parametrize("raw", ["127.1", "2130706433", "0x7f000001", "0177.0.0.1", "0x7f.1", "127.0.1", "127.0.0.1.",
                                 "0x7F.0.0.0x1"])
def test_ipv4_variants_normalize_to_dotted_quad(raw):
    assert parse_entry(raw).text == "127.0.0.1"
    assert ManualSites.of([raw]).match("http://127.0.0.1:8080/") == "127.0.0.1"


@pytest.mark.parametrize("raw", ["256.0.0.1", "1.2.3.4.5", "0x100000000", "1.2.3.09", "example.123"])
def test_ipv4_invalid_numeric_hosts_rejected(raw):
    with pytest.raises(ManualSiteError):
        parse_entry(raw)


def test_ipv4_variant_in_page_url_side_too():
    s = ManualSites.of(["127.0.0.1:9000"])
    assert s.match("http://127.1:9000/") and s.match("http://2130706433:9000/")
    assert parse_entry("*.example.com").wildcard and parse_entry("10.0.0.1").text == "10.0.0.1"
    with pytest.raises(ManualSiteError):
        parse_entry("*.127.1")


# ----------------------------------------------------------------- B2 실효 출처(주소 문자열이 아니라)


def test_blob_url_uses_inner_origin():
    s = ManualSites.of(["srail.kr"])
    assert s.match("blob:https://srail.kr/0b9c-uuid") == "srail.kr"
    assert s.match("blob:https://other.example/0b9c") is None
    assert s.match("blob:null/abc") is None


async def test_blob_popup_hit_after_opener_left():
    store = ManualSites.of(["srail.kr"])
    opener = _P("https://elsewhere.example/")  # opener 는 목록 밖으로 이동함
    popup = _P("blob:https://srail.kr/uuid", opener=opener)
    assert await ms.manual_site_hit(_Ctx(popup), ActionType.CLICK, CLICK, store) == "srail.kr"


async def test_about_blank_popup_uses_effective_origin():
    store = ManualSites.of(["srail.kr"])
    popup = _P("about:blank", opener=_P("https://elsewhere.example/"), origin="https://srail.kr")
    assert await ms.manual_site_hit(_Ctx(popup), ActionType.TYPE_TEXT, CLICK, store) == "srail.kr"
    plain = _P("about:blank", origin="https://other.example")
    assert await ms.manual_site_hit(_Ctx(plain), ActionType.CLICK, CLICK, store) is None


@pytest.mark.parametrize("url", ["about:srcdoc", "about:blank", "data:text/html,x"])
@pytest.mark.parametrize("origin", ["null", RuntimeError("detached"), None])
async def test_inherited_or_opaque_frame_blocked_by_listed_parent_chain(url, origin):
    store = ManualSites.of(["srail.kr"])
    top = _P("https://outer.example/")
    listed_parent = _P("https://srail.kr/embed", parent=top)
    nested = _P(url, origin=origin, parent=listed_parent)
    assert await ms.manual_site_hit(_Ctx(nested, top), ActionType.CLICK, CLICK, store) == "srail.kr"
    # 사슬에 목록 출처가 없으면 막지 않는다(과차단 방지).
    nested2 = _P(url, origin=origin, parent=_P("https://ad.example/", parent=top))
    assert await ms.manual_site_hit(_Ctx(nested2, top), ActionType.CLICK, CLICK, store) is None


async def test_effective_origin_eval_timeout_does_not_hang(monkeypatch):
    monkeypatch.setattr(ms, "EFFECTIVE_ORIGIN_TIMEOUT_S", 0.05)

    class _Hang(_P):
        async def evaluate(self, expr: str) -> Any:
            import asyncio

            await asyncio.sleep(10)

    store = ManualSites.of(["srail.kr"])
    t = time.monotonic()
    top = _P("https://srail.kr/")
    assert await ms.manual_site_hit(_Ctx(_Hang("about:blank", parent=top), top), ActionType.CLICK, CLICK, store)
    assert time.monotonic() - t < 2


def test_sync_hit_for_dialogs_covers_blob_and_broken(tmp_path):
    store = ManualSites.of(["srail.kr"])
    assert ms.manual_hit_sync(store, _P("blob:https://srail.kr/u")) == "srail.kr"
    assert ms.manual_hit_sync(store, _P("https://x.example/", frames=[_P("https://srail.kr/f")])) == "srail.kr"
    assert ms.manual_hit_sync(store, _P("https://x.example/")) is None
    assert ms.manual_hit_sync(ManualSiteStore(_broken(tmp_path, "invalid-json")), _P("https://x.example/")) == "?"
