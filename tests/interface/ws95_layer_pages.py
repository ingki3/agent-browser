"""WS-95a(#275) 시험 페이지 — 하나투어 화면 모형(합성 값만, 실제 사이트 CSS·글자 없음).

날짜 칸을 누르면 body 끝에 absolute 두 달 달력 레이어가 늦게 붙는다(실사용 2026-11 날짜 클릭 뒤 화면 구조). 표제 모양 3종:
`div`(div.tit — 표제 null), `p`(p.tit), `sephead`(달 본문 밖 머리 행에 두 표제). 레이어 안 날짜를 누르면 `.foot .d` 첫 칸에
'월표제 일' 이 적힌다(추가 id 클릭 반영 확인용). '다음' 은 두 표제를 한 달씩 민다(표제 바뀐 뒤 옛 id 거부 확인용).
레이어 없는 비교 페이지: 상품 행·쿠키 띠(감지 안 됨)·aria-modal 모달·중앙 쿠키 설정창(covering).
"""

from __future__ import annotations

import json
from typing import Dict

STYLE = (
    "body{margin:0;font:14px sans-serif} nav li{display:inline-block;margin:2px} "
    ".bn{display:inline-block;width:280px;height:120px;margin:8px;background:#9cf} "
    ".month{display:inline-block;width:330px;vertical-align:top} .week span{display:inline-block;width:44px} "
    "ul.day{list-style:none;padding:0;margin:0} ul.day li{display:inline-block;width:44px;height:40px} "
    ".heads .tit{display:inline-block;width:300px}"
)

NAV = "<nav><ul>" + "".join(f'<li><a href="#m{i}">메뉴 항목 {i}</a></li>' for i in range(1, 31)) + "</ul></nav>"
BANNERS = "".join(f'<a class="bn" href="#b{i}">특가 배너 {i} 바로가기</a>' for i in range(1, 19))
FOOTER = "<footer>" + " ".join(f'<a href="#f{i}">하단 링크 {i}</a>' for i in range(1, 15)) + "</footer>"
SEARCH = ('<div id="search"><button type="button" id="from">SEL 인천/김포</button><button type="button" id="to">도착지</button>'
          '<button type="button" id="d1" onclick="openCal()">11.13(금)</button><button type="button" id="d2">11.17(화)</button>'
          '<button type="button">성인 1명 일반석</button><button type="button">항공권 검색</button></div>')
WEEK = '<div class="week">' + "".join(f"<span>{d}</span>" for d in "일월화수목금토") + "</div>"
FOOT = ('<div class="foot"><span class="d">2026.11.13(금)</span><span class="d">2026.11.17(화)</span>'
        '<button type="button" class="ok">2026.11.13(금) ~ 2026.11.17(화) 선택완료</button></div>')
LAYER_STYLE = ("position:absolute;left:70px;top:170px;width:720px;height:540px;background:#fff;"
               "border:1px solid #333;z-index:100")

#: 레이어 안 날짜 클릭 → `.foot .d` 첫 칸에 '표제 일'. '다음' → 표제를 한 달씩 민다(본문 날짜는 그대로 — 표제만 바뀐다).
SCRIPT = r"""
function calTitles(){ return Array.from(document.querySelectorAll('#cal .tit')); }
document.addEventListener('click', function (ev) {
  const a = ev.target.closest && ev.target.closest('#cal ul.day a');
  if (a) {
    const months = Array.from(document.querySelectorAll('#cal .month'));
    const k = months.indexOf(a.closest('.month'));
    const t = calTitles()[k];
    document.querySelector('#cal .foot .d').textContent = (t ? t.textContent : String(k)) + ' ' + a.textContent;
    return;
  }
  const nx = ev.target.closest && ev.target.closest('#cal .next');
  if (nx) {
    for (const t of calTitles()) {
      const m = /(\d+)년 (\d+)월/.exec(t.textContent);
      if (!m) continue;
      let y = +m[1], mo = +m[2] + 1;
      if (mo > 12) { mo = 1; y += 1; }
      t.textContent = y + '년 ' + mo + '월';
    }
  }
});
"""


def _days(n: int, lead: int = 0) -> str:
    return ('<ul class="day">' + "<li></li>" * lead
            + "".join(f'<li><a href="javascript:void(0)">{d}</a></li>' for d in range(1, n + 1)) + "</ul>")


def calendar(shape: str, extra: str = "") -> str:
    """레이어 HTML. extra = 레이어 끝에 붙일 추가 마크업(묶음에 들지 않는 레이어 구성원 등)."""
    nov, dec = _days(30), _days(31, lead=2)
    nav_btns = '<button type="button" class="prev">이전</button><button type="button" class="next">다음</button>'
    if shape == "sephead":
        head = ('<div class="heads"><button type="button" class="prev">이전</button><div class="tit">2026년 11월</div>'
                '<div class="tit">2026년 12월</div><button type="button" class="next">다음</button></div>')
        body = (f'<div class="bodies"><div class="month">{WEEK}<div class="days">{nov}</div></div>'
                f'<div class="month">{WEEK}<div class="days">{dec}</div></div></div>')
        inner = head + body
    else:
        tag = {"div": "div", "p": "p"}[shape]
        inner = nav_btns + (
            f'<div class="bodies"><div class="month"><{tag} class="tit">2026년 11월</{tag}>{WEEK}<div class="days">{nov}</div></div>'
            f'<div class="month"><{tag} class="tit">2026년 12월</{tag}>{WEEK}<div class="days">{dec}</div></div></div>')
    return f'<div id="cal" style="{LAYER_STYLE}">{inner}{FOOT}{extra}</div>'


