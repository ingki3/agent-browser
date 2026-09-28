"""메인 프레임 문서 응답 상태 추적 (WS-24 F2 → WS-26 공용화).

run(`interface/run_cli.py`)과 MCP 서버(`interface/mcp_server.py`)가 같은 규칙으로
"마지막 메인 문서 HTTP 상태"를 기록한다. 차단 판정(browser.challenge)이
`last_status` 로 쓴다.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def track_main_document_status(target: Any, record: Dict[str, Any]) -> Any:
    """target(context 우선) 의 메인 프레임 문서 응답 상태를 record["last_http_status"] 에.

    WS-24 F2: 첫 이동(http_status)만 남기면 검색 페이지에서 403 으로 막혀도 200 이었다.
    context 단위로 달아 새 탭(탭 전환·팝업)의 문서 응답도 잡는다 — 여러 탭이 동시에
    움직이면 "마지막으로 온 메인 문서 응답"이다(어느 탭인지는 구분하지 않는다).
    반환값은 해제용 리스너(달지 못했으면 None). 루프의 _last_status 는 강제 계속 때
    None 으로 초기화되므로 쓰지 않는다.
    """
    on = getattr(target, "on", None)
    if not callable(on):
        return None

    def _on_response(response: Any) -> None:
        try:
            if is_main_document_response(response):
                record["last_http_status"] = response.status
        except Exception:  # noqa: BLE001 - 상태 기록 실패로 실행을 막지 않는다
            pass

    on("response", _on_response)
    return _on_response


def _strip_fragment(url: str) -> str:
    return (url or "").split("#", 1)[0]


class PageDocumentStatus:
    """탭(페이지)별 마지막 메인 문서 응답 상태 (WS-26b, 검증 NB-6).

    `track_main_document_status` 는 세션 전역 "마지막 메인 문서 응답" 하나라, 팝업이
    403 을 받은 뒤 원래 탭을 보면 원래 탭이 차단으로 오판됐다. 여기서는 응답을 그
    응답이 온 페이지에 묶어 둔다. run 경로의 전역 기록(record["last_http_status"])은
    건드리지 않는다 — 이 클래스는 MCP 판정 전용의 별도 기록이다.

    새 탭 첫 문서 응답은 frame 을 아직 얻을 수 없다(is_main_document_response 참고).
    그런 응답은 보류해 두었다가 조회할 때 response.frame.page 로 다시 묶는다(실측:
    context 'page' 이벤트 뒤에는 같은 응답 객체에서 frame 을 얻는다).
    """

    #: 페이지를 끝내 못 찾는 보류 응답이 쌓이지 않도록 둔 상한.
    MAX_PENDING = 32

    def __init__(self) -> None:
        self._seq = 0
        #: page -> (seq, url, status)
        self._by_page: Dict[Any, Tuple[int, str, int]] = {}
        self._pending: List[Tuple[int, Any]] = []

    # -- 기록 ---------------------------------------------------------------

    def attach(self, context: Any) -> None:
        """context 의 응답을 듣는다. 닫힌 페이지 기록은 지운다."""
        on = getattr(context, "on", None)
        if not callable(on):
            return
        on("response", self.on_response)

        def _on_page(page: Any) -> None:
            try:
                page.on("close", self.forget)
            except Exception:  # noqa: BLE001
                pass

        on("page", _on_page)

    def on_response(self, response: Any) -> None:
        try:
            if not is_main_document_response(response):
                return
            self._seq += 1
            try:
                page = response.frame.page
            except Exception:  # noqa: BLE001 - 새 탭 첫 요청: 프레임 미생성 → 보류
                self._pending.append((self._seq, response))
                del self._pending[: -self.MAX_PENDING]
                return
            self._store(page, self._seq, response.url, response.status)
        except Exception:  # noqa: BLE001 - 상태 기록 실패로 실행을 막지 않는다
            pass

    def record(self, page: Any, url: str, status: int) -> None:
        """직접 기록(테스트·주입용)."""
        self._seq += 1
        self._store(page, self._seq, url, status)

    def forget(self, page: Any) -> None:
        self._by_page.pop(page, None)

    def _store(self, page: Any, seq: int, url: str, status: int) -> None:
        current = self._by_page.get(page)
        if current is None or current[0] < seq:
            self._by_page[page] = (seq, url, status)

    def _resolve_pending(self) -> None:
        if not self._pending:
            return
        still: List[Tuple[int, Any]] = []
        for seq, response in self._pending:
            try:
                page = response.frame.page
            except Exception:  # noqa: BLE001
                still.append((seq, response))
                continue
            try:
                self._store(page, seq, response.url, response.status)
            except Exception:  # noqa: BLE001
                pass
        self._pending = still

    # -- 조회 ---------------------------------------------------------------

    def status_for(self, page: Any) -> Optional[int]:
        """page 가 마지막으로 받은 메인 문서 응답 상태(없으면 None)."""
        self._resolve_pending()
        entry = self._by_page.get(page)
        return entry[2] if entry else None

    def judge_status_for(self, page: Any) -> Optional[int]:
        """차단 판정에 넘길 상태 — 지금 URL 이 그 응답 URL 과 같을 때만.

        403 문서 뒤 history.pushState 로 URL 이 바뀐 SPA 화면은 그 403 문서의 화면이
        아니다. fragment(#…)만 다르면 같은 문서로 본다. URL 이 같은 채 본문만 JS 로
        바뀌는 경우는 구분하지 못한다(한계).
        """
        self._resolve_pending()
        entry = self._by_page.get(page)
        if entry is None:
            return None
        try:
            current = page.url
        except Exception:  # noqa: BLE001
            return None
        if _strip_fragment(current) != _strip_fragment(entry[1]):
            return None
        return entry[2]


def is_main_document_response(response: Any) -> bool:
    """메인 프레임(탭 최상위) 문서 응답인가.

    WS-24 R1-1 실측: target=_blank·window.open 으로 연 새 탭의 **첫** 문서 응답은
    프레임이 생기기 전에 요청돼 response.frame 이 "Frame for this navigation request is
    not available" 예외를 낸다(context 'page' 이벤트도 그 응답 뒤에 온다). 그 경우만
    문서 요청이면 새 탭 최상위 문서로 본다 — iframe 문서 요청은 부모 문서 안에서 프레임이
    먼저 붙은 뒤 나가므로 frame 을 얻고, parent_frame 검사로 계속 걸러진다(실측).
    """
    request = response.request
    if not request.is_navigation_request():
        return False
    try:
        frame = response.frame
    except Exception:  # noqa: BLE001 - 새 탭 첫 요청: 프레임 미생성
        return request.resource_type == "document"
    return frame.parent_frame is None
