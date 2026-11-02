"""WS-76 (#204): 휴지통·스팸·차단·비우기 판정이 모든 클릭 경로에서 같은지 (실제 서버 + 헤드리스 Chromium, 로컬 HTTP).

* 단건: element_id·selector·좌표·포커스 Space/Enter(WS-31 의 다섯 경로 도우미 재사용) — 이름·aria·title(아이콘)·
  의사요소·testid 로 위험을 드러낸 버튼은 모두 차단(눌리지 않음), 폴더·복원 버튼은 모두 통과(눌림).
* 모호한 selector(WS-39): 후보 중 '휴지통 비우기' 가 있으면 승인 경로, 후보가 폴더 이름뿐이면 모호함 오류.
* browser_batch(WS-73): 'Report spam' 단계에서 approval_required 로 멈추고 남은 단계 실행 0.
레시피 재생 경로는 tests/recipes/test_ws76_replay.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_cli import requires_chromium  # noqa: E402,F401
from test_ws31_gate_paths import _HEAD, _PRESS, PAGES, PATHS, _press, site  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402

from contracts import ActionType, ErrorCode  # noqa: E402
from interface.mcp_server import BrowserMCPServer, tool_name  # noqa: E402

RISKY_KINDS: Dict[str, str] = {
    "name_ko": f"<button id=t {_PRESS}>휴지통으로 이동</button>",
    "name_en": f"<button id=t {_PRESS}>Move to trash</button>",
    "aria_icon": f"<button id=t aria-label=\"Report spam\" {_PRESS}>&#x26A0;</button>",
    "title_icon": f"<button id=t title=\"차단하기\" {_PRESS}>&#x1F6AB;</button>",
    "pseudo": f"<style>#t::before{{content:'휴지통 비우기'}}</style><button id=t {_PRESS}></button>",
    "testid_icon": f"<button id=t data-testid=\"move-to-trash\" {_PRESS}>&#x1F5D1;</button>",
}

BENIGN_KINDS: Dict[str, str] = {
    "folder": f"<button id=t {_PRESS}>휴지통</button>",
    "not_spam": f"<button id=t {_PRESS}>Not spam</button>",
    "unblock": f"<button id=t {_PRESS}>차단 해제</button>",
    "restore": f"<button id=t {_PRESS}>휴지통에서 복원</button>",
}


@requires_chromium
@pytest.mark.parametrize("kind", sorted(RISKY_KINDS))
async def test_risky_blocked_on_every_path(site, kind):  # noqa: F811
    PAGES["/r76"] = _HEAD + RISKY_KINDS[kind]
    verdicts = {}
    async with BrowserMCPServer() as server:
        for path in PATHS:
            r, pressed = await _press(server, site + "/r76", path)
            verdicts[path] = (r.error_code, pressed, (r.data or {}).get("gate_basis"))
    for path, (code, pressed, basis) in verdicts.items():
        assert code is ErrorCode.HITL_UNATTENDED_BLOCKED, (kind, path, verdicts)
        assert not pressed, f"차단됐는데 눌림: {kind} {path}"
        assert isinstance(basis, dict) and basis.get("matched_keyword"), (kind, path, basis)


@requires_chromium
@pytest.mark.parametrize("kind", sorted(BENIGN_KINDS))
async def test_folder_and_restore_pass_on_every_path(site, kind):  # noqa: F811
    PAGES["/b76"] = _HEAD + BENIGN_KINDS[kind]
    verdicts = {}
    async with BrowserMCPServer() as server:
        for path in PATHS:
            r, pressed = await _press(server, site + "/b76", path)
            verdicts[path] = (r.success, r.error_code, r.error_message, pressed)
    for path, (ok, code, msg, pressed) in verdicts.items():
        assert ok and pressed, (kind, path, code, msg)


_AMB_MIXED = _HEAD + (f"<button class=act {_PRESS}>보기</button>"
                      f"<button class=act {_PRESS}>휴지통 비우기</button>")
_AMB_FOLDERS = _HEAD + (f"<button class=act {_PRESS}>휴지통</button>"
                        f"<button class=act {_PRESS}>스팸함</button>")


@requires_chromium
async def test_ambiguous_selector_candidate_judged(site):  # noqa: F811
    PAGES["/amb-mixed"], PAGES["/amb-folders"] = _AMB_MIXED, _AMB_FOLDERS
    async with BrowserMCPServer() as server:
        page_of = lambda: server._dispatcher.ctx.page  # noqa: E731,SLF001
        await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": site + "/amb-mixed"})
        mixed = await server.call_tool(tool_name(ActionType.CLICK), {"selector": "button.act"})
        mixed_out = await page_of().text_content("#out")
        await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": site + "/amb-folders"})
        folders = await server.call_tool(tool_name(ActionType.CLICK), {"selector": "button.act"})
        folders_out = await page_of().text_content("#out")
    assert mixed.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, (mixed.error_code, mixed.error_message)
    assert folders.error_code is ErrorCode.ELEMENT_NOT_FOUND, (folders.error_code, folders.error_message)
    assert folders.data["ambiguous_target"]["count"] == 2
    assert mixed_out == folders_out == "대기"


_LOG = "<script>window.__log = []; function log(x) { window.__log.push(x); }</script>"
_BATCH_HTML = ("<!doctype html><html><head><meta charset=utf-8></head><body>" + _LOG
               + "<button onclick=\"log('spam')\">Report spam</button>"
               + "<button onclick=\"log('next')\">다음</button></body></html>")


@pytest.fixture
def batch_site():
    yield from _serve({"/": _BATCH_HTML})


@requires_chromium
async def test_batch_stops_at_report_spam(batch_site, tmp_path):
    def click(name: str) -> Dict[str, Any]:
        return {"action": "click", "target": {"role": "button", "name": name}}

    async with BrowserMCPServer(handoff_root=tmp_path / "s", recipes=False) as s:
        assert (await s.call_tool("browser_navigate", {"url": batch_site + "/"})).success
        out = await s.call_server_tool("browser_batch", {"steps": [click("Report spam"), click("다음")]})
        log = await s._page.evaluate("() => window.__log")  # noqa: SLF001
    b = out["data"]["batch"]
    assert (b["reason"], b["ran"]) == ("approval_required", 1), b
    assert log == []
    assert out["data"]["step_result"]["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value
