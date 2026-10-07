"""승인 확인 코드 전용 작은 창 (WS-34 D1) — on-demand 서버가 headless 일 때.

페이지는 headless 그대로 두고(승인한 행동이 문서 해시 일치로 그대로 실행되게), 사람에게 보일 코드만
**별도 브라우저 인스턴스**(별도 Chromium 프로세스, 임시 user-data-dir)의 작은 창에 띄운다.

* 에이전트 도구가 닿지 않는다: BrowserCore 의 탭·컨텍스트·영속 프로필에 등록하지 않는다.
* 네트워크 전부 차단: 닿을 수 없는 프록시(127.0.0.1:9, 루프백도 프록시 경유) + 오프라인 컨텍스트 +
  모든 요청 route abort. 내용은 로컬 HTML(set_content), 스크립트 끔.
* 표시 문자열(액션·대상·출처)은 display_safe 를 거친 뒤 HTML 이스케이프한다.
* 띄우지 못하면 False — 부르는 쪽이 코드를 무효로 한다(fail-closed: 창에 없는 코드로는 승인 불가).
"""

from __future__ import annotations

import html
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 창 크기(px).
WINDOW_W, WINDOW_H = 520, 340
#: 닿을 수 없는 프록시 — 혹시 요청이 생겨도 밖으로 나가지 않는다(로컬 내용만 쓴다).
_BLACKHOLE_PROXY = "http://127.0.0.1:9"


def _launch_args() -> list:
    return [
        f"--window-size={WINDOW_W},{WINDOW_H}",
        f"--proxy-server={_BLACKHOLE_PROXY}",
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
    ]


def render_html(*, action: str, target: str, origin: str, expires_at: str, code: str,
                ttl_s: int) -> str:
    """창 내용(정적 HTML). 인자는 이미 display_safe 를 거친 문자열이어야 한다.

    R1 NB-4: 대상(사이트가 붙인 이름)은 "(사이트가 붙인 이름)" 표기 뒤에, 6자리 이상 숫자열은 가려서 —
    진짜 코드는 #code 칸에만 큰 글씨로.
    """
    from interface.handoff import SITE_NAME_LABEL, mask_code_like

    e = html.escape
    target = f"{SITE_NAME_LABEL} {mask_code_like(target)}"
    spaced = " ".join(code)
    return (
        "<!doctype html><html lang=ko><head><meta charset=utf-8>"
        "<title>agent-browser 승인 확인 코드</title>"
        "<style>body{font:15px -apple-system,system-ui,sans-serif;margin:18px;color:#111}"
        "h1{font-size:15px;margin:0 0 10px}dl{margin:0 0 12px;display:grid;"
        "grid-template-columns:auto 1fr;gap:4px 10px}dt{color:#555}dd{margin:0;word-break:break-all}"
        "#code{font:600 34px ui-monospace,Menlo,monospace;letter-spacing:4px;padding:8px 0}"
        "p{color:#555;font-size:13px;margin:6px 0 0}</style></head><body>"
        "<h1>agent-browser 승인 확인 코드</h1>"
        f"<dl><dt>액션</dt><dd id=action>{e(action)}</dd>"
        f"<dt>대상</dt><dd id=target>{e(target)}</dd>"
        f"<dt>문서</dt><dd id=origin>{e(origin)}</dd>"
        f"<dt>승인 만료</dt><dd id=expires>{e(expires_at)}</dd></dl>"
        f"<div id=code aria-label='확인 코드 {e(code)}'>{e(spaced)}</div>"
        f"<p>코드 유효 {int(ttl_s)}초. 터미널의 approve 화면(액션·대상)과 대조해 입력하세요. "
        "입력·거절·만료되면 이 창은 닫힙니다.</p></body></html>"
    )


class ApprovalCodeWindow:
    """코드 하나를 띄우는 별도 브라우저 창. show() 마다 새로 띄우고 close() 로 닫는다."""

    def __init__(self) -> None:
        self._browser: Any = None
        self._context: Any = None
        self.page: Any = None
        #: 띄우기를 마친 창이 있다(show 성공 뒤 close 전) — 띄우는 중에는 False.
        self._shown = False

    @property
    def is_open(self) -> bool:
        if self._browser is None or self.page is None:
            return False
        try:
            return bool(self._browser.is_connected()) and not self.page.is_closed()
        except Exception:  # noqa: BLE001
            return False

    @property
    def closed_externally(self) -> bool:
        """띄워 둔 창이 우리 close() 없이 사라졌다(사람이 창을 닫음·브라우저 종료) — R1 NB-3.

        띄우는 중(show 진행 중)은 False — 아직 열리지 않은 창을 닫힌 것으로 보지 않는다.
        """
        return self._shown and not self.is_open

    async def _launch(self, playwright: Any) -> Any:
        return await playwright.chromium.launch(headless=False, args=_launch_args())

    async def show(self, playwright: Any, *, action: str, target: str, origin: str,
                   expires_at: str, code: str, ttl_s: int) -> bool:
        """창을 띄워 코드를 보인다. 성공하면 True. 실패하면 띄운 것을 정리하고 False."""
        await self.close()
        if playwright is None:
            return False
        try:
            self._browser = await self._launch(playwright)
            self._context = await self._browser.new_context(
                viewport={"width": WINDOW_W - 20, "height": WINDOW_H - 60},
                java_script_enabled=False,
                offline=True,
            )

            async def _abort(route: Any, _request: Any = None) -> None:
                await route.abort("blockedbyclient")

            await self._context.route("**/*", _abort)
            self.page = await self._context.new_page()
            await self.page.set_content(render_html(
                action=action, target=target, origin=origin, expires_at=expires_at,
                code=code, ttl_s=ttl_s))
            try:
                await self.page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            self._shown = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("승인 코드 창을 띄우지 못함: %s", type(exc).__name__)
            await self.close()
            return False

    async def close(self) -> bool:
        """창을 닫는다. 닫았거나 원래 없으면 True."""
        browser, self._browser = self._browser, None
        self._shown = False
        self._context = None
        self.page = None
        if browser is None:
            return True
        try:
            await browser.close()
            return True
        except Exception:  # noqa: BLE001
            logger.warning("승인 코드 창 닫기 실패", exc_info=True)
            return False


def safe_text(value: Optional[str], limit: int) -> str:
    from interface.handoff import display_safe

    return display_safe(value or "", limit)
