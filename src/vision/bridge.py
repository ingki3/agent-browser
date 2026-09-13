"""태그 -> element_id 브리지 (PRD §3.1 Tier-2, Stage 4 Task 6).

VLM이 고른 `SomCandidate`를 인지 엔진의 핸들 테이블에 `@sN`으로 등록해,
**기존 액션 경로**(dispatcher의 element_id + epoch 검증, 자가치유, HITL)를
그대로 태운다. 새 액션을 만들지 않는다.

`@s` 접두사는 Tier-1 `@e`와 구분하기 위한 것이다 — 트레이스만 보고도
시각 폴백이 발동했는지 식별할 수 있다. 카운터는 에포크마다 1부터 다시
시작한다(핸들 자체가 에포크와 함께 무효화되므로 번호를 이어갈 이유가 없다).
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from perception.engine import ElementHandle
from vision.candidates import SomCandidate

#: Tier-2 핸들 접두사
SOM_ID_PREFIX = "@s"

#: 엔진 객체별 (에포크, 다음 번호). 엔진에 필드를 추가하지 않고 외부에서
#: 추적한다 — perception 모듈 변경을 1메서드로 제한하기 위해서다.
_counters: Dict[int, Tuple[int, int]] = {}


def _next_id(engine: Any) -> str:
    key = id(engine)
    epoch = engine.epoch
    last_epoch, seq = _counters.get(key, (epoch, 0))
    if last_epoch != epoch:
        seq = 0
    seq += 1
    _counters[key] = (epoch, seq)
    return f"{SOM_ID_PREFIX}{seq}"


async def bind_tag(engine: Any, page: Any, candidate: SomCandidate) -> str:
    """후보를 `@sN` 핸들로 등록하고 element_id를 반환한다.

    `page.query_selector(candidate.selector_path)`로 요소가 아직 있는지
    확인한다(살균기 경로는 루트 기준 고유 경로). 없으면 `LookupError` —
    캡처와 바인딩 사이에 DOM이 바뀐 것이므로 호출자는 재캡처해야 한다.
    """
    element = await page.query_selector(candidate.selector_path)
    if element is None:
        raise LookupError(f"SoM 후보 {candidate.tag}의 요소가 사라짐: {candidate.selector_path}")

    element_id = _next_id(engine)
    engine.register_external_handle(
        element_id,
        ElementHandle(
            element_id=element_id,
            epoch=engine.epoch,
            role=candidate.role,
            name=candidate.name,
            css_path=candidate.selector_path,
            # 살균기 경로는 루트 기준이라 shadow 내부 요소도 document 기준
            # 로케이터로 지목한다(`_locator_for`가 shadow면 role+name으로
            # 우회하는데, SoM 후보는 라벨 없는 요소가 대부분이라 경로가 낫다).
            is_shadow=False,
        ),
    )
    return element_id


__all__ = ["SOM_ID_PREFIX", "bind_tag"]
