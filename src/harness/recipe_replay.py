"""레시피 재생 하네스 (WS-38, AGENTS §5 규칙 1~5).

    python -m harness.recipe_replay

제품 경로(MCP 서버 `BrowserMCPServer.call_tool` + 서버 도구 `browser_recipe`)로 Mock 사이트에서
"기록 → save → 페이지 변형 → run" 을 시나리오 8종으로 잰다. 외부 접속 없음(로컬 HTTP).

양성(재생 성공 + 맞는 대상을 눌렀는가)
  ① content_swap   기사 교체 → 같은 자리(1번째)의 새 기사
  ② reorder        목록 순서 변경 → 순번(3번째)대로(이름 아님)
  ⑧ params         입력 params 치환 → 새 검색어로 검색하고 첫 결과
음성(정해진 사유로 멈추고 **아무것도 누르지 않았는가**)
  ③ ab_unrecorded       A/B 변형(기록 안 된 화면) → page_changed
  ④ partial_load        덜 로드(요소 50% 미만) → not_ready
  ⑤ ambiguous_lists     같은 틀 목록 2개 → target_ambiguous
  ⑥ ad_forgery          같은 자리·같은 모양 광고(href 패턴 다름) → target_not_found
  ⑦ payment_approval    결제 단계(HITL 고위험) → approval_required (승인 증표 저장 안 함)

판정
* 오클릭(음성에서 무엇이든 누름, 양성에서 다른 대상을 누름)이 하나라도 있으면 **exit 2**.
* 커버리지(scenarios_covered) 8/8 미만이면 exit 2. 시나리오가 의도한 사유가 아닌 다른 사유로 멈추면
  실패(규칙 2) — 값에 반영돼 exit 1.
* 오클릭 판정은 하네스 쪽 독립 증거(Mock 서버 요청 기록 + 재생 응답의 실행 단계)로 한다.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from harness.result import MetricResult, emit, emit_error

METRIC = "recipe_replay_success"
#: 시나리오 → (종류, 기대 사유). 양성의 기대 사유는 None(성공).
SCENARIOS: Dict[str, Tuple[str, Optional[str]]] = {
    "content_swap": ("positive", None),
    "reorder": ("positive", None),
    "params": ("positive", None),
    "ab_unrecorded": ("negative", "page_changed"),
    "partial_load": ("negative", "not_ready"),
    "ambiguous_lists": ("negative", "target_ambiguous"),
    "ad_forgery": ("negative", "target_not_found"),
    "payment_approval": ("negative", "approval_required"),
}


async def _call(srv: Any, action: Any, **args: Any) -> Any:
    from interface.mcp_server import tool_name

    return await srv.call_tool(tool_name(action), args)


async def _observe(srv: Any) -> Any:
    from contracts import ActionType

    res = await _call(srv, ActionType.OBSERVE_PAGE)
    if not res.success:
        raise RuntimeError(f"관찰 실패: {res.error_message}")
    return res


def _eid(obs: Any, name: str, role: str = "") -> str:
    for el in obs.data["observation"]["elements"]:
        if el["name"] == name and (not role or el["role"] == role):
            return el["element_id"]
    raise RuntimeError(f"요소 없음: {name}")


async def _click(srv: Any, obs: Any, name: str, role: str = "") -> Any:
    from contracts import ActionType

    res = await _call(srv, ActionType.CLICK, element_id=_eid(obs, name, role), epoch=obs.snapshot_epoch)
    if not res.success:
        raise RuntimeError(f"기록 단계 실패: click {name}: {res.error_message}")
    return res


async def _save(srv: Any, **args: Any) -> str:
    out = await srv.call_server_tool("browser_recipe", dict(op="save", **args))
    if not out.get("success"):
        raise RuntimeError(f"save 실패: {out.get('error_message')}")
    return out["data"]["recipe"]["id"]


async def _record_article(srv: Any, site: Any, name: str) -> str:
    from contracts import ActionType

    await _call(srv, ActionType.NAVIGATE, url=site.url("/list"))
    obs = await _observe(srv)
    await _click(srv, obs, name)
    return await _save(srv, name=f"목록에서 {name} 자리 열기", last_n=1)


async def _run(srv: Any, site: Any, rid: str, path: str = "/list", **params: Any) -> Dict[str, Any]:
    from contracts import ActionType

    await _call(srv, ActionType.NAVIGATE, url=site.url(path))
    site.clear_log()
    return await srv.call_server_tool("browser_recipe", {"op": "run", "id": rid, "params": params})


def _dispatched(out: Dict[str, Any]) -> int:
    info = (out.get("data") or {}).get("recipe") or {}
    return sum(1 for s in info.get("steps") or [] if s.get("action") in ("click", "type_text", "press_key",
                                                                        "select_option", "check_box"))


async def _scenario(name: str, site: Any) -> Dict[str, Any]:
    """시나리오 하나 → {outcome, reason, misclick, detail}."""
    from contracts import ActionType
    from harness import recipe_mock as m
    from interface import BrowserMCPServer

    expected_open: Optional[List[str]] = None
    site.feed(m.ROWS)
    if name == "payment_approval":
        async with BrowserMCPServer(headless=True, pre_approved_actions=("click:결제하기",),
                                    profile="replay-pay") as srv:
            await _call(srv, ActionType.NAVIGATE, url=site.url("/cart"))
            obs = await _observe(srv)
            await _click(srv, obs, "결제하기")
            rid = await _save(srv, name="결제", last_n=1)
        async with BrowserMCPServer(headless=True, profile="replay-pay") as srv:
            out = await _run(srv, site, rid, "/cart")
        bad = bool(site.opened("/paid")) or (_dispatched(out) > 0 and bool(out.get("success")))
        return _judge(name, out, misclick=bad)

    async with BrowserMCPServer(headless=True) as srv:
        if name == "params":
            await _call(srv, ActionType.NAVIGATE, url=site.url("/list"))
            obs = await _observe(srv)
            res = await _call(srv, ActionType.TYPE_TEXT, element_id=_eid(obs, "검색어"), text="노트북",
                              epoch=obs.snapshot_epoch)
            if not res.success:
                raise RuntimeError(f"기록 단계 실패: type_text: {res.error_message}")
            await _click(srv, obs, "검색", "button")
            await _click(srv, await _observe(srv), "노트북 결과 1")
            rid = await _save(srv, name="검색 후 첫 결과", last_n=3, params={"query": "노트북"})
            out = await _run(srv, site, rid, query="무선 이어폰")
            ok_search = site.opened("/search") == ["/search?q=%EB%AC%B4%EC%84%A0%20%EC%9D%B4%EC%96%B4%ED%8F%B0"]
            opened = site.opened("/item")
            mis = bool(opened) and opened != ["/item?id=301"] or (bool(site.opened("/search")) and not ok_search)
            return _judge(name, out, misclick=mis, positive_check=ok_search and opened == ["/item?id=301"])
        if name == "content_swap":
            rid = await _record_article(srv, site, m.ROWS[0][1])
            site.feed(m.NEW_ROWS)
            expected_open = ["/item?id=201"]
        elif name == "reorder":
            rid = await _record_article(srv, site, m.ROWS[2][1])  # 3번째 자리(Gamma)
            site.feed([m.ROWS[4], m.ROWS[3], m.ROWS[0], m.ROWS[1], m.ROWS[2]])
            expected_open = ["/item?id=101"]  # 이름(Gamma=103)이 아니라 3번째 자리
        elif name == "ab_unrecorded":
            rid = await _record_article(srv, site, m.ROWS[0][1])
            site.feed(m.ROWS, header=m.HEADER_B)
        elif name == "partial_load":
            rid = await _record_article(srv, site, m.ROWS[0][1])
            site.feed(m.ROWS[:1], header="<header><nav><span>…</span></nav></header>")
        elif name == "ambiguous_lists":
            site.feed(m.ROWS, lists=m.two_cols(m.ROWS, m.NEW_ROWS, twin=False))
            rid = await _record_article(srv, site, m.ROWS[0][1])
            site.feed(m.ROWS, lists=m.two_cols(m.ROWS, m.NEW_ROWS, twin=True))
        elif name == "ad_forgery":
            rid = await _record_article(srv, site, m.ROWS[0][1])
            site.feed(m.ROWS, ad_first=True)
        else:
            raise RuntimeError(f"알 수 없는 시나리오: {name}")
        out = await _run(srv, site, rid)
        opened = site.opened("/item")
        if expected_open is not None:
            return _judge(name, out, misclick=bool(opened) and opened != expected_open,
                          positive_check=opened == expected_open)
        return _judge(name, out, misclick=bool(opened) or _dispatched(out) > 0)


def _judge(name: str, out: Dict[str, Any], *, misclick: bool,
           positive_check: bool = True) -> Dict[str, Any]:
    kind, want = SCENARIOS[name]
    info = (out.get("data") or {}).get("recipe") or {}
    reason = None if out.get("success") else info.get("reason")
    if kind == "positive":
        ok = bool(out.get("success")) and positive_check and not misclick
    else:
        ok = (not out.get("success")) and reason == want and not misclick
    return {"ok": ok, "kind": kind, "expected": want, "reason": reason, "misclick": bool(misclick),
            "detail": info.get("detail") or out.get("error_message")}


async def _run_all(only: Optional[List[str]]) -> Dict[str, Dict[str, Any]]:
    from harness.recipe_mock import RecipeSite

    results: Dict[str, Dict[str, Any]] = {}
    with RecipeSite() as site:
        for name in SCENARIOS:
            if only and name not in only:
                continue
            try:
                results[name] = await _scenario(name, site)
            except Exception as exc:  # noqa: BLE001 - 시나리오 실패는 기록하고 계속
                results[name] = {"ok": False, "kind": SCENARIOS[name][0], "expected": SCENARIOS[name][1],
                                 "reason": None, "misclick": False,
                                 "detail": f"{type(exc).__name__}: {exc}", "error": True}
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="레시피 재생 하네스 (Mock, 제품 경로)")
    parser.add_argument("--only", nargs="*", default=None, help="일부 시나리오만(커버리지 미달 → exit 2)")
    args = parser.parse_args(argv)

    from browser.serve_profile import PROFILE_ROOT_ENV
    from interface.handoff import STATE_ROOT_ENV

    with tempfile.TemporaryDirectory(prefix="ab-recipe-") as tmp:
        # 사용자 홈(~/.agent-browser)을 건드리지 않는다.
        os.environ[PROFILE_ROOT_ENV] = os.path.join(tmp, "profiles")
        os.environ[STATE_ROOT_ENV] = os.path.join(tmp, "servers")
        try:
            results = asyncio.run(_run_all(args.only))
        except Exception as exc:  # noqa: BLE001
            return int(emit_error(METRIC, f"실행 실패: {type(exc).__name__}: {exc}"))

    covered = [n for n in SCENARIOS if n in results and not results[n].get("error")]
    missing = [n for n in SCENARIOS if n not in covered]
    misclicks = [n for n, r in results.items() if r.get("misclick")]
    summary = {n: (("ok" if r["ok"] else "FAIL") + f":{r.get('reason')}") for n, r in results.items()}
    if misclicks:
        return int(emit_error(METRIC, f"오클릭 {len(misclicks)}건 {misclicks} — {summary}"))
    if missing:
        return int(emit_error(METRIC, f"시나리오 {len(missing)}종 미측정: {missing} — {summary}"))
    positives = [r for r in results.values() if r["kind"] == "positive"]
    negatives = [r for r in results.values() if r["kind"] == "negative"]
    passed = sum(1 for r in results.values() if r["ok"])
    result = MetricResult(
        metric=METRIC,
        value=passed / len(SCENARIOS),
        threshold=1.0,
        samples=len(results),
        extra={
            "scenarios_covered": len(covered),
            "scenarios_required": len(SCENARIOS),
            "misclicks": 0,
            "positive_ok": sum(1 for r in positives if r["ok"]),
            "positive_total": len(positives),
            "negative_stopped_as_expected": sum(1 for r in negatives if r["ok"]),
            "negative_total": len(negatives),
            "per_scenario": summary,
            "failures": {n: r.get("detail") for n, r in results.items() if not r["ok"]} or None,
            "measured_on": "product_path: MCP call_tool + browser_recipe save/run (Mock, 로컬)",
        },
    )
    return int(emit(result))


if __name__ == "__main__":
    sys.exit(main())
