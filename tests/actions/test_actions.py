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
    READ_ONLY_ACTIONS,
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
    path_heal_allowed,
    verify_post_condition,
    verify_staleness,
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


@pytest.mark.parametrize(
    "before,after",
    [
        ("로그인", "로그인하기"),
        ("저장", "저장하기"),
        ("확인", "확인하기"),
        ("Save", "Save Changes"),
        ("Delete", "Delete Item"),
    ],
)
def test_stage3_handles_short_label_suffix_extension(before, after):
    """짧은 라벨의 접미 확장은 A/B 테스트·i18n에서 흔하다.

    순수 편집 거리는 '저장'->'저장하기'를 0.50으로 평가해 3단계가
    통째로 무력해졌다. label_similarity가 이를 교정해야 한다.
    """
    target = make_handle(name=before)
    candidate = make_candidate(name=after, css_path="다름")
    result = heal(target, [candidate])
    assert result.healed is True, f"{before!r} -> {after!r} 치유 실패"
    assert result.strategy is HealingStrategy.TEXT_SIMILARITY


@pytest.mark.parametrize(
    "before,after",
    [
        ("삭제", "전체 삭제"),   # 한정어가 붙어 대상 범위가 달라짐
        ("저장", "취소"),
        ("결제 진행", "회원 탈퇴"),
    ],
)
def test_stage3_rejects_semantic_change(before, after):
    """접두 추가는 의미가 바뀌므로 치유하면 안 된다."""
    target = make_handle(name=before)
    candidate = make_candidate(name=after, css_path="다름")
    result = heal(target, [candidate])
    assert result.healed is False, f"{before!r} -> {after!r}를 잘못 치유함"


def test_stage3_refuses_when_candidates_are_ambiguous():
    """유사한 후보가 둘이면 잘못 누르느니 포기해야 한다.

    '삭제' 버튼이 사라진 자리에 '삭제하기'와 '삭제하기2'가 함께 있으면
    어느 쪽을 눌러도 부작용이 크다.
    """
    target = make_handle(name="삭제")
    candidates = [
        make_candidate(element_id="@e1", name="삭제하기", css_path="a"),
        make_candidate(element_id="@e2", name="삭제하기2", css_path="b"),
    ]
    result = heal(target, candidates)
    assert result.strategy is not HealingStrategy.TEXT_SIMILARITY
    assert any("ambiguous" in a for a in result.attempts)


def test_stage3_proceeds_when_one_candidate_is_clearly_better():
    """모호성 가드가 정상 케이스를 막으면 안 된다."""
    target = make_handle(name="로그인")
    candidates = [
        make_candidate(element_id="@e1", name="로그인하기", css_path="a"),
        make_candidate(element_id="@e2", name="회원가입", css_path="b"),
    ]
    result = heal(target, candidates)
    assert result.healed is True
    assert result.strategy is HealingStrategy.TEXT_SIMILARITY
    assert result.candidate is not None
    assert result.candidate.element_id == "@e1"


def test_label_similarity_is_symmetric():
    from perception import label_similarity

    assert label_similarity("저장", "저장하기") == label_similarity("저장하기", "저장")


def test_stage4_css_path_last_resort():
    """읽기 액션이면 role/name/testid가 모두 달라도 CSS 경로가 같으면 4단계로 치유한다."""
    target = make_handle(role="button", name="확인", css_path="form > button#go")
    candidate = make_candidate(
        role="menuitem", name="전혀 다른 이름", css_path="form > button#go"
    )
    result = heal(target, [candidate], action=ActionType.HOVER)
    assert result.healed is True
    assert result.strategy is HealingStrategy.CSS_PATH
    assert result.identity_refused is None


#: WS-37: 같은 자리(css_path)에 role/name 이 바뀐 요소 — 이름만 / 역할만 / 둘 다
_SWAPPED = [
    ("name", "button", "회원 탈퇴"),
    ("role", "checkbox", "확인"),
    ("both", "menuitem", "광고: 대출 상담"),
]


