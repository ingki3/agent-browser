"""Real headed Chromium and CLI subprocess, exclusively on a local Mock site."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time

import pytest

from browser import serve_profile
from interface.mcp_server import BrowserMCPServer
from interface.cli import main
from interface import login_cli
from test_ws34_on_demand_e2e import site, requires_chromium, requires_display, _headed, _wait_window, _procs_for


async def eventually(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline
        await asyncio.sleep(.05)


async def cli(*args):
    return await asyncio.create_subprocess_exec(sys.executable, "-m", "interface.cli", *args,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


@requires_chromium
@pytest.mark.parametrize("host", ["a$(id).invalid", "a;id.invalid", "a`id`.invalid", "a'b.invalid", "a!b.invalid"])
async def test_probe_login_hint_host_metachars(site, host):
    # Synthetic navigation is fulfilled in-process: Chromium parses these hosts,
    # but no public DNS or HTTP request is made.
    async with BrowserMCPServer(profile="mock") as s:
        await s.call_tool("browser_navigate", {"url": site.url + "/pay"})
        async def fulfill(route):
            await route.fulfill(body='<meta charset="utf-8"><button>결제하기</button>', content_type="text/html; charset=utf-8")
        await s._page.route("**/*", fulfill)
        await s._page.goto(f"http://{host}/")
        assert host in s._page.url
        control = await s.call_server_tool("browser_control_request", {"reason": "로그인 필요"})
        blocked = await s.call_tool("browser_click", {"selector": "button"})
        assert not blocked.success and blocked.error_code.value == "E_HITL_UNATTENDED_BLOCKED"
        for data in (control["data"], blocked.data):
            hint = data["login_hint"]
            assert not any(char in hint for char in "$();'!`"), hint
            assert "<페이지 주소>" in hint
        assert host not in control.get("how_to_respond", "")


@requires_chromium
@requires_display
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGKILL])
async def test_login_cli_signal_recovery(site, sig):
    async with BrowserMCPServer(profile="mock", browser_mode="on-demand") as s:
        await s.call_tool("browser_navigate", {"url": site.url + "/"})
        timeout = 5 if sig == signal.SIGKILL else 60
        p = await cli("login", site.url + "/login", "--timeout", str(timeout))
        try:
            await eventually(lambda: s.hub.login and s.hub.login.status == "ready")
            assert s.hub.holder == "human"
            deadline = s.hub.login.deadline
            p.send_signal(sig)
            await asyncio.wait_for(p.communicate(), 8)
            if sig == signal.SIGKILL:
                assert p.returncode == -signal.SIGKILL
                await eventually(lambda: s.hub.holder == "agent" and s.hub.login is None,
                                 max(0, deadline - time.monotonic()) + 3)
            else:
                assert p.returncode == 128 + sig
                await eventually(lambda: s.hub.holder == "agent" and s.hub.login is None, 3)
                assert time.monotonic() < deadline
            await _wait_window(s, "headless")
            assert (await s.call_tool("browser_navigate", {"url": site.url + "/"})).success
        finally:
            if p.returncode is None:
                p.kill()
                await p.communicate()


@requires_chromium
@requires_display
async def test_login_window_x_allows_next_login(site):
    async with BrowserMCPServer(profile="mock", browser_mode="on-demand") as s:
        await s.call_tool("browser_navigate", {"url": site.url + "/"})
        for close_window in (True, False):
            p = await cli("login", site.url + "/login", "--timeout", "30")
            try:
                await eventually(lambda: s.hub.login and s.hub.login.status == "ready")
                if close_window:
                    for tab in list(s._core.tabs()):
                        await tab.page.close()
                else:
                    p.stdin.write(b"\n")
                    await p.stdin.drain()
                stdout, stderr = await asyncio.wait_for(p.communicate(), 8)
                assert p.returncode == 0, (stdout, stderr)
                await eventually(lambda: s.hub.holder == "agent" and s.hub.login is None)
                await _wait_window(s, "headless")
            finally:
                if p.returncode is None:
                    p.kill()
                    await p.communicate()


@requires_chromium
@requires_display
@pytest.mark.parametrize("ending", ["enter", "close", "interrupt", "timeout"])
async def test_live_server_login(site, ending, capsys):
    async with BrowserMCPServer(profile="mock", browser_mode="on-demand") as s:
        await s.call_tool("browser_navigate", {"url": site.url + "/"})
        before = len(s._core.tabs())
        args = ["login", site.url + "/login"]
        if ending == "timeout":
            args += ["--timeout", "6"]
        p = await cli(*args)
        try:
            await eventually(lambda: s.hub.login and s.hub.login.status == "ready")
            assert s.hub.holder == "human" and _headed(s) == 1
            assert len(s._core.tabs()) == before + 1
            for tool in ("browser_observe_page", "browser_take_screenshot"):
                result = await s.call_tool(tool, {})
                assert not result.success, tool
                assert result.error_code.value == "E_HITL_UNATTENDED_BLOCKED"
            page = next(t.page for t in s._core.tabs() if t.page.url.endswith("/login"))
            assert s._control_banner and "로그인" in s._control_banner
            assert s._banner and s._banner[2] is page
            await page.locator("button[type=submit]").click()
            await page.wait_for_url("**/shop")
            await page.locator("#solve").click()
            await page.wait_for_selector("#who")
            assert await asyncio.to_thread(main, ["profile", "sites", "mock", "--json"]) == 0
            out = capsys.readouterr()
            assert "127.0.0.1" in out.out
            assert '"auth"' not in out.out and '"sess"' not in out.out
            assert "auth=ok" not in out.out + out.err
            if ending == "enter":
                p.stdin.write(b"\n")
                await p.stdin.drain()
            elif ending == "close":
                await page.close()
            elif ending == "interrupt":
                p.send_signal(signal.SIGINT)
            stdout, stderr = await asyncio.wait_for(p.communicate(), 35)
            assert p.returncode == {"enter": 0, "close": 0, "interrupt": 130, "timeout": 1}[ending], (stdout, stderr)
            await eventually(lambda: s.hub.holder == "agent")
            await _wait_window(s, "headless")
            assert _headed(s) == 0
            nav = await s.call_tool("browser_navigate", {"url": site.url + "/shop"})
            assert nav.success
            who = await s.call_tool("browser_extract", {"selector": "#who"})
            assert "로그인됨" in json.dumps(who.data, ensure_ascii=False)
        finally:
            if p.returncode is None:
                p.send_signal(signal.SIGINT)
                await asyncio.wait_for(p.communicate(), 35)
    assert main(["profile", "sites", "mock", "--json"]) == 0
    assert "127.0.0.1" in capsys.readouterr().out


@requires_chromium
@requires_display
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
async def test_direct_login_signal_unlocks(site, sig):
    p = await cli("login", site.url + "/login", "--profile", "mock", "--timeout", "60")
    try:
        line = await asyncio.wait_for(p.stdout.readline(), 15)
        assert "창에서 로그인" in line.decode()
        path = serve_profile.profile_dir("mock")
        assert serve_profile.holder("mock") is not None
        assert any(row["headed"] for row in _procs_for(path))
        p.send_signal(sig)
        await asyncio.wait_for(p.communicate(), 8)
        assert p.returncode == 128 + sig
        assert serve_profile.holder("mock") is None
        await eventually(lambda: not _procs_for(path), 3)
    finally:
        if p.returncode is None:
            p.kill()
            await p.communicate()


@requires_chromium
@requires_display
async def test_direct_login_persists_and_unlocks(site, monkeypatch):
    from browser.core import BrowserCore
    real = BrowserCore.new_tab

    async def new_tab(core, name, url=None):
        tab = await real(core, name, url)
        assert serve_profile.holder("mock") is not None
        await tab.page.locator("button[type=submit]").click()
        await tab.page.wait_for_url("**/shop")
        await tab.page.locator("#solve").click()
        await tab.page.wait_for_selector("#who")
        return tab

    async def done(*args):
        return

    monkeypatch.setattr(BrowserCore, "new_tab", new_tab)
    monkeypatch.setattr(login_cli, "wait_for_done", done)
    assert await login_cli.direct_login("mock", site.url + "/login", 20) == 0
    assert serve_profile.holder("mock") is None
    monkeypatch.setattr(BrowserCore, "new_tab", real)
    async with BrowserMCPServer(profile="mock") as s:
        assert (await s.call_tool("browser_navigate", {"url": site.url + "/shop"})).success
        result = await s.call_tool("browser_extract", {"selector": "#who"})
        assert "로그인됨" in json.dumps(result.data, ensure_ascii=False)


@requires_chromium
@requires_display
async def test_login_waits_for_inflight_and_cancel_pending(site):
    async with BrowserMCPServer(profile="mock", browser_mode="on-demand") as s:
        await s.call_tool("browser_navigate", {"url": site.url + "/"})
        # Actual product call remains in flight while human command arrives.
        pending = asyncio.create_task(s.call_tool("browser_wait_for", {"condition": "selector", "selector": "#never", "timeout_ms": 10000}))
        await eventually(lambda: s._inflight > 0)
        p = await cli("login", site.url + "/login", "--timeout", ".4")
        try:
            stdout, stderr = await asyncio.wait_for(p.communicate(), 35)
            assert p.returncode != 0
            assert not s.hub.login
            await pending
            await asyncio.sleep(.2)
            assert s.hub.holder == "agent"
            assert s._window.state == "headless"
            assert not any(t.page.url.endswith("/login") for t in s._core.tabs())
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if p.returncode is None:
                p.kill()
                await p.communicate()


@requires_chromium
@requires_display
async def test_server_egress_blocks_login(site):
    async with BrowserMCPServer(profile="mock", browser_mode="on-demand", block_loopback=True) as s:
        p = await cli("login", site.url + "/login", "--timeout", "10")
        stdout, stderr = await asyncio.wait_for(p.communicate(), 35)
        assert p.returncode != 0
        assert not any(hit.startswith("/login") for hit in site.hits)
        await eventually(lambda: s.hub.holder == "agent")


@requires_chromium
@requires_display
async def test_login_drains_existing_product_call(site):
    async with BrowserMCPServer(profile="mock", browser_mode="on-demand") as s:
        await s.call_tool("browser_navigate", {"url": site.url + "/"})
        await s._page.evaluate("setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<p id=arrived>ready</p>'),1800)")
        pending = asyncio.create_task(s.call_tool("browser_wait_for", {"condition": "selector", "selector": "#arrived", "timeout_ms": 5000}))
        await eventually(lambda: s._inflight > 0)
        p = await cli("login", site.url + "/login", "--timeout", "20")
        try:
            await eventually(lambda: s.hub.login is not None)
            assert not pending.done()
            assert s._window.state == "headless"
            assert not any(t.page.url.endswith("/login") for t in s._core.tabs())
            assert (await pending).success
            await eventually(lambda: s.hub.login and s.hub.login.status == "ready")
            p.stdin.write(b"\n")
            await p.stdin.drain()
            await asyncio.wait_for(p.communicate(), 35)
            assert p.returncode == 0
            await _wait_window(s, "headless")
            assert s.hub.holder == "agent"
        finally:
            if p.returncode is None:
                p.send_signal(signal.SIGINT)
                await asyncio.wait_for(p.communicate(), 35)


@requires_chromium
async def test_unattended_hint_without_profile(site):
    async with BrowserMCPServer() as s:
        await s.call_tool("browser_navigate", {"url": site.url + "/pay"})
        result = await s.call_tool("browser_click", {"selector": "button"})
        assert not result.success
        assert "--profile 로 서버를 띄워야" in result.data["login_hint"]


@requires_chromium
@requires_display
async def test_stdio_login_before_first_tool(site, tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from interface import handoff

    params = StdioServerParameters(command=sys.executable,
             args=["-m", "interface.cli", "serve", "--browser", "on-demand", "--profile", "mock"], env=dict(os.environ))
    with (tmp_path / "stdio-error.log").open("w+") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                info = handoff.list_servers()[0]
                path = serve_profile.profile_dir("mock")
                assert not _procs_for(path), "serve starts its browser lazily, after initialize"
                p = await cli("login", site.url + "/login", "--timeout", "8")
                try:
                    await eventually(lambda: (handoff.read_control(None, info["server_id"]) or {}).get("login", {}).get("status") == "ready", 12)
                    state = handoff.read_control(None, info["server_id"])
                    assert state["holder"] == "human" and state["window"]["state"] == "headed"
                    assert any(row["headed"] for row in _procs_for(path))
                    p.stdin.write(b"\n")
                    await p.stdin.drain()
                    stdout, stderr = await asyncio.wait_for(p.communicate(), 35)
                    assert p.returncode == 0, (stdout, stderr)
                    res = await session.call_tool("browser_navigate", {"url": site.url + "/"})
                    out = json.loads(res.content[0].text)
                    assert out["success"]
                    assert out["data"]["window"]["state"] == "headless"
                finally:
                    if p.returncode is None:
                        p.send_signal(signal.SIGINT)
                        await asyncio.wait_for(p.communicate(), 35)
