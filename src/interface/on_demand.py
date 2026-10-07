"""필요할 때만 창 (WS-34) — `serve --browser on-demand` 의 상태·판정(브라우저 없이 도는 부분).

Chromium 은 같은 브라우저를 headless ↔ 창 있음으로 바꿀 수 없다. 그래서 영속 프로필(WS-32, 없으면
서버 전용 임시 프로필)을 닫고 같은 폴더로 headless=False/True 로 **다시 연다** — 쿠키·localStorage·
IndexedDB 는 폴더에 남고, 열린 탭은 URL 로 복원한다(입력 중 내용·sessionStorage 는 사라질 수 있음).
실제 전환(닫기·열기·연결 재설치)은 mcp_server.BrowserMCPServer 가 하고, 여기는 상태와 규칙만 둔다.

* 창 열기: 조작권 요청(control_request) 또는 사람이 take.
* 창 닫기(D2): 사람이 release 하면 headless 로 복귀 — 에이전트에게 "다시 관찰" 안내.
* sticky(D3): 복귀 뒤 창에서 해결했던 사이트(등록 가능 도메인)에서 차단/캡차가 다시 보이면
  sticky_pending 을 알리고, 다음 조작권 요청 때 창을 열어 서버 수명 동안 유지한다.
  headless 는 UA 에 HeadlessChrome 을 싣고 우리는 UA 를 바꾸지 않는다(우회 금지) — 사이트가
  창에서 통과한 결과를 headless 에서 인정하지 않을 수 있어서다.
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlsplit

#: serve --browser 값.
ON_DEMAND = "on-demand"

#: 창 전환 중 들어온 도구 호출이 전환 끝을 기다리는 상한(초). 넘으면 E_TIMEOUT + data.window.
SWITCH_WAIT_S = 30.0
#: 전환이 진행 중인 도구 호출이 끝나길 기다리는 상한(초). 넘으면 전환하지 않는다(브라우저 그대로).
DRAIN_TIMEOUT_S = 30.0
#: 탭 복원 이동 상한(밀리초).
RESTORE_NAV_TIMEOUT_MS = 15_000
#: 조작권 요청으로 연 창을 사람이 take 하지 않으면 닫는 시간(초).
REQUEST_WINDOW_TTL_S = 600.0

#: 다시 열린 브라우저에 대한 안내(창이 열릴 때·닫힐 때 공통 앞부분).
REOPEN_NOTE = (
    "브라우저를 같은 프로필로 다시 열어 탭 URL 을 복원했습니다 — 쿠키·localStorage 는 유지되지만 "
    "입력 중이던 폼 내용·sessionStorage·스크롤 위치는 사라졌을 수 있습니다. 이전 element_id·tab_id 는 무효."
)
HEADED_HINT = REOPEN_NOTE + " 사람이 창에서 조작합니다 — browser_control_wait 로 기다리세요."
HEADLESS_HINT = REOPEN_NOTE + " 창을 닫고 headless 로 돌아왔습니다 — browser_observe_page 로 다시 관찰하세요."
STICKY_PENDING_HINT = (
    "창에서 해결했던 사이트에서 차단/캡차가 다시 감지됐습니다(headless 를 인정하지 않는 사이트일 수 "
    "있음). browser_control_request 로 사람을 다시 부르세요 — 이번에는 창을 서버 수명 동안 유지합니다."
)

_RESTORABLE_SCHEMES = frozenset({"http", "https"})
#: 두 단계 공용 접미사(흔한 것만 — 공개 접미사 목록 전체는 싣지 않는다, 보고서 한계 참조).
_TWO_LEVEL_SUFFIXES = frozenset({
    "co.kr", "or.kr", "go.kr", "ac.kr", "ne.kr", "re.kr", "pe.kr",
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "ne.jp", "or.jp", "ac.jp",
    "com.au", "net.au", "org.au", "com.br", "com.cn", "com.tw", "com.hk", "co.nz", "co.in",
})


def _host_of(url_or_host: str) -> str:
    text = str(url_or_host or "").strip()
    if not text:
        return ""
    if "://" not in text and (":" in text or "/" in text):
        return ""  # about:blank·data:·javascript: 등 호스트 없는 URL
    try:
        parts = urlsplit(text if "://" in text else f"//{text}")
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if " " in host:
        return ""
    return host


def registrable_domain(url_or_host: str) -> str:
    """URL(또는 호스트) → 등록 가능 도메인(eTLD+1 근사). IP·localhost 는 그대로, 판정 불가면 ''."""
    host = _host_of(url_or_host)
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = [x for x in host.split(".") if x]
    if len(labels) <= 1:
        return host
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def restorable_url(url: str) -> bool:
    """전환 뒤 다시 열 탭 URL 인가 — http(s) 만(about:blank·chrome://·data:·file: 등은 건너뜀)."""
    try:
        scheme = urlsplit(str(url or "")).scheme.lower()
    except ValueError:
        return False
    return scheme in _RESTORABLE_SCHEMES


@dataclass
class WindowState:
    """on-demand 서버 한 개의 창 상태(서버 메모리 — 재시작하면 초기화)."""

    #: headless | switching | headed | failed
    state: str = "headless"
    sticky: bool = False
    sticky_reason: str = ""
    sticky_pending: Optional[Dict[str, Any]] = None
    #: 창에서 사람이 해결했던 사이트(등록 가능 도메인).
    solved_domains: Set[str] = field(default_factory=set)
    #: 실패 사유(state=failed).
    error: str = ""
    #: 서버가 알릴 사건(사람이 창을 닫음·요청 만료 등) — control_status/wait 에 싣는다.
    notice: Optional[Dict[str, Any]] = None
    #: 조작권 요청으로 창을 연 시각(monotonic). 사람이 take 하면 None.
    opened_for_request: Optional[float] = None
    #: 창이 열린 동안 마지막으로 본 탭 URL(사람이 창을 닫은 뒤 복원용) [(tab_id, url, active)].
    last_tabs: List[Dict[str, Any]] = field(default_factory=list)

    def info(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"mode": ON_DEMAND, "state": self.state, "sticky": self.sticky}
        if self.sticky and self.sticky_reason:
            out["sticky_reason"] = self.sticky_reason
        if self.sticky_pending is not None:
            out["sticky_pending"] = dict(self.sticky_pending)
        if self.state == "failed" and self.error:
            out["error"] = self.error
        if self.notice is not None:
            out["notice"] = dict(self.notice)
        return out

    def note_solved(self, urls: Iterable[str]) -> None:
        for url in urls:
            dom = registrable_domain(url)
            if dom:
                self.solved_domains.add(dom)

    def note_challenge(self, url: str, challenge: Optional[Dict[str, Any]]
                       ) -> Optional[Dict[str, Any]]:
        """headless 에서 차단/캡차 감지 → 창에서 해결했던 사이트면 sticky_pending(새로 생겼을 때만 반환)."""
        if not challenge or self.sticky or self.state != "headless":
            return None
        dom = registrable_domain(url)
        if not dom or dom not in self.solved_domains:
            return None
        if self.sticky_pending is not None and self.sticky_pending.get("domain") == dom:
            return None
        self.sticky_pending = {
            "domain": dom,
            "kind": str(challenge.get("kind") or ""),
            "detected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return dict(self.sticky_pending)

    def take_sticky_for_request(self) -> bool:
        """조작권 요청 때: sticky_pending 이 있으면 sticky 로 확정한다. sticky 면 True."""
        if self.sticky_pending is not None and not self.sticky:
            pending, self.sticky_pending = self.sticky_pending, None
            self.sticky = True
            self.sticky_reason = (
                f"{pending.get('domain')} 에서 창에서 해결한 뒤 headless 복귀 후 "
                f"{pending.get('kind') or '차단'} 재감지 — 서버 수명 동안 창 유지"
            )
        return self.sticky

    def keep_window_after_release(self) -> bool:
        return self.sticky
