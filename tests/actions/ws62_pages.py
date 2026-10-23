"""WS-62 시험 페이지 — 증거(hana_20376/20382 관찰 원문, 2026-10-22 실제 DOM)의 구조를 본뜬 로컬 페이지들.

하나투어 달력: `div.calendar_wrap > div.inr / div.inr.right > span "2026년 12월" + ul.day > li(class 만 다름) > a[href=#]`,
ARIA 없음, 선택 상태는 class 뿐, 한쪽 달 날짜에만 가격(`22\\n\\n148만`), 이전/다음달 링크, 요일 머리글, 이름 없는 링크
30개, 캐러셀 복제, 푸터 링크 열(이름이 다 다름 — 미발동 대조군).
"""

from __future__ import annotations

_HANA = r"""<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font:14px sans-serif;width:1200px}
.calendar_wrap{display:flex;gap:20px}
.inr{width:320px}
ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;width:300px}
li{width:40px;height:34px}
a{display:inline-block;min-width:20px;min-height:20px}
.noname{display:inline-block;width:12px;height:12px}
.wrapper{display:flex}
.slide{width:200px}
footer{display:flex}
</style></head><body>
<label for="dep">가는 날</label><input id="dep" readonly value="">
<p id="out">대기</p>
<div class="calendar_wrap" id="cal"></div>
<div class="nonames">__NONAMES__</div>
<div class="swiper"><div class="wrapper">
  <div class="slide"><a href="#s">괌 왕복 특가 항공권 일정 선택 가능 최저가 보장 상품</a></div>
  <div class="slide"><a href="#s">하와이 왕복 특가 항공권 일정 선택 가능 최저가 보장 상품</a></div>
  <div class="slide"><a href="#s">사이판 왕복 특가 항공권 일정 선택 가능 최저가 보장 상품</a></div>
  <div class="slide"><a href="#s">괌 왕복 특가 항공권 일정 선택 가능 최저가 보장 상품</a></div>
  <div class="slide"><a href="#s">하와이 왕복 특가 항공권 일정 선택 가능 최저가 보장 상품</a></div>
</div></div>
<footer>
  <div class="col"><h3>회사</h3><ul><li><a href="#f">회사소개</a></li><li><a href="#f">채용</a></li><li><a href="#f">제휴</a></li></ul></div>
  <div class="col"><h3>고객</h3><ul><li><a href="#f">공지사항</a></li><li><a href="#f">자주묻는질문</a></li><li><a href="#f">1:1문의</a></li></ul></div>
  <div class="col"><h3>약관</h3><ul><li><a href="#f">이용약관</a></li><li><a href="#f">개인정보처리방침</a></li><li><a href="#f">여행약관</a></li></ul></div>
  <div class="col"><h3>메뉴</h3><ul><li><a href="#f">해외여행</a></li><li><a href="#f">국내여행</a></li><li><a href="#f">호텔</a></li></ul></div>
</footer>
<script>
let base = 11;  // 0-based 달 번호: 11 = 2026년 12월
const FIRST = __FIRST__, COUNT = __COUNT__;  // 보이는 날짜 범위(작은 변형은 치유 재관찰 상위 20 안에 들게)
function ym(k) { return [2026 + Math.floor(k / 12), k % 12]; }
function days(y, m) { return new Date(y, m + 1, 0).getDate(); }
function build(wrap) {
  const cal = document.getElementById('cal');
  cal.innerHTML = '';
  for (const side of ['left', 'right']) {
    const inr = document.createElement('div');
    inr.className = 'inr' + (side === 'right' ? ' right' : '');
    const cell = wrap ? '<li><span class="w"><a href="#"></a></span></li>' : '<li><a href="#"></a></li>';
    inr.innerHTML = (side === 'left' ? '<a href="#" class="prev" onclick="move(-1);return false">이전달</a>' : '')
      + '<span class="month"></span>'
      + (side === 'right' ? '<a href="#" class="next" onclick="move(1);return false">다음달</a>' : '')
      + '<ul class="week">' + '일월화수목금토'.split('').map(function (d) { return '<li>' + d + '</li>'; }).join('') + '</ul>'
      + '<ul class="day"><li class="before"></li><li class="before"></li>'
      + Array.from({length: COUNT}, function () { return cell; }).join('') + '</ul>';
    cal.appendChild(inr);
  }
  render();
}
function render() {
  document.querySelectorAll('#cal .inr').forEach(function (p, idx) {
    const ym2 = ym(base + idx), y = ym2[0], m = ym2[1];
    p.querySelector('.month').textContent = y + '년 ' + (m + 1) + '월';
    const n = days(y, m);
    p.querySelectorAll('ul.day li:not(.before)').forEach(function (li, i) {
      const d = FIRST + i, a = li.querySelector('a');
      li.style.display = d <= n ? '' : 'none';
      // 오른쪽 달만 가격이 섞인다(증거 20382: '22\n\n148만'). 노드(a)는 그대로 두고 내용만 바꾼다.
      a.innerHTML = d + (idx === 1 && d % 3 === 1 ? '<br><span class="price">148만</span>' : '');
      a.onclick = function () {
        document.getElementById('dep').value = y + '.' + String(m + 1).padStart(2, '0') + '.' + String(d).padStart(2, '0');
        document.getElementById('out').textContent = 'picked';
        return false;
      };
      li.className = (idx === 0 && d === 19) ? 'select' : (d === 20 ? 'pick' : '');
    });
  });
}
function move(k) { base += k; render(); }          // 달 넘김: 같은 노드 재사용(하나투어 실측)
function rerender(k) { base += k; build(true); }    // 노드를 지우고 다른 구조로 다시 그림(치유 시험)
build(false);
</script></body></html>"""


