"""WS-77 — 사람용 제어 창: 창 안의 [다 했어요] 로 조작권을 돌려받는다(#210, 리뷰 설계 (b)).

조작권 요청(창이 있는 serve) 때 서버가 같은 브라우저에 **별도 작은 창**(``Target.createTarget{newWindow:true}``,
스크립트 없는 ``data:`` 문서, opener 없음, opaque origin)을 띄우고, 그 문서의 **우리만 아는 isolated world** 에서
고정 문구와 버튼을 그린다. 버튼 클릭은 그 world 전용 이름으로 붙인 ``Runtime.addBinding(executionContextName)``
으로만 서버에 닿는다.

서버가 받아들이는 조건(B2) — 하나라도 어긋나면 버린다:

1. ``isTrusted`` 클릭(페이지·제어 창 main world 스크립트의 ``click()``·``dispatchEvent`` 거름), 그리고 클릭된 버튼이
   **원래 제어 창 문서에 붙어 있음**(ownerDocument·isConnected·event.view — R1 F1: main world 가 버튼을 팝업 등 다른
   문서로 옮기면 그 문서는 관리 탭이 될 수 있다). 버튼이 원 문서를 떠나면(MutationObserver) world 가 버튼을 죽이고
   '옮겨짐' 을 보고 → 서버는 그 창을 닫고 1회 다시 띄운다(B3 과 같은 흐름),
2. payload 의 nonce == 이 창을 띄운 요청의 request_id (hub 가 poll 때 **지금** 요청과 다시 대조),
3. 호출한 실행 문맥 == 우리가 만든 isolated world, 그리고 그 문서(loaderId)가 띄울 때 그대로(사람이 창을 다른
   사이트로 옮기면 새 문서에는 바인딩이 없고, 이동은 감지해 창을 닫는다 — B3),
4. hub 가 아직 그 요청을 기다리거나 사람이 조작권을 쥔 상태(hub._take_window_done).

``isTrusted`` 는 *페이지 스크립트* 만 거른다 — 에이전트의 CDP 입력도 trusted 다. 진짜 경계는 이 창이 코어 관리 탭이
아니라는 것(B1, ``BrowserCore.exclude_page``): 에이전트 도구(click·좌표·press_key·batch·tab_control·레시피)는 관리 탭만
대상으로 삼으므로 이 창에 닿을 수 없다. 만들기~빼기는 부르는 쪽이 관문+drain 구간 안에서 한다.

바인딩 콜백은 hub 대기 사건 큐에 넣기만 한다(B4 — 상태 변경은 poll 의 기존 반납 흐름). 창에는 확인 코드·페이지 유래
글자를 싣지 않는다(사유는 에이전트가 쓴 글을 display_safe 로, NB3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: 창 크기(px) — Chrome 최소 폭에 맞춰 잘린다(실측 500x375).
WINDOW_W, WINDOW_H = 520, 400
#: 스크립트 없는 빈 문서 — 내용은 isolated world 가 textContent 로만 그린다.
DATA_URL = "data:text/html;charset=utf-8,%3C!doctype%20html%3E%3Cmeta%20charset%3Dutf-8%3E%3Ctitle%3Eagent-browser%3C%2Ftitle%3E%3Cbody%3E%3C%2Fbody%3E"
#: 만든 대상을 Playwright 페이지로 찾는 상한·한 CDP 단계 상한(초).
FIND_TIMEOUT_S = 5.0
STEP_TIMEOUT_S = 3.0
#: 한 창이 서버로 보내는 보고 상한(넘으면 world 쪽에서 더 보내지 않는다).
MAX_REPORTS = 16

TITLE = "agent-browser — 사람 차례"
HEADING = "에이전트가 사람의 조작을 기다립니다"
REASON_LABEL = "요청 사유(에이전트가 쓴 글): "
BODY = ("브라우저 창에서 로그인·확인을 모두 마친 뒤 아래 [다 했어요]를 누르세요. "
        "누르면 에이전트가 다시 화면을 봅니다.")
NOTE = ("이 작은 창은 agent-browser 가 띄운 것입니다(웹 페이지가 아님). 창을 닫아도 '완료' 로 보지 않습니다. "
        "브라우저 창 위쪽 띠의 ▶ 버튼은 이 버튼이 아닙니다.")
BUTTON = "다 했어요"
SENT = "전달했습니다 — 에이전트가 이어서 진행합니다"

_UI_JS = r"""(() => {
  const B = globalThis[__BINDING__];
  if (typeof B !== 'function') return null;
  try { delete globalThis[__BINDING__]; } catch (e) {}
  const nonce = __NONCE__;
  const T = __TEXT__;
  document.title = T.title;
  const body = document.body || document.documentElement.appendChild(document.createElement('body'));
  body.textContent = '';
  body.style.cssText = 'font:15px -apple-system,system-ui,sans-serif;margin:18px;color:#111;background:#fff';
  const add = (tag, text, css) => { const e = document.createElement(tag); e.textContent = text;
    if (css) e.style.cssText = css; body.appendChild(e); return e; };
  add('h1', T.heading, 'font-size:16px;margin:0 0 10px');
  if (T.reason) add('p', T.label + T.reason, 'margin:0 0 10px;word-break:break-all;color:#333');
  add('p', T.body, 'margin:0 0 14px');
  const b = add('button', T.button, 'font-size:20px;padding:12px 28px;cursor:pointer');
  add('p', T.note, 'color:#666;font-size:12px;margin:14px 0 0');
  let sent = 0;
  let dead = false;
  const doc = document, view = window;
  // R1 F1: 버튼이 원래 이 문서에 붙어 있는가(adoptNode·appendChild 로 다른 문서·창에 옮기면 거짓).
  const attached = () => b.isConnected === true && b.ownerDocument === doc && doc.contains(b);
  const send = (msg) => { if (sent >= __MAX__) return; sent += 1; try { B(JSON.stringify(msg)); } catch (err) {} };
  const kill = () => {
    if (dead) return;
    dead = true;
    b.disabled = true;
    send({nonce: nonce, moved: true});  // 서버가 이 창을 닫고 1회 다시 띄운다(완료 아님)
  };
  new MutationObserver(() => { if (!attached()) kill(); }).observe(doc, {childList: true, subtree: true});
  b.addEventListener('click', (e) => {
    const here = !dead && attached() && e.view === view && (e.target === b || b.contains(e.target));
    send({nonce: nonce, trusted: e.isTrusted === true, attached: here});
    if (!here) { kill(); return; }
    if (e.isTrusted === true) { b.disabled = true; b.textContent = T.sent; }
  });
  const r = b.getBoundingClientRect();
  return [r.x + r.width / 2, r.y + r.height / 2];
})()"""


def _ui_source(binding: str, nonce: str, reason: str) -> str:
    text = {"title": TITLE, "heading": HEADING, "label": REASON_LABEL, "reason": reason, "body": BODY,
            "note": NOTE, "button": BUTTON, "sent": SENT}
    return (_UI_JS.replace("__BINDING__", json.dumps(binding)).replace("__NONCE__", json.dumps(nonce))
            .replace("__TEXT__", json.dumps(text, ensure_ascii=False)).replace("__MAX__", str(MAX_REPORTS)))


async def _step(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, STEP_TIMEOUT_S)


class ControlWindow:
    """요청 하나의 제어 창. open() 은 부르는 쪽이 도구 호출을 멈춘 구간(관문+drain) 안에서 부른다."""

    def __init__(self, request_id: str, on_done: Callable[[str], None]) -> None:
        self.request_id = request_id
        self._on_done = on_done
        #: isolated world·바인딩 이름(창마다 새로) — main world 에는 이 바인딩이 없다.
        self.world = "ab_control_" + secrets.token_hex(6)
        self.binding = "abd" + secrets.token_hex(8)
        self.target_id: Optional[str] = None
        self.page: Any = None
        self.session: Any = None
        self.frame_id: Optional[str] = None
        self.loader_id: Optional[str] = None
        self.context_id: Optional[int] = None
        #: 버튼 중심(창 CSS 좌표) — 시험이 '사람 흉내' 클릭에 쓴다.
        self.button_center: Optional[tuple] = None
        #: 받아들인·버린 바인딩 호출 수(진단·시험).
        self.accepted = 0
        self.rejected = 0
        #: 사람이 창을 닫음(우리 close 아님) · 창 문서가 다른 문서로 바뀜(B3).
        self.gone = False
        self.navigated = False
        #: R1 F1: 버튼이 원 문서를 떠났다는 우리 world 의 보고 — 이 창은 무효(서버가 닫고 1회 다시 띄움).
        self.moved = False
        self._closing = False

    # -- 만들기 -------------------------------------------------------------

    async def open(self, core: Any, opener_page: Any, *, reason: str = "") -> bool:
        """창을 띄우고 코어 관리 탭에서 뺀 뒤 버튼을 그린다. 실패하면 띄운 것을 닫고 False."""
        try:
            await self._open(core, opener_page, reason)
            return True
        except Exception as exc:  # noqa: BLE001 - 창을 못 띄움: 띠·CLI 안내로 남는다(조작권 흐름은 그대로)
            logger.warning("제어 창을 띄우지 못함: %s", type(exc).__name__)
            await self.close()
            return False

    async def _open(self, core: Any, opener_page: Any, reason: str) -> None:
        context = opener_page.context
        known = list(context.pages)
        opener = await _step(context.new_cdp_session(opener_page))
        opener_window: Optional[Dict[str, Any]] = None
        try:
            info = (await _step(opener.send("Target.getTargetInfo"))).get("targetInfo") or {}
            params: Dict[str, Any] = {"url": DATA_URL, "newWindow": True}
            if info.get("browserContextId"):
                params["browserContextId"] = info["browserContextId"]  # 에이전트 탭과 같은 컨텍스트(쿠키 무관 — 빈 문서)
            try:
                opener_window = await _step(opener.send("Browser.getWindowForTarget"))
            except Exception:  # noqa: BLE001 - 자리 잡기용일 뿐
                opener_window = None
            created = await _step(opener.send("Target.createTarget", params))
            self.target_id = str(created["targetId"])
        finally:
            try:
                await _step(opener.detach())
            except Exception:  # noqa: BLE001
                pass
        page, session = await self._find(context, known)
        # B1: 'page' 사건으로 이미 관리 탭이 됐을 수 있다 — 지금(관문 안) 뺀다.
        core.exclude_page(page)
        self.page, self.session = page, session
        page.on("close", self._closed)
        session.on("Runtime.bindingCalled", self._on_binding)
        session.on("Page.frameNavigated", self._on_navigated)
        await _step(session.send("Page.enable"))
        await _step(session.send("Runtime.enable"))
        await _step(session.send("Runtime.addBinding", {"name": self.binding, "executionContextName": self.world}))
        frame = await self._loaded_frame(session)
        self.frame_id, self.loader_id = frame["id"], frame.get("loaderId")
        world = await _step(session.send("Page.createIsolatedWorld",
                                         {"frameId": self.frame_id, "worldName": self.world,
                                          "grantUniveralAccess": False}))
        self.context_id = world["executionContextId"]
        drawn = await _step(session.send("Runtime.evaluate", {
            "expression": _ui_source(self.binding, self.request_id, reason),
            "contextId": self.context_id, "returnByValue": True}))
        value = (drawn.get("result") or {}).get("value")
        if not isinstance(value, list) or len(value) != 2:
            raise RuntimeError("제어 창 버튼을 그리지 못함")
        self.button_center = (float(value[0]), float(value[1]))
        await self._place(session, opener_window)

    async def _find(self, context: Any, known: list) -> tuple:
        """createTarget 이 돌려준 targetId 의 Playwright 페이지를 찾는다(같은 순간 생긴 남의 팝업과 구별)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FIND_TIMEOUT_S
        checked: list = []
        while loop.time() < deadline:
            for page in list(context.pages):
                if any(page is k for k in known) or any(page is c for c in checked):
                    continue
                session = await _step(context.new_cdp_session(page))
                try:
                    info = (await _step(session.send("Target.getTargetInfo"))).get("targetInfo") or {}
                except Exception:  # noqa: BLE001 - 닫히는 중인 페이지
                    info = {}
                if info.get("targetId") == self.target_id:
                    return page, session
                checked.append(page)
                try:
                    await _step(session.detach())
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(0.02)
        raise RuntimeError("만든 제어 창을 찾지 못함")

    async def _loaded_frame(self, session: Any) -> Dict[str, Any]:
        """창의 최상위 문서가 우리 data: 문서가 된 뒤의 frame(처음 about:blank 를 거칠 수 있다)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FIND_TIMEOUT_S
        while True:
            tree = await _step(session.send("Page.getFrameTree"))
            frame = (tree.get("frameTree") or {}).get("frame") or {}
            if str(frame.get("url") or "").startswith("data:") and frame.get("id"):
                return frame
            if loop.time() >= deadline:
                raise RuntimeError("제어 창 문서를 열지 못함")
            await asyncio.sleep(0.02)

    async def _place(self, session: Any, opener_window: Optional[Dict[str, Any]]) -> None:
        """작게, 에이전트 창 오른쪽 위에 둔다(편의 — 실패해도 창은 쓸 수 있다)."""
        try:
            win = await _step(session.send("Browser.getWindowForTarget", {"targetId": self.target_id}))
            bounds: Dict[str, Any] = {"width": WINDOW_W, "height": WINDOW_H}
            ob = (opener_window or {}).get("bounds") or {}
            if isinstance(ob.get("left"), int) and isinstance(ob.get("width"), int):
                bounds["left"] = max(0, ob["left"] + ob["width"] - WINDOW_W - 24)
                bounds["top"] = max(0, int(ob.get("top") or 0) + 90)
            await _step(session.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": bounds}))
        except Exception:  # noqa: BLE001 - 의도된 fail-safe: 자리 잡기는 편의 기능
            logger.debug("제어 창 자리 잡기 실패", exc_info=True)

    # -- 사건 ---------------------------------------------------------------

    def _closed(self, *_a: Any) -> None:
        if not self._closing:
            self.gone = True

    def _on_navigated(self, event: Dict[str, Any]) -> None:
        frame = event.get("frame") or {}
        if frame.get("parentId"):
            return
        if frame.get("loaderId") != self.loader_id or (self.frame_id and frame.get("id") != self.frame_id):
            self.navigated = True  # B3: 다른 문서 — 이 창은 더 이상 우리 것이 아니다(서버가 닫고 1회 다시 띄움)

    def _on_binding(self, event: Dict[str, Any]) -> None:
        """B2 ①~③ 검사 뒤 hub 대기 사건 큐에 넣는다(④는 hub.poll). 상태를 직접 바꾸지 않는다."""
        if event.get("name") != self.binding:
            return
        ok = (not self._closing and not self.gone and not self.navigated and not self.moved
              and self.context_id is not None and event.get("executionContextId") == self.context_id)
        payload: Any = None
        if ok:
            raw = event.get("payload")
            try:
                payload = json.loads(raw) if isinstance(raw, str) and len(raw) <= 512 else None
            except ValueError:
                payload = None
            mine = (isinstance(payload, dict) and isinstance(payload.get("nonce"), str)
                    and secrets.compare_digest(payload["nonce"], self.request_id))
            if mine and payload.get("moved") is True:
                self.moved = True  # 버튼이 원 문서를 떠났다 — 이후 이 창의 호출은 모두 버린다
                return
            # R1 F1: 클릭 대상 버튼이 원래 제어 창 문서에 붙어 있었고 사건의 view 가 그 창일 때만(attached).
            ok = mine and payload.get("trusted") is True and payload.get("attached") is True
        if not ok:
            self.rejected += 1
            return
        self.accepted += 1
        try:
            self._on_done(self.request_id)
        except Exception:  # noqa: BLE001 - 사건 처리 중(받을 사람이 없다)
            logger.warning("제어 창 완료 신호 전달 실패", exc_info=True)

    # -- 닫기 ---------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        if self.page is None or self.gone or self._closing:
            return False
        try:
            return not self.page.is_closed()
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        """창을 닫는다(이미 닫혔으면 그대로). 이후 바인딩 호출은 모두 버린다."""
        self._closing = True
        page, self.page = self.page, None
        session, self.session = self.session, None
        if session is not None:
            try:
                await _step(session.send("Runtime.removeBinding", {"name": self.binding}))
            except Exception:  # noqa: BLE001 - 닫힌 창
                pass
            try:
                await _step(session.detach())
            except Exception:  # noqa: BLE001
                pass
        if page is not None:
            try:
                if not page.is_closed():
                    await _step(page.close())
            except Exception:  # noqa: BLE001 - 브라우저와 함께 사라진 창
                logger.debug("제어 창 닫기 실패", exc_info=True)
