"""WS-62 시험 페이지 corpus(R12) — R4~R11 시험·검증 증거 생성기를 원문 그대로 옮긴 것(시험 아님).

R12 에서 R6~R11 시험(다중 고정·가림 전제)은 지웠지만 그 페이지들은 기준(v1.16.0) 동일성 불변식·행동 시험의 표본으로 그대로
쓴다. 출처: test_ws62_r4_hanatour·r5·r6·r7·r8·r9·r10·r11 의 같은 이름 함수·상수(r8 `_quick` 은 `_quick8` 로 이름만 바꿈),
B 45 는 검증 증거 `.hermes/verify/ws62-r11/evidence/b_risk_probe.py` 의 cases·html_for.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any, Dict, List, Tuple

# ------------------------------------------------------------------ test_ws62_r4_hanatour.py

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ws62" / "hanatour_calendar.html"


def page_html(css: bool = True) -> str:
    html = FIXTURE.read_text(encoding="utf-8")
    assert '<style id="hide">' in html
    return html if css else html.replace('<style id="hide">', '<style id="hide" media="not all">')


def _n2_html(empties: int) -> str:
    def panel(head: str, body: str) -> str:
        return f"<div class=p><span>{head}</span>{body}</div>"

    days = "<ul>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in (1, 2, 3)) + "</ul>"

    def only_buttons(names: str) -> str:   # 패널마다 다른 이름 — 단추끼리 항목 쌍둥이 충돌을 만들지 않게
        return ("<ul><li>가</li><li>나</li><li>다</li></ul><div class=b>"
                + "".join(f"<button>{n}번</button>" for n in names) + "</div>")

    extra = "".join(panel(head, only_buttons(names)) for head, names in (("안내", "xyz"), ("공지", "uvw"))[:empties])
    return ("<!doctype html><html><body><div style='display:flex'>" + panel("1월", days) + panel("2월", days)
            + extra + "</div></body></html>")


# ------------------------------------------------------------------ test_ws62_r5.py

def _dates(kind: str, first: int, last: int) -> str:
    if kind == "ul":
        return "<ul>" + "".join(f"<li><a href='#'>{d}</a></li>" for d in range(first, last + 1)) + "</ul>"
    weeks = [range(w, min(w + 7, last + 1)) for w in range(first, last + 1, 7)]
    if kind == "table":
        return ("<table><tbody>" + "".join("<tr>" + "".join(f"<td><a href='#'>{d}</a></td>" for d in w) + "</tr>"
                                            for w in weeks) + "</tbody></table>")
    assert kind == "div"
    return ("<div class=weeks>" + "".join("<div class=week>" + "".join(f"<div class=cell><a href='#'>{d}</a></div>" for d in w)
                                          + "</div>" for w in weeks) + "</div>")


def mixed_depth_html(kind: str) -> str:
    months = []
    for month, n in ((10, 31), (11, 30)):
        deep = "<section>" * 7 + _dates(kind, 19, n) + "</section>" * 7
        months.append(f"<div class=month><h2>2026년 {month}월</h2>{_dates(kind, 1, 18)}{deep}</div>")
    script = ("<script>document.querySelectorAll('.month a').forEach(function (a) { a.onclick = function () {"
              " document.getElementById('out').textContent = this.closest('.month').querySelector('h2').textContent"
              " + '/' + this.textContent; return false; }; });</script>")
    return "<!doctype html><html><body><p id=out>none</p><div id=cal>" + "".join(months) + "</div>" + script + "</body></html>"


# ------------------------------------------------------------------ test_ws62_r6.py

def _cells(kind: str, first: int, last: int) -> str:
    if kind == "ul":
        return "<ul>" + "".join(f'<li><a href="#">{i}</a></li>' for i in range(first, last + 1)) + "</ul>"
    weeks = [range(w, min(last + 1, w + 7)) for w in range(first, last + 1, 7)]
    if kind == "table":
        return ("<table><tbody>" + "".join("<tr>" + "".join(f'<td><a href="#">{i}</a></td>' for i in w) + "</tr>"
                                           for w in weeks) + "</tbody></table>")
    assert kind == "div"
    return ("<div class=weeks>" + "".join("<div class=week>" + "".join(f'<div class=cell><a href="#">{i}</a></div>' for i in w)
                                          + "</div>" for w in weeks) + "</div>")


def mixed_sig_html(shallow: str, deep: str, depth: int) -> str:
    months = []
    for m, n in ((10, 31), (11, 30)):
        inner = "<section>" * depth + _cells(deep, 19, n) + "</section>" * depth
        months.append(f"<article class=month><h2>2026년 {m}월</h2>" + _cells(shallow, 1, 18) + inner + "</article>")
    script = ("<script>document.querySelectorAll('.month a').forEach(function (a) { a.onclick = function () {"
              " document.getElementById('out').textContent = this.closest('.month').querySelector('h2').textContent"
              " + '/' + this.textContent; return false; }; });</script>")
    return "<!doctype html><html><body><p id=out>none</p><div id=cal>" + "".join(months) + "</div>" + script + "</body></html>"


def product_html(kind: str) -> str:
    rows = []
    for k in "ABCD":
        if kind == "card":
            rows.append(f'<article class=product aria-label="상품 {k}"><div class=nm>상품 {k}</div><div>재고 있음</div>'
                        f"<div><button>담기</button></div></article>")
        else:
            rows.append(f'<tr class=product aria-label="상품 {k}"><td class=nm>상품 {k}</td><td>12000원</td>'
                        f"<td><button>담기</button></td></tr>")
    body = ("<div class=grid>" + "".join(rows) + "</div>") if kind == "card" else "<table><tbody>" + "".join(rows) + "</tbody></table>"
    script = ("<script>document.querySelectorAll('button').forEach(function (b) { b.onclick = function () {"
              " document.getElementById('out').textContent = b.closest('.product').getAttribute('aria-label') + '/' + b.textContent;"
              " }; });</script>")
    return "<!doctype html><html><body><p id=out>none</p>" + body + script + "</body></html>"


def deep_card_html(depth: int = 6) -> str:
    """상품 카드: article > [div.nm, div 재고, div.buy > div×depth > button] — 담기는 상품에서 depth+2 단계."""
    cards = "".join(f'<article class=product aria-label="상품 {k}"><div class=nm>상품 {k}</div><div>재고 있음</div><div class=buy>'
                    + "<div>" * depth + "<button>담기</button>" + "</div>" * depth + "</div></article>" for k in "ABCD")
    script = ("<script>document.querySelectorAll('button').forEach(function (b) { b.onclick = function () {"
              " document.getElementById('out').textContent = b.closest('.product').getAttribute('aria-label') + '/' + b.textContent;"
              " }; });</script>")
    return "<!doctype html><html><body><p id=out>none</p><div class=grid>" + cards + "</div>" + script + "</body></html>"


CENSUS_PAGES = ["hanatour_css", "hanatour_nocss", "mixed_ul", "mixed_table", "mixed_div", "sig_d7_ul_table",
                "sig_d10_div_ul", "sig_d10_table_div", "product_card", "product_table", "n2_one", "n2_two", "legend",
                "widgets"]


def _legend_html() -> str:
    def panel(month: str, notes) -> str:
        days = "<ul class=day>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in range(1, 6)) + "</ul>"
        legend = "<div class=legend>" + "".join(f"<span><a href='#'>{n} 안내</a></span>" for n in notes) + "</div>"
        return f"<div class=inr><div class=header><span>{month}</span></div>{days}{legend}</div>"

    return ("<!doctype html><html><body><div id=cal style='display:flex'>" + panel("2026년 12월", ("휴무", "마감", "특가"))
            + panel("2027년 1월", ("운항", "좌석", "요금")) + "</div></body></html>")


def _widgets_html() -> str:
    def widget(months) -> str:
        btns = "<ul class=btns>" + "".join(f"<li><a href='#'>{b}</a></li>" for b in ("초기화", "닫기", "적용")) + "</ul>"
        cal = "".join("<div class=inr><span>" + m + "</span><ul class=day>"
                      + "".join(f"<li><a href='#'>{n}</a></li>" for n in range(1, 4)) + "</ul></div>" for m in months)
        return f"<div class=w>{btns}<div class=cal style='display:flex'>{cal}</div></div>"

    return ("<!doctype html><html><body><div style='display:flex'>" + widget(("2026년 12월", "2027년 1월"))
            + widget(("2027년 2월", "2027년 3월")) + "</div></body></html>")


def _census_html(name: str) -> str:
    if name.startswith("hanatour"):
        return page_html(name.endswith("_css"))
    if name.startswith("mixed_"):
        return mixed_depth_html(name.split("_")[1])
    if name.startswith("sig_"):
        _, d, a, b = name.split("_")
        return mixed_sig_html(a, b, int(d[1:]))
    if name == "product_deep":
        return deep_card_html()
    if name.startswith("product_"):
        return product_html(name.split("_")[1])
    if name.startswith("n2_"):
        return _n2_html(1 if name == "n2_one" else 2)
    return _legend_html() if name == "legend" else _widgets_html()


# ------------------------------------------------------------------ test_ws62_r7.py

def _quick() -> str:
    return "<ul class=quick>" + "".join("<li><button>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"


def _options(kind: str = "ul", twins: int = 2, same: bool = False) -> str:
    sections = []
    for i in range(twins):
        label = "공통 옵션" if same else ("기본 옵션" if i == 0 else "추가 옵션")
        if kind == "table":
            inner = ("<table><tbody><tr>" + "".join("<td><button class=target>담기</button><button>찜</button></td>"
                                                   for _ in range(3)) + "</tr></tbody></table>")
        elif kind == "calendar":
            inner = "<ul>" + "".join(f"<li><button class=target>{n}</button><button>찜</button></li>" for n in (19, 20, 21)) + "</ul>"
        elif kind == "badges":
            inner = "<div>" + "".join("<span>할인<button class=target>담기</button><button>찜</button></span>" for _ in range(3)) + "</div>"
        else:
            inner = "<ul>" + "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3)) + "</ul>"
        sections.append(f'<section aria-label="{label}"><h3>{label}</h3>{inner}</section>')
    return "<div class=options>" + "".join(sections) + "</div>"


def nested_product_html(case: str) -> str:
    """검증 증거 adversarial_matrix.py 와 같은 13변형(상품 A~D, 같은 노드 유지·상품 신원만 바꾼다)."""
    rows = []
    for k in "ABCD":
        q = _quick() if case not in ("nested_no_prefix", "same_twin_no_prefix", "single_nested") else ""
        typ = {"table_in_table": "table", "calendar_product": "calendar", "repeated_badges": "badges"}.get(case, "ul")
        opt = _options(typ, 1 if case == "single_nested" else 2, case in ("carousel_clone", "same_twin_no_prefix"))
        if case == "basic_two_buttons":
            opt = "<div><button class=target>담기</button><button>찜</button></div>"
        if case == "deep_inner":
            opt = "<div>" * 7 + opt + "</div>" * 7
        if case == "inner_three_buttons":
            opt = opt.replace("<button>찜</button>", "<button>찜</button><button>비교</button>")
        if case in ("n2_one_empty", "n2_two_empty"):
            n = 1 if case == "n2_one_empty" else 2
            opt = opt.replace("</div>", '<section aria-label="빈 옵션"><ul><li>없음</li><li>없음</li><li>없음</li></ul></section>' * n + "</div>")
        if case == "table_in_table":
            rows.append(f'<tr class=product data-sku="{k}" aria-label="상품 {k}"><td><h2 class=nm>상품 {k}</h2>{q}</td>'
                        f"<td>재고 있음</td><td>{opt}</td></tr>")
        else:
            rows.append(f'<article class=product data-sku="{k}" aria-label="상품 {k}"><h2 class=nm>상품 {k}</h2>{q}{opt}</article>')
    body = ("<table><tbody>" + "".join(rows) + "</tbody></table>") if case == "table_in_table" else "<div class=grid>" + "".join(rows) + "</div>"
    return "<!doctype html><html><body><p id=out>none</p>" + body + "</body></html>"


NESTED_CASES = ["basic_two_buttons", "single_nested", "nested_no_prefix", "nested_prefix", "repeated_badges",
                "table_in_table", "carousel_clone", "calendar_product", "n2_one_empty", "n2_two_empty",
                "same_twin_no_prefix", "deep_inner", "inner_three_buttons"]


def same_twin_layout_html() -> str:
    def discover(names) -> str:
        return "<ul>" + "".join('<li><a href="#">' + x + "</a></li>" for x in names) + "</ul>"

    actions = "<div class=actions>" + "".join("<button class=buy>담기</button>" for _ in range(3)) + "</div>"
    wrap = (discover(("검색", "지도", "일정")) + '<a href="#">고객센터</a><main data-sku="A" aria-label="상품 A"><h2>상품 A</h2>'
            + "<section>" * 7 + actions + "</section>" * 7 + "</main>")
    footer = discover(("회사", "약관", "제휴")) + '<a href="#">고객센터</a>'
    return ("<!doctype html><html><body><p id=out>none</p><div id=screenArea><div id=wrap>" + wrap + "</div><div id=footer>"
            + footer + "</div></div></body></html>")


def live_panels_html(kind: str) -> str:
    attr = {"live": 'aria-live="polite"', "timer": 'role="timer"', "status": 'role="status"', "marquee": 'role="marquee"'}[kind]

    def panel(month: str) -> str:
        days = "<ul class=day>" + "".join(f"<li><a href='#'>{n}</a></li>" for n in range(1, 6)) + "</ul>"
        return f"<div class=inr><div class=header><span>{month}</span><em class=ticker {attr}>소식 0</em></div>{days}</div>"

    return ("<!doctype html><html><body><p id=out>none</p><div id=cal style='display:flex'>" + panel("2026년 12월")
            + panel("2027년 1월") + "</div><script>document.querySelectorAll('#cal a').forEach(function (a) { a.onclick ="
            " function () { document.getElementById('out').textContent = this.closest('.inr').querySelector('span').textContent"
            " + '/' + this.textContent; return false; }; });</script></body></html>")


def nav_rows_html() -> str:
    """기준(v1.16.0)이 항목 쌍둥이(div.row ×3 — 바깥 '가는 날' ↔ 팝업 입력칸 '가는 날' 충돌)로 팝업 안 이전/다음달까지 고정하는
    구조. 팝업 항목 안 기간 표시(p.range)가 달 이동마다 바뀐다 — 달 패널 밖이라 가림으로는 흡수되지 않는다."""
    def panel() -> str:
        return ("<div class=inr><div class=header><span></span></div><ul class=day>"
                + "".join("<li><a href='#'></a></li>" for _ in range(5)) + "</ul></div>")

    script = """<script>
