"""WS-85 R1: 격리 판독·게이트 판정을 **입력한 그 노드·그 문서**에 묶는다(검증 B1·B2·B3).

B1 — 격리 민감 판독(입력 직후 activeElement)은 입력한 그 노드일 때만 OR 에 더한다: 입력 완료 이벤트·Tab 이 포커스를 다른
비밀번호 칸으로 옮겨도 검색어를 비밀로 등록하지 않는다(기준 594f087 동작). 정상 비밀번호 칸(위조 type 포함)은 계속 민감.
실제 BrowserMCPServer(헤드리스 Chromium) + 로컬 HTTP 만, 비밀값은 합성.
"""

from __future__ import annotations

import json

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer
from security.secret_registry import mask_registered

from test_run_cli import requires_chromium  # noqa: F401
from test_ws85_isolated_gates import HEAD, PAGES, T_TYPE, _call, _eid, _page, _script, _truth, site  # noqa: F401

PW = "SYNTHuntouched85r1"


def _focus_moves(query: str, spoof: bool) -> str:
    js = ("document.getElementById('q').addEventListener('input',()=>{if(document.getElementById('q').value==="
          + json.dumps(query) + ")document.getElementById('p').focus()});")
    return js + (T_TYPE if spoof else "")


@requires_chromium
@pytest.mark.parametrize("spoof", [False, True])
async def test_b1_search_query_not_secret_when_input_event_focuses_password(site, spoof):
    query = f"ordinaryquery-{spoof}-ws85r1"
    PAGES["/b1"] = (HEAD + f"<input id=q type=search aria-label=검색어><input id=p type=password value={PW}>"
                    + _script(_focus_moves(query, spoof)))
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b1"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.TYPE_TEXT, {"element_id": _eid(obs, lambda e: e.get("name") == "검색어"),
                                                       "text": query, "epoch": obs.snapshot_epoch})
        actual = await _truth(server, "[document.getElementById('q').value, document.getElementById('p').value,"
                                      " document.activeElement.id]")
    assert actual == [query, PW, "p"], actual  # 전제: 검색칸에만 입력됐고 포커스는 비밀번호 칸으로 옮겨졌다
    assert r.success, r.error_message
    assert mask_registered(query) == query
    assert query in json.dumps(r.model_dump(mode="json"), ensure_ascii=False)


@requires_chromium
async def test_b1_tab_to_spoofed_password_is_not_sensitive(site):
    """마무리 보고 한계(2): 일반 칸에서 Tab 으로 위조 비밀번호 칸에 옮겨 가도 키 뒤 격리 판독을 더하지 않는다."""
    PAGES["/b1t"] = (HEAD + f"<input id=q aria-label=검색어 value=plainTabword><input id=p type=password value={PW}t>"
                     + _script(T_TYPE))
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b1t"})
        await _page(server).focus("#q")
        k = await _call(server, ActionType.PRESS_KEY, {"key": "Tab"})
        sensitive = server._dispatcher.last_key_sensitive  # noqa: SLF001
        focused = await _truth(server, "document.activeElement.id")
    assert k.success, k.error_message
    assert focused == "p"  # 전제: 포커스가 위조 비밀번호 칸으로 옮겨졌다
    assert sensitive is False
    assert mask_registered(f"{PW}t") == f"{PW}t"


@requires_chromium
async def test_b1_keydown_moves_focus_to_password_not_sensitive(site):
    """press_key: 키 이벤트가 포커스를 비밀번호 칸으로 옮겨도 키 전 일반 칸 입력은 비밀이 아니다."""
    PAGES["/b1k"] = (HEAD + f"<input id=q aria-label=검색어 value=plainkey><input id=p type=password value={PW}k>"
                     + _script("document.getElementById('q').addEventListener('keyup',()=>"
                               "document.getElementById('p').focus());" + T_TYPE))
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b1k"})
        await _page(server).focus("#q")
        k = await _call(server, ActionType.PRESS_KEY, {"key": "x"})
        sensitive = server._dispatcher.last_key_sensitive  # noqa: SLF001
        actual = await _truth(server, "[document.getElementById('q').value, document.activeElement.id]")
    assert k.success, k.error_message
    assert actual[1] == "p", actual
    assert sensitive is False
    assert mask_registered(f"{PW}k") == f"{PW}k"


@requires_chromium
async def test_b1_spoofed_password_typed_with_focus_kept_still_sensitive(site):
    """정상 비밀번호 칸(위조 type): 입력한 그 노드에 포커스가 남아 있으면 계속 민감 — 노드 일치 검사가 OR 을 끄지 않는다."""
    secret = "SYNTHr1typed85"
    PAGES["/b1p"] = HEAD + "<input id=q aria-label=검색어><input id=p type=password aria-label=pw>" + _script(T_TYPE)
    async with BrowserMCPServer(recipes=False) as server:
        await _call(server, ActionType.NAVIGATE, {"url": site + "/b1p"})
        obs = await _call(server, ActionType.OBSERVE_PAGE, {})
        r = await _call(server, ActionType.TYPE_TEXT, {"element_id": _eid(obs, lambda e: "pw" in (e.get("name") or "")),
                                                       "text": secret, "epoch": obs.snapshot_epoch})
        typed = await _truth(server, "document.getElementById('p').value")
    assert typed == secret
    assert secret not in json.dumps(r.model_dump(mode="json"), ensure_ascii=False)
    assert mask_registered(secret) != secret
