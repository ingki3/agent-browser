"""WS-68 R4 (#140) — 저장 트랜잭션 동안 주 프레임 문서 커밋 이벤트 구독(표본 창 제거).

* remember_page 는 첫 결속값 읽기 **전에** 주 프레임 새 문서 커밋 계수기(Playwright 내부 프레임 'navigated' 이벤트의
  newDocument 만 — pushState·hash 제외)를 걸고, 저장 직전·최종 성공 반환 직전(마지막 await 뒤) **동기적으로** 계수를
  본다. 시작 이후 1회라도 커밋이 있으면 저장 전 = E_CHANGED, 저장 뒤 = undo_add + E_CHANGED. 계수기는 finally 해제.
* 계수기를 걸 수 없으면(내부 이벤트 부재·연결 실패) 판정 불가 E_UNJUDGED — 저장하지 않는다(fail-closed).
* 표본(결속값 3회 읽기)은 이중 방어로 남는다 — 표본 비교를 무력화해도 probe_binding_captcha 전 변형 디스크 0.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from typing import Any, List, Optional

import pytest

from browser.document_commits import watch_document_commits
from recipes import saved_pages as sp
from recipes.saved_pages import SavedPageStore

from test_ws68_r1 import PAGE_URL, _Doc, _run, _svc

B1 = ("F", "L1", 1, PAGE_URL)


# ---------------------------------------------------------------------------
# 단위(가짜 판독기) — 표본은 늘 같고(결속값 불변) 커밋 이벤트만 온다
# ---------------------------------------------------------------------------


class _Watch:
    def __init__(self, log: List[str]) -> None:
        self.commits = 0
        self.closed = 0
        self.log = log

    def close(self) -> None:
        self.closed += 1
        self.log.append("close")


class _CommitDoc(_Doc):
    """n 번째 결속값 읽기 도중(값은 그대로) 새 문서 커밋 1회 — 표본으로는 보이지 않는 reload."""

    def __init__(self, commit_at: Optional[int]) -> None:
        super().__init__()
        self.commit_at = commit_at
        self.log: List[str] = []
        self.watches: List[_Watch] = []

    def watch_top_document_commits(self) -> Any:
        self.log.append("watch")
        w = _Watch(self.log)
        self.watches.append(w)
        return w

    async def top_document_binding(self) -> Any:
        self.log.append("bind")
        out = await super().top_document_binding()
        if self.binds == self.commit_at:
            for w in self.watches:
                w.commits += 1
        return out


@pytest.mark.parametrize("commit_at", [1, 2, 3, 4], ids=["start", "check_screen", "pre_save", "final"])
def test_r4_any_commit_after_watch_is_refused_even_if_samples_agree(commit_at, tmp_path):
    path = tmp_path / sp.FILE_NAME
    doc = _CommitDoc(commit_at)
    svc = _svc(doc, store=SavedPageStore(path))
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_CHANGED, out
    assert len(svc.store) == 0
    assert not path.exists() or json.loads(path.read_text(encoding="utf-8"))["pages"] == []
    assert [w.closed for w in doc.watches] == [1]


def test_r4_final_commit_restores_prior_name(tmp_path):
    """저장 뒤(마지막 결속값 읽기 도중) 커밋 — 이름 갱신도 되돌린다."""
    store = SavedPageStore(tmp_path / sp.FILE_NAME)
    store.add("default", PAGE_URL, "예전")
    doc = _CommitDoc(4)
    svc = _svc(doc, store=store)
    out = _run(svc, name="새이름")
    assert not out["success"] and out["error_message"] == sp.E_CHANGED
    assert [p["name"] for p in store.list("default")] == ["예전"]


def test_r4_watch_is_attached_before_first_binding_read_and_released():
    doc = _CommitDoc(None)
    svc = _svc(doc)
    out = _run(svc)
    assert out["success"], out
    assert doc.log[0] == "watch" and doc.log[1] == "bind" and doc.log[-1] == "close"
    assert doc.log.count("watch") == 1 and doc.log.count("close") == 1


@pytest.mark.parametrize("watch", [
    None,                                            # 내부 이벤트 없음
    "raise",                                         # 연결 실패
    NS(commits=None, close=lambda: None),            # 계수 못 함
    NS(commits=True, close=lambda: None),            # bool 은 계수가 아니다
    "missing",                                       # 판독기에 계수기 자체가 없음
], ids=["none", "raise", "no_count", "bool", "missing"])
def test_r4_unwired_commit_watch_is_unjudged_and_saves_nothing(watch, monkeypatch):
    doc = _Doc()
    svc = _svc(doc)
    disp = svc.server._dispatcher
    if watch == "missing":
        monkeypatch.delattr(_Doc, "watch_top_document_commits", raising=False)
    else:
        def make() -> Any:
            if watch == "raise":
                raise RuntimeError("emitter")
            return watch

        monkeypatch.setattr(_Doc, "watch_top_document_commits", lambda self: make(), raising=False)
    assert disp is not None
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_UNJUDGED, out
    assert len(svc.store) == 0


def test_r4_watch_released_on_refusal_paths():
    class Boom(_CommitDoc):
        async def top_document_binding(self) -> Any:
            self.log.append("bind")
            raise RuntimeError("cdp")

    doc = Boom(None)
    out = _run(_svc(doc))
    assert not out["success"] and out["error_message"] == sp.E_UNJUDGED
    assert [w.closed for w in doc.watches] == [1]


def test_r4_watch_helper_without_internal_emitter_is_none():
    """공개 API 만 있는 페이지(내부 프레임 이벤트 없음 — Playwright 업그레이드 대역)·프레임 없음은 None."""
    assert watch_document_commits(NS(main_frame=NS())) is None
    assert watch_document_commits(NS()) is None
    assert watch_document_commits(None) is None

    class BadEmitter:
        def on(self, *_a: Any) -> None:
            raise RuntimeError("nope")

    assert watch_document_commits(NS(main_frame=NS(_impl_obj=NS(_event_emitter=BadEmitter())))) is None


# ---------------------------------------------------------------------------
# E2E(실제 Chromium + 로컬 http)
# ---------------------------------------------------------------------------

from test_ws68_remember_strict import _PAGES, _Site  # noqa: E402

_CLEAN = (200, "<title>Orders</title><h1>Order list</h1>")
_CAPTCHA = (200, "<title>Verify you are human</title><h1>verify you are human</h1>"
                 "<div class='cf-turnstile' style='width:300px;height:65px'></div>")


@pytest.mark.asyncio
async def test_r4_internal_navigated_event_is_wired_and_counts_new_documents_only():
    """연결 단정: 실제 페이지에서 계수기가 내부 'navigated' 리스너로 붙고, reload 만 세고 pushState·hash 는 안 센다."""
    from interface.mcp_server import BrowserMCPServer

    _PAGES["/r4-wired"] = _CLEAN
    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            d = srv._dispatcher
            page = d.ctx.page
            await page.goto(site.url("/r4-wired"))
            emitter = page.main_frame._impl_obj._event_emitter  # noqa: SLF001
            base = len(emitter.listeners("navigated"))
            watch = d.watch_top_document_commits()
            assert watch is not None and watch.commits == 0
            assert len(emitter.listeners("navigated")) == base + 1
            await page.evaluate("history.pushState({}, '', '/r4-wired-pushed')")
            await page.evaluate("location.hash = 'x'")
            await page.wait_for_function("location.hash === '#x'")
            assert watch.commits == 0
            await page.reload()
            assert watch.commits == 1
            watch.close()
            assert len(emitter.listeners("navigated")) == base
            await page.reload()
            assert watch.commits == 1


async def _reload_inside_binding(path_name: str, at: int, frame_at: int, swap_to: Optional[tuple], tmp_path: Any,
                                 monkeypatch: Any, blind_samples: bool) -> tuple:
    """probe_binding_captcha: at 번째 결속값 읽기의 frame_at 번째 실제 Page.getFrameTree 응답 직후 실제 reload.

    blind_samples: 표본 비교 무력화 — 결속값 읽기는 실제로 하되(reload 시점 유지) 늘 첫 값을 돌려줘 비교가 항상
    같다고 본다. 이벤트 경로(와 판독기의 expect_loader)만 남는다.
    """
    from interface.mcp_server import BrowserMCPServer

    _PAGES[path_name] = _CLEAN
    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            d = srv._dispatcher
            page = d.ctx.page
            await page.goto(site.url(path_name))
            cdp = await d._gate_cdp_for(page)  # noqa: SLF001
            path = tmp_path / f"{path_name.strip('/')}.json"
            srv._saved_pages._store = SavedPageStore(path)
            native_bind, native_send = d.top_document_binding, cdp.send
            state = {"n": 0, "active": False, "frames": 0, "fired": False, "first": None}

            async def bind() -> Any:
                state["n"] += 1
                state["active"], state["frames"] = state["n"] == at, 0
                try:
                    got = await native_bind()
                finally:
                    state["active"] = False
                if blind_samples:
                    if state["first"] is None:
                        state["first"] = got
                    return state["first"]
                return got

            async def send(method: str, params: Any = None) -> Any:
                result = await native_send(method, params)
                if state["active"] and method == "Page.getFrameTree":
                    state["frames"] += 1
                    if state["frames"] == frame_at and not state["fired"]:
                        state["fired"] = True
                        if swap_to is not None:
                            _PAGES[path_name] = swap_to
                        await page.reload()
                return result

            d.top_document_binding, cdp.send = bind, send
            try:
                out = await srv.call_server_tool("browser_recipe", {"op": "remember_page", "name": "결속캡차검증"})
            finally:
                d.top_document_binding, cdp.send = native_bind, native_send
            disk = json.loads(path.read_text(encoding="utf-8"))["pages"] if path.exists() else []
            emitter = page.main_frame._impl_obj._event_emitter  # noqa: SLF001
            leftover = len(emitter.listeners("navigated"))
            return out, disk, state, leftover


_VARIANTS = [(at, fa) for at in (1, 2, 3, 4) for fa in (1, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("blind_samples", [False, True], ids=["samples", "events_only"])
@pytest.mark.parametrize("at,frame_at", _VARIANTS, ids=[f"at{a}-frame{f}" for a, f in _VARIANTS])
async def test_r4_e2e_captcha_reload_at_every_binding_boundary_is_never_saved(at, frame_at, blind_samples, tmp_path,
                                                                               monkeypatch):
    out, disk, state, _ = await _reload_inside_binding(
        f"/r4-captcha-{at}-{frame_at}-{int(blind_samples)}", at, frame_at, _CAPTCHA, tmp_path, monkeypatch,
        blind_samples)
    assert state["fired"], state
    assert not out["success"], out
    assert disk == []
    if at == 4:  # 마지막 읽기 도중 reload — 표본이 눈멀어도 이벤트 계수가 잡는다(판정 불가가 아니라 '바뀜')
        assert out["error_message"] == sp.E_CHANGED, out


@pytest.mark.asyncio
@pytest.mark.parametrize("at", [2, 4])
async def test_r4_e2e_plain_reload_during_transaction_is_now_refused(at, tmp_path, monkeypatch):
    """R3 의 NB(정상 reload 경합은 저장·거부 둘 다 허용)를 거부로 고정 — 문서가 바뀐 트랜잭션은 저장하지 않는다."""
    out, disk, state, leftover = await _reload_inside_binding(
        f"/r4-plain-{at}", at, 2, None, tmp_path, monkeypatch, False)
    assert state["fired"]
    assert not out["success"] and out["error_message"] in (sp.E_CHANGED, sp.E_UNJUDGED), out
    if at == 4:
        assert out["error_message"] == sp.E_CHANGED
    assert disk == []


@pytest.mark.asyncio
async def test_r4_e2e_no_over_refusal_for_spa_push_and_hash(tmp_path):
    """과잉 거부 0: pushState SPA·해시 이동 뒤의 페이지는 저장된다(새 문서 커밋 아님). 계수기는 해제된다."""
    from interface.mcp_server import BrowserMCPServer

    _PAGES["/r4-spa"] = _CLEAN
    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            d = srv._dispatcher
            page = d.ctx.page
            path = tmp_path / sp.FILE_NAME
            srv._saved_pages._store = SavedPageStore(path)
            await page.goto(site.url("/r4-spa"))
            emitter = page.main_frame._impl_obj._event_emitter  # noqa: SLF001
            base = len(emitter.listeners("navigated"))
            await page.evaluate("history.pushState({}, '', '/r4-spa/inbox')")
            out = await srv.call_server_tool("browser_recipe", {"op": "remember_page", "name": "받은편지함"})
            assert out["success"], out
            await page.evaluate("location.hash = 'thread-1'")
            await page.wait_for_function("location.hash === '#thread-1'")
            out2 = await srv.call_server_tool("browser_recipe", {"op": "remember_page", "name": "글타래"})
            assert out2["success"], out2
            assert len(emitter.listeners("navigated")) == base
            disk = json.loads(path.read_text(encoding="utf-8"))["pages"]
            assert disk and all("/r4-spa/inbox" in p["url"] for p in disk), disk


@pytest.mark.asyncio
@pytest.mark.parametrize("at", [2, 4])
async def test_r4_e2e_same_document_navigation_during_transaction_is_not_counted(at, tmp_path):
    """과잉 거부 0(트랜잭션 도중): 같은 주소 replaceState 는 프레임 'navigated'(같은 문서)를 내지만 새 문서가 아니다 —
    결속값도 그대로라 저장된다. 조건 성립 확인: 공개 framenavigated 가 실제로 발화했다."""
    from interface.mcp_server import BrowserMCPServer

    _PAGES["/r4-spa-replace"] = _CLEAN
    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            d = srv._dispatcher
            page = d.ctx.page
            await page.goto(site.url("/r4-spa-replace"))
            path = tmp_path / sp.FILE_NAME
            srv._saved_pages._store = SavedPageStore(path)
            navs: List[str] = []
            page.on("framenavigated", lambda f: navs.append(f.url))
            native_bind = d.top_document_binding
            state = {"n": 0}

            async def bind() -> Any:
                state["n"] += 1
                if state["n"] == at:
                    await page.evaluate("history.replaceState(history.state, '', location.href)")
                    await page.wait_for_timeout(50)
                return await native_bind()

            d.top_document_binding = bind
            try:
                out = await srv.call_server_tool("browser_recipe", {"op": "remember_page", "name": "같은문서"})
            finally:
                d.top_document_binding = native_bind
            assert navs, "replaceState 가 프레임 이동 이벤트를 내지 않으면 이 시험은 아무것도 재지 않는다"
            assert out["success"], out
            assert len(json.loads(path.read_text(encoding="utf-8"))["pages"]) == 1
