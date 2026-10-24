"""WS-64 R2 — 주소로 출처를 알 수 없는 문서의 실효 출처(브라우저 값)와 불투명 문서의 생성 이력(lineage).

B2: about:blank·about:srcdoc·data:·blob:null 같은 문서는 주소에도, 페이지 world 의 `location`/`self` 에도 믿을 출처가
없다(location.origin 은 초기 빈 문서에서 'null', 페이지는 window 속성을 가릴 수 있다). 브라우저가 아는 값으로 본다 —
CDP `Page.getFrameTree` 의 frame.securityOrigin(튜플 출처일 때) 1순위, 아니면 같은 프레임의 **격리 world**(페이지가
손댈 수 없는 전역)에서 `self.origin`. 둘 다 못 읽으면 판정 불가(호출자가 차단). 실측(Chromium): 초기 빈 문서·srcdoc 의
securityOrigin 은 '://' 이고 격리 world self.origin 이 상속 출처(목록 출처)를 준다. sandbox·blob:null 은 'null'(불투명).

B3: 불투명 문서는 출처로 판정할 수 없다 — 탭·프레임 문서가 **생길 때** 이력을 적어 두고 끝까지 유지한다.
* 팝업: 생성 순간 opener 탭의 이력 전체(그 탭 프레임들이 거친 출처 ∪ 그 탭이 물려받은 이력 ∪ 프레임 문서 이력) 를
  물려받는다 — 어느 프레임이 열었는지 모르고, opener 의 이동 이벤트가 팝업 이벤트보다 먼저 올 수 있어(경합) 보수적으로.
* 프레임 문서: 튜플 출처가 아닌 주소로 이동한 순간 그 탭의 프레임 출처들(직전 주소 포함)을 적는다. 호출자는 여기에
  부모 프레임 사슬(지금)을 함께 본다.
* 감시 설치 전부터 있던 탭은 브라우저가 opener 없음을 확인하고 탭 이동 기록(Page.getNavigationHistory)을 읽어야 앎 —
  opener 가 있던 탭·확인하지 못한 탭은 이력 모름 → 불투명 문서는 차단.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlsplit

#: 격리 world 이름(같은 이름이면 Chromium 이 같은 world 를 다시 쓴다 — 호출마다 새 world 를 쌓지 않음, 실측).
ORIGIN_WORLD = "agent_browser_manual_origin"
#: 불투명 출처 표시.
OPAQUE = "null"


def impl(obj: Any) -> Any:
    """Playwright 공개 객체 → 내부 구현 객체(이벤트 콜백·opener 가 같은 키를 쓰게). 가짜 객체는 그대로."""
    return getattr(obj, "_impl_obj", obj)


def tuple_origin(url: Any) -> Optional[str]:
    """http(s) 주소(blob: 은 안쪽 주소)의 출처 'scheme://host[:port]'. 그 밖(about:·data:·blob:null·'://')은 None."""
    text = str(url or "")
    if text[:5].lower() == "blob:":
        text = text[5:]
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


# ---------------------------------------------------------------------------- lineage


class TabLineage:
    """한 탭(Page)의 생성 이력. known=False 면 이력 모름(불투명 문서는 차단)."""

    def __init__(self, known: bool, inherited: Iterable[str] = ()) -> None:
        self.known = known
        self.inherited: Set[str] = set(inherited)
        self.history: Set[str] = set()
        self.docs: "weakref.WeakKeyDictionary[Any, Set[str]]" = weakref.WeakKeyDictionary()
        self.last: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()
        #: opener 없음 확인(Target.getTargetInfo) 대기 — 끝나기 전 조회는 기다리거나 모름.
        self.pending: Optional["asyncio.Future[Any]"] = None

    def current(self) -> Set[str]:
        return {o for o in (tuple_origin(u) for u in list(self.last.values())) if o}

    def see(self, frame: Any) -> None:
        """프레임 이동(framenavigated) — 튜플 출처는 이력에, 아니면 그 순간 탭의 프레임 출처들을 그 문서 이력으로."""
        try:
            url = str(getattr(frame, "url", "") or "")
            key = impl(frame)
            origin = tuple_origin(url)
            if origin:
                self.history.add(origin)
                self.docs.pop(key, None)
            else:
                snap = self.current()
                prev = tuple_origin(self.last.get(key, ""))
                if prev:
                    snap.add(prev)
                self.docs[key] = snap
            self.last[key] = url
        except Exception:  # noqa: BLE001 — 기록 실패: 이력을 믿지 않게
            self.known = False

    def forget(self, frame: Any) -> None:
        """떨어진 프레임(framedetached)은 '지금 탭의 프레임 출처'에서 뺀다(이력 history 에는 남는다)."""
        try:
            key = impl(frame)
            self.last.pop(key, None)
            self.docs.pop(key, None)
        except TypeError:
            pass

    def snapshot(self) -> Set[str]:
        """이 탭이 연 팝업이 물려받을 이력(보수적으로 전부)."""
        out = set(self.history) | set(self.inherited) | self.current()
        for doc in list(self.docs.values()):
            out |= doc
        return out

    def of_document(self, frame_key: Any) -> Set[str]:
        """그 프레임 문서의 이력 ∪ 물려받은 이력. 문서 기록이 없으면(초기 문서) 탭 이력 전체(상위집합)."""
        doc = self.docs.get(frame_key) if frame_key is not None else None
        return set(self.inherited) | (set(doc) if doc is not None else self.snapshot())


_LINEAGE: "weakref.WeakKeyDictionary[Any, TabLineage]" = weakref.WeakKeyDictionary()
_WATCHED: "weakref.WeakSet[Any]" = weakref.WeakSet()


def lineage_record(page: Any) -> Optional[TabLineage]:
    try:
        return _LINEAGE.get(impl(page))
    except TypeError:
        return None


def _track(page: Any, known: bool, inherited: Iterable[str] = ()) -> Optional[TabLineage]:
    try:
        key = impl(page)
        rec = _LINEAGE.get(key)
        if rec is not None:
            return rec
        rec = TabLineage(known, inherited)
        _LINEAGE[key] = rec
    except TypeError:  # weakref 불가 객체: 기록 없음(이력 모름)
        return None
    for frame in list(getattr(page, "frames", []) or []):
        rec.see(frame)
    try:
        page.on("framenavigated", rec.see)
        page.on("framedetached", rec.forget)
    except Exception:  # noqa: BLE001 — 이동을 볼 수 없으면 이력을 믿지 않는다
        rec.known = False
    return rec


async def _confirm_no_opener(page: Any, rec: TabLineage, replay_history: bool = False) -> None:
    """Playwright 가 opener 를 주지 않은 탭: 브라우저(Target.getTargetInfo)도 opener 가 없다고 하면 물려받은 이력 = 빈
    집합(앎). replay_history(감시 설치 전부터 있던 탭)면 브라우저의 탭 이동 기록(Page.getNavigationHistory)의 출처를 이력에
    넣고서야 앎으로 — 기록을 못 읽으면 모름. opener 가 있던 탭(팝업)은 그 opener 의 당시 이력을 알 수 없어 모름."""
    session = None
    try:
        session = await page.context.new_cdp_session(page)
        info = await session.send("Target.getTargetInfo")
        if (info.get("targetInfo") or {}).get("openerId"):
            return
        if replay_history:
            nav = await session.send("Page.getNavigationHistory")
            for entry in nav.get("entries") or []:
                origin = tuple_origin(entry.get("url"))
                if origin:
                    rec.history.add(origin)
        rec.known = True
    except Exception:  # noqa: BLE001 — 확인 못 함: 모름 유지
        pass
    finally:
        if session is not None:
            try:
                await session.detach()
            except Exception:  # noqa: BLE001
                pass


_MISSING = object()


def on_new_page(page: Any) -> None:
    """context 'page' 이벤트(동기): 팝업이면 opener 이력을 그 순간 물려받는다."""
    if lineage_record(page) is not None:
        return
    opener = getattr(impl(page), "_opener", _MISSING)
    if opener is _MISSING:
        _track(page, known=False)
        return
    if opener is not None:
        try:
            parent = _LINEAGE.get(opener)
        except TypeError:
            parent = None
        if parent is None:  # 기록 없는 opener(감시 전부터 있던 탭): 이력 모름
            _track(page, known=False)
            return
        waiting = parent.pending is not None and not parent.pending.done()
        rec = _track(page, known=parent.known and not waiting, inherited=parent.snapshot())
        if rec is not None and waiting:
            # opener 의 '모름→앎' 확인이 아직이면 그 결과를 따른다.
            rec.pending = asyncio.ensure_future(_follow(parent, rec))
        return
    rec = _track(page, known=False)
    if rec is not None:
        try:
            rec.pending = asyncio.ensure_future(_confirm_no_opener(page, rec))
        except Exception:  # noqa: BLE001 — 이벤트 루프 없음: 모름
            rec.pending = None


async def _follow(parent: TabLineage, rec: TabLineage) -> None:
    if parent.pending is not None:
        try:
            await asyncio.shield(parent.pending)
        except Exception:  # noqa: BLE001
            pass
    rec.known = parent.known


def watch_lineage(context: Any) -> None:
    """이 컨텍스트의 탭 생성·이동을 기록한다(한 번만). 설치 전부터 있던 탭은 이력 모름."""
    if context is None:
        return
    try:
        key = impl(context)
        if key in _WATCHED:
            return
        context.on("page", on_new_page)
        _WATCHED.add(key)
    except Exception:  # noqa: BLE001 — 가짜 컨텍스트: 기록 없음(이력 모름)
        return
    for page in list(getattr(context, "pages", []) or []):
        if lineage_record(page) is None:
            rec = _track(page, known=False)
            if rec is not None and getattr(impl(page), "_opener", None) is None:
                try:
                    rec.pending = asyncio.ensure_future(_confirm_no_opener(page, rec, replay_history=True))
                except Exception:  # noqa: BLE001 — 이벤트 루프 없음: 모름
                    rec.pending = None


# ---------------------------------------------------------------------------- 실효 출처(브라우저 값)


class OriginProbe:
    """manual_site_hit 한 번 동안의 출처 조회 — 시간 상한은 **호출 전체** 기준(문서 수와 무관), 트리는 탭당 한 번."""

    def __init__(self, budget_s: float) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self.deadline = loop.time() + max(0.0, float(budget_s))
        self._trees: Dict[int, Any] = {}

    def remaining(self) -> float:
        return self.deadline - self._loop.time()

    async def _bounded(self, aw: Any, cap: Optional[float] = None) -> Any:
        left = self.remaining()
        if cap is not None:
            left = min(left, cap)
        if left <= 0:
            if asyncio.iscoroutine(aw):
                aw.close()
            raise asyncio.TimeoutError()
        return await asyncio.wait_for(aw, left)

    @staticmethod
    def _page_and_chain(src: Any) -> Any:
        main = getattr(src, "main_frame", None)
        if main is not None:  # Page
            return src, [main]
        page = getattr(src, "page", None)
        chain = [src]
        cur = getattr(src, "parent_frame", None)
        while cur is not None and len(chain) < 33:
            chain.append(cur)
            cur = getattr(cur, "parent_frame", None)
        return page, chain

    async def _session_tree(self, owner: Any, context: Any) -> Any:
        key = id(owner)
        if key in self._trees:
            return self._trees[key]
        session = await self._bounded(context.new_cdp_session(owner))
        try:
            tree = await self._bounded(session.send("Page.getFrameTree"))
        except BaseException:
            try:
                await session.detach()
            except Exception:  # noqa: BLE001
                pass
            raise
        self._trees[key] = (session, tree)
        return self._trees[key]

    async def close(self) -> None:
        for session, _ in list(self._trees.values()):
            try:
                await session.detach()
            except Exception:  # noqa: BLE001
                pass
        self._trees.clear()

    @staticmethod
    def _node_url(frame: Dict[str, Any]) -> str:
        return str(frame.get("url") or "") + str(frame.get("urlFragment") or "")

    @classmethod
    def _matches(cls, node: Dict[str, Any], urls: List[str]) -> List[Dict[str, Any]]:
        """트리 뿌리 = urls[-1](가장 바깥) 에서 시작해 주소 사슬이 같은 자손 노드들(보수적으로 전부)."""
        if not urls or cls._node_url(node.get("frame") or {}) != urls[-1]:
            return []
        if len(urls) == 1:
            return [node]
        out: List[Dict[str, Any]] = []
        for child in node.get("childFrames") or []:
            out.extend(cls._matches(child, urls[:-1]))
        return out

    async def _world_origin(self, session: Any, frame_id: str) -> str:
        world = await self._bounded(session.send("Page.createIsolatedWorld", {
            "frameId": frame_id, "worldName": ORIGIN_WORLD, "grantUniveralAccess": False}))
        res = await self._bounded(session.send("Runtime.evaluate", {
            "expression": "String(self.origin)", "contextId": world["executionContextId"], "returnByValue": True}))
        if res.get("exceptionDetails"):
            raise RuntimeError("격리 world 평가 실패")
        return str((res.get("result") or {}).get("value") or "")

    async def origins(self, src: Any) -> Optional[List[str]]:
        """그 문서의 실효 출처 후보들(튜플 출처 또는 OPAQUE). 판정 불가면 None."""
        try:
            page, chain = self._page_and_chain(src)
            context = getattr(page, "context", None)
            if page is None or context is None:
                return None
            urls = [str(getattr(f, "url", "") or "") for f in chain]
            # 페이지 세션의 트리(뿌리 = 최상위)를 먼저, 못 찾으면 사슬의 각 프레임을 OOPIF 뿌리로(그 세션 트리의 뿌리 =
            # 그 프레임) — 사이트 격리가 켜진 브라우저에서 다른 프로세스 프레임은 페이지 트리에 없다.
            owners = [(page, urls)] + [(chain[j], urls[:j + 1]) for j in range(len(chain) - 1)]
            for owner, sub in owners:
                try:
                    session, tree = await self._session_tree(owner, context)
                except asyncio.TimeoutError:
                    return None
                except Exception:  # noqa: BLE001 — 이 frame 은 따로 세션이 없다(OOPIF 아님)
                    continue
                nodes = self._matches(tree.get("frameTree") or {}, sub)
                if not nodes:
                    continue
                out: List[str] = []
                for node in nodes:
                    frame = node.get("frame") or {}
                    so = tuple_origin(frame.get("securityOrigin"))
                    if so:
                        out.append(so)
                        continue
                    value = await self._world_origin(session, str(frame.get("id") or ""))
                    origin = tuple_origin(value)
                    if origin:
                        out.append(origin)
                    elif value == OPAQUE:
                        out.append(OPAQUE)
                    else:
                        return None
                return out
            return None
        except Exception:  # noqa: BLE001 — 시간 초과·떨어진 프레임·세션 실패: 판정 불가
            return None

    async def page_world(self, src: Any) -> List[str]:
        """보조(차단 근거로만): 페이지 world 의 self.origin·location.origin. 실패·시간 초과는 빈 목록."""
        evaluate = getattr(src, "evaluate", None)
        if not callable(evaluate):
            return []
        try:
            got = await self._bounded(evaluate("() => [String(self.origin), String(location.origin)]"), cap=0.25)
        except Exception:  # noqa: BLE001
            return []
        return [str(v) for v in (got if isinstance(got, (list, tuple)) else [got]) if v]

    async def lineage(self, src: Any, top: Any) -> Optional[Set[str]]:
        """불투명 문서의 생성 이력(그 탭 기록). 모르면 None."""
        rec = lineage_record(top)
        if rec is None:
            return None
        if rec.pending is not None and not rec.pending.done():
            try:
                await self._bounded(asyncio.shield(rec.pending))
            except Exception:  # noqa: BLE001
                return None
        if not rec.known:
            return None
        main = getattr(src, "main_frame", None)
        frame = main if main is not None else src
        try:
            key = impl(frame)
            return rec.of_document(key)
        except TypeError:
            return rec.snapshot() | set(rec.inherited)
