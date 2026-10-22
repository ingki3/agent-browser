"""WS-62 R2: 과잉 거부(R5) — 같은 달인데 항목 안 형제 글자(가격·호버 글자)만 바뀌어도 다른 묶음으로 판정하던 결함.

검증 증거(.hermes/verify/ws62-r1/evidence/main/usability_probe.py)의 달력을 실제 MCP 서버 + 로컬 HTTP + Chromium 으로
돌린다: 두 달 패널(`span.month` + `ul > li > a[data-date]` + 형제 `span.price`), 가격이 0.5/1.2/2.0초에 갱신·1초마다
지속 갱신·호버 때 `small` 추가·가격이 `a` 안에서 바뀜. 12/19 → 1/6 두 날짜 선택이 재관찰 1회 이하로 끝나고 입력칸 값이
맞아야 한다. 달(묶음 신원)이 바뀌는 차단은 tests/actions/test_ws62_r1.py·test_ws62_repeated.py 가 그대로 고정한다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402

HTML = """<!doctype html><html><meta charset=utf-8><style>
body{font:14px sans-serif}.cal{display:flex;gap:40px}.panel{width:380px}
ul{display:flex;flex-wrap:wrap;padding:0;list-style:none}li{width:48px;height:44px}
a{display:inline-block;min-width:25px;min-height:20px}.price{display:block;font-size:10px}
.hover{background:yellow}</style><body><button id=open>날짜 열기</button>
<input id=dep readonly aria-label="가는 날"><input id=ret readonly aria-label="오는 날"><p id=out>대기</p>
<div id=calendar hidden></div><script>
const mode=location.pathname.slice(1);window.clicks=[];window.priceChanges=0;
function prices(){document.querySelectorAll('.price').forEach((p,i)=>p.textContent=(148+priceChanges+i%2)+'만');priceChanges++}
document.querySelector('#open').onclick=function(){
const cal=document.querySelector('#calendar');cal.hidden=false;cal.className='cal';
const inside=mode==='inside-price';
cal.innerHTML=['2026년 12월','2027년 1월'].map((m,k)=>'<div class=panel><span class=month>'+m+'</span><ul>'
 +Array.from({length:31},(_,i)=>'<li><a href=# data-date="'+(k?'2027.01.':'2026.12.')+String(i+1).padStart(2,'0')+'">'
 +(i+1)+(inside?'<span class=price></span>':'')+'</a>'+(inside?'':'<span class=price></span>')+'</li>').join('')
 +'</ul></div>').join('');
cal.querySelectorAll('a').forEach(a=>{
 a.onclick=e=>{e.preventDefault();let key=clicks.length?'ret':'dep';document.querySelector('#'+key).value=a.dataset.date;
  clicks.push(a.dataset.date);document.querySelector('#out').textContent='선택 '+a.dataset.date+' ('+clicks.length+')';
  a.parentNode.classList.add('pick');};
 if(mode==='hover-text'){a.onmouseenter=()=>{a.parentNode.classList.add('hover');
  if(!a.parentNode.querySelector('small')){let s=document.createElement('small');s.textContent='특가';a.parentNode.appendChild(s)}};}
});
if(mode==='delay500'||mode==='inside-price')setTimeout(prices,500);
if(mode==='refresh'){setTimeout(prices,500);setTimeout(prices,1200);setTimeout(prices,2000);}
if(mode==='live-refresh'){setTimeout(prices,500);setInterval(prices,1000);}
};</script></body></html>"""

MODES = ["refresh", "live-refresh", "hover-text", "inside-price", "delay500"]
#: 관찰 → 판단 지연(초). live-refresh 는 갱신 주기(1초)보다 길게 — 매번 관찰 뒤 가격이 바뀐다.
DELAY = {"refresh": 0.75, "live-refresh": 1.1, "hover-text": 0.03, "inside-price": 0.75, "delay500": 0.75}


@pytest.fixture
def site():
    yield from _serve({"/": HTML})


def _target(obs, date: str) -> str:
    month = "2026년 12월" if date.startswith("2026") else "2027년 1월"
    day = str(int(date[-2:]))
    return next(m["id"] for g in obs.data["repeated"]["groups"] if g["group"] == month
                for m in g["members"] if m["name"].split()[0] == day)


@requires_chromium
@pytest.mark.parametrize("mode", MODES)
async def test_item_sibling_text_change_does_not_force_reobserve(site, tmp_path, mode):
    from interface.mcp_server import BrowserMCPServer

    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        assert (await s.call_tool("browser_navigate", {"url": f"{site}/{mode}"})).success
        assert (await s.call_tool("browser_click", {"selector": "#open"})).success
        obs = await s.call_tool("browser_observe_page", {"prune_top_n": 300})
        assert obs.success
        reobserve = 0
        for date in ["2026.12.19", "2027.01.06"]:
            for _attempt in range(4):
                eid = _target(obs, date)
                if mode == "hover-text":
                    await s._page.locator(s._dispatcher.ctx.engine.get_handle(eid).css_path).hover()
                await asyncio.sleep(DELAY[mode])
                res = await s.call_tool("browser_click", {"element_id": eid, "epoch": obs.snapshot_epoch})
                if res.success:
                    break
                assert res.reobserve_required, (res.error_code, res.error_message)
                reobserve += 1
                obs = await s.call_tool("browser_observe_page", {"prune_top_n": 300})
                assert obs.success
        values = [await s._page.input_value("#dep"), await s._page.input_value("#ret")]
        changed = await s._page.evaluate("priceChanges")
        small = await s._page.locator("small").count()
    assert values == ["2026.12.19", "2027.01.06"], (mode, values, reobserve)
    assert reobserve <= 1, (mode, reobserve)
    # 시나리오가 실제로 형제 글자를 바꿨는가(측정 조건 성립 — 규칙 4)
    if mode == "hover-text":
        assert small >= 1
    else:
        assert changed >= 1


# ------------------------------------------------------------------ 바깥 정적 글자가 빈 패널(span 셋): 신원은 항목 순서열


def _triple_spans_html() -> str:
    from ws62_pages import HANA_MINI_HTML

    html = HANA_MINI_HTML.replace(
        "+ '<span class=\"month\"></span>'",
        "+ '<span class=badge>추천</span><span class=\"month\"></span><span>특가</span>'",
    )
    assert "<span class=badge>" in html
    return html


@requires_chromium
async def test_triple_spans_same_month_sibling_text_change_click_works():
    """span 셋 패널(달 표제가 반복 항목처럼 보여 (a) 가 빈다) — 같은 달에서 칸마다 형제 가격·호버 글자가 붙어도 누른다."""
    from playwright.async_api import async_playwright
    from test_ws62_r1 import _by_selector
    from test_ws62_repeated import _observe, _open

    from contracts import ActionType

    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, _triple_spans_html())
        try:
            await _observe(d)
            eid = await _by_selector(engine, page, "#cal .inr:first-child ul.day li:nth-child(6) a")
            await page.evaluate("document.querySelectorAll('#cal ul.day li').forEach(function (li, i) {"
                                "const s = document.createElement('span'); s.className = 'price';"
                                "s.textContent = (150 + i) + '만'; li.appendChild(s);"
                                "const m = document.createElement('small'); m.textContent = '특가'; li.appendChild(m);})")
            res = await d.dispatch(ActionType.CLICK, {"element_id": eid})
            dep = await page.input_value("#dep")
        finally:
            await browser.close()
    assert res.success, (res.error_code, res.error_message)
    assert dep == "2026.12.19"


@requires_chromium
async def test_item_sequence_change_without_static_text_is_refused():
    """바깥 정적 글자가 전혀 없는 두 패널 — 같은 노드를 재사용한 채 앞 빈칸 수(달의 시작 요일)가 바뀌면 누르지 않는다."""
    from playwright.async_api import async_playwright
    from test_ws62_r1 import OUT, _by_selector
    from test_ws62_repeated import _observe, _open

    from contracts import ActionType, ErrorCode

    def panel(k: int) -> str:
        return ("<div class=p><ul>" + "<li></li>" * (2 + k) + "".join(
            f"<li><a href='#' onclick=\"document.getElementById('out').textContent='p{k}-{n}';return false\">{n}</a></li>"
            for n in range(1, 8)) + "</ul></div>")

    html = "<!doctype html><body>" + OUT + "<div style='display:flex'>" + panel(0) + panel(1) + "</div></body>"
    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, html)
        try:
            await _observe(d)
            eid = await _by_selector(engine, page, ".p:first-child li:nth-child(5) a")
            await page.evaluate("const u = document.querySelector('.p ul'); u.insertBefore(document.createElement('li'),"
                                " u.firstChild)")
            res = await d.dispatch(ActionType.CLICK, {"element_id": eid})
            out = await page.text_content("#out")
        finally:
            await browser.close()
    assert out == "대기", f"바뀐 묶음을 눌렀다: {out}"
    assert res.success is False and res.error_code is ErrorCode.TOCTOU_MISMATCH and res.reobserve_required