@pytest.mark.parametrize("kind,role,name", _SWAPPED, ids=[k for k, _, _ in _SWAPPED])
@pytest.mark.parametrize(
    "action", sorted(set(ActionType) - set(READ_ONLY_ACTIONS), key=lambda a: a.value)
)
def test_stage4_refuses_swapped_element_for_side_effect_actions(action, kind, role, name):
    """WS-37 결함1: 부작용 액션은 경로 단계로 제자리 교체된 요소를 고르지 않는다."""
    target = make_handle(role="button", name="확인", css_path="form > button#go")
    swapped = make_candidate(role=role, name=name, css_path="form > button#go")
    result = heal(target, [swapped], action=action)
    assert result.healed is False and result.candidate is None
    assert result.identity_refused is swapped
    assert result.attempts[-1] == "css_path(identity_changed)"
    assert "확인" in result.reason and name in result.reason and role in result.reason


@pytest.mark.parametrize("kind,role,name", _SWAPPED, ids=[k for k, _, _ in _SWAPPED])
def test_stage4_unknown_action_is_fail_closed(kind, role, name):
    """action 을 모르면(None) 부작용으로 본다."""
    target = make_handle(role="button", name="확인", css_path="form > button#go")
    result = heal(target, [make_candidate(role=role, name=name, css_path="form > button#go")])
    assert result.healed is False and result.identity_refused is not None


def test_shadow_piercing_stage_has_same_guard():
    target = make_handle(name="확인", css_path="x-app >>> button", is_shadow=True)
    swapped = make_candidate(name="회원 탈퇴", css_path="x-app >>> button", is_shadow=True)
    refused = heal(target, [swapped], action=ActionType.CLICK)
    assert refused.healed is False and refused.attempts[-1] == "css_piercing(identity_changed)"
    ok = heal(target, [swapped], action=ActionType.EXTRACT)
    assert ok.healed is True and ok.strategy is HealingStrategy.CSS_PIERCING


@pytest.mark.parametrize("action", sorted(READ_ONLY_ACTIONS, key=lambda a: a.value))
@pytest.mark.parametrize("kind,role,name", _SWAPPED, ids=[k for k, _, _ in _SWAPPED])
def test_stage4_read_actions_keep_path_healing(action, kind, role, name):
    target = make_handle(role="button", name="확인", css_path="form > button#go")
    result = heal(target, [make_candidate(role=role, name=name, css_path="form > button#go")],
                  action=action)
    assert result.healed is True and result.strategy is HealingStrategy.CSS_PATH


def test_read_only_actions_exclude_every_state_changing_action():
    """읽기 집합에 부작용 액션(재시도 불가 집합 + 선택·체크)이 섞이면 가드가 뚫린다."""
    assert not (set(READ_ONLY_ACTIONS) & set(SIDE_EFFECT_ACTIONS))
    for action in (*SIDE_EFFECT_ACTIONS, ActionType.SELECT_OPTION, ActionType.CHECK_BOX):
        assert path_heal_allowed(action) is False, action
    assert path_heal_allowed(None) is False
    assert set(READ_ONLY_ACTIONS) == {
        ActionType.OBSERVE_PAGE, ActionType.TAKE_SCREENSHOT, ActionType.SCROLL,
        ActionType.HOVER, ActionType.WAIT_FOR, ActionType.EXTRACT,
    }


def test_side_effect_action_still_heals_same_identity_at_other_path():
    """가드는 경로 단계만 막는다: 같은 role+name 이 옮겨지면 클릭도 1단계로 치유."""
    target = make_handle(role="button", name="확인", css_path="form > button#go")
    moved = make_candidate(role="button", name="확인", css_path="section > button")
    result = heal(target, [moved], action=ActionType.CLICK)
    assert result.healed is True and result.strategy is HealingStrategy.ROLE_NAME


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


