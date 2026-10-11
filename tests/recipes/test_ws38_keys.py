"""WS-38 단계 1: PageKey·Target 계산과 locate (Mock HTML, set_content).

설계 §1·§4-1·§7. 텍스트 유사도·좌표 재생은 쓰지 않는다 — 어긋나면 찾지 못한 것(중단)이다.
"""

from __future__ import annotations

import pytest

from recipes import keys

# ---------------------------------------------------------------------------
# URL 패턴 (순수 함수)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://News.YCombinator.com/", "news.ycombinator.com/"),
        ("https://news.ycombinator.com/item?id=4512", "news.ycombinator.com/item?id"),
        ("https://x.test/a/123/b?q=%EB%85%B8%ED%8A%B8%EB%B6%81&page=2#frag", "x.test/a/{n}/b?page&q"),
        ("https://x.test/p/3f2504e0-4f89-11d3-9a0c-0305e82c3301", "x.test/p/{id}"),
        ("https://x.test/p/abcdef0123456789XYZ", "x.test/p/{id}"),
        ("https://x.test/p/short-slug", "x.test/p/short-slug"),
        ("http://127.0.0.1:8123/list", "127.0.0.1:8123/list"),
    ],
)
def test_url_pattern(url, expected):
    assert keys.url_pattern(url) == expected


def test_origin_of():
    assert keys.origin_of("https://A.test:8443/x?y=1") == "https://a.test:8443"
    assert keys.origin_of("about:blank") == ""


# ---------------------------------------------------------------------------
# Mock HTML
# ---------------------------------------------------------------------------


def _items(rows, *, ad_first=False, cls="items"):
    lis = []
    if ad_first:
        lis.append(
            '<li class="item"><span class="rank">0.</span>'
            '<a class="title" href="https://ads.example/click?c=1">광고 상품</a> '
            '<a class="sub" href="https://ads.example/click?c=2">discuss</a></li>'
        )
    for i, (rid, title) in enumerate(rows, 1):
        lis.append(
            f'<li class="item"><span class="rank">{i}.</span>'
            f'<a class="title" href="/item?id={rid}">{title}</a> '
            f'<a class="sub" href="/item?id={rid}">discuss</a></li>'
        )
    return f'<ul class="{cls}">' + "".join(lis) + "</ul>"


ROWS = [(101, "Alpha"), (102, "Beta"), (103, "Gamma"), (104, "Delta"), (105, "Epsilon")]
NEW_ROWS = [(201, "Zeta"), (202, "Eta"), (203, "Theta"), (204, "Iota"), (205, "Kappa")]

HEADER = (
    '<header><nav><a href="/">home</a> <a href="/new">new</a> '
    '<input type="search" aria-label="검색어"><button id="go">검색</button></nav></header>'
)


def page_html(rows=ROWS, *, header=HEADER, extra="", ad_first=False, body_lists=None):
    lists = body_lists if body_lists is not None else _items(rows, ad_first=ad_first)
    return (
        "<html><head><script>var x=1;</script></head><body>"
        + header
        + f"<main><section class='feed'>{lists}</section></main>{extra}"
        + "<footer><a href='/about'>about</a></footer></body></html>"
    )


async def _page(pw, html, url="http://mock.test/list"):
    browser = await pw.chromium.launch(headless=True)
    page = await (await browser.new_context()).new_page()

    async def _route(route):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    await page.route("**/*", _route)
    await page.goto(url)
    return browser, page


