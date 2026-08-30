"""WS-2 인지 엔진 테스트 (Gate 2).

살균기, 스코어러, 에포크 정책, 복구 사다리, Shadow DOM 순회를 검증한다.
실브라우저가 필요한 테스트는 Chromium 부재 시 skip 처리한다.
"""

from __future__ import annotations

import pytest

from contracts import ObserveResult, thresholds
from perception import (
    ROLE_WEIGHTS,
    SHADOW_HEALING_STRATEGIES,
    PerceptionEngine,
    RawElement,
    estimate_tokens,
    expand_top_n,
    filter_by_keywords,
    levenshtein,
    parse_collection,
    prune,
    score_element,
    similarity,
)


def make_element(
    seq: int = 0,
    role: str = "button",
    name: str = "확인",
    *,
    disabled: bool = False,
    in_viewport: bool = True,
    is_shadow: bool = False,
    testid: str | None = None,
    width: int = 100,
    height: int = 40,
) -> RawElement:
    return RawElement(
        seq=seq,
        role=role,
        name=name,
        tag="button",
        css_path=f"body > button:nth-of-type({seq + 1})",
        bbox={"x": 0, "y": 0, "width": width, "height": height},
        disabled=disabled,
        in_viewport=in_viewport,
        is_shadow=is_shadow,
        testid=testid,
    )


# ---------------------------------------------------------------------------
# 1. 스코어러
# ---------------------------------------------------------------------------


def test_button_outranks_generic():
    button = score_element(make_element(0, "button", "제출"))
    generic = score_element(make_element(1, "generic", "제출"))
    assert button.score > generic.score


def test_named_element_outranks_unnamed():
    named = score_element(make_element(0, "button", "장바구니 담기"))
    unnamed = score_element(make_element(1, "button", ""))
    assert named.score > unnamed.score


def test_viewport_element_outranks_offscreen():
    inside = score_element(make_element(0, in_viewport=True))
    outside = score_element(make_element(1, in_viewport=False))
    assert inside.score > outside.score


def test_disabled_element_is_penalized():
    enabled = score_element(make_element(0, disabled=False))
    disabled = score_element(make_element(1, disabled=True))
    assert enabled.score > disabled.score


def test_testid_adds_score():
    with_testid = score_element(make_element(0, testid="submit-btn"))
    without = score_element(make_element(1))
    assert with_testid.score > without.score


def test_goal_keyword_dominates_ranking():
    """목표 키워드 일치가 가장 강한 신호여야 한다."""
    target = make_element(5, "link", "결제 진행")
    noise = [make_element(i, "button", f"버튼 {i}") for i in range(20)]
    ranked = prune([*noise, target], top_n=3, goal_keywords=["결제"])
    assert any(s.name == "결제 진행" for s in ranked)


def test_pruning_is_deterministic():
    """동일 입력은 항상 동일 순위여야 한다 (플레이키율 KPI)."""
    elements = [make_element(i, name=f"항목 {i}") for i in range(50)]
    first = [s.element.seq for s in prune(elements, 20)]
    for _ in range(5):
        assert [s.element.seq for s in prune(elements, 20)] == first


def test_ties_break_by_dom_order():
    elements = [make_element(i, name="동일") for i in range(5)]
    ranked = prune(elements, 5)
    assert [s.element.seq for s in ranked] == [0, 1, 2, 3, 4]


def test_prune_respects_top_n():
    elements = [make_element(i) for i in range(100)]
    assert len(prune(elements, 20)) == 20
    assert len(prune(elements, 5)) == 5


def test_role_weights_cover_common_roles():
    for role in ("button", "link", "textbox", "checkbox", "combobox"):
        assert role in ROLE_WEIGHTS


# ---------------------------------------------------------------------------
# 2. 유사도 유틸 (자가 치유 3단계에서도 사용)
# ---------------------------------------------------------------------------


def test_levenshtein_basics():
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("", "abc") == 3
    assert levenshtein("kitten", "sitting") == 3


def test_similarity_is_normalized():
    assert similarity("결제", "결제") == 1.0
    assert similarity("", "") == 1.0
    assert 0.0 <= similarity("결제 진행", "결제하기") <= 1.0


