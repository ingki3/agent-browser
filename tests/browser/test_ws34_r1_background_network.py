"""WS-34 R1 NB-1 — 창 있는 Chromium 의 Google 배경 접속을 끄는 실행 인자.

실측(.hermes/state/ws34-r1/report.md): 창 있는(headed) Chromium 은 시작 직후 Playwright 기본 인자
(--disable-background-networking·--disable-sync·--disable-component-update)로도 꺼지지 않는 요청을
보낸다 — 트래픽 주석으로 기능을 확정했다:
  gaia_auth_list_accounts(accounts.google.com/ListAccounts, 계정 일관성 Dice 조정기),
  gcm_checkin(android.clients.google.com/checkin → 성공하면 mtalk.google.com:5228, GCM),
  network_time_component(clients2.google.com/time, 네트워크 시각),
  aim_eligibility_fetch(www.google.com/async/folae, AI 모드 자격), www.google.com 사전 연결(검색 preconnect).
BrowserCore._launch_kwargs 한 곳에서 영속 프로필(launch_persistent_context)로 띄우는 모든 경로
(headless·창, on-demand·human --profile)에 넣는다.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import List

import pytest

from browser import core as core_mod
from browser.core import BrowserCore

PW_BUNDLE = Path(__import__("playwright").__file__).parent / "driver" / "package" / "lib" / "coreBundle.js"


def _args(core: BrowserCore) -> List[str]:
    return [str(a) for a in (core._launch_kwargs().get("args") or [])]


class _Egress:
    """launch_kwargs 만 흉내 — 실제 EgressRuntime 처럼 proxy + args 를 준다."""

    def __init__(self, server: str) -> None:
        self.server = server

    def launch_kwargs(self):
        return {"proxy": {"server": self.server}, "args": ["--disable-quic"]}


@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("egress", [True, False])
def test_launch_kwargs_carry_background_off_args(tmp_path, headless, persistent, egress):
    core = BrowserCore(headless=headless, persistent_profile=tmp_path / "p" if persistent else None,
                       egress=_Egress("http://127.0.0.1:1") if egress else None)
    args = _args(core)
    joined = " ".join(args)
    if not persistent:
        # 범위: 영속 프로필 경로만(비영속 launch 인자는 기존 그대로 — test_core_browser_modes)
        assert "--gaia-url" not in joined and "NetworkTimeServiceQuerying" not in joined
        return
    for feat in ("NetworkTimeServiceQuerying", "PreconnectToSearch", "AimEligibilityService"):
        assert feat in joined
    assert any(a.startswith("--gaia-url=http://127.0.0.1:") for a in args)
    assert any(a.startswith("--gcm-checkin-url=http://127.0.0.1:") for a in args)
    assert any(a.startswith("--gcm-mcs-endpoint=") and "127.0.0.1" in a for a in args)
    if egress:
        assert "--disable-quic" in args  # 검증 프록시 인자는 그대로


def test_single_disable_features_keeps_playwright_defaults(tmp_path):
    """Chromium 은 --disable-features 가 여러 번이면 마지막 것만 쓴다(실측: chrome://version 변형
    명령줄) — 우리 목록이 Playwright 기본 목록(Translate·OptimizationHints·HttpsUpgrades 등)을
    덮어 되살리지 않게, 하나로 합쳐 Playwright 기본 목록을 모두 포함해야 한다."""
    args = _args(BrowserCore(headless=False, persistent_profile=tmp_path / "p"))
    dis = [a for a in args if a.startswith("--disable-features=")]
    assert len(dis) == 1
    ours = set(dis[0].split("=", 1)[1].split(","))
    text = PW_BUNDLE.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"disabledFeatures\s*=\s*\(?[^\[]*\[(.*?)\]", text, re.S)
    assert m, "Playwright 기본 disabledFeatures 목록을 찾지 못함 — Playwright 구조 변경 확인"
    pw = set(re.findall(r'"([A-Za-z0-9]+)"', re.sub(r"//[^\n]*", "", m.group(1))))
    assert pw, "Playwright 기본 목록이 비어 있음"
    missing = pw - ours
    assert not missing, f"Playwright 기본 --disable-features 를 덮어씀(업그레이드 후 목록 갱신 필요): {missing}"


def test_no_masking_flags():
    """배경 네트워크 끄기만 — 자동화 숨김·UA 위장 같은 플래그는 넣지 않는다."""
    joined = " ".join(core_mod.background_network_off_args())
    for bad in ("AutomationControlled", "user-agent", "enable-automation", "webdriver"):
        assert bad not in joined


# ---------------------------------------------------------------- 실제 창(headed) 통합


requires_display = pytest.mark.skipif(
    sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
    reason="창(headed) 테스트: 화면 없음",
)


async def _hosts_seen(tmp_path: Path, monkeypatch, *, off: bool) -> List[str]:
    """차단 프록시(모든 요청 403, 첫 줄의 호스트만 기록)에 묶은 영속 프로필 창을 6초 띄운다."""
    seen: List[str] = []

    async def on_client(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(r.readline(), 5)).decode("latin1").split(" ")
            if len(line) > 1:
                seen.append(line[1])
            w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await w.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            w.close()

    if not off:
        monkeypatch.setattr(core_mod, "background_network_off_args", lambda: [])
    srv = await asyncio.start_server(on_client, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    core = BrowserCore(headless=False, persistent_profile=tmp_path / ("on" if off else "off"),
                       egress=_Egress(f"http://127.0.0.1:{port}"))
    try:
        await core.start()
        await asyncio.sleep(6.0)
    finally:
        await core.close()
        srv.close()
    hosts = set()
    for target in seen:
        host = re.sub(r"^https?://", "", target).split("/")[0].rsplit(":", 1)[0]
        hosts.add(host)
    return sorted(h for h in hosts if h not in ("127.0.0.1", "localhost", ""))


@requires_display
async def test_headed_persistent_sees_only_loopback(tmp_path, monkeypatch):
    # 대조군(규칙 4): 인자를 빼면 같은 조건에서 실제로 Google 배경 요청이 생겨야 측정이 유효하다.
    control = await _hosts_seen(tmp_path, monkeypatch, off=False)
    assert any(h.endswith("google.com") for h in control), f"대조군에서 배경 요청 미재현: {control}"
    monkeypatch.undo()
    hosts = await _hosts_seen(tmp_path, monkeypatch, off=True)
    assert hosts == [], f"배경 요청이 남음: {hosts}"