def test_harness_requires_all_ladder_stages():
    """하네스 시나리오가 4단계를 모두 유발하도록 배치되어야 한다.

    초기 하네스는 모든 시나리오가 role+name을 유지해 1단계에서만
    해결됐고, 그 상태에서는 2~4단계를 통째로 무력화해도 성공률이
    1.0으로 보고됐다(사보타주 실험으로 확인). 시나리오가 다시
    1단계로 쏠리면 이 테스트가 실패한다.
    """
    from harness.self_healing import MUTATION_CASES, REQUIRED_STAGES

    declared = {case.expected_stage for case in MUTATION_CASES}
    missing = [s for s in REQUIRED_STAGES if s not in declared]
    assert not missing, f"하네스가 유발하지 않는 치유 단계: {missing}"


def test_harness_stage4_positive_uses_read_action_and_refusals_are_side_effect():
    """WS-37: 4단계 양성은 읽기 액션으로, 부작용 액션 제자리 교체는 거부(음성) 케이스로."""
    from harness.self_healing import MUTATION_CASES, REQUIRED_REFUSALS, REQUIRED_STAGES

    positive = [c for c in MUTATION_CASES if not c.expect_refusal]
    negative = [c for c in MUTATION_CASES if c.expect_refusal]
    assert {c.expected_stage for c in positive} == set(REQUIRED_STAGES)
    stage4 = [c for c in positive if c.expected_stage == "css_path"]
    assert stage4 and all(c.action in READ_ONLY_ACTIONS for c in stage4)
    assert {c.expect_refusal for c in negative} == set(REQUIRED_REFUSALS)
    assert all(c.action not in READ_ONLY_ACTIONS for c in negative)


def test_harness_cases_reach_product_heal_path():
    """WS-37 R1: 음성은 testid·비슷한 이름 경로까지(검증 B1·NB-2), 양성은 제품에서 사다리에 닿는 액션.

    extract 는 디스패처가 element_id 를 받지 않아 제품 경로에서 치유에 닿지 않는다(검증 NB-5).
    """
    from harness.self_healing import FRONT_GUARD, MUTATION_CASES, REQUIRED_REFUSALS

    assert {"testid", "similar"} <= set(REQUIRED_REFUSALS)
    positive = [c for c in MUTATION_CASES if not c.expect_refusal]
    negative = [c for c in MUTATION_CASES if c.expect_refusal]
    assert all(c.action is not ActionType.EXTRACT for c in positive)
    assert all(c.expected_stage == FRONT_GUARD for c in negative)
    assert FRONT_GUARD not in {c.expected_stage for c in positive}


def test_harness_required_stages_match_ladder():
    """REQUIRED_STAGES가 실제 사다리 정의와 어긋나면 안 된다."""
    from harness.self_healing import REQUIRED_STAGES

    ladder_values = {s.value for s in DEFAULT_LADDER}
    assert set(REQUIRED_STAGES) == ladder_values


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


#: (액션 × 실패 단계) 기대 판정 — 소스가 아니라 PRD §4.1 멱등성 열과 §4.3 흐름도에서 옮겼다.
#: PRE_DISPATCH: 브라우저에 아무 이벤트도 가지 않았으므로 모든 액션이 안전(§4.3 1단계).
#: POST_DISPATCH: §4.1 멱등성 Yes 만 안전. No(부작용: 키 입력·대화상자 응답·파일 업·다운로드)와
#: '실패단계 종속'(click·type_text = 발송 뒤엔 이중 제출 위험)은 불가. tab_control 은 'Depends'
#: 지만 create/close 가 탭을 만들고 닫는 부작용이라 명령을 모르는 판정에서는 보수적으로 불가.
_POST_DISPATCH_SAFE = {
    ActionType.OBSERVE_PAGE: True,
    ActionType.TAKE_SCREENSHOT: True,
    ActionType.NAVIGATE: True,
    ActionType.GO_BACK: True,
    ActionType.RELOAD: True,
    ActionType.CLICK: False,
    ActionType.TYPE_TEXT: False,
    ActionType.SELECT_OPTION: True,
    ActionType.CHECK_BOX: True,
    ActionType.SCROLL: True,
    ActionType.HOVER: True,
    ActionType.PRESS_KEY: False,
    ActionType.WAIT_FOR: True,
    ActionType.EXTRACT: True,
    ActionType.SWITCH_FRAME: True,
    ActionType.HANDLE_DIALOG: False,
    ActionType.UPLOAD_FILE: False,
    ActionType.DOWNLOAD_FILE: False,
    ActionType.TAB_CONTROL: False,
}


