"""Computed Layout DOM 살균기 (PRD §3.1 Tier 1).

브라우저 화면 래스터화 없이 **Computed Style만으로 가시성을 판정**해
노이즈 요소를 제거한다.

핵심 설계 — 단일 `Runtime.evaluate` 일괄 수집:
engine_spike 실측에서 CDP 왕복은 0.23ms였으나, 요소가 수백 개인 페이지에서
요소별 호출은 왕복 횟수만큼 비용이 누적된다(AGENTS.md §5 리포트 활용 규칙).
따라서 페이지 컨텍스트에서 **한 번의 evaluate로 전체 후보를 수집**하고,
스코어링은 Python에서 수행한다.

Shadow DOM: open은 `element.shadowRoot`로 순회 가능하나 closed는 불가능하다.
closed shadow root는 `shadow_dom.py`의 CDP pierce 경로가 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

#: 상호작용 가능 후보로 간주하는 태그/역할
INTERACTIVE_TAGS = (
    "a", "button", "input", "select", "textarea", "summary", "option", "label",
)

#: 명시적 role 속성으로 상호작용을 표현하는 값
INTERACTIVE_ROLES = (
    "button", "link", "checkbox", "radio", "textbox", "combobox", "menuitem",
    "tab", "switch", "searchbox", "option", "slider", "spinbutton",
)

#: 최소 클릭 가능 크기 (이보다 작으면 트래킹 픽셀 등으로 간주)
MIN_CLICKABLE_PX = 2

#: 페이지 컨텍스트에서 한 번에 실행되는 수집 스크립트.
#: 반환값은 순수 JSON 직렬화 가능 구조여야 한다.
COLLECT_SCRIPT = """
(() => {
  const INTERACTIVE_TAGS = %(tags)s;
  const INTERACTIVE_ROLES = %(roles)s;
  const MIN_PX = %(min_px)d;

  const results = [];
  let seq = 0;

  function accessibleName(el) {
    // 우선순위: aria-label > aria-labelledby > label > placeholder > title > 텍스트
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();

    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const ref = document.getElementById(labelledBy);
      if (ref && ref.textContent.trim()) return ref.textContent.trim();
    }
    if (el.id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lbl && lbl.textContent.trim()) return lbl.textContent.trim();
    }
    const ph = el.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    const title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    if (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) {
      if (el.value) return el.value.trim();
    }
    const text = (el.innerText || el.textContent || '').trim();
    return text.slice(0, 200);
  }

  function inferRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'summary') return 'button';
    if (tag === 'option') return 'option';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'search') return 'searchbox';
      if (t === 'range') return 'slider';
      if (t === 'number') return 'spinbutton';
      if (t === 'hidden') return 'none';
      return 'textbox';
    }
    return 'generic';
  }

  function isInteractive(el, role) {
    const tag = el.tagName.toLowerCase();
    if (INTERACTIVE_TAGS.indexOf(tag) !== -1) return true;
    if (INTERACTIVE_ROLES.indexOf(role) !== -1) return true;
    if (el.hasAttribute('onclick')) return true;
    if (el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1') return true;
    if (el.isContentEditable) return true;
    return false;
  }

  // Computed Style 기반 가시성 판정 (래스터화 없음)
  function visibility(el) {
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none') return { visible: false, why: 'display_none' };
    if (cs.visibility === 'hidden' || cs.visibility === 'collapse')
      return { visible: false, why: 'visibility_hidden' };
    if (parseFloat(cs.opacity) === 0) return { visible: false, why: 'opacity_zero' };
    if (el.hasAttribute('hidden')) return { visible: false, why: 'hidden_attr' };
    if (el.getAttribute('aria-hidden') === 'true')
      return { visible: false, why: 'aria_hidden' };

    const r = el.getBoundingClientRect();
    if (r.width < MIN_PX || r.height < MIN_PX)
      return { visible: false, why: 'zero_size' };

    // 뷰포트 밖이어도 스크롤로 도달 가능하므로 제거하지 않고 표시만 한다.
    const inViewport =
      r.bottom > 0 && r.right > 0 &&
      r.top < (window.innerHeight || 0) && r.left < (window.innerWidth || 0);

    return { visible: true, why: 'ok', inViewport: inViewport, rect: r };
  }

  function cssPath(el) {
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 6) {
      let part = node.tagName.toLowerCase();
      if (node.id) { parts.unshift(part + '#' + CSS.escape(node.id)); break; }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(
          (c) => c.tagName === node.tagName
        );
        if (siblings.length > 1) {
          part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
        }
      }
      parts.unshift(part);
      node = node.parentElement;
      depth++;
    }
    return parts.join(' > ');
  }

  function walk(root, isShadow, framePath) {
    let nodes;
    try {
      nodes = root.querySelectorAll('*');
    } catch (e) {
      return;
    }
    for (const el of nodes) {
      // open shadow root는 여기서 재귀 순회 (closed는 CDP pierce 담당)
      if (el.shadowRoot) {
        walk(el.shadowRoot, true, framePath);
      }

      const role = inferRole(el);
      if (role === 'none') continue;
      if (!isInteractive(el, role)) continue;

      const vis = visibility(el);
      if (!vis.visible) continue;

      const name = accessibleName(el);
      const r = vis.rect;
      results.push({
        seq: seq++,
        role: role,
        name: name,
        value: el.value !== undefined ? String(el.value).slice(0, 200) : null,
        tag: el.tagName.toLowerCase(),
        testid: el.getAttribute('data-testid') || el.getAttribute('data-test-id') || null,
        href: el.getAttribute('href') || null,
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
        in_viewport: !!vis.inViewport,
        is_shadow: isShadow,
        frame_path: framePath,
        css_path: cssPath(el),
        bbox: { x: Math.round(r.x), y: Math.round(r.y),
                width: Math.round(r.width), height: Math.round(r.height) },
      });
    }
  }

  walk(document, false, '');
  return {
    title: document.title || '',
    url: location.href,
    elements: results,
    total_dom_nodes: document.querySelectorAll('*').length,
  };
})()
""" % {
    "tags": list(INTERACTIVE_TAGS),
    "roles": list(INTERACTIVE_ROLES),
    "min_px": MIN_CLICKABLE_PX,
}


@dataclass
class RawElement:
    """살균기가 수집한 단일 후보 요소 (스코어링 전)."""

    seq: int
    role: str
    name: str
    tag: str
    css_path: str
    bbox: Dict[str, int]
    value: Optional[str] = None
    testid: Optional[str] = None
    href: Optional[str] = None
    disabled: bool = False
    in_viewport: bool = True
    is_shadow: bool = False
    frame_path: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RawElement":
        return cls(
            seq=int(data["seq"]),
            role=str(data.get("role", "generic")),
            name=str(data.get("name") or ""),
            tag=str(data.get("tag", "")),
            css_path=str(data.get("css_path", "")),
            bbox=dict(data.get("bbox") or {"x": 0, "y": 0, "width": 0, "height": 0}),
            value=data.get("value"),
            testid=data.get("testid"),
            href=data.get("href"),
            disabled=bool(data.get("disabled", False)),
            in_viewport=bool(data.get("in_viewport", True)),
            is_shadow=bool(data.get("is_shadow", False)),
            frame_path=str(data.get("frame_path") or ""),
        )


@dataclass
class SanitizedPage:
    """살균 완료된 페이지 스냅샷."""

    title: str
    url: str
    elements: List[RawElement]
    total_dom_nodes: int

    @property
    def noise_reduction_ratio(self) -> float:
        """전체 DOM 노드 대비 후보로 남은 비율."""
        if not self.total_dom_nodes:
            return 0.0
        return len(self.elements) / self.total_dom_nodes


def parse_collection(payload: Dict[str, Any]) -> SanitizedPage:
    """`COLLECT_SCRIPT` 반환값을 도메인 객체로 변환한다."""
    elements = [RawElement.from_dict(e) for e in payload.get("elements", [])]
    return SanitizedPage(
        title=str(payload.get("title") or ""),
        url=str(payload.get("url") or ""),
        elements=elements,
        total_dom_nodes=int(payload.get("total_dom_nodes") or 0),
    )


async def collect(page: Any) -> SanitizedPage:
    """페이지에서 단일 evaluate로 후보 요소를 일괄 수집한다."""
    payload = await page.evaluate(COLLECT_SCRIPT)
    return parse_collection(payload)
