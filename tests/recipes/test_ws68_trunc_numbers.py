"""WS-68 (#140-1): 잘린 결과 안내의 숫자가 실제와 맞는다.

* hint 는 설정 상한(--max-result-chars)과 실제로 자른 실효 예산(창 보고 등을 뺀 값)을 둘 다 적는다 — 같으면 하나.
* returned_chars 는 최종 봉투(창 보고까지 붙인 뒤) 길이와 같다(자기 길이를 담는 값 — 고정점).
* 문서(도구 설명·CLI 도움말)는 '설정 상한'으로 말하고, hint 의 실효 값이 그보다 작을 수 있음을 적는다.
"""

from __future__ import annotations

import re
from types import SimpleNamespace as NS
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from contracts import ActionResult, ActionType
from interface.server_envelope import cap_result_size, envelope_json

from test_ws61_r1 import URL, _route_server
from test_ws61_r2 import LAST_WINDOW, LIMIT, WINDOW_INFO

_BUDGET = re.compile(r"설정 (\d+)자 중 창 보고 등을 뺀 (\d+)자")


def _obs(n: int = 400) -> Dict[str, Any]:
    return {"title": "목록", "url": URL, "snapshot_epoch": 1, "token_count": 1000,
            "elements": [{"element_id": f"@e{i}", "role": "link", "name": "정상 기사 " + "가" * 64,
                          "bbox": {"x": 0, "y": i * 20, "width": 100, "height": 20}, "interactable": True,
                          "score": 1.0} for i in range(n)],
            "axtree_summary": "\n".join(f"[@e{i}] link 정상 기사" for i in range(n))}


async def _route(window: bool, action: ActionType = ActionType.OBSERVE_PAGE, **data: Any):
    from interface.mcp_server import tool_name

    s, _page, res = _route_server()
    s.max_result_chars = LIMIT
    if window:
        s._window = NS(info=lambda: dict(WINDOW_INFO))
        s._window_unreported = True
        s._recovered_notice = False
        s._last_window_result = dict(LAST_WINDOW)
        s._on_demand_enter = AsyncMock(return_value=None)
    s._note_tabs = lambda: None
    s._call_tool = AsyncMock(return_value=res(action, **data))
    out = await s._route_tool_call(tool_name(action), {})
    return s, out


def _notice_chars(out: ActionResult) -> int:
    """최종 봉투에서 창 보고(data.window)가 차지한 글자 수."""
    bare = out.model_copy(deep=True)
    bare.data.pop("window", None)
    return len(envelope_json(out)) - len(envelope_json(bare))


@pytest.mark.asyncio
async def test_window_report_hint_states_both_numbers_and_returned_chars_is_final():
    _s, out = await _route(True, observation=_obs())
    t = out.data["truncated"]
    assert "window" in out.data
    final = len(envelope_json(out))
    assert final <= LIMIT
    assert t["returned_chars"] == final  # 부착 전 값이 아니라 최종 봉투
    m = _BUDGET.search(t["hint"])
    assert m, t["hint"]
    assert int(m.group(1)) == LIMIT
    assert int(m.group(2)) == LIMIT - _notice_chars(out) < LIMIT


@pytest.mark.asyncio
async def test_no_window_report_hint_has_single_limit_and_returned_chars_is_final():
    _s, out = await _route(False, observation=_obs())
    t = out.data["truncated"]
    assert "window" not in out.data
    assert t["returned_chars"] == len(envelope_json(out)) <= LIMIT
    assert f"{LIMIT}자 상한" in t["hint"] and "설정" not in t["hint"]


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [False, True])
async def test_extract_rows_path(window):
    items = [{"text": "행 " + "나" * 90} for _ in range(400)]
    _s, out = await _route(window, ActionType.EXTRACT, items=items)
    t = out.data["truncated"]
    assert 0 < t["returned_items"] < 400
    assert t["returned_chars"] == len(envelope_json(out)) <= LIMIT
    assert bool(_BUDGET.search(t["hint"])) is window


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [False, True])
async def test_extract_single_text_path(window):
    _s, out = await _route(window, ActionType.EXTRACT, items=[{"text": "다" * 50_000}])
    t = out.data["truncated"]
    assert t.get("item_text_truncated") is True
    assert t["returned_chars"] == len(envelope_json(out)) <= LIMIT
    m = _BUDGET.search(t["hint"])
    assert bool(m) is window
    if m:
        assert int(m.group(2)) == LIMIT - _notice_chars(out)


def _big_extract(n: int) -> ActionResult:
    return ActionResult(success=True, action=ActionType.EXTRACT, current_url=URL, snapshot_epoch=1, tab_id="tab-1",
                        retry_safe=True, data={"items": [{"text": "라" * 50} for _ in range(n)]})


def test_cap_result_size_limit_kwarg_and_digit_boundary():
    """설정 상한 자리 표시(자릿수가 더 많을 수 있음)로 재도 최종 ≤ 실효 예산 — 9,990 예산 / 10,000 설정."""
    r = _big_extract(400)
    cap_result_size(r, 9_990, limit=10_000)
    t = r.data["truncated"]
    assert len(envelope_json(r)) <= 9_990 and t["returned_chars"] == len(envelope_json(r))
    assert "설정 10000자 중 창 보고 등을 뺀 9990자" in t["hint"]
    same = _big_extract(400)
    cap_result_size(same, 9_990)  # limit 생략 = 기존 바이트
    assert "설정" not in same.data["truncated"]["hint"] and "9990자 상한" in same.data["truncated"]["hint"]


def test_docs_speak_of_configured_limit():
    from interface.server_schema import _DESCRIPTIONS

    desc = _DESCRIPTIONS[ActionType.OBSERVE_PAGE]
    # 도구 설명은 고정 숫자를 말하지 않는다(실효 값은 응답마다 hint) — 토큰 예산(전체 2500) 안에서.
    assert "설정 상한" in desc and "data.truncated.hint" in desc and "2만" not in desc
    import inspect

    from interface import cli

    src = inspect.getsource(cli)
    assert "기본 20000" in src and "창 보고" in src and "실효 예산" in src