async def _snap_then(html_before, html_after, css, *, pin=None, url_after="http://mock.test/list"):
    """기록(html_before 에서 css 대상) → 다른 페이지(html_after)에서 locate."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page = await _page(pw, html_before)
        snap = await keys.snapshot(page, css)
        target = keys.make_target(snap["target"], pin=pin)
        browser2, page2 = await _page(pw, html_after, url=url_after)
        found = await keys.locate(page2, target)
        clicked_text = None
        if found["ok"]:
            clicked_text = await page2.locator(found["css"]).inner_text()
        snap2 = await keys.snapshot(page2, None)
        await browser.close()
        await browser2.close()
    return snap, target, found, clicked_text, snap2


FIRST_TITLE = "ul.items > li:nth-of-type(1) > a.title"
THIRD_TITLE = "ul.items > li:nth-of-type(3) > a.title"
FIRST_DISCUSS = "ul.items > li:nth-of-type(1) > a.sub"


@pytest.mark.asyncio
async def test_slot_survives_content_replacement():
    snap, target, found, text, snap2 = await _snap_then(page_html(), page_html(NEW_ROWS), FIRST_TITLE)
    assert target["kind"] == "slot"
    assert target["ordinal"] == 0
    assert found["ok"], found
    assert text == "Zeta"  # 새 기사(같은 자리)
    # 기사가 바뀌어도 골격은 같다(텍스트·속성값 제외)
    assert snap["skel"] == snap2["skel"]


@pytest.mark.asyncio
async def test_slot_follows_ordinal_after_reorder():
    reordered = [ROWS[4], ROWS[3], ROWS[0], ROWS[1], ROWS[2]]
    _, target, found, text, _ = await _snap_then(page_html(), page_html(reordered), THIRD_TITLE)
    assert target["ordinal"] == 2
    assert found["ok"], found
    assert text == "Alpha"  # 이름(Gamma)이 아니라 순번(3번째)대로


@pytest.mark.asyncio
async def test_skeleton_folds_list_count_and_ignores_hidden_script():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b1, p1 = await _page(pw, page_html(ROWS))
        b2, p2 = await _page(pw, page_html(ROWS[:4], extra="<div style='display:none'><p>x</p></div><script>1</script>"))
        b3, p3 = await _page(pw, page_html(ROWS, header="<div class='ab'><span>B 화면</span></div>"))
        s1 = await keys.snapshot(p1, None)
        s2 = await keys.snapshot(p2, None)
        s3 = await keys.snapshot(p3, None)
        for b in (b1, b2, b3):
            await b.close()
    assert len(s1["skel"]) == 12
    assert s1["skel"] == s2["skel"]  # 5개 → 4개(접힘), 숨김·script 제외
    assert s1["skel"] != s3["skel"]  # A/B 변형(머리 구조가 다름)


@pytest.mark.asyncio
async def test_ready_count_reflects_partial_load():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b1, p1 = await _page(pw, page_html(ROWS))
        b2, p2 = await _page(pw, "<html><body><main><section class='feed'>" + _items(ROWS[:1]) + "</section></main></body></html>")
        full, partial = await keys.snapshot(p1, None), await keys.snapshot(p2, None)
        await b1.close()
        await b2.close()
    assert full["ready"] >= 14
    assert partial["ready"] * 2 < full["ready"]


@pytest.mark.asyncio
async def test_two_same_template_lists_are_ambiguous():
    twin = _items(ROWS) + _items(NEW_ROWS)
    _, target, found, text, _ = await _snap_then(page_html(), page_html(body_lists=twin), FIRST_TITLE)
    assert not found["ok"]
    assert found["reason"] == "target_ambiguous"
    assert text is None


@pytest.mark.asyncio
async def test_ad_swapped_into_slot_is_refused_by_template():
    """같은 자리에 같은 모양의 광고가 들어오면(href 패턴 불일치) 누르지 않는다."""
    _, target, found, text, _ = await _snap_then(page_html(), page_html(ad_first=True), FIRST_TITLE)
    assert not found["ok"]
    assert found["reason"] == "target_not_found"
    assert "href" in found["detail"]
    assert text is None


@pytest.mark.asyncio
async def test_hn_discuss_slot_ui_identity():
    """HN discuss: 이름·CSS 틀이 같고 id 만 다른 링크들."""
    reordered = [ROWS[2], ROWS[0], ROWS[1], ROWS[3], ROWS[4]]
    # slot: 같은 자리(1번째 항목)의 discuss — 순서가 바뀌면 그 자리의 새 항목 것
    _, t_slot, f_slot, _, _ = await _snap_then(page_html(), page_html(reordered), FIRST_DISCUSS)
    assert t_slot["kind"] == "slot" and f_slot["ok"]
    assert f_slot["href"].endswith("/item?id=103")
    # ui: 같은 이름이 여럿 → 모호(멈춤), 이름 유사도로 고르지 않는다
    _, t_ui, f_ui, _, _ = await _snap_then(page_html(), page_html(reordered), FIRST_DISCUSS, pin="ui")
    assert t_ui["kind"] == "ui"
    assert not f_ui["ok"] and f_ui["reason"] == "target_ambiguous"
    # identity: 바로 그 기사(id=101)의 discuss — 자리가 바뀌어도 그 기사
    _, t_id, f_id, _, _ = await _snap_then(page_html(), page_html(reordered), FIRST_DISCUSS, pin="identity")
    assert t_id["kind"] == "identity"
    assert f_id["ok"], f_id
    assert f_id["href"].endswith("/item?id=101")
    # identity: 그 기사가 사라지면 찾지 못함(다른 기사로 바꾸지 않는다)
    _, _, f_gone, _, _ = await _snap_then(page_html(), page_html(NEW_ROWS), FIRST_DISCUSS, pin="identity")
    assert not f_gone["ok"] and f_gone["reason"] == "target_not_found"


@pytest.mark.asyncio
async def test_identity_refused_for_ad_price_rank():
    from playwright.async_api import async_playwright

    html = page_html(extra=(
        "<div class='sponsored-box'><a id='promo1' href='/p?pcode=9'>특가 상품</a></div>"
        "<a id='price1' href='/p?pcode=7'>12,900원</a>"
        "<a id='rank1' href='/p?pcode=8'>1위</a>"
    ))
    async with async_playwright() as pw:
        b, p = await _page(pw, html)
        ad = await keys.snapshot(p, "#promo1")
        price = await keys.snapshot(p, "#price1")
        rank = await keys.snapshot(p, "#rank1")
        ok = await keys.snapshot(p, FIRST_DISCUSS)
        await b.close()
    for s in (ad, price, rank):
        assert s["target"]["identity"] is None
        assert s["target"]["identity_refused"]
        with pytest.raises(keys.TargetError):
            keys.make_target(s["target"], pin="identity")
    assert ok["target"]["identity"] is not None


def _ui_page(top: int) -> str:
    return (
        "<html><body style='margin:0'>"
        f"<div style='position:absolute;top:{top}px;left:20px'>"
        "<form role='search'><input type='search' aria-label='검색어'>"
        "<button id='go'>검색</button></form></div></body></html>"
    )


@pytest.mark.asyncio
async def test_ui_target_450px_guard():
    _, target, near, text, _ = await _snap_then(_ui_page(10), _ui_page(300), "#go")
    assert target["kind"] == "ui"
    assert near["ok"] and text == "검색"
    _, _, far, text_far, _ = await _snap_then(_ui_page(10), _ui_page(900), "#go")
    assert not far["ok"] and far["reason"] == "target_not_found"
    assert "450" in far["detail"]
    assert text_far is None


@pytest.mark.asyncio
async def test_ui_name_must_match_exactly_no_similarity():
    """해외여행 → 해외여행자보험 같은 비슷한 이름은 고르지 않는다."""
    before = "<html><body><nav><a id='t' href='/travel'>해외여행</a></nav></body></html>"
    after = "<html><body><nav><a id='t' href='/travel'>해외여행자보험</a></nav></body></html>"
    _, target, found, text, _ = await _snap_then(before, after, "#t")
    assert target["kind"] == "ui"
    assert not found["ok"] and text is None


@pytest.mark.asyncio
async def test_secret_field_flag_and_locate_roundtrip():
    from playwright.async_api import async_playwright

    html = ("<html><body><form><input id='u' aria-label='아이디'>"
            "<input id='p' type='password' aria-label='비밀번호'></form></body></html>")
    async with async_playwright() as pw:
        b, p = await _page(pw, html)
        user = await keys.snapshot(p, "#u")
        pw_snap = await keys.snapshot(p, "#p")
        found = await keys.locate(p, keys.make_target(user["target"]))
        await b.close()
    assert user["target"]["secret"] is False
    assert pw_snap["target"]["secret"] is True
    assert found["ok"] and found["role"] == "textbox" and found["name"] == "아이디"
