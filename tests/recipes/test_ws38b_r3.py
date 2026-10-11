"""WS-38b R3: 페이지 문자열·쿼리 키 토큰, sub-delim, 실사이트 슬러그(로컬만)."""

from __future__ import annotations

import json
from urllib.parse import quote, urlsplit

import pytest

from contracts import ActionType
from harness.recipe_mock import HEAD, HEADER, RecipeSite
from interface.mcp_server import BrowserMCPServer
from recipes import store as st

from test_ws38b_autosave import _desc, _entry, _nav
from test_ws38b_r1 import B64, _call, _eid, _profile_file
from test_ws38b_r2 import HEX, _compile


@pytest.mark.parametrize("agent", [False, True], ids=["auto", "save"])
@pytest.mark.parametrize("name", [
    f"http://mock.test/verify/{HEX}", f"https://other.test/unsub/{B64}",
    f"코드 {HEX} 복사", f"코드({HEX})", f"코드：{HEX}，복사",
    f"열기 /verify/{HEX}", f"링크 https://mock.test/help?{HEX}=1 확인",
    f"코드 {B64} 복사",
    "코드 {" + B64 + "} 복사", "http://mock.test/verify/{" + B64 + "}",
    "http://mock.test/verify/%7B" + B64 + "%7D",
    "코드 {a1234567890123456} 복사",
])
def test_target_name_tokens_refused(name, agent):
    with pytest.raises(st.RecipeError, match="토큰"):
        _compile([_nav(), _entry("click", desc=_desc(name, slot=False))], agent)


@pytest.mark.parametrize("name", [
    "주문번호 20241010123456", "B08N5WRWNW", "SM-S928NZKEKOO", "고객센터 1588-1234",
    "장바구니(2)", "https://mock.test/help", "링크(https://mock.test/help)",
])
def test_ordinary_names_still_save(name):
    rec = st.compile_auto([_nav(), _entry("click", desc=_desc(name, slot=False))])
    assert rec["auto"] is True
    assert rec["steps"][1]["variants"][0]["target"]["name"] == name.lower()


@pytest.mark.parametrize("field", ["name", "value", "key", "attr"])
def test_identity_free_strings_refused(field):
    desc = _desc("계속", slot=False)
    desc["identity"] = {"by": "query", "path": "/item", "key": "id", "value": "42",
                        "name": "계속", "role": "link", "attr": "data-testid"}
    desc["identity"][field] = HEX
    with pytest.raises(st.RecipeError, match="토큰"):
        st.compile_recipe("확인", [_nav(), _entry("click", desc=desc)], pins={1: "identity"})


@pytest.mark.parametrize("field", ["csel", "rel", "tsig", "landmark", "key", "name", "new_page_field"])
def test_other_free_string_fields_refused(field):
    rec = st.compile_auto([_nav(), _entry("click", desc=_desc("계속", slot=False))])
    rec["steps"][1]["variants"][0]["target"][field] = f"공개값 {HEX}"
    with pytest.raises(st.RecipeError, match="토큰"):
        st._check_paths(rec)


def test_auto_parameter_label_cannot_carry_token():
    desc = _desc(HEX, slot=False)
    with pytest.raises(st.RecipeError, match="토큰"):
        st.compile_auto([_nav(), _entry("type_text", {"text": "노트북"}, desc=desc)])


def test_hash_fields_remain_exempt():
    rec = st.compile_auto([_nav(), _entry("click", desc=_desc("계속"))])
    rec["steps"][1]["variants"][0]["skel"] = HEX
    rec["steps"][1]["variants"][0]["target"].update(chain=HEX, itpl=HEX)
    st._check_paths(rec)


@pytest.mark.parametrize("field", sorted(st._URL_FIELDS))
@pytest.mark.parametrize("token", [HEX, quote(B64, safe=""), "%33" + HEX[1:]])
def test_query_key_checked_in_every_url_field(field, token):
    rec = st.compile_auto([_nav(), _entry("click", desc=_desc("계속", slot=False))])
    rec["steps"][1]["variants"][0]["target"][field] = f"http://mock.test/help?{token}=1"
    with pytest.raises(st.RecipeError, match="토큰"):
        st._check_paths(rec)


@pytest.mark.parametrize("agent", [False, True], ids=["auto", "save"])
@pytest.mark.parametrize("delimiter", list("!$&'()*+,;=:@"))
def test_sub_delimiters_do_not_hide_tokens(delimiter, agent):
    token = HEX[:8] + delimiter + HEX[8:]
    url = "http://mock.test/reset/" + token
    with pytest.raises(st.RecipeError, match="토큰"):
        _compile([_nav(url), _entry("click", desc=_desc("계속", slot=False), url=url)], agent)


