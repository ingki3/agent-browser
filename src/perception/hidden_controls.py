"""숨긴 체크박스·라디오 — 관찰 `data.hidden_controls` 와 check_box·click 사후 확인 (WS-81, #207).

메일 목록·할 일 목록·커스텀 폼은 실제 `<input type=checkbox|radio>` 를 opacity:0·1px clip·display:none 등으로 숨기고 보이는
`<label>` 로 조작한다. 살균기(동결)는 숨긴 input 을 버리고 라벨만 role `generic` 으로 남기므로 에이전트는 상태·짝을 모른다.

* **표시**: 관찰 뒤 **격리 world** 에서 숨긴 checkbox/radio 를 훑어, 브라우저의 label↔control 연결(`input.labels`·
  `label.control`)로만 짝을 짓는다(fail-closed — 그 루트에서 id 가 중복·보이는 라벨이 정확히 1개가 아님·`label.control` 이
  그 input 이 아님 → 싣지 않음). 항목 `{element_id(라벨), kind, checked, operable, row_element_id?, group?, added?}`.
  * `operable:false` — 라벨 자체에 명시 role(none/presentation 외)이 있거나 라벨 안에 control 말고 상호작용 후손(링크·버튼·
    다른 입력·onclick·tabindex≥0 …)이 있거나 input 이 disabled. 상태만 알리고 check_box 는 누르지 않고 거부한다.
  * `row_element_id` — 라벨 이름이 '' 일 때만: 라벨에서 위로 올라가며 처음 관찰된 링크를 품은 조상에 그 링크가 정확히
    1개이고 다른 checkbox/radio 를 품지 않으면 그 링크의 element_id(글자를 새로 싣지 않는다 — 가림·주입·크기 부담 없음).
  * 짝 라벨이 프루닝으로 elements 에 없으면 그 라벨을 **정직한 추가 요소**(살균기가 그 노드에 매기는 그대로의 role·이름·
    css 경로, 새 id, 점수 0)로 elements 끝에 붙이고 항목에 `added: true` 를 단다(요약 axtree_summary 도 같은 형식으로 덧붙임).
  * 상한: 항목 HC_MAX_ENTRIES 개·블록 HC_MAX_CHARS 자(넘는 항목은 통째로 빼고 `data.hidden_controls_omitted` 에 센다).
    숨긴 control 이 없으면 키·추가 요소 모두 없다(기존 바이트 그대로).
* **동작**: check_box(라벨)는 기존 check()/_pointer_call 경로 그대로 실행하고 사후에 격리 world 에서 label.control.checked 를
  읽어 기대값이면 성공(이미 그 상태면 무입력 성공). click(라벨)은 토글 의미 유지 — 전후 값이 바뀌었으면 성공 +
  `data.hidden_control`. 판단 읽기는 모두 격리 world, 못 읽으면 기준 동작 그대로(페이지 world 로 내려가지 않음).
* **노드 핀(R1)**: 관찰이 노출한 라벨은 스캔 때 격리 world 전역에 고정한 (라벨, input) 노드와 실행 직전·사후에 대조한다 —
  다르거나 대조 불가면 누르지 않는다(TOCTOU). input id 유무와 무관.

살균기의 규칙(가시성·role·css 경로·이름)은 sanitizer 원문(COLLECT_SCRIPT·ACCESSIBLE_NAME_JS — 바이트 동결)에서 함수를 그대로
떼어 쓴다 — 추가 요소의 role·이름이 staleness 검증(같은 이름 규칙)과 어긋나지 않게. 엔진·살균기·점수기는 건드리지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: data 키.
BLOCK_KEY = "hidden_controls"
OMITTED_KEY = "hidden_controls_omitted"
#: click(짝 라벨) 성공 data 키 — {checked_before, checked_after}.
CLICK_KEY = "hidden_control"
#: 블록 항목 수 상한.
HC_MAX_ENTRIES = 30
#: 블록 직렬화 상한(글자).
HC_MAX_CHARS = 2000

#: check_box 거부 안내(고정 문구 — 페이지 글자 없음).
INOPERABLE_HINT = ("이 라벨은 숨긴 체크박스의 짝이지만 행 전체·링크·버튼을 함께 품어(data.hidden_controls operable:false) "
                   "누르면 다른 동작이 함께 일어날 수 있어 check_box 를 보내지 않았습니다 — 상태만 참고하세요.")
UNPAIRED_HINT = ("이 라벨과 숨긴 체크박스의 짝을 하나로 확정할 수 없어(같은 id 중복·라벨 여러 개 등) check_box 를 보내지 "
                 "않았습니다 — 다시 관찰해 data.hidden_controls 에 실린 라벨을 고르세요.")
MOVED_MESSAGE = ("관찰 뒤 이 라벨이 가리키는 숨긴 체크박스가 바뀌었습니다(행 추가·삭제로 같은 자리가 다른 행) — 누르지 "
                 "않았습니다. 다시 관찰하세요.")
MISMATCH_MESSAGE = ("check_box 뒤 숨긴 체크박스 상태가 기대와 다릅니다(격리 world 확인) — 다시 관찰한 뒤 "
                    "data.hidden_controls 의 checked 를 보고 다시 시도하세요.")

#: 고정 문구 표(키 = 거부 사유).
HINTS: Dict[str, str] = {"inoperable": INOPERABLE_HINT, "unpaired": UNPAIRED_HINT}


def _sanitizer_fn(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _common_js() -> str:
    from perception.sanitizer import ACCESSIBLE_NAME_JS, COLLECT_SCRIPT, MIN_CLICKABLE_PX

    infer = _sanitizer_fn(COLLECT_SCRIPT, "  function inferRole(el) {", "  function isInteractive(el, role) {")
    vis = _sanitizer_fn(COLLECT_SCRIPT, "  function visibility(el) {", "  function cssPath(el) {")
    css = _sanitizer_fn(COLLECT_SCRIPT, "  function cssPath(el) {", "  function walk(root, isShadow, framePath) {")
    return ("const MIN_PX = %d;\n" % MIN_CLICKABLE_PX) + infer + vis + css + ACCESSIBLE_NAME_JS.strip() + _HC_JS


_HC_JS = r"""
  const HC_XHTML = 'http://www.w3.org/1999/xhtml';
  const HC_PINS = '__agentBrowserHiddenControlPins';
  const HC_BAD = 'a[href],button,input,select,textarea,summary,iframe,object,embed,[contenteditable=""],' +
    '[contenteditable=true],[role=link],[role=button],[role=checkbox],[role=switch],[role=radio],[role=menuitem],' +
    '[role=tab],[role=option],[role=row],[onclick]';
  function hcKind(n) {
    if (!n || n.nodeType !== 1 || n.localName !== 'input' || n.namespaceURI !== HC_XHTML) return null;
    const t = String(n.type || '').toLowerCase();
    return (t === 'checkbox' || t === 'radio') ? t : null;
  }
  function hcVis(n) { try { return !!visibility(n).visible; } catch (e) { return false; } }
  function hcOperable(label, input) {
    if (input.disabled) return false;
    const r = label.getAttribute('role');
    if (r !== null && r.trim() && ['none', 'presentation'].indexOf(r.trim().toLowerCase()) === -1) return false;
    for (const d of label.querySelectorAll('*')) {
      if (d === input) continue;
      if (d.matches(HC_BAD)) return false;
      if (d.hasAttribute('tabindex') && parseInt(d.getAttribute('tabindex'), 10) >= 0) return false;
      if (d.isContentEditable) return false;
    }
    return true;
  }
  function hcPair(input) {
    const kind = hcKind(input);
    if (!kind || input.getRootNode() !== document || hcVis(input)) return null;
    if (input.id) {
      let n = 0;
      try { n = document.querySelectorAll('#' + CSS.escape(input.id)).length; } catch (e) { return {ok: false}; }
      if (n !== 1) return {ok: false};
    }
    const vis = Array.prototype.filter.call(input.labels || [], hcVis);
    if (vis.length !== 1) return {ok: false};
    const label = vis[0];
    if (label.control !== input || label.getRootNode() !== document || inferRole(label) === 'none') return {ok: false};
    return {ok: true, label: label, kind: kind, checked: !!input.checked, operable: hcOperable(label, input)};
  }
