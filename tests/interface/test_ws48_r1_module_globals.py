"""WS-48 R1 — `interface.mcp_server` 모듈 전역 재바인딩(monkeypatch) 호환 회귀 테스트.

분리 전(base f3d18c3)에는 서버 코드가 전부 `interface.mcp_server` 한 모듈에 있어, 그 모듈의 이름
(`TOOL_PREFIX`·`_wait_timeout`·`envelope_dict`·`ActionResult`·`logger` …)을 바꿔 끼우면 모든 사용처가
호출 시점에 그 값을 봤다. 책임별 모듈(`interface.server_*`)로 옮긴 함수도 같은 효과를 내야 한다.

검증 축
1. 독립 검증자 mirror probe 5개 — base 출력 그대로.
2. 옮긴 함수가 참조하는 전역 이름 전부(아래 `PRESERVED`): mcp_server 에서 바꾸면 그 이름을 쓰는 옮긴 함수
   전부가 호출 시점에 바뀐 값을 본다. 바꾸지 않은 상태에서는 분리 모듈 값과 동일 객체(무패치 동작 불변).
3. `typing.get_type_hints` 로 모든 메서드 annotation 해석 성공(`__aenter__` → BrowserMCPServer).
"""

from __future__ import annotations

import asyncio
import builtins
import dis
import inspect
import json
import sys
import types
import typing
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Set, Tuple

import pytest

from contracts import ActionType, ErrorCode
from interface import mcp_server as ms

SPLIT_MODULES = (
    "interface.server_common",
    "interface.server_display",
    "interface.server_envelope",
    "interface.server_handoff",
    "interface.server_hitl",
    "interface.server_lifecycle",
    "interface.server_schema",
    "interface.server_signals",
    "interface.server_tools",
    "interface.server_window",
    "interface.server_workspace",  # WS-57
)

#: 옮긴 함수(믹스인 메서드 포함)가 런타임에 조회하는 모듈 전역 이름 — 전부 '보존'(보고서 R1 표).
PRESERVED = (
    "ACTION_INPUT_MAP", "ActionResult", "ActionType", "AsyncExitStack", "CHALLENGE_CHECK_ACTIONS",
    "CHALLENGE_NOTE", "CHALLENGE_NOTE_HOME", "CHALLENGE_REF", "ENVELOPE_ALWAYS", "ErrorCode", "Path",
    "RECIPE_TOOL", "SERVER_TOOLS", "SITE_NAME_LABEL", "TOOL_PREFIX", "UNOBSERVED_HINT", "WAIT_DEFAULT_S",
    "WAIT_MAX_S", "YOUTUBE_TOOL", "_BUTTON_INPUT_TYPES", "_DESCRIPTIONS", "_ENTER_SUBMITS_INPUT_TYPES", "_PageObservation",
    "_RELEASE_HINT", "_SCHEMA_VALUE_KEYS", "_TRUNC_HINT_EXTRACT", "_TRUNC_HINT_OBSERVE", "_TRUNC_HINT_TEXT",
    "_UNCERTAIN_CODES", "_compact_observation", "_describe", "_field_default", "_iso", "_safe_cut",
    "_signals_of", "_stderr_line", "_wait_timeout", "action_from_tool", "approve_hint_text", "asyncio",
    "blocked_hint_text", "build_all_tools", "build_server_tools", "build_tool_schema", "cap_result_size",
    "code_overlay_text", "compact_schema", "envelope_dict", "envelope_json", "logger", "os",
    "pre_approve_hint", "time", "tool_name", "unicodedata",
    # WS-63 C 사람 인계 안내
    "HANDOFF_HINT", "HANDOFF_STEPS_AVAILABLE", "HANDOFF_STEPS_UNAVAILABLE", "HANDOFF_TOOL", "_HANDOFF_EXCLUDED_STATUSES",
    "challenge_handoff", "robots_disallowed",  # R1: robots 금지 시 인계 안내 제외
    # WS-57 작업 공간
    "DEFAULT_WORKSPACE", "WORKSPACE_ARG", "WorkspaceNameError", "WorkspaceRuntime", "_CALL_STATE", "_CallScope",
    "_all_tabs", "_closed", "_snapshot_tabs", "current_workspace", "pop_workspace_arg", "validate_workspace",
    "workspace_scope",
    # WS-57 R2 작업 공간 직렬화
    "_SERIAL_HELD", "_WorkspaceSerial",
    # WS-56 R4 사람 응답 대기 동안 직렬화 구간 잠시 놓기
    "_SERIAL_STACK", "_SerialPause",
    # WS-67 격리 world 판정(포커스·좌표 판정 분리 + 높은 위험 고르기)
    "_focus_verdict", "_gate_rank", "_isolated", "_point_verdict",
    # WS-68 잘림 안내 숫자(설정 상한·실효 예산, 최종 봉투 returned_chars)
    "_limit_text", "settle_returned_chars",
)


