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
            if action in ("finish", "give_up", "request_vision"):
                # 실제 _run_step과 동일 — 판단 스텝은 액션을 실행하지 않는다.
                # 수용된 request_vision은 성공 스텝이다(실제 구현과 동일).
                return StepOutcome(
                    step=step,
                    decision=decision,
                    judged_success=decision.is_vision_request and self.som_enabled,
                    note=(
                        "vision_request: Tier-2 시각 폴백으로 전환합니다"
                        if decision.is_vision_request and self.som_enabled
                        else ("vision_request: SoM 비활성" if decision.is_vision_request else "")
                    ),
                )
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


# --- LLM 자발 요청 경로 (request_vision) ------------------------------------
#
# 실측(glm-5.3-flash, icon-buttons live) — 라벨 없는 아이콘 5개를 LLM이 하나씩
# 다 눌러봤고 각 클릭은 *성공*이었다. "2회 연속 실패" 조건은 성공-but-헛수고
# 에는 영원히 안 걸린다. 시각 폴백이 가장 필요한 순간에 발동하지 않는 구조.
# LLM이 스스로 "구분이 안 된다"고 판단하면 즉시 Tier-2로 간다.


def test_request_vision_triggers_tier2_without_prior_failures(monkeypatch):
    ground = _grounder(GroundingResult(tag="A1", reason="cart icon", latency_ms=9.0, tokens=100))
    loop, dispatcher = _scripted_loop(
        monkeypatch,
        [
            ("click", "@e1", True, None),          # 성공했지만 헛수고
            ("request_vision", None, True, None),  # LLM: "구분이 안 됩니다"
            ("finish", None, True, None),
        ],
        grounder=ground,
        som_enabled=True,
    )
    run = _run(loop)

    assert len(ground.calls) == 1
    tier2 = [s for s in run.steps if s.note == "tier2"]
    assert len(tier2) == 1
    assert tier2[0].succeeded is True
    assert tier2[0].decision.action == "click"          # 재실행할 실패 액션이 없으니 CLICK
    assert tier2[0].decision.element_id == "@s1"
    assert run.tier2_calls == 1
    assert run.completed is True
    # 요청 스텝 자체는 액션을 실행하지 않는다
    actions = [a for a, _ in dispatcher.calls]
    assert actions == [ActionType.TAKE_SCREENSHOT, ActionType.CLICK]


def test_request_vision_counts_toward_tier2_budget(monkeypatch):
    """성공하는 Tier-2가 반복돼도 상한(무인 3회)은 자발 요청에도 똑같이 적용된다."""
    ground = _grounder(GroundingResult(tag="A1", reason="ok"))
    loop, _ = _scripted_loop(
        monkeypatch,
        [("request_vision", None, True, None)] * 5 + [("finish", None, True, None)],
        grounder=ground,
        som_enabled=True,
        unattended=True,
    )
    run = _run(loop)
    assert run.tier2_calls == 3
    assert ErrorCode.TIER2_BUDGET_EXCEEDED.value in run.terminal_reason


def test_request_vision_when_som_disabled_is_a_failed_step(monkeypatch):
    """SoM이 꺼져 있으면 요청은 실패로 기록되고, 반복하면 연속 실패로 끊긴다."""
    ground = _grounder(GroundingResult(tag="A1"))
    loop, dispatcher = _scripted_loop(
        monkeypatch,
        [("request_vision", None, True, None)] * 4 + [("finish", None, True, None)],
        grounder=ground,
        som_enabled=False,
    )
    run = _run(loop)
    assert ground.calls == []
    assert dispatcher.calls == []
    assert run.tier2_calls == 0
    assert run.completed is False
    assert "연속 3회" in run.terminal_reason


# --- 수용된 request_vision은 성공 스텝이다 -------------------------------------
#
# 실측(live 50런) — Tier-2 런 5개 전부 히스토리에 이렇게 남았다:
#     request_vision -> FAIL vision_request
#     click @s1      -> OK tier2
# 비전 호출은 실제로 됐고 정답까지 맞혔는데 요청 스텝이 FAIL로 찍혔다.
# 그 줄이 그대로 LLM 히스토리에 들어가 "네 요청은 실패했다"고 알려주니,
# 모델은 믿고 재요청했고 icon-buttons 런 2건이 상한 3회를 소진해 끝났다.
# 루프 상태에서만 실패로 세지 않았을 뿐, 모델에게 보이는 텍스트는 FAIL이었다.


