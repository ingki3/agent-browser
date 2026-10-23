// v1.16.0 (6e449e2) perception.repeated.REPEATED_JS 원문 — WS-62 R6 기준 대비 신원 회귀 census 전용(제품 코드가 아니다).
(args) => {
  
  const RP_SKIP = {SCRIPT:1, STYLE:1, NOSCRIPT:1, TEMPLATE:1, LINK:1, META:1, BASE:1, TITLE:1};
  const RP_ITAGS = {A:1, BUTTON:1, INPUT:1, SELECT:1, TEXTAREA:1, SUMMARY:1, OPTION:1, LABEL:1};
  const RP_IROLES = {button:1, link:1, checkbox:1, radio:1, textbox:1, combobox:1, menuitem:1, tab:1, 'switch':1,
                     searchbox:1, option:1, slider:1, spinbutton:1};
  const RP_LIMIT = 40, RP_MIN = 3, RP_FP_LIMIT = 1000;
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
    // 고르지 않는다(키워드 추론 금지): 후보 전부를 순서대로 잇는다
    const c = rpCandidates(g);
    if (c.length === 1) return {text: rpClean(c[0]), source: 'text'};
    if (c.length > 1) return {text: rpClean(c.join(' ')), source: 'text'};
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
      gr.id = rpIdentity(gr.g);
      gr.fp = gr.id.body;
      const key = gr.fp + '\u0003' + gr.members.map(function (t) { return t.rn; }).join('\u0002');
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
                  return {i: t.i, states: states(t.el), fp: rpPinOf(gr.id, t.el),
                          pin: kind === 'item' || t.lv.some(function (lv) { return gr.g.contains(lv.list) && gr.g !== lv.item; })};
                }),
                folded: [].concat.apply([], gr.folded.map(function (f) {
                  // 접힌 복제본도 자기 묶음의 경로·지문으로 고정한다(표시는 접어도 고정은 핸들마다, R1 R1).
                  const fpath = absPath(f.g);
                  return f.members.map(function (t) {
                    return {i: t.i, path: fpath, fp: rpPinOf(f.id, t.el),
                            pin: kind === 'item' || t.lv.some(function (lv) { return f.g.contains(lv.list) && f.g !== lv.item; })};
                  });
                }))});
      if (out[out.length - 1].folded.some(function (f) { return !f.path; })) return null;  // 고정 못 할 복제본
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
