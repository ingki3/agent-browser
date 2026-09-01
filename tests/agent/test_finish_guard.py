"""WS-15 실환경 평가에서 드러난 결함 2건의 회귀 테스트.

hn-comments 실패 분석:
  click @e1 -> OK
  click @e2 -> FAIL E_TIMEOUT
  finish    -> OK          <- 실패 직후 완료 선언

두 가지 결함이 겹쳤다.
1. 이름이 둘 다 'comments'라 LLM이 구분할 수 없었다
   (/newcomments = 사이트 전체, item?id= = 개별 기사)
2. 액션이 실패했는데 완료를 선언할 수 있었다
"""

from __future__ import annotations

import inspect
import textwrap

import pytest

from agent.policy import _href_hint, render_observation
from contracts import BBox, ObservedElement, ObserveResult


def make_observation(elements, url="https://news.ycombinator.com/"):
    return ObserveResult(
        title="t",
        url=url,
        snapshot_epoch=0,
        elements=elements,
        axtree_summary="요약",
        token_count=10,
    )


def make_element(eid, role="link", name="comments"):
    return ObservedElement(
        element_id=eid,
        role=role,
        name=name,
        value=None,
        bbox=BBox(x=0, y=0, width=50, height=16),
        interactable=True,
        is_shadow=False,
        score=1.0,
    )


class FakeHandle:
    def __init__(self, href):
        self.href = href


# ---------------------------------------------------------------------------
# 1. 링크 목적지 힌트
# ---------------------------------------------------------------------------


def test_href_hint_extracts_last_segment():
    assert _href_hint("newcomments", "/") == "newcomments"
    assert _href_hint("item?id=123", "/") == "item"
    assert _href_hint("/help/example-domains", "/") == "example-domains"


def test_href_hint_skips_useless_cases():
    """정보가 없는 힌트는 붙이지 않는다 (토큰 낭비)."""
    assert _href_hint(None, "/") == ""
    assert _href_hint("", "/") == ""
    assert _href_hint("#section", "/") == ""       # 같은 페이지 앵커
    assert _href_hint("/news", "/news") == ""      # 현재 경로와 동일


def test_href_hint_is_length_capped():
    long_path = "/" + "x" * 200
    assert len(_href_hint(long_path, "/")) <= 24


def test_same_named_links_are_distinguishable_in_prompt():
    """hn-comments 재현 — 이름이 같아도 목적지로 구분되어야 한다."""
    obs = make_observation([
        make_element("@e1", name="comments"),
        make_element("@e2", name="78 comments"),
    ])
    handles = {
        "@e1": FakeHandle("newcomments"),
        "@e2": FakeHandle("item?id=49507822"),
    }
    rendered = render_observation(obs, 20, handles)

    assert "newcomments" in rendered, "사이트 전체 댓글 링크가 구분되지 않습니다"
    assert "item" in rendered, "기사별 댓글 링크가 구분되지 않습니다"
    # 두 줄이 서로 달라야 LLM이 고를 수 있다
    lines = [ln for ln in rendered.splitlines() if "comment" in ln]
    assert len(lines) == 2
    assert lines[0] != lines[1]


def test_render_without_handles_is_unchanged():
    """핸들이 없으면 기존 표현 그대로여야 한다 (하위 호환)."""
    obs = make_observation([make_element("@e1", name="로그인")])
    assert "->" not in render_observation(obs, 20)
    assert "->" not in render_observation(obs, 20, None)


def test_hint_is_skipped_when_name_already_says_it():
    """이름에 이미 있는 정보를 반복하지 않는다."""
    obs = make_observation([make_element("@e1", name="newcomments 보기")])
    handles = {"@e1": FakeHandle("newcomments")}
    assert "->" not in render_observation(obs, 20, handles)


def test_hint_only_applies_to_links():
    """버튼 등 링크가 아닌 요소에는 붙이지 않는다."""
    obs = make_observation([make_element("@e1", role="button", name="제출")])
    handles = {"@e1": FakeHandle("submit")}
    assert "->" not in render_observation(obs, 20, handles)


def test_contract_model_name_is_not_mutated():
    """계약 모델의 name은 원본이어야 한다.

    힌트를 계약 모델에 넣으면 골든셋의 이름 일치 판정이 깨진다.
    실제로 그렇게 만들었다가 Gate 2 Recall이 1.0 -> 0.818로 떨어졌다.
    """
    el = make_element("@e1", name="comments")
    obs = make_observation([el])
    handles = {"@e1": FakeHandle("newcomments")}
    render_observation(obs, 20, handles)

    assert el.name == "comments", "계약 모델의 name이 변경되었습니다"
    assert obs.elements[0].name == "comments"


