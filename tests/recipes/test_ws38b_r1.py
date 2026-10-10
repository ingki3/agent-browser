"""WS-38b R1 — 자동 저장 누출 수정(독립 검증 BLOCKING-1 · NB-1~5 회귀).

* BLOCKING-1: 경로의 토큰(JWT·base64url·하이픈 토큰)·이메일 조각이 보이는 흐름은 자동 저장하지 않고(조용히),
  에이전트 save 는 이유와 함께 거부. 한글 퍼센트 경로는 통과. keys 의 `{id}` 접기는 그대로.
* NB-1: 저장될 대상 이름·식별값에 이메일·전화번호가 있으면 미저장/거부.
* NB-2: select_option 값도 자동 params(라벨, 없으면 choiceN) — 원문 없음.
* NB-3: 누출 비교는 NFKC + casefold.
* NB-4: 200개·1MB 정리는 자동 레시피부터.
* NB-5: 에이전트 save 도 params 값 누출 검사.
* 서버 경로(실제 Chromium + 로컬 Mock): 경로 base64url 토큰·이메일·JWT 로 이동 + 클릭 → 미저장, 파일에 없음.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any
from urllib.parse import quote

import pytest

from contracts import ActionType
from harness.recipe_mock import HEAD, HEADER, RecipeSite
from interface.mcp_server import BrowserMCPServer, tool_name
from recipes import keys
from recipes import store as st

from test_ws38b_autosave import _box, _desc, _entry, _flow, _nav

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlLXZhbHVl"
B64 = "Zm9vYmFy-QUJDREVG_MTIzNDU2"
EMAIL = "hyungjoo@example.com"


def _click(name="계속", *, url="http://mock.test/list", to=None, slot=False):
    exp = {"nav": "none", "url_pat": None, "signals": ["dom_changed"]}
    if to:
        exp = {"nav": "same", "url_pat": keys.url_pattern(to), "signals": ["navigated"]}
    return _entry("click", {}, desc=_desc(name, slot=slot), url=url, expect=exp)


def _raw(rec) -> str:
    return json.dumps(rec, ensure_ascii=False)


# ---------------------------------------------------------------------------
# BLOCKING-1 — 경로 토큰·이메일
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seg", [
    JWT,                                   # 점 포함 JWT
    B64,                                   # base64url(하이픈·밑줄)
    "a1b2c3d4-e5f6-g7h8-reset",            # 하이픈 토큰(UUID 아님)
    "QUJDREVGR0hJSktMTU5PUA%3D%3D",        # %3D 패딩 base64
    "abcDEF123~xyz789QQ",                  # ~ 포함
])
def test_auto_refuses_token_path_in_navigate(seg):
    url = f"http://mock.test/auth/callback/{seg}"
    with pytest.raises(st.RecipeError):
        st.compile_auto([_nav(url), _click(url=url, to="http://mock.test/home")])


def test_auto_refuses_token_path_only_in_click_url_pat():
    """이동 없이 토큰 경로 페이지에서 시작한 클릭 구간(url_pat·index_pat 에 토큰)."""
    url = f"http://mock.test/magic/{B64}"
    with pytest.raises(st.RecipeError):
        st.compile_auto([_click(url=url, to="http://mock.test/home"), _click("대시보드", url="http://mock.test/home")])
    # 클릭 뒤 이동 기대(expect.url_pat)에만 토큰이 있어도
    with pytest.raises(st.RecipeError):
        st.compile_auto([_nav(), _click(to=f"http://mock.test/reset/{JWT}")])


@pytest.mark.parametrize("seg", [EMAIL, quote(EMAIL, safe=""), "user%40example.com"])
def test_auto_refuses_email_in_path(seg):
    url = f"http://mock.test/u/{seg}/settings"
    with pytest.raises(st.RecipeError):
        st.compile_auto([_nav(url), _click("저장", url=url)])


def test_undecodable_or_control_char_segment_is_treated_as_token():
    for seg in ("abc%FFdef", "abc%00def"):
        url = f"http://mock.test/p/{seg}"
        with pytest.raises(st.RecipeError):
            st.compile_auto([_nav(url), _click(url=url)])


@pytest.mark.parametrize("path", [
    "/list",
    "/search/" + quote("노트북과무선이어폰가방"),   # 한글 퍼센트 경로(인코딩 뒤 16자 훨씬 넘음)
    "/blog/my-post",                              # 짧은 하이픈 조각
    "/item/12345",                                # 숫자 → {n}
    "/doc/ABCDEFGHIJKLMNOPQRSTUVWX",              # 긴 영문만 조각은 허용(R2)
])
def test_ordinary_paths_still_autosave(path):
    url = "http://mock.test" + path
    rec = st.compile_auto([_nav(url), _click(url=url, to="http://mock.test/home")])
    assert rec["auto"] is True


def test_agent_save_refuses_token_path_with_reason():
    url = f"http://mock.test/auth/callback/{JWT}"
    with pytest.raises(st.RecipeError, match="토큰"):
        st.compile_recipe("콜백", [_nav(url), _click(url=url)])
    with pytest.raises(st.RecipeError, match="이메일"):
        st.compile_recipe("설정", [_nav(f"http://mock.test/u/{EMAIL}"), _click(url=f"http://mock.test/u/{EMAIL}")])


def test_keys_segment_folding_unchanged():
    """재생 href_pat 비교를 지키려고 `{id}` 접기는 바꾸지 않는다(검사만 추가)."""
    assert keys._segment(B64) == B64
    assert keys._segment("ABCDEFGHIJKLMNOP") == "{id}"
    assert keys.url_pattern(f"http://mock.test/a/{B64}") == f"mock.test/a/{B64}"


# ---------------------------------------------------------------------------
# NB-1 — 대상 이름 PII
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    f"Hyungjoo Lee ({EMAIL})",
    "고객센터 010-1234-5678",
    "01012345678",
    "연락처 +82 10-1234-5678",
    "Ｈｙｕｎｇｊｏｏ＠ｅｘａｍｐｌｅ．ｃｏｍ",  # 전각(NFKC 뒤 이메일)
])
def test_auto_refuses_pii_in_target_name(name):
    with pytest.raises(st.RecipeError):
        st.compile_auto([_nav("http://mock.test/home"), _click(name, url="http://mock.test/home")])


def test_agent_save_refuses_pii_in_target_name_and_identity():
    with pytest.raises(st.RecipeError, match="이메일 또는 전화번호"):
        st.compile_recipe("계정", [_nav("http://mock.test/home"), _click(f"계정 {EMAIL}", url="http://mock.test/home")])
    d = _desc("프로필", slot=False)
    d["identity"] = {"by": "attr", "attr": "data-testid", "value": "010-9876-5432", "role": "link", "name": "프로필"}
    with pytest.raises(st.RecipeError):
        st.compile_recipe("프로필", [_nav(), _entry("click", {}, desc=d)], pins={1: "identity"})


@pytest.mark.parametrize("name", ["검색", "결과 2024", "상품 1,234,567원", "주문번호 2024101012"])
def test_pii_check_does_not_flag_ordinary_names(name):
    st.compile_auto([_nav("http://mock.test/home"), _click(name, url="http://mock.test/home")])


# ---------------------------------------------------------------------------
# NB-2 — select_option 값
# ---------------------------------------------------------------------------


def _select(value, label):
    d = _desc(label, slot=False)
    d.update(role="combobox", tag="select", href_pat=None)
    d["ui"].update(group="input", href_pat=None)
    return _entry("select_option", {"value": value}, desc=d, url="http://mock.test/signup")


def test_auto_select_values_become_params():
    rec = st.compile_auto([_nav("http://mock.test/signup"), _select("1985", "출생연도"), _select("여성", "성별"),
                           _select("서울", "")])
    raw = _raw(rec)
    for v in ("1985", "여성", "서울"):
        assert v not in raw
    assert [s["args"].get("value") for s in rec["steps"][1:]] == ["{출생연도}", "{성별}", "{choice1}"]
    assert sorted(rec["params"]) == sorted(["출생연도", "성별", "choice1"])
    assert st.render_args(rec["steps"][1], {"출생연도": "1990"})["value"] == "1990"


def test_auto_select_value_left_elsewhere_is_refused():
    with pytest.raises(st.RecipeError):
        st.compile_auto([_nav("http://mock.test/signup"), _select("1985", "출생연도"),
                         _click("1985년생 다음", url="http://mock.test/signup")])


def test_agent_save_select_only_params_substituted():
    ents = [_nav("http://mock.test/signup"), _select("1985", "출생연도"), _select("M", "성별")]
    rec = st.compile_recipe("가입", ents, params={"year": "1985"})
    assert rec["steps"][1]["args"]["value"] == "{year}"
    assert rec["steps"][2]["args"]["value"] == "M"  # params 로 안 준 값은 그대로(기존 동작)
    plain = st.compile_recipe("가입", ents)
    assert plain["steps"][1]["args"]["value"] == "1985"


# ---------------------------------------------------------------------------
# NB-3 — 유니코드 정규화
# ---------------------------------------------------------------------------


def test_leak_check_normalizes_nfd_and_fullwidth():
    nfd = unicodedata.normalize("NFD", "café-résumé")
    nfc = unicodedata.normalize("NFC", "café-résumé")
    with pytest.raises(st.RecipeError):
        st.compile_auto([_nav(), _entry("type_text", {"text": nfd}, desc=_box()),
                         _click(f"{nfc} 검색", to="http://mock.test/search?q=x")])
    with pytest.raises(st.RecipeError):
        st.compile_auto([_nav(), _entry("type_text", {"text": "ＭｙＷｏｒｄ"}, desc=_box()),
                         _click("myword 검색", to="http://mock.test/search?q=x")])


# ---------------------------------------------------------------------------
# NB-4 — 정리 순서(자동 먼저)
# ---------------------------------------------------------------------------


def _auto_flow(i: int):
    return [_nav(f"http://mock.test/p{i}"), _entry("type_text", {"text": f"q{i}"}, desc=_box()),
            _click("검색", url=f"http://mock.test/p{i}", to="http://mock.test/search?q=x")]


def test_count_cap_evicts_auto_before_agent_recipes(tmp_path):
    t = [1000.0]
    s = st.RecipeStore(tmp_path / "r.json", clock=lambda: t[0])
    agent = s.save(st.compile_recipe("중요 검색", _flow(), params={"q": "노트북"}))["id"]
    for i in range(st.MAX_RECIPES + 5):
        t[0] += 1
        s.save_auto(st.compile_auto(_auto_flow(i)))
    assert len(s) == st.MAX_RECIPES
    assert s.get(agent) is not None  # 가장 오래됐지만 에이전트 레시피는 남는다
    # 자동이 모두 밀린 뒤에야 에이전트 레시피도 LRU
    autos = sorted((r for r in s.list() if r.get("auto")), key=lambda r: r["stats"]["last_used_at"])
    assert autos[0]["stats"]["last_used_at"] > 1000.0 + 5


def test_byte_cap_evicts_auto_before_agent_recipes(tmp_path, monkeypatch):
    t = [1000.0]
    s = st.RecipeStore(tmp_path / "r.json", clock=lambda: t[0])
    agent = s.save(st.compile_recipe("중요 검색", _flow(), params={"q": "노트북"}))["id"]
    one = len(s._payload())
    monkeypatch.setattr(st, "MAX_FILE_BYTES", one * 4)
    for i in range(12):
        t[0] += 1
        s.save_auto(st.compile_auto(_auto_flow(i)))
    assert len(s._payload()) <= one * 4
    assert s.get(agent) is not None
    assert len(s) < 13


# ---------------------------------------------------------------------------
# NB-5 — 에이전트 save 누출
# ---------------------------------------------------------------------------


def test_agent_save_refuses_param_value_left_in_url_pat():
    ents = [_nav(), _entry("type_text", {"text": "노트북"}, desc=_box()),
            _click("검색", to="http://mock.test/search/노트북")]
    with pytest.raises(st.RecipeError, match="입력값") as exc:
        st.compile_recipe("검색", ents, params={"q": "노트북"})
    assert "노트북" not in str(exc.value)
    # 퍼센트 인코딩으로 남아도
    ents[2] = _click("검색", to="http://mock.test/search/" + quote("노트북"))
    with pytest.raises(st.RecipeError):
        st.compile_recipe("검색", ents, params={"q": "노트북"})


def test_agent_save_refuses_param_value_in_target_name():
    ents = [_nav(), _entry("type_text", {"text": "노트북"}, desc=_box()),
            _click("노트북 검색", to="http://mock.test/search?q=x")]
    with pytest.raises(st.RecipeError):
        st.compile_recipe("검색", ents, params={"q": "노트북"})


# ---------------------------------------------------------------------------
# 서버 경로(MCP call_tool) — 실제 Chromium + 로컬 Mock
# ---------------------------------------------------------------------------


class _TokenSite(RecipeSite):
    """인증·재설정 링크처럼 경로에 토큰·이메일이 실린 페이지(누르면 /list)."""

    def _page(self, path):
        from urllib.parse import urlsplit

        p = urlsplit(path).path
        if p.startswith(("/magic/", "/auth/callback/", "/u/")):
            return 200, (HEAD.format(title="확인") + HEADER
                         + "<main><p>인증 링크</p><a id='go' href='/list'>계속</a></main></body></html>")
        return super()._page(path)


@pytest.fixture
def token_site():
    with _TokenSite() as s:
        yield s


async def _call(srv, action: ActionType, **args: Any):
    return await srv.call_tool(tool_name(action), args)


def _eid(obs, name: str) -> str:
    for el in obs.data["observation"]["elements"]:
        if el["name"] == name:
            return el["element_id"]
    raise AssertionError(f"{name} 없음")


def _profile_file(profile: str):
    from browser.serve_profile import profile_dir

    return profile_dir(profile) / "recipes.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,path,needle", [
    ("tok1", f"/magic/{B64}.sig", B64),
    ("tok2", f"/u/{EMAIL}/settings", EMAIL),
    ("tok3", f"/auth/callback/{JWT}", JWT.split(".")[1]),
])
async def test_server_token_or_email_path_flow_is_not_saved(token_site, _isolated_profile_root, profile, path,
                                                            needle):
    async with BrowserMCPServer(headless=True, profile=profile) as srv:
        nav = await _call(srv, ActionType.NAVIGATE, url=token_site.url(path))
        assert nav.success, nav.error_message
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        r1 = await _call(srv, ActionType.CLICK, element_id=_eid(obs, "계속"), epoch=obs.snapshot_epoch)
        assert r1.success, r1.error_message
        for r in (nav, r1):
            assert "recipe_saved" not in r.data and r.error_message in (None, "")
        listed = (await srv.call_server_tool("browser_recipe", {"op": "list"}))["data"]["recipes"]
        assert listed == []
    f = _profile_file(profile)
    raw = f.read_bytes() if f.exists() else b""
    for form in (needle, quote(needle, safe=""), quote(needle)):
        assert form.encode() not in raw