@pytest.mark.parametrize("path", [
    "/category/electronics-accessories", "/products/samsung-galaxy-s24-ultra",
    "/news/2024/10/10/breaking-news-headline", "/blog/how-to-set-up-python-3-12-on-macos",
    "/category/electronics%2Daccessories",
])
def test_ordinary_hyphen_slugs_still_save(path):
    url = "http://mock.test" + path
    assert st.compile_auto([_nav(url), _entry("click", desc=_desc("계속", slot=False), url=url)])["auto"]


def test_ordinary_hyphen_host_still_saves():
    url = "http://my-online-shop-store.test/help"
    assert st.compile_auto([_nav(url), _entry("click", desc=_desc("계속", slot=False), url=url)])["auto"]


@pytest.mark.parametrize("token", [
    B64, "a1b2c3d4-e5f6-47a8-b9c0-d1e2f3a4b5c6", "abcdefghi123-long-slug",
    "abcdefghijklm-long-slug", "zm9vymfy-qujdrevg-mtizndu2",
])
def test_hyphens_do_not_exempt_tokens(token):
    url = "http://mock.test/reset/" + token
    with pytest.raises(st.RecipeError, match="토큰"):
        st.compile_auto([_nav(url), _entry("click", desc=_desc("계속", slot=False), url=url)])


@pytest.mark.parametrize("field", sorted(st._URL_FIELDS))
def test_invalid_port_refused_in_every_url_field(field):
    rec = st.compile_auto([_nav(), _entry("click", desc=_desc("계속", slot=False))])
    rec["steps"][1]["variants"][0]["target"][field] = f"http://mock.test:{HEX}/help"
    with pytest.raises(st.RecipeError, match="토큰"):
        st._check_paths(rec)


class _R3Site(RecipeSite):
    def _page(self, path):
        parsed = urlsplit(path)
        p = parsed.path
        if p == "/inbox":
            link = f"<a href='/verify/{HEX}'>{self.url('/verify/' + HEX)}</a>"
        elif p == "/landing":
            link = f"<a href='/done?{parsed.query}'>계속</a>"
        elif p.startswith("/reset/"):
            link = "<a href='/done'>계속</a>"
        elif p == "/category/electronics-accessories":
            link = "<a href='/category/electronics-accessories?page=2'>次へ</a>"
        elif p.startswith("/verify/") or p == "/done":
            link = "<a href='/list'>홈</a>"
        else:
            return super()._page(path)
        return 200, HEAD.format(title="확인") + HEADER + "<main>" + link + "</main></body></html>"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,name,needle", [
    ("/inbox", None, HEX), (f"/landing?{HEX}=1", "계속", HEX),
    (f"/reset/{HEX[:8]}!{HEX[8:]}", "계속", HEX[:8]),
], ids=["S4-name", "S6-query-key", "S7-sub-delim"])
async def test_server_tokens_do_not_save_bytes(path, name, needle):
    with _R3Site() as site:
        async with BrowserMCPServer(headless=True, profile="r3") as srv:
            nav = await _call(srv, ActionType.NAVIGATE, url=site.url(path))
            assert nav.success, nav.error_message
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            target_name = name or site.url('/verify/' + HEX)
            click = await _call(srv, ActionType.CLICK, element_id=_eid(obs, target_name), epoch=obs.snapshot_epoch)
            assert click.success, click.error_message
            assert all("recipe_saved" not in r.data and not r.error_message for r in (nav, click))
            assert (await srv.call_server_tool("browser_recipe", {"op": "list"}))["data"]["recipes"] == []
            saved = await srv.call_server_tool("browser_recipe", {"op": "save", "name": "확인", "last_n": 2})
            assert saved["success"] is False
            assert "토큰" in saved["error_message"]
        file = _profile_file("r3")
        raw = file.read_bytes() if file.exists() else b""
        for form in (needle, quote(needle, safe=""), "".join(f"%{b:02X}" for b in needle.encode())):
            assert form.lower().encode() not in raw.lower()


@pytest.mark.asyncio
async def test_server_ordinary_slug_saves_bytes():
    with _R3Site() as site:
        async with BrowserMCPServer(headless=True, profile="r3-slug") as srv:
            nav = await _call(srv, ActionType.NAVIGATE, url=site.url("/category/electronics-accessories"))
            assert nav.success, nav.error_message
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            click = await _call(srv, ActionType.CLICK, element_id=_eid(obs, "次へ"), epoch=obs.snapshot_epoch)
            assert click.success, click.error_message
            assert click.data["recipe_saved"]["steps"] == 2
        raw = _profile_file("r3-slug").read_bytes()
        assert b"electronics-accessories" in raw
        assert len(json.loads(raw)["recipes"]) == 1
