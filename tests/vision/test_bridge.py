"""태그 -> element_id 브리지 테스트 (Stage 4 Task 6).

VLM이 고른 태그를 기존 액션 경로(`@sN` 핸들 + dispatcher)로 실행한다.
"""

from __future__ import annotations

from contracts import ActionType
from perception import PerceptionEngine
from perception.engine import ElementHandle
from vision import collect_candidates
from vision.bridge import bind_tag

from _browser import requires_chromium


def test_register_external_handle_is_cleared_by_bump_epoch():
    engine = PerceptionEngine()
    handle = ElementHandle(
        element_id="@s1", epoch=engine.epoch, role="button", name="", css_path="#x", is_shadow=False
    )
    engine.register_external_handle("@s1", handle)
    assert engine.get_handle("@s1") is handle
    engine.bump_epoch("test")
    assert engine.get_handle("@s1") is None


@requires_chromium
async def test_bind_tag_then_dispatch_click(mock_server, page):
    from actions import ActionDispatcher, DispatchContext

    await page.goto(mock_server.site_url("icon-buttons"))
    engine = PerceptionEngine()
    dispatcher = ActionDispatcher(DispatchContext(page=page, engine=engine, som_enabled=True))

    candidates = await collect_candidates(page)
    third = next(c for c in candidates if c.selector_path.endswith("#ic3") or "ic3" in c.selector_path)

    eid = await bind_tag(engine, page, third)
    assert eid == "@s1"
    handle = engine.get_handle("@s1")
    assert handle is not None
    assert handle.epoch == engine.epoch
    assert handle.css_path == third.selector_path
    assert handle.role == third.role

    # 같은 에포크에서 두 번째 바인딩은 @s2
    assert await bind_tag(engine, page, candidates[0]) == "@s2"

    result = await dispatcher.dispatch(
        ActionType.CLICK, {"element_id": "@s1", "epoch": engine.epoch}
    )
    assert result.success is True, result.error_message
    assert await page.evaluate("document.body.getAttribute('data-result')") == "ok"

    engine.bump_epoch("test")
    assert engine.get_handle("@s1") is None
    # 새 에포크에서는 카운터가 1부터 다시 시작한다
    assert await bind_tag(engine, page, third) == "@s1"


@requires_chromium
async def test_bind_tag_missing_element_raises(mock_server, page):
    from vision import SomCandidate
    from contracts import BBox

    await page.goto(mock_server.site_url("icon-buttons"))
    engine = PerceptionEngine()
    ghost = SomCandidate(
        tag="A1", selector_path="#does-not-exist", bbox=BBox(x=0, y=0, width=1, height=1), role="button", name=""
    )
    try:
        await bind_tag(engine, page, ghost)
    except LookupError:
        pass
    else:
        raise AssertionError("존재하지 않는 셀렉터는 LookupError여야 한다")
