"""take_screenshot(annotate_som=True) 디스패치 테스트 (Stage 4 Task 4).

게이트 `DispatchContext.som_enabled`:
* False(기본) — 레거시 클라이언트 보호(PRD §8-2). 여전히 E_FEATURE_NOT_IMPLEMENTED.
  `harness/actions_test.py:204`, `harness/mcp_smoke.py:167`이 이 값에 의존한다.
* True — 실제 SoM PNG와 태그 목록을 반환한다.
"""

from __future__ import annotations

import base64

import pytest

from contracts import ActionType, ErrorCode, thresholds


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Chromium 바이너리 없음"
)


@pytest.fixture(scope="module")
def mock_server():
    from harness import MockServer

    with MockServer() as srv:
        yield srv


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={
                "width": thresholds.VIEWPORT_WIDTH,
                "height": thresholds.VIEWPORT_HEIGHT,
            }
        )
        pg = await ctx.new_page()
        try:
            yield pg
        finally:
            await browser.close()


def _dispatcher(page, som_enabled: bool):
    from perception import PerceptionEngine

    from actions import ActionDispatcher, DispatchContext

    engine = PerceptionEngine()
    ctx = DispatchContext(page=page, engine=engine, som_enabled=som_enabled)
    return ActionDispatcher(ctx), engine


def test_dispatch_context_som_gate_defaults_off():
    from actions import DispatchContext

    ctx = DispatchContext(page=object(), engine=object())
    assert ctx.som_enabled is False


@requires_chromium
async def test_gate_off_keeps_feature_not_implemented(mock_server, page):
    await page.goto(mock_server.site_url("icon-buttons"))
    dispatcher, _ = _dispatcher(page, som_enabled=False)
    result = await dispatcher.dispatch(ActionType.TAKE_SCREENSHOT, {"annotate_som": True})
    assert result.success is False
    assert result.error_code is ErrorCode.FEATURE_NOT_IMPLEMENTED


@requires_chromium
async def test_gate_off_plain_screenshot_still_works(mock_server, page):
    await page.goto(mock_server.site_url("icon-buttons"))
    dispatcher, _ = _dispatcher(page, som_enabled=False)
    result = await dispatcher.dispatch(ActionType.TAKE_SCREENSHOT, {})
    assert result.success is True
    assert result.data["bytes"] > 0
    assert "som_tags" not in result.data


@requires_chromium
async def test_gate_on_returns_som_payload(mock_server, page):
    await page.goto(mock_server.site_url("icon-buttons"))
    dispatcher, engine = _dispatcher(page, som_enabled=True)
    epoch_before = engine.epoch

    result = await dispatcher.dispatch(ActionType.TAKE_SCREENSHOT, {"annotate_som": True})

    assert result.success is True, result.error_message
    data = result.data
    assert data["image_tokens"] == thresholds.SOM_IMAGE_TOKENS_PER_CAPTURE == 1600
    assert data["candidate_count"] == 5
    assert len(data["som_tags"]) == 5
    for entry in data["som_tags"]:
        assert set(entry) == {"tag", "role", "name", "bbox", "selector_path"}
        assert set(entry["bbox"]) == {"x", "y", "width", "height"}
    assert [e["tag"] for e in data["som_tags"]] == ["A1", "A2", "A3", "A4", "A5"]

    png = base64.b64decode(data["image_b64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert data["bytes"] == len(png)

    # 오버레이는 제거됐고 에포크는 그대로다.
    assert await page.evaluate("document.querySelectorAll('[data-som-tag]').length") == 0
    assert engine.epoch == epoch_before
    assert result.reobserve_required is False


@requires_chromium
async def test_gate_on_canvas_page_returns_empty_tags_with_screenshot(mock_server, page):
    """후보 0개(순수 Canvas)여도 성공 — 호출자가 좌표 모드로 전환하는 신호다."""
    await page.goto(mock_server.site_url("canvas-ui"))
    dispatcher, _ = _dispatcher(page, som_enabled=True)

    result = await dispatcher.dispatch(ActionType.TAKE_SCREENSHOT, {"annotate_som": True})

    assert result.success is True, result.error_message
    assert result.data["som_tags"] == []
    assert result.data["candidate_count"] == 0
    assert result.data["image_tokens"] == thresholds.SOM_IMAGE_TOKENS_PER_CAPTURE
    png = base64.b64decode(result.data["image_b64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


@requires_chromium
async def test_gate_on_screenshot_failure_maps_to_error_code(mock_server, page, monkeypatch):
    await page.goto(mock_server.site_url("icon-buttons"))
    dispatcher, _ = _dispatcher(page, som_enabled=True)

    async def _boom(**_kw):
        raise RuntimeError("capture failed")

    monkeypatch.setattr(page, "screenshot", _boom)
    result = await dispatcher.dispatch(ActionType.TAKE_SCREENSHOT, {"annotate_som": True})
    assert result.success is False
    assert result.error_code is ErrorCode.SCREENSHOT_FAILED
    assert result.retry_safe is True