def test_handle_carries_href():
    """엔진이 핸들에 href를 담아야 프롬프트에서 쓸 수 있다."""
    from perception.engine import ElementHandle

    assert "href" in ElementHandle.__dataclass_fields__


# ---------------------------------------------------------------------------
# 2. 실패 직후 finish 금지
# ---------------------------------------------------------------------------


def test_loop_guards_finish_after_failure():
    """루프가 직전 액션 실패 시 finish를 거부해야 한다."""
    from agent import loop as loop_mod

    src = inspect.getsource(loop_mod.AgentLoop.run)
    assert "last_action_failed" in src, "실패 여부를 추적하지 않습니다"
    assert "finish_rejected" in src, "거부 후 재시도 처리가 없습니다"


def test_prompt_forbids_finish_after_failure():
    """프롬프트에도 명시되어야 한다 (루프 가드는 최후 방어선)."""
    from agent.policy import SYSTEM_PROMPT

    assert "FAIL" in SYSTEM_PROMPT, "실패 시 finish 금지가 프롬프트에 없습니다"


def test_failed_finish_is_not_counted_as_completed():
    """가드의 완수 판정 의미론 (WS-18 정밀화 반영).

    옛 의미론: 거부 후 재선언도 completed=False.
    새 의미론: 재확인(새 관찰) 후의 재선언은 수용하되 terminal_reason에
    흔적을 남긴다. 목표 달성 여부는 루프가 알 수 없으므로 최종 판정은
    독립 검증/호출자의 몫이다.

    여기서는 소스 수준 불변식만 확인한다. 행동은 아래
    TestFinishGuardBehavior에서 실제 루프로 검증한다.
    """
    from agent import loop as loop_mod

    src = inspect.getsource(loop_mod.AgentLoop.run)
    assert "guard_active" in src, "가드 발동 조건이 없습니다"
    assert "last_failure_harmless" in src, "무해 실패 판정이 없습니다"
    assert "재확인 후" in src, "재확인 수용 경로가 없습니다"


# ---------------------------------------------------------------------------
# 5. 실행 시간 상한 (WS-17)
# ---------------------------------------------------------------------------


def test_loop_enforces_wall_clock_limit():
    """루프가 PRD의 실행 시간 상한을 강제해야 한다.

    계약에 MAX_WALL_CLOCK_SECONDS(600초)가 정의돼 있는데 루프가 이를
    읽지 않아, 네트워크 대기로 멈추면 무한정 매달렸다.
    실측 — internet-checkbox-both가 13분 넘게 CPU 0.1%로 정지해
    31태스크 통합 측정 전체를 막았다.

    문자열 존재만 확인하면 `if False:`로 바꿔도 통과한다(실제로 겪었다).
    조건식이 살아 있는지 AST로 검사한다.
    """
    import ast

    from agent import loop as loop_mod

    src = inspect.getsource(loop_mod.AgentLoop.run)
    tree = ast.parse(textwrap.dedent(src))

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # if <무언가> >= MAX_WALL_CLOCK_SECONDS: 형태를 찾는다
        test = node.test
        if isinstance(test, ast.Compare) and any(
            isinstance(c, ast.Name) and c.id == "MAX_WALL_CLOCK_SECONDS"
            for c in test.comparators
        ):
            found = True
            break

    assert found, (
        "루프에 살아 있는 시간 상한 조건이 없습니다 "
        "(상수만 언급하고 실제 비교하지 않는 경우 포함)"
    )


def test_loop_imports_limit_from_contracts():
    """상한값을 자체 정의하지 말고 계약에서 가져와야 한다."""
    from agent import loop as loop_mod
    from contracts.thresholds import MAX_WALL_CLOCK_SECONDS

    assert loop_mod.MAX_WALL_CLOCK_SECONDS == MAX_WALL_CLOCK_SECONDS
    assert MAX_WALL_CLOCK_SECONDS == 600


def test_harness_has_outer_timeout():
    """하네스에도 상한이 있어야 한다 (최후 방어선).

    루프 상한만으로는 부족하다. 한 스텝 안에서 멈추면 루프의 검사
    지점에 도달하지 못한다.
    """
    from harness import agent_eval

    src = inspect.getsource(agent_eval)
    assert "asyncio.wait_for" in src, "하네스에 외곽 타임아웃이 없습니다"
    assert "TASK_TIMEOUT_MARGIN_S" in src


def test_harness_timeout_is_looser_than_loop():
    """루프 상한이 먼저 걸려야 정상 종료 경로를 탄다."""
    from contracts.thresholds import MAX_WALL_CLOCK_SECONDS
    from harness.agent_eval import TASK_TIMEOUT_MARGIN_S

    assert TASK_TIMEOUT_MARGIN_S > 0
    assert MAX_WALL_CLOCK_SECONDS + TASK_TIMEOUT_MARGIN_S > MAX_WALL_CLOCK_SECONDS


