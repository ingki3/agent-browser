"""WS-82(#198·#158): 활성 오버레이(모달·달력 레이어) 감지와 관찰 순서 후처리 — 엔진·점수기 바이트 동결 밖.

감지(리뷰 B2 — '가림 사실' 기준, z-index·role=dialog 단독은 신호가 아님)는 관찰 문서(최상위 또는 switch_frame 한 프레임)의
**격리 world** 에서만 한다. 켜지는 조건은 셋 중 하나:

* ``top_layer``  — 보이는 ``dialog:modal``(showModal 로 연 top layer 대화상자).
* ``aria_modal`` — 보이는 ``[aria-modal=true]``.
* ``covering``   — 배치 레이어 L(position fixed/absolute) 이 **L 밖** 관찰 후보 ``COVER_MIN`` 개 이상의 가운데 점을
  elementFromPoint 로 덮고(그 점의 맨 위 요소가 L 안), 화면 가운데 점의 요소 스택(elementsFromPoint)에 L 이 있다(없으면 가림
  검사 자체를 건너뛴다 — 평소 비용 최소). 고정 머리띠·하단 쿠키 띠·옆
  서랍처럼 가장자리에 붙은 띠, 일반 흐름 안 히어로·지도(아무것도 덮지 않음), SPA 껍데기(후보를 모두 담음 — 'L 밖' 후보 없음),
  pointer-events:none 워터마크(맞지 않음), 작은 채팅 창(가운데 밖)은 켜지지 않는다.

못 읽으면(격리 world 없음·예외) 꺼짐 — 관찰은 지금 바이트 그대로다. 감지한 레이어 노드는 격리 world 전역(``__abOverlay``,
관찰마다 새 nonce)에만 두고 페이지 world 는 닿지 못한다. 순서 규칙(B3)은 `plan_top_n`·`plan_full` 참조.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 'L 밖 후보 가운데 점을 덮음' 최소 개수 k.
COVER_MIN = 3
#: 감지 때 훑는 후보 상한(문서 순서) — 큰 페이지에서도 비용을 묶는다.
SCAN_LIMIT = 3000
#: 오버레이 감지 종류(응답 어휘 — 페이지 글자 0).
KINDS = ("top_layer", "aria_modal", "covering")
#: 감지 JS 가 훑는 대화형 후보(살균기 후보의 근사 — 감지는 '덮였나'만 본다).
_CANDIDATES = ("a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],"
               "[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=gridcell],[onclick],"
               "[tabindex]:not([tabindex='-1'])")

_DETECT_JS = r"""
(() => {
  const NONCE = __NONCE__, K = __K__, LIMIT = __LIMIT__, SEL = __SEL__;
  const doc = document, win = window, vw = win.innerWidth, vh = win.innerHeight;
  function box(e) { const r = e.getBoundingClientRect(); return (r.width >= 1 && r.height >= 1) ? r : null; }
  function shown(e) {
    if (!box(e)) return false;
    if (typeof e.checkVisibility === 'function') return e.checkVisibility({opacityProperty: true, visibilityProperty: true});
    const s = win.getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0;
  }
  const prev = win.__abOverlay && Array.isArray(win.__abOverlay.layers) ? win.__abOverlay.layers : [];
  const layers = [], kinds = [];
  function add(e, kind) {
    if (layers.some(function (l) { return l === e || l.contains(e); })) return;
    for (let i = layers.length - 1; i >= 0; i--) { if (e.contains(layers[i])) { layers.splice(i, 1); kinds.splice(i, 1); } }
    layers.push(e); kinds.push(kind);
  }
  for (const d of doc.querySelectorAll('dialog')) {
    let m = false; try { m = d.matches(':modal'); } catch (x) { m = false; }
    if (m && shown(d)) add(d, 'top_layer');
  }
  for (const e of doc.querySelectorAll('[aria-modal]')) {
    if (String(e.getAttribute('aria-modal')).trim().toLowerCase() === 'true' && shown(e)) add(e, 'aria_modal');
  }
  if (!layers.length) {
    // 화면 가운데를 지나는 배치 레이어(fixed/absolute) 후보 — 없으면 가림 검사를 하지 않는다(평소 비용 최소).
    const centre = new Set();
    for (const e of (doc.elementsFromPoint ? doc.elementsFromPoint(vw / 2, vh / 2) : [])) {
      for (let a = e; a && a !== doc.body && a !== doc.documentElement; a = a.parentElement) {
        const p = win.getComputedStyle(a).position;
        if (p === 'fixed' || p === 'absolute') centre.add(a);
      }
    }
    const counts = new Map();
    let n = 0;
    for (const c of (centre.size ? doc.querySelectorAll(SEL) : [])) {
      if (++n > LIMIT) break;
      const r = box(c);
      if (!r || !shown(c)) continue;  // 숨은 후보(opacity 0 슬라이드 등)는 '덮였다'고 세지 않는다
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      if (cx < 0 || cy < 0 || cx >= vw || cy >= vh) continue;
      const hit = doc.elementFromPoint(cx, cy);
      if (!hit || hit === c || c.contains(hit) || hit.contains(c)) continue;
      // 그 점을 덮은 배치 레이어 = hit 의 조상 중 c 를 담지 않는 가장 바깥 fixed/absolute (SPA 껍데기처럼 c 를 담으면 아님)
      let L = null;
      for (let a = hit; a && a !== doc.body && a !== doc.documentElement; a = a.parentElement) {
        const p = win.getComputedStyle(a).position;
        if ((p === 'fixed' || p === 'absolute') && !a.contains(c)) L = a;
      }
      if (L && centre.has(L)) counts.set(L, (counts.get(L) || 0) + 1);
    }
    for (const [L, cnt] of counts) {
      if (cnt < K) continue;
      add(L, 'covering');
    }
  }
  const opened = layers.some(function (l) { return prev.indexOf(l) < 0; });
  win.__abOverlay = {nonce: NONCE, layers: layers};
  return {on: layers.length > 0, kinds: Array.from(new Set(kinds)), opened: opened};
})()
"""

_MEMBERS_JS = r"""
(() => {
  const st = window.__abOverlay, NONCE = __NONCE__, PATHS = __PATHS__;
  if (!st || st.nonce !== NONCE || !Array.isArray(st.layers) || !st.layers.length) return null;
  return PATHS.map(function (p) {
    if (!p) return false;
    let e = null; try { e = document.querySelector(p); } catch (x) { e = null; }
    return !!e && st.layers.some(function (L) { return L === e || L.contains(e); });
  });
})()
"""


#: WS-87(#222, 리뷰 B1·U3): 요소 액션 직전 '열린 모달 밖 대상' 신호의 어휘(KINDS 중 모달 종류만 — covering 은 신호 아님).
MODAL_KINDS = ("top_layer", "aria_modal")
#: 그 신호의 고정 hint(페이지 글자 0). 거부가 아니라 알림 — 입력은 이미 보냈다.
OUTSIDE_MODAL_HINT = ("열린 모달(대화상자) 밖의 대상에 입력했습니다 — 모달이 아직 열려 있으면 먼저 모달을 처리(확인·닫기)해야 "
                      "하는지 확인하세요. 다시 관찰하면 모달 안 요소가 앞에 옵니다.")

# 같은 감지(_DETECT_JS)를 돌리고, 모달 종류 레이어가 있을 때만 대상(css_path, 실행 노드와 같은 경로)이 어느 레이어 안에도 없는지 본다.
# 감지가 남기는 격리 world 상태(__abOverlay — 클릭 뒤 overlay_opened 의 '직전 레이어')는 되돌려 놓는다(WS-82 동작 불변).
# 대상을 못 찾으면 null(판독 실패 = 신호 생략).
_OUTSIDE_JS = r"""
(() => {
  const PATH = __PATH__, W = window, KEY = '__abOverlay', __abOutside = true;
  const had = Object.prototype.hasOwnProperty.call(W, KEY), saved = W[KEY];
  try {
    const r = __DETECT__;
    const kinds = (r && r.kinds || []).filter(function (k) { return k === 'top_layer' || k === 'aria_modal'; });
    if (!r || !r.on || !kinds.length) return {modal: false};
    let e = null; try { e = document.querySelector(PATH); } catch (x) { e = null; }
    if (!e) return null;
    const layers = W[KEY] && Array.isArray(W[KEY].layers) ? W[KEY].layers : [];
    const inside = layers.some(function (L) { return L === e || L.contains(e); });
    return {modal: true, outside: !inside, kinds: kinds};
  } finally {
    if (had) W[KEY] = saved; else delete W[KEY];
  }
})()
"""


def outside_modal_expr(nonce: str, path: str) -> str:
    # 감지 식을 먼저 끼우고 경로를 나중에(경로 글자가 자리표시자와 같아도 다시 치환되지 않게).
    return _OUTSIDE_JS.replace("__DETECT__", detect_expr(nonce).strip()).replace(
        "__PATH__", json.dumps(str(path or ""), ensure_ascii=False))


def parse_outside_modal(raw: Any) -> Optional[Dict[str, List[str]]]:
    """판독 → {"kinds": [...]}(모달 밖 대상) 또는 None(모달 없음·모달 안·모르는 모양 = 신호 없음). kinds 는 고정 어휘만."""
    if not isinstance(raw, dict) or raw.get("modal") is not True or raw.get("outside") is not True:
        return None
    kinds = [k for k in MODAL_KINDS if k in (raw.get("kinds") or [])]
    return {"kinds": kinds} if kinds else None


def detect_expr(nonce: str) -> str:
    return (_DETECT_JS.replace("__NONCE__", json.dumps(nonce)).replace("__K__", str(COVER_MIN))
            .replace("__LIMIT__", str(SCAN_LIMIT)).replace("__SEL__", json.dumps(_CANDIDATES)))


def members_expr(nonce: str, paths: Sequence[str]) -> str:
    return _MEMBERS_JS.replace("__NONCE__", json.dumps(nonce)).replace(
        "__PATHS__", json.dumps([str(p or "") for p in paths], ensure_ascii=False))


def parse_detect(raw: Any) -> Optional[Dict[str, Any]]:
    """감지 결과 → {"on", "kinds", "opened"} (모르는 모양이면 None = 꺼짐). kinds 는 고정 어휘만."""
    if not isinstance(raw, dict) or not isinstance(raw.get("on"), bool):
        return None
    kinds = [k for k in KINDS if k in (raw.get("kinds") or [])]
    if raw["on"] and not kinds:
        return None
    return {"on": raw["on"], "kinds": kinds, "opened": bool(raw.get("opened")) and raw["on"]}


def parse_members(raw: Any, n: int) -> Optional[List[bool]]:
    if not isinstance(raw, list) or len(raw) != n or not all(isinstance(x, bool) for x in raw):
        return None
    return list(raw)


def quota(top_n: int) -> int:
    """오버레이 구성원이 base 꼬리를 대신할 수 있는 몫(≤ N/2)."""
    return max(0, int(top_n) // 2)


def plan_top_n(n_full: int, members: Sequence[bool], top_n: int) -> Tuple[List[int], List[int]]:
    """기본(top-N) 관찰의 최종 순서(엔진 full 순위의 인덱스 목록)와 밀려난 base 인덱스 목록.

    base = full[:N](= 엔진 top-N 결과, scorer 가 전부 정렬 뒤 자르므로 접두가 같다). base 밖 오버레이 구성원 A 를 순위 순으로
    q = min(|A|, N/2) 개까지 고르고, base 는 앞 N−q 개를 **그대로**(접두 불변) 둔다. 잘린 base 꼬리 안의 오버레이 구성원은
    A 보다 먼저 되살린다(이미 상위권인 구성원을 잃지 않게). 총 N 개.
    """
    n = min(int(top_n), n_full)
    base = list(range(n))
    outside = [i for i in range(n, n_full) if members[i]]
    q = min(len(outside), quota(top_n))
    if q <= 0:
        return base, []
    prefix = base[:n - q]
    tail = base[n - q:]
    extra = ([i for i in tail if members[i]] + outside)[:q]
    final = prefix + sorted(extra)
    pushed = [i for i in tail if i not in extra]
    return final, pushed


def plan_full(n_full: int, members: Sequence[bool]) -> List[int]:
    """전체(force_full_tree) 관찰: 오버레이 구성원을 앞에(순위 순), 나머지는 원래 순서 — 크기 상한(접두만 남김)에서도 남게."""
    return [i for i in range(n_full) if members[i]] + [i for i in range(n_full) if not members[i]]
