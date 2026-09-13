"""WS-v1.1 Tier-2 Set-of-Marks(SoM) 시각 그라운딩 패키지 (PRD §3.1, §3.4, §8-2).

텍스트 셀렉터가 연속 실패했을 때의 폴백 사다리:

* `collect_candidates` — 뷰포트 내 가시·상호작용 요소에 태그 부여 (프루닝 이전 집합)
* `render_som`         — 태그 라벨을 DOM에 주입해 PNG를 찍고 원상복구
* `ground`             — VLM에 스크린샷을 보내 태그(또는 Canvas 좌표) 하나를 받음
* `bind_tag`           — 고른 태그를 `@sN` 핸들로 등록해 기존 액션 경로로 실행

계약(`contracts/`)은 변경하지 않는다 — `annotate_som`, `SOM_IMAGE_TOKENS_PER_CAPTURE`,
`VIEWPORT_*`는 이미 동결돼 있다.
"""

from vision.bridge import SOM_ID_PREFIX, bind_tag
from vision.candidates import MAX_CANDIDATES, SomCandidate, collect_candidates, make_tag
from vision.grounder import GroundingResult, ground
from vision.overlay import SOM_ATTR, render_som

__all__ = [
    "MAX_CANDIDATES",
    "SomCandidate",
    "collect_candidates",
    "make_tag",
    "SOM_ATTR",
    "render_som",
    "SOM_ID_PREFIX",
    "bind_tag",
    "GroundingResult",
    "ground",
]
