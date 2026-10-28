"""BrowserMCPServer 의 묶음 실행 서버 도구 browser_batch (WS-73).

에이전트가 여러 단계를 한 번의 호출로 넘기면 차례로 실행한다. 원칙:

* 단계마다 **단건 호출과 같은 관문**(`call_tool` — 조작권·manual-sites·HITL·승인·egress·robots·challenge·IPI 신호).
  임의 JS 실행 없음, 판단은 에이전트. 묶음 전체가 이 작업 공간의 직렬화 구간(`_dispatch_serial`) 안 + 자가 치유 끔.
* 대상은 `target:{role, name, group?}` 로만 지정한다(element_id·selector·좌표·epoch 금지). 단계마다 현재 화면을 전체 후보
  (자르기 전)로 다시 관찰해 role + 이름(NFKC·공백 정규화 정확 일치)으로 정확히 1개일 때만, 그 관찰의 핸들(v1.16.0 핀 포함 —
  실행 직전 묶음 확인이 돈다)로 실행한다. group 은 data.repeated 표시 블록이 아니라 같은 role+이름 후보만으로 **격리 world**
  에서 계산한 묶음 표제와 비교한다(페이지 main world 의 글자 위조가 닿지 않는다).
* 2단계부터 대상은 **묶음 시작 관찰(기준 스냅샷)에 있던 노드**(지문 = css_path + role + 이름 + 격리 world 표제)일 때만
  실행한다 — 1단계가 띄운 모달의 '확인'·SPA 가 새로 만든 같은 이름 단추는 target_new 로 멈춘다. v1.16.0 핀은 지문에서 뺀다
  (WS-62 R4: 첫 관찰에서는 날짜가 바깥 레이아웃 쌍둥이 묶음에 핀되고 클릭 뒤에는 달력 묶음에 핀돼 지문이 흔들린다 —
  실측 777개 중 69개). 핀은 실행 핸들에 그대로 붙어 실행 직전 확인(actions.verification)은 그대로 돈다.
  한계: 같은 위치(css_path)에 같은 이름으로 다시 그려진 다른 요소는 같은 노드로 본다(단건 호출의 재렌더 처리와 같은 수준).
* 위험·변화·불확실이 보이면 그 자리에서 멈춘다(끌 수 없음, 남은 단계 실행 0, 되돌리지 않음) — 멈춘 단계 봉투
  `data.step_result` + 현재 관찰 + `data.next`.
"""

from __future__ import annotations

import json
import time
import unicodedata
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple

from contracts import ActionType, ErrorCode

#: 서버 도구 이름.
BATCH_TOOL = "browser_batch"
#: 단계 수 상한.
MAX_STEPS = 10
#: 묶음 시간 상한(초) — 단계 사이에서만 판정한다(진행 중 단계는 취소하지 않는다).
TIME_LIMIT_S = 60.0
#: 묶음에 넣을 수 있는 액션(v1). 대상이 필요한 액션은 target 필수, scroll 만 선택.
BATCH_ACTIONS = ("click", "type_text", "select_option", "check_box", "hover", "press_key", "scroll")
TARGET_OPTIONAL = frozenset({"scroll"})
#: 단계 args 금지 키 — 하나라도 있으면 묶음 전체를 거부한다(아무것도 실행하지 않음).
FORBIDDEN_ARG_KEYS = frozenset({"element_id", "target_element_id", "selector", "x", "y", "epoch", "workspace",
                                "approval_id", "tab_id"})
#: 응답 관찰 인자(observe_page 계약 키만 — tab_id 등 다른 키 거부).
OBSERVE_KEYS = frozenset({"prune_top_n", "force_full_tree"})

REASONS = ("action_failed", "approval_required", "manual_site", "human_control", "handoff", "challenge", "robots",
           "egress", "injection_suspected", "dialog", "navigated", "new_tab", "target_not_found", "target_ambiguous",
           "target_unverifiable", "target_new", "group_mismatch", "focus_failed", "time_limit")

