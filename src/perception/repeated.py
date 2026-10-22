"""반복 묶음 대상 구분 — 관찰 `data.repeated` (WS-62).

실사례(하나투어 달력, 2026-10-22): 두 달이 나란히 있어 `link "19"` 가 둘인데 어느 달인지 단서가 없어 모델이 다른
달을 눌렀다. 계약(ObservedElement)은 동결이라 관찰 결과 `data` 에 부가 정보를 붙인다.

* **발동(구조)**: 같은 틀(태그+role, class 무시)의 반복 목록(형제 ≥3)을 담은 같은 틀의 묶음이 2개 이상(패널 쌍둥이 —
  두 달 패널·두 표), 또는 반복 목록의 항목들(항목 쌍둥이 — 상품 행). 그리고 묶음 사이에 **이름 첫 낱말**(NFKC·공백
  기준, 예 `'22\\n\\n148만'` → `22`)이 같은 대상이 하나 이상 있을 때만 낸다(사용자 결정 A — 푸터 열·메가메뉴 열처럼
  이름이 다 다른 쌍둥이는 내지 않는다). 이름 없는 대상·shadow 안 대상은 뺀다. 표제와 (role, name) 열이 모두 같은
  묶음(캐러셀 복제)은 하나로 접고(`copies`), 접은 뒤 묶음이 2개 미만이거나 첫 낱말이 더 겹치지 않으면 내지 않는다
  (표제가 없고 이름 열까지 같은 두 묶음은 복제와 구분하지 못해 접힌다 — 한계).
* **모양**: `{how, groups:[{group, group_source, x, y, members:[{id, role, name, states?}], copies?}], omitted?}` —
  페이지 문자열은 모두 값(이름을 키로 쓰지 않는다 — 주입 신호·가림 규칙이 키를 보지 않는다, 리뷰 B1).
* **표제(group)**: 묶음의 aria-labelledby/aria-label → caption → legend → heading(h1~h6·role=heading) → 묶음 안 첫
  비대화형 글자. 대화형 요소(링크·버튼·입력 …)와 반복 목록 항목 안의 글자는 후보가 아니다(이전달/다음달 링크·요일
  머리글·날짜 제외). 같은 쌍둥이 묶음끼리 표제가 **서로 다를 때만** 싣고, 같거나 못 찾으면 null(+ 위치 x·y).
  `group_source` 로 출처를 밝힌다. 서버 판정이 아니다(how).
* **상태(states)**: ARIA 만(aria-selected/checked/pressed/current/expanded=참, disabled). class 이름은 추측하지 않는다 —
  ARIA 없는 위젯(하나투어)은 상태가 비고, 반영은 누른 뒤 입력칸 값으로 확인한다.
* **크기**: 블록(groups 직렬화) 3,000자. 넘으면 대상이 많은 쌍둥이 묶음부터 담고 못 담은 묶음은 통째로 빼
  `omitted` 에 개수. 응답 크기 상한 맞춤은 서버(`fit_repeated`, saved_pages 를 먼저 뺀다).
* **고정(B4)**: 낸(또는 상한으로 뺀) 묶음의 대상 핸들에 구분 조상 css 경로·표제 글자를 저장한다(패널 쌍둥이는 반복
  목록 항목 안 대상만 — 이전/다음달 링크는 달이 바뀌어도 같은 뜻이라 고정하지 않는다). 실행 직전 staleness
  검증(`actions.verification`)이 "아직 그 조상 안 + 표제 같음" 만 확인한다(알고리즘을 다시 돌리지 않는다). epoch 에
  기대지 않는다(달 넘김에도 epoch 는 그대로다). 자가 치유도 같은 묶음 후보만 쓴다(B5).

한계: 첫 낱말이 서로 다른 반복 대상은 잡지 않는다. 같은 문서 범위만(open/closed shadow 안 대상 제외, 프레임은 전환한
그 문서 기준). 표제 없는 묶음의 노드 재사용(가상 목록)은 위치·조상 비교만 한다. 좌표·selector 클릭은 고정 밖.
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

#: 블록 안내(고정 문구).
HOW = ("같은 틀이 반복되는 묶음(두 달 달력·상품 행 등)의 대상입니다. group 은 페이지 글자이며 서버 판정이 아닙니다"
       "(null 이면 x·y 위치로 구분). 고른 묶음의 id 로 누르고, 누른 뒤 입력칸 값 등으로 반영을 확인하세요. 최근 관찰의 "
       "id 만 쓰세요 — 관찰 뒤 묶음(달)이 바뀌면 누르지 않고 재관찰을 요구합니다.")

#: 관찰·실행 직전 검증이 함께 쓰는 페이지 함수(같은 규칙 — 표제를 다르게 읽으면 정상 클릭을 거부한다).
#: sigOf: 태그+role(class 무시). isItem: 같은 틀 형제 ≥3 의 하나. headingOf(g): 묶음 표제 {text, source} 또는 null.
COMMON_JS = r"""
  const RP_SKIP = {SCRIPT:1, STYLE:1, NOSCRIPT:1, TEMPLATE:1, LINK:1, META:1, BASE:1, TITLE:1};
  const RP_ITAGS = {A:1, BUTTON:1, INPUT:1, SELECT:1, TEXTAREA:1, SUMMARY:1, OPTION:1, LABEL:1};
  const RP_IROLES = {button:1, link:1, checkbox:1, radio:1, textbox:1, combobox:1, menuitem:1, tab:1, 'switch':1,
                     searchbox:1, option:1, slider:1, spinbutton:1};
  const RP_LIMIT = __LIMIT__, RP_MIN = __MIN__;
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
  function rpClean(s) { return String(s || '').replace(/\s+/g, ' ').trim().slice(0, RP_LIMIT); }
  function rpHidden(el) {
    if (el.hidden) return true;
    const cs = getComputedStyle(el);
    return cs.display === 'none' || cs.visibility === 'hidden';
  }
  // node(요소)부터 g 바로 아래까지: 대화형·반복 목록 항목·숨김·script 면 후보 아님. g 밖이면 후보 아님.
  function rpBlocked(node, g) {
    let e = node;
    while (e && e !== g) {
      if (RP_SKIP[e.tagName] || rpInteractive(e) || rpIsItem(e) || rpHidden(e)) return true;
      e = e.parentElement;
    }
    return e !== g;
  }
  function rpText(node, g) {
    const doc = node.ownerDocument || document;
    const w = doc.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const out = [];
    let t;
    while ((t = w.nextNode())) {
      if (t.nodeValue && t.nodeValue.trim() && !rpBlocked(t.parentElement, g)) out.push(t.nodeValue);
    }
    return rpClean(out.join(' '));
  }
  function headingOf(g) {
    const doc = g.ownerDocument || document;
    const lb = g.getAttribute('aria-labelledby');
    if (lb) {
      const t = rpClean(lb.split(/\s+/).map(function (id) {
        const r = id ? doc.getElementById(id) : null; return r ? r.textContent : '';
      }).join(' '));
      if (t) return {text: t, source: 'aria'};
    }
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
    // 마지막: 묶음 안 첫 비대화형·반복 목록 밖 텍스트 노드
    const w = doc.createTreeWalker(g, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      if (n.nodeValue && n.nodeValue.trim() && !rpBlocked(n.parentElement, g)) return {text: rpClean(n.nodeValue), source: 'text'};
    }
    return null;
  }
