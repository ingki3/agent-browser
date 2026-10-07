"""WS-32 실 브라우저 E2E (로컬 Mock 로그인 사이트 + 실제 Chromium).

(a) serve --profile t1 로 로그인(Mock 폼 제출로 쿠키 받기) → 서버 종료 → 같은 프로필로 재시작 →
    보호 페이지가 로그인 상태
(b) --profile 없이 재시작하면 비로그인
(c) 같은 프로필 동시 사용 거부(서버 id 안내)
(d) 영속 모드에서도 사설망 차단·검증 프록시 경유·승인 게이트가 그대로 동작
(e) MCP 실제 stdio 경로(ClientSession + 하위 프로세스 serve)로 --profile 기동 — 정상 종료(stdin EOF),
    SIGTERM, SIGINT 뒤에도 로그인 쿠키가 남는지
세션 쿠키(만료 없음)는 Chromium 이 재시작 때 버린다 — 그 한계도 여기서 고정한다(README).

실제 공개 사이트 로그인 없음. 프로필 폴더는 conftest 가 임시 디렉터리로 돌린다.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

import pytest

from browser import serve_profile as sp
from contracts import ErrorCode
from interface import handoff
from interface.mcp_server import BrowserMCPServer, envelope_dict

from test_run_cli import requires_chromium
from ws29_helpers import srv_approve

LOGIN = """<!doctype html><meta charset=utf-8><title>로그인</title>
<form method=post action=/login>
<input aria-label='아이디' name=u><input aria-label='비밀번호' type=password name=p>
<button type=submit>로그인</button></form>"""
PAY = """<!doctype html><meta charset=utf-8><title>결제</title><p id=out>대기</p>
<button onclick="document.getElementById('out').textContent='결제됨'">결제하기</button>"""


@pytest.fixture
def login_site():
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
            # 사이트가 준 그대로: 영속 로그인 쿠키(Max-Age) + 세션 쿠키(만료 없음)
            self._send(303, "", {"Location": "/protected", "Set-Cookie": [
                "auth=ok; Max-Age=86400; Path=/; HttpOnly", "sess=ok; Path=/"]})

        def do_GET(self):  # noqa: N802
            cookies = self.headers.get("Cookie") or ""
            if self.path.startswith("/login"):
                self._send(200, LOGIN)
            elif self.path.startswith("/protected"):
                state = "로그인됨" if "auth=ok" in cookies else "로그인 필요"
                sess = "세션쿠키 있음" if "sess=ok" in cookies else "세션쿠키 없음"
                self._send(200, f"<!doctype html><meta charset=utf-8><title>내 정보</title>"
                                f"<p id=state>{state}</p><p id=sess>{sess}</p>")
            elif self.path.startswith("/pay"):
                self._send(200, PAY)
            else:
                self._send(200, "<p>home</p>")

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ 에이전트 흐름(두 경로 공용)


async def _login(call, site: str) -> None:
    """에이전트 역할: 로그인 폼을 관찰 → 칸 입력 → 제출(Mock 값, 실제 계정 아님)."""
    nav = await call("browser_navigate", {"url": site + "/login"})
    assert nav["success"], nav
    obs = await call("browser_observe_page", {})
    by = {e["name"]: e["element_id"] for e in obs["data"]["observation"]["elements"]}
    ep = obs["data"]["observation"]["snapshot_epoch"]
    for name, text in (("아이디", "mock-user"), ("비밀번호", "mock-pass")):
        r = await call("browser_type_text", {"element_id": by[name], "epoch": ep, "text": text})
        assert r["success"], r
    r = await call("browser_click", {"element_id": by["로그인"], "epoch": ep})
    assert r["success"], r


async def _state(call, site: str) -> str:
    nav = await call("browser_navigate", {"url": site + "/protected"})
    assert nav["success"], nav
    got = await call("browser_extract", {"selector": "body"})
    return json.dumps(got.get("data"), ensure_ascii=False)


def _inproc(s: BrowserMCPServer):
    async def call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name.startswith("browser_control") or name.startswith("browser_approval"):
            return await s.call_server_tool(name, args)
        return envelope_dict(await s.call_tool(name, args))
    return call


# ------------------------------------------------------------------ (a)(b)(c) 서버 직접


@requires_chromium
async def test_a_login_survives_restart_and_b_no_profile_is_logged_out(login_site):
    async with BrowserMCPServer(profile="t1") as s1:
        call = _inproc(s1)
        await _login(call, login_site)
        assert "로그인됨" in await _state(call, login_site)
        assert "세션쿠키 있음" in await _state(call, login_site)
    async with BrowserMCPServer(profile="t1") as s2:
        state = await _state(_inproc(s2), login_site)
        assert "로그인됨" in state  # (a) 영속 쿠키는 남는다
        # 한계 고정: 만료 없는 세션 쿠키는 Chromium 이 재시작 때 버린다(README '로그인 유지')
        assert "세션쿠키 없음" in state
    async with BrowserMCPServer() as s3:
        assert "로그인 필요" in await _state(_inproc(s3), login_site)  # (b)


@requires_chromium
async def test_c_same_profile_concurrent_use_refused(login_site):
    async with BrowserMCPServer(profile="t1") as s1:
        await _inproc(s1)("browser_navigate", {"url": login_site + "/"})
        s2 = BrowserMCPServer(profile="t1")
        with pytest.raises(sp.ProfileInUseError) as ei:
            await s2.start()
        assert s1.hub.server_id in str(ei.value)
        assert s2._core is None and s2._egress_runtime is None
        rows = {r["name"]: r for r in sp.list_profiles()}
        assert rows["t1"]["in_use"] and rows["t1"]["server_id"] == s1.hub.server_id
        await s2.close()
    assert sp.holder("t1") is None


@requires_chromium
@pytest.mark.parametrize("profile", [None, "t1"], ids=["ephemeral", "persistent"])
async def test_d_persistent_mode_keeps_egress_and_approval_gate(login_site, monkeypatch, profile):
    """기존(빈 컨텍스트)과 영속 모드가 같은 보안 경로를 갖는다 — 진입점 대조."""
    monkeypatch.setattr(handoff, "_stdin_is_tty", lambda: False)
    async with BrowserMCPServer(profile=profile) as s:
        call = _inproc(s)
        before = s._egress_runtime.proxy.handled
        assert (await call("browser_navigate", {"url": login_site + "/"}))["success"]
        assert s._egress_runtime.proxy.handled > before  # 브라우저가 검증 프록시를 거친다
        # 사설망 차단(WS-29b 기본). route 가드가 조기 차단하므로 요청이 프록시에 닿지 않는다.
        at_proxy = s._egress_runtime.proxy.handled
        blocked = await call("browser_navigate", {"url": "http://10.0.0.1/"})
        assert not blocked["success"]
        assert blocked["data"]["egress"]["category"] == "private", blocked
        assert s._egress_runtime.proxy.handled == at_proxy, "route 가드가 설치되지 않음"
        # 승인 게이트(창이 있다고 두고 오버레이 문구 = 사람의 눈)
        s._human_can_see = lambda: True
        s.banners = []
        real = s._set_banner

        async def spy(text: Any) -> bool:
            s.banners.append(text)
            return await real(text)

        s._set_banner = spy
        await call("browser_navigate", {"url": login_site + "/pay"})
        obs = await call("browser_observe_page", {})
        by = {e["name"]: e["element_id"] for e in obs["data"]["observation"]["elements"]}
        pay = {"element_id": by["결제하기"], "epoch": obs["data"]["observation"]["snapshot_epoch"]}
        r1 = await call("browser_click", dict(pay))
        assert r1["error_code"] == ErrorCode.HITL_UNATTENDED_BLOCKED.value
        aid = r1["data"]["approval"]["approval_id"]
        assert await s._page.text_content("#out") == "대기"
        await srv_approve(s, aid)
        r2 = await call("browser_click", {**pay, "approval_id": aid})
        assert r2["success"], r2
        assert await s._page.text_content("#out") == "결제됨"


# ------------------------------------------------------------------ (e) 실제 stdio


def _serve_params(profile: str = None):
    from mcp.client.stdio import StdioServerParameters

    args = ["-m", "interface.cli", "serve"] + (["--profile", profile] if profile else [])
    env = {k: v for k, v in os.environ.items()}
    return StdioServerParameters(command=sys.executable, args=args, env=env)


async def _stdio(profile: str, body, *, kill: int = 0) -> Any:
    """하위 프로세스 serve 와 ClientSession 으로 붙어 body(call) 를 돌린다.

    kill 이 신호 번호면 body 뒤 serve 프로세스에 그 신호를 보내고 끝날 때까지 기다린다(비정상 종료).
    """
    import tempfile

    # serve stderr 는 실제 파일로 받는다 — sys.stderr 에 fileno 가 없는 환경(백그라운드 실행 등)에서도 돈다.
    with tempfile.TemporaryFile(mode="w+") as errlog:
        return await _stdio_inner(profile, body, kill, errlog)


async def _stdio_inner(profile: str, body, kill: int, errlog: Any) -> Any:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    out: Dict[str, Any] = {}
    async with stdio_client(_serve_params(profile), errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
                res = await session.call_tool(name, args)
                return json.loads(res.content[0].text)

            out["value"] = await body(call)
            st = await call("browser_control_status", {})
            out["status"] = st
            pid = int(st["data"]["control"]["server_id"].split("-")[0])
            out["pid"] = pid
            if kill:
                os.kill(pid, kill)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("신호 뒤 serve 가 끝나지 않음")
                return out
    return out


async def _stdio_safe(profile: str, body, *, kill: int = 0) -> Dict[str, Any]:
    try:
        return await _stdio(profile, body, kill=kill)
    except BaseException as exc:  # noqa: BLE001 - 신호로 끝난 serve 의 스트림 정리 오류
        if not kill:
            raise
        return {"closed_with": repr(exc)}


@requires_chromium
async def test_e_stdio_profile_login_persists_after_normal_exit(login_site):
    async def login(call):
        await _login(call, login_site)
        return await _state(call, login_site)

    first = await _stdio("t1", login)
    assert "로그인됨" in first["value"]
    assert first["status"]["data"]["profile"] == {"name": "t1", "persistent": True}
    assert sp.holder("t1") is None  # 정상 종료 뒤 잠금 반납

    second = await _stdio("t1", lambda call: _state(call, login_site))
    assert "로그인됨" in second["value"]
    third = await _stdio(None, lambda call: _state(call, login_site))
    assert "로그인 필요" in third["value"]


@requires_chromium
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_e_stdio_profile_login_persists_after_signal(login_site, signum):
    name = f"sig{int(signum)}"

    async def login(call):
        await _login(call, login_site)
        return await _state(call, login_site)

    await _stdio_safe(name, login, kill=signum)
    deadline = time.monotonic() + 10
    while sp.holder(name) is not None and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert sp.holder(name) is None  # 잠금이 풀렸다(프로세스 종료)
    again = await _stdio(name, lambda call: _state(call, login_site))
    assert "로그인됨" in again["value"]


@requires_chromium
async def test_e_stdio_second_serve_same_profile_exits_2(login_site):
    import subprocess

    async def hold(call):
        await call("browser_navigate", {"url": login_site + "/"})
        st = await call("browser_control_status", {})
        proc = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-m", "interface.cli", "serve", "--profile", "t1"],
            input="", capture_output=True, text=True, timeout=60, env=dict(os.environ))
        return {"code": proc.returncode, "err": proc.stderr,
                "sid": st["data"]["control"]["server_id"]}

    got = (await _stdio("t1", hold))["value"]
    assert got["code"] == 2, got
    assert got["sid"] in got["err"] and "t1" in got["err"]