def test_similarity_ignores_case_and_spacing():
    assert similarity("Submit  Order", "submit order") == 1.0


# ---------------------------------------------------------------------------
# 3. 복구 사다리 유틸 (PRD §3.2)
# ---------------------------------------------------------------------------


def test_expand_top_n_follows_spec():
    """1단계는 N=20 -> 50 확장이다."""
    assert expand_top_n(20) == 50
    assert expand_top_n(50) == 100


def test_keyword_filter_narrows_candidates():
    elements = [
        make_element(0, name="장바구니 담기"),
        make_element(1, name="위시리스트"),
        make_element(2, name="결제 진행"),
    ]
    filtered = filter_by_keywords(elements, ["결제"])
    assert [e.name for e in filtered] == ["결제 진행"]


def test_keyword_filter_without_keywords_returns_all():
    elements = [make_element(i) for i in range(3)]
    assert len(filter_by_keywords(elements, [])) == 3


# ---------------------------------------------------------------------------
# 4. 살균 결과 파싱
# ---------------------------------------------------------------------------


def test_parse_collection_maps_fields():
    payload = {
        "title": "테스트",
        "url": "https://a.test/",
        "total_dom_nodes": 120,
        "elements": [
            {
                "seq": 0,
                "role": "button",
                "name": "확인",
                "tag": "button",
                "css_path": "body > button",
                "bbox": {"x": 1, "y": 2, "width": 30, "height": 10},
                "in_viewport": True,
                "is_shadow": False,
                "disabled": False,
            }
        ],
    }
    page = parse_collection(payload)
    assert page.title == "테스트"
    assert len(page.elements) == 1
    assert page.elements[0].bbox["width"] == 30
    assert 0 < page.noise_reduction_ratio < 1


def test_noise_reduction_ratio_handles_zero():
    page = parse_collection({"title": "", "url": "", "elements": [], "total_dom_nodes": 0})
    assert page.noise_reduction_ratio == 0.0


# ---------------------------------------------------------------------------
# 5. 에포크 정책 (PRD §4.2) — 가장 중요
# ---------------------------------------------------------------------------


def test_epoch_starts_at_zero():
    assert PerceptionEngine().epoch == 0


def test_bump_epoch_increments_and_invalidates_handles():
    engine = PerceptionEngine()
    engine._handles["@e1"] = object()  # type: ignore[assignment]
    engine.bump_epoch("navigate")
    assert engine.epoch == 1
    assert engine.handles == {}


def test_stale_handle_is_not_returned():
    """이전 에포크의 element_id는 조회되지 않아야 한다."""
    from perception.engine import ElementHandle

    engine = PerceptionEngine()
    engine._handles["@e1"] = ElementHandle(
        element_id="@e1", epoch=0, role="button", name="확인",
        css_path="button", is_shadow=False,
    )
    assert engine.get_handle("@e1") is not None
    engine.bump_epoch("navigate")
    assert engine.get_handle("@e1") is None


def test_unknown_element_id_returns_none():
    assert PerceptionEngine().get_handle("@e999") is None


# ---------------------------------------------------------------------------
# 6. Shadow DOM 전략 (PRD §4.3)
# ---------------------------------------------------------------------------


def test_shadow_healing_excludes_xpath():
    """XPath는 Shadow Boundary를 통과할 수 없다."""
    assert "xpath" not in SHADOW_HEALING_STRATEGIES
    assert "css_piercing" in SHADOW_HEALING_STRATEGIES


# ---------------------------------------------------------------------------
# 7. 토큰 추정
# ---------------------------------------------------------------------------


def test_estimate_tokens_is_positive():
    assert estimate_tokens("button 확인\nlink 취소") > 0


def test_estimate_tokens_grows_with_text():
    short = estimate_tokens("button 확인")
    long = estimate_tokens("button 확인\n" * 50)
    assert long > short


# ---------------------------------------------------------------------------
# 8. 실브라우저 통합 (Gate 2 대응)
# ---------------------------------------------------------------------------


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