let base = 10; window.picks = [];
function render() {
  document.querySelectorAll('#cal .inr').forEach(function (p, k) {
    const m = base + k; p.querySelector('.header span').textContent = '2026년 ' + m + '월';
    p.querySelectorAll('li a').forEach(function (a, i) { a.textContent = String(i + 1 + (m % 2)); a.dataset.m = m; });
  });
  document.querySelector('.range').textContent = '2026.' + base + ' ~ 2026.' + (base + 1);
}
document.querySelector('#cal .next').onclick = function () { base += 1; render(); return false; };
document.querySelector('#cal .prev').onclick = function () { base -= 1; render(); return false; };
document.querySelectorAll('#cal li a').forEach(function (a) { a.onclick = function () { window.picks.push(a.dataset.m + '/' + a.textContent); return false; }; });
render();
</script>"""
    return ("<!doctype html><html><body><div class=rows>"
            "<div class=row><div class=bowl><a href='#'>가는 날</a></div><div class=bowl><a href='#'>오는 날</a></div>"
            "<div class=bowl><a href='#'>성인 1명</a></div></div>"
            "<div class=row><a href='#'>항공권 검색</a></div>"
            "<div class=row id=pop><div class=ly><p class=range></p><div id=cal><a href='#' class=prev>이전달</a>"
            "<a href='#' class=next>다음달</a>" + panel() + panel() + "</div></div>"
            "<div class=opt><input aria-label='가는 날'><input aria-label='오는 날'></div></div>"
            "</div>" + script + "</body></html>")


UNION_PAGES = CENSUS_PAGES + ["same_twin", "nav_rows", "nested_prefix", "nested_no_prefix", "table_in_table",
                              "deep_inner", "calendar_product", "n2_two_empty"]


# ------------------------------------------------------------------ test_ws62_r8.py

def _quick8() -> str:
    return "<ul>" + "".join("<li><button>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"


def _opts(kind: str = "ul", buried: bool = False, k: str = "A", rolling: bool = False) -> str:
    parts = []
    for i in range(2):
        title = f"<h2 class=nm>상품 {k}</h2>" if buried and i == 0 else ""
        roll = "<small class=roll>오늘 12명 구매</small>" if rolling and i == 0 else ""
        cells = "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3))
        if kind == "table":
            cells = ("<table><tbody><tr>" + "".join("<td><button class=target>담기</button><button>찜</button></td>"
                                                   for _ in range(3)) + "</tr></tbody></table>")
        else:
            cells = "<ul>" + cells + "</ul>"
        parts.append(f"<section><h3>옵션 {i}</h3>{title}{roll}{cells}</section>")
    return "<div class=options>" + "".join(parts) + "</div>"


def adversarial_html(case: str, rolling: bool = False) -> str:
    rows = []
    buried = case in BURIED
    for k in "ABCD":
        name = "" if buried else f"<h2 class=nm>상품 {k}</h2>"
        inner = _opts("table" if "table" in case else "ul", buried, k, rolling)
        if "deep" in case:
            inner = "<div>" * 7 + inner + "</div>" * 7
        if case == "tab_panel":
            inner = "<div role=tabpanel>" + inner + "</div>"
        if case == "carousel":
            inner += inner
        if case == "list_in_table":
            inner = "<table><tbody><tr><td>" + inner + "</td></tr></tbody></table>"
        if case == "multi_nested":
            inner = "<aside>" + _quick8() + inner + "</aside>" + _opts(k=k)
        if case == "calendar":
            inner = inner.replace(">담기<", ">19<")
        if case == "nav_product":
            inner = "<nav>" + inner + "</nav>"
        rows.append(f"<article class=product data-sku={k}>{name}{_quick8()}{inner}</article>")
    return "<!doctype html><html><body><p id=out>none</p><div class=grid>" + "".join(rows) + "</div></body></html>"


BURIED = ("buried_name", "buried_name_table", "buried_name_deep")


ADV_CASES = ["nested", "deep", "tab_panel", "carousel", "list_in_table", "multi_nested", "calendar", "nav_product",
             *BURIED]


def _page_for(name: str) -> str:
    if name == "same_twin":
        return same_twin_layout_html()
    if name == "nav_rows":
        return nav_rows_html()
    if name in ADV_CASES and name not in NESTED_CASES:
        return adversarial_html(name)
    if name in NESTED_CASES:
        return nested_product_html(name)
    return _census_html(name)


# ------------------------------------------------------------------ test_ws62_r9.py

SHAPES = {
    "h1": "<h1 class=nm>상품 {k}</h1>",
    "h2": "<h2 class=nm>상품 {k}</h2>",
    "h6": "<h6 class=nm>상품 {k}</h6>",
    "heading": "<div role=heading class=nm>상품 {k}</div>",
    "caption": "<table><caption class=nm>상품 {k}</caption></table>",
    "legend": "<fieldset><legend class=nm>상품 {k}</legend></fieldset>",
    "rowhead": "<table><tbody><tr><th class=nm>상품 {k}</th></tr></tbody></table>",
    "strong": "<strong class=nm>상품 {k}</strong>",
    "span": "<span class=nm>상품 {k}</span>",
    "aria_label": '<div class=nm aria-label="상품 {k}"></div>',
    "image_alt": '<img class=nm alt="상품 {k}">',
    "dt_dd": "<dl><dt>상품명</dt><dd class=nm>상품 {k}</dd></dl>",
    "link": '<a class=nm href="#detail">상품 {k}</a>',
    "root_labelled": '<span class=nm id="label{k}">상품 {k}</span>',
    "root_aria": "",
    "inner_aria": "",
    "inner_labelled": '<span class=nm id="label{k}">상품 {k}</span>',
}


SIBLING_CASES = [(s, p) for p in ("buried", "outside") for s in SHAPES
                 if not (p == "outside" and s in ("inner_aria", "inner_labelled"))]


def sibling_html(shape: str, pos: str) -> str:
    products = []
    for k in "ABCD":
        nm = SHAPES[shape].format(k=k)
        attrs = (f' aria-labelledby="label{k}"' if shape == "root_labelled" else
                 f' aria-label="상품 {k}"' if shape == "root_aria" else "")
        quick = "<ul>" + "".join("<li><button class=quick>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"
        inner = []
        for i in range(2):
            at = (f' aria-label="상품 {k}"' if shape == "inner_aria" and i == 0 else
                  f' aria-labelledby="label{k}"' if shape == "inner_labelled" and i == 0 else "")
            rows = "<ul>" + "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3)) + "</ul>"
            inner.append(f"<section{at}><h3>옵션 {i}</h3>{nm if i == 0 and pos == 'buried' else ''}{rows}</section>")
        products.append(f"<article class=product data-sku={k}{attrs}>{nm if pos == 'outside' else ''}{quick}"
                        "<div class=options>" + "".join(inner) + "</div></article>")
    return "<!doctype html><html><body><p id=out>none</p><div class=grid>" + "".join(products) + "</div></body></html>"


# ------------------------------------------------------------------ test_ws62_r10.py

#: 이름 태그 — labelledby 는 이름 span 을 담은 묶음(section·article)의 aria-labelledby 가 가리킨다.
NAME_TAGS = ("h2", "span", "strong", "dd", "labelledby")


#: 이름 위치 — 첫/중간/마지막 안쪽 section, 또는 바깥(article 바로 아래).
NAME_POS = ("first", "middle", "last", "outside")


def _name_index(pos: str, n: int) -> int:
    return {"first": 0, "middle": n // 2, "last": n - 1}.get(pos, -1)


#: R7 동적 글자 제외의 위험 축(조정자 조건 — 기준 고정에도 그 제외를 쓰므로 기준 대비로 잰다): 상품명이 aria-live·role=status·
#: role=timer 영역 안(live·status·timer), 이름이 여러 텍스트 노드로 나뉨(split — '여행 상품 <b>A</b>'), 대상 이름이 텍스트 노드
#: 경계로 나뉨(btnsplit — '담기<span>A</span>', 기준 textContent 첫 낱말 '담기a' / 지금 첫 텍스트 노드 '담기').
WRAPS = ("live", "status", "timer", "split", "btnsplit")


def corpus_html(n: int, levels: int, tag: str, table: bool, inside: bool, named: bool,
                pos: str = "first", partial: bool = True, wrap: str = "") -> str:
    """상품 article×4(A~D) 안 옵션 section×n(levels 단 중첩), 각 leaf section 에 담기/찜 목록 3행.

    n: 안쪽 쌍둥이 개수. levels: section 중첩 단수(2 = article·바깥 section·안쪽 section 3중). table: 상품을 표 행 안에.
    inside: 담기 목록을 표 칸으로. named: article aria-label. pos·tag: 상품명(.identity) 위치·태그. partial: 일부 section 에만
    aside(부분 공통 대칭 자리). wrap: WRAPS 중 하나(동적 제외 위험 축). pos='first'·partial=True·wrap='' 이면 검증 증거 new_siblings.py 와 같은 HTML(span·strong·dd).
    """
    cards = []
    for sku in "ABCD":
        uid = [0]

        def name_el(container_attr: List[str]) -> str:
            uid[0] += 1
            text = f"여행 상품 <b>{sku}</b>" if wrap == "split" else f"여행 상품 {sku}"
            if tag == "labelledby":
                lid = f"lbl{sku}{uid[0]}"
                container_attr.append(f' aria-labelledby="{lid}"')
                el = f"<span class=identity id={lid}>{text}</span>"
            else:
                el = f"<{tag} class=identity>{text}</{tag}>"
            live = {"live": ' aria-live="polite"', "status": " role=status", "timer": " role=timer"}.get(wrap)
            return f"<div{live}>{el}</div>" if live else el

        def sections(depth: int) -> str:
            out = []
            at_name = _name_index(pos, n)
            for i in range(n):
                attr: List[str] = []
                nm = name_el(attr) if i == at_name else ""
                shared = "<p><small>공통 설명</small></p>"
                extra = "<aside><em>부가 설명</em></aside>" if partial and i < max(1, n - 1) else ""
                if depth:
                    body = sections(depth - 1) if i == 0 else "<div>" + sections(depth - 1) + "</div>"
                else:
                    btn = f"담기<span class=identity>{sku}</span>" if wrap == "btnsplit" else "담기"
                    cells = "".join(f"<li><button class=target>{btn}</button><button>찜</button></li>" for _ in range(3))
                    body = "<ul>" + cells + "</ul>"
                    if inside:
                        body = ("<table><tbody><tr>" + "".join(
                            f"<td><button class=target>{btn}</button><button>찜</button></td>" for _ in range(3))
                            + "</tr></tbody></table>")
                out.append(f"<section{''.join(attr)}><h3>옵션 {i}</h3>{nm}{shared}{extra}{body}</section>")
            return "<div class=options>" + "".join(out) + "</div>"

        quick = "<ul>" + "".join(f"<li><button class=quick>{s}</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"
        root_attr: List[str] = [f' aria-label="상품 {sku}"'] if named else []
        outer = name_el(root_attr) if pos == "outside" else ""
        card = f"<article class=product data-sku={sku}{''.join(root_attr)}>{outer}{quick}{sections(levels - 1)}</article>"
        cards.append("<tr><td>" + card + "</td></tr>" if table else card)
    body = ("<table><tbody>" + "".join(cards) + "</tbody></table>") if table else "<div class=grid>" + "".join(cards) + "</div>"
    return "<!doctype html><html><body><output id=out>none</output>" + body + "</body></html>"


#: 검증 증거 run_new.py 18변형(name, n, levels, tag, table, inside, named) — 이름은 첫 section, 부분 공통 자리.
NEW_CASES: List[Tuple[str, int, int, str, bool, bool, bool]] = [
    ("three_buried", 3, 1, "span", False, False, False),
    ("four_buried", 4, 1, "strong", False, False, False),
    ("three_root_named", 3, 1, "span", False, False, True),
    ("four_root_named", 4, 1, "strong", False, False, True),
    ("partial2_span", 2, 1, "span", False, False, False),
    ("partial2_dd", 2, 1, "dd", False, False, False),
    ("partial3_named", 3, 1, "dd", False, False, True),
    ("triple_span", 2, 2, "span", False, False, False),
    ("triple_strong", 2, 2, "strong", False, False, False),
    ("triple_dd", 2, 2, "dd", False, False, False),
    ("triple_three", 3, 2, "span", False, False, True),
    ("card_in_table_span", 2, 1, "span", True, False, False),
    ("card_in_table_dd", 2, 1, "dd", True, False, False),
    ("triple_card_table", 2, 2, "strong", True, False, False),
    ("inner_table_span", 2, 1, "span", False, True, False),
    ("inner_table_triple", 2, 2, "strong", False, True, False),
    ("table_three_named", 3, 1, "span", True, True, True),
    ("deep_partial_named", 4, 2, "dd", True, False, True),
]


NEW_BY_NAME = {c[0]: c for c in NEW_CASES}


def new_html(name: str) -> str:
    _, n, levels, tag, table, inside, named = NEW_BY_NAME[name]
    return corpus_html(n, levels, tag, table, inside, named)


def _corpus_cases() -> List[Dict[str, Any]]:
    """이름 위치 4 × 태그 5 × 안쪽 쌍둥이 개수(2·3·4) = 60조합. 중첩 단수·표 안 카드·표 칸·부분 공통·루트 이름은 순번으로 돌린다
    (조합마다 고정 — 결정론)."""
    out = []
    k = 0
    for pos in NAME_POS:
        for tag in NAME_TAGS:
            for n in (2, 3, 4):
                levels = 2 if k % 3 == 1 and n < 4 else 1  # 3중 중첩(4쌍둥이×2단은 대상이 너무 많아 2·3 만)
                out.append({"id": f"{pos}-{tag}-n{n}-L{levels}-{'T' if k % 4 in (1, 2) else 'g'}"
                                  f"{'c' if k % 5 == 3 else ''}{'p' if k % 3 != 2 else 's'}{'N' if k % 7 == 4 else ''}",
                            "n": n, "levels": levels, "tag": tag, "table": k % 4 in (1, 2), "inside": k % 5 == 3,
                            "named": k % 7 == 4, "pos": pos, "partial": k % 3 != 2})
                k += 1
    # 동적 제외 위험 축: 감싸기 5 × 이름 위치 4(태그·쌍둥이 개수·표는 순번으로).
    for w in WRAPS:
        for pos in NAME_POS:
            tag, n = NAME_TAGS[k % 5], (2, 3, 4)[k % 3]
            out.append({"id": f"{w}-{pos}-{tag}-n{n}{'-T' if k % 2 else ''}", "n": n, "levels": 1, "tag": tag,
                        "table": bool(k % 2), "inside": False, "named": False, "pos": pos, "partial": True, "wrap": w})
            k += 1
    return out


CORPUS = _corpus_cases()


# ------------------------------------------------------------------ test_ws62_r11.py

#: (이름 태그, live 영역 안, 반복 span 셋) — 상품명이 모든 안쪽 section 에 같은 자리로(대칭 → 안쪽 대칭 자리 null).
SYM_CASES: List[Tuple[str, bool, bool]] = [("h2", False, False), ("span", False, False), ("strong", False, False),
                                           ("dd", False, False), ("span", True, False), ("span", False, True),
                                           ("span", True, True)]


def symmetric_html(tag: str, live: bool = False, units: bool = False) -> str:
    """검증 증거 cold_symmetric.py make() 와 같은 HTML."""
    cards = []
    for sku in "ABCD":
        opts = []
        for i in range(2):
            nm = f"<{tag} class=nm>여행 상품 {sku}</{tag}>"
            if units:
                nm = "<div>" + "".join(f"<span class=nm>{sku}{j}</span>" for j in range(3)) + "</div>"
            if live:
                nm = "<div aria-live=polite>" + nm + "</div>"
            opts.append("<section><h3>옵션 " + str(i) + "</h3>" + nm + "<p><small>공통 설명</small></p><ul>"
                        + "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3))
                        + "</ul></section>")
        quick = "<ul>" + "".join("<li><button class=quick>" + s + "</button></li>" for s in ["정보", "배송", "문의"]) + "</ul>"
        cards.append(f"<article class=product data-sku={sku}>{quick}<div class=options>" + "".join(opts) + "</div></article>")
    return "<!doctype html><html><body><output id=out>none</output><div class=grid>" + "".join(cards) + "</div></body></html>"


#: 상품명이 **모든** 안쪽 section 에 같은 자리로(대칭, 비-live) — 태그 5 × 형제 2·3·4. 쌍둥이 밖 대상('정보/배송/문의')에게 가린
#: 상품명은 안쪽 대상 고정만 비교한다(census '다른 대상 고정만 비교') — 실제 v1.16.0 JS 대비 행동으로 오클릭 0 을 잰다.
SYMNAME_TAGS = ("h2", "span", "strong", "dd", "labelledby")


SYMNAME_CASES = [(t, n) for t in SYMNAME_TAGS for n in (2, 3, 4)]


def symname_html(tag: str, n: int) -> str:
    cards = []
    for sku in "ABCD":
        opts = []
        for i in range(n):
            attr, nm = "", f"<{tag} class=nm>여행 상품 {sku}</{tag}>"
            if tag == "labelledby":
                attr, nm = f' aria-labelledby="lbl{sku}{i}"', f"<span class=nm id=lbl{sku}{i}>여행 상품 {sku}</span>"
            opts.append(f"<section{attr}><h3>옵션 {i}</h3>{nm}<p><small>공통 설명</small></p><ul>"
                        + "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3))
                        + "</ul></section>")
        quick = "<ul>" + "".join("<li><button class=quick>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"
        cards.append(f"<article class=product data-sku={sku}>{quick}<div class=options>" + "".join(opts) + "</div></article>")
    return "<!doctype html><html><body><output id=out>none</output><div class=grid>" + "".join(cards) + "</div></body></html>"


def independent_cases() -> List[Dict[str, str]]:
    return [{"id": f"{shape}-{layout}-{pos}", "shape": shape, "layout": layout, "pos": pos}
            for shape in ("data_mix", "title_mix", "alt_mix", "data_labelled", "title_labelled", "alt_same_name")
            for layout in ("twins5", "deep4", "list_table_list", "same_names")
            for pos in ("first", "last")]


def independent_html(c: Dict[str, str]) -> str:
    """검증 증거 corpus_probe.py independent_html 과 같은 HTML(data-*·title·img alt 와 글자 혼합, 쌍둥이 5·4중 중첩·목록/표/목록·같은 이름)."""
    def name(sku: str, uid: str) -> str:
        text = f"여행 상품 {sku}"
        shape = c["shape"]
        if shape == "data_mix":
            return f'<span class=identity data-name="{text}">여행 상품 <em>{sku}</em></span>'
        if shape == "title_mix":
            return f'<div class=identity title="{text}"><span>여행 상품</span> <strong>{sku}</strong></div>'
        if shape == "alt_mix":
            return f'<div class=identity><img alt="{text}"><span>여행 상품 {sku}</span></div>'
        if shape == "data_labelled":
            return f'<div class=identity data-product="{text}" role=heading aria-label="{text}"><small>{text}</small></div>'
        if shape == "title_labelled":
            return f'<div class=identity title="{text}" aria-labelledby="{uid}"><span id="{uid}">{text}</span></div>'
        return f'<div class=identity><img alt="같은 이름 여행"><span>같은 이름 여행</span><em>재고 {sku}</em></div>'

    cards = []
    for sku in "ABCDE":
        counter = [0]

        def section(depth: int) -> str:
            n = 5 if c["layout"] == "twins5" else 2
            out = []
            for i in range(n):
                counter[0] += 1
                uid = f"caption_{sku}_{counter[0]}"
                at = 0 if c["pos"] == "first" else n - 1
                nm = name(sku, uid) if i == at else ""
                if depth:
                    body = section(depth - 1)
                else:
                    rows = "".join("<li><button class=target>담기</button><button>찜</button></li>" for _ in range(3))
                    body = "<ul>" + rows + "</ul>"
                    if c["layout"] == "list_table_list":
                        body = "<table><tbody><tr><td>" + body + "</td></tr></tbody></table>"
                out.append(f"<section><h3>옵션 {i}</h3>{nm}<p><small>공통 설명</small></p>"
                           + ("<aside><em>추가 정보</em></aside>" if i < n - 1 else "") + body + "</section>")
            return "<div class=options>" + "".join(out) + "</div>"

        quick = "<ul>" + "".join("<li><button class=quick>" + s + "</button></li>" for s in ("정보", "배송", "문의")) + "</ul>"
        title = "<h2>공동 여행 상품</h2>" if c["layout"] == "same_names" else ""
        body = section(2 if c["layout"] == "deep4" else 0)
        article = f"<article class=product data-sku={sku}>{title}{quick}{body}</article>"
        cards.append("<li>" + article + "</li>" if c["layout"] == "list_table_list" else article)
    body = ("<ul>" + "".join(cards) + "</ul>") if c["layout"] == "list_table_list" else "<div class=grid>" + "".join(cards) + "</div>"
    return "<!doctype html><html><body><output id=out>none</output>" + body + "</body></html>"


# ------------------------------------------------------------------ 검증 증거 b_risk_probe.py B 45(그대로)


def b_cases() -> List[Dict[str, Any]]:
    return [dict(id=f'{shape}-n{n}-{layout}', shape=shape, n=n, layout=layout)
            for shape, n, layout in itertools.product(
                ('tag', 'wrapper', 'partial', 'same_name', 'live'), (2, 3, 4),
                ('flat', 'table', 'nested'))]


def b_html(c: Dict[str, Any]) -> str:
    products = []
    for k, sku in enumerate('ABCD'):
        def name(i):
            text = f'여행 상품 {sku}'
            shape = c['shape']
            if shape == 'same_name':
                text = f'동일 상품 재고 {sku}'
            tag = ('span', 'strong', 'dd', 'em')[k]
            node = f'<{tag} class=identity>{text}</{tag}>'
            if shape == 'wrapper':
                node = '<div>' * k + '<span class=identity>' + text + '</span>' + '</div>' * k
            if shape == 'partial':
                node = '<span class=identity>여행</span><' + tag + f' class=identity>상품 {sku}</{tag}>'
            if shape == 'live':
                node = '<div aria-live=polite>' + node + '</div>'
            return node
        sections = []
        for i in range(c['n']):
            rows = ''.join('<li><button class=inside>담기</button><button>찜</button></li>' for _ in range(3))
            body = '<ul>' + rows + '</ul>'
            if c['layout'] == 'nested':
                body = '<div class=deep>' + ''.join(f'<section><h4>하위 옵션 {j}</h4><ul>{rows}</ul></section>' for j in range(2)) + '</div>'
            sections.append(f'<section><h3>옵션 {i}</h3>{name(i)}<p><small>공통 설명</small></p>{body}</section>')
        quick = '<ul>' + ''.join(f'<li><button class=quick>{s}</button></li>' for s in ('정보', '배송', '문의')) + '</ul>'
        card = f'<article class=product data-sku={sku}>{quick}<div class=options>' + ''.join(sections) + '</div></article>'
        products.append('<tr><td>' + card + '</td></tr>' if c['layout'] == 'table' else card)
    body = '<table><tbody>' + ''.join(products) + '</tbody></table>' if c['layout'] == 'table' else '<div class=grid>' + ''.join(products) + '</div>'
    return '<!doctype html><html><body><output id=out>none</output>' + body + '</body></html>'


# ------------------------------------------------------------------ 묶음(family) → [(이름, html | 흐름 이름)]

#: 보존 캡처(로컬 검증 자료 — 저장소 밖 .hermes, 없으면 그 묶음은 건너뛴다).
CAPTURE_DIR = Path(__file__).resolve().parents[2] / ".hermes" / "ws62-r4-evidence"
CAPTURES = ("hana_calendar_wrap.html", "hana_full_calendar_open.html")
#: 하나투어 시험 자료 흐름(관찰 여러 번 — 시험 쪽 _hanatour_states).
FLOWS = ("hanatour_css", "hanatour_nocss")
FAMILIES = ("fixture", "captures", "prior", "corpus", "independent", "symmetric", "symname", "b45")


def family_pages(family: str) -> List[Tuple[str, str]]:
    if family == "fixture":
        return [(n, n) for n in FLOWS]
    if family == "captures":
        return [(n, (CAPTURE_DIR / n).read_text(encoding="utf-8")) for n in CAPTURES if (CAPTURE_DIR / n).is_file()]
    if family == "prior":
        names = [n for n in dict.fromkeys(CENSUS_PAGES + UNION_PAGES + ADV_CASES + NESTED_CASES) if not n.startswith("hanatour")]
        out: List[Tuple[str, str]] = [(n, _page_for(n)) for n in names]
        out += [(f"rolling:{b}", adversarial_html(b, rolling=True)) for b in BURIED]
        out += [(f"livepanels:{k}", live_panels_html(k)) for k in ("live", "timer", "status", "marquee")]
        out += [(f"sib:{s}/{p}", sibling_html(s, p)) for s, p in SIBLING_CASES]
        out += [(f"new:{c[0]}", new_html(c[0])) for c in NEW_CASES]
        return out
    if family == "corpus":
        return [(c["id"], corpus_html(c["n"], c["levels"], c["tag"], c["table"], c["inside"], c["named"], c["pos"],
                                      c["partial"], c.get("wrap", ""))) for c in CORPUS]
    if family == "independent":
        return [(c["id"], independent_html(c)) for c in independent_cases()]
    if family == "symname":
        return [(f"{t}-n{n}", symname_html(t, n)) for t, n in SYMNAME_CASES]
    if family == "b45":
        return [(c["id"], b_html(c)) for c in b_cases()]
    assert family == "symmetric", family
    return [(f"{t}-live{int(lv)}-units{int(u)}", symmetric_html(t, lv, u)) for t, lv, u in SYM_CASES]
