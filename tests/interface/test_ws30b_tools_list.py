"""WS-30b 항목 2: tools/list 다이어트.

* CHALLENGE_NOTE(차단·캡차 안내 전문)는 **한 곳**(browser_navigate 설명)에만 싣고, 나머지 대상
  10개 툴은 짧은 참조로 — 의도(에이전트가 data.challenge 를 보게 함)는 11개 모두 유지.
* 입력 스키마는 계약 모델의 `model_json_schema()` 를 서버 경계에서 정리한다(계약 무수정):
  title 제거, `anyOf [T, null]` → `type: [T, "null"]`, `default: null` 제거.
  정리 전후 스키마가 **같은 입력을 받아들이고 같은 입력을 거부**하는지 JSON Schema 검증기
  (Draft 2020-12)로 대량의 입력(유효 예시 + 필드별 잘못된 값)에 대해 비교한다.
"""

from __future__ import annotations

import itertools
import json
from typing import Any, Dict, List

import jsonschema
import pytest

from contracts import ACTION_INPUT_MAP, ActionType
from interface import mcp_server
from interface.mcp_server import (
    CHALLENGE_CHECK_ACTIONS,
    CHALLENGE_NOTE,
    CHALLENGE_NOTE_HOME,
    build_all_tools,
    build_tool_schema,
    compact_schema,
    tool_name,
)

#: 필드마다 넣어 볼 값(타입·경계·null·빈 값). 조합으로 유효/무효 입력을 모두 만든다.
PROBE_VALUES: List[Any] = [
    None, "", "a", "@e1", "Enter", "down", "left", "selector", "create", "load",
    0, 1, -1, 3.5, True, False, [], ["a"], [1], {}, {"k": 1},
]

#: 기존 입력 예시(레시피·스모크·README·테스트에서 실제로 보내는 인자).
EXISTING_EXAMPLES: Dict[ActionType, List[Dict[str, Any]]] = {
    ActionType.OBSERVE_PAGE: [{}, {"prune_top_n": 50}, {"force_full_tree": True}, {"tab_id": None}],
    ActionType.TAKE_SCREENSHOT: [{}, {"annotate_som": True}, {"full_page": True}],
    ActionType.NAVIGATE: [{"url": "http://x/"}, {"url": "http://x/", "wait_until": "load"},
                          {"url": "http://x/", "timeout_ms": 30000}],
    ActionType.GO_BACK: [{}, {"timeout_ms": 5000}],
    ActionType.RELOAD: [{}, {"ignore_cache": True}],
    ActionType.CLICK: [{"element_id": "@e1", "epoch": 1}, {"selector": "#t"},
                       {"x": 10, "y": 20, "epoch": 1}, {"element_id": "@e1", "epoch": 1, "button": "right"},
                       {"element_id": "@e1", "epoch": None}, {"x": None}],
    ActionType.TYPE_TEXT: [{"element_id": "@e2", "epoch": 1, "text": "tester"},
                           {"element_id": "@e2", "epoch": 1, "text": "q", "press_enter": True}],
    ActionType.SELECT_OPTION: [{"element_id": "@e1", "epoch": 1, "value": "express"},
                               {"element_id": "@e1", "epoch": 1, "index": 2}],
    ActionType.CHECK_BOX: [{"element_id": "@e1", "epoch": 1, "checked": True}],
    ActionType.SCROLL: [{"direction": "down"}, {"direction": "up", "distance": 3000}],
    ActionType.HOVER: [{"element_id": "@e1", "epoch": 1}],
    ActionType.PRESS_KEY: [{"key": "Enter"}, {"key": "Tab"}],
    ActionType.WAIT_FOR: [{"condition": "selector", "selector": "#late", "timeout_ms": 3000},
                          {"condition": "stabilize"}, {"condition": "network_idle"}],
    ActionType.EXTRACT: [{"selector": "h1"}, {"selector": "a", "attributes": ["href"], "extract_all": True}],
    ActionType.SWITCH_FRAME: [{"frame_selector": "iframe"}, {"shadow_root_selector": "#host"}],
    ActionType.HANDLE_DIALOG: [{}, {"accept": False}, {"accept": True, "prompt_text": "x"}],
    ActionType.UPLOAD_FILE: [{"element_id": "@e1", "epoch": 1, "file_paths": ["/tmp/a.txt"]}],
    ActionType.DOWNLOAD_FILE: [{"trigger_element_id": "@e1", "epoch": 1, "save_dir": "/tmp"}],
    ActionType.TAB_CONTROL: [{"command": "list"}, {"command": "switch", "tab_id": "tab-2"},
                             {"command": "create", "url": "http://x/"}],
}


def _instances(schema: Dict[str, Any], examples: List[Dict[str, Any]]) -> List[Any]:
    """예시 + 예시의 각 필드를 PROBE_VALUES 로 바꾼 변형 + 필수 누락 + 모르는 키 + 비객체."""
    props = list((schema.get("properties") or {}).keys())
    out: List[Any] = [None, [], "x", 1]
    base_list = examples or [{}]
    for base in base_list:
        out.append(dict(base))
        for key in props:
            for v in PROBE_VALUES:
                out.append(dict(base, **{key: v}))
            if key in base:
                out.append({k: v for k, v in base.items() if k != key})
        out.append(dict(base, unknown_key=1))
    # 필드 둘씩 동시에 바꾼 조합(작은 값 집합)
    small = [None, "a", 1, -1, True]
    for a, b in itertools.combinations(props, 2):
        for va, vb in itertools.product(small, small):
            out.append(dict(base_list[0], **{a: va, b: vb}))
    return out


