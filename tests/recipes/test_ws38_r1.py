"""WS-38 R1: 독립 검증 NB-1·2·3·5 회귀 고정 (순수 파이썬 + Mock HTML).

* NB-1 params 치환은 경로 조각·쿼리 값에만 — scheme·host·port 금지. 재생 navigate 는 렌더된 URL 의
  출처가 기록(저장된 틀) 출처와 다르면 page_changed.
* NB-2 navigate URL 의 params 로 치환되지 않은 쿼리 값은 버린다(키는 유지). 민감 키 값은 params 로도 거부.
* NB-3 이동 뒤 주소가 chrome-error:// 등 빈 출처면 cross 성공이 아니다(재생 expect_mismatch·기록 안 함).
* NB-5 slot 목록 항목 ≥3 개의 대상 href 가 완전히 같으면 자리표시자 — not_ready.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from recipes import keys
from recipes import recorder as rc
from recipes import replay as rp
from recipes import store as st


def _nav(url: str, *, after_pat: str = "mock.test/search?q") -> Dict[str, Any]:
    return {
        "action": "navigate", "args": {"url": url}, "origin": "", "url": "about:blank", "url_pat": "",
        "skel": "", "ready": 0, "desc": None, "secret": False,
        "expect": {"nav": "cross", "url_pat": after_pat, "signals": ["navigated"]},
    }


# ---------------------------------------------------------------------------
# NB-1: 치환 범위
# ---------------------------------------------------------------------------


def test_param_equal_to_host_or_port_or_scheme_is_not_substituted():
    for value in ("127.0.0.1", "8123", "http"):
        with pytest.raises(st.RecipeError, match="어느 입력에도"):
            st.compile_recipe("x", [_nav("http://127.0.0.1:8123/search?q=abc", after_pat="127.0.0.1:8123/search?q")],
                              params={"h": value})


def test_path_segment_param_is_encoded_and_cannot_leave_origin():
    # WS-38b R1 NB-5: params 값이 이동 뒤 URL 패턴에 원문으로 남으면 save 도 거부한다.
    with pytest.raises(st.RecipeError):
        st.compile_recipe("x", [_nav("http://mock.test/u/alice/profile", after_pat="mock.test/u/alice/profile")],
                          params={"who": "alice"})
    rec = st.compile_recipe("x", [_nav("http://mock.test/u/alice/profile", after_pat="mock.test/u/{n}/profile")],
                            params={"who": "alice"})
    url = rec["steps"][0]["args"]["url"]
    assert url == "http://mock.test/u/{who:url}/profile"
    rendered = st.render_args(rec["steps"][0], {"who": "@evil.invalid/x?y#z"})["url"]
    assert keys.origin_of(rendered) == "http://mock.test"


def test_query_value_param_is_encoded_on_render():
    rec = st.compile_recipe("x", [_nav("http://mock.test/search?q=abc")], params={"q": "abc"})
    assert rec["steps"][0]["args"]["url"] == "http://mock.test/search?q={q:url}"
    rendered = st.render_args(rec["steps"][0], {"q": "x#@evil.invalid/"})["url"]
    assert rendered == "http://mock.test/search?q=x%23%40evil.invalid%2F"


class _Page:
    def __init__(self, url: str) -> None:
        self.url = url


class _Host:
    """재생 host 대역 — dispatch 가 불리면 기록만(실제로 누르지 않음)."""

    def __init__(self, url: str = "about:blank") -> None:
        self._page = _Page(url)
        self.dispatched: List[Any] = []

    def page(self):
        return self._page

    def epoch(self) -> int:
        return 0

    def register(self, found):
        return "@r1"

    async def dispatch(self, action, args):
        self.dispatched.append((action, dict(args)))
        self._page.url = args.get("url") or self._page.url
        return SimpleNamespace(success=True, current_url=self._page.url, data={"signals": ["navigated"]},
                               error_code=None, error_message=None)

    async def observe(self):
        return {}


def _store_with(steps, origin="http://127.0.0.1:8123", params=()):
    s = st.RecipeStore(None)
    rid = s.save({"name": "n", "origin": origin, "params": sorted(params), "index_pat": "",
                  "steps": steps})["id"]
    return s, rid


def _nav_step(url: str, pat: str = "") -> Dict[str, Any]:
    return {"action": "navigate", "args": {"url": url}, "origin": "", "url_pat": None, "variants": [],
            "expect": {"nav": "cross", "url_pat": pat or None, "signals": ["navigated"]}}


@pytest.mark.asyncio
async def test_replay_navigate_rendered_origin_differs_from_recorded_stops():
    """구버전 저장 레시피(호스트 자리 치환)도 재생에서 막는다(page_changed, 이동 안 함)."""
    s, rid = _store_with([_nav_step("http://{h}:8123/search?q=abc")], params=("h",))
    host = _Host()
    out = await rp.run_recipe(host, s, rid, {"h": "evil.invalid"})
    assert not out["success"]
    assert out["data"]["recipe"]["reason"] == "page_changed"
    assert host.dispatched == []


@pytest.mark.asyncio
async def test_replay_navigate_recorded_cross_origin_step_is_allowed():
    """기록에 있던 다른 출처 이동(저장된 틀의 출처 그대로)은 허용."""
    s, rid = _store_with([_nav_step("http://other.test/landing?q={q:url}", "other.test/landing?q")],
                         origin="http://127.0.0.1:8123", params=("q",))
    host = _Host("http://127.0.0.1:8123/list")
    out = await rp.run_recipe(host, s, rid, {"q": "abc"})
    assert out["success"], out
    assert host.dispatched and host.dispatched[0][1]["url"] == "http://other.test/landing?q=abc"


# ---------------------------------------------------------------------------
# NB-2: 쿼리 값 버림 + 민감 키
# ---------------------------------------------------------------------------


def test_unsubstituted_query_values_are_dropped_keys_kept():
    dropped: List[str] = []
    rec = st.compile_recipe(
        "x", [_nav("http://mock.test/list?token=SESSIONTOKEN123&user=alice%40example.com&q=abc#access_token=Z9",
                   after_pat="mock.test/list?q&token&user")],
        params={"q": "abc"}, dropped=dropped)
    url = rec["steps"][0]["args"]["url"]
    assert url == "http://mock.test/list?token=&user=&q={q:url}"
    raw = json.dumps(rec, ensure_ascii=False)
    for secret in ("SESSIONTOKEN123", "alice", "Z9"):
        assert secret not in raw
    assert dropped == ["token", "user"]


def test_userinfo_in_url_is_not_stored():
    rec = st.compile_recipe("x", [_nav("http://bob:pw123@mock.test/list", after_pat="mock.test/list")])
    assert "pw123" not in json.dumps(rec) and "bob" not in json.dumps(rec)
    assert rec["origin"] == "http://mock.test"


@pytest.mark.parametrize("key", ["token", "access_token", "SessionId", "PHPSESSID", "sid", "api_key",
                                 "apiKey", "code", "auth", "Authorization", "email", "user_email",
                                 "password", "client_secret"])
def test_sensitive_query_key_as_param_is_refused(key):
    with pytest.raises(st.RecipeError, match="민감"):
        st.compile_recipe("x", [_nav(f"http://mock.test/list?{key}=v4lue1", after_pat=f"mock.test/list?{key}")],
                          params={"p": "v4lue1"})


@pytest.mark.parametrize("key", ["q", "keyword", "query", "page", "author", "side"])
def test_ordinary_query_key_as_param_is_allowed(key):
    rec = st.compile_recipe("x", [_nav(f"http://mock.test/list?{key}=v4lue1", after_pat=f"mock.test/list?{key}")],
                            params={"p": "v4lue1"})
    assert rec["steps"][0]["args"]["url"] == f"http://mock.test/list?{key}={{p:url}}"


# ---------------------------------------------------------------------------
# NB-3: 오류 페이지·빈 출처 이동
# ---------------------------------------------------------------------------


def _click_step(nav: str) -> Dict[str, Any]:
    return {"action": "click", "expect": {"nav": nav, "url_pat": None, "signals": ["navigated"]}}


@pytest.mark.parametrize("after", ["chrome-error://chromewebdata/", "about:blank", "data:text/html,x"])
@pytest.mark.parametrize("nav", ["cross", "same", "none"])
def test_expect_ok_rejects_error_or_empty_origin_after_click(after, nav):
    result = SimpleNamespace(current_url=after, data={"signals": ["navigated"], "nav_committed": True})
    assert rp.expect_ok(_click_step(nav), "http://a.test/list", result) is not None


def test_expect_ok_rejects_error_page_after_navigate():
    step = {"action": "navigate", "expect": {"nav": "cross", "url_pat": None, "signals": []}}
    result = SimpleNamespace(current_url="chrome-error://chromewebdata/", data={})
    assert rp.expect_ok(step, "about:blank", result) is not None


def test_expect_ok_still_accepts_real_cross_origin():
    result = SimpleNamespace(current_url="https://b.test/x", data={"signals": ["navigated"]})
    assert rp.expect_ok(_click_step("cross"), "http://a.test/list", result) is None


def test_recorder_does_not_record_move_to_error_page():
    from recipes.service import RecipeService

    svc = RecipeService(SimpleNamespace(profile=None))
    action = SimpleNamespace(value="click")
    pre = {"url": "http://a.test/list", "skel": "s" * 12, "ready": 5, "target": {"ui": {}}}
    ok = SimpleNamespace(success=True, healed=False, current_url="http://a.test/item?id=1",
                         data={"signals": ["navigated"]})
    svc.post(action, {"element_id": "@e1"}, pre, ok, False)
    assert len(svc.trajectory.entries) == 1
    bad = SimpleNamespace(success=True, healed=False, current_url="chrome-error://chromewebdata/",
                          data={"signals": ["navigated"]})
    svc.post(action, {"element_id": "@e1"}, pre, bad, False)
    assert len(svc.trajectory.entries) == 0  # 끊김(오류 페이지 이동은 기록 안 함)
    assert svc.trajectory.last_break == "nav_error"
    assert rc.nav_kind("http://a.test/list", "chrome-error://chromewebdata/") == "error"


# ---------------------------------------------------------------------------
# NB-5: 같은 href 자리표시자
# ---------------------------------------------------------------------------


def _list_html(rows) -> str:
    lis = "".join(
        f"<li class='item'><span class='rank'>{i}.</span><a class='title' href='{href}'>{t}</a> "
        f"<a class='sub' href='{href}'>discuss</a></li>" for i, (href, t) in enumerate(rows, 1))
    return ("<html><body><header><nav><a href='/'>home</a></nav></header><main><section class='feed'>"
            f"<ul class='items'>{lis}</ul></section></main></body></html>")


REAL = [(f"/item?id={i}", n) for i, n in [(101, "Alpha"), (102, "Beta"), (103, "Gamma"), (104, "Delta"),
                                           (105, "Epsilon")]]


async def _locate_after(before: str, after: str, css: str):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            async def page_for(html):
                p = await (await browser.new_context()).new_page()

                async def _route(route):
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

                await p.route("**/*", _route)
                await p.goto("http://mock.test/list")
                return p

            snap = await keys.snapshot(await page_for(before), css)
            target = keys.make_target(snap["target"])
            return await keys.locate(await page_for(after), target)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_placeholder_items_with_identical_href_are_not_ready():
    placeholders = [("/item?id=0", "loading…")] * 5
    found = await _locate_after(_list_html(REAL), _list_html(placeholders),
                                "ul.items > li:nth-of-type(1) > a.title")
    assert not found["ok"]
    assert found["reason"] == "not_ready", found


@pytest.mark.asyncio
async def test_distinct_hrefs_and_fragment_links_still_found():
    found = await _locate_after(_list_html(REAL), _list_html(list(reversed(REAL))),
                                "ul.items > li:nth-of-type(1) > a.title")
    assert found["ok"], found
    # 같은 '#' 링크(자바스크립트 버튼 역할)는 자리표시자가 아니다
    hash_rows = [("#", n) for _, n in REAL]
    found = await _locate_after(_list_html(hash_rows), _list_html(hash_rows),
                                "ul.items > li:nth-of-type(2) > a.title")
    assert found["ok"], found
