"""네이티브 select 선택지 — 관찰 `data.select_options` 표시와 select_option 해석 (WS-80, #206).

* **표시(T1)**: 관찰된 combobox 중 실제 `<select>` 마다 선택지를 element 순서 목록으로 싣는다
  (`[{element_id, total, options:[{index, label, selected?, disabled?, group?}], more, multiple?, hint?}]`).
  상한: select 당 앞 SELECT_MAX_OPTIONS 개 + 선택된 것(원래 index 유지), 라벨 SELECT_LABEL_LIMIT 자, select
  SELECT_MAX_SELECTS 개, 블록 SELECT_MAX_CHARS 자(넘는 항목은 통째로 빼고 `data.select_options_omitted` 에 센다).
  value(코드)는 싣지 않는다 — 고르기는 index 나 보이는 라벨로. select 가 없으면 키도 없다(기존 바이트 그대로).
* **해석(T2)**: Playwright 의 value-or-label 첫 일치(`select_option(value=…)` — 문서 순서로 value 나 라벨이 같은 첫
  option)를 쓰지 않는다. 1) value 가 정확히 같은 option(여럿이면 첫 번째 — 브라우저 select.value 의미), 2) 없으면
  정규화(NFKC·공백 접기·앞뒤 공백) 라벨 정확 일치가 **정확히 1개**. 0개·2개 이상·disabled(optgroup disabled 포함)는
  즉시 실패(후보 index 만 — 라벨 글자 없음). 실행은 `index=` 로, 실행 뒤 고른 option 이 해석한 option 인지 확인한다.
* 표시·해석·확인은 같은 페이지 함수(SO_COMMON_JS)를 쓰고, 최상위 문서면 **격리 world** 에서 css 경로로 평가한다
  (페이지가 HTMLSelectElement.prototype.options getter 를 바꿔도 닿지 않음). 프레임·shadow 도(R1) 그 프레임의 CDP 격리
  world(다른 출처 iframe 은 그 프레임의 CDP 세션)에서 같은 노드를 잡아 평가한다 — 노드가 정확히 1개로 정해지지 않거나
  격리 world 를 못 얻으면 페이지 world 로 내려가지 않는다(해석 없음 = 기준 호출 그대로, 표시 없음, 확인 없음).
* 대리 위젯(select2·choices.js 류 — 보이는 combobox 가 select 가 아님)은 값 넣기·짝짓기를 하지 않고 고정 안내(T3).

엔진·살균기·점수기는 건드리지 않는다(동결 — test_ws62_r12). 응답 크기 맞춤·내용 가림·주입 신호는 data.repeated 와 같은
배선이다(server_tools._fit_result·public_url.CONTENT_PATHS·injection_signal.SIDE_BLOCK_KEYS).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: data 키.
BLOCK_KEY = "select_options"
OMITTED_KEY = "select_options_omitted"
#: select 당 싣는 선택지 수(앞에서부터; 선택된 것은 이 밖이어도 싣는다).
SELECT_MAX_OPTIONS = 10
#: 라벨·optgroup 표제 글자 상한.
SELECT_LABEL_LIMIT = 40
#: 블록에 싣는 select 수 상한.
SELECT_MAX_SELECTS = 12
#: 블록 직렬화 상한(글자).
SELECT_MAX_CHARS = 3000
#: 페이지에서 가져오는 라벨 앞부분(글자 — 서버가 다시 SELECT_LABEL_LIMIT 로 글자 묶음 경계에서 자른다).
_RAW_LABEL_CHARS = 200
#: 한 select 에서 앞 SELECT_MAX_OPTIONS 개 밖의 선택된 것을 더 싣는 상한(multiple 대비 — 블록 상한이 최종).
_EXTRA_SELECTED = 50
#: 후보 index 상한(ambiguous).
MAX_CANDIDATES = 20

#: 선택지를 다 싣지 못했을 때(more > 0) 항목의 안내(고정 문구).
MORE_HINT = "선택지 일부만 실었습니다 — index 로 고르거나 보이는 라벨을 그대로 value 로 주세요(정확 일치)."
#: 대리 위젯 항목(proxy:true)에서 선택지를 다 싣지 못했을 때(고정 문구, WS-84).
PROXY_MORE_HINT = "선택지 일부만 실었습니다 — 위젯을 열고 다시 관찰하면 목록 전체가 보입니다."
#: 대리 위젯에 select_option 이 왔을 때(고정 문구, T3 — WS-84: 관찰의 선택지 이름으로 항목 클릭 안내 추가).
PROXY_HINT = ("이 대상은 실제 <select> 가 아닌 대리 위젯(select2 류)이라 select_option 으로 고를 수 없습니다 — "
              "browser_click 으로 위젯을 열고 browser_observe_page 로 다시 관찰한 뒤 나타난 항목(option)을 "
              "browser_click 하세요(관찰의 data.select_options 에 proxy:true 로 실린 선택지 이름으로 항목을 고르세요).")
#: 해석 실패(고정 문구 — 라벨 글자를 싣지 않는다).
FAIL_MESSAGES: Dict[str, str] = {
    "no_match": ("value 와 같은 값이나 보이는 라벨(앞뒤 공백·NFKC 정규화 뒤 정확 일치)을 가진 선택지가 없습니다 — "
                 "관찰의 data.select_options 를 보고 라벨이나 index 로 다시 고르세요."),
    "ambiguous": ("같은 라벨의 선택지가 여럿이라 고르지 않았습니다 — data.select_candidates.indices 중 하나를 "
                  "index 로 고르세요."),
    "disabled": "고르려는 선택지가 비활성(disabled)이라 고르지 않았습니다.",
    "out_of_range": "index 가 선택지 범위 밖이라 고르지 않았습니다(data.select_candidates.total 개).",
}
#: 실행 뒤 확인 실패(해석과 입력 사이에 선택지가 바뀜 — 고정 문구).
MISMATCH_MESSAGE = ("고른 뒤 확인한 선택지가 해석한 선택지와 다릅니다(그 사이 선택지가 바뀜) — 다시 관찰한 뒤 "
                    "고르세요.")

#: 표시·해석·확인이 함께 쓰는 페이지 함수(같은 규칙). 격리 world·노드 평가 어디서나 동작한다.
SO_COMMON_JS = r"""
  const soNorm = (s) => String(s == null ? '' : s).normalize('NFKC').replace(/\s+/g, ' ').trim();
  function soRead(el) {
    if (!el || el.nodeType !== 1 || el.localName !== 'select' || el.namespaceURI !== 'http://www.w3.org/1999/xhtml') {
      return null;
    }
    const out = [];
    const opts = el.options;
    for (let i = 0; i < opts.length; i++) {
      const o = opts[i];
      const p = o.parentElement;
      const g = p && p.localName === 'optgroup' ? p : null;
      out.push({label: String(o.label), value: String(o.value), selected: !!o.selected,
                disabled: !!(o.disabled || (g && g.disabled)), group: g ? String(g.label) : null});
    }
    return out;
  }
