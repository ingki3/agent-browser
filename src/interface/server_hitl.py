"""BrowserMCPServer 의 HITL 승인 게이트·승인 증표 사용·대상 판정(WS-29·30·31·39).

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from contracts import ActionResult, ActionType, ErrorCode
from interface.server_common import _iso


def pre_approve_hint(action: ActionType, element_name: str) -> str:
    """`--pre-approve` 에 그대로 넣으면 이 액션을 여는 값 (HITLGate._is_pre_approved 형식).

    요소 이름이 없으면(업·다운로드, press_key 등) 이름 매칭이 불가능하므로 `<action>:*`.
    """
    name = (element_name or "").strip()
    return f"{action.value}:{name}" if name else f"{action.value}:*"


def _signals_of(info: Any) -> tuple:
    """페이지에서 읽은 대상 정보의 문맥 신호 [[원천, 텍스트], …] → 게이트 입력 튜플 (WS-31)."""
    if not isinstance(info, dict):
        return ()
    out = []
    for item in info.get("signals") or ():
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
    return tuple(out)


def blocked_hint_text(hint: str, approval_id: str = "") -> str:
    """무인 차단 메시지 끝에 붙는 사람(운영자)용 해결 경로."""
    text = ""
    if approval_id:
        text += approve_hint_text(approval_id)
    return text + (
        f" — 이 액션을 허용하려면 서버를 `--pre-approve \"{hint}\"` 으로 다시 띄우거나"
        " `--mode interactive` 를 쓰세요. 사용자에게 이 안내를 전하고 멈추세요."
    )


def approve_hint_text(approval_id: str) -> str:
    """승인 증표 해결 경로 (WS-29): 사람만 승인할 수 있다 — 에이전트에게 승인 도구는 없다."""
    return (
        f" — 사람에게 `agent-browser approve {approval_id}` 실행을 요청하세요"
        f"(승인 뒤 같은 인자에 approval_id 를 더해 다시 호출, browser_approval_wait 로 대기)"
    )


#: Chromium 실측(WS-30 R1, 제출 버튼 있는 폼): 포커스된 <input type=…> 에서 Enter 가 폼을 제출하는 type.
#: text/search/email/number/password/tel/url/date/time/datetime-local/month/week/checkbox/radio/range
#: + 버튼형 submit/image. 제출 안 함: color/file/button/reset(reset 은 폼을 비운다 — 제출은 아님).
#: type 속성이 없거나 모르는 값이면 브라우저는 text 로 다룬다 → el.type 은 'text' 로 읽힌다.
_ENTER_SUBMITS_INPUT_TYPES = frozenset({
    "text", "search", "email", "number", "password", "tel", "url",
    "date", "time", "datetime-local", "month", "week",
    "checkbox", "radio", "range", "submit", "image",
})


#: 키로 누르면 그 요소를 클릭하는 input type(이름 게이트 대상).
_BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})


def _gate_rank(action: ActionType, domain: str, name: str, submits_form: bool, unresolved: str,
               signals: tuple) -> tuple:
    """한 판정 결과의 위험 순위 (WS-67). 판정 불가 > 폼 제출 > 신호(키워드) > 없음.

    격리 world 판정과 페이지 world 판정을 **각각** 기존 `assess_risk` 로 분류한 뒤 이 순위로 높은
    쪽을 고른다(필드 단위 OR 아님). (등급, 근거 범주) 튜플 — 같으면 기존 판정을 쓴다.
    """
    from security import ActionContext, RiskLevel
    from security.hitl import assess_risk

    assessed = assess_risk(ActionContext(
        action=action, element_name=name, domain=domain, submits_form=submits_form,
        unresolved_target=unresolved, signals=signals,
    ))
    level = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}[assessed.risk]
    source = assessed.basis.get("source")
    if source == "unresolved":
        category = 3
    elif source == "form_submit":
        category = 2
    elif assessed.basis.get("matched_keyword"):
        category = 1
    else:
        category = 0
    return level, category


async def _isolated(reader: Any, *args: Any) -> Optional[Dict[str, Any]]:
    """격리 world 판정 호출 (WS-67). 판정기가 없거나 실패하면 None — 기존 판정만 쓴다(강화만)."""
    if reader is None:
        return None
    try:
        out = await reader(*args)
    except Exception:  # noqa: BLE001 — 의도된 fail-safe: 격리 판정 실패 = 지금 동작 그대로
        return None
    return out if isinstance(out, dict) else None


def _focus_verdict(kind: str, info: Dict[str, Any]) -> tuple:
    """포커스 요소 정보 → (폼 제출, 대상 이름, 판정 불가 사유, 문맥 신호) (WS-30 R1, WS-67 에서 분리)."""
    tag = str(info.get("tag") or "")
    itype = str(info.get("type") or "")
    in_form = bool(info.get("in_form"))
    name = str(info.get("name") or "")
    # WS-31: 키가 그 요소를 **누르는** 경우에만 문맥 신호를 본다(클릭과 같은 판정).
    signals = _signals_of(info)
    if kind == "enter":
        if in_form and tag == "INPUT" and itype in _ENTER_SUBMITS_INPUT_TYPES:
            # 한 줄 입력칸·체크박스 등에서 Enter = 암묵적 폼 제출(버튼형이면 그 버튼 이름도).
            return True, name if itype in _BUTTON_INPUT_TYPES else "", "", ()
        if in_form and tag == "SELECT":
            return True, "", "", ()
    if in_form and (
        (tag == "BUTTON" and itype in ("", "submit"))
        or (tag == "INPUT" and itype in ("submit", "image"))
    ):
        # 폼 안 제출 버튼을 키로 누름 = 폼 제출(이름이 '다음' 이어도).
        return True, name, "", signals
    if tag in ("BUTTON", "A", "SUMMARY") or (tag == "INPUT" and itype in _BUTTON_INPUT_TYPES):
        # 포커스된 버튼·링크에서 Enter/Space = 그 요소 클릭 — 이름·문맥 게이트를 탄다.
        return False, name, "", signals
    if info.get("unknown_tag") and info.get("inside_form"):
        # 폼 안 사용자 정의 요소(폼 연계 요소일 수 있음) — 키 동작을 확정할 수 없다.
        return False, name, f"폼 안의 알 수 없는 요소({tag.lower()})에 포커스", ()
    return False, "", "", ()


def _point_verdict(target: Dict[str, Any]) -> tuple:
    """좌표 대상 해석 결과({name, info}) → (이름, 신호, 판정 불가 사유, 근거 부가정보) (WS-31)."""
    info = target.get("info") or {}
    extra = {
        "coordinate_target": "interactive" if info.get("interactive") else "non_interactive",
        "tag": str(info.get("tag") or ""),
    }
    unresolved = ""
    if not info.get("interactive") and info.get("inside_form"):
        unresolved = "좌표가 폼 안의 비상호작용 요소 위(무엇이 일어날지 판정 불가)"
    return str(target.get("name") or ""), _signals_of(info), unresolved, extra


#: 승인 증표로 실행했는데 결과가 이 코드면 실행 여부가 불확실하다(outcome_unknown, 재시도 금지).
_UNCERTAIN_CODES = frozenset({
    ErrorCode.TIMEOUT, ErrorCode.PAGE_CRASHED, ErrorCode.NAVIGATE_TIMEOUT,
})


class HitlMixin:
    """BrowserMCPServer 의 HITL 승인 게이트·승인 증표 사용·대상 판정(WS-29·30·31·39) (WS-48 믹스인 — 상태는 BrowserMCPServer.__init__)."""

    #: WS-56 R1 B1: 증표별로 잡아 둔 대상 노드 장부 상한(서버 메모리 — 넘치면 오래된 것부터 놓는다).
    #: (클래스 속성 — WS-48 분리 모듈의 함수는 mcp_server 전역으로 돌아 모듈 전역을 새로 둘 수 없다.)
    _PIN_BOOK_CAP = 32

    def _current_origin(self) -> str:
        from urllib.parse import urlsplit

        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None) or self._page
        try:
            parts = urlsplit(getattr(page, "url", "") or "")
            return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme else ""
        except ValueError:
            return ""

    def _attach_approval_outcome(self, approval_id: str, result: ActionResult) -> None:
        """승인 증표로 실행한 결과에 outcome 을 붙인다. 불확실하면 outcome_unknown(자동 재시도 금지)."""
        unknown = (not result.success) and result.error_code in _UNCERTAIN_CODES
        outcome = "outcome_unknown" if unknown else ("succeeded" if result.success else "failed")
        self.hub.set_outcome(approval_id, outcome)
        info: Dict[str, Any] = {"approval_id": approval_id, "status": "used", "outcome": outcome}
        if unknown:
            result.retry_safe = False
            info["note"] = ("실행 결과가 불확실합니다(이미 실행됐을 수 있음). 자동으로 다시 시도하지 말고 "
                            "화면을 관찰해 확인하세요. 이 증표는 다시 쓸 수 없습니다.")
        result.data["approval"] = info

    def _approval_components(self, action: ActionType, params: Dict[str, Any],
                             basis: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "action": action.value,
            "params": params,
            "gate_basis": basis,
            "tab_id": self._core.active_tab_id if self._core else "",
            "origin": self._current_origin(),
            "snapshot_epoch": self._engine.epoch if self._engine else 0,
        }

    async def _use_approval(self, approval_id: str, action: ActionType, params: Dict[str, Any],
                            components: Dict[str, Any]) -> Any:
        """승인 증표 사용(WS-29 R1 NB-1). True = 통과(증표 소모, 이번 호출은 치유 끔),
        ActionResult = 대상이 승인 때와 달라 거부(증표는 승인 상태로 남음), dict = 증표 거부 사유.

        digest(대상 이름·문맥 근거·탭·origin·epoch 재계산)가 같아야 하고, element_id 대상이 관찰 때 그
        요소 그대로(연결·role·이름)여야 한다 — 같은 이름의 다른 버튼으로 바뀌었으면 누르지 않는다.
        """
        ok, why, reason_code = self.hub.check_approval(approval_id, components)
        if not ok:
            return {"approval_id": approval_id, "reason": why, "reason_code": reason_code}
        # WS-56 R2 B4 → R4: 재확인~실제 입력~효과 판정은 _call_tool 이 쥔 작업 공간 직렬화 구간 안(다른 호출과 겹치지 않음).
        check = getattr(self._dispatcher, "approval_target_check", None)
        if check is None:
            target = {"fresh": False, "detail": "대상 확인 불가"}  # fail-closed
        else:
            try:
                target = await check(params)
            except Exception as exc:  # noqa: BLE001
                target = {"fresh": False, "detail": f"대상 확인 실패: {type(exc).__name__}"}
        if target.get("fresh"):
            # WS-56 R1 B1: 이름·role·css 가 같아도 증표 발급(승인 요청) 때 잡은 그 노드여야 한다.
            target = await self._pinned_check(self._approval_pins().get(approval_id), params)
        if not target.get("fresh"):
            refused = self._error_result(
                action,
                ErrorCode.TOCTOU_MISMATCH,
                "승인한 대상이 바뀌어 실행하지 않았습니다(" + str(target.get("detail") or "") + "). "
                "다시 관찰하세요 — 같은 대상이면 이 승인으로 다시 호출할 수 있고, 다르면 새 승인이 필요합니다.",
                data={"approval": {"approval_id": approval_id, "status": "approved",
                                   "reason": "target_changed",
                                   "detail": str(target.get("detail") or "")}},
            )
            if target.get("group_changed") is True:
                refused.reobserve_required = True  # WS-62 NB1: 묶음(달·행)이 바뀌었다 — 재관찰해야 고를 수 있다
            return refused
        ok, why = self.hub.consume_approval(approval_id, components)
        if not ok:  # 그사이 다른 호출이 썼다
            return {"approval_id": approval_id, "reason": why, "reason_code": "used"}
        self._used_approval = approval_id
        # 증표는 한 번 쓴다 — 잡은 노드는 이번 호출의 실행 대상으로 넘기고(디스패처가 그 노드에만) 장부에서 뺀다.
        self._approval_pin = self._approval_pins().pop(approval_id, None)
        return True

    def _approval_pins(self) -> Dict[str, Any]:
        """승인 증표 id → 발급(승인 요청) 때 잡은 대상 노드(WS-56 R1 B1). 서버 메모리, 지연 생성."""
        return self.__dict__.setdefault("_approval_pin_book", {})

    async def _pin_for_token(self, approval_id: str, params: Dict[str, Any]) -> None:
        """증표 발급 때 대상 노드를 잡아 둔다. 못 잡으면 장부에 없음 → 사용 때 대상 바뀜으로 거부(fail-closed)."""
        from interface.server_elicit import _release

        pinned = await self._pin_approval_target(params)
        if not pinned:
            return
        book = self._approval_pins()
        book[approval_id] = pinned
        while len(book) > self._PIN_BOOK_CAP:  # 오래된 것부터 놓는다(증표 상한과 같은 취지)
            oldest = next(iter(book))
            await _release(self._dispatcher, book.pop(oldest))

    async def _check_hitl(
        self, action: ActionType, params: Dict[str, Any], approval_id: Any = None
    ) -> Optional[ActionResult]:
        """고위험 액션 승인 게이트. 차단 시 ActionResult를 반환한다."""
        target = await self._gate_target(action, params)
        if isinstance(target, ActionResult):
            return target
        element_name, selector, submits_form, unresolved, signals, basis_extra, report_basis = target

        gate_ctx = self._gate_context(action, target)
        decision = self._hitl.evaluate(gate_ctx)
        if decision.allowed:
            if report_basis:
                self._pending_gate_basis = dict(decision.basis)
            return None

        # WS-56: approval_id 없는 호출은 먼저 대역 밖 승인 — 사람 CLI 정책, 그다음 클라이언트 elicitation(사람 버튼).
        # 둘 다 아니면(거절·시간 초과·미지원) 아래 기존 경로 그대로.
        self._oob_note = None
        if not approval_id:
            oob = await self._out_of_band(action, params, decision, gate_ctx, report_basis)
            if oob is True:
                return None
            if isinstance(oob, ActionResult):
                return oob

        # WS-29: 승인 증표 — 사람이 대역 밖(`agent-browser approve` + 창의 확인 코드)에서 승인한
        # 바로 그 행동만 통과. R1: 사람이 볼 창이 없으면(headless) 증표를 발급하지 않는다 —
        # 확인 코드를 띄울 곳이 없어 사람만 승인할 수 있다는 보장이 없다.
        ap: Any = None
        approval: Optional[Dict[str, Any]] = None
        if self._human_can_see():
            issued = await self._approval_or_pass(action, params, approval_id, decision, report_basis,
                                                  element_name, selector)
            if issued is None or isinstance(issued, ActionResult):
                return issued
            ap, approval, rejected = issued
        else:
            rejected = None
        return self._attach_oob_note(
            self._hitl_blocked_result(action, decision, element_name, ap, approval, rejected))

    def _gate_context(self, action: ActionType, target: tuple) -> Any:
        """_gate_target 해석 결과 → 게이트 판정 입력(ActionContext)."""
        from security import ActionContext

        element_name, selector, submits_form, unresolved, signals, basis_extra, _report = target
        return ActionContext(
            action=action,
            element_name=element_name,
            selector=selector,
            domain=self._current_domain(),
            submits_form=submits_form,
            unresolved_target=unresolved,
            signals=signals,
            basis_extra=basis_extra,
        )

    async def _gate_target(self, action: ActionType, params: Dict[str, Any]) -> Any:
        """게이트가 판정할 대상 해석. 반환: (이름, selector, 폼 제출, 판정 불가 사유, 문맥 신호,
        근거 부가정보, 통과해도 근거를 싣는가) 튜플 또는 모호한 selector 거부 ActionResult(WS-39)."""
        element_name = ""
        element_id = params.get("element_id")
        if element_id and self._engine is not None:
            handle = self._engine.get_handle(element_id)
            if handle is not None:
                element_name = handle.name

        submits_form = bool(params.get("press_enter"))
        unresolved = ""
        selector = params.get("selector", "") or ""
        dispatcher = self._dispatcher
        #: WS-31: 이름 밖 문맥 신호(보이는 텍스트·aria·alt·목적지·식별자·의사요소)와 근거 부가정보.
        signals: tuple = ()
        basis_extra: Dict[str, Any] = {}
        #: 통과해도 결과에 gate_basis 를 싣는가(좌표 클릭 — 어떤 요소로 판정했는지 알린다).
        report_basis = False
        if action is ActionType.CLICK and element_id:
            if dispatcher is None:
                unresolved = "대상을 읽을 수 없음"
            else:
                target = await dispatcher.describe_element_for_gate(element_id)
                unresolved = str(target.get("unresolved") or "")
                signals = _signals_of(target.get("info"))
        elif action is ActionType.CLICK and selector:
            # WS-30 추가 A: selector 문자열이 아니라 페이지에서 읽은 대상 이름으로 판정한다.
            # 0개·여러 개·읽기 실패면 판정 불가 → 고위험(fail-closed).
            if dispatcher is None:
                unresolved = "selector 대상을 읽을 수 없음"
            else:
                target = await dispatcher.describe_selector_target(selector)
                element_name = str(target.get("name") or "")
                unresolved = str(target.get("unresolved") or "")
                signals = _signals_of(target.get("info"))
                ambiguous = self._ambiguous_low_risk(action, selector, target.get("ambiguous"))
                if ambiguous is not None:
                    return ambiguous
        elif action is ActionType.CLICK and params.get("x") is not None:
            # WS-31: 좌표 클릭도 그 좌표가 누를 요소(상호작용 조상)의 이름·문맥으로 판정한다.
            element_name, signals, unresolved, basis_extra = await self._point_target(params)
            report_basis = True
        elif action is ActionType.TYPE_TEXT and not submits_form:
            submits_form, unresolved = await self._newline_submit_target(
                params, element_id, submits_form, unresolved)
        elif action is ActionType.PRESS_KEY:
            submits_form, name, unresolved, signals = await self._press_key_target(params)
            element_name = name or element_name
        return element_name, selector, submits_form, unresolved, signals, basis_extra, report_basis

    async def _newline_submit_target(self, params: Dict[str, Any], element_id: Any, submits_form: bool,
                                     unresolved: str) -> tuple:
        """type_text 의 줄바꿈 입력이 폼 제출인지 대상 요소로 판정한다. 반환: (폼 제출, 판정 불가 사유)."""
        dispatcher = self._dispatcher
        text = str(params.get("text") or "")
        if "\n" in text or "\r" in text:
            # 줄바꿈 입력은 한 줄 입력칸에서 Enter 와 같다(Playwright type 실측: 폼 제출).
            info = await dispatcher.describe_element(element_id) if dispatcher else None
            if info is None:
                unresolved = "입력 대상을 읽을 수 없음"
            elif info.get("tag") == "INPUT" and info.get("in_form"):
                submits_form = True
        return submits_form, unresolved

    async def _approval_or_pass(self, action: ActionType, params: Dict[str, Any], approval_id: Any,
                                decision: Any, report_basis: bool, element_name: str,
                                selector: str) -> Any:
        """승인 증표 사용 또는 새 증표 발급(사람이 볼 창이 있을 때만).

        반환: None = 증표로 통과, ActionResult = 대상이 승인 때와 달라 거부,
        (발급한 증표, data.approval, 거부된 증표 사유 또는 None) = 차단 결과에 싣는다.
        """
        components = self._approval_components(action, params, dict(decision.basis))
        rejected: Optional[Dict[str, Any]] = None
        if approval_id:
            passed = await self._use_approval(str(approval_id), action, params, components)
            if passed is True:
                if report_basis:
                    self._pending_gate_basis = dict(decision.basis)
                return None
            if isinstance(passed, ActionResult):
                return passed
            rejected = passed
        ap = self.hub.issue_approval(components, {
            "target": element_name or selector,
            "reason": decision.reason,
            "domain": self._current_domain(),
        })
        await self._pin_for_token(ap.approval_id, params)
        # NB-7: action_digest 는 싣지 않는다 — 사람 CLI 가 상태 파일에서 읽는다.
        approval = {
            "approval_id": ap.approval_id,
            "expires_at": _iso(ap.expires),
            "how_to_approve": (f"사람이 터미널에서: agent-browser approve {ap.approval_id} "
                               "(브라우저 창에 뜨는 확인 코드 입력)"),
        }
        if rejected is not None:
            approval["rejected"] = rejected
            if rejected.get("reason_code") == "target_changed":
                approval["reason"] = "target_changed"
        return ap, approval, rejected

    def _hitl_blocked_result(self, action: ActionType, decision: Any, element_name: str, ap: Any,
                             approval: Optional[Dict[str, Any]],
                             rejected: Optional[Dict[str, Any]]) -> ActionResult:
        """게이트 차단 결과(메시지·gate_basis·승인 증표·사람용 해결 경로)."""
        message = decision.reason
        if decision.requires_confirmation and decision.dialog is not None:
            # 대화형 모드: 클라이언트가 렌더링할 정형 모달을 함께 전달한다.
            message = decision.dialog.message
        if rejected is not None:
            message = f"승인 증표 거부: {rejected['reason']}. " + message

        data: Dict[str, Any] = {
            "requires_confirmation": decision.requires_confirmation,
            "risk": decision.risk.value,
            "dialog": (
                decision.dialog.model_dump(mode="json") if decision.dialog else None
            ),
            # WS-31: 운영자가 왜 막혔는지 — {name, matched_keyword, source, …}.
            "gate_basis": dict(decision.basis),
        }
        if approval is not None:
            data["approval"] = approval
        approval_id_hint = ap.approval_id if ap is not None else ""
        code = decision.error_code or ErrorCode.HITL_UNATTENDED_BLOCKED
        if code is ErrorCode.HITL_UNATTENDED_BLOCKED:
            # WS-30 항목 5: 에이전트가 "어떻게 승인하나요?"로 멈추지 않게, 사람(운영자)이
            # 할 수 있는 해결 경로만 알린다 — 다른 도구로 돌아가는 방법은 알리지 않는다.
            hint = pre_approve_hint(action, element_name)
            message += blocked_hint_text(hint, approval_id_hint)
            data["pre_approve_hint"] = hint
        elif approval_id_hint:
            message += approve_hint_text(approval_id_hint)

        return self._error_result(action, code, message, data=data)

    def _ambiguous_low_risk(self, action: ActionType, selector: str,
                            ambiguous: Any) -> Optional[ActionResult]:
        """selector 가 여러 요소에 맞을 때 (WS-39): 후보가 **모두** 저위험이면 승인 대신 모호함 오류.

        모호함은 에이전트가 스스로 풀 수 있다(관찰 → element_id) — 사람 승인으로 보내면 승인 명령을
        칠 수 없는 사용자(텔레그램 등)에서 진행이 막힌다(실사용 2026-10-10, '항공편 더보기').
        각 후보를 기존 판정(`assess_risk`: 이름·selector·문맥 신호)으로 보고 하나라도 HIGH 면 None —
        기존 판정 불가 승인 경로(fail-closed)를 그대로 탄다. 어느 쪽이든 누르지 않는다.

        E_ELEMENT_NOT_FOUND 인 까닭: '정확히 하나'인 대상을 찾지 못한 것이다(디스패처의 selector
        0개·여러 개 응답, element_id 누락과 같은 코드). NOT_INTERACTABLE 은 찾은 요소가 비활성·가림 등
        으로 못 누르는 경우라 뜻이 다르다.
        """
        if not isinstance(ambiguous, dict):
            return None
        infos = ambiguous.get("infos")
        count = int(ambiguous.get("count") or 0)
        if not isinstance(infos, list) or count < 2 or len(infos) != count:
            return None
        from actions.dispatcher import ambiguous_message, ambiguous_target_view
        from security.hitl import assess_risk
        from security import ActionContext, RiskLevel

        for info in infos:
            if not isinstance(info, dict):
                return None
            assessed = assess_risk(ActionContext(
                action=action,
                element_name=str(info.get("name") or ""),
                selector=selector,
                domain=self._current_domain(),
                signals=_signals_of(info),
            ))
            if assessed.risk is RiskLevel.HIGH:
                return None
        result = self._error_result(
            action,
            ErrorCode.ELEMENT_NOT_FOUND,
            ambiguous_message(count),
            data={"ambiguous_target": ambiguous_target_view(count, infos)},
        )
        return result.model_copy(update={"reobserve_required": True, "retry_safe": True})

    async def _press_key_target(self, params: Dict[str, Any]) -> tuple:  # noqa: C901
        """press_key 가 폼 제출·버튼 활성화인지 실제 포커스 요소로 판정한다 (WS-30 항목 6, R1).

        type_text(press_enter=True) 는 '폼 제출'로 막히는데 type_text 뒤 press_key("Enter") 는
        같은 제출이 통과하던 불일치를 막는다. 반환: (submits_form, 대상 이름, 판정 불가 사유).

        무엇이 제출하는지는 Chromium 실측 표(`_ENTER_SUBMITS_INPUT_TYPES` 등)를 따른다. 포커스는
        최상위 Page 기준(키가 가는 곳)으로 읽는다 — `ActionDispatcher.focused_target`.
        """
        from actions.dispatcher import key_kind

        kind = key_kind(str(params.get("key", "")))
        if not kind:
            return False, "", "", ()
        dispatcher = self._dispatcher
        info = await dispatcher.focused_target() if dispatcher is not None else None
        if info is None:
            # Enter·Space 모두 요소를 누를 수 있다 — 대상을 모르면 판정 불가(fail-closed).
            return False, "", "포커스 요소를 읽을 수 없음", ()
        if info.get("opaque"):
            return False, "", "포커스가 다른 출처 프레임 안에 있음", ()
        verdict = _focus_verdict(kind, info)
        # WS-67 (#141): 같은 포커스를 격리 world 에서도 읽어(페이지가 form getter 등을 지워도 원본)
        # 각각 위험 분류한 뒤 높은 쪽을 쓴다. 격리 판정이 없으면(실패·OOPIF) 기존 판정 그대로.
        reader = getattr(dispatcher, "isolated_focus_target", None)
        isolated = await _isolated(reader)
        if isinstance(isolated, dict):
            other = _focus_verdict(kind, isolated)
            domain = self._current_domain()
            if (_gate_rank(ActionType.PRESS_KEY, domain, other[1], other[0], other[2], other[3])
                    > _gate_rank(ActionType.PRESS_KEY, domain, verdict[1], verdict[0], verdict[2],
                                 verdict[3])):
                return other
        return verdict

    async def _point_target(self, params: Dict[str, Any]) -> tuple:
        """좌표 클릭의 대상 해석 (WS-31). 반환: (이름, 신호, 판정 불가 사유, 근거 부가정보).

        디스패처가 어차피 거부할 좌표(epoch 불일치·뷰포트 밖)는 해석하지 않는다 — 클릭이 일어나지
        않으므로 그 오류(TOCTOU/ELEMENT_NOT_FOUND)를 그대로 받게 한다.

        정책(Tier-2 SoM 의 본래 용도 — 캔버스·이름 없는 그림): 상호작용 조상이 없고, 폼 안도 아니고,
        문맥 신호에 위험 단어도 없으면 저위험으로 통과시키고 근거(coordinate_target=non_interactive)
        를 남긴다. 폼 안의 비상호작용 요소는 무엇이 일어날지 몰라 판정 불가(fail-closed).
        """
        dispatcher = self._dispatcher
        if dispatcher is None:
            return "", (), "좌표 대상을 읽을 수 없음", {}
        if not dispatcher.coordinates_dispatchable(params):
            return "", (), "", {"coordinate_target": "rejected_by_dispatcher"}
        x, y = int(params["x"]), int(params["y"])
        target = await dispatcher.describe_point(x, y)
        if target.get("unresolved"):
            return "", (), str(target["unresolved"]), {"coordinate_target": "unresolved"}
        verdict = _point_verdict(target)
        # WS-67 (#141): 같은 좌표를 격리 world 에서도 판정해(DOM.getNodeForLocation → 원본 prototype)
        # 각각 위험 분류한 뒤 높은 쪽을 쓴다. 격리 판정이 없으면(IFRAME 위·실패) 기존 판정 그대로.
        reader = getattr(dispatcher, "isolated_point_target", None)
        isolated = await _isolated(reader, x, y)
        if isinstance(isolated, dict):
            other = _point_verdict(isolated)
            domain = self._current_domain()
            if (_gate_rank(ActionType.CLICK, domain, other[0], False, other[2], other[1])
                    > _gate_rank(ActionType.CLICK, domain, verdict[0], False, verdict[2], verdict[1])):
                return other
        return verdict

    def _current_domain(self) -> str:
        try:
            from urllib.parse import urlparse

            return urlparse(self._page.url).hostname or ""
        except Exception:  # noqa: BLE001
            # 의도된 fail-safe(WS-48 점검): URL 을 못 읽으면 빈 도메인으로 본다.
            return ""