"""

#: 표시: (obs[{css, link}], lim, nonce) → {entries:[{i, kind, checked, operable, pin, group?, row?, add?}], total}.
#: R1 B1: 짝(라벨·input) 노드를 이 **격리 world** 의 전역에 고정한다({n: nonce, p: [[label, input], ...]}) — 페이지 world 는
#: 이 전역에 닿지 못하고(위조 불가) 노드 동일성(===)은 id·css 경로와 무관하다. 문서가 바뀌거나 world 가 새로 만들어지면
#: 핀이 사라져 대조 불가(=누르지 않음)가 된다.
_SCAN_FN = r"""(obs, lim, nonce) => {
  __COMMON__
  const nodes = obs.map((o) => { try { return document.querySelector(o.css); } catch (e) { return null; } });
  const links = [];
  obs.forEach((o, i) => { if (o.link && nodes[i]) links.push(i); });
  const all = Array.prototype.filter.call(document.querySelectorAll('input'), (n) => !!hcKind(n));
  const pairs = [];
  for (const inp of all) { const p = hcPair(inp); if (p && p.ok) pairs.push([inp, p]); }
  function row(label, self) {
    let cur = label.parentElement;
    while (cur && cur !== document.documentElement) {
      for (const c of all) { if (c !== self && cur.contains(c)) return -1; }
      const hits = links.filter((i) => cur.contains(nodes[i]));
      if (hits.length === 1) return hits[0];
      if (hits.length > 1) return -1;
      cur = cur.parentElement;
    }
    return -1;
  }
  const groups = new Map();
  let gid = 0;
  const out = [];
  const pins = [];
  for (const [inp, p] of pairs) {
    if (out.length >= lim) break;
    const e = {i: nodes.indexOf(p.label), kind: p.kind, checked: p.checked, operable: p.operable, pin: pins.length};
    pins.push([p.label, inp]);
    if (p.kind === 'radio' && inp.name) {
      const f = inp.form || document;
      if (!groups.has(f)) groups.set(f, new Map());
      const g = groups.get(f);
      if (!g.has(inp.name)) g.set(inp.name, gid++);
      e.group = g.get(inp.name);
    }
    const name = String(accessibleName(p.label) || '');
    if (e.i < 0) {
      const r = p.label.getBoundingClientRect();
      e.add = {role: inferRole(p.label), name: name, css: cssPath(p.label),
               testid: p.label.getAttribute('data-testid') || p.label.getAttribute('data-test-id') || null,
               disabled: !!p.label.disabled || p.label.getAttribute('aria-disabled') === 'true',
               bbox: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}};
    }
    if (name === '') { const k = row(p.label, inp); if (k >= 0) e.row = k; }
    // WS-85 (#226): 숨긴 input 자신의 접근성 이름 원천(aria-label·title)과 라벨 이름 — 서버가 위험 범주만 비교한다(글자는
    // 싣지 않음).
    e.hn = String(inp.getAttribute('aria-label') || inp.getAttribute('title') || '').slice(0, 200);
    e.ln = name.slice(0, 200);
    out.push(e);
  }
  globalThis[HC_PINS] = {n: nonce, p: pins};
  return {entries: out, total: pairs.length};
}"""

#: 동작: (라벨 노드, {pin?: [nonce, k]}) → {verdict: none|unpaired|moved|paired, kind?, checked?, operable?}.
#: none = 라벨이 아니거나 그 control 이 숨긴 checkbox/radio 가 아님(기준 동작 그대로). moved(R1 B1) = 관찰이 노출한 라벨인데
#: 지금 css 경로의 노드·그 control 이 관찰 때 고정한 노드가 아니거나 핀이 없음(행이 빠져 같은 경로가 다른 행 — input id 가
#: 없거나 같은 id 를 다시 써도) — 대조 불가도 moved.
STATE_FN = r"""(el, a) => {
  __COMMON__
  if (a && a.pin) {
    const P = globalThis[HC_PINS];
    const q = P && P.n === a.pin[0] && Array.isArray(P.p) ? P.p[a.pin[1]] : null;
    if (!q || q[0] !== el || !el || el.control !== q[1]) return {verdict: 'moved'};
  }
  if (!el || el.nodeType !== 1 || el.localName !== 'label' || el.namespaceURI !== HC_XHTML) return {verdict: 'none'};
  const c = el.control;
  if (!hcKind(c) || c.getRootNode() !== document || hcVis(c)) return {verdict: 'none'};
  const p = hcPair(c);
  if (!p || !p.ok || p.label !== el) return {verdict: 'unpaired'};
  return {verdict: 'paired', kind: p.kind, checked: p.checked, operable: p.operable};
}"""

_COMMON: Optional[str] = None


def common_js() -> str:
    global _COMMON
    if _COMMON is None:
        _COMMON = _common_js()
    return _COMMON


def fn(name: str) -> str:
    """페이지 함수 원문(공통 부분 채움) — STATE_FN."""
    return name.replace("__COMMON__", common_js())


def scan_expr(targets: List[Dict[str, Any]], nonce: str) -> str:
    return (f"({fn(_SCAN_FN)})({json.dumps(targets, ensure_ascii=False)}, {HC_MAX_ENTRIES}, "
            f"{json.dumps(nonce)})")


def scan_targets(engine: Any, elements: Sequence[Any]) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """관찰 요소(shadow·css 없는 것 제외)의 css 경로·링크 여부와 핸들(같은 순서)."""
    targets: List[Dict[str, Any]] = []
    handles: List[Any] = []
    for el in elements:
        handle = engine.get_handle(getattr(el, "element_id", ""))
        if handle is None or handle.is_shadow or not handle.css_path:
            continue
        targets.append({"css": handle.css_path, "link": getattr(el, "role", None) == "link"})
        handles.append(handle)
    return targets, handles


def _bbox(raw: Any) -> Optional[Dict[str, int]]:
    if not isinstance(raw, dict):
        return None
    out = {}
    for k in ("x", "y", "width", "height"):
        v = raw.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
        out[k] = int(v)
    return out


#: WS-85 (#226, WS-86 리뷰 D2 a): 숨긴 control 의 aria-label·title 이 라벨보다 위험하면 항목에 고정 어휘 신호
#: `name_risk: "high"`(값 하나, 페이지 글자 없음)를 싣는다.


def _hidden_risky(hidden: Any, label: Any) -> bool:
    """숨긴 이름의 위험(assess_risk)이 HIGH 이고 보이는 라벨 이름은 HIGH 가 아닌가 — 신호만(게이트 불변)."""
    if not isinstance(hidden, str) or not hidden.strip():
        return False
    from contracts import ActionType
    from security import ActionContext, RiskLevel
    from security.hitl import assess_risk

    def risk(name: str) -> Any:
        return assess_risk(ActionContext(action=ActionType.CLICK, element_name=name)).risk

    if risk(hidden) is not RiskLevel.HIGH:
        return False
    return risk(label if isinstance(label, str) else "") is not RiskLevel.HIGH


def _parse(raw: Any, n_handles: int) -> Tuple[List[Dict[str, Any]], int]:
    """페이지 결과 검증 → (항목 원료, 전체 짝 수). 모양이 어긋난 항목은 버린다(표시 전용)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        return [], 0
    out: List[Dict[str, Any]] = []
    for e in raw["entries"]:
        if not isinstance(e, dict) or e.get("kind") not in ("checkbox", "radio"):
            continue
        if not isinstance(e.get("checked"), bool) or not isinstance(e.get("operable"), bool):
            continue
        i = e.get("i")
        if not isinstance(i, int) or isinstance(i, bool) or i >= n_handles:
            continue
        add = e.get("add")
        if i < 0:
            if not isinstance(add, dict) or not isinstance(add.get("css"), str) or not add["css"]:
                continue
            if not isinstance(add.get("role"), str) or not isinstance(add.get("name"), str):
                continue
            if _bbox(add.get("bbox")) is None:
                continue
        row = e.get("row")
        if row is not None and (not isinstance(row, int) or isinstance(row, bool) or not 0 <= row < n_handles):
            row = None
        group = e.get("group")
        if group is not None and (not isinstance(group, int) or isinstance(group, bool)):
            group = None
        pin = e.get("pin")
        if not isinstance(pin, int) or isinstance(pin, bool) or pin < 0:
            continue  # 노드 핀 없는 항목은 노출하지 않는다(R1 B1 — 실행 때 대조할 수 없다)
        out.append({"i": i, "kind": e["kind"], "checked": e["checked"], "operable": e["operable"], "row": row,
                    "group": group, "add": add if i < 0 else None, "pin": pin,
                    "hidden_risky": _hidden_risky(e.get("hn"), e.get("ln"))})
    total = raw.get("total")
    total = total if isinstance(total, int) and not isinstance(total, bool) else len(out)
    return out, max(total, len(out))


