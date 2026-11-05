"""WS-84 (#227): 목록 밖 generic 라벨 레시피 재생 — locateUi 후보에만 label, 짝 control 정체 구별, 짝 라벨 click → check_box.

설계 기준 .hermes/plans/ws84-evidence/review.md B5·F6. 키 계산(KEYS_JS)은 로컬 route 페이지, 제품 경로는 BrowserMCPServer +
로컬 HTTP. '바뀌었나' 는 서버 요청 기록(/mark — change 이벤트가 보냄)과 페이지 상태로 본다.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, tool_name
from recipes import keys

CSS = ("<style>input.h{position:absolute;opacity:0;width:1px;height:1px} "
       "label{display:block;margin:8px;padding:6px;border:1px solid #888;width:200px}</style>")


def row(cid: str, text: str, checked: bool = False) -> str:
    c = " checked" if checked else ""
    return (f"<div class=row><input class=h type=checkbox id={cid}{c} "
            f"onchange=\"fetch('/mark?id='+this.id+'&v='+this.checked)\"><label for={cid}>{text}</label></div>")


def wrapped(text: str) -> str:
    return (f"<div class=row><label><input class=h type=checkbox "
            f"onchange=\"fetch('/mark?w='+this.checked)\">{text}</label></div>")


def doc(body: str) -> str:
    return ("<!doctype html><html><head><meta charset=utf-8><title>약관</title>" + CSS + "</head><body><main>"
            + body + "<button type=button onclick=\"document.getElementById('st').textContent='다음'\">다음</button>"
            "<p id=st></p></main></body></html>")


REC = doc(row("c1", "안내 확인") + row("c2", "마케팅 수신"))
SWAPPED = doc(row("c9", "안내 확인") + row("c2", "마케팅 수신"))
LABEL1 = "main > div:nth-of-type(1) > label:nth-of-type(1)"


async def _page(pw, html: str, url: str = "http://mock.test/terms"):
    browser = await pw.chromium.launch(headless=True)
    page = await (await browser.new_context()).new_page()

    async def _route(route):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    await page.route("**/*", _route)
    await page.goto(url)
    return browser, page


async def _snap_then(before: str, after: str, css: str):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b1, p1 = await _page(pw, before)
        snap = await keys.snapshot(p1, css)
        target = keys.make_target(snap["target"])
        b2, p2 = await _page(pw, after)
        found = await keys.locate(p2, target)
        ctl = None
        if found.get("ok"):
            ctl = await p2.evaluate("c => { const l = document.querySelector(c); return l && l.control ? l.control.id : null; }",
                                    found["css"])
        snap2 = await keys.snapshot(p2, None)
        await b1.close()
        await b2.close()
    return snap, target, found, ctl, snap2


@pytest.mark.asyncio
async def test_label_target_records_control_identity_and_relocates():
    snap, target, found, ctl, _ = await _snap_then(REC, REC, LABEL1)
    assert target["kind"] == "ui" and target["group"] == "generic", target
    assert target["ctl"] == {"tag": "input", "type": "checkbox", "id": "c1", "name": "", "value": None}, target
    assert found["ok"], found
    assert ctl == "c1"


@pytest.mark.asyncio
async def test_same_text_same_place_label_bound_to_other_control_is_not_found():
    _, _, found, _, _ = await _snap_then(REC, SWAPPED, LABEL1)
    assert found["ok"] is False and found["reason"] == "target_not_found", found


@pytest.mark.asyncio
async def test_two_same_labels_without_control_id_nearby_are_ambiguous():
    _, target, found, _, _ = await _snap_then(doc(wrapped("안내 확인")),
                                              doc(wrapped("안내 확인") + wrapped("안내 확인")), LABEL1)
    assert target["ctl"]["id"] == "", target
    assert found["ok"] is False and found["reason"] == "target_ambiguous", found


@pytest.mark.asyncio
async def test_plain_generic_label_outside_list_replays():
    page = doc("<label onclick=\"document.getElementById('st').textContent='열림'\">자세히 보기</label>")
    _, target, found, _, _ = await _snap_then(page, page, "main > label:nth-of-type(1)")
    assert target["ctl"] is None and target["group"] == "generic", target
    assert found["ok"], found


@pytest.mark.asyncio
async def test_ready_count_ignores_labels():
    """label 은 locateUi 후보에만 — 준비 수(readyCount)·골격 바이트는 그대로(리뷰 B5a)."""
    many = doc(row("c1", "안내 확인") + "".join(f"<label>설명 {i}</label>" for i in range(12)))
    few = doc(row("c1", "안내 확인"))
    snap_many, _, _, _, _ = await _snap_then(many, many, LABEL1)
    snap_few, _, _, _, _ = await _snap_then(few, few, LABEL1)
    assert snap_many["ready"] == snap_few["ready"], (snap_many["ready"], snap_few["ready"])


@pytest.mark.asyncio
async def test_old_ui_target_without_ctl_ignores_plain_labels():
    """ctl 이 없는(이전) ui 대상은 label 후보를 보지 않는다 — 기존 레시피 동작 그대로."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, p = await _page(pw, doc("<label>다음</label>"))
        btn = await keys.snapshot(p, "main > button:nth-of-type(1)")
        tgt = keys.make_target(btn["target"])
        found = await keys.locate(p, tgt)
        await b.close()
    assert "ctl" not in tgt and found["ok"] and found["role"] == "button", (tgt, found)


