"""WS-82(#198): 직접 만든 시험 페이지 — 달력 오버레이(실제 CSS 형태 4종)와 적대 세트(오탐 0 이어야 함).

사용자 결정 D1=C: 하나투어 실제 스타일시트 없이 이 페이지들로만 개발·시험한다('하나투어가 고쳐졌다'고 주장하지 않음).
모든 페이지는 set_content 로 연다(외부 요청 없음). 바탕 페이지는 상호작용 후보가 N(20) 보다 훨씬 많아 기본 관찰이 실제로 잘린다.
"""

from __future__ import annotations

from typing import Dict

VIEW = {"width": 1280, "height": 900}


def _links(prefix: str, n: int) -> str:
    return "".join(f"<a href='#' onclick='return false'>{prefix} {i}</a> " for i in range(1, n + 1))


#: 바탕: 상단 메뉴·검색 폼·상품 링크·캐러셀 단추 — 달력이 열리면 이 위를 덮는다.
BASE = ("<header style='height:60px;background:#eee'>" + _links("메뉴", 10) + "</header>"
        "<main style='padding:10px'><form onsubmit='return false'><input id=dep aria-label='가는 날' readonly>"
        "<input id=ret aria-label='오는 날' readonly><output id=sel>미선택</output><button type=button>항공권 검색</button></form>"
        "<section style='display:grid;grid-template-columns:repeat(6,1fr);gap:8px'>"
        + "".join(f"<div style='height:70px;border:1px solid #ccc'><a href='#' onclick='return false'>추천 상품 {i}</a>"
                  f"<button>찜 {i}</button></div>" for i in range(1, 49))
        + "</section><div><button>Go to slide 2</button><button>Go to slide 3</button></div></main>")


def _month(title: str, days: int, start: int) -> str:
    cells = "".join("<li></li>" for _ in range(start)) + "".join(
        f"<li><a href='#' onclick=\"pick(this);return false\">{d}</a></li>" for d in range(1, days + 1))
    return (f"<div class=inr><div class=header><span>{title}</span></div>"
            f"<ul class=day style='display:grid;grid-template-columns:repeat(7,40px);list-style:none;padding:0'>{cells}</ul></div>")


CAL_BODY = ("<a href='#' class=prev onclick='return false'>이전달</a> <a href='#' class=next onclick='return false'>다음달</a>"
            "<div style='display:flex;gap:20px'>" + _month("2026년 12월", 31, 2) + _month("2027년 1월", 31, 5) + "</div>"
            "<div><button type=button class=close onclick='return false'>닫기</button>"
            "<button type=button class=ok onclick=\"document.getElementById('sel').textContent='확정됨';return false\">확정</button></div>")

PICK_JS = ("<script>function pick(a){ document.getElementById('dep').value = a.textContent;"
           " document.getElementById('sel').textContent = '선택 ' + a.textContent; }</script>")

_BOX = "top:120px;left:240px;width:800px;height:560px;background:#fff;border:1px solid #333;overflow:auto"

#: 달력 오버레이 — 실제 CSS 형태 각각(z-index 없음/낮음·fixed/absolute·top layer·aria-modal).
CALENDARS: Dict[str, str] = {
    # z-index auto, absolute — 문서 뒤쪽이라 앞 내용 위에 그려진다(z-index 기준 감지는 놓친다)
    "absolute_z_auto": BASE + f"<div id=cal style='position:absolute;{_BOX}'>" + CAL_BODY + "</div>" + PICK_JS,
    # fixed, z-index 1 (z≥100 기준 감지는 놓친다)
    "fixed_z_low": BASE + f"<div id=cal style='position:fixed;z-index:1;{_BOX}'>" + CAL_BODY + "</div>" + PICK_JS,
    # 배경 막(dim) 안의 달력 — 막이 레이어
    "fixed_dim_backdrop": BASE + "<div style='position:fixed;inset:0;background:rgba(0,0,0,.4)'>"
                          f"<div id=cal style='position:absolute;{_BOX}'>" + CAL_BODY + "</div></div>" + PICK_JS,
    # top layer(showModal)
    "top_layer_dialog": BASE + "<dialog id=cal style='width:800px'>" + CAL_BODY + "</dialog>" + PICK_JS
                        + "<script>document.getElementById('cal').showModal()</script>",
    # aria-modal(위치 무관)
    "aria_modal": BASE + f"<div id=cal role=dialog aria-modal=true style='position:absolute;{_BOX}'>" + CAL_BODY + "</div>" + PICK_JS,
}

