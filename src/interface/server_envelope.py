"""MCP 응답 봉투(envelope)와 결과 크기 상한(cap_result_size, WS-30b).

WS-48: interface/mcp_server.py 에서 동작 무변경으로 옮겼다. 이름은 mcp_server 가 재수출한다.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Dict, List

from contracts import ActionResult, ActionType


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
