"""WS-76 R1 (#204 검증 B1~B4): 실제 서버 + 헤드리스 Chromium + 로컬 HTTP 로 검증 repro 표본 전부와 대조를 누른다.

* 위험(B3·B4): `스팸으로 보내기`·`휴지통으로 옮겨`·`Block "Alice"`·동작 버튼 `Trash`·`div role=button` Trash·
  항목 조작 목적지 링크(`/messages/42/trash`·`/messages/42/spam`) — 모든 경로에서 차단(눌리지 않음).
* 대조(B1·B2): 검색어 지우기 `비우기`(입력칸 옆)·상태 필터 탭 `차단`(role=tab)·`휴지통 비우기 취소`·`… 안내`·
  `How to report spam`·폴더 링크 `Trash`·`/mail/trash`·`?folder=trash`·탭 `Trash` — 모든 경로에서 눌림(기준 그대로).
* 기존 단독 `차단`·`비우기` 버튼(문맥 없음)은 여전히 차단(fail-closed).
* browser_batch(WS-73): 검색어 지우기 `비우기` 는 끝까지 실행, `스팸으로 보내기` 는 approval_required.
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

from contracts import ErrorCode  # noqa: E402
from interface.mcp_server import BrowserMCPServer  # noqa: E402

_LINK_PRESS = "onclick=\"document.getElementById('out').textContent='눌림';return false\""
#: 링크는 Space 로 눌리지 않는다(브라우저 동작) — 링크 표본은 Space 경로를 뺀다.
LINK_PATHS = tuple(p for p in PATHS if p != "focus_space")

RISKY: Dict[str, str] = {
    "spam_send": f"<button id=t {_PRESS}>스팸으로 보내기</button>",
    "trash_move_conj": f"<button id=t {_PRESS}>휴지통으로 옮겨</button>",
    "block_quoted": f"<button id=t {_PRESS}>Block \"Alice\"</button>",
    "trash_button": f"<button id=t type=button {_PRESS}>Trash</button>",
    "lone_block": f"<button id=t type=button {_PRESS}>차단</button>",
    "lone_empty": f"<button id=t type=button {_PRESS}>비우기</button>",
    # WS-85 (#211 D1, 의도한 기대값 변경): 예전 BENIGN block_tab(탭 1개 tablist) — tablist 래핑 우회와 같은 모양이라 막는다.
    "block_single_tab_list": f"<div role=tablist><button id=t type=button role=tab aria-controls=blocked "
                             f"aria-selected=false {_PRESS}>차단</button></div><section id=blocked>차단한 사용자 보기</section>",
    "lone_empty_no_text_input": f"<label>수량<input type=number value=1></label>"
                                f"<button id=t type=button {_PRESS}>비우기</button>",
}
#: 키 처리기 없는 div role=button 은 Space/Enter 로 눌리지 않는다(눌림 없음 = 미탐 아님) — 포인터 경로만.
RISKY_POINTER: Dict[str, str] = {
    "trash_div_role_button": f"<div id=t role=button tabindex=0 {_PRESS}>Trash</div>",
}
POINTER_PATHS = ("element_id", "selector", "coordinates")
RISKY_LINKS: Dict[str, str] = {
    "endpoint_trash": f"<a id=t href=\"/messages/42/trash\" {_LINK_PRESS}>&#x1F5D1;</a>",
    "endpoint_spam": f"<a id=t href=\"/messages/42/spam\" {_LINK_PRESS}>&#x26A0;</a>",
    "endpoint_query": f"<a id=t href=\"/mail?action=block&amp;id=7\" {_LINK_PRESS}>&#x26D4;</a>",
}

BENIGN: Dict[str, str] = {
    "clear_search": f"<label>검색<input id=q value=\"search words\"></label>"
                    f"<button id=t type=button {_PRESS}>비우기</button>",
    "clear_search_sibling": f"<div><input type=search><button id=t type=button {_PRESS}>비우기</button></div>",
    # WS-85 (#211 D1): 탭 문맥은 보이는 탭 2개 이상인 실제 탭 목록에서만 — 탭 1개 목록은 RISKY 의 block_single_tab_list.
    "block_tab": f"<div role=tablist><button type=button role=tab aria-selected=true>전체</button>"
                 f"<button id=t type=button role=tab aria-controls=blocked aria-selected=false "
                 f"{_PRESS}>차단</button></div><section id=blocked>차단한 사용자 보기</section>",
    "empty_cancel": f"<button id=t {_PRESS}>휴지통 비우기 취소</button>",
    "empty_guide": f"<button id=t {_PRESS}>휴지통 비우기 안내</button>",
    "spam_empty_guide": f"<button id=t {_PRESS}>스팸함 비우기 안내</button>",
    "spam_report_guide": f"<button id=t {_PRESS}>스팸 신고하기 안내</button>",
    "how_to_report": f"<button id=t {_PRESS}>How to report spam</button>",
    "trash_tab": f"<div role=tablist><button id=t type=button role=tab {_PRESS}>Trash</button></div>",
}
BENIGN_LINKS: Dict[str, str] = {
    "folder_trash_link": f"<a id=t href=\"/folder/Trash\" {_LINK_PRESS}>Trash</a>",
    "mail_trash_link": f"<a id=t href=\"/mail/trash\" {_LINK_PRESS}>&#x1F5D1;</a>",
    "folder_query_link": f"<a id=t href=\"/mail?folder=trash\" {_LINK_PRESS}>&#x1F5D1;</a>",
    "spam_link": f"<a id=t href=\"/spam\" {_LINK_PRESS}>Spam</a>",
    "message_view_link": f"<a id=t href=\"/messages/42\" {_LINK_PRESS}>&#x2709;</a>",
}


async def _run(site: str, html: str, paths) -> Dict[str, Any]:  # noqa: F811
    PAGES["/r1"] = _HEAD + html
    out = {}
    async with BrowserMCPServer() as server:
        for path in paths:
            r, pressed = await _press(server, site + "/r1", path)
            out[path] = (r.success, r.error_code, pressed, (r.data or {}).get("gate_basis"))
    return out


@requires_chromium
@pytest.mark.parametrize("kind", sorted(RISKY) + sorted(RISKY_LINKS) + sorted(RISKY_POINTER))
async def test_r1_risky_blocked_on_every_path(site, kind):  # noqa: F811
    html, paths = ((RISKY[kind], PATHS) if kind in RISKY else (RISKY_LINKS[kind], LINK_PATHS) if kind in RISKY_LINKS
                   else (RISKY_POINTER[kind], POINTER_PATHS))
    verdicts = await _run(site, html, paths)
    for path, (_ok, code, pressed, basis) in verdicts.items():
        assert code is ErrorCode.HITL_UNATTENDED_BLOCKED, (kind, path, verdicts)
        assert not pressed, f"차단됐는데 눌림: {kind} {path}"
        assert isinstance(basis, dict) and basis.get("matched_keyword"), (kind, path, basis)


@requires_chromium
@pytest.mark.parametrize("kind", sorted(BENIGN) + sorted(BENIGN_LINKS))
async def test_r1_controls_pass_on_every_path(site, kind):  # noqa: F811
    html, paths = (BENIGN[kind], PATHS) if kind in BENIGN else (BENIGN_LINKS[kind], LINK_PATHS)
    verdicts = await _run(site, html, paths)
    for path, (ok, code, pressed, _basis) in verdicts.items():
        assert ok and pressed, (kind, path, code, verdicts)


def test_sample_sizes():
    assert len(BENIGN) + len(BENIGN_LINKS) >= 10 and len(RISKY) + len(RISKY_LINKS) >= 8


_BATCH_HTML = ("<!doctype html><html><head><meta charset=utf-8></head><body>"
               "<script>window.__log = []; function log(x) { window.__log.push(x); }</script>"
               "<label>검색<input id=q value=\"search words\"></label>"
               "<p id=st>입력됨</p>"
               "<button type=button onclick=\"document.getElementById('q').value='';"
               "document.getElementById('st').textContent='지움';log('clear')\">비우기</button>"
               "<button type=button onclick=\"document.getElementById('st').textContent='스팸';log('spam')\">스팸으로 보내기</button>"
               "<button type=button onclick=\"log('next')\">다음</button></body></html>")


@pytest.fixture
def batch_site():
    yield from _serve({"/": _BATCH_HTML})


@requires_chromium
async def test_batch_clear_search_runs_and_spam_send_stops(batch_site, tmp_path):
    def click(name: str) -> Dict[str, Any]:
        return {"action": "click", "target": {"role": "button", "name": name}}

    async with BrowserMCPServer(handoff_root=tmp_path / "s", recipes=False) as s:
        assert (await s.call_tool("browser_navigate", {"url": batch_site + "/"})).success
        out = await s.call_server_tool("browser_batch", {"steps": [click("비우기"), click("스팸으로 보내기"),
                                                                    click("다음")]})
        log = await s._page.evaluate("() => window.__log")  # noqa: SLF001
        q = await s._page.input_value("#q")  # noqa: SLF001
    b = out["data"]["batch"]
    assert (b["reason"], b["ran"]) == ("approval_required", 2), b
    assert log == ["clear"] and q == ""
    assert out["data"]["step_result"]["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value