def test_accepted_request_vision_is_reported_as_success(monkeypatch):
    ground = _grounder(GroundingResult(tag="A1", reason="cart icon"))
    loop, _ = _scripted_loop(
        monkeypatch,
        [("request_vision", None, True, None), ("finish", None, True, None)],
        grounder=ground,
        som_enabled=True,
    )
    run = _run(loop)
    requests = [s for s in run.steps if s.decision.is_vision_request]
    assert len(requests) == 1
    assert requests[0].succeeded is True
    assert "FAIL" not in requests[0].summary()
    assert "OK" in requests[0].summary()


def test_rejected_request_vision_is_still_a_failure(monkeypatch):
    """SoM이 꺼져 있으면 갈 곳이 없으므로 실패 표기를 유지한다."""
    ground = _grounder(GroundingResult(tag="A1"))
    loop, _ = _scripted_loop(
        monkeypatch,
        [("request_vision", None, True, None)] * 4 + [("finish", None, True, None)],
        grounder=ground,
        som_enabled=False,
    )
    run = _run(loop)
    requests = [s for s in run.steps if s.decision.is_vision_request]
    assert requests
    assert all(s.succeeded is False for s in requests)
    assert "FAIL" in requests[0].summary()


def test_accepted_request_vision_history_line_does_not_say_fail(monkeypatch):
    """LLM이 실제로 보는 히스토리 줄을 검증한다 — 재요청 유발의 직접 원인."""
    seen: list[list[str]] = []

    ground = _grounder(GroundingResult(tag="A1", reason="ok"))
    loop, _ = _scripted_loop(
        monkeypatch,
        [("request_vision", None, True, None), ("finish", None, True, None)],
        grounder=ground,
        som_enabled=True,
    )
    original = loop._run_step

    async def spy(client, goal, step, history, failures):
        seen.append(list(history))
        return await original(client, goal, step, history, failures)

    loop._run_step = spy  # type: ignore[method-assign]
    _run(loop)

    # finish 스텝이 볼 히스토리에 request_vision 줄이 FAIL로 남아 있으면 안 된다.
    assert seen, "히스토리를 관측하지 못했다"
    vision_lines = [
        line for hist in seen for line in hist if line.startswith("request_vision")
    ]
    assert vision_lines, "request_vision 줄이 히스토리에 없다"
    assert all("FAIL" not in line for line in vision_lines), vision_lines


@pytest.mark.parametrize("som_enabled,expected_ok", [(True, True), (False, False)])
def test_real_run_step_marks_request_vision(monkeypatch, som_enabled, expected_ok):
    """`_run_step` 실물을 탄다 — ScriptedLoop이 우회하는 경로를 직접 검증.

    다른 테스트는 `_run_step`을 스크립트로 대체하므로 구현 자체는 검증하지
    못한다. 여기서는 LLM 응답만 가짜로 주고 실제 코드를 통과시킨다.
    """

    class _Resp:
        content = '{"action": "request_vision", "reason": "아이콘 구분 불가"}'
        total_tokens = 10
        cost_usd = 0.0

        def parse_json(self):
            import json

            return json.loads(self.content)

    class _Client:
        async def complete(self, messages, **kwargs):
            return _Resp()

    class _Engine:
        async def observe_page(self, page, **kwargs):
            from contracts import ObserveResult

            return ObserveResult(
                title="t", url="u", snapshot_epoch=0, elements=[],
                axtree_summary="", token_count=0,
            )

    loop = AgentLoop(
        page=_Page(),
        engine=_Engine(),
        dispatcher=_FakeDispatcher(PerceptionEngine()),
        budget=BudgetGuard(),
        som_enabled=som_enabled,
    )
    outcome = asyncio.run(loop._run_step(_Client(), "장바구니 열기", 1, [], []))

    assert outcome.decision.is_vision_request
    assert outcome.succeeded is expected_ok
    assert ("FAIL" in outcome.summary()) is not expected_ok


def test_policy_prompt_offers_request_vision_only_when_som_enabled():
    from agent.policy import REQUEST_VISION, build_messages
    from contracts import ObserveResult

    obs = ObserveResult(title="t", url="u", snapshot_epoch=0, elements=[], axtree_summary="", token_count=0)
    on = build_messages("g", obs, step=1, max_steps=5, history=[], limit=20, som_enabled=True)
    off = build_messages("g", obs, step=1, max_steps=5, history=[], limit=20)
    assert REQUEST_VISION in on[0]["content"]
    assert REQUEST_VISION not in off[0]["content"]


def test_policy_prompt_mentions_som_ids():
    from agent.policy import SYSTEM_PROMPT

    assert "@sN" in SYSTEM_PROMPT
