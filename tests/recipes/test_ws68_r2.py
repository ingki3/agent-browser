"""WS-68 R2 (#140) — 최종 확인 판정 도중 문서 교체(B2 잔여) 회귀 시험.

* 샌드위치: 모든 엄격 판정은 결속값 읽기 → 엄격 판정 → 결속값 다시 읽기. 뒤쪽 결속값이 다르면 그 판정은 무효
  (= 거부, 저장 뒤 단계면 undo_add 로 되돌림). 같은 URL reload(loaderId 변경)도 잡는다.
* 재시도 결속: 격리 world 평가기의 'context 없음' 재시도는 엄격 모드(expect_loader)에서 시작 문서(loaderId)와
  같은 문서일 때만 유효 — 다르면 판정 불가. 기본 모드(WS-67 포커스·좌표 판정)의 재시도 동작은 그대로.
* E2E: 검증 재현(probe_final_race 3변형·final_binding_reload)을 실제 Chromium 으로 편입 — 디스크 0건.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from recipes import saved_pages as sp
from recipes.saved_pages import SavedPageStore

from test_ws68_r1 import CAPTCHA, CLEAN, PAGE_URL, _Doc, _run, _svc

B1 = ("F", "L1", 1, PAGE_URL)


# ---------------------------------------------------------------------------
# 샌드위치 — 단위(가짜 판독기)
# ---------------------------------------------------------------------------


class _SwapDoc(_Doc):
    """n 번째 엄격 판정이 **정상 값을 돌려준 직후** 문서가 바뀐다(결속값이 그 뒤부터 new)."""

    def __init__(self, swap_at: int, new: Any) -> None:
        super().__init__()
        self.swap_at = swap_at
        self.new = new
        self.swapped = False
        self.loaders: List[Optional[str]] = []

    async def isolated_top_evaluate(self, expression: str, expect_loader: Optional[str] = None) -> Any:
        self.loaders.append(expect_loader)
        out = await super().isolated_top_evaluate(expression)
        if self.evals == self.swap_at:
            self.swapped = True
        return out

    async def top_document_binding(self) -> Any:
        self.binds += 1
        return self.new if self.swapped else B1


@pytest.mark.parametrize("swap_at", [1, 2, 3], ids=["check_screen", "pre_save", "post_save"])
@pytest.mark.parametrize("new", [
    ("F", "L2", 1, PAGE_URL),                       # 같은 URL reload — loaderId 만 바뀜
    ("F", "L2", 2, "https://shop.test/other"),      # 다른 문서로 이동
    ("F", "L1", 2, "https://shop.test/pushed"),     # pushState
], ids=["reload_same_url", "navigate", "push"])
def test_r2_document_swapped_during_any_strict_probe_is_refused(swap_at, new, tmp_path):
    """판정 도중(값을 읽은 뒤·다음 결속값 전) 바뀐 문서 — 판정 무효 = 거부, 디스크 0건."""
    path = tmp_path / sp.FILE_NAME
    doc = _SwapDoc(swap_at, new)
    svc = _svc(doc, SavedPageStore(path))
    out = _run(svc)
    assert not out["success"] and out["error_message"] == sp.E_CHANGED, out
    assert len(svc.store) == 0
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8"))["pages"] == []
    assert doc.evals == swap_at  # 바뀐 문서를 두고 더 판정하지 않는다


def test_r2_swap_during_final_probe_rolls_back_name_update(tmp_path):
    """저장 뒤 마지막 판정 도중 교체 — 이름 갱신도 되돌린다(undo_add)."""
    path = tmp_path / sp.FILE_NAME
    store = SavedPageStore(path)
    entry, _ = store.add("default", PAGE_URL, "원래 이름")
    doc = _SwapDoc(3, ("F", "L2", 1, PAGE_URL))
    svc = _svc(doc, store)
    out = _run(svc, "바뀐 이름")
    assert not out["success"] and out["error_message"] == sp.E_CHANGED
    assert store.get("default", entry["id"])["name"] == "원래 이름"
    assert [p["name"] for p in json.loads(path.read_text(encoding="utf-8"))["pages"]] == ["원래 이름"]


def test_r2_every_strict_probe_is_bound_to_the_start_loader():
    """엄격 판정마다 시작 결속값의 loaderId 를 판독기에 넘긴다(재시도 결속 검사의 기준)."""
    doc = _SwapDoc(99, B1)
    out = _run(_svc(doc))
    assert out["success"], out
    assert doc.loaders == ["L1", "L1", "L1"]
    assert doc.binds == 4 and doc.evals == 3  # 결속값 4회(앞·사이·사이·뒤) — 판정 3회 모두 샌드위치


def test_r2_stable_document_no_over_refusal():
    out = _run(_svc(_Doc(probes=(CLEAN,))))
    assert out["success"] and out["data"]["saved_page"]["url"] == PAGE_URL


def test_r2_unreadable_binding_after_probe_is_unjudged():
    doc = _Doc(bindings=[B1, B1, B1, RuntimeError("detached")])
    out = _run(_svc(doc))
    assert not out["success"] and out["error_message"] == sp.E_UNJUDGED


def test_r2_changed_document_wins_over_captcha_verdict():
    """판정 도중 바뀌었으면 그 판정(캡차·정상 무엇이든)은 무효 — 바뀜으로 거부한다."""
    doc = _SwapDoc(1, ("F", "L2", 1, PAGE_URL))
    doc.probes = [CAPTCHA]
    out = _run(_svc(doc))
    assert not out["success"] and out["error_message"] == sp.E_CHANGED


# ---------------------------------------------------------------------------
# 재시도 결속 — 단위(가짜 CDP)
# ---------------------------------------------------------------------------


class _LoaderCdp:
    """loaderId 를 바꿀 수 있는 가짜 게이트 CDP 세션. 사라진 컨텍스트로 평가하면 'context 없음'."""

    def __init__(self) -> None:
        self.loader = "L1"
        self.live: Optional[int] = None
        self.created: List[str] = []
        self.on_evaluate: Any = None

    def on(self, *_a: Any) -> None:
        pass

    async def send(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F", "loaderId": self.loader}}}
        if method == "Page.createIsolatedWorld":
            self.created.append(self.loader)
            self.live = 100 + len(self.created)
            return {"executionContextId": self.live}
        if method == "Runtime.evaluate":
            if self.on_evaluate is not None:
                hook, self.on_evaluate = self.on_evaluate, None
                hook()
            if params["contextId"] != self.live:
                raise RuntimeError("Protocol error (Runtime.evaluate): Cannot find context with specified id")
            return {"result": {"value": {"doc": self.loader}}}
        raise AssertionError(method)


def _dispatcher(fake: _LoaderCdp) -> Any:
    from actions.dispatcher import ActionDispatcher

    d = ActionDispatcher.__new__(ActionDispatcher)

    async def gate_cdp(_page: Any) -> Any:
        return fake

    d._gate_cdp_for = gate_cdp  # noqa: SLF001
    d._top_page = lambda: object()  # noqa: SLF001
    return d


def _navigate(fake: _LoaderCdp, loader: str = "L2") -> None:
    """새 문서로 이동 — loaderId 가 바뀌고 이전 문서의 격리 world 컨텍스트는 사라진다."""
    fake.loader = loader
    fake.live = None


async def test_r2_strict_retry_on_new_document_is_unjudged():
    """검증 재현: 마지막 평가 직전 이동 → 'context 없음' 재시도가 새 문서를 판정하면 안 된다."""
    fake = _LoaderCdp()
    d = _dispatcher(fake)
    assert await d.isolated_top_evaluate("1", expect_loader="L1") == {"doc": "L1"}
    fake.on_evaluate = lambda: _navigate(fake)
    with pytest.raises(RuntimeError):
        await d.isolated_top_evaluate("1", expect_loader="L1")
    assert fake.created == ["L1"]  # 새 문서(L2)에 격리 world 를 만들어 판정하지 않았다


async def test_r2_strict_evaluation_that_landed_on_new_document_is_unjudged():
    """평가는 성공했지만 그 사이 문서가 바뀌었다(평가 뒤 loaderId 다름) — 결과를 쓰지 않는다."""
    fake = _LoaderCdp()
    d = _dispatcher(fake)
    await d.isolated_top_evaluate("1", expect_loader="L1")

    def nav_after_reply() -> None:
        fake.loader = "L2"  # 컨텍스트는 아직 살아 응답하지만 결속 문서는 바뀜

    fake.on_evaluate = nav_after_reply
    with pytest.raises(RuntimeError):
        await d.isolated_top_evaluate("1", expect_loader="L1")


async def test_r2_strict_mode_refuses_when_document_already_changed():
    fake = _LoaderCdp()
    fake.loader = "L9"
    d = _dispatcher(fake)
    with pytest.raises(RuntimeError):
        await d.isolated_top_evaluate("1", expect_loader="L1")
    assert fake.created == []


async def test_r2_strict_retry_on_same_document_still_works():
    """같은 문서에서 컨텍스트만 사라짐(document.open 등) — 엄격 모드도 한 번 다시 만들어 판정한다."""
    fake = _LoaderCdp()
    d = _dispatcher(fake)
    await d.isolated_top_evaluate("1", expect_loader="L1")
    fake.live = 999
    assert await d.isolated_top_evaluate("1", expect_loader="L1") == {"doc": "L1"}
    assert fake.created == ["L1", "L1"]


async def test_r2_default_mode_retry_unchanged_for_ws67():
    """옵션 없으면(WS-67 포커스·좌표 판정) 재시도 동작 그대로 — 새 문서에서 다시 만들어 판정한다."""
    fake = _LoaderCdp()
    d = _dispatcher(fake)
    assert await d.isolated_top_evaluate("1") == {"doc": "L1"}
    fake.on_evaluate = lambda: _navigate(fake)
    assert await d.isolated_top_evaluate("1") == {"doc": "L2"}
    assert fake.created == ["L1", "L2"]


# ---------------------------------------------------------------------------
# E2E(실제 Chromium + 로컬 http) — 검증 재현 편입
# ---------------------------------------------------------------------------

from test_ws68_remember_strict import _PAGES, _Site  # noqa: E402

_PAGES.setdefault("/r2-clean", (200, "<title>Orders</title><h1>Order list</h1>"))
_PAGES.setdefault("/r2-other", (200, "<title>Other</title><h1>Other content</h1>"))
_R2_CAPTCHA = "<h1>verify you are human</h1><div class='cf-turnstile' style='width:300px;height:65px'></div>"


async def _change(page: Any, site: Any, how: str) -> None:
    if how == "reload":
        await page.reload()
    else:
        await page.goto(site.url("/r2-other"))
        if how == "navigate_captcha":
            await page.evaluate("(html) => { document.body.innerHTML = html; }", _R2_CAPTCHA)


async def _race(how: str, *, at_evaluate: Optional[int] = None, after_binding: Optional[int] = None,
                tmp_path: Any) -> tuple:
    """실제 CDP 응답은 그대로 — 바꾸는 것은 시점뿐(검증 probe_final_race·probe_new 와 같은 주입)."""
    from interface.mcp_server import BrowserMCPServer

    with _Site() as site:
        async with BrowserMCPServer(headless=True) as srv:
            d = srv._dispatcher
            page = d.ctx.page
            await page.goto(site.url("/r2-clean"))
            before = await d.top_document_binding()
            path = tmp_path / sp.FILE_NAME
            srv._saved_pages._store = SavedPageStore(path)
            cdp = await d._gate_cdp_for(page)  # noqa: SLF001
            native_send, native_bind = cdp.send, d.top_document_binding
            counts = {"eval": 0, "bind": 0}

            async def send(method: str, params: Any = None) -> Any:
                if method == "Runtime.evaluate" and params and params.get("contextId") is not None:
                    counts["eval"] += 1
                    if counts["eval"] == at_evaluate:
                        await _change(page, site, how)
                return await native_send(method, params)

            async def bind() -> Any:
                got = await native_bind()
                counts["bind"] += 1
                if counts["bind"] == after_binding:
                    await _change(page, site, how)
                return got

            cdp.send = send
            d.top_document_binding = bind
            try:
                out = await srv.call_server_tool("browser_recipe", {"op": "remember_page", "name": "경합검증"})
            finally:
                cdp.send, d.top_document_binding = native_send, native_bind
            after = await d.top_document_binding()
            disk = json.loads(path.read_text(encoding="utf-8"))["pages"] if path.exists() else []
            return out, disk, before, after, counts


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["navigate", "reload", "navigate_captcha"])
@pytest.mark.parametrize("at_evaluate", [1, 2, 3], ids=["eval1", "eval2", "eval3_final"])
async def test_r2_e2e_probe_final_race_document_changed_inside_strict_evaluation(how, at_evaluate, tmp_path):
    """probe_final_race: 엄격 판정의 Runtime.evaluate 직전에 실제 문서 교체 — 저장 0건(3번째 = 마지막 판정)."""
    out, disk, before, after, counts = await _race(how, at_evaluate=at_evaluate, tmp_path=tmp_path)
    assert before[1] != after[1]  # 표본 확인: 실제로 문서(loaderId)가 바뀌었다
    assert counts["eval"] >= at_evaluate
    assert not out["success"] and out["error_message"] in (sp.E_CHANGED, sp.E_UNJUDGED), out
    assert disk == []


@pytest.mark.asyncio
@pytest.mark.parametrize("after_binding", [1, 2, 3])  # 4번째 = 마지막 확인(그 뒤 변경은 저장 뒤 일)
@pytest.mark.parametrize("how", ["reload", "navigate"])
async def test_r2_e2e_final_binding_reload(how, after_binding, tmp_path):
    """new-probes final_binding_reload: 결속값을 읽은 직후 reload — 그 뒤 판정·저장은 무효, 디스크 0건."""
    out, disk, before, after, counts = await _race(how, after_binding=after_binding, tmp_path=tmp_path)
    assert before[1] != after[1]
    assert counts["bind"] >= after_binding
    assert not out["success"] and out["error_message"] in (sp.E_CHANGED, sp.E_UNJUDGED), out
    assert disk == []


@pytest.mark.asyncio
async def test_r2_e2e_stable_document_saves(tmp_path):
    """과잉 거부 0: 아무것도 바뀌지 않으면 저장 1건(같은 주입 틀, 주입 시점 없음)."""
    out, disk, before, after, counts = await _race("reload", tmp_path=tmp_path)
    assert out["success"], out
    assert before == after and len(disk) == 1 and disk[0]["url"].endswith("/r2-clean")
    assert counts == {"eval": 3, "bind": 4}

