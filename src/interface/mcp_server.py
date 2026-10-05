"""MCP 서버 (PRD §6.3, Gate 3-B 항목 3).

19종 액션 툴을 RFC 표준 MCP(JSON-RPC 2.0)로 외부 에이전트에 노출한다.

설계 원칙:
* **툴 목록을 손으로 쓰지 않는다.** `ACTION_INPUT_MAP`에서 자동 생성하므로
  계약에 액션이 추가되면 툴도 자동으로 늘어난다. 수동 목록은 계약과
  어긋나도 아무도 모르게 되므로 금지한다.
* **입력 스키마도 Pydantic에서 생성한다.** 각 액션의 Input 모델이
  `model_json_schema()`로 JSON Schema를 제공한다.
* **에러는 예외가 아니라 ActionResult로 반환한다.** MCP 클라이언트가
  구조화된 실패 정보를 받아야 재시도 판단이 가능하다.

세션 수명주기:
MCP 서버는 stdio로 구동되며 클라이언트 연결당 하나의 브라우저 세션을
유지한다. 첫 툴 호출 시 지연 초기화하고, 서버 종료 시 정리한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import unicodedata
from typing import Any, Dict, List, Optional

from contracts import (
    ACTION_INPUT_MAP,
    ActionResult,
    ActionType,
    ErrorCode,
    ExecutionMode,
    thresholds,
)

logger = logging.getLogger(__name__)

#: 툴 이름 접두사. MCP 클라이언트에서 다른 서버와 충돌하지 않도록 한다.
TOOL_PREFIX = "browser_"


def tool_name(action: ActionType) -> str:
    """ActionType -> MCP 툴 이름."""
    return f"{TOOL_PREFIX}{action.value}"


def action_from_tool(name: str) -> Optional[ActionType]:
    """MCP 툴 이름 -> ActionType (역변환)."""
    if not name.startswith(TOOL_PREFIX):
        return None
    raw = name[len(TOOL_PREFIX) :]
    try:
        return ActionType(raw)
    except ValueError:
        return None


def build_tool_schema(action: ActionType) -> Dict[str, Any]:
    """단일 액션의 MCP 툴 정의를 계약에서 생성한다.

    입력 스키마는 계약 모델의 `model_json_schema()` 를 서버 경계에서 `compact_schema` 로
    정리한 것이다(WS-30b — 같은 입력을 받아들이고 같은 입력을 거부한다, 계약 무수정).
    """
    model = ACTION_INPUT_MAP.get(action)
    if model is None:
        # 입력이 없는 액션도 빈 스키마로 노출한다.
        schema: Dict[str, Any] = {"type": "object", "properties": {}}
    else:
        schema = compact_schema(model.model_json_schema())

    return {
        "name": tool_name(action),
        "description": _describe(action),
        "inputSchema": schema,
    }


#: 스키마 정리에서 값을 그대로 두는 키워드(하위 스키마가 아니라 이름·값 목록이다).
_SCHEMA_VALUE_KEYS = frozenset({"enum", "const", "required", "examples", "default"})


def compact_schema(schema: Any) -> Any:
    """pydantic 기본 JSON Schema 에서 검증 의미가 없는 군더더기를 뺀다 (WS-30b).

    * `title` — 주석 키워드(검증에 쓰이지 않음). 속성 이름이 title 이어도 속성은 남는다.
    * `anyOf: [{type: T, ...}, {type: null}]` → `{type: [T, "null"], ...}` — T 쪽 제약
      (minimum 등)은 null 에 적용되지 않으므로(타입별 키워드) 두 형태가 받는 값이 같다.
      두 갈래가 이 모양이 아니면($ref·여러 타입 등) 그대로 둔다.
    * `default: null` — 주석 키워드. 다른 기본값("left", 20 …)은 에이전트가 볼 정보라 남긴다.
    """
    if isinstance(schema, list):
        return [compact_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key == "default" and value is None:
            continue
        if key in ("properties", "$defs", "patternProperties") and isinstance(value, dict):
            out[key] = {name: compact_schema(sub) for name, sub in value.items()}
        elif key in _SCHEMA_VALUE_KEYS:
            out[key] = value
        else:
            out[key] = compact_schema(value)
    branches = out.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2 and {"type": "null"} in branches:
        other = branches[0] if branches[1] == {"type": "null"} else branches[1]
        if (
            isinstance(other, dict)
            and isinstance(other.get("type"), str)
            and other["type"] != "null"
            and "$ref" not in other
            and not (set(other) & set(out) - {"anyOf"})
        ):
            merged = {k: v for k, v in out.items() if k != "anyOf"}
            merged.update(other)
            merged["type"] = [other["type"], "null"]
            if "enum" in merged:  # enum 은 타입과 별개로 값을 제한한다 — null 도 넣어야 같다
                merged["enum"] = list(merged["enum"]) + [None]
            return merged
    return out


def build_all_tools() -> List[Dict[str, Any]]:
    """19종 툴 정의 전체를 생성한다.

    `ActionType`을 순회하므로 계약에 액션이 추가되면 자동 반영된다.
    """
    return [build_tool_schema(action) for action in ActionType]


#: 계약 밖 서버 도구 (WS-29 사람 인계). 액션 툴(19종)과 **별도 목록** — 계약·ActionType 과 무관하다.
#: tools/list = build_all_tools() + build_server_tools(). 설명은 짧게(WS-30b 토큰 예산).
_WAIT_SCHEMA = {"type": "number", "maximum": 120}
SERVER_TOOLS: Dict[str, Dict[str, Any]] = {
    f"{TOOL_PREFIX}control_request": {
        "description": (
            "사람에게 조작권 요청(캡차·로그인). 창 있는 serve 만. secret_wanted=true 면 관찰도 막힘. "
            "다음: control_wait"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}, "secret_wanted": {"type": "boolean"}},
            "required": ["reason"],
        },
    },
    f"{TOOL_PREFIX}control_status": {
        "description": "조작권 상태(holder agent|human)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    f"{TOOL_PREFIX}control_wait": {
        "description": "사람의 take/release 까지 대기(초, 기본 60)",
        "inputSchema": {"type": "object", "properties": {"timeout_s": _WAIT_SCHEMA}},
    },
    f"{TOOL_PREFIX}approval_wait": {
        "description": "data.approval 을 사람이 승인/거절할 때까지 대기",
        "inputSchema": {
            "type": "object",
            "properties": {"approval_id": {"type": "string"}, "timeout_s": _WAIT_SCHEMA},
            "required": ["approval_id"],
        },
    },
}


def build_server_tools() -> List[Dict[str, Any]]:
    """계약 밖 서버 도구 정의(WS-29). 액션 툴 목록(build_all_tools)과 합쳐 tools/list 가 된다."""
    return [{"name": name, **spec} for name, spec in SERVER_TOOLS.items()]


def build_listed_tools() -> List[Dict[str, Any]]:
    """MCP tools/list 전체 = 액션 툴 19종 + 서버 도구."""
    return build_all_tools() + build_server_tools()


_DESCRIPTIONS: Dict[ActionType, str] = {
    ActionType.OBSERVE_PAGE: (
        "현재 페이지를 관찰해 상호작용 가능한 요소 목록을 반환합니다. "
        "각 요소에는 이후 액션에서 사용할 element_id가 부여됩니다. "
        "모든 툴 응답에서 빠진 필드=계약 기본값(null/false/{}). "
        "이 툴·extract 결과는 2만 자 초과 시 앞쪽만(data.truncated)."
    ),
    ActionType.TAKE_SCREENSHOT: "현재 페이지의 스크린샷을 캡처합니다.",
    ActionType.NAVIGATE: "지정한 URL로 이동합니다. snapshot_epoch가 증가합니다.",
    ActionType.GO_BACK: "브라우저 히스토리에서 이전 페이지로 이동합니다.",
    ActionType.RELOAD: "현재 페이지를 새로고침합니다.",
    ActionType.CLICK: "지정한 요소를 클릭합니다. 실행 전 요소 유효성을 검증합니다.",
    ActionType.TYPE_TEXT: "입력 필드에 텍스트를 입력합니다.",
    ActionType.SELECT_OPTION: "드롭다운에서 옵션을 선택합니다.",
    ActionType.CHECK_BOX: "체크박스나 라디오 버튼을 토글합니다.",
    ActionType.SCROLL: "뷰포트를 스크롤합니다. 동적 로딩 시 재관찰이 필요할 수 있습니다.",
    ActionType.HOVER: "요소 위에 마우스를 올려 툴팁이나 서브메뉴를 노출시킵니다.",
    ActionType.PRESS_KEY: "키보드 이벤트를 발생시킵니다 (Enter, Tab, Escape 등).",
    ActionType.WAIT_FOR: "지정한 조건이 만족될 때까지 대기합니다.",
    ActionType.EXTRACT: "CSS 셀렉터로 텍스트와 속성을 추출합니다.",
    ActionType.SWITCH_FRAME: (
        "iframe 또는 Shadow DOM 컨텍스트로 전환합니다. frame_selector 는 현재 프레임 기준으로 "
        "먼저, 없으면 최상위 문서 기준으로 찾습니다(이때 더 얕은 프레임으로 돌아갈 수 있음 — "
        "data.resolved_from(current_frame|root)·frame_depth 로 확인). 중첩 프레임은 바깥부터 "
        "한 단계씩(data.child_frames 의 selector_hint 가 다음 후보). "
        "최상위 문서 복귀는 {\"to_main\": true} 만 보내십시오."
    ),
    ActionType.HANDLE_DIALOG: "Alert/Confirm/Prompt 다이얼로그를 처리합니다.",
    ActionType.UPLOAD_FILE: "파일 입력 필드에 파일을 바인딩합니다.",
    ActionType.DOWNLOAD_FILE: (
        "다운로드를 트리거하고 파일을 저장합니다. save_dir 는 절대 경로(상대 경로 거부, `..` 정규화)."
    ),
    ActionType.TAB_CONTROL: "탭을 생성/전환/종료하거나 목록을 조회합니다.",
}


#: 결과 data 에 차단·캡차 신호(challenge, last_http_status)를 싣는 액션 (WS-26).
#: 화면이 바뀌거나 화면을 보는 액션만 — 나머지(스크린샷·추출·스크롤·호버 등)는
#: 판정 비용·소음을 줄이려고 싣지 않는다.
CHALLENGE_CHECK_ACTIONS = frozenset({
    ActionType.NAVIGATE,
    ActionType.GO_BACK,
    ActionType.RELOAD,
    ActionType.CLICK,
    ActionType.PRESS_KEY,
    ActionType.TYPE_TEXT,
    ActionType.SELECT_OPTION,
    ActionType.CHECK_BOX,
    ActionType.OBSERVE_PAGE,
    ActionType.TAB_CONTROL,
    ActionType.WAIT_FOR,
})

#: 대상 툴 설명 뒤에 붙는 공통 안내.
CHALLENGE_NOTE = (
    " 결과 data.challenge 가 null 이 아니면 캡차/차단 화면입니다"
    "(kind=captcha|blocked, vendor, reason; data.last_http_status 는 이 탭의 마지막 메인 문서 HTTP 상태)"
    " — 풀려고 하지 말고 사람에게 넘길지 판단하십시오."
)


#: 차단·캡차 안내 전문(CHALLENGE_NOTE)을 싣는 **한 곳** (WS-30b). 대부분의 세션이 처음 부르는 툴이다.
CHALLENGE_NOTE_HOME = ActionType.NAVIGATE

#: 대상 툴 중 안내 전문을 싣지 않는 툴에 붙는 짧은 참조 — 의도(data.challenge 를 보고, 풀지 말고
#: 사람에게)는 참조만으로도 읽히게 한다.
CHALLENGE_REF = (
    " data.challenge: null 아니면 캡차/차단 → 사람에게"
    f"({TOOL_PREFIX}{CHALLENGE_NOTE_HOME.value} 참조)."
)


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


def _describe(action: ActionType) -> str:
    text = _DESCRIPTIONS.get(action, f"{action.value} 액션을 실행합니다.")
    if action is CHALLENGE_NOTE_HOME:
        text += CHALLENGE_NOTE
    elif action in CHALLENGE_CHECK_ACTIONS:
        text += CHALLENGE_REF
    return text


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


#: 응답 봉투 규칙 (WS-30b): 계약 기본값과 같은 필드는 뺀다 — **빠진 필드 = 계약 기본값**.
#: 계약 필수 필드(기본값 없음)와 아래 필드는 기본값이어도 항상 싣는다. reobserve_required 는
#: 에이전트가 매 응답에서 보고 element_id 재사용 여부를 정하는 칸이라 false 도 명시한다.
ENVELOPE_ALWAYS = frozenset({"reobserve_required"})
#: 봉투·README 에 쓰는 규칙 한 줄.
ENVELOPE_RULE = (
    "응답에서 빠진 필드는 계약 기본값입니다(healed=false, downloaded_path·popup_tab_id·"
    "error_code·error_message=null, data={}, 관찰 요소의 value=null·is_shadow=false). "
    "data 안의 null 은 그대로 싣습니다(data.challenge: null = 차단 없음)."
)


def _field_default(field: Any) -> Any:
    default = field.get_default(call_default_factory=True)
    return default.value if hasattr(default, "value") and not isinstance(default, dict) else default


def _compact_observation(obs: Any) -> Any:
    """관찰 요소에서 ObservedElement 기본값(value=None, is_shadow=False)과 같은 키를 뺀다.

    계약 모델로 읽히지 않는 모양이면 건드리지 않는다(다시 파싱해 같은 객체가 되는 것만 줄인다).
    """
    from contracts import ObserveResult, ObservedElement

    if not isinstance(obs, dict) or not isinstance(obs.get("elements"), list):
        return obs
    try:
        ObserveResult.model_validate(obs)
    except Exception:  # noqa: BLE001 - 모양이 다르면 원본 그대로
        return obs
    defaults = {
        name: _field_default(f)
        for name, f in ObservedElement.model_fields.items()
        if not f.is_required()
    }
    elements = []
    for el in obs["elements"]:
        elements.append({
            k: v for k, v in el.items()
            if not (k in defaults and v == defaults[k] and type(v) is type(defaults[k]))
        })
    return dict(obs, elements=elements)


def envelope_dict(result: ActionResult) -> Dict[str, Any]:
    """MCP 응답 봉투(dict). `ActionResult.model_validate(봉투)` 는 원래 결과와 같다."""
    full = result.model_dump(mode="json")
    out: Dict[str, Any] = {}
    for name, field in ActionResult.model_fields.items():
        value = full[name]
        if not field.is_required() and name not in ENVELOPE_ALWAYS:
            default = _field_default(field)
            if value == default and type(value) is type(default):
                continue
        out[name] = value
    data = out.get("data")
    if isinstance(data, dict) and "observation" in data:
        out["data"] = dict(data, observation=_compact_observation(data["observation"]))
    return out


def envelope_json(result: ActionResult) -> str:
    """MCP 응답 텍스트(JSON 한 덩어리, 공백 없음)."""
    import json

    return json.dumps(envelope_dict(result), ensure_ascii=False, separators=(",", ":"))


from interface.handoff import DEFAULT_APPROVAL_TTL_S, WAIT_DEFAULT_S, WAIT_MAX_S  # noqa: E402

#: WS-29 R2: 확인 코드를 띄우기 전, 이미 진행 중인 화면 캡처가 끝나길 기다리는 상한(초). 넘으면
#: 표시를 취소한다(코드 무효 — fail-closed). 무거운 페이지 SoM 캡처 실측 수백 ms 의 10배 이상.
CAPTURE_DRAIN_TIMEOUT_S = 5.0

#: 승인 증표로 실행했는데 결과가 이 코드면 실행 여부가 불확실하다(outcome_unknown, 재시도 금지).
_UNCERTAIN_CODES = frozenset({
    ErrorCode.TIMEOUT, ErrorCode.PAGE_CRASHED, ErrorCode.NAVIGATE_TIMEOUT,
})
#: 조작권 반납 뒤 안내.
_RELEASE_HINT = "사람이 화면을 바꿨을 수 있습니다 — 이전 element_id 는 무효. browser_observe_page 로 다시 관찰하세요."


def _wait_timeout(raw: Any) -> float:
    try:
        value = float(raw) if raw is not None else WAIT_DEFAULT_S
    except (TypeError, ValueError):
        value = WAIT_DEFAULT_S
    return max(0.0, min(WAIT_MAX_S, value))


def _iso(ts: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds")


#: 응답 봉투 크기 상한(글자) 기본값 (WS-30b). 근거: Claude Code 는 MCP 도구 결과가 기본
#: 25,000 토큰(MAX_MCP_OUTPUT_TOKENS)을 넘으면 결과를 버리고 오류를 낸다(비교 시험: observe
#: force_full_tree 143,456자·extract 58,620자). 한글 본문은 cl100k 로 1자 ≈ 1토큰(실측 0.97)
#: 이라 글자 수로 재면 20,000자 ≈ 20,000토큰 이하 — 25,000 토큰 한도에 여유를 둔다.
#: `serve --max-result-chars` 로 바꿀 수 있다.
DEFAULT_MAX_RESULT_CHARS = 20_000
#: 이보다 작은 상한은 받지 않는다(봉투 고정부 + truncated 안내만으로도 수백 자).
MIN_MAX_RESULT_CHARS = 2_000

_TRUNC_HINT_OBSERVE = (
    "결과가 {limit}자 상한을 넘어 점수 순 앞쪽 요소만 담았습니다. 더 보려면 force_full_tree 없이 "
    "prune_top_n 을 줄여 부르거나, extract 의 selector 를 좁혀 필요한 부분만 읽으세요."
)
_TRUNC_HINT_EXTRACT = (
    "결과가 {limit}자 상한을 넘어 앞쪽 항목만 담았습니다. 더 보려면 selector 를 좁히세요"
    "(예: 목록 안 특정 구역, :nth-child(-n+50))."
)
_TRUNC_HINT_TEXT = (
    "항목 하나의 텍스트가 {limit}자 상한을 넘어 앞부분만 담았습니다(text_truncated). "
    "selector 를 좁히거나 extract_all 없이 필요한 부분만 읽으세요."
)


def _safe_cut(text: str, n: int) -> str:
    """text 를 n 글자 이하로 자르되 글자 묶음 중간(결합 문자·ZWJ·변형 선택자·서로게이트)에서
    끊지 않고, 가능하면 마지막 200자 안의 공백/줄바꿈에서 끊는다."""
    if len(text) <= n:
        return text
    n = max(0, n)

    def _joins(i: int) -> bool:
        # i 위치에서 끊으면 text[i] 가 앞 글자에 붙는 문자인가, 또는 text[i-1] 이 ZWJ 인가
        if i <= 0 or i >= len(text):
            return False
        ch, prev = text[i], text[i - 1]
        if unicodedata.combining(ch) or ch in "\u200d\ufe0e\ufe0f" or prev == "\u200d":
            return True
        if 0x1F3FB <= ord(ch) <= 0x1F3FF:  # 피부색 수식자
            return True
        return 0xDC00 <= ord(ch) <= 0xDFFF  # 서로게이트 짝(파이썬 str 에선 드묾)

    i = n
    while i > 0 and _joins(i):
        i -= 1
    window = text[max(0, i - 200):i]
    for sep in ("\n", " "):
        k = window.rfind(sep)
        if k > 0:
            j = max(0, i - 200) + k + 1
            if not _joins(j):
                return text[:j]
    return text[:i]


def cap_result_size(result: ActionResult, max_chars: int) -> None:
    """observe_page·extract 결과 봉투를 max_chars 이하로 줄인다 (WS-30b, 제자리 수정).

    * 자르는 단위는 항목 경계(관찰 요소 — 점수 순, 추출 행 — 문서 순). 앞쪽을 남긴다.
    * 첫 항목 하나만으로도 넘으면 그 항목의 텍스트만 글자 묶음 경계에서 자른다
      (`text_truncated: true`, `text_chars` = 원래 글자 수).
    * 잘랐으면 `data.truncated = {total_items, returned_items, total_chars, returned_chars, hint}`
      (+ 텍스트를 잘랐으면 `item_text_truncated: true`). 상한 이하면 아무것도 바꾸지 않는다.
    """
    if result.action not in (ActionType.OBSERVE_PAGE, ActionType.EXTRACT) or not result.success:
        return
    total_chars = len(envelope_json(result))
    if total_chars <= max_chars:
        return
    data = result.data
    all_items: List[Any] = []

    if result.action is ActionType.OBSERVE_PAGE:
        obs = data.get("observation")
        if not isinstance(obs, dict) or not isinstance(obs.get("elements"), list):
            return
        from perception.engine import estimate_tokens

        all_els = list(obs["elements"])
        lines = str(obs.get("axtree_summary") or "").split("\n")
        keep_summary = len(lines) == len(all_els)

        def _set(k: int) -> None:
            summary = "\n".join(lines[:k]) if keep_summary else obs.get("axtree_summary", "")
            data["observation"] = dict(
                obs, elements=all_els[:k], axtree_summary=summary,
                token_count=estimate_tokens(summary) if keep_summary else obs.get("token_count"),
            )

        total, hint = len(all_els), _TRUNC_HINT_OBSERVE
    else:
        items = data.get("items")
        if isinstance(items, list):
            all_items = list(items)

            def _set(k: int) -> None:
                data["items"] = all_items[:k]
        elif isinstance(items, dict):
            all_items = [items]

            def _set(k: int) -> None:
                data["items"] = all_items[0] if k else None
        else:
            return
        total, hint = len(all_items), _TRUNC_HINT_EXTRACT

    def _fits(k: int) -> bool:
        _set(k)
        data["truncated"] = {
            "total_items": total, "returned_items": k, "total_chars": total_chars,
            # 자리 표시값 = max_chars: 실제 값(≤ max_chars)은 자릿수가 같거나 적다 — 채운 뒤에도 상한 이하.
            "returned_chars": max_chars, "hint": hint.format(limit=max_chars),
        }
        if item_text_cut:
            data["truncated"]["item_text_truncated"] = True
        return len(envelope_json(result)) <= max_chars

    item_text_cut = False

    lo, hi = 0, total  # 들어가는 최대 k (0 은 항상 들어간다고 본다)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _fits(mid):
            lo = mid
        else:
            hi = mid - 1
    k = lo
    all_items_ref = all_items if result.action is ActionType.EXTRACT else None
    if k == 0 and total > 0 and all_items_ref is not None:
        # 결정 C: 첫 항목 하나가 이미 상한을 넘는다 — 그 텍스트만 경계에서 자른다.
        first = all_items_ref[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            original = first["text"]
            hint, item_text_cut = _TRUNC_HINT_TEXT, True
            lo_c, hi_c, best = 0, len(original), None
            while lo_c <= hi_c:
                mid = (lo_c + hi_c) // 2
                all_items_ref[0] = dict(first, text=_safe_cut(original, mid), text_truncated=True,
                                        text_chars=len(original))
                if _fits(1):
                    best, lo_c = all_items_ref[0], mid + 1
                else:
                    hi_c = mid - 1
            if best is not None:
                all_items_ref[0], k = best, 1
            else:
                all_items_ref[0], hint, item_text_cut = first, _TRUNC_HINT_EXTRACT, False
    _fits(k)
    trunc = data["truncated"]
    for _ in range(5):  # 자기 길이를 담는 값 — 자릿수가 바뀌면 다시 잰다(고정점)
        n = len(envelope_json(result))
        if trunc["returned_chars"] == n:
            break
        trunc["returned_chars"] = n


class BrowserMCPServer:
    """19종 툴을 노출하는 MCP 서버.

    실제 MCP SDK 바인딩은 `create_server()`에서 수행하고, 본 클래스는
    툴 호출을 디스패처로 라우팅하는 순수 로직만 담당한다. 그래야
    MCP 런타임 없이도 단위 테스트가 가능하다.
    """

    def __init__(
        self,
        *,
        mode: ExecutionMode = ExecutionMode.UNATTENDED,
        allowed_domains: tuple = (),
        pre_approved_actions: tuple = (),
        headless: bool = True,
        secrets: Any = None,
        som_enabled: bool = False,
        browser_mode: str = "headless",
        chrome_profile: Any = None,
        keep_open: bool = False,
        nav_settle: bool = True,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
        allow_private_network: bool = False,
        block_loopback: bool = False,
        approval_ttl_s: float = DEFAULT_APPROVAL_TTL_S,
        handoff_root: Any = None,
    ) -> None:
        from interface.handoff import HandoffHub, state_root

        #: 사람 인계 통로(조작권·승인 증표, WS-29). 디스크 상태는 open_handoff() 에서 만든다.
        self.hub = HandoffHub(
            root=handoff_root if handoff_root is not None else state_root(),
            browser_mode=browser_mode,
            approval_ttl_s=approval_ttl_s,
        )
        self._hub_task: Any = None
        #: 안내 띠를 띄운 (탭 id, CDP 세션) — 같은 세션으로 지운다.
        self._banner: Any = None
        #: 이번 호출에서 쓴 승인 증표 id(결과에 outcome 을 붙인다).
        self._used_approval: Optional[str] = None
        #: 조작권 안내 띠 문구(코드 표시 중에는 미뤄 두었다가 코드를 내린 뒤 다시 띄운다).
        self._control_banner: Optional[str] = None
        #: 창 오버레이에 확인 코드가 떠 있다(또는 내렸는지 확인하지 못했다) — 그동안 화면 캡처 거부.
        #: 코드 평문은 여기에도 두지 않는다(표시 직후 버린다).
        self._code_on_overlay = False
        #: WS-29 R2: 확인 코드 오버레이를 켤 때마다 1 씩(세대 번호) · 진행 중인 화면 캡처 수.
        self._pixel_gen = 0
        self._captures_inflight = 0
        #: 사람이 에이전트 활성 탭을 닫았음(조작권 반납 때 확인, NB-3) — control_wait/status 로 알린다.
        self._tab_notice: Optional[Dict[str, Any]] = None
        #: Egress 대역 옵션 (WS-29b). 기본: 루프백 허용, 사설·링크로컬·CGNAT·ULA 차단.
        self.allow_private_network = allow_private_network
        self.block_loopback = block_loopback
        #: 이동 대기 스위치 (WS-28). DispatchContext.nav_settle 로 전달된다.
        self.nav_settle = nav_settle
        #: observe_page·extract 응답 봉투 크기 상한(글자, WS-30b). cap_result_size 참조.
        self.max_result_chars = max_result_chars
        #: 자격증명 플레이스홀더 해석기 (PRD 5.3). 디스패처에 주입되어
        #: type_text의 키를 실제 값으로 바꾼다. LLM에는 키만 노출된다.
        self.secrets = secrets
        #: Tier-2 SoM 게이트 (PRD §8-2). 기본 OFF — 레거시 클라이언트는
        #: annotate_som=True에 E_FEATURE_NOT_IMPLEMENTED를 그대로 받는다.
        self.som_enabled = som_enabled
        self.mode = mode
        self.allowed_domains = allowed_domains
        self.pre_approved_actions = pre_approved_actions
        self.headless = headless
        #: 브라우저 방식 (WS-27): headless / human / user-chrome. 가드·문서 상태·HITL 은
        #: 방식과 무관하게 같다 — 브라우저를 여는 방법만 다르다.
        self.browser_mode = browser_mode
        self.chrome_profile = chrome_profile
        self.keep_open = keep_open

        self._core: Any = None
        self._engine: Any = None
        self._dispatcher: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._hitl: Any = None
        #: 통과한 좌표 클릭의 판정 근거(WS-31) — 디스패치 결과 data.gate_basis 로 옮긴다.
        self._pending_gate_basis: Optional[Dict[str, Any]] = None
        self._egress: Any = None
        #: 검증 프록시 묶음(WS-29b, security.egress_runtime.EgressRuntime)
        self._egress_runtime: Any = None
        self._started = False
        #: 세션 전역 메인 문서 상태(WS-26). 탭별 기록(_page_status)이 없을 때만 쓴다
        #: (브라우저 없이 디스패처를 바꿔 끼운 단위 테스트 등).
        self._http_status: Dict[str, Any] = {"last_http_status": None}
        #: 탭(페이지)별 메인 문서 상태(WS-26b). start() 가 context 에 단다.
        self._page_status: Any = None

    # -- 수명주기 -----------------------------------------------------------

    async def start(self) -> None:
        """브라우저 세션과 파이프라인을 초기화한다."""
        if self._started:
            return

        from browser import BrowserCore
        from security.egress_runtime import EgressRuntime

        # WS-29b: 브라우저보다 검증 프록시를 먼저 띄운다 — 브라우저는 프록시로만 나간다.
        runtime = EgressRuntime(
            allowed_domains=self.allowed_domains,
            allow_private_network=self.allow_private_network,
            block_loopback=self.block_loopback,
            tokenless=self.browser_mode == "user-chrome",
        )
        await runtime.start()
        self._egress_runtime = runtime
        try:
            core = BrowserCore(
                headless=self.headless,
                browser_mode=self.browser_mode,
                chrome_profile=self.chrome_profile,
                keep_open=self.keep_open,
                egress=runtime,
            )
            self._core = await core.start()
        except BaseException:
            await self._close_egress()
            raise
        try:
            await self._init_session()
        except BaseException:
            # 시작 도중 실패하면 띄운 브라우저(user-chrome 이면 Chrome 프로세스)를 남기지 않는다.
            # keep_open 은 정상 종료 때만 존중한다 — 시작 실패면 옵션과 무관하게 닫는다(README).
            self._core.keep_open = False
            try:
                await self._core.close()
            except Exception:  # noqa: BLE001
                logger.warning("시작 실패 뒤 브라우저 정리 실패", exc_info=True)
            self._core = None
            await self._close_egress()
            raise
        self._started = True
        self.open_handoff()
        logger.info(
            "MCP 브라우저 세션 시작 (mode=%s, browser=%s)", self.mode.value, self.browser_mode
        )

    # -- 사람 인계 통로 (WS-29) -------------------------------------------------

    def open_handoff(self) -> Optional[str]:
        """서버 상태 디렉터리를 열고 감시 태스크를 띄운다. 실패하면 None(통로 없음 — fail-closed:
        사람이 승인·조작권을 줄 수 없으므로 고위험 액션은 계속 막힌다)."""
        if not self.hub.opened:
            try:
                self.hub.open()
            except Exception as exc:  # noqa: BLE001
                logger.warning("사람 인계 상태 디렉터리를 열 수 없음: %r", exc)
                return None
        if self._hub_task is None:
            try:
                self._hub_task = asyncio.get_running_loop().create_task(self._watch_handoff())
            except RuntimeError:
                self._hub_task = None
        return self.hub.server_id

    async def _watch_handoff(self) -> None:
        from interface.handoff import POLL_INTERVAL_S

        while True:
            try:
                await self._poll_handoff()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 감시는 죽지 않는다
                logger.warning("사람 인계 감시 실패", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _poll_handoff(self) -> List[str]:
        events = self.hub.poll()
        for event in events:
            if event == "released":
                # 사람이 화면을 바꿨을 수 있다 — 기존 element_id 를 모두 무효로.
                if self._engine is not None:
                    self._engine.bump_epoch("control_release")
                await self._recover_closed_tab()
                await self._show_control_banner(None)
                _stderr_line("agent-browser serve: 조작권 반납됨 — 에이전트가 이어서 진행합니다")
            elif event == "taken":
                await self._show_control_banner(
                    "사람이 조작 중 — 끝나면 `agent-browser control release`")
                _stderr_line("agent-browser serve: 조작권을 사람이 가져감")
        await self._sync_code_overlay()
        return events

    # -- 확인 코드 오버레이 (WS-29 R1) ------------------------------------------

    async def _sync_code_overlay(self) -> None:
        """hub 가 내준 확인 코드를 창 오버레이에 띄우고, 만료·입력·폐기되면 내린다.

        코드는 **창 오버레이에만** 간다 — MCP 응답·stderr·로그·상태 파일에는 쓰지 않는다. 표시가
        실패하면 hub 에 알려 코드를 무효로 한다(창에 없는 코드로는 승인할 수 없다).
        """
        job = self.hub.take_code_job()
        if job is not None:
            shown = False
            if self._human_can_see():
                from interface.handoff import CODE_TTL_S, display_safe

                ap = self.hub.approvals.get(job.approval_id)
                target = display_safe((ap.summary or {}).get("target", "") if ap else "", 40)
                kind = display_safe((ap.components or {}).get("action", "") if ap else "", 20)
                # (a) 표시 예정: 표시 시도 전에 켠다 — 새 화면 캡처는 이때부터 거부(fail-closed).
                self._code_on_overlay = True
                # (b) 이미 진행 중인 캡처가 끝나야 띄운다. 상한을 넘으면 표시 취소(코드 무효).
                if await self._wait_captures_idle():
                    # (c) 오버레이를 켜기 직전 세대를 올린다 — 그 사이 끝나지 않은 캡처는 (d) 에서 버린다.
                    self._pixel_gen += 1
                    shown = bool(await self._set_banner(
                        f"agent-browser 승인 확인 코드 {job.code}  (액션: {kind}, 대상: {target}, "
                        f"{int(CODE_TTL_S)}초) — 터미널의 approve 화면과 대조해 입력"))
                else:
                    logger.warning("진행 중 화면 캡처가 끝나지 않아 확인 코드 표시를 취소함")
            self.hub.code_shown(job.nonce, shown)
            del job  # 평문을 붙잡지 않는다
        if self._code_on_overlay and not self.hub.code_displayed():
            if await self._set_banner(self._control_banner):
                self._code_on_overlay = False

    def _pixels_blocked(self) -> bool:
        """확인 코드가 창에 떠 있을 수 있는 동안 화면 픽셀을 에이전트에게 주지 않는다."""
        return self._code_on_overlay or self.hub.code_displayed()

    async def _wait_captures_idle(self) -> bool:
        """진행 중인 화면 캡처가 모두 끝날 때까지 기다린다(상한 CAPTURE_DRAIN_TIMEOUT_S). 끝나면 True."""
        deadline = time.monotonic() + CAPTURE_DRAIN_TIMEOUT_S
        while self._captures_inflight > 0:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    def _pixels_refused(self, action: ActionType) -> ActionResult:
        from interface.handoff import CODE_TTL_S

        return self._error_result(
            action,
            ErrorCode.SCREENSHOT_FAILED,
            "사람이 승인 확인 중이라(창에 확인 코드 표시) 잠시 화면 캡처를 할 수 없습니다. "
            "observe_page·extract 는 쓸 수 있습니다. 잠시 뒤 다시 시도하세요.",
            data={"blocked_by": "approval_code_displayed", "retry_after_s": int(CODE_TTL_S)},
        )

    async def _show_control_banner(self, text: Optional[str]) -> None:
        self._control_banner = text
        if not self._code_on_overlay:  # 코드가 떠 있으면 코드를 덮지 않는다(내린 뒤 다시 띄움)
            await self._set_banner(text)

    async def _recover_closed_tab(self) -> None:
        """조작권 반납 때 에이전트 활성 탭이 살아 있는지 확인(NB-3). 사람이 닫았으면 남은 탭(없으면
        새 탭)으로 바꾸고 알린다 — 닫힌 탭을 관찰해 PAGE_CRASHED 가 나지 않게."""
        core, dispatcher = self._core, self._dispatcher
        ctx = getattr(dispatcher, "ctx", None)
        if core is None or ctx is None or not hasattr(core, "get_tab"):
            return
        closed_id = getattr(ctx, "tab_id", None)
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None)
        alive = core.get_tab(closed_id) is not None
        try:
            alive = alive and not page.is_closed()
        except Exception:  # noqa: BLE001
            pass
        if alive:
            return
        try:
            tabs = core.tabs()
            tab = tabs[0] if tabs else await core.new_tab("mcp-session")
            core.set_active_tab(tab.tab_id)
            dispatcher._set_active_page(tab.page, tab.tab_id)
            ctx.cdp = await core.new_cdp_session(tab.tab_id)
            if self._page is None or self._page.is_closed():
                self._page = tab.page
                self._cdp = ctx.cdp
            if self._engine is not None:
                self._engine.bump_epoch("tab_closed_by_human")
        except Exception:  # noqa: BLE001
            logger.warning("사람이 닫은 탭 복구 실패", exc_info=True)
            return
        self._tab_notice = {
            "closed_tab_id": closed_id,
            "active_tab_id": tab.tab_id,
            "hint": ("사람이 에이전트가 쓰던 탭을 닫았습니다 — 남은 탭으로 바꿨습니다. "
                     "browser_tab_control(command=\"list\") 로 탭을 확인하고 다시 관찰하세요."),
        }

    def _human_can_see(self) -> bool:
        """사람이 볼 브라우저 창이 있는가(headless 면 없다)."""
        return self.browser_mode != "headless" or not self.headless

    async def _set_banner(self, text: Optional[str]) -> bool:
        """창 위 안내 띠(DevTools 오버레이 — 페이지 DOM 을 바꾸지 않고 페이지 스크립트가 읽거나
        누를 수 없다). 성공하면 True. 창이 없거나 실패하면 False(안내는 stderr 에도 나간다).

        지울 때는 띄웠던 그 탭의 세션으로 지운다(활성 탭이 바뀌었어도 옛 탭에 남지 않게). 띄우기 전
        DOM.enable 이 필요하다(Chromium: 'DOM should be enabled first').
        주의: headed 창에서는 이 오버레이가 Page 스크린샷에 찍힌다(R1 실측) — 확인 코드가 떠 있는
        동안 화면 캡처를 거부하는 이유(_pixels_blocked).
        """
        if not self._human_can_see() or self._core is None:
            return False
        try:
            if not text:
                if self._banner is not None:
                    cdp = self._banner[1]
                    await cdp.send("Overlay.setPausedInDebuggerMessage", {})
                    await cdp.send("Overlay.disable")
                return True
            tab_id = self._core.active_tab_id
            if self._banner is not None and self._banner[0] != tab_id:
                old = self._banner[1]
                await old.send("Overlay.setPausedInDebuggerMessage", {})
                self._banner = None
            if self._banner is None:
                self._banner = (tab_id, await self._core.new_cdp_session(tab_id))
            cdp = self._banner[1]
            await cdp.send("DOM.enable")
            await cdp.send("Overlay.enable")
            await cdp.send("Overlay.setPausedInDebuggerMessage", {"message": text})
            return True
        except Exception:  # noqa: BLE001
            logger.debug("안내 띠 표시 실패", exc_info=True)
            return False

    def _current_origin(self) -> str:
        from urllib.parse import urlsplit

        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None) or self._page
        try:
            parts = urlsplit(getattr(page, "url", "") or "")
            return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme else ""
        except ValueError:
            return ""

    async def call_server_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """계약 밖 서버 도구(WS-29). 결과는 dict(JSON 응답)."""
        args = dict(arguments or {})
        if name not in SERVER_TOOLS:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": f"알 수 없는 툴: {name}"}
        if not self.hub.opened:
            self.open_handoff()
        if not self.hub.opened:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": "사람 인계 통로(상태 디렉터리)를 열 수 없습니다 — 서버 stderr 참조."}
        await self._poll_handoff()
        short = name[len(TOOL_PREFIX):]
        if short == "control_status":
            data: Dict[str, Any] = {"control": self.hub.status()}
            if self._tab_notice is not None:
                data["tab_closed_by_human"] = self._tab_notice
            return {"success": True, "data": data}
        if short == "control_request":
            return await self._control_request(args)
        timeout = _wait_timeout(args.get("timeout_s"))
        if short == "control_wait":
            return await self._control_wait(timeout)
        return await self._approval_wait(str(args.get("approval_id") or ""), timeout)

    async def _control_request(self, args: Dict[str, Any]) -> Dict[str, Any]:
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": "reason 이 필요합니다."}
        if not self._human_can_see():
            # 사람이 볼 창이 없다 — 운영자가 창 보이는 방식으로 띄워야 한다(우회 아님).
            return {
                "success": False,
                "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                "error_message": (
                    "headless 서버라 사람이 볼 창이 없습니다. 운영자에게 `agent-browser serve "
                    "--browser human` 또는 `--browser user-chrome` 으로 다시 띄우도록 요청하세요."
                ),
                "data": {"control": self.hub.status(), "browser_mode": self.browser_mode},
            }
        if not self._started:
            await self.start()
        out = self.hub.request(reason, secret_wanted=bool(args.get("secret_wanted")))
        page = getattr(getattr(self._dispatcher, "ctx", None), "page", None) or self._page
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        from interface.handoff import display_safe

        await self._show_control_banner(f"에이전트가 사람 조작을 요청: {display_safe(reason, 80)} — "
                                        f"`agent-browser control take`")
        _stderr_line(f"agent-browser serve: [사람 조작 요청] {display_safe(reason, 500)} "
                     f"(secret_wanted={out['secret_wanted']}) — {out['how_to_respond']}")
        return {"success": True, "data": out}

    async def _control_wait(self, timeout: float) -> Dict[str, Any]:
        st = self.hub.status()
        if st["holder"] == "agent" and not st["requested"]:
            return {"success": True, "data": {"control": st, "changed": "none_pending",
                                              "hint": _RELEASE_HINT}}
        start_version = self.hub.version
        deadline = time.monotonic() + timeout
        changed = "timeout"
        while time.monotonic() < deadline:
            events = await self._poll_handoff()
            hit = [e for e in events if e in ("taken", "released")]
            if hit:
                changed = hit[-1]
                break
            if self.hub.version != start_version and self.hub.last_event in ("taken", "released"):
                changed = self.hub.last_event
                break
            await asyncio.sleep(0.05)
        data: Dict[str, Any] = {"control": self.hub.status(), "changed": changed}
        if changed == "released":
            data["hint"] = _RELEASE_HINT
            data["snapshot_epoch"] = self._engine.epoch if self._engine else 0
            if self._tab_notice is not None:
                data["tab_closed_by_human"], self._tab_notice = self._tab_notice, None
        return {"success": True, "data": data}

    async def _approval_wait(self, approval_id: str, timeout: float) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            st = self.hub.approval_status(approval_id)
            if st["status"] != "pending" or time.monotonic() >= deadline:
                break
            await self._poll_handoff()
            await asyncio.sleep(0.05)
        out = dict(st)
        if out["status"] == "approved":
            out["next"] = "같은 툴을 같은 인자 + approval_id 로 다시 호출하세요(1회용)."
        return {"success": out["status"] != "unknown", "data": {"approval": out}}

    async def _init_session(self) -> None:
        """컨텍스트·첫 탭·디스패처·Egress 가드·HITL 을 준비한다(세 방식 공통)."""
        from actions import ActionDispatcher, DispatchContext
        from perception import PerceptionEngine
        from security import HITLGate

        await self._core.new_context("mcp-session")
        # 새 탭·팝업 문서 응답도 잡도록 context 단위로 달되, 응답을 온 탭에 묶는다
        # (WS-26b: 팝업 403 이 원래 탭 판정에 새지 않게).
        from browser.doc_status import PageDocumentStatus

        self._page_status = PageDocumentStatus()
        self._page_status.attach(self._core.context_for("mcp-session"))
        tab = await self._core.new_tab("mcp-session")
        self._page = tab.page
        self._cdp = await self._core.new_cdp_session(tab.tab_id)

        self._engine = PerceptionEngine()
        self._dispatcher = ActionDispatcher(
            DispatchContext(
                page=self._page,
                engine=self._engine,
                cdp=self._cdp,
                tab_id=tab.tab_id,
                core=self._core,  # tab_control이 탭 수명주기에 접근하려면 필요
                secrets=self.secrets,  # 자격증명 플레이스홀더 해석 (PRD 5.3)
                som_enabled=self.som_enabled,  # Tier-2 SoM 게이트 (PRD §8-2)
                nav_settle=self.nav_settle,  # 이동 대기 스위치 (WS-28)
            )
        )

        # WS-29b: 루프백만 기본 허용(이전 allow_loopback=True 는 사설 대역 전체를 열었다).
        # 같은 가드를 프록시(리다이렉트 홉·WebSocket·재바인딩)와 route(조기 차단)가 공유한다.
        self._egress = self._egress_runtime.guard
        await self._egress.install(self._core.context_for("mcp-session"))

        self._hitl = HITLGate(
            mode=self.mode, pre_approved_actions=self.pre_approved_actions
        )

    async def close(self) -> None:
        task, self._hub_task = self._hub_task, None
        if task is not None:
            task.cancel()
        try:
            if self._core is not None:
                await self._core.close()
            await self._close_egress()
        finally:
            self.hub.close()  # 상태 디렉터리 정리(사람 CLI 가 죽은 서버를 고르지 않게)
            self._started = False

    async def _close_egress(self) -> None:
        runtime, self._egress_runtime = self._egress_runtime, None
        if runtime is not None:
            try:
                await runtime.close()
            except Exception:  # noqa: BLE001
                logger.warning("Egress 프록시 정리 실패", exc_info=True)

    @property
    def _egress_proxy(self) -> Any:
        return self._egress_runtime.proxy if self._egress_runtime is not None else None

    async def __aenter__(self) -> "BrowserMCPServer":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        await self.close()

    # -- 툴 호출 ------------------------------------------------------------

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> ActionResult:
        """MCP 툴 호출을 디스패처로 라우팅한다(_call_tool). 화면 캡처는 확인 코드 표시와 직렬화한다.

        WS-29 R2 (d): 캡처를 시작한 뒤 확인 코드 오버레이가 한 번이라도 켜졌으면(세대 번호가 바뀜)
        캡처 결과를 버리고 거부한다 — 코드 표시 쪽의 기다림(b)이 없어도 막히는 겹 방어.
        """
        capture: Dict[str, Any] = {}
        try:
            result = await self._call_tool(name, arguments, capture)
            if capture and capture["gen"] != self._pixel_gen:
                return self._pixels_refused(ActionType.TAKE_SCREENSHOT)
            return result
        finally:
            if capture:
                self._captures_inflight -= 1

    async def _call_tool(self, name: str, arguments: Dict[str, Any],
                         capture: Dict[str, Any]) -> ActionResult:
        """MCP 툴 호출을 디스패처로 라우팅한다.

        실패는 예외가 아니라 `ActionResult`로 반환해 클라이언트가
        구조화된 정보를 받도록 한다.
        """
        action = action_from_tool(name)
        if action is None:
            return self._error_result(
                ActionType.OBSERVE_PAGE,
                ErrorCode.FEATURE_NOT_IMPLEMENTED,
                f"알 수 없는 툴: {name}",
            )

        if not self._started:
            await self.start()

        params: Dict[str, Any] = dict(arguments or {})
        # WS-29: 승인 증표 id 는 계약 밖 인자 — 계약 입력 검증 전에 떼어 낸다.
        approval_id = params.pop("approval_id", None)
        # WS-29: 사람이 조작권을 가진 동안(또는 비밀 입력 요청 중) 에이전트 액션 거부.
        if self.hub.opened:
            await self._poll_handoff()
        control = self.hub.control_blocks(action, params)
        if control is not None:
            what = "관찰을 포함한 모든 액션" if control["secret_wanted"] else "조작 액션"
            return self._error_result(
                action,
                ErrorCode.HITL_UNATTENDED_BLOCKED,
                f"사람이 브라우저를 다루는 중이라 {what}을 거부했습니다({control['reason']}). "
                "browser_control_wait 로 반납을 기다린 뒤 다시 관찰하세요.",
                data={"control": control},
            )
        if action is ActionType.TAKE_SCREENSHOT:
            if self._pixels_blocked():
                # WS-29 R1: 사람용 확인 코드가 창 오버레이에 떠 있다(또는 띄울 예정). headed 창에서는
                # 오버레이가 스크린샷에 찍히므로(실측) 그동안 화면 픽셀(일반·전체·SoM)을 주지 않는다.
                return self._pixels_refused(action)
            # WS-29 R2: 이 캡처를 등록한다(검사와 등록 사이에 await 없음). 코드 표시는 등록된 캡처가
            # 끝나길 기다리고, call_tool 은 반환 직전 세대 번호로 캡처 도중 오버레이가 켜졌는지 본다.
            self._captures_inflight += 1
            capture["gen"] = self._pixel_gen

        # 입력 검증: 계약 모델로 파싱해 잘못된 인자를 조기 차단한다.
        model = ACTION_INPUT_MAP.get(action)
        to_main = False
        if action is ActionType.SWITCH_FRAME and "to_main" in params:
            # WS-30 추가 B: 디스패처의 메인 복귀(to_main)는 계약(동결)에 없는 키라 검증에서
            # 버려지고 '정확히 하나' 검증에 걸려 MCP 로는 복귀할 수 없었다 — 경계 특례.
            to_main = bool(params.pop("to_main"))
            if to_main and any(params.get(k) for k in ("frame_selector", "shadow_root_selector")):
                return self._error_result(
                    action,
                    ErrorCode.FRAME_NOT_FOUND,
                    "to_main 과 frame_selector/shadow_root_selector 는 함께 지정할 수 없습니다.",
                )
        if to_main:
            params = {"to_main": True}
        elif model is not None:
            try:
                validated = model(**params)
                params = validated.model_dump(exclude_none=True)
            except Exception as exc:  # noqa: BLE001 - Pydantic ValidationError 등
                return self._error_result(
                    action,
                    ErrorCode.ELEMENT_NOT_FOUND
                    if "element_id" in str(exc)
                    else ErrorCode.INVALID_URL,
                    f"입력 검증 실패: {exc}",
                )
        # WS-30 항목 2: 계약 키(trigger_element_id 등)를 디스패처 키로 — 게이트도 같은 키를 본다.
        from actions.dispatcher import normalize_action_params

        params = normalize_action_params(action, params)

        # HITL 게이트: 고위험 액션은 모드에 따라 차단하거나 승인을 요구한다.
        self._pending_gate_basis = None
        self._used_approval = None
        blocked = await self._check_hitl(action, params, approval_id=approval_id)
        if blocked is not None:
            return blocked

        # 누적 수로 이번 호출의 신규분을 센다(기록은 상한 deque 라 길이로는 못 센다, WS-29b R1).
        blocks_before = self._egress.blocked_total if self._egress is not None else 0
        upstream_before = self._egress.upstream_total if self._egress is not None else 0
        used, self._used_approval = self._used_approval, None
        if used is not None:
            # NB-1: 승인한 그 요소만 — 자가 치유(유사 이름 대체)를 이 호출 동안 끈다.
            self._dispatcher.heal_disabled = True
        try:
            result = await self._dispatcher.dispatch(action, params)
        except BaseException:
            if used is not None:
                self.hub.set_outcome(used, "outcome_unknown")
            raise
        finally:
            if used is not None:
                self._dispatcher.heal_disabled = False
        if used is not None:
            self._attach_approval_outcome(used, result)
        result = self._attach_egress_block(action, params, result, blocks_before)
        result = self._attach_upstream_failure(action, params, result, upstream_before)
        if self._pending_gate_basis is not None:
            result.data.setdefault("gate_basis", self._pending_gate_basis)
            self._pending_gate_basis = None
        if action in CHALLENGE_CHECK_ACTIONS:
            await self._attach_challenge(result)
        # WS-30b: 큰 페이지 결과가 클라이언트 도구 결과 한도를 넘지 않게 항목 경계에서 자른다.
        cap_result_size(result, self.max_result_chars)
        return result

    def _attach_egress_block(
        self, action: ActionType, params: Dict[str, Any], result: ActionResult, before: int
    ) -> ActionResult:
        """이번 호출 중 Egress 가 막은 이동을 에이전트에게 알린다 (WS-29b).

        활성 탭의 문서 이동(요청 주소·리다이렉트 홉·현재 주소의 호스트)이 막혔으면
        data.egress 에 이유(code=egress_blocked, host, category, reason, open_with)를 싣는다.
        navigate 가 막혔으면 실패(E_INVALID_URL)로 돌려준다 — 막힌 문서를 성공으로 보고하지
        않는다. 하위 요청(이미지·비콘 등) 차단은 싣지 않는다(문서 이동만).
        """
        if self._egress is None:
            return result
        new = self._egress.blocked_since(before)
        if not new:
            return result
        from urllib.parse import urlparse

        hosts = set()
        for raw in (params.get("url"), getattr(self._page, "url", None), result.current_url):
            try:
                h = urlparse(raw or "").hostname
            except ValueError:
                h = None
            if h:
                hosts.add(h.lower())
        hit = next((d for d in reversed(new) if d.host and d.host.lower() in hosts), None)
        if hit is None and action is ActionType.NAVIGATE and not result.success:
            hit = new[-1]
        if hit is None:
            return result
        info = hit.to_agent()
        if action is ActionType.NAVIGATE:
            return self._error_result(
                action,
                ErrorCode.INVALID_URL,
                f"egress_blocked: {info['host']} ({info['category']})",
                data={"egress": info, "url": getattr(self._page, "url", "")},
            )
        result.data["egress"] = info
        return result

    def _attach_upstream_failure(
        self, action: ActionType, params: Dict[str, Any], result: ActionResult, before: int
    ) -> ActionResult:
        """프록시가 업스트림에 닿지 못해 스스로 만든 502 문서를 이동 실패로 알린다 (WS-29b R1).

        프록시 없이는 브라우저가 이름 해석·접속 실패로 이동 자체를 실패시킨다(base 실측:
        E_NAVIGATE_TIMEOUT). 프록시를 거치면 그 실패가 502 문서로 바뀌어 '열렸다'로 보였다.
        이번 호출 중 프록시가 기록한 업스트림 실패(호스트·포트)가 이동 대상과 같고, 탭의 메인
        문서가 502 일 때만 해당한다 — 사이트가 실제로 준 502 는 프록시 기록이 없어 그대로 둔다.
        이미 실패한 이동(https CONNECT 실패 등)에는 이유만 싣는다.
        """
        if self._egress is None or self._dispatcher is None:
            return result
        new = self._egress.upstream_failures_since(before)
        if not new:
            return result
        if result.data.get("egress") is not None:
            return result  # 차단 이유가 먼저다
        from urllib.parse import urlsplit

        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "page", None) or self._page
        targets = set()
        for raw in (params.get("url") if action is ActionType.NAVIGATE else None,
                    getattr(page, "url", None), result.current_url):
            try:
                parts = urlsplit(raw or "")
                host = (parts.hostname or "").lower()
                port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())
            except ValueError:
                continue
            if host:
                targets.add((host, port))
        hit = next((f for f in reversed(new)
                    if (f.host.lower().strip("[]"), f.port) in targets), None)
        if hit is None:
            return result
        if result.success:
            status: Optional[int] = None
            try:
                if self._page_status is not None:
                    status = self._page_status.status_for(page)
            except Exception:  # noqa: BLE001
                status = None
            if status != 502:
                return result  # 메인 문서는 정상 — 하위 요청 실패일 뿐
        info = hit.to_agent()
        if action is ActionType.NAVIGATE:
            return self._error_result(
                action,
                ErrorCode.NAVIGATE_TIMEOUT,
                f"이동 실패: {info['code']}: {info['host']}",
                data={"egress": info, "url": getattr(page, "url", "")},
            )
        result.data["egress"] = info
        return result

    async def _attach_challenge(self, result: ActionResult) -> None:
        """활성 탭 화면의 차단·캡차 판정을 result.data 에 병합한다 (WS-26).

        판정만 한다 — 풀거나 우회하지 않는다. 판정 실패는 challenge=None 으로 둔다.
        기존 data 키는 덮어쓰지 않는다.

        판정 페이지는 root_page(프레임 진입 전 최상위) → page → self._page 순이다 —
        최상위가 캡차인데 그 안 정상 iframe 에 들어가 있어도 캡차를 놓치지 않는다.
        WS-26b: 상태는 **그 페이지가** 받은 마지막 메인 문서 응답이고, 판정에는 지금
        URL 이 그 응답 URL 과 같을 때만 넘긴다(pushState 로 옮긴 SPA 화면 오판 방지).
        """
        ctx = getattr(self._dispatcher, "ctx", None)
        page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None)
        if page is None:
            page = self._page
        tracker = self._page_status
        last_status: Optional[int] = None
        judge_status: Optional[int] = None
        try:
            if tracker is not None:
                last_status = tracker.status_for(page)
                judge_status = tracker.judge_status_for(page)
            else:
                last_status = self._http_status.get("last_http_status")
                judge_status = last_status
        except Exception:  # noqa: BLE001 - 상태 조회 실패로 결과를 망가뜨리지 않는다
            logger.warning("문서 상태 조회 실패 — null 로 둔다", exc_info=True)
        challenge: Optional[Dict[str, str]] = None
        try:
            from browser import challenge as challenge_mod

            found = await challenge_mod.detect_challenge(page, last_status=judge_status)
            if found.detected:
                challenge = {
                    "kind": found.kind.value,
                    "vendor": found.vendor,
                    "reason": found.reason,
                }
        except Exception:  # noqa: BLE001 - 판정 실패로 결과를 망가뜨리지 않는다
            logger.warning("차단 판정 실패 — challenge=None 으로 둔다", exc_info=True)
        result.data.setdefault("challenge", challenge)
        result.data.setdefault("last_http_status", last_status)

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
        check = getattr(self._dispatcher, "approval_target_check", None)
        if check is None:
            target = {"fresh": False, "detail": "대상 확인 불가"}  # fail-closed
        else:
            try:
                target = await check(params)
            except Exception as exc:  # noqa: BLE001
                target = {"fresh": False, "detail": f"대상 확인 실패: {type(exc).__name__}"}
        if not target.get("fresh"):
            return self._error_result(
                action,
                ErrorCode.TOCTOU_MISMATCH,
                "승인한 대상이 바뀌어 실행하지 않았습니다(" + str(target.get("detail") or "") + "). "
                "다시 관찰하세요 — 같은 대상이면 이 승인으로 다시 호출할 수 있고, 다르면 새 승인이 필요합니다.",
                data={"approval": {"approval_id": approval_id, "status": "approved",
                                   "reason": "target_changed",
                                   "detail": str(target.get("detail") or "")}},
            )
        ok, why = self.hub.consume_approval(approval_id, components)
        if not ok:  # 그사이 다른 호출이 썼다
            return {"approval_id": approval_id, "reason": why, "reason_code": "used"}
        self._used_approval = approval_id
        return True

    async def _check_hitl(
        self, action: ActionType, params: Dict[str, Any], approval_id: Any = None
    ) -> Optional[ActionResult]:
        """고위험 액션 승인 게이트. 차단 시 ActionResult를 반환한다."""
        from security import ActionContext

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
        elif action is ActionType.CLICK and params.get("x") is not None:
            # WS-31: 좌표 클릭도 그 좌표가 누를 요소(상호작용 조상)의 이름·문맥으로 판정한다.
            element_name, signals, unresolved, basis_extra = await self._point_target(params)
            report_basis = True
        elif action is ActionType.TYPE_TEXT and not submits_form:
            text = str(params.get("text") or "")
            if "\n" in text or "\r" in text:
                # 줄바꿈 입력은 한 줄 입력칸에서 Enter 와 같다(Playwright type 실측: 폼 제출).
                info = await dispatcher.describe_element(element_id) if dispatcher else None
                if info is None:
                    unresolved = "입력 대상을 읽을 수 없음"
                elif info.get("tag") == "INPUT" and info.get("in_form"):
                    submits_form = True
        elif action is ActionType.PRESS_KEY:
            submits_form, name, unresolved, signals = await self._press_key_target(params)
            element_name = name or element_name

        decision = self._hitl.evaluate(
            ActionContext(
                action=action,
                element_name=element_name,
                selector=selector,
                domain=self._current_domain(),
                submits_form=submits_form,
                unresolved_target=unresolved,
                signals=signals,
                basis_extra=basis_extra,
            )
        )
        if decision.allowed:
            if report_basis:
                self._pending_gate_basis = dict(decision.basis)
            return None

        # WS-29: 승인 증표 — 사람이 대역 밖(`agent-browser approve` + 창의 확인 코드)에서 승인한
        # 바로 그 행동만 통과. R1: 사람이 볼 창이 없으면(headless) 증표를 발급하지 않는다 —
        # 확인 코드를 띄울 곳이 없어 사람만 승인할 수 있다는 보장이 없다.
        ap: Any = None
        approval: Optional[Dict[str, Any]] = None
        if self._human_can_see():
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
        else:
            rejected = None

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
        target = await dispatcher.describe_point(int(params["x"]), int(params["y"]))
        if target.get("unresolved"):
            return "", (), str(target["unresolved"]), {"coordinate_target": "unresolved"}
        info = target.get("info") or {}
        extra = {
            "coordinate_target": "interactive" if info.get("interactive") else "non_interactive",
            "tag": str(info.get("tag") or ""),
        }
        unresolved = ""
        if not info.get("interactive") and info.get("inside_form"):
            unresolved = "좌표가 폼 안의 비상호작용 요소 위(무엇이 일어날지 판정 불가)"
        return str(target.get("name") or ""), _signals_of(info), unresolved, extra

    def _current_domain(self) -> str:
        try:
            from urllib.parse import urlparse

            return urlparse(self._page.url).hostname or ""
        except Exception:  # noqa: BLE001
            return ""

    def _error_result(
        self,
        action: ActionType,
        code: ErrorCode,
        message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> ActionResult:
        return ActionResult(
            success=False,
            action=action,
            current_url=self._page.url if self._page else "",
            snapshot_epoch=self._engine.epoch if self._engine else 0,
            tab_id=self._core.active_tab_id if self._core else "",
            healed=False,
            reobserve_required=False,
            retry_safe=True,
            error_code=code,
            error_message=message,
            data=data or {},
        )

    # -- 조회 ---------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    def list_tools(self) -> List[Dict[str, Any]]:
        return build_all_tools()


def create_server(
    *,
    mode: ExecutionMode = ExecutionMode.UNATTENDED,
    allowed_domains: tuple = (),
    pre_approved_actions: tuple = (),
    secrets: Any = None,
    som_enabled: bool = False,
    browser_mode: str = "headless",
    chrome_profile: Any = None,
    keep_open: bool = False,
    nav_settle: bool = True,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    allow_private_network: bool = False,
    block_loopback: bool = False,
    approval_ttl_s: float = DEFAULT_APPROVAL_TTL_S,
):
    """MCP SDK에 바인딩된 서버 인스턴스를 생성한다.

    `mcp` 패키지가 없는 환경에서도 나머지 모듈이 import되도록
    지연 import한다.
    """
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    backend = BrowserMCPServer(
        mode=mode,
        allowed_domains=allowed_domains,
        pre_approved_actions=pre_approved_actions,
        secrets=secrets,
        som_enabled=som_enabled,
        browser_mode=browser_mode,
        chrome_profile=chrome_profile,
        keep_open=keep_open,
        nav_settle=nav_settle,
        max_result_chars=max_result_chars,
        allow_private_network=allow_private_network,
        block_loopback=block_loopback,
        approval_ttl_s=approval_ttl_s,
    )

    def _build_tools() -> List[Tool]:
        """툴 정의를 SDK 타입으로 변환한다.

        `Tool`의 스키마 필드는 SDK 메이저에 따라 다르다.
        - mcp 1.x: `inputSchema`
        - mcp 2.x: `input_schema` (JSON alias는 inputSchema)

        한쪽만 지원하면 다른 메이저에서 tools/list가 통째로 실패한다.
        실제 필드를 조회해 맞춘다 — 버전 문자열 분기는 프리릴리스나
        포크에서 어긋난다.
        """
        field = "input_schema" if "input_schema" in Tool.model_fields else "inputSchema"
        out: List[Tool] = []
        for spec in build_listed_tools():  # 액션 툴 19종 + 서버 도구(WS-29)
            kwargs = {
                "name": spec["name"],
                "description": spec["description"],
                field: spec["inputSchema"],
            }
            out.append(Tool(**kwargs))
        return out

    async def _list_tools_impl() -> List[Tool]:
        return _build_tools()

    async def _call_tool_impl(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
        if name in SERVER_TOOLS:
            import json

            payload = await backend.call_server_tool(name, arguments)
            return [TextContent(type="text", text=json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")))]
        result = await backend.call_tool(name, arguments)
        # WS-30b: 기본값 필드를 뺀 봉투(빠진 필드 = 계약 기본값, ENVELOPE_RULE).
        return [TextContent(type="text", text=envelope_json(result))]

    # SDK 메이저별 등록 방식이 다르다. 2.x의 lowlevel Server에는
    # list_tools/call_tool 데코레이터가 없고 생성자 콜백을 받는다.
    # 실측 — 데코레이터만 쓰면 create_server()가 AttributeError로 즉사해
    # `agent-browser serve` 경로 전체가 막힌다.
    if hasattr(Server("__probe__"), "list_tools"):
        # mcp 1.x — 데코레이터 등록
        server = Server("agent-browser")
        server.list_tools()(_list_tools_impl)  # type: ignore[attr-defined]
        server.call_tool()(_call_tool_impl)  # type: ignore[attr-defined]
        return server, backend

    # mcp 2.x — 생성자 콜백 등록
    from mcp import types as mcp_types

    async def _on_list_tools(ctx: Any, params: Any) -> Any:
        return mcp_types.ListToolsResult(tools=_build_tools())

    async def _on_call_tool(ctx: Any, params: Any) -> Any:
        content = await _call_tool_impl(params.name, params.arguments or {})
        return mcp_types.CallToolResult(content=list(content))

    server = Server(
        "agent-browser",
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )
    return server, backend


async def run_stdio(
    *,
    mode: ExecutionMode = ExecutionMode.UNATTENDED,
    allowed_domains: tuple = (),
    pre_approved_actions: tuple = (),
    secrets_path: Optional[str] = None,
    som_enabled: bool = False,
    browser_mode: str = "headless",
    chrome_profile: Any = None,
    keep_open: bool = False,
    nav_settle: bool = True,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    allow_private_network: bool = False,
    block_loopback: bool = False,
    approval_ttl_s: float = DEFAULT_APPROVAL_TTL_S,
) -> None:
    """stdio 트랜스포트로 MCP 서버를 구동한다.

    stdout 은 MCP 프로토콜 전용이다 — 시작 로그는 stderr 로 한 줄만 쓴다.
    """
    import sys

    from mcp.server.stdio import stdio_server

    secrets = None
    if secrets_path:
        # 권한이 느슨하면 SecretsError로 기동을 거부한다(PRD 5.3).
        from security import SecretStore

        secrets = SecretStore.from_file(secrets_path)

    server, backend = create_server(
        mode=mode,
        allowed_domains=allowed_domains,
        pre_approved_actions=tuple(pre_approved_actions),
        secrets=secrets,
        som_enabled=som_enabled,
        browser_mode=browser_mode,
        chrome_profile=chrome_profile,
        keep_open=keep_open,
        nav_settle=nav_settle,
        max_result_chars=max_result_chars,
        allow_private_network=allow_private_network,
        block_loopback=block_loopback,
        approval_ttl_s=approval_ttl_s,
    )
    # WS-29: 사람 인계 통로(상태 디렉터리)를 브라우저보다 먼저 연다 — 사람이 server_id 로 고른다.
    server_id = backend.open_handoff() if hasattr(backend, "open_handoff") else None
    extra = ""
    if browser_mode == "user-chrome":
        extra = f" chrome_profile={chrome_profile or '(기본)'} keep_open={bool(keep_open)}"
    extra += (
        f" egress(loopback={'blocked' if block_loopback else 'allowed'},"
        f" private={'allowed' if allow_private_network else 'blocked'})"
    )
    if browser_mode == "user-chrome":
        # WS-29b: 우리가 띄운 Chrome 에만 프록시 플래그를 줄 수 있다 — 한 줄로 알린다.
        extra += " [egress 프록시는 우리가 띄운 Chrome 에만 적용, README 보안 절]"
    print(
        f"agent-browser serve: browser={browser_mode} mode={mode.value}{extra}"
        " (브라우저는 첫 툴 호출 때 시작)",
        file=sys.stderr,
        flush=True,
    )
    if server_id:
        print(
            f"agent-browser serve: server_id={server_id} — 사람 인계: "
            f"`agent-browser control take|release --server {server_id}`, "
            f"승인: `agent-browser approve <approval_id>`",
            file=sys.stderr,
            flush=True,
        )
    async def _serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    received: List[int] = []
    previous = _install_shutdown_signals(loop, stop, received)
    serve_task = asyncio.ensure_future(_serve())
    stop_task = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        serve_task.cancel()
        stop_task.cancel()
        _restore_signals(previous)
        await _bounded_close(backend)
        raise

    if not received:
        # 정상 종료(stdin EOF 등): 기존과 같다 — 정리 뒤 반환(종료 코드 0), 서버 예외는 다시 올린다.
        stop_task.cancel()
        try:
            await serve_task
        finally:
            _restore_signals(previous)
            await _bounded_close(backend)
        return

    # 종료 신호: 서버를 취소하고(기다리지 않음) 브라우저를 정리한 뒤 프로세스를 끝낸다.
    # stdin 리더는 스레드에서 막혀 있어 취소가 끝나지 않을 수 있으므로(실측: SIGINT 로
    # asyncio.run 이 무한 대기) 정리가 끝나면 os._exit 로 바로 종료한다.
    serve_task.cancel()
    # 진행 중인 툴 호출(브라우저 시작 등)이 취소 정리를 마칠 시간을 짧게 준다(상한 있음 —
    # stdin 리더 스레드 때문에 서버 태스크 자체는 끝나지 않을 수 있다).
    await asyncio.wait({serve_task}, timeout=min(SHUTDOWN_TASK_GRACE_S, SHUTDOWN_CLOSE_TIMEOUT_S))
    signum = received[0]
    ok = await _bounded_close(backend)
    _stderr_line(
        f"agent-browser serve: {'정리 완료' if ok else '정리 미완료'} — 종료 코드 {128 + signum}"
    )
    os._exit(128 + signum)


#: 종료 신호 뒤 backend.close() 상한(초). UserChrome.close 의 기본 timeout(10초)과 같다.
SHUTDOWN_CLOSE_TIMEOUT_S = 10.0
#: 정리가 이벤트 루프를 막는(동기 대기) 경우의 감시 스레드 여유(초). 상한+여유 뒤 강제 종료.
SHUTDOWN_WATCHDOG_GRACE_S = 5.0
#: 신호 뒤 취소된 서버 태스크가 자기 정리를 마치기를 기다리는 시간(초).
SHUTDOWN_TASK_GRACE_S = 2.0
#: serve 가 처리하는 종료 신호. SIGKILL 은 잡을 수 없다(README 참조).
SHUTDOWN_SIGNALS = tuple(
    getattr(signal, n) for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n)
)


def _stderr_line(text: str) -> None:
    """stderr 에 한 줄(신호 처리기 안에서도 안전하게 os.write). stdout 은 MCP 전용.

    한 줄 보장: 외부 유래 문자열(에이전트 reason·URL·예외 문구)이 섞여도 제어문자·개행·양방향
    제어를 보이는 표기로 바꾼다(WS-29 R1 BLOCKING-1 — 운영자 터미널 위조 방지).
    """
    from interface.handoff import display_safe

    try:
        os.write(2, (display_safe(text, 4000) + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def _install_shutdown_signals(loop: Any, stop: Any, received: List[int]) -> Dict[int, Any]:
    """SIGTERM·SIGHUP·SIGINT 처리기를 단다. 이전 처리기를 돌려준다.

    loop.add_signal_handler 가 아니라 signal.signal 을 쓴다 — 정리가 이벤트 루프를 동기로
    막고 있을 때(UserChrome.close 의 proc.wait 등)도 두 번째 신호로 바로 끝낼 수 있게.
    첫 신호: stop 이벤트(루프 스레드 안전) + 감시 스레드 시작. 두 번째 신호: 즉시 종료.
    """
    import threading

    def _handler(signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        if received:
            _stderr_line(f"agent-browser serve: 신호 {name} 다시 받음 — 정리를 기다리지 않고 종료")
            os._exit(128 + signum)
        received.append(signum)
        _stderr_line(f"agent-browser serve: 신호 {name} — 브라우저 정리 후 종료")
        cap = SHUTDOWN_CLOSE_TIMEOUT_S + SHUTDOWN_WATCHDOG_GRACE_S

        def _watchdog() -> None:
            time.sleep(cap)
            _stderr_line(f"agent-browser serve: 정리가 {cap:g}s 안에 끝나지 않아 강제 종료")
            os._exit(128 + signum)

        threading.Thread(target=_watchdog, name="serve-shutdown-watchdog", daemon=True).start()
        loop.call_soon_threadsafe(stop.set)

    previous: Dict[int, Any] = {}
    for sig in SHUTDOWN_SIGNALS:
        try:
            previous[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):  # 메인 스레드가 아니면 등록 불가 — 기존 동작 유지
            continue
    # 신호 마스크는 부모에게서 상속되고 signal.signal 은 마스크를 풀지 않는다 — 부모가 막아 둔
    # 채로 띄우면 처리기를 달아도 신호가 전달되지 않아 serve 가 끝나지 않았다(WS-29b R1 실측).
    # 처리기를 단 신호만 이 스레드에서 푼다(stdin 리더 등 이후 만든 스레드도 이 마스크를 받는다).
    if previous and hasattr(signal, "pthread_sigmask"):
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, set(previous))
        except (ValueError, OSError):
            pass
    return previous


def _restore_signals(previous: Dict[int, Any]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            pass


async def _bounded_close(backend: Any) -> bool:
    """backend.close() 를 상한 시간 안에서 돌린다. 끝까지 정리했으면 True."""
    try:
        await asyncio.wait_for(backend.close(), timeout=SHUTDOWN_CLOSE_TIMEOUT_S)
        return True
    except asyncio.TimeoutError:
        _stderr_line(
            f"agent-browser serve: 브라우저 정리가 상한({SHUTDOWN_CLOSE_TIMEOUT_S:g}s)을 넘어 중단"
        )
        _kill_owned_chrome(backend)
    except Exception as exc:  # noqa: BLE001
        _stderr_line(f"agent-browser serve: 브라우저 정리 실패: {exc!r}")
        _kill_owned_chrome(backend)
    return False


def _kill_owned_chrome(backend: Any) -> None:
    """정리가 막혔을 때 마지막 수단: 우리가 띄운 Chrome 만 닫는다(keep_open 이면 둔다).

    UserChrome.close 는 attach 로 붙은(소유하지 않은) Chrome 은 건드리지 않는다.
    """
    core = getattr(backend, "_core", None)
    uc = getattr(core, "_user_chrome", None)
    if uc is None or getattr(core, "keep_open", False):
        return
    try:
        uc.close(timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        _stderr_line(f"agent-browser serve: Chrome 종료 실패: {exc!r}")
