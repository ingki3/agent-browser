"""WS-85 (#211, WS-86 리뷰 B1·B3·NB2·D1): 단독 '차단'·'비우기' 의 탐색·편집 문맥 예외는 격리 world 의 **보이는 배치**가 확인할
때만 — 6경로(element_id·selector·좌표·Enter·Space·batch) 모두.

표본은 WS-86 리뷰 probe_a(원 9변형 + 추가 3변형 + markup 없는 prototype 위조 2 + 대조 10 + 위험 2)와 실사용 모양 12종(probe_b)을
그대로 옮겼다. 한계(B3·D3): 보이는 장식 입력칸 바로 옆 '비우기'(attack-empty-input)는 진짜 검색 지우기와 DOM·배치가 같아 막지
못한다 — 문서화한 한계로 눌림을 고정한다. 의도한 판정 변화(D1): tablist 없는 role=tab(filter-tab)·탭 1개 tablist 는 이제 승인.
실제 효과(눌림)는 격리 world 판독으로 단정한다(페이지 world 위조가 닿지 않음).
"""

from __future__ import annotations

import html as _html
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_cli import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402

from interface.mcp_server import BrowserMCPServer, envelope_dict  # noqa: E402

CASES: List[Dict[str, Any]] = []


def add(key: str, name: str, kind: str = "attack", attrs: str = "", before: str = "", href: Any = None,
        after: str = "", script: str = "", wrap: Any = None) -> None:
    tag = "a" if href else "button"
    attrs += ((' href="' + href + '"') if href else " type=button")
    el = ("<" + tag + " id=t " + attrs + " onclick=\"document.querySelector('#out').textContent='clicked';"
          "return false\">" + _html.escape(name) + "</" + tag + ">")
    if wrap:
        el = wrap.replace("@@", el)
    body = ("<!doctype html><meta charset=utf-8><title>local mail UI</title><main><h1>메일 도구 모음</h1>"
            "<p id=out>waiting</p>" + before + el + after + "</main>" + (("<script>" + script + "</script>") if script else ""))
    role = "tab" if "role=tab " in attrs or "role=tab>" in attrs else ("link" if href else "button")
    CASES.append(dict(id=key, name=name, kind=kind, html=body, role=role))


# --- WS-76 R1 원 9변형(.hermes/verify/ws76-r1/public_r1.py:18-26)
add("attack-block-tab", "차단", attrs="role=tab aria-selected=false ")
add("attack-block-selected", "차단", attrs="aria-selected=false")
add("attack-block-controls", "차단", attrs="aria-controls=out")
add("attack-empty-input", "비우기", kind="limit", before='<input value="decorative">')  # B3·D3 한계
add("attack-empty-hidden-input", "비우기", before='<input hidden value="decorative">')
add("attack-guide-before", "안내 확인 후 휴지통 비우기")
add("attack-guide-after", "휴지통 비우기 지금 실행 안내")
add("attack-cancel", "Report spam and cancel subscription")
add("attack-how", "How to report spam now")
# --- markup 없는 main world prototype 위조
SPOOF_HAS = ("const oh=Element.prototype.hasAttribute;Element.prototype.hasAttribute=function(n){"
             "if(n==='aria-selected')return true;return oh.call(this,n)};")
add("spoof-proto-block", "차단", script=SPOOF_HAS)
SPOOF_CLOSEST = ("const oc=Element.prototype.closest;Element.prototype.closest=function(s){"
                 "if(String(s).indexOf('tablist')>=0)return document.body;return oc.call(this,s)};")
add("spoof-closest-block", "차단", script=SPOOF_CLOSEST)
# 페이지 world 판독을 통째로 속이는 위조(사보타주 '예외를 페이지 판독으로' 가 잡히게): role·closest·querySelectorAll 을 바꿔
# 대상을 '보이는 탭 2개 목록' 의 탭으로 보이게 한다 — 격리 world 에는 role 이 없다.
SPOOF_ROLE = ("const T=document.getElementById('t');const P=T.parentElement;const S=document.createElement('button');"
              "S.type='button';S.textContent='전체';P.insertBefore(S,T);const ga=Element.prototype.getAttribute;"
              "Element.prototype.getAttribute=function(n){if(n==='role'){if(this===T||this===S)return 'tab';"
              "if(this===P)return 'tablist';}return ga.call(this,n)};const cl=Element.prototype.closest;"
              "Element.prototype.closest=function(q){q=String(q);if(q.indexOf('tablist')>=0&&(this===P||P.contains(this)))"
              "return P;if(q.indexOf('\"tab\"')>=0&&this===T)return T;return cl.call(this,q)};"
              "const qa=Element.prototype.querySelectorAll;Element.prototype.querySelectorAll=function(q){"
              "if(this===P&&String(q).indexOf('tab')>=0)return [S,T];return qa.call(this,q)};")
