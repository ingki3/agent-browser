"""WS-64 — 직접 조작할 사이트: 출처 정규화·판정·차단 집합·목록 파일(로컬, 브라우저 없음)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, List, Optional

import pytest

from contracts import ActionType
from security import manual_sites as ms
from security.manual_sites import ManualSiteError, ManualSites, ManualSiteStore, parse_entry


# ----------------------------------------------------------------- 차단/허용 집합(R1-2)


def test_gated_and_allowed_partition_all_action_types():
    assert ms.MANUAL_GATED | ms.MANUAL_ALLOWED == set(ActionType)
    assert not (ms.MANUAL_GATED & ms.MANUAL_ALLOWED)
    assert len(ms.MANUAL_GATED) == 9 and len(ms.MANUAL_ALLOWED) == 10


def test_gated_set_exact_members():
    assert {a.value for a in ms.MANUAL_GATED} == {
        "click", "type_text", "select_option", "check_box", "hover", "press_key", "handle_dialog",
        "upload_file", "download_file"}
    for a in ActionType:
        assert ms.is_gated(a) is (a in ms.MANUAL_GATED)


def test_unknown_action_is_gated_by_default():
    class _New:  # 계약에 새 액션이 생기면(허용 집합에 없으면) 기본 차단
        pass

    assert ms.is_gated(_New()) is True  # type: ignore[arg-type]


# ----------------------------------------------------------------- 항목 정규화(R2-5)


@pytest.mark.parametrize("raw,text", [
    ("srail.kr", "srail.kr"),
    ("SRail.KR.", "srail.kr"),
    ("https://etk.srail.kr/", "etk.srail.kr"),
    ("http://etk.srail.kr", "etk.srail.kr"),
    ("etk.srail.kr:443", "etk.srail.kr"),
    ("http://etk.srail.kr:80", "etk.srail.kr"),
    ("etk.srail.kr:8443", "etk.srail.kr:8443"),
    ("*.srail.kr", "*.srail.kr"),
    ("https://*.srail.kr", "*.srail.kr"),
    ("127.0.0.1:8080", "127.0.0.1:8080"),
    ("[::1]:9000", "[::1]:9000"),
    ("[0:0:0:0:0:0:0:1]", "[::1]"),
    ("한국.kr", "xn--3e0b707e.kr"),
])
def test_parse_entry_normalizes(raw, text):
    assert parse_entry(raw).text == text


@pytest.mark.parametrize("raw", [
    "", " ", "*", "*.kr", "*.co.kr", "co.kr", "*.github.io", "github.io", "ftp://srail.kr", "srail.kr/path",
    "https://srail.kr/?a=1", "user@srail.kr", "https://u:p@srail.kr", "sr*ail.kr", "*.127.0.0.1", "srail .kr",
    "srail..kr", "-bad.kr", "srail.kr:99999",
])
def test_parse_entry_rejects(raw):
    with pytest.raises(ManualSiteError):
        parse_entry(raw)


# ----------------------------------------------------------------- 주소 판정


SITES = ManualSites.of(["srail.kr", "*.letskorail.com", "127.0.0.1:8765", "[::1]"])


@pytest.mark.parametrize("url", [
    "https://srail.kr/", "http://srail.kr/x", "https://SRAIL.KR./a", "https://srail.kr:8443/",  # 포트 없는 항목 = 모든 포트
    "https://letskorail.com/", "https://www.letskorail.com/", "https://a.b.letskorail.com:444/",
    "http://127.0.0.1:8765/p", "http://[::1]:3000/", "http://[0:0::1]/",
])
def test_match_hits(url):
    assert SITES.match(url) is not None


@pytest.mark.parametrize("url", [
    "https://etk.srail.kr/",  # 하위 도메인은 와일드카드 항목만
    "https://srail.kr.evil.test/", "https://xsrail.kr/", "https://evil-srail.kr/",
    "https://letskorail.com.evil.test/", "https://xletskorail.com/",
    "http://127.0.0.1:8766/", "http://127.0.0.1/", "http://127.0.0.2:8765/",
    "about:blank", "data:text/html,srail.kr", "", "chrome://newtab",
])
def test_match_misses(url):
    assert SITES.match(url) is None


def test_scheme_downgrade_and_default_port_do_not_bypass():
    s = ManualSites.of(["https://etk.srail.kr"])
    assert s.match("http://etk.srail.kr/") == "etk.srail.kr"
    assert s.match("http://etk.srail.kr:80/") == "etk.srail.kr"
    assert s.match("https://etk.srail.kr:443/") == "etk.srail.kr"
    s2 = ManualSites.of(["etk.srail.kr:8443"])
    assert s2.match("https://etk.srail.kr:8443/") and s2.match("https://etk.srail.kr/") is None


def test_wildcard_includes_apex_but_not_lookalikes():
    s = ManualSites.of(["*.srail.kr"])
    assert s.match("https://srail.kr/") and s.match("https://etk.srail.kr/")
    for bad in ("https://srail.kr.evil.test/", "https://xsrail.kr/", "https://notsrail.kr/"):
        assert s.match(bad) is None


def test_idn_entry_matches_punycode_url():
    s = ManualSites.of(["한국.kr"])
    assert s.match("https://xn--3e0b707e.kr/") is not None


def test_malformed_http_url_raises_for_fail_closed():
    with pytest.raises(ManualSiteError):
        SITES.match("http://srail.kr:abc/")


# ----------------------------------------------------------------- manual_site_hit(프레임·opener·좌표)


class _P:
    def __init__(self, url: str, frames: Optional[List[Any]] = None, opener: Any = None) -> None:
        self.url = url
        self.frames = frames or []
        self._opener = opener

    async def opener(self) -> Any:
        return self._opener


class _Ctx:
    def __init__(self, page: Any, root: Any = None) -> None:
        self.page = page
        self.root_page = root


class _BadUrl:
    @property
    def url(self) -> str:
        raise RuntimeError("detached")


async def test_hit_top_frame_and_opener():
    store = ManualSites.of(["srail.kr"])
    plain = _P("https://example.org/")
    assert await ms.manual_site_hit(_Ctx(plain), ActionType.CLICK, {"element_id": "@e1"}, store) is None
    top = _P("https://srail.kr/")
    assert await ms.manual_site_hit(_Ctx(top), ActionType.CLICK, {"element_id": "@e1"}, store) == "srail.kr"
    # iframe 안(switch_frame 뒤): 대상 프레임이 다른 출처라도 최상위가 목록이면 막는다(합집합).
    frame = _P("https://pg.example.net/pay")
    assert await ms.manual_site_hit(_Ctx(frame, top), ActionType.TYPE_TEXT, {"element_id": "@e1"}, store)
    # 최상위는 목록 밖, 대상 프레임이 목록.
    assert await ms.manual_site_hit(_Ctx(_P("https://srail.kr/f"), plain), ActionType.CLICK,
                                    {"element_id": "@e1"}, store)
    # 팝업 탭: 그 탭을 연 opener 가 목록이면 막는다.
    popup = _P("https://calendar.example.org/", opener=top)
    assert await ms.manual_site_hit(_Ctx(popup), ActionType.CLICK, {"element_id": "@e1"}, store)


async def test_hit_any_frame_for_coordinates_keys_dialogs():
    store = ManualSites.of(["srail.kr"])
    top = _P("https://example.org/", frames=[_P("https://example.org/"), _P("https://srail.kr/embed")])
    ctx = _Ctx(top)
    assert await ms.manual_site_hit(ctx, ActionType.CLICK, {"element_id": "@e1"}, store) is None
    assert await ms.manual_site_hit(ctx, ActionType.CLICK, {"x": 10, "y": 10}, store)
    assert await ms.manual_site_hit(ctx, ActionType.PRESS_KEY, {"key": "Enter"}, store)
    assert await ms.manual_site_hit(ctx, ActionType.HANDLE_DIALOG, {"accept": True}, store)


async def test_hit_allowed_actions_and_empty_list_are_noop():
    top = _P("https://srail.kr/")
    for action in ms.MANUAL_ALLOWED:
        assert await ms.manual_site_hit(_Ctx(top), action, {}, ManualSites.of(["srail.kr"])) is None
    for action in ms.MANUAL_GATED:
        assert await ms.manual_site_hit(_Ctx(top), action, {}, ManualSites()) is None
        assert await ms.manual_site_hit(_Ctx(top), action, {}, None) is None


async def test_hit_unreadable_url_fails_closed():
    store = ManualSites.of(["srail.kr"])
    assert await ms.manual_site_hit(_Ctx(_BadUrl()), ActionType.CLICK, {"element_id": "@e1"}, store) == "?"
    assert await ms.manual_site_hit(_Ctx(_P("http://srail.kr:x/")), ActionType.CLICK, {"element_id": "@e"}, store)


# ----------------------------------------------------------------- 응답 data 모양


def test_handoff_data_shape_fixed_values_only():
    d = ms.handoff_data(ActionType.CLICK, available=True, handoff_id="mh-1",
                        target={"role": "button", "name": "조회하기"})
    h = d["handoff"]
    assert h["suggested"] is True and h["reason"] == "manual_site" and h["tool"] == "browser_control_wait"
    assert h["available"] is True and h["handoff_id"] == "mh-1" and h["action"] == "click"
    assert h["steps"] == list(ms.STEPS_AVAILABLE)
    assert "조회하기" not in str(h)  # 페이지 글자는 스캔되는 키로만
    assert d["handoff_target"] == {"role": "button", "name": "조회하기"}
    off = ms.handoff_data(ActionType.CLICK, available=False, handoff_id="mh-1")
    assert "handoff_id" not in off["handoff"] and off["handoff"]["available"] is False
    assert any("browser_control_request" in s and "사라짐" in s for s in off["handoff"]["steps"])
    assert ms.is_manual_handoff(d) and not ms.is_manual_handoff({"handoff": {"reason": "captcha"}})


# ----------------------------------------------------------------- 목록 파일(0600·다시 읽기)


def test_file_add_list_remove_mode_0600(tmp_path):
    path = tmp_path / "ab" / "manual_sites.json"
    assert ms.read_entries(path) == []
    assert ms.add_site("https://etk.srail.kr/", path) == ("etk.srail.kr", True)
    assert ms.add_site("etk.srail.kr", path) == ("etk.srail.kr", False)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [r["site"] for r in ms.read_entries(path)] == ["etk.srail.kr"]
    assert ms.remove_site("http://etk.srail.kr", path) is True
    assert ms.remove_site("etk.srail.kr", path) is False
    assert ms.read_entries(path) == []


def test_file_rejects_loose_perms_symlink_and_garbage(tmp_path):
    path = tmp_path / "manual_sites.json"
    ms.add_site("srail.kr", path)
    os.chmod(path, 0o644)
    with pytest.raises(ManualSiteError):
        ms.read_entries(path)
    os.chmod(path, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ManualSiteError):
        ms.read_entries(link)
    path.write_text("{not json")
    with pytest.raises(ManualSiteError):
        ms.read_entries(path)


def test_store_reloads_on_change_and_keeps_last_on_error(tmp_path):
    path = tmp_path / "manual_sites.json"
    store = ManualSiteStore(path, extra=["127.0.0.1:9"])
    assert store.current().texts() == ["127.0.0.1:9"]
    ms.add_site("srail.kr", path)
    assert set(store.current().texts()) == {"127.0.0.1:9", "srail.kr"}  # 재시작 없이 반영
    path.write_text("garbage")
    os.chmod(path, 0o600)
    assert "srail.kr" in store.current().texts() and store.error  # 손상: 마지막 목록 유지(fail-closed)
    store_none = ManualSiteStore(None)
    assert store_none.empty


def test_default_store_is_not_read_by_dispatch_context():
    from actions.dispatcher import DispatchContext

    ctx = DispatchContext(page=None, engine=None)  # type: ignore[arg-type]
    assert ctx.manual_sites is None  # 기본 빈 목록(홈 파일을 읽지 않음)


def test_default_file_path_under_agent_browser_home():
    assert ms.MANUAL_SITES_FILE == Path.home() / ".agent-browser" / "manual_sites.json"
