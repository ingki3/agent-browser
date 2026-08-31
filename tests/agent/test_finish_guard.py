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
    """거부 후에도 우기면 completed=False로 종료되어야 한다.

    자기 보고를 그대로 믿으면 false_claim이 완수율에 섞인다.
    """
    from agent import loop as loop_mod

    src = inspect.getsource(loop_mod.AgentLoop.run)
    assert "run.completed = not last_action_failed" in src, (
        "실패 후 finish를 완료로 집계하고 있습니다"
    )
