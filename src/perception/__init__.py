"""WS-2 인지 엔진 패키지 (PRD §3.1, §3.2, §4.2, §4.3).

* `PerceptionEngine` — 살균 → shadow pierce → 스코어링 파이프라인
* `collect`          — Computed Style 기반 단일 evaluate 수집
* `prune`            — Prune4Web 결정론적 스코어러
* `scan_shadow_roots`— CDP pierce closed Shadow DOM 순회

`build_extractor`는 `harness.recall`이 Gate 2 측정에 사용하는 진입점이다.
"""

from typing import Any, List, Sequence, Tuple

from perception.engine import (
    ElementHandle,
    PerceptionEngine,
    RecoveryTrace,
    estimate_tokens,
)
from perception.sanitizer import (
    COLLECT_SCRIPT,
    INTERACTIVE_ROLES,
    INTERACTIVE_TAGS,
    RawElement,
    SanitizedPage,
    collect,
    parse_collection,
)
from perception.scorer import (
    ROLE_WEIGHTS,
    ScoredElement,
    expand_top_n,
    filter_by_keywords,
    levenshtein,
    prune,
    prune_async,
    score_element,
    similarity,
)
from perception.shadow_dom import (
    FULL_HEALING_STRATEGIES,
    SHADOW_HEALING_STRATEGIES,
    ShadowElement,
    ShadowScanResult,
    scan_shadow_roots,
)


def build_extractor():
    """`harness.recall`이 Gate 2 측정에 사용하는 추출기 팩토리.

    반환 함수는 Playwright `page`를 받아 (role, name) 목록을 돌려준다.
    """

    engine = PerceptionEngine()

    async def _extract(page: Any, top_n: int = 20) -> List[Tuple[str, str]]:
        result = await engine.observe_page(prune_top_n=top_n, page=page)
        return [(e.role, e.name) for e in result.elements]

    return _extract


__all__ = [
    # 엔진
    "PerceptionEngine",
    "ElementHandle",
    "RecoveryTrace",
    "estimate_tokens",
    "build_extractor",
    # 살균기
    "collect",
    "parse_collection",
    "RawElement",
    "SanitizedPage",
    "COLLECT_SCRIPT",
    "INTERACTIVE_TAGS",
    "INTERACTIVE_ROLES",
    # 스코어러
    "prune",
    "prune_async",
    "score_element",
    "ScoredElement",
    "ROLE_WEIGHTS",
    "levenshtein",
    "similarity",
    "expand_top_n",
    "filter_by_keywords",
    # Shadow DOM
    "scan_shadow_roots",
    "ShadowElement",
    "ShadowScanResult",
    "SHADOW_HEALING_STRATEGIES",
    "FULL_HEALING_STRATEGIES",
]