NEXT = ("앞 단계 효과는 이미 일어났습니다(되돌리지 않음). 묶음 전에 받은 @eN id 는 무효 — 이 응답의 관찰 id 를 쓰고, "
        "멈췄다면 그 지점부터 평소대로 관찰·판단해 이어 가세요.")

DESCRIPTION = ("여러 단계를 한 번에(1~10, target={role,name,group?}, id·selector·좌표 금지). 단계마다 같은 관문, "
               "위험·화면 변화·대상 불확실이면 멈춤(data.batch.reason + 현재 관찰)")

INSTRUCTIONS = (f"{BATCH_TOOL}: 같은 화면에서 이어지는 여러 조작(두 달 달력 날짜 둘 등)은 묶어서 한 번에 — 대상은 role+이름"
                "(+data.repeated 의 group). 멈추면 data.batch.reason 과 응답 관찰을 보고 이어 가세요. 이름이 앞 단계로 바뀌는 "
                "단추·달 넘기기는 묶음 밖에서.")

#: 묶음이 만든 내부 관찰 표시(호출 문맥 단위). 값이 dict 면 그 관찰의 자르기 전 전체 요소를 담는다.
#: 서버는 이 표시가 있는 observe 에서 saved_pages uses 소비·레시피 후보 계산을 건너뛴다(server_tools).
_INTERNAL: ContextVar[Optional[Dict[str, Any]]] = ContextVar("browser_batch_internal_observe", default=None)


def batch_internal() -> Optional[Dict[str, Any]]:
    """지금 호출 문맥이 묶음의 내부 관찰이면 그 수집 dict, 아니면 None."""
    return _INTERNAL.get()


def norm(text: Any) -> str:
    """이름·표제 비교 정규화: NFKC + 공백 묶기 + 앞뒤 정리(대소문자 유지, 부분 일치 없음)."""
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).split())


