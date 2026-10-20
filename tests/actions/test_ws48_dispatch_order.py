"""WS-48 NB-1 — dispatcher 요소 액션의 순서 고정(동작 고정 테스트, base f3d18c3 에서도 통과).

독립 검증자 `download_order_probe.py`·`test_independent_coverage.py` 를 편입했다. 기존 actions 테스트와 골든은
'다운로드 marker 처리 앞에 일반 사후조건을 실행'하는 교환 사보타주를 잡지 못했다(512 passed).

- 다운로드: 저장 경로 marker 가 있으면 일반 사후조건(verify_post_condition)을 부르지 않고 file_saved 로 성공.
- 일반 입력: 사후조건을 부른다.
- 저장 뒤 after-capture 실패·조기 navigation 의 현재 동작(예외 전파·marker 잔류)도 그대로 고정한다 — 기존
  정책 그대로이며 개선은 별도 라운드(보고서 '후속').
- 진단 로그가 실패해도 `_capture_root` 의 fallback(None)이 유지된다.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from actions import dispatcher as ad
from actions.verification import PageStateSnapshot, PostConditionResult
from contracts import ActionResult, ActionType
from interface.mcp_server import envelope_dict


async def _scenario(action: ActionType, fail_capture: bool = False, nav: bool = False) -> Dict[str, Any]:
    trace: List[str] = []

    class Page:
        url = "http://mock.local/"

        def on(self, event: str, fn: Any) -> None:
            trace.append("listen:" + event)

        def remove_listener(self, event: str, fn: Any) -> None:
            trace.append("unlisten:" + event)

        async def wait_for_timeout(self, ms: int) -> None:
            trace.append("wait")

    page = Page()
    d = ad.ActionDispatcher.__new__(ad.ActionDispatcher)
    d.ctx = SimpleNamespace(page=page, root_page=None, nav_settle=False)
    d._nav_info = {}
    d._opened_pages = []
    d._dialogs = []
    d._top_page = lambda: page
    handle = SimpleNamespace(element_id="@independent", css_path="#independent")
    calls = 0

    async def capture(page: Any, handle: Any = None) -> PageStateSnapshot:
        nonlocal calls
        calls += 1
        trace.append("capture-before" if calls == 1 else "capture-after")
        if calls == 2 and fail_capture:
            raise RuntimeError("synthetic-after-capture")
        return PageStateSnapshot(url="http://mock.local/", dom_node_count=1, text_signature=1)

    async def root(**kwargs: Any) -> None:
        trace.append("root")
        return None

    async def execute(a: ActionType, h: Any, p: Dict[str, Any]) -> None:
        trace.append("send")
        if action is ActionType.DOWNLOAD_FILE:
            p["_downloaded_path"] = "/synthetic/finished.csv"
        if nav:
            d._nav_info = {"nav_committed": True}

    def post(*args: Any, **kwargs: Any) -> PostConditionResult:
        trace.append("postcondition")
        return PostConditionResult(satisfied=True, signals=["synthetic"])

    def result(**kwargs: Any) -> ActionResult:
        kwargs.setdefault("healed", False)
        kwargs.setdefault("reobserve_required", False)
        return ActionResult(current_url=page.url, snapshot_epoch=1, tab_id="fixed", **kwargs)

    def outside(a: Any, p: Any, post: Any, *rest: Any) -> Any:
        return post

    d._capture_root = root
    d._execute_element_action = execute
    d._result = result
    d._with_outside_effects = outside
    params: Dict[str, Any] = {}
    with patch.object(ad, "capture_state", capture), patch.object(ad, "verify_post_condition", post):
        try:
            payload: Dict[str, Any] = envelope_dict(await d._run_element_action(action, handle, params, False))
        except Exception as exc:  # noqa: BLE001 - 전파되는 예외 종류도 고정 대상
            payload = {"exception": type(exc).__name__, "message": str(exc)}
    return {"trace": trace, "result": payload, "marker_left": params.get("_downloaded_path")}


_ENVELOPE = {"current_url": "http://mock.local/", "retry_safe": False, "snapshot_epoch": 1, "success": True,
             "tab_id": "fixed"}


async def test_download_bypasses_generic_postcondition():
    out = await _scenario(ActionType.DOWNLOAD_FILE)
    assert out["trace"] == ["capture-before", "root", "listen:popup", "send", "capture-after", "unlisten:popup"]
    assert "postcondition" not in out["trace"], "download must bypass postcondition"
    assert out["result"] == dict(_ENVELOPE, action="download_file", reobserve_required=False,
                                 downloaded_path="/synthetic/finished.csv",
                                 data={"element_id": "@independent",
                                       "signals": ["file_saved: /synthetic/finished.csv"]})
    assert out["marker_left"] is None


async def test_ordinary_action_runs_postcondition_after_capture():
    out = await _scenario(ActionType.TYPE_TEXT)
    assert out["trace"] == ["capture-before", "root", "listen:popup", "send", "capture-after", "unlisten:popup",
                            "postcondition"]
    assert out["result"] == dict(_ENVELOPE, action="type_text", reobserve_required=False,
                                 data={"element_id": "@independent", "signals": ["synthetic"]})


async def test_download_after_capture_failure_propagates_and_leaves_marker():
    """현재 동작 고정: 저장 뒤 after-capture 예외는 그대로 전파되고(팝업 리스너 해제 전), marker 가 남는다."""
    out = await _scenario(ActionType.DOWNLOAD_FILE, fail_capture=True)
    assert out["trace"] == ["capture-before", "root", "listen:popup", "send", "capture-after"]
    assert out["result"] == {"exception": "RuntimeError", "message": "synthetic-after-capture"}
    assert out["marker_left"] == "/synthetic/finished.csv"


async def test_download_with_early_navigation_reports_navigation_not_file():
    """현재 동작 고정: 조기 navigation 이면 navigation_started 로 성공, 저장 경로 없음·marker 잔류."""
    out = await _scenario(ActionType.DOWNLOAD_FILE, fail_capture=True, nav=True)
    assert out["trace"] == ["capture-before", "root", "listen:popup", "send", "capture-after", "unlisten:popup"]
    assert out["result"] == dict(_ENVELOPE, action="download_file", reobserve_required=True,
                                 data={"element_id": "@independent", "signals": ["navigation_started"]})
    assert out["marker_left"] == "/synthetic/finished.csv"


async def test_debug_failure_preserves_capture_root_fallback():
    class Page:
        async def evaluate(self, *args: Any) -> Any:
            raise RuntimeError("synthetic page failure")

    d = ad.ActionDispatcher.__new__(ad.ActionDispatcher)
    d.ctx = SimpleNamespace(root_page=Page())
    try:
        result = await d._capture_root()
    except Exception as exc:  # noqa: BLE001
        pytest.fail("logging changed fallback: " + type(exc).__name__)
    assert result is None