add("spoof-role-tablist", "차단", script=SPOOF_ROLE)
# --- 추가 3변형
add("attack-tablist-wrap", "차단", attrs="role=tab ", wrap="<div role=tablist>@@</div>")
add("attack-offscreen-input", "비우기", before='<input style="position:absolute;left:-9999px" value="x">')
add("attack-opacity-input", "비우기", before='<input style="opacity:0" value="x">')
# --- 추가 적대(NB2): clip-path 로 감춘 입력칸·덮개로 가린 입력칸
add("attack-clip-input", "비우기", before='<input style="clip-path:inset(100%)" value="x">')
add("attack-covered-input", "비우기",
    before='<span style="position:relative;display:inline-block"><input value="x">'
           '<span style="position:absolute;inset:0;background:#fff"></span></span>')
# --- 대조(LOW 유지)
add("search-clear", "비우기", "control", before='<label>검색<input id=q value="search words"></label>')
add("real-tablist", "차단", "control", attrs="role=tab aria-selected=false aria-controls=p2 ",
    wrap='<div role=tablist><button type=button role=tab aria-selected=true aria-controls=p1>전체</button>@@'
         '<button type=button role=tab aria-selected=false>허용</button></div><div id=p1>목록</div>'
         '<div id=p2 hidden>차단 목록</div>')
for _k, _n in [("empty-cancel", "휴지통 비우기 취소"), ("empty-guide", "휴지통 비우기 안내"),
               ("spam-guide", "스팸 신고하기 안내"), ("how", "How to report spam"),
               ("spam-empty-guide", "스팸함 비우기 안내")]:
    add(_k, _n, "control")
add("help-link", "스팸 신고 방법", "control", href="/help/report-spam")
# --- 의도한 판정 변화(D1): 예전 대조 → 이제 승인
add("filter-tab", "차단", "changed", attrs="role=tab aria-selected=false aria-controls=blocked ",
    before="<div id=blocked>상태별 목록</div>")
add("single-tab-tablist", "차단", "changed", attrs="role=tab aria-controls=blocked aria-selected=false ",
    wrap="<div role=tablist>@@</div><section id=blocked>차단한 사용자 보기</section>")
# --- 문맥 없는 위험(HIGH 유지)
add("plain-block", "차단", "risky")
add("plain-empty", "비우기", "risky")

_B = "onclick=\"document.querySelector('#out').textContent='clicked';return false\""


def _real(body: str) -> str:
    return "<!doctype html><meta charset=utf-8><title>realistic</title><main><p id=out>waiting</p>" + body + "</main>"


#: WS-86 리뷰 probe_b 실사용 모양 12종 — rt-* 는 LOW(눌림), rk-* 와 rt-wrapped-clear(기준도 HIGH, NB1) 는 HIGH.
REAL: Dict[str, tuple] = {
    "rt-link-tabs": ("control", "tab", "차단", _real(
        f'<div role=tablist><a role=tab href="?f=all" aria-selected=true>전체</a> <a id=t role=tab href="?f=blocked" {_B}>'
        '차단</a> <a role=tab href="?f=ok">허용</a></div>')),
    "rt-vertical": ("control", "tab", "차단", _real(
        '<div role=tablist aria-orientation=vertical style="display:flex;flex-direction:column;width:120px">'
        f'<button role=tab aria-selected=true>전체</button><button id=t role=tab {_B}>차단</button>'
        '<button role=tab>스팸</button></div>')),
    "rt-li-tabs": ("control", "tab", "차단", _real(
        '<ul role=tablist style="display:flex;list-style:none;gap:12px"><li role=presentation><button role=tab '
        f'aria-selected=true>받은 요청</button></li><li role=presentation><button id=t role=tab {_B}>차단</button></li></ul>')),
    "rt-span-tab": ("control", "tab", "차단", _real(
        f'<div role=tablist><button role=tab>전체</button><button id=t role=tab {_B}><span>차단</span></button></div>')),
    "rt-overlay-clear": ("control", "button", "비우기", _real(
        '<div style="position:relative;display:inline-block"><input id=q value="abc" style="width:240px;padding-right:60px">'
        f'<button id=t style="position:absolute;right:4px;top:1px" {_B}>비우기</button></div>')),
    "rt-flex-clear": ("control", "button", "비우기", _real(
        f'<div style="display:flex;gap:8px"><input id=q type=search value="abc"><button id=t {_B}>비우기</button></div>')),
    "rt-wrapped-clear": ("risky", "button", "비우기", _real(
        '<div style="display:flex;gap:6px"><div class=box><input id=q value="abc"></div><div>'
        f'<button id=t {_B}>비우기</button></div></div>')),
    "rt-textarea-clear": ("control", "button", "비우기", _real(
        f'<div><textarea id=q rows=3>memo</textarea><button id=t {_B}>비우기</button></div>')),
    "rt-below-clear": ("control", "button", "비우기", _real(
        '<div><input id=q value="abc" style="display:block;width:240px"><button id=t style="margin-top:6px" '
        f'{_B}>비우기</button></div>')),
    "rk-cart-empty": ("risky", "button", "비우기", _real(
        f'<div>수량 <input type=number value=2> <button id=t {_B}>비우기</button></div>')),
    "rk-far-input": ("risky", "button", "비우기", _real(
        '<div><input value="abc"></div><div style="height:400px"></div>'
        f'<div><button id=t {_B}>비우기</button></div>')),
    "rk-block-in-list": ("risky", "button", "차단", _real(f'<ul><li>Alice <button id=t {_B}>차단</button></li></ul>')),
}
for _key, (_kind, _role, _name, _body) in REAL.items():
    CASES.append(dict(id=_key, name=_name, kind=_kind, html=_body, role=_role))

