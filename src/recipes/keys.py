"""PageKey·Target 계산과 대상 찾기 (WS-38 단계 1, 설계 §1·§4-1).

* **PageKey** = 출처 + URL 패턴 + 골격 서명(깊이 4, 같은 모양 형제 ≥3 접기, 숨김·script 제외,
  해시 12자) + 준비 기준(보이는 상호작용 요소 수).
* **Target** 3종
  - slot: 반복 목록(같은 틀 형제 ≥3) 안의 대상 — 목록 서명(컨테이너 조상 사슬 + 항목 틀) +
    항목 안 상대 경로 + 대상 틀(모양·역할·href 패턴) + **순번**.
  - ui: 목록 밖 고정 컨트롤 — 역할군 + 정규화 이름(정확히 같음) + 랜드마크 + 위치(450px 가드).
  - identity: 식별값(쿼리 id·요소 id·data-testid) 정확히 같음. 광고·가격·순위 대상은 거부.
* **locate**: 정확히 1개가 아니면 실패(target_not_found / target_ambiguous). 텍스트 유사도·
  좌표 재생은 쓰지 않는다(M-1 오일치 사례: 해외여행→해외여행자보험, 같은 자리 다른 광고).

페이지 계산은 **한 번의 page.evaluate**(KEYS_JS)로 한다 — 골격·준비 수·대상 기술·찾기를 함께.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlsplit

from perception.sanitizer import ACCESSIBLE_NAME_JS

#: ui 대상 위치 가드(px). 기록 위치에서 이보다 멀리 있는 같은 이름 요소는 다른 요소로 본다(M-1 최선 전략).
UI_GUARD_PX = 450
#: 반복 목록으로 보는 같은 틀 형제 수 하한.
LIST_MIN_ITEMS = 3
#: 골격 깊이(body 아래).
SKELETON_DEPTH = 4

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_LONG_ID = re.compile(r"^[A-Za-z0-9]{16,}$")


class TargetError(ValueError):
    """요청한 대상 종류(pin)로 저장할 수 없다(예: 광고·가격·순위 대상의 identity)."""


def _segment(seg: str) -> str:
    if seg.isdigit():
        return "{n}"
    if _UUID.match(seg) or _LONG_ID.match(seg):
        return "{id}"
    return seg


def path_pattern(path: str, query: str) -> str:
    """경로 조각의 숫자 → {n}, 16자 이상 영숫자·UUID → {id}, 쿼리는 키 이름만 정렬(값 버림)."""
    segs = (path or "/").split("/")
    out = "/".join(_segment(s) for s in segs) or "/"
    names = sorted({k for k, _ in parse_qsl(query or "", keep_blank_values=True)})
    return out + ("?" + "&".join(names) if names else "")


def url_pattern(url: str) -> str:
    """URL → 패턴 문자열(호스트 소문자 + 경로 패턴 + 쿼리 키). `#` 이하는 버린다."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    host = (parts.netloc or "").lower()
    return host + path_pattern(parts.path, parts.query)


def origin_of(url: str) -> str:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


# ---------------------------------------------------------------------------
# 페이지 스크립트 (한 번의 evaluate)
# ---------------------------------------------------------------------------