def test_every_action_type_has_a_retry_verdict():
    """19종 각각이 단계별로 PRD 가 정한 판정을 내야 한다(bool 이기만 하면 통과하던 것을 조임)."""
    assert set(_POST_DISPATCH_SAFE) == set(ActionType) and len(_POST_DISPATCH_SAFE) == 19
    wrong = {
        action.value: (
            is_retry_safe(action, FailurePhase.PRE_DISPATCH),
            is_retry_safe(action, FailurePhase.POST_DISPATCH),
        )
        for action in ActionType
        if is_retry_safe(action, FailurePhase.PRE_DISPATCH) is not True
        or is_retry_safe(action, FailurePhase.POST_DISPATCH) is not _POST_DISPATCH_SAFE[action]
    }
    assert wrong == {}, f"(PRE, POST) 판정이 PRD 와 다름: {wrong}"


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


def test_focus_move_alone_is_not_an_effect():
    """포커스 이동만으로는 성공이 아니다.

    포커스는 브라우저가 클릭 시 자동으로 옮긴다 — 클릭이 요소에 **닿았다**는
    증거일 뿐 무언가 **일어났다**는 증거가 아니다. PRD §4.3 사후조건 목록에도
    없다. 실측 — onclick 없는 버튼이 1회차에만 성공으로 판정되고(포커스가
    BODY -> BUTTON), 2회차는 포커스가 그대로라 Silent Failure가 됐다. 같은 무반응
    버튼이 호출 순서에 따라 성공/실패로 갈렸고, 모델은 성공을 믿고 재클릭했다.
    """
    result = verify_post_condition(base_state(), base_state(active_element="BUTTON#go"))
    assert result.satisfied is False
    assert result.silent_failure is True
    # 진단용으로 기록은 남는다
    assert any(s.startswith("focus_moved") for s in result.signals)


def test_focus_move_with_effect_still_succeeds():
    after = base_state(active_element="BUTTON#go", text_signature=9999)
    result = verify_post_condition(base_state(), after)
    assert result.satisfied is True


@pytest.mark.parametrize(
    "before_url,after_url",
    [
        ("https://a.test/form", "https://a.test/form?"),
        ("https://a.test/form", "https://a.test/form#"),
        ("https://a.test/form?", "https://a.test/form"),
    ],
)
def test_empty_query_or_fragment_is_not_navigation(before_url, after_url):
    """핸들러 없는 form submit이 붙이는 빈 '?'는 이동이 아니다.

    실측 — s03_multistep '다음 단계'가 /s03_multistep -> /s03_multistep? 로
    바뀐 것만으로 성공 판정됐다. 페이지는 그대로였다.
    """
    result = verify_post_condition(base_state(url=before_url), base_state(url=after_url))
    assert result.satisfied is False


def test_real_query_change_is_navigation():
    result = verify_post_condition(
        base_state(url="https://a.test/form"), base_state(url="https://a.test/form?step=2")
    )
    assert result.satisfied is True


def test_new_tab_is_an_effect():
    """팝업(새 탭) 열림은 원래 페이지를 바꾸지 않지만 명백한 효과다."""
    result = verify_post_condition(base_state(page_count=1), base_state(page_count=2))
    assert result.satisfied is True
    assert any(s.startswith("new_tab") for s in result.signals)


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


