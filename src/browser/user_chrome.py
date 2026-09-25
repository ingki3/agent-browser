"""본인 Chrome 연결 모드 (WS-22 파트 3) — 전용 프로필, 자동화 플래그 없음.

사람이 쓰는 Chrome 을 `--enable-automation` 없이 띄우고 Playwright
``connect_over_cdp`` 로 붙는다. 그래서 navigator.webdriver 는 false 이고
플러그인·GPU·화면 크기가 평소 Chrome 과 같다. **위장이 아니다** — UA 변경,
stealth 스크립트, webdriver 끄기 플래그, 프록시는 쓰지 않는다.

Chrome 136+ 는 기본 프로필(사용자 평소 프로필)에서 --remote-debugging-port 를
무시한다(https://developer.chrome.com/blog/remote-debugging-port). 그래서
전용 프로필 폴더(기본 ~/.agent-browser/chrome-profile, 권한 700)를 쓴다.
사용자 평소 프로필 경로는 거부한다(ValueError) — 쿠키·비밀번호 보호.

원격 디버깅 주소는 Chrome 기본값(127.0.0.1 전용 바인딩)을 그대로 쓰고, 붙을 때도
127.0.0.1 로만 붙는다. 포트는 항상 구체 번호로 넘긴다(port=0 이면 우리가 빈 포트를
고른다) — 실측상 `--remote-debugging-port=0` 이면 webdriver=true 가 되기 때문이다.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Tuple

#: 전용 프로필 폴더(사용자 평소 Chrome 프로필과 분리)
DEFAULT_PROFILE_DIR = Path.home() / ".agent-browser" / "chrome-profile"

#: 설치된 Chrome 후보 경로(앞에서부터 확인)
CHROME_CANDIDATES: Tuple[Path, ...] = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/google-chrome-stable"),
    Path("/opt/google/chrome/chrome"),
)

#: 사용자 평소 Chrome 프로필 루트 — 이 아래는 거부한다
_USER_PROFILE_ROOTS: Tuple[Path, ...] = (
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome Beta",
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome Canary",
    Path.home() / ".config" / "google-chrome",
    Path.home() / ".config" / "google-chrome-beta",
    Path.home() / ".config" / "chromium",
)

_HOST = "127.0.0.1"
READY_TIMEOUT_S = 20.0


def find_chrome() -> Optional[Path]:
    """설치된 Chrome 실행 파일 경로. 없으면 None."""
    for cand in CHROME_CANDIDATES:
        if cand.is_file():
            return cand
    return None


def _is_user_default_profile(path: Path) -> bool:
    p = path.expanduser().resolve()
    for root in _USER_PROFILE_ROOTS:
        r = root.resolve()
        if p == r or r in p.parents:
            return True
    return False


def _guard_profile(profile_dir: Path) -> Path:
    if _is_user_default_profile(profile_dir):
        raise ValueError(
            "사용자 평소 Chrome 프로필은 쓸 수 없습니다(평소 프로필 보호 + Chrome 136+ "
            f"원격 디버깅 제약). 전용 폴더를 쓰세요: {DEFAULT_PROFILE_DIR}"
        )
    return profile_dir.expanduser().resolve()


def prepare_profile_dir(profile_dir: Path = DEFAULT_PROFILE_DIR) -> Path:
    """전용 프로필 폴더를 권한 700 으로 만든다(있으면 700 으로 조인다)."""
    path = _guard_profile(profile_dir)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def pick_free_port() -> int:
    """127.0.0.1 에서 비어 있는 TCP 포트 하나."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((_HOST, 0))
        return int(s.getsockname()[1])