def _code_globals(code: types.CodeType) -> Set[str]:
    out = {i.argval for i in dis.get_instructions(code) if i.opname == "LOAD_GLOBAL"}
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            out |= _code_globals(const)
    return out


def _unwrap(attr: Any) -> List[Any]:
    """클래스 속성에서 소스에 정의된 함수들을 꺼낸다(staticmethod·property·asynccontextmanager)."""
    if isinstance(attr, (staticmethod, classmethod)):
        attr = attr.__func__
    if isinstance(attr, property):
        return [f for f in (attr.fget, attr.fset, attr.fdel) if f is not None]
    if inspect.isfunction(attr):
        return [attr.__wrapped__] if hasattr(attr, "__wrapped__") else [attr]
    return []


def _moved_functions() -> Iterator[Tuple[str, Any]]:
    """분리 모듈 소스에 정의된 모든 함수(모듈 함수 + 그 모듈 클래스의 메서드)."""
    for name in SPLIT_MODULES:
        mod = sys.modules[name]
        for key, value in vars(mod).items():
            if inspect.isfunction(value) and value.__code__.co_filename == mod.__file__:
                yield f"{name}:{key}", value
            elif inspect.isclass(value) and value.__module__ == name:
                for attr_name, attr in vars(value).items():
                    for fn in _unwrap(attr):
                        if fn.__code__.co_filename == mod.__file__:
                            yield f"{name}:{key}.{attr_name}", fn


MOVED = dict(_moved_functions())


def _users(name: str) -> List[Any]:
    return [fn for fn in MOVED.values() if name in _code_globals(fn.__code__)]


# --------------------------------------------------------------------------- 인벤토리


def test_inventory_is_not_vacuous_and_fully_listed():
    assert len(MOVED) >= 100, len(MOVED)
    referenced = set().union(*(_code_globals(f.__code__) for f in MOVED.values()))
    module_names = {n for n in referenced if not hasattr(builtins, n)}
    # 새 전역 참조가 생기면 PRESERVED(와 보고서 표)에 의식적으로 추가해야 한다.
    assert module_names == set(PRESERVED)


@pytest.mark.parametrize("qualname", sorted(MOVED))
def test_moved_function_resolves_globals_in_mcp_server(qualname):
    assert MOVED[qualname].__globals__ is vars(ms)


@pytest.mark.parametrize("name", PRESERVED)
def test_unpatched_value_is_the_split_module_object(name):
    owners = [sys.modules[m] for m in SPLIT_MODULES if name in vars(sys.modules[m])]
    assert owners, name
    for mod in owners:
        assert vars(mod)[name] is getattr(ms, name), (mod.__name__, name)


@pytest.mark.parametrize("name", PRESERVED)
def test_patching_mcp_server_reaches_every_moved_user(name, monkeypatch):
    sentinel = object()
    users = _users(name)
    assert users, name
    monkeypatch.setattr(ms, name, sentinel)
    for fn in users:
        assert fn.__globals__[name] is sentinel, fn.__qualname__


