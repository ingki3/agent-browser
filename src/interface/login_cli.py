"""Human-only login preparation in a named serve profile (WS-42)."""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import secrets
import select
import signal
import sys
import time
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

from browser import serve_profile
from interface import handoff

MAX_LOGIN_TIMEOUT_S = 3600


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        valid = (parts.scheme in ("http", "https") and parts.hostname
                 and not parts.username and not parts.password
                 and not any(ord(c) < 32 for c in url))
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("로그인 URL 은 사용자 정보 없는 http/https 주소만 됩니다.")
    return url


def display_available() -> bool:
    return sys.platform != "linux" or bool(os.environ.get("DISPLAY"))


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("login", help="맥 앞에서 serve 프로필에 미리 로그인합니다.")
    p.add_argument("url", metavar="URL", help="창에서 로그인할 http/https 주소")
    p.add_argument("--profile", metavar="NAME", help="로그인을 유지할 프로필 이름(서버가 하나면 생략 가능)")
    p.add_argument("--server", metavar="ID", help="로그인 창을 요청할 실행 중인 서버 ID")
    p.add_argument("--timeout", type=float, default=600, metavar="SEC",
                   help="로그인 대기 초(기본 600s, 최대 3600s); 만료 시 조작권 반납")


def _enter_ready() -> bool:
    """Nonblocking stdin: EOF is not human confirmation; closing the tab still works."""
    try:
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(readable and b"\n" in os.read(sys.stdin.fileno(), 4096))
    except (OSError, ValueError, TypeError):
        return False


async def wait_for_done(closed: Callable[[], bool], deadline: float) -> None:
    print("창에서 로그인('로그인 상태 유지' 체크)한 뒤 Enter를 누르거나 그 탭을 닫으세요.", flush=True)
    while not closed():
        if _enter_ready():
            return
        if time.monotonic() >= deadline:
            raise asyncio.TimeoutError
        await asyncio.sleep(0.1)


async def server_login(info: Mapping[str, object], url: str, timeout: float) -> int:
    root, sid = handoff.state_root(), str(info["server_id"])
    login_id = secrets.token_hex(16)
    deadline = time.monotonic() + timeout
    try:
        nonce = handoff.write_command(root, sid, "login", url=url,
                                      login_id=login_id, timeout=timeout)
        ack = await asyncio.to_thread(handoff.wait_ack, root, sid, nonce, timeout)
        if not ack or not ack.get("ok"):
            print("agent-browser login: " + handoff.display_safe((ack or {}).get("message", "서버 응답 없음")), file=sys.stderr)
            return 1

        def closed() -> bool:
            state = handoff.read_control(root, sid)
            return state is None or not state.get("login")

        await wait_for_done(closed, deadline)
        return 0
    except asyncio.TimeoutError:
        print("agent-browser login: 로그인 대기 시간이 끝났습니다.", file=sys.stderr)
        return 1
    finally:
        # Includes cancellation while the server is draining an existing call.
        nonce = handoff.write_command(root, sid, "login_finish", login_id=login_id)
        ack = await asyncio.to_thread(handoff.wait_ack, root, sid, nonce, 30)
        if not ack or not ack.get("ok"):
            print("agent-browser login: 반납 응답을 확인하지 못했습니다; 서버는 대기 만료 시 반납합니다.", file=sys.stderr)


async def direct_login(name: str, url: str, timeout: float) -> int:
    from browser.core import BrowserCore

    lock = serve_profile.acquire(name, server_id="login-" + secrets.token_hex(4))
    core: Optional[BrowserCore] = None
    try:
        async with asyncio.timeout(timeout):
            core = BrowserCore(headless=False, persistent_profile=lock.path, human_like=True)
            await core.start()
            tab = await core.new_tab(name, url)
            await wait_for_done(tab.page.is_closed, time.monotonic() + timeout)
        return 0
    except asyncio.TimeoutError:
        print("agent-browser login: 로그인 대기 시간이 끝났습니다.", file=sys.stderr)
        return 1
    finally:
        try:
            if core is not None:
                await core.close()
        finally:
            lock.release()


def run(args: argparse.Namespace) -> int:
    try:
        validate_url(args.url)
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= MAX_LOGIN_TIMEOUT_S:
            raise ValueError("--timeout 은 0보다 크고 3600 이하인 유한한 초여야 합니다.")
        if not display_available():
            raise ValueError("화면(DISPLAY)이 없습니다. 맥 앞에서 같은 명령을 실행하세요.")
        servers = handoff.list_servers()
        info: Optional[Mapping[str, object]] = None
        name = args.profile
        if args.server:
            info, error = handoff.resolve_server(None, args.server)
            if info is None:
                raise ValueError(error)
            server_profile = info.get("profile")
            if name and name != server_profile:
                raise ValueError("--server 와 --profile 이 같은 프로필을 가리켜야 합니다.")
            name = server_profile
        elif name:
            matches = [s for s in servers if s.get("profile") == name]
            if len(matches) > 1:
                raise ValueError("같은 프로필 서버가 여러 개입니다; --server 로 고르세요.")
            info = matches[0] if matches else None
        elif len(servers) == 1:
            info = servers[0]
            name = info.get("profile")
        if not name:
            raise ValueError("--profile NAME 을 지정하세요. --profile 로 서버를 띄워야 로그인이 유지됩니다.")
        serve_profile.profile_dir(name)
        if info and info.get("browser_mode") == "headless":
            raise ValueError("headless 전용 서버에는 로그인 창을 열 수 없습니다. 맥 앞에서 서버를 끝내고 "
                             "같은 login 명령을 실행한 뒤 serve --browser on-demand --profile 로 띄우세요.")
        return asyncio.run(_login_with_signals(info, name, args.url, args.timeout))
    except KeyboardInterrupt:
        print("agent-browser login: 취소했습니다; 조작권·프로필 잠금을 반납합니다.", file=sys.stderr)
        return 130
    except (ValueError, serve_profile.ProfileInUseError) as exc:
        print("agent-browser login: " + handoff.display_safe(str(exc), 1000), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"agent-browser login: 창을 열지 못했습니다({type(exc).__name__}). 맥 앞에서 화면/Chromium을 확인하세요.", file=sys.stderr)
        return 2


async def _login_with_signals(info: Optional[Mapping[str, object]], name: str,
                              url: str, timeout: float) -> int:
    """Cancel on TERM/HUP so both login paths finish their existing cleanup."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    received: Optional[int] = None
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}

    def stop(sig: int) -> None:
        nonlocal received
        if received is None:
            received = sig
            task.cancel()

    try:
        for sig in previous:
            loop.add_signal_handler(sig, stop, sig)
        try:
            return await (server_login(info, url, timeout) if info else direct_login(name, url, timeout))
        except asyncio.CancelledError:
            if received is None:
                raise
            print("agent-browser login: 종료 신호를 받았습니다; 조작권·프로필 잠금을 반납합니다.", file=sys.stderr)
            return 128 + received
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)