def _size(entries: List[Dict[str, Any]]) -> int:
    return len(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))


def _rebuild_summary(obs: Dict[str, Any]) -> None:
    """관찰 요약·토큰 수를 elements 로부터 엔진 형식(_summarize)으로 다시 만든다(cap_result_size 와 같은 방식)."""
    from contracts import ObservedElement
    from perception.engine import _summarize, estimate_tokens

    views = [ObservedElement.model_construct(
        element_id=e.get("element_id"), role=e.get("role"), name=e.get("name"),
        interactable=e.get("interactable", True),
    ) if isinstance(e, dict) else e for e in obs.get("elements") or []]
    summary = _summarize(views)
    obs["axtree_summary"] = summary
    obs["token_count"] = estimate_tokens(summary)


def apply_block(data: Dict[str, Any], raw: Any, handles: Sequence[Any], engine: Any,
                nonce: str = "") -> Dict[str, Tuple[Any, List[Any]]]:
    """data 에 블록을 싣고(추가 요소는 elements 끝 + 핸들 등록) 노출한 라벨 {element_id: (핸들, 노드 핀 [nonce, k])} 를
    돌려준다(핀 = 스캔이 격리 world 전역에 고정한 짝 노드의 자리 — STATE_FN 이 실행 직전·사후에 대조).

    숨긴 control 이 없으면 아무 키도 넣지 않는다. 상한(항목 수·글자)을 넘는 항목은 통째로 빼고 omitted 에 센다 — 뺀 항목의
    추가 요소는 만들지 않는다."""
    from contracts import BBox, ObservedElement
    from perception.engine import ElementHandle

    parsed, total = _parse(raw, len(handles))
    obs = data.get("observation")
    if not parsed or not isinstance(obs, dict) or not isinstance(obs.get("elements"), list):
        if total and isinstance(obs, dict):
            data[OMITTED_KEY] = total
        return {}
    # WS-82: 오버레이 순서 후처리가 base 밖 구성원(@e45 등)을 실으면 id 가 연속이 아니다 — 가장 큰 @eN 다음부터(연속이면
    # 지금과 같은 len+1). 다른 요소의 id 와 겹치지 않게.
    # WS-95a: 반복 묶음이 elements 밖 레이어 구성원 id 를 실을 수 있다 — 그 id 와도 겹치지 않게(묶음 id ⊆ elements 면 그대로).
    from perception.repeated import repeated_ids

    taken = [e.get("element_id") for e in obs["elements"] if isinstance(e, dict)] + sorted(repeated_ids(data.get("repeated")))
    next_n = max([len(obs["elements"])] + [int(t[2:]) for t in taken
                 if isinstance(t, str) and re.fullmatch(r"@e\d+", t)]) + 1
    kept: List[Dict[str, Any]] = []
    added: List[Tuple[Any, Any]] = []
    exposed: Dict[str, Tuple[Any, List[Any]]] = {}
    for p in parsed:
        if p["i"] >= 0:
            handle = handles[p["i"]]
            element_id = handle.element_id
            new = None
        else:
            add = p["add"]
            element_id = f"@e{next_n + len(added)}"
            box = _bbox(add["bbox"]) or {"x": 0, "y": 0, "width": 0, "height": 0}
            new = (ObservedElement(element_id=element_id, role=add["role"], name=add["name"], value=None,
                                   bbox=BBox(**box), interactable=not bool(add.get("disabled")), is_shadow=False,
                                   score=0.0),
                   ElementHandle(element_id=element_id, epoch=engine.epoch, role=add["role"], name=add["name"],
                                 css_path=add["css"], is_shadow=False,
                                 testid=add.get("testid") if isinstance(add.get("testid"), str) else None))
            handle = new[1]
        entry: Dict[str, Any] = {"element_id": element_id, "kind": p["kind"], "checked": p["checked"],
                                 "operable": p["operable"]}
        if p["row"] is not None:
            entry["row_element_id"] = handles[p["row"]].element_id
        if p["group"] is not None:
            entry["group"] = p["group"]
        if new is not None:
            entry["added"] = True
        if p.get("hidden_risky") is True:
            entry["name_risk"] = "high"
        if _size(kept + [entry]) > HC_MAX_CHARS:
            continue
        kept.append(entry)
        exposed[element_id] = (handle, [nonce, p["pin"]])
        if new is not None:
            added.append(new)
    omitted = total - len(kept)
    if kept:
        if added:
            elements = list(obs["elements"])
            for observed, handle in added:
                engine.register_external_handle(observed.element_id, handle)
                elements.append(observed.model_dump(mode="json"))
            obs = dict(obs, elements=elements)
            _rebuild_summary(obs)
            data["observation"] = obs
        data[BLOCK_KEY] = kept
    if omitted:
        data[OMITTED_KEY] = omitted
    return exposed