KEYS_JS = r"""
(args) => {
  __ACCESSIBLE_NAME__
  const SKIP = {SCRIPT:1, STYLE:1, NOSCRIPT:1, TEMPLATE:1, LINK:1, META:1, BASE:1, TITLE:1};
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],' +
    '[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=textbox],' +
    '[role=searchbox],[role=combobox],[role=option],[role=switch]';
  const AD_RE = /(^|[\s_-])(ad|ads|adv|advert|advertise|advertisement|sponsor|sponsored|promotion|powerclick|banner)([\s_-]|$)|광고/i;
  const PRICE_RE = /(\d[\d,.]*\s*(원|₩|won|krw|usd|달러|\$))|((₩|\$)\s*\d)/i;
  const RANK_RE = /^\s*(no\.|#)?\s*\d+\s*(위|등|\.)?\s*$/i;
  const ID_KEYS = ['id','pcode','pid','no','item','itemid','productid','product_id','goodsno',
                   'goods_no','code','prdno','seq','idx','aid','articleid','article_id'];
  const DEPTH = __DEPTH__, MIN_ITEMS = __MIN_ITEMS__, GUARD = __GUARD__;

  function h12(str) {
    let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
    for (let i = 0; i < str.length; i++) {
      const ch = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ ch, 2654435761);
      h2 = Math.imul(h2 ^ ch, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507);
    h1 ^= Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507);
    h2 ^= Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    const v = 4294967296 * (2097151 & h2) + (h1 >>> 0);
    return v.toString(16).padStart(14, '0').slice(-12);
  }
  function norm(s) { return String(s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
  function gone(el) { return el.getClientRects().length === 0; }
  function hiddenStyle(el) {
    if (el.hidden) return true;
    const cs = getComputedStyle(el);
    return cs.display === 'none' || cs.visibility === 'hidden';
  }
  // 관찰 엔진(perception.sanitizer inferRole)과 같은 표
  function inferRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
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
  // 실행 직전 검증(actions.verification STALENESS_CHECK_SCRIPT)과 같은 표 — 핸들 role 로 쓴다
  function staleRole(el) {
    let role = el.getAttribute('role');
    if (role) return role.toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      return (t === 'checkbox') ? 'checkbox' : (t === 'radio') ? 'radio'
        : (t === 'submit' || t === 'button' || t === 'reset') ? 'button'
        : (t === 'search') ? 'searchbox' : (t === 'range') ? 'slider'
        : (t === 'number') ? 'spinbutton' : (t === 'hidden') ? 'none' : 'textbox';
    }
    return 'generic';
  }
  const GROUPS = {link: 'link', button: 'button', menuitem: 'button', tab: 'tab', textbox: 'text',
                  searchbox: 'text', combobox: 'combo', checkbox: 'check', switch: 'check',
                  radio: 'radio', option: 'option'};
  function group(role) { return GROUPS[role] || role; }
  function clsOf(el) {
    const raw = typeof el.className === 'string' ? el.className : (el.getAttribute('class') || '');
    return raw.trim().split(/\s+/).filter(c => c && !/\d{2,}/.test(c)).sort();
  }
  function sig(el) {
    const c = clsOf(el);
    let s = el.tagName.toLowerCase() + (c.length ? '.' + c.join('.') : '');
    const r = el.getAttribute('role');
    if (r) s += '[' + r.toLowerCase() + ']';
    return s;
  }
  function tpl(el, d) {
    const s = sig(el);
    if (d <= 0) return s;
    const kids = new Set();
    for (const c of el.children) { if (!SKIP[c.tagName]) kids.add(tpl(c, d - 1)); }
    return kids.size ? s + '(' + Array.from(kids).sort().join(',') + ')' : s;
  }
  function fold(list) {
    const cnt = new Map();
    for (const k of list) cnt.set(k, (cnt.get(k) || 0) + 1);
    const out = [], seen = new Set();
    for (const k of list) {
      if (cnt.get(k) >= MIN_ITEMS) { if (!seen.has(k)) { seen.add(k); out.push(k + '*'); } }
      else out.push(k);
    }
    return out.join(',');
  }
  function shape(el, d) {
    let s = el.tagName.toLowerCase();
    const r = el.getAttribute('role');
    if (r) s += '[' + r.toLowerCase() + ']';
    if (d >= DEPTH) return s;
    const kids = [];
    for (const c of el.children) {
      if (SKIP[c.tagName] || hiddenStyle(c)) continue;
      kids.push(shape(c, d + 1));
    }
    return kids.length ? s + '(' + fold(kids) + ')' : s;
  }
  function skeleton() {
    const body = document.body;
    if (!body) return h12('');
    const kids = [];
    for (const c of body.children) {
      if (SKIP[c.tagName] || hiddenStyle(c)) continue;
      kids.push(shape(c, 1));
    }
    return h12(fold(kids));
  }
  function readyCount() {
    let n = 0;
    for (const el of document.querySelectorAll(SEL)) { if (!gone(el)) n++; if (n >= 5000) break; }
    return n;
  }
  function seg(s) {
    if (/^\d+$/.test(s)) return '{n}';
    if (/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(s)
        || /^[A-Za-z0-9]{16,}$/.test(s)) return '{id}';
    return s;
  }
  function pathPat(u) {
    const p = (u.pathname || '/').split('/').map(seg).join('/') || '/';
    const names = Array.from(new Set(Array.from(u.searchParams.keys()))).sort();
    return p + (names.length ? '?' + names.join('&') : '');
  }
  function hrefPat(el) {
    const h = el.getAttribute('href');
    if (h === null) return null;
    let u;
    try { u = new URL(h, location.href); } catch (e) { return 'bad'; }
    if (u.protocol === 'javascript:') return 'js';
    if (u.origin !== location.origin) return 'ext';
    return pathPat(u);
  }
  const LANDMARK_TAGS = {HEADER: 'header', NAV: 'nav', MAIN: 'main', ASIDE: 'aside', FOOTER: 'footer',
                         FORM: 'form', DIALOG: 'dialog'};
  const LANDMARK_ROLES = {banner: 'header', navigation: 'nav', main: 'main', complementary: 'aside',
                          contentinfo: 'footer', search: 'search', form: 'form', dialog: 'dialog'};
  function landmark(el) {
    let e = el.parentElement;
    while (e && e !== document.body) {
      const r = (e.getAttribute('role') || '').toLowerCase();
      if (LANDMARK_ROLES[r]) return LANDMARK_ROLES[r];
      if (LANDMARK_TAGS[e.tagName]) return LANDMARK_TAGS[e.tagName];
      e = e.parentElement;
    }
    return '';
  }
  function center(el) {
    const r = el.getBoundingClientRect();
    return [Math.round(r.left + r.width / 2 + window.scrollX), Math.round(r.top + r.height / 2 + window.scrollY)];
  }
  function nthOf(el) {
    let i = 1, s = el.previousElementSibling;
    while (s) { if (s.tagName === el.tagName) i++; s = s.previousElementSibling; }
    return el.tagName.toLowerCase() + ':nth-of-type(' + i + ')';
  }
  function cssPath(el) {
    const parts = [];
    let e = el;
    while (e && e.nodeType === 1 && e !== document.documentElement) {
      if (e === document.body) { parts.unshift('body'); break; }
      parts.unshift(nthOf(e));
      e = e.parentElement;
    }
    const css = parts.join(' > ');
    try { if (document.querySelectorAll(css).length === 1) return css; } catch (e2) {}
    return null;
  }
  function chainOf(c) {
    const out = [];
    let e = c, d = 0;
    while (e && e !== document.documentElement && d < 6) { out.push(sig(e)); if (e === document.body) break; e = e.parentElement; d++; }
    return out.join('<');
  }
  function cssSel(el) {
    const c = clsOf(el);
    return el.tagName.toLowerCase() + c.map(x => '.' + CSS.escape(x)).join('');
  }
  function listItems(container, itplHash) {
    const items = [];
    for (const k of container.children) {
      if (SKIP[k.tagName] || gone(k)) continue;
      if (h12(tpl(k, 2)) === itplHash) items.push(k);
    }
    return items;
  }
  function findItem(t) {
    let cur = t;
    for (let depth = 0; cur && cur !== document.body && depth < 8; depth++, cur = cur.parentElement) {
      const p = cur.parentElement;
      if (!p) break;
      const mine = h12(tpl(cur, 2));
      const items = listItems(p, mine);
      if (items.length >= MIN_ITEMS && items.indexOf(cur) >= 0) return {item: cur, container: p, itpl: mine, items};
    }
    return null;
  }
  function relPath(item, t) {
    const parts = [];
    let e = t;
    while (e && e !== item) { parts.unshift(nthOf(e)); e = e.parentElement; }
    return e === item ? parts.join(' > ') : null;
  }
  function identityOf(el, role, nameNorm) {
    let e = el, d = 0;
    while (e && e !== document.body && d < 12) {
      const c = (typeof e.className === 'string' ? e.className : '') + ' ' + (e.id || '');
      if (AD_RE.test(c)) return {refused: '광고 영역의 대상'};
      if (/(^|[\s_-])rank(ing)?([\s_-]|$)/i.test(c)) return {refused: '순위 영역의 대상'};
      e = e.parentElement; d++;
    }
    if (PRICE_RE.test(nameNorm)) return {refused: '가격 대상'};
    if (RANK_RE.test(nameNorm) || /\d+\s*위/.test(nameNorm)) return {refused: '순위 대상'};
    const base = {group: group(role), tag: el.tagName.toLowerCase(), name: nameNorm};
    const href = el.getAttribute('href');
    if (href !== null) {
      try {
        const u = new URL(href, location.href);
        for (const [k, v] of u.searchParams.entries()) {
          if (ID_KEYS.indexOf(k.toLowerCase()) >= 0 && v) {
            return {id: Object.assign({by: 'query', path: u.origin === location.origin ? u.pathname : u.origin + u.pathname,
                                       key: k, value: v}, base)};
          }
        }
      } catch (e3) {}
    }
    if (el.id && !/\d{4,}/.test(el.id)) return {id: Object.assign({by: 'attr', attr: 'id', value: el.id}, base)};
    const tid = el.getAttribute('data-testid');
    if (tid) return {id: Object.assign({by: 'attr', attr: 'data-testid', value: tid}, base)};
    return {refused: '식별값(쿼리 id·요소 id·data-testid) 없음'};
  }
  function describe(t) {
    const role = inferRole(t);
    const nameRaw = String(accessibleName(t) || '');
    const nameNorm = norm(nameRaw);
    const tagName = t.tagName.toLowerCase();
    const typ = (t.getAttribute('type') || '').toLowerCase();
    const ac = (t.getAttribute('autocomplete') || '').toLowerCase();
    const out = {
      role, name: nameRaw.slice(0, 120), tag: tagName, sig: sig(t), href_pat: hrefPat(t),
      secret: (tagName === 'input' && typ === 'password') || ac.indexOf('password') >= 0,
      ui: {group: group(role), name: nameNorm, landmark: landmark(t), pos: center(t), href_pat: hrefPat(t)},
      slot: null, identity: null, identity_refused: null,
    };
    const li = findItem(t);
    if (li) {
      const rel = relPath(li.item, t);
      if (rel !== null) {
        out.slot = {chain: h12(chainOf(li.container)), csel: cssSel(li.container), itpl: li.itpl,
                    ordinal: li.items.indexOf(li.item), rel: rel, tsig: sig(t), role: role,
                    href_pat: hrefPat(t)};
      }
    }
    const idn = identityOf(t, role, nameNorm);
    if (idn.id) out.identity = idn.id; else out.identity_refused = idn.refused;
    return out;
  }
  function found(el, extra) {
    const css = cssPath(el);
    if (!css) return {ok: false, reason: 'target_not_found', detail: '대상 경로를 만들 수 없음'};
    return Object.assign({ok: true, css, role: staleRole(el), name: String(accessibleName(el) || ''),
                          href: el.href || el.getAttribute('href') || null, pos: center(el)}, extra || {});
  }
  function locateSlot(s) {
    let conts;
    try { conts = document.querySelectorAll(s.csel); } catch (e) { conts = []; }
    const hits = [];
    for (const c of conts) {
      if (h12(chainOf(c)) !== s.chain) continue;
      const items = listItems(c, s.itpl);
      if (items.length >= MIN_ITEMS) hits.push(items);
    }
    if (!hits.length) return {ok: false, reason: 'target_not_found', detail: '같은 틀의 목록이 없음'};
    if (hits.length > 1) return {ok: false, reason: 'target_ambiguous', detail: '같은 틀의 목록 ' + hits.length + '개'};
    const items = hits[0];
    if (s.ordinal >= items.length)
      return {ok: false, reason: 'target_not_found', detail: '순번 ' + s.ordinal + ' 이 목록 길이 ' + items.length + ' 밖'};
    const item = items[s.ordinal];
    let ts;
    try { ts = s.rel ? item.querySelectorAll(':scope > ' + s.rel) : [item]; } catch (e) { ts = []; }
    if (ts.length !== 1) return {ok: false, reason: 'target_not_found', detail: '항목 안 대상 경로 불일치'};
    const t = ts[0];
    if (sig(t) !== s.tsig) return {ok: false, reason: 'target_not_found', detail: '틀 불일치(모양)'};
    if (inferRole(t) !== s.role) return {ok: false, reason: 'target_not_found', detail: '틀 불일치(역할)'};
    if (hrefPat(t) !== s.href_pat) return {ok: false, reason: 'target_not_found', detail: '틀 불일치(href 패턴)'};
    return found(t, {items: items.length});
  }
  function locateUi(u) {
    const cands = [];
    let best = Infinity;
    for (const el of document.querySelectorAll(SEL)) {
      if (gone(el)) continue;
      if (group(inferRole(el)) !== u.group) continue;
      if (norm(accessibleName(el)) !== u.name) continue;
      if (hrefPat(el) !== (u.href_pat === undefined ? null : u.href_pat)) continue;
      if (landmark(el) !== u.landmark) continue;
      const p = center(el);
      const d = Math.hypot(p[0] - u.pos[0], p[1] - u.pos[1]);
      best = Math.min(best, d);
      if (d <= GUARD) cands.push(el);
    }
    if (best === Infinity) return {ok: false, reason: 'target_not_found', detail: '같은 역할·이름·랜드마크 요소 없음'};
    if (!cands.length)
      return {ok: false, reason: 'target_not_found', detail: '가장 가까운 후보가 ' + Math.round(best) + 'px — ' + GUARD + 'px 가드 밖'};
    if (cands.length > 1) return {ok: false, reason: 'target_ambiguous', detail: '같은 역할·이름 요소 ' + cands.length + '개'};
    return found(cands[0]);
  }
  function locateIdentity(t) {
    let cands = [];
    if (t.by === 'query') {
      for (const el of document.querySelectorAll('[href]')) {
        if (gone(el) || el.tagName.toLowerCase() !== t.tag || group(inferRole(el)) !== t.group) continue;
        let u;
        try { u = new URL(el.getAttribute('href'), location.href); } catch (e) { continue; }
        const path = u.origin === location.origin ? u.pathname : u.origin + u.pathname;
        if (path === t.path && u.searchParams.get(t.key) === t.value) cands.push(el);
      }
    } else {
      let nodes = [];
      try { nodes = document.querySelectorAll('[' + t.attr + '="' + CSS.escape(t.value) + '"]'); } catch (e) {}
      for (const el of nodes) {
        if (!gone(el) && el.tagName.toLowerCase() === t.tag && group(inferRole(el)) === t.group) cands.push(el);
      }
    }
    if (cands.length > 1) cands = cands.filter(el => norm(accessibleName(el)) === t.name);
    if (!cands.length) return {ok: false, reason: 'target_not_found', detail: '같은 식별값의 요소 없음'};
    if (cands.length > 1) return {ok: false, reason: 'target_ambiguous', detail: '같은 식별값의 요소 ' + cands.length + '개'};
    return found(cands[0]);
  }

  const out = {url: location.href, skel: skeleton(), ready: readyCount()};
  if (args && args.css) {
    let t = null;
    try { t = document.querySelector(args.css); } catch (e) { t = null; }
    out.target = t ? describe(t) : null;
  }
  if (args && args.target) {
    const tg = args.target;
    out.found = tg.kind === 'slot' ? locateSlot(tg) : tg.kind === 'identity' ? locateIdentity(tg) : locateUi(tg);
  }
  return out;
}
""".replace("__ACCESSIBLE_NAME__", ACCESSIBLE_NAME_JS.strip()).replace(
    "__DEPTH__", str(SKELETON_DEPTH)
).replace("__MIN_ITEMS__", str(LIST_MIN_ITEMS)).replace("__GUARD__", str(UI_GUARD_PX))


