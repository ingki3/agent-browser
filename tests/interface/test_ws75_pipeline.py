"""WS-75 (#199) 응답 파이프라인 — 가린 사본(③) 위에서 크기 맞춤(④), version 변경 시 원본에서 다시(NB-h).

브라우저 없이 `_route_tool_call` 을 가짜 `_call_tool` 결과로 돈다(test_ws61_r1 `_route_server` 와 같은 방식).

* 자르기 위치 전수: 첫 extract 항목 글자 자르기가 등록 값을 걸치는 모든 위치에서 앞부분(≥ 4자) 0.
* 사본 생성 직후 새 값 등록 주입 → 그 값도 모든 자르기 위치에서 0(리뷰 r4 NB-i).
* 크기: 등록 값이 `[REDACTED]` 보다 짧아 늘어나도 봉투 ≤ max_result_chars(5k/20k/200k).
* 바이트 동일: 레지스트리가 비었을 때와 응답에 없는 값만 등록됐을 때 12 페이지 봉투가 같다.
* 성능(CI 단정): 비었을 때 가림 단계 비용 ≈ 0, 값 4개·~200KB 관찰 추가 ≤ 20ms.
"""

from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace as NS
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from contracts import ActionResult, ActionType
from interface.server_envelope import envelope_json
import security.public_url  # noqa: F401 - 모듈 적재
from security import secret_registry

public_url = sys.modules["security.public_url"]  # 패키지 속성 `security.public_url` 은 같은 이름 함수다

R = "[REDACTED]"
URL = "http://127.0.0.1:9/p"
V = "SYNTHcutSecret42"
W = "SYNTHlateValue77"


@pytest.fixture(autouse=True)
def _clean():
    secret_registry.clear()
    yield
    secret_registry.clear()


def _server(limit: int):
    from interface.mcp_server import BrowserMCPServer

    s = BrowserMCPServer(recipes=False)
    s.max_result_chars = limit
    s._started = True
    s._dispatcher = NS(ctx=NS(page=NS(url=URL), root_page=None))
    s._page = s._dispatcher.ctx.page
    s._note_tabs = lambda: None
    s._attach_observation_guidance = AsyncMock()
    s._attach_robots = AsyncMock()
    s._attach_block_hint = AsyncMock()
    return s


def _res(action: ActionType, data: Dict[str, Any]) -> ActionResult:
    return ActionResult(success=True, action=action, current_url=URL, snapshot_epoch=1, tab_id="tab-1",
                        retry_safe=True, data=data)


async def _route(limit: int, action: ActionType, data: Dict[str, Any]) -> ActionResult:
    from interface.mcp_server import tool_name

    s = _server(limit)
    s._call_tool = AsyncMock(return_value=_res(action, json.loads(json.dumps(data))))
    return await s._route_tool_call(tool_name(action), {})


def _observe_data(n: int, name, *, repeated: bool = True) -> Dict[str, Any]:
    els = [{"element_id": f"@e{i}", "role": "link", "name": name(i), "value": None,
            "bbox": {"x": 0, "y": i, "width": 10, "height": 10}, "interactable": True, "score": 1.0}
           for i in range(n)]
    data: Dict[str, Any] = {"observation": {
        "title": "t", "url": URL, "snapshot_epoch": 1, "token_count": 1, "elements": els,
        "axtree_summary": "\n".join(f'{e["element_id"]} link "{e["name"]}"' for e in els)}}
    if repeated:
        data["repeated"] = {"how": "h", "groups": [
            {"group": f"묶음 {g} {name(g)}", "group_source": "text", "x": g, "y": 0,
             "members": [{"id": f"@e{g}", "role": "link", "name": name(g)[:40]}]} for g in range(min(n, 30))]}
    return data


def _prefix_leaks(wire: str, value: str):
    return [k for k in range(4, len(value) + 1) if value[:k] in wire]


# ------------------------------------------------------------------ 자르기 위치 전수


async def _cut_position(limit: int) -> int:
    """채움 글자만 있는 본문에서 첫 항목 자르기가 남기는 글자 수(가린 결과 기준 자르기 위치)."""
    out = await _route(limit, ActionType.EXTRACT, {"items": [{"text": "w" * (limit * 2)}]})
    return len(out.data["items"][0]["text"])


@pytest.mark.parametrize("limit", [2_000, 5_000])
async def test_first_item_text_cut_never_leaves_registered_prefix(limit):
    cut = await _cut_position(limit)
    secret_registry.register_secret_values([V])
    positions = range(max(0, cut - len(R) - len(V) - 2), cut + len(V) + 2)
    for p in positions:
        text = "w" * p + V + "w" * (limit * 2 - p)
        out = await _route(limit, ActionType.EXTRACT, {"items": [{"text": text}]})
        wire = envelope_json(out)
        assert out.data["items"][0].get("text_truncated") is True
        assert _prefix_leaks(wire, V) == [], (p, wire[-120:])
        assert len(wire) <= limit


async def test_registration_after_copy_is_redone_from_original(monkeypatch):
    """사본(③)을 만든 직후 새 값이 등록되면 원본에서 다시 만든다 — 모든 자르기 위치에서 그 값의 앞부분 0."""
    limit = 2_000
    cut = await _cut_position(limit)
    original = public_url.content_view
    calls = []

    def racing_view(data):
        view = original(data)
        calls.append(1)
        if len(calls) == 1:
            secret_registry.register_secret_values([W])  # 동시 호출이 새 값을 등록(사본 생성 직후)
        return view

    monkeypatch.setattr(public_url, "content_view", racing_view)
    for p in range(max(0, cut - len(R) - len(W) - 2), cut + len(W) + 2):
        secret_registry.clear()
        calls.clear()
        text = "w" * p + W + "w" * (limit * 2 - p)
        out = await _route(limit, ActionType.EXTRACT, {"items": [{"text": text}]})
        wire = envelope_json(out)
        assert len(calls) >= 2, "등록 주입이 다시 만들기를 일으키지 않으면 이 시험은 아무것도 재지 않는다"
        assert _prefix_leaks(wire, W) == [], (p, wire[-120:])


