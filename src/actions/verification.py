"""Staleness 검증 및 사후조건 검증 (PRD §4.2, §4.3).

**Staleness (dispatch 이전)**: element_id가 가리키는 요소가 여전히 유효한지
액션 실행 직전에 확인한다. 전역 에포크는 네비게이션에서만 오르므로,
광고 로테이션 같은 동적 변경은 여기서 잡아야 한다.

검증 항목 (PRD §4.2):
1. 백킹 노드가 DOM에 연결되어 있는가
2. Role / Accessible Name이 일치하는가

**사후조건 (dispatch 이후)**: 이벤트를 보냈다고 액션이 성공한 것은 아니다.
클릭이 먹히지 않는 Silent Failure를 잡기 위해 상태 변화를 확인한다.

검증 항목 (PRD §4.3):
1. URL 전환 발생 여부
2. DOM 노드 변위 / 신규 노드 출현
3. 기대 상태값(Checked/Value) 반영 여부
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from contracts import ErrorCode
from perception.engine import ElementHandle

#: shadow DOM을 관통하는 요소 조회 헬퍼 (JS).
#: `document.querySelector`는 shadow root 내부를 보지 못한다. 관찰은
#: shadow를 수집하므로, 검증만 문서 범위로 조회하면 정상 요소를
#: '사라졌다'(NODE_DETACHED)거나 값이 `None`이라고 오판한다.
DEEP_QUERY_JS = """
  function deepQuery(sel) {
    if (!sel) return null;
    let found = null;
    try { found = document.querySelector(sel); } catch (e) { return null; }
    if (found) return found;
    const stack = [document];
    let guard = 0;
    while (stack.length && guard++ < 500) {
      const root = stack.pop();
      let nodes;
      try { nodes = root.querySelectorAll('*'); } catch (e) { continue; }
      for (const node of nodes) {
        if (!node.shadowRoot) continue;
        try {
          const hit = node.shadowRoot.querySelector(sel);
          if (hit) return hit;
        } catch (e) { /* 무시 */ }
        stack.push(node.shadowRoot);
      }
    }
    return null;
  }