"""

#: WS-84 (#224): 대리 위젯(보이는 combobox) ↔ 숨긴 select 짝 — **구조 규칙만**(리뷰 B1·F2: 둘 사이에는 ARIA 연결이 없다).
#: * select2: proxy 의 aria-labelledby/aria-controls 가 `select2-<X>-container` 하나만 가리키고, 그 표시 칸이 proxy 안에
#:   있고, `select#X.select2-hidden-accessible` 와 표시 칸 id 가 문서에 정확히 1개, select 바로 다음 형제가 proxy 의
#:   `.select2-container`, 그 컨테이너 안 combobox 는 proxy 하나·select 0개, proxy(와 그 안) 밖의 다른 요소가 표시 칸 id 를
#:   주장(aria-owns/controls/labelledby)하지 않음.
#: * choices 류(컨테이너 포함): proxy 안 select 가 정확히 1개, proxy 안에 다른 combobox 없음, proxy 조상에 combobox 없음.
#: * 공통: 숨긴 select 는 single(multiple 아님)·같은 문서·보이지 않음. proxy 가 가리키는(aria-owns/controls) id 는 문서에 1개
#:   이하이고, listbox(가리킨 것 + proxy 안)가 2개 이상이면 거부, 1개면 그 id 가 유일·다른 요소의 소유 주장 없음·**listbox
#:   option 라벨 집합 == 숨긴 select 라벨 집합**(잘못 연결된 목록·미끼·나중 주입 항목).
#: 하나라도 어긋나면 null(표시 없음 — 지금의 고정 안내). 필드 라벨은 숨긴 select 의 보이는 label 이 정확히 1개일 때 그 글자.
#: 판정은 부르는 쪽이 **격리 world** 에서만 한다(페이지 world 의 closest·options 위조가 닿지 않음).
PX_JS = r"""
  const PX_XHTML = 'http://www.w3.org/1999/xhtml';
  const pxIds = (s) => String(s == null ? '' : s).split(/\s+/).filter(Boolean);
  const pxRole = (n) => String(n.getAttribute('role') || '').trim().toLowerCase();
  function pxCount(id) {
    try { return document.querySelectorAll('[id="' + CSS.escape(id) + '"]').length; } catch (e) { return -1; }
  }
  function pxVis(n) { try { return !!visibility(n).visible; } catch (e) { return false; } }
  function pxClaimed(id, mine) {
    for (const e of document.querySelectorAll('[aria-owns],[aria-controls],[aria-labelledby]')) {
      if (mine(e)) continue;
      const t = pxIds(e.getAttribute('aria-owns')).concat(pxIds(e.getAttribute('aria-controls')),
                                                         pxIds(e.getAttribute('aria-labelledby')));
      if (t.indexOf(id) >= 0) return true;
    }
    return false;
  }
  function pxPair(p) {
    if (!p || p.nodeType !== 1 || p.namespaceURI !== PX_XHTML || p.localName === 'select') return null;
    if (p.getRootNode() !== document || pxRole(p) !== 'combobox') return null;
    const inP = (e) => e === p || p.contains(e);
    let sel = null;
    const cont = p.closest('.select2-container');
    if (cont) {
      const xs = new Set();
      for (const t of pxIds(p.getAttribute('aria-labelledby')).concat(pxIds(p.getAttribute('aria-controls')))) {
        const m = /^select2-(.+)-container$/.exec(t);
        if (m) xs.add(m[1]);
      }
      if (xs.size !== 1) return null;
      const x = Array.from(xs)[0], rid = 'select2-' + x + '-container';
      if (pxCount(x) !== 1 || pxCount(rid) !== 1) return null;
      const s = document.getElementById(x), rend = document.getElementById(rid);
      if (!s || s.localName !== 'select' || s.namespaceURI !== PX_XHTML || !s.classList.contains('select2-hidden-accessible')) return null;
      if (s.nextElementSibling !== cont || !rend || !p.contains(rend)) return null;
      const combos = cont.querySelectorAll('[role=combobox]');
      if (combos.length !== 1 || combos[0] !== p || cont.querySelectorAll('select').length !== 0) return null;
      if (pxClaimed(rid, inP)) return null;
      sel = s;
    } else {
      const inside = p.querySelectorAll('select');
      if (inside.length !== 1 || p.querySelectorAll('[role=combobox]').length !== 0) return null;
      for (let a = p.parentElement; a; a = a.parentElement) { if (pxRole(a) === 'combobox') return null; }
      sel = inside[0];
    }
    if (sel.multiple || sel.getRootNode() !== document || pxVis(sel)) return null;
    const r = soRead(sel);
    if (!r || !r.length) return null;
    const lbs = new Set();
    for (const t of pxIds(p.getAttribute('aria-owns')).concat(pxIds(p.getAttribute('aria-controls')))) {
      const c = pxCount(t);
      if (c < 0 || c > 1) return null;
      const e = c === 1 ? document.getElementById(t) : null;
      if (e && pxRole(e) === 'listbox') lbs.add(e);
    }
    for (const e of p.querySelectorAll('[role=listbox]')) lbs.add(e);
    if (lbs.size > 1) return null;
    if (lbs.size === 1) {
      const lb = Array.from(lbs)[0];
      if (lb.id) {
        if (pxCount(lb.id) !== 1) return null;
        let pop = p.contains(lb) ? p : (lb.parentElement || lb);
        if (pop === document.body || pop === document.documentElement) pop = lb;
        if (pxClaimed(lb.id, (e) => inP(e) || pop.contains(e))) return null;
      }
      const want = r.map((o) => soNorm(o.label)).sort().join('\u0001');
      const have = Array.prototype.map.call(lb.querySelectorAll('[role=option]'), (o) => soNorm(o.textContent))
        .sort().join('\u0001');
      if (want !== have) return null;
    }
    const labs = Array.prototype.filter.call(sel.labels || [], pxVis);
    return {sel: sel, field: labs.length === 1 ? soNorm(labs[0].textContent) : null};
  }