# --- v1.1 재동결: 좌표 클릭 (Tier-2 SoM / Canvas 폴백) ---


@requires_chromium
async def test_click_by_coordinates_hits_element_under_point(mock_server):
    """DOM 대상 없이 뷰포트 좌표만으로 클릭하면 그 지점의 요소가 눌려야 한다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s13_spa"))
        dispatcher, engine = await _make_dispatcher(page)
        await engine.observe_page(page=page)
        box = await page.locator("#go-settings").bounding_box()
        assert box is not None
        x = int(box["x"] + box["width"] / 2)
        y = int(box["y"] + box["height"] / 2)

        result = await dispatcher.dispatch(
            ActionType.CLICK, {"x": x, "y": y, "epoch": engine.epoch}
        )
        url_after = page.url
        await browser.close()

    assert result.success is True, result.error_message
    assert result.data.get("coordinates") == {"x": x, "y": y}
    assert url_after.endswith("/s13_spa/settings")


@requires_chromium
async def test_click_by_coordinates_rejects_stale_epoch(mock_server):
    """좌표는 스크린샷 시점에 종속되므로 epoch 불일치는 거부한다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s13_spa"))
        dispatcher, engine = await _make_dispatcher(page)
        await engine.observe_page(page=page)

        result = await dispatcher.dispatch(
            ActionType.CLICK, {"x": 10, "y": 10, "epoch": engine.epoch + 5}
        )
        await browser.close()

    assert result.success is False
    assert result.error_code is ErrorCode.TOCTOU_MISMATCH
    assert result.reobserve_required is True


@requires_chromium
async def test_click_by_coordinates_outside_viewport_fails(mock_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s13_spa"))
        dispatcher, engine = await _make_dispatcher(page)
        await engine.observe_page(page=page)

        result = await dispatcher.dispatch(
            ActionType.CLICK, {"x": 99999, "y": 99999, "epoch": engine.epoch}
        )
        await browser.close()

    assert result.success is False
    assert result.error_code is ErrorCode.ELEMENT_NOT_FOUND


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


# --- 사후조건: 도달이 아니라 효과 (실브라우저) ---------------------------------


_INERT_BUTTON = (
    "data:text/html;charset=utf-8,"
    "<h1>무반응</h1><button id=b>아무 일도 안 하는 버튼</button>"
)


@requires_chromium
@pytest.mark.parametrize("attempt_twice", [False, True])
async def test_inert_button_click_is_silent_failure_every_time(mock_server, attempt_twice):
    """onclick 없는 버튼은 몇 번째 클릭이든 실패여야 한다.

    이전에는 1회차가 포커스 이동(BODY -> BUTTON)만으로 성공, 2회차가 실패였다.
    판정이 호출 순서에 좌우되면 안 된다.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(_INERT_BUTTON)
        dispatcher, engine = await _make_dispatcher(page)
        obs = await engine.observe_page(page=page)
        target = next(e for e in obs.elements if e.role == "button")

        result = await dispatcher.dispatch(ActionType.CLICK, {"element_id": target.element_id})
        if attempt_twice:
            obs = await engine.observe_page(page=page)
            target = next(e for e in obs.elements if e.role == "button")
            result = await dispatcher.dispatch(
                ActionType.CLICK, {"element_id": target.element_id}
            )
        await browser.close()

    assert result.success is False
    assert result.error_code is ErrorCode.TIMEOUT


@requires_chromium
async def test_download_counts_as_effect(mock_server, tmp_path):
    """파일 저장은 페이지를 바꾸지 않지만 액션의 효과 그 자체다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context(accept_downloads=True)).new_page()
        await page.goto(mock_server.site_url("s04_download"))
        dispatcher, engine = await _make_dispatcher(page)
        obs = await engine.observe_page(page=page)
        target = next(e for e in obs.elements if "CSV" in e.name)

        result = await dispatcher.dispatch(
            ActionType.DOWNLOAD_FILE,
            {"element_id": target.element_id, "save_dir": str(tmp_path)},
        )
        await browser.close()

    assert result.success is True, result.error_message
    assert result.downloaded_path and result.downloaded_path.endswith(".csv")


