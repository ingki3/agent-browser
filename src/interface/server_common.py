"""MCP 서버 공용 보조 함수(시각 표기·대기 상한·stderr 한 줄).

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import os
from typing import Any

from interface.handoff import WAIT_DEFAULT_S, WAIT_MAX_S


def _wait_timeout(raw: Any) -> float:
    try:
        value = float(raw) if raw is not None else WAIT_DEFAULT_S
    except (TypeError, ValueError):
        value = WAIT_DEFAULT_S
    return max(0.0, min(WAIT_MAX_S, value))


def _iso(ts: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds")


def _stderr_line(text: str) -> None:
    """stderr 에 한 줄(신호 처리기 안에서도 안전하게 os.write). stdout 은 MCP 전용.

    한 줄 보장: 외부 유래 문자열(에이전트 reason·URL·예외 문구)이 섞여도 제어문자·개행·양방향
    제어를 보이는 표기로 바꾼다(WS-29 R1 BLOCKING-1 — 운영자 터미널 위조 방지). 주소의 비밀 값은 가린다(WS-51).
    """
    from interface.handoff import display_safe
    from security.public_url import public_text

    try:
        # WS-51: 줄 안의 페이지 주소는 비밀 값을 가린다(에이전트 응답과 같은 public_url).
        os.write(2, (display_safe(public_text(text), 4000) + "\n").encode("utf-8", "replace"))
    except OSError:
        pass