async def test_registration_every_attempt_fails_closed(monkeypatch):
    """매 시도마다 새 값이 등록되면(재시도 상한 초과) 최종 결과를 최신 레지스트리로 다시 가린다(누출보다 잘림)."""
    original = public_url.content_view
    values = [f"SYNTHraceValue{i:02d}" for i in range(10)]
    count = []

    def always(data):
        view = original(data)
        if len(count) < len(values):
            secret_registry.register_secret_values([values[len(count)]])
        count.append(1)
        return view

    monkeypatch.setattr(public_url, "content_view", always)
    data = _observe_data(20, lambda i: f"item {i} " + " ".join(values))
    out = await _route(20_000, ActionType.OBSERVE_PAGE, data)
    wire = envelope_json(out)
    assert len(count) >= 4
    assert [v for v in values[:len(count) - 1] if v in wire] == []
    assert len(wire) <= 20_000


# ------------------------------------------------------------------ 크기


@pytest.mark.parametrize("limit", [5_000, 20_000, 200_000])
async def test_all_envelopes_within_limit_when_masks_lengthen(limit):
    secret_registry.register_secret_values(["7k3q", "SYNTHgrow12345"])  # 4자 값 → [REDACTED] 로 +6자씩 늘어난다
    obs = _observe_data(3000, lambda i: f"7k3q 7k3q 7k3q 링크 {i} SYNTHgrow12345")
    ext = {"items": [{"text": "7k3q " * 20, "href": "/a?x=7k3q"} for _ in range(3000)]}
    one = {"items": [{"text": "7k3q " * (limit // 2)}]}
    for action, data in ((ActionType.OBSERVE_PAGE, obs), (ActionType.EXTRACT, ext), (ActionType.EXTRACT, one)):
        out = await _route(limit, action, data)
        wire = envelope_json(out)
        assert len(wire) <= limit, (action, len(wire))
        assert "7k3q" not in wire and "SYNTHgrow12345" not in wire
        assert out.data.get("truncated", {}).get("returned_chars") == len(wire)


# ------------------------------------------------------------------ 바이트 동일


def _pages():
    pages = []
    for i in range(12):
        name = (lambda k, i=i: f"페이지{i} 링크 {k} /a?b={k} 2024-10 507f1f77bcf86cd7994390{k:02d}")
        pages.append((ActionType.OBSERVE_PAGE, _observe_data(40 + 20 * i, name, repeated=i % 2 == 0)))
        pages.append((ActionType.EXTRACT, {"items": [{"text": f"본문 {i} " * (i + 1), "href": f"/x/{i}"}
                                                      for _ in range(5 + i)]}))
    return pages


async def test_byte_identical_when_registry_empty_or_values_absent():
    base = [envelope_json(await _route(20_000, a, d)) for a, d in _pages()]
    secret_registry.register_secret_values(["SYNTHabsent0001", "4q7z"])
    secret_registry.register_url("http://h/?token=SYNTHabsent0002&id=0123456789abcdef01234567")
    after = [envelope_json(await _route(20_000, a, d)) for a, d in _pages()]
    assert len(base) >= 10 and after == base


# ------------------------------------------------------------------ 성능(CI 단정은 4값·200KB 와 비었을 때)


def _big_observe(chars: int) -> Dict[str, Any]:
    per = 100
    n = chars // (per + 120)
    vals = ("SYNTHperfA1234", "SYNTHperfB5678", "4k2q", "SYNTHperfC9012")
    return _observe_data(n, lambda i: f"상품 {i} 설명 {vals[i % 4]} " + "가" * (per - 40))


async def _best(limit: int, data: Dict[str, Any], runs: int = 5) -> float:
    best = float("inf")
    for _ in range(runs):
        public_url._CONTENT_MEMO = (-1, {})  # 차가운 메모 — 처음 보는 관찰 기준
        t0 = time.perf_counter()
        await _route(limit, ActionType.OBSERVE_PAGE, data)
        best = min(best, time.perf_counter() - t0)
    return best * 1000


async def test_perf_empty_registry_and_four_values_200kb():
    data = _big_observe(200_000)
    assert len(json.dumps(data, ensure_ascii=False)) >= 150_000
    t0 = time.perf_counter()
    for _ in range(50):
        public_url.content_view(data)
    empty_view_ms = (time.perf_counter() - t0) * 1000 / 50
    assert empty_view_ms < 1.0, empty_view_ms  # 비었을 때: 가림 없이 얕은 구조만(내용은 공유)
    empty = await _best(20_000, data)
    secret_registry.register_secret_values(["SYNTHperfA1234", "SYNTHperfB5678", "4k2q"])
    secret_registry.register_url("http://h/?token=SYNTHperfC9012")
    four = await _best(20_000, data)
    print(f"\nWS75 perf: empty {empty:.1f}ms, 4 values {four:.1f}ms (+{four - empty:.1f}ms), "
          f"empty content_view {empty_view_ms:.3f}ms")
    assert four - empty <= 20.0, (empty, four)