@requires_chromium
async def test_open_shadow_click_effect_is_seen(mock_server):
    """shadow 안에서 일어난 변화도 효과다.

    실측 — '주문 확정'을 누르면 shadow 안에 확인 문구가 뜨는데, 상태 캡처가
    light DOM의 노드 수와 body.innerText만 봐서 변화가 0이었다. 예전에는 포커스
    이동(BODY -> DIV#host)이 이 사각지대를 가려 성공으로 보였다.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s06_open_shadow"))
        dispatcher, engine = await _make_dispatcher(page)
        obs = await engine.observe_page(page=page)
        target = next(e for e in obs.elements if "주문 확정" in e.name)

        result = await dispatcher.dispatch(ActionType.CLICK, {"element_id": target.element_id})
        confirmed = await page.evaluate("document.body.getAttribute('data-result')")
        await browser.close()

    assert confirmed == "ok"  # 클릭은 실제로 효과가 있었다
    assert result.success is True, result.error_message


@requires_chromium
async def test_late_effect_within_grace_is_counted(mock_server):
    """클릭 직후가 아니라 조금 늦게 나타나는 효과도 효과다.

    실제 사이트는 클릭 → 요청 → 렌더링으로 효과가 수십~수백 ms 늦게 뜬다.
    포커스가 효과로 인정되던 때는 이 경우도 포커스로 통과했다. 포커스를 뺀 뒤
    대기만 하고 다시 보지 않으면 늦은 효과를 Silent Failure로 판정한다.
    """
    from playwright.async_api import async_playwright

    late_page = (
        "data:text/html;charset=utf-8,"
        "<button id=b onclick=\"setTimeout(()=>{document.getElementById('o')"
        ".textContent='저장됨'},80)\">저장</button><p id=o></p>"
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(late_page)
        dispatcher, engine = await _make_dispatcher(page)
        obs = await engine.observe_page(page=page)
        target = next(e for e in obs.elements if e.role == "button")

        result = await dispatcher.dispatch(ActionType.CLICK, {"element_id": target.element_id})
        await browser.close()

    assert result.success is True, result.error_message
    assert "text_changed" in result.data["signals"]


@requires_chromium
async def test_popup_click_counts_as_effect(mock_server):
    """새 탭을 여는 클릭은 원래 페이지가 그대로여도 성공이다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(mock_server.site_url("s11_popup"))
        dispatcher, engine = await _make_dispatcher(page)
        obs = await engine.observe_page(page=page)
        target = next(e for e in obs.elements if e.role in ("button", "link"))

        result = await dispatcher.dispatch(ActionType.CLICK, {"element_id": target.element_id})
        await browser.close()

    assert result.success is True, result.error_message
    assert any(s.startswith("new_tab") for s in result.data["signals"])


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


# ---------------------------------------------------------------------------
# 키 이름 정규화 (WS-10 실측)
#     Playwright는 'Enter'만 받고 'enter'/'Return'은 Unknown key로 거부한다.
#     LLM은 소문자와 별칭을 자주 쓴다.
# ---------------------------------------------------------------------------

from actions.dispatcher import _normalize_key


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("enter", "Enter"),
        ("Enter", "Enter"),
        ("return", "Enter"),
        ("Return", "Enter"),
        ("esc", "Escape"),
        ("escape", "Escape"),
        ("tab", "Tab"),
        ("up", "ArrowUp"),
        ("arrowdown", "ArrowDown"),
        ("pageup", "PageUp"),
    ],
)
def test_key_aliases_normalize_to_playwright_names(raw, expected):
    assert _normalize_key(raw) == expected


