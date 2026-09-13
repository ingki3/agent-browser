"""VLM 그라운더 (PRD §3.1 Tier-2, Stage 4 Task 5).

SoM 스크린샷을 VLM에 보내 **태그 하나**(태그 모드) 또는 **좌표 하나**
(좌표 모드, 옵션 B — 순수 Canvas처럼 DOM 후보가 0개일 때)를 받는다.

설계 원칙:
* **응답을 신뢰하지 않는다.** 후보에 없는 태그는 버린다(환각 차단 — 루프의
  element_id 검증과 같은 원칙). 뷰포트 밖 좌표도 버린다.
* **프롬프트는 반드시 `build_vision_prompt`로 감싼다** (PRD §5.3-2). 이미지
  안의 문구는 전부 외부 데이터다.
* **이미지 토큰은 호출 전에 예산에 선반영한다** (PRD §3.4). OpenRouter usage가
  이미지 토큰을 포함해 돌려주는지는 모델마다 다르므로, 계약 상수
  `SOM_IMAGE_TOKENS_PER_CAPTURE`를 보수적으로 먼저 더한다.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from contracts import thresholds
from security.prompt_isolation import build_vision_prompt
from vision.candidates import SomCandidate

logger = logging.getLogger(__name__)

#: 응답은 `{"tag": ..}` 또는 `{"x", "y"}` 한 줄이면 충분하다.
GROUND_MAX_TOKENS = 256

_TAG_INSTRUCTION = (
    "당신은 웹 페이지 스크린샷에서 목표 달성에 필요한 요소 하나를 고르는 시각 "
    "그라운딩 모델입니다. 스크린샷 위 각 상호작용 요소에는 영숫자 태그(예: B3)가 "
    "그려져 있습니다. 목표에 가장 적합한 요소의 태그 하나를 고르십시오. "
    "적합한 요소가 없으면 tag를 null로 하십시오. "
    '반드시 JSON 한 개만 출력하십시오: {"tag": "B3" 또는 null, "reason": "한 줄"}'
)

_POINT_INSTRUCTION = (
    "당신은 웹 페이지 스크린샷에서 목표 달성을 위해 클릭할 지점을 고르는 시각 "
    "그라운딩 모델입니다. 이 페이지는 DOM 요소가 없는(Canvas 등) 화면이므로 "
    "클릭할 픽셀 좌표를 직접 지정하십시오. 좌표 원점은 좌상단, 단위는 픽셀입니다. "
    "적합한 지점이 없으면 x, y를 null로 하십시오. "
    '반드시 JSON 한 개만 출력하십시오: {"x": 정수, "y": 정수, "reason": "한 줄"}'
)


@dataclass
class GroundingResult:
    """VLM 그라운딩 결과. `tag`/`point` 중 최대 하나만 채워진다."""

    tag: Optional[str] = None
    point: Optional[Tuple[int, int]] = None
    reason: str = ""
    latency_ms: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0

    @property
    def grounded(self) -> bool:
        return self.tag is not None or self.point is not None


def _candidate_lines(candidates: Sequence[SomCandidate]) -> str:
    lines = []
    for c in candidates:
        name = (c.name or "").strip().replace("\n", " ")[:60]
        lines.append(f"- {c.tag}: {c.role}" + (f' "{name}"' if name else " (라벨 없음)"))
    return "\n".join(lines)


def _build_messages(
    png: bytes,
    candidates: Sequence[SomCandidate],
    goal: str,
    failure_context: str,
) -> List[Dict[str, Any]]:
    tag_mode = bool(candidates)
    instruction = _TAG_INSTRUCTION if tag_mode else _POINT_INSTRUCTION

    text = [f"목표: {goal}"]
    if failure_context:
        text.append(f"이전 실패 맥락:\n{failure_context}")
    if tag_mode:
        text.append(f"후보 태그 목록:\n{_candidate_lines(candidates)}")
    else:
        text.append(
            f"뷰포트 크기: {thresholds.VIEWPORT_WIDTH}x{thresholds.VIEWPORT_HEIGHT} "
            "(x는 0 이상 너비 미만, y는 0 이상 높이 미만)"
        )

    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return [
        {"role": "system", "content": build_vision_prompt(instruction)},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "\n\n".join(text)},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]


def _prerecord_image_tokens(client: Any, model: str) -> None:
    """이미지 토큰을 호출 **전에** 예산에 더한다 (PRD §3.4 누적 합산)."""
    budget = getattr(client, "budget", None)
    if budget is None:
        return
    budget.record(
        prompt_tokens=thresholds.SOM_IMAGE_TOKENS_PER_CAPTURE,
        completion_tokens=0,
        model=model,
        # 실제 과금은 응답 usage에 실리므로 여기서는 비용 0으로 토큰만 누적한다.
        actual_usd=0.0,
    )


def _vision_model(client: Any) -> Optional[str]:
    config = getattr(client, "config", None)
    if config is None:
        return None
    return getattr(config, "effective_vision_model", None) or getattr(config, "model", None)


def _parse_tag(payload: Any, candidates: Sequence[SomCandidate]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    tag = payload.get("tag")
    if not isinstance(tag, str):
        return None
    tag = tag.strip().upper()
    valid = {c.tag for c in candidates}
    if tag not in valid:
        logger.debug("VLM이 후보에 없는 태그를 반환: %r", tag)
        return None
    return tag


def _parse_point(payload: Any) -> Optional[Tuple[int, int]]:
    if not isinstance(payload, dict):
        return None
    try:
        x = int(payload.get("x"))  # type: ignore[arg-type]
        y = int(payload.get("y"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (0 <= x < thresholds.VIEWPORT_WIDTH and 0 <= y < thresholds.VIEWPORT_HEIGHT):
        logger.debug("VLM이 뷰포트 밖 좌표를 반환: (%d, %d)", x, y)
        return None
    return (x, y)


async def ground(
    client: Any,
    png: bytes,
    candidates: Sequence[SomCandidate],
    goal: str,
    *,
    failure_context: str = "",
) -> GroundingResult:
    """스크린샷과 후보를 VLM에 보내 태그(또는 좌표)를 고른다.

    `client`는 `OpenRouterClient` 호환 객체(`complete()`, `budget`, `config`)면
    된다. 후보가 비어 있으면 좌표 모드로 동작한다.
    """
    started = time.perf_counter()
    model = _vision_model(client)
    messages = _build_messages(png, candidates, goal, failure_context)

    _prerecord_image_tokens(client, model or "")

    kwargs: Dict[str, Any] = {
        "temperature": 0,
        "max_tokens": GROUND_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    if model:
        kwargs["model"] = model
    response = await client.complete(messages, **kwargs)

    result = GroundingResult(
        latency_ms=(time.perf_counter() - started) * 1000,
        tokens=int(getattr(response, "total_tokens", 0) or 0),
        cost_usd=float(getattr(response, "cost_usd", 0.0) or 0.0),
    )

    try:
        payload = response.parse_json()
    except Exception as exc:  # noqa: BLE001 — 파싱 실패는 '그라운딩 없음'으로 처리
        result.reason = f"VLM 응답 파싱 실패: {str(exc)[:120]}"
        return result

    if isinstance(payload, dict):
        reason = payload.get("reason")
        result.reason = str(reason)[:200] if reason is not None else ""

    if candidates:
        result.tag = _parse_tag(payload, candidates)
    else:
        result.point = _parse_point(payload)
    return result


__all__ = ["GROUND_MAX_TOKENS", "GroundingResult", "ground"]
