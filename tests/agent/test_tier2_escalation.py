"""Tier-2 SoM 에스컬레이션 테스트 (Stage 4 Task 7, PRD §3.1 사다리).

LLM·VLM·브라우저를 호출하지 않는다. `_run_step`은 스크립트로 대체하고
`_tier2_step`은 실제 코드를 가짜 디스패처/주입 그라운더로 구동한다.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from agent import loop as loop_mod
from agent.loop import AgentLoop, StepOutcome
from agent.policy import Decision
from contracts import ActionResult, ActionType, ErrorCode
from llm import BudgetGuard
from perception import PerceptionEngine
from vision.grounder import GroundingResult

PNG_B64 = base64.b64encode(b"\x89PNG fake").decode("ascii")


class _FakeClient:
    async def __aenter__(self):
        return self

    async def close(self):
        pass


def _result(success: bool, action=ActionType.CLICK, error_code=None, data=None) -> ActionResult:
    return ActionResult(
        success=success,
        action=action,
        current_url="https://example.com/",
        snapshot_epoch=0,
        tab_id="tab-0",
        healed=False,
        reobserve_required=False,
        retry_safe=True,
        error_code=None if success else error_code,
        error_message=None if success else "scripted failure",
        data=data or {},
    )


class _FakeDispatcher:
    """take_screenshot(annotate_som)와 element/좌표 클릭만 흉내낸다."""

    def __init__(self, engine, som_tags=None):
        self.engine = engine
        self.calls = []
        self.som_tags = som_tags if som_tags is not None else [
            {
                "tag": "A1",
                "role": "button",
                "name": "",
                "selector_path": "#ic3",
                "bbox": {"x": 10, "y": 10, "width": 20, "height": 20},
            }
        ]

    async def dispatch(self, action, params):
        self.calls.append((action, dict(params)))
        if action is ActionType.TAKE_SCREENSHOT:
            return _result(
                True,
                action,
                data={
                    "som_tags": self.som_tags,
                    "image_b64": PNG_B64,
                    "image_tokens": 1600,
                    "candidate_count": len(self.som_tags),
                },
            )
        if "element_id" in params:
            ok = self.engine.get_handle(params["element_id"]) is not None
            return _result(ok, action, error_code=None if ok else ErrorCode.ELEMENT_NOT_FOUND)
        if "x" in params and "y" in params:
            return _result(True, action)
        return _result(False, action, error_code=ErrorCode.TIMEOUT)


class _Page:
    url = "https://example.com/"

    async def query_selector(self, selector):
        return object()


def _grounder(result: GroundingResult):
    calls = []

    async def ground(client, png, candidates, goal, *, failure_context=""):
        calls.append({"png": png, "candidates": candidates, "goal": goal, "ctx": failure_context})
        return result

    ground.calls = calls  # type: ignore[attr-defined]
    return ground


def _scripted_loop(monkeypatch, script, *, grounder=None, **kwargs):
    """script: [(action, element_id, success, error_code), ...]"""
    monkeypatch.setattr(loop_mod, "OpenRouterClient", lambda *a, **k: _FakeClient())
    items = list(script)
    engine = PerceptionEngine()
    dispatcher = _FakeDispatcher(engine)

    class ScriptedLoop(AgentLoop):
        async def _run_step(self, client, goal, step, history, failures):
            if not items:
                return StepOutcome(step=step, decision=Decision(action="give_up", reason="script end"))
            action, element_id, success, error_code = items.pop(0)
            decision = Decision(action=action, element_id=element_id, reason="scripted")
            if action in ("finish", "give_up"):
                return StepOutcome(step=step, decision=decision)
            return StepOutcome(
                step=step, decision=decision, result=_result(success, error_code=error_code)
            )

    loop = ScriptedLoop(
        page=_Page(),
        engine=engine,
        dispatcher=dispatcher,
        config=None,
        budget=BudgetGuard(),
        max_steps=30,
        grounder=grounder,
        **kwargs,
    )
    return loop, dispatcher


def _run(loop):
    return asyncio.run(loop.run("장바구니 열기"))


# ---------------------------------------------------------------------------


def test_two_failures_trigger_tier2(monkeypatch):
    ground = _grounder(GroundingResult(tag="A1", reason="cart icon", latency_ms=12.5, tokens=150))
    loop, dispatcher = _scripted_loop(
        monkeypatch,
        [
            ("click", "@e1", False, ErrorCode.TIMEOUT),
            ("click", "@e1", False, ErrorCode.TIMEOUT),
            ("finish", None, True, None),
        ],
        grounder=ground,
        som_enabled=True,
    )
    run = _run(loop)

    assert len(ground.calls) == 1
    assert ground.calls[0]["candidates"][0].tag == "A1"
    assert ground.calls[0]["candidates"][0].selector_path == "#ic3"
    assert ground.calls[0]["png"] == base64.b64decode(PNG_B64)

    tier2 = [s for s in run.steps if s.note == "tier2"]
    assert len(tier2) == 1
    assert tier2[0].succeeded is True
    assert tier2[0].decision.element_id == "@s1"
    assert tier2[0].vision_latency_ms == 12.5
    assert run.tier2_calls == 1
    assert run.to_dict()["tier2_calls"] == 1

    actions = [a for a, _ in dispatcher.calls]
    assert actions == [ActionType.TAKE_SCREENSHOT, ActionType.CLICK]
    assert dispatcher.calls[1][1]["element_id"] == "@s1"
    assert run.completed is True


def test_tier2_reuses_failed_action_type(monkeypatch):
    ground = _grounder(GroundingResult(tag="A1"))
    loop, dispatcher = _scripted_loop(
        monkeypatch,
        [
            ("type_text", "@e1", False, ErrorCode.TIMEOUT),
            ("type_text", "@e1", False, ErrorCode.TIMEOUT),
            ("finish", None, True, None),
        ],
        grounder=ground,
        som_enabled=True,
    )
    loop_items = None  # noqa: F841
    # 마지막 실패 decision의 text를 유지해야 한다
    run = _run(loop)
    assert dispatcher.calls[1][0] is ActionType.TYPE_TEXT
    assert dispatcher.calls[1][1]["element_id"] == "@s1"
    assert run.tier2_calls == 1


def test_coordinate_mode_dispatches_xy_click(monkeypatch):
    ground = _grounder(GroundingResult(point=(640, 360)))
    loop, dispatcher = _scripted_loop(
        monkeypatch,
        [
            ("click", "@e1", False, ErrorCode.TIMEOUT),
            ("click", "@e1", False, ErrorCode.TIMEOUT),
            ("finish", None, True, None),
        ],
        grounder=ground,
        som_enabled=True,
    )
    dispatcher.som_tags = []  # 순수 Canvas
    run = _run(loop)
    assert dispatcher.calls[1][0] is ActionType.CLICK
    assert dispatcher.calls[1][1]["x"] == 640 and dispatcher.calls[1][1]["y"] == 360
    assert "epoch" in dispatcher.calls[1][1]
    assert run.tier2_calls == 1
    assert run.completed is True


def test_grounding_miss_counts_as_failure(monkeypatch):
    ground = _grounder(GroundingResult(tag=None, reason="none"))
    loop, _ = _scripted_loop(
        monkeypatch,
        [
            ("click", "@e1", False, ErrorCode.TIMEOUT),
            ("click", "@e1", False, ErrorCode.TIMEOUT),
            ("finish", None, True, None),
        ],
        grounder=ground,
        som_enabled=True,
    )
    run = _run(loop)
    tier2 = [s for s in run.steps if s.note.startswith("tier2")]
    assert len(tier2) == 1 and tier2[0].succeeded is False
    assert run.tier2_calls == 1
    assert "연속" in run.terminal_reason  # 3회 연속 실패로 종료


def test_harmless_stale_ref_failures_do_not_count(monkeypatch):
    ground = _grounder(GroundingResult(tag="A1"))
    loop, _ = _scripted_loop(
        monkeypatch,
        [
            ("click", "@e2", True, None),
            ("click", "@e2", False, ErrorCode.ELEMENT_NOT_FOUND),  # 무해
            ("click", "@e2", False, ErrorCode.ELEMENT_NOT_FOUND),  # 무해
            ("finish", None, True, None),
        ],
        grounder=ground,
        som_enabled=True,
    )
    run = _run(loop)
    assert ground.calls == []
    assert run.tier2_calls == 0


def test_fourth_unattended_call_exceeds_budget(monkeypatch):
    from contracts.thresholds import TIER2_MAX_CALLS_UNATTENDED

    assert TIER2_MAX_CALLS_UNATTENDED == 3
    ground = _grounder(GroundingResult(tag="A1"))
    pair = [("click", "@e1", False, ErrorCode.TIMEOUT)] * 2
    loop, _ = _scripted_loop(
        monkeypatch,
        pair * 4 + [("finish", None, True, None)],
        grounder=ground,
        som_enabled=True,
        unattended=True,
    )
    run = _run(loop)
    assert len(ground.calls) == 3
    assert run.tier2_calls == 3
    assert ErrorCode.TIER2_BUDGET_EXCEEDED.value in run.terminal_reason
    assert run.completed is False


def test_interactive_allows_five_calls(monkeypatch):
    ground = _grounder(GroundingResult(tag="A1"))
    pair = [("click", "@e1", False, ErrorCode.TIMEOUT)] * 2
    loop, _ = _scripted_loop(
        monkeypatch,
        pair * 5 + [("finish", None, True, None)],
        grounder=ground,
        som_enabled=True,
        unattended=False,
    )
    run = _run(loop)
    assert run.tier2_calls == 5
    assert run.completed is True


def test_som_disabled_never_escalates(monkeypatch):
    ground = _grounder(GroundingResult(tag="A1"))
    loop, dispatcher = _scripted_loop(
        monkeypatch,
        [
            ("click", "@e1", False, ErrorCode.TIMEOUT),
            ("click", "@e1", False, ErrorCode.TIMEOUT),
            ("click", "@e1", False, ErrorCode.TIMEOUT),
            ("finish", None, True, None),
        ],
        grounder=ground,
        som_enabled=False,
    )
    run = _run(loop)
    assert ground.calls == []
    assert dispatcher.calls == []
    assert run.tier2_calls == 0
    assert all(s.note != "tier2" for s in run.steps)
    assert "연속 3회" in run.terminal_reason


def test_default_is_som_disabled():
    loop = AgentLoop(page=_Page(), engine=None, dispatcher=None, budget=BudgetGuard())
    assert loop.som_enabled is False
    assert loop.unattended is True


def test_policy_prompt_mentions_som_ids():
    from agent.policy import SYSTEM_PROMPT

    assert "@sN" in SYSTEM_PROMPT
