"""WS-64 R2 B1 — 다단계 입력의 **각 단계** 직전 재판정(액션 × 단계 전수, 브라우저 없음).

가짜 페이지의 모든 await 지점(민감 칸 판정·clear·포커스·글자·Enter·이동·누름·뗌 …)마다 '이 await 동안 목록 사이트로
이동했다'를 한 번씩 만든다. 그 뒤로 페이지를 바꾸는 호출이 하나라도 나가면 실패. 실제 Chromium 재현은
tests/interface/test_ws64_r2_server.py(clear·글자 사이 이동).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from actions import dispatcher as dmod
from actions.dispatcher import ActionDispatcher, DispatchContext
from contracts import ActionType
from security.manual_sites import ManualSites

UNLISTED = "https://plain.example/form"
LISTED = "https://srail.kr/form"

#: 페이지를 바꾸는(입력·포커스 이벤트를 내는) 호출. evaluate(민감 칸 판정)는 읽기.
SIDE_EFFECTS = {"fill", "type", "focus", "key", "press", "click", "select_option", "check", "uncheck", "hover",
                "set_input_files", "mouse.move", "mouse.down", "mouse.up", "mouse.click"}


class _Recorder:
    def __init__(self) -> None:
        self.ops: List[Tuple[str, str]] = []  # (op, url at call time)
        self.flip_at: Optional[int] = None
        self.page: Any = None

    async def hit(self, op: str) -> None:
        self.ops.append((op, self.page.url))
        if self.flip_at is not None and len(self.ops) - 1 == self.flip_at:
            self.page.url = LISTED  # 이 await 동안 목록 사이트로 이동이 끝났다


class _Locator:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec
        self.first = self

    async def evaluate(self, *_a: Any, **_k: Any) -> Any:
        await self._rec.hit("evaluate")
        return False

    def __getattr__(self, name: str) -> Any:
        if name not in {"fill", "type", "focus", "press", "click", "select_option", "check", "uncheck", "hover",
                        "set_input_files"}:
            raise AttributeError(name)

        async def call(*_a: Any, **_k: Any) -> None:
            await self._rec.hit(name)
        return call


class _Device:
    def __init__(self, rec: _Recorder, prefix: str, names: Tuple[str, ...]) -> None:
        for n in names:
            async def call(*_a: Any, _n: str = n, **_k: Any) -> None:
                await rec.hit(_n if prefix == "" else f"{prefix}.{_n}")
            setattr(self, n, call)


class _Page:
    def __init__(self, rec: _Recorder) -> None:
        self.url = UNLISTED
        self._rec = rec
        self.keyboard = SimpleNamespace(type=self._key)
        self.mouse = _Device(rec, "mouse", ("move", "down", "up", "click"))
        self.viewport_size = {"width": 800, "height": 600}
        rec.page = self

    async def _key(self, *_a: Any, **_k: Any) -> None:
        await self._rec.hit("key")

    def locator(self, _selector: str) -> _Locator:
        return _Locator(self._rec)


def _dispatcher(rec: _Recorder) -> ActionDispatcher:
    page = _Page(rec)
    engine: Any = SimpleNamespace(epoch=0)
    ctx = DispatchContext(page=page, engine=engine, manual_sites=ManualSites.of(["srail.kr"]),
                          nav_settle=False)
    return ActionDispatcher(ctx)


HANDLE: Any = SimpleNamespace(css_path="#q", is_shadow=False, name="q", role="textbox")

ELEMENT_CASES: List[Tuple[ActionType, Dict[str, Any]]] = [
    (ActionType.TYPE_TEXT, {"text": "abc", "clear_before": True, "press_enter": True}),
    (ActionType.TYPE_TEXT, {"text": "ab", "clear_before": False}),
    (ActionType.CLICK, {}),
    (ActionType.SELECT_OPTION, {"value": "b"}),
    (ActionType.CHECK_BOX, {"checked": True}),
    (ActionType.CHECK_BOX, {"checked": False}),
    (ActionType.HOVER, {}),
    (ActionType.UPLOAD_FILE, {"file_paths": ["/dev/null"]}),
]


async def _run_element(action: ActionType, params: Dict[str, Any], flip_at: Optional[int]) -> Tuple[_Recorder, bool]:
    rec = _Recorder()
    rec.flip_at = flip_at
    disp = _dispatcher(rec)
    refused = False
    try:
        await disp._execute_element_action(action, HANDLE, dict(params))
    except dmod._ManualSiteRefused:
        refused = True
    return rec, refused


def _assert_no_effect_after_flip(rec: _Recorder, flip_at: int) -> None:
    late = [op for op, url in rec.ops[flip_at + 1:] if op in SIDE_EFFECTS]
    assert not late, (flip_at, rec.ops)
    assert all(url == UNLISTED for op, url in rec.ops if op in SIDE_EFFECTS), rec.ops


@pytest.mark.parametrize("action,params", ELEMENT_CASES, ids=lambda v: getattr(v, "value", None) or str(v))
async def test_every_await_point_rechecked_before_next_side_effect(action, params):
    base, refused = await _run_element(action, params, None)
    assert not refused and any(op in SIDE_EFFECTS for op, _ in base.ops)
    if action is ActionType.TYPE_TEXT:
        keys = [op for op, _ in base.ops if op == "key"]
        assert len(keys) == len(params["text"])  # 목록이 있으면 글자 단계로 나뉜다(조건 성립)
    for i in range(len(base.ops)):
        rec, refused = await _run_element(action, params, i)
        _assert_no_effect_after_flip(rec, i)
        later = [op for op, _ in base.ops[i + 1:] if op in SIDE_EFFECTS]
        assert refused == bool(later), (i, rec.ops)


async def test_listed_from_start_sends_nothing():
    for action, params in ELEMENT_CASES:
        rec = _Recorder()
        disp = _dispatcher(rec)
        rec.page.url = LISTED
        with pytest.raises(dmod._ManualSiteRefused):
            await disp._execute_element_action(action, HANDLE, dict(params))
        assert not [op for op, _ in rec.ops if op in SIDE_EFFECTS], (action, rec.ops)


async def test_empty_list_keeps_single_type_call():
    """과차단·회귀 대조: 빈 목록이면 기존 경로(한 번의 type 호출) 그대로."""
    rec = _Recorder()
    disp = _dispatcher(rec)
    disp.ctx.manual_sites = ManualSites()
    await disp._execute_element_action(ActionType.TYPE_TEXT, HANDLE, {"text": "abc"})
    assert [op for op, _ in rec.ops if op in SIDE_EFFECTS] == ["fill", "type"]


async def _run_coordinates(flip_at: Optional[int], monkeypatch: Any) -> Tuple[_Recorder, Any]:
    rec = _Recorder()
    rec.flip_at = flip_at
    disp = _dispatcher(rec)

    async def state(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(url=rec.page.url, page_count=1)

    monkeypatch.setattr(disp, "_capture_state", state)
    try:
        result = await disp._click_coordinates(ActionType.CLICK, {"x": 10, "y": 10, "epoch": 0})
    except Exception as exc:  # noqa: BLE001 — 사후조건 계산은 가짜 페이지에서 깨질 수 있다(입력 기록만 본다)
        result = exc
    return rec, result


async def test_coordinate_click_each_step_rechecked(monkeypatch):
    base, _ = await _run_coordinates(None, monkeypatch)
    assert [op for op, _ in base.ops if op in SIDE_EFFECTS] == ["mouse.move", "mouse.down", "mouse.up"]
    for i in range(len(base.ops)):
        rec, result = await _run_coordinates(i, monkeypatch)
        _assert_no_effect_after_flip(rec, i)
        if [op for op, _ in base.ops[i + 1:] if op in SIDE_EFFECTS]:
            assert ((getattr(result, "data", None) or {}).get("handoff") or {}).get("reason") == "manual_site"


async def test_pinned_press_rechecks_before_down_and_up():
    rec = _Recorder()
    page = _Page(rec)
    calls: List[str] = []

    class _Node:
        async def evaluate_handle(self, _js: str) -> Any:
            await rec.hit("evaluate")
            page.url = LISTED  # 감시 설치 await 동안 이동

            class _G:
                async def evaluate(self, _j: str) -> Any:
                    return None

                async def dispose(self) -> None:
                    return None
            return _G()

    async def before() -> None:
        calls.append(page.url)
        if page.url == LISTED:
            raise dmod._ManualSiteRefused(SimpleNamespace())  # type: ignore[arg-type]

    with pytest.raises(dmod._ManualSiteRefused):
        await ActionDispatcher._pinned_press(page, SimpleNamespace(node=_Node()), "left", before=before)
    assert calls == [LISTED] and not [op for op, _ in rec.ops if op in SIDE_EFFECTS]