def _hana(first: int, count: int, extras: bool) -> str:
    html = _HANA.replace("__FIRST__", str(first)).replace("__COUNT__", str(count))
    if extras:
        return html.replace("__NONAMES__", "".join('<a href="#n" class="noname"></a>' for _ in range(30)))
    start, end = html.index('<div class="nonames">'), html.index("</footer>") + len("</footer>")
    return html[:start] + html[end:]


#: 증거 구조 전체(이름 없는 링크 30개·캐러셀 복제·푸터 열 포함, 날짜 1~31).
HANA_HTML = _hana(1, 31, True)
#: 같은 구조의 작은 변형(날짜 16~22, 부가 영역 없음) — 치유 재관찰(기본 상위 20)에 날짜가 들어오게.
HANA_MINI_HTML = _hana(16, 7, False)

#: 표 caption 형 — 두 달 표.
CAPTION_HTML = """<!doctype html><html><body><p id=out>대기</p><div class=wrap style="display:flex">""" + "".join(
    f"""<table><caption>{cap}</caption><thead><tr>""" + "".join(f"<th>{d}</th>" for d in "일월화수목금토")
    + "</tr></thead><tbody>" + "".join(
        "<tr>" + "".join(
            f"<td><a href='#' onclick=\"document.getElementById('out').textContent='{cap} {r * 7 + c + 1}';return false\">"
            f"{r * 7 + c + 1}</a></td>" for c in range(7)) + "</tr>" for r in range(4))
    + "</tbody></table>" for cap in ("2026년 12월", "2027년 1월")) + "</div></body></html>"

#: ARIA grid 형 — aria-label 표제·aria-selected/aria-disabled 상태.
GRID_HTML = """<!doctype html><html><body><div class=dp style="display:flex">""" + "".join(
    f"""<div role=grid aria-label="{cap}">""" + "".join(
        "<div role=row>" + "".join(
            f"<span role=gridcell><button aria-selected=\"{'true' if (cap.endswith('12월') and n == 19) else 'false'}\""
            f"{' aria-disabled=true' if n == 3 else ''}>{n}</button></span>"
            for n in range(r * 7 + 1, r * 7 + 8)) + "</div>" for r in range(3))
    + "</div>" for cap in ("2026년 12월", "2027년 1월")) + "</div></body></html>"

#: 상품 행 형 — 같은 이름 '담기' 버튼.
PRODUCTS_HTML = """<!doctype html><html><body><p id=out>대기</p><ul class=prod>""" + "".join(
    f"<li><span class=nm>상품 {k}</span> <em>{p}원</em> "
    f"<button onclick=\"document.getElementById('out').textContent='담음 {k}'\">담기</button></li>"
    for k, p in (("A", "12,000"), ("B", "15,000"), ("C", "9,900"), ("D", "30,000"))) + "</ul></body></html>"


def twin_panels(head_a: str, head_b: str, names_a=("1", "2", "3", "4"), names_b=("1", "2", "3", "4"),
                prefix_link: str = "") -> str:
    """두 패널(div > [링크] + span 표제 + ul > li > a) — 표제·이름을 바꿔 가며 쓰는 일반형."""
    def panel(head: str, names) -> str:
        lead = f'<a href="#">{prefix_link}</a>' if prefix_link else ""
        span = f"<span>{head}</span>" if head else ""
        lis = "".join(f'<li><a href="#">{n}</a></li>' for n in names)
        return f'<div class="p">{lead}{span}<ul>{lis}</ul></div>'
    return ("<!doctype html><html><body><div class=w style='display:flex'>" + panel(head_a, names_a)
            + panel(head_b, names_b) + "</div></body></html>")
