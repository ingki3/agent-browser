"""WS-94(#272): extract_all 항목 판독 — 렌더되지 않는 항목 제외(B1·B2)와 열린 오버레이 소속(B3·B4)을 격리 world 한 번에.

* 항목은 기준 식과 같은 모양(``{text: innerText || textContent, <속성>: getAttribute}``)으로 읽는다 — 숨은 것이 없으면 같은 바이트.
* '렌더되지 않음'(B1) = ``checkVisibility()`` 기본 호출이 거짓(자신·조상 display:none, hidden 속성, 닫힌 details 내용,
  content-visibility:hidden)이거나, ``visibility`` 가 visible 아님 + innerText 빔. 예외: display:contents 는 부모 기준,
  option·optgroup 은 소속 select 기준. opacity:0·sr-only·화면 밖·높이 0 그릇·캐러셀 밖 슬라이드·font-size 0 은 렌더되므로
  그대로 남는다(문서화한 한계 — 스크롤 등장 효과 글자를 지키려고).
* 오버레이 소속 = WS-82 감지(`perception.overlay.detect_expr`, 가림 사실 기준)의 레이어 안. 감지가 남기는 격리 world 상태
  (``__abOverlay`` — 다음 클릭 ``overlay_opened`` 의 '직전 레이어')는 저장·복원한다(B4, `_OUTSIDE_JS` 방식).
* 못 읽으면(selector 문법 오류·일치 0·예외·모르는 모양) None — 부르는 쪽이 기준 호출을 그대로 한다(NN2).

판독 결과는 응답 키가 아니다. 서버 크기 맞춤으로 넘기는 오버레이 소속은 bool 목록(제어 어휘 키 없음 — NN4)이다.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

_JS = r"""
(() => {
  const A = __ARGS__, W = window, KEY = '__abOverlay';
  let nodes;
  try { nodes = Array.from(document.querySelectorAll(A.selector)); } catch (e) { return null; }
  if (!nodes.length) return null;
  const had = Object.prototype.hasOwnProperty.call(W, KEY), saved = W[KEY];
  let layers = [];
  try {
    const r = __DETECT__;
    if (r && r.on && W[KEY] && Array.isArray(W[KEY].layers)) layers = W[KEY].layers.slice();
  } catch (e) { layers = []; }
  finally { if (had) W[KEY] = saved; else delete W[KEY]; }
  function style(el) { try { return W.getComputedStyle(el); } catch (e) { return null; } }
  function shown(el) {
    if (typeof el.checkVisibility !== 'function') return true;
    try { if (el.checkVisibility()) return true; } catch (e) { return true; }
    const tag = el.localName;
    if (tag === 'option' || tag === 'optgroup') { const s = el.closest('select'); return !!s && shown(s); }
    const cs = style(el);
    if (cs && cs.display === 'contents') return !!el.parentElement && shown(el.parentElement);
    return false;
  }
  return nodes.map(function (el) {
    const inner = el.innerText;
    const item = { text: (inner || el.textContent || '').trim() };
    for (const a of A.attrs) item[a] = el.getAttribute(a);
    const cs = style(el);
    const blank = !(typeof inner === 'string' && inner.trim());
    const rendered = shown(el) && !(cs && cs.visibility !== 'visible' && blank);
    const inLayer = layers.some(function (L) { return L === el || L.contains(el); });
    return [item, rendered, inLayer];
  });
})()
"""


def extract_all_expr(nonce: str, selector: str, attrs: Sequence[str]) -> str:
    """격리 world 식. 감지 식을 먼저 끼우고 인자를 나중에(selector 글자가 자리표시자와 같아도 다시 치환되지 않게)."""
    from perception.overlay import detect_expr

    args = json.dumps({"selector": str(selector), "attrs": [str(a) for a in attrs]}, ensure_ascii=False)
    return _JS.replace("__DETECT__", detect_expr(nonce).strip()).replace("__ARGS__", args)


def parse_rows(raw: Any) -> Optional[Tuple[List[Dict[str, Any]], List[bool], List[bool]]]:
    """판독 → (항목, 렌더됨, 레이어 안) 또는 None(못 읽음 — 기준 호출). 한 줄이라도 모양이 다르면 None."""
    if not isinstance(raw, list) or not raw:
        return None
    items: List[Dict[str, Any]] = []
    rendered: List[bool] = []
    layer: List[bool] = []
    for row in raw:
        if (not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], dict)
                or not isinstance(row[1], bool) or not isinstance(row[2], bool)):
            return None
        items.append(row[0])
        rendered.append(row[1])
        layer.append(row[2])
    return items, rendered, layer


def keep_rendered(items: List[Dict[str, Any]], rendered: Sequence[bool],
                  layer: Sequence[bool]) -> Tuple[List[Dict[str, Any]], List[bool], int]:
    """(남길 항목, 그 항목의 레이어 소속, 뺀 개수). 전부 렌더됨·전부 렌더 안 됨이면 그대로(B2 — 조용한 전부 누락 금지)."""
    if not any(rendered):
        return list(items), [False] * len(items), 0  # 기준 출력 그대로(순서도)
    if all(rendered):
        return list(items), list(layer), 0
    keep = [i for i, r in enumerate(rendered) if r]
    return [items[i] for i in keep], [layer[i] for i in keep], len(items) - len(keep)


def layer_first_order(marks: Sequence[bool]) -> Optional[List[int]]:
    """레이어 안 항목을 먼저(각자 문서 순서), 나머지를 뒤에. 순서가 그대로면 None(바꿀 것 없음)."""
    order = [i for i, m in enumerate(marks) if m] + [i for i, m in enumerate(marks) if not m]
    return None if order == list(range(len(marks))) else order
