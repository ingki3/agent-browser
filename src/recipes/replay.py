"""레시피 재생 (WS-38 단계 4, 설계 §3).

단계마다:
1. **PageKey 확인** — 출처 같음 → URL 패턴 맞음 → 준비 기준(요소 수 ≥ 기록의 50%, 최대 2초 대기) →
   골격 서명이 이 단계의(A/B 변형 포함) 서명과 같음.
2. **대상 찾기** — slot/ui/identity 규칙대로 정확히 1개(0개·여러 개면 중단).
3. **실행** — 찾은 요소로 새 핸들을 만들어 기존 경로(서버 call_tool → HITL·egress·디스패처의 TOCTOU·
   WS-37 신원 가드·IPI 신호)로 보낸다. 치유는 끈다(heal_disabled). HITL 이 승인을 요구하면 그
   응답을 그대로 돌려주고 멈춘다(승인 증표는 저장하지 않는다 — 결제·제출은 매번 새로 승인).
4. **기대 확인** — Expect 와 다르면 중단.

중단 사유 7종: page_changed · target_not_found · target_ambiguous · not_ready · expect_mismatch ·
approval_required · action_failed. 중단 시 현재 관찰을 함께 싣는다(에이전트가 바로 이어서 판단).
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Dict, List, Optional, Protocol

from recipes import keys
from recipes.recorder import nav_kind, signal_kinds
from recipes.store import RecipeError, RecipeStore, clean_text, render_args

REASONS = ("page_changed", "target_not_found", "target_ambiguous", "not_ready",
           "expect_mismatch", "approval_required", "action_failed")
#: 준비 기준: 기록 당시 요소 수의 이 비율 이상.
READY_RATIO = 0.5
#: 준비 대기 상한(초)과 다시 보는 간격.
READY_WAIT_S = 2.0
READY_POLL_S = 0.2


class ReplayHost(Protocol):
    """재생이 쓰는 서버 쪽 기능(서버가 구현한다 — recipes.service.ServerHost)."""

    def page(self) -> Any: ...

    def epoch(self) -> int: ...

    def register(self, found: Dict[str, Any]) -> str: ...

    async def dispatch(self, action: str, args: Dict[str, Any]) -> Any: ...

    async def observe(self) -> Dict[str, Any]: ...


class _Stop(Exception):
    def __init__(self, reason: str, detail: str, step_result: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(reason)
        assert reason in REASONS
        self.reason = reason
        self.detail = clean_text(detail)
        self.step_result = step_result


def expect_ok(step: Dict[str, Any], before_url: str, result: Any) -> Optional[str]:
    """기대 결과와 비교. 맞으면 None, 다르면 이유 문구."""
    exp = step.get("expect") or {}
    after = str(getattr(result, "current_url", "") or "")
    data = getattr(result, "data", None) or {}
    if step.get("action") == "navigate":
        want = exp.get("url_pat")
        if want and keys.url_pattern(after) != want:
            return "이동한 URL 패턴이 기록과 다름"
        return None
    now = nav_kind(before_url, after)
    if data.get("nav_committed") and now == "none":
        now = "same"
    want_nav = str(exp.get("nav") or "none")
    if want_nav == "none":
        if now != "none":
            return "기록 때는 이동이 없었는데 이동함"
        want = set(exp.get("signals") or []) - {"navigated"}
        if want and not (want & set(signal_kinds(data.get("signals")))):
            return "사후 확인 신호 종류가 기록과 다름"
        return None
    if now == "none":
        return "기록 때는 이동했는데 이동하지 않음"
    if want_nav == "same":
        if now != "same":
            return "같은 출처 이동이어야 하는데 다른 출처로 이동"
        if exp.get("url_pat") and keys.url_pattern(after) != exp.get("url_pat"):
            return "이동한 URL 패턴이 기록과 다름"
    elif want_nav == "cross" and now != "cross":
        return "다른 출처 이동이어야 하는데 같은 출처에 머묾"
    return None


def _is_approval(result: Any) -> bool:
    data = getattr(result, "data", None) or {}
    code = getattr(getattr(result, "error_code", None), "value", getattr(result, "error_code", None))
    if data.get("control"):
        return False
    return (code == "E_HITL_UNATTENDED_BLOCKED" or bool(data.get("requires_confirmation"))
            or "approval" in data)


async def _probe(page: Any, variants: List[Dict[str, Any]], step: Dict[str, Any]) -> Dict[str, Any]:
    """골격·준비 수(+ 첫 변형 대상 찾기)를 한 번의 evaluate 로."""
    target = variants[0].get("target") if variants else None
    if target:
        return await keys.locate(page, target)
    return await keys.snapshot(page, None)


async def run_recipe(host: ReplayHost, store: RecipeStore, rid: str, params: Dict[str, Any],
                     *, encode: Any = None) -> Dict[str, Any]:
    """레시피 하나를 재생한다. 반환: 서버 도구 응답 dict({success, data:{recipe,…}})."""
    rec = store.get(rid)
    if rec is None:
        raise RecipeError(f"레시피 {clean_text(rid)!r} 가 없습니다(browser_recipe list).")
    if rec["stats"].get("disabled"):
        raise RecipeError("이 레시피는 연속 실패로 disabled 입니다 — 다시 기록해 save 하면 갱신됩니다.")
    params = {str(k): str(v) for k, v in (params or {}).items()}
    missing = [p for p in rec.get("params") or [] if p not in params]
    if missing:
        raise RecipeError(f"params.{missing[0]} 가 필요합니다(이 레시피의 params: {rec['params']}).")

    steps_out: List[Dict[str, Any]] = []
    ui_moves: Dict[tuple, List[int]] = {}
    total = len(rec["steps"])
    started = time.perf_counter()
    i = 0
    try:
        for i, step in enumerate(rec["steps"]):
            t0 = time.perf_counter()
            args = render_args(step, params)
            action = step["action"]
            page = host.page()
            before_url = str(getattr(page, "url", "") or "") if page is not None else ""
            chosen: Optional[Dict[str, Any]] = None
            if action != "navigate":
                if page is None:
                    raise _Stop("page_changed", "활성 페이지 없음(프레임 안이거나 탭이 닫힘)")
                if keys.origin_of(before_url) != step["origin"]:
                    raise _Stop("page_changed", "출처가 기록과 다름")
                if keys.url_pattern(before_url) != step["url_pat"]:
                    raise _Stop("page_changed", "URL 패턴이 기록과 다름")
                variants = step.get("variants") or []
                need = math.ceil(READY_RATIO * min((int(v.get("ready") or 0) for v in variants), default=0))
                probe = await _probe(page, variants, step)
                deadline = time.monotonic() + READY_WAIT_S
                while probe["ready"] < need and time.monotonic() < deadline:
                    await asyncio.sleep(READY_POLL_S)
                    probe = await _probe(page, variants, step)
                if probe["ready"] < need:
                    raise _Stop("not_ready", f"상호작용 요소 {probe['ready']}개 — 기록의 50%({need}) 미만")
                chosen = next((v for v in variants if v.get("skel") == probe["skel"]), None)
                if chosen is None:
                    raise _Stop("page_changed", "골격이 다름(A/B 변형 미기록)")
                target = chosen.get("target")
                if target is not None:
                    found = probe if chosen is variants[0] else await keys.locate(page, target)
                    if not found.get("ok"):
                        raise _Stop(str(found.get("reason") or "target_not_found"),
                                    str(found.get("detail") or ""))
                    args["element_id"] = host.register(found)
                    args["epoch"] = host.epoch()
                    if target.get("kind") == "ui" and found.get("pos") and found["pos"] != target.get("pos"):
                        ui_moves[(i, chosen["skel"])] = list(found["pos"])
            result = await host.dispatch(action, args)
            envelope = encode(result) if encode is not None else None
            if not result.success:
                steps_out.append({"action": action, "ok": False})
                if _is_approval(result):
                    raise _Stop("approval_required", "사람 승인이 필요한 단계(재생은 승인을 대신하지 않음)",
                                envelope)
                code = getattr(getattr(result, "error_code", None), "value", "") or ""
                raise _Stop("action_failed", f"{code}: {result.error_message or ''}", envelope)
            why = expect_ok(step, before_url, result)
            if why is not None:
                steps_out.append({"action": action, "ok": False})
                raise _Stop("expect_mismatch", why, envelope)
            steps_out.append({"action": action, "ok": True,
                              "ms": round((time.perf_counter() - t0) * 1000, 1)})
    except _Stop as stop:
        stats = store.note_run(rid, ok=False, count_failure=stop.reason != "approval_required")
        info = {"id": rid, "name": rec["name"], "ran": i, "of": total, "stopped_at": i,
                "reason": stop.reason, "detail": stop.detail, "steps": steps_out,
                "disabled": bool((stats or {}).get("disabled"))}
        data: Dict[str, Any] = {"recipe": info}
        if stop.step_result is not None:
            data["step_result"] = stop.step_result
        data.update(await host.observe())
        data["next"] = "멈춘 지점부터 평소대로 관찰·판단해 이어서 진행하십시오."
        return {"success": False, "error_message": f"레시피 {i}단계에서 멈춤: {stop.reason}", "data": data}
    store.note_run(rid, ok=True, ui_moves=ui_moves)
    page = host.page()
    return {"success": True, "data": {"recipe": {
        "id": rid, "name": rec["name"], "ran": total, "of": total, "steps": steps_out,
        "ms": round((time.perf_counter() - started) * 1000, 1)},
        "current_url": str(getattr(page, "url", "") or "") if page is not None else ""}}
