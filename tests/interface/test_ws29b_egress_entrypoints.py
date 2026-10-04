"""WS-29b 진입점별 Egress 설치 — serve·run·user-chrome 이 같은 정책을 쓰는지 고정한다.

실측(base): run(예시 에이전트)에는 Egress 가드가 아예 없었고, serve 는 사설 대역 전체를
열었다(allow_loopback=True). 브라우저 없이 가짜로 인자 전달만 본다.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest

from interface import cli


# -- serve 플래그 ---------------------------------------------------------------


def _serve_kwargs(monkeypatch, argv: List[str]) -> Dict[str, Any]:
    from interface import mcp_server

    got: Dict[str, Any] = {}

    async def fake_run_stdio(**kw: Any) -> None:
        got.update(kw)

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    assert cli.main(["serve", *argv]) == 0
    return got


def test_serve_default_keeps_private_network_closed(monkeypatch):
    kw = _serve_kwargs(monkeypatch, [])
    assert kw["allow_private_network"] is False
    assert kw["block_loopback"] is False


def test_serve_flags_are_passed(monkeypatch):
    kw = _serve_kwargs(monkeypatch, ["--allow-private-network", "--block-loopback"])
    assert kw["allow_private_network"] is True
    assert kw["block_loopback"] is True


def test_run_stdio_passes_egress_options_to_create_server(monkeypatch):
    import contextlib

    import mcp.server.stdio as stdio_mod

    from interface import mcp_server

    created: Dict[str, Any] = {}

    class _S:
        def create_initialization_options(self) -> Any:
            return None

        async def run(self, *a: Any) -> None:
            return None

    class _B:
        async def close(self) -> None:
            return None

    def fake_create_server(**kw: Any):
        created.update(kw)
        return _S(), _B()

    @contextlib.asynccontextmanager
    async def fake_stdio():
        yield (None, None)

    monkeypatch.setattr(mcp_server, "create_server", fake_create_server)
    monkeypatch.setattr(stdio_mod, "stdio_server", fake_stdio)
    asyncio.run(mcp_server.run_stdio(allow_private_network=True, block_loopback=True))
    assert created["allow_private_network"] is True
    assert created["block_loopback"] is True


# -- serve: 코어가 프록시에 묶여 뜨는가 --------------------------------------------


class _RecordingCore:
    seen: List[Dict[str, Any]] = []

    def __init__(self, **kw: Any) -> None:
        _RecordingCore.seen.append(kw)

    async def start(self) -> Any:
        raise RuntimeError("stop-here")


@pytest.mark.parametrize("mode", ["headless", "human", "user-chrome"])
async def test_server_start_gives_core_an_egress_runtime(monkeypatch, tmp_path, mode):
    import browser

    from interface.mcp_server import BrowserMCPServer

    _RecordingCore.seen = []
    monkeypatch.setattr(browser, "BrowserCore", _RecordingCore)
    kw: Dict[str, Any] = {"browser_mode": mode}
    if mode == "user-chrome":
        kw["chrome_profile"] = tmp_path / "p"
    srv = BrowserMCPServer(allow_private_network=True, **kw)
    with pytest.raises(RuntimeError, match="stop-here"):
        await srv.start()
    egress = _RecordingCore.seen[-1]["egress"]
    assert egress is not None
    assert egress.guard.allow_private_network is True
    assert egress.guard.allow_loopback is True
    # user-chrome 은 Chrome 명령줄로 자격증명을 못 줘 토큰 없는 프록시
    assert (egress.tokenless is True) == (mode == "user-chrome")
    # 시작 실패면 프록시도 닫는다(고아 리스너 없음)
    assert egress.proxy is None
    assert srv._egress_runtime is None


async def test_core_launch_uses_proxy_and_flags(monkeypatch):
    """BrowserCore 가 Playwright Chromium 을 프록시·QUIC 끔·WebRTC 정책으로 띄운다."""
    import playwright.async_api as pw_api

    from browser import BrowserCore
    from security.egress_runtime import EgressRuntime

    calls: List[Dict[str, Any]] = []

    class _Chromium:
        async def launch(self, **kw: Any) -> Any:
            calls.append(kw)
            return object()

    class _PW:
        chromium = _Chromium()

        async def stop(self) -> None:
            return None

    class _Starter:
        async def start(self) -> Any:
            return _PW()

    monkeypatch.setattr(pw_api, "async_playwright", lambda: _Starter())
    rt = await EgressRuntime().start()
    try:
        await BrowserCore(egress=rt).start()
    finally:
        await rt.close()
    kw = calls[-1]
    assert kw["proxy"]["server"].startswith("http://127.0.0.1:")
    assert kw["proxy"]["username"] and kw["proxy"]["password"]
    assert "--disable-quic" in kw["args"]
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in kw["args"]


async def test_core_user_chrome_launch_gets_proxy_args(monkeypatch, tmp_path):
    from browser import BrowserCore, user_chrome
    from security.egress_runtime import EgressRuntime

    got: Dict[str, Any] = {}

    async def fake_launch(**kw: Any) -> Any:
        got.update(kw)
        raise RuntimeError("stop-here")

    monkeypatch.setattr(user_chrome, "launch_user_chrome", fake_launch)
    rt = await EgressRuntime(tokenless=True).start()
    try:
        with pytest.raises(RuntimeError, match="stop-here"):
            await BrowserCore(browser_mode="user-chrome", chrome_profile=tmp_path / "p",
                              egress=rt).start()
    finally:
        await rt.close()
    args = got["extra_args"]
    assert any(a.startswith("--proxy-server=http://127.0.0.1:") for a in args)
    assert "--proxy-bypass-list=<-loopback>" in args
    assert "--disable-quic" in args


def test_launch_user_chrome_appends_extra_args(monkeypatch, tmp_path):
    """launch_user_chrome(extra_args=…) 가 Chrome 명령줄에 그대로 붙는다."""
    from browser import user_chrome

    seen: List[List[str]] = []

    class _P:
        returncode = 1

        def poll(self) -> int:
            return 1

    def fake_popen(argv, **kw):
        seen.append(list(argv))
        return _P()

    monkeypatch.setattr(user_chrome.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(user_chrome.UserChrome, "close", lambda self, timeout=10.0: None)
    with pytest.raises(RuntimeError):
        asyncio.run(user_chrome.launch_user_chrome(
            profile_dir=tmp_path / "prof", chrome_path=Path("/bin/false"),
            extra_args=["--proxy-server=http://127.0.0.1:1"],
        ))
    assert "--proxy-server=http://127.0.0.1:1" in seen[0]


# -- run: 같은 정책이 설치되는가 --------------------------------------------------


def _run_ns(**kw: Any) -> argparse.Namespace:
    base = dict(user_chrome=False, human=False, headed=False, chrome_profile="", keep_open=False,
                url="http://127.0.0.1:1/", goal="g", max_steps=1, handoff=False,
                handoff_wait=1.0, handoff_file="/nonexistent", no_answer=True, out="",
                allow_private_network=False, block_loopback=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_run_parser_has_egress_flags():
    parser = cli._build_parser()
    a = parser.parse_args(["run", "--url", "http://x/", "--goal", "g",
                           "--allow-private-network", "--block-loopback"])
    assert a.allow_private_network is True and a.block_loopback is True
    b = parser.parse_args(["run", "--url", "http://x/", "--goal", "g"])
    assert b.allow_private_network is False and b.block_loopback is False


@pytest.mark.parametrize("flags", [{}, {"allow_private_network": True, "block_loopback": True}])
def test_run_opens_browser_with_egress_and_installs_route(monkeypatch, flags):
    from interface import run_cli

    seen: Dict[str, Any] = {}

    class _Ctx:
        routes: List[str] = []

        async def route(self, pattern: str, handler: Any) -> None:
            _Ctx.routes.append(pattern)

        async def close(self) -> None:
            return None

        async def new_cdp_session(self, page: Any) -> Any:
            raise RuntimeError("stop-run")

    class _Page:
        url = "about:blank"

        async def goto(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("stop-run")

    class _Br:
        async def close(self) -> None:
            return None

    async def fake_open(pw, args, record, **kw):
        seen.update(kw)
        seen["proxy_running"] = kw["egress"].proxy is not None and kw["egress"].proxy.running
        return _Br(), _Ctx(), _Page(), None

    async def fake_body(page: Any) -> str:
        return ""

    monkeypatch.setattr(run_cli, "_open_browser", fake_open)
    monkeypatch.setattr(run_cli, "_read_body_text", fake_body)
    monkeypatch.setattr(run_cli, "_load_config", lambda: object())
    rec = asyncio.run(run_cli.run_goal(_run_ns(**flags)))
    eg = seen["egress"]
    assert seen["proxy_running"] is True
    assert eg.guard.allow_private_network is bool(flags.get("allow_private_network"))
    assert eg.guard.allow_loopback is not bool(flags.get("block_loopback"))
    assert _Ctx.routes == ["**/*"]
    assert eg.proxy is None  # 끝나면 프록시를 닫는다
    assert "egress" in rec


def test_open_browser_with_egress_passes_proxy_to_launch():
    from interface import run_cli
    from security.egress_runtime import EgressRuntime

    log: List[Any] = []

    class _Ctx:
        async def new_page(self) -> str:
            return "PAGE"

    class _B:
        async def new_context(self, **kw: Any) -> Any:
            return _Ctx()

    class _C:
        async def launch(self, **kw: Any) -> Any:
            log.append(kw)
            return _B()

    class _PW:
        chromium = _C()

    async def go() -> None:
        rt = await EgressRuntime().start()
        try:
            await run_cli._open_browser(_PW(), _run_ns(human=True), {}, egress=rt)
            await run_cli._open_browser(_PW(), _run_ns(), {}, egress=rt)
        finally:
            await rt.close()

    asyncio.run(go())
    assert log[0]["headless"] is False and "proxy" in log[0]
    assert log[1]["headless"] is True and "proxy" in log[1]
    assert "--disable-quic" in log[1]["args"]


def test_user_chrome_writes_webrtc_policy_pref(monkeypatch, tmp_path):
    """설치된 Chrome(154 실측)은 --force-webrtc-ip-handling-policy 플래그를 무시하고 STUN UDP 를
    보냈다 — 전용 프로필의 webrtc.ip_handling_policy 설정으로 막는다(기존 설정은 보존)."""
    import json as _json

    from browser import user_chrome

    prof = tmp_path / "prof"
    (prof / "Default").mkdir(parents=True)
    (prof / "Default" / "Preferences").write_text(_json.dumps({"keep": {"x": 1}}))

    class _P:
        returncode = 1

        def poll(self) -> int:
            return 1

    monkeypatch.setattr(user_chrome.subprocess, "Popen", lambda argv, **kw: _P())
    monkeypatch.setattr(user_chrome.UserChrome, "close", lambda self, timeout=10.0: None)
    with pytest.raises(RuntimeError):
        asyncio.run(user_chrome.launch_user_chrome(
            profile_dir=prof, chrome_path=Path("/bin/false"),
            extra_args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
        ))
    prefs = _json.loads((prof / "Default" / "Preferences").read_text())
    assert prefs["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
    assert prefs["keep"] == {"x": 1}


def test_user_chrome_without_egress_does_not_touch_prefs(monkeypatch, tmp_path):
    from browser import user_chrome

    prof = tmp_path / "prof"

    class _P:
        returncode = 1

        def poll(self) -> int:
            return 1

    monkeypatch.setattr(user_chrome.subprocess, "Popen", lambda argv, **kw: _P())
    monkeypatch.setattr(user_chrome.UserChrome, "close", lambda self, timeout=10.0: None)
    with pytest.raises(RuntimeError):
        asyncio.run(user_chrome.launch_user_chrome(profile_dir=prof, chrome_path=Path("/bin/false")))
    assert not (prof / "Default" / "Preferences").exists()
