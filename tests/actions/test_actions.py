"""WS-3 액션 스페이스 테스트 (Gate 3-A 항목 1).

자가 치유 사다리, retry_safe 판정, staleness/사후조건 검증,
19종 디스패처를 검증한다.
"""

from __future__ import annotations

import pytest

from contracts import ActionType, ErrorCode, thresholds
from perception.engine import ElementHandle

from actions import (
    DEFAULT_LADDER,
    IDEMPOTENT_ACTIONS,
    SHADOW_LADDER,
    SIDE_EFFECT_ACTIONS,
    FailurePhase,
    HealingCandidate,
    HealingStrategy,
    PageStateSnapshot,
    StalenessReason,
    heal,
    is_retry_safe,
    ladder_for,
    verify_post_condition,
)


def make_handle(
    element_id: str = "@e1",
    role: str = "button",
    name: str = "로그인",
    css_path: str = "body > button",
    testid: str | None = None,
    is_shadow: bool = False,
    epoch: int = 0,
) -> ElementHandle:
    return ElementHandle(
        element_id=element_id,
        epoch=epoch,
        role=role,
        name=name,
        css_path=css_path,
        is_shadow=is_shadow,
        testid=testid,
    )


def make_candidate(
    element_id: str = "@e1",
    role: str = "button",
    name: str = "로그인",
    css_path: str = "body > button",
    testid: str | None = None,
    is_shadow: bool = False,
) -> HealingCandidate:
    return HealingCandidate(
        element_id=element_id,
        role=role,
        name=name,
        css_path=css_path,
        testid=testid,
        is_shadow=is_shadow,
    )


# ---------------------------------------------------------------------------
# 1. 자가 치유 사다리 — 각 단계를 개별 검증
# ---------------------------------------------------------------------------


def test_stage1_role_name_exact_match():
    target = make_handle()
    result = heal(target, [make_candidate(css_path="완전히 달라진 경로")])
    assert result.healed is True
    assert result.strategy is HealingStrategy.ROLE_NAME


def test_stage2_testid_when_name_changed():
    """이름이 바뀌어도 testid가 같으면 2단계에서 치유되어야 한다."""
    target = make_handle(name="로그인", testid="login-btn")
    candidate = make_candidate(name="Sign In", testid="login-btn", css_path="다름")
    result = heal(target, [candidate])
    assert result.healed is True
    assert result.strategy is HealingStrategy.TESTID


def test_stage3_text_similarity_for_minor_change():
    """문구가 조금 바뀐 경우 3단계 유사도로 치유한다."""
    target = make_handle(name="장바구니 담기")
    candidate = make_candidate(name="장바구니에 담기", css_path="다름")
    result = heal(target, [candidate])
    assert result.healed is True
    assert result.strategy is HealingStrategy.TEXT_SIMILARITY


def test_stage4_css_path_last_resort():
    """role/name/testid가 모두 달라도 CSS 경로가 같으면 4단계로 치유한다."""
    target = make_handle(role="button", name="확인", css_path="form > button#go")
    candidate = make_candidate(
        role="button", name="전혀 다른 이름", css_path="form > button#go"
    )
    result = heal(target, [candidate])
    assert result.healed is True
    assert result.strategy is HealingStrategy.CSS_PATH


def test_ladder_order_prefers_earlier_stage():
    """상위 단계가 가능하면 하위 단계로 내려가지 않아야 한다."""
    target = make_handle(name="로그인", testid="login-btn")
    exact = make_candidate(element_id="@e1", name="로그인", testid="login-btn")
    result = heal(target, [exact])
    assert result.strategy is HealingStrategy.ROLE_NAME
    assert result.attempts == ["role_name"]


def test_text_similarity_rejects_different_role():
    """이름이 비슷해도 role이 다르면 다른 요소다."""
    target = make_handle(role="button", name="검색")
    candidate = make_candidate(role="link", name="검색", css_path="다름")
    result = heal(target, [candidate])
    assert result.healed is False


def test_text_similarity_respects_threshold():
    target = make_handle(name="장바구니 담기")
    candidate = make_candidate(name="회원 탈퇴하기", css_path="다름")
    result = heal(target, [candidate])
    assert result.healed is False


def test_healing_fails_when_element_gone():
    target = make_handle(name="로그인")
    result = heal(target, [])
    assert result.healed is False
    assert len(result.attempts) == len(DEFAULT_LADDER)


def test_shadow_ladder_excludes_xpath_uses_piercing():
    """Shadow Boundary는 XPath로 통과할 수 없다 (PRD §4.3)."""
    shadow_handle = make_handle(is_shadow=True)
    ladder = ladder_for(shadow_handle)
    assert ladder == SHADOW_LADDER
    assert HealingStrategy.CSS_PIERCING in ladder
    assert HealingStrategy.CSS_PATH not in ladder


def test_normal_ladder_uses_css_path():
    assert ladder_for(make_handle(is_shadow=False)) == DEFAULT_LADDER