def test_all_record_paths_fill_same_keys():
    """정상/타임아웃/예외 경로가 같은 키를 채워야 한다.

    하나라도 빠지면 출력부에서 KeyError로 측정 전체가 무효화된다.
    실측 — 타임아웃 경로에 'steps'가 없어 31태스크 측정이 exit 2로
    끝났다. 결과 파일은 {"error": "측정 실패: 'steps'"}뿐이었다.
    """
    import ast
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src" / "harness" / "agent_eval.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))

    updates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "update"):
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "record"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Dict):
                updates.append({
                    k.value for k in arg.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                })

    assert len(updates) >= 3, (
        f"record.update() 경로가 {len(updates)}개뿐입니다 "
        "(정상/타임아웃/예외 3종이 필요합니다)"
    )

    base = updates[0]
    for i, keys in enumerate(updates[1:], 2):
        missing = base - keys
        assert not missing, (
            f"{i}번째 경로에 키가 없습니다: {sorted(missing)} "
            "— 출력부에서 KeyError가 납니다"
        )


# ---------------------------------------------------------------------------
# 6. 빈 LLM 응답 진단 (WS-18)
# ---------------------------------------------------------------------------


def _resp(content, finish_reason="stop", reasoning=""):
    from llm.client import LLMResponse

    return LLMResponse(
        content=content,
        model="test",
        prompt_tokens=10,
        completion_tokens=0,
        cost_usd=0.0,
        finish_reason=finish_reason,
        reasoning=reasoning,
        raw={},
    )


def test_empty_response_reports_real_cause():
    """빈 응답을 'JSON 파싱 실패'로 보고하면 원인을 오해한다.

    실측(dyn-enable-input) — reasoning 계열 모델이 max_tokens를 사고
    과정에만 쓰고 content를 못 냈다. 로그에는 이렇게만 남았다:

        JSON 파싱 실패: Expecting value: line 1 column 1 (char 0).
        본문 앞부분: ''

    모델이 잘못된 JSON을 냈다고 오해하게 된다. 실제로는 아무것도
    내지 못한 것이다.
    """
    from llm import LLMError

    with pytest.raises(LLMError) as exc:
        _resp("", finish_reason="length", reasoning="가" * 500).parse_json()

    msg = str(exc.value)
    assert "본문을 내지 못했습니다" in msg
    assert "max_tokens" in msg
    assert "JSON 파싱 실패" not in msg, "빈 응답을 파싱 오류로 보고합니다"


def test_empty_response_without_truncation():
    """잘림이 아닌 빈 응답도 구분해서 알린다."""
    from llm import LLMError

    with pytest.raises(LLMError) as exc:
        _resp("", finish_reason="stop").parse_json()

    msg = str(exc.value)
    assert "빈 응답" in msg
    assert "finish_reason" in msg


def test_malformed_json_still_reports_parse_error():
    """실제 파싱 오류는 그대로 보고한다 (과잉 일반화 방지)."""
    from llm import LLMError

    with pytest.raises(LLMError) as exc:
        _resp("{not json").parse_json()

    assert "JSON 파싱 실패" in str(exc.value)


def test_valid_json_unaffected():
    assert _resp('{"action": "finish"}').parse_json() == {"action": "finish"}
    assert _resp('```json\n{"a": 1}\n```').parse_json() == {"a": 1}


# ---------------------------------------------------------------------------
# 7. finish 가드 행동 검증 (WS-18) — 실제 루프를 스크립트로 구동
# ---------------------------------------------------------------------------


class _FakeClient:
    """OpenRouterClient 대역. 네트워크를 쓰지 않는다."""

    async def __aenter__(self):
        return self

    async def close(self):
        pass


def _scripted_loop(monkeypatch, script):
    """AgentLoop을 만들고 _run_step이 script의 outcome을 순서대로 내게 한다.

    script: [(action, element_id, success, error_code), ...]
      action이 'finish'/'give_up'이면 terminal decision.
    """
    from agent import loop as loop_mod
    from agent.loop import AgentLoop, StepOutcome
    from agent.policy import Decision
    from contracts import ActionResult, ActionType
    from llm import BudgetGuard

    monkeypatch.setattr(loop_mod, "OpenRouterClient", lambda *a, **k: _FakeClient())

    items = list(script)

    class ScriptedLoop(AgentLoop):
        async def _run_step(self, client, goal, step, history, failures):
            action, element_id, success, error_code = items.pop(0)
            decision = Decision(
                action=action,
                element_id=element_id,
                reason="scripted",
            )
            if action in ("finish", "give_up"):
                return StepOutcome(step=step, decision=decision)
            result = ActionResult(
                success=success,
                action=ActionType.CLICK,
                current_url="https://example.com/",
                snapshot_epoch=0,
                tab_id="tab-0",
                healed=False,
                reobserve_required=False,
                retry_safe=True,
                error_code=None if success else error_code,
                error_message=None if success else "scripted failure",
            )
            return StepOutcome(step=step, decision=decision, result=result)

    class _Page:
        url = "https://example.com/"

    return ScriptedLoop(
        page=_Page(),
        engine=None,
        dispatcher=None,
        config=None,
        budget=BudgetGuard(),
        max_steps=10,
    )


