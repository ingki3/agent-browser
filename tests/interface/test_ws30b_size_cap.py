"""WS-30b 항목 3: 큰 페이지 결과 크기 상한 (observe_page force_full_tree, extract extract_all).

비교 시험에서 observe_page(force_full_tree) 143,456자·extract 58,620자가 Claude Code 도구 결과
한도(기본 25,000 토큰)를 넘어 통째로 버려졌다. 서버가 응답 봉투를 상한(기본 20,000자) 이하로
**항목 경계**(관찰 요소·추출 행)에서 자르고 `data.truncated` 로 알린다. 항목 하나가 이미
상한을 넘을 때만 그 항목의 텍스트를 코드포인트(가능하면 공백) 경계에서 자른다(코디네이터 결정 C).
"""

from __future__ import annotations

import json
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionResult, ActionType, ObserveResult
from interface.mcp_server import (
    DEFAULT_MAX_RESULT_CHARS,
    BrowserMCPServer,
    cap_result_size,
    envelope_json,
    tool_name,
)

from test_run_cli import requires_chromium  # noqa: E402,F401 - Chromium 유무 판정 재사용


def _extract_result(items: Any) -> ActionResult:
    return ActionResult(
        success=True, action=ActionType.EXTRACT, current_url="http://x/", snapshot_epoch=1,
        tab_id="tab-1", retry_safe=True, data={"items": items, "latency_ms": 1.0},
    )


def _obs_result(n: int) -> ActionResult:
    els = [
        {"element_id": f"@e{i}", "role": "button", "name": f"버튼 {i} " + "가" * 30, "value": None,
         "bbox": {"x": i, "y": i, "width": 10, "height": 10}, "interactable": True,
         "is_shadow": False, "score": round(10 - i / 1000, 4)}
        for i in range(1, n + 1)
    ]
    summary = "\n".join(f'{e["element_id"]} {e["role"]} "{e["name"]}"' for e in els)
    obs = {"title": "t", "url": "http://x/", "snapshot_epoch": 1, "elements": els,
           "axtree_summary": summary, "token_count": 999}
    return ActionResult(
        success=True, action=ActionType.OBSERVE_PAGE, current_url="http://x/", snapshot_epoch=1,
        tab_id="tab-1", retry_safe=True,
        data={"observation": obs, "challenge": None, "last_http_status": 200},
    )


def test_default_limit_is_20000():
    assert DEFAULT_MAX_RESULT_CHARS == 20_000


def test_small_results_are_unchanged():
    for r in (_extract_result([{"text": "a"}] * 10), _extract_result({"text": "a"}), _obs_result(20)):
        before = r.model_dump(mode="json")
        cap_result_size(r, 20_000)
        assert r.model_dump(mode="json") == before
        assert "truncated" not in r.data


def test_extract_5000_rows_cut_on_row_boundary():
    rows = [{"text": f"행 {i} " + "내용" * 5, "href": f"/a/{i}"} for i in range(5000)]
    r = _extract_result(list(rows))
    full_chars = len(envelope_json(r))
    cap_result_size(r, 20_000)
    text = envelope_json(r)
    assert len(text) <= 20_000
    items = r.data["items"]
    assert items == rows[: len(items)], "앞에서부터 온전한 행만"
    t = r.data["truncated"]
    assert t["total_items"] == 5000
    assert t["returned_items"] == len(items) > 0
    assert t["total_chars"] == full_chars
    assert t["returned_chars"] == len(text)
    assert "selector" in t["hint"]
    # 한 행을 더 넣으면 상한을 넘는다(최대한 채웠다).
    more = _extract_result(rows[: len(items) + 1])
    more.data["truncated"] = dict(t, returned_items=len(items) + 1)
    assert len(envelope_json(more)) > 20_000


def test_observe_1000_elements_cut_on_element_boundary_and_summary_matches():
    r = _obs_result(1000)
    full = r.model_dump(mode="json")["data"]["observation"]
    cap_result_size(r, 20_000)
    assert len(envelope_json(r)) <= 20_000
    obs = r.data["observation"]
    n = len(obs["elements"])
    assert 0 < n < 1000
    assert obs["elements"] == full["elements"][:n], "점수 순 앞쪽 요소만"
    assert obs["axtree_summary"].splitlines() == full["axtree_summary"].splitlines()[:n]
    ObserveResult.model_validate(obs)
    t = r.data["truncated"]
    assert (t["total_items"], t["returned_items"]) == (1000, n)
    assert "prune_top_n" in t["hint"]