"""

#: 요소가 살아있는지 확인하는 스크립트.
#: css_path로 재조회해 role/name이 일치하는지 본다.
STALENESS_CHECK_SCRIPT = """
(args) => {
  __DEEP_QUERY__
  const el = deepQuery(args.cssPath);
  if (!el) return { connected: false, reason: 'not_found' };
  if (!el.isConnected) return { connected: false, reason: 'detached' };

  const tag = el.tagName.toLowerCase();
  let role = el.getAttribute('role');
  if (!role) {
    if (tag === 'a') role = 'link';
    else if (tag === 'button') role = 'button';
    else if (tag === 'select') role = 'combobox';
    else if (tag === 'textarea') role = 'textbox';
    else if (tag === 'input') {
      // sanitizer(inferRole)와 동일한 표를 사용해야 한다. 두 곳이 갈라지면
      // 관찰 role과 검증 role이 불일치해 ROLE_CHANGED로 오판된다.
      // 실제 피해 — 위키백과 검색창(input[type=search])을 sanitizer는
      // 'searchbox'로, 검증기는 'textbox'로 계산해 액션이 3회 연속 차단됐다.
      const t = (el.type || 'text').toLowerCase();
      role = (t === 'checkbox') ? 'checkbox'
           : (t === 'radio') ? 'radio'
           : (t === 'submit' || t === 'button' || t === 'reset') ? 'button'
           : (t === 'search') ? 'searchbox'
           : (t === 'range') ? 'slider'
           : (t === 'number') ? 'spinbutton'
           : (t === 'hidden') ? 'none'
           : 'textbox';
    } else role = 'generic';
  }
  // W3C Accessible Name Computation 순서를 따른다:
  //   aria-label > 콘텐츠 텍스트 > placeholder > title
  //
  // 주의: title을 텍스트보다 앞에 두면 안 된다. sanitizer(WS-6)에서
  // 같은 버그를 고쳤는데 검증 경로에 남아 있었다. 실제 피해 —
  // 위키백과 'Log in' 링크가 72자 title 툴팁으로 계산되어, 관찰 시점
  // 이름('Log in')과 불일치해 NAME_CHANGED(E_TOCTOU_MISMATCH)로
  // 오판됐다. 요소는 전혀 바뀌지 않았는데 액션이 2회 연속 차단됐다.
  const ariaName = el.getAttribute('aria-label');
  const textName = (el.innerText || el.textContent || '').trim();
  const name = (ariaName && ariaName.trim() ? ariaName
                : textName ? textName
                : (el.getAttribute('placeholder') ||
                   el.getAttribute('title') || '')).trim().slice(0, 200);

  return {
    connected: true,
    role: String(role).toLowerCase(),
    name: name,
    checked: el.checked !== undefined ? !!el.checked : null,
    value: el.value !== undefined ? String(el.value) : null,
  };
}
""".replace("__DEEP_QUERY__", DEEP_QUERY_JS)


class StalenessReason(str, Enum):
    """staleness 판정 사유."""

    FRESH = "fresh"
    EPOCH_MISMATCH = "epoch_mismatch"
    NODE_DETACHED = "node_detached"
    ROLE_CHANGED = "role_changed"
    NAME_CHANGED = "name_changed"


@dataclass
class StalenessResult:
    """dispatch 이전 검증 결과."""

    fresh: bool
    reason: StalenessReason
    detail: str = ""
    observed_role: Optional[str] = None
    observed_name: Optional[str] = None

    @property
    def error_code(self) -> Optional[ErrorCode]:
        """판정 사유에 대응하는 표준 에러 코드.

        에포크 불일치는 별도 코드가 계약에 없으므로 `E_TOCTOU_MISMATCH`로
        보고한다. 관찰 시점과 실행 시점 사이에 상태가 바뀐 것이므로
        의미상 TOCTOU에 해당한다. (계약 동결 상태이므로 코드를 추가하지
        않고 기존 코드에 매핑한다.)
        """
        if self.fresh:
            return None
        if self.reason is StalenessReason.NODE_DETACHED:
            return ErrorCode.ELEMENT_NOT_FOUND
        # 에포크 불일치 및 role/name 변화는 모두 TOCTOU 계열이다.
        return ErrorCode.TOCTOU_MISMATCH


async def verify_staleness(
    page: Any,
    handle: ElementHandle,
    current_epoch: int,
    *,
    expected_role: Optional[str] = None,
    expected_name: Optional[str] = None,
) -> StalenessResult:
    """액션 실행 직전 요소 유효성을 검증한다."""
    # 1) 전역 에포크 확인 (네비게이션이 있었다면 모든 핸들 무효)
    if handle.epoch != current_epoch:
        return StalenessResult(
            fresh=False,
            reason=StalenessReason.EPOCH_MISMATCH,
            detail=f"핸들 에포크 {handle.epoch} != 현재 {current_epoch}",
        )

    if not handle.css_path:
        return StalenessResult(
            fresh=False,
            reason=StalenessReason.NODE_DETACHED,
            detail="css_path가 비어 있어 재조회 불가",
        )

    probe = await page.evaluate(STALENESS_CHECK_SCRIPT, {"cssPath": handle.css_path})

    # 2) 백킹 노드 연결 확인
    if not probe.get("connected"):
        return StalenessResult(
            fresh=False,
            reason=StalenessReason.NODE_DETACHED,
            detail=str(probe.get("reason", "unknown")),
        )

    # 3) Role 일치
    want_role = expected_role or handle.role
    got_role = probe.get("role", "")
    if want_role and got_role != want_role:
        return StalenessResult(
            fresh=False,
            reason=StalenessReason.ROLE_CHANGED,
            detail=f"role '{want_role}' -> '{got_role}'",
            observed_role=got_role,
            observed_name=probe.get("name"),
        )

    # 4) Accessible Name 일치
    want_name = expected_name if expected_name is not None else handle.name
    got_name = probe.get("name", "")
    if want_name and got_name != want_name:
        return StalenessResult(
            fresh=False,
            reason=StalenessReason.NAME_CHANGED,
            detail=f"name '{want_name}' -> '{got_name}'",
            observed_role=got_role,
            observed_name=got_name,
        )

    return StalenessResult(
        fresh=True,
        reason=StalenessReason.FRESH,
        observed_role=got_role,
        observed_name=got_name,
    )


# ---------------------------------------------------------------------------
# 사후조건 검증 (PRD §4.3)
# ---------------------------------------------------------------------------


@dataclass
class PageStateSnapshot:
    """액션 전후 비교를 위한 페이지 상태.

    DOM 노드 개수만으로는 부족하다. shadow 내부 변화, 텍스트 교체,
    포커스 이동, 클래스 토글은 개수를 바꾸지 않으면서도 실제 상태
    변화이기 때문이다. 여러 신호를 함께 캡처해 오탐(정상 액션을
    Silent Failure로 판정)을 줄인다.
    """

    url: str
    dom_node_count: int
    #: 본문 텍스트 해시 — 텍스트 교체 감지
    text_signature: int = 0
    #: 활성 요소 식별자 — 포커스 이동 감지
    active_element: str = ""
    #: 대상 요소의 클래스/속성 서명 — 토글 감지
    element_signature: str = ""
    element_checked: Optional[bool] = None
    element_value: Optional[str] = None


@dataclass
class PostConditionResult:
    """dispatch 이후 검증 결과."""

    satisfied: bool
    signals: List[str] = field(default_factory=list)
    detail: str = ""

    @property
    def silent_failure(self) -> bool:
        """이벤트는 보냈으나 아무 변화가 없는 상태."""
        return not self.satisfied


async def capture_state(
    page: Any, handle: Optional[ElementHandle] = None
) -> PageStateSnapshot:
    """사후조건 비교용 상태를 캡처한다.

    shadow DOM 요소는 `document.querySelector`로 찾을 수 없다. 값 검증이
    항상 `None`을 읽어 정상 입력을 Silent Failure로 오판한다.
    실측 — MDN 검색창(shadow 내부)에 'flexbox'가 정상 입력됐는데
    "기대값 'flexbox' != 실제 None"으로 E_TIMEOUT 처리됐다.
    따라서 문서에서 못 찾으면 shadow root를 재귀 탐색한다.
    """
    payload = await page.evaluate(
        """
        (cssPath) => {
          __DEEP_QUERY__
          const el = deepQuery(cssPath);
          const text = (document.body ? document.body.innerText || '' : '');
          // 간이 문자열 해시 (djb2)
          let h = 5381;
          for (let i = 0; i < text.length; i++) {
            h = ((h << 5) + h + text.charCodeAt(i)) | 0;
          }
          const active = document.activeElement;
          return {
            url: location.href,
            nodes: document.querySelectorAll('*').length,
            textSig: h,
            active: active ? (active.tagName + '#' + (active.id || '')) : '',
            elSig: el ? (el.className + '|' + (el.getAttribute('aria-expanded') || '')
                         + '|' + (el.getAttribute('aria-pressed') || '')) : '',
            checked: el && el.checked !== undefined ? !!el.checked : null,
            value: el && el.value !== undefined ? String(el.value) : null,
          };
        }
        """.replace("__DEEP_QUERY__", DEEP_QUERY_JS),
        handle.css_path if handle else None,
    )
    return PageStateSnapshot(
        url=payload["url"],
        dom_node_count=int(payload["nodes"]),
        text_signature=int(payload.get("textSig") or 0),
        active_element=str(payload.get("active") or ""),
        element_signature=str(payload.get("elSig") or ""),
        element_checked=payload.get("checked"),
        element_value=payload.get("value"),
    )


def verify_post_condition(
    before: PageStateSnapshot,
    after: PageStateSnapshot,
    *,
    expected_value: Optional[str] = None,
    expected_checked: Optional[bool] = None,
    dom_delta_threshold: int = 1,
) -> PostConditionResult:
    """액션 전후 상태를 비교해 실제 변화가 있었는지 판정한다.

    기대 상태값이 주어지면 그것이 유일한 판정 기준이다(가장 강한 신호).
    그렇지 않으면 URL/DOM/텍스트/포커스/속성 중 하나라도 변하면 성공으로
    본다. 판정을 좁게 잡으면 정상 액션을 Silent Failure로 오판해
    액션 성공률이 실제보다 낮게 측정된다.
    """
    signals: List[str] = []

    # 기대 상태값이 있으면 그것만으로 판정한다 (가장 강한 신호).
    if expected_value is not None:
        if after.element_value == expected_value:
            return PostConditionResult(
                satisfied=True, signals=[f"value_applied: {expected_value!r}"]
            )
        return PostConditionResult(
            satisfied=False,
            signals=signals,
            detail=f"기대값 {expected_value!r} != 실제 {after.element_value!r}",
        )

    if expected_checked is not None:
        if after.element_checked == expected_checked:
            return PostConditionResult(
                satisfied=True, signals=[f"checked_applied: {expected_checked}"]
            )
        return PostConditionResult(
            satisfied=False,
            signals=signals,
            detail=f"기대 checked {expected_checked} != 실제 {after.element_checked}",
        )

    # 1) URL 전환
    if before.url != after.url:
        signals.append(f"url_changed: {before.url} -> {after.url}")

    # 2) DOM 변위 / 신규 노드
    delta = abs(after.dom_node_count - before.dom_node_count)
    if delta >= dom_delta_threshold:
        signals.append(f"dom_delta: {before.dom_node_count} -> {after.dom_node_count}")

    # 3) 본문 텍스트 변경 (노드 수는 그대로여도 내용이 바뀌는 경우)
    if before.text_signature != after.text_signature:
        signals.append("text_changed")

    # 4) 포커스 이동 (클릭이 실제로 요소에 도달했다는 증거)
    if before.active_element != after.active_element:
        signals.append(f"focus_moved: {before.active_element} -> {after.active_element}")

    # 5) 대상 요소 속성/클래스 토글 (aria-expanded, aria-pressed 등)
    if before.element_signature != after.element_signature:
        signals.append("element_attr_changed")

    # 6) 대상 요소의 value/checked 변화
    #    select_option, upload_file 등은 페이지 구조를 바꾸지 않고
    #    요소 자신의 값만 갱신하므로 위 신호로는 잡히지 않는다.
    if before.element_value != after.element_value:
        signals.append(
            f"element_value_changed: {before.element_value!r} -> {after.element_value!r}"
        )
    if before.element_checked != after.element_checked:
        signals.append(
            f"element_checked_changed: {before.element_checked} -> {after.element_checked}"
        )

    if signals:
        return PostConditionResult(satisfied=True, signals=signals)

    return PostConditionResult(
        satisfied=False,
        signals=[],
        detail=(
            "URL/DOM/텍스트/포커스/속성 어디에도 변화가 없음 (Silent Failure 의심)"
        ),
    )