def test_unknown_key_passes_through():
    """단일 문자 등 유효한 입력을 막으면 안 된다."""
    assert _normalize_key("a") == "a"
    assert _normalize_key("F5") == "F5"


def test_combo_keys_normalize_each_part():
    assert _normalize_key("Control+enter") == "Control+Enter"


def test_empty_key_stays_empty():
    assert _normalize_key("") == ""
    assert _normalize_key("   ") == ""


# ---------------------------------------------------------------------------
# 5. staleness 판정 겹별 단독 고정 (WS-35 R2 — 뮤테이션 V1·V2·V3)
# ---------------------------------------------------------------------------


class _ProbePage:
    """STALENESS_CHECK_SCRIPT 의 재조회 결과를 정해 주는 페이지 대역. 호출 여부도 센다."""

    def __init__(self, probe: dict) -> None:
        self.probe, self.calls = probe, 0

    async def evaluate(self, script: str, arg: object = None) -> dict:
        self.calls += 1
        return self.probe


async def test_staleness_epoch_mismatch_alone():
    """V1: 에포크가 다르면 노드가 멀쩡해도(같은 role/name) 재조회 없이 EPOCH_MISMATCH.

    엔진 bump_epoch 의 핸들 비우기·get_handle 에포크 검사와 겹치는 방어라 다른 테스트로는
    이 겹 하나만 빠진 것을 못 잡는다(감사 EQUIVALENT) — 여기서 단독으로 고정한다.
    """
    page = _ProbePage({"connected": True, "role": "button", "name": "로그인"})
    result = await verify_staleness(page, make_handle(epoch=0), current_epoch=1)
    assert result.fresh is False
    assert result.reason is StalenessReason.EPOCH_MISMATCH
    assert result.error_code is ErrorCode.TOCTOU_MISMATCH
    assert "0" in result.detail and "1" in result.detail
    assert page.calls == 0, "에포크가 다르면 페이지를 다시 볼 필요도 없다"
    same = await verify_staleness(page, make_handle(epoch=1), current_epoch=1)
    assert same.fresh and same.reason is StalenessReason.FRESH  # 대조


@pytest.mark.parametrize(
    "probe,reason,observed",
    [
        ({"connected": True, "role": "button", "name": "회원 탈퇴"},
         StalenessReason.NAME_CHANGED, ("button", "회원 탈퇴")),
        ({"connected": True, "role": "checkbox", "name": "로그인"},
         StalenessReason.ROLE_CHANGED, ("checkbox", "로그인")),
        # 둘 다 바뀌면 role 이 먼저 보고된다(판정 순서 고정)
        ({"connected": True, "role": "link", "name": "광고"},
         StalenessReason.ROLE_CHANGED, ("link", "광고")),
    ],
    ids=["name", "role", "both"],
)
async def test_staleness_role_name_change_is_toctou(probe, reason, observed):
    """V2·V3: 같은 css_path 에 연결된 노드라도 role/name 이 다르면 TOCTOU (광고 로테이션 방어)."""
    result = await verify_staleness(_ProbePage(probe), make_handle(), current_epoch=0)
    assert result.fresh is False
    assert result.reason is reason
    assert result.error_code is ErrorCode.TOCTOU_MISMATCH
    assert (result.observed_role, result.observed_name) == observed


async def test_staleness_expected_overrides_handle():
    """호출자의 expected_role/expected_name 이 핸들 값보다 우선한다."""
    page = _ProbePage({"connected": True, "role": "button", "name": "로그인"})
    r = await verify_staleness(page, make_handle(), 0, expected_name="로그아웃")
    assert r.reason is StalenessReason.NAME_CHANGED
    r = await verify_staleness(page, make_handle(), 0, expected_role="link")
    assert r.reason is StalenessReason.ROLE_CHANGED
    r = await verify_staleness(page, make_handle(), 0)
    assert r.fresh and r.reason is StalenessReason.FRESH  # 대조
