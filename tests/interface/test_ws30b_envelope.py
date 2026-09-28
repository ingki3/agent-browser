"""WS-30b 항목 1: MCP 응답 봉투 다이어트.

`_call_tool_impl` 이 `ActionResult.model_dump_json()` 으로 대부분 null/false 인 고정 필드를
매 응답에 실었다(작업당 ~100 토큰). 봉투(`envelope_json`)는 계약 기본값과 같은 필드를 빼되
의미 있는 값은 남긴다. 규칙: **빠진 필드 = 계약 기본값(null/false/빈 data)**.

* 19종 액션의 실제 결과(+ 실패 결과)를 봉투로 보내고 클라이언트가 계약 모델로 다시 파싱하면
  원래 결과와 같은 객체다(관찰은 ObserveResult 로 다시 파싱해 비교).
* 항상 남는 필드: 계약 필수 필드(success·action·current_url·snapshot_epoch·tab_id·retry_safe),
  reobserve_required(false 도 명시), 실패 시 error_code·error_message.
* data 안의 값은 계약 모델이 없는 자유 형식이라 null 도 그대로 둔다(data.challenge: null =
  "차단 없음", last_http_status: null = "아직 문서 응답 없음").
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from contracts import ActionResult, ActionType, ErrorCode, ObserveResult
from interface.mcp_server import BrowserMCPServer, envelope_json, tool_name

requires_chromium = pytest.mark.requires_chromium


def _reparsed_equal(original: ActionResult, text: str) -> None:
    """봉투 텍스트를 계약 모델로 다시 파싱하면 원래 결과와 같다."""
    back = ActionResult.model_validate(json.loads(text))
    a = original.model_dump(mode="json")
    b = back.model_dump(mode="json")
    obs_a = a["data"].pop("observation", None)
    obs_b = b["data"].pop("observation", None)
    assert a == b
    if obs_a is not None or obs_b is not None:
        assert ObserveResult.model_validate(obs_a) == ObserveResult.model_validate(obs_b)


def _result(**kw: Any) -> ActionResult:
    base: Dict[str, Any] = dict(
        success=True, action=ActionType.NAVIGATE, current_url="http://x/", snapshot_epoch=1,
        tab_id="tab-1", retry_safe=True,
    )
    base.update(kw)
    return ActionResult(**base)


# ------------------------------------------------------------------ 필드 규칙 (브라우저 없음)


def test_success_envelope_drops_default_fields_keeps_required():
    r = _result(data={"url": "http://x/", "challenge": None, "last_http_status": 200})
    j = json.loads(envelope_json(r))
    for key in ("success", "action", "current_url", "snapshot_epoch", "tab_id", "retry_safe",
                "reobserve_required"):
        assert key in j, key
    for key in ("healed", "downloaded_path", "popup_tab_id", "error_code", "error_message"):
        assert key not in j, key
    assert j["reobserve_required"] is False
    # 의미 있는 null 은 data 안에 그대로 남는다.
    assert "challenge" in j["data"] and j["data"]["challenge"] is None
    _reparsed_equal(r, envelope_json(r))


def test_failure_envelope_keeps_error_fields():
    r = _result(success=False, error_code=ErrorCode.TIMEOUT, error_message="사후조건 미충족",
                retry_safe=False)
    j = json.loads(envelope_json(r))
    assert j["success"] is False
    assert j["error_code"] == "E_TIMEOUT"
    assert j["error_message"] == "사후조건 미충족"
    assert "data" not in j, "빈 data 는 기본값({})이라 뺀다"
    _reparsed_equal(r, envelope_json(r))


def test_non_default_optional_fields_are_kept():
    r = _result(healed=True, reobserve_required=True, downloaded_path="/tmp/a.csv",
                popup_tab_id="tab-2", data={"k": 1})
    j = json.loads(envelope_json(r))
    assert j["healed"] is True and j["reobserve_required"] is True
    assert j["downloaded_path"] == "/tmp/a.csv" and j["popup_tab_id"] == "tab-2"
    _reparsed_equal(r, envelope_json(r))


def test_observation_elements_drop_contract_defaults_only():
    obs = {
        "title": "t", "url": "http://x/", "snapshot_epoch": 1, "axtree_summary": "s",
        "token_count": 1,
        "elements": [
            {"element_id": "@e1", "role": "button", "name": "로그인", "value": None,
             "bbox": {"x": 1, "y": 2, "width": 3, "height": 4}, "interactable": True,
             "is_shadow": False, "score": 8.1},
            {"element_id": "@e2", "role": "textbox", "name": "아이디", "value": "",
             "bbox": {"x": 1, "y": 2, "width": 3, "height": 4}, "interactable": False,
             "is_shadow": True, "score": 7.0},
        ],
    }
    r = _result(action=ActionType.OBSERVE_PAGE, data={"observation": obs, "challenge": None})
    j = json.loads(envelope_json(r))
    e1, e2 = j["data"]["observation"]["elements"]
    assert "value" not in e1 and "is_shadow" not in e1
    assert e2["value"] == "" and e2["is_shadow"] is True and e2["interactable"] is False
    assert e1["interactable"] is True, "필수 필드(기본값 없음)는 남는다"
    _reparsed_equal(r, envelope_json(r))


def test_unparseable_observation_is_left_as_is():
    """계약 모델로 읽히지 않는 관찰(형식이 다른 dict)은 건드리지 않는다."""
    r = _result(action=ActionType.OBSERVE_PAGE, data={"observation": {"odd": None}})
    assert json.loads(envelope_json(r))["data"] == {"observation": {"odd": None}}


def test_rule_is_stated_in_tool_description():
    from interface.mcp_server import build_tool_schema

    desc = build_tool_schema(ActionType.OBSERVE_PAGE)["description"]
    assert "빠진 필드=계약 기본값" in desc


def test_envelope_is_smaller_than_full_dump():
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    r = _result(data={"url": "http://x/", "latency_ms": 18.6, "challenge": None,
                      "last_http_status": 200})
    before = len(enc.encode(r.model_dump_json()))
    after = len(enc.encode(envelope_json(r)))
    assert after <= before - 20, (before, after)


@requires_chromium
async def test_sdk_roundtrip_returns_envelope():
    """실제 MCP 세션(tools/call)으로 받은 텍스트가 봉투 형식이다(_call_tool_impl 경로)."""
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from interface.mcp_server import create_server

    server, backend = create_server()
    got: Dict[str, Any] = {}
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: server.run(server_streams[0], server_streams[1],
                                             server.create_initialization_options()))
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    res = await session.call_tool("browser_navigate", {"url": "about:blank"})
                    got["text"] = res.content[0].text
            finally:
                tg.cancel_scope.cancel()
    await backend.close()
    j = json.loads(got["text"])
    assert j["success"] is True and j["reobserve_required"] is False
    assert "healed" not in j and "error_code" not in j and "popup_tab_id" not in j
    assert j["data"]["challenge"] is None
    assert ActionResult.model_validate(j).healed is False


# ------------------------------------------------------------------ 19종 실제 결과 왕복


async def _all_19_results() -> List[ActionResult]:
    """harness.mcp_smoke 의 호출 계획으로 19종을 실제 서버에서 한 번씩 부른다."""
    from harness import MockServer
    from harness.mcp_smoke import CALL_PLAN

    out: List[ActionResult] = []
    tmpdir = tempfile.mkdtemp(prefix="ws30b_env_")
    upload = Path(tmpdir) / "sample.txt"
    upload.write_text("x", encoding="utf-8")
    with MockServer() as mock:
        srv = BrowserMCPServer(
            headless=True, pre_approved_actions=("click:*", "download_file:*", "upload_file:*")
        )
        await srv.start()
        try:
            for action in ActionType:
                plan = dict(CALL_PLAN.get(action, {}))
                site = plan.pop("_site", "s01_login")
                await srv._page.goto(mock.site_url(site), wait_until="domcontentloaded")  # noqa: SLF001
                if plan.pop("_needs_history", False):
                    await srv._page.goto(mock.site_url("s02_twofactor"))  # noqa: SLF001
                if "_navigate_to" in plan:
                    plan["url"] = mock.site_url(plan.pop("_navigate_to"))
                if plan.pop("_needs_temp_file", False):
                    plan["file_paths"] = [str(upload)]
                if plan.pop("_needs_temp_dir", False):
                    plan["save_dir"] = tmpdir
                target = plan.pop("_element", None)
                if target is not None:
                    obs = await srv.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
                    o = obs.data["observation"]
                    eid = next(e["element_id"] for e in o["elements"]
                               if (e["role"], e["name"]) == tuple(target))
                    key = "trigger_element_id" if action is ActionType.DOWNLOAD_FILE else "element_id"
                    plan[key] = eid
                    plan["epoch"] = o["snapshot_epoch"]
                out.append(await srv.call_tool(tool_name(action), plan))
            # 실패 결과 3종: 입력 검증 실패, 알 수 없는 툴, HITL 차단
            out.append(await srv.call_tool(tool_name(ActionType.NAVIGATE), {}))
            out.append(await srv.call_tool("browser_nope", {}))
            blocked = BrowserMCPServer(headless=True)
            blocked._started = True  # noqa: SLF001 - 브라우저 없이 게이트만
            blocked._dispatcher = srv._dispatcher  # noqa: SLF001
            blocked._engine = srv._engine  # noqa: SLF001
            blocked._page = srv._page  # noqa: SLF001
            blocked._core = srv._core  # noqa: SLF001
            from security import HITLGate

            blocked._hitl = HITLGate(mode=blocked.mode)  # noqa: SLF001
            out.append(await blocked.call_tool(
                tool_name(ActionType.UPLOAD_FILE),
                {"element_id": "@e1", "epoch": srv._engine.epoch, "file_paths": [str(upload)]},  # noqa: SLF001
            ))
        finally:
            await srv.close()
    return out


@requires_chromium
async def test_all_19_actions_roundtrip_through_envelope():
    results = await _all_19_results()
    actions = {r.action for r in results}
    assert actions == set(ActionType), sorted(a.value for a in set(ActionType) - actions)
    assert any(not r.success for r in results), "실패 결과도 포함돼야 한다"
    assert any(r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED for r in results)
    saved = 0
    for r in results:
        text = envelope_json(r)
        _reparsed_equal(r, text)
        j = json.loads(text)
        assert "reobserve_required" in j and "retry_safe" in j and "success" in j
        if not r.success:
            assert j["error_code"] == r.error_code.value
            assert j["error_message"] == r.error_message
        saved += len(r.model_dump_json()) - len(text)
    assert saved > 0