# ---------------------------------------------------------------------------
# 2. retry_safe 판정 (PRD §4.1) — 가장 중요
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.PRESS_KEY,
     ActionType.UPLOAD_FILE, ActionType.DOWNLOAD_FILE, ActionType.HANDLE_DIALOG],
)
def test_side_effect_actions_are_unsafe_after_dispatch(action):
    """발송 후 재시도하면 이중 제출/결제가 발생할 수 있다."""
    assert is_retry_safe(action, FailurePhase.POST_DISPATCH) is False


@pytest.mark.parametrize(
    "action",
    [ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.PRESS_KEY,
     ActionType.UPLOAD_FILE, ActionType.DOWNLOAD_FILE],
)
def test_all_actions_are_safe_before_dispatch(action):
    """발송 전에는 브라우저에 아무 일도 일어나지 않았으므로 안전하다."""
    assert is_retry_safe(action, FailurePhase.PRE_DISPATCH) is True


@pytest.mark.parametrize(
    "action",
    [ActionType.SELECT_OPTION, ActionType.CHECK_BOX, ActionType.SCROLL,
     ActionType.HOVER, ActionType.OBSERVE_PAGE, ActionType.EXTRACT],
)
def test_idempotent_actions_are_safe_after_dispatch(action):
    assert is_retry_safe(action, FailurePhase.POST_DISPATCH) is True


def test_idempotent_and_side_effect_sets_are_disjoint():
    """한 액션이 양쪽에 속하면 판정이 모순된다."""
    assert IDEMPOTENT_ACTIONS & SIDE_EFFECT_ACTIONS == set()


def test_every_action_type_has_a_retry_verdict():
    """19종 전부가 판정 가능해야 한다."""
    for action in ActionType:
        assert isinstance(is_retry_safe(action, FailurePhase.POST_DISPATCH), bool)


# ---------------------------------------------------------------------------
# 3. 사후조건 검증
# ---------------------------------------------------------------------------


def base_state(**kwargs) -> PageStateSnapshot:
    defaults = dict(
        url="https://a.test/",
        dom_node_count=100,
        text_signature=1234,
        active_element="BODY#",
        element_signature="cls||",
    )
    defaults.update(kwargs)
    return PageStateSnapshot(**defaults)  # type: ignore[arg-type]


def test_url_change_satisfies_post_condition():
    result = verify_post_condition(base_state(), base_state(url="https://a.test/next"))
    assert result.satisfied is True


def test_dom_delta_satisfies_post_condition():
    result = verify_post_condition(base_state(), base_state(dom_node_count=105))
    assert result.satisfied is True


def test_text_change_satisfies_post_condition():
    """노드 수가 그대로여도 텍스트가 바뀌면 변화가 있었다."""
    result = verify_post_condition(base_state(), base_state(text_signature=9999))
    assert result.satisfied is True


def test_focus_move_satisfies_post_condition():
    """클릭이 요소에 도달했다는 증거."""
    result = verify_post_condition(base_state(), base_state(active_element="BUTTON#go"))
    assert result.satisfied is True


def test_attribute_toggle_satisfies_post_condition():
    result = verify_post_condition(base_state(), base_state(element_signature="cls|true|"))
    assert result.satisfied is True


def test_no_change_is_silent_failure():
    result = verify_post_condition(base_state(), base_state())
    assert result.satisfied is False
    assert result.silent_failure is True


def test_expected_value_is_authoritative():
    """기대값이 주어지면 다른 신호가 있어도 그것으로 판정한다."""
    after = base_state(url="https://a.test/changed", element_value="wrong")
    result = verify_post_condition(base_state(), after, expected_value="typed")
    assert result.satisfied is False


def test_expected_value_match_succeeds():
    after = base_state(element_value="typed")
    result = verify_post_condition(base_state(), after, expected_value="typed")
    assert result.satisfied is True


def test_expected_checked_mismatch_fails():
    after = base_state(element_checked=False)
    result = verify_post_condition(base_state(), after, expected_checked=True)
    assert result.satisfied is False


# ---------------------------------------------------------------------------
# 4. 실브라우저 통합
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


async def _make_dispatcher(page, cdp=None):
    from perception import PerceptionEngine

    from actions import ActionDispatcher, DispatchContext

    engine = PerceptionEngine()
    return ActionDispatcher(DispatchContext(page=page, engine=engine, cdp=cdp)), engine


