"""SoM 오버레이 렌더러 테스트 (Stage 4 Task 3).

라벨 주입 → PNG 캡처 → 원상복구가 한 호출 안에서 끝나야 하며,
오버레이는 상호작용 요소가 아니므로 인지 엔진 에포크를 올리지 않는다.
"""

from __future__ import annotations

from contracts import thresholds

from _browser import requires_chromium

_HTML = """
<button id="a" style="width:60px;height:30px">하나</button>
<button id="b" style="width:60px;height:30px">둘</button>
<a href="/x" id="c" style="display:inline-block;width:40px;height:20px"></a>
"""

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@requires_chromium
async def test_render_som_returns_png_and_restores_dom(page):
    from vision import collect_candidates, render_som

    await page.set_content(_HTML)
    cands = await collect_candidates(page)
    assert len(cands) == 3

    png = await render_som(page, cands)

    assert png[:8] == PNG_MAGIC
    assert len(png) > 1000
    assert await page.evaluate("document.querySelectorAll('[data-som-tag]').length") == 0
    assert page.viewport_size == {
        "width": thresholds.VIEWPORT_WIDTH,
        "height": thresholds.VIEWPORT_HEIGHT,
    }


@requires_chromium
async def test_render_som_forces_contract_viewport(page):
    from vision import collect_candidates, render_som

    await page.set_viewport_size({"width": 800, "height": 600})
    await page.set_content(_HTML)
    cands = await collect_candidates(page)
    png = await render_som(page, cands)
    assert png[:8] == PNG_MAGIC
    assert page.viewport_size == {"width": 1280, "height": 720}


@requires_chromium
async def test_render_som_labels_are_visible_during_capture(page):
    """주입된 라벨이 실제로 캡처에 포함됐는지: 라벨 없는 캡처와 바이트가 달라야 한다."""
    from vision import collect_candidates, render_som

    await page.set_content(_HTML)
    cands = await collect_candidates(page)
    plain = await page.screenshot(type="png")
    som = await render_som(page, cands)
    assert som != plain


@requires_chromium
async def test_render_som_with_no_candidates_is_plain_screenshot(page):
    from vision import render_som

    await page.set_content('<canvas width="600" height="300"></canvas>')
    png = await render_som(page, [])
    assert png[:8] == PNG_MAGIC
    assert await page.evaluate("document.querySelectorAll('[data-som-tag]').length") == 0


@requires_chromium
async def test_render_som_does_not_bump_engine_epoch(page):
    from perception import PerceptionEngine
    from vision import collect_candidates, render_som

    engine = PerceptionEngine()
    await page.set_content(_HTML)
    await engine.observe_page(page=page)
    before = engine.epoch
    await render_som(page, await collect_candidates(page))
    assert engine.epoch == before


@requires_chromium
async def test_render_som_cleans_up_even_if_screenshot_fails(page, monkeypatch):
    from vision import collect_candidates, render_som

    await page.set_content(_HTML)
    cands = await collect_candidates(page)

    async def _boom(**_kw):
        raise RuntimeError("screenshot failed")

    monkeypatch.setattr(page, "screenshot", _boom)
    try:
        await render_som(page, cands)
    except RuntimeError:
        pass
    else:
        raise AssertionError("예외가 전파돼야 한다")
    assert await page.evaluate("document.querySelectorAll('[data-som-tag]').length") == 0
