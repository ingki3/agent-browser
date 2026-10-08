"""WS-38 단계 2: 레시피 저장소(store.py) — 형식·params 치환·권한·원자적 쓰기·손상 파일.

설계 §1·§2·§4-1·§5. 브라우저 없이 순수 파이썬으로 검사한다(궤적 항목은 기록기가 만드는 모양).
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from recipes import store as st


def _desc(slot=True, *, secret=False, identity=True, name="Alpha"):
    return {
        "role": "link", "name": name, "tag": "a", "sig": "a.title", "href_pat": "/item?id",
        "secret": secret,
        "ui": {"group": "link", "name": name.lower(), "landmark": "main", "pos": [100, 200],
               "href_pat": "/item?id"},
        "slot": ({"chain": "c" * 12, "csel": "ul.items", "itpl": "i" * 12, "ordinal": 0,
                  "rel": "a:nth-of-type(1)", "tsig": "a.title", "role": "link",
                  "href_pat": "/item?id"} if slot else None),
        "identity": ({"by": "query", "path": "/item", "key": "id", "value": "101", "group": "link",
                      "tag": "a", "name": name.lower()} if identity else None),
        "identity_refused": None if identity else "가격 대상",
    }


def _entry(action, args=None, *, desc=None, origin="http://mock.test", url="http://mock.test/list",
           skel="s" * 12, ready=20, expect=None, secret=False):
    return {
        "action": action, "args": dict(args or {}), "origin": origin, "url": url,
        "url_pat": st.keys.url_pattern(url), "skel": skel, "ready": ready, "desc": desc,
        "secret": secret,
        "expect": expect or {"nav": "none", "url_pat": None, "signals": ["element_value_changed"]},
    }


def _search_flow(query="노트북"):
    return [
        _entry("type_text", {"text": query}, desc=_desc(slot=False)),
        _entry("press_key", {"key": "Enter"},
               expect={"nav": "same", "url_pat": "mock.test/search?q", "signals": ["navigated"]}),
        _entry("click", {}, desc=_desc(), url="http://mock.test/search?q=x",
               expect={"nav": "same", "url_pat": "mock.test/item?id", "signals": ["navigated"]}),
    ]


# ---------------------------------------------------------------------------
# compile: params 치환·거부
# ---------------------------------------------------------------------------


def test_compile_substitutes_params_and_defaults_targets():
    rec = st.compile_recipe("노트북 검색 후 첫 결과", _search_flow(), params={"query": "노트북"})
    assert rec["params"] == ["query"]
    assert rec["origin"] == "http://mock.test"
    steps = rec["steps"]
    assert steps[0]["args"]["text"] == "{query}"
    assert steps[0]["variants"][0]["target"]["kind"] == "ui"  # 목록 밖
    assert steps[2]["variants"][0]["target"]["kind"] == "slot"  # 목록 안 → 자동 slot
    assert steps[1]["variants"][0]["target"] is None  # press_key: 대상 없음
    assert rec["index_pat"] == "mock.test/list"
    # 입력 원문이 어디에도 남지 않는다
    assert "노트북" not in json.dumps(rec, ensure_ascii=False).replace("노트북 검색", "")


def test_compile_refuses_leftover_typed_text():
    with pytest.raises(st.RecipeError, match="치환되지 않은"):
        st.compile_recipe("x", _search_flow("노트북 15인치"), params={"query": "노트북"})
    with pytest.raises(st.RecipeError, match="치환되지 않은"):
        st.compile_recipe("x", _search_flow("노트북"), params={})


def test_compile_refuses_secret_field_even_with_params():
    flow = [_entry("type_text", {"text": "hunter2"}, desc=_desc(slot=False, secret=True))]
    with pytest.raises(st.RecipeError, match="비밀번호"):
        st.compile_recipe("login", flow, params={"pw": "hunter2"})
    flow = [_entry("type_text", {"text": "{{SECRET}}"}, desc=_desc(slot=False), secret=True)]
    with pytest.raises(st.RecipeError, match="비밀번호"):
        st.compile_recipe("login", flow, params={"pw": "{{SECRET}}"})


def test_compile_refuses_unused_param_and_bad_name():
    with pytest.raises(st.RecipeError, match="어느 입력에도"):
        st.compile_recipe("x", _search_flow(), params={"query": "노트북", "other": "zz"})
    with pytest.raises(st.RecipeError, match="이름"):
        st.compile_recipe("x", _search_flow(), params={"bad name": "노트북"})


def test_compile_pins_identity_and_refuses_bad_identity():
    rec = st.compile_recipe("x", _search_flow(), params={"query": "노트북"}, pins={"2": "identity"})
    assert rec["steps"][2]["variants"][0]["target"]["kind"] == "identity"
    flow = _search_flow()
    flow[2]["desc"] = _desc(identity=False)
    with pytest.raises(st.RecipeError, match="identity"):
        st.compile_recipe("x", flow, params={"query": "노트북"}, pins={2: "identity"})
    with pytest.raises(st.RecipeError, match="대상이 없는"):
        st.compile_recipe("x", flow, params={"query": "노트북"}, pins={1: "ui"})


def test_compile_step_limit_and_empty():
    flow = [_entry("click", {}, desc=_desc()) for _ in range(st.MAX_STEPS + 1)]
    with pytest.raises(st.RecipeError, match="20"):
        st.compile_recipe("x", flow, params={})
    with pytest.raises(st.RecipeError):
        st.compile_recipe("x", [], params={})


def test_navigate_url_params_and_index_pattern():
    flow = [
        _entry("navigate", {"url": "http://mock.test/search?q=%EB%85%B8%ED%8A%B8%EB%B6%81"},
               desc=None, url="about:blank", origin="",
               expect={"nav": "cross", "url_pat": "mock.test/search?q", "signals": ["navigated"]}),
        _entry("click", {}, desc=_desc(), url="http://mock.test/search?q=x"),
    ]
    rec = st.compile_recipe("검색 결과 첫 항목", flow, params={"query": "노트북"})
    assert rec["steps"][0]["args"]["url"] == "http://mock.test/search?q={query:url}"
    assert rec["origin"] == "http://mock.test"
    assert rec["index_pat"] == "mock.test/search?q"
    args = st.render_args(rec["steps"][0], {"query": "무선 이어폰"})
    assert args["url"] == "http://mock.test/search?q=%EB%AC%B4%EC%84%A0%20%EC%9D%B4%EC%96%B4%ED%8F%B0"


def test_render_args_requires_params():
    rec = st.compile_recipe("x", _search_flow(), params={"query": "노트북"})
    assert st.render_args(rec["steps"][0], {"query": "이어폰"})["text"] == "이어폰"
    with pytest.raises(st.RecipeError, match="query"):
        st.render_args(rec["steps"][0], {})


def test_name_is_sanitized():
    rec = st.compile_recipe("a\x1b[31m\u202e" + "가" * 200, _search_flow(), params={"query": "노트북"})
    assert "\x1b" not in rec["name"] and "\u202e" not in rec["name"]
    assert len(rec["name"]) <= 80
    with pytest.raises(st.RecipeError):
        st.compile_recipe("\x00\x01  ", _search_flow(), params={"query": "노트북"})


# ---------------------------------------------------------------------------
# 저장소: 형식·권한·원자적 쓰기·write-through
# ---------------------------------------------------------------------------


def _rec(name="r", query="노트북"):
    return st.compile_recipe(name, _search_flow(query), params={"query": query})


def test_store_write_through_format_and_perms(tmp_path):
    path = tmp_path / "recipes.json"
    s = st.RecipeStore(path)
    saved = s.save(_rec("첫 결과"))
    assert saved["id"].startswith("r_")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["index"] == {"http://mock.test": {"mock.test/list": [saved["id"]]}}
    assert data["recipes"][saved["id"]]["name"] == "첫 결과"
    # 다시 읽으면 같다 + 색인 조회
    s2 = st.RecipeStore(path)
    assert s2.get(saved["id"])["name"] == "첫 결과"
    assert [r["id"] for r in s2.candidates("http://mock.test", "mock.test/list")] == [saved["id"]]
    assert s2.candidates("http://other.test", "mock.test/list") == []
    assert s2.has_origin("http://mock.test") and not s2.has_origin("http://other.test")
    # delete 도 즉시 파일에
    assert s2.delete(saved["id"]) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["recipes"] == {} and data["index"] == {}
    assert not [p for p in tmp_path.iterdir() if p.name != "recipes.json"]  # 임시 파일 안 남음


def test_store_memory_only_without_path(tmp_path):
    s = st.RecipeStore(None)
    saved = s.save(_rec())
    assert s.get(saved["id"]) is not None
    assert s.persistent is False


def test_atomic_write_keeps_old_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "recipes.json"
    s = st.RecipeStore(path)
    first = s.save(_rec("one"))
    before = path.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(st.os, "replace", boom)
    out = s.save(_rec("two"))
    assert out["persisted"] is False and s.warnings
    assert path.read_text(encoding="utf-8") == before
    assert not [p for p in tmp_path.iterdir() if p.name != "recipes.json"]
    assert first["id"] in json.loads(before)["recipes"]


def test_corrupt_file_starts_empty_and_preserves_original(tmp_path):
    path = tmp_path / "recipes.json"
    path.write_text("{not json", encoding="utf-8")
    os.chmod(path, 0o600)
    s = st.RecipeStore(path)
    assert s.list() == []
    assert any("손상" in w for w in s.warnings)
    kept = [p for p in tmp_path.iterdir() if p.name.startswith("recipes.json.corrupt-")]
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{not json"
    s.save(_rec())
    assert kept[0].read_text(encoding="utf-8") == "{not json"  # 새 저장이 원본을 덮지 않음


def test_wrong_shape_counts_as_corrupt(tmp_path):
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps({"version": 99, "recipes": []}), encoding="utf-8")
    s = st.RecipeStore(path)
    assert s.list() == [] and s.warnings


def test_symlink_file_is_not_followed(tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"version": 1, "index": {}, "recipes": {}}), encoding="utf-8")
    path = tmp_path / "recipes.json"
    path.symlink_to(real)
    s = st.RecipeStore(path)
    assert s.warnings
    s.save(_rec())
    assert json.loads(real.read_text(encoding="utf-8"))["recipes"] == {}  # 링크 대상은 그대로


def test_loose_permissions_are_tightened(tmp_path):
    path = tmp_path / "recipes.json"
    st.RecipeStore(path).save(_rec())
    os.chmod(path, 0o644)
    s = st.RecipeStore(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert any("권한" in w for w in s.warnings)
    assert len(s.list()) == 1


def test_recipe_cap_evicts_least_recently_used(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "MAX_RECIPES", 3)
    clock = iter(range(100, 1000))
    s = st.RecipeStore(tmp_path / "recipes.json", clock=lambda: float(next(clock)))
    a = s.save(_rec("a"))
    b = s.save(_rec("b"))
    c = s.save(_rec("c"))
    s.touch(a["id"])  # a 를 최근에 씀
    d = s.save(_rec("d"))
    ids = {r["id"] for r in s.list()}
    assert ids == {a["id"], c["id"], d["id"]} and b["id"] not in ids


def test_file_size_cap(tmp_path, monkeypatch):
    path = tmp_path / "recipes.json"
    s = st.RecipeStore(path)
    one = s.save(_rec("a"))
    size = len(path.read_bytes())
    monkeypatch.setattr(st, "MAX_FILE_BYTES", int(size * 1.6))
    s.save(_rec("b"))  # 둘은 안 들어감 → 오래된 a 정리
    assert {r["name"] for r in s.list()} == {"b"} and one["id"] not in {r["id"] for r in s.list()}
    assert len(path.read_bytes()) <= st.MAX_FILE_BYTES
    monkeypatch.setattr(st, "MAX_FILE_BYTES", 100)
    with pytest.raises(st.RecipeError, match="1개"):
        s.save(_rec("c"))


def test_stats_are_batched_then_flushed(tmp_path):
    now = [1000.0]
    path = tmp_path / "recipes.json"
    s = st.RecipeStore(path, clock=lambda: now[0])
    rid = s.save(_rec())["id"]
    s.note_run(rid, ok=True)
    on_disk = json.loads(path.read_text(encoding="utf-8"))["recipes"][rid]["stats"]
    assert on_disk["runs"] == 0  # 아직 안 씀(모아 쓰기)
    now[0] += st.STATS_FLUSH_S
    s.maybe_flush()
    assert json.loads(path.read_text(encoding="utf-8"))["recipes"][rid]["stats"]["runs"] == 1
    s.note_run(rid, ok=False)
    s.close()  # 종료 때 마지막 쓰기
    stats = json.loads(path.read_text(encoding="utf-8"))["recipes"][rid]["stats"]
    assert stats["runs"] == 2 and stats["fail_streak"] == 1


def test_three_failures_disable_and_resave_reenables(tmp_path):
    s = st.RecipeStore(tmp_path / "recipes.json")
    rid = s.save(_rec("같은 일"))["id"]
    for _ in range(3):
        s.note_run(rid, ok=False)
    assert s.get(rid)["stats"]["disabled"] is True
    assert s.candidates("http://mock.test", "mock.test/list") == []  # disabled 는 후보 아님
    again = s.save(_rec("같은 일"))
    assert again["id"] == rid and again["merged"] is True
    assert s.get(rid)["stats"]["disabled"] is False and s.get(rid)["stats"]["fail_streak"] == 0


def test_ab_variant_merges_into_same_recipe(tmp_path):
    s = st.RecipeStore(tmp_path / "recipes.json")
    a = s.save(_rec("A/B"))
    flow = _search_flow()
    for e in flow:
        e["skel"] = "b" * 12
    b = s.save(st.compile_recipe("A/B", flow, params={"query": "노트북"}))
    assert b["id"] == a["id"]
    skels = [v["skel"] for v in s.get(a["id"])["steps"][0]["variants"]]
    assert skels == ["s" * 12, "b" * 12]


def test_ui_position_update_only(tmp_path):
    s = st.RecipeStore(tmp_path / "recipes.json")
    rid = s.save(_rec())["id"]
    s.note_run(rid, ok=True, ui_moves={(0, "s" * 12): [111, 222]})
    t = s.get(rid)["steps"][0]["variants"][0]["target"]
    assert t["kind"] == "ui" and t["pos"] == [111, 222]
