"""WS-73 browser_batch 입력 검증·노출 — 브라우저 없이(검증은 시작 전, 아무것도 실행하지 않음)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from interface import server_batch as sb

CLICK = {"action": "click", "target": {"role": "button", "name": "다음"}}


async def _rejected(args):
    from interface.mcp_server import BrowserMCPServer

    s = BrowserMCPServer.__new__(BrowserMCPServer)
    calls = []

    async def no_start():
        calls.append("start")

    async def no_tool(*a, **k):
        calls.append("call_tool")

    s.start = no_start
    s.call_tool = no_tool
    out = await BrowserMCPServer._batch_tool(s, args)
    assert calls == [], calls
    return out


@pytest.mark.parametrize("key", sorted(sb.FORBIDDEN_ARG_KEYS))
async def test_forbidden_arg_keys_reject_whole_batch(key):
    step = {"action": "click", "target": {"role": "button", "name": "다음"}, "args": {key: "@e1" if "id" in key else 1}}
    out = await _rejected({"steps": [CLICK, step]})
    assert out["success"] is False and key in out["error_message"]


@pytest.mark.parametrize("args", [
    {"steps": []},
    {"steps": [CLICK] * (sb.MAX_STEPS + 1)},
    {"steps": [CLICK], "stop_on": []},  # 멈춤 끄기 인자 없음
    {"steps": [{"action": "navigate", "args": {"url": "http://127.0.0.1/"}}]},
    {"steps": [{"action": "extract", "args": {"selector": "p"}}]},
    {"steps": [{"action": "wait_for", "args": {"condition": "network_idle"}}]},
    {"steps": [{"action": "click"}]},  # target 필수
    {"steps": [{"action": "press_key", "args": {"key": "Enter"}}]},  # press_key 도 target 필수
    {"steps": [{"action": "click", "target": {"role": "button"}}]},
    {"steps": [{"action": "click", "target": {"role": "button", "name": "다음", "css": "#x"}}]},
    {"steps": [{"action": "click", "target": {"role": "button", "name": "다음", "group": ""}}]},
    {"steps": [{"action": "type_text", "target": {"role": "textbox", "name": "q"}}]},  # text 없음(계약 검증)
    {"steps": [{"action": "type_text", "target": {"role": "textbox", "name": "q"}, "args": {"text": "a", "bogus": 1}}]},
    {"steps": [CLICK], "observe": {"tab_id": "t1"}},
    {"steps": [CLICK], "observe": {"prune_top_n": "많이"}},
    {"steps": [dict(CLICK, extra=1)]},
])
async def test_invalid_input_rejected_before_anything_runs(args):
    out = await _rejected(args)
    assert out["success"] is False and out["error_code"] == "E_FEATURE_NOT_IMPLEMENTED"


def test_scroll_target_optional_and_valid_inputs_pass():
    steps, obs = sb.validate_batch({"steps": [
        {"action": "scroll", "args": {"direction": "down"}},
        {"action": "scroll", "target": {"role": "list", "name": "결과"}, "args": {"direction": "up", "distance": 200}},
        {"action": "type_text", "target": {"role": "textbox", "name": "q"}, "args": {"text": "부산"}},
        {"action": "select_option", "target": {"role": "combobox", "name": "좌석"}, "args": {"value": "Y"}},
        {"action": "check_box", "target": {"role": "checkbox", "name": "동의"}, "args": {"checked": True}},
        {"action": "hover", "target": {"role": "link", "name": "메뉴"}},
        {"action": "press_key", "target": {"role": "textbox", "name": "q"}, "args": {"key": "Enter"}},
    ], "observe": {"prune_top_n": 50}})
    assert len(steps) == 7 and obs == {"prune_top_n": 50}


def test_norm_is_exact_after_nfkc_and_spaces():
    assert sb.norm(" ２０２６년\n10월 ") == "2026년 10월"
    assert sb.norm("항공권 검색") != sb.norm("항공권")


def test_tool_listed_regardless_of_recipes_and_instructions():
    from interface.mcp_server import _server_instructions, build_server_tools

    for recipes in (True, False):
        names = [t["name"] for t in build_server_tools(recipes)]
        assert sb.BATCH_TOOL in names
        assert sb.BATCH_TOOL in _server_instructions(recipes)
    spec = next(t for t in build_server_tools(False) if t["name"] == sb.BATCH_TOOL)
    assert "\n" not in spec["description"]
    assert "stop_on" not in json.dumps(spec)


def test_batch_tool_token_cost_small():
    import tiktoken

    from interface.mcp_server import build_server_tools

    enc = tiktoken.get_encoding("cl100k_base")
    spec = next(t for t in build_server_tools(False) if t["name"] == sb.BATCH_TOOL)
    cost = len(enc.encode(json.dumps(spec, ensure_ascii=False)))
    assert cost <= 260, cost


def test_server_tools_branch_before_approval_wait_fallback():
    """분기 위치: _server_tool_payload 에서 approval_wait 폴백(맨 끝)보다 앞, 레시피 분기와 같은 직렬화 모양."""
    import inspect

    from interface import server_tools

    src = inspect.getsource(server_tools.ToolsMixin._server_tool_payload)
    assert src.index('f"{TOOL_PREFIX}batch"') < src.index("_approval_wait(")
    block = src[src.index('f"{TOOL_PREFIX}batch"'):src.index("YOUTUBE_TOOL")]
    assert "_dispatch_serial()" in block and "_batch_tool(args)" in block


def test_docs_mention_batch():
    root = Path(__file__).resolve().parents[2]
    assert "browser_batch" in (root / "docs" / "features.md").read_text(encoding="utf-8")
    assert "browser_batch" in (root / "README.md").read_text(encoding="utf-8")


def test_reasons_are_control_vocabulary():
    """data.batch.reason 은 public_url 제어 어휘 안(가림 대상 아님) — 어휘 표와 REASONS 가 어긋나면 실패."""
    from security.public_url import CONTROL_VOCABULARY, control_value

    assert set(sb.REASONS) <= CONTROL_VOCABULARY["reason"]
    assert all(control_value("reason", r) for r in sb.REASONS)
