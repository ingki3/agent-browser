"""메인 프레임 문서 응답 상태 추적 (WS-24 F2 → WS-26 공용화).

run(`interface/run_cli.py`)과 MCP 서버(`interface/mcp_server.py`)가 같은 규칙으로
"마지막 메인 문서 HTTP 상태"를 기록한다. 차단 판정(browser.challenge)이
`last_status` 로 쓴다.
"""

from __future__ import annotations

from typing import Any, Dict


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