""".replace("__LIMIT__", str(NAME_LIMIT)).replace("__MIN__", str(LIST_MIN_ITEMS))

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
  function collide(groups) {  // groups: [{members:[t]}] — 첫 낱말이 둘 이상의 묶음에 걸치나
    const seen = new Map();
    for (let gi = 0; gi < groups.length; gi++) {
      for (const t of groups[gi].members) {
        const s = seen.get(t.tok);
        if (s === undefined) seen.set(t.tok, gi); else if (s !== gi) return true;
      }
    }
    return false;
  }
  function finish(kind, conts, pool) {  // conts: 묶음 요소들, pool: 배정 가능한 대상
    const groups = [];
    for (const g of conts) {
      const members = pool.filter(function (t) { return !t.done && g.contains(t.el); });
      if (members.length) groups.push({g: g, members: members});
    }
    if (groups.length < 2 || !collide(groups)) return null;
    // 캐러셀 복제: 표제와 (role, name) 열이 모두 같은 묶음 → 하나로 접는다(표제가 다르면 두 달 달력처럼 다른 묶음)
    const kept = [], byKey = new Map();
    for (const gr of groups) {
      gr.members.sort(function (a, b) { return a.el === b.el ? 0 : (a.el.compareDocumentPosition(b.el) & 4 ? -1 : 1); });
      gr.h = headingOf(gr.g);
      const key = (gr.h ? gr.h.text : '') + '\u0003' + gr.members.map(function (t) { return t.rn; }).join('\u0002');
      const prev = byKey.get(key);
      if (prev) { prev.copies++; prev.folded.push(gr); continue; }
      gr.copies = 0; gr.folded = []; byKey.set(key, gr); kept.push(gr);
    }
    if (kept.length < 2 || !collide(kept)) return null;
    if (kind === 'item') {
      // 항목 쌍둥이는 묶음이 정보를 줄 때만 — 대상 둘 이상을 묶거나 서로 다른 표제(상품명)가 있을 때.
      const heads = kept.map(function (gr) { return gr.h ? gr.h.text : ''; });
      const informative = kept.some(function (gr, i) {
        return gr.members.length >= 2 || (heads[i] && heads.filter(function (x) { return x === heads[i]; }).length === 1);
      });
      if (!informative) return null;
    }
    const out = [];
    for (const gr of kept) {
      const path = absPath(gr.g);
      if (!path) return null;  // 고정할 수 없는 묶음 — 내지 않는다
      const r = gr.g.getBoundingClientRect();
      const h = gr.h;
      out.push({path: path, x: Math.round(r.x), y: Math.round(r.y), heading: h ? h.text : null,
                source: h ? h.source : null, copies: gr.copies,
                // 고정(pin): 항목 쌍둥이는 전부, 패널 쌍둥이는 반복 목록 항목 안 대상만(날짜) — 패널의 이전/다음달
                // 링크는 달이 바뀌어도 뜻이 같아 같은 id 로 다시 누를 수 있다.
                members: gr.members.map(function (t) {
                  return {i: t.i, states: states(t.el),
                          pin: kind === 'item' || t.lv.some(function (lv) { return gr.g.contains(lv.list) && gr.g !== lv.item; })};
                }),
                folded: [].concat.apply([], gr.folded.map(function (f) {
                  return f.members.map(function (t) { return {i: t.i, path: absPath(f.g)}; });
                }))});
    }
    for (const gr of groups) for (const t of gr.members) t.done = true;
    return {kind: kind, groups: out};
  }
  const sets = [];
  // (a) 패널 쌍둥이 — 각 대상의 가장 안쪽 반복 목록보다 위에서 같은 틀의 형제 묶음(같은 틀 목록을 담은)
  const panels = new Map();
  for (const t of ts) {
    const twins = panelOf(t.lv);
    if (!twins) continue;
    const key = twins[0];
    if (!panels.has(key)) panels.set(key, twins);
  }
  for (const twins of panels.values()) {
    const s = finish('panel', twins, ts);
    if (s) sets.push(s);
  }
  // (b) 항목 쌍둥이 — 남은 대상의 반복 목록 중 첫 낱말이 항목 사이에 겹치는 가장 안쪽 목록의 항목들
  const lists = [];
  for (const t of ts) {
    if (t.done) continue;
    for (const lv of t.lv) {
      // 항목이 대상 자신(버튼 목록)이면 묶음이 대상과 같아 구분 정보가 없다. body 바로 아래는 목록으로 보지 않는다.
      if (lv.item === t.el || lv.list === document.body) continue;
      const key = rpSig(lv.item);
      const k2 = lists.findIndex(function (x) { return x.list === lv.list && x.sig === key; });
      if (k2 < 0) lists.push({list: lv.list, sig: key});
    }
  }
  function depth(e) { let d = 0; while (e) { d++; e = e.parentElement; } return d; }
  lists.sort(function (a, b) { return depth(b.list) - depth(a.list); });  // 안쪽(깊은) 목록 먼저
  for (const L of lists) {
    const items = [];
    for (const c of L.list.children) { if (!RP_SKIP[c.tagName] && rpSig(c) === L.sig) items.push(c); }
    const s = finish('item', items, ts);
    if (s) sets.push(s);
  }
  return {sets: sets};
}
""".replace("__COMMON__", COMMON_JS)

