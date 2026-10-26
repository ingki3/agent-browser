"""주 프레임 새 문서 커밋 계수기(WS-68 R4, #140) — 기억해 저장 트랜잭션이 '그동안 문서가 바뀌었는가'를 본다.

결속값(Page.getFrameTree·getNavigationHistory) 표본은 마지막 읽기 뒤의 창을 닫지 못한다(R3 검증: 마지막 frame 응답
직후 reload). 그래서 트랜잭션 시작 전에 이벤트를 걸어 커밋을 놓치지 않고 센다.

Playwright 공개 page 'framenavigated' 는 같은 문서 안 이동(pushState·hash)에도 발화해 문서 교체와 구분할 수 없다.
주 프레임 자신의 내부 'navigated' 이벤트에서 newDocument 가 있고 error 가 없는 것(=새 문서 커밋)만 센다(WS-69 R2 와
같은 기법, 독립 구현). 내부 이벤트를 걸 수 없으면 None — 부르는 쪽이 판정 불가로 거부한다(fail-closed).
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class DocumentCommitWatch:
    """걸려 있는 동안 주 프레임 새 문서 커밋 수(commits). close() 로 해제(여러 번 불러도 무해)."""

    def __init__(self, unwatch: Callable[[], None]) -> None:
        self.commits = 0
        self._unwatch: Optional[Callable[[], None]] = unwatch

    def on_navigated(self, event: Any) -> None:
        if isinstance(event, dict) and "newDocument" in event and "error" not in event:
            self.commits += 1

    def close(self) -> None:
        unwatch, self._unwatch = self._unwatch, None
        if unwatch is None:
            return
        try:
            unwatch()
        except Exception:  # noqa: BLE001 — 페이지가 이미 닫힘 등: 해제할 것이 없다
            pass


def watch_document_commits(page: Any) -> Optional[DocumentCommitWatch]:
    """page(최상위 Page) 주 프레임의 새 문서 커밋 계수기를 건다. 내부 이벤트가 없거나 걸 수 없으면 None."""
    frame = getattr(page, "main_frame", None)
    emitter = getattr(getattr(frame, "_impl_obj", None), "_event_emitter", None)
    if frame is None or emitter is None or not callable(getattr(emitter, "on", None)) \
            or not callable(getattr(emitter, "remove_listener", None)):
        return None
    holder: dict = {}

    def handler(event: Any) -> None:
        holder["watch"].on_navigated(event)

    watch = DocumentCommitWatch(lambda: emitter.remove_listener("navigated", handler))
    holder["watch"] = watch
    try:
        emitter.on("navigated", handler)
    except Exception:  # noqa: BLE001 — 연결 실패: 판정 불가
        return None
    return watch