def _run(loop):
    # asyncio.get_event_loop()는 다른 async 테스트가 이벤트 루프를 닫은
    # 뒤에는 RuntimeError를 낸다(전체 스위트에서만 실패, 단독 통과 —
    # 실제로 겪었다). asyncio.run()은 매번 새 루프를 만들므로 안전하다.
    import asyncio

    return asyncio.run(loop.run("목표"))


def test_behavior_harmless_stale_ref_allows_finish(monkeypatch):
    """melon-chart 재현: 같은 요소 성공 -> 같은 요소 NOT_FOUND -> finish.

    구식 참조 실패는 직전 성공의 부산물이므로 가드가 발동하지 않아야
    한다. 수정 전에는 silent_win으로 집계됐다.
    """
    from contracts import ErrorCode

    loop = _scripted_loop(monkeypatch, [
        ("click", "@e2", True, None),
        ("click", "@e2", False, ErrorCode.ELEMENT_NOT_FOUND),
        ("finish", None, True, None),
    ])
    run = _run(loop)

    assert run.completed is True
    assert "무해" in run.terminal_reason
    assert len(run.steps) == 3, "가드가 불필요하게 거부했습니다"


def test_behavior_effect_failure_still_guarded(monkeypatch):
    """hn-comments 재현: 성공 -> 다른 요소 E_TIMEOUT -> finish.

    효과 실패 직후의 완료 선언은 여전히 1차 거부되어야 한다.
    """
    from contracts import ErrorCode

    loop = _scripted_loop(monkeypatch, [
        ("click", "@e1", True, None),
        ("click", "@e2", False, ErrorCode.TIMEOUT),
        ("finish", None, True, None),   # 1차 -> 거부되어야 함
        ("finish", None, True, None),   # 재확인 후 -> 수용
    ])
    run = _run(loop)

    assert run.completed is True
    assert "재확인 후" in run.terminal_reason, (
        "1차 거부 없이 통과했거나, 재선언이 수용되지 않았습니다"
    )
    assert any("[시스템]" in h for h in []) or len(run.steps) == 4


def test_behavior_different_element_not_found_still_guarded(monkeypatch):
    """다른 요소를 못 찾은 것은 무해하지 않다.

    '다음 단계 요소를 찾다 NOT_FOUND'는 목표 미달성일 수 있으므로
    가드가 발동해야 한다 (같은 요소 조건의 존재 이유).
    """
    from contracts import ErrorCode

    loop = _scripted_loop(monkeypatch, [
        ("click", "@e1", True, None),
        ("click", "@e7", False, ErrorCode.ELEMENT_NOT_FOUND),
        ("finish", None, True, None),   # 1차 -> 거부
        ("finish", None, True, None),   # 재확인 후 -> 수용
    ])
    run = _run(loop)

    assert run.completed is True
    assert "재확인 후" in run.terminal_reason, (
        "다른 요소의 NOT_FOUND가 무해로 오판됐습니다"
    )


def test_behavior_clean_finish_unchanged(monkeypatch):
    """실패 없는 정상 종료는 그대로여야 한다."""
    loop = _scripted_loop(monkeypatch, [
        ("click", "@e1", True, None),
        ("finish", None, True, None),
    ])
    run = _run(loop)

    assert run.completed is True
    assert run.terminal_reason == "LLM이 목표 달성을 선언했습니다."


def test_behavior_success_resets_harmless_state(monkeypatch):
    """성공 -> 무해실패 -> 다른 성공 -> 효과실패 -> finish.

    상태가 스텝마다 올바르게 재계산되는지 (재귀적 오염 방지).
    마지막 실패는 TIMEOUT이므로 가드가 발동해야 한다.
    """
    from contracts import ErrorCode

    loop = _scripted_loop(monkeypatch, [
        ("click", "@e1", True, None),
        ("click", "@e1", False, ErrorCode.ELEMENT_NOT_FOUND),  # 무해
        ("click", "@e3", True, None),
        ("click", "@e4", False, ErrorCode.TIMEOUT),            # 효과 실패
        ("finish", None, True, None),   # 1차 -> 거부
        ("finish", None, True, None),   # 수용
    ])
    run = _run(loop)

    assert run.completed is True
    assert "재확인 후" in run.terminal_reason, (
        "이전의 무해 판정이 이후 효과 실패에 잘못 이월됐습니다"
    )