#: 실행 직전(staleness)·치유 후보 확인: 대상이 아직 그 구분 조상 안이고 표제가 같은가.
GROUP_CHECK_JS = r"""
  __COMMON__
  function groupCheck(el, groupPath, groupHeading) {
    let g = null;
    try { g = document.querySelector(groupPath); } catch (x) { g = null; }
    if (!g) return {ok: false, now: null, why: 'group_gone'};
    if (!el || !g.contains(el)) return {ok: false, now: null, why: 'outside_group'};
    if (groupHeading === null || groupHeading === undefined) return {ok: true, now: null, why: ''};
    const h = headingOf(g);
    const now = h ? h.text : '';
    return {ok: now === groupHeading, now: now, why: now === groupHeading ? '' : 'heading_changed'};
  }
""".replace("__COMMON__", COMMON_JS)

#: 치유 후보 거르기: css 경로 목록 → 같은 묶음인가(bool 목록).
GROUP_FILTER_JS = r"""
(args) => {
  __CHECK__
  return (args.paths || []).map(function (p) {
    let el = null;
    try { el = p ? document.querySelector(p) : null; } catch (x) { el = null; }
    return !!(el && groupCheck(el, args.groupPath, args.groupHeading).ok);
  });
}
""".replace("__CHECK__", GROUP_CHECK_JS)


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

    부수 효과: 묶음에 든 대상 핸들에 group_path·group_heading 을 저장한다(실행 직전 고정).
    페이지 계산이 실패하면 None — 관찰은 그대로 성공한다(부가 정보).
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