async def snapshot(page: Any, css: Optional[str] = None) -> Dict[str, Any]:
    """현재 페이지의 골격·준비 수(+ css 대상의 기술)를 한 번에 읽는다.

    반환: {url, skel, ready, target(css 를 줬을 때: 기술 dict 또는 None)}.
    """
    raw = await page.evaluate(KEYS_JS, {"css": css} if css else {})
    out: Dict[str, Any] = {
        "url": str(raw.get("url") or ""),
        "skel": str(raw.get("skel") or ""),
        "ready": int(raw.get("ready") or 0),
    }
    if css:
        out["target"] = raw.get("target")
    return out


async def locate(page: Any, target: Dict[str, Any]) -> Dict[str, Any]:
    """저장된 Target 으로 현재 페이지에서 대상을 찾는다(골격·준비 수도 함께 — 같은 evaluate).

    반환: {ok, reason?, detail?, css?, role?, name?, href?, pos?, url, skel, ready}.
    정확히 1개가 아니면 ok=False(target_not_found / target_ambiguous).
    """
    raw = await page.evaluate(KEYS_JS, {"target": target})
    res: Dict[str, Any] = dict(raw.get("found") or {"ok": False, "reason": "target_not_found",
                                                    "detail": "찾기 실패"})
    res.update(url=str(raw.get("url") or ""), skel=str(raw.get("skel") or ""),
               ready=int(raw.get("ready") or 0))
    return res


def make_target(desc: Optional[Dict[str, Any]], pin: Optional[str] = None) -> Dict[str, Any]:
    """기록 때의 대상 기술 → 저장할 Target. 기본: 목록 안이면 slot, 밖이면 ui. pin 으로 바꾼다."""
    if not desc:
        raise TargetError("대상을 기술할 수 없습니다(기록 때 요소를 찾지 못함)")
    kind = pin or ("slot" if desc.get("slot") else "ui")
    if kind == "slot":
        if not desc.get("slot"):
            raise TargetError("반복 목록 안의 대상이 아니라 slot 으로 저장할 수 없습니다")
        return {"kind": "slot", **desc["slot"]}
    if kind == "ui":
        return {"kind": "ui", **desc["ui"]}
    if kind == "identity":
        if not desc.get("identity"):
            raise TargetError("identity 로 저장할 수 없습니다: " + str(desc.get("identity_refused") or ""))
        return {"kind": "identity", **desc["identity"]}
    raise TargetError(f"알 수 없는 대상 종류: {kind!r} (slot|ui|identity)")
