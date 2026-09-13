"""SoM 태그 후보 수집기 테스트 (Stage 4 Task 1).

뷰포트 내 가시·상호작용 가능 요소를 태그 순서대로 수집한다.
Top-20 프루닝 이전 집합이므로 인지 엔진의 스코어러와 무관하다.
"""

from __future__ import annotations

import pytest

from contracts import thresholds
from contracts.models import BBox

from _browser import requires_chromium


# ---------------------------------------------------------------------------
# 순수 단위 (브라우저 불필요)
# ---------------------------------------------------------------------------


def test_tag_sequence_is_letter_plus_digit():
    from vision.candidates import MAX_CANDIDATES, make_tag

    tags = [make_tag(i) for i in range(MAX_CANDIDATES)]
    assert tags[:10] == ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "B1"]
    assert len(set(tags)) == MAX_CANDIDATES
    for t in tags:
        assert len(t) == 2 and t[0].isalpha() and t[1].isdigit() and t[1] != "0"


def test_max_candidates_is_60():
    from vision.candidates import MAX_CANDIDATES

    assert MAX_CANDIDATES == 60


def test_som_candidate_model_uses_contract_bbox():
    from vision import SomCandidate

    c = SomCandidate(
        tag="A1",
        selector_path="button#x",
        bbox={"x": 1, "y": 2, "width": 3, "height": 4},
        role="button",
        name="",
    )
    assert isinstance(c.bbox, BBox)
    assert c.bbox.width == 3


# ---------------------------------------------------------------------------
# 실브라우저 통합
# ---------------------------------------------------------------------------


_ICON_HTML = """
<style>
  .ic { width: 40px; height: 40px; border: 1px solid #333; background: #eee; }
</style>
<button class="ic" id="b1"></button>
<button class="ic" id="b2"></button>
<button class="ic" id="b3"></button>
<a href="/x" id="lnk" style="display:inline-block;width:30px;height:30px"></a>
<div role="button" id="rb" style="width:30px;height:30px"></div>
<div onclick="void 0" id="oc" style="width:30px;height:30px"></div>
<div tabindex="0" id="ti" style="width:30px;height:30px"></div>
<button id="hidden" style="display:none">숨김</button>
<button id="offscreen" style="position:absolute;top:5000px">화면 밖</button>
<p id="plain">텍스트</p>
"""


@requires_chromium
async def test_collect_candidates_returns_visible_viewport_elements(page):
    from vision import collect_candidates

    await page.set_content(_ICON_HTML)
    cands = await collect_candidates(page)

    assert len(cands) >= 3
    assert [c.tag for c in cands[:3]] == ["A1", "A2", "A3"]
    ids = {c.selector_path for c in cands}
    # 라벨이 전혀 없는 아이콘 버튼도 후보에 포함된다 (Tier-2의 존재 이유)
    assert "button#b3" in ids or any("b3" in p for p in ids)
    assert not any("hidden" in p or "offscreen" in p or "plain" in p for p in ids)
    for c in cands:
        assert c.bbox.width > 0 and c.bbox.height > 0
        assert 0 <= c.bbox.x < thresholds.VIEWPORT_WIDTH
        assert 0 <= c.bbox.y < thresholds.VIEWPORT_HEIGHT


@requires_chromium
async def test_collect_candidates_covers_role_onclick_tabindex(page):
    from vision import collect_candidates

    await page.set_content(_ICON_HTML)
    cands = await collect_candidates(page)
    paths = " ".join(c.selector_path for c in cands)
    for marker in ("lnk", "rb", "oc", "ti"):
        assert marker in paths, f"{marker} 누락: {paths}"


@requires_chromium
async def test_collect_candidates_is_capped(page):
    from vision import collect_candidates
    from vision.candidates import MAX_CANDIDATES

    buttons = "".join(
        f'<button style="width:20px;height:20px;margin:1px">{i}</button>'
        for i in range(100)
    )
    await page.set_content(f"<div>{buttons}</div>")
    cands = await collect_candidates(page)
    assert len(cands) == MAX_CANDIDATES
    assert len({c.tag for c in cands}) == MAX_CANDIDATES


@requires_chromium
async def test_collect_candidates_empty_for_canvas_only(page):
    from vision import collect_candidates

    await page.set_content('<canvas width="600" height="300"></canvas>')
    assert await collect_candidates(page) == []