class _Stop(Exception):
    def __init__(self, reason: str, detail: str, step_result: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(reason)
        assert reason in REASONS
        self.reason = reason
        self.detail = detail
        self.step_result = step_result


class BatchInputError(ValueError):
    pass


def _model_for(action: ActionType) -> Any:
    from contracts import ACTION_INPUT_MAP

    return ACTION_INPUT_MAP[action]


def validate_batch(args: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """입력 검증(전부 통과해야 시작). 반환: (단계 목록, 응답 관찰 인자). 실패면 BatchInputError."""
    extra = set(args) - {"steps", "observe"}
    if extra:
        raise BatchInputError(f"알 수 없는 인자: {sorted(extra)[:3]} (steps·observe 만 — 멈춤 조건은 끌 수 없음)")
    steps = args.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise BatchInputError(f"steps 는 1~{MAX_STEPS}개 목록입니다.")
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise BatchInputError(f"steps[{i}] 는 객체여야 합니다.")
        unknown = set(raw) - {"action", "target", "args"}
        if unknown:
            raise BatchInputError(f"steps[{i}] 의 알 수 없는 키: {sorted(unknown)[:3]}")
        action = raw.get("action")
        if action not in BATCH_ACTIONS:
            raise BatchInputError(f"steps[{i}].action 은 {list(BATCH_ACTIONS)} 중 하나입니다(그 외 액션은 단건 호출로).")
        step_args = raw.get("args") if raw.get("args") is not None else {}
        if not isinstance(step_args, dict):
            raise BatchInputError(f"steps[{i}].args 는 객체여야 합니다.")
        bad = sorted(set(step_args) & FORBIDDEN_ARG_KEYS)
        if bad:
            raise BatchInputError(f"steps[{i}].args 에 쓸 수 없는 키: {bad} — 대상은 target{{role,name,group?}} 으로만.")
        target = raw.get("target")
        if target is None:
            if action not in TARGET_OPTIONAL:
                raise BatchInputError(f"steps[{i}] ({action}) 에는 target{{role,name,group?}} 이 필요합니다.")
        else:
            if not isinstance(target, dict) or set(target) - {"role", "name", "group"}:
                raise BatchInputError(f"steps[{i}].target 은 {{role, name, group?}} 만 받습니다.")
            if not all(isinstance(target.get(k), str) and norm(target.get(k)) for k in ("role", "name")):
                raise BatchInputError(f"steps[{i}].target 의 role·name 은 비어 있지 않은 문자열입니다.")
            if "group" in target and not (isinstance(target["group"], str) and norm(target["group"])):
                raise BatchInputError(f"steps[{i}].target.group 은 비어 있지 않은 문자열입니다.")
        _check_contract_args(i, ActionType(action), step_args, target is not None)
        out.append({"action": action, "target": target, "args": dict(step_args)})
    observe = args.get("observe")
    if observe is None:
        observe = {}
    if not isinstance(observe, dict) or set(observe) - OBSERVE_KEYS:
        raise BatchInputError(f"observe 는 {sorted(OBSERVE_KEYS)} 만 받습니다.")
    try:
        from contracts import ObservePageInput

        ObservePageInput(**observe)
    except Exception as exc:  # noqa: BLE001 - Pydantic ValidationError
        raise BatchInputError(f"observe 검증 실패: {type(exc).__name__}") from None
    return out, dict(observe)


def _check_contract_args(i: int, action: ActionType, step_args: Dict[str, Any], has_target: bool) -> None:
    """계약 입력 모델로 미리 검증한다(실행 때 주입할 element_id·epoch 는 자리표시). 모르는 키도 거부."""
    model = _model_for(action)
    unknown = set(step_args) - set(model.model_fields)
    if unknown:
        raise BatchInputError(f"steps[{i}].args 의 알 수 없는 키: {sorted(unknown)[:3]}")
    trial = dict(step_args)
    if "element_id" in model.model_fields and action is not ActionType.SCROLL:
        trial.update(element_id="@e1", epoch=0)
    elif action is ActionType.SCROLL and has_target:
        trial["target_element_id"] = "@e1"
    try:
        model(**trial)
    except Exception as exc:  # noqa: BLE001 - Pydantic ValidationError
        fields = []
        for err in getattr(exc, "errors", lambda: [])():
            fields.extend(str(x) for x in err.get("loc") or ())
        raise BatchInputError(f"steps[{i}].args 검증 실패({action.value}: {sorted(set(fields))[:3]})") from None


# ---------------------------------------------------------------------------- 격리 world 표제


def _heading_expr(targets: List[Dict[str, Any]]) -> str:
    from perception.repeated import REPEATED_JS

    return f"({REPEATED_JS})({json.dumps({'targets': targets}, ensure_ascii=False)})"


def _headings_from_sets(raw: Any, n: int) -> Dict[int, Tuple[str, ...]]:
    """REPEATED_JS 결과 → 대상 순번별 비교 가능한 표제 후보(보통 표제·첫 조각). 표제를 못 정한 대상은 빠진다."""
    from perception.repeated import _short

    out: Dict[int, Tuple[str, ...]] = {}
    sets = raw.get("sets") if isinstance(raw, dict) else None
    for s in sets if isinstance(sets, list) else []:
        groups = s.get("groups") if isinstance(s, dict) else None
        if not isinstance(groups, list) or len(groups) < 2:
            continue
        heads = [g.get("heading") if isinstance(g, dict) else None for g in groups]
        for g, head in zip(groups, heads):
            if not isinstance(g, dict) or not (isinstance(head, str) and head and heads.count(head) == 1):
                continue
            labels = [norm(_short(head)), norm(head)]
            first = g.get("first")
            if isinstance(first, str) and first:
                labels.append(norm(_short(first)))
            for m in g.get("members") or []:
                idx = m.get("i") if isinstance(m, dict) else None
                if isinstance(idx, int) and 0 <= idx < n and idx not in out:
                    out[idx] = tuple(x for x in labels if x)
    return out


def _observe_tool() -> str:
    from interface.mcp_server import tool_name

    return tool_name(ActionType.OBSERVE_PAGE)


class BatchMixin:
    """browser_batch 서버 도구 (WS-73 믹스인 — 상태는 BrowserMCPServer.__init__)."""

    async def _batch_tool(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            steps, observe_args = validate_batch(args)
        except BatchInputError as exc:
            from recipes.store import clean_text

            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": clean_text(str(exc), 300)}
        if not self._started:
            await self.start()
        await self._ensure_workspace()
        ctx = getattr(self._dispatcher, "ctx", None)
        if ctx is None or getattr(ctx, "root_page", None) is not None or getattr(ctx, "page", None) is None:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": "묶음 실행은 최상위 문서에서만 됩니다(프레임 안이면 switch_frame to_main 뒤 다시)."}
        payload = await self._run_batch(steps, observe_args)
        data = payload.get("data")
        if isinstance(data, dict) and "injection_suspected" not in data:
            from security.injection_signal import injection_signal

            signal = injection_signal(data, None, 10 * self.max_result_chars)
            if signal is not None:
                data["injection_suspected"] = signal
        return payload

    # -- 실행 루프 -------------------------------------------------------------

    async def _run_batch(self, steps: List[Dict[str, Any]], observe_args: Dict[str, Any]) -> Dict[str, Any]:
        from interface.mcp_server import tool_name
        from interface.server_envelope import envelope_dict

        disp = self._dispatcher
        started = time.monotonic()
        total = len(steps)
        steps_out: List[Dict[str, Any]] = []
        ran = 0
        i = 0
        scope = getattr(disp, "heal_disabled_scope", None)
        import contextlib

        heal_off = scope() if callable(scope) else contextlib.nullcontext()
        try:
            with heal_off:
                # 기준 스냅샷: 1단계 전에(에이전트가 넘긴 id 가 없으므로 @eN 재배정 문제 없음). 1단계 해석에도 쓴다.
                view = await self._batch_view()
                baseline = await self._batch_baseline(view, steps)
                for i, step in enumerate(steps):
                    if time.monotonic() - started > TIME_LIMIT_S:
                        raise _Stop("time_limit", f"묶음 시간 상한 {int(TIME_LIMIT_S)}초를 넘어 {i}단계 전에 멈춤")
                    if self._dispatcher is not disp:
                        # 창 전환·작업 공간 복구로 디스패처가 바뀜 — 치유 끔 구간·관찰 근거가 그 디스패처 것이 아니다.
                        raise _Stop("navigated", "브라우저 문맥(디스패처)이 바뀜 — 다시 관찰 필요")
                    if i > 0:
                        view = await self._batch_view(after_step=True)
                    t0 = time.perf_counter()
                    call_args = dict(step["args"])
                    if step["target"] is not None:
                        eid = await self._batch_resolve(view, step["target"], baseline if i > 0 else None, i)
                        if step["action"] == "scroll":
                            call_args["target_element_id"] = eid
                        else:
                            call_args.update(element_id=eid, epoch=view["epoch"])
                        if step["action"] == "press_key":
                            await self._batch_focus(eid)
                    epoch_before = self._batch_epoch()
                    result = await self.call_tool(tool_name(ActionType(step["action"])), call_args)
                    ran = i + 1
                    entry: Dict[str, Any] = {"action": step["action"], "ok": bool(result.success),
                                             "ms": round((time.perf_counter() - t0) * 1000, 1)}
                    saved = result.data.pop("recipe_saved", None) if isinstance(result.data, dict) else None
                    if saved is not None:
                        entry["recipe_saved"] = saved
                    steps_out.append(entry)
                    stop = self._batch_judge(result, epoch_before)
                    if stop is not None:
                        raise _Stop(stop[0], stop[1], envelope_dict(result))
        except _Stop as stop:
            from recipes.store import clean_text

            info = {"ran": ran, "of": total, "stopped_at": i, "reason": stop.reason,
                    "detail": clean_text(stop.detail, 200), "steps": steps_out,
                    "ms": round((time.monotonic() - started) * 1000, 1)}
            data: Dict[str, Any] = {"batch": info}
            if stop.step_result is not None:
                data["step_result"] = stop.step_result
            data.update(await self._batch_observation(observe_args))
            data["next"] = NEXT
            return {"success": False, "error_message": f"묶음 {i}단계에서 멈춤: {stop.reason}", "data": data}
        data = {"batch": {"ran": total, "of": total, "stopped_at": None, "reason": None, "detail": None, "steps": steps_out,
                          "ms": round((time.monotonic() - started) * 1000, 1)}}
        data.update(await self._batch_observation(observe_args))
        data["next"] = NEXT
        return {"success": True, "data": data}

    def _batch_epoch(self) -> int:
        engine = getattr(getattr(self._dispatcher, "ctx", None), "engine", None)
        return int(engine.epoch) if engine is not None else 0

    # -- 관찰 -----------------------------------------------------------------

    async def _batch_view(self, after_step: bool = False) -> Dict[str, Any]:
        """해석 관찰: 일반 observe_page 경로(같은 신호)로 전체 후보를 관찰하고, 자르기 전 요소 목록을 받는다.

        신호(injection_suspected·robots·challenge·handoff·dialogs)가 붙었으면 멈춘다. 반환: {epoch, elements}.
        """
        late = getattr(self._dispatcher, "late_dialogs", None)
        if after_step and callable(late) and late():
            # 앞 단계가 끝난 뒤(다음 dispatch 전) 뜬 다이얼로그 — 다음 dispatch 가 기록을 비우기 전에 본다.
            raise _Stop("dialog", "앞 단계 뒤 다이얼로그가 뜸")
        sink: Dict[str, Any] = {}
        token = _INTERNAL.set(sink)
        try:
            res = await self.call_tool(_observe_tool(), {"force_full_tree": True})
        finally:
            _INTERNAL.reset(token)
        # 해석용 관찰(전체 후보)의 봉투는 step_result 로 싣지 않는다 — 응답의 현재 관찰이 같은 경로로 같은 신호를 싣고,
        # 전체 후보 봉투까지 실으면 응답이 크기 상한의 두 배가 된다. 실패면 그 오류 코드만 detail 에.
        if not res.success:
            stop = self._batch_judge(res, self._batch_epoch())
            raise _Stop(stop[0] if stop else "target_unverifiable", "대상 해석용 관찰 실패 — " + (stop[1] if stop else ""))
        signal = self._batch_signal(res.data or {})
        if signal is not None:
            raise _Stop(signal[0], signal[1] + "(대상 해석용 관찰)")
        elements = sink.get("elements")
        if not isinstance(elements, list):
            raise _Stop("target_unverifiable", "전체 후보를 셀 수 없음")
        return {"epoch": int(res.snapshot_epoch), "elements": elements}

    async def _batch_observation(self, observe_args: Dict[str, Any]) -> Dict[str, Any]:
        """응답에 싣는 현재 관찰 — 일반 observe_page 경로(같은 신호·같은 크기 상한), 인자는 에이전트가 고른 것."""
        from interface.server_envelope import envelope_dict
        from recipes.store import clean_text

        token = _INTERNAL.set({})
        try:
            res = await self.call_tool(_observe_tool(), dict(observe_args))
        except Exception as exc:  # noqa: BLE001 - 관찰 실패로 묶음 응답을 망치지 않는다
            return {"observation_error": clean_text(type(exc).__name__)}
        finally:
            _INTERNAL.reset(token)
        data = envelope_dict(res).get("data") or {}
        out: Dict[str, Any] = {}
        for k in ("observation", "repeated", "injection_suspected", "truncated", "challenge", "handoff", "hint",
                  "robots", "dialogs"):
            if data.get(k) is not None:
                out[k] = data[k]
        if not res.success:
            out["observation_error"] = clean_text(res.error_message or "", 200)
        return out

    # -- 판정 -----------------------------------------------------------------

    @staticmethod
    def _batch_signal(data: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """관찰·단계 결과의 멈춤 신호. 없으면 None."""
        from security.manual_sites import is_manual_handoff

        if "approval" in data or data.get("requires_confirmation"):
            return "approval_required", "사람 승인이 붙은 단계(묶음은 승인 뒤 계속 가지 않음)"
        if is_manual_handoff(data):
            return "manual_site", "직접 조작할 사이트(사람이 직접 해야 함)"
        if data.get("control"):
            return "human_control", "사람이 조작권을 가짐"
        if data.get("challenge") is not None:
            return "challenge", "캡차/차단 화면"
        if data.get("handoff") is not None:
            return "handoff", "사람 인계 안내가 붙음"
        robots = data.get("robots")
        if isinstance(robots, dict) and (robots.get("disallowed") or robots.get("ai_agents_disallowed")):
            return "robots", "robots.txt 금지 신호"
        if data.get("egress"):
            return "egress", "외부 요청 차단(egress)"
        if data.get("injection_suspected") is not None:
            return "injection_suspected", "페이지 글자에 지시처럼 보이는 문구(IPI 신호)"
        if data.get("dialogs"):
            return "dialog", "다이얼로그가 뜸"
        return None

    def _batch_judge(self, result: Any, epoch_before: int) -> Optional[Tuple[str, str]]:
        """단계 결과 판정. 멈춰야 하면 (reason, detail)."""
        data = result.data if isinstance(getattr(result, "data", None), dict) else {}
        if not result.success:
            from security.manual_sites import is_manual_handoff

            code = getattr(getattr(result, "error_code", None), "value", "") or ""
            # 레시피 재생(recipes.replay)과 같은 순서: 직접 조작할 사이트 → 사람 조작권 → 승인 → 그 밖의 신호 → 실패.
            if is_manual_handoff(data):
                return "manual_site", "직접 조작할 사이트(사람이 직접 해야 함 — 묶음은 대신하지 않음)"
            if data.get("control"):
                return "human_control", "사람이 조작권을 가짐"
            if code == ErrorCode.HITL_UNATTENDED_BLOCKED.value or data.get("requires_confirmation") or "approval" in data:
                return "approval_required", "사람 승인이 필요한 단계(묶음은 승인을 대신하지 않음)"
            signal = self._batch_signal(data)
            if signal is not None:
                return signal
            return "action_failed", f"{code}: {result.error_message or ''}"
        signal = self._batch_signal(data)
        if signal is not None:
            return signal
        if data.get("nav_committed") is True or self._batch_epoch() > epoch_before:
            return "navigated", "주 프레임 새 문서(이전 관찰 무효)"
        if data.get("opened_tab_ids"):
            return "new_tab", "새 탭/팝업이 열림"
        return None

    # -- 대상 해석 ---------------------------------------------------------------

    def _batch_matches(self, view: Dict[str, Any], target: Dict[str, Any]) -> List[Tuple[str, Any]]:
        """해석 관찰(자르기 전 전체 요소)에서 role + 이름 정확 일치 후보 (id, 핸들). 핸들은 그 관찰의 것."""
        engine = self._dispatcher.ctx.engine
        role, name = norm(target["role"]), norm(target["name"])
        out: List[Tuple[str, Any]] = []
        for el in view["elements"]:
            if norm(el.get("role")) != role or norm(el.get("name")) != name:
                continue
            h = engine.get_handle(str(el.get("element_id") or ""))
            if h is None or h.epoch != view["epoch"]:
                raise _Stop("target_unverifiable", "후보 핸들이 관찰과 맞지 않음")
            out.append((str(el["element_id"]), h))
        return out

    async def _batch_headings(self, handles: List[Any]) -> Dict[int, Tuple[str, ...]]:
        """같은 role+이름 후보만으로 묶음 표제를 **격리 world** 에서 계산한다. 실패=멈춤."""
        targets = [{"i": k, "css": h.css_path, "name": h.name, "role": h.role} for k, h in enumerate(handles)]
        evaluate = getattr(self._dispatcher, "isolated_eval", None)
        if not callable(evaluate):
            raise _Stop("target_unverifiable", "격리 world 판정 불가")
        try:
            raw = await evaluate(_heading_expr(targets))
        except Exception:  # noqa: BLE001 - 판정 불가 = 멈춤
            raise _Stop("target_unverifiable", "묶음 표제 계산 실패(격리 world)") from None
        return _headings_from_sets(raw, len(handles))

    async def _batch_fingerprints(self, view: Dict[str, Any], target: Dict[str, Any]) -> List[Tuple[str, Any, Tuple]]:
        """후보 (id, 핸들, 지문). 지문 = css_path + role + 이름 + (group 지정 시) 격리 world 표제."""
        matches = self._batch_matches(view, target)
        heads: Dict[int, Tuple[str, ...]] = {}
        plain = [(eid, h) for eid, h in matches if not h.is_shadow]
        if target.get("group") is not None and plain:
            heads = await self._batch_headings([h for _, h in plain])
        out = []
        k = 0
        for eid, h in matches:
            head: Tuple[str, ...] = ()
            if not h.is_shadow:
                head = heads.get(k, ())
                k += 1
            out.append((eid, h, (h.css_path, norm(h.role), norm(h.name), head)))
        return out

    async def _batch_baseline(self, view: Dict[str, Any], steps: List[Dict[str, Any]]) -> Dict[str, set]:
        """기준 스냅샷: 단계 대상별 후보 지문 집합(묶음 시작 관찰 = 1단계 전)."""
        base: Dict[str, set] = {}
        for step in steps:
            target = step["target"]
            if target is None:
                continue
            key = json.dumps(target, sort_keys=True, ensure_ascii=False)
            if key not in base:
                base[key] = {fp for _, _, fp in await self._batch_fingerprints(view, target)}
        return base

    async def _batch_resolve(self, view: Dict[str, Any], target: Dict[str, Any],
                             baseline: Optional[Dict[str, set]], i: int) -> str:
        """대상 해석 → 실행할 element_id(이 관찰의 v1.16.0 핀이 붙은 핸들). 정확히 1개가 아니면 멈춘다."""
        cands = await self._batch_fingerprints(view, target)
        if not cands:
            raise _Stop("target_not_found", f"{i}단계 대상(role·이름 정확 일치)이 화면에 없음")
        if any(h.is_shadow for _, h, _ in cands):
            raise _Stop("target_unverifiable", f"{i}단계 대상 후보가 shadow DOM 안(묶음 확인 불가)")
        group = target.get("group")
        if group is not None:
            want = norm(group)
            unknown = [c for c in cands if not c[2][3]]
            hit = [c for c in cands if want in c[2][3]]
            if not hit:
                if unknown:
                    raise _Stop("target_unverifiable", f"{i}단계 대상의 묶음 표제를 정할 수 없음")
                raise _Stop("group_mismatch", f"{i}단계 대상이 지정한 group 에 없음")
            cands = hit
        if len(cands) != 1:
            raise _Stop("target_ambiguous", f"{i}단계 대상 후보 {len(cands)}개(group 으로 좁히세요)")
        eid, handle, fp = cands[0]
        same_name = len(self._batch_matches(view, target))
        if same_name >= 2 and not getattr(handle, "group_path", None):
            raise _Stop("target_unverifiable", f"{i}단계 같은 이름 후보 {same_name}개인데 실행 직전 묶음 확인 불가")
        if baseline is not None:
            key = json.dumps(target, sort_keys=True, ensure_ascii=False)
            if fp not in baseline.get(key, set()):
                raise _Stop("target_new", f"{i}단계 대상이 묶음 시작 때 없던 노드(앞 단계로 새로 생김)")
        return eid

    async def _batch_focus(self, eid: str) -> None:
        """press_key 대상에 포커스를 주고, 격리 world 에서 그 요소가 포커스를 가졌는지 확인한다. 실패=멈춤."""
        handle = self._dispatcher.ctx.engine.get_handle(eid)
        page = self._dispatcher.ctx.page
        try:
            await page.locator(handle.css_path).first.focus(timeout=2000)
            css = json.dumps(handle.css_path)
            ok = await self._dispatcher.isolated_eval(
                f"(() => {{ const e = document.querySelector({css}); return !!e && e === document.activeElement; }})()")
        except Exception:  # noqa: BLE001 - 포커스 판정 불가 = 멈춤
            ok = False
        if ok is not True:
            raise _Stop("focus_failed", "키 입력 대상에 포커스를 확인하지 못함")