def _build_block(sets: List[Any], handles: List[Any]) -> Optional[Dict[str, Any]]:
    built: List[Dict[str, Any]] = []
    for s in sets:
        groups = s.get("groups") if isinstance(s, dict) else None
        if not isinstance(groups, list) or len(groups) < 2:
            continue
        heads = [g.get("heading") if isinstance(g, dict) else None for g in groups]
        out_groups: List[Dict[str, Any]] = []
        pins: List[Any] = []
        ranks: List[int] = []
        ok = True
        for g, head in zip(groups, heads):
            if not isinstance(g, dict) or not isinstance(g.get("path"), str) or not isinstance(g.get("members"), list):
                ok = False
                break
            shown = isinstance(head, str) and head != "" and heads.count(head) == 1
            members = []
            for m in g["members"]:
                idx = m.get("i") if isinstance(m, dict) else None
                if not isinstance(idx, int) or not 0 <= idx < len(handles):
                    continue
                h = handles[idx]
                member: Dict[str, Any] = {"id": h.element_id, "role": h.role, "name": _short(h.name)}
                states = [str(x) for x in (m.get("states") or []) if isinstance(x, str)]
                if states:
                    member["states"] = states
                members.append(member)
                if m.get("pin", True) is not False:
                    pins.append((h, g["path"], head if shown else None))
                ranks.append(idx)
            for f in g.get("folded") or []:
                idx = f.get("i") if isinstance(f, dict) else None
                if isinstance(idx, int) and 0 <= idx < len(handles) and isinstance(f.get("path"), str):
                    pins.append((handles[idx], f["path"], None))
            if not members:
                ok = False
                break
            entry: Dict[str, Any] = {
                "group": _short(head) if shown else None,
                "group_source": str(g.get("source")) if shown else None,
                "x": int(g.get("x") or 0), "y": int(g.get("y") or 0),
                "members": members,
            }
            if int(g.get("copies") or 0) > 0:
                entry["copies"] = int(g["copies"])
            out_groups.append(entry)
        if ok and len(out_groups) >= 2:
            built.append({"groups": out_groups, "pins": pins, "count": len(ranks), "best": min(ranks)})
    if not built:
        return None
    # 고정은 상한으로 뺀 묶음까지 — elements 에 실린 같은 대상을 눌러도 같은 확인을 받는다.
    for b in built:
        for h, path, head in b["pins"]:
            h.group_path = path
            h.group_heading = head
    built.sort(key=lambda b: (-b["count"], b["best"]))
    kept: List[Dict[str, Any]] = []
    omitted = 0
    for b in built:
        if _block_chars(kept + b["groups"]) <= REPEATED_MAX_CHARS:
            kept.extend(b["groups"])
        else:
            omitted += len(b["groups"])
    block: Dict[str, Any] = {"how": HOW, "groups": kept}
    if omitted:
        block["omitted"] = omitted
    return block


def fit_repeated(result: Any, max_chars: int, finalize: Optional[Callable[[Any], Any]] = None) -> None:
    """최종 응답(주입 신호 + 요소 자르기 뒤)이 상한을 넘으면 반복 묶음을 뒤(우선순위 낮은 쪽)부터 통째로 뺀다.

    요소는 cap_result_size 가 자른다 — 그래도 못 들어가는 경우(작은 --max-result-chars)에만 뺀다. 뺀 수는
    `omitted` 에 더한다. 다 빼도 넘으면 블록을 지운다. saved_pages 는 호출자가 먼저 맞춘다(fit_saved_pages).
    """
    data = getattr(result, "data", None)
    if not isinstance(data, dict) or not isinstance(data.get("repeated"), dict):
        return
    from interface.server_envelope import cap_result_size, envelope_json

    def _fits() -> bool:
        trial = result.model_copy(deep=True)
        if finalize is not None:
            finalize(trial)
        cap_result_size(trial, max_chars)
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
