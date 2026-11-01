"""반복 묶음 대상 구분 — 관찰 `data.repeated` (WS-62).

실사례(하나투어 달력, 2026-10-22): 두 달이 나란히 있어 `link "19"` 가 둘인데 어느 달인지 단서가 없어 모델이 다른
달을 눌렀다. 계약(ObservedElement)은 동결이라 관찰 결과 `data` 에 부가 정보를 붙인다.

* **발동(구조)**: 같은 틀(태그+role, class 무시)의 반복 목록(형제 ≥3)을 담은 같은 틀의 묶음이 2개 이상(패널 쌍둥이 —
  두 달 패널·두 표), 또는 반복 목록의 항목들(항목 쌍둥이 — 상품 행). 그리고 묶음 사이에 **이름 첫 낱말**(NFKC·공백
  기준, 예 `'22\\n\\n148만'` → `22`)이 같은 대상이 하나 이상 있을 때만 낸다(사용자 결정 A — 푸터 열·메가메뉴 열처럼
  이름이 다 다른 쌍둥이는 내지 않는다). 이름 없는 대상·shadow 안 대상은 뺀다. 내부 지문과 (role, name) 열이 모두
  같은 묶음(캐러셀 복제)은 하나로 접고(`copies`), 접은 뒤 묶음이 2개 미만이거나 첫 낱말이 더 겹치지 않으면 내지
  않는다(정적 글자가 없고 이름 열까지 같은 두 묶음은 복제와 구분하지 못해 접힌다 — 한계).
* **국소성(R4)**: 묶음은 대상 가까이에서만 대상을 받는다 — 패널 쌍둥이는 그 대상의 가장 안쪽 반복 행 목록에서 위로
  `GROUP_CLIMB`(6) 단계 이내, 항목 쌍둥이는 그 대상의 가장 안쪽 반복 행(대상 자신 제외)이 그 항목이고 6단계 이내(R5).
  패널 쌍둥이는 깊은(작은) 것부터 배정한다. 국소성은 형성(먼저 차지)에만 쓰고, 형성된 패널은 그 행 틀 대상을 깊이와
  무관하게 받는다(R5 — 신원 고정과 분리). 실사례(2026-10-23): 바깥 레이아웃 쌍둥이(#wrap/#footer)가 날짜·다음달·검색 단추를 먼저 흡수했다.
* **형성(R6)**: 셀 대상(가장 안쪽 행이 항목 안 셀)은 같은 틀 경로끼리 충돌로 형성에 참여(상품 카드·표 행).
* **관찰 정보 전용(R12 — 사용자 결정 B, #136)**: 이 모듈은 `data.repeated` 표시만 만든다. 핸들 고정(group_path·
  group_heading)은 v1.16.0 원문 `perception.repeated_v1160` 만 쓰고, 실행 직전 확인도 그 원문 groupCheck 하나다. 아래 JS 가
  계산하는 `pins`·`fp`(R4~R6 형성의 일부 — 배정 순서·`done` 에 쓰인다)는 핸들에 저장하지 않는다. 두 모듈은 함수를 공유하지
  않는다(R6~R11 처럼 섞이면 기준 고정이 다시 줄어든다).
* **모양**: `{how, groups:[{group, group_source, x, y, role?, members:[{id, role?, name, states?}], copies?}], compact?, omitted?}` —
  페이지 문자열은 모두 값(이름을 키로 쓰지 않는다 — 주입 신호·가림 규칙이 키를 보지 않는다, 리뷰 B1).
* **표제(group)**: 묶음의 aria-labelledby/aria-label → caption → legend → heading(h1~h6·role=heading) → 묶음 안
  비대화형 정적 글자. 대화형 요소(링크·버튼·입력 …)와 반복 목록 항목 안의 글자는 후보가 아니다(이전달/다음달 링크·요일
  머리글·날짜 제외) — aria-labelledby 참조 노드에도 같은 제외(R1 R3). 정적 후보(묶음 바로 아래 자식 단위)가 여럿이면
  하나를 고르지 않고 순서대로 이은 글자(결정 A, 키워드 추론 없음). 같은 쌍둥이 묶음끼리 표제가 **서로 다를 때만** 싣고, 같거나 못 찾으면 null(+ 위치 x·y).
  `group_source` 로 출처를 밝힌다. 서버 판정이 아니다(how).
* **상태(states)**: ARIA 만(aria-selected/checked/pressed/current/expanded=참, disabled). class 이름은 추측하지 않는다 —
  ARIA 없는 위젯(하나투어)은 상태가 비고, 반영은 누른 뒤 입력칸 값으로 확인한다.
* **크기**: 블록(groups 직렬화) 3,000자. 다 담아 넘으면 먼저 전부 압축(`compact: true` — 이름은 살균·절단 뒤 첫 낱말
  12자, 묶음 안 role 이 같으면 묶음 단위 `role`, 표제는 정적 후보 첫 조각)하고, 대상이 많은 세트부터 담아 그래도
  안 들어가는 세트는 통째로 빼 `omitted` 에 개수(탐욕). 응답 크기 상한 맞춤은 서버(`fit_repeated`, saved_pages 를 먼저 뺀다).
* **고정**: 이 모듈은 고정하지 않는다(위 R12). members 의 id 는 평소 관찰 id 그대로(같은 핸들)라 누르면 v1.16.0 고정으로
  확인받는다.

한계: 첫 낱말이 서로 다른 반복 대상은 잡지 않는다. 같은 문서 범위만(open/closed shadow 안 대상 제외, 프레임은 전환한
그 문서 기준). 바깥 정적 글자가 없고 항목 순서열도 같은 두 묶음(같은 시작 요일·같은 날 수의 두 달)은 구분하지
못한다. 지문 상한 뒤 변화는 못 잡는다. 좌표·selector 클릭은 고정 밖. 대상이 더 많은 다른 세트(무 CSS 의 전체 메뉴)가
예산을 먼저 쓰면 달력 세트가 omitted 될 수 있다(고정은 유지). 항목 쌍둥이(팝업 래퍼 형제 등)에서 충돌하는 바깥 대상이
래퍼 항목 바로 아래(사이 반복 행 없음)에 있으면 그 안 이전/다음 단추도 항목 단위로 고정된다.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: data.repeated 블록(groups 직렬화) 상한(글자).
REPEATED_MAX_CHARS = 3000
#: 대상 이름·표제 글자 상한.
NAME_LIMIT = 40
#: 반복 목록으로 보는 같은 틀 형제 수 하한(recipes.keys.LIST_MIN_ITEMS 와 같은 값 — 그 모듈은 class 를 틀에 넣어
#: li.select·li.before 를 다른 틀로 가르므로 재사용하지 않는다, 리뷰 N2).
LIST_MIN_ITEMS = 3
#: 계산에 넘기는 대상 수 상한(관찰 핸들 수).
MAX_TARGETS = 1000
#: 내부 고정 지문(묶음 정적 글자 전체) 상한(글자). 넘는 뒷부분의 변화는 잡지 않는다(한계).
PIN_LIMIT = 1000
#: 압축 이름(첫 낱말) 상한(글자).
COMPACT_WORD = 12
#: 대상별 국소성(R4): 묶음은 대상의 반복 행 목록(패널)·대상 자신(항목)에서 위로 이 단계 이내일 때만 그 대상을 받는다
#: (simple-browser-plugin targets.ts GROUP_CLIMB=6 — 달력 ul.day→패널 3단계, 날짜 a→패널 5단계).
GROUP_CLIMB = 6

#: 표제 마지막 단계에서 정적 후보가 여럿일 때(가격·배지·월 …): 하나를 고르지 않고 전부를 순서대로 이은 글자.
_AMBIG_NOTE = "후보 전부를 순서대로 잇는다"
_AMBIG_RET = "{text: rpClean(c.join(' ')), source: 'text', first: rpClean(c[0])}"

#: 블록 안내(고정 문구).
HOW = ("같은 틀이 반복되는 묶음(두 달 달력·상품 행 등)의 대상입니다. group 은 페이지 글자이며 서버 판정이 아닙니다"
       "(null 이면 x·y 위치로 구분). 고른 묶음의 id 로 누르고, 누른 뒤 입력칸 값 등으로 반영을 확인하세요. 최근 관찰의 "
       "id 만 쓰세요 — 관찰 뒤 묶음(달)이 바뀌면 누르지 않고 재관찰을 요구합니다.")

#: 압축했을 때 안내에 덧붙이는 한 줄.
COMPACT_HOW = ("압축됨(compact): 이름은 첫 낱말만(날짜면 숫자) — 가격·배지 등은 elements 나 extract 로 보세요. "
               "repeated 의 id 는 elements 에 없어도 누를 수 있습니다.")

#: 관찰·실행 직전 검증이 함께 쓰는 페이지 함수(같은 규칙 — 표제를 다르게 읽으면 정상 클릭을 거부한다).
#: sigOf: 태그+role(class 무시). isItem: 같은 틀 형제 ≥3 의 하나. headingOf(g): 묶음 표제 {text, source} 또는 null.
COMMON_JS = r"""
  const RP_SKIP = {SCRIPT:1, STYLE:1, NOSCRIPT:1, TEMPLATE:1, LINK:1, META:1, BASE:1, TITLE:1};
  const RP_ITAGS = {A:1, BUTTON:1, INPUT:1, SELECT:1, TEXTAREA:1, SUMMARY:1, OPTION:1, LABEL:1};
  const RP_IROLES = {button:1, link:1, checkbox:1, radio:1, textbox:1, combobox:1, menuitem:1, tab:1, 'switch':1,
                     searchbox:1, option:1, slider:1, spinbutton:1};
  const RP_LIMIT = __LIMIT__, RP_MIN = __MIN__, RP_FP_LIMIT = __FP_LIMIT__, RP_CLIMB = __CLIMB__;
  function rpSig(el) { const r = el.getAttribute('role'); return el.tagName + (r ? '[' + r.toLowerCase() + ']' : ''); }
  function rpIsItem(el) {
    const p = el.parentElement;
    if (!p) return false;
    const s = rpSig(el);
    let n = 0;
    for (const c of p.children) {
      if (!RP_SKIP[c.tagName] && rpSig(c) === s && ++n >= RP_MIN) return true;
    }
    return false;
  }
  function rpInteractive(el) {
    if (RP_ITAGS[el.tagName]) return true;
    if (RP_IROLES[(el.getAttribute('role') || '').toLowerCase()]) return true;
    if (el.hasAttribute('onclick')) return true;
    const ti = el.getAttribute('tabindex');
    if (ti !== null && ti !== '-1') return true;
    return !!el.isContentEditable;
  }
  function rpNorm(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
  function rpClean(s) { return rpNorm(s).slice(0, RP_LIMIT); }
  function rpHidden(el) {
    if (el.hidden) return true;
    const cs = getComputedStyle(el);
    return cs.display === 'none' || cs.visibility === 'hidden';
  }
  // node(요소)부터 stop 바로 아래까지: 대화형·반복 목록 항목·(숨김)·script 면 후보 아님.
  function rpBlockedUntil(node, stop, allowHidden) {
    let e = node;
    while (e && e !== stop) {
      if (RP_SKIP[e.tagName] || rpInteractive(e) || rpIsItem(e) || (!allowHidden && rpHidden(e))) return true;
      e = e.parentElement;
    }
    return false;
  }
  // node(요소)부터 g 바로 아래까지 막혔거나 g 밖이면 후보 아님.
  function rpBlocked(node, g) {
    if (!node || !g.contains(node)) return true;
    return rpBlockedUntil(node, g, false);
  }
  // node 안의 후보 글자 조각(문서 순서, 정리만 — 자르지 않는다).
  function rpPieces(node, g) {
    const doc = node.ownerDocument || document;
    const w = doc.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const out = [];
    let t;
    while ((t = w.nextNode())) {
      if (t.nodeValue && t.nodeValue.trim() && !rpBlocked(t.parentElement, g)) out.push(rpNorm(t.nodeValue));
    }
    return out;
  }
  function rpText(node, g) { return rpClean(rpPieces(node, g).join(' ')); }
  // aria-labelledby 참조 글자 — 참조에도 대화형·반복 목록 항목 제외(숨긴 참조는 ARIA 상 정상이라 허용, R1 R3).
  function rpLabelled(g) {
    const doc = g.ownerDocument || document;
    const lb = g.getAttribute('aria-labelledby');
    if (!lb) return '';
    return rpNorm(lb.split(/\s+/).map(function (id) {
      const r = id ? doc.getElementById(id) : null;
      if (!r) return '';
      const stop = g.contains(r) ? g : (doc.body || null);
      if (r !== g && rpBlockedUntil(r, stop, true)) return '';
      const w = doc.createTreeWalker(r, NodeFilter.SHOW_TEXT);
      const out = [];
      let t;
      while ((t = w.nextNode())) {
        if (t.nodeValue && t.nodeValue.trim() && !rpBlockedUntil(t.parentElement, r.parentElement, true)) out.push(t.nodeValue);
      }
      return out.join(' ');
    }).join(' '));
  }
  // 묶음의 정적 글자 후보를 g 의 바로 아래 자식 단위로 모은다(같은 자식 안 조각은 한 후보).
  function rpCandidates(g) {
    const doc = g.ownerDocument || document;
    const w = doc.createTreeWalker(g, NodeFilter.SHOW_TEXT);
    const out = [];
    let last = null, n;
    while ((n = w.nextNode())) {
      if (!n.nodeValue || !n.nodeValue.trim() || rpBlocked(n.parentElement, g)) continue;
      let top = n;
      while (top.parentNode && top.parentNode !== g) top = top.parentNode;
      if (top === last) out[out.length - 1].push(rpNorm(n.nodeValue));
      else { out.push([rpNorm(n.nodeValue)]); last = top; }
    }
    return out.map(function (p) { return p.join(' '); });
  }
  function headingOf(g) {
    const lt = rpClean(rpLabelled(g));
    if (lt) return {text: lt, source: 'aria'};
    const al = rpClean(g.getAttribute('aria-label'));
    if (al) return {text: al, source: 'aria'};
    const KINDS = [['caption', 'caption'], ['legend', 'legend'], ['h1,h2,h3,h4,h5,h6,[role=heading]', 'heading']];
    for (const k of KINDS) {
      for (const h of g.querySelectorAll(k[0])) {
        if (h !== g && rpBlocked(h, g)) continue;
        const t = rpText(h, g);
        if (t) return {text: t, source: k[1]};
      }
    }
    // 마지막: 묶음 안 비대화형·반복 목록 밖 정적 글자. 후보가 하나면 그 글자 — 여럿(가격·배지·월 …)이면 하나를
    // 고르지 않는다(키워드 추론 금지): __AMBIG__
    const c = rpCandidates(g);
    if (c.length === 1) return {text: rpClean(c[0]), source: 'text'};
    if (c.length > 1) return __AMBIG_RET__;
    return null;
  }
  function rpTok(s) {
    const n = String(s || '').normalize('NFKC').trim();
    return n ? n.split(/\s+/)[0].toLowerCase() : '';
  }
  // 묶음 안 반복 항목 단위(R2): 같은 틀 형제 ≥3 인 요소 중 대화형 안·숨김·script 밑이 아니고, 그 아래에 그런 항목이
  // 더 없는 가장 안쪽 항목(달력 칸 li·td, span 셋의 span, 요일 머리글 li). 문서 순서.
  function rpUnits(g) {
    const doc = g.ownerDocument || document;
    const counts = new Map(), dead = new Map(), items = [];
    function isItem(e) {
      const p = e.parentElement;
      if (!p) return false;
      let c = counts.get(p);
      if (!c) {
        c = new Map();
        for (const k of p.children) { if (!RP_SKIP[k.tagName]) { const s = rpSig(k); c.set(s, (c.get(s) || 0) + 1); } }
        counts.set(p, c);
      }
      return (c.get(rpSig(e)) || 0) >= RP_MIN;
    }
    function blocked(e) {  // e 자신 제외, e 의 조상(g 바로 아래까지)이 대화형·숨김·script 인가(캐시)
      const p = e.parentElement;
      if (!p || p === g) return false;
      if (dead.has(p)) return dead.get(p);
      const v = RP_SKIP[p.tagName] || rpInteractive(p) || rpHidden(p) || blocked(p);
      dead.set(p, !!v);
      return !!v;
    }
    const w = doc.createTreeWalker(g, NodeFilter.SHOW_ELEMENT);
    let e;
    while ((e = w.nextNode())) {
      if (RP_SKIP[e.tagName] || !isItem(e) || rpHidden(e) || blocked(e)) continue;
      items.push(e);
    }
    const inner = new Set();
    for (const it of items) { for (let a = it.parentElement; a && a !== g; a = a.parentElement) inner.add(a); }
    return items.filter(function (it) { return !inner.has(it); });
  }
  // 항목 하나의 신원 낱말: 대상(대화형 — 자신 또는 첫 대화형 자손)이 있으면 그 이름 첫 낱말(형제 글자 아님 — 가격·
  // 호버 글자·선택 표시는 비교하지 않는다), 대상이 없으면 null(호출자가 빈칸 또는 항목 글자로 정한다).
  function rpUnitKey(u) {
    let t = rpInteractive(u) ? u : null;
    if (!t) {
      const doc = u.ownerDocument || document;
      const w = doc.createTreeWalker(u, NodeFilter.SHOW_ELEMENT);
      let e;
      while ((e = w.nextNode())) { if (rpInteractive(e)) { t = e; break; } }
    }
    return t ? 'i:' + rpTok(t.getAttribute('aria-label') || t.textContent || t.getAttribute('placeholder')) : null;
  }
  // 순서열: 대상이 있는 항목은 대상 이름 첫 낱말. 대상 없는 항목은 같은 목록에 대상 항목이 있으면 빈칸('_' — 달력 앞
  // 빈칸, 그 안에 붙는 글자는 보지 않는다), 목록 전체에 대상이 없으면 항목 글자(span 셋의 달·요일 머리글 — 정적 글자).
  function rpSequence(units) {
    const keys = units.map(rpUnitKey), live = new Set();
    units.forEach(function (u, i) { if (keys[i] !== null) live.add(u.parentElement); });
    return units.map(function (u, i) {
      if (keys[i] !== null) return keys[i];
      // 신원은 표시용 40자 절단(rpClean)과 분리 — 긴 공통 접두어 뒤의 월 글자를 잃지 않게(R6). 항목당 200자로 묶어 둔다.
      return live.has(u.parentElement) ? '_' : 't:' + rpNorm(u.textContent).slice(0, 200);
    }).join('\u0001');
  }
  // 내부 고정 지문 = 묶음 신원(R2). 표시용 표제(headingOf)와 무관하게 핸들마다 저장·비교한다.
  //  (a) ARIA 이름 + 반복 항목 **바깥**의 정적 글자(대화형·숨김 제외, 문서 순서 — 월 표제 등)
  //  (b) 반복 항목들의 신원 낱말 순서열(빈칸·1·2·…·31) — span 셋처럼 (a) 가 비는 패널의 달 변경을 메운다
  //  (c) el 이 주어지면 그 대상이 든 항목의 순번(끝에 두되 상한에 잘리지 않게)
  // 항목 안 형제 글자(가격·툴팁·선택 표시)는 넣지 않는다 — 같은 달인데 가격 갱신·호버만으로 거부하던 과잉 거부(R5).
  function rpIdentity(g) {
    const parts = [];
    const lt = rpLabelled(g);
    if (lt) parts.push('L:' + lt);
    const al = rpNorm(g.getAttribute('aria-label'));
    if (al) parts.push('A:' + al);
    for (const c of rpCandidates(g)) parts.push(c);
    const units = rpUnits(g);
    parts.push('S:' + rpSequence(units));
    const idx = new Map();
    units.forEach(function (u, i) { idx.set(u, i); });
    return {idx: idx, g: g, body: parts.join('\n')};
  }
  // id = rpIdentity(g) (묶음마다 한 번 계산해 대상마다 재사용 — 대상 수 × 묶음 크기 비용을 피한다).
  function rpPinOf(id, el) {
    let k = -1;
    for (let e = el; e && e !== id.g && k < 0; e = e.parentElement) { if (id.idx.has(e)) k = id.idx.get(e); }
    const tail = el ? '\n#' + k : '';  // 순번은 끝에 — 본문을 상한에서 먼저 잘라 순번이 잘리지 않게
    return id.body.slice(0, RP_FP_LIMIT - tail.length) + tail;
  }
  function rpFingerprint(g, el) { return rpPinOf(rpIdentity(g), el); }
""".replace("__LIMIT__", str(NAME_LIMIT)).replace("__MIN__", str(LIST_MIN_ITEMS)).replace(
    "__FP_LIMIT__", str(PIN_LIMIT)).replace("__CLIMB__", str(GROUP_CLIMB)).replace("__AMBIG_RET__", _AMBIG_RET).replace("__AMBIG__", _AMBIG_NOTE)

#: 관찰 때 한 번: 대상 목록(css·이름·role) → 쌍둥이 묶음 집합.
REPEATED_JS = r"""
(args) => {
  __COMMON__
  function tok(name) {
    const s = String(name || '').normalize('NFKC').trim();
    return s ? s.split(/\s+/)[0].toLowerCase() : '';
  }
  function absPath(el) {
    const parts = [];
    let e = el;
    while (e && e.nodeType === 1 && e !== document.documentElement) {
      if (e === document.body) { parts.unshift('body'); break; }
      let i = 1, s = e.previousElementSibling;
      while (s) { if (s.tagName === e.tagName) i++; s = s.previousElementSibling; }
      parts.unshift(e.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      e = e.parentElement;
    }
    const css = parts.join(' > ');
    try { if (document.querySelectorAll(css).length === 1 && document.querySelector(css) === el) return css; } catch (x) {}
    return null;
  }
  function states(el) {
    const out = [];
    for (const k of ['selected', 'checked', 'pressed', 'expanded']) {
      if ((el.getAttribute('aria-' + k) || '').toLowerCase() === 'true') out.push(k);
    }
    const cur = (el.getAttribute('aria-current') || '').toLowerCase();
    if (cur && cur !== 'false') out.push('current');
    if (el.disabled || (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true') out.push('disabled');
    return out;
  }
  function levels(el) {  // 반복 목록 사슬(안쪽부터): [{item, list}]
    const out = [];
    let cur = el;
    for (let d = 0; cur && cur !== document.body && d < 8; d++, cur = cur.parentElement) {
      if (rpIsItem(cur)) out.push({item: cur, list: cur.parentElement});
    }
    return out;
  }
  function hasList(scope, ltag, isig) {
    for (const l of scope.getElementsByTagName(ltag)) {
      let n = 0;
      for (const c of l.children) { if (rpSig(c) === isig && ++n >= RP_MIN) return true; }
    }
    return false;
  }
  function panelOf(lv) {
    if (!lv.length) return null;
    const L = lv[0].list, isig = rpSig(lv[0].item);
    let A = L.parentElement;
    for (let d = 0; A && A !== document.body && A !== document.documentElement && d < 6; d++, A = A.parentElement) {
      const P = A.parentElement;
      if (!P) break;
      const s = rpSig(A), twins = [];
      for (const c of P.children) { if (!RP_SKIP[c.tagName] && rpSig(c) === s && hasList(c, L.tagName, isig)) twins.push(c); }
      if (twins.length >= 2) return twins;
    }
    return null;
  }
  const ts = [];
  for (const t of (args.targets || [])) {
    let el = null;
    try { el = document.querySelector(t.css); } catch (x) { el = null; }
    if (!el || el.getRootNode() !== document) continue;
    const k = tok(t.name);
    if (!k) continue;
    ts.push({i: t.i, el: el, tok: k, rn: String(t.role) + '\u0001' + String(t.name), lv: levels(el)});
  }
  function collide(groups, keyOf) {  // groups: [{members:[t]}] — 첫 낱말(keyOf)이 둘 이상의 묶음에 걸치나
    const seen = new Map();
    for (let gi = 0; gi < groups.length; gi++) {
      for (const t of groups[gi].members) {
        const k = keyOf ? keyOf(t) : t.tok;
        const s = seen.get(k);
        if (s === undefined) seen.set(k, gi); else if (s !== gi) return true;
      }
    }
    return false;
  }
  function rpDist(inner, g) { let d = 0; for (let e = inner; e && e !== g; e = e.parentElement) d++; return d; }
  function rowSig(item) { return rpSig(item.parentElement) + '>' + rpSig(item); }
  // 대상 t 의 묶음 g 안 가장 안쪽 반복 행(목록·항목) — 항목이 g 자신이면 행이 아니다.
  function rowOf(t, g) {
    for (const lv of t.lv) { if (g.contains(lv.list) && g !== lv.item) return {key: rowSig(lv.item), dist: rpDist(lv.list, g)}; }
    return null;
  }
  // 대상별 국소성(R4 R2 — sbp GROUP_CLIMB=6): 바깥 레이아웃 쌍둥이(#wrap/#footer 등)가 멀리 있는 대상(날짜·다음달·
  // 항공권 검색)을 흡수하지 않게, 묶음은 그 대상 가까이(위로 RP_CLIMB 단계 이내)에서만 대상을 받는다.
  //  패널: 그 대상의 가장 안쪽 반복 행 목록에서 위로 RP_CLIMB 이내. 항목: 그 대상 자신의 반복 항목(levels)이고 RP_CLIMB 이내.
  //  항목(R5 R2 — sbp rowElement): 대상의 **가장 안쪽** 반복 행(대상 자신 제외)이 그 항목일 때만 — 팝업 래퍼 형제(div.row ↔
  //  달력 팝업) 안의 바깥 날짜 표시(가장 안쪽 행 = div.bowl)·날짜(li)는 래퍼 항목 쌍둥이의 대상이 아니다.
  function innerRow(t) {  // 깊이 제한 없음(R6 — 8단계 아래 셀 대상도 셀 행을 찾는다), 대상마다 한 번
    if (t.ir === undefined) {
      t.ir = null;
      for (let e = t.el.parentElement; e && e !== document.body; e = e.parentElement) { if (rpIsItem(e)) { t.ir = e; break; } }
    }
    return t.ir;
  }
  function local(kind, t, g) {
    if (kind === 'panel') { const r = rowOf(t, g); return !!r && r.dist <= RP_CLIMB; }
    return (innerRow(t) === g && rpDist(t.el, g) <= RP_CLIMB) || cellOf(t, g);
  }
  // 항목 쌍둥이의 셀 대상(R6 R2): 가장 안쪽 반복 행이 항목 안의 셀(article>div×3·tr>td×3)인 대상(깊이 무관 — 같은 틀
  // 경로가 다른 항목에도 있어야 하므로 구조 쌍둥이 신호다). 충돌은 항목에서 대상까지의 틀 경로가 같은 셀 대상끼리만 본다(itemKey) — 상품 카드마다 같은 자리의 '담기'(DIV>BUTTON) 는 충돌, 팝업 래퍼 항목
  // (div.row) 의 바깥 '오는 날'(DIV>A) 은 팝업의 입력칸 '오는 날'(래퍼 항목 바로 아래 대상) 과 충돌하지 않는다(R5 R2 유지).
  function cellOf(t, g) { const r = innerRow(t); return !!r && r !== g && g.contains(r); }
  function relPath(el, g) { const out = []; for (let e = el; e && e !== g; e = e.parentElement) out.push(rpSig(e)); return out.join('<'); }
  function itemKey(g) {
    return function (t) { return innerRow(t) === g ? t.tok : t.tok + '\u0004' + relPath(t.el, g); };
  }
  // 대상이 g 안에서 keys 의 행 틀(목록 틀>항목 틀) 안에 있는가 — 항목이 하나뿐인 주(마지막 주 '31')도 같은 틀이면 행이다.
  function inRow(t, g, keys) {
    for (let e = t.el; e && e !== g; e = e.parentElement) {
      if (e.parentElement && keys.has(rowSig(e))) return true;
    }
    return false;
  }
  // 고정(R6 — 형성과 분리): 형성된 쌍둥이 g 가 담은 대상 중 g 안의 어떤 반복 행(sig 무관 — 셀 목록·대상 자신이 반복
  // 항목인 경우 포함) 안에 있는 대상. 항목 쌍둥이는 항목 자체가 행이라 안의 대상 전부. 행이 하나도 없는 대상(패널의
  // 이전/다음·표제·탭)만 고정 없음. 깊이 제한 없이 g 까지 올라간다(혼합 깊이 이종 행 — R5 R1).
  function rowBetween(t, g) {
    for (let e = t.el; e && e !== g; e = e.parentElement) { if (rpIsItem(e)) return true; }
    return false;
  }
  // 항목 쌍둥이는 항목 자체가 행: 항목에서 RP_CLIMB 이내 대상 전부. 그보다 깊은 대상은 다른 항목(형성된 쌍둥이)에 같은 첫
  // 낱말 대상이 있을 때만(fail-closed — 항목 사이 혼동) — 페이지 섹션 항목(div.submain) 깊숙한 달력 이전/다음·탭·입력칸이
  // 섹션 지문으로 고정되는 과잉 거부를 막는다. 더 안쪽 쌍둥이가 먼저 잡은 대상(done)은 건드리지 않는다.
  function pinTargets(kind, g, pool, others) {
    return pool.filter(function (t) {
      if (t.done || !g.contains(t.el)) return false;
      if (kind !== 'item') return rowBetween(t, g);
      return rpDist(t.el, g) <= RP_CLIMB || others.has(t.tok);
    });
  }
  // 표시 없는 고정(R6): 형성된 쌍둥이마다 아직 배정되지 않은 고정 대상을 그 쌍둥이 경로·지문으로 pins 에 싣는다.
  // elements 로 눌러도 표시된 대상과 같은 확인을 받는다. 경로를 못 만드는 쌍둥이는 건너뛴다(다른 묶음이 받을 수 있게).
  function hidePins(kind, formed, pool) {
    const toks = formed.map(function (one) {
      const s = new Set();
      if (kind === 'item') { for (const t of pool) { if (one.g.contains(t.el)) s.add(t.tok); } }
      return s;
    });
    formed.forEach(function (one, k) {
      const others = new Set();
      toks.forEach(function (s, j) { if (j !== k) s.forEach(function (x) { others.add(x); }); });
      const ts = pinTargets(kind, one.g, pool, others);
      if (!ts.length) return;
      const path = absPath(one.g);
      if (!path) return;
      for (const t of ts) { pins.push({i: t.i, path: path, fp: rpPinOf(one.id, t.el)}); t.done = true; }
    });
  }
  function finish(kind, conts, pool) {  // conts: 묶음 요소들, pool: 배정 가능한 대상
    const groups = [], keys0 = new Map();
    for (const g of conts) {
      const members = pool.filter(function (t) { return !t.done && g.contains(t.el) && local(kind, t, g); });
      if (kind === 'item') { const kf = itemKey(g); for (const t of members) keys0.set(t, kf(t)); }
      if (members.length) groups.push({g: g, members: members});
    }
    if (kind === 'item') {
      // 셀 대상은 같은 키가 다른 항목에도 있을 때만 묶음 대상(표시 변화 최소 — 충돌하지 않는 셀 대상은 고정만, hidePins).
      const cnt = new Map();
      groups.forEach(function (gr, gi) {
        for (const t of gr.members) { const k = keys0.get(t); const s = cnt.get(k) || new Set(); s.add(gi); cnt.set(k, s); }
      });
      for (const gr of groups) {
        gr.members = gr.members.filter(function (t) { return innerRow(t) === gr.g || cnt.get(keys0.get(t)).size > 1; });
      }
      for (let i = groups.length - 1; i >= 0; i--) { if (!groups[i].members.length) groups.splice(i, 1); }
    }
    const keyOf = kind === 'item' ? function (t) { return keys0.get(t); } : null;
    if (groups.length < 2 || !collide(groups, keyOf)) return null;
    // 캐러셀 복제: 표제와 (role, name) 열이 모두 같은 묶음 → 하나로 접는다(표제가 다르면 두 달 달력처럼 다른 묶음)
    let kept = [];
    const byKey = new Map();
    for (const gr of groups) {
      gr.members.sort(function (a, b) { return a.el === b.el ? 0 : (a.el.compareDocumentPosition(b.el) & 4 ? -1 : 1); });
      gr.h = headingOf(gr.g);
      gr.id = rpIdentity(gr.g);
      gr.fp = gr.id.body;
      const key = gr.fp + '\u0003' + gr.members.map(function (t) { return t.rn; }).join('\u0002');
      const prev = byKey.get(key);
      if (prev) { prev.copies++; prev.folded.push(gr); continue; }
      gr.copies = 0; gr.folded = []; byKey.set(key, gr); kept.push(gr);
    }
    if (kept.length < 2 || !collide(kept, keyOf)) return null;
    // 형성된 쌍둥이 전부(접힌 복제본 포함) — 아래 표시 거르기와 무관하게 고정 대상(R6).
    const formed = [];
    for (const gr of kept) { formed.push(gr); for (const f of gr.folded) formed.push(f); }
    if (kind === 'item') {
      // 항목 쌍둥이는 묶음이 정보를 줄 때만 — 대상 둘 이상을 묶거나 서로 다른 표제(상품명)가 있을 때.
      const heads = kept.map(function (gr) { return gr.h ? gr.h.text : ''; });
      const informative = kept.some(function (gr, i) {
        return gr.members.length >= 2 || (heads[i] && heads.filter(function (x) { return x === heads[i]; }).length === 1);
      });
      if (!informative) return null;
    }
    if (kind === 'panel') {
      // 행 고정(R4 R1-3): 묶음 사이 첫 낱말 충돌을 만든 대상의 행 틀(목록>항목) 전체가 묶음 대상이다 — 주마다 다른 ul.day
      // 모두, 자기 낱말 충돌과 무관하게. 행 밖 대상(이전/다음달·표제·탭)은 달이 바뀌어도 뜻이 같아 대상·고정에서 뺀다.
      const where = new Map();
      kept.forEach(function (gr, gi) { for (const t of gr.members) { const s = where.get(t.tok) || new Set(); s.add(gi); where.set(t.tok, s); } });
      const keys = new Set();
      kept.forEach(function (gr) {
        for (const t of gr.members) { if (where.get(t.tok).size > 1) { const r = rowOf(t, gr.g); if (r) keys.add(r.key); } }
      });
      // 신원 고정은 국소성과 분리(R5 R1): 국소성은 어떤 묶음이 형성되는지(먼저 차지)에만 쓰고, 형성된 패널이 담은 그 행
      // 틀의 대상은 깊이와 무관하게 전부 받는다 — 같은 달 안 일부 날짜 목록만 깊이 묻혀(section 7개 아래) 고정 없이 남으면
      // 표제가 바뀐 달의 옛 날짜가 다른 달에서 눌린다.
      const rowMembers = function (g) {
        return pool.filter(function (t) { return !t.done && g.contains(t.el) && inRow(t, g, keys); }).sort(function (a, b) {
          return a.el === b.el ? 0 : (a.el.compareDocumentPosition(b.el) & 4 ? -1 : 1);
        });
      };
      for (const gr of kept) {
        gr.members = rowMembers(gr.g);
        for (const f of gr.folded) f.members = rowMembers(f.g);
      }
      // 거르기 뒤 빈 쌍둥이(충돌 행이 없는 패널)는 빼고, 남은 쌍둥이가 2개 이상이고 여전히 충돌하면 그 세트를 낸다(R5 — N2
      // 의미 변경: 예전엔 세트를 통째로 버렸고 패널 항목 쌍둥이가 받았으나, 항목 쌍둥이는 이제 가장 안쪽 행이 그 항목인 대상만
      // 받으므로 버리면 달 정보가 사라진다). 아니면 표시 없이 고정만(R5 R1 fail-closed — hidePins).
      for (const gr of kept) gr.folded = gr.folded.filter(function (f) { return f.members.length > 0; });
      const live = kept.filter(function (gr) { return gr.members.length > 0; });
      if (live.length < kept.length) {
        if (live.length < 2 || !collide(live)) {
          hidePins(kind, formed, pool);
          return null;
        }
        kept = live;
      }
    }
    const out = [];
    for (const gr of kept) {
      const path = absPath(gr.g);
      if (!path) return null;  // 고정할 수 없는 묶음 — 내지 않는다
      const r = gr.g.getBoundingClientRect();
      const h = gr.h;
      out.push({path: path, x: Math.round(r.x), y: Math.round(r.y), heading: h ? h.text : null, first: h && h.first ? h.first : null,
                source: h ? h.source : null, copies: gr.copies,
                // 고정(pin): 남은 대상 전부 — 패널 쌍둥이는 위에서 행 안 대상(날짜)만 남겼다(이전/다음달 링크는
                // 달이 바뀌어도 뜻이 같아 같은 id 로 다시 누를 수 있다).
                members: gr.members.map(function (t) {
                  return {i: t.i, states: states(t.el), fp: rpPinOf(gr.id, t.el), pin: true};
                }),
                folded: [].concat.apply([], gr.folded.map(function (f) {
                  // 접힌 복제본도 자기 묶음의 경로·지문으로 고정한다(표시는 접어도 고정은 핸들마다, R1 R1).
                  const fpath = absPath(f.g);
                  return f.members.map(function (t) {
                    return {i: t.i, path: fpath, fp: rpPinOf(f.id, t.el), pin: true};
                  });
                }))});
      if (out[out.length - 1].folded.some(function (f) { return !f.path; })) return null;  // 고정 못 할 복제본
    }
    // 배정 끝: 낸 대상, 그리고 형성된 쌍둥이 안 반복 행 대상 전부(표시 없이 고정 — R6). 행 밖 대상(이전/다음달·표제·
    // 탭)은 다른 묶음이 받을 수 있게 남긴다.
    for (const gr of kept) { for (const t of gr.members) t.done = true; for (const f of gr.folded) for (const t of f.members) t.done = true; }
    hidePins(kind, formed, pool);
    return {kind: kind, groups: out};
  }
  const sets = [], pins = [], unformed = [];
  // (a) 패널 쌍둥이 — 각 대상의 가장 안쪽 반복 목록보다 위에서 같은 틀의 형제 묶음(같은 틀 목록을 담은)
  const panels = new Map();
  for (const t of ts) {
    const twins = panelOf(t.lv);
    if (!twins) continue;
    const key = twins[0];
    if (!panels.has(key)) panels.set(key, twins);
  }
  // 깊은(작은) 묶음 먼저(R4 R1-1) — 바깥 레이아웃 쌍둥이가 관찰 순서상 먼저 나와도 달 패널이 먼저 대상을 받는다.
  function depth(e) { let d = 0; while (e) { d++; e = e.parentElement; } return d; }
  const panelSets = Array.from(panels.values()).sort(function (a, b) { return depth(b[0]) - depth(a[0]); });
  for (const twins of panelSets) {
    const s = finish('panel', twins, ts);
    if (s) sets.push(s); else unformed.push(twins);
  }
  // (b) 항목 쌍둥이 — 남은 대상의 반복 목록 중 첫 낱말이 항목 사이에 겹치는 가장 안쪽 목록의 항목들
  const lists = [];
  function addList(item) {
    const key = rpSig(item);
    const k2 = lists.findIndex(function (x) { return x.list === item.parentElement && x.sig === key; });
    if (k2 < 0) lists.push({list: item.parentElement, sig: key});
  }
  for (const t of ts) {
    if (t.done) continue;
    for (const lv of t.lv) {
      // 항목이 대상 자신(버튼 목록)이면 묶음이 대상과 같아 구분 정보가 없다. body 바로 아래는 목록으로 보지 않는다.
      if (lv.item === t.el || lv.list === document.body) continue;
      addList(lv.item);
    }
    // 셀 대상(R6 R2): 가장 안쪽 행(셀)과 그 위 행(상품 항목)은 깊이 제한(levels 8) 밖이어도 후보 — 8단계 아래 '담기'.
    let n = 0;
    for (let e = innerRow(t); e && e.parentElement && e.parentElement !== document.body && n < 2; e = e.parentElement) {
      if (rpIsItem(e)) { addList(e); n++; }
    }
  }
  lists.sort(function (a, b) { return depth(b.list) - depth(a.list); });  // 안쪽(깊은) 목록 먼저
  for (const L of lists) {
    const items = [];
    for (const c of L.list.children) { if (!RP_SKIP[c.tagName] && rpSig(c) === L.sig) items.push(c); }
    const s = finish('item', items, ts);
    if (s) sets.push(s);
  }
  // (c) 남은 쌍둥이 간 혼동(R6 — 조정자 결정 A'): 국소성 때문에 형성되지 않은 패널 쌍둥이(바깥 레이아웃 등)라도, 아직 아무
  // 묶음에도 들지 않은 대상 중 첫 낱말이 그 쌍둥이 사이에 걸치는 대상은 그 쌍둥이로 고정한다(표시 없음). 레이아웃 흡수(이전/
  // 다음·검색·탭 — 짝 없는 대상)는 되살리지 않고, 다른 쌍둥이에 같은 이름이 있는 대상만 fail-closed.
  for (const twins of unformed) {
    // 걸침은 쌍둥이 안 대상 전부(이미 더 안쪽 묶음이 고정한 짝 포함)로 보고, 고정은 아직 아무 묶음에도 들지 않은 대상만.
    const grs = twins.map(function (g) { return {g: g, members: ts.filter(function (t) { return g.contains(t.el); })}; });
    const where = new Map();
    grs.forEach(function (gr, gi) { for (const t of gr.members) { const s = where.get(t.tok) || new Set(); s.add(gi); where.set(t.tok, s); } });
    for (const gr of grs) {
      const hit = gr.members.filter(function (t) { return !t.done && where.get(t.tok).size > 1; });
      if (!hit.length) continue;
      const path = absPath(gr.g);
      if (!path) continue;
      const id = rpIdentity(gr.g);
      for (const t of hit) { pins.push({i: t.i, path: path, fp: rpPinOf(id, t.el)}); t.done = true; }
    }
  }
  return {sets: sets, pins: pins};
}
""".replace("__COMMON__", COMMON_JS)

def first_token(name: Any) -> str:
    """이름 첫 낱말(NFKC·앞뒤 공백 정리·공백 기준·소문자). 빈 이름은 ''."""
    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    return text.split()[0].lower() if text else ""


def _short(value: Any) -> str:
    from actions.verification import safe_page_text

    return safe_page_text(" ".join(str(value or "").split()), NAME_LIMIT)


def _block_chars(groups: Sequence[Dict[str, Any]]) -> int:
    return len(json.dumps(list(groups), ensure_ascii=False, separators=(",", ":")))


async def annotate_repeated(page: Any, engine: Any, elements: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """관찰 결과의 대상(핸들)으로 쌍둥이 묶음을 계산해 data.repeated 블록을 만든다(없으면 None).

    핸들을 바꾸지 않는다(R12 — 고정은 perception.repeated_v1160). 페이지 계산이 실패하면 None — 관찰은 그대로 성공한다(부가 정보).
    """
    targets: List[Dict[str, Any]] = []
    handles: List[Any] = []
    toks: Dict[str, int] = {}
    for el in list(elements)[:MAX_TARGETS]:
        handle = engine.get_handle(getattr(el, "element_id", ""))
        if handle is None or handle.is_shadow or not handle.css_path or not str(handle.name or "").strip():
            continue
        tok = first_token(handle.name)
        if not tok:
            continue
        toks[tok] = toks.get(tok, 0) + 1
        targets.append({"i": len(handles), "css": handle.css_path, "name": handle.name, "role": handle.role})
        handles.append(handle)
    # 첫 낱말이 겹치는 대상이 둘 이상 없으면 쌍둥이 묶음이 날 수 없다 — 페이지 계산을 하지 않는다(평소 비용 0).
    if not any(n >= 2 for n in toks.values()):
        return None
    try:
        raw = await page.evaluate(REPEATED_JS, {"targets": targets})
    except Exception as exc:  # noqa: BLE001 - 부가 정보: 실패해도 관찰은 성공
        logger.debug("반복 묶음 계산 실패: %s", type(exc).__name__)
        return None
    sets = raw.get("sets") if isinstance(raw, dict) else None
    if not isinstance(sets, list) or not sets:
        return None
    return _build_block(sets, handles)


def _first_word(value: Any) -> str:
    """압축용 이름: 전체 이름을 먼저 살균·절단(_short)한 뒤 첫 낱말, 최대 COMPACT_WORD 자(NB2 — 가림 문구를 쪼개지 않는다)."""
    words = _short(value).split()
    return words[0][:COMPACT_WORD] if words else ""


def _set_groups(groups: List[Any], handles: List[Any], compact: bool) -> Optional[Dict[str, Any]]:
    """페이지가 준 세트 하나 → 공개 묶음 목록(보통 또는 압축). 형식이 어긋난 세트면 None(R6 형성 그대로)."""
    heads = [g.get("heading") if isinstance(g, dict) else None for g in groups]
    if compact:
        # 압축 표제(N3): 정적 후보 첫 조각(달 이름 — 뒤 안내문 제외)이 서로 다르면 그것, 아니면 전체 표제.
        firsts = [g.get("first") if isinstance(g, dict) and isinstance(g.get("first"), str) and g.get("first") else h
                  for g, h in zip(groups, heads)]
        uniq = [x for x in firsts if isinstance(x, str) and x]
        if len(set(uniq)) == len(uniq):
            heads = firsts
    out_groups: List[Dict[str, Any]] = []
    ranks: List[int] = []
    for g, head in zip(groups, heads):
        if not isinstance(g, dict) or not isinstance(g.get("path"), str) or not isinstance(g.get("members"), list):
            return None
        shown = isinstance(head, str) and head != "" and heads.count(head) == 1
        members = []
        for m in g["members"]:
            idx = m.get("i") if isinstance(m, dict) else None
            if not isinstance(idx, int) or not 0 <= idx < len(handles):
                continue
            h = handles[idx]
            if compact:
                member: Dict[str, Any] = {"id": h.element_id, "role": h.role, "name": _first_word(h.name)}
            else:
                member = {"id": h.element_id, "role": h.role, "name": _short(h.name)}
            states = [str(x) for x in (m.get("states") or []) if isinstance(x, str)]
            if states:
                member["states"] = states
            members.append(member)
            ranks.append(idx)
        for f in g.get("folded") or []:
            idx = f.get("i") if isinstance(f, dict) else None
            if not (isinstance(idx, int) and 0 <= idx < len(handles)):
                continue
            if not isinstance(f.get("path"), str):
                return None  # 경로 없는 복제본 — 세트를 내지 않는다(R6 형성 그대로)
        if not members:
            return None
        entry: Dict[str, Any] = {
            "group": _short(head) if shown else None,
            "group_source": str(g.get("source")) if shown else None,
            "x": int(g.get("x") or 0), "y": int(g.get("y") or 0),
        }
        roles = {m["role"] for m in members}
        if compact and len(roles) == 1:
            # 묶음 안 role 이 모두 같으면 묶음 단위로 한 번(R1-4 ②).
            entry["role"] = members[0]["role"]
            for m in members:
                del m["role"]
        entry["members"] = members
        if int(g.get("copies") or 0) > 0:
            entry["copies"] = int(g["copies"])
        out_groups.append(entry)
    if len(out_groups) < 2:
        return None
    return {"groups": out_groups, "count": len(ranks), "best": min(ranks)}


def _distinct_headings(groups: Sequence[Dict[str, Any]]) -> bool:
    """세트의 공개 묶음들이 모두 표제를 보이고 서로 다른가(WS-74 F2).

    _set_groups 가 내는 group 값(shown 규칙 — 비어 있지 않음·서로 다름, 압축이면 첫 조각 — 뒤 _short 40자)을 그대로 본다:
    원 표제가 달라도 표시 뒤 같아지거나 하나라도 null 이면 고를 근거가 없다.
    """
    heads = [g.get("group") for g in groups]
    return bool(heads) and all(isinstance(h, str) and h for h in heads) and len(set(heads)) == len(heads)


def _build_block(sets: List[Any], handles: List[Any]) -> Optional[Dict[str, Any]]:
    built: List[Dict[str, Any]] = []
    for s in sets:
        groups = s.get("groups") if isinstance(s, dict) else None
        if not isinstance(groups, list) or len(groups) < 2:
            continue
        normal = _set_groups(groups, handles, compact=False)
        if normal is None:
            continue
        compact = _set_groups(groups, handles, compact=True)
        normal["compact_groups"] = compact["groups"] if compact else normal["groups"]
        built.append(normal)
    if not built:
        return None
    # 크기(R4 R1-4): 다 담아 예산을 넘으면 먼저 전부 압축(첫 낱말·묶음 role·표제 첫 조각) — 그래도 넘는 세트는
    # 통째로 빼고(omitted) 다음 세트를 시도한다(탐욕). 압축 없이 들어가면 그대로 낸다.
    compact = _block_chars([g for b in built for g in b["groups"]]) > REPEATED_MAX_CHARS
    # WS-74(#190) F2: 실제로 보일 묶음들이 서로 다른 표제를 가진 세트(두 달 달력·상품 행 — 묶음을 고를 근거) 먼저,
    # 그 다음 큰 세트·앞 순위. 표시 전용 — 고정(repeated_v1160)·실행 직전 확인·batch 판정은 이 순서를 쓰지 않는다.
    built.sort(key=lambda b: (not _distinct_headings(b["compact_groups"] if compact else b["groups"]),
                              -b["count"], b["best"]))
    kept: List[Dict[str, Any]] = []
    omitted = 0
    for b in built:
        groups = b["compact_groups"] if compact else b["groups"]
        if _block_chars(kept + groups) <= REPEATED_MAX_CHARS:
            kept.extend(groups)
        else:
            omitted += len(groups)
    block: Dict[str, Any] = {"how": HOW + (" " + COMPACT_HOW if compact else ""), "groups": kept}
    if compact:
        block["compact"] = True
    if omitted:
        block["omitted"] = omitted
    return block


def fit_repeated(result: Any, max_chars: int, finalize: Optional[Callable[[Any], Any]] = None, *,
                 limit: Optional[int] = None) -> None:
    """최종 응답(주입 신호 + 요소 자르기 뒤)이 상한을 넘으면 반복 묶음을 뒤(우선순위 낮은 쪽)부터 통째로 뺀다.

    요소는 cap_result_size 가 자른다 — 그래도 못 들어가는 경우(작은 --max-result-chars)에만 뺀다. 뺀 수는
    `omitted` 에 더한다. 다 빼도 넘으면 블록을 지운다. saved_pages 는 호출자가 먼저 맞춘다(fit_saved_pages).
    limit(WS-68 R1) = 최종 자르기(cap_result_size)에 넘기는 표시용 설정 상한 — 같은 값으로 재야 hint 길이까지 든다.
    """
    data = getattr(result, "data", None)
    if not isinstance(data, dict) or not isinstance(data.get("repeated"), dict):
        return
    from interface.server_envelope import cap_result_size, envelope_json

    def _fits() -> bool:
        trial = result.model_copy(deep=True)
        if finalize is not None:
            finalize(trial)
        cap_result_size(trial, max_chars, limit=limit)
        return len(envelope_json(trial)) <= max_chars

    block = data["repeated"]
    groups = block.get("groups")
    if not isinstance(groups, list):
        return
    while not _fits():
        if not groups:
            del data["repeated"]
            return
        groups.pop()
        block["omitted"] = int(block.get("omitted") or 0) + 1