def build_chrome_args(*, profile_dir: Path, port: int) -> list:
    """Chrome 실행 인자. 자동화 플래그(--enable-automation, --headless 등)는 넣지 않는다.

    port 는 구체 번호여야 한다. 실측(Chrome 154, 2026-09-25): ``--remote-debugging-port=0``
    으로 띄우면 connect_over_cdp 로 붙은 페이지의 navigator.webdriver 가 true 가 된다
    (같은 조건에서 구체 포트면 false). 그래서 0 은 거부하고 launch 가 빈 포트를 고른다.
    """
    path = _guard_profile(profile_dir)
    port = int(port)
    if not 0 < port < 65536:
        raise ValueError(f"구체 포트 번호가 필요합니다(받은 값 {port}) — 0 은 webdriver=true 를 만든다")
    return [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={path}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


@dataclass
class UserChrome:
    """디버그 포트가 열린 Chrome. process 가 있으면 우리가 띄운 것(owned)."""

    port: int
    process: Optional[subprocess.Popen] = None
    profile_dir: Optional[Path] = None
    ws_url: str = field(default="")

    @property
    def owned(self) -> bool:
        return self.process is not None

    @property
    def endpoint(self) -> str:
        return f"http://{_HOST}:{self.port}"

    def close(self, timeout: float = 10.0) -> None:
        """우리가 띄운 Chrome 만 종료한다. attach 로 붙은 Chrome 은 건드리지 않는다."""
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        # start_new_session=True 로 띄웠으므로 프로세스 그룹 = 우리가 띄운 Chrome 과 그 헬퍼들
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=timeout)


def _version_info(port: int, timeout: float = 1.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"http://{_HOST}:{port}/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


async def launch_user_chrome(
    *,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    port: int = 0,
    chrome_path: Optional[Path] = None,
    timeout_s: float = READY_TIMEOUT_S,
) -> UserChrome:
    """사람이 쓰는 Chrome 을 자동화 플래그 없이 전용 프로필로 띄우고 준비될 때까지 기다린다.

    port=0 이면 127.0.0.1 빈 포트를 골라 구체 번호로 넘긴다(0 을 Chrome 에 넘기지 않는다 —
    build_chrome_args 참조). 준비 여부는 http://127.0.0.1:<port>/json/version 폴링으로 본다.
    """
    _guard_profile(profile_dir)  # 가드가 먼저(프로세스·폴더를 만들기 전)
    if not port:
        port = pick_free_port()
    args = build_chrome_args(profile_dir=profile_dir, port=port)
    path = prepare_profile_dir(profile_dir)
    exe = chrome_path or find_chrome()
    if exe is None:
        raise FileNotFoundError("설치된 Google Chrome 을 찾지 못했습니다")
    proc = subprocess.Popen(
        [str(exe), *args, "about:blank"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    uc = UserChrome(port=port, process=proc, profile_dir=path)
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Chrome 이 바로 종료됨(exit {proc.returncode}) — 같은 프로필을 쓰는 "
                    "Chrome 이 이미 떠 있을 수 있습니다"
                )
            info = _version_info(port)
            if info is not None:
                uc.ws_url = info.get("webSocketDebuggerUrl", "")
                return uc
            await asyncio.sleep(0.2)
        raise TimeoutError(f"Chrome 디버그 포트 준비 대기 초과({timeout_s}s)")
    except BaseException:
        uc.close()
        raise


async def attach(port: int, *, timeout_s: float = 5.0) -> UserChrome:
    """사용자가 직접 띄운 디버그 Chrome(127.0.0.1:port)에 붙는다. 소유하지 않는다."""
    deadline = time.monotonic() + timeout_s
    while True:
        info = _version_info(port)
        if info is not None:
            return UserChrome(port=port, process=None,
                              ws_url=info.get("webSocketDebuggerUrl", ""))
        if time.monotonic() >= deadline:
            raise ConnectionError(f"{_HOST}:{port} 에 디버그 Chrome 이 없습니다")
        await asyncio.sleep(0.2)


async def connect_user_chrome(pw: Any, uc: UserChrome) -> Tuple[Any, Any, Any]:
    """connect_over_cdp 로 붙어 기본 context 를 재사용한다. 페이지가 없으면 새로 연다."""
    browser = await pw.chromium.connect_over_cdp(uc.endpoint)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()
    return browser, context, page
