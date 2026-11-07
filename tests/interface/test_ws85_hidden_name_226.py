"""WS-85 (#226, WS-86 리뷰 D2 a): 보이는 라벨은 평범한데 숨긴 checkbox/radio 의 aria-label·title 이 위험 의미면 **신호만**.

`data.hidden_controls[]` 항목에 고정 어휘 `name_risk: "high"` 하나(페이지 글자 없음) — 숨긴 이름의 위험이 라벨보다 높을 때만.
승인 판정은 바꾸지 않는다(라벨 기준 그대로 — WS-81 키워드 게이트는 18호출 과잉이었다). 판독은 격리 world 스캔에서.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, tool_name

from test_run_cli import requires_chromium  # noqa: F401

PAGES: Dict[str, str] = {}
HEAD = ("<!doctype html><meta charset=utf-8><title>s</title><style>.clip{position:absolute;width:1px;height:1px;"
        "clip:rect(0 0 0 0);overflow:hidden;margin:-1px}</style><body><p id=out>대기</p>")
_CHG = "<script>document.addEventListener('change',e=>{document.getElementById('out').textContent=e.target.id},true)</script>"


def _serve() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = PAGES.get(self.path.split("?", 1)[0], "<p>x</p>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def site():
    srv = _serve()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _box(cid: str, hidden_attr: str, label: str) -> str:
    return f"<div><input type=checkbox id={cid} class=clip {hidden_attr}><label for={cid}>{label}</label></div>"


BODY = (_box("a", "aria-label='계정 삭제'", "알림 받기")          # 숨긴 이름만 위험 → 신호
        + _box("b", "title='Delete account'", "Option B")          # title 도
        + _box("c", "aria-label='알림 설정'", "알림 설정")          # 같은 평범한 이름 → 없음
        + _box("d", "aria-label='계정 삭제'", "계정 삭제 동의")     # 라벨도 위험 → 없음(라벨보다 높지 않음)
        + _box("e", "", "뉴스레터"))                                # 숨긴 이름 없음 → 없음


@requires_chromium
async def test_226_hidden_name_risk_signal_only(site):
    PAGES["/h"] = HEAD + BODY + _CHG
    async with BrowserMCPServer() as server:
        await server.call_tool(tool_name(ActionType.NAVIGATE), {"url": site + "/h"})
        obs = await server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        names = {e["element_id"]: e["name"] for e in obs.data["observation"]["elements"]}
        block = obs.data.get("hidden_controls") or []
        by_label = {names.get(e["element_id"]): e for e in block}
        eid = by_label["알림 받기"]["element_id"]
        r = await server.call_tool(tool_name(ActionType.CLICK), {"element_id": eid, "epoch": obs.snapshot_epoch})
        changed = await server._dispatcher.isolated_top_evaluate(  # noqa: SLF001
            "document.getElementById('a').checked")
    assert len(by_label) == 5, by_label
    flagged = sorted(k for k, v in by_label.items() if v.get("name_risk") == "high")
    assert flagged == ["Option B", "알림 받기"], by_label
    assert {v["name_risk"] for v in by_label.values() if "name_risk" in v} == {"high"}
    # 페이지 글자(숨긴 이름)는 싣지 않는다.
    dump = json.dumps(block, ensure_ascii=False)
    assert "계정 삭제" not in dump and "Delete account" not in dump
    # 승인 판정 불변: 평범한 라벨 클릭은 그대로 통과(신호만).
    assert r.success, r.error_message
    assert changed is True
