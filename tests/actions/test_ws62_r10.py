"""WS-62 R10: 진짜 합집합 — 같은 경로라도 기준(v1.16.0) 전체 지문 고정을 버리지 않는다(사용자 결정 A 안전 우선, #136).

R9 재검증 BLOCKING(R1): 상품 article×4 안 옵션 section 이 셋 이상(안쪽 형성 쌍둥이)이고 상품명(span·strong·dd)이 첫 section 에만
있으면, 안쪽 고정은 대칭 자리(H3·SMALL<P)만 비교하고, 기준 고정(같은 section 경로·상품명 포함 지문)은 '같은 (대상, 경로)가 이미
있다' 는 이유로 건너뛰었다. section ≥3 이면 바깥(article) 지문은 반복 항목 제외로 상품명을 못 본다 → 상품 A→Z 뒤 옛 '담기' 가
Z 를 눌렀다(기준은 거부, 4변형·21대상).

결정: 기준 고정은 기준 알고리즘이 만든 지문 그대로 항상 보존 — 경로가 같아도 지문이 다르면 둘 다 보관·둘 다 비교. 기준 고정에
적용하는 변경은 내비게이션 예외(사이 반복 행 없음)와 바깥 가림(안쪽이 대칭 자리로 실제 비교하는 글자만)뿐. 대가: 같은 경로 안
비대칭 롤링은 기준처럼 거부(#154).

기준 비교는 기준 런타임(`baseline_runtime` — tests/fixtures/ws62/repeated_v1160.js 원문)으로 같은 흐름을 다시 돌려 잰다.
모든 시험은 로컬 Chromium set_content(요청 차단).
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Dict, List, Tuple

import pytest

from contracts import ActionType, ErrorCode

from test_scroll_navigation import requires_chromium  # noqa: F401
from test_ws62_r4_hanatour import _observe
from test_ws62_r9 import baseline_runtime, install_identity_census_all

# ------------------------------------------------------------------ 생성기(검증 증거 new_siblings.py 일반화)

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
#: R9 재검증 BLOCKING 4변형(기준은 첫 section 담기 거부, R9 는 Z 클릭).
R9_REGRESSIONS = ("three_buried", "four_buried", "table_three_named", "deep_partial_named")
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
#: 남은 기준 대비 회귀(조정자 결정 A — 하나투어 흐름 거부 0 우선): 상품 신원이 **다른 버튼 안 조각 글자**('담기<span>A</span>')에만
#: 있으면, 바깥 기준 고정은 안쪽 쌍둥이(section) 항목 낱말을 가리고 안쪽 고정은 첫 텍스트 노드('담기')만 비교한다 — 상품 바깥
#: '정보/배송/문의' 3대상이 Z 를 누른다. 가림 안 낱말을 기준처럼 textContent 로 비교하면 달력 링크 '4<p>101만최저가</p>' 가격과
#: 구조가 같아 하나투어 CSS 흐름이 거부된다.
KNOWN_REGRESSIONS = {"btnsplit-first-span-n3", "btnsplit-middle-strong-n4-T"}


def _corpus_params() -> List[Any]:
    return [pytest.param(c, id=c["id"], marks=pytest.mark.xfail(
        strict=True, reason="기준 대비 회귀 — 버튼 안 조각 글자에만 신원이 있는 구조(#156)"))
        if c["id"] in KNOWN_REGRESSIONS else pytest.param(c, id=c["id"]) for c in CORPUS]

SINK = ("() => { window.sink = []; document.querySelectorAll('button').forEach(b => b.onclick = () => {"
        " const a = b.closest('.product'); window.sink.push(a.dataset.sku);"
        " document.querySelector('#out').textContent = a.dataset.sku + '/' + window.sink.length; }); }")
TO_Z = ("() => { const a = document.querySelector('.product'); a.dataset.sku = 'Z';"
        " if (a.hasAttribute('aria-label')) a.setAttribute('aria-label', '상품 Z');"
        " a.querySelectorAll('.identity').forEach(n => n.textContent = '여행 상품 Z'); }")


async def _flow(browser, html: str) -> Dict[str, Any]:
    """상품 A 의 '.target'·'.quick' 대상 관찰 → A→Z → 옛 id 로 전부 누르고, 바뀌지 않은 B 를 누른다."""
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = await ctx.new_page()
        await ctx.route("**/*", lambda route: route.abort())
        await page.set_content(html)
        engine = PerceptionEngine()
        d = ActionDispatcher(DispatchContext(page=page, engine=engine))
        await page.evaluate(SINK)
        await _observe(d, {"force_full_tree": True, "prune_top_n": 1000})
        targets, b = [], None
        for h in engine.handles.values():
            if not h.css_path or h.is_shadow:
                continue
            sku = await page.evaluate("(css) => { const e = document.querySelector(css), a = e && e.closest('.product');"
                                      " return a && e.matches('.target,.quick') ? a.dataset.sku : null; }", h.css_path)
            if sku == "A":
                targets.append(h)
            if sku == "B" and b is None:
                b = h
        assert targets and b is not None
        from perception.repeated import handle_pins

        npins = max(len(handle_pins(h)) for h in targets)
        await page.evaluate(TO_Z)
        res = [await d.dispatch(ActionType.CLICK, {"element_id": h.element_id}) for h in targets]
        sink = await page.evaluate("window.sink")
        good = await d.dispatch(ActionType.CLICK, {"element_id": b.element_id})
        ctrl = await page.evaluate("window.sink")
    finally:
        await ctx.close()
    return {"css": [h.css_path for h in targets], "refused": [r.error_code is ErrorCode.TOCTOU_MISMATCH for r in res],
            "sink": sink, "b_ok": bool(good.success) and ctrl[-1:] == ["B"], "max_pins": npins}


async def _both(html: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            now = await _flow(browser, html)
            with baseline_runtime():
                base = await _flow(browser, html)
        finally:
            await browser.close()
    return now, base


def _lost(now: Dict[str, Any], base: Dict[str, Any]) -> List[str]:
    """기준이 거부한 (대상, 변화)를 지금 통과시킨 대상."""
    assert now["css"] == base["css"]
    return [c for c, rb, rn in zip(now["css"], base["refused"], now["refused"]) if rb and not rn]


def _assert_not_worse(now: Dict[str, Any], base: Dict[str, Any]) -> None:
    lost = _lost(now, base)
    assert not lost, f"기준이 거부한 옛 대상을 눌렀다: {lost} sink={now['sink']}"
    assert now["sink"].count("Z") <= base["sink"].count("Z"), (now["sink"], base["sink"])
    assert now["b_ok"] and base["b_ok"], (now, base)


# ------------------------------------------------------------------ 검증 증거 run_new.py 18변형(그대로 편입)


@requires_chromium
@pytest.mark.parametrize("name", [c[0] for c in NEW_CASES])
async def test_new_siblings_not_worse_than_baseline(name):
    """RED(R9 e688748): three_buried·four_buried·table_three_named(각 3)·deep_partial_named(12) — 기준은 거부, R9 는 Z 클릭.

    18변형 모두 기준 런타임과 같은 흐름 — 기준 대비 오클릭 0, 바뀌지 않은 B 정상.
    """
    now, base = await _both(new_html(name))
    _assert_not_worse(now, base)
    if name in R9_REGRESSIONS:
        assert any(base["refused"]), base  # 측정 조건(규칙 4): 기준이 실제로 거부하던 대상이 있다


# ------------------------------------------------------------------ 매개변수 corpus 60조합 — 실제 기준 archive JS 와 같은 흐름


def test_corpus_covers_required_axes():
    """corpus 가 요구 축을 실제로 덮는다(규칙 1·4): 60조합 이상, 쌍둥이 2·3·4, 3중 중첩, 표 안 카드, 표 칸, 부분 공통·전부 공통,
    이름 위치 4 × 태그 5 전부, 루트 이름 유·무."""
    assert len(CORPUS) >= 60 + len(WRAPS) * len(NAME_POS)
    assert {c.get("wrap", "") for c in CORPUS} == {""} | set(WRAPS)
    assert {c["n"] for c in CORPUS} == {2, 3, 4}
    assert {(c["pos"], c["tag"]) for c in CORPUS} == {(p, t) for p in NAME_POS for t in NAME_TAGS}
    for key in ("table", "inside", "partial", "named"):
        assert {c[key] for c in CORPUS} == {True, False}, key
    assert {c["levels"] for c in CORPUS} == {1, 2}
    assert any(c["levels"] == 2 and c["n"] == 3 for c in CORPUS) and any(c["table"] and c["levels"] == 2 for c in CORPUS)
    assert len({c["id"] for c in CORPUS}) == len(CORPUS)
    # 18변형 생성기 동치: 검증 증거와 같은 HTML(이름 첫 section·부분 공통).
    import hashlib

    assert {c[0]: hashlib.sha256(new_html(c[0]).encode()).hexdigest()[:16] for c in NEW_CASES} == NEW_SHA


#: 검증 증거 new_ab-ws62r4.json 의 html_sha256 앞 16자(같은 생성기 원형 — 일반화가 18변형 HTML 을 바꾸지 않았다).
NEW_SHA = {
    "three_buried": "60252498a5479675",
    "four_buried": "2ee754ac42937c43",
    "three_root_named": "5be4a8a50813d714",
    "four_root_named": "630dc37e14f58cba",
    "partial2_span": "26715aec50de50ed",
    "partial2_dd": "c3341bef4864b9d6",
    "partial3_named": "f50fc9e200577f99",
    "triple_span": "f1e452e416f41dab",
    "triple_strong": "4e6540f602c8d6ee",
    "triple_dd": "86bae7c5797a4b2e",
    "triple_three": "48c79bc7b2529b73",
    "card_in_table_span": "a26d463f47556200",
    "card_in_table_dd": "e844ee24a6802071",
    "triple_card_table": "da32c8330f5b19af",
    "inner_table_span": "0b4e3ebfd3431203",
    "inner_table_triple": "f59f51382fdf4f38",
    "table_three_named": "36de8e0ca7bfd89c",
    "deep_partial_named": "170af1e920381d46",
}


@requires_chromium
@pytest.mark.parametrize("case", _corpus_params())
async def test_corpus_not_worse_than_baseline(case):
    """기준이 거부하던 (대상, 상품 A→Z)를 R10 이 하나라도 통과시키면 실패. 다른 상품 클릭 ≤ 기준, B 정상."""
    html = corpus_html(case["n"], case["levels"], case["tag"], case["table"], case["inside"], case["named"],
                       case["pos"], case["partial"], case.get("wrap", ""))
    now, base = await _both(html)
    _assert_not_worse(now, base)
    print(f"\nWS62R10-CORPUS {case['id']} targets={len(now['css'])} base_refused={sum(base['refused'])}"
          f" now_refused={sum(now['refused'])} base_Z={base['sink'].count('Z')} now_Z={now['sink'].count('Z')}"
          f" max_pins={now['max_pins']}")


# ------------------------------------------------------------------ 주입: '같은 경로 건너뛰기' 되살리기

#: 기준 고정 루프의 내비게이션 예외 줄(주입 기준점).
NAV_LINE = "    if (!rowBetween(t, g)) continue;  // 내비게이션 예외(사이 반복 행 없음)\n"
#: R9 까지의 결함: 같은 (대상, 경로)가 위(R6) 고정에 이미 있으면 기준 고정을 건너뛴다.
SAME_PATH_SKIP = (
    "    if (pins.some(function (p) { return p.i === t.i && p.path === path; }) || sets.some(function (st) {"
    " return st.groups.some(function (gg) { return (gg.path === path && gg.members.some(function (m) { return m.i === t.i; }))"
    " || gg.folded.some(function (f) { return f.i === t.i && f.path === path; }); }); })) continue;\n")


#: 기준 고정 지문의 동적 제외 끄기(R10 raw) — 주입은 이것을 무력화한다(기준 고정에도 live 영역·텍스트 노드 경계 제외 적용).
RAW_ON = ("    rpRaw = !!(opt && opt.raw);", "    rpRaw = true;\n    try { return basePinsRaw(); }")
RAW_OFF = ("    rpRaw = false;", "    rpRaw = false;\n    try { return basePinsRaw(); }")


@contextlib.contextmanager
def raw_off():
    """제품 JS(관찰·실행 직전 확인·치유 거르기) 안 기준 고정 raw 를 끈다(사보타주)."""
    import actions.verification as ver
    import perception.repeated as rp

    mp = pytest.MonkeyPatch()
    try:
        for mod, name in ((rp, "REPEATED_JS"), (rp, "GROUP_CHECK_JS"), (rp, "GROUP_FILTER_JS"),
                          (ver, "STALENESS_CHECK_SCRIPT")):
            src = getattr(mod, name)
            assert src.count(RAW_ON[0]) == 1, name
            src = src.replace(RAW_ON[0], RAW_OFF[0])
            if name == "REPEATED_JS":
                assert src.count(RAW_ON[1]) == 1
                src = src.replace(RAW_ON[1], RAW_OFF[1])
            mp.setattr(mod, name, src)
        yield
    finally:
        mp.undo()


@requires_chromium
async def test_injected_raw_off_misclicks_live_regions():
    """주입: 기준 고정에도 동적 제외(live 영역)를 쓰면 상품명이 aria-live·status·timer 안인 조합에서 기준 대비 오클릭이 난다."""
    hit = []
    with raw_off():
        for case in [c for c in CORPUS if c.get("wrap") in ("live", "status", "timer")][:6]:
            now, base = await _both(corpus_html(case["n"], case["levels"], case["tag"], case["table"], case["inside"],
                                                case["named"], case["pos"], case["partial"], case["wrap"]))
            hit.append(len(_lost(now, base)))
    assert sum(1 for x in hit if x) >= 4, hit


@contextlib.contextmanager
def same_path_skip():
    """제품 관찰 JS 에 '같은 경로 건너뛰기' 를 되살린다(사보타주)."""
    import perception.repeated as rp

    mp = pytest.MonkeyPatch()
    try:
        assert rp.REPEATED_JS.count(NAV_LINE) == 1
        mp.setattr(rp, "REPEATED_JS", rp.REPEATED_JS.replace(NAV_LINE, NAV_LINE + SAME_PATH_SKIP))
        yield
    finally:
        mp.undo()


@requires_chromium
async def test_injected_same_path_skip_misclicks():
    """주입: 같은 경로 건너뛰기를 되살리면 18변형 중 R9 회귀 4변형 모두, 그리고 corpus 에서 기준 대비 오클릭이 난다."""
    lost = {}
    with same_path_skip():
        for name in R9_REGRESSIONS:
            now, base = await _both(new_html(name))
            lost[name] = len(_lost(now, base))
        hit = 0
        for case in CORPUS[:15]:
            now, base = await _both(corpus_html(case["n"], case["levels"], case["tag"], case["table"], case["inside"],
                                                case["named"], case["pos"], case["partial"], case.get("wrap", "")))
            hit += bool(_lost(now, base))
    assert all(v >= 3 for v in lost.values()), lost
    assert hit >= 1, hit


# ------------------------------------------------------------------ census: 같은 경로 다른 지문 포함, 기준 지문 글자 하나씩


CENSUS_NEW = list(R9_REGRESSIONS) + ["triple_three", "partial3_named", "inner_table_triple"]


async def _census(name: str, monkeypatch) -> Tuple[List[Dict[str, Any]], int]:
    """기준 고정 (대상, 쌍둥이)마다 기준 지문의 모든 정적 글자를 하나씩 바꿔 그 대상의 다중 고정이 거부하는지(R9 census) +
    '같은 경로에 지문이 다른 고정 둘 이상' 인 핸들 수."""
    from playwright.async_api import async_playwright

    from perception.repeated import handle_pins
    from test_ws62_r4_hanatour import _open

    log = install_identity_census_all(monkeypatch)
    async with async_playwright() as pw:
        browser, page, engine, d = await _open(pw, new_html(name))
        try:
            await page.context.route("**/*", lambda r: r.abort())
            await _observe(d, {"force_full_tree": True, "prune_top_n": 1000})
            same_path = 0
            for h in engine.handles.values():
                paths = [p.path for p in handle_pins(h)]
                same_path += len(paths) != len(set(paths))
        finally:
            await browser.close()
    return log, same_path


@requires_chromium
@pytest.mark.parametrize("name", CENSUS_NEW)
async def test_identity_census_new_corpus(name, monkeypatch):
    """RED(R9): three_buried 등에서 첫 section 상품명(기준 지문 글자)을 바꿔도 그 대상의 어떤 고정도 거부하지 않았다."""
    log, _ = await _census(name, monkeypatch)
    assert log and any(e["pairs"] for e in log), log  # 측정 조건(규칙 4)
    assert not any(e["bad"] for e in log), json.dumps(log, ensure_ascii=False)[:1500]


@requires_chromium
async def test_census_measures_same_path_different_fingerprint(monkeypatch):
    """측정 조건(규칙 4): census 대상에 '같은 경로·다른 지문' 고정 쌍(안쪽 대칭 자리 지문 + 기준 전체 지문)이 실제로 있다."""
    _, same = await _census("three_buried", monkeypatch)
    assert same >= 3, same


@requires_chromium
@pytest.mark.parametrize("name", R9_REGRESSIONS)
async def test_identity_census_detects_same_path_skip(name, monkeypatch):
    """주입: 같은 경로 건너뛰기를 되살리면 census 가 누락을 잡는다."""
    with same_path_skip():
        log, _ = await _census(name, monkeypatch)
    assert sum(e["bad"] for e in log) >= 1, json.dumps(log, ensure_ascii=False)[:1500]


# ------------------------------------------------------------------ R7 에서 옮김: 같은 경로 안 live 영역(대칭) — 거부 ≤ 기준(#154)


@requires_chromium
@pytest.mark.parametrize("kind", ["live", "timer", "status", "marquee"])
async def test_live_region_refusals_at_most_baseline(kind):
    """R7 test_live_region_text_excluded_from_identity(거부 0 기대)를 옮김: 두 달 패널 모두에 있는 aria-live·role=timer/status/
    marquee 글자가 갱신되면, 같은 패널 경로의 기준 고정(R10 — 동적 제외 없음)이 비교하므로 기준처럼 재관찰을 요구할 수 있다.
    거부 여부는 기준 런타임 이하, 월 표제가 바뀐 달의 옛 날짜는 여전히 거부, 다른 달을 누르지 않는다."""
    from playwright.async_api import async_playwright

    from test_ws62_r1 import _by_selector
    from test_ws62_r4_hanatour import _open
    from test_ws62_r7 import live_panels_html

    async def run(pw) -> Dict[str, Any]:
        browser, page, engine, d = await _open(pw, live_panels_html(kind))
        try:
            await _observe(d, {"prune_top_n": 300})
            one = await _by_selector(engine, page, "#cal > .inr:nth-of-type(1) li:nth-child(3) a")
            two = await _by_selector(engine, page, "#cal > .inr:nth-of-type(1) li:nth-child(4) a")
            await page.evaluate("document.querySelectorAll('.ticker').forEach((e, k) => e.textContent = '소식 ' + (k + 7))")
            ok = await d.dispatch(ActionType.CLICK, {"element_id": one})
            out = await page.text_content("#out")
            await page.evaluate("document.querySelector('#cal .inr span').textContent = '2027년 3월'")
            bad = await d.dispatch(ActionType.CLICK, {"element_id": two})
            out2 = await page.text_content("#out")
        finally:
            await browser.close()
        return {"refused": ok.error_code is ErrorCode.TOCTOU_MISMATCH, "ok": bool(ok.success), "out": out,
                "bad": bad.error_code is ErrorCode.TOCTOU_MISMATCH, "out2": out2}

    async with async_playwright() as pw:
        now = await run(pw)
        with baseline_runtime():
            base = await run(pw)
    assert now["refused"] <= base["refused"], (now, base)
    assert now["ok"] or now["refused"], now
    assert now["out"] in ("none", "2026년 12월/3"), now  # 다른 달을 누르지 않는다
    assert now["bad"] and now["out2"] == now["out"], now
    print(f"\nWS62R10-LIVE {kind} base_refused={base['refused']} now_refused={now['refused']}")