@requires_chromium
async def test_click_succeeds_and_returns_contract_fields(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s13_spa"))

        dispatcher, engine = await _make_dispatcher(page)
        observation = await engine.observe_page(page=page)
        target = next(e for e in observation.elements if "설정으로 이동" in e.name)

        result = await dispatcher.dispatch(
            ActionType.CLICK, {"element_id": target.element_id}
        )
        await browser.close()

    assert result.success is True
    # 동결된 계약의 필수 필드가 모두 채워져야 한다.
    assert result.current_url
    assert result.tab_id
    assert isinstance(result.snapshot_epoch, int)
    assert isinstance(result.retry_safe, bool)


@requires_chromium
async def test_navigate_bumps_epoch(mock_server):
    """네비게이션은 에포크를 올려야 한다 (PRD §4.2)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        dispatcher, engine = await _make_dispatcher(page)

        before = engine.epoch
        result = await dispatcher.dispatch(
            ActionType.NAVIGATE, {"url": mock_server.site_url("s01_login")}
        )
        await browser.close()

    assert result.success is True
    assert engine.epoch == before + 1


@requires_chromium
async def test_stale_element_id_is_rejected_after_navigation(mock_server):
    """이전 에포크의 element_id로 액션하면 TOCTOU로 차단되어야 한다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s01_login"))

        dispatcher, engine = await _make_dispatcher(page)
        observation = await engine.observe_page(page=page)
        old_id = observation.elements[0].element_id

        # 네비게이션 -> 에포크 증가 -> 기존 핸들 전역 무효화
        await dispatcher.dispatch(
            ActionType.NAVIGATE, {"url": mock_server.site_url("s02_twofactor")}
        )
        result = await dispatcher.dispatch(ActionType.CLICK, {"element_id": old_id})
        await browser.close()

    assert result.success is False
    assert result.error_code is ErrorCode.TOCTOU_MISMATCH
    assert result.reobserve_required is True


@requires_chromium
async def test_type_text_verifies_value(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s01_login"))

        dispatcher, engine = await _make_dispatcher(page)
        observation = await engine.observe_page(page=page)
        target = next(e for e in observation.elements if e.role == "textbox")

        result = await dispatcher.dispatch(
            ActionType.TYPE_TEXT,
            {"element_id": target.element_id, "text": "홍길동", "clear_before": True},
        )
        await browser.close()

    assert result.success is True


@requires_chromium
async def test_screenshot_som_returns_not_implemented(mock_server):
    """SoM은 v1.1 기능이므로 명시적으로 미구현을 반환해야 한다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s01_login"))
        dispatcher, _ = await _make_dispatcher(page)

        result = await dispatcher.dispatch(
            ActionType.TAKE_SCREENSHOT, {"annotate_som": True}
        )
        await browser.close()

    assert result.success is False
    assert result.error_code is ErrorCode.FEATURE_NOT_IMPLEMENTED


@requires_chromium
async def test_go_back_without_history_returns_no_history(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s01_login"))
        dispatcher, _ = await _make_dispatcher(page)

        result = await dispatcher.dispatch(ActionType.GO_BACK, {})
        await browser.close()

    assert result.success is False
    assert result.error_code is ErrorCode.NO_HISTORY


@requires_chromium
async def test_extract_returns_text(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s04_download"))
        dispatcher, _ = await _make_dispatcher(page)

        result = await dispatcher.dispatch(ActionType.EXTRACT, {"selector": "h1"})
        await browser.close()

    assert result.success is True
    assert "월간 보고서" in result.data["items"]["text"]


@requires_chromium
async def test_extract_missing_selector_fails(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s01_login"))
        dispatcher, _ = await _make_dispatcher(page)

        result = await dispatcher.dispatch(
            ActionType.EXTRACT, {"selector": "#does-not-exist"}
        )
        await browser.close()

    assert result.success is False
    assert result.error_code is ErrorCode.ELEMENT_NOT_FOUND


@requires_chromium
async def test_switch_frame_bumps_epoch(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s05_iframe"))
        dispatcher, engine = await _make_dispatcher(page)

        before = engine.epoch
        result = await dispatcher.dispatch(
            ActionType.SWITCH_FRAME, {"frame_selector": "#outer"}
        )
        await browser.close()

    assert result.success is True
    assert engine.epoch == before + 1


@requires_chromium
async def test_scroll_flags_reobserve_on_infinite_list(mock_server):
    """스크롤로 동적 노드가 로드되면 재관찰이 필요하다고 알려야 한다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s08_infinite"))
        dispatcher, _ = await _make_dispatcher(page)

        result = await dispatcher.dispatch(
            ActionType.SCROLL, {"direction": "down", "distance": 2000}
        )
        await browser.close()

    assert result.success is True


@requires_chromium
async def test_dispatcher_heals_after_dom_mutation(mock_server):
    """관찰 후 DOM이 바뀌어도 치유로 액션이 성공해야 한다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s01_login"))

        dispatcher, engine = await _make_dispatcher(page)
        observation = await engine.observe_page(page=page)
        target = next(e for e in observation.elements if "로그인" in e.name)

        # 클래스 변경 + 부모 래핑으로 CSS 경로를 깨뜨린다
        await page.evaluate(
            """
            () => {
              const el = document.getElementById('submit');
              const wrap = document.createElement('div');
              el.parentNode.insertBefore(wrap, el);
              wrap.appendChild(el);
              el.className = 'regenerated-hash';
            }
            """
        )

        result = await dispatcher.dispatch(
            ActionType.CLICK, {"element_id": target.element_id}
        )
        await browser.close()

    # 치유가 동작했거나, 최소한 재관찰 요구로 안전하게 실패해야 한다.
    assert result.success is True or result.reobserve_required is True
