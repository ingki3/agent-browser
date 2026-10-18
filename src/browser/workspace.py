"""작업 공간(WS-57) — 같은 브라우저(영속 컨텍스트·로그인)를 여러 호출자가 서로 건드리지 않고 쓰기.

봇의 대화와 정기 작업(cron)이 같은 serve 서버를 동시에 쓰면 활성 탭·snapshot_epoch·탭 닫기가 서로에게
샜다(실사용 2026-10-14: 감시 작업이 대화가 사람에게 열어 준 창의 마지막 탭을 닫음). 작업 공간 하나는
자기 탭 묶음·자기 활성 탭·자기 관찰 epoch 를 갖는다. 쿠키·로그인은 같은 컨텍스트라 그대로 공유한다.

현재 작업 공간은 ContextVar 로 흐른다 — MCP 요청마다 태스크가 따로라 동시 호출이 섞이지 않는다.
기본 이름 `default` = 작업 공간을 쓰지 않던 기존 동작 그대로.
"""

from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar
from typing import Any, Iterator

#: 작업 공간을 지정하지 않은 호출(기존 단일 세션 동작).
DEFAULT_WORKSPACE = "default"
#: 주인이 아직 정해지지 않은 탭(코어 내부 표시 — 이름 규칙에 맞지 않아 어떤 호출의 작업 공간과도 같지 않다).
PENDING_WORKSPACE = "<pending>"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

_CURRENT: ContextVar[str] = ContextVar("agent_browser_workspace", default=DEFAULT_WORKSPACE)


class WorkspaceNameError(ValueError):
    """작업 공간 이름이 규칙(영문 소문자·숫자·하이픈 1~32자, 첫 글자 영숫자)에 맞지 않음."""


def validate_workspace(name: Any) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise WorkspaceNameError(
            "작업 공간 이름은 영문 소문자·숫자·하이픈 1~32자(첫 글자 영숫자)만 됩니다.")
    return name


def current_workspace() -> str:
    return _CURRENT.get()


@contextlib.contextmanager
def workspace_scope(name: str) -> Iterator[str]:
    """이 블록(과 그 안에서 만든 태스크)의 현재 작업 공간을 name 으로."""
    token = _CURRENT.set(validate_workspace(name))
    try:
        yield name
    finally:
        _CURRENT.reset(token)
