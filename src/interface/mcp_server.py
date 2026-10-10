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
from pathlib import Path
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
            "사람에게 조작권 요청(캡차·로그인). 창 serve(human·on-demand) 만. secret_wanted=true 면 "
            "관찰도 막힘. 다음: control_wait"
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
    # WS-38 동작 캐시(레시피). serve --no-recipes 면 목록에서 뺀다(build_server_tools).
    f"{TOOL_PREFIX}recipe": {
        "description": (
            "레시피=검증된 동작 묶음. save: 최근 통과 last_n 단계 저장(입력은 params). "
            "run: 관찰 data.recipes 후보 일괄 실행, 멈추면 reason 보고 직접 이어감. list·delete"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"enum": ["save", "run", "list", "delete"]},
                "id": {"type": "string", "description": "run·delete 대상 레시피 id"},
                "name": {"type": "string", "description": "save 할 이름"},
                "last_n": {"type": "integer", "minimum": 1, "maximum": 20,
                           "description": "save: 최근 통과 단계 수"},
                "params": {"type": "object", "description": "{이름: 값} — save 는 입력한 글자, run 은 새 값"},
                "pins": {"type": "object", "description": "save: {\"단계번호\": \"identity\"} (slot|ui|identity)"},
            },
            "required": ["op"],
        },
    },
}

#: 레시피 서버 도구 이름(WS-38).
RECIPE_TOOL = f"{TOOL_PREFIX}recipe"

#: MCP initialize 의 서버 instructions(WS-38 — 레시피 쓰는 법). serve --no-recipes 면 보내지 않는다.
RECIPE_INSTRUCTIONS = (
    "agent-browser: 같은 사이트에서 반복할 일은 성공한 뒤 browser_recipe save 로 저장하세요"
    "(입력 글자는 params).\n"
    "observe_page 결과 data.recipes 에 후보가 있으면 단계별로 하기 전에 browser_recipe run 을 먼저 "
    "시도하세요.\n"
    "run 이 멈추면(data.recipe.reason + 현재 관찰) 그 지점부터 평소대로 진행하세요. 재생도 승인·차단 "
    "관문을 그대로 지납니다."
)


def build_server_tools(recipes: bool = True) -> List[Dict[str, Any]]:
    """계약 밖 서버 도구 정의(WS-29). 액션 툴 목록(build_all_tools)과 합쳐 tools/list 가 된다.

    recipes=False(serve --no-recipes)면 레시피 도구(WS-38)를 뺀다.
    """
    return [{"name": name, **spec} for name, spec in SERVER_TOOLS.items()
            if recipes or name != RECIPE_TOOL]


def build_listed_tools(recipes: bool = True) -> List[Dict[str, Any]]:
    """MCP tools/list 전체 = 액션 툴 19종 + 서버 도구."""
    return build_all_tools() + build_server_tools(recipes)


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
from interface.handoff import SITE_NAME_LABEL  # noqa: E402


def code_overlay_text(code: str, kind: str, target: str, ttl_s: int) -> str:
    """창 오버레이에 띄우는 확인 코드 문구. target 은 사이트가 붙인 이름(살균된 것) — 6자리 이상 숫자열은
    여기서도 가린다(R1 NB-4)."""
    from interface.handoff import mask_code_like

    return (f"agent-browser 승인 확인 코드 {code}  (액션: {kind}, 대상: {SITE_NAME_LABEL} "
            f"{mask_code_like(target)}, {int(ttl_s)}초) — 터미널의 approve 화면과 대조해 입력")

