"""인지 엔진 (PRD §3.1, §3.2, §4.2).

`contracts.PerceptionEngineProtocol` 구현체.

책임:
* 살균 → shadow pierce → 스코어링 → Top-N 프루닝 파이프라인
* 전역 에포크 관리 (네비게이션/프레임 전환에서만 증가, PRD §4.2)
* Top-N 실패 시 4단계 복구 사다리 (PRD §3.2)
* element_id ↔ 요소 핸들 매핑 (WS-3 액션 실행이 소비)

에포크 정책의 핵심:
동적 DOM 변경(광고, 툴팁)으로는 에포크를 올리지 않는다. 올리면 광고가
200ms마다 도는 페이지에서 element_id가 영구히 만료돼 시스템이 마비된다.
대신 액션 실행 직전 per-element staleness를 검증한다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from contracts import BBox, ObservedElement, ObserveResult, thresholds

from perception.sanitizer import RawElement, SanitizedPage, collect
from perception.scorer import (
    ScoredElement,
    expand_top_n,
    filter_by_keywords,
    prune_async,
)
from perception.shadow_dom import ShadowScanResult, scan_shadow_roots

logger = logging.getLogger(__name__)

#: 관찰 토큰 추정 시 사용할 인코딩 (tiktoken 부재 시 근사 폴백)
_TIKTOKEN_ENCODING = "cl100k_base"


@dataclass
class ElementHandle:
    """element_id에 대응하는 요소 핸들 (WS-3이 액션 실행에 사용)."""

    element_id: str
    epoch: int
    role: str
    name: str
    css_path: str
    is_shadow: bool
    backend_node_id: Optional[int] = None
    host_path: str = ""
    testid: Optional[str] = None


@dataclass
class RecoveryTrace:
    """복구 사다리 실행 이력 (관측용)."""

    stages_used: List[str] = field(default_factory=list)
    final_top_n: int = thresholds.DEFAULT_PRUNE_TOP_N

    @property
    def recovered(self) -> bool:
        return bool(self.stages_used)


def estimate_tokens(text: str) -> int:
    """관찰 페이로드의 토큰 수를 추정한다 (tiktoken cl100k_base 기준)."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding(_TIKTOKEN_ENCODING).encode(text))
    except Exception:  # noqa: BLE001 - tiktoken 부재/네트워크 차단 환경
        # 한글·영문 혼용을 고려한 보수적 근사 (실제보다 과대 추정)
        return max(1, len(text) // 3)


def _to_observed(scored: ScoredElement, element_id: str) -> ObservedElement:
    el = scored.element
    box = el.bbox or {}
    return ObservedElement(
        element_id=element_id,
        role=el.role,
        name=el.name,
        value=el.value,
        bbox=BBox(
            x=int(box.get("x", 0)),
            y=int(box.get("y", 0)),
            width=int(box.get("width", 0)),
            height=int(box.get("height", 0)),
        ),
        interactable=not el.disabled,
        is_shadow=el.is_shadow,
        score=float(scored.score),
    )


def _summarize(elements: Sequence[ObservedElement]) -> str:
    """AxTree 요약 문자열. LLM 컨텍스트에 주입되는 형태."""
    lines = [
        f"{e.element_id} {e.role} \"{e.name}\"" + ("" if e.interactable else " (disabled)")
        for e in elements
    ]
    return "\n".join(lines)


class PerceptionEngine:
    """Tier-1 텍스트 우선 인지 엔진."""

    def __init__(
        self,
        *,
        default_top_n: int = thresholds.DEFAULT_PRUNE_TOP_N,
        enable_shadow_pierce: bool = True,
    ) -> None:
        self.default_top_n = default_top_n
        self.enable_shadow_pierce = enable_shadow_pierce

        self._epoch = 0
        self._handles: Dict[str, ElementHandle] = {}
        self._last_recovery = RecoveryTrace()
        self._last_latency_ms = 0.0

    # -- 에포크 (PRD §4.2) ---------------------------------------------------

    @property
    def epoch(self) -> int:
        return self._epoch

    def bump_epoch(self, reason: str = "navigation") -> int:
        """전역 에포크를 올린다.

        **네비게이션(navigate/reload)과 프레임 전환에서만 호출해야 한다.**
        동적 DOM 변경으로 호출하면 element_id가 조기 만료돼 시스템이 마비된다.
        """
        self._epoch += 1
        self._handles.clear()  # 이전 에포크의 핸들을 전역 무효화
        logger.debug("에포크 증가 -> %d (%s)", self._epoch, reason)
        return self._epoch

    def get_handle(self, element_id: str) -> Optional[ElementHandle]:
        """element_id에 대응하는 핸들을 조회한다 (현재 에포크만 유효)."""
        handle = self._handles.get(element_id)
        if handle is None or handle.epoch != self._epoch:
            return None
        return handle

    @property
    def handles(self) -> Dict[str, ElementHandle]:
        return dict(self._handles)

    # -- 관찰 (PerceptionEngineProtocol) -------------------------------------

    async def observe_page(
        self,
        tab_id: Optional[str] = None,
        prune_top_n: int = thresholds.DEFAULT_PRUNE_TOP_N,
        force_full_tree: bool = False,
        *,
        page: Any = None,
        cdp: Any = None,
        goal_keywords: Sequence[str] = (),
    ) -> ObserveResult:
        """페이지를 관찰해 프루닝된 후보 목록을 반환한다.

        `page`/`cdp`는 WS-1 BrowserCore가 주입한다. Protocol 시그니처를
        유지하기 위해 키워드 전용 인자로 받는다.
        """
        if page is None:
            raise ValueError("page 핸들이 필요합니다 (BrowserCore.get_active_page).")

        started = time.perf_counter()

        sanitized = await collect(page)
        candidates: List[RawElement] = list(sanitized.elements)

        # closed shadow root는 CDP pierce만이 유일한 경로
        shadow_result: Optional[ShadowScanResult] = None
        if self.enable_shadow_pierce and cdp is not None:
            shadow_result = await self._merge_shadow(candidates, cdp)

        top_n = prune_top_n or self.default_top_n
        recovery = RecoveryTrace(final_top_n=top_n)

        if force_full_tree:
            recovery.stages_used.append("full_tree")
            scored = await prune_async(candidates, len(candidates), goal_keywords)
        else:
            scored = await prune_async(candidates, top_n, goal_keywords)

        self._last_recovery = recovery
        result = self._build_result(sanitized, scored, shadow_result)
        self._last_latency_ms = (time.perf_counter() - started) * 1000
        return result

    async def _merge_shadow(
        self, candidates: List[RawElement], cdp: Any
    ) -> ShadowScanResult:
        """CDP pierce 결과를 후보 목록에 병합한다 (중복 제거 포함)."""
        try:
            shadow = await scan_shadow_roots(cdp)
        except Exception as exc:  # noqa: BLE001 - pierce 실패가 관찰 전체를 막지 않게
            logger.warning("Shadow pierce 실패: %s", exc)
            return ShadowScanResult()

        # sanitizer가 이미 잡은 open shadow 요소와 (role, name)으로 중복 제거
        seen = {(e.role, e.name) for e in candidates}
        next_seq = max((e.seq for e in candidates), default=-1) + 1

        for se in shadow.elements:
            key = (se.role, se.name)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                RawElement(
                    seq=next_seq,
                    role=se.role,
                    name=se.name,
                    tag=se.tag,
                    css_path=f"{se.host_path} >>> {se.tag}",
                    bbox=se.bbox,
                    value=se.value,
                    testid=se.testid,
                    disabled=se.disabled,
                    in_viewport=True,
                    is_shadow=True,
                )
            )
            next_seq += 1
        return shadow

    def _build_result(
        self,
        sanitized: SanitizedPage,
        scored: Sequence[ScoredElement],
        shadow: Optional[ShadowScanResult],
    ) -> ObserveResult:
        """스코어링 결과를 계약 모델로 변환하고 핸들을 등록한다."""
        observed: List[ObservedElement] = []
        for idx, item in enumerate(scored, start=1):
            element_id = f"@e{idx}"
            observed.append(_to_observed(item, element_id))
            self._handles[element_id] = ElementHandle(
                element_id=element_id,
                epoch=self._epoch,
                role=item.element.role,
                name=item.element.name,
                css_path=item.element.css_path,
                is_shadow=item.element.is_shadow,
                testid=item.element.testid,
            )

        summary = _summarize(observed)
        return ObserveResult(
            title=sanitized.title,
            url=sanitized.url,
            snapshot_epoch=self._epoch,
            elements=observed,
            axtree_summary=summary,
            token_count=estimate_tokens(summary),
        )

    # -- 복구 사다리 (PRD §3.2) ----------------------------------------------

    async def observe_with_recovery(
        self,
        page: Any,
        cdp: Any = None,
        *,
        goal_keywords: Sequence[str] = (),
        target_predicate=None,
        top_n: int = thresholds.DEFAULT_PRUNE_TOP_N,
    ) -> Tuple[ObserveResult, RecoveryTrace]:
        """Top-N에 정답이 없을 때 4단계 복구 사다리를 가동한다.

        `target_predicate(ObservedElement) -> bool`이 True인 요소가
        결과에 포함될 때까지 단계를 올린다.

        **에포크 억제**: 사다리 진행 중 스크롤이 발생해도 에포크를 올리지
        않는다. 최종 후보군 확정 시점에 단일 스냅샷으로 발행한다.
        """
        trace = RecoveryTrace(final_top_n=top_n)

        def _hit(res: ObserveResult) -> bool:
            if target_predicate is None:
                return True
            return any(target_predicate(e) for e in res.elements)

        # 0단계: 기본 관찰
        result = await self.observe_page(
            prune_top_n=top_n, page=page, cdp=cdp, goal_keywords=goal_keywords
        )
        if _hit(result):
            self._last_recovery = trace
            return result, trace

        # 1단계: N 확장 재프루닝
        expanded = expand_top_n(top_n)
        trace.stages_used.append("expand_n")
        trace.final_top_n = expanded
        result = await self.observe_page(
            prune_top_n=expanded, page=page, cdp=cdp, goal_keywords=goal_keywords
        )
        if _hit(result):
            self._last_recovery = trace
            return result, trace

        # 2단계: 시맨틱 키워드 재검색
        if goal_keywords:
            trace.stages_used.append("keyword_filter")
            sanitized = await collect(page)
            filtered = filter_by_keywords(sanitized.elements, goal_keywords)
            if filtered:
                scored = await prune_async(filtered, expanded, goal_keywords)
                result = self._build_result(sanitized, scored, None)
                if _hit(result):
                    self._last_recovery = trace
                    return result, trace

        # 3단계: Full AxTree 폴백
        trace.stages_used.append("full_tree")
        result = await self.observe_page(
            force_full_tree=True, page=page, cdp=cdp, goal_keywords=goal_keywords
        )
        if _hit(result):
            self._last_recovery = trace
            return result, trace

        # 4단계: 스크롤 후 재관찰 (지연 로딩 노드 인입)
        trace.stages_used.append("scroll_reobserve")
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(300)
        except Exception as exc:  # noqa: BLE001
            logger.debug("스크롤 재관찰 실패: %s", exc)
        result = await self.observe_page(
            prune_top_n=expanded, page=page, cdp=cdp, goal_keywords=goal_keywords
        )

        self._last_recovery = trace
        return result, trace

    # -- 관측 ---------------------------------------------------------------

    @property
    def last_latency_ms(self) -> float:
        return self._last_latency_ms

    @property
    def last_recovery(self) -> RecoveryTrace:
        return self._last_recovery
