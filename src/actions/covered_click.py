"""WS-79 (#205): 가운데가 덮인 대상에 대한 포인터 입력(click·check_box·hover·download trigger).

Playwright 는 요소 **중앙**만 쏘고, 중앙이 다른 요소에 덮이면 스크롤 정렬을 바꿔 가며 재시도하다 시간 초과로
실패한다(스크롤로 뺄 수 없는 짧은 페이지·iframe·모달에서). 대상의 다른 부분이 비어 있어도 누르지 못한다.

2단계(리뷰 .hermes/plans/ws79-evidence/review.md B1~B8):
  (1) 읽기 전용 사전 판정(스크롤 없음) — 클릭할 그 locator/handle 에서 따로 평가한다(`COVER_JUDGE_JS`).
      중앙이 대상이면 기존 경로 그대로(바이트·지연 동일). 중앙이 덮였고 다른 점이 대상이면 그 점으로
      **비 force** position 클릭(Playwright 실입력 — 자기 hit 검사를 그대로 거친다).
  (2) 그런 점이 없거나 판정 불가면 기존 호출을 `COVERED_CLICK_BUDGET_MS` 예산으로 — Playwright 의 스크롤 재시도·
      덮개 사라짐 대기를 그대로 쓴다. 덮임 시간 초과면 스크롤된 상태에서 한 번 더 판정하고, 그래도 없으면
      E_ELEMENT_NOT_INTERACTABLE(retry_safe) + data.covered_by(고정 어휘, 이름은 safe_page_text 80).

받아들이는 점(B3): hit 에서 위로 올라가며 처음 만나는 상호작용 요소가 Playwright 가 retarget 한 대상(또는 지목한
요소 자신)이어야 한다 — 대상 안 중첩 버튼·링크 자리는 버린다(HITL 이 판정하지 않은 다른 조작부). 열린 모달
(`dialog:modal`·보이는 aria-modal=true) 밖 대상이거나 화면 전체 고정 덮개(backdrop)면 다른 점을 쓰지 않는다(B4).

이 판정은 페이지 world 에서 돈다(속을 수 있다) — 그러나 대체 점은 늘 비 force 라 Playwright 의 hit 검사가
덮개 클릭을 막는다(리뷰 F4·probe4). force 대체 점은 쓰지 않는다.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

#: (2) 단계 예산 — 기준(dev bf5248b)과 같은 5초. 줄일지는 사용자 결정 대기(계획 v2) — 바꾸지 말 것.
COVERED_CLICK_BUDGET_MS = 5000
#: 판정이 고른 덮이지 않은 점으로 다시 보낼 때(시간 초과 뒤 재판정·목록 사이트 결속 경로)의 상한. 방금 확인한 점이라
#: Playwright 는 곧바로 보낸다 — 이 상한은 그 사이 덮개가 생긴 경우에만 쓰인다.
COVERED_POINT_TIMEOUT_MS = 1000

#: 실패 문구(고정 — Playwright 원문·덮개 HTML·페이지 글자 금지, B8).
COVERED_MESSAGE = (
    "대상의 누를 자리가 다른 요소(배너·대화상자 등)에 덮여 있어 입력을 보내지 않았습니다 — "
    "data.covered_by 를 보고 덮은 요소를 먼저 닫거나 스크롤한 뒤 다시 시도하세요."
)

#: covered_by.role 고정 어휘(B8). injection_signal 은 'role' 키를 검사하지 않으므로 페이지 글자를 싣지 않는다.
COVER_ROLES = frozenset({
    "alert", "alertdialog", "banner", "button", "checkbox", "complementary", "contentinfo", "dialog", "document",
    "form", "group", "heading", "img", "link", "list", "listitem", "log", "main", "marquee", "menu", "menubar",
    "navigation", "none", "note", "presentation", "progressbar", "region", "search", "status", "tab", "tablist",
    "tabpanel", "timer", "toolbar", "tooltip",
})
COVER_LAYERS = frozenset({"fixed", "sticky", "modal", "frame", "other"})
_TAG_RE = re.compile(r"[a-z0-9-]{1,32}")
_POINTS_CAP = 1000
#: 덮개 이름 길이(ambiguous_target_view 와 같은 safe_page_text 상한 80).
COVER_NAME_LIMIT = 80

#: 판정 본체(페이지 world, 읽기 전용 — 스크롤·포커스·이벤트 없음). 반환:
#:   {s:'center'} 중앙이 대상 | {s:'point', x, y, checked, covered, cover} 덮이지 않은 대상 점(x·y 는 Playwright
#:   position — 요소 padding box 왼쪽 위 기준) | {s:'covered', modal, checked, covered, cover} | {s:'unknown'}.
_COVER_CORE_JS = r"""
function __ws79Judge(n) {
  const el = n && (n.nodeType === 1 ? n : n.parentElement);
  if (!el || !el.isConnected) return {s: 'unknown'};
  const doc = el.ownerDocument, win = doc && doc.defaultView;
  if (!win) return {s: 'unknown'};
  const vw = win.innerWidth, vh = win.innerHeight;
  const INTERACTIVE = 'button, a[href], input, textarea, select, [contenteditable=""], '
    + '[contenteditable="true"], [role=button], [role=link], [role=menuitem], [role=tab], '
    + '[role=checkbox], [role=switch], label, summary, [onclick], [tabindex]:not([tabindex="-1"])';
  const up = (e) => e.parentElement || (e.parentNode && e.parentNode.host) || null;
  // Playwright 의 hit 대상 retarget('button-link') — 같은 대상만 받아들인다.
  const T = el.closest('button,a,[role=button],[role=link]') || el;
  const root = el.getRootNode();
  const scope = (root && typeof root.elementFromPoint === 'function') ? root : doc;
  const hitAt = (x, y) => {
    let h = scope.elementFromPoint(x, y);
    for (let i = 0; h && h.shadowRoot && i < 16; i++) {
      const inner = h.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === h) break;
      h = inner;
    }
    return h;
  };
  const accept = (hit) => {
    let e = hit, i = 0;
    for (; e && i < 200; i++, e = up(e)) {
      if (e === T || e === el) break;
      if (e.nodeType === 1 && e.tagName === 'LABEL' && e.control === el) return true;
      if (e.nodeType === 1 && e.matches && e.matches(INTERACTIVE)) return false;  // 중첩 조작부(B3)
    }
    if (!e || i >= 200) return false;
    for (; e && i < 400; i++, e = up(e)) { if (e === T) return true; }
    return false;
  };
  const clip = (r) => {
    const l = Math.max(r.left, 0), t = Math.max(r.top, 0), rr = Math.min(r.right, vw), b = Math.min(r.bottom, vh);
    return (rr - l) * (b - t) > 0.99 && rr > l && b > t ? {l: l, t: t, r: rr, b: b} : null;
  };
  const own = Array.from(el.getClientRects()).map(clip).filter(Boolean);
  if (!own.length) return {s: 'unknown'};  // 뷰포트 밖 — 기존 경로(Playwright 가 스크롤)
  const cx = (own[0].l + own[0].r) / 2, cy = (own[0].t + own[0].b) / 2;
  const centerHit = hitAt(cx, cy);
  if (centerHit && accept(centerHit)) return {s: 'center'};
  const inside = (node, box) => { for (let e = node, i = 0; e && i < 400; e = up(e), i++) { if (e === box) return true; } return false; };
  const vis = (e) => { const cs = win.getComputedStyle(e); const r = e.getBoundingClientRect();
    return cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0; };
  let modalEl = null;
  try { modalEl = doc.querySelector('dialog:modal'); } catch (e) { modalEl = null; }
  if (!modalEl) {
    for (const m of doc.querySelectorAll('[aria-modal="true" i]')) { if (vis(m)) { modalEl = m; break; } }
  }
  const IMPLICIT = {DIALOG: 'dialog', BUTTON: 'button', NAV: 'navigation', HEADER: 'banner', FOOTER: 'contentinfo',
    ASIDE: 'complementary', FORM: 'form', IMG: 'img', MAIN: 'main'};
  const cover = (() => {
    const c = centerHit;
    if (!c || c.nodeType !== 1) return null;
    let layer = 'other', fixedEl = null;
    if (c.tagName === 'IFRAME' || c.tagName === 'FRAME') layer = 'frame';
    else {
      for (let e = c, i = 0; e && i < 60; e = up(e), i++) {
        if (e.nodeType !== 1) continue;
        const p = win.getComputedStyle(e).position;
        if (p === 'fixed' || p === 'sticky') { layer = p; fixedEl = e; break; }
      }
    }
    let op = 1;
    for (let e = c, i = 0; e && i < 60; e = up(e), i++) {
      if (e.nodeType !== 1) continue;
      const o = parseFloat(win.getComputedStyle(e).opacity);
      op *= isNaN(o) ? 1 : o;
    }
    const cs = win.getComputedStyle(c);
    const bg = String(cs.backgroundColor || '');
    const noBg = (bg === 'transparent' || /rgba\([^)]*,\s*0\)$/.test(bg)) && cs.backgroundImage === 'none';
    const text = String(c.innerText || '').trim();
    let backdrop = false;
    if (fixedEl) { const r = fixedEl.getBoundingClientRect(); backdrop = r.width >= vw * 0.9 && r.height >= vh * 0.9; }
    const inModal = !!(modalEl && inside(c, modalEl));
    if (inModal) layer = 'modal';
    const explicit = String(c.getAttribute('role') || '').trim().toLowerCase().split(/\s+/)[0] || '';
    let role = explicit || IMPLICIT[c.tagName] || ((c.tagName === 'A' && c.hasAttribute('href')) ? 'link' : '');
    const name = String(c.getAttribute('aria-label') || text || '').slice(0, 200);
    return {tag: String(c.tagName || '').toLowerCase(), role: role || null, layer: layer,
      transparent: op < 0.1 || (noBg && !text), backdrop: backdrop, name: name};
  })();
  const modal = !!(modalEl && !inside(T, modalEl)) || !!(cover && cover.backdrop);
  if (modal) return {s: 'covered', modal: true, checked: 1, covered: 1, cover: cover};
  // 다른 점: 대상(retarget) 사각형마다(최대 3) 뷰포트로 자르고 안쪽 여백 max(10%, 테두리+2px), 격자 5×3.
  const ecs = win.getComputedStyle(el);
  const bl = parseFloat(ecs.borderLeftWidth) || 0, bt = parseFloat(ecs.borderTopWidth) || 0;
  const tcs = win.getComputedStyle(T);
  const bmax = Math.max(parseFloat(tcs.borderLeftWidth) || 0, parseFloat(tcs.borderRightWidth) || 0,
    parseFloat(tcs.borderTopWidth) || 0, parseFloat(tcs.borderBottomWidth) || 0);
  const rects = Array.from(T.getClientRects()).slice(0, 3).map(clip).filter(Boolean);
  const pts = [];
  for (const r of rects) {
    const w = r.r - r.l, h = r.b - r.t;
    const mx = Math.max(w * 0.1, bmax + 2), my = Math.max(h * 0.1, bmax + 2);
    const xs = w - 2 * mx > 0 ? [0, 1, 2, 3, 4].map(k => r.l + mx + (w - 2 * mx) * k / 4) : [(r.l + r.r) / 2];
    const ys = h - 2 * my > 0 ? [0, 1, 2].map(k => r.t + my + (h - 2 * my) * k / 2) : [(r.t + r.b) / 2];
    for (const y of ys) for (const x of xs) pts.push([x, y]);
  }
  pts.sort((a, b) => Math.hypot(a[0] - cx, a[1] - cy) - Math.hypot(b[0] - cx, b[1] - cy));
  let checked = 1, covered = 1;
  const box = el.getBoundingClientRect();
  for (const [x, y] of pts) {
    checked++;
    const h = hitAt(x, y);
    if (h && accept(h)) {
      return {s: 'point', x: Math.round((x - box.left - bl) * 100) / 100, y: Math.round((y - box.top - bt) * 100) / 100,
        checked: checked, covered: covered, cover: cover};
    }
    covered++;
  }
  return {s: 'covered', modal: false, checked: checked, covered: covered, cover: cover};
}
"""

#: ElementHandle 에서: `handle.evaluate(COVER_JUDGE_JS)`.
COVER_JUDGE_JS = "(n) => { " + _COVER_CORE_JS.strip() + " return __ws79Judge(n); }"
#: Locator(.first) 에서: `locator.evaluate_all(...)` — 기다리지 않는다(요소가 없으면 판정 불가 → 기존 경로).
COVER_JUDGE_ALL_JS = "(els) => { " + _COVER_CORE_JS.strip() + " return els.length ? __ws79Judge(els[0]) : {s: 'unknown'}; }"
#: _ACTIONABLE_JS(목록 사이트 결속 경로)에 끼워 넣는 같은 판정 본체.
COVER_CORE_JS = _COVER_CORE_JS.strip()

_STATES = frozenset({"center", "point", "covered", "unknown"})


def parse_judge(raw: Any) -> Optional[Dict[str, Any]]:
    """페이지 world 판정 결과를 믿을 수 있는 모양으로만 받는다(모르는 모양이면 None = 판정 불가 → 기존 경로)."""
    if not isinstance(raw, dict) or raw.get("s") not in _STATES:
        return None
    out: Dict[str, Any] = {"s": raw["s"]}
    if raw["s"] == "point":
        x, y = raw.get("x"), raw.get("y")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1e6 for v in (x, y)):
            return None
        out["x"], out["y"] = float(x), float(y)
    for k in ("checked", "covered"):
        out[k] = _count(raw.get(k))
    out["modal"] = raw.get("modal") is True
    out["cover"] = raw.get("cover") if isinstance(raw.get("cover"), dict) else None
    return out


def _count(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return max(0, min(_POINTS_CAP, int(v)))


def covered_by_view(judge: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """data.covered_by — {tag, role, layer, modal, transparent, points_checked, points_covered, name?}(리뷰 모양).

    페이지 world 값이라 여기서 다시 고정 어휘로 거른다: tag 는 [a-z0-9-]≤32(아니면 "unknown"), role·layer 는 고정
    어휘(아니면 null·"other"), 이름은 공백을 접어 safe_page_text 80.
    """
    from actions.verification import safe_page_text

    j = judge if isinstance(judge, dict) else {}
    cover = j.get("cover") if isinstance(j.get("cover"), dict) else {}
    tag = str(cover.get("tag") or "")
    role = cover.get("role")
    layer = cover.get("layer")
    out: Dict[str, Any] = {
        "tag": tag if _TAG_RE.fullmatch(tag) else "unknown",
        "role": role if isinstance(role, str) and role in COVER_ROLES else None,
        "layer": layer if isinstance(layer, str) and layer in COVER_LAYERS else "other",
        "modal": j.get("modal") is True,
        "transparent": cover.get("transparent") is True,
        "points_checked": _count(j.get("checked")),
        "points_covered": _count(j.get("covered")),
    }
    name = " ".join(str(cover.get("name") or "").split())
    if name:
        out["name"] = safe_page_text(name, COVER_NAME_LIMIT)
    return out


def click_point_view(judge: Dict[str, Any]) -> Dict[str, Any]:
    """다른 점으로 성공했을 때만 싣는 data.click_point(NB2) — x·y 는 요소 기준 position."""
    return {"reason": "center_covered", "points_checked": _count(judge.get("checked")),
            "x": round(float(judge["x"]), 1), "y": round(float(judge["y"]), 1)}


def is_pointer_intercept(exc: BaseException) -> bool:
    """Playwright 시간 초과가 '다른 요소가 포인터 이벤트를 가로챔' 때문인가(호출 기록의 줄 끝으로 본다)."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    if not isinstance(exc, PlaywrightTimeoutError):
        return False
    return any(line.rstrip().endswith("intercepts pointer events") for line in str(exc).splitlines())


def public_error_text(exc: BaseException) -> str:
    """예외 문구의 첫 줄만 — Playwright 호출 기록(Call log: 대상·덮개 요소 HTML, 속성 값)은 응답에 싣지 않는다(B8·F3)."""
    text = str(exc).strip()
    return text.splitlines()[0].rstrip() if text else type(exc).__name__