@requires_chromium
async def test_observe_returns_contract_model(mock_server):
    from playwright.async_api import async_playwright

    engine = PerceptionEngine()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s01_login"))
        result = await engine.observe_page(page=page)
        await browser.close()

    assert isinstance(result, ObserveResult)
    assert result.snapshot_epoch == 0
    assert result.elements
    assert all(e.element_id.startswith("@e") for e in result.elements)


@requires_chromium
async def test_hidden_elements_are_sanitized_out(mock_server):
    """display:none 등은 후보에서 제거되어야 한다."""
    from playwright.async_api import async_playwright

    engine = PerceptionEngine()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.set_content(
            """
            <button id="visible">보이는 버튼</button>
            <button id="none" style="display:none">숨김1</button>
            <button id="hidden" style="visibility:hidden">숨김2</button>
            <button id="transparent" style="opacity:0">숨김3</button>
            <button id="tiny" style="width:1px;height:1px;padding:0;border:0;
                    font-size:0;overflow:hidden">숨김4</button>
            <button id="aria" aria-hidden="true">숨김5</button>
            """
        )
        result = await engine.observe_page(page=page)
        await browser.close()

    names = {e.name for e in result.elements}
    assert "보이는 버튼" in names
    for hidden in ("숨김1", "숨김2", "숨김3", "숨김4", "숨김5"):
        assert hidden not in names


@requires_chromium
async def test_open_shadow_dom_is_traversed(mock_server):
    from playwright.async_api import async_playwright

    engine = PerceptionEngine()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s06_open_shadow"))
        result = await engine.observe_page(page=page)
        await browser.close()

    assert any("주문 확정" in e.name for e in result.elements)


