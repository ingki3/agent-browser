"""WS-64 R2 — B2 실효 출처 조회기(호출 전체 상한·보조 값은 차단 근거로만)와 B3 lineage 기록(브라우저 없음).

실제 Chromium 값(초기 빈 문서 securityOrigin '://' · 격리 world self.origin)은 tests/interface/test_ws64_r2_server.py.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from contracts import ActionType
from security import manual_origin as mo
from security import manual_sites as ms
from security.manual_sites import ManualSites

SITES = ManualSites.of(["srail.kr"])
CLICK = {"element_id": "@e1"}


class _Emitter:
    def __init__(self) -> None:
        self.handlers: Dict[str, List[Any]] = {}

    def on(self, event: str, fn: Any) -> None:
        self.handlers.setdefault(event, []).append(fn)

    def emit(self, event: str, *args: Any) -> None:
        for fn in self.handlers.get(event, []):
            fn(*args)


class _Frame:
    def __init__(self, url: str, parent: Any = None) -> None:
        self.url = url
        self.parent_frame = parent


class _Page(_Emitter):
    def __init__(self, url: str, opener: Any = None, context: Any = None) -> None:
        super().__init__()
        self.main_frame = _Frame(url)
        self.frames = [self.main_frame]
        self._opener = opener
        self.context = context

    @property
    def url(self) -> str:
        return self.main_frame.url

    def navigate(self, frame: _Frame, url: str) -> None:
        frame.url = url
        self.emit("framenavigated", frame)


class _Context(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.pages: List[Any] = []

    def open(self, url: str, opener: Any = None) -> _Page:
        page = _Page(url, opener=opener, context=self)
        self.pages.append(page)
        self.emit("page", page)
        return page


def _known_root(ctx: _Context, url: str) -> _Page:
    page = ctx.open(url)
    rec = mo.lineage_record(page)
    assert rec is not None and rec.pending is not None  # opener 없음 확인 대기
    rec.pending.cancel()
    rec.pending = None
    rec.known = True  # 시험: 브라우저가 opener 없음을 확인했다고 둔다
    return page


# ======================================================================= B3 lineage 기록


async def test_popup_inherits_opener_history_even_if_opener_left_first():
    ctx = _Context()
    mo.watch_lineage(ctx)
    root = _known_root(ctx, "https://srail.kr/main")
    root.navigate(root.main_frame, "https://plain.example/")  # opener 이동 이벤트가 팝업 이벤트보다 먼저 와도
    popup = ctx.open("blob:null/uuid", opener=root)
    rec = mo.lineage_record(popup)
    assert rec is not None and rec.known and "https://srail.kr" in rec.of_document(popup.main_frame)
    grand = ctx.open("blob:null/2", opener=popup)  # 불투명 팝업이 또 팝업
    grec = mo.lineage_record(grand)
    assert grec is not None and grec.known and "https://srail.kr" in grec.of_document(grand.main_frame)


async def test_frame_document_lineage_is_tab_frames_at_navigation():
    ctx = _Context()
    mo.watch_lineage(ctx)
    root = _known_root(ctx, "https://srail.kr/main")
    root.navigate(root.main_frame, "https://plain.example/")
    ad = _Frame("about:blank", parent=root.main_frame)
    root.frames.append(ad)
    root.navigate(ad, "about:srcdoc")  # 목록 밖 문서의 sandbox 광고 — 지금 탭 프레임 = 목록 밖
    rec = mo.lineage_record(root)
    assert rec is not None and rec.of_document(ad) == {"https://plain.example"}
    # 목록 문서가 최상위를 불투명 문서로 이동시키면(직전 주소 = 목록) 그 문서 이력에 목록 출처
    root.navigate(root.main_frame, "https://srail.kr/again")
    root.navigate(root.main_frame, "blob:null/top")
    assert "https://srail.kr" in rec.of_document(root.main_frame)


async def test_pages_before_watch_and_unknown_opener_are_unknown():
    ctx = _Context()  # CDP 없음 — 브라우저 확인 불가
    early = _Page("https://plain.example/", context=ctx)
    ctx.pages.append(early)
    mo.watch_lineage(ctx)
    rec = mo.lineage_record(early)
    assert rec is not None and rec.known is False
    popup = ctx.open("blob:null/x", opener=early)
    prec = mo.lineage_record(popup)
    assert prec is not None and prec.known is False


class _InfoSession:
    def __init__(self, opener_id: str, entries: List[str]) -> None:
        self.opener_id, self.entries = opener_id, entries

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"openerId": self.opener_id} if self.opener_id else {}}
        if method == "Page.getNavigationHistory":
            return {"entries": [{"url": u} for u in self.entries]}
        raise AssertionError(method)

    async def detach(self) -> None:
        return None


@pytest.mark.parametrize("opener_id,known", [("", True), ("T-OPENER", False)])
async def test_pre_existing_page_bootstraps_from_browser_history(opener_id, known):
    class _Ctx(_Context):
        async def new_cdp_session(self, page: Any) -> Any:
            return _InfoSession(opener_id, ["about:blank", "https://srail.kr/a", "https://plain.example/"])

    ctx = _Ctx()
    early = _Page("https://plain.example/", context=ctx)
    ctx.pages.append(early)
    mo.watch_lineage(ctx)  # 감시 전부터 있던 탭
    rec = mo.lineage_record(early)
    assert rec is not None and rec.pending is not None
    await rec.pending
    assert rec.known is known
    if known:  # 브라우저 탭 이동 기록의 출처가 이력에 — 목록 사이트를 거쳤으면 그 탭의 불투명 문서는 막힌다
        assert "https://srail.kr" in rec.snapshot()


async def test_opaque_hit_uses_lineage_and_unknown_blocks(monkeypatch):
    class _Probe:
        def __init__(self, budget_s: float) -> None:
            pass

        async def origins(self, src: Any) -> Any:
            return ["null"]

        async def lineage(self, src: Any, top: Any) -> Any:
            return getattr(top, "_lin", None)

        async def page_world(self, src: Any) -> List[str]:
            return []

    monkeypatch.setattr(ms, "_origin_probe", _Probe)
    page = SimpleNamespace(url="blob:null/x", _lin={"https://srail.kr"})
    assert await ms.manual_site_hit(SimpleNamespace(page=page, root_page=None), ActionType.CLICK, CLICK, SITES) == "srail.kr"
    page._lin = {"https://plain.example"}
    assert await ms.manual_site_hit(SimpleNamespace(page=page, root_page=None), ActionType.CLICK, CLICK, SITES) is None
    page._lin = None  # 이력 모름
    assert await ms.manual_site_hit(SimpleNamespace(page=page, root_page=None), ActionType.CLICK, CLICK, SITES) == "?"
    # 목록이 비면 차단 없음
    assert await ms.manual_site_hit(SimpleNamespace(page=page, root_page=None), ActionType.CLICK, CLICK,
                                    ManualSites()) is None


async def test_page_world_is_block_only(monkeypatch):
    class _Probe:
        def __init__(self, budget_s: float) -> None:
            pass

        async def origins(self, src: Any) -> Any:
            return getattr(src, "_browser", None)

        async def lineage(self, src: Any, top: Any) -> Any:
            return set()

        async def page_world(self, src: Any) -> List[str]:
            return list(getattr(src, "_world", []))

    monkeypatch.setattr(ms, "_origin_probe", _Probe)
    hit = ms.manual_site_hit
    # 페이지 world 가 목록이라 하면 막는다(브라우저 값이 목록 밖이어도)
    p = SimpleNamespace(url="about:blank", _browser=["https://plain.example"], _world=["https://srail.kr"])
    assert await hit(SimpleNamespace(page=p, root_page=None), ActionType.CLICK, CLICK, SITES) == "srail.kr"
    # 페이지 world 가 목록 밖이라 해도 브라우저 값이 목록이면 막는다 / 브라우저 값을 못 읽으면 막는다
    p = SimpleNamespace(url="about:blank", _browser=["https://srail.kr"], _world=["https://plain.example"])
    assert await hit(SimpleNamespace(page=p, root_page=None), ActionType.CLICK, CLICK, SITES) == "srail.kr"
    p = SimpleNamespace(url="about:blank", _browser=None, _world=["https://plain.example"])
    assert await hit(SimpleNamespace(page=p, root_page=None), ActionType.CLICK, CLICK, SITES) == "?"


# ======================================================================= B2 조회기: 호출 전체 상한


class _HangSession:
    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        await asyncio.sleep(30)

    async def detach(self) -> None:
        return None


class _HangContext:
    async def new_cdp_session(self, owner: Any) -> Any:
        return _HangSession()


@pytest.mark.parametrize("count", [1, 3, 6])
async def test_budget_is_per_call_not_per_document(monkeypatch, count):
    monkeypatch.setattr(ms, "EFFECTIVE_ORIGIN_TIMEOUT_S", 0.3)
    ctx = _HangContext()
    top = SimpleNamespace(url="about:blank", main_frame=_Frame("about:blank"), context=ctx, frames=[])
    frames = []
    parent = top.main_frame
    for _ in range(count - 1):
        f = SimpleNamespace(url="about:blank", parent_frame=parent, page=top)
        frames.append(f)
        parent = f
    target = frames[-1] if frames else top
    t = time.perf_counter()
    got = await ms.manual_site_hit(SimpleNamespace(page=target, root_page=top if frames else None),
                                   ActionType.CLICK, CLICK, SITES)
    elapsed = time.perf_counter() - t
    assert got == "?" and elapsed < 0.3 * 1.8, elapsed  # 문서 수와 무관하게 상한 한 번


async def test_tree_matching_is_conservative():
    tree = {"frame": {"url": "https://plain.example/", "id": "root"}, "childFrames": [
        {"frame": {"url": "about:blank", "id": "a"}},
        {"frame": {"url": "about:blank", "id": "b"}},
        {"frame": {"url": "about:srcdoc", "id": "c"}},
    ]}
    nodes = mo.OriginProbe._matches(tree, ["about:blank", "https://plain.example/"])
    assert sorted(n["frame"]["id"] for n in nodes) == ["a", "b"]  # 같은 주소 사슬 전부(가장 나쁜 판정)
    assert mo.OriginProbe._matches(tree, ["about:blank", "https://other.example/"]) == []


def test_tuple_origin():
    assert mo.tuple_origin("https://SRAIL.kr:8443/x") == "https://srail.kr:8443"
    assert mo.tuple_origin("blob:https://srail.kr/u") == "https://srail.kr"
    for raw in ("about:blank", "blob:null/u", "data:text/html,x", "://", "null", ""):
        assert mo.tuple_origin(raw) is None
