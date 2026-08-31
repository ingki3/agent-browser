"""Closed Shadow DOM CDP Pierce 순회 (PRD §4.3).

`element.shadowRoot`는 closed shadow root에 대해 `null`을 반환하므로
`page.evaluate` 경로로는 내부 요소에 접근할 수 없다. 유일한 경로는 CDP다:

    DOM.getDocument(depth=-1, pierce=True)
        -> 노드 트리에서 shadowRoots(type="closed") 탐색
    DOM.resolveNode(backendNodeId=...)
        -> Runtime.callFunctionOn 으로 속성 추출

주의 (PRD §4.3): XPath는 Shadow Boundary를 통과할 수 없으므로 shadow 내부
요소의 자가 치유는 XPath 단계를 건너뛰고 CSS Piercing만 사용해야 한다.
`ShadowElement.healing_strategies`가 이를 반영한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: shadow 내부 요소에서 사용 가능한 자가 치유 전략 (XPath 제외)
SHADOW_HEALING_STRATEGIES = ("role_name", "testid", "text_similarity", "css_piercing")

#: 일반 요소의 전체 전략 (XPath 포함)
FULL_HEALING_STRATEGIES = ("role_name", "testid", "text_similarity", "css_path")

#: 상호작용 후보로 간주할 태그 (sanitizer와 동일 기준)
_INTERACTIVE_TAGS = frozenset(
    {"a", "button", "input", "select", "textarea", "summary", "option", "label"}
)

#: 한 번의 callFunctionOn으로 요소 속성을 뽑는 함수 선언
_EXTRACT_FN = """
function() {
  const el = this;
  const cs = window.getComputedStyle(el);
  if (cs.display === 'none' || cs.visibility === 'hidden' ||
      parseFloat(cs.opacity) === 0) {
    return null;
  }
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return null;

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
  // W3C accname 순서: aria-label > 콘텐츠 텍스트 > placeholder > title.
  // sanitizer / verification과 동일 규칙을 유지해야 한다. 세 곳이
  // 갈라지면 관찰 이름과 검증 이름이 불일치해 TOCTOU 오탐이 난다.
  const ariaName = el.getAttribute('aria-label');
  const textName = (el.innerText || el.textContent || '').trim();
  const name = (ariaName && ariaName.trim() ? ariaName
                : textName ? textName
                : (el.getAttribute('placeholder') ||
                   el.getAttribute('title') || '')).trim().slice(0, 200);
  return {
    tag: tag,
    role: String(role).toLowerCase(),
    name: name,
    value: el.value !== undefined ? String(el.value).slice(0, 200) : null,
    testid: el.getAttribute('data-testid') || null,
    disabled: !!el.disabled,
    bbox: { x: Math.round(r.x), y: Math.round(r.y),
            width: Math.round(r.width), height: Math.round(r.height) }
  };
}
"""


@dataclass
class ShadowElement:
    """closed/open shadow root 내부에서 발견된 요소."""

    backend_node_id: int
    role: str
    name: str
    tag: str
    bbox: Dict[str, int]
    host_path: str  # 호스트 요소까지의 CSS 경로
    shadow_type: str  # "closed" | "open"
    value: Optional[str] = None
    testid: Optional[str] = None
    disabled: bool = False

    @property
    def healing_strategies(self) -> Tuple[str, ...]:
        """XPath를 제외한 전략만 반환한다 (Shadow Boundary 통과 불가)."""
        return SHADOW_HEALING_STRATEGIES


@dataclass
class ShadowScanResult:
    """pierce 스캔 결과."""

    elements: List[ShadowElement] = field(default_factory=list)
    closed_roots: int = 0
    open_roots: int = 0

    @property
    def has_closed_shadow(self) -> bool:
        return self.closed_roots > 0


def _iter_nodes(node: Dict[str, Any]):
    """DOM.getDocument(pierce=True) 트리를 깊이 우선 순회한다."""
    yield node
    for child in node.get("children", []) or []:
        yield from _iter_nodes(child)
    for root in node.get("shadowRoots", []) or []:
        yield from _iter_nodes(root)
    content = node.get("contentDocument")
    if content:
        yield from _iter_nodes(content)


def _host_css_path(node: Dict[str, Any]) -> str:
    """shadow host 노드의 간이 식별 경로를 만든다."""
    name = (node.get("nodeName") or "").lower()
    attrs = node.get("attributes") or []
    pairs = dict(zip(attrs[0::2], attrs[1::2]))
    if "id" in pairs:
        return f"{name}#{pairs['id']}"
    if "class" in pairs:
        first = pairs["class"].split()[0] if pairs["class"].split() else ""
        if first:
            return f"{name}.{first}"
    return name


async def scan_shadow_roots(cdp: Any) -> ShadowScanResult:
    """CDP pierce로 shadow root 내부 상호작용 요소를 수집한다.

    closed shadow root는 이 경로가 유일한 접근 수단이다.
    """
    result = ShadowScanResult()

    doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
    root = doc.get("root")
    if not root:
        return result

    # shadow host와 그 내부 노드를 함께 수집한다.
    for node in _iter_nodes(root):
        shadow_roots = node.get("shadowRoots") or []
        if not shadow_roots:
            continue

        host_path = _host_css_path(node)
        for sroot in shadow_roots:
            stype = sroot.get("shadowRootType", "open")
            if stype == "closed":
                result.closed_roots += 1
            else:
                result.open_roots += 1

            for inner in _iter_nodes(sroot):
                tag = (inner.get("nodeName") or "").lower()
                if tag not in _INTERACTIVE_TAGS:
                    continue
                backend_id = inner.get("backendNodeId")
                if backend_id is None:
                    continue

                props = await _extract_properties(cdp, backend_id)
                if props is None:
                    continue

                result.elements.append(
                    ShadowElement(
                        backend_node_id=backend_id,
                        role=props.get("role", "generic"),
                        name=props.get("name", ""),
                        tag=props.get("tag", tag),
                        bbox=props.get("bbox", {}),
                        host_path=host_path,
                        shadow_type=stype,
                        value=props.get("value"),
                        testid=props.get("testid"),
                        disabled=bool(props.get("disabled", False)),
                    )
                )

    return result


async def _extract_properties(cdp: Any, backend_node_id: int) -> Optional[Dict[str, Any]]:
    """resolveNode → callFunctionOn 으로 요소 속성을 추출한다."""
    try:
        resolved = await cdp.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
    except Exception as exc:  # noqa: BLE001 - 노드가 이미 제거된 경우 등
        logger.debug("resolveNode 실패 (backendNodeId=%s): %s", backend_node_id, exc)
        return None

    object_id = (resolved.get("object") or {}).get("objectId")
    if not object_id:
        return None

    try:
        response = await cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": _EXTRACT_FN,
                "returnByValue": True,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("callFunctionOn 실패: %s", exc)
        return None
    finally:
        try:
            await cdp.send("Runtime.releaseObject", {"objectId": object_id})
        except Exception:  # noqa: BLE001
            pass

    return (response.get("result") or {}).get("value")
