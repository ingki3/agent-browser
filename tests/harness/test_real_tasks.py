"""실환경 태스크셋 정합성 테스트 (WS-9).

네트워크를 호출하지 않는다. 태스크 정의 자체의 결함을 잡는다.
실제 실행은 `harness.agent_eval`이 담당한다.
"""

from __future__ import annotations

import pytest

from harness.real_tasks import (
    DIFFICULTY_LEVELS,
    TASKS,
    get_task,
    tasks_by_difficulty,
    validate_taskset,
)


def test_taskset_has_no_defects():
    assert validate_taskset() == []


def test_task_ids_are_unique():
    ids = [t.task_id for t in TASKS]
    assert len(ids) == len(set(ids))


def test_every_difficulty_has_multiple_tasks():
    """쉬운 태스크만 있으면 완수율이 부풀려진다."""
    counts = tasks_by_difficulty()
    for level in DIFFICULTY_LEVELS:
        assert counts.get(level, 0) >= 2, f"난이도 {level}가 부족: {counts}"


def test_multistep_tier_exists():
    """2~4스텝 태스크만으로는 긴 호흡의 실패 모드를 관측할 수 없다."""
    assert "multistep" in DIFFICULTY_LEVELS
    multistep = [t for t in TASKS if t.difficulty == "multistep"]
    assert len(multistep) >= 4, f"멀티스텝 태스크가 {len(multistep)}개뿐"


def test_multistep_tasks_allow_enough_steps():
    """상태를 누적하려면 스텝 여유가 필요하다."""
    for task in TASKS:
        if task.difficulty == "multistep":
            assert task.max_steps >= 8, f"{task.task_id}: {task.max_steps}스텝"


def test_multistep_goals_require_multiple_actions():
    """단일 액션으로 끝나는 목표는 멀티스텝이 아니다.

    순차 표현('이어서', '뒤')이 있거나, 조작 대상이 2개 이상이어야 한다.
    'A와 B를 입력하고 버튼 누르기'처럼 순차어 없이도 다단계인 경우가 있다.
    """
    sequence_markers = ("이어서", "뒤", "다시", "차례로", "모두", "그 ")
    action_verbs = ("입력", "클릭", "선택", "체크", "누르", "추가", "변경", "이동")

    for task in TASKS:
        if task.difficulty != "multistep":
            continue
        goal = task.goal
        has_sequence = any(m in goal for m in sequence_markers)
        verb_count = sum(goal.count(v) for v in action_verbs)
        assert has_sequence or verb_count >= 2, (
            f"{task.task_id}: 단일 액션으로 보임 — {goal}"
        )


def test_every_task_has_independent_success_check():
    """성공 판정은 에이전트 자기 보고와 분리되어야 한다."""
    for task in TASKS:
        assert task.success_expr.strip(), task.task_id
        # 검증식이 에이전트 상태가 아니라 페이지 상태를 봐야 한다.
        assert any(
            token in task.success_expr
            for token in ("location", "document", "window")
        ), f"{task.task_id}: 페이지 상태를 검증하지 않음"


def test_every_task_declares_capability():
    """무엇을 검증하는 태스크인지 명시되어야 한다."""
    for task in TASKS:
        assert task.capability.strip(), task.task_id


def test_all_urls_are_https():
    for task in TASKS:
        assert task.url.startswith("https://"), task.task_id


def test_step_limits_are_reasonable():
    for task in TASKS:
        assert 2 <= task.max_steps <= 15, f"{task.task_id}: {task.max_steps}"


def test_get_task_returns_none_for_unknown():
    assert get_task("does-not-exist") is None
    assert get_task(TASKS[0].task_id) is TASKS[0]


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.task_id)
def test_goal_is_actionable(task):
    """목표가 구체적이어야 키워드 추출이 동작한다."""
    from agent.keywords import extract_keywords

    keywords = extract_keywords(task.goal)
    assert len(keywords) >= 2, f"{task.task_id}: 키워드 부족 {keywords}"


# ---------------------------------------------------------------------------
# dynamic 티어 (WS-12)
#     기존 태스크는 모두 '이미 존재하는 요소'를 다뤘다. 실제 웹에서는
#     클릭 후에야 요소가 생기거나, 비활성이 풀리거나, 스크롤해야 나타난다.
# ---------------------------------------------------------------------------


def test_dynamic_tier_exists():
    assert "dynamic" in DIFFICULTY_LEVELS
    dynamic = [t for t in TASKS if t.difficulty == "dynamic"]
    assert len(dynamic) >= 4, f"dynamic 태스크가 {len(dynamic)}개뿐"


def test_dynamic_tasks_cover_distinct_capabilities():
    """같은 능력을 반복 측정하면 커버리지가 늘지 않는다."""
    caps = [t.capability for t in TASKS if t.difficulty == "dynamic"]
    assert len(set(caps)) == len(caps), f"중복된 capability: {caps}"


def test_taskset_capabilities_are_unique_overall():
    """전체 태스크셋에서 능력 설명이 중복되면 무엇을 못 하는지 알 수 없다."""
    from collections import Counter

    counts = Counter(t.capability for t in TASKS)
    dupes = {c: n for c, n in counts.items() if n > 1}
    # 의도적 반복 검증은 capability에 '(반복 검증)'을 명시한다.
    unexpected = {c: n for c, n in dupes.items() if "반복 검증" not in c}
    assert not unexpected, f"의도치 않은 중복: {unexpected}"