# ------------------------------------------------------------------ 크기 맞춤


def element_ids(data: Any) -> set:
    obs = data.get("observation") if isinstance(data, dict) else None
    els = obs.get("elements") if isinstance(obs, dict) else None
    return {e.get("element_id") for e in els if isinstance(e, dict)} if isinstance(els, list) else set()


def _remove_elements(data: Dict[str, Any], ids: set) -> None:
    """elements 에서 ids 를 뺀 새 목록·요약(원본 목록·dict 는 바꾸지 않는다)."""
    obs = data.get("observation")
    if not ids or not isinstance(obs, dict) or not isinstance(obs.get("elements"), list):
        return
    obs = dict(obs, elements=[e for e in obs["elements"] if not (isinstance(e, dict) and e.get("element_id") in ids)])
    _rebuild_summary(obs)
    data["observation"] = obs


def _added_ids(data: Any) -> set:
    block = data.get(BLOCK_KEY) if isinstance(data, dict) else None
    return {e.get("element_id") for e in block if isinstance(e, dict) and e.get("added")} \
        if isinstance(block, list) else set()


def drop_orphans(data: Any, kept_ids: Any) -> int:
    """elements 에 남지 않은 element_id 를 가리키는 항목을 지우고(행 참조만 빠졌으면 그 키만) 바꾼 수(지운 항목 + 지운
    행 참조)를 돌려준다. 지운 항목 수는 omitted 에 더한다."""
    if not isinstance(data, dict) or not isinstance(data.get(BLOCK_KEY), list):
        return 0
    keep: List[Dict[str, Any]] = []
    rows = 0
    for e in data[BLOCK_KEY]:
        if not isinstance(e, dict) or e.get("element_id") not in kept_ids:
            continue
        if "row_element_id" in e and e["row_element_id"] not in kept_ids:
            e = {k: v for k, v in e.items() if k != "row_element_id"}
            rows += 1
        keep.append(e)
    dropped = len(data[BLOCK_KEY]) - len(keep)
    if dropped or rows:
        data[BLOCK_KEY] = keep
    if dropped:
        data[OMITTED_KEY] = int(data.get(OMITTED_KEY) or 0) + dropped
    if not keep:
        del data[BLOCK_KEY]
    return dropped + rows