def test_split_module_and_class_attributes_are_the_rebound_objects():
    """분리 모듈 속성·mcp_server 재수출·믹스인 클래스 속성이 모두 같은(재바인딩된) 함수 객체."""
    for name in SPLIT_MODULES:
        mod = sys.modules[name]
        for key, value in vars(mod).items():
            if inspect.isfunction(value) and value.__module__.startswith("interface.server_"):
                assert value.__globals__ is vars(ms), (name, key)
                if key in vars(ms):
                    assert getattr(ms, key) is value, (name, key)


# --------------------------------------------------------------------------- 검증자 mirror probe 5개


def _bare_server() -> Any:
    s = ms.BrowserMCPServer.__new__(ms.BrowserMCPServer)
    s._page = s._engine = s._core = None
    return s


def test_mirror_tool_prefix(monkeypatch):
    monkeypatch.setattr(ms, "TOOL_PREFIX", "patched_")
    assert ms.tool_name(ActionType.CLICK) == "patched_click"


def test_mirror_wait_max(monkeypatch):
    monkeypatch.setattr(ms, "WAIT_MAX_S", 0.125)
    assert ms._wait_timeout(999) == 0.125


def test_mirror_envelope_dict(monkeypatch):
    error = _bare_server()._error_result(ActionType.EXTRACT, ErrorCode.TIMEOUT, "synthetic")
    monkeypatch.setattr(ms, "envelope_dict", lambda _: {"patched": True})
    assert json.loads(ms.envelope_json(error)) == {"patched": True}


def test_mirror_action_result(monkeypatch):
    monkeypatch.setattr(ms, "ActionResult", lambda **kw: SimpleNamespace(patched=True))
    patched = _bare_server()._error_result(ActionType.CLICK, ErrorCode.TIMEOUT, "synthetic")
    assert getattr(patched, "patched", False) is True


def test_mirror_wait_timeout_through_server_tool(monkeypatch):
    calls: List[Any] = []
    monkeypatch.setattr(ms, "_wait_timeout", lambda v: (calls.append(v) or 17))
    s = _bare_server()
    s.hub = SimpleNamespace(opened=True)

    async def poll() -> list:
        return []

    async def wait(t: float) -> Dict[str, Any]:
        return {"timeout": t}

    s._poll_handoff = poll
    s._control_wait = wait
    result = asyncio.run(s.call_server_tool("browser_control_wait", {"timeout_s": 2}))
    assert calls == [2]
    assert result == {"timeout": 17}


# --------------------------------------------------------------------------- 대조군(원래도 되던 경로)


def test_class_override_still_wins(monkeypatch):
    async def fake_close(self):  # noqa: ANN001
        return "patched_close"

    monkeypatch.setattr(ms.BrowserMCPServer, "close", fake_close)
    assert asyncio.run(_bare_server().close()) == "patched_close"


def test_in_place_mutation_is_shared(monkeypatch):
    monkeypatch.setitem(ms.SERVER_TOOLS, "browser_ws48_probe", {"name": "browser_ws48_probe"})
    assert "browser_ws48_probe" in sys.modules["interface.server_schema"].SERVER_TOOLS


# --------------------------------------------------------------------------- annotation 해석


def _all_methods() -> Dict[str, Any]:
    methods: Dict[str, Any] = {}
    for cls in ms.BrowserMCPServer.__mro__[:-1]:
        for name, attr in vars(cls).items():
            for fn in _unwrap(attr):
                methods.setdefault(name, fn)
    return methods


def test_aenter_annotation_resolves_to_concrete_server():
    hints = typing.get_type_hints(ms.BrowserMCPServer.__aenter__)
    assert hints["return"] is ms.BrowserMCPServer


@pytest.mark.parametrize("name", sorted(_all_methods()))
def test_every_server_method_annotation_resolves(name):
    typing.get_type_hints(_all_methods()[name])