"""

#: 표시: (targets[{i, css}], 상한들) → [{i, total, multiple, options, proxy?, field?}] (select·증명된 대리 위젯이 아니면 null).
_DISPLAY_FN = r"""(targets, lim, cut, extra) => {
  __COMMON__
  __PX__
  return targets.map((t) => {
    let el = null;
    try { el = document.querySelector(t.css); } catch (e) { el = null; }
    let r = soRead(el);
    let px = null;
    if (!r) {
      px = pxPair(el);
      if (!px) return null;
      el = px.sel;
      r = soRead(el);
    }
    const options = [];
    let extraN = 0;
    for (let i = 0; i < r.length; i++) {
      const o = r[i];
      if (i < lim || (o.selected && extraN < extra)) {
        if (i >= lim) extraN++;
        options.push({index: i, label: o.label.slice(0, cut), selected: o.selected, disabled: o.disabled,
                      group: o.group == null ? null : o.group.slice(0, cut)});
      }
    }
    const out = {i: t.i, total: r.length, multiple: !!el.multiple, options};
    if (px) { out.proxy = true; if (px.field) out.field = px.field.slice(0, cut); }
    return out;
  });
}"""

#: 판정(HITL 강화): (대리 위젯 노드) → {verdict: proxy, field} | {verdict: none}. 짝 규칙은 표시와 같다(PX_JS).
PROXY_FN = r"""(el, a) => {
  __COMMON__
  __PX__
  const px = pxPair(el);
  return px ? {verdict: 'proxy', field: px.field} : {verdict: 'none'};
}"""

_PX_DONE: Dict[str, str] = {}


def _with_px(fn: str) -> str:
    """페이지 함수 원문에 공통 부분(SO_COMMON_JS + 살균기 visibility 등 + PX_JS)을 채운다(한 번 만들어 둔다)."""
    done = _PX_DONE.get(fn)
    if done is None:
        from perception.hidden_controls import common_js

        done = fn.replace("__COMMON__", SO_COMMON_JS).replace("__PX__", common_js() + PX_JS)
        _PX_DONE[fn] = done
    return done


def proxy_fn() -> str:
    return _with_px(PROXY_FN)

#: 해석: (el, {value?|index?}) → {verdict: ok|fail|index|not_select|gone, …}.
RESOLVE_FN = r"""(el, a) => {
  __COMMON__
  const r = soRead(el);
  if (!r) return {verdict: el ? 'not_select' : 'gone'};
  if (a.index !== undefined && a.index !== null) return {verdict: 'index', total: r.length};
  const v = String(a.value);
  let hit = -1;
  for (let i = 0; i < r.length; i++) { if (r[i].value === v) { hit = i; break; } }
  if (hit < 0) {
    const nv = soNorm(v);
    const m = [];
    for (let i = 0; i < r.length; i++) { if (soNorm(r[i].label) === nv) m.push(i); }
    if (m.length > 1) return {verdict: 'fail', reason: 'ambiguous', indices: m.slice(0, __MAXC__), total: m.length};
    if (!m.length) return {verdict: 'fail', reason: 'no_match', indices: []};
    hit = m[0];
  }
  if (r[hit].disabled) return {verdict: 'fail', reason: 'disabled', indices: [hit]};
  return {verdict: 'ok', index: hit, value: r[hit].value, label: soNorm(r[hit].label), already: r[hit].selected};
}""".replace("__COMMON__", SO_COMMON_JS).replace("__MAXC__", str(MAX_CANDIDATES))

#: 확인: (el, {index, value, label}) → {verdict: match|differ|gone} — 고른 option 이 해석한 그 option 인가.
CONFIRM_FN = r"""(el, a) => {
  __COMMON__
  const r = soRead(el);
  if (!r) return {verdict: 'gone'};
  const o = r[a.index];
  if (!o || !o.selected) return {verdict: 'differ'};
  if (!el.multiple && el.selectedIndex !== a.index) return {verdict: 'differ'};
  return {verdict: o.value === a.value && soNorm(o.label) === a.label ? 'match' : 'differ'};
}""".replace("__COMMON__", SO_COMMON_JS)


def node_expr(fn: str, css: str, arg: Any) -> str:
    """격리 world 식: css 경로의 노드(없으면 null)에 fn(el, arg) 을 적용한다."""
    return (f"(() => {{ let el = null; try {{ el = document.querySelector({json.dumps(css)}); }} catch (e) {{}} "
            f"return ({fn})(el, {json.dumps(arg, ensure_ascii=False)}); }})()")


def display_expr(targets: List[Dict[str, Any]]) -> str:
    return (f"({_with_px(_DISPLAY_FN)})({json.dumps(targets, ensure_ascii=False)}, {SELECT_MAX_OPTIONS}, "
            f"{_RAW_LABEL_CHARS}, {_EXTRA_SELECTED})")


#: 프레임·shadow 격리 world 노드 찾기 상한(shadow root 수).
_DEEP_ROOTS = 500


def deep_node_expr(css: str) -> str:
    """격리 world 식(R1): 문서 + 열린 shadow root 전부에서 css 에 맞는 노드가 **정확히 1개**면 그 노드, 아니면 null.

    Playwright 로케이터(shadow 관통 css·role+name)의 '첫 일치' 와 이 world 의 노드가 갈릴 수 있는 경우(0·2개 이상)는
    같은 노드라고 단정하지 않는다(부르는 쪽이 판정 불가 — fail-closed)."""
    return (f"(() => {{ const css = {json.dumps(css)}; const hits = []; const stack = [document]; let n = 0; "
            f"while (stack.length) {{ if (++n > {_DEEP_ROOTS}) return null; const root = stack.pop(); "
            f"try {{ for (const h of root.querySelectorAll(css)) hits.push(h); }} catch (e) {{ return null; }} "
            f"for (const x of root.querySelectorAll('*')) {{ if (x.shadowRoot) stack.push(x.shadowRoot); }} "
            f"if (hits.length > 1) return null; }} return hits.length === 1 ? hits[0] : null; }})()")


def call_fn(fn: str) -> str:
    """Runtime.callFunctionOn 용 선언 — this(그 world 의 노드)에 fn(el, a) 을 적용한다."""
    return f"function(a) {{ return ({fn})(this, a); }}"


def _cut(text: Any) -> str:
    from interface.server_envelope import _safe_cut

    return _safe_cut(str(text), SELECT_LABEL_LIMIT)


def _entry(raw: Any, handle: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("options"), list) or not isinstance(raw.get("total"), int):
        return None
    options: List[Dict[str, Any]] = []
    for o in raw["options"]:
        if not isinstance(o, dict) or not isinstance(o.get("index"), int) or not isinstance(o.get("label"), str):
            return None
        item: Dict[str, Any] = {"index": o["index"], "label": _cut(o["label"])}
        if o.get("selected") is True:
            item["selected"] = True
        if o.get("disabled") is True:
            item["disabled"] = True
        if isinstance(o.get("group"), str):
            item["group"] = _cut(o["group"])
        options.append(item)
    total = max(raw["total"], len(options))
    entry: Dict[str, Any] = {"element_id": handle.element_id, "total": total, "options": options,
                             "more": total - len(options)}
    if raw.get("multiple") is True:
        entry["multiple"] = True
    if raw.get("proxy") is True:
        # WS-84: 구조로 증명된 대리 위젯 — 선택지는 숨긴 select 의 것(표시 전용, 고르기는 위젯 열고 항목 click).
        entry["proxy"] = True
        if isinstance(raw.get("field"), str) and raw["field"].strip():
            entry["field"] = _cut(raw["field"])
        if entry["more"] > 0:
            entry["hint"] = PROXY_MORE_HINT
    elif entry["more"] > 0:
        entry["hint"] = MORE_HINT
    return entry


def _size(entries: List[Dict[str, Any]]) -> int:
    return len(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))


def build_block(raw: Any, handles: Sequence[Any]) -> Tuple[List[Dict[str, Any]], int]:
    """페이지 결과 → (element 순서 목록, 뺀 select 수). select 수·블록 글자 상한을 넘는 항목은 통째로 뺀다(탐욕)."""
    entries: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            idx = item.get("i") if isinstance(item, dict) else None
            if not isinstance(idx, int) or not 0 <= idx < len(handles):
                continue
            entry = _entry(item, handles[idx])
            if entry is not None:
                entries.append(entry)
    kept: List[Dict[str, Any]] = []
    omitted = max(0, len(entries) - SELECT_MAX_SELECTS)
    for entry in entries[:SELECT_MAX_SELECTS]:
        if _size(kept + [entry]) <= SELECT_MAX_CHARS:
            kept.append(entry)
        else:
            omitted += 1
    return kept, omitted


def select_targets(engine: Any, elements: Sequence[Any]) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """관찰 요소 중 combobox(네이티브 select 후보) 핸들 — shadow·css 없는 것은 뺀다(문서 querySelector 로 찾는다)."""
    targets: List[Dict[str, Any]] = []
    handles: List[Any] = []
    for el in elements:
        if getattr(el, "role", None) != "combobox":
            continue
        handle = engine.get_handle(getattr(el, "element_id", ""))
        if handle is None or handle.is_shadow or not handle.css_path:
            continue
        targets.append({"i": len(handles), "css": handle.css_path})
        handles.append(handle)
    return targets, handles


def apply_block(data: Dict[str, Any], raw: Any, handles: Sequence[Any]) -> None:
    """data 에 블록을 싣는다(select 가 없으면 아무 키도 넣지 않는다)."""
    kept, omitted = build_block(raw, handles)
    if kept:
        data[BLOCK_KEY] = kept
    if omitted:
        data[OMITTED_KEY] = omitted


def drop_orphans(data: Any, kept_ids: Any) -> int:
    """elements 에 남지 않은 element_id 를 가리키는 항목을 지운다(WS-74 'elements 밖 id 없음'). 지운 수."""
    if not isinstance(data, dict) or not isinstance(data.get(BLOCK_KEY), list):
        return 0
    block = data[BLOCK_KEY]
    keep = [e for e in block if isinstance(e, dict) and e.get("element_id") in kept_ids]
    dropped = len(block) - len(keep)
    if dropped:
        block[:] = keep
        data[OMITTED_KEY] = int(data.get(OMITTED_KEY) or 0) + dropped
        if not block:
            del data[BLOCK_KEY]
    return dropped


def element_ids(result: Any) -> set:
    obs = (getattr(result, "data", None) or {}).get("observation")
    els = obs.get("elements") if isinstance(obs, dict) else None
    return {e.get("element_id") for e in els if isinstance(e, dict)} if isinstance(els, list) else set()


def _kept_after_cap(result: Any, max_chars: int, finalize: Optional[Callable[[Any], Any]], limit: Optional[int],
                    without_block: bool = False) -> Tuple[set, int, Any]:
    """시험 사본에 주입 신호 + 요소 자르기를 해 본 결과 → (남는 element_id, 남는 요소 수, 사본)."""
    from interface.server_envelope import cap_result_size

    trial = result.model_copy(deep=True)
    if without_block:
        trial.data.pop(BLOCK_KEY, None)
        trial.data.pop(OMITTED_KEY, None)
    if finalize is not None:
        finalize(trial)
    cap_result_size(trial, max_chars, limit=limit)
    obs = trial.data.get("observation")
    els = obs.get("elements") if isinstance(obs, dict) else None
    return element_ids(trial), len(els) if isinstance(els, list) else 0, trial


def fit_select_options(result: Any, max_chars: int, finalize: Optional[Callable[[Any], Any]] = None, *,
                       limit: Optional[int] = None) -> None:
    """크기 맞춤 — 블록은 **요소보다 먼저** 줄인다(요소를 하나도 더 자르게 하지 않는다).

    1. 블록이 없을 때 최종 응답(주입 신호 + 요소 자르기)에 남는 요소를 구한다(= 블록 없는 응답과 같은 요소·요약).
    2. 그 요소 밖(어차피 잘릴 요소)을 가리키는 항목을 지운다(drop_orphans — WS-74 'elements 밖 id 없음').
    3. 블록을 실은 시험이 상한을 넘거나 1번(블록 없음)보다 요소를 적게 남기면 뒤 항목부터 통째로 뺀다(뺀 수는 select_options_omitted).
       다 빼면 두 키를 모두 지운다(요소 자리를 쓰지 않는다). 블록 자체의 상한은 수집 때(SELECT_MAX_CHARS) 지켰다."""
    data = getattr(result, "data", None)
    if not isinstance(data, dict) or not isinstance(data.get(BLOCK_KEY), list):
        return
    from interface.server_envelope import envelope_json

    ids0, k0, _ = _kept_after_cap(result, max_chars, finalize, limit, without_block=True)
    drop_orphans(data, ids0)
    while isinstance(data.get(BLOCK_KEY), list):
        _, k, trial = _kept_after_cap(result, max_chars, finalize, limit)
        if k >= k0 and len(envelope_json(trial)) <= max_chars:
            return
        block = data[BLOCK_KEY]
        block.pop()
        data[OMITTED_KEY] = int(data.get(OMITTED_KEY) or 0) + 1
        if not block:
            del data[BLOCK_KEY]
    data.pop(OMITTED_KEY, None)
