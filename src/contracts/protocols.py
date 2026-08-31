"""모듈 간 상호 호출 규약 (Stage 0 동결).

PRD.md §7.2에 정의된 `Protocol` 클래스를 구현한다.
각 워크스트림은 타 모듈의 구체 클래스가 아니라 본 프로토콜에 의존해야 하며,
그래야 병렬 개발 중 함수 시그니처 충돌이 발생하지 않는다.

`Protocol`은 런타임 상속이 아닌 구조적 서브타이핑이므로, 구현체는 본 모듈을
import하지 않고도 시그니처만 맞추면 된다.
"""

from typing import Any, Dict, Optional, Protocol, runtime_checkable

from playwright.async_api import BrowserContext, Page

from contracts.models import ActionResult, ActionType, ObserveResult
from contracts.thresholds import DEFAULT_PRUNE_TOP_N


@runtime_checkable
class PerceptionEngineProtocol(Protocol):
    """WS-2 `perception/`이 제공하는 관찰 인터페이스."""

    async def observe_page(
        self,
        tab_id: Optional[str] = None,
        prune_top_n: int = DEFAULT_PRUNE_TOP_N,
        force_full_tree: bool = False,
    ) -> ObserveResult:
        """현재 페이지를 관찰해 프루닝된 후보 요소 목록을 반환한다."""
        ...


@runtime_checkable
class BrowserCoreProtocol(Protocol):
    """WS-1 `browser/`가 제공하는 브라우저 수명주기 인터페이스."""

    async def new_context(self, profile_name: str) -> BrowserContext:
        """프로파일 기반 격리 `BrowserContext`를 생성한다."""
        ...

    async def get_active_page(self, tab_id: Optional[str] = None) -> Page:
        """활성 탭(또는 지정 탭)의 `Page` 핸들을 반환한다."""
        ...


@runtime_checkable
class ActionDispatcherProtocol(Protocol):
    """WS-3 `actions/`가 제공하는 액션 실행 인터페이스."""

    async def dispatch(
        self,
        action: ActionType,
        params: Dict[str, Any],
        epoch: int,
    ) -> ActionResult:
        """액션을 실행한다.

        `params`는 `ACTION_INPUT_MAP[action]`으로 검증한 뒤 사용해야 하며,
        구현체는 원시 dict를 그대로 신뢰하지 않는다.
        """
        ...
