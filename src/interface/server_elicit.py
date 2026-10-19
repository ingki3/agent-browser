"""BrowserMCPServer 의 대역 밖 승인 두 가지 (WS-56): MCP elicitation(클라이언트의 사람 확인) · 사이트별 상시 허용 정책.

고위험 액션이 HITL 게이트에 막히면(그리고 이 호출에 approval_id 가 없으면) `_check_hitl` 이 여기 `_out_of_band`
를 부른다. 순서:

1. **정책**(사람 CLI `agent-browser approve-policy` 가 프로필 안 0600 파일에 쓴 것) — (origin, 액션, [대상 이름]) 이
   맞고 최고위험 범주(security.approve_policy.forbidden_category)가 아니면 실행(`data.approval.via="policy"`).
   범주에 걸리면 `data.approval.policy_refused=<범주>` 를 남기고 다음 단계로.
2. **elicitation** — 이 도구 호출을 보낸 클라이언트가 초기화 때 elicitation 을 선언했으면, 같은 호출 안에서
   `elicitation/create`(빈 스키마 = 버튼 승인만)를 보내고 기다린다(상한 `elicit_timeout`). accept 면 실행 직전에
   승인 증표 실행 경로와 같은 재확인(대상 digest: 이름·문맥·탭·origin·epoch + 요소 연결·role·이름)을 하고 자가
   치유를 끈 채 실행(`data.approval.via="elicitation"`). 바뀌었으면 E_TOCTOU_MISMATCH.
3. 그 밖(decline·cancel·시간 초과·오류·미지원) → None: 기존 경로(창이 있으면 증표, headless 면 안내) 그대로.
   응답했으면 `data.elicitation.answer` 만 덧붙인다. 미지원 클라이언트는 아무것도 덧붙이지 않는다(바이트 동일).

신뢰 경계: elicitation 응답은 서버가 보낸 JSON-RPC 요청 id 에 대한 클라이언트 응답만 SDK 가 이어 준다 — 도구 인자나
서버 도구로는 만들 수 없다. 그 클라이언트(예: Hermes gateway 의 텔레그램 버튼)가 정말 사람에게 물었는지는 서버가
확인할 수 없다 — 클라이언트를 신뢰하는 등급이다(README '사람 인계' 절 한계).

이 모듈은 WS-48 분리 모듈이 아니다(새 코드) — 전역 이름은 이 모듈에서 찾는다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextvars import ContextVar
from typing import Any, Dict, Optional

from contracts import ActionResult, ActionType, ErrorCode

logger = logging.getLogger("interface.mcp_server")

#: elicitation 응답 대기 상한(초). 승인 증표 수명(approval_ttl_s)보다 길지 않다.
ELICIT_TIMEOUT_S = 120.0

#: 이 도구 호출을 보낸 MCP 클라이언트 세션(create_server 의 도구 처리기가 호출마다 설정). 직접 call_tool(테스트·
#: 하네스) 에는 없다 → elicitation 없음.
CURRENT_ELICITOR: ContextVar[Optional["Elicitor"]] = ContextVar("agent_browser_elicitor", default=None)
#: 호출별 상태(ElicitMixin 속성의 저장소) — 이번 호출의 실행 근거 · 폴백 결과 표지.
_APPROVAL_VIA: ContextVar[Optional[Dict[str, Any]]] = ContextVar("agent_browser_approval_via", default=None)
_OOB_NOTE: ContextVar[Optional[Dict[str, Any]]] = ContextVar("agent_browser_oob_note", default=None)
#: WS-56 R1 B2: 승인 증표 호출 표지·통과 근거도 호출 문맥에 — 인스턴스 속성이면 동시 호출 하나가 call 입구에서
#: None 으로 지워(다른 호출의 치유 금지·증표 결과가 사라짐) 섞였다. (WS-57 의 _CALL_STATE 와 병합 때 맞출 것.)
_USED_APPROVAL: ContextVar[Optional[str]] = ContextVar("agent_browser_used_approval", default=None)
_PENDING_GATE_BASIS: ContextVar[Optional[Dict[str, Any]]] = ContextVar("agent_browser_pending_gate_basis",
                                                                        default=None)
#: WS-56 R1 B1: 이번 호출이 사람 승인으로 열렸을 때 승인 요청 시점에 잡은 대상 노드(디스패처 PinnedTarget).
_APPROVAL_PIN: ContextVar[Any] = ContextVar("agent_browser_approval_pin", default=None)
#: WS-56 R2 B4: 이 호출이 승인 실행 구간 락(ElicitMixin._approval_exec_lock)을 잡았는가.
_EXEC_HELD: ContextVar[bool] = ContextVar("agent_browser_approval_exec_held", default=False)

#: 응답 action → data.elicitation.answer
_STATUS = {"decline": "declined", "cancel": "cancelled", "timeout": "timeout", "error": "error"}


async def _release(dispatcher: Any, pin: Any) -> None:
    if pin is None:
        return
    release = getattr(dispatcher, "release_pin", None)
    if release is not None:
        await release(pin)


def elicit_timeout(approval_ttl_s: float) -> float:
    return float(min(ELICIT_TIMEOUT_S, float(approval_ttl_s)))


class Elicitor:
    """한 도구 호출의 클라이언트 세션 — 그 호출에 묶인 elicitation 요청을 보낸다."""

    def __init__(self, session: Any, related_request_id: Any = None) -> None:
        self.session = session
        self.related_request_id = related_request_id

    def supported(self) -> bool:
        """클라이언트가 초기화 capabilities 에 elicitation 을 선언했는가. 확인 실패 = 미지원."""
        try:
            from mcp import types as t

            return bool(self.session.check_client_capability(
                t.ClientCapabilities(elicitation=t.ElicitationCapability())))
        except Exception:  # noqa: BLE001 - 확인 불가 = 미지원(기존 경로)
            return False

    async def ask(self, message: str, timeout_s: float) -> str:
        """'accept' | 'decline' | 'cancel' | 'timeout' | 'error'. 실패는 모두 accept 가 아니다(fail-closed)."""
        send = getattr(self.session, "elicit_form", None) or getattr(self.session, "elicit", None)
        if send is None:
            return "error"
        try:
            result = await asyncio.wait_for(
                send(message, {"type": "object", "properties": {}}, self.related_request_id), timeout_s)
        except asyncio.TimeoutError:
            return "timeout"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 클라이언트 오류 응답·검증 실패·끊김
            logger.info("elicitation 실패(%s) — 기존 승인 경로로", type(exc).__name__)
            return "error"
        action = getattr(result, "action", None)
        return action if action in ("accept", "decline", "cancel") else "error"


def elicitation_message(action: ActionType, element_name: str, origin: str, tab_id: str,
                        reason: str, nonce: str, destination: Optional[Dict[str, Any]] = None) -> str:
    """사람에게 보일 승인 문구. 페이지 유래 문자열은 가림(WS-51) → 코드 흉내 숫자 가림 → 표시 살균(제어·양방향).

    R2 B3: destination(승인 대상 노드의 실행 의미 — 링크 href · 폼 action/method/target)이 있으면 '목적지' 줄을 더한다
    (주소는 public_url 가림 · 코드 흉내 숫자 가림 · 표시 살균). 실행 직전 같은 값인지 다시 본다.
    """
    from interface.handoff import SITE_NAME_LABEL, display_safe, mask_code_like
    from security.public_url import public_text, public_url

    name = display_safe(mask_code_like(public_text(str(element_name or ""))), 120) or "(이름 없음)"
    site = display_safe(public_url(str(origin or "")), 120) or "(알 수 없음)"
    why = display_safe(mask_code_like(public_text(str(reason or ""))), 200)
    dest = destination_text(destination)
    return "\n".join([
        f"agent-browser 고위험 동작 승인 요청 [{nonce}]",
        "'항상 허용'·'세션 허용'은 누르지 마세요 — 이번 한 건만 승인하세요"
        "(사이트별 상시 허용은 터미널 `agent-browser approve-policy` 로만).",
        f"동작: {action.value}",
        f"대상: {SITE_NAME_LABEL} {name}",
        f"사이트: {site}",
        *([f"목적지: {dest}"] if dest else []),
        f"탭: {display_safe(str(tab_id or ''), 40)}",
        f"이유: {why}",
        "승인하면 실행 직전에 대상이 그대로인지 다시 확인합니다. 요청한 적 없는 동작이면 거절하세요.",
    ])


def destination_text(meaning: Any) -> str:
    """승인 대상의 목적지 한 줄(표시용, 가림 적용). 링크면 href, 폼 제출이면 'METHOD action'. 없으면 ''."""
    from interface.handoff import display_safe, mask_code_like
    from security.public_url import public_url

    if not isinstance(meaning, dict):
        return ""

    def url(raw: Any) -> str:
        return display_safe(mask_code_like(public_url(str(raw or ""))), 200)

    target = str(meaning.get("target") or "")
    suffix = f" (새 창/프레임: {display_safe(target, 40)})" if target and target != "_self" else ""
    if meaning.get("href"):
        return url(meaning["href"]) + suffix
    if meaning.get("action"):
        method = display_safe(str(meaning.get("method") or "get"), 10).upper()
        return f"{method} {url(meaning['action'])}" + suffix
    return ""


class ElicitMixin:
    """BrowserMCPServer 의 대역 밖 승인(WS-56) — 상태: `_approval_via`(이번 호출 실행 근거), `_policy_store`.

    호출별 상태(`_approval_via`·`_oob_note`)는 인스턴스가 아니라 **호출 문맥(contextvar)** 에 둔다 — elicitation 이
    최대 120초 기다리는 동안 같은 서버의 다른 도구 호출이 그 값을 지우거나 가져가지 않게(SDK 는 요청마다 다른 태스크).
    """

    @property
    def _approval_via(self) -> Optional[Dict[str, Any]]:
        """이번 호출이 정책·elicitation 으로 열렸으면 결과에 실을 data.approval 원본(_run_gated_action 이 꺼낸다)."""
        return _APPROVAL_VIA.get()

    @_approval_via.setter
    def _approval_via(self, value: Optional[Dict[str, Any]]) -> None:
        _APPROVAL_VIA.set(value)

    @property
    def _oob_note(self) -> Optional[Dict[str, Any]]:
        """차단 결과에 덧붙일 정보(policy_refused·elicitation 응답) — 이번 호출에서만."""
        return _OOB_NOTE.get()

    @_oob_note.setter
    def _oob_note(self, value: Optional[Dict[str, Any]]) -> None:
        _OOB_NOTE.set(value)

    @property
    def _used_approval(self) -> Optional[str]:
        """이번 호출이 쓴 승인 증표 id(_use_approval 이 통과시킴) — 호출 문맥 단위(B2)."""
        return _USED_APPROVAL.get()

    @_used_approval.setter
    def _used_approval(self, value: Optional[str]) -> None:
        _USED_APPROVAL.set(value)

    @property
    def _pending_gate_basis(self) -> Optional[Dict[str, Any]]:
        """통과한 게이트 근거(좌표 클릭 등) — 결과에 싣는다. 호출 문맥 단위(B2)."""
        return _PENDING_GATE_BASIS.get()

    @_pending_gate_basis.setter
    def _pending_gate_basis(self, value: Optional[Dict[str, Any]]) -> None:
        _PENDING_GATE_BASIS.set(value)

    @property
    def _approval_pin(self) -> Any:
        """이번 호출을 연 사람 승인의 대상 노드(승인 요청 때 잡음) — 디스패처는 이 노드에만 동작한다(B1)."""
        return _APPROVAL_PIN.get()

    @_approval_pin.setter
    def _approval_pin(self, value: Any) -> None:
        _APPROVAL_PIN.set(value)

    _policy_store: Any = None

    # -- R2 B4: 승인 실행 구간 직렬화 -------------------------------------------------------------------------
    # 승인(정책·elicitation·승인 증표)으로 연 호출의 '재확인 → 실제 입력 → 효과 판정' 을 서버 단위 asyncio 락으로 한
    # 호출씩. 디스패처는 dispatch 마다 비우는 인스턴스 기록(이동 대기·다이얼로그·팝업·최상위 기록기)을 쓰고 실제 입력도
    # 공유 Chromium 이라, 동시 두 승인 호출이 겹치면 한쪽 입력이 사라지고도 둘 다 성공으로 보고됐다(검증 BLOCKING-4).
    # 사람 응답 대기(elicitation)는 락 밖이다. 잡은 호출 표시는 호출 문맥(_EXEC_HELD) — 같은 호출 안 중첩(헤드리스
    # 재시도 등)은 다시 잡지 않는다. 푸는 곳은 _call_tool 의 finally 한 곳(_release_approval_exec).
    # 병합 메모: WS-57 의 같은 작업 공간 dispatch 직렬화 락이 들어오면 그 락으로 통일한다.

    def _approval_exec_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_approval_exec_lock_obj", None)
        if lock is None:
            lock = asyncio.Lock()
            self._approval_exec_lock_obj = lock
        return lock

    def _approval_exec_held(self) -> bool:
        """이 호출 문맥이 승인 실행 구간 락을 잡고 있는가."""
        return _EXEC_HELD.get()

    async def _hold_approval_exec(self) -> None:
        """이 호출이 승인 실행 구간 락을 잡는다(이미 잡았으면 그대로)."""
        if _EXEC_HELD.get():
            return
        await self._approval_exec_lock().acquire()
        _EXEC_HELD.set(True)

    def _release_approval_exec(self) -> None:
        """이 호출이 잡은 승인 실행 구간 락을 푼다(안 잡았으면 아무것도 안 함)."""
        if _EXEC_HELD.get():
            _EXEC_HELD.set(False)
            self._approval_exec_lock().release()

    def _approval_policies(self) -> Any:
        """serve --profile 로 잡은 프로필의 정책 저장소. 프로필이 없으면 None(정책 없음)."""
        from pathlib import Path

        from security.approve_policy import POLICY_FILE, PolicyStore

        lock = getattr(self, "_profile_lock", None)
        if self.profile is None or lock is None or not getattr(lock, "held", False):
            return None
        path = Path(lock.path) / POLICY_FILE
        if self._policy_store is None or self._policy_store.path != path:
            self._policy_store = PolicyStore(path)
        return self._policy_store

    async def _out_of_band(self, action: ActionType, params: Dict[str, Any], decision: Any, ctx: Any,
                           report_basis: bool) -> Any:
        """True = 실행(정책·elicitation 승인), ActionResult = 그 결과로 끝(대상 바뀜 등), None = 기존 경로."""
        self._oob_note = None
        refused = self._policy_pass(action, ctx)
        if refused is True:
            # R2 B4: 정책으로 연 호출도 실제 입력~효과 판정을 다른 승인 호출과 겹치지 않게(서버 단위 직렬화).
            await self._hold_approval_exec()
            if report_basis:
                self._pending_gate_basis = dict(decision.basis)
            return True
        elicitor = CURRENT_ELICITOR.get()
        if elicitor is None or not elicitor.supported():
            return None
        components = self._approval_components(action, params, dict(decision.basis))
        # B1: 사람에게 묻는 지금의 실제 노드를 잡는다 — 못 잡으면 결속할 수 없으니 묻지 않는다(기존 경로).
        # R2 B2: 좌표(hit-test 노드)·키(포커스 노드)도 같은 규칙.
        pinned = await self._pin_approval_target(params)
        if pinned is False:
            return None
        message = elicitation_message(action, ctx.element_name or ctx.selector, components["origin"],
                                      components["tab_id"], decision.reason, secrets.token_hex(3),
                                      destination=getattr(pinned, "meaning", None))
        self._approval_pin = pinned
        try:
            answer = await elicitor.ask(message, elicit_timeout(self.hub.approval_ttl_s))
            if answer == "accept":
                # R2 B4: 사람 응답 대기는 락 밖 — 응답 뒤 재확인·실제 입력·효과 판정은 서버 단위로 한 호출씩.
                # (락은 _call_tool 끝에서 푼다 — _release_approval_exec.)
                await self._hold_approval_exec()
            checked = (await self._recheck_after_accept(action, params, components)
                       if answer == "accept" else None)
        except BaseException:
            await self._drop_approval_pin()
            raise
        if answer != "accept":
            await self._drop_approval_pin()
            note = dict(self._oob_note or {})
            note["elicitation"] = {"answer": _STATUS.get(answer, "error")}
            self._oob_note = note
            return None
        if checked is not None:
            await self._drop_approval_pin()
            return checked
        via: Dict[str, Any] = {"via": "elicitation"}
        if self._oob_note and "policy_refused" in self._oob_note:
            via["policy_refused"] = self._oob_note["policy_refused"]
        self._approval_via = via
        if report_basis:
            self._pending_gate_basis = dict(decision.basis)
        return True

    def _policy_pass(self, action: ActionType, ctx: Any) -> Any:
        """정책 일치 + 최고위험 아님 → True(이번 호출 via=policy). 범주 거부는 _oob_note 에 남긴다."""
        store = self._approval_policies()
        if store is None:
            return None
        row = store.match(self._current_origin(), action, ctx.element_name)
        if row is None:
            return None
        from security.approve_policy import forbidden_category

        category = forbidden_category(ctx)
        if category is not None:
            self._oob_note = {"policy_refused": category}
            return None
        self._approval_via = {"via": "policy", "policy": {
            "origin": row["origin"], "action": row["action"], "target_name": row.get("target_name")}}
        return True

    async def _recheck_after_accept(self, action: ActionType, params: Dict[str, Any],
                                    components: Dict[str, Any]) -> Optional[ActionResult]:
        """사람이 승인한 그 대상인가 — 승인 증표 실행 경로(_use_approval)와 같은 재확인."""
        from interface.handoff import DIGEST_KEYS, action_digest, normalize_components

        # 기다리는 동안 사람이 조작권을 가져갔으면 실행하지 않는다.
        refused = await self._refuse_before_validation(action, params, {})
        if refused is not None:
            return refused
        target = await self._gate_target(action, params)
        if isinstance(target, ActionResult):
            return target
        decision = self._hitl.evaluate(self._gate_context(action, target))
        now = self._approval_components(action, params, dict(decision.basis))
        if action_digest(now) != action_digest(components):
            before, after = normalize_components(components), normalize_components(now)
            diff = ", ".join(k for k in DIGEST_KEYS if before.get(k) != after.get(k)) or "내용"
            return self._elicitation_toctou(action, f"승인 뒤 바뀜: {diff}")
        check = getattr(self._dispatcher, "approval_target_check", None)
        if check is None:
            fresh = {"fresh": False, "detail": "대상 확인 불가"}  # fail-closed
        else:
            try:
                fresh = await check(params)
            except Exception as exc:  # noqa: BLE001
                fresh = {"fresh": False, "detail": f"대상 확인 실패: {type(exc).__name__}"}
        if not fresh.get("fresh"):
            return self._elicitation_toctou(action, str(fresh.get("detail") or ""))
        # B1: 이름·role·css 가 같아도 승인 요청 때 그 노드가 아니면(새 노드로 교체) 실행하지 않는다.
        same = await self._pinned_check(self._approval_pin, params)
        if not same.get("fresh"):
            return self._elicitation_toctou(action, str(same.get("detail") or ""))
        return None

    async def _pin_approval_target(self, params: Dict[str, Any]) -> Any:
        """사람 승인을 요청하는 지금 대상 노드를 잡는다(B1). None = 요소 대상 아님(좌표·키 — 결속 대상 없음),
        False = 요소 대상인데 못 잡음(fail-closed), 그 밖 = 잡은 노드(PinnedTarget)."""
        from actions.dispatcher import ActionDispatcher

        if ActionDispatcher.pin_key(params) is None:
            return None
        pin = getattr(self._dispatcher, "pin_target", None)
        if pin is None:
            return False
        try:
            got = await pin(params)
        except Exception as exc:  # noqa: BLE001
            logger.info("승인 대상 노드 확인 실패(%s)", type(exc).__name__)
            return False
        return got.get("pin") or False

    async def _pinned_check(self, pin: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        """실행 직전 지금 대상이 잡아 둔 그 노드인가. 요소 대상이 아니면 통과, 확인 수단·노드가 없으면 실패."""
        from actions.dispatcher import ActionDispatcher

        if ActionDispatcher.pin_key(params) is None:
            return {"fresh": True, "detail": ""}
        check = getattr(self._dispatcher, "pinned_target_check", None)
        if pin is None or check is None:
            return {"fresh": False, "detail": "승인 때 잡은 대상 노드 없음"}  # fail-closed
        try:
            return await check(pin, params)
        except Exception as exc:  # noqa: BLE001
            return {"fresh": False, "detail": f"대상 노드 확인 실패: {type(exc).__name__}"}

    async def _drop_approval_pin(self) -> None:
        """이번 호출의 잡은 노드를 놓는다(호출 끝·실행 안 함)."""
        pin, self._approval_pin = self._approval_pin, None
        await _release(self._dispatcher, pin)

    def _elicitation_toctou(self, action: ActionType, detail: str) -> ActionResult:
        return self._error_result(
            action,
            ErrorCode.TOCTOU_MISMATCH,
            "승인한 대상이 바뀌어 실행하지 않았습니다(" + detail + "). 다시 관찰한 뒤 호출하세요 — "
            "다시 승인을 묻습니다.",
            data={"approval": {"via": "elicitation", "reason": "target_changed",
                               "detail": detail}},
        )

    def _attach_oob_note(self, result: ActionResult) -> ActionResult:
        """기존 경로(차단) 결과에 이번 호출의 정책 거부 범주·elicitation 응답을 덧붙인다."""
        note, self._oob_note = self._oob_note, None
        if not note:
            return result
        if "policy_refused" in note:
            approval = result.data.get("approval")
            approval = dict(approval) if isinstance(approval, dict) else {}
            approval["policy_refused"] = note["policy_refused"]
            result.data["approval"] = approval
        if "elicitation" in note:
            result.data["elicitation"] = note["elicitation"]
            if note["elicitation"]["answer"] == "declined":
                result.error_message = ("사람이 승인 요청을 거절했습니다 — 같은 동작을 다시 요청하지 말고 사용자에게 "
                                        "확인하세요. " + (result.error_message or ""))
        return result

    def _attach_via_outcome(self, via: Dict[str, Any], result: ActionResult) -> None:
        """정책·elicitation 으로 실행한 결과에 data.approval(via·outcome). 불확실하면 재시도 금지."""
        from interface.server_hitl import _UNCERTAIN_CODES

        unknown = (not result.success) and result.error_code in _UNCERTAIN_CODES
        info = dict(via)
        info["outcome"] = "outcome_unknown" if unknown else ("succeeded" if result.success else "failed")
        if unknown:
            result.retry_safe = False
            info["note"] = ("실행 결과가 불확실합니다(이미 실행됐을 수 있음). 자동으로 다시 시도하지 말고 "
                            "화면을 관찰해 확인하세요.")
        result.data["approval"] = info
