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


def blocked_hint_text(hint: str) -> str:
    """무인 차단 메시지 끝에 붙는 사람(운영자)용 해결 경로."""
    return (
        f" — 이 액션을 허용하려면 서버를 `--pre-approve \"{hint}\"` 으로 다시 띄우거나"
        " `--mode interactive` 를 쓰세요. 사용자에게 이 안내를 전하고 멈추세요."
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
    ) -> None:
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
        logger.info(
            "MCP 브라우저 세션 시작 (mode=%s, browser=%s)", self.mode.value, self.browser_mode
        )

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
        if self._core is not None:
            await self._core.close()
        await self._close_egress()
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

        # 입력 검증: 계약 모델로 파싱해 잘못된 인자를 조기 차단한다.
        model = ACTION_INPUT_MAP.get(action)
        params: Dict[str, Any] = dict(arguments or {})
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
        blocked = await self._check_hitl(action, params)
        if blocked is not None:
            return blocked

        # 누적 수로 이번 호출의 신규분을 센다(기록은 상한 deque 라 길이로는 못 센다, WS-29b R1).
        blocks_before = self._egress.blocked_total if self._egress is not None else 0
        upstream_before = self._egress.upstream_total if self._egress is not None else 0
        result = await self._dispatcher.dispatch(action, params)
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

    async def _check_hitl(
        self, action: ActionType, params: Dict[str, Any]
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

        message = decision.reason
        if decision.requires_confirmation and decision.dialog is not None:
            # 대화형 모드: 클라이언트가 렌더링할 정형 모달을 함께 전달한다.
            message = decision.dialog.message

        data: Dict[str, Any] = {
            "requires_confirmation": decision.requires_confirmation,
            "risk": decision.risk.value,
            "dialog": (
                decision.dialog.model_dump(mode="json") if decision.dialog else None
            ),
            # WS-31: 운영자가 왜 막혔는지 — {name, matched_keyword, source, …}.
            "gate_basis": dict(decision.basis),
        }
        code = decision.error_code or ErrorCode.HITL_UNATTENDED_BLOCKED
        if code is ErrorCode.HITL_UNATTENDED_BLOCKED:
            # WS-30 항목 5: 에이전트가 "어떻게 승인하나요?"로 멈추지 않게, 사람(운영자)이
            # 할 수 있는 해결 경로만 알린다 — 다른 도구로 돌아가는 방법은 알리지 않는다.
            hint = pre_approve_hint(action, element_name)
            message += blocked_hint_text(hint)
            data["pre_approve_hint"] = hint

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
        for spec in build_all_tools():
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
    )
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
    """stderr 에 한 줄(신호 처리기 안에서도 안전하게 os.write). stdout 은 MCP 전용."""
    try:
        os.write(2, (text + "\n").encode("utf-8", "replace"))
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