def hana(shape: str, extra: str = "", before: str = "") -> str:
    """하나투어 모형: 날짜 칸(#d1)을 누르면 레이어가 body 끝에 붙는다(처음엔 없음)."""
    layer = json.dumps(calendar(shape, extra), ensure_ascii=False)
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>항공 합성</title>'
            f"<style>{STYLE}</style></head><body>{NAV}{before}{SEARCH}<div id=\"banners\">{BANNERS}</div>{FOOTER}"
            f"<script>{SCRIPT}\nfunction openCal(){{if(!document.getElementById('cal')){{"
            f"document.body.insertAdjacentHTML('beforeend', {layer})}}}}</script></body></html>")


SHAPES = ("div", "p", "sephead")

#: 레이어 안·묶음 밖 구성원(쌍둥이 아님) — 반환도 묶음도 안 되면 등록 해제돼야 한다.
LAYER_NOTES = "".join(f'<a href="#n{i}" class="note">{w} 안내</a>' for i, w in
                      enumerate(["수하물", "운임", "환불", "좌석", "마일리지", "제휴카드", "공동운항", "결항", "탑승수속", "여권",
                                 "비자", "환승", "기내식", "반려동물", "유아", "단체", "보험", "렌터카", "호텔", "공항"]))

#: 레이어 밖·base 안의 큰 쌍둥이 세트(서로 다른 표제 — 상품 행). 반복 묶음 예산을 레이어 묶음과 다툰다(리뷰 B4).
BIG_ROWS = ('<div class="rows">' + "".join(
    f'<div class="row"><b>추천 노선 {i}</b> <button type="button">담기 {i}</button> <button type="button">비교 {i}</button>'
    f' <button type="button">찜 {i}</button></div>' for i in range(1, 9)) + "</div>")

#: 레이어 밖 숨긴 체크박스(WS-81 추가 요소) — 레이어 묶음 id 와 겹치면 안 된다.
HIDDEN_CHECK = ('<style>.hc input{position:absolute;opacity:0;width:1px;height:1px}</style>'
                '<label class="hc"><input type="checkbox" name="direct"><span>직항만</span></label>')


def _plain(title: str, body: str, style: str = "") -> str:
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{title}</title><style>{STYLE}{style}</style>'
            f"</head><body>{NAV}<div>{BANNERS}</div>{body}</body></html>")


NO_LAYER = {
    "products": ('<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>상품</title></head><body><h1>상품</h1>'
                 '<div class="list">' + "".join(
                     f'<div class="row"><span>상품 {i}</span><span>{100 + i}만원</span><button type="button">담기</button></div>'
                     for i in range(1, 9)) + "</div></body></html>"),
    "cookie_bar": _plain("쿠키띠", '<div id="cb">쿠키를 사용합니다 <button type="button">허용</button>'
                         '<button type="button">거부</button><a href="#pol">정책</a></div>',
                         "#cb{position:fixed;left:0;bottom:0;width:100%;height:80px;background:#fff;"
                         "border-top:1px solid #000;z-index:9}"),
}

MODAL = _plain("모달", '<div id="dlg" role="dialog" aria-modal="true" aria-label="로그인"><h2>회원 로그인</h2>'
               '<label>아이디 <input name="uid"></label><label>비밀번호 <input type="password" name="pw"></label>'
               '<label><input type="checkbox" name="keep"> 로그인 유지</label><button type="button">로그인</button>'
               '<button type="button">닫기</button><a href="#find">아이디 찾기</a><a href="#join">회원가입</a></div>',
               "#dlg{position:fixed;left:25%;top:15%;width:50%;height:60%;background:#fff;border:1px solid #000;z-index:9}")

COOKIE_CENTER = _plain("쿠키", '<div id="cw"><p>쿠키 설정</p><button type="button">모두 허용</button>'
                       '<button type="button">모두 거부</button><button type="button">선택 저장</button><ul>' + "".join(
                           f'<li><label><input type="checkbox" name="v{i}"> 파트너 {i}</label> '
                           f'<button type="button">자세히 {i}</button></li>' for i in range(1, 13)) + "</ul></div>",
                       "#cw{position:fixed;left:20%;top:10%;width:60%;height:75%;overflow:auto;background:#fff;"
                       "border:1px solid #000;z-index:9}")


def pages() -> Dict[str, str]:
    out = {f"/hana_{s}": hana(s) for s in SHAPES}
    out["/hana_notes"] = hana("p", extra=LAYER_NOTES)
    out["/hana_big"] = hana("div", before=BIG_ROWS)
    out["/hana_hidden"] = hana("p", before=HIDDEN_CHECK)
    out.update({f"/{k}": v for k, v in NO_LAYER.items()})
    out["/modal"] = MODAL
    out["/cookie_center"] = COOKIE_CENTER
    return out
