"""WS-73 R1 — 묶음 수명 전체 이벤트 구독(문서 커밋·새 탭·다이얼로그)과 detail 고정 어휘.

검증 FAIL 3건(.hermes/verify/ws73/findings.md)의 저장소 회귀 시험:

* B1: 해석 관찰과 겹쳐 주 프레임이 새 문서로 바뀌면(지문이 같은 같은 이름 단추) 새 문서를 누르지 않고 navigated 로 멈춘다.
* B2: 해석 관찰 중 새 탭이 열리면 new_tab 으로 멈추고 후속 단계 실행 0.
* (같은 방식) 해석 관찰 뒤 늦게 뜬 다이얼로그 → dialog 로 멈춤.
* B3: native 오류 문구(DOM id 에 등록 비밀)가 data.batch 에 복사되지 않고, 응답 어디에도 비밀 접두가 남지 않는다.

실제 setTimeout 타이밍은 부하에 민감하므로, 같은 전이를 **묶음의 내부 관찰 호출 경계에 결정적으로 주입**한다(페이지의
실제 location 이동·window.open·alert — 제품 메서드는 바꾸지 않고 call_tool 앞뒤에서 페이지를 움직일 뿐).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402

from interface import server_batch as sb  # noqa: E402

BATCH = "browser_batch"

#: 같은 HTML 이 ?phase=new 로 다시 열리면 Review item 의 효과가 wrong — 두 문서의 대상 role/name/css 는 같다.
#: 원장은 sessionStorage(같은 탭·같은 출처라 문서가 바뀌어도 남는다).
FLOW_HTML = """<!doctype html><html><head><meta charset=utf-8><title>flow</title></head><body>
<button id=start>Open section</button><main><button id=review>Review item</button></main>
<aside><button id=tail>Final marker</button></aside><output id=o></output><script>
const NEW = location.search.includes('phase=new');
function rec(v) { const l = JSON.parse(sessionStorage.getItem('ledger') || '[]'); l.push(v);
  sessionStorage.setItem('ledger', JSON.stringify(l)); document.getElementById('o').textContent = l.join(','); }
