"""WS-38b R2: 남은 URL 토큰 경로와 신호 값의 저장 회귀(로컬 Mock만)."""

from __future__ import annotations

import json
from urllib.parse import quote, urlsplit

import pytest

from contracts import ActionType
from harness.recipe_mock import HEAD, HEADER, RecipeSite
from interface.mcp_server import BrowserMCPServer
from recipes import store as st

from test_ws38b_autosave import _desc, _entry, _nav
from test_ws38b_r1 import B64, _call, _click, _eid, _profile_file

HEX = "3f2a9b8c7d6e5f4a3b2c1d0e9f8a7b6c"
JSID = "0123456789ABCDEF0123456789ABCDEF"


def _compile(entries, agent):
    return st.compile_recipe("확인", entries) if agent else st.compile_auto(entries)


@pytest.mark.parametrize("agent", [False, True], ids=["auto", "save"])
@pytest.mark.parametrize("segment", [
    HEX, "ABCDEFGHIJKLMNOPQRST1234", "a123456789012345",  # 혼합, 16자 경계
    "%61" + "123456789012345",                         # 디코드 뒤 16자 혼합
    "account;jsessionid=" + JSID, "account%3Bsid=abc", "plain;param",
])
def test_refuse_mixed_alnum_and_path_parameters(segment, agent):
    url = "http://mock.test/reset/" + segment
    with pytest.raises(st.RecipeError, match="토큰"):
        _compile([_nav(url), _click(url=url, to="http://mock.test/home")], agent)


@pytest.mark.parametrize("segment", [
    "123456789012345678901234", "ABCDEFGHIJKLMNOPQRSTUVWX", "a12345678901234",
])
def test_alnum_only_letters_digits_and_short_boundary_still_save(segment):
    url = "http://mock.test/products/" + segment
    rec = st.compile_auto([_nav(url), _click("장바구니(2)", url=url)])
    assert rec["auto"] is True
    assert segment in rec["steps"][0]["args"]["url"]


@pytest.mark.parametrize("pin", ["ui", "slot", "identity"])
@pytest.mark.parametrize("agent", [False, True], ids=["auto", "save"])
def test_target_url_fields_are_checked(pin, agent):
    desc = _desc("계속", slot=pin == "slot")
    if pin == "identity":
        desc["slot"] = None
        desc["identity"] = {"by": "query", "path": f"http://mock.test/reset/{HEX}",
                            "key": "id", "value": "123", "role": "link", "name": "계속"}
        # identity 는 명시적 save pin 으로만 선택된다.
        with pytest.raises(st.RecipeError, match="토큰"):
            st.compile_recipe("확인", [_nav(), _entry("click", {}, desc=desc)], pins={1: pin})
        return
    desc[pin]["href_pat"] = f"/unsub/{B64}"
    with pytest.raises(st.RecipeError, match="토큰"):
        _compile([_nav(), _entry("click", {}, desc=desc)], agent)


@pytest.mark.parametrize("field", ["origin", "step_origin", "index_pat", "url", "url_pat", "expect_url_pat", "href_pat", "path"])
def test_every_stored_url_field_checks_token_host(field):
    """저장 형태의 모든 URL 필드: 하나씩만 오염시켜 다른 필드가 가리지 않게 한다."""
    host = HEX + ".mock.test"
    rec = st.compile_auto([_nav(), _click()])
    step = rec["steps"][1]
    if field == "origin":
        rec["origin"] = "http://" + host
    elif field == "step_origin":
        step["origin"] = "http://" + host
    elif field == "index_pat":
        rec[field] = host + "/home"
    elif field == "url":
        rec["steps"][0]["args"][field] = "http://" + host + "/home"
    elif field == "expect_url_pat":
        step["expect"]["url_pat"] = host + "/home"
    elif field == "url_pat":
        step[field] = host + "/home"
    else:
        step["variants"][0]["target"][field] = "http://" + host + "/home"
    with pytest.raises(st.RecipeError, match="토큰"):
        st._check_paths(rec)


@pytest.mark.parametrize("host", [HEX + ".mock.test", "zm9vymfy-qujdrevg-mtizndu2.mock.test",
                                  "zm9vymfy_qujdrevg_mtizndu2.mock.test"])
def test_token_subdomains_refused(host):
    url = "http://" + host + "/home"
    with pytest.raises(st.RecipeError, match="토큰"):
        st.compile_auto([_nav(url), _click(url=url)])


@pytest.mark.parametrize("host", ["www.shop.mock.co.kr", "m.mock.test", "shop.mock.test",
                                  "abcdefghijklmnopqrst.mock.test", "1234567890123456.mock.test"])
def test_ordinary_host_labels_still_save(host):
    url = "http://" + host + "/products/12345"
    assert st.compile_auto([_nav(url), _click("장바구니(2)", url=url)])["auto"] is True


@pytest.mark.parametrize("agent", [False, True], ids=["auto", "save"])
def test_expect_signals_store_kinds_only(agent):
    click = _click()
    click["expect"]["signals"] = [f"url_changed:http://mock.test/reset/{HEX}",
                                    f"url_changed:/unsub/{B64}", " navigated:some-value "]
    rec = _compile([_nav(), click], agent)
    assert rec["steps"][1]["expect"]["signals"] == ["navigated", "url_changed"]
    raw = json.dumps(rec, ensure_ascii=False)
    assert HEX not in raw and B64 not in raw and "some-value" not in raw


class _RemainingTokenSite(RecipeSite):
    def _page(self, path):
        p = urlsplit(path).path
        link = None
        if p.startswith("/reset/"):
            link = "<a id='go' href='/done'>비밀번호 변경</a>"
        elif p.startswith("/account"):
            link = f"<a id='go' href='/orders;jsessionid={JSID}'>내역 보기</a>"
        elif p == "/inbox":
            link = (f"<a id='go' href='/unsub/{B64}' "
                    "onclick=\"event.preventDefault(); location.href='/unsubscribed'\">수신 거부</a>")
        elif p.startswith("/orders") or p in ("/done", "/unsubscribed"):
            link = "<a href='/list'>홈</a>"
        if link:
            return 200, HEAD.format(title="확인") + HEADER + "<main>" + link + "</main></body></html>"
        return super()._page(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("path,name,needle", [
    (f"/reset/{HEX}", "비밀번호 변경", HEX),
    (f"/account;jsessionid={JSID}", "내역 보기", JSID),
    ("/inbox", "수신 거부", B64),
], ids=["S1-alnum", "S2-semicolon", "S3-href"])
async def test_server_remaining_token_paths_do_not_save_bytes(path, name, needle):
    with _RemainingTokenSite() as site:
        async with BrowserMCPServer(headless=True, profile="r2") as srv:
            nav = await _call(srv, ActionType.NAVIGATE, url=site.url(path))
            assert nav.success, nav.error_message
            obs = await _call(srv, ActionType.OBSERVE_PAGE)
            click = await _call(srv, ActionType.CLICK, element_id=_eid(obs, name), epoch=obs.snapshot_epoch)
            assert click.success, click.error_message
            for result in (nav, click):
                assert "recipe_saved" not in result.data and result.error_message in (None, "")
            listed = await srv.call_server_tool("browser_recipe", {"op": "list"})
            assert listed["data"]["recipes"] == []
        file = _profile_file("r2")
        raw = file.read_bytes() if file.exists() else b""
        for form in (needle, quote(needle, safe=""), "".join(f"%{b:02X}" for b in needle.encode())):
            assert form.encode() not in raw
