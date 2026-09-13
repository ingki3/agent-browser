"""SoM(Set-of-Marks) 태그 후보 수집기 (PRD §3.1 Tier-2, Stage 4 Task 1).

뷰포트 안에 **보이는** 상호작용 가능 요소 전체를 DOM 순서대로 모아
영숫자 태그(`A1..A9, B1..`)를 붙인다. Top-20 프루닝 이전 집합이므로
Tier-1 스코어러와 무관하며, 텍스트 라벨이 없어도 후보에 남는다 —
그것이 Tier-2가 존재하는 이유다.

설계 — 살균기 재사용:
가시성 판정과 css 경로 생성은 `perception.sanitizer.COLLECT_SCRIPT`가
이미 단일 evaluate로 수행한다(Computed Style 기반, shadow root 순회,
루트 기준 고유 셀렉터). 여기서는 그 스크립트를 **그대로 실행**하고
Python에서 뷰포트 교차만 추가로 거른다. JS를 복제하면 두 판정이
어긋날 때 Tier-1이 본 요소와 Tier-2가 태그한 요소가 달라진다.

주의 — 살균기의 `in_viewport`는 `bottom > 0 && right > 0` 기준이라
경계에 걸친 요소도 포함된다. 태그 라벨은 bbox 좌상단에 그리므로
좌상단이 뷰포트 밖이면 라벨이 잘린다. 따라서 여기서는 좌상단이
뷰포트 안에 있는 요소만 남긴다.
"""

from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel

from contracts import thresholds
from contracts.models import BBox
from perception.sanitizer import RawElement, collect

#: 한 장에 태그할 수 있는 최대 후보 수. 60개를 넘으면 라벨이 겹쳐
#: VLM 오독이 급증한다(1280×720 기준).
MAX_CANDIDATES = 60

#: 태그 문자. 숫자 0은 O와, I는 1과 혼동되므로 제외한다.
_TAG_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_TAG_DIGITS = "123456789"


class SomCandidate(BaseModel):
    """오버레이 태그 하나에 대응하는 후보 요소."""

    #: VLM에 보여줄 태그 (예: "B3")
    tag: str
    #: 요소를 다시 찾기 위한 CSS 경로 (살균기가 생성한 루트 기준 고유 경로)
    selector_path: str
    #: 뷰포트 기준 바운딩 박스
    bbox: BBox
    role: str
    name: str


def make_tag(index: int) -> str:
    """0 기반 인덱스를 `A1..A9, B1..` 태그로 바꾼다.

    1글자+1숫자 두 글자 고정 — VLM이 "A10"을 "A1"+"0"으로 잘못 읽는
    일을 막는다.
    """
    letter = _TAG_LETTERS[index // len(_TAG_DIGITS)]
    digit = _TAG_DIGITS[index % len(_TAG_DIGITS)]
    return f"{letter}{digit}"


def _in_viewport(el: RawElement, width: int, height: int) -> bool:
    b = el.bbox
    return (
        b.get("width", 0) > 0
        and b.get("height", 0) > 0
        and 0 <= b.get("x", -1) < width
        and 0 <= b.get("y", -1) < height
    )


def _viewport_size(page: Any) -> tuple[int, int]:
    size = getattr(page, "viewport_size", None)
    if isinstance(size, dict) and size.get("width") and size.get("height"):
        return int(size["width"]), int(size["height"])
    return thresholds.VIEWPORT_WIDTH, thresholds.VIEWPORT_HEIGHT


async def collect_candidates(page: Any) -> List[SomCandidate]:
    """뷰포트 안 가시·상호작용 요소를 태그 순서대로 수집한다 (최대 60개).

    빈 리스트는 "DOM에 태그할 것이 없다"는 뜻이며(순수 Canvas 등),
    호출자는 이를 좌표 모드 전환 신호로 쓴다.
    """
    width, height = _viewport_size(page)
    sanitized = await collect(page)

    out: List[SomCandidate] = []
    for el in sanitized.elements:
        if el.disabled:
            continue
        if not el.in_viewport or not _in_viewport(el, width, height):
            continue
        if not el.css_path:
            continue
        out.append(
            SomCandidate(
                tag=make_tag(len(out)),
                selector_path=el.css_path,
                bbox=BBox(**el.bbox),
                role=el.role,
                name=el.name,
            )
        )
        if len(out) >= MAX_CANDIDATES:
            break
    return out
