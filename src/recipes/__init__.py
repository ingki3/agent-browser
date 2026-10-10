"""동작 캐시(레시피) — WS-38.

에이전트가 성공한 동작 묶음을 "페이지 구조(PageKey) + 대상 자리(Target) + 기대 결과(Expect)" 로
저장하고, 다음에 한 번의 서버 도구 호출(`browser_recipe run`)로 재생한다. 재생은 기존 디스패처
경로를 그대로 지나므로(HITL·egress·WS-37 신원 가드·IPI 신호·사후 확인) 새 우회로가 아니다.
페이지 구조가 어긋나면 멈추고 이유를 알린다 — 텍스트 유사도·좌표 재생은 쓰지 않는다.

설계: .hermes/plans/2026-10-08_144951-action-recipe-cache.md (§9 결정 우선).
"""

from recipes.keys import (  # noqa: F401
    KEYS_JS,
    locate,
    origin_of,
    snapshot,
    url_pattern,
)
