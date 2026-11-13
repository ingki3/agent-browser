"""MCP 서버 공용 보조 함수(시각 표기·대기 상한·stderr 한 줄).

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Sequence

from interface.handoff import WAIT_DEFAULT_S, WAIT_MAX_S


def _wait_timeout(raw: Any) -> float:
    try:
        value = float(raw) if raw is not None else WAIT_DEFAULT_S
    except (TypeError, ValueError):
        value = WAIT_DEFAULT_S
    return max(0.0, min(WAIT_MAX_S, value))


#: WS-95b(#278): 선언 밖 인자 거부 문구에 이름을 그대로 보여 줄 키 모양 — 그 밖(주입 문구·긴 이름·대문자·비ASCII)은 개수만.
_ECHO_KEY = re.compile(r"[a-z_]{1,32}")
_ECHO_MAX = 5


def undeclared_args_message(extra: Iterable[Any], allowed: Sequence[str]) -> str:
    """선언되지 않은 인자 거부의 고정 문장(액션 도구·서버 도구·browser_batch 공용, WS-95b)."""
    keys = list(extra)
    shown = sorted(k for k in keys if isinstance(k, str) and _ECHO_KEY.fullmatch(k))
    hidden = len(keys) - len(shown)
    from recipes.store import clean_text

    # R1(검증 NB2): 반향 이름은 최대 _ECHO_MAX 개 + '외 N개' — 600자 절단이 뒤의 허용 키 목록을 자르지 않게.
    listed = ", ".join(shown[:_ECHO_MAX]) + (f" 외 {len(shown) - _ECHO_MAX}개" if len(shown) > _ECHO_MAX else "")
    text = f"선언되지 않은 인자 {len(keys)}개: [{listed}]"
    if hidden:
        text += f" ({hidden}개 이름 생략)"
    text += f" — 허용: [{', '.join(allowed)}]. 실행하지 않았습니다 — 선언된 인자만으로 다시 호출하세요."
    return clean_text(text, 600)


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
