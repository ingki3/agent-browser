"""WS-34 실 브라우저 E2E — serve --browser on-demand (로컬 Mock 사이트 + 실제 Chromium).

(a) 시작 → 창 없음(이 프로필의 headed 브라우저 프로세스 0) → navigate·observe 정상
(b) Mock 캡차 → control_request → 창 열림(같은 URL·로그인 쿠키 유지) → (테스트가 사람 역할) take →
    해결 → release → headless 복귀(해결 상태 유지, epoch 증가) → 에이전트가 가격 읽기
(c) sticky: 복귀 뒤 같은 도메인에서 캡차 재감지 → sticky_pending → 다음 인계 뒤 창 유지
(d) 결제 버튼 차단 → approval → approve 시 별도 창에 코드(페이지는 headless 유지) → 코드 입력 →
    재호출 1회 성공(두 번째 거부), 별도 창 닫힘. 별도 창은 core 탭·tab_control list 에 없고 네트워크 차단
(e) 전환 중 동시 도구 호출 · 재오픈/route 재설치/탭 복원 실패 · 사람이 창을 직접 닫음 · 전환 중 SIGTERM
(f) 매 전환 뒤 egress(사설망 차단·route 조기 차단·검증 프록시 경유)
(g) 실제 stdio(ClientSession) 로 `serve --browser on-demand` 기동

창이 실제로 뜨는 테스트(requires_display)는 macOS(또는 DISPLAY 있는 Linux)에서만 돈다 — Linux CI 에
xvfb 가 없으면 skip(사유 표시). 창 없이 도는 핵심 논리는 test_ws34_on_demand_unit.py 가 고정한다.
실제 공개 사이트 없음(Mock 만). 프로필 폴더는 conftest 가 임시 디렉터리로 돌린다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from contracts import ErrorCode
from interface import handoff, on_demand
from interface.handoff import write_command
from interface.mcp_server import BrowserMCPServer, envelope_dict

from test_run_cli import requires_chromium

requires_display = pytest.mark.skipif(
    sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
    reason="창(headed) 테스트: 화면 없음(Linux CI·xvfb 없음) — 창 없는 핵심 논리는 "
           "test_ws34_on_demand_unit.py 가 고정",
)

LOGIN = """<!doctype html><meta charset=utf-8><title>로그인</title>
<form method=post action=/login><input aria-label='아이디' name=u>
<button type=submit>로그인</button></form>"""
CAPTCHA = """<!doctype html><meta charset=utf-8><title>Just a moment...</title>
<p>Verify you are human</p><button id=solve onclick="{js}">사람입니다</button>"""
PRICE = """<!doctype html><meta charset=utf-8><title>상품</title><p id=price>사과 3000원</p>
<p id=who>{who}</p>"""
PAY = """<!doctype html><meta charset=utf-8><title>결제</title><p id=out>대기</p>
<button onclick="document.getElementById('out').textContent='결제됨'">결제하기</button>"""


def _ua_tag(ua: str) -> str:
    return hashlib.sha256(ua.encode()).hexdigest()[:12]


@pytest.fixture
def site():
    hits: List[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str, headers: Dict[str, Any] = None) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            for k, v in (headers or {}).items():
                for one in (v if isinstance(v, list) else [v]):
                    self.send_header(k, one)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            if self.path.startswith("/login"):
                self._send(303, "", {"Location": "/shop",
                                     "Set-Cookie": "auth=ok; Max-Age=86400; Path=/"})
            elif self.path.startswith("/solve_strict"):
                # 이 브라우저(UA) 에서 통과한 결과만 인정하는 사이트(HeadlessChrome 이면 다시 요구).
                tag = _ua_tag(self.headers.get("User-Agent") or "")
                self._send(200, "ok", {"Set-Cookie": f"pass_ua={tag}; Max-Age=3600; Path=/"})
            else:
                self._send(404, "")

        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            cookies = self.headers.get("Cookie") or ""
            ua = self.headers.get("User-Agent") or ""
            who = "로그인됨" if "auth=ok" in cookies else "비로그인"
            if self.path.startswith("/login"):
                self._send(200, LOGIN)
            elif self.path.startswith("/shop"):
                if "pass=1" in cookies:
                    self._send(200, PRICE.format(who=who))
                else:
                    js = "document.cookie='pass=1; max-age=3600; path=/'; location.reload()"
                    self._send(403, CAPTCHA.format(js=js))
            elif self.path.startswith("/strict"):
                if f"pass_ua={_ua_tag(ua)}" in cookies:
                    self._send(200, PRICE.format(who=who))
                else:
                    js = ("fetch('/solve_strict',{method:'POST'}).then(()=>location.reload())")
                    self._send(403, CAPTCHA.format(js=js))
            elif self.path.startswith("/pay"):
                self._send(200, PAY)
            elif self.path.startswith("/form"):
                self._send(200, "<!doctype html><meta charset=utf-8><title>폼</title>"
                                "<input id=q aria-label='메모'>")
            else:
                self._send(200, "<!doctype html><meta charset=utf-8><title>home</title><p>home</p>")

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield type("Site", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}", "hits": hits,
                                "server": srv})
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ 도구


def _inproc(s: BrowserMCPServer):
    async def call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name.startswith("browser_control") or name.startswith("browser_approval"):
            return await s.call_server_tool(name, args)
        return envelope_dict(await s.call_tool(name, args))
    return call


def _procs_for(path: Path) -> List[Dict[str, Any]]:
    """이 프로필 폴더를 쓰는 Chromium 브라우저(메인) 프로세스 — headed 여부와 함께."""
    out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True)
    rows = []
    needle = f"--user-data-dir={path}"
    for line in out.stdout.splitlines():
        if needle not in line or "--type=" in line:
            continue
        pid, _, cmd = line.strip().partition(" ")
        rows.append({"pid": int(pid), "headed": "--headless" not in cmd})
    return rows


def _headed(s: BrowserMCPServer) -> int:
    return sum(1 for p in _procs_for(s._core.persistent_profile) if p["headed"])


async def _wait_no_procs(path: Path, timeout: float = 10.0) -> List[Dict[str, Any]]:
    deadline = time.monotonic() + timeout
    rows = _procs_for(path)
    while rows and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        rows = _procs_for(path)
    return rows


async def _human(s: BrowserMCPServer, op: str) -> None:
    write_command(s.hub.root, s.hub.server_id, op)


async def _egress_ok(s: BrowserMCPServer, call, site) -> None:
    """(f) 전환 뒤에도: 허용 이동은 검증 프록시를 거치고, 사설망은 route 가 조기 차단한다.

    창 있는 Chromium 은 배경 요청(구성요소 갱신 등)도 프록시로 보낸다 — 그래서 '처리 수가 그대로'
    가 아니라 '프록시가 차단 대상 호스트를 보지 않았음'(route 가 먼저 막음)으로 판정한다."""
    proxy = s._egress_runtime.proxy
    seen: List[str] = []
    real = proxy._decide

    async def spy(url, host, port):
        seen.append(str(host))
        return await real(url, host, port)

    proxy._decide = spy
    try:
        assert (await call("browser_navigate", {"url": site.url + "/"}))["success"]
        assert "127.0.0.1" in seen, "브라우저가 검증 프록시를 거치지 않음"
        blocked = await call("browser_navigate", {"url": "http://10.0.0.1/"})
        assert not blocked["success"]
        assert blocked["data"]["egress"]["category"] == "private", blocked
        assert "10.0.0.1" not in seen, "route 가드가 다시 설치되지 않음"
    finally:
        proxy._decide = real


async def _wait_holder(s: BrowserMCPServer, holder: str, timeout: float = 10.0) -> None:
    """사람 명령은 감시 태스크가 먼저 처리할 수 있다(control_wait 는 '다음' 변화를 기다린다) —
    상태로 확인한다."""
    deadline = time.monotonic() + timeout
    while s.hub.holder != holder and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert s.hub.holder == holder


async def _wait_window(s: BrowserMCPServer, state: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while s._window.state != state and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert s._window.state == state, s._window.info()


# ------------------------------------------------------------------ (a)


@requires_chromium
async def test_a_starts_headless_without_window(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        assert (await call("browser_navigate", {"url": site.url + "/pay"}))["success"]
        obs = await call("browser_observe_page", {})
        assert obs["success"]
        assert any(e["name"] == "결제하기" for e in obs["data"]["observation"]["elements"])
        rows = _procs_for(s._core.persistent_profile)
        assert rows and not any(r["headed"] for r in rows), rows  # 브라우저는 있고 창은 없다
        st = await call("browser_control_status", {})
        assert st["data"]["window"] == {"mode": "on-demand", "state": "headless", "sticky": False}
        path = s._core.persistent_profile
    assert await _wait_no_procs(path) == []
    assert not path.exists()  # 서버 전용 임시 프로필은 종료 때 지운다


# ------------------------------------------------------------------ (b) + (f)


@requires_chromium
@requires_display
async def test_b_handoff_opens_window_then_returns_headless(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await _egress_ok(s, call, site)
        await call("browser_navigate", {"url": site.url + "/login"})
        obs = await call("browser_observe_page", {})
        by = {e["name"]: e["element_id"] for e in obs["data"]["observation"]["elements"]}
        ep = obs["data"]["observation"]["snapshot_epoch"]
        await call("browser_type_text", {"element_id": by["아이디"], "epoch": ep, "text": "mock"})
        await call("browser_click", {"element_id": by["로그인"], "epoch": ep})
        nav = await call("browser_navigate", {"url": site.url + "/shop"})
        assert nav["data"]["challenge"]["kind"] == "captcha", nav
        epoch0 = nav["snapshot_epoch"]

        r = await call("browser_control_request", {"reason": "캡차를 풀어 주세요"})
        assert r["success"], r
        w = r["data"]["window"]
        assert w["state"] == "headed" and w["reopened"] is True and w["mode"] == "on-demand"
        assert "sessionStorage" in w["hint"]
        assert w["tabs"][0]["restored"] is True and w["tabs"][0]["url"].endswith("/shop")
        assert _headed(s) == 1
        assert s._page.url.endswith("/shop")
        cookies = {c["name"]: c["value"] for c in await s._page.context.cookies()}
        assert cookies.get("auth") == "ok"  # 로그인 쿠키가 창으로 넘어왔다
        await _egress_ok(s, call, site)  # (f) 창 쪽 브라우저도 같은 가드
        await call("browser_navigate", {"url": site.url + "/shop"})

        await _human(s, "take")
        await _wait_holder(s, "human")
        # 사람 역할: 창에서 직접 해결
        await s._page.click("#solve")
        await s._page.wait_for_selector("#price")
        await _human(s, "release")
        got = await call("browser_control_wait", {"timeout_s": 20})
        assert got["data"]["changed"] == "released", got
        w = got["data"]["window"]
        assert w["state"] == "headless" and w["reopened"] is True
        assert "다시 관찰" in w["hint"]
        assert got["data"]["snapshot_epoch"] > epoch0
        assert _headed(s) == 0
        assert s._page.url.endswith("/shop")
        price = await call("browser_extract", {"selector": "#price"})
        assert "3000원" in json.dumps(price["data"], ensure_ascii=False), price
        who = await call("browser_extract", {"selector": "#who"})
        assert "로그인됨" in json.dumps(who["data"], ensure_ascii=False)
        st = await call("browser_control_status", {})
        assert st["data"]["window"]["state"] == "headless"
        await _egress_ok(s, call, site)  # (f) 복귀 뒤에도
        path = s._core.persistent_profile
    assert await _wait_no_procs(path) == []


# ------------------------------------------------------------------ (c) sticky


@requires_chromium
@requires_display
async def test_c_sticky_after_rechallenge_on_same_domain(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        nav = await call("browser_navigate", {"url": site.url + "/strict"})
        assert nav["data"]["challenge"] is not None
        assert "window" not in nav["data"]  # 아직 해결한 적 없는 사이트 — sticky 알림 없음
        assert (await call("browser_control_request", {"reason": "캡차"}))["success"]
        await _human(s, "take")
        await _wait_holder(s, "human")
        await s._page.click("#solve")
        await s._page.wait_for_selector("#price")
        await _human(s, "release")
        got = await call("browser_control_wait", {"timeout_s": 20})
        assert got["data"]["window"]["state"] == "headless"
        # HeadlessChrome UA 라 사이트가 창에서 통과한 결과를 인정하지 않는다 → 다시 캡차
        again = await call("browser_navigate", {"url": site.url + "/strict"})
        assert again["data"]["challenge"] is not None
        w = again["data"]["window"]
        assert w["sticky_pending"]["domain"] == "127.0.0.1", w
        assert "control_request" in w["hint"]
        st = await call("browser_control_status", {})
        assert st["data"]["window"]["sticky_pending"]["domain"] == "127.0.0.1"
        # 사람용 상태 파일(agent-browser control status)에도
        disk = handoff.read_control(s.hub.root, s.hub.server_id)
        assert disk["window"]["sticky_pending"]["domain"] == "127.0.0.1"

        r = await call("browser_control_request", {"reason": "캡차 또"})
        assert r["data"]["window"]["sticky"] is True and r["data"]["window"]["state"] == "headed"
        await _human(s, "take")
        await _wait_holder(s, "human")
        # 창(같은 UA)에서는 앞서 통과한 결과가 그대로 인정된다 — 사람은 확인만 하고 돌려준다
        await s._page.reload()
        await s._page.wait_for_selector("#price")
        await _human(s, "release")
        got = await call("browser_control_wait", {"timeout_s": 20})
        w = got["data"]["window"]
        assert w["state"] == "headed" and w["sticky"] is True and w["reopened"] is False
        assert w.get("sticky_reason")
        assert _headed(s) == 1  # 창 유지
        price = await call("browser_extract", {"selector": "#price"})
        assert "3000원" in json.dumps(price["data"], ensure_ascii=False)
        disk = handoff.read_control(s.hub.root, s.hub.server_id)
        assert disk["window"]["sticky"] is True
        await _egress_ok(s, call, site)


# ------------------------------------------------------------------ (d) 승인 코드 별도 창


async def _show_code(s: BrowserMCPServer, aid: str) -> Dict[str, Any]:
    shown = handoff.read_pending_approval(s.hub.root, s.hub.server_id, aid)
    nonce = write_command(s.hub.root, s.hub.server_id, "show_code", approval_id=aid,
                          action_digest=shown["action_digest"])
    await s._poll_handoff()
    ack = await asyncio.to_thread(handoff.wait_ack, s.hub.root, s.hub.server_id, nonce, 5.0)
    return {"ack": ack, "digest": shown["action_digest"]}


@requires_chromium
@requires_display
async def test_d_approval_code_in_separate_window(site, monkeypatch):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/pay"})
        obs = await call("browser_observe_page", {})
        by = {e["name"]: e["element_id"] for e in obs["data"]["observation"]["elements"]}
        pay = {"element_id": by["결제하기"], "epoch": obs["data"]["observation"]["snapshot_epoch"]}
        r1 = await call("browser_click", dict(pay))
        assert r1["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value
        aid = r1["data"]["approval"]["approval_id"]  # on-demand 는 headless 여도 증표 발급

        got = await _show_code(s, aid)
        assert got["ack"]["ok"], got
        win = s._code_window
        assert win is not None and win.is_open
        body = await win.page.inner_text("body")
        # 사람 역할: 창에 띄운 코드(읽기 쉽게 띄어 씀)를 보고 붙여 입력한다
        code = (await win.page.inner_text("#code")).replace(" ", "")
        assert re.fullmatch(r"\d{6}", code), code
        assert "click" in body and "결제하기" in body and "127.0.0.1" in body  # 승인 내용
        assert _headed(s) == 0  # 페이지는 headless 그대로
        # 별도 창은 에이전트 도구가 닿지 않는다: core 탭·tab_control list 에 없다
        assert all(t.page is not win.page for t in s._core.tabs())
        tabs = await call("browser_tab_control", {"command": "list"})
        assert len(tabs["data"]["tabs"]) == 1
        # 별도 창은 네트워크 차단(로컬 내용만)
        before = len(site.hits)
        with pytest.raises(Exception):
            await win.page.goto(site.url + "/leak", timeout=3000)
        await asyncio.sleep(0.2)
        assert len(site.hits) == before and "/leak" not in site.hits
        # 코드가 떠 있는 동안 화면 캡처 거부(방어 겹침 유지)
        shot = await call("browser_take_screenshot", {})
        assert shot["data"].get("blocked_by") == "approval_code_displayed"

        write_command(s.hub.root, s.hub.server_id, "approve", approval_id=aid,
                      action_digest=got["digest"], code=code)
        await s._poll_handoff()
        assert not win.is_open  # 코드 수락 → 창 닫힘
        r2 = await call("browser_click", {**pay, "approval_id": aid})
        assert r2["success"], r2  # 문서 해시 일치(페이지를 다시 열지 않았다)
        assert await s._page.text_content("#out") == "결제됨"
        r3 = await call("browser_click", {**pay, "approval_id": aid})
        assert not r3["success"] and r3["data"]["approval"]["rejected"]["reason_code"] == "used"


@requires_chromium
@requires_display
async def test_d_code_window_closes_on_deny(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/pay"})
        obs = await call("browser_observe_page", {})
        by = {e["name"]: e["element_id"] for e in obs["data"]["observation"]["elements"]}
        r1 = await call("browser_click", {"element_id": by["결제하기"],
                                          "epoch": obs["data"]["observation"]["snapshot_epoch"]})
        aid = r1["data"]["approval"]["approval_id"]
        got = await _show_code(s, aid)
        assert s._code_window.is_open
        write_command(s.hub.root, s.hub.server_id, "deny", approval_id=aid,
                      action_digest=got["digest"])
        await s._poll_handoff()
        assert not s._code_window.is_open


@requires_chromium
async def test_d_code_window_failure_is_fail_closed(site, monkeypatch):
    """별도 창을 띄우지 못하면 코드 미표시 → 승인 불가(이유 안내)."""
    from interface import code_window

    async def boom(self, *a, **kw):
        raise RuntimeError("no display")

    monkeypatch.setattr(code_window.ApprovalCodeWindow, "_launch", boom)
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/pay"})
        obs = await call("browser_observe_page", {})
        by = {e["name"]: e["element_id"] for e in obs["data"]["observation"]["elements"]}
        r1 = await call("browser_click", {"element_id": by["결제하기"],
                                          "epoch": obs["data"]["observation"]["snapshot_epoch"]})
        aid = r1["data"]["approval"]["approval_id"]
        got = await _show_code(s, aid)
        assert got["ack"]["ok"] is False
        assert "확인 코드" in got["ack"]["message"]
        assert not s.hub.code_displayed()
        assert s._pixels_blocked() is False  # 표시 실패 뒤 캡처 거부도 풀린다


# ------------------------------------------------------------------ (e) 동시 호출·실패 경로


@requires_chromium
@requires_display
async def test_e_concurrent_tool_call_during_switch_waits(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/shop"})
        req, nav = await asyncio.gather(
            call("browser_control_request", {"reason": "캡차"}),
            call("browser_navigate", {"url": site.url + "/pay"}),
        )
        assert req["success"], req
        assert nav["success"], nav  # 전환을 기다렸다가(또는 전환 전에) 정상 실행
        assert len(s._core.tabs()) == 1
        obs = await call("browser_observe_page", {})
        assert obs["success"]
        assert _headed(s) == 1


@requires_chromium
async def test_e_reopen_failure_is_fail_closed_then_recovers(site, monkeypatch):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/shop"})
        core = s._core
        real = core.open_persistent
        calls = {"n": 0}

        async def flaky(*, headless=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("launch failed")
            return await real(headless=True)  # 회복은 headless 로(창 없는 환경에서도)

        monkeypatch.setattr(core, "open_persistent", flaky)
        r = await call("browser_control_request", {"reason": "캡차"})
        assert r["success"] is False
        assert r["data"]["window"]["state"] == "failed"
        assert await _wait_no_procs(core.persistent_profile) == []  # 보호 없는 브라우저 없음
        assert s.hub.status()["requested"] is False  # 창이 없으니 요청도 내지 않았다
        nav = await call("browser_navigate", {"url": site.url + "/pay"})
        assert nav["success"], nav
        assert nav["data"]["window"]["recovered"] is True
        assert s._window.state == "headless"
        await _egress_ok(s, call, site)


@requires_chromium
async def test_e_route_reinstall_failure_closes_browser(site, monkeypatch):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/"})
        guard = s._egress_runtime.guard
        real = guard.install
        state = {"fail": True}

        async def install(ctx):
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("route install failed")
            return await real(ctx)

        monkeypatch.setattr(guard, "install", install)
        out = await s._switch_window(False, "test", force=True)
        assert out["ok"] is False and s._window.state == "failed"
        assert await _wait_no_procs(s._core.persistent_profile) == []
        await _egress_ok(s, call, site)  # 다음 호출이 복구하고, 가드도 다시 선다


@requires_chromium
async def test_e_tab_restore_failure_is_reported(site):
    other = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    dead_url = f"http://127.0.0.1:{other.server_address[1]}/gone"
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/pay"})
        await call("browser_tab_control", {"command": "create", "url": site.url + "/form"})
        await call("browser_tab_control", {"command": "create", "url": dead_url})
        await call("browser_tab_control", {"command": "create", "url": "about:blank"})
        tabs = s._core.tabs()
        s._core.set_active_tab(tabs[1].tab_id)  # 활성 탭 = /form
        s._dispatcher._set_active_page(tabs[1].page, tabs[1].tab_id)
        other.shutdown()
        other.server_close()
        out = await s._switch_window(False, "test", force=True)
        assert out["ok"] is True
        w = out["window"]
        urls = [t["url"] for t in w["tabs"]]
        assert urls[0].endswith("/form")  # 활성 탭 먼저
        assert w["tabs"][0]["active"] is True
        assert any(t["url"].endswith("/pay") and t["restored"] for t in w["tabs"])
        bad = [t for t in w["tabs"] if t["url"] == dead_url]
        assert bad and bad[0]["restored"] is False and bad[0]["error"]
        assert w["skipped"] == 1  # about:blank 은 건너뜀
        assert s._page.url.endswith("/form")
        assert s._core.active_tab_id == w["tabs"][0]["tab_id"]


@requires_chromium
@requires_display
async def test_e_human_closes_window(site):
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/pay"})
        await call("browser_control_request", {"reason": "확인"})
        await _human(s, "take")
        await _wait_holder(s, "human")
        # 사람이 창의 X 버튼 = 창의 탭이 모두 닫힘
        for page in list(s._page.context.pages):
            await page.close()
        await _wait_window(s, "headless")
        st = await call("browser_control_status", {})
        assert st["data"]["control"]["holder"] == "agent"
        assert st["data"]["window"]["state"] == "headless"
        assert st["data"]["window"]["notice"]["reason"] == "window_closed"
        assert _headed(s) == 0
        nav = await call("browser_navigate", {"url": site.url + "/pay"})
        assert nav["success"]
        await _egress_ok(s, call, site)


@requires_chromium
@requires_display
async def test_e_request_expiry_closes_window(site, monkeypatch):
    monkeypatch.setattr(on_demand, "REQUEST_WINDOW_TTL_S", 0.5)
    async with BrowserMCPServer(browser_mode="on-demand") as s:
        call = _inproc(s)
        await call("browser_navigate", {"url": site.url + "/pay"})
        await call("browser_control_request", {"reason": "확인"})
        assert _headed(s) == 1
        await asyncio.sleep(0.7)
        await _wait_window(s, "headless")
        assert s.hub.status()["requested"] is False
        assert _headed(s) == 0


# ------------------------------------------------------------------ (g) 실제 stdio · SIGTERM


def _serve_params(*extra: str):
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "interface.cli", "serve", "--browser", "on-demand", *extra],
        env=dict(os.environ))


async def _stdio(body, *extra: str) -> Any:
    import tempfile

    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryFile(mode="w+") as errlog:
        async with stdio_client(_serve_params(*extra), errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
                    res = await session.call_tool(name, args)
                    return json.loads(res.content[0].text)

                return await body(call)


@requires_chromium
async def test_g_stdio_on_demand_starts_headless(site):
    from browser import serve_profile as sp

    async def body(call):
        nav = await call("browser_navigate", {"url": site.url + "/pay"})
        st = await call("browser_control_status", {})
        return nav, st

    nav, st = await _stdio(body)
    assert nav["success"]
    assert st["data"]["window"] == {"mode": "on-demand", "state": "headless", "sticky": False}
    sid = st["data"]["control"]["server_id"]
    path = sp.profile_root() / f"{sp.EPHEMERAL_PREFIX}{sid}"
    assert not path.exists()  # 정상 종료 → 임시 프로필 삭제
    assert await _wait_no_procs(path) == []


@requires_chromium
@requires_display
async def test_g_stdio_handoff_round_trip(site):
    async def body(call):
        await call("browser_navigate", {"url": site.url + "/shop"})
        r = await call("browser_control_request", {"reason": "캡차"})
        sid = r["data"]["server_id"]
        root = handoff.state_root()
        write_command(root, sid, "take")
        for _ in range(100):
            st = await call("browser_control_status", {})
            if st["data"]["control"]["holder"] == "human":
                break
            await asyncio.sleep(0.05)
        write_command(root, sid, "release")
        got = await call("browser_control_wait", {"timeout_s": 20})
        return r, got

    r, got = await _stdio(body)
    assert r["data"]["window"]["state"] == "headed"
    assert got["data"]["window"]["state"] == "headless" and got["data"]["window"]["reopened"]


@requires_chromium
@requires_display
async def test_e_sigterm_during_switch_leaves_no_orphans(site):
    from browser import serve_profile as sp

    info: Dict[str, Any] = {}

    async def body(call):
        st = await call("browser_control_status", {})
        info["sid"] = st["data"]["control"]["server_id"]
        await call("browser_navigate", {"url": site.url + "/shop"})
        pid = int(info["sid"].split("-")[0])
        task = asyncio.ensure_future(call("browser_control_request", {"reason": "캡차"}))
        await asyncio.sleep(0.25)  # 창 전환 도중(close 16ms + headed open ~500ms)
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("SIGTERM 뒤 serve 가 끝나지 않음")
        task.cancel()
        return pid

    try:
        await _stdio(body)
    except BaseException:  # noqa: BLE001 - 신호로 끝난 serve 의 스트림 정리 오류
        pass
    path = sp.profile_root() / f"{sp.EPHEMERAL_PREFIX}{info['sid']}"
    assert await _wait_no_procs(path) == []  # 고아 Chromium 0
    assert not path.exists()
