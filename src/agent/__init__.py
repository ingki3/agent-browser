"""WS-8 자율 에이전트 루프.

    from agent import AgentLoop

    loop = AgentLoop(page=page, engine=engine, dispatcher=dispatcher)
    run = await loop.run("이 사이트에 로그인하기")
    print(run.completed, run.step_count, run.budget)

구성:
* `keywords`  — 목표에서 관찰 키워드 추출 (LLM 미사용, 결정론적)
* `policy`    — 프롬프트 구성 및 LLM 응답 파싱
* `loop`      — 관찰-판단-액션-검증 루프 및 종료 조건
"""

from agent.keywords import extract_keywords, keywords_for_step
from agent.loop import (
    DEFAULT_MAX_TOKENS,
    MAX_CONSECUTIVE_FAILURES,
    AgentLoop,
    StepOutcome,
    TaskRun,
)
from agent.policy import (
    FINISH,
    GIVE_UP,
    LOOP_ACTIONS,
    SYSTEM_PROMPT,
    Decision,
    build_messages,
    decision_to_params,
    parse_decision,
    render_observation,
)

__all__ = [
    # 루프
    "AgentLoop",
    "TaskRun",
    "StepOutcome",
    "DEFAULT_MAX_TOKENS",
    "MAX_CONSECUTIVE_FAILURES",
    # 정책
    "Decision",
    "build_messages",
    "parse_decision",
    "decision_to_params",
    "render_observation",
    "LOOP_ACTIONS",
    "SYSTEM_PROMPT",
    "FINISH",
    "GIVE_UP",
    # 키워드
    "extract_keywords",
    "keywords_for_step",
]