@requires_chromium
async def test_closed_shadow_dom_requires_cdp_pierce(mock_server):
    """closed shadow root는 evaluate로는 불가, CDP pierce로만 접근 가능하다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(mock_server.site_url("s07_closed_shadow"))

        # (1) CDP 없이 관찰 -> 내부 요소를 찾지 못한다
        no_cdp = await PerceptionEngine().observe_page(page=page)
        found_without = any("비공개 결제" in e.name for e in no_cdp.elements)

        # (2) CDP pierce 사용 -> 찾아낸다
        cdp = await context.new_cdp_session(page)
        with_cdp = await PerceptionEngine().observe_page(page=page, cdp=cdp)
        found_with = any("비공개 결제" in e.name for e in with_cdp.elements)

        await browser.close()

    assert found_without is False, "closed shadow가 evaluate로 노출되면 안 된다"
    assert found_with is True, "CDP pierce로는 접근 가능해야 한다"


@requires_chromium
async def test_epoch_is_stable_under_ad_rotation(mock_server):
    """200ms 광고 로테이션에도 에포크가 오르면 안 된다 (PRD §4.2 핵심)."""
    from playwright.async_api import async_playwright

    engine = PerceptionEngine()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s09_ad_rotation"))

        first = await engine.observe_page(page=page)
        await page.wait_for_timeout(700)  # 광고 3회 이상 회전
        second = await engine.observe_page(page=page)
        await browser.close()

    assert first.snapshot_epoch == second.snapshot_epoch == 0


@requires_chromium
async def test_recovery_ladder_escalates_when_target_missing(mock_server):
    """Top-N 안에 정답이 없으면 사다리가 실제로 단계를 올려야 한다."""
    from playwright.async_api import async_playwright

    engine = PerceptionEngine()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s08_infinite"))

        # 절대 매칭되지 않는 조건을 주어 사다리를 끝까지 태운다.
        result, trace = await engine.observe_with_recovery(
            page,
            goal_keywords=["존재하지 않는 요소"],
            target_predicate=lambda e: e.name == "__NEVER_MATCHES__",
            top_n=5,
        )
        await browser.close()

    # 1 -> 4단계가 모두 시도되어야 한다.
    assert "expand_n" in trace.stages_used
    assert "keyword_filter" in trace.stages_used
    assert "full_tree" in trace.stages_used
    assert "scroll_reobserve" in trace.stages_used
    assert trace.recovered is True


@requires_chromium
async def test_recovery_ladder_stops_early_when_target_found(mock_server):
    """정답이 이미 Top-N에 있으면 사다리를 타지 않아야 한다 (불필요한 비용 방지)."""
    from playwright.async_api import async_playwright

    engine = PerceptionEngine()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s14_lazy"))
        await page.wait_for_timeout(400)

        result, trace = await engine.observe_with_recovery(
            page,
            goal_keywords=["지연 로딩"],
            target_predicate=lambda e: "지연 로딩 버튼" in e.name,
            top_n=20,
        )
        await browser.close()

    assert any("지연 로딩 버튼" in e.name for e in result.elements)
    assert trace.stages_used == []
    assert trace.recovered is False


@requires_chromium
async def test_observation_meets_gate2_budgets(mock_server):
    """관찰 토큰·지연이 Gate 2 예산 안에 있어야 한다."""
    from playwright.async_api import async_playwright

    engine = PerceptionEngine()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s08_infinite"))
        result = await engine.observe_page(page=page)
        await browser.close()

    assert result.token_count <= thresholds.OBSERVATION_TOKENS_P50
    assert engine.last_latency_ms <= thresholds.OBSERVE_LATENCY_MS_P50


# ---------------------------------------------------------------------------
# accessible name / role 계산 일관성 (WS-8 실환경 검증에서 발견)
#
# sanitizer, verification, shadow_dom 세 곳이 각자 name/role을 계산한다.
# 규칙이 갈라지면 관찰 시점과 검증 시점의 값이 달라져 TOCTOU 오탐이 난다.
#
# 실제 피해:
#   - title이 텍스트보다 앞섬     -> 'Log in'이 72자 툴팁으로 계산 -> NAME_CHANGED
#   - input[type=search] 처리 누락 -> 'searchbox' vs 'textbox'     -> ROLE_CHANGED
# 둘 다 요소는 전혀 바뀌지 않았는데 액션이 연속 차단됐다.
# ---------------------------------------------------------------------------

import re as _re
from pathlib import Path as _Path

_JS_SOURCES = {
    "sanitizer": _Path("src/perception/sanitizer.py"),
    "verification": _Path("src/actions/verification.py"),
    "shadow_dom": _Path("src/perception/shadow_dom.py"),
}


@pytest.mark.parametrize("name,path", list(_JS_SOURCES.items()))
def test_content_text_precedes_title_in_name_computation(name, path):
    """W3C accname 순서: 콘텐츠 텍스트가 title보다 앞선다."""
    if not path.exists():
        pytest.skip(f"{path} 없음")
    src = path.read_text(encoding="utf-8")
    title_pos = src.find("getAttribute('title')")
    if title_pos < 0:
        pytest.skip("title 폴백 없음")
    text_pos = min(
        (p for p in (src.find("innerText"), src.find("textContent")) if p >= 0),
        default=-1,
    )
    assert text_pos >= 0, f"{name}: 텍스트 폴백이 없음"
    assert text_pos < title_pos, (
        f"{name}: title이 콘텐츠 텍스트보다 먼저 평가됨. "
        "링크/버튼 이름이 툴팁으로 계산되어 TOCTOU 오탐이 발생한다."
    )


@pytest.mark.parametrize("name,path", list(_JS_SOURCES.items()))
def test_search_input_maps_to_searchbox_everywhere(name, path):
    """input[type=search]를 한 곳만 searchbox로 처리하면 role이 갈린다."""
    if not path.exists():
        pytest.skip(f"{path} 없음")
    src = path.read_text(encoding="utf-8")
    if "'textbox'" not in src:
        pytest.skip("input role 추론 없음")

    # 주석이나 다른 위치의 'searchbox' 언급으로는 통과하면 안 된다.
    # type === 'search' 분기가 실제로 존재하는지 확인한다.
    has_branch = bool(
        _re.search(r"===\s*'search'[^\n]*searchbox", src)
        or _re.search(r"t === 'search'\)\s*\?\s*'searchbox'", src)
    )
    assert has_branch, (
        f"{name}: input[type=search] -> searchbox 분기가 없어 'textbox'로 "
        "계산된다. 관찰(searchbox)과 검증(textbox)이 불일치해 "
        "ROLE_CHANGED 오탐이 난다."
    )
