"""WS-64 — 사람 인계 대상의 '사람이 직접 눌렀다' 감지(격리 world 수동 리스너, login_watch 와 같은 방식).

페이지 DOM 에는 아무것도 넣지 않는다. 대상 탭에 CDP 세션을 열어 격리 world(페이지 스크립트가 볼 수 없는 실행 문맥)를
만들고, 그 world 의 window 에 capture·passive 리스너를 단다 — 이벤트를 막거나 바꾸지 않는다. 사람이 실제로(isTrusted)
대상 요소(composedPath 에 포함)를 누르거나(click) 값을 바꾸면(change) 바인딩으로 서버에 알린다. 값은 읽지 않는다.

남는 위험: 페이지가 먼저 stopImmediatePropagation 을 부르거나(window capture 보다 앞선 리스너는 없다 — 대부분 잡힘)
닫힌 shadow 안 대상이면 놓칠 수 있다 → 문서 바뀜(page_changed)·`agent-browser handoff done` 으로 대신 판정한다.
설치가 실패하면 clicked 판정만 꺼진다(installed=False).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

#: 설치·해제 한 단계 상한(초) — 대화상자가 떠 있는 등 페이지가 멈춰도 인계 응답을 늦추지 않는다.
STEP_TIMEOUT_S = 2.0

_LISTEN_JS = r"""function (name, events) {
  const el = this;
  const B = globalThis[name];
  if (typeof B !== 'function') return false;
  try { delete globalThis[name]; } catch (e) {}
  let sent = false;
  const seen = (e) => {
    if (sent || !e.isTrusted) return;
    try { if (!e.composedPath().includes(el)) return; } catch (x) { return; }
    sent = true;
    try { B('1'); } catch (x) {}
  };
  for (const t of events) window.addEventListener(t, seen, {capture: true, passive: true});
  return true;
}"""


class ManualClickWatch:
    """대상 요소 하나에 대한 수동 감지기. start() 뒤 triggered 가 True 면 사람이 직접 조작한 것."""

    def __init__(self) -> None:
        tag = secrets.token_hex(6)
        self.binding = f"__ab_manual_{tag}"
        self.world = f"agent-browser-manual-{tag}"
        self.triggered = False
        self.installed = False
        self.backend_node_id: Optional[int] = None
        self._session: Any = None

    async def _send(self, method: str, params: Optional[dict] = None) -> Any:
        return await asyncio.wait_for(self._session.send(method, params or {}), STEP_TIMEOUT_S)

    async def start(self, page: Any, *, frame_url: Optional[str] = None, backend_node_id: Optional[int] = None,
                    css: Optional[str] = None, events: Iterable[str] = ("click",)) -> bool:
        """대상에 리스너를 단다. 대상 노드의 backendNodeId 를 기억한다(표시용). 성공하면 True."""
        try:
            self._session = await asyncio.wait_for(page.context.new_cdp_session(page), STEP_TIMEOUT_S)

            def called(event: Any) -> None:
                if isinstance(event, dict) and event.get("name") == self.binding:
                    self.triggered = True

            self._session.on("Runtime.bindingCalled", called)
            await self._send("Runtime.enable")
            await self._send("DOM.enable")
            await self._send("Runtime.addBinding", {"name": self.binding, "executionContextName": self.world})
            tree = await self._send("Page.getFrameTree")
            frame_id = _frame_id(tree.get("frameTree") or {}, frame_url)
            if not frame_id:
                return False
            world = await self._send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": self.world})
            context_id = world["executionContextId"]
            object_id = None
            if backend_node_id:
                node = await self._send("DOM.resolveNode", {"backendNodeId": int(backend_node_id),
                                                            "executionContextId": context_id})
                object_id = (node.get("object") or {}).get("objectId")
            elif css:
                found = await self._send("Runtime.evaluate", {"expression": f"document.querySelector({json.dumps(css)})",
                                                              "contextId": context_id})
                object_id = (found.get("result") or {}).get("objectId")
            if not object_id:
                return False
            if not backend_node_id:
                desc = await self._send("DOM.describeNode", {"objectId": object_id})
                backend_node_id = (desc.get("node") or {}).get("backendNodeId")
            self.backend_node_id = int(backend_node_id) if backend_node_id else None
            out = await self._send("Runtime.callFunctionOn", {
                "objectId": object_id, "functionDeclaration": _LISTEN_JS,
                "arguments": [{"value": self.binding}, {"value": list(events)}], "returnByValue": True})
            self.installed = bool((out.get("result") or {}).get("value"))
            return self.installed
        except Exception:  # noqa: BLE001 - 설치 실패: clicked 판정만 끈다(page_changed·done 으로 대신)
            logger.debug("직접 조작 감지 설치 실패", exc_info=True)
            return False

    async def stop(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        try:
            await asyncio.wait_for(session.detach(), STEP_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - 닫힌 탭: 세션도 함께 사라졌다
            pass


def _frame_id(tree: dict, frame_url: Optional[str]) -> str:
    """프레임 트리에서 대상 프레임 id(frame_url 이 없으면 최상위, 못 찾으면 '')."""
    root = (tree.get("frame") or {}).get("id") or ""
    if not frame_url:
        return root
    stack = list(tree.get("childFrames") or [])
    while stack:
        node = stack.pop(0)
        frame = node.get("frame") or {}
        if frame.get("url") == frame_url and frame.get("id"):
            return str(frame["id"])
        stack.extend(node.get("childFrames") or [])
    return ""