document.getElementById('start').onclick = () => rec('start');
document.getElementById('review').onclick = () => rec(NEW ? 'wrong' : 'good');
document.getElementById('tail').onclick = () => rec('tail');
</script></body></html>"""

LEDGER_JS = "() => JSON.parse(sessionStorage.getItem('ledger') || '[]')"
STEPS = [{"action": "click", "target": {"role": "button", "name": n}} for n in ("Open section", "Review item",
                                                                              "Final marker")]

CANARY = "KpVqRmZsTyNfAbCd9876XwUv"


def _native_html(pad: int) -> str:
    ident = "prefix" + "p" * pad + CANARY
    return ("<!doctype html><meta charset=utf-8><title>Native error</title>"
            f"<select id=\"{ident}\" aria-label=\"Choice\"><option value=\"A\">Alpha</option></select>")


@pytest.fixture
def site():
    yield from _serve({"/": FLOW_HTML, "/aux": "<p>aux</p>", "/native": _native_html(58)})


def _server(tmp_path: Path, **kw: Any) -> Any:
    from interface.mcp_server import BrowserMCPServer

    kw.setdefault("recipes", False)
    return BrowserMCPServer(handoff_root=tmp_path / "s", **kw)


async def _open(s: Any, url: str) -> None:
    r = await s.call_tool("browser_navigate", {"url": url})
    assert r.success, r.error_message
    await s._page.evaluate("() => sessionStorage.clear()")


# ---------------------------------------------------------------------------- 전이 주입


async def _nav(s: Any) -> None:
    page = s._page
    async with page.expect_navigation():
        await page.evaluate("() => setTimeout(() => { location.href = location.pathname + '?phase=new'; }, 0)")
    assert "phase=new" in page.url


async def _popup(s: Any) -> None:
    page = s._page
    async with page.context.expect_page():
        await page.evaluate("() => setTimeout(() => window.open('/aux', '_blank'), 0)")


async def _dialog(s: Any) -> None:
    page = s._page
    async with page.expect_event("dialog"):
        await page.evaluate("() => setTimeout(() => alert('late'), 0)")


INJECT = {"nav": _nav, "popup": _popup, "dialog": _dialog}


def _hook(s: Any, kind: str, at: int, when: str) -> List[int]:
    """묶음의 at 번째 내부 해석 관찰(0=기준 스냅샷) 직전(pre)·직후(post)에 전이를 일으킨다."""
    real = s.call_tool
    seen: List[int] = []

    async def wrapped(name: str, args: Dict[str, Any]) -> Any:
        internal = sb.batch_internal() is not None and args.get("force_full_tree") is True
        if not internal:
            return await real(name, args)
        n = len(seen)
        seen.append(n)
        if n == at and when == "pre":
            await INJECT[kind](s)
        res = await real(name, args)
        if n == at and when == "post":
            await INJECT[kind](s)
        return res

    s.call_tool = wrapped
    return seen


VARIANTS = [(0, "post"), (1, "pre"), (1, "post")]


@requires_chromium
@pytest.mark.parametrize("at,when", VARIANTS)
async def test_b1_document_commit_during_internal_observe_stops(site, tmp_path, at, when):
    """B1: 해석 관찰과 겹친 새 문서 → navigated, 새 문서 클릭 0(wrong 0)·후속 0."""
    async with _server(tmp_path) as s:
        await _open(s, site + "/")
        seen = _hook(s, "nav", at, when)
        out = await s.call_server_tool(BATCH, {"steps": STEPS})
        ledger = await s._page.evaluate(LEDGER_JS)
    assert at in seen, "주입 지점에 닿지 못하면 이 시험은 아무것도 재지 않는다"
    b = out["data"]["batch"]
    assert out["success"] is False and b["reason"] == "navigated", b
    assert "wrong" not in ledger and "tail" not in ledger, ledger
    assert b["ran"] == at, b  # 주입이 at 번째 관찰에 겹쳤으니 그 뒤 단계는 실행 0
    assert ledger == ["start"][:at], ledger


@requires_chromium
@pytest.mark.parametrize("at,when", VARIANTS)
async def test_b2_new_tab_during_internal_observe_stops(site, tmp_path, at, when):
    """B2: 해석 관찰 중 열린 새 탭 → new_tab, 후속 단계 실행 0."""
    async with _server(tmp_path) as s:
        await _open(s, site + "/")
        seen = _hook(s, "popup", at, when)
        out = await s.call_server_tool(BATCH, {"steps": STEPS})
        ledger = await s._page.evaluate(LEDGER_JS)
        pages = len(s._page.context.pages)
    assert at in seen and pages == 2
    b = out["data"]["batch"]
    assert out["success"] is False and b["reason"] == "new_tab", b
    assert b["ran"] == at and "tail" not in ledger, (b, ledger)
    assert ledger == ["start", "good"][:at], ledger


@requires_chromium
@pytest.mark.parametrize("at,when", [(0, "post"), (1, "post")])
async def test_dialog_after_internal_observe_stops(site, tmp_path, at, when):
    """다이얼로그도 묶음 수명 동안 계수 — 해석 관찰이 끝난 뒤(다음 dispatch 가 기록을 비우기 전) 뜬 것도 멈춤."""
    async with _server(tmp_path) as s:
        await _open(s, site + "/")
        seen = _hook(s, "dialog", at, when)
        out = await s.call_server_tool(BATCH, {"steps": STEPS})
        ledger = await s._page.evaluate(LEDGER_JS)
    assert at in seen
    b = out["data"]["batch"]
    assert out["success"] is False and b["reason"] == "dialog", b
    assert b["ran"] == at and ledger == ["start"][:at], (b, ledger)


@requires_chromium
async def test_step_own_change_after_step_stops(site, tmp_path):
    """한 단계가 정상적으로 일으킨 변화도 멈춤: 1단계 실행 '직후' 새 문서 → navigated, 남은 단계 0."""
    async with _server(tmp_path) as s:
        await _open(s, site + "/")
        real = s.call_tool

        async def wrapped(name: str, args: Dict[str, Any]) -> Any:
            res = await real(name, args)
            if name == "browser_click":
                await _nav(s)
            return res

        s.call_tool = wrapped
        out = await s.call_server_tool(BATCH, {"steps": STEPS})
        ledger = await s._page.evaluate(LEDGER_JS)
    b = out["data"]["batch"]
    assert (b["reason"], b["ran"], b["stopped_at"]) == ("navigated", 1, 0), b
    assert ledger == ["start"]
    assert out["data"]["step_result"]["success"] is True  # 이미 실행된 단계 효과는 보고한다


@requires_chromium
async def test_residual_window_same_as_single(site, tmp_path):
    """남는 창(실행 직전 확인 ~ 실제 클릭 디스패치) 비교: 그 틈에 새 문서로 바뀌면 묶음도 단건과 같은 결과,
    그리고 묶음은 그 단계 뒤 멈춘다(후속 tail 0)."""
    async def run(batch: bool) -> Any:
        async with _server(tmp_path) as s:
            await _open(s, site + "/")
            disp = s._dispatcher
            real = disp.dispatch
            clicks: List[int] = []

            async def dispatch(action, params):
                if getattr(action, "value", "") == "click":
                    clicks.append(1)
                    if len(clicks) == 2:
                        await _nav(s)
                return await real(action, params)

            disp.dispatch = dispatch
            if batch:
                out = await s.call_server_tool(BATCH, {"steps": STEPS})
                single = None
            else:
                obs = await s.call_tool("browser_observe_page", {})
                ids = {e["name"]: e["element_id"] for e in obs.data["observation"]["elements"]}
                out = None
                single = []
                for name in ("Open section", "Review item"):
                    r = await s.call_tool("browser_click", {"element_id": ids[name], "epoch": obs.snapshot_epoch})
                    single.append(r.success)
            return out, single, await s._page.evaluate(LEDGER_JS)

    out, _, batch_ledger = await run(True)
    _, single_ok, single_ledger = await run(False)
    print("\nWS-73 R1 residual window:", {"batch": batch_ledger, "single": single_ledger,
                                         "batch_reason": out["data"]["batch"]["reason"], "single_ok": single_ok})
    assert batch_ledger == single_ledger, (batch_ledger, single_ledger)
    assert "tail" not in batch_ledger
    assert out["success"] is False and out["data"]["batch"]["ran"] == 2


@requires_chromium
@pytest.mark.parametrize("navigate", [True, False])
async def test_internal_exception_during_document_change_is_navigated_stop(site, tmp_path, navigate):
    """문서 교체로 해석 관찰이 예외(context destroyed)로 끝나면 navigated 멈춤 — 변화가 없으면 원래 예외 그대로."""
    async with _server(tmp_path) as s:
        await _open(s, site + "/")
        real = s.call_tool
        n: List[int] = []

        async def wrapped(name: str, args: Dict[str, Any]) -> Any:
            if sb.batch_internal() is not None and args.get("force_full_tree") is True:
                n.append(1)
                if len(n) == 2:
                    if navigate:
                        await _nav(s)
                    raise RuntimeError("Execution context was destroyed")
            return await real(name, args)

        s.call_tool = wrapped
        if navigate:
            out = await s.call_server_tool(BATCH, {"steps": STEPS})
            ledger = await s._page.evaluate(LEDGER_JS)
            b = out["data"]["batch"]
            assert (b["reason"], b["ran"]) == ("navigated", 1), b
            assert ledger == ["start"]
        else:
            with pytest.raises(RuntimeError):
                await s.call_server_tool(BATCH, {"steps": STEPS})


# ---------------------------------------------------------------------------- 연결 단정·fail-closed


def _listener_counts(s: Any) -> Dict[str, int]:
    page = s._page
    emitter = page.main_frame._impl_obj._event_emitter
    return {"navigated": len(emitter.listeners("navigated")),
            "page": len(page.context._impl_obj.listeners("page")),
            "dialog": len(page._impl_obj.listeners("dialog"))}


@requires_chromium
async def test_watch_is_really_attached_for_whole_batch(site, tmp_path):
    """구독이 묶음 시작(기준 스냅샷 전)부터 끝까지 실제로 걸려 있고, 끝나면 떼어진다."""
    async with _server(tmp_path) as s:
        await _open(s, site + "/")
        before = _listener_counts(s)
        real = s.call_tool
        during: List[Dict[str, int]] = []

        async def wrapped(name: str, args: Dict[str, Any]) -> Any:
            during.append(_listener_counts(s))
            return await real(name, args)

        s.call_tool = wrapped
        out = await s.call_server_tool(BATCH, {"steps": STEPS})
        after = _listener_counts(s)
    assert out["success"] is True, out["data"]["batch"]
    assert during, "call_tool 이 불리지 않았다"
    for snap in during:  # 첫 호출(기준 스냅샷 관찰) 포함 모든 호출 동안
        assert snap == {k: v + 1 for k, v in before.items()}, (before, snap)
    assert after == before


@requires_chromium
@pytest.mark.parametrize("broken", ["commits", "context", "dialog"])
async def test_watch_unavailable_refuses_whole_batch(site, tmp_path, monkeypatch, broken):
    """내부 API 가 없거나 모양이 다르면 묶음 자체를 거부(아무것도 실행하지 않음)."""
    async with _server(tmp_path) as s:
        await _open(s, site + "/")
        disp = s._dispatcher
        if broken == "commits":
            monkeypatch.setattr(disp, "_watch_document_commits", lambda page: {"count": None, "unwatch": None})
        elif broken == "context":
            monkeypatch.setattr(sb, "_context_of", lambda page: None)
        else:
            monkeypatch.setattr(disp, "_dialog_pages", [])
            monkeypatch.setattr(disp, "_install_dialog_listener", lambda: None)
        calls: List[str] = []
        real = s.call_tool

        async def wrapped(name: str, args: Dict[str, Any]) -> Any:
            calls.append(name)
            return await real(name, args)

        s.call_tool = wrapped
        out = await s.call_server_tool(BATCH, {"steps": STEPS})
        ledger = await s._page.evaluate(LEDGER_JS)
    assert out["success"] is False and out["error_code"] == "E_FEATURE_NOT_IMPLEMENTED", out
    assert calls == [] and ledger == []


def test_watch_helper_fail_closed_on_fakes():
    """가짜 객체: 내부 이벤트 출구가 없으면 None(=거부)."""
    class Disp:
        _dialog_pages: List[Any] = []

        def _install_dialog_listener(self) -> None:
            pass

        def _watch_document_commits(self, page):
            return {"count": None, "unwatch": None}

        def _unwatch_document_commits(self, state):
            pass

    class Page:
        main_frame = object()
        context = object()

    assert sb.start_watch(Disp(), Page()) is None
    assert sb.start_watch(object(), Page()) is None


# ---------------------------------------------------------------------------- 과잉 거부 대조군


@requires_chromium
async def test_same_document_url_changes_do_not_stop(site, tmp_path):
    """pushState·hash 는 newDocument 가 아니다 — 해석 관찰에 겹쳐도 멈춤 0, 끝까지 통과."""
    async with _server(tmp_path) as s:
        await _open(s, site + "/")
        real = s.call_tool
        n: List[int] = []

        async def wrapped(name: str, args: Dict[str, Any]) -> Any:
            if sb.batch_internal() is not None and args.get("force_full_tree") is True:
                n.append(1)
                await s._page.evaluate(f"() => {{ history.pushState({{}}, '', '?p={len(n)}'); location.hash = 'h{len(n)}'; }}")
            return await real(name, args)

        s.call_tool = wrapped
        out = await s.call_server_tool(BATCH, {"steps": STEPS})
        ledger = await s._page.evaluate(LEDGER_JS)
    assert len(n) == 3
    assert out["success"] is True, out["data"]["batch"]
    assert ledger == ["start", "good", "tail"]


# ---------------------------------------------------------------------------- B3 detail 고정 어휘


def _fragments(text: str, width: int = 16) -> List[str]:
    return [text[k:k + width] for k in range(0, max(1, len(text) - width + 1))]


@requires_chromium
async def test_b3_native_error_not_copied_and_no_secret_prefix(site, tmp_path):
    """실제 Playwright select_option timeout(오류 문구에 DOM id = 등록 비밀 포함) — data.batch 에 원 오류 조각 0,
    응답 어디에도 등록 비밀 접두(8자 이상) 0, detail 은 고정 어휘."""
    from security import secret_registry as sr

    async with _server(tmp_path) as s:
        await _open(s, site + "/native")
        sr.clear()
        sr.register_secret_values([CANARY])
        raw: List[str] = []
        real = s.call_tool

        async def wrapped(name: str, args: Dict[str, Any]) -> Any:
            res = await real(name, args)
            if name == "browser_select_option":
                raw.append(res.error_message or "")
            return res

        s.call_tool = wrapped
        try:
            out = await s.call_server_tool(BATCH, {"steps": [
                {"action": "select_option", "target": {"role": "combobox", "name": "Choice"},
                 "args": {"value": "MISSING"}}]})
        finally:
            sr.clear()
    b = out["data"]["batch"]
    assert b["reason"] == "action_failed", b
    assert raw and len(raw[0]) > 40, "원 오류가 없으면 이 시험은 복사 여부를 재지 못한다"
    assert b["detail"] in sb.fixed_details(), b["detail"]
    batch_text = json.dumps(b, ensure_ascii=False)
    leaked = [f for f in _fragments(raw[0]) if f in batch_text]
    assert leaked == [], leaked[:3]
    whole = json.dumps(out, ensure_ascii=False)
    assert [CANARY[:k] for k in range(8, len(CANARY) + 1) if CANARY[:k] in whole] == []
    assert out["data"]["step_result"]["success"] is False  # 원 문구가 필요하면 정제된 step_result 봉투만


@requires_chromium
async def test_b3_every_stop_detail_is_fixed_vocabulary(site, tmp_path):
    """합성 오류 문구(긴 원문 + 비밀이 200자 경계에 걸침)도 batch 어디에도 안 실림 — 응답 관찰 실패 문구 포함."""
    from contracts import ActionResult, ActionType, ErrorCode
    from security import secret_registry as sr

    marker = "x" * 190 + CANARY
    async with _server(tmp_path) as s:
        await _open(s, site + "/")
        sr.clear()
        sr.register_secret_values([CANARY])
        real = s.call_tool

        async def wrapped(name: str, args: Dict[str, Any]) -> Any:
            if name == "browser_click":
                return ActionResult(success=False, action=ActionType.CLICK, error_code=ErrorCode.ELEMENT_NOT_FOUND,
                                    error_message=marker, snapshot_epoch=0,
                                    current_url="", tab_id="t", retry_safe=False)
            if name == "browser_observe_page" and sb.batch_internal() is not None and not args:
                return ActionResult(success=False, action=ActionType.OBSERVE_PAGE,
                                    error_code=ErrorCode.ELEMENT_NOT_FOUND, error_message=marker, snapshot_epoch=0,
                                    current_url="", tab_id="t", retry_safe=False)
            return await real(name, args)

        s.call_tool = wrapped
        try:
            out = await s.call_server_tool(BATCH, {"steps": STEPS})
        finally:
            sr.clear()
    data = out["data"]
    assert data["batch"]["reason"] == "action_failed" and data["batch"]["detail"] in sb.fixed_details()
    assert ErrorCode.ELEMENT_NOT_FOUND.value in data["batch"]["detail"]
    for key in ("batch", "observation_error"):
        assert "xxxxxxxxxxxxxxxx" not in json.dumps(data.get(key), ensure_ascii=False), key
    whole = json.dumps(out, ensure_ascii=False)
    assert [CANARY[:k] for k in range(8, len(CANARY) + 1) if CANARY[:k] in whole] == []