#: 적대 세트 — 켜지면 오탐(리뷰 F3·B1). 모두 '가운데 점을 덮는 레이어'가 아니거나 덮는 후보가 없다.
ADVERSARIAL: Dict[str, str] = {
    # 일반 흐름 안 히어로 캐러셀(absolute z100, 화면 대부분) — 숨은 슬라이드는 opacity 0
    "hero_carousel": "<div style='position:relative;height:700px'>"
                     "<div style='position:absolute;inset:0;z-index:100;background:#abc'>" + _links("배너", 6) + "<button>Go to slide 2</button></div>"
                     "<div style='position:absolute;inset:0;z-index:99;opacity:0'>" + _links("숨은 배너", 6) + "</div></div>" + BASE,
    # 지도(leaflet 형태: 흐름 안 relative 상자, 안쪽 absolute 판·조작 단추 z400)
    "map_leaflet": "<div style='position:relative;height:700px;overflow:hidden;background:#cde'>"
                   "<div style='position:absolute;inset:0;z-index:400'><img alt='' style='width:100%;height:100%'></div>"
                   "<div style='position:absolute;top:10px;left:10px;z-index:1000'><button>+</button><button>-</button></div></div>" + BASE,
    # 최소화된 채팅 창(role=dialog, 작음, 구석 고정)
    "chat_minimized": BASE + "<div role=dialog aria-label='채팅' style='position:fixed;right:10px;bottom:10px;width:80px;height:80px;"
                      "background:#fc0'><button>열기</button></div>",
    # SPA 껍데기(#root absolute inset 0 이 모든 후보를 담음)
    "spa_shell": "<div id=root style='position:absolute;inset:0;z-index:1;overflow:auto'>" + BASE + "</div>",
    # 고정 머리띠(스크롤해 내용이 그 밑으로 들어감)
    "sticky_header_scrolled": "<div style='position:fixed;top:0;left:0;right:0;height:120px;z-index:1000;background:#eee'>"
                              + _links("고정 메뉴", 8) + "</div><div style='height:40px'></div>" + BASE
                              + "<div style='height:1500px'></div><script>window.scrollTo(0, 200)</script>",
    # 하단 쿠키 띠
    "cookie_bottom": BASE + "<div style='position:fixed;bottom:0;left:0;right:0;height:220px;z-index:9999;background:#ddd'>"
                     "쿠키 <button>동의</button></div>",
    # 옆 서랍(가운데를 포함하지 않음)
    "offcanvas_left": BASE + "<nav style='position:fixed;top:0;left:0;width:400px;height:100%;z-index:1000;background:#fff'>"
                      + _links("카테고리", 10) + "</nav>",
    # pointer-events:none 워터마크(가운데 포함·전체 덮음이지만 맞지 않음)
    "watermark_pointer_none": BASE + "<div style='position:fixed;inset:0;z-index:9999;pointer-events:none;opacity:.1;background:#000'></div>",
    # 숨은 aria-modal(display:none)
    "hidden_aria_modal": BASE + "<div role=dialog aria-modal=true style='display:none'><button>x</button></div>",
}


def doc(body: str) -> str:
    return "<!doctype html><html><head><meta charset=utf-8></head><body style='margin:0'>" + body + "</body></html>"
