"""WS-38b 레시피 자동 저장 — 구간(흐름) 나누기 · 자동 params · 중복 없는 upsert · 알림 정리.

설계 §9-1(2026-10-10 사용자 결정): 통과한 흐름은 도구가 자동 저장한다. 실행(run)은 에이전트가 고른다.

* 순수 파이썬: compile_auto(자동 params·이름·민감 키·원문 누출 거부), RecipeStore.save_auto(구간당 1개·
  같은 구조 병합·에이전트 save 가 자동 레시피를 덮어씀), 구간 규칙(출처 변경·MAX_STEPS·비밀 단계).
* 서버 경로(실제 Chromium + 로컬 Mock, 외부 접속 없음): MCP call_tool 만으로 저장·재시작 후 후보·run.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from contracts import ActionType
from harness.recipe_mock import ROWS, RecipeSite
from interface.mcp_server import BrowserMCPServer, tool_name
from recipes import store as st
from recipes.service import RecipeService

RECIPE_TOOL = "browser_recipe"


# ---------------------------------------------------------------------------
# 궤적 항목(기록기가 만드는 모양) — tests/recipes/test_ws38_store.py 와 같은 모양
# ---------------------------------------------------------------------------


def _desc(name="Alpha", *, slot=True, secret=False, ordinal=0):
    return {
        "role": "link", "name": name, "tag": "a", "sig": "a.title", "href_pat": "/item?id",
        "secret": secret,
        "ui": {"group": "link", "name": name.lower(), "landmark": "main", "pos": [100, 200],
               "href_pat": "/item?id"},
        "slot": ({"chain": "c" * 12, "csel": "ul.items", "itpl": "i" * 12, "ordinal": ordinal,
                  "rel": "a:nth-of-type(1)", "tsig": "a.title", "role": "link",
                  "href_pat": "/item?id"} if slot else None),
        "identity": None, "identity_refused": "식별값 없음",
    }


def _box(name="검색어", *, secret=False):
    d = _desc(name, slot=False, secret=secret)
    d.update(role="searchbox", tag="input", href_pat=None)
    d["ui"].update(group="input", href_pat=None, landmark="search")
    return d


def _entry(action, args=None, *, desc=None, url="http://mock.test/list", skel="s" * 12, expect=None,
           secret=False):
    return {
        "action": action, "args": dict(args or {}), "origin": st.keys.origin_of(url), "url": url,
        "url_pat": st.keys.url_pattern(url), "skel": skel, "ready": 20, "desc": desc, "secret": secret,
        "expect": expect or {"nav": "none", "url_pat": None, "signals": ["element_value_changed"]},
    }


def _nav(url="http://mock.test/list"):
    return _entry("navigate", {"url": url}, url="about:blank",
                  expect={"nav": "cross", "url_pat": st.keys.url_pattern(url), "signals": ["navigated"]})


def _flow(query="노트북", *, ordinal=0):
    """navigate → type_text(검색어) → click(검색) → click(결과 첫 자리)."""
    return [
        _nav(),
        _entry("type_text", {"text": query}, desc=_box()),
        _entry("click", {}, desc=_desc("검색", slot=False),
               expect={"nav": "same", "url_pat": "mock.test/search?q", "signals": ["navigated"]}),
        _entry("click", {}, desc=_desc(f"{query} 결과 1", ordinal=ordinal),
               url="http://mock.test/search?q=x",
               expect={"nav": "same", "url_pat": "mock.test/item?id", "signals": ["navigated"]}),
    ]


# ---------------------------------------------------------------------------
# compile_auto
# ---------------------------------------------------------------------------


def test_compile_auto_turns_every_typed_text_into_params_named_by_label():
    rec = st.compile_auto(_flow("노트북"))
    assert rec["auto"] is True
    assert rec["params"] == ["검색어"]
    assert rec["steps"][1]["args"]["text"] == "{검색어}"
    raw = json.dumps(rec, ensure_ascii=False)
    assert "노트북" not in raw  # 입력 원문(과 그 원문이 든 대상 이름)은 남지 않는다
    assert rec["name"].startswith("자동: /list ") and len(rec["name"]) <= 40
    assert "검색어 입력" in rec["name"] and "클릭×2" in rec["name"]
    # 렌더하면 새 값으로 돈다
    assert st.render_args(rec["steps"][1], {"검색어": "이어폰"})["text"] == "이어폰"


def test_compile_auto_unlabeled_or_clashing_boxes_get_text_n():
    entries = [
        _nav(),
        _entry("type_text", {"text": "kim"}, desc=_box("")),
        _entry("type_text", {"text": "seoul"}, desc=_box("")),
        _entry("type_text", {"text": "busan"}, desc=_box("도시")),
        _entry("type_text", {"text": "daegu"}, desc=_box("도시")),
    ]
    rec = st.compile_auto(entries)
    assert rec["params"] == sorted(["text1", "text2", "도시", "text3"])
    assert [s["args"].get("text") for s in rec["steps"][1:]] == ["{text1}", "{text2}", "{도시}", "{text3}"]
    raw = json.dumps(rec, ensure_ascii=False)
    for v in ("kim", "seoul", "busan", "daegu"):
        assert v not in raw


def test_compile_auto_same_value_typed_twice_is_one_param():
    entries = [_nav(), _entry("type_text", {"text": "노트북"}, desc=_box()),
               _entry("type_text", {"text": "노트북"}, desc=_box("다시"))]
    rec = st.compile_auto(entries)
    assert rec["params"] == ["검색어"]


def test_auto_param_name_never_carries_the_value():
    assert st.auto_param_name("서울 지점", "서울", set()) == ""  # 라벨에 값이 들어 있으면 이름으로 안 씀
    assert st.auto_param_name("검색어를 입력하세요!", "노트북", set()) == "검색어를_입력하세요"
    assert st.auto_param_name("검색어", "노트북", {"검색어"}) == ""  # 겹치면 textN 으로
    assert st.auto_param_name("123", "x", set()) == ""
    assert len(st.auto_param_name("가" * 50, "x", set())) <= 32


def test_compile_auto_value_seen_in_stored_text_is_refused():
    """입력값이 저장될 문자열(대상 이름·URL 패턴)에 보이면 저장하지 않는다(원문 누출 fail-closed)."""
    entries = [_nav(), _entry("type_text", {"text": "서울"}, desc=_box("서울 지점"))]
    with pytest.raises(st.RecipeError):
        st.compile_auto(entries)


def test_compile_auto_navigate_url_value_becomes_param():
    entries = [_entry("navigate", {"url": "http://mock.test/s/노트북?q=노트북&page=2"}, url="about:blank",
                      expect={"nav": "cross", "url_pat": "mock.test/s/노트북?page&q", "signals": []}),
               _entry("type_text", {"text": "노트북"}, desc=_box())]
    # 이동 뒤 URL 패턴(경로)에 입력값이 남으면 → 원문 누출이라 저장하지 않는다
    with pytest.raises(st.RecipeError):
        st.compile_auto(entries)
    entries[0]["expect"]["url_pat"] = "mock.test/s/{n}?page&q"
    rec = st.compile_auto(entries)
    url = rec["steps"][0]["args"]["url"]
    assert "{검색어:url}" in url and "노트북" not in url and "page=" in url


def test_compile_auto_refuses_sensitive_query_key_navigation():
    entries = [_nav("http://mock.test/list?token=ABCDEF123"),
               _entry("type_text", {"text": "노트북"}, desc=_box())]
    with pytest.raises(st.RecipeError):
        st.compile_auto(entries)


def test_compile_auto_refuses_secret_steps():
    with pytest.raises(st.RecipeError):
        st.compile_auto([_nav(), _entry("type_text", {"text": "pw"}, desc=_box("비밀번호", secret=True))])
    with pytest.raises(st.RecipeError):
        st.compile_auto([_nav(), _entry("type_text", {"text": "X-LOGIN"}, desc=_box(), secret=True)])


def test_param_names_accept_unicode_letters_but_not_spaces():
    rec = st.compile_recipe("x", _flow()[1:2], params={"검색어": "노트북"})
    assert rec["params"] == ["검색어"]
    with pytest.raises(st.RecipeError):
        st.compile_recipe("x", _flow()[1:2], params={"검 색": "노트북"})


# ---------------------------------------------------------------------------
# RecipeStore.save_auto
# ---------------------------------------------------------------------------


def test_save_auto_grows_one_recipe_per_segment(tmp_path):
    s = st.RecipeStore(tmp_path / "r.json")
    flow = _flow()
    a = s.save_auto(st.compile_auto(flow[:2]))
    assert a["created"] and not a["merged"]
    b = s.save_auto(st.compile_auto(flow[:3]), replace=a["id"])
    c = s.save_auto(st.compile_auto(flow), replace=b["id"])
    assert a["id"] == b["id"] == c["id"]
    assert [len(r["steps"]) for r in s.list()] == [4]  # 짧은 판은 대체 — 구간당 1개, 가장 긴 판
    assert s.get(c["id"])["auto"] is True


def test_save_auto_same_structure_with_other_query_merges(tmp_path):
    s = st.RecipeStore(tmp_path / "r.json")
    first = s.save_auto(st.compile_auto(_flow("노트북")))
    again = s.save_auto(st.compile_auto(_flow("이어폰")))
    assert again["id"] == first["id"] and again["merged"] and not again["created"]
    # 구간이 자라며 만든 짧은 판(소유)은 같은 구조의 기존 레시피에 합쳐질 때 지운다
    short = s.save_auto(st.compile_auto(_flow("마우스")[:3]))
    assert short["created"]
    full = s.save_auto(st.compile_auto(_flow("마우스")), replace=short["id"])
    assert full["id"] == first["id"] and full["merged"]
    assert len(s.list()) == 1 and s.get(short["id"]) is None
    raw = (tmp_path / "r.json").read_bytes().decode("utf-8")
    for q in ("노트북", "이어폰", "마우스"):
        assert q not in raw


def test_segment_repeats_with_other_queries_leave_one_recipe():
    svc = _svc()
    for q in ("노트북", "이어폰", "무선 마우스"):
        _feed(svc, _flow(q))
    recs = svc.store.list()
    assert len(recs) == 1 and len(recs[0]["steps"]) == 4
    raw = json.dumps(recs, ensure_ascii=False)
    for q in ("노트북", "이어폰", "무선 마우스"):
        assert q not in raw


def test_save_auto_other_target_is_another_recipe(tmp_path):
    s = st.RecipeStore(tmp_path / "r.json")
    s.save_auto(st.compile_auto(_flow(ordinal=0)))
    s.save_auto(st.compile_auto(_flow(ordinal=2)))  # 같은 페이지·같은 동작 순서, 다른 자리
    assert len(s.list()) == 2


def test_agent_save_overwrites_same_structure_auto_recipe(tmp_path):
    s = st.RecipeStore(tmp_path / "r.json")
    auto = s.save_auto(st.compile_auto(_flow("노트북")))
    manual = s.save(st.compile_recipe("노트북 검색 첫 결과", _flow("노트북"), params={"query": "노트북"}))
    assert manual["id"] == auto["id"] and manual["merged"] is True
    rec = s.get(auto["id"])
    assert rec["auto"] is False and rec["name"] == "노트북 검색 첫 결과" and rec["params"] == ["query"]
    assert len(s.list()) == 1
    # 이후 같은 구조의 자동 저장은 에이전트가 붙인 이름·params 를 바꾸지 않는다
    again = s.save_auto(st.compile_auto(_flow("이어폰")))
    assert again["id"] == auto["id"] and again["merged"]
    rec = s.get(auto["id"])
    assert rec["auto"] is False and rec["name"] == "노트북 검색 첫 결과" and rec["params"] == ["query"]
    assert rec["steps"][1]["args"]["text"] == "{query}"


def test_save_auto_does_not_replace_a_recipe_the_agent_took_over(tmp_path):
    s = st.RecipeStore(tmp_path / "r.json")
    short = s.save_auto(st.compile_auto(_flow()[:3]))
    s.save(st.compile_recipe("내 것", _flow()[:3], params={"q": "노트북"}))
    longer = s.save_auto(st.compile_auto(_flow()), replace=short["id"])
    assert longer["id"] != short["id"]
    assert sorted(len(r["steps"]) for r in s.list()) == [3, 4]


def test_candidates_carry_auto_and_ok_and_sort_by_ok_then_recent(tmp_path):
    t = [100.0]
    s = st.RecipeStore(tmp_path / "r.json", clock=lambda: t[0])
    a = s.save_auto(st.compile_auto(_flow(ordinal=0)))
    t[0] = 200.0
    b = s.save_auto(st.compile_auto(_flow(ordinal=1)))
    t[0] = 300.0
    c = s.save_auto(st.compile_auto(_flow(ordinal=2)))
    s.note_run(a["id"], ok=True)
    s.note_run(a["id"], ok=True)
    t[0] = 400.0
    s.note_run(b["id"], ok=True)
    order = [r["id"] for r in s.candidates("http://mock.test", "mock.test/list")]
    assert order == [a["id"], b["id"], c["id"]]


# ---------------------------------------------------------------------------
# 구간 규칙(서비스 단위 — 가짜 서버, 메모리 저장소)
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}


class _FakeServer:
    profile = None


def _svc() -> RecipeService:
    return RecipeService(_FakeServer())


def _feed(svc: RecipeService, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for e in entries:
        r = _Result()
        svc.segment_add(e, r)
        out.append(r.data)
    return out


def test_segment_saves_from_two_steps_and_announces_once():
    svc = _svc()
    datas = _feed(svc, _flow())
    assert "recipe_saved" not in datas[0]
    saved = datas[1]["recipe_saved"]
    assert saved["steps"] == 2 and saved["params"] == ["검색어"] and saved["id"]
    assert all("recipe_saved" not in d for d in datas[2:])  # 갱신 때는 붙이지 않는다
    assert [len(r["steps"]) for r in svc.store.list()] == [4]
    assert all("recipe_hint" not in d for d in datas)


def test_one_step_segment_is_not_saved():
    svc = _svc()
    _feed(svc, [_nav(), _nav("http://mock.test/other")])
    assert svc.store.list() == []


def test_navigate_starts_new_segment():
    svc = _svc()
    _feed(svc, _flow()[:3] + [_nav("http://mock.test/list?new=1"),
                              _entry("click", {}, desc=_desc("Beta", ordinal=1),
                                     url="http://mock.test/list?new=1")])
    assert sorted(len(r["steps"]) for r in svc.store.list()) == [2, 3]


def test_origin_change_starts_new_segment():
    svc = _svc()
    other = "http://other.test/list"
    _feed(svc, _flow()[:3] + [_entry("click", {}, desc=_desc("x"), url=other),
                              _entry("click", {}, desc=_desc("y", ordinal=1), url=other)])
    recs = svc.store.list()
    assert sorted((r["origin"], len(r["steps"])) for r in recs) == [
        ("http://mock.test", 3), ("http://other.test", 2)]


def test_segment_over_max_steps_starts_new_one():
    svc = _svc()
    steps = [_nav()] + [_entry("click", {}, desc=_desc(f"n{i}", ordinal=i % 5)) for i in range(22)]
    _feed(svc, steps)
    assert sorted(len(r["steps"]) for r in svc.store.list()) == [3, st.MAX_STEPS]


def test_reset_starts_new_segment():
    svc = _svc()
    _feed(svc, _flow()[:2])
    svc.reset("failed")
    _feed(svc, [_entry("click", {}, desc=_desc("x"))])
    assert [len(r["steps"]) for r in svc.store.list()] == [2]  # 끊긴 뒤 1단계는 저장 안 함


def test_secret_step_cuts_segment_and_is_never_saved():
    svc = _svc()
    datas = _feed(svc, [
        _nav("http://mock.test/login"),
        _entry("type_text", {"text": "kim01"}, desc=_box("아이디"), url="http://mock.test/login"),
        _entry("type_text", {"text": "pw-secret-123"}, desc=_box("비밀번호", secret=True),
               url="http://mock.test/login"),
        _entry("click", {}, desc=_desc("로그인", slot=False), url="http://mock.test/login"),
        _entry("click", {}, desc=_desc("내 정보", slot=False), url="http://mock.test/login"),
    ])
    recs = svc.store.list()
    assert [len(r["steps"]) for r in recs] == [2]  # 비밀 단계 앞까지만, 이후는 저장 안 함
    raw = json.dumps(recs, ensure_ascii=False)
    assert "pw-secret-123" not in raw and "kim01" not in raw
    assert all("error" not in json.dumps(d) for d in datas)


def test_sensitive_query_navigation_segment_silently_skipped():
    svc = _svc()
    datas = _feed(svc, [_nav("http://mock.test/list?session=S3CR3T"),
                        _entry("click", {}, desc=_desc("Alpha")),
                        _entry("click", {}, desc=_desc("Beta", ordinal=1))])
    assert svc.store.list() == []
    assert datas == [{}, {}, {}]


# ---------------------------------------------------------------------------
# 서버 경로(MCP call_tool) — 실제 Chromium + 로컬 Mock
# ---------------------------------------------------------------------------


@pytest.fixture
def site():
    with RecipeSite() as s:
        yield s


async def call(srv, action: ActionType, **args: Any):
    return await srv.call_tool(tool_name(action), args)


async def observe(srv):
    res = await call(srv, ActionType.OBSERVE_PAGE)
    assert res.success, res.error_message
    return res


def eid(obs, name: str, role: str = "") -> str:
    for el in obs.data["observation"]["elements"]:
        if el["name"] == name and (not role or el["role"] == role):
            return el["element_id"]
    raise AssertionError(f"{name} 없음: {[e['name'] for e in obs.data['observation']['elements']]}")


async def search_flow(srv, site, query: str) -> List[Any]:
    """save 를 부르지 않고 navigate → type_text → click(검색) → click(첫 결과)."""
    results = [await call(srv, ActionType.NAVIGATE, url=site.url("/list"))]
    obs = await observe(srv)
    results.append(await call(srv, ActionType.TYPE_TEXT, element_id=eid(obs, "검색어"), text=query,
                              epoch=obs.snapshot_epoch))
    results.append(await call(srv, ActionType.CLICK, element_id=eid(obs, "검색", "button"),
                              epoch=obs.snapshot_epoch))
    obs = await observe(srv)
    results.append(await call(srv, ActionType.CLICK, element_id=eid(obs, f"{query} 결과 1"),
                              epoch=obs.snapshot_epoch))
    for r in results:
        assert r.success, r.error_message
    return results


def _file(profile: str):
    from browser.serve_profile import profile_dir

    return profile_dir(profile) / "recipes.json"


@pytest.mark.asyncio
async def test_server_autosaves_flow_without_save_call(site, _isolated_profile_root):
    async with BrowserMCPServer(headless=True, profile="auto1") as srv:
        results = await search_flow(srv, site, "노트북")
        saved = [r.data.get("recipe_saved") for r in results]
        assert saved[0] is None and saved[1] is not None and saved[2] is None and saved[3] is None
        assert saved[1]["params"] == ["검색어"] and saved[1]["steps"] == 2
        assert all("recipe_hint" not in r.data for r in results)
        listed = (await srv.call_server_tool(RECIPE_TOOL, {"op": "list"}))["data"]["recipes"]
        assert len(listed) == 1 and listed[0]["steps"] == 4 and listed[0]["params"] == ["검색어"]
        assert listed[0]["auto"] is True and listed[0]["id"] == saved[1]["id"]
    raw = _file("auto1").read_bytes()
    assert "노트북".encode() not in raw
    from urllib.parse import quote

    assert quote("노트북").encode() not in raw


@pytest.mark.asyncio
async def test_server_repeat_three_queries_one_recipe_then_new_session_runs(site, _isolated_profile_root):
    async with BrowserMCPServer(headless=True, profile="auto2") as srv:
        for q in ("노트북", "이어폰", "모니터"):
            await search_flow(srv, site, q)
        listed = (await srv.call_server_tool(RECIPE_TOOL, {"op": "list"}))["data"]["recipes"]
        assert len(listed) == 1, listed
        rid = listed[0]["id"]
    raw = _file("auto2").read_bytes().decode("utf-8")
    for q in ("노트북", "이어폰", "모니터"):
        assert q not in raw
    async with BrowserMCPServer(headless=True, profile="auto2") as srv2:
        ok = 0
        for i, q in enumerate(("무선 이어폰", "키보드", "가방")):
            await call(srv2, ActionType.NAVIGATE, url=site.url("/list"))
            obs = await observe(srv2)
            cands = obs.data["recipes"]["candidates"]
            assert [c["id"] for c in cands] == [rid]
            assert cands[0]["auto"] is True and cands[0]["ok"] == i
            site.clear_log()
            out = await srv2.call_server_tool(RECIPE_TOOL, {"op": "run", "id": rid, "params": {"검색어": q}})
            assert out["success"], out
            from urllib.parse import quote

            assert site.opened("/search") == ["/search?q=" + quote(q)]
            assert site.opened("/item") == ["/item?id=301"]
            ok += 1
        assert ok == 3


@pytest.mark.asyncio
async def test_server_password_and_secret_steps_never_saved(site, _isolated_profile_root):
    from security import SecretStore

    secrets = SecretStore({"X-LOGIN": "realaccount77"})
    async with BrowserMCPServer(headless=True, profile="auto3", secrets=secrets) as srv:
        await call(srv, ActionType.NAVIGATE, url=site.url("/login"))
        obs = await observe(srv)
        e = obs.snapshot_epoch
        r_id = await call(srv, ActionType.TYPE_TEXT, element_id=eid(obs, "아이디"), text="X-LOGIN", epoch=e)
        assert r_id.success and r_id.data.get("secret_resolved") is True
        r_pw = await call(srv, ActionType.TYPE_TEXT, element_id=eid(obs, "비밀번호"), text="pw-secret-123",
                          epoch=e)
        assert r_pw.success, r_pw.error_message
        r_btn = await call(srv, ActionType.CLICK, element_id=eid(obs, "로그인", "button"), epoch=e)
        assert r_btn.success, r_btn.error_message
        for r in (r_id, r_pw, r_btn):
            assert "recipe_saved" not in r.data
        assert (await srv.call_server_tool(RECIPE_TOOL, {"op": "list"}))["data"]["recipes"] == []
        # 비밀 아닌 첫 칸 → 저장(2단계), 비밀번호 칸부터는 저장 안 함
        await call(srv, ActionType.NAVIGATE, url=site.url("/login"))
        obs = await observe(srv)
        e = obs.snapshot_epoch
        assert (await call(srv, ActionType.TYPE_TEXT, element_id=eid(obs, "아이디"), text="kim01",
                           epoch=e)).success
        assert (await call(srv, ActionType.TYPE_TEXT, element_id=eid(obs, "비밀번호"), text="pw-secret-123",
                           epoch=e)).success
        assert (await call(srv, ActionType.CLICK, element_id=eid(obs, "로그인", "button"), epoch=e)).success
        listed = (await srv.call_server_tool(RECIPE_TOOL, {"op": "list"}))["data"]["recipes"]
        assert [r["steps"] for r in listed] == [2]
    raw = _file("auto3").read_bytes().decode("utf-8")
    for bad in ("pw-secret-123", "realaccount77", "X-LOGIN", "kim01"):
        assert bad not in raw


@pytest.mark.asyncio
async def test_server_sensitive_query_segment_not_saved_without_error(site, _isolated_profile_root):
    async with BrowserMCPServer(headless=True, profile="auto4") as srv:
        nav = await call(srv, ActionType.NAVIGATE, url=site.url("/list?token=TOKVALUE99"))
        obs = await observe(srv)
        r1 = await call(srv, ActionType.CLICK, element_id=eid(obs, ROWS[0][1]), epoch=obs.snapshot_epoch)
        assert nav.success and r1.success
        for r in (nav, r1):
            assert "recipe_saved" not in r.data and r.error_message in (None, "")
        listed = (await srv.call_server_tool(RECIPE_TOOL, {"op": "list"}))["data"]["recipes"]
        assert listed == []
    assert not _file("auto4").exists() or "TOKVALUE99" not in _file("auto4").read_text("utf-8")


@pytest.mark.asyncio
async def test_server_failure_breaks_segment(site):
    async with BrowserMCPServer(headless=True) as srv:
        await call(srv, ActionType.NAVIGATE, url=site.url("/list"))
        obs = await observe(srv)
        bad = await call(srv, ActionType.CLICK, element_id="@e999", epoch=obs.snapshot_epoch)
        assert not bad.success
        r = await call(srv, ActionType.CLICK, element_id=eid(obs, ROWS[0][1]), epoch=obs.snapshot_epoch)
        assert r.success and "recipe_saved" not in r.data
        assert (await srv.call_server_tool(RECIPE_TOOL, {"op": "list"}))["data"]["recipes"] == []


@pytest.mark.asyncio
async def test_server_healed_breaks_segment(site, monkeypatch):
    async with BrowserMCPServer(headless=True) as srv:
        await call(srv, ActionType.NAVIGATE, url=site.url("/list"))
        obs = await observe(srv)
        disp = srv._dispatcher
        real = disp.dispatch

        async def healed(action, params):
            res = await real(action, params)
            if action is ActionType.CLICK:
                res.healed = True
            return res

        monkeypatch.setattr(disp, "dispatch", healed)
        r = await call(srv, ActionType.CLICK, element_id=eid(obs, ROWS[0][1]), epoch=obs.snapshot_epoch)
        assert r.success and r.healed and "recipe_saved" not in r.data
        assert (await srv.call_server_tool(RECIPE_TOOL, {"op": "list"}))["data"]["recipes"] == []


@pytest.mark.asyncio
async def test_server_approval_replay_breaks_segment(site, monkeypatch):
    async with BrowserMCPServer(headless=True) as srv:
        await call(srv, ActionType.NAVIGATE, url=site.url("/list"))
        obs = await observe(srv)
        real = srv._check_hitl

        async def approved(action, params, approval_id=None):
            blocked = await real(action, params, approval_id=approval_id)
            if blocked is None and action is ActionType.CLICK:
                srv._used_approval = "ap_test"
            return blocked

        monkeypatch.setattr(srv, "_check_hitl", approved)
        r = await call(srv, ActionType.CLICK, element_id=eid(obs, ROWS[0][1]), epoch=obs.snapshot_epoch)
        assert r.success and "recipe_saved" not in r.data
        assert (await srv.call_server_tool(RECIPE_TOOL, {"op": "list"}))["data"]["recipes"] == []


@pytest.mark.asyncio
async def test_server_no_recipes_saves_nothing_and_creates_no_file(site, _isolated_profile_root):
    async with BrowserMCPServer(headless=True, profile="auto5", recipes=False) as srv:
        results = await search_flow(srv, site, "노트북")
        assert all("recipe_saved" not in r.data for r in results)
    assert not _file("auto5").exists()


@pytest.mark.asyncio
async def test_server_one_step_segment_not_saved(site):
    async with BrowserMCPServer(headless=True) as srv:
        await call(srv, ActionType.NAVIGATE, url=site.url("/list"))
        await call(srv, ActionType.NAVIGATE, url=site.url("/list?new=1"))
        assert (await srv.call_server_tool(RECIPE_TOOL, {"op": "list"}))["data"]["recipes"] == []


def test_instructions_and_tool_description_say_autosave():
    from interface.mcp_server import RECIPE_INSTRUCTIONS, SERVER_TOOLS
    from recipes.service import HOW

    assert "자동 저장" in RECIPE_INSTRUCTIONS and "run" in RECIPE_INSTRUCTIONS
    assert "save" in RECIPE_INSTRUCTIONS
    assert "자동" in SERVER_TOOLS[RECIPE_TOOL]["description"]
    assert "run" in HOW


def test_cached_payload_is_byte_identical_to_full_dump(tmp_path):
    """자동 저장 비용 절감(레시피별 직렬화 캐시)이 파일 내용을 바꾸지 않는다 — 제자리 변경(note_run·touch)
    뒤에도 전체 json.dumps 와 바이트까지 같다."""
    path = tmp_path / "r.json"
    s = st.RecipeStore(path)

    def full() -> bytes:
        index: Dict[str, Dict[str, List[str]]] = {}
        for rid, rec in s._recipes.items():
            index.setdefault(rec["origin"], {}).setdefault(rec["index_pat"], []).append(rid)
        return json.dumps({"version": st.VERSION, "index": index, "recipes": s._recipes},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    a = s.save_auto(st.compile_auto(_flow(ordinal=0)))
    b = s.save_auto(st.compile_auto(_flow(ordinal=1)))
    assert path.read_bytes() == full()
    s.note_run(a["id"], ok=True, ui_moves={(2, "s" * 12): [5, 6]})
    s.touch(b["id"])
    s.flush()
    assert path.read_bytes() == full()
    for _ in range(3):
        s.note_run(b["id"], ok=False)  # 비활성 → 즉시 쓰기
    assert s.get(b["id"])["stats"]["disabled"] is True
    assert path.read_bytes() == full()
    s.delete(a["id"])
    assert path.read_bytes() == full()
    assert st.RecipeStore(path).list()[0]["id"] == b["id"]


def test_deferred_autosave_is_written_on_flush_and_close(tmp_path):
    path = tmp_path / "r.json"
    s = st.RecipeStore(path)
    out = s.save_auto(st.compile_auto(_flow()), write=False)
    assert out["persisted"] is False and not path.exists() and s.dirty
    s.close()
    assert out["id"] in json.loads(path.read_text("utf-8"))["recipes"]


def test_prefix_of_existing_recipe_waits_instead_of_churning(monkeypatch):
    """반복 흐름의 앞부분(기존 레시피의 앞 단계와 같은 구조)은 바로 새 레시피로 만들지 않는다 — 상한(200)에서
    임시 짧은 판이 다른 레시피를 LRU 로 밀어내지 않게. 구간이 거기서 끝나면 그때 저장한다."""
    monkeypatch.setattr(st, "MAX_RECIPES", 2)
    svc = _svc()
    _feed(svc, _flow("노트북"))
    other = [_nav("http://mock.test/list?new=1"),
             _entry("click", {}, desc=_desc("Beta", ordinal=1), url="http://mock.test/list?new=1")]
    _feed(svc, other)
    ids = {r["id"] for r in svc.store.list()}
    assert len(ids) == 2
    datas = _feed(svc, _flow("이어폰"))
    assert {r["id"] for r in svc.store.list()} == ids  # 밀려난 레시피 없음, 새 레시피 없음
    assert datas[3]["recipe_saved"]["id"] in ids  # 기존 레시피에 합쳐진 단계에서 한 번 알림
    assert all("recipe_saved" not in d for d in datas[:3])
    # 앞부분만 하고 끝난 흐름은 구간이 끝날 때 저장(2개 상한이라 가장 오래 안 쓴 것이 밀림 — 정상 LRU)
    _feed(svc, _flow("마우스")[:3])
    svc.reset("failed")
    assert sorted(len(r["steps"]) for r in svc.store.list()) == [3, 4]
