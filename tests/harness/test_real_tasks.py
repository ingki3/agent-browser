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