def _verdicts(schema: Dict[str, Any], instances: List[Any]) -> List[bool]:
    v = jsonschema.Draft202012Validator(schema)
    return [v.is_valid(i) for i in instances]


@pytest.mark.parametrize("action", list(ACTION_INPUT_MAP), ids=lambda a: a.value)
def test_compacted_schema_accepts_and_rejects_the_same_inputs(action):
    original = ACTION_INPUT_MAP[action].model_json_schema()
    compact = build_tool_schema(action)["inputSchema"]
    jsonschema.Draft202012Validator.check_schema(compact)
    instances = _instances(original, EXISTING_EXAMPLES.get(action, []))
    before, after = _verdicts(original, instances), _verdicts(compact, instances)
    diffs = [json.dumps(i, ensure_ascii=False) for i, a, b in zip(instances, before, after) if a != b]
    assert not diffs, f"{action.value}: 판정이 달라진 입력 {len(diffs)}개 예: {diffs[:3]}"
    assert any(before) and not all(before), "유효·무효 입력이 모두 섞여 있어야 비교가 의미 있다"


@pytest.mark.parametrize("action", list(ACTION_INPUT_MAP), ids=lambda a: a.value)
def test_existing_examples_still_validate(action):
    compact = build_tool_schema(action)["inputSchema"]
    v = jsonschema.Draft202012Validator(compact)
    for ex in EXISTING_EXAMPLES.get(action, []):
        assert v.is_valid(ex), (action.value, ex, list(v.iter_errors(ex)))


def test_invalid_inputs_still_rejected():
    cases = [
        (ActionType.NAVIGATE, {}),                                   # 필수 url 누락
        (ActionType.NAVIGATE, {"url": "x", "wait_until": "idle"}),  # enum 밖
        (ActionType.CLICK, {"x": -1}),                               # minimum 0
        (ActionType.CLICK, {"epoch": "1"}),                          # 타입
        (ActionType.CLICK, {"button": None}),                        # null 불가(기본값 있는 비nullable)
        (ActionType.SCROLL, {"direction": "left"}),
        (ActionType.UPLOAD_FILE, {"element_id": "@e1", "epoch": 1, "file_paths": []}),  # minItems
        (ActionType.EXTRACT, {"selector": "a", "attributes": [1]}),  # items 타입
        (ActionType.TYPE_TEXT, {"element_id": "@e1", "epoch": 1}),   # text 누락
    ]
    for action, inst in cases:
        v = jsonschema.Draft202012Validator(build_tool_schema(action)["inputSchema"])
        assert not v.is_valid(inst), (action.value, inst)


def test_compact_schema_drops_pydantic_boilerplate():
    for spec in build_all_tools():
        text = json.dumps(spec["inputSchema"])
        assert '"title"' not in text, spec["name"]
        assert '"anyOf"' not in text, spec["name"]
        assert '"default": null' not in text, spec["name"]
    # 의미 있는 기본값·제약은 남는다.
    click = build_tool_schema(ActionType.CLICK)["inputSchema"]["properties"]
    assert click["button"]["default"] == "left"
    assert click["x"] == {"type": ["integer", "null"], "minimum": 0}
    assert "url" in build_tool_schema(ActionType.NAVIGATE)["inputSchema"]["required"]


def test_property_named_title_or_default_is_kept():
    """속성 이름이 title·default 인 입력 필드는 지우지 않는다(주석 키워드와 속성 이름을 구분)."""
    from pydantic import BaseModel
    from typing import Optional

    class M(BaseModel):
        title: str
        default: Optional[int] = None

    src = M.model_json_schema()
    out = compact_schema(src)
    assert set(out["properties"]) == {"title", "default"}
    assert out["required"] == ["title"]
    v_src, v_out = jsonschema.Draft202012Validator(src), jsonschema.Draft202012Validator(out)
    for inst in ({}, {"title": "a"}, {"title": 1}, {"title": "a", "default": None},
                 {"title": "a", "default": "x"}):
        assert v_src.is_valid(inst) == v_out.is_valid(inst), inst


def test_compact_schema_leaves_unknown_anyof_untouched():
    """두 갈래가 (T, null) 모양이 아니면 그대로 둔다 — 의미를 추측하지 않는다."""
    src = {"type": "object", "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                                            "b": {"anyOf": [{"$ref": "#/$defs/X"}, {"type": "null"}]}}}
    out = compact_schema(src)
    assert out["properties"]["a"] == src["properties"]["a"]
    assert out["properties"]["b"] == src["properties"]["b"]


# ------------------------------------------------------------------ CHALLENGE_NOTE 한 곳


def test_challenge_note_full_text_once():
    specs = build_all_tools()
    full = [s["name"] for s in specs if CHALLENGE_NOTE.strip() in s["description"]]
    assert full == [tool_name(CHALLENGE_NOTE_HOME)]
    assert CHALLENGE_NOTE_HOME in CHALLENGE_CHECK_ACTIONS


def test_challenge_reference_on_every_target_tool_points_home():
    home = tool_name(CHALLENGE_NOTE_HOME)
    for spec in build_all_tools():
        action = mcp_server.action_from_tool(spec["name"])
        if action in CHALLENGE_CHECK_ACTIONS and action is not CHALLENGE_NOTE_HOME:
            assert "data.challenge" in spec["description"], spec["name"]
            assert home in spec["description"], spec["name"]
            assert "사람" in spec["description"], "참조만으로도 '풀지 말고 사람에게' 의도가 보여야 한다"


def test_tools_list_token_budget():
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    text = json.dumps(build_all_tools(), ensure_ascii=False)
    assert len(enc.encode(text)) <= 2500, len(enc.encode(text))