def test_single_item_over_limit_cuts_text_at_boundary():
    """코디네이터 결정 C: 첫 항목 하나가 상한을 넘으면 그 텍스트만 경계에서 자른다."""
    body = ("한국어 본문 문장입니다. 👨‍👩‍👧 가족 이모지와 é 결합 문자. " * 1200)[:29_000]
    for items in ({"text": body}, [{"text": body}, {"text": "둘째"}]):
        r = _extract_result(items)
        cap_result_size(r, 20_000)
        text = envelope_json(r)
        assert len(text) <= 20_000
        got = r.data["items"] if isinstance(items, dict) else r.data["items"][0]
        if isinstance(items, list):
            assert len(r.data["items"]) == 1
        assert got["text_truncated"] is True
        assert got["text_chars"] == len(body)
        cut = got["text"]
        assert body.startswith(cut) and len(cut) < len(body)
        # 경계: 결합 문자·ZWJ·이모지 수식자 앞에서 끊기지 않는다(다음 글자가 결합형이 아님).
        nxt = body[len(cut)]
        assert not unicodedata.combining(nxt) and nxt not in "\u200d\ufe0f"
        assert not (0xDC00 <= ord(cut[-1]) <= 0xDFFF)
        assert cut[-1] != "\u200d"
        t = r.data["truncated"]
        assert t["item_text_truncated"] is True
        assert t["returned_items"] == 1
        assert "extract_all" in t["hint"]


def test_cut_never_splits_zwj_sequence():
    fam = "👨‍👩‍👧"
    body = fam * 20_000
    r = _extract_result({"text": body})
    cap_result_size(r, 20_000)
    cut = r.data["items"]["text"]
    assert len(cut) % len(fam) == 0, "ZWJ 이모지 묶음 중간에서 자르지 않는다"


def test_other_actions_untouched():
    r = ActionResult(success=True, action=ActionType.CLICK, current_url="http://x/", snapshot_epoch=1,
                     tab_id="t", retry_safe=True, data={"signals": ["x" * 50_000]})
    cap_result_size(r, 20_000)
    assert "truncated" not in r.data


# ------------------------------------------------------------------ 실제 서버 경로


PAGES: Dict[str, str] = {}


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = PAGES.get(self.path.split("?", 1)[0], "<p>x</p>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@requires_chromium
async def test_server_caps_force_full_tree_1000_elements(site):
    PAGES["/big"] = "<!doctype html><meta charset=utf-8><title>big</title>" + "".join(
        f"<button>상품 {i} 장바구니에 담기</button>" for i in range(1000)
    )
    PAGES["/small"] = "<!doctype html><meta charset=utf-8><title>s</title><button>하나</button>"
    async with BrowserMCPServer() as server:
        await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": site + "/big"})
        r = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {"force_full_tree": True})
        await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": site + "/small"})
        small = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {"force_full_tree": True})
    assert len(envelope_json(r)) <= DEFAULT_MAX_RESULT_CHARS
    t = r.data["truncated"]
    assert t["total_items"] >= 1000
    assert t["returned_items"] == len(r.data["observation"]["elements"]) > 0
    assert t["total_chars"] > DEFAULT_MAX_RESULT_CHARS
    assert r.data["challenge"] is None, "차단 신호 칸은 남는다"
    assert "truncated" not in small.data


@requires_chromium
async def test_server_caps_extract_all_5000_rows(site):
    PAGES["/rows"] = "<!doctype html><meta charset=utf-8><title>rows</title><ul>" + "".join(
        f"<li>{i}번째 기사 제목입니다</li>" for i in range(5000)
    ) + "</ul>"
    async with BrowserMCPServer() as server:
        await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": site + "/rows"})
        r = await server.call_tool(tool_name(ActionType.EXTRACT), {"selector": "li", "extract_all": True})
        few = await server.call_tool(tool_name(ActionType.EXTRACT), {"selector": "li:nth-child(-n+3)",
                                                                     "extract_all": True})
    assert r.success
    assert len(envelope_json(r)) <= DEFAULT_MAX_RESULT_CHARS
    t = r.data["truncated"]
    assert t["total_items"] == 5000
    assert t["returned_items"] == len(r.data["items"])
    assert r.data["items"][0]["text"] == "0번째 기사 제목입니다"
    assert [x["text"] for x in few.data["items"]] == [f"{i}번째 기사 제목입니다" for i in range(3)]
    assert "truncated" not in few.data


def test_serve_option_sets_limit(monkeypatch):
    from interface import cli, mcp_server

    seen: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    assert cli.main(["serve"]) == 0
    assert seen["max_result_chars"] == DEFAULT_MAX_RESULT_CHARS
    assert cli.main(["serve", "--max-result-chars", "50000"]) == 0
    assert seen["max_result_chars"] == 50_000
    _, backend = mcp_server.create_server(max_result_chars=1234)
    assert backend.max_result_chars == 1234


def test_serve_option_rejects_tiny_limit():
    from interface import cli

    with pytest.raises(SystemExit):
        cli.main(["serve", "--max-result-chars", "100"])


def test_json_of_envelope_parses():
    r = _obs_result(1000)
    cap_result_size(r, 20_000)
    assert json.loads(envelope_json(r))["data"]["truncated"]["returned_items"] > 0