def _trial(result: Any, max_chars: int, finalize: Optional[Callable[[Any], Any]], limit: Optional[int],
           baseline: bool) -> Tuple[set, Any]:
    from interface.server_envelope import cap_result_size

    trial = result.model_copy(deep=True)
    if baseline:
        _remove_elements(trial.data, _added_ids(trial.data))
        trial.data.pop(BLOCK_KEY, None)
        trial.data.pop(OMITTED_KEY, None)
    if finalize is not None:
        finalize(trial)
    cap_result_size(trial, max_chars, limit=limit)
    return element_ids(trial.data), trial


def fit_hidden_controls(result: Any, max_chars: int, finalize: Optional[Callable[[Any], Any]] = None, *,
                        limit: Optional[int] = None) -> None:
    """크기 맞춤 — 블록과 추가 요소는 **원래 요소보다 먼저** 줄인다(원래 요소를 하나도 더 자르게 하지 않는다).

    1. 블록·추가 요소가 없을 때 최종 응답(주입 신호 + 요소 자르기)에 남는 원래 요소를 구한다.
    2. 그 밖(어차피 잘릴 원래 요소)을 가리키는 항목을 지운다.
    3. 블록을 실은 시험이 상한을 넘거나, 1번보다 원래 요소를 적게 남기거나, 추가 요소가 잘리면 뒤 항목부터 통째로 뺀다
       (그 항목의 추가 요소도 함께). 다 빼면 두 키를 모두 지운다."""
    data = getattr(result, "data", None)
    if not isinstance(data, dict) or not isinstance(data.get(BLOCK_KEY), list):
        return
    from interface.server_envelope import envelope_json

    ids0, _ = _trial(result, max_chars, finalize, limit, baseline=True)
    added = _added_ids(data)
    drop_orphans(data, ids0 | added)
    while isinstance(data.get(BLOCK_KEY), list):
        added = _added_ids(data)
        ids, trial = _trial(result, max_chars, finalize, limit, baseline=False)
        if ids0 <= ids and added <= ids and len(envelope_json(trial)) <= max_chars:
            return
        block = data[BLOCK_KEY]
        last = block[-1]
        data[BLOCK_KEY] = block[:-1]
        data[OMITTED_KEY] = int(data.get(OMITTED_KEY) or 0) + 1
        if isinstance(last, dict) and last.get("added"):
            _remove_elements(data, {last.get("element_id")})
        if not data[BLOCK_KEY]:
            del data[BLOCK_KEY]
    data.pop(OMITTED_KEY, None)
