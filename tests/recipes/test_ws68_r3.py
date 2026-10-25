"""WS-68 R3 (#140) — 결속값 읽기 혼합 스냅샷(최종 결속값 읽기 도중 같은 URL 캡차 reload) 회귀 시험.

* top_document_binding 은 frame(loaderId) → history → frame(loaderId) 로 읽어 앞뒤 loaderId 가 같을 때만 인정한다.
  다르면 1회 다시 읽고, 그래도 다르면 None(판정 불가). 이전 loader + 현재 history 가 섞인 값을 내지 않는다.
* saved_pages: 저장 뒤 마지막 결속값 읽기가 실패(None·예외)하거나 다르면 undo_add 로 되돌린다 — 디스크 0건.
* E2E: 검증 재현 probe_binding_captcha(실제 Page.getFrameTree 응답 직후 같은 URL 을 캡차로 바꿔 실제 reload) 편입.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from recipes import saved_pages as sp
from recipes.saved_pages import SavedPageStore

from test_ws68_r1 import PAGE_URL, _Doc, _run, _svc

B1 = ("F", "L1", 1, PAGE_URL)


# ---------------------------------------------------------------------------
# 결속값 읽기 — 단위(가짜 CDP)
# ---------------------------------------------------------------------------


class _BindCdp:
    """Page.getFrameTree 가 부를 때마다 loaders 의 다음 값을 낸다(마지막 값 유지)."""

    def __init__(self, loaders: List[str]) -> None:
        self.loaders = list(loaders)
        self.calls: List[str] = []

    def on(self, *_a: Any) -> None:
        pass

    async def send(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        self.calls.append(method)
        if method == "Page.getFrameTree":
            loader = self.loaders.pop(0) if len(self.loaders) > 1 else self.loaders[0]
            return {"frameTree": {"frame": {"id": "F", "loaderId": loader}}}
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"id": 1, "url": PAGE_URL}]}
        raise AssertionError(method)


def _dispatcher(fake: _BindCdp) -> Any:
    from actions.dispatcher import ActionDispatcher

    d = ActionDispatcher.__new__(ActionDispatcher)

    async def gate_cdp(_page: Any) -> Any:
        return fake

    d._gate_cdp_for = gate_cdp  # noqa: SLF001
    d._top_page = lambda: object()  # noqa: SLF001
    return d


async def test_r3_stable_binding_reads_loader_before_and_after_history():
    fake = _BindCdp(["L1"])
    assert await _dispatcher(fake).top_document_binding() == B1
    assert fake.calls == ["Page.getFrameTree", "Page.getNavigationHistory", "Page.getFrameTree"]


async def test_r3_mixed_snapshot_is_reread_once():
    """앞 frame 은 이전 문서(L1), 그 사이 reload — 섞인 값(L1+현재 history) 대신 다시 읽어 새 문서(L2)를 낸다."""
    fake = _BindCdp(["L1", "L2", "L2", "L2"])
    assert await _dispatcher(fake).top_document_binding() == ("F", "L2", 1, PAGE_URL)
    assert fake.calls.count("Page.getFrameTree") == 4 and fake.calls.count("Page.getNavigationHistory") == 2


async def test_r3_persistent_mixed_snapshot_is_unjudged():
    fake = _BindCdp(["L1", "L2", "L3", "L4", "L5"])
    assert await _dispatcher(fake).top_document_binding() is None
    assert fake.calls.count("Page.getFrameTree") == 4  # 1회만 다시 읽는다


# ---------------------------------------------------------------------------
# saved_pages — 저장 뒤 결속값 읽기 실패·불일치는 되돌림
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("last, message", [
    (None, sp.E_UNJUDGED),                          # 읽기 실패(혼합 스냅샷이 계속됨 포함) = 판정 불가
    (RuntimeError("gone"), sp.E_UNJUDGED),
    (("F", "L2", 1, PAGE_URL), sp.E_CHANGED),       # 불일치 = 바뀜
], ids=["none", "error", "changed"])
def test_r3_final_binding_failure_or_change_rolls_back(last, message, tmp_path):
    path = tmp_path / sp.FILE_NAME
    doc = _Doc(bindings=[B1, B1, B1, last])
    svc = _svc(doc, SavedPageStore(path))
    out = _run(svc)
    assert not out["success"] and out["error_message"] == message, out
    assert len(svc.store) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["pages"] == []


@pytest.mark.parametrize("at", [0, 1, 2])
def test_r3_none_binding_is_refused_at_every_stage(at):
    bindings: List[Any] = [B1, B1, B1, B1]
    bindings[at] = None
    svc = _svc(_Doc(bindings=bindings))
    out = _run(svc)
    assert not out["success"] and len(svc.store) == 0


# ---------------------------------------------------------------------------
# E2E(실제 Chromium + 로컬 http) — probe_binding_captcha 편입
# ---------------------------------------------------------------------------

from test_ws68_remember_strict import _PAGES, _Site  # noqa: E402

_CLEAN = (200, "<title>Orders</title><h1>Order list</h1>")
_CAPTCHA = (200, "<title>Verify you are human</title><h1>verify you are human</h1>"
                 "<div class='cf-turnstile' style='width:300px;height:65px'></div>")


async def _final_binding_reload(path_name: str, swap_to: Optional[tuple], tmp_path: Any) -> tuple:
    """마지막(4번째) 결속값 읽기의 실제 Page.getFrameTree 응답 직후 같은 URL 을 (캡차로) 바꿔 실제 reload."""
    from interface.mcp_server import BrowserMCPServer

    _PAGES[path_name] = _CLEAN
    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            d = srv._dispatcher
            page = d.ctx.page
            await page.goto(site.url(path_name))
            cdp = await d._gate_cdp_for(page)  # noqa: SLF001
            before = await d.top_document_binding()
            path = tmp_path / sp.FILE_NAME
            srv._saved_pages._store = SavedPageStore(path)
            native_bind, native_send = d.top_document_binding, cdp.send
            state = {"n": 0, "final": False, "fired": False}

            async def bind() -> Any:
                state["n"] += 1
                state["final"] = state["n"] == 4
                try:
                    return await native_bind()
                finally:
                    state["final"] = False

            async def send(method: str, params: Any = None) -> Any:
                result = await native_send(method, params)
                if state["final"] and method == "Page.getFrameTree" and not state["fired"]:
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
            after = await d.top_document_binding()
            disk = json.loads(path.read_text(encoding="utf-8"))["pages"] if path.exists() else []
            screen = await d.isolated_top_evaluate(
                "({title: document.title, widget: !!document.querySelector('.cf-turnstile')})")
            control = None
            if swap_to is not None:
                control = await srv.call_server_tool("browser_recipe", {"op": "remember_page", "name": "새캡차거부대조"})
            return out, disk, before, after, state, screen, control


@pytest.mark.asyncio
async def test_r3_e2e_captcha_reload_inside_final_binding_read_is_never_saved(tmp_path):
    out, disk, before, after, state, screen, control = await _final_binding_reload(
        "/r3-binding-captcha", _CAPTCHA, tmp_path)
    assert state["fired"] and state["n"] == 4
    assert before[1] != after[1] and before[2:] == after[2:]  # 표본: loader 만 바뀌고 history id·URL 같음
    assert screen["widget"]  # 실제 화면은 캡차
    assert control is not None and not control["success"]  # 양성 대조: 같은 캡차는 일반 호출로도 거부
    assert not out["success"] and out["error_message"] == sp.E_CHANGED, out  # 다시 읽은 새 loader ≠ 시작 loader
    assert disk == []


@pytest.mark.asyncio
async def test_r3_e2e_plain_reload_inside_final_binding_read_is_consistent(tmp_path):
    """NB: 정상 reload 경합은 거부(롤백) 또는 저장 어느 쪽이든 허용 — 단 응답과 디스크가 일치한다."""
    out, disk, before, after, state, screen, _ = await _final_binding_reload("/r3-binding-plain", None, tmp_path)
    assert state["fired"] and before[1] != after[1] and not screen["widget"]
    if out["success"]:
        assert len(disk) == 1 and disk[0]["url"].endswith("/r3-binding-plain")
    else:
        assert disk == [] and out["error_message"] in (sp.E_CHANGED, sp.E_UNJUDGED)
