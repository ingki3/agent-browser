"""19종 액션 디스패처 (PRD §4.1, §4.3).

`contracts.ActionDispatcherProtocol` 구현체.

실행 흐름 (PRD §4.3):

    [1] Staleness 검증 (dispatch 이전)
         |- 통과 -> [2]
         `- 실패 -> 자가 치유 사다리 -> 대체 요소로 [2] (부작용 없으므로 안전)
    [2] CDP 이벤트 발송
    [3] 사후조건 검증
         |- 통과 -> success
         `- 미충족 -> retry_safe 판정
                      |- Yes -> 치유 후 1회 재시도
                      `- No  -> 즉시 중단 (이중 제출 방지)

핵심 원칙: dispatch 이후 실패에서 click/submit을 재시도하지 않는다.
결제가 두 번 실행되는 것보다 실패를 보고하는 편이 낫다.

`ActionResult`는 Stage 0에서 동결된 계약이므로 필수 필드
(`current_url`, `snapshot_epoch`, `tab_id`, `retry_safe`)를 항상 채운다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from contracts import ActionResult, ActionType, ErrorCode
from perception.engine import ElementHandle, PerceptionEngine
from perception.sanitizer import ACCESSIBLE_NAME_JS
from security.secrets import KEY_PATTERN as SECRET_KEY_PATTERN

from actions.healing import (
    FailurePhase,
    HealingCandidate,
    HealingResult,
    heal,
    is_retry_safe,
)
from actions.verification import (
    PostConditionResult,
    capture_state,
    verify_post_condition,
    verify_staleness,
)

logger = logging.getLogger(__name__)

#: 요소를 대상으로 하지 않는 액션 (staleness 검증 대상 아님)
_ELEMENTLESS_ACTIONS = frozenset(
    {
        ActionType.OBSERVE_PAGE,
        ActionType.TAKE_SCREENSHOT,
        ActionType.NAVIGATE,
        ActionType.GO_BACK,
        ActionType.RELOAD,
        ActionType.SCROLL,
        ActionType.PRESS_KEY,
        ActionType.WAIT_FOR,
        ActionType.EXTRACT,
        ActionType.SWITCH_FRAME,
        ActionType.HANDLE_DIALOG,
        ActionType.TAB_CONTROL,
    }
)

#: 에포크를 증가시키는 액션 (PRD §4.2)
EPOCH_BUMPING_ACTIONS = frozenset(
    {ActionType.NAVIGATE, ActionType.GO_BACK, ActionType.RELOAD, ActionType.SWITCH_FRAME}
)

#: 효과 없는 클릭 뒤 한 번 더 기다렸다 다시 보는 시간. 실측(s11_popup) — 새 탭은
#: click()이 반환되고 20~50ms 뒤에 생겼다. 늦게 렌더링되는 효과도 이 안에 들면
#: 잡힌다. 효과가 이미 잡힌 클릭은 기다리지 않는다.
POPUP_GRACE_MS = 150

#: 스크롤 중 문서가 바뀌었을 때 Playwright 예외 메시지(WS-24 F1 로컬 재현).
_CONTEXT_DESTROYED = "Execution context was destroyed"
#: 그때 새 문서 로드를 기다리는 상한 — 짧게(스텝 지연을 키우지 않게).
_SCROLL_RELOAD_WAIT_MS = 5000
#: 문서 높이 — body 가 아직 없는 순간(새 문서 head 수신 중)에도 null 에 걸리지 않게
#: scrollingElement → documentElement 순으로 본다(WS-24 F1 로컬 재현).
_SCROLL_HEIGHT_JS = (
    "(() => { const el = document.scrollingElement || document.documentElement;"
    " return el ? el.scrollHeight : 0; })()"
)
#: 문서 높이 + 세로 스크롤 위치(WS-30b: 스크롤이 실제로 움직였는지).
_SCROLL_STATE_JS = (
    "(() => { const el = document.scrollingElement || document.documentElement;"
    " return [el ? el.scrollHeight : 0, Math.round(window.scrollY || (el ? el.scrollTop : 0) || 0)]; })()"
)
#: 스크롤이 움직이지 않았을 때 결과 data.hint (정보만 — 성공 판정은 그대로).
SCROLL_NO_EFFECT_HINT = (
    "스크롤 위치가 바뀌지 않았습니다(이미 끝이거나 이 페이지는 창 스크롤이 없음). "
    "새 항목이 필요하면 '더 보기' 버튼 등을 observe_page 로 찾으세요."
)

#: WS-25 — 페이지를 옮길 수 있는 액션 뒤 메인 프레임 문서 요청이 시작되는지 보는 창.
#: 실측(로컬 Chromium, 액션 반환 → 요청 시작, 각 30회): 유휴 Enter p95 21ms·max 23,
#: 링크 max 52, select onchange max 12, JS setTimeout(0) max 70, setTimeout(100) max 155.
#: CPU 12코어 부하에서 폼·링크·select·setTimeout(0) max 82ms, setTimeout(100) max 166ms.
#: 150ms 는 여유 0, 100ms 는 setTimeout(100) 을 5/30 만 감지했다. 200ms 는 부하 최악
#: 대비 ~34ms 여유. 창 안에 요청이 없으면 기다리지 않는다 — 이동 없는 액션의 추가
#: 지연은 이 값 이하다(모든 이동 없는 click/press_key 가 이만큼 느려진다).
#: 링크·리다이렉트 클릭은 Playwright click() 이 커밋까지 기다려 반환하므로 nav_wait_ms 가
#: 0 에 가깝게 찍힌다 — 대기가 없었다는 뜻이 아니라 click() 안에서 기다린 것이다.
NAV_DETECT_MS = 200
#: 이동이 시작됐을 때 새 문서의 domcontentloaded 까지 기다리는 상한
#: (루프의 SETTLE_TIMEOUT_MS 와 같은 값). 넘기면 더 기다리지 않고 진행한다.
NAV_SETTLE_TIMEOUT_MS = 8000
#: 다운로드·빈 응답은 문서를 바꾸지 않는다 — 곧바로 진행한다.
_NO_DOCUMENT_STATUS = frozenset({204, 205})


class _NavWatch:
    """액션이 유발한 메인 프레임 문서 내비게이션을 감지하고 새 문서를 기다린다 (WS-25).

    액션을 보내기 **전에** 붙인다. 오래된 문서의 load state 는 이미 충족돼 있어
    `wait_for_load_state` 가 즉시 반환되는 함정이 있으므로, 요청 시작 → 메인 프레임
    framenavigated(커밋) → 그 이후의 domcontentloaded 이벤트 순서를 직접 본다.

    한계: SPA(pushState, 문서 요청 없음)와 감지 창 밖(예: 1초 뒤 JS)에서 시작되는
    이동은 잡지 않는다 — 기존 사후조건·루프 `_settle` 이 맡는다.
    """

    def __init__(self, page: Any, frame: Any = None) -> None:
        self.page = page
        #: WS-30 R1 BLOCKING-5: switch_frame 안이면 그 프레임의 문서 이동도 본다(메인 프레임과 함께).
        self._frame = frame
        self._nav_frame: Any = None
        self.active = False
        self.started = False
        self.committed = False
        self.dcl = False
        self.aborted = ""
        self._current: Any = None
        self._changed = asyncio.Event()
        self._handlers: List[Tuple[str, Any]] = []
        try:
            self._main = page.main_frame
            self._watched = [self._main] + ([frame] if frame is not None else [])
            for name, fn in (
                ("request", self._on_request),
                ("response", self._on_response),
                ("requestfailed", self._on_failed),
                ("framenavigated", self._on_nav),
                ("domcontentloaded", self._on_dcl),
                ("download", self._on_download),
            ):
                page.on(name, fn)
                self._handlers.append((name, fn))
            self.active = True
        except Exception:  # noqa: BLE001 — 프레임(switch_frame)·가짜 페이지
            self.close()

    def _is_main_nav(self, request: Any) -> bool:
        try:
            return bool(request.is_navigation_request()) and any(
                request.frame == f for f in self._watched
            )
        except Exception:  # noqa: BLE001 — 서비스 워커 요청 등은 frame 이 없다
            return False

    def _on_request(self, request: Any) -> None:
        if self._is_main_nav(request):
            # 리다이렉트·연속 이동은 새 요청이 이전 것을 대체한다.
            self._current = request
            self.started = True
            self.aborted = ""
            self._changed.set()

    def _on_response(self, response: Any) -> None:
        try:
            if response.request is not self._current:
                return
            status = response.status
            disposition = (response.headers or {}).get("content-disposition", "")
        except Exception:  # noqa: BLE001
            return
        if status in _NO_DOCUMENT_STATUS:
            self.aborted = f"status_{status}"
        elif "attachment" in disposition.lower():
            self.aborted = "download"
        self._changed.set()

    def _on_failed(self, request: Any) -> None:
        if request is self._current and not self.committed:
            self.aborted = "request_failed"
            self._changed.set()

    def _on_download(self, _download: Any) -> None:
        if not self.committed:
            self.aborted = "download"
            self._changed.set()

    def _on_nav(self, frame: Any) -> None:
        if self.started and any(frame == f for f in self._watched):
            self._nav_frame = frame
            self.committed = True
            self.dcl = False
            self.aborted = ""
            self._changed.set()

    def _on_dcl(self, _page: Any) -> None:
        # 커밋 이후의 domcontentloaded 만 새 문서의 것이다. Page 의 이 이벤트는 메인 프레임 것뿐이다.
        if self.committed and self._nav_frame == self._main:
            self.dcl = True
            self._changed.set()

    async def _wait_change(self, deadline: float) -> bool:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return False
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return False
        return True

    async def settle(self) -> Dict[str, Any]:
        """감지 창 안에 이동이 시작됐으면 새 문서를 기다린다. 기다린 정보를 돌려준다.

        이동이 없었으면 빈 dict(추가 지연 ≤ NAV_DETECT_MS).
        """
        if not self.active:
            return {}
        t0 = time.perf_counter()
        try:
            detect_deadline = t0 + NAV_DETECT_MS / 1000
            while not self.started and await self._wait_change(detect_deadline):
                pass
            if not self.started:
                return {}
            deadline = t0 + NAV_SETTLE_TIMEOUT_MS / 1000
            while not self.aborted and not (self.committed and self.dcl):
                if self.committed and self._nav_frame is not None and self._nav_frame != self._main:
                    # 하위 프레임 문서: 커밋 뒤에는 그 프레임의 load state 가 새 문서 기준이다.
                    remaining = max(0.0, deadline - time.perf_counter())
                    try:
                        await self._nav_frame.wait_for_load_state(
                            "domcontentloaded", timeout=remaining * 1000
                        )
                        self.dcl = True
                    except Exception:  # noqa: BLE001 — 상한 초과·프레임 분리
                        pass
                    break
                if not await self._wait_change(deadline):
                    break
            info: Dict[str, Any] = {
                "nav_wait_ms": round((time.perf_counter() - t0) * 1000, 1),
                "nav_committed": self.committed,
            }
            if self.committed and self._nav_frame is not None and self._nav_frame != self._main:
                info["nav_frame"] = "current_frame"
            if self.aborted:
                info["nav_aborted"] = self.aborted
            elif not (self.committed and self.dcl):
                info["nav_timed_out"] = True
            return info
        finally:
            self.close()

    def close(self) -> None:
        for name, fn in self._handlers:
            try:
                self.page.remove_listener(name, fn)
            except Exception:  # noqa: BLE001
                pass
        self._handlers = []
        self.active = False


#: 페이지를 옮길 수 있는 요소 액션(WS-25). type_text 는 press_enter 일 때만 —
#: 입력만으로 문서가 바뀌는 경우는 드물고, 매 입력에 감지 창 지연을 물리지 않는다.
_NAV_ELEMENT_ACTIONS = frozenset(
    {ActionType.CLICK, ActionType.SELECT_OPTION, ActionType.CHECK_BOX}
)


def _has_expected_state(action: ActionType, params: Dict[str, Any]) -> bool:
    """기대 상태값(입력값·checked)이 있는 액션 — 값이 판정 기준이라 다른 변화로 성공하지 않는다."""
    return (action is ActionType.TYPE_TEXT and params.get("text") is not None) or (
        action is ActionType.CHECK_BOX and params.get("checked") is not None
    )


#: 최상위 변화 기록기를 다시 시작하지 않는 읽기 전용 액션(R1 BLOCKING-4 기준선 유지).
_TOP_RECORDER_KEEP_ACTIONS = frozenset({
    ActionType.OBSERVE_PAGE, ActionType.TAKE_SCREENSHOT, ActionType.EXTRACT, ActionType.WAIT_FOR,
})

#: 최상위 문서 전후 비교에서 자발 변화와 섞이지 않는 강한 효과 신호(R1 BLOCKING-4).
_TOP_STRONG_SIGNALS = ("url_changed", "new_tab")


def _may_navigate(action: ActionType, params: Dict[str, Any]) -> bool:
    if action in _NAV_ELEMENT_ACTIONS:
        return True
    return action is ActionType.TYPE_TEXT and bool(params.get("press_enter"))


#: WS-30: 계약 입력 키 → 디스패처가 읽는 키. 계약(동결)은 그대로 두고 경계에서 맞춘다.
#: 실측 — download_file 은 계약·MCP 스키마가 `trigger_element_id` 인데 디스패처는
#: `element_id` 만 읽어 사전 승인을 해도 항상 "element_id가 필요합니다" 로 실패했다.
_PARAM_ALIASES: Dict[ActionType, Dict[str, str]] = {
    ActionType.DOWNLOAD_FILE: {"trigger_element_id": "element_id"},
}


def normalize_action_params(action: ActionType, params: Dict[str, Any]) -> Dict[str, Any]:
    """계약 키를 디스패처 키로 옮긴 사본을 돌려준다(없으면 원본 그대로).

    MCP 서버는 HITL 게이트 **전에** 이것을 불러 게이트와 디스패처가 같은 키를 본다.
    """
    aliases = _PARAM_ALIASES.get(action)
    if not aliases or not any(src in params for src in aliases):
        return params
    out = dict(params)
    for src, dst in aliases.items():
        if src in out:
            value = out.pop(src)
            out.setdefault(dst, value)
    return out


def key_kind(key: str) -> str:
    """press_key 의 키가 폼 제출·버튼 활성화를 일으킬 수 있는지 (WS-30 항목 6).

    'enter' — Enter / NumpadEnter (조합 키의 마지막 키 기준, 대소문자·별칭 무시)
    'space' — Space (포커스된 버튼·체크박스를 누른다)
    ''      — 그 밖
    """
    last = _normalize_key(str(key or "")).split("+")[-1].strip().lower()
    if last in ("enter", "numpadenter"):
        return "enter"
    if last in ("space", " "):
        return "space"
    return ""


#: 대상 요소의 이름 — **관찰 엔진과 같은 규칙**(perception.sanitizer.ACCESSIBLE_NAME_JS 를 그대로 끼워
#: 넣는다). 게이트가 selector 문자열 대신 페이지에서 읽은 이름으로 판정하게 한다(WS-30 추가 A).
#: WS-30 R1 BLOCKING-1: 예전에는 staleness 순서(aria > 텍스트 > placeholder > title)를 따로 적어
#: `<input type=submit value=결제>`·img alt·aria-labelledby 이름을 못 읽었다 — element_id 로는 차단,
#: selector 클릭·포커스 Space/Enter 로는 통과였다.
#: WS-31: 이름 밖 문맥 신호 — 게이트(security.hitl.assess_risk)가 원천별 규칙으로 키워드를 찾는다.
#: 이름 원천 전부(보이는 텍스트·aria-label 둘 다·title·자손 img alt·svg <title>·자손 aria-label·
#: 버튼 value), 목적지(a[href]·formaction·소속 폼 action — **속성 원문**만: href="#" 가 현재 페이지
#: 경로로 풀려 과차단되지 않게), 식별자(id·class·name·data-testid, 자손 class — 아이콘 <i>),
#: CSS ::before/::after content. 원천 하나만 쓰면 aria '확인' + 보이는 글자 '결제' 같은 불일치를 놓친다.
_GATE_SIGNALS_JS = """
function gateSignals(el) {
  const out = [];
  const push = (src, v) => {
    if (v === null || v === undefined) return;
    const s = String(v).trim();
    if (s) out.push([src, s.slice(0, 300)]);
  };
  const tag = el.tagName;
  const type = (el.type || '').toLowerCase();
  push('text', el.innerText || el.textContent || '');
  push('aria', el.getAttribute('aria-label'));
  push('title', el.getAttribute('title'));
  push('alt', el.getAttribute('alt'));
  if (tag === 'BUTTON' || (tag === 'INPUT' && ['submit', 'button', 'reset', 'image'].indexOf(type) !== -1)) {
    push('value', el.value);
  }
  push('id', el.id);
  push('class', el.getAttribute('class'));
  push('name_attr', el.getAttribute('name'));
  push('testid', el.getAttribute('data-testid') || el.getAttribute('data-test-id') || el.getAttribute('data-test'));
  // WS-31 R1 NB-1: 자손은 **신호를 가진 요소만** 고른다(수집 원천과 같은 셀렉터). 예전에는
  // querySelectorAll('*') 앞 40개만 봐서 빈 <i> 40개를 앞에 끼우면 뒤의 아이콘·alt·svg title 을 못 봤다.
  // 값은 원천별로 모아(중복 제거) 한 신호로 보낸다 — 상한을 올려도 결과 크기가 커지지 않게.
  const agg = {};
  const collect = (src, v) => {
    if (v === null || v === undefined) return;
    const s = String(v).trim().slice(0, 300);
    if (!s) return;
    (agg[src] = agg[src] || new Set()).add(s);
  };
  const SIGNAL_DESC = 'img[alt], area[alt], input[alt], svg title, [aria-label], [title], [class]';
  let desc = [];
  try { desc = Array.prototype.slice.call(el.querySelectorAll(SIGNAL_DESC), 0, 400); } catch (e) { desc = []; }
  for (const d of desc) {
    const dt = d.tagName.toUpperCase();
    if (dt === 'IMG' || dt === 'AREA' || dt === 'INPUT') collect('alt', d.getAttribute('alt'));
    if (dt === 'TITLE' && d.closest && d.closest('svg')) collect('svg_title', d.textContent);
    collect('child_aria', d.getAttribute('aria-label'));
    collect('child_title', d.getAttribute('title'));
    collect('class', d.getAttribute('class'));
  }
  for (const src of Object.keys(agg)) {
    // ' ¦ ' 로 잇는다 — 공백으로 이으면 '결'+'제' 나 'cancel'+'account' 가 붙어 보일 수 있다.
    out.push([src, Array.from(agg[src]).join(' \\u00a6 ')]);
  }
  // ::before/::after 글자: 빈 요소에도 붙으므로 셀렉터로 거를 수 없다. 대신 **문서 스타일시트에서
  // 글자(문자·숫자)가 든 content 를 가진 ::before/::after 규칙**을 찾아 그 셀렉터에 맞는 자손만 본다
  // (아이콘 폰트의 PUA 글리프 규칙은 글자가 아니라 건너뛴다). 읽을 수 없는 시트(다른 출처)·해석
  // 못 한 셀렉터가 있어도 앞 자손 20개는 예전처럼 항상 본다. 읽을 수 없는 시트(다른 출처 CSS)가
  // 하나라도 있으면 규칙을 다 모르는 것이므로 자손 400개까지 본다(판정 불가 쪽으로 넓힌다).
  const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
  const pseudoNodes = [el];
  const seen = new Set(pseudoNodes);
  const addNode = (n) => { if (pseudoNodes.length < 400 && !seen.has(n)) { seen.add(n); pseudoNodes.push(n); } };
  try { Array.prototype.slice.call(el.querySelectorAll('*'), 0, 20).forEach(addNode); } catch (e) { /* 없음 */ }
  const PSEUDO_RE = /::?(before|after)\\b/i;
  const LETTER_RE = /[\\p{L}\\p{N}]/u;
  const walkRules = (rules, depth) => {
    if (!rules || depth > 8) return;
    for (const r of Array.prototype.slice.call(rules)) {
      const st = r.selectorText;
      if (st && PSEUDO_RE.test(st)) {
        const c = (r.style && r.style.getPropertyValue('content')) || '';
        if (c && c !== 'none' && c !== 'normal' && LETTER_RE.test(c)) {
          for (const part of st.split(',')) {
            if (!PSEUDO_RE.test(part)) continue;
            const base = part.replace(/::?(before|after)\\b/gi, '').trim() || '*';
            try { el.querySelectorAll(base).forEach(addNode); } catch (e) { /* 해석 못 한 셀렉터 */ }
          }
        }
      }
      if (r.cssRules) walkRules(r.cssRules, depth + 1);
    }
  };
  const root = el.getRootNode ? el.getRootNode() : el.ownerDocument;
  const sheets = [];
  for (const holder of [root, el.ownerDocument]) {
    if (!holder) continue;
    try { Array.prototype.forEach.call(holder.styleSheets || [], (s) => sheets.push(s)); } catch (e) { /* 없음 */ }
    try { Array.prototype.forEach.call(holder.adoptedStyleSheets || [], (s) => sheets.push(s)); } catch (e) { /* 없음 */ }
  }
  let unreadable = false;
  for (const sh of new Set(sheets)) {
    let rules = null;
    try { rules = sh.cssRules; } catch (e) { rules = null; unreadable = true; }
    walkRules(rules, 0);
  }
  if (unreadable) {
    try { Array.prototype.slice.call(el.querySelectorAll('*'), 0, 400).forEach(addNode); } catch (e) { /* 없음 */ }
  }
  for (const node of pseudoNodes) {
    for (const p of ['::before', '::after']) {
      let c = '';
      try { c = view.getComputedStyle(node, p).content || ''; } catch (e) { c = ''; }
      if (c && c !== 'none' && c !== 'normal') push('pseudo', c.replace(/^["']|["']$/g, ''));
    }
  }
  // WS-31 R1 NB-6: 같은 출처로 **이동**하는 링크(a[href], download 아님, http(s), 같은 문서 안
  // 앵커·'#' 아님) 표시 — 게이트는 이런 링크에 마크업 신호(class·아이콘·id·testid)와 title 을
  // 적용하지 않는다(링크는 이동이지 부작용이 아니다). 이름·aria·href 신호는 그대로 본다.
  try {
    if (el.matches && el.matches('a[href]') && !el.hasAttribute('download')) {
      const d = el.ownerDocument;
      const u = new URL(el.getAttribute('href'), d.baseURI);
      const loc = new URL(d.URL);
      if ((u.protocol === 'http:' || u.protocol === 'https:') && u.origin === loc.origin
          && !(u.pathname === loc.pathname && u.search === loc.search)) {
        out.push(['nav_link', '1']);
      }
    }
  } catch (e) { /* 판정 못 하면 링크 완화 없음 */ }
  const link = el.closest ? el.closest('a[href]') : null;
  if (link) push('href', link.getAttribute('href'));
  const submitter = (tag === 'BUTTON' && (type === '' || type === 'submit'))
    || (tag === 'INPUT' && (type === 'submit' || type === 'image'));
  if (submitter && el.form) {
    if (el.hasAttribute('formaction')) push('formaction', el.getAttribute('formaction'));
    else push('form_action', el.form.getAttribute('action'));
    // WS-31 R1 NB-6: 실제 제출 메서드(formmethod > form method, 기본 get). GET 제출은 목적지
    // 쿼리를 보지 않는다(검색 폼 `/search?type=order`) — POST 는 쿼리도 본다.
    const m = (el.getAttribute('formmethod') || el.form.getAttribute('method') || 'get').trim().toLowerCase();
    push('form_method', m);
  }
  return out;
}
"""

_TARGET_INFO_JS = (
    """
(el) => {
  __ACCESSIBLE_NAME__
  __GATE_SIGNALS__
  const text = (el.innerText || el.textContent || '').trim();
  const name = String(accessibleName(el) || '').slice(0, 200);
  const value = (el.value !== undefined && el.value !== null) ? String(el.value).slice(0, 200) : '';
  return {
    tag: el.tagName, type: (el.type || '').toLowerCase(), name: name,
    text: text.slice(0, 200), value: (el.type || '').toLowerCase() === 'password' ? '' : value,
    title: (el.getAttribute('title') || '').slice(0, 200),
    in_form: !!el.form, editable: !!el.isContentEditable,
    // 폼 연계 요소가 아니어도(사용자 정의 요소 등) 폼 조상 안에 있는가 — 판정 불가 대상의 fail-closed 용.
    inside_form: !!(el.form || (el.closest && el.closest('form'))),
    // 브라우저가 모르는 태그(사용자 정의 요소 포함) — 키 동작을 실측으로 확정할 수 없다.
    unknown_tag: el.tagName.indexOf('-') !== -1
      || Object.prototype.toString.call(el) === '[object HTMLUnknownElement]',
    signals: gateSignals(el),
  };
}
""".replace("__ACCESSIBLE_NAME__", ACCESSIBLE_NAME_JS.strip())
    .replace("__GATE_SIGNALS__", _GATE_SIGNALS_JS.strip())
)

#: WS-31: 좌표 클릭이 실제로 누르는 요소 — 최상위 뷰포트 좌표에서 elementFromPoint 로 찾고
#: 같은 출처 iframe(테두리·패딩만큼 좌표를 옮겨)·open shadow 를 따라 내려간다. 반환은 요소
#: 자체(CDP 원격 객체) 또는 판정 불가 사유 문자열('opaque' 다른 출처 iframe, 'none' 요소 없음).
_POINT_TARGET_JS = """
(x, y) => {
  let doc = document;
  let el = doc.elementFromPoint(x, y);
  for (let i = 0; el && i < 16; i++) {
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      let d = null;
      try { d = el.contentDocument; } catch (e) { d = null; }
      if (!d) return 'opaque';
      const r = el.getBoundingClientRect();
      const cs = el.ownerDocument.defaultView.getComputedStyle(el);
      x -= r.left + el.clientLeft + (parseFloat(cs.paddingLeft) || 0);
      y -= r.top + el.clientTop + (parseFloat(cs.paddingTop) || 0);
      doc = d;
      el = d.elementFromPoint(x, y);
      continue;
    }
    if (el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (inner && inner !== el) { el = inner; continue; }
    }
    break;
  }
  return el || 'none';
}
"""

#: 클릭을 실제로 받는 상호작용 조상(자신 포함). 입력칸·select·contenteditable 도 넣는다 — 그
#: 자리를 누르는 것은 입력 포커스일 뿐이고, 빼면 폼 안 입력칸 클릭이 '폼 안 비상호작용'으로
#: 판정 불가(차단)가 된다. tabindex=-1 은 뺀다(건너뛰기 링크 대상 <main tabindex=-1> 등 영역 전체).
_POINT_INFO_JS = (
    """
function () {
  const INTERACTIVE = 'button, a[href], input, textarea, select, [contenteditable=""], '
    + '[contenteditable="true"], [role=button], [role=link], [role=menuitem], [role=tab], '
    + '[role=checkbox], [role=switch], label, summary, [onclick], [tabindex]:not([tabindex="-1"])';
  const info = __INFO__;
  const hit = this;
  let cur = hit, target = null;
  for (let i = 0; cur && i < 60; i++) {
    if (cur.nodeType === 1 && cur.matches && cur.matches(INTERACTIVE)) { target = cur; break; }
    const root = cur.getRootNode ? cur.getRootNode() : null;
    cur = cur.parentElement || (root && root.host) || null;
  }
  const out = info(target || hit);
  out.interactive = !!target;
  out.hit_tag = hit.tagName;
  out.inside_form = !!(out.inside_form || (hit.closest && hit.closest('form')));
  return out;
}
""".replace("__INFO__", _TARGET_INFO_JS.strip())
)

#: 키 입력을 실제로 받는 요소(document.activeElement) — iframe(같은 출처)·open shadow 를
#: 따라 내려간다. 다른 출처 iframe 이면 opaque(판정 불가 → 게이트는 fail-closed).
_FOCUS_INFO_JS = (
    """
() => {
  const info = __INFO__;
  let el = document.activeElement;
  for (let i = 0; el && i < 12; i++) {
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      let d = null;
      try { d = el.contentDocument; } catch (e) { d = null; }
      if (!d) return { opaque: true, tag: el.tagName };
      el = d.activeElement;
      continue;
    }
    if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
    break;
  }
  if (!el) return { tag: '' };
  return info(el);
}
""".replace("__INFO__", _TARGET_INFO_JS.strip())
)

#: iframe 요소를 다시 가리킬 수 있는 짧은 셀렉터 (switch_frame 안내용).
_FRAME_HINT_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  if (el.id) return '#' + CSS.escape(el.id);
  const name = el.getAttribute('name');
  if (name) return tag + '[name=' + JSON.stringify(name) + ']';
  const src = el.getAttribute('src');
  if (src) return tag + '[src=' + JSON.stringify(src) + ']';
  const all = Array.from(document.querySelectorAll('iframe, frame'));
  return 'iframe >> nth=' + all.indexOf(el);
}
"""

#: 한 dispatch 동안 기록할 다이얼로그 최대 수(무한 alert 루프 방지).
_DIALOG_LOG_MAX = 20

#: WS-30 R1 BLOCKING-4: 프레임 안 액션 뒤 최상위 문서의 텍스트·노드 변화(text_changed/dom_delta)가
#: **액션 때문인지** 가리는 사후 관찰 창. 최상위가 스스로 바뀌는(시계·광고 로테이션) 페이지에서
#: 프레임 안 무효과 클릭이 성공으로 판정됐다. 액션 창에서 바뀐 노드가 이 창에서 **다시** 바뀌면
#: 자발 변화로 본다. 200ms 주기 시계를 확실히 잡으려면 주기보다 길어야 한다(여유 100ms).
#: 비용: 프레임 안 액션이고 최상위에 약한 신호(text/dom)만 있을 때만 이만큼 기다린다.
TOP_SPONTANEOUS_WINDOW_MS = 300

#: 최상위 문서의 변화 기록기(MutationObserver). 노드마다 변화 시각을 모은다.
#: 기록은 switch_frame 직후·액션 판정 직후 다시 시작한다 — 그래서 **액션 사이 대기 시간**(에이전트가
#: 생각하는 동안)에 바뀐 노드는 추가 비용 없이 자발 변화의 기준선이 된다.
_TOP_RECORDER_ARM_JS = """
() => {
  const prev = window.__abTopRec;
  if (prev && prev.obs) prev.obs.disconnect();
  const ids = new WeakMap();
  let seq = 0;
  const key = (n) => {
    if (n && n.nodeType !== 1) n = n.parentNode;
    if (!n) return 0;
    let k = ids.get(n);
    if (!k) { k = ++seq; ids.set(n, k); }
    return k;
  };
  const rec = { times: new Map(), overflow: false, armed: performance.now() };
  const push = (list) => {
    const t = performance.now();
    for (const m of list) {
      const k = key(m.target);
      let arr = rec.times.get(k);
      if (!arr) {
        if (rec.times.size >= 5000) { rec.overflow = true; continue; }
        arr = []; rec.times.set(k, arr);
      }
      if (arr.length < 32) arr.push(t); else arr[31] = t;
    }
  };
  rec.push = push;
  rec.obs = new MutationObserver(push);
  rec.obs.observe(document, { subtree: true, childList: true, characterData: true });
  window.__abTopRec = rec;
  return true;
}
"""

#: 액션 시작 시각. 기록기가 없으면(새 문서) None — 기준선 없이 사후 창만으로 판정한다.
_TOP_RECORDER_START_JS = """
() => {
  const r = window.__abTopRec;
  if (!r) return null;
  r.push(r.obs.takeRecords());
  return performance.now();
}
"""

#: 사후 창이 끝난 뒤 판정: 액션 창(start, end] 에서 바뀐 노드 중 기준선(≤ start)·사후 창(> end)에서
#: 바뀌지 않은 노드 수.
_TOP_RECORDER_READ_JS = """
(args) => {
  const r = window.__abTopRec;
  if (!r) return null;
  r.push(r.obs.takeRecords());
  const start = args.start === null ? r.armed : args.start;
  let act = 0, spont = 0, effect = 0;
  for (const arr of r.times.values()) {
    let inAct = false, outside = false;
    for (const t of arr) {
      if (t > start && t <= args.end) inAct = true; else outside = true;
    }
    if (!inAct) continue;
    act++;
    if (outside) spont++; else effect++;
  }
  return { act: act, spontaneous: spont, effect: effect, overflow: r.overflow,
           baseline_ms: Math.round(start - r.armed), post_ms: Math.round(performance.now() - args.end) };
}
"""


@dataclass
class DispatchContext:
    """액션 실행에 필요한 런타임 핸들."""

    page: Any
    engine: PerceptionEngine
    cdp: Any = None
    tab_id: str = "tab-1"
    #: BrowserCore 인스턴스. tab_control에 필요하며 미주입 시 해당 액션만 제한된다.
    core: Any = None
    #: switch_frame으로 프레임에 진입했을 때의 원래 메인 페이지.
    #: 프레임 안에서도 메인 기준으로 다른 프레임을 찾거나 복귀할 수 있어야 한다.
    root_page: Any = None
    #: 자격증명 플레이스홀더 해석기 (PRD 5.3). 미주입 시 치환하지 않는다.
    #: 주입되면 type_text의 text가 등록된 키일 때만 실제 값으로 바꾼다.
    secrets: Any = None
    #: Tier-2 SoM 게이트 (PRD §8-2). 기본 OFF — 레거시 클라이언트는
    #: `annotate_som=True`에 여전히 E_FEATURE_NOT_IMPLEMENTED를 받는다.
    #: MCP 서버가 `capabilities.experimental.som_vision` 협상 후 켠다.
    som_enabled: bool = False
    #: 이동 대기 스위치 (WS-28). True(기본)면 페이지를 옮길 수 있는 액션 뒤 새 문서를
    #: 기다린다(WS-25 `_NavWatch`). False 면 감시자를 붙이지 않는다 — 이동 없는
    #: click/press_key 가 감지 창(NAV_DETECT_MS)만큼 빨라지는 대신, 이동 뒤 새 문서를
    #: 확인할 책임(wait_for/observe)이 호출자에게 있다. `serve --nav-settle off`.
    nav_settle: bool = True


#: 자격증명으로 치환된 입력임을 사후조건 검증에 알리는 내부 표시.
#: 치환된 params 사본에만 붙는다 — 결과에 실제 값을 싣지 않게 한다.
_CONCEAL_KEY = "_conceal_value"


#: Playwright 키 이름 별칭.
#: Playwright는 'Enter'만 받고 'enter'/'Return'은 Unknown key로 거부한다.
#: LLM은 소문자나 별칭('return', 'esc')을 자주 쓰므로 정규화한다.
#: 실측 — TodoMVC에서 LLM이 Enter 입력에 실패해 항목 추가가 무산됐다.
_KEY_ALIASES: Dict[str, str] = {
    "enter": "Enter",
    "return": "Enter",
    "cr": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "Space",
    "spacebar": "Space",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "home": "Home",
    "end": "End",
}


def _normalize_key(key: str) -> str:
    """키 이름을 Playwright가 받는 형태로 정규화한다.

    조합 키('Control+A')는 각 파트를 개별 정규화한다.
    알 수 없는 이름은 그대로 두어 Playwright가 판단하게 한다
    (단일 문자 'a' 등은 유효한 입력이다).
    """
    raw = (key or "").strip()
    if not raw:
        return raw
    if "+" in raw:
        return "+".join(_normalize_key(part) for part in raw.split("+"))
    return _KEY_ALIASES.get(raw.lower(), raw)


class ActionDispatcher:
    """19종 액션 실행기."""

    def __init__(self, context: DispatchContext) -> None:
        self.ctx = context
        self._healing_attempts = 0
        self._healing_successes = 0
        #: 이번 dispatch 에서 문서 이동을 기다린 기록(WS-25). dispatch 마다 비운다.
        self._nav_info: Dict[str, Any] = {}
        #: WS-26b: 이번 액션이 연 팝업 Page 들(click 경로가 채운다).
        self._opened_pages: List[Any] = []
        #: WS-30 4(c): 이번 dispatch 중 뜬 네이티브 다이얼로그 기록(dispatch 마다 비운다).
        self._dialogs: List[Dict[str, Any]] = []
        #: handle_dialog 가 예약한 다음 다이얼로그 처리(1회용). None 이면 기본값(거절).
        self._dialog_arm: Optional[Dict[str, Any]] = None
        #: 다이얼로그 리스너를 단 페이지들(중복 등록 방지).
        self._dialog_pages: List[Any] = []
        #: WS-30 R1 BLOCKING-4: 프레임 안 액션 직전 최상위 기록기 시각(performance.now), 판정 기록.
        self._top_start: Optional[float] = None
        self._top_attribution: Optional[Dict[str, Any]] = None

    # -- 통계 (하네스가 성공률 측정에 사용) ----------------------------------

    @property
    def healing_attempts(self) -> int:
        return self._healing_attempts

    @property
    def healing_successes(self) -> int:
        return self._healing_successes

    @property
    def healing_rate(self) -> float:
        if not self._healing_attempts:
            return 0.0
        return self._healing_successes / self._healing_attempts

    # -- 계약 필수 필드 채우기 -----------------------------------------------

    def _set_active_page(self, page: Any, tab_id: str) -> None:
        """탭 전환·생성·종료로 활성 페이지를 바꾼다 (WS-26b, 검증 NB-4).

        root_page 는 switch_frame 으로 들어가기 전 **그 탭의** 최상위 페이지다. 탭이
        바뀌면 옛 탭의 root_page 가 남아 차단 판정·프레임 복귀가 옛 탭을 보므로 비운다.
        """
        self.ctx.page = page
        self.ctx.tab_id = tab_id
        self.ctx.root_page = None

    def _attach_opened_tabs(self, result: ActionResult) -> None:
        """이 액션이 연 팝업의 코어 탭 id 를 data["opened_tab_ids"] 로 알린다.

        코어에 등록되지 않은 팝업(탭 상한 초과·코어 미주입)은 빠진다. 기존 키는
        덮어쓰지 않는다.
        """
        pages, self._opened_pages = self._opened_pages, []
        core = self.ctx.core
        finder = getattr(core, "tab_for_page", None)
        if not callable(finder):
            return
        ids = []
        for page in pages:
            tab = finder(page)
            if tab is not None and tab.tab_id not in ids:
                ids.append(tab.tab_id)
        if ids:
            result.data.setdefault("opened_tab_ids", ids)

    def _off_popup(self, listener: Any) -> None:
        # 붙인 곳(최상위 Page)에서 뗀다 — 프레임 안에서는 ctx.page 가 Frame 이라 떼지 못하고
        # 리스너가 액션마다 쌓였다(WS-30 R1 NB-8).
        try:
            self._top_page().remove_listener("popup", listener)
        except Exception:  # noqa: BLE001 — 가짜 페이지
            pass

    # -- 프레임·다이얼로그 (WS-30) --------------------------------------------

    def _top_page(self) -> Any:
        """프레임 안이어도 그 탭의 최상위 Page (팝업·다이얼로그·스크린샷·문서 이동 기준)."""
        return self.ctx.root_page or self.ctx.page

    def _install_dialog_listener(self) -> None:
        """활성 탭에 다이얼로그 기록·처리 리스너를 한 번만 단다.

        리스너가 하나라도 있으면 Playwright 는 다이얼로그를 자동으로 닫지 않는다 — 그래서
        이 리스너가 직접 처리한다: handle_dialog 가 예약했으면 그대로(수락/거절), 아니면
        Playwright 기본 동작과 같게(beforeunload 는 수락, 나머지는 거절).
        """
        page = self._top_page()
        if page is None or not hasattr(page, "on"):
            return
        if any(p is page for p in self._dialog_pages):
            return
        try:
            page.on("dialog", self._on_dialog)
        except Exception:  # noqa: BLE001 — 가짜 페이지
            return
        self._dialog_pages.append(page)

    def _on_dialog(self, dialog: Any) -> None:
        arm, self._dialog_arm = self._dialog_arm, None
        try:
            kind = str(dialog.type)
            message = str(dialog.message)[:300]
        except Exception:  # noqa: BLE001
            kind, message = "unknown", ""
        if arm is not None:
            accept = bool(arm.get("accept", True))
            prompt_text = arm.get("prompt_text") or ""
        else:
            accept = kind == "beforeunload"
            prompt_text = ""
        if len(self._dialogs) < _DIALOG_LOG_MAX:
            self._dialogs.append(
                {"type": kind, "message": message,
                 "handled": "accepted" if accept else "dismissed"}
            )

        async def _settle() -> None:
            try:
                if accept:
                    await dialog.accept(prompt_text) if kind == "prompt" else await dialog.accept()
                else:
                    await dialog.dismiss()
            except Exception:  # noqa: BLE001 — 이미 닫힌 다이얼로그 등
                pass

        asyncio.ensure_future(_settle())

    def _dialog_signals(self) -> List[str]:
        return [f"dialog_opened:{d['type']}" for d in self._dialogs]

    async def _frame_depth_path(self, frame: Any) -> Tuple[int, List[str]]:
        """프레임 깊이(최상위=0)와 최상위 아래부터의 프레임 URL 경로."""
        path: List[str] = []
        cur = frame
        try:
            while cur is not None and getattr(cur, "parent_frame", None) is not None:
                path.append(cur.url)
                cur = cur.parent_frame
        except Exception:  # noqa: BLE001
            pass
        path.reverse()
        return len(path), path

    async def _child_frames(self, context: Any) -> List[Dict[str, str]]:
        """현재 컨텍스트(Page 또는 Frame) 바로 아래 iframe 목록 — 다시 고를 셀렉터 힌트와 URL."""
        frame = getattr(context, "main_frame", None) or context
        out: List[Dict[str, str]] = []
        try:
            children = list(frame.child_frames)
        except Exception:  # noqa: BLE001
            return out
        for child in children[:20]:
            hint = ""
            try:
                element = await child.frame_element()
                hint = await element.evaluate(_FRAME_HINT_JS)
            except Exception:  # noqa: BLE001
                pass
            out.append({"selector_hint": hint, "url": child.url})
        return out

    async def _frame_data(self, frame: Any) -> Dict[str, Any]:
        depth, path = await self._frame_depth_path(frame)
        return {
            "frame_url": frame.url,
            "frame_depth": depth,
            "frame_path": path,
            "child_frames": await self._child_frames(frame),
        }

    # -- HITL 판정 보조 (WS-30 추가 A, 항목 6) -------------------------------

    async def describe_selector_target(self, selector: str) -> Dict[str, Any]:
        """selector 가 가리키는 요소를 현재 활성 컨텍스트에서 해석해 이름을 읽는다.

        정확히 1개일 때만 {"name": …}. 0개·여러 개·읽기 실패면 {"unresolved": 사유} —
        게이트는 이를 고위험으로 본다(fail-closed).
        """
        try:
            locator = self.ctx.page.locator(selector)
            count = await locator.count()
        except Exception as exc:  # noqa: BLE001
            return {"unresolved": f"selector 해석 실패: {type(exc).__name__}"}
        if count != 1:
            return {"unresolved": f"selector 가 {count}개 요소에 맞음"}
        try:
            info = await locator.first.evaluate(_TARGET_INFO_JS)
        except Exception as exc:  # noqa: BLE001
            return {"unresolved": f"대상 이름 읽기 실패: {type(exc).__name__}"}
        return {"name": str(info.get("name") or ""), "info": info}

    async def describe_element(self, element_id: str) -> Optional[Dict[str, Any]]:
        """관찰로 받은 요소의 실제 DOM 정보(태그·폼 소속 등). 못 읽으면 None."""
        handle = self.ctx.engine.get_handle(element_id)
        if handle is None:
            return None
        try:
            return await self._locator_for(handle).evaluate(_TARGET_INFO_JS)
        except Exception:  # noqa: BLE001
            return None

    async def describe_element_for_gate(self, element_id: str) -> Dict[str, Any]:
        """element_id 클릭의 게이트 판정용 DOM 정보 (WS-31 문맥 신호).

        반환: {"info": …} 읽음 / {"missing": True} 지금 DOM 에 없음(디스패처가 staleness·TOCTOU
        로 거부하거나 치유한다 — 이름 판정만) / {"unresolved": 사유} 있는데 못 읽음(fail-closed).
        """
        handle = self.ctx.engine.get_handle(element_id)
        if handle is None:
            return {"missing": True}
        try:
            locator = self._locator_for(handle)
            if await locator.count() == 0:
                return {"missing": True}
            info = await locator.evaluate(_TARGET_INFO_JS, timeout=2000)
        except Exception as exc:  # noqa: BLE001
            return {"unresolved": f"대상 문맥 읽기 실패: {type(exc).__name__}"}
        return {"info": info}

    def coordinates_dispatchable(self, params: Dict[str, Any]) -> bool:
        """좌표 클릭이 디스패처의 사전 검사(epoch 일치·뷰포트 안)를 통과하는가.

        통과 못 하면 디스패처가 클릭 없이 TOCTOU/ELEMENT_NOT_FOUND 로 거부하므로 게이트가
        좌표를 해석할 필요가 없다(그 오류를 그대로 받게 한다). `_click_coordinates` 와 같은 검사.
        """
        try:
            x, y = int(params["x"]), int(params["y"])
            claimed = params.get("epoch")
            if claimed is None or int(claimed) != self.ctx.engine.epoch:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        viewport = getattr(self._top_page(), "viewport_size", None) or {}
        vw, vh = viewport.get("width", 0), viewport.get("height", 0)
        if vw and vh and (x >= vw or y >= vh):
            return False
        return True

    async def _gate_cdp_for(self, page: Any) -> Any:
        """게이트 판독용 CDP 세션 (최상위 Page 마다 하나, 재사용)."""
        cache = self.__dict__.setdefault("_gate_cdp_sessions", {})
        key = id(page)
        entry = cache.get(key)
        if entry is not None and entry[0] is page:
            return entry[1]
        session = await page.context.new_cdp_session(page)
        cache[key] = (page, session)
        return session

    async def describe_point(self, x: int, y: int) -> Dict[str, Any]:
        """좌표 클릭이 누를 요소를 해석한다 (WS-31). 최상위 Page 뷰포트 좌표 기준.

        반환: {"name", "info"} 해석됨 / {"unresolved": 사유} 판정 불가(게이트는 fail-closed) —
        요소 없음, 다른 출처 iframe 위, closed shadow host 위(안을 읽을 수 없음), 해석 중 예외.
        closed shadow 는 JS 로 알 수 없어(host 로 retarget) CDP DOM.describeNode 로 확인한다.
        """
        top = self._top_page()
        try:
            cdp = await self._gate_cdp_for(top)
            res = await cdp.send(
                "Runtime.evaluate",
                {"expression": f"({_POINT_TARGET_JS.strip()})({float(x)}, {float(y)})",
                 "returnByValue": False},
            )
            if res.get("exceptionDetails"):
                return {"unresolved": "좌표 해석 스크립트 예외"}
            obj = res.get("result") or {}
            if obj.get("type") == "string":
                reason = obj.get("value")
                if reason == "opaque":
                    return {"unresolved": "좌표가 다른 출처 iframe 위(내부를 읽을 수 없음)"}
                return {"unresolved": "좌표에 요소 없음"}
            object_id = obj.get("objectId")
            if obj.get("subtype") != "node" or not object_id:
                return {"unresolved": "좌표에 요소 없음"}
            try:
                node = await cdp.send("DOM.describeNode", {"objectId": object_id, "depth": 0})
                roots = (node.get("node") or {}).get("shadowRoots") or []
                if any(r.get("shadowRootType") == "closed" for r in roots):
                    return {"unresolved": "좌표가 closed shadow host 위(내부를 읽을 수 없음)"}
                called = await cdp.send(
                    "Runtime.callFunctionOn",
                    {"objectId": object_id, "functionDeclaration": _POINT_INFO_JS.strip(),
                     "returnByValue": True},
                )
            finally:
                try:
                    await cdp.send("Runtime.releaseObject", {"objectId": object_id})
                except Exception:  # noqa: BLE001
                    pass
            if called.get("exceptionDetails"):
                return {"unresolved": "좌표 대상 읽기 예외"}
            info = (called.get("result") or {}).get("value")
            if not isinstance(info, dict):
                return {"unresolved": "좌표 대상 읽기 실패"}
        except Exception as exc:  # noqa: BLE001
            return {"unresolved": f"좌표 해석 실패: {type(exc).__name__}"}
        return {"name": str(info.get("name") or ""), "info": info}

    async def focused_target(self) -> Optional[Dict[str, Any]]:
        """키 입력을 받을 요소(activeElement). 못 읽으면 None.

        WS-30 R1 BLOCKING-3: press_key 는 **최상위 Page 의 키보드**로 보내므로(프레임에는 .keyboard
        가 없다) 판정도 최상위 문서에서 시작해 실제 포커스를 따라 내려간다. 예전에는 switch_frame
        뒤 현재 프레임의 activeElement 를 봐, 포커스가 최상위 폼 입력칸이면 판정은 '프레임 body',
        키는 최상위 폼으로 가서 제출이 통과했다.

        같은 출처 iframe·open shadow 는 JS 가 따라 내려간다. 다른 출처 iframe 에서 막히면(opaque)
        Playwright 로 그 프레임 안에서 이어서 읽는다 — 포커스를 가진 문서 사슬(document.hasFocus)이
        한 줄로 확정될 때만. 확정 못 하면 opaque 를 그대로 돌려준다(게이트는 fail-closed).
        """
        top = self._top_page()
        try:
            info = await top.evaluate(_FOCUS_INFO_JS)
        except Exception:  # noqa: BLE001
            return None
        if isinstance(info, dict) and info.get("opaque"):
            deeper = await self._focus_in_focused_frame(top)
            if deeper is not None:
                return deeper
        return info

    async def _focus_in_focused_frame(self, top: Any) -> Optional[Dict[str, Any]]:
        """포커스를 가진 가장 깊은 프레임에서 activeElement 를 읽는다. 확정 못 하면 None."""
        try:
            frames = [f for f in top.frames if f is not top.main_frame]
        except Exception:  # noqa: BLE001 — 가짜 페이지
            return None
        focused: List[Any] = []
        for frame in frames:
            try:
                if await frame.evaluate("document.hasFocus()"):
                    focused.append(frame)
            except Exception:  # noqa: BLE001 — 분리된 프레임 등: 판정 불가
                return None
        if not focused:
            return None
        depth = {f: (await self._frame_depth_path(f))[0] for f in focused}
        deepest = max(focused, key=lambda f: depth[f])
        # 포커스 사슬은 한 줄이어야 한다 — 같은 깊이에 둘 이상이거나 조상 관계가 아니면 판정 불가.
        chain = []
        cur = deepest
        while cur is not None and cur is not top.main_frame:
            chain.append(cur)
            cur = cur.parent_frame
        if any(f not in chain for f in focused):
            return None
        try:
            info = await deepest.evaluate(_FOCUS_INFO_JS)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(info, dict) or info.get("opaque"):
            return None
        return info

    @staticmethod
    async def _scroll_once(page: Any, delta: int) -> Tuple[bool, Optional[int]]:
        """한 번 스크롤하고 (문서 높이가 바뀌었는지(동적 로드), 실제 세로 이동 px) 를 돌려준다.

        WS-30b: 높이만 보면 스크롤이 전혀 안 되는 페이지(본문이 창보다 짧음)도 성공으로만 보였다 —
        scrollY 전후 차이를 함께 돌려준다(부호 있음, 위로는 음수). 위치를 읽지 못하면 None
        (이동 없음이라고 주장하지 않는다).
        """

        def _state(v: Any) -> Tuple[Any, Optional[int]]:
            if isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    return v[0], int(v[1])
                except (TypeError, ValueError):
                    return v[0], None
            return v, None

        before_height, before_y = _state(await page.evaluate(_SCROLL_STATE_JS))
        await page.evaluate(f"window.scrollBy(0, {delta})")
        await page.wait_for_timeout(150)
        after_height, after_y = _state(await page.evaluate(_SCROLL_STATE_JS))
        moved = None if before_y is None or after_y is None else after_y - before_y
        return after_height != before_height, moved

    def _current_url(self) -> str:
        try:
            return self.ctx.page.url or ""
        except Exception:  # noqa: BLE001
            return ""

    def _result(
        self,
        *,
        success: bool,
        action: ActionType,
        retry_safe: bool,
        error_code: Optional[ErrorCode] = None,
        error_message: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        healed: bool = False,
        reobserve_required: bool = False,
        downloaded_path: Optional[str] = None,
        popup_tab_id: Optional[str] = None,
    ) -> ActionResult:
        """동결된 계약 형태로 결과를 구성한다."""
        return ActionResult(
            success=success,
            action=action,
            current_url=self._current_url(),
            snapshot_epoch=self.ctx.engine.epoch,
            tab_id=self.ctx.tab_id,
            healed=healed,
            reobserve_required=reobserve_required,
            retry_safe=retry_safe,
            downloaded_path=downloaded_path,
            popup_tab_id=popup_tab_id,
            error_code=error_code,
            error_message=error_message,
            data=data or {},
        )

    # -- 메인 진입점 ---------------------------------------------------------

    async def dispatch(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        """액션을 실행하고 `ActionResult`를 반환한다."""
        started = time.perf_counter()
        # WS-30: 계약 키(trigger_element_id 등)를 디스패처 키로 맞춘다.
        params = normalize_action_params(action, params)
        params, secret_resolved = self._resolve_secret(action, params)
        if secret_resolved is True and not self._secret_allowed_here():
            # 도메인에 묶인 자격증명(credentials.BoundSecrets)을 다른 사이트에서
            # 쓰려 함 — 값도 키 이름도 입력하지 않고 실패한다.
            result = self._result(
                success=False,
                action=action,
                retry_safe=False,
                error_code=ErrorCode.ELEMENT_NOT_INTERACTABLE,
                error_message=(
                    f"자격증명은 {self.ctx.secrets.domain} 도메인 페이지에서만 입력합니다. "
                    "현재 페이지는 다른 도메인이라 입력하지 않았습니다."
                ),
            )
            result.data["secret_resolved"] = False
            return result
        self._nav_info = {}
        self._opened_pages = []
        self._dialogs = []
        self._install_dialog_listener()
        try:
            result = await self._dispatch_inner(action, params)
        except Exception as exc:  # noqa: BLE001 - 어떤 실패도 계약 형태로 반환
            logger.exception("액션 실행 중 예외: %s", action.value)
            result = self._result(
                success=False,
                action=action,
                retry_safe=is_retry_safe(action, FailurePhase.PRE_DISPATCH),
                error_code=ErrorCode.PAGE_CRASHED,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        if self.ctx.root_page is not None and action not in _TOP_RECORDER_KEEP_ACTIONS:
            # R1 BLOCKING-4: 다음 액션까지의 대기 시간을 최상위 자발 변화 기준선으로 쓴다.
            # 읽기 전용 액션(관찰·추출·스크린샷·대기)은 기준선을 끊지 않는다 — 에이전트는 보통
            # observe → click 순서라, 관찰마다 다시 시작하면 기준선이 그 사이 몇 ms 로 줄어든다.
            await self._arm_top_recorder()
        if self._top_attribution is not None:
            result.data.setdefault("top_change_attribution", self._top_attribution)
            self._top_attribution = None
        if self._nav_info:
            # WS-25: 문서 이동을 기다렸다 — 기다린 사실을 남기고, 새 문서가 떴으면
            # 이전 관찰(요소 id)은 무효이므로 재관찰을 요구한다.
            result.data.update(self._nav_info)
            if self._nav_info.get("nav_committed"):
                # current_url 은 _result() 가 settle 뒤에 이미 계산했다.
                result.reobserve_required = True
            self._nav_info = {}
        if self._opened_pages:
            self._attach_opened_tabs(result)
        if self._dialogs:
            # WS-30 4(c): 이 액션 중 뜬 다이얼로그(종류·문구·처리 결과)를 알린다.
            result.data.setdefault("dialogs", list(self._dialogs))
        result.data.setdefault(
            "latency_ms", round((time.perf_counter() - started) * 1000, 2)
        )
        if secret_resolved is not None:
            # 호출자가 치환 성공 여부를 알 수 있어야 한다. 조용히 실패하면
            # 사용자는 키 이름이 그대로 입력된 것을 눈치채지 못한다.
            result.data["secret_resolved"] = secret_resolved
        return result

    def _secret_allowed_here(self) -> bool:
        """현재 페이지에서 치환된 값을 입력해도 되는가.

        도메인에 묶이지 않은 해석기(기존 `SecretStore`, `--secrets` 파일)는
        제한이 없다 — 하위 호환. `allowed_for`가 있으면 그것을 따른다.
        """
        check = getattr(self.ctx.secrets, "allowed_for", None)
        if check is None:
            return True
        try:
            url = self.ctx.page.url
        except Exception:  # noqa: BLE001
            return False
        return bool(check(url))

    def _resolve_secret(
        self, action: ActionType, params: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Optional[bool]]:
        """자격증명 플레이스홀더를 실제 값으로 바꾼다 (PRD 5.3).

        LLM은 키 이름만 보고, 실제 값은 여기서만 존재한다. 원본 params를
        변형하지 않고 사본을 만들어 호출자의 기록(트레이스)에는 키 이름이
        남도록 한다.

        반환값의 두 번째 요소:
            None   치환 대상 액션이 아니거나 해석기 미주입
            False  키 형식이지만 등록되지 않음 (원본 그대로 전달)
            True   치환됨
        """
        store = self.ctx.secrets
        if store is None or action is not ActionType.TYPE_TEXT:
            return params, None

        text = params.get("text")
        if not isinstance(text, str) or not text:
            return params, None

        resolution = store.resolve(text)
        if not resolution.resolved:
            # 등록되지 않은 키는 그대로 전달한다(조용한 실패 금지).
            # 다만 키 형식이었다면 호출자가 오타를 알아챌 수 있도록
            # False를 보고한다.
            looks_like_key = bool(SECRET_KEY_PATTERN.match(text))
            return params, (False if looks_like_key else None)

        resolved_params = dict(params)
        resolved_params["text"] = resolution.value
        # 사후조건 검증이 값을 결과에 싣지 않도록 표시한다. 사본에만 붙으므로
        # 호출자의 params(트레이스)에는 나타나지 않는다.
        resolved_params[_CONCEAL_KEY] = True
        return resolved_params, True

    async def _dispatch_inner(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        # 요소를 다루지 않는 액션은 곧바로 실행한다.
        if action in _ELEMENTLESS_ACTIONS:
            return await self._execute_elementless(action, params)

        if action is ActionType.DOWNLOAD_FILE:
            # R1 NB-5: 상대 경로는 서버 프로세스 cwd 기준이라 호출자가 모르는 곳에 조용히 썼다
            # (검증: `reldir` → 저장소 루트, `sub/../../escape` → 정규화 없이 저장). 누르기 전에
            # 거부하고, 절대 경로는 정규화(`..`·심볼릭 링크 해소)한 경로에 저장한다.
            save_dir = str(params.get("save_dir") or "")
            if not os.path.isabs(save_dir):
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,  # 발송 전
                    error_code=ErrorCode.DOWNLOAD_FAILED,
                    error_message=(
                        f"save_dir 는 절대 경로여야 합니다: {save_dir!r} "
                        "(상대 경로는 서버 작업 폴더 기준이 되어 저장 위치를 알 수 없습니다)."
                    ),
                )
            params = dict(params, save_dir=os.path.realpath(save_dir))

        element_id = params.get("element_id")
        if not element_id and action is ActionType.CLICK and params.get("x") is not None:
            return await self._click_coordinates(action, params)
        if not element_id and action is ActionType.CLICK and params.get("selector"):
            # WS-30 추가 A: 계약이 허용하는 selector 클릭. 정확히 1개에 맞을 때만 실행한다.
            handle_or_error = await self._handle_for_selector(action, params["selector"])
            if isinstance(handle_or_error, ActionResult):
                return handle_or_error
            return await self._run_element_action(action, handle_or_error, params, False)
        if not element_id:
            return self._result(
                success=False,
                action=action,
                retry_safe=True,  # 발송 전이므로 안전
                error_code=ErrorCode.ELEMENT_NOT_FOUND,
                error_message="element_id가 필요합니다.",
            )

        handle = self.ctx.engine.get_handle(element_id)
        if handle is None:
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.TOCTOU_MISMATCH,
                error_message=f"{element_id}는 현재 에포크에서 유효하지 않습니다.",
                reobserve_required=True,
            )

        # --- [0] 호출자가 명시한 epoch 검증 --------------------------------
        # 계약상 필수 입력인데 검증하지 않으면, 클라이언트가 임의의 값을
        # 보내도 통과한다. element_id 무효화만으로 보호되므로 실제 사고로는
        # 이어지지 않지만, "필수 입력"이라는 계약이 지켜지지 않는 상태다.
        # 실측 — epoch=999, -1, 생략 모두 클릭이 성공했다.
        #
        # 관찰 시점의 epoch과 다르면 클라이언트가 오래된 스냅샷을 근거로
        # 판단하고 있다는 뜻이므로 재관찰을 요구한다.
        claimed_epoch = params.get("epoch")
        if claimed_epoch is not None:
            try:
                claimed = int(claimed_epoch)
            except (TypeError, ValueError):
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.TOCTOU_MISMATCH,
                    error_message=f"epoch 값이 정수가 아닙니다: {claimed_epoch!r}",
                    reobserve_required=True,
                )
            current = self.ctx.engine.epoch
            if claimed != current:
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.TOCTOU_MISMATCH,
                    error_message=(
                        f"epoch 불일치: 요청 {claimed}, 현재 {current}. "
                        "페이지가 변경되었으므로 재관찰이 필요합니다."
                    ),
                    reobserve_required=True,
                )

        # --- [1] dispatch 이전 Staleness 검증 -------------------------------
        staleness = await verify_staleness(
            self.ctx.page,
            handle,
            self.ctx.engine.epoch,
            expected_role=params.get("expected_role"),
            expected_name=params.get("expected_name"),
        )

        healed_flag = False
        if not staleness.fresh:
            # 부작용이 없는 시점이므로 치유가 안전하다.
            healing = await self._attempt_heal(handle)
            if not healing.healed or healing.candidate is None:
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=staleness.error_code or ErrorCode.ELEMENT_NOT_FOUND,
                    error_message=(
                        f"Staleness 검증 실패({staleness.detail}) 및 자가 치유 실패"
                    ),
                    reobserve_required=True,
                    data={"healing_attempts": healing.attempts},
                )
            new_handle = self.ctx.engine.get_handle(healing.candidate.element_id)
            if new_handle is None:
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.ELEMENT_NOT_FOUND,
                    error_message="치유된 요소의 핸들을 찾을 수 없습니다.",
                    reobserve_required=True,
                )
            handle = new_handle
            healed_flag = True

        return await self._run_element_action(action, handle, params, healed_flag)

    async def _handle_for_selector(self, action: ActionType, selector: str) -> Any:
        """selector 로 대상 요소 핸들을 만든다. 0개·여러 개면 실패 결과를 돌려준다."""
        try:
            count = await self.ctx.page.locator(selector).count()
        except Exception as exc:  # noqa: BLE001
            return self._result(
                success=False, action=action, retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_FOUND,
                error_message=f"selector 를 해석할 수 없습니다: {selector} ({exc})",
            )
        if count != 1:
            return self._result(
                success=False, action=action, retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_FOUND,
                error_message=(
                    f"selector 가 요소 {count}개에 맞습니다: {selector} — 정확히 1개여야 합니다"
                    " (observe_page 의 element_id 를 쓰십시오)."
                ),
                data={"match_count": count},
            )
        target = await self.describe_selector_target(selector)
        return ElementHandle(
            element_id=f"selector:{selector}",
            epoch=self.ctx.engine.epoch,
            role="",
            name=str(target.get("name") or ""),
            css_path=selector,
            is_shadow=False,
        )

    async def _capture_root(self, *, mark_start: bool = False) -> Any:
        """프레임 안이면 최상위 문서 상태도 캡처한다(WS-30 4(b)). 최상위면 None.

        mark_start=True(액션 직전)면 최상위 변화 기록기의 액션 시작 시각도 찍는다(R1 BLOCKING-4).
        """
        root = self.ctx.root_page
        if root is None:
            return None
        try:
            state = await capture_state(root)
        except Exception:  # noqa: BLE001 — 문서 교체 중 등
            return None
        if mark_start:
            self._top_start = None
            self._top_attribution = None
            try:
                started = await root.evaluate(_TOP_RECORDER_START_JS)
                if started is None:
                    # 기록기가 없다(새 문서 등) — 지금 붙인다. 기준선 없이 사후 창만으로 판정한다.
                    await root.evaluate(_TOP_RECORDER_ARM_JS)
                self._top_start = started
            except Exception:  # noqa: BLE001
                pass
        return state

    async def _arm_top_recorder(self) -> None:
        """프레임 컨텍스트면 최상위 변화 기록을 새로 시작한다(액션 사이 대기 = 자발 변화 기준선)."""
        root = self.ctx.root_page
        if root is None:
            return
        try:
            await root.evaluate(_TOP_RECORDER_ARM_JS)
        except Exception:  # noqa: BLE001 — 문서 교체 중·가짜 페이지
            pass

    @staticmethod
    def _top_effect_signals(root_before: Any, root_after: Any, weak_ok: bool) -> List[str]:
        """최상위 문서 전후 비교의 효과 신호(top:…).

        강한 신호(url_changed·new_tab)는 그대로 인정한다. 약한 신호(text_changed·dom_delta)는
        weak_ok 일 때만 — 최상위가 스스로 바뀌는 문서(시계·광고 로테이션)에서는 액션 효과라는
        근거가 있어야 한다(R1 BLOCKING-4, `_top_change_attributable`). focus_moved 는 도달
        신호라 싣지 않는다.
        """
        if root_before is None or root_after is None:
            return []
        top = verify_post_condition(root_before, root_after)
        if not top.satisfied:
            return []
        effect = [s for s in top.signals if not s.startswith("focus_moved")]
        if any(s.startswith(_TOP_STRONG_SIGNALS) for s in effect) or weak_ok:
            return [f"top:{s}" for s in effect]
        return []

    def _effect_beyond_target(
        self, root_before: Any, root_after: Any, weak_ok: bool = False
    ) -> List[str]:
        """대상 문서 밖의 효과 신호: 최상위 문서 변화(프레임 안 액션)·다이얼로그."""
        extra = self._top_effect_signals(root_before, root_after, weak_ok)
        extra.extend(self._dialog_signals())
        return extra

    async def _top_change_attributable(self, root_before: Any, root_after: Any) -> bool:
        """최상위의 약한 변화(text/dom)가 이 액션의 효과인가 (R1 BLOCKING-4).

        기록기(MutationObserver)로 노드별 변화 시각을 본다. 액션 창(시작~지금)에 바뀐 노드 중
        기준선(직전 액션 뒤 대기 시간)이나 사후 창(TOP_SPONTANEOUS_WINDOW_MS)에서도 바뀐 노드는
        자발 변화다. 액션 창에서만 바뀐 노드가 하나라도 있어야 효과로 인정한다. 사후 창은
        기준선이 창보다 짧을 때만 기다린다. 기록을 못 읽거나 넘치면 인정하지 않는다(판정 불가 →
        효과 없음).
        """
        effect = self._top_effect_signals(root_before, root_after, weak_ok=True)
        if not effect:
            return False
        root = self.ctx.root_page
        try:
            end = await root.evaluate("performance.now()")
            args = {"start": self._top_start, "end": end}
            read = await root.evaluate(_TOP_RECORDER_READ_JS, args)
            if (
                isinstance(read, dict)
                and int(read.get("effect") or 0) > 0
                and int(read.get("baseline_ms") or 0) < TOP_SPONTANEOUS_WINDOW_MS
            ):
                # 기준선(액션 사이 대기)이 관찰 창보다 짧다 — 그 주기보다 느린 자발 변화를 기준선이
                # 못 봤을 수 있으니 사후 창만큼 더 본다. 기준선이 충분히 길면(에이전트가 생각한
                # 시간) 그 안에 한 번도 안 바뀐 노드는 주기 ≤ 창인 자발 변화가 아니다 — 대기 없음.
                await root.wait_for_timeout(TOP_SPONTANEOUS_WINDOW_MS)
                read = await root.evaluate(_TOP_RECORDER_READ_JS, args)
        except Exception:  # noqa: BLE001
            return False
        self._top_attribution = read if isinstance(read, dict) else None
        if not isinstance(read, dict) or read.get("overflow"):
            return False
        return int(read.get("effect") or 0) > 0

    def _with_outside_effects(
        self,
        action: ActionType,
        params: Dict[str, Any],
        post: PostConditionResult,
        root_before: Any,
        root_after: Any,
        top_weak_ok: bool = False,
    ) -> PostConditionResult:
        """대상 문서 스냅샷 밖에서 일어난 효과를 반영한다 (WS-30 항목 4).

        판정을 느슨하게 하는 게 아니라 놓치던 신호를 보는 것이다:
        * 새 메인 문서가 커밋됨(nav_committed) — 4(a): press_enter 로 폼이 새 문서로 가면
          입력값 비교는 **새 문서의** 같은 경로 요소를 읽어 항상 틀렸다.
        * 프레임 안 액션이 최상위 문서를 바꿈 — 4(b): capture_state 가 프레임 문서만 봤다.
        * 네이티브 다이얼로그가 뜸 — 4(c): 다이얼로그는 DOM 을 바꾸지 않는다.
        기대 상태값(입력값·checked)이 있는 액션은 문서 이동만 효과로 인정한다 — 값이 안
        들어갔는데 다른 변화로 성공이라 하지 않게.
        """
        dialog_signals = self._dialog_signals()
        if post.satisfied:
            if dialog_signals:
                post.signals = post.signals + dialog_signals
            return post
        extra: List[str] = []
        if self._nav_info.get("nav_committed"):
            extra.append(f"navigated: {self._current_url()}")
        if not _has_expected_state(action, params):
            extra.extend(self._effect_beyond_target(root_before, root_after, top_weak_ok))
        elif dialog_signals and extra:
            extra.extend(dialog_signals)
        if not extra:
            return post
        return PostConditionResult(satisfied=True, signals=list(post.signals) + extra)

    async def _run_element_action(
        self,
        action: ActionType,
        handle: ElementHandle,
        params: Dict[str, Any],
        healed_flag: bool,
    ) -> ActionResult:
        """[2] 이벤트 발송 → [3] 사후조건 검증."""
        # --- [2] 이벤트 발송 -------------------------------------------------
        before = await capture_state(self.ctx.page, handle)
        root_before = await self._capture_root(mark_start=True)
        # 새 탭은 클릭이 반환된 뒤 20~50ms 늦게 생긴다(실측, s11_popup). 전후 탭 수를
        # 스냅샷으로 비교하면 타이밍에 따라 놓치므로 popup 이벤트를 직접 듣는다.
        popups: List[Any] = []

        def _on_popup(p: Any) -> None:
            popups.append(p)

        # 프레임 안이어도 팝업·문서 이동은 최상위 Page 의 이벤트다(Frame 에는 .on 이 없어
        # 조용히 빠졌다 — WS-30 4(b) 원인의 하나).
        top_page = self._top_page()
        try:
            top_page.on("popup", _on_popup)
        except Exception:  # noqa: BLE001 — 가짜 페이지
            pass
        # WS-25: 문서 요청 감지는 액션 **전에** 붙여야 즉시 시작되는 요청도 잡는다.
        frame_ctx = self.ctx.page if self.ctx.root_page is not None else None
        watch = (
            (_NavWatch(top_page, frame=frame_ctx) if frame_ctx is not None else _NavWatch(top_page))
            if self.ctx.nav_settle and _may_navigate(action, params)
            else None
        )
        try:
            await self._execute_element_action(action, handle, params)
        except Exception as exc:  # noqa: BLE001
            # 발송 자체가 실패했으므로 부작용이 없다 -> 재시도 안전
            self._off_popup(_on_popup)
            if watch is not None:
                watch.close()
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_INTERACTABLE,
                error_message=f"이벤트 발송 실패: {exc}",
                healed=healed_flag,
            )
        if watch is not None:
            self._nav_info = await watch.settle()

        # --- [3] 사후조건 검증 ------------------------------------------------
        try:
            after = await capture_state(self.ctx.page, handle)
        except Exception:  # noqa: BLE001
            if not self._nav_info:
                raise
            # 상한을 넘겨 진행했는데 그 사이 문서가 바뀌는 중 — 이동 자체가 효과다.
            self._off_popup(_on_popup)
            return self._result(
                success=True,
                action=action,
                retry_safe=is_retry_safe(action, FailurePhase.POST_DISPATCH),
                healed=healed_flag,
                reobserve_required=True,
                data={"signals": ["navigation_started"], "element_id": handle.element_id},
            )
        root_after = await self._capture_root() if root_before is not None else None
        if not popups and action is ActionType.CLICK and not self._nav_info.get("nav_committed"):
            # 효과가 전혀 없을 때만 짧게 더 기다렸다가 **다시 본다**. 실제 사이트는
            # 클릭 → 요청 → 렌더링으로 효과가 늦게 뜨고, 새 탭도 20~50ms 늦게
            # 생긴다. 기다리기만 하고 다시 보지 않으면 늦은 효과를 놓친다.
            # 효과가 이미 있으면 기다리지 않는다(정상 경로 지연 0).
            # 최상위의 약한 변화(text/dom)도 여기서는 "무언가 있음"으로 본다 — 그 경우 아래의
            # 자발 변화 관찰 창(TOP_SPONTANEOUS_WINDOW_MS)이 이 유예 대기를 겸한다(이중 대기 방지).
            if not verify_post_condition(before, after).satisfied and not (
                self._effect_beyond_target(root_before, root_after, weak_ok=True)
            ):
                try:
                    await self.ctx.page.wait_for_timeout(POPUP_GRACE_MS)
                    after = await capture_state(self.ctx.page, handle)
                    if root_before is not None:
                        root_after = await self._capture_root()
                except Exception:  # noqa: BLE001 — 페이지 이동으로 컨텍스트가 바뀐 경우 등
                    pass
        self._off_popup(_on_popup)
        # WS-26b: 이 액션이 연 새 탭 — dispatch() 가 코어 탭 id 로 바꿔 data 에 싣는다.
        self._opened_pages = list(popups)
        if popups:
            # 팝업 이벤트가 곧 증거다. 스냅샷 탭 수가 아직 안 늘었어도 반영한다.
            before.page_count = before.page_count or 1
            after.page_count = max(after.page_count, before.page_count + len(popups))

        # 다운로드는 파일 저장 자체가 효과다 — 페이지는 바뀌지 않는다.
        downloaded = params.pop("_downloaded_path", None)
        if downloaded:
            return self._result(
                success=True,
                action=action,
                retry_safe=is_retry_safe(action, FailurePhase.POST_DISPATCH),
                healed=healed_flag,
                downloaded_path=downloaded,
                data={
                    "signals": [f"file_saved: {downloaded}"],
                    "element_id": handle.element_id,
                },
            )

        post = verify_post_condition(
            before,
            after,
            expected_value=params.get("text") if action is ActionType.TYPE_TEXT else None,
            expected_checked=(
                params.get("checked") if action is ActionType.CHECK_BOX else None
            ),
            conceal_value=bool(params.get(_CONCEAL_KEY)),
        )
        top_weak_ok = False
        if (
            not post.satisfied
            and root_before is not None
            and root_after is not None
            and not self._nav_info.get("nav_committed")
            and not _has_expected_state(action, params)
        ):
            # 프레임 안 액션이 최상위에 약한 변화만 남겼다 — 자발 변화인지 가린다(이때만 비용).
            top_weak_ok = await self._top_change_attributable(root_before, root_after)
            if not top_weak_ok and self._top_attribution is not None:
                # 관찰 창이 유예 대기를 겸했다 — 대상 문서의 늦은 효과를 다시 본다.
                try:
                    after = await capture_state(self.ctx.page, handle)
                    post = verify_post_condition(
                        before,
                        after,
                        expected_value=None,
                        expected_checked=None,
                        conceal_value=bool(params.get(_CONCEAL_KEY)),
                    )
                except Exception:  # noqa: BLE001 — 컨텍스트 교체 중
                    pass
        post = self._with_outside_effects(
            action, params, post, root_before, root_after, top_weak_ok
        )

        if post.satisfied:
            return self._result(
                success=True,
                action=action,
                retry_safe=is_retry_safe(action, FailurePhase.POST_DISPATCH),
                healed=healed_flag,
                data={"signals": post.signals, "element_id": handle.element_id},
            )

        # 사후조건 미충족 (Silent Failure)
        post_retry_safe = is_retry_safe(action, FailurePhase.POST_DISPATCH)
        if not post_retry_safe:
            # 이중 제출/결제 방지를 위해 재시도하지 않는다.
            return self._result(
                success=False,
                action=action,
                retry_safe=False,
                error_code=ErrorCode.TIMEOUT,
                error_message=(
                    f"사후조건 미충족({post.detail}). "
                    f"{action.value}는 재시도 시 부작용이 중복될 수 있어 중단합니다."
                ),
                reobserve_required=True,
                healed=healed_flag,
            )

        # 멱등 액션만 1회 재시도
        try:
            await self._execute_element_action(action, handle, params)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_INTERACTABLE,
                error_message=f"재시도 실패: {exc}",
                reobserve_required=True,
                healed=healed_flag,
            )

        retry_after = await capture_state(self.ctx.page, handle)
        retry_post = verify_post_condition(
            before,
            retry_after,
            expected_value=params.get("text") if action is ActionType.TYPE_TEXT else None,
            expected_checked=(
                params.get("checked") if action is ActionType.CHECK_BOX else None
            ),
            conceal_value=bool(params.get(_CONCEAL_KEY)),
        )
        return self._result(
            success=retry_post.satisfied,
            action=action,
            retry_safe=True,
            error_code=None if retry_post.satisfied else ErrorCode.TIMEOUT,
            error_message=(
                None if retry_post.satisfied else f"재시도 후에도 미충족({retry_post.detail})"
            ),
            data={"signals": retry_post.signals, "retried": True},
            healed=healed_flag,
            reobserve_required=not retry_post.satisfied,
        )

    # -- 치유 ---------------------------------------------------------------

    async def _attempt_heal(self, handle: ElementHandle) -> HealingResult:
        """재관찰 후 자가 치유 사다리를 가동한다."""
        self._healing_attempts += 1

        result = await self.ctx.engine.observe_page(
            page=self.ctx.page, cdp=self.ctx.cdp
        )
        candidates: List[HealingCandidate] = []
        for observed in result.elements:
            h = self.ctx.engine.get_handle(observed.element_id)
            candidates.append(
                HealingCandidate(
                    element_id=observed.element_id,
                    role=observed.role,
                    name=observed.name,
                    css_path=h.css_path if h else "",
                    testid=h.testid if h else None,
                    is_shadow=observed.is_shadow,
                )
            )

        healing = heal(handle, candidates)
        if healing.healed:
            self._healing_successes += 1
            logger.debug("치유 성공: %s (%s)", healing.strategy, healing.reason)
        return healing

    # -- 실행 ---------------------------------------------------------------

    async def is_sensitive_field(self, handle: ElementHandle) -> bool:
        """이 요소가 비밀번호 입력 필드인가.

        PRD 5.3은 "비밀번호 인풋 필드 자동 마스킹"을 규정하지만, 마스킹기
        혼자서는 판정할 수 없다:

        - 액션 파라미터의 키는 `text`라는 중립적 이름이라 키 기반 규칙에
          걸리지 않는다.
        - 값은 평범한 문자열이라 정규식으로도 잡히지 않는다.
        - 접근성 role로도 구분되지 않는다. 실측상 `input[type=password]`와
          일반 텍스트 입력이 **모두 role=textbox**다.

        DOM을 직접 아는 디스패처만이 판정할 수 있다. 실패 시 False를
        반환하되, 판정 실패가 곧 '안전함'을 뜻하지는 않는다는 점을
        호출자가 알아야 한다.
        """
        try:
            locator = self._locator_for(handle)
            return bool(
                await locator.evaluate(
                    "el => el.tagName === 'INPUT' "
                    "&& (el.type || '').toLowerCase() === 'password'"
                )
            )
        except Exception:  # noqa: BLE001
            return False

    def _locator_for(self, handle: ElementHandle) -> Any:
        """요소를 지목하는 Playwright 로케이터를 만든다.

        shadow DOM 요소는 CSS 경로로 안정적으로 지목할 수 없다.
        Playwright의 CSS 엔진이 shadow 경계를 자동 관통하므로, shadow
        내부에서 고유한 경로도 document 전체에서는 여러 요소에 매칭된다.
        실측 — MDN에서 shadow 내부 'button' 경로가 18개 요소에 매칭되어
        항상 첫 번째(display:none) 요소가 잡히고 클릭이 실패했다.

        따라서 shadow 요소는 접근성 role+name으로 지목한다. 이것이
        shadow 경계와 무관하게 동작하는 유일한 안정적 수단이다.
        """
        page = self.ctx.page
        selector = handle.css_path or ""

        if getattr(handle, "is_shadow", False) and handle.name:
            role = (handle.role or "").strip()
            try:
                if role and role not in ("generic", "none"):
                    loc = page.get_by_role(role, name=handle.name, exact=True)
                else:
                    loc = page.get_by_text(handle.name, exact=True)
                return loc.first
            except Exception:  # noqa: BLE001
                pass

        return page.locator(selector).first

    async def _execute_element_action(
        self, action: ActionType, handle: ElementHandle, params: Dict[str, Any]
    ) -> None:
        """요소 대상 액션을 실제로 발송한다."""
        target = self._locator_for(handle)

        if action is ActionType.CLICK:
            await target.click(button=params.get("button", "left"), timeout=5000)

        elif action is ActionType.TYPE_TEXT:
            if params.get("clear_before", True):
                await target.fill("", timeout=5000)
            # 치환된 값은 _resolve_secret에서 이미 params에 반영돼 있다.
            await target.type(params.get("text", ""), timeout=5000)
            if params.get("press_enter"):
                await target.press("Enter", timeout=5000)

        elif action is ActionType.SELECT_OPTION:
            if params.get("value") is not None:
                await target.select_option(value=params["value"], timeout=5000)
            else:
                await target.select_option(
                    index=params.get("index", 0), timeout=5000
                )

        elif action is ActionType.CHECK_BOX:
            if params.get("checked", True):
                await target.check(timeout=5000)
            else:
                await target.uncheck(timeout=5000)

        elif action is ActionType.HOVER:
            await target.hover(timeout=5000)

        elif action is ActionType.UPLOAD_FILE:
            await target.set_input_files(
                params.get("file_paths", []), timeout=5000
            )

        elif action is ActionType.DOWNLOAD_FILE:
            async with self.ctx.page.expect_download(
                timeout=params.get("timeout_ms", 30000)
            ) as dl:
                await target.click(timeout=5000)
            download = await dl.value
            # save_dir 는 _dispatch_inner 에서 절대 경로로 정규화됐다(R1 NB-5).
            save_path = os.path.join(params["save_dir"], download.suggested_filename)
            await download.save_as(save_path)
            params["_downloaded_path"] = save_path

        else:
            raise ValueError(f"요소 대상 액션이 아닙니다: {action.value}")

    async def _execute_elementless(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        """요소를 다루지 않는 액션을 실행한다."""
        page = self.ctx.page

        if action is ActionType.OBSERVE_PAGE:
            observed = await self.ctx.engine.observe_page(
                prune_top_n=params.get("prune_top_n", 20),
                force_full_tree=params.get("force_full_tree", False),
                page=page,
                cdp=self.ctx.cdp,
            )
            return self._result(
                success=True,
                action=action,
                retry_safe=True,
                data={"observation": observed.model_dump(mode="json")},
            )

        if action is ActionType.NAVIGATE:
            try:
                await page.goto(
                    params["url"],
                    wait_until=params.get("wait_until", "domcontentloaded"),
                    timeout=params.get("timeout_ms", 30000),
                )
            except Exception as exc:  # noqa: BLE001
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.NAVIGATE_TIMEOUT,
                    error_message=str(exc),
                )
            self.ctx.engine.bump_epoch("navigate")
            return self._result(
                success=True, action=action, retry_safe=True, data={"url": page.url}
            )

        if action is ActionType.GO_BACK:
            response = await page.go_back(timeout=params.get("timeout_ms", 10000))
            if response is None:
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.NO_HISTORY,
                    error_message="이전 히스토리가 없습니다.",
                )
            self.ctx.engine.bump_epoch("go_back")
            return self._result(
                success=True, action=action, retry_safe=True, data={"url": page.url}
            )

        if action is ActionType.RELOAD:
            await page.reload(timeout=params.get("timeout_ms", 30000))
            self.ctx.engine.bump_epoch("reload")
            return self._result(
                success=True, action=action, retry_safe=True, data={"url": page.url}
            )

        if action is ActionType.SCROLL:
            distance = params.get("distance", 500)
            delta = distance if params.get("direction", "down") == "down" else -distance
            try:
                changed, moved = await self._scroll_once(page, delta)
            except Exception as exc:  # noqa: BLE001
                # WS-24 F1: 검색 제출 직후처럼 스크롤 중 문서가 바뀌면 Playwright 가
                # "Execution context was destroyed" 를 던진다(로컬 재현). 이 경우만
                # 새 문서 로드를 짧게 기다려 1회 재시도하고, 재관찰을 요구한다.
                # 다른 예외는 삼키지 않는다(dispatch 의 PAGE_CRASHED 로 간다).
                if _CONTEXT_DESTROYED not in str(exc):
                    raise
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=_SCROLL_RELOAD_WAIT_MS
                    )
                except Exception:  # noqa: BLE001 - 대기 실패는 재시도 결과로 판정
                    pass
                _, moved = await self._scroll_once(page, delta)
                changed = True
            data: Dict[str, Any] = {"scrolled": delta}
            if moved is not None:
                data["scrolled_px"] = moved
            if moved == 0:
                # WS-30b: 정보만 추가 — 성공 판정·reobserve_required 규칙은 그대로다.
                data["no_effect"] = True
                data["hint"] = SCROLL_NO_EFFECT_HINT
            return self._result(
                success=True,
                action=action,
                retry_safe=True,
                data=data,
                # 동적 노드가 로드되었거나 문서가 바뀌었으면 재관찰이 필요하다.
                reobserve_required=changed,
            )

        if action is ActionType.PRESS_KEY:
            raw_key = str(params.get("key", ""))
            key = _normalize_key(raw_key)
            # 키보드·문서 이동은 최상위 Page 의 것이다 — 프레임 안이어도 포커스된 요소가
            # 키를 받는다(Frame 에는 .keyboard 가 없다, WS-30).
            top = self._top_page()
            frame_ctx = self.ctx.page if self.ctx.root_page is not None else None
            watch = None
            if self.ctx.nav_settle:
                # 프레임 안이면 그 프레임 문서의 이동도 기다린다(R1 BLOCKING-5 와 같은 감시).
                watch = _NavWatch(top, frame=frame_ctx) if frame_ctx is not None else _NavWatch(top)
            try:
                await top.keyboard.press(key)
            except Exception as exc:  # noqa: BLE001
                if watch is not None:
                    watch.close()
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=False,  # 키 입력은 발송 후 재시도 위험
                    error_code=ErrorCode.KEY_PRESS_FAILED,
                    error_message=f"{exc} (입력값: {raw_key!r} -> {key!r})",
                )
            # WS-25: Enter 폼 제출은 키 입력이 즉시 끝나도 결과 문서는 늦게 온다
            # (G마켓 0.7~0.9초). 떠나는 중인 페이지를 관찰하지 않게 기다린다.
            if watch is not None:
                self._nav_info = await watch.settle()
            return self._result(
                success=True, action=action, retry_safe=False, data={"key": key}
            )

        if action is ActionType.WAIT_FOR:
            return await self._wait_for(params)

        if action is ActionType.EXTRACT:
            return await self._extract(params)

        if action is ActionType.TAKE_SCREENSHOT:
            if params.get("annotate_som"):
                if not self.ctx.som_enabled:
                    # PRD §8-2: 게이트 OFF면 레거시 클라이언트 보호를 위해
                    # 미구현 코드를 그대로 반환한다(actions_test/mcp_smoke 의존).
                    return self._result(
                        success=False,
                        action=action,
                        retry_safe=True,
                        error_code=ErrorCode.FEATURE_NOT_IMPLEMENTED,
                        error_message="SoM 주석은 som_enabled 게이트가 꺼져 있어 비활성입니다.",
                    )
                if self.ctx.root_page is not None:
                    # SoM 좌표는 최상위 뷰포트 기준인데 프레임 안 후보는 프레임 기준이다.
                    return self._result(
                        success=False,
                        action=action,
                        retry_safe=True,
                        error_code=ErrorCode.SCREENSHOT_FAILED,
                        error_message=(
                            "프레임 안에서는 SoM 주석 스크린샷을 지원하지 않습니다. "
                            "switch_frame(to_main=true) 로 최상위 문서로 돌아간 뒤 시도하십시오."
                        ),
                    )
                return await self._screenshot_som(action)
            # WS-30: 프레임에는 screenshot 이 없다('Frame' object has no attribute ...) —
            # 프레임 안이면 최상위 페이지를 찍고 프레임 영역을 data 로 알린다.
            shot_page = self._top_page()
            extra: Dict[str, Any] = {}
            try:
                shot = await shot_page.screenshot(full_page=params.get("full_page", False))
                if self.ctx.root_page is not None:
                    extra["captured"] = "root_page"
                    extra["frame_url"] = page.url
                    try:
                        box = await (await page.frame_element()).bounding_box()
                    except Exception:  # noqa: BLE001
                        box = None
                    extra["frame_bbox"] = box
            except Exception as exc:  # noqa: BLE001
                return self._result(
                    success=False,
                    action=action,
                    retry_safe=True,
                    error_code=ErrorCode.SCREENSHOT_FAILED,
                    error_message=f"스크린샷 실패: {type(exc).__name__}",
                )
            return self._result(
                success=True, action=action, retry_safe=True,
                data={"bytes": len(shot), **extra},
            )

        if action is ActionType.SWITCH_FRAME:
            return await self._switch_frame(params)

        if action is ActionType.HANDLE_DIALOG:
            accept = params.get("accept", True)
            # WS-30: 다음 다이얼로그 처리를 예약한다. 실제 처리는 탭에 단 리스너
            # (_on_dialog)가 한다 — 같은 리스너가 다이얼로그 발생을 효과 신호로 기록한다.
            self._dialog_arm = {"accept": accept, "prompt_text": params.get("prompt_text")}
            self._install_dialog_listener()
            return self._result(
                success=True, action=action, retry_safe=False, data={"accept": accept}
            )

        if action is ActionType.TAB_CONTROL:
            return await self._dispatch_tab_control(action, params)

    async def _click_coordinates(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        """뷰포트 좌표 클릭 (v1.1 재동결, Tier-2 SoM / Canvas 폴백).

        DOM 대상이 없으므로 요소 staleness 검증 대신 **epoch 일치**로
        시점을 검증한다. 좌표는 SoM 스크린샷을 찍은 그 스냅샷에서만
        의미가 있고, 페이지가 바뀌었다면 같은 좌표가 다른 것을 가리킨다.

        사후조건은 DOM 신호로만 잡을 수 있다. Canvas 클릭은 DOM을 바꾸지
        않는 것이 정상이므로, 무변화를 실패로 처리하지 않고
        `data["silent"]=True`로 보고해 호출자(루프/VLM)가 판단하게 한다.
        """
        # 좌표는 최상위 뷰포트 기준이다 — 프레임 안이어도 최상위 Page 로 누른다(WS-30).
        page = self._top_page()
        x, y = int(params["x"]), int(params["y"])

        claimed_epoch = params.get("epoch")
        current = self.ctx.engine.epoch
        if claimed_epoch is None or int(claimed_epoch) != current:
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.TOCTOU_MISMATCH,
                error_message=(
                    f"좌표 클릭 epoch 불일치: 요청 {claimed_epoch}, 현재 {current}. "
                    "SoM 스크린샷을 다시 찍으십시오."
                ),
                reobserve_required=True,
            )

        viewport = page.viewport_size or {}
        vw, vh = viewport.get("width", 0), viewport.get("height", 0)
        if vw and vh and (x >= vw or y >= vh):
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_FOUND,
                error_message=f"좌표 ({x}, {y})가 뷰포트 {vw}x{vh} 밖입니다.",
            )

        before = await capture_state(page)
        watch = _NavWatch(page) if self.ctx.nav_settle else None
        try:
            await page.mouse.click(x, y, button=params.get("button", "left"))
        except Exception as exc:  # noqa: BLE001
            if watch is not None:
                watch.close()
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_INTERACTABLE,
                error_message=f"좌표 클릭 발송 실패: {exc}",
            )
        # WS-25: 좌표 클릭(mouse.click)은 이동을 기다리지 않는다 — 여기서 기다린다.
        if watch is not None:
            self._nav_info = await watch.settle()
        try:
            after = await capture_state(page)
        except Exception:  # noqa: BLE001
            if not self._nav_info:
                raise
            return self._result(
                success=True,
                action=action,
                retry_safe=is_retry_safe(action, FailurePhase.POST_DISPATCH),
                reobserve_required=True,
                data={"coordinates": {"x": x, "y": y}, "signals": ["navigation_started"],
                      "silent": False},
            )
        post = verify_post_condition(before, after)
        return self._result(
            success=True,
            action=action,
            retry_safe=is_retry_safe(action, FailurePhase.POST_DISPATCH),
            data={
                "coordinates": {"x": x, "y": y},
                "signals": post.signals,
                "silent": not post.satisfied,
            },
        )

    async def _dispatch_tab_control(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        """탭 생성/전환/종료/목록 (PRD §4.1 tab_control 4서브커맨드).

        `BrowserCore`가 주입되지 않은 경우(단위 테스트 등)에는 페이지의
        컨텍스트를 직접 사용해 최소 동작을 제공한다.
        """
        command = str(params.get("command", "")).lower()
        core = self.ctx.core

        if core is None:
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.FEATURE_NOT_IMPLEMENTED,
                error_message="tab_control에는 BrowserCore 주입이 필요합니다.",
            )

        try:
            if command == "list":
                tabs = core.tabs()
                return self._result(
                    success=True,
                    action=action,
                    retry_safe=True,
                    data={
                        "tabs": [
                            {"tab_id": t.tab_id, "url": t.page.url} for t in tabs
                        ],
                        "count": len(tabs),
                        "active": core.active_tab_id,
                    },
                )

            if command in ("create", "new"):
                # 계약(TabControlInput)은 "create", 내부 호출 호환용으로 "new" 도 받는다.
                tab = await core.new_tab(
                    core.active_profile, url=params.get("url")
                )
                # 새 탭이 활성 대상이 되도록 디스패처 컨텍스트를 갱신한다.
                self._set_active_page(tab.page, tab.tab_id)
                self.ctx.engine.bump_epoch("tab_new")
                return self._result(
                    success=True,
                    action=action,
                    retry_safe=False,
                    reobserve_required=True,
                    data={"tab_id": tab.tab_id, "url": tab.page.url},
                )

            if command == "switch":
                tab_id = params.get("tab_id")
                tab = core.get_tab(tab_id) if tab_id else None
                if tab is None:
                    return self._result(
                        success=False,
                        action=action,
                        retry_safe=True,
                        error_code=ErrorCode.TAB_NOT_FOUND,
                        error_message=f"탭을 찾을 수 없습니다: {tab_id}",
                    )
                core.set_active_tab(tab.tab_id)
                self._set_active_page(tab.page, tab.tab_id)
                # 탭 전환은 컨텍스트 전환이므로 에포크를 올린다 (PRD §4.2).
                self.ctx.engine.bump_epoch("tab_switch")
                return self._result(
                    success=True,
                    action=action,
                    retry_safe=True,
                    reobserve_required=True,
                    data={"tab_id": tab.tab_id, "url": tab.page.url},
                )

            if command == "close":
                tab_id = params.get("tab_id") or self.ctx.tab_id
                if core.get_tab(tab_id) is None:
                    return self._result(
                        success=False,
                        action=action,
                        retry_safe=True,
                        error_code=ErrorCode.TAB_NOT_FOUND,
                        error_message=f"탭을 찾을 수 없습니다: {tab_id}",
                    )
                await core.close_tab(tab_id)
                remaining = core.tabs()
                if remaining:
                    core.set_active_tab(remaining[0].tab_id)
                    self._set_active_page(remaining[0].page, remaining[0].tab_id)
                self.ctx.engine.bump_epoch("tab_close")
                return self._result(
                    success=True,
                    action=action,
                    retry_safe=False,
                    reobserve_required=True,
                    data={"closed": tab_id, "remaining": len(remaining)},
                )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                success=False,
                action=action,
                retry_safe=False,
                error_code=ErrorCode.PAGE_CRASHED,
                error_message=f"탭 제어 실패: {exc}",
            )

        return self._result(
            success=False,
            action=action,
            retry_safe=True,
            error_code=ErrorCode.FEATURE_NOT_IMPLEMENTED,
            error_message=(
                f"알 수 없는 서브커맨드: {command!r} "
                "(create / switch / close / list 중 하나여야 합니다)"
            ),
        )

    async def _dispatch_unknown(
        self, action: ActionType, params: Dict[str, Any]
    ) -> ActionResult:
        return self._result(
            success=False,
            action=action,
            retry_safe=True,
            error_code=ErrorCode.FEATURE_NOT_IMPLEMENTED,
            error_message=f"미지원 액션: {action.value}",
        )

    async def _wait_for(self, params: Dict[str, Any]) -> ActionResult:
        page = self.ctx.page
        condition = params.get("condition", "stabilize")
        timeout = params.get("timeout_ms", 10000)
        try:
            if condition == "selector":
                await page.wait_for_selector(params["selector"], timeout=timeout)
            elif condition == "network_idle":
                await page.wait_for_load_state("networkidle", timeout=timeout)
            elif condition == "spa_route":
                start_url = page.url
                deadline = time.perf_counter() + timeout / 1000
                while page.url == start_url and time.perf_counter() < deadline:
                    await page.wait_for_timeout(100)
                if page.url == start_url:
                    raise TimeoutError("SPA 라우팅이 발생하지 않았습니다.")
            else:  # stabilize
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                await page.wait_for_timeout(200)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                success=False,
                action=ActionType.WAIT_FOR,
                retry_safe=True,
                error_code=ErrorCode.TIMEOUT,
                error_message=f"{condition} 대기 실패: {exc}",
            )
        return self._result(
            success=True,
            action=ActionType.WAIT_FOR,
            retry_safe=True,
            data={"condition": condition},
        )

    async def _extract(self, params: Dict[str, Any]) -> ActionResult:
        page = self.ctx.page
        # 필수 인자 누락은 KeyError 예외가 아니라 명확한 실패로 보고한다.
        # 예외로 터지면 상위에서 원인을 알 수 없고 스텝만 낭비된다.
        # 실측 — LLM이 selector 없이 extract를 호출해 KeyError가 났다.
        selector = params.get("selector")
        if not selector:
            # 계약에 INVALID_SELECTOR가 없다(동결). 셀렉터가 없으면
            # 요소를 특정할 수 없으므로 ELEMENT_NOT_FOUND로 매핑한다.
            return self._result(
                success=False,
                action=ActionType.EXTRACT,
                retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_FOUND,
                error_message="extract에는 selector가 필요합니다 (CSS 선택자).",
            )
        attributes: Sequence[str] = params.get("attributes", [])
        extract_all = params.get("extract_all", False)

        payload = await page.evaluate(
            """
            (args) => {
              const nodes = args.all
                ? Array.from(document.querySelectorAll(args.selector))
                : [document.querySelector(args.selector)].filter(Boolean);
              return nodes.map((el) => {
                const item = { text: (el.innerText || el.textContent || '').trim() };
                for (const attr of args.attrs) item[attr] = el.getAttribute(attr);
                return item;
              });
            }
            """,
            {"selector": selector, "attrs": list(attributes), "all": extract_all},
        )
        if not payload:
            return self._result(
                success=False,
                action=ActionType.EXTRACT,
                retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_FOUND,
                error_message=f"셀렉터에 해당하는 요소가 없습니다: {selector}",
            )
        return self._result(
            success=True,
            action=ActionType.EXTRACT,
            retry_safe=True,
            data={"items": payload if extract_all else payload[0]},
        )

    async def _screenshot_som(self, action: ActionType) -> ActionResult:
        """Tier-2 SoM 스크린샷 (PRD §3.1, §3.4). `som_enabled` 게이트 통과 후에만 호출.

        반환 data:
        * som_tags        — [{tag, role, name, bbox, selector_path}] (프루닝 이전 후보, 최대 60)
        * image_b64       — 라벨이 얹힌 뷰포트 PNG (1280×720)
        * image_tokens    — 계약 상수 SOM_IMAGE_TOKENS_PER_CAPTURE (예산 누적용)
        * candidate_count — 후보 수. 0이면 순수 Canvas 등 DOM 타깃이 없는 페이지이며,
                            호출자는 이를 좌표 모드 전환 신호로 쓴다(옵션 B).
                            따라서 0개도 **성공**으로 반환한다.

        vision 패키지는 지연 import — SoM이 꺼진 배포에서 dispatcher가
        vision 의존을 끌고 들어오지 않게 한다.
        """
        import base64

        from contracts import thresholds
        from vision import collect_candidates, render_som

        try:
            candidates = await collect_candidates(self.ctx.page)
            png = await render_som(self.ctx.page, candidates)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                success=False,
                action=action,
                retry_safe=True,
                error_code=ErrorCode.SCREENSHOT_FAILED,
                error_message=f"SoM 캡처 실패: {exc}",
            )

        return self._result(
            success=True,
            action=action,
            retry_safe=True,
            data={
                "bytes": len(png),
                "som_tags": [
                    {
                        "tag": c.tag,
                        "role": c.role,
                        "name": c.name,
                        "bbox": c.bbox.model_dump(),
                        # 루프가 후보를 재구성해 bind_tag에 넘길 때 필요하다.
                        "selector_path": c.selector_path,
                    }
                    for c in candidates
                ],
                "image_b64": base64.b64encode(png).decode("ascii"),
                "image_tokens": thresholds.SOM_IMAGE_TOKENS_PER_CAPTURE,
                "candidate_count": len(candidates),
            },
        )

    @staticmethod
    async def _query_frame_element(context: Any, selector: str) -> Any:
        try:
            return await context.query_selector(selector)
        except Exception:  # noqa: BLE001 — 잘못된 셀렉터는 '못 찾음'으로 보고한다
            return None

    async def _switch_frame(self, params: Dict[str, Any]) -> ActionResult:
        """iframe·shadow 컨텍스트 전환 (WS-30: 상대 → 절대 순서).

        frame_selector 는 **현재 컨텍스트(지금 들어가 있는 프레임) 기준으로 먼저** 찾고,
        없으면 **최상위 문서 기준으로** 찾는다. 예전에는 최상위 기준으로 먼저 찾아, outer
        안에서 `iframe` 을 다시 주면 항상 outer 가 잡혔다(에이전트 헛돌기 16회).
        to_main=true 는 최상위 문서로 돌아간다.
        """
        page = self.ctx.page
        frame_selector = params.get("frame_selector")
        if frame_selector:
            if params.get("to_main"):
                return self._result(
                    success=False,
                    action=ActionType.SWITCH_FRAME,
                    retry_safe=True,
                    error_code=ErrorCode.FRAME_NOT_FOUND,
                    error_message="to_main 과 frame_selector 는 함께 지정할 수 없습니다.",
                )
            root = self.ctx.root_page
            resolved_from = "current_frame"
            element = await self._query_frame_element(page, frame_selector)
            if element is None and root is not None and root is not page:
                element = await self._query_frame_element(root, frame_selector)
                resolved_from = "root"
            frame = None
            if element is not None:
                try:
                    frame = await element.content_frame()
                except Exception:  # noqa: BLE001
                    frame = None
            if frame is None:
                children = await self._child_frames(page)
                current_url = self._current_url()
                hints = ", ".join(c["selector_hint"] or c["url"] for c in children) or "없음"
                return self._result(
                    success=False,
                    action=ActionType.SWITCH_FRAME,
                    retry_safe=True,
                    error_code=ErrorCode.FRAME_NOT_FOUND,
                    error_message=(
                        f"프레임을 찾을 수 없습니다: {frame_selector} "
                        f"(현재 프레임 {current_url} 과 최상위 문서에서 찾음; "
                        f"현재 프레임 안 iframe: {hints})"
                    ),
                    data={
                        "current_frame_url": current_url,
                        "frame_depth": (await self._frame_depth_path(page))[0]
                        if self.ctx.root_page is not None else 0,
                        "child_frames": children,
                    },
                )

            # **전환한 프레임을 실제 활성 컨텍스트로 만든다.**
            # epoch만 올리고 프레임을 저장하지 않으면 이후 관찰이 계속
            # 메인 문서를 본다(실측 — switch_frame 성공 후에도 관찰
            # 결과가 전환 전과 동일했다).
            if self.ctx.root_page is None:
                self.ctx.root_page = page
            self.ctx.page = frame
            self.ctx.engine.bump_epoch("switch_frame")
            data = await self._frame_data(frame)
            data["resolved_from"] = resolved_from
            return self._result(
                success=True,
                action=ActionType.SWITCH_FRAME,
                retry_safe=True,
                data=data,
            )

        # frame_selector가 없으면 메인 문서로 복귀한다.
        if params.get("to_main"):
            if self.ctx.root_page is not None:
                self.ctx.page = self.ctx.root_page
                self.ctx.root_page = None
                self.ctx.engine.bump_epoch("switch_frame")
            return self._result(
                success=True,
                action=ActionType.SWITCH_FRAME,
                retry_safe=True,
                data={
                    "frame_url": self.ctx.page.url,
                    "frame_depth": 0,
                    "frame_path": [],
                    "child_frames": await self._child_frames(self.ctx.page),
                },
            )

        shadow_selector = params.get("shadow_root_selector")
        exists = await page.evaluate(
            "(sel) => { const el = document.querySelector(sel); "
            "return !!(el && el.shadowRoot); }",
            shadow_selector,
        )
        if not exists:
            return self._result(
                success=False,
                action=ActionType.SWITCH_FRAME,
                retry_safe=True,
                error_code=ErrorCode.SHADOW_ROOT_NOT_FOUND,
                error_message=f"Shadow root를 찾을 수 없습니다: {shadow_selector}",
            )
        self.ctx.engine.bump_epoch("switch_shadow")
        return self._result(
            success=True,
            action=ActionType.SWITCH_FRAME,
            retry_safe=True,
            data={"shadow_root": shadow_selector},
        )