#: WS-29 R2: 확인 코드를 띄우기 전, 이미 진행 중인 화면 캡처가 끝나길 기다리는 상한(초). 넘으면
#: 표시를 취소한다(코드 무효 — fail-closed). 무거운 페이지 SoM 캡처 실측 수백 ms 의 10배 이상.
CAPTURE_DRAIN_TIMEOUT_S = 5.0
#: WS-37: 조작권 반납 때 진행 중인 닫힌 탭 복구(CDP 세션 생성 등)를 기다리는 상한(초).
RELEASE_RECOVERY_WAIT_S = 10.0

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
        profile: Optional[str] = None,
        recipes: bool = True,
    ) -> None:
        from interface.handoff import HandoffHub, state_root

        #: 이름 붙인 영속 프로필(WS-32, serve --profile). None 이면 기존 동작(빈 컨텍스트).
        if profile is not None:
            from browser import serve_profile

            serve_profile.validate_name(profile)
            if browser_mode == "user-chrome":
                raise ValueError(
                    "--profile 과 --browser user-chrome 은 함께 쓸 수 없습니다 — user-chrome 은 "
                    "이미 전용 영속 프로필(--chrome-profile)을 씁니다."
                )
        self.profile = profile
        self._profile_lock: Any = None
        #: WS-38 동작 캐시(레시피). 기본 켬(serve --no-recipes 로 끔). --profile 이면 프로필 폴더의
        #: recipes.json(0600), 없으면 메모리만.
        self.recipes_enabled = bool(recipes)
        self._recipes: Any = None
        if self.recipes_enabled:
            from recipes.service import RecipeService

            self._recipes = RecipeService(self)
        #: WS-34 on-demand 창 상태(다른 방식이면 None — 기존 동작 그대로).
        from interface.on_demand import ON_DEMAND, WindowState

        self._window: Optional[WindowState] = (
            WindowState() if browser_mode == ON_DEMAND else None)
        #: on-demand 를 --profile 없이 띄우면 쓰는 서버 전용 임시 프로필 잠금(종료 때 폴더 삭제).
        self._ephemeral_lock: Any = None
        #: 창 전환 직렬화(WS-34): 전환 동안 새 도구 호출은 관문에서 기다리고, 전환은 진행 중인
        #: 호출(_inflight)이 끝나길 기다린다. 전환끼리는 _switch_lock 으로 한 번에 하나.
        self._gate = asyncio.Event()
        self._gate.set()
        self._gate_holds = 0
        self._inflight = 0
        self._switch_lock = asyncio.Lock()
        self._window_tasks: set = set()
        #: 마지막 전환 결과(window 정보)·전환 횟수 — control_wait 가 그사이 전환을 알아챈다.
        self._last_window_result: Optional[Dict[str, Any]] = None
        self._switch_seq = 0
        #: 에이전트가 아직 듣지 못한 창 전환·반납(WS-34) — 다음 control_wait/도구 결과에 한 번 싣는다.
        self._window_unreported = False
        self._release_unreported = False
        #: 다음 도구 결과에 실을 '실패 뒤 복구함' 표시.
        self._recovered_notice = False
        #: 조작권 요청 때의 탭 URL(창에서 해결한 사이트 판정용).
        self._request_urls: List[str] = []
        #: 현재 컨텍스트가 닫혔음(브라우저 종료 등 — 사람이 창을 닫은 경우 포함).
        self._ctx_closed = False
        #: 승인 확인 코드 별도 창(D1)과 코드가 떠 있는 곳("window"|"overlay").
        self._code_window: Any = None
        #: R1 NB-5: 진행 중인 wait_for 호출(창 전환이 취소해 E_TIMEOUT 으로 끝낸다).
        self._wait_calls: "set[asyncio.Task]" = set()
        self._code_where: Optional[str] = None

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
        #: WS-37: 진행 중인 닫힌 탭 복구(반납 처리). 감시 태스크가 반납을 먼저 집어 복구하는 동안
        #: 다른 호출(control_wait 등)이 복구가 끝나기 전에 '반납됨' 을 알리지 않도록 기다리는 데 쓴다.
        self._release_recovery: Optional["asyncio.Future[None]"] = None
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
        from security.robots_signal import RobotsSignals

        self._robots = RobotsSignals()  # WS-41: server-memory intent signal cache

    # -- 수명주기 -----------------------------------------------------------

    async def start(self) -> None:
        """브라우저 세션과 파이프라인을 초기화한다."""
        if self._started:
            return

        from browser import BrowserCore
        from security.egress_runtime import EgressRuntime

        # WS-32: 영속 프로필이면 먼저 잠근다 — 다른 serve 가 쓰는 중이면 아무것도 띄우지 않고 거부.
        persistent = self.acquire_profile()
        # WS-29b: 브라우저보다 검증 프록시를 먼저 띄운다 — 브라우저는 프록시로만 나간다.
        runtime = EgressRuntime(
            allowed_domains=self.allowed_domains,
            allow_private_network=self.allow_private_network,
            block_loopback=self.block_loopback,
            tokenless=self.browser_mode == "user-chrome",
        )
        try:
            await runtime.start()
        except BaseException:
            self.release_profile()
            raise
        self._egress_runtime = runtime
        try:
            core_kw: Dict[str, Any] = {}
            if persistent is not None:
                core_kw["persistent_profile"] = persistent
            # WS-34: on-demand 는 headless 로 시작하는 영속 컨텍스트 — 창은 전환 때 다시 열어 띄운다.
            on_demand_mode = self._window is not None
            core = BrowserCore(
                headless=True if on_demand_mode else self.headless,
                browser_mode="headless" if on_demand_mode else self.browser_mode,
                chrome_profile=self.chrome_profile,
                keep_open=self.keep_open,
                egress=runtime,
                **core_kw,
            )
            self._core = await core.start()
        except BaseException:
            await self._close_egress()
            self.release_profile()
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
            self.release_profile()
            raise
        self._started = True
        self.open_handoff()
        self._sync_window()
        logger.info(
            "MCP 브라우저 세션 시작 (mode=%s, browser=%s)", self.mode.value, self.browser_mode
        )

    # -- 영속 프로필 (WS-32) ---------------------------------------------------

    def acquire_profile(self) -> Optional[Path]:
        """--profile 이면 프로필 폴더를 만들고(0700) 이 서버 id 로 잠근다. 폴더 경로를 돌려준다.

        이미 잡았으면 그대로. 다른 serve(또는 Chromium)가 쓰는 중이면 ProfileInUseError —
        사람이 읽을 이유(어느 서버 id)가 담긴다. --profile 이 없으면 None(기존 동작).
        WS-34: on-demand 를 --profile 없이 띄우면 서버 전용 임시 프로필(0700, 종료 때 삭제)을 쓴다 —
        그 전에 비정상 종료로 남은 임시 프로필(잠금 풀린 것)을 지운다.
        """
        if self.profile is None:
            if self._window is None:
                return None
            if self._ephemeral_lock is not None and self._ephemeral_lock.held:
                return self._ephemeral_lock.path
            from browser import serve_profile

            serve_profile.cleanup_ephemeral()
            self._ephemeral_lock = serve_profile.acquire_ephemeral(server_id=self.hub.server_id)
            return self._ephemeral_lock.path
        if self._profile_lock is not None and self._profile_lock.held:
            return self._profile_lock.path
        from browser import serve_profile

        # R1 NB-6: --profile 서버도 비정상 종료로 남은 임시 프로필(잠금 풀린 것만)을 지운다.
        serve_profile.cleanup_ephemeral()
        self._profile_lock = serve_profile.acquire(self.profile, server_id=self.hub.server_id)
        return self._profile_lock.path

    def release_profile(self) -> None:
        lock, self._profile_lock = self._profile_lock, None
        if lock is not None:
            lock.release()
        eph, self._ephemeral_lock = self._ephemeral_lock, None
        if eph is not None:
            from browser import serve_profile

            serve_profile.remove_ephemeral(eph)

    def _profile_info(self) -> Optional[Dict[str, Any]]:
        """에이전트에게 보이는 프로필 상태 — 이름만(경로는 넣지 않는다)."""
        if self.profile is None:
            return None
        return {"name": self.profile, "persistent": True}

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
        if events and self._recipes is not None and ("taken" in events or "released" in events):
            self._recipes.reset("human_control")  # 사람 조작 구간에서 궤적을 끊는다(WS-38)
        for event in events:
            if event == "released":
                self._release_unreported = True
                # 사람이 화면을 바꿨을 수 있다 — 기존 element_id 를 모두 무효로.
                if self._engine is not None:
                    self._engine.bump_epoch("control_release")
                if self._window is not None:
                    # WS-34 D2: 창을 닫고 headless 로 다시 연다(sticky 면 창 유지). 탭은 다시 열려
                    # 복원되므로 닫힌 탭 복구(NB-3)는 필요 없다.
                    self._on_demand_released()
                else:
                    await self._recover_closed_tab_serialized()
                await self._show_control_banner(None)
                _stderr_line("agent-browser serve: 조작권 반납됨 — 에이전트가 이어서 진행합니다")
            elif event == "taken":
                if self._window is not None:
                    self._window.opened_for_request = None
                    if self._window.state == "headless" and self._started:
                        # 요청 없이 사람이 바로 take — 조작할 창이 필요하다.
                        self._spawn_switch(True, "taken")
                await self._show_control_banner(
                    "사람이 조작 중 — 끝나면 `agent-browser control release`")
                _stderr_line("agent-browser serve: 조작권을 사람이 가져감")
        if self._window is not None:
            await self._watch_window()
        await self._sync_code_overlay()
        # WS-37: 다른 태스크(감시 태스크)가 반납을 먼저 집어 복구 중이면 끝날 때까지 기다린다 —
        # 이 함수가 돌아온 뒤 호출자가 보는 상태(반납·탭 알림)가 어긋나지 않게.
        await self._await_release_recovery()
        return events

    async def _recover_closed_tab_serialized(self) -> None:
        """닫힌 탭 복구를 Future 로 감싸 진행 중임을 알린다(WS-37 반납 알림 경쟁 조건).

        Future 는 첫 await 전에 만든다 — hub.poll() 이 'released' 를 돌려준 같은 동기 구간 안이라,
        hub.version 이 바뀐 것을 본 다른 태스크는 반드시 이 Future 도 본다.
        """
        fut: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        self._release_recovery = fut
        try:
            await self._recover_closed_tab()
        finally:
            if not fut.done():
                fut.set_result(None)
            if self._release_recovery is fut:
                self._release_recovery = None

    async def _await_release_recovery(self) -> None:
        """진행 중인 닫힌 탭 복구가 있으면 끝날 때까지(상한 RELEASE_RECOVERY_WAIT_S) 기다린다."""
        deadline = time.monotonic() + RELEASE_RECOVERY_WAIT_S
        while True:
            fut = self._release_recovery
            if fut is None or fut.done():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("닫힌 탭 복구가 %.0f초 안에 끝나지 않음", RELEASE_RECOVERY_WAIT_S)
                return
            try:
                await asyncio.wait_for(asyncio.shield(fut), remaining)
            except asyncio.TimeoutError:
                logger.warning("닫힌 탭 복구가 %.0f초 안에 끝나지 않음", RELEASE_RECOVERY_WAIT_S)
                return

    # -- 확인 코드 오버레이 (WS-29 R1) ------------------------------------------

    async def _sync_code_overlay(self) -> None:
        """hub 가 내준 확인 코드를 창 오버레이에 띄우고, 만료·입력·폐기되면 내린다.

        코드는 **창 오버레이에만** 간다 — MCP 응답·stderr·로그·상태 파일에는 쓰지 않는다. 표시가
        실패하면 hub 에 알려 코드를 무효로 한다(창에 없는 코드로는 승인할 수 없다).
        """
        if self._banner is not None and self._banner_gone():
            # R3 NB-2: 오버레이를 띄운 탭이 닫혔다 — 오버레이는 탭과 함께 사라졌다. 오버레이는 이
            # 세션 하나에만 띄우므로(_set_banner) 다른 탭에 코드가 남았을 수 없다. 창에 없는 코드는
            # 무효로 한다(사람은 approve 를 다시 실행해 지금 탭에 새 코드를 띄운다).
            self._banner = None
            if self._code_on_overlay and self._code_where != "window":
                self.hub.withdraw_codes()
        job = self.hub.take_code_job()
        if job is not None:
            shown = False
            busy = False
            if self._human_can_see():
                from interface.handoff import CODE_TTL_S, display_safe, mask_code_like

                ap = self.hub.approvals.get(job.approval_id)
                raw_target = (ap.summary or {}).get("target", "") if ap else ""
                target = display_safe(mask_code_like(str(raw_target or "")), 40)
                kind = display_safe((ap.components or {}).get("action", "") if ap else "", 20)
                # (a) 표시 예정: 표시 시도 전에 켠다 — 새 화면 캡처는 이때부터 거부(fail-closed).
                self._code_on_overlay = True
                # (b) 이미 진행 중인 캡처가 끝나야 띄운다. 상한을 넘으면 표시 취소(코드 무효).
                if await self._wait_captures_idle():
                    # (c) 오버레이를 켜기 직전 세대를 올린다 — 그 사이 끝나지 않은 캡처는 (d) 에서 버린다.
                    self._pixel_gen += 1
                    if self._code_in_window():
                        # WS-34 D1: on-demand 가 headless 면 페이지는 그대로 두고 별도 작은 창에.
                        self._code_where = "window"
                        shown = await self._show_code_window(job, ap, kind, target)
                    else:
                        self._code_where = "overlay"
                        shown = bool(await self._set_banner(
                            code_overlay_text(job.code, kind, target, int(CODE_TTL_S))))
                else:
                    busy = True
                    logger.warning("진행 중 화면 캡처가 끝나지 않아 확인 코드 표시를 취소함")
            self.hub.code_shown(job.nonce, shown, reason="capture_busy" if busy else None)
            del job  # 평문을 붙잡지 않는다
        if (self._code_on_overlay and self._code_where == "window" and self._code_window is not None
                and self._code_window.closed_externally):
            # R1 NB-3: 사람이 별도 코드 창을 닫았다 — 창에 없는 코드는 무효(오버레이 탭이 닫힐 때와 같은
            # 원칙). 아래에서 캡처 거부를 푼다. 사람은 approve 를 다시 실행해 새 창·새 코드를 받는다.
            self.hub.withdraw_codes()
        if self._code_on_overlay and not self.hub.code_displayed():
            if self._code_where == "window":
                # 별도 창을 닫았음이 확인돼야 화면 캡처 거부를 푼다(fail-closed).
                if await self._close_code_window():
                    self._code_on_overlay = False
                    self._code_where = None
            # 조작권 안내로 되돌린다. 그게 안 되면(예: 띄울 활성 탭이 없음) 코드만이라도 지운다 —
            # 지운 것이 확인돼야(또는 오버레이 탭이 사라져야) 화면 캡처 거부를 푼다(fail-closed).
            elif await self._set_banner(self._control_banner) or (
                    self._control_banner and await self._set_banner(None)):
                self._code_on_overlay = False
                self._code_where = None

    def _code_in_window(self) -> bool:
        """확인 코드를 별도 창에 띄우는가(WS-34 D1: on-demand 이고 지금 창이 없을 때)."""
        return self._window is not None and self._window.state != "headed"

    async def _show_code_window(self, job: Any, ap: Any, kind: str, target: str) -> bool:
        from interface.code_window import ApprovalCodeWindow
        from interface.handoff import CODE_TTL_S, display_safe

        if self._code_window is None:
            self._code_window = ApprovalCodeWindow()
        comps = (ap.components or {}) if ap is not None else {}
        return await self._code_window.show(
            getattr(self._core, "_playwright", None),
            action=kind, target=target or "(이름 없음)",
            origin=display_safe(comps.get("origin") or "", 80),
            expires_at=_iso(ap.expires) if ap is not None else "",
            code=job.code, ttl_s=int(CODE_TTL_S),
        )

    async def _close_code_window(self) -> bool:
        win = self._code_window
        return True if win is None else await win.close()

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
        """사람이 볼 브라우저 창이 있는가(headless 면 없다). on-demand 는 필요할 때 창을 연다(WS-34)."""
        if self._window is not None:
            return True
        return self.browser_mode != "headless" or not self.headless

    def _banner_gone(self) -> bool:
        """오버레이를 띄운 탭이 닫혔음이 **확인**되는가(R3 NB-2). 페이지를 모르거나 확인이 안 되면
        False — 오버레이가 남았을 수 있다고 본다(fail-closed)."""
        page = self._banner[2] if self._banner is not None and len(self._banner) > 2 else None
        if page is None:
            return False
        try:
            return bool(page.is_closed())
        except Exception:  # noqa: BLE001
            return False

    async def _set_banner(self, text: Optional[str]) -> bool:
        """창 위 안내 띠(DevTools 오버레이 — 페이지 DOM 을 바꾸지 않고 페이지 스크립트가 읽거나
        누를 수 없다). 성공하면 True. 창이 없거나 실패하면 False(안내는 stderr 에도 나간다).

        지울 때는 띄웠던 그 탭의 세션으로 지운다(활성 탭이 바뀌었어도 옛 탭에 남지 않게). 띄우기 전
        DOM.enable 이 필요하다(Chromium: 'DOM should be enabled first').
        주의: headed 창에서는 이 오버레이가 Page 스크린샷에 찍힌다(R1 실측) — 확인 코드가 떠 있는
        동안 화면 캡처를 거부하는 이유(_pixels_blocked).

        R3 NB-2: 띄운 탭이 닫혔으면(전송 실패 뒤 페이지 닫힘 확인) 오버레이는 탭과 함께 사라졌다 —
        지우기는 성공, 띄우기는 현재 활성 탭의 새 세션으로 (한 번 더) 시도한다. 탭이 살아 있는데 전송이
        실패하면 여전히 False(오버레이가 남았을 수 있다).
        """
        if not self._human_can_see() or self._core is None:
            return False
        if self._window is not None and self._window.state != "headed":
            return False  # WS-34: on-demand 가 headless(또는 전환 중)면 띄울 창이 없다
        for _attempt in range(2):
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
                    get_tab = getattr(self._core, "get_tab", None)
                    tab = get_tab(tab_id) if callable(get_tab) else None
                    self._banner = (tab_id, await self._core.new_cdp_session(tab_id),
                                    getattr(tab, "page", None))
                cdp = self._banner[1]
                await cdp.send("DOM.enable")
                await cdp.send("Overlay.enable")
                await cdp.send("Overlay.setPausedInDebuggerMessage", {"message": text})
                return True
            except Exception:  # noqa: BLE001
                logger.debug("안내 띠 표시 실패", exc_info=True)
                if self._banner is not None and self._banner_gone():
                    # 보내는 도중 탭이 닫혔다 — 오버레이도 함께 사라졌다.
                    self._banner = None
                    if not text:
                        return True
                    continue  # 띄우기: 현재 활성 탭으로 한 번 더
                return False
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
        if name == RECIPE_TOOL:
            return await self._recipe_tool(args)
        if not self.hub.opened:
            self.open_handoff()
        if not self.hub.opened:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": "사람 인계 통로(상태 디렉터리)를 열 수 없습니다 — 서버 stderr 참조."}
        await self._poll_handoff()
        short = name[len(TOOL_PREFIX):]
        if short == "control_status":
            data: Dict[str, Any] = {"control": self.hub.status()}
            data["control"].pop("window", None)
            if self.profile is not None:
                data["profile"] = self._profile_info()
            if self._window is not None:
                data["window"] = self._window.info()
            if self._tab_notice is not None:
                data["tab_closed_by_human"] = self._tab_notice
            return {"success": True, "data": data}
        if short == "control_request":
            return await self._control_request(args)
        timeout = _wait_timeout(args.get("timeout_s"))
        if short == "control_wait":
            return await self._control_wait(timeout)
        return await self._approval_wait(str(args.get("approval_id") or ""), timeout)

    async def _recipe_tool(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """browser_recipe(WS-38). 응답 data 도 IPI 신호 경로를 거친다(레시피 이름·중단 사유·관찰)."""
        if self._recipes is None:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": "레시피가 꺼져 있습니다(serve --no-recipes)."}
        payload = await self._recipes.tool(args)
        data = payload.get("data")
        if isinstance(data, dict) and "injection_suspected" not in data:
            from security.injection_signal import injection_signal

            signal = injection_signal(data, None, 10 * self.max_result_chars)
            if signal is not None:
                data["injection_suspected"] = signal
        return payload

    def _recipe_page(self) -> Any:
        """레시피 기록·재생 대상 페이지. 프레임 안이면 None(지원하지 않음)."""
        ctx = getattr(self._dispatcher, "ctx", None)
        if ctx is None or getattr(ctx, "root_page", None) is not None:
            return None
        return getattr(ctx, "page", None)

    async def _control_request(self, args: Dict[str, Any]) -> Dict[str, Any]:
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                    "error_message": "reason 이 필요합니다."}
        if not self._human_can_see():
            # 사람이 볼 창이 없다 — 운영자가 창 보이는 방식으로 띄워야 한다(우회 아님).
            message = (
                "headless 서버라 사람이 볼 창이 없습니다. 운영자에게 `agent-browser serve "
                "--browser on-demand`(필요할 때만 창) · `--browser human` · `--browser user-chrome` "
                "중 하나로 다시 띄우도록 요청하세요."
            )
            data: Dict[str, Any] = {"control": self.hub.status(), "browser_mode": self.browser_mode}
            if self.profile is not None:
                # WS-32: 영속 프로필이면 창 있는 서버로 한 번 로그인해 두면 headless 에서도 유지된다.
                message += (
                    f" 이 서버는 영속 프로필 {self.profile!r} 을 씁니다 — 운영자가 이 서버를 끝내고 "
                    f"`agent-browser serve --browser human --profile {self.profile}` 으로 한 번 "
                    "로그인하면 이후 headless 에서도 로그인이 유지됩니다."
                )
                data["profile"] = self._profile_info()
            return {
                "success": False,
                "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                "error_message": message,
                "data": data,
            }
        if not self._started:
            await self.start()
        window_out: Optional[Dict[str, Any]] = None
        if self._window is not None:
            window_out = await self._open_window_for_request()
            if not window_out.get("ok"):
                return {
                    "success": False,
                    "error_code": ErrorCode.PAGE_CRASHED.value,
                    "error_message": (
                        "사람에게 보일 창을 열지 못해 조작권을 요청하지 않았습니다"
                        f"({window_out.get('error') or '창 전환 실패'}). "
                        + ("브라우저를 닫았습니다 — 다음 도구 호출 때 headless 로 다시 엽니다. "
                           if self._window.state == "failed" else "")
                        + "잠시 뒤 다시 요청하거나 운영자에게 알리세요."),
                    "data": {"window": window_out.get("window") or self._window.info()},
                }
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
        if window_out is not None:
            out = dict(out, window=window_out["window"])
        return {"success": True, "data": out}

    async def _open_window_for_request(self) -> Dict[str, Any]:
        """on-demand: 조작권 요청 때 창을 연다(이미 창이면 그대로). sticky_pending 이면 sticky 로 확정."""
        from interface.on_demand import HEADED_HINT

        w = self._window
        assert w is not None
        w.take_sticky_for_request()
        self._request_urls = [str(getattr(t.page, "url", "") or "") for t in self._core.tabs()] \
            if self._core is not None else []
        if w.state == "headed":
            self._sync_window()
            info = dict(w.info(), reopened=False)
            return {"ok": True, "window": info}
        out = await self._switch_window(True, "control_request")
        if out.get("ok"):
            self._window_unreported = False  # 이 응답으로 알린다
            w.opened_for_request = time.monotonic()
            w.notice = None
            info = dict(out["window"])
            info.update(w.info())
            info.setdefault("hint", HEADED_HINT)
            out["window"] = info
            self._sync_window()
        return out

    async def _control_wait(self, timeout: float) -> Dict[str, Any]:
        st = self._control_view()
        # WS-34: on-demand 에서는 감시 태스크가 반납을 먼저 처리하고 창 전환을 시작했을 수 있다 —
        # 에이전트가 아직 듣지 못한 반납이면 그 반납(과 전환 결과)을 알린다.
        pre_released = self._window is not None and self._release_unreported
        if st["holder"] == "agent" and not st["requested"] and not pre_released:
            data0: Dict[str, Any] = {"control": st, "changed": "none_pending", "hint": _RELEASE_HINT}
            if self._window is not None:
                data0["window"] = self._take_window_report()
            return {"success": True, "data": data0}
        start_version = self.hub.version
        deadline = time.monotonic() + timeout
        changed = "released" if pre_released else "timeout"
        while changed == "timeout" and time.monotonic() < deadline:
            events = await self._poll_handoff()
            hit = [e for e in events if e in ("taken", "released")]
            if hit:
                changed = hit[-1]
                break
            if self.hub.version != start_version and self.hub.last_event in ("taken", "released"):
                changed = self.hub.last_event
                break
            await asyncio.sleep(0.05)
        window: Optional[Dict[str, Any]] = None
        if self._window is not None:
            # WS-34: 반납(또는 take)으로 창 전환이 시작됐으면 끝날 때까지 기다린다 — 다시 연 브라우저를
            # 에이전트가 바로 쓸 수 있게.
            from interface.on_demand import SWITCH_WAIT_S

            await self._await_gate(SWITCH_WAIT_S)
            window = self._take_window_report()
        data: Dict[str, Any] = {"control": self._control_view(), "changed": changed}
        if changed == "released":
            # WS-37: 감시 태스크가 복구 중이었다면 위 _poll_handoff 가 그 복구를 기다린 뒤 돌아왔다
            # (복구 Future) — 창이 없는(복구를 하는) 경로에서는 그 뒤로 await 가 없으므로 아래
            # tab_closed_by_human 은 빠지지 않는다.
            self._release_unreported = False
            data["hint"] = _RELEASE_HINT
            data["snapshot_epoch"] = self._engine.epoch if self._engine else 0
            if self._tab_notice is not None:
                data["tab_closed_by_human"], self._tab_notice = self._tab_notice, None
        if window is not None:
            data["window"] = window
        return {"success": True, "data": data}

    def _attach_window_report(self, result: ActionResult) -> None:
        """도구 결과에 아직 알리지 않은 창 전환(다시 열림·실패 뒤 복구)을 싣는다 — control_wait 를
        부르지 않는 에이전트도 '페이지가 새로 열렸으니 다시 관찰' 을 듣게."""
        from interface.on_demand import HEADLESS_HINT

        report = self._take_window_report()
        if self._recovered_notice:
            self._recovered_notice = False
            report.update(recovered=True,
                          hint="창 전환이 실패해 브라우저를 headless 로 다시 열었습니다. " + HEADLESS_HINT)
        existing = result.data.get("window")
        result.data["window"] = {**report, **existing} if isinstance(existing, dict) else report

    def _control_view(self) -> Dict[str, Any]:
        """에이전트에게 보이는 조작권 상태(창 상태는 data.window 로 따로 싣는다)."""
        st = self.hub.status()
        st.pop("window", None)
        return st

    def _take_window_report(self) -> Dict[str, Any]:
        """에이전트가 아직 듣지 못한 창 전환이 있으면 그 결과(reopened=true, 탭 복원 내역)를 한 번
        돌려주고, 없으면 현재 상태(reopened=false)."""
        assert self._window is not None
        if self._window_unreported and self._last_window_result is not None:
            self._window_unreported = False
            out = dict(self._last_window_result)
            out.update(self._window.info())
            return out
        return dict(self._window.info(), reopened=False)

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
        from perception import PerceptionEngine
        from security import HITLGate

        self._engine = PerceptionEngine()
        await self._wire_session()
        self._hitl = HITLGate(
            mode=self.mode, pre_approved_actions=self.pre_approved_actions
        )

    async def _wire_session(self, restore: Optional[List[Dict[str, Any]]] = None,
                            cookies: Optional[List[Dict[str, Any]]] = None) -> tuple:
        """열린 브라우저에 서버 연결을 단다 — 처음 시작과 창 전환 뒤(WS-34)가 **같은 경로**.

        컨텍스트 채택 → 문서 상태 추적(PageDocumentStatus) → 첫 탭(about:blank)·CDP·디스패처 → Egress
        route 가드(어떤 페이지 이동보다 먼저) → (전환이면) 탭 복원. 검증 프록시는 브라우저 실행 인자
        (BrowserCore._launch_kwargs)로 이미 묶였다.
        restore(창 전환) 가 있으면 그 탭 URL 들을 다시 연다(활성 탭 먼저, http(s) 만).
        반환: (탭 복원 내역, 건너뛴 탭 수).
        """
        from actions import ActionDispatcher, DispatchContext
        from browser.doc_status import PageDocumentStatus

        await self._core.new_context("mcp-session")
        context = self._core.context_for("mcp-session")
        # 새 탭·팝업 문서 응답도 잡도록 context 단위로 달되, 응답을 온 탭에 묶는다
        # (WS-26b: 팝업 403 이 원래 탭 판정에 새지 않게).
        self._page_status = PageDocumentStatus()
        self._page_status.attach(context)
        tab = await self._core.new_tab("mcp-session")
        self._page = tab.page
        self._cdp = await self._core.new_cdp_session(tab.tab_id)

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
        # WS-34: 어떤 페이지 이동(탭 복원 포함)보다 먼저 단다 — 다시 연 브라우저도 처음부터 가드 안.
        self._egress = self._egress_runtime.guard
        await self._egress.install(context)
        if self._window is not None:
            self._watch_context_close(context)
        if cookies:
            try:
                await context.add_cookies(cookies)
            except Exception:  # noqa: BLE001 - 세션 쿠키를 못 넣어도 전환은 계속(영속 쿠키는 남음)
                logger.warning("세션 쿠키 복원 실패", exc_info=True)
        if restore is None:
            return [], 0
        return await self._restore_tabs(tab, restore)

    async def _restore_tabs(self, first: Any, snapshot: List[Dict[str, Any]]) -> tuple:
        """창 전환 뒤 탭 URL 복원(WS-34). 활성 탭을 첫 탭에 먼저, 나머지는 새 탭에. about:blank·
        chrome:// 등(http(s) 아님)은 건너뛴다. 이동 실패는 그 탭만 restored=false(이유)로 알린다."""
        from browser.serve_profile import safe_reason
        from interface import on_demand
        from urllib.parse import urlsplit

        order = sorted(snapshot, key=lambda t: not t.get("active"))
        tabs_info: List[Dict[str, Any]] = []
        skipped = 0
        for item in order:
            url = str(item.get("url") or "")
            if not on_demand.restorable_url(url):
                skipped += 1
                continue
            tab = first if not tabs_info else await self._core.new_tab("mcp-session")
            entry: Dict[str, Any] = {"tab_id": tab.tab_id, "was": item.get("tab_id"), "url": url,
                                     "active": not tabs_info, "restored": False}
            egress = self._egress
            up_before = egress.upstream_total if egress is not None else 0
            try:
                resp = await tab.page.goto(url, wait_until="domcontentloaded",
                                           timeout=on_demand.RESTORE_NAV_TIMEOUT_MS)
                status = getattr(resp, "status", None)
                if status is not None:
                    entry["http_status"] = status
                fails = egress.upstream_failures_since(up_before) if egress is not None else []
                parts = urlsplit(url)
                key = ((parts.hostname or "").lower(),
                       parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower()))
                fails = [f for f in fails if (f.host.lower().strip("[]"), f.port) == key]
                if fails:
                    # 프록시가 업스트림에 닿지 못해 만든 502 문서 — 복원한 것이 아니다(WS-29b R1 과 같은 판정).
                    entry["error"] = f"업스트림 접속 실패({fails[-1].to_agent().get('code')})"
                else:
                    entry["restored"] = True
            except Exception as exc:  # noqa: BLE001 - 한 탭 실패로 전환 전체를 실패시키지 않는다
                entry["error"] = safe_reason(exc)
            tabs_info.append(entry)
        self._core.set_active_tab(first.tab_id)
        return tabs_info, skipped

    # -- 필요할 때만 창 (WS-34) ---------------------------------------------------

    def _sync_window(self) -> None:
        """창 상태를 사람용 상태 파일(control.json — `agent-browser control status`)에도 싣는다."""
        if self._window is not None:
            self.hub.set_window(self._window.info())

    def _hold_gate(self) -> None:
        self._gate_holds += 1
        self._gate.clear()

    def _release_gate(self) -> None:
        self._gate_holds = max(0, self._gate_holds - 1)
        if self._gate_holds == 0:
            self._gate.set()

    async def _await_gate(self, timeout: Optional[float] = None) -> bool:
        """창 전환이 끝날 때까지 기다린다. 상한 안에 끝나면 True."""
        if self._gate.is_set():
            return True
        try:
            await asyncio.wait_for(self._gate.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def _drain_calls(self, timeout: Optional[float] = None) -> bool:
        """진행 중인 도구 호출이 끝나길 기다린다(전환과 겹치지 않게). 상한 안에 끝나면 True."""
        from interface import on_demand

        limit = on_demand.DRAIN_TIMEOUT_S if timeout is None else timeout
        deadline = time.monotonic() + limit
        # R1 NB-5: wait_for 는 timeout_ms 상한이 없다 — 잠깐(WAIT_CANCEL_AFTER_S, 상한의 절반 이하) 기다려도
        # 남아 있으면 멈춘다(E_TIMEOUT 결과로 돌아감). 다른 호출은 지금처럼 끝나길 기다린다.
        cancel_at = time.monotonic() + min(on_demand.WAIT_CANCEL_AFTER_S, limit / 2)
        while self._inflight > 0:
            now = time.monotonic()
            if now >= deadline:
                return False
            if now >= cancel_at:
                for task in list(self._wait_calls):
                    task.cancel()
            await asyncio.sleep(0.01)
        return True

    def _spawn_switch(self, headed: bool, reason: str,
                      snapshot: Optional[List[Dict[str, Any]]] = None) -> None:
        """감시 경로(사건 처리)에서 전환을 시작한다. 관문은 **지금** 닫는다 — 이 뒤에 들어오는 도구
        호출은 전환이 끝날 때까지 기다린다(태스크가 늦게 돌아도)."""
        self._hold_gate()

        async def _run() -> None:
            try:
                await self._switch_window(headed, reason, snapshot=snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 실패는 _switch_window 가 상태로 남긴다
                logger.warning("창 전환 실패(%s)", reason, exc_info=True)
            finally:
                self._release_gate()

        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            self._release_gate()
            return
        self._window_tasks.add(task)
        task.add_done_callback(self._window_tasks.discard)

    def _tab_snapshot(self) -> List[Dict[str, Any]]:
        core = self._core
        tabs = core.tabs() if core is not None else []
        if not tabs:
            return list(self._window.last_tabs) if self._window is not None else []
        active = core.active_tab_id
        return [{"tab_id": t.tab_id, "url": str(getattr(t.page, "url", "") or ""),
                 "active": t.tab_id == active} for t in tabs]

    async def _switch_window(self, headed: bool, reason: str, *, force: bool = False,
                             snapshot: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """같은 프로필을 닫고 headless=not headed 로 다시 연 뒤 서버 연결을 모두 다시 단다(WS-34).

        순서: 관문 닫기(새 호출 대기) → 진행 중 호출 끝나길 대기 → 탭 URL·활성 탭 기록 → 영속 컨텍스트
        닫기 → 같은 프로필로 다시 열기(검증 프록시 실행 인자 포함) → 연결 재설치(_wire_session: 문서 상태·
        route 가드·탭·CDP·디스패처) → 탭 복원 → epoch 올림 → (창이면) 안내 띠. 프로필 잠금은 내내 유지.
        실패하면 fail-closed: 브라우저를 닫고 state=failed — 다음 도구 호출이 headless 로 다시 연다.
        반환: {ok, reopened, window, error?}.
        """
        from browser.serve_profile import safe_reason
        from interface.on_demand import HEADED_HINT, HEADLESS_HINT

        w = self._window
        assert w is not None
        target = "headed" if headed else "headless"
        self._hold_gate()
        try:
            async with self._switch_lock:
                if w.state == target and not force:
                    return {"ok": True, "reopened": False, "window": dict(w.info(), reopened=False)}
                if self._core is None:
                    return {"ok": False, "reopened": False, "error": "브라우저가 시작되지 않음",
                            "window": w.info()}
                if not await self._drain_calls():
                    return {"ok": False, "reopened": False,
                            "error": "진행 중인 도구 호출이 끝나지 않아 창을 전환하지 못함",
                            "window": w.info()}
                snap = snapshot if snapshot is not None else self._tab_snapshot()
                w.state = "switching"
                self._sync_window()
                t0 = time.perf_counter()
                try:
                    tabs_info, skipped = await self._reopen_browser(not headed, snap)
                except BaseException as exc:
                    await self._fail_closed(safe_reason(exc), snap)
                    if not isinstance(exc, Exception):
                        raise  # 취소(SIGTERM 등) — 정리했으니 그대로 올린다
                    return {"ok": False, "reopened": False, "error": safe_reason(exc),
                            "window": w.info()}
                ms = round((time.perf_counter() - t0) * 1000, 1)
                w.state = target
                w.error = ""
                w.last_tabs = [] if headed else w.last_tabs
                if self._engine is not None:
                    self._engine.bump_epoch("window_reopen")
                self._switch_seq += 1
                info = dict(w.info(), reopened=True, reason=reason, switch_ms=ms,
                            tabs=tabs_info, skipped=skipped,
                            hint=HEADED_HINT if headed else HEADLESS_HINT)
                if self._engine is not None:
                    info["snapshot_epoch"] = self._engine.epoch
                self._last_window_result = info
                self._window_unreported = True
                self._sync_window()
                if headed and self._control_banner:
                    await self._set_banner(self._control_banner)
                _stderr_line(f"agent-browser serve: 창 {'열림' if headed else '닫힘(headless 복귀)'} "
                             f"({reason}, {ms:g}ms, 탭 {len(tabs_info)}개 복원)")
                return {"ok": True, "reopened": True, "window": info}
        finally:
            self._release_gate()

    async def _reopen_browser(self, headless: bool, snapshot: List[Dict[str, Any]]) -> tuple:
        core = self._core
        # R1 NB-2: 다시 열 실행 인자를 먼저 만든다 — 검증 프록시가 실제로 듣고 있지 않으면 여기서
        # 예외(전환하지 않음 → _switch_window 가 fail-closed: 브라우저 닫고 state=failed + 이유).
        core._launch_kwargs()
        # 창을 닫으면 그 창의 오버레이(확인 코드·안내 띠)도 함께 사라진다 — 창에 없는 코드는 무효.
        self._banner = None
        if self._code_where == "overlay":
            self.hub.withdraw_codes()
            self._code_on_overlay = False
            self._code_where = None
        self._ctx_closed = False
        # 세션 쿠키(만료 없음)는 Chromium 이 브라우저를 다시 열 때 버린다(WS-32 실측) — 이 서버가 이미
        # 가진 세션 쿠키만 그대로 다시 넣는다(사이트가 준 값 그대로, 수명·값을 바꾸지 않음).
        session_cookies = await self._session_cookies()
        await core.close_persistent()
        await core.open_persistent(headless=headless)
        return await self._wire_session(restore=snapshot, cookies=session_cookies)

    async def _session_cookies(self) -> List[Dict[str, Any]]:
        ctx = self._core.context_for("mcp-session") if self._core is not None else None
        if ctx is None or self._ctx_closed:
            return []
        try:
            cookies = await ctx.cookies()
        except Exception:  # noqa: BLE001 - 창이 이미 닫힘 등 — 영속 쿠키는 폴더에 남는다
            return []
        return [c for c in cookies if float(c.get("expires", -1) or -1) < 0]

    async def _fail_closed(self, reason: str, snapshot: List[Dict[str, Any]]) -> None:
        """전환 실패: 보호가 덜 걸렸을 수 있는 브라우저를 남기지 않는다(닫음). state=failed."""
        w = self._window
        assert w is not None
        w.state = "failed"
        w.error = reason
        if snapshot:
            w.last_tabs = list(snapshot)
        self._banner = None
        try:
            await self._core.close_persistent()
        except BaseException:  # noqa: BLE001
            logger.warning("전환 실패 뒤 브라우저 닫기 실패", exc_info=True)
        self._sync_window()
        _stderr_line(f"agent-browser serve: 창 전환 실패 — 브라우저를 닫았습니다(다음 도구 호출 때 "
                     f"headless 로 다시 엽니다): {reason}")

    def _watch_context_close(self, context: Any) -> None:
        on = getattr(context, "on", None)
        if not callable(on):
            return

        def _closed(*_a: Any) -> None:
            if self._core is not None and self._core.context_for("mcp-session") is context:
                self._ctx_closed = True

        on("close", _closed)

    def _on_demand_released(self) -> None:
        """사람이 조작권을 돌려줬다(D2): 창에서 해결한 사이트를 기록하고, sticky 가 아니면 headless 로."""
        w = self._window
        assert w is not None
        w.opened_for_request = None
        if w.state not in ("headed", "switching"):
            return
        urls = [str(getattr(t.page, "url", "") or "") for t in self._core.tabs()] \
            if self._core is not None else []
        w.note_solved(urls + list(self._request_urls))
        if not w.keep_window_after_release():
            self._spawn_switch(False, "released")
        else:
            self._sync_window()

    async def _watch_window(self) -> None:
        """창이 열린 동안: 마지막 탭 URL 기록, 사람이 창을 닫았는지, 요청 만료를 본다."""
        from interface import on_demand

        w = self._window
        if w is None or w.state != "headed" or self._switch_lock.locked() or self._core is None:
            return
        if self._gate_holds > 0:
            # R1 NB-5: 이미 시작한 전환(태스크가 아직 첫 틱 전)이 있다 — 다시 판정·spawn 하지 않는다.
            return
        tabs = self._core.tabs()
        if tabs and not self._ctx_closed:
            active = self._core.active_tab_id
            w.last_tabs = [{"tab_id": t.tab_id, "url": str(getattr(t.page, "url", "") or ""),
                            "active": t.tab_id == active} for t in tabs]
            opened = w.opened_for_request
            st = self.hub.status()
            if (opened is not None and st["holder"] == "agent" and st["requested"]
                    and time.monotonic() - opened > on_demand.REQUEST_WINDOW_TTL_S):
                # 사람이 take 하지 않았다 — 요청을 거두고 창도 닫는다.
                w.opened_for_request = None
                self.hub.cancel_request()
                w.notice = {"reason": "request_expired",
                            "hint": "사람이 조작권을 가져가지 않아 요청을 거두고 창을 닫았습니다."}
                _stderr_line("agent-browser serve: 조작권 요청 만료 — 창을 닫습니다")
                if not w.keep_window_after_release():
                    self._spawn_switch(False, "request_expired")
                else:
                    self._sync_window()
            return
        # 사람이 창을 직접 닫았다(창의 탭이 모두 닫힘·브라우저 종료) — 창이 없으니 조작권을 돌리고
        # headless 로 다시 열어 마지막 탭을 복원한다.
        w.opened_for_request = None
        w.notice = {"reason": "window_closed",
                    "hint": ("사람이 창을 닫았습니다 — 조작권을 에이전트에게 돌리고 headless 로 다시 열어 "
                             "마지막 탭을 복원했습니다. 다시 관찰하세요.")}
        self.hub.release_by_server()
        if self._engine is not None:
            self._engine.bump_epoch("window_closed_by_human")
        self._control_banner = None
        _stderr_line("agent-browser serve: 사람이 창을 닫음 — headless 로 돌아갑니다")
        self._spawn_switch(False, "window_closed", snapshot=list(w.last_tabs))

    async def _on_demand_enter(self, action: ActionType) -> Optional[ActionResult]:
        """on-demand 도구 호출 입구: 전환 중이면 기다리고, 실패 상태면 headless 로 다시 연다."""
        from interface import on_demand

        w = self._window
        assert w is not None
        if self._started and self.hub.opened:
            await self._poll_handoff()
        if not await self._await_gate(on_demand.SWITCH_WAIT_S):
            return self._error_result(
                action, ErrorCode.TIMEOUT,
                f"브라우저 창 전환 중이라 {int(on_demand.SWITCH_WAIT_S)}초 안에 실행하지 못했습니다 — "
                "잠시 뒤 다시 호출하세요.",
                data={"window": w.info()},
            )
        if self._started and w.state == "failed":
            out = await self._switch_window(False, "recover", force=True)
            if not out.get("ok"):
                return self._error_result(
                    action, ErrorCode.PAGE_CRASHED,
                    "브라우저를 다시 열지 못했습니다(" + str(out.get("error") or "") + ") — "
                    "운영자에게 serve 재시작을 요청하세요.",
                    data={"window": w.info()},
                )
            self._recovered_notice = True
        return None

    async def close(self) -> None:
        await self._robots.close()  # WS-41: join background requests before closing their context
        if self._recipes is not None:
            self._recipes.close()  # 모아 둔 실행 통계 마지막 쓰기(WS-38)
        task, self._hub_task = self._hub_task, None
        if task is not None:
            task.cancel()
        # WS-34: 진행 중인 창 전환을 멈추고(전환은 취소되면 스스로 브라우저를 닫는다) 코드 창을 닫는다.
        pending = [t for t in self._window_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5.0)
        if self._code_window is not None:
            try:
                await self._code_window.close()
            except Exception:  # noqa: BLE001
                logger.warning("승인 코드 창 정리 실패", exc_info=True)
        try:
            if self._core is not None:
                await self._core.close()
            await self._close_egress()
        finally:
            # 브라우저(영속 컨텍스트)를 닫은 뒤에 잠금을 푼다 — 닫히기 전에 다른 serve 가 잡지 않게.
            self.release_profile()
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
        if self._window is not None:
            # WS-34: 창 전환과 겹치지 않게 — 전환 중이면 기다리고, 실패 상태면 headless 로 다시 연다.
            blocked = await self._on_demand_enter(action_from_tool(name) or ActionType.OBSERVE_PAGE)
            if blocked is not None:
                return blocked
        # 관문 검사와 등록 사이에 await 없음 — 전환은 등록된 호출이 끝나길 기다린다(_drain_calls).
        self._inflight += 1
        try:
            if self._window is not None and action_from_tool(name) is ActionType.WAIT_FOR:
                result = await self._call_wait_tool(name, arguments, capture)
            else:
                result = await self._call_tool(name, arguments, capture)
            if capture and capture["gen"] != self._pixel_gen:
                return self._pixels_refused(ActionType.TAKE_SCREENSHOT)
            if action_from_tool(name) in {ActionType.NAVIGATE, ActionType.OBSERVE_PAGE}:
                from security.robots_signal import attach_robots_signal

                ctx = getattr(self._dispatcher, "ctx", None)
                page = getattr(ctx, "root_page", None) or getattr(ctx, "page", None) or self._page
                await attach_robots_signal(self._robots, result, page, self._egress)
            # WS-36: 결과에 실린 웹 유래 텍스트(관찰 요소 이름·제목, 추출 텍스트·속성, 다이얼로그
            # 문구, 차단 결과의 대상 이름·오류 문구 …)에 주입 문구가 있으면 data.injection_suspected
            # 신호를 붙인다 — 차단·수정하지 않는다. 모든 반환 경로(HITL 차단 등 조기 반환 포함)를
            # 덮으려고 여기서 한 번 한다. 자르기 전에 붙여 신호까지 크기 상한 안에 들게 한다.
            from security.injection_signal import attach_injection_signal

            # 검사 입력 상한 = 결과 크기 상한의 10배(WS-36 R1 NB2) — 자르기가 앞쪽을 남기므로 돌려주는
            # 부분은 늘 검사 범위 안이다.
            attach_injection_signal(result, max_scan_chars=10 * self.max_result_chars)
            # WS-30b: 큰 페이지 결과가 클라이언트 도구 결과 한도를 넘지 않게 항목 경계에서 자른다.
            cap_result_size(result, self.max_result_chars)
            if self._window is not None and (self._window_unreported or self._recovered_notice):
                self._attach_window_report(result)
            return result
        finally:
            self._inflight -= 1
            if capture:
                self._captures_inflight -= 1

    async def _call_wait_tool(self, name: str, arguments: Dict[str, Any],
                              capture: Dict[str, Any]) -> ActionResult:
        """on-demand 의 wait_for — 창 전환이 시작되면 대기를 끝내고 E_TIMEOUT 으로 돌려준다(R1 NB-5).

        wait_for 의 timeout_ms 는 상한이 없어 전환이 진행 중 호출을 기다리는 상한(DRAIN_TIMEOUT_S)보다 길 수
        있다 — 긴 대기 때문에 사람 호출(control_request)이 실패하지 않게, 전환(_drain_calls)이 이 대기를
        취소한다. 바깥 취소(클라이언트 취소·종료)는 그대로 올린다.
        """
        task = asyncio.get_running_loop().create_task(self._call_tool(name, arguments, capture))
        self._wait_calls.add(task)
        try:
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                raise
            if task.cancelled():
                w = self._window
                return self._error_result(
                    ActionType.WAIT_FOR, ErrorCode.TIMEOUT,
                    "브라우저 창 전환(사람 호출 등) 때문에 대기를 멈췄습니다 — 전환 뒤 다시 관찰하고 "
                    "필요하면 다시 기다리세요.",
                    data={"window": w.info() if w is not None else {}},
                )
            return task.result()
        finally:
            self._wait_calls.discard(task)

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
            if self._recipes is not None:
                self._recipes.reset("human_control")
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

        # WS-38: 레시피 궤적 기록용 스냅숏(기록하는 동작만, 한 번의 evaluate).
        recipe_pre = await self._recipes.pre(action, params) if self._recipes is not None else None
        # 누적 수로 이번 호출의 신규분을 센다(기록은 상한 deque 라 길이로는 못 센다, WS-29b R1).
        blocks_before = self._egress.blocked_total if self._egress is not None else 0
        upstream_before = self._egress.upstream_total if self._egress is not None else 0
        # WS-38 R1 NB-3: 클릭한 링크의 주소(실행 전 핸들) — 클릭으로 나간 이동의 차단을 알아보는 데 쓴다.
        target_href = self._handle_href(params)
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
        result = self._attach_egress_block(action, params, result, blocks_before, target_href=target_href)
        result = self._attach_upstream_failure(action, params, result, upstream_before)
        if self._pending_gate_basis is not None:
            result.data.setdefault("gate_basis", self._pending_gate_basis)
            self._pending_gate_basis = None
        if action in CHALLENGE_CHECK_ACTIONS:
            await self._attach_challenge(result)
        if self._recipes is not None:
            # WS-38: 성공 + 사후 확인 통과만 궤적에(승인 재생·실패·치유는 끊음), 관찰에는 후보.
            self._recipes.post(action, params, recipe_pre, result, used is not None)
            if action is ActionType.OBSERVE_PAGE:
                await self._recipes.observe(result)
        # (WS-36: 크기 상한은 call_tool 에서 — 주입 신호를 붙인 뒤 자른다.)
        return result

    def _handle_href(self, params: Dict[str, Any]) -> Optional[str]:
        """element_id 대상 핸들의 href(링크가 아니거나 핸들이 없으면 None)."""
        eid = params.get("element_id")
        if not eid or self._engine is None:
            return None
        try:
            handle = self._engine.get_handle(eid)
        except Exception:  # noqa: BLE001 - 진단용 보조 정보일 뿐
            return None
        href = getattr(handle, "href", None) if handle is not None else None
        return href if isinstance(href, str) and href else None

    def _attach_egress_block(
        self, action: ActionType, params: Dict[str, Any], result: ActionResult, before: int,
        *, target_href: Optional[str] = None,
    ) -> ActionResult:
        """이번 호출 중 Egress 가 막은 이동을 에이전트에게 알린다 (WS-29b).

        활성 탭의 문서 이동(요청 주소·리다이렉트 홉·현재 주소의 호스트)이 막혔으면
        data.egress 에 이유(code=egress_blocked, host, category, reason, open_with)를 싣는다.
        navigate 가 막혔으면 실패(E_INVALID_URL)로 돌려준다 — 막힌 문서를 성공으로 보고하지
        않는다. 하위 요청(이미지·비콘 등) 차단은 싣지 않는다(문서 이동만).
        클릭한 링크의 호스트(target_href, WS-38 R1 NB-3)도 이동 대상으로 본다 — 막히면 탭이
        chrome-error:// 로 바뀌어 현재 주소로는 호스트를 알 수 없기 때문이다.
        """
        if self._egress is None:
            return result
        new = self._egress.blocked_since(before)
        if not new:
            return result
        from urllib.parse import urlparse

        hosts = set()
        for raw in (params.get("url"), getattr(self._page, "url", None), result.current_url, target_href):
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
        if self._window is not None and challenge is not None:
            # WS-34 D3: 창에서 해결했던 사이트에서 headless 복귀 뒤 다시 차단/캡차 → 알리고, 다음
            # 조작권 요청 때 창을 서버 수명 동안 유지(sticky).
            from interface.on_demand import STICKY_PENDING_HINT

            pending = self._window.note_challenge(str(getattr(page, "url", "") or ""), challenge)
            if pending is not None:
                self._sync_window()
                _stderr_line(f"agent-browser serve: {pending['domain']} 에서 창으로 해결한 뒤 "
                             "headless 에서 다시 차단/캡차 — 다음 조작권 요청 때 창을 열고 유지합니다")
            if self._window.sticky_pending is not None:
                result.data.setdefault("window", dict(self._window.info(), hint=STICKY_PENDING_HINT))

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
            # 유일한 탭이 닫히면 활성 탭이 None 이다(R3 NB-2) — 계약은 str.
            tab_id=(self._core.active_tab_id if self._core else "") or "",
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
    profile: Optional[str] = None,
    recipes: bool = True,
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
        profile=profile,
        recipes=recipes,
    )
    #: WS-38: MCP initialize 의 서버 instructions(레시피 쓰는 법). 꺼져 있으면 보내지 않는다.
    from security.robots_signal import ROBOTS_INSTRUCTIONS

    instructions = (RECIPE_INSTRUCTIONS + "\n" if recipes else "") + ROBOTS_INSTRUCTIONS

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
        for spec in build_listed_tools(recipes):  # 액션 툴 19종 + 서버 도구(WS-29·WS-38)
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
        if name in SERVER_TOOLS and (recipes or name != RECIPE_TOOL):
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
        # mcp 1.x — 데코레이터 등록. instructions 인자가 없는 옛 1.x 는 속성으로 넣는다
        # (create_initialization_options 가 self.instructions 를 읽는다).
        try:
            server = Server("agent-browser", instructions=instructions)
        except TypeError:
            server = Server("agent-browser")
            server.instructions = instructions  # type: ignore[attr-defined]
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
        instructions=instructions,
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
    profile: Optional[str] = None,
    recipes: bool = True,
) -> None:
    """stdio 트랜스포트로 MCP 서버를 구동한다.

    stdout 은 MCP 프로토콜 전용이다 — 시작 로그는 stderr 로 한 줄만 쓴다.
    profile(WS-32): 영속 프로필 이름. MCP 를 열기 전에 잠근다 — 다른 serve 가 쓰는 중이면
    serve_profile.ProfileInUseError 를 올린다(CLI 가 stderr 한 줄 + exit 2).
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
        **({"profile": profile} if profile is not None else {}),
        **({} if recipes else {"recipes": False}),
    )
    # WS-29: 사람 인계 통로(상태 디렉터리)를 브라우저보다 먼저 연다 — 사람이 server_id 로 고른다.
    server_id = backend.open_handoff() if hasattr(backend, "open_handoff") else None
    if profile is not None:
        # WS-32: 브라우저는 첫 툴 호출 때 뜨지만 잠금은 지금 잡는다 — 동시 사용이면 바로 실패.
        try:
            backend.acquire_profile()
        except BaseException:
            if hasattr(backend, "hub"):
                backend.hub.close()
            raise
    extra = ""
    if browser_mode == "user-chrome":
        extra = f" chrome_profile={chrome_profile or '(기본)'} keep_open={bool(keep_open)}"
    if profile is not None:
        extra += f" profile={profile}(영속)"
    if browser_mode == "on-demand":
        extra += (" [평소 headless — 사람 인계·승인 코드 때만 창"
                  + ("" if profile is not None else ", 서버 전용 임시 프로필(종료 때 삭제)") + "]")
    extra += " recipes=" + (("on(profile)" if profile is not None else "on(memory)") if recipes else "off")
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
