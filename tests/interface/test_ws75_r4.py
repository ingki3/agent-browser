"""WS-75 R4 (#199 재검증 S1·S2) — 자르기·줄 나누기 **전에** 등록 값 전체를 가린다. 합성 값만.

* S1: 사본(③)을 만들 때마다 version 이 바뀌는 결정적 스케줄(가짜 content_view — 매 copy 직후 등록). 마지막 재시도
  (fallback)도 자르기 전 원본에서 가리고 자른다 — 서버가 자른 등록 값 앞부분(≥ 4자) 0. 끝까지 흔들리면 fail-closed
  (내용 칸 페이지 글자를 고정 문구로) — 어떤 등록 값의 앞부분도 0, 봉투 ≤ 상한.
* S2: 줄바꿈·탭이 든 등록 값이 axtree_summary 에서 줄 경계에 걸쳐도 전체가 가려진다 — 요약은 줄로 나누지 않는다(엔진
  형식이면 가린 요소 이름으로 다시 만들고, 아니면 전체 문자열을 한 번에 가린다). 실 Chromium aria-label 1건(외부 요청은
  모두 abort, set_content).
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_cli import requires_chromium  # noqa: E402
from test_ws75_pipeline import _cut_position, _observe_data, _prefix_leaks, _route, public_url  # noqa: E402

from browser.serve_profile import PROFILE_ROOT_ENV  # noqa: E402
from contracts import ActionType  # noqa: E402
from interface.handoff import STATE_ROOT_ENV  # noqa: E402
from interface.server_envelope import envelope_json  # noqa: E402
from security import secret_registry  # noqa: E402

R = "[REDACTED]"
T = "T9qXRaceCanary83"
LIMIT = 2_000


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path / "servers"))
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    secret_registry.clear()
    yield
    secret_registry.clear()


def _schedule(monkeypatch, plan):
    """content_view 를 감싸 k 번째 사본 직후 plan(k) 값을 등록한다(None 이면 등록 안 함 — version 그대로)."""
    original = public_url.content_view
    calls = []

    def view(data):
        masked = original(data)
        value = plan(len(calls))
        calls.append(secret_registry.version())
        if value is not None:
            secret_registry.register_secret_values([value])
        return masked

    monkeypatch.setattr(public_url, "content_view", view)
    return calls


# ------------------------------------------------------------------ S1


async def test_last_retry_redone_from_original_no_server_cut_prefix(monkeypatch):
    """검증 재현 스케줄: 세 copy 직후 version 변경, 셋째에 대상 값 등록(재시도 상한 소진) → 그 뒤 안정."""
    cut = await _cut_position(LIMIT)
    calls = _schedule(monkeypatch, lambda k: (T if k == 2 else f"D7pZDummyCanary{k}") if k < 3 else None)
    for p in range(max(0, cut - len(R) - len(T) - 2), cut + len(T) + 2):
        secret_registry.clear()
        calls.clear()
        text = "w" * p + T + "w" * (LIMIT * 2 - p)
        out = await _route(LIMIT, ActionType.EXTRACT, {"items": [{"text": text}]})
        wire = envelope_json(out)
        assert len(calls) >= 4, "마지막 재시도가 원본에서 다시 가리지 않으면 이 시험은 fallback 을 재지 않는다"
        assert _prefix_leaks(wire, T) == [], (p, wire[-120:])
        assert len(wire) <= LIMIT


async def test_reproduced_eleven_char_prefix(monkeypatch):
    """재검증 보고 원 재현 모양(끝에 `T9qXRaceCan` 11자가 걸리는 위치, 본문 5549자) — 앞부분이 남지 않는다."""
    p = await _cut_position(LIMIT) - 11
    _schedule(monkeypatch, lambda k: (T if k == 2 else f"D7pZDummyCanary{k}") if k < 3 else None)
    out = await _route(LIMIT, ActionType.EXTRACT, {"items": [{"text": "w" * p + T + "w" * (5549 - p - len(T))}]})
    wire = envelope_json(out)
    assert _prefix_leaks(wire, T) == [], wire[-120:]
    assert R in out.data["items"][0]["text"]


async def test_version_never_settles_fails_closed(monkeypatch):
    """매 copy 직후 새 값 등록(끝까지 흔들림) → 내용 칸은 고정 문구, 어떤 등록 값의 앞부분도 0, 봉투 ≤ 상한."""
    values = [f"SYNTHflap{i:02d}Canary{i:02d}" for i in range(50)]
    calls = _schedule(monkeypatch, lambda k: values[k] if k < len(values) else None)
    text = "w" * 1533 + "".join(values[:6]) + "w" * 4000
    out = await _route(LIMIT, ActionType.EXTRACT, {"items": [{"text": text}]})
    wire = envelope_json(out)
    assert len(calls) >= 4
    for v in values[:len(calls)]:
        assert _prefix_leaks(wire, v) == [], (v, wire[-160:])
    assert out.data["items"][0]["text"] == R
    assert len(wire) <= LIMIT


async def test_version_never_settles_observe_fails_closed(monkeypatch):
    values = [f"SYNTHobsFlap{i:02d}x{i:02d}" for i in range(50)]
    calls = _schedule(monkeypatch, lambda k: values[k] if k < len(values) else None)
    data = _observe_data(200, lambda i: f"item {i} " + " ".join(values[:6]))
    out = await _route(5_000, ActionType.OBSERVE_PAGE, data)
    wire = envelope_json(out)
    assert len(calls) >= 4
    assert [v for v in values[:len(calls)] if any(v[:k] in wire for k in range(6, len(v) + 1))] == []
    obs = out.data["observation"]
    assert {e["name"] for e in obs["elements"]} == {R}
    assert all(e["element_id"].startswith("@e") for e in obs["elements"])  # 구조 칸은 그대로
    assert len(wire) <= 5_000


# ------------------------------------------------------------------ S2


@pytest.mark.parametrize("value", ["Q7vx\nCanary83", "Q7vx\tCanary83", "Tab\tNew\nLine9Canary"])
async def test_summary_value_across_line_boundary_masked(value):
    secret_registry.register_secret_values([value])
    els = [{"element_id": "@e1", "role": "button", "name": value, "interactable": True},
           {"element_id": "@e2", "role": "link", "name": "ordinary link", "interactable": True}]
    summary = f'@e1 button "{value}"\n@e2 link "ordinary link"'
    data = {"observation": {"title": "t", "url": "http://127.0.0.1:9/p", "snapshot_epoch": 1, "token_count": 1,
                            "elements": els, "axtree_summary": summary}}
    out = await _route(20_000, ActionType.OBSERVE_PAGE, data)
    # 파이프라인 사본 자체(직렬화 재가림 전)도 가려져 있어야 한다 — 자르기·묶음 맞춤은 이 사본 위에서 한다.
    assert out.data["observation"]["axtree_summary"] == f'@e1 button "{R}"\n@e2 link "ordinary link"'
    obj = json.loads(envelope_json(out))
    got = obj["data"]["observation"]["axtree_summary"]
    assert value not in got, got
    assert got == f'@e1 button "{R}"\n@e2 link "ordinary link"', got
    assert obj["data"]["observation"]["elements"][0]["name"] == R


@pytest.mark.parametrize("value", ["Q7vx\nCanary83", "Tab\tNew\nLine9Canary"])
async def test_summary_not_engine_format_masked_whole(value):
    """요약이 요소들로 만든 형식과 다르면(요소 밖 줄) 전체 문자열 가림 — 줄바꿈 등록 값도 가려진다."""
    secret_registry.register_secret_values([value])
    els = [{"element_id": "@e2", "role": "link", "name": "ordinary link", "interactable": True}]
    summary = f'@e1 button "{value}"\n@e2 link "ordinary link"'
    data = {"observation": {"title": "t", "url": "http://127.0.0.1:9/p", "snapshot_epoch": 1, "token_count": 1,
                            "elements": els, "axtree_summary": summary}}
    out = await _route(20_000, ActionType.OBSERVE_PAGE, data)
    got = json.loads(envelope_json(out))["data"]["observation"]["axtree_summary"]
    assert got == f'@e1 button "{R}"\n@e2 link "ordinary link"', got


def test_content_summary_unit_order_and_identity():
    value = "Q7vx\nCanary83"
    plain = '@e1 button "Q7vx"\n@e2 link "Canary83 here"'
    secret_registry.register_secret_values([value])
    assert public_url.content_summary(plain) is plain  # 각 줄에 따로 있는 조각은 원문(전체 일치만 가림)
    s = f'@e1 button "{value}"'
    assert public_url.content_summary(s) == f'@e1 button "{R}"'


@requires_chromium
async def test_real_chromium_multiline_aria_label_summary_masked(tmp_path):
    from interface.mcp_server import BrowserMCPServer

    value = "Q7vx\nCanary83"
    secret_registry.register_secret_values([value])
    async with BrowserMCPServer(headless=True, recipes=False, nav_settle=False, handoff_root=tmp_path / "h") as s:
        await s._page.context.route("**/*", lambda route: route.abort())
        await s._page.set_content("<!doctype html><title>local multiline</title><button id=target aria-label=\""
                                  + html.escape(value, quote=True) + "\">Apply</button>")
        assert await s._page.locator("#target").get_attribute("aria-label") == value
        out = await s.call_tool("browser_observe_page", {"force_full_tree": True})
        obj = json.loads(envelope_json(out))
    obs = obj["data"]["observation"]
    button = next(e for e in obs["elements"] if e["role"] == "button")
    assert out.success
    assert value not in button["name"]
    assert value not in obs["axtree_summary"], obs["axtree_summary"]
    assert "Canary83" not in obs["axtree_summary"]