# ------------------------------------------------------------------ 제품 경로: 기록 → 재생 멱등


@pytest.fixture
def site():
    hits: List[str] = []
    state: Dict[str, Any] = {"html": REC}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            if self.path.startswith("/mark"):
                self.send_response(204)
                self.end_headers()
                return
            body = (b"User-agent: *\nAllow: /\n" if self.path == "/robots.txt" else state["html"].encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits, state
    finally:
        srv.shutdown()
        srv.server_close()


async def _call(srv: BrowserMCPServer, action: ActionType, **args: Any):
    return await srv.call_tool(tool_name(action), args)


async def _checked(srv: BrowserMCPServer) -> Dict[str, bool]:
    page = srv._dispatcher.ctx.page  # noqa: SLF001
    await page.wait_for_timeout(300)  # change 의 fetch 가 서버에 닿을 시간
    return await page.evaluate("() => Object.fromEntries([...document.querySelectorAll('input')].map(i => [i.id, i.checked]))")


@pytest.mark.asyncio
async def test_paired_label_click_recorded_as_check_box_and_replay_is_idempotent(site):
    base, hits, state = site
    async with BrowserMCPServer(headless=True, profile="ws84") as srv:
        assert (await _call(srv, ActionType.NAVIGATE, url=base + "/terms")).success
        obs = await _call(srv, ActionType.OBSERVE_PAGE)
        assert any(e["element_id"] for e in obs.data.get("hidden_controls") or []), obs.data.keys()
        eid = next(e["element_id"] for e in obs.data["observation"]["elements"] if e["name"] == "안내 확인")
        res = await _call(srv, ActionType.CLICK, element_id=eid, epoch=obs.snapshot_epoch)
        assert res.success and res.data.get("hidden_control") == {"checked_before": False, "checked_after": True}, \
            res.model_dump(mode="json")
        saved = await srv.call_server_tool("browser_recipe", {"op": "save", "name": "약관", "last_n": 1})
        assert saved["success"], saved
        rid = saved["data"]["recipe"]["id"]
        rec = srv._recipes.store.get(rid)  # noqa: SLF001
        step = rec["steps"][0]
        assert step["action"] == "check_box" and step["args"] == {"checked": True}, step

        outs = {}
        for start in (True, False):
            state["html"] = doc(row("c1", "안내 확인", checked=start) + row("c2", "마케팅 수신"))
            assert (await _call(srv, ActionType.NAVIGATE, url=base + "/terms")).success
            hits.clear()
            out = await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid})
            outs[start] = (out, await _checked(srv), [h for h in hits if h.startswith("/mark")])

        state["html"] = SWAPPED
        assert (await _call(srv, ActionType.NAVIGATE, url=base + "/terms")).success
        hits.clear()
        swapped = await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid})
        swapped_state = await _checked(srv)
        swapped_marks = [h for h in hits if h.startswith("/mark")]

    out, st, marks = outs[True]  # 이미 체크 → 체크 유지(풀지 않음), 입력 없음
    assert out["success"], out
    assert st == {"c1": True, "c2": False} and marks == [], (st, marks)
    out, st, marks = outs[False]  # 해제 → 체크
    assert out["success"], out
    assert st == {"c1": True, "c2": False} and marks == ["/mark?id=c1&v=true"], (st, marks)
    assert swapped["success"] is False and swapped["data"]["recipe"]["reason"] == "target_not_found", swapped
    assert swapped_state == {"c9": False, "c2": False} and swapped_marks == []