PATHS = ("element_id", "selector", "coordinates", "enter", "space", "batch")


@pytest.fixture(scope="module")
def site():
    pages = {"/case/" + c["id"]: c["html"] for c in CASES}
    pages["/"] = "<!doctype html><meta charset=utf-8><p>root</p>"
    yield from _serve(pages)


async def _effect(server: BrowserMCPServer) -> str:
    await server._dispatcher.ctx.page.wait_for_timeout(60)  # noqa: SLF001
    return str(await server._dispatcher.isolated_top_evaluate(  # noqa: SLF001
        "document.querySelector('#out').textContent"))


async def _press(server: BrowserMCPServer, base: str, case: Dict[str, Any], path: str) -> tuple:
    assert (await server.call_tool("browser_navigate", {"url": base + "/case/" + case["id"]})).success
    page = server._dispatcher.ctx.page  # noqa: SLF001
    if path == "selector":
        r = await server.call_tool("browser_click", {"selector": "#t"})
    elif path in ("enter", "space"):
        await page.focus("#t")
        r = await server.call_tool("browser_press_key", {"key": "Enter" if path == "enter" else "Space"})
    elif path == "batch":
        r = await server.call_server_tool("browser_batch", {"steps": [
            {"action": "click", "target": {"role": case["role"], "name": case["name"]}}]})
    else:
        obs = await server.call_tool("browser_observe_page", {})
        eid = next(e["element_id"] for e in obs.data["observation"]["elements"]
                   if (server._engine.get_handle(e["element_id"]).css_path or "").endswith("#t"))  # noqa: SLF001
        if path == "element_id":
            params: Dict[str, Any] = {"element_id": eid, "epoch": obs.snapshot_epoch}
        else:
            box = await page.locator("#t").bounding_box()
            params = {"x": int(box["x"] + box["width"] / 2), "y": int(box["y"] + box["height"] / 2),
                      "epoch": obs.snapshot_epoch}
        r = await server.call_tool("browser_click", params)
    env = r if isinstance(r, dict) else envelope_dict(r)
    text = json.dumps(env, ensure_ascii=False)
    gated = "approval_required" in text or "E_HITL" in text
    return await _effect(server), gated


async def _matrix(site: str, path: str, tmp_path: Path) -> Dict[str, tuple]:
    out = {}
    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "h") as server:
        for case in CASES:
            if path == "space" and (case["role"] == "link" or "<a id=t" in case["html"]):
                continue  # 링크는 Space 로 눌리지 않는다(브라우저 동작)
            out[case["id"]] = (case["kind"],) + await _press(server, site, case, path)
    return out


@requires_chromium
@pytest.mark.parametrize("path", PATHS)
async def test_211_context_exceptions_only_from_isolated_visible_layout(site, path, tmp_path):
    rows = await _matrix(site, path, tmp_path)
    wrong = []
    for cid, (kind, effect, gated) in rows.items():
        if kind in ("attack", "risky", "changed"):
            if effect == "clicked" or not gated:
                wrong.append((cid, kind, effect, gated))
        elif effect != "clicked":  # control·limit: 눌림(기준 그대로 / 문서화한 한계)
            wrong.append((cid, kind, effect, gated))
    assert not wrong, (path, wrong)


def test_211_sample_census():
    kinds = [c["kind"] for c in CASES]
    # 원 9 + 추가 3 + markup 0 위조 2 = 14(그중 1 한계) + 페이지 판독 통째 위조 1 + NB2 2, 대조 8 + 실사용 LOW 8, 의도 변화 2, 위험 2 + 4.
    assert kinds.count("attack") == 16 and kinds.count("limit") == 1
    assert kinds.count("control") == 16 and kinds.count("changed") == 2 and kinds.count("risky") == 6
