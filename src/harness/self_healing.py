"""자가 치유 성공률 측정 (Gate 3-A 항목 3, >= 80.0%).

`python -m harness.self_healing --tasks 100`

관찰 이후 DOM을 의도적으로 변형해 element_id를 stale로 만든 뒤,
자가 치유 사다리가 대체 요소를 찾아내는 비율을 측정한다.

**사다리 단계 커버리지 강제 (중요)**:
초기 시나리오는 role+name이 그대로 유지되어 전부 1단계에서 해결됐다.
그 상태에서는 2~4단계를 통째로 무력화해도 성공률 1.0이 나와, 하네스가
사다리 파손을 탐지하지 못한다(실제로 사보타주 실험으로 확인).

따라서 각 단계를 **고유하게 유발하는** 변형을 배치하고, 측정 종료 시
`--require-all-stages`(기본 활성)로 4단계가 모두 최소 1회 사용됐는지
검증한다. 한 단계라도 미사용이면 지표를 신뢰할 수 없으므로 실패시킨다.

변형 시나리오는 실제 웹에서 흔한 패턴을 재현한다:
* 클래스명 변경 (CSS-in-JS 해시 재생성) -> 1단계
* 이름 변경 + testid 유지 (i18n 전환)   -> 2단계 (hover, 또는 요소가 옮겨져 사라진 경우 click)
* 문구 미세 변경 (A/B 테스트)           -> 3단계 (요소가 옮겨져 사라짐 — NODE_DETACHED)
* role/name 변경, 경로 유지 + 읽기 액션 -> 4단계 (hover)

**제자리 교체 거부 (WS-37 R1, 음성 케이스)**:
부작용 액션(click 등, `actions.READ_ONLY_ACTIONS` 밖)에서 관찰한 그 자리 요소의 role/name 이
바뀌면(staleness NAME_CHANGED/ROLE_CHANGED) 디스패처는 치유 사다리를 **아예 돌리지 않고**
재관찰을 요구한다(`actions.identity_change_refused`, 사용자 결정 A). 사다리의 어느 단계로
골라도 — testid 유지(2단계), '인증 확인 취소' 같은 비슷한 이름(3단계), 같은 경로(4단계) —
교체된 요소를 누르게 되기 때문이다. 그래서

* 양성 시나리오는 제품 경로에서 실제로 사다리에 닿는 것만 둔다: 읽기 액션이거나 요소가
  사라진(NODE_DETACHED) 경우. 부작용 액션 + 신원 변경 양성은 제품이 거부하므로 그 단계를
  측정하지 못한다 — 측정 중 `identity_change_refused` 가 참이면 단계 불일치로 exit 2(규칙 2).
* 음성 케이스(`expect_refusal`)는 **실제 디스패처**(`ActionDispatcher.dispatch`)로 돌린다:
  이름만/역할만/둘 다/testid 유지/비슷한 이름. 눌리거나 치유되면 wrongful heal 로 exit 2,
  앞단 가드가 아닌 다른 사유(사다리를 돈 흔적 `healing_attempts`)로 거부되면 exit 2(규칙 2),
  다섯 종류 중 하나라도 측정하지 못하면 exit 2(규칙 1).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from contracts import ActionType, ErrorCode, thresholds

from harness.mock_sites import MockServer
from harness.result import MetricResult, emit, emit_error

#: 사다리 4단계가 모두 검증되어야 지표를 신뢰할 수 있다.
REQUIRED_STAGES = ("role_name", "testid", "text_similarity", "css_path")

#: 부작용 액션의 제자리 교체 거부가 다섯 종류 모두 측정되어야 한다.
#: testid·similar 는 앞단 가드가 없으면 2·3단계가 교체된 요소를 고르는 경로다(검증 B1·NB-2).
REQUIRED_REFUSALS = ("name", "role", "both", "testid", "similar")

#: 음성 케이스의 기대 거부 지점(사다리 단계가 아니라 사다리 앞단).
FRONT_GUARD = "front_guard"


@dataclass
class MutationCase:
    """DOM 변형 시나리오."""

    site_id: str
    target_name: str
    #: 관찰 후 실행할 변형 스크립트
    script: str
    label: str
    #: 이 변형이 유발해야 하는 치유 단계 (커버리지 검증용). 음성 케이스는
    #: 거부가 일어나야 하는 단계.
    expected_stage: str
    #: 치유를 요청하는 액션. 4단계 허용 여부가 여기에 달렸다.
    action: ActionType = ActionType.CLICK
    #: 음성 케이스: 치유를 **거부**해야 정답. 값은 바뀐 신원 종류(name/role/both).
    expect_refusal: str = ""


MUTATION_CASES: Tuple[MutationCase, ...] = (
    # --- 1단계: role+name이 유지되므로 즉시 매칭 ---
    MutationCase(
        "s01_login",
        "로그인",
        # CSS-in-JS 해시 재생성 모사
        "document.getElementById('submit').className = 'btn-a1b2c3';",
        "클래스명 변경",
        "role_name",
    ),
    MutationCase(
        "s01_login",
        "로그인",
        # React 리렌더 모사: 같은 요소를 제거 후 재삽입
        """
        const el = document.getElementById('submit');
        const parent = el.parentNode;
        const clone = el.cloneNode(true);
        el.remove();
        parent.appendChild(clone);
        """,
        "요소 재삽입",
        "role_name",
    ),
    # --- 2단계: 이름이 바뀌고 testid만 남음 (i18n 언어 전환 모사) ---
    #     부작용 액션 + 제자리 이름 변경은 앞단 가드가 거부하므로(음성 'testid' 참고)
    #     양성은 읽기 액션(hover)이거나, 요소가 옮겨져 원래 자리에서 사라진 경우(click).
    MutationCase(
        "s13_spa",
        "설정으로 이동",
        "document.getElementById('go-settings').textContent = 'Go to Settings';",
        "testid 유지 이름 변경 (i18n, hover)",
        "testid",
        ActionType.HOVER,
    ),
    MutationCase(
        "s13_spa",
        "설정으로 이동",
        """
        const el = document.getElementById('go-settings');
        el.textContent = 'Go to Settings';
        el.removeAttribute('id');
        const wrap = document.createElement('section');
        el.parentNode.insertBefore(wrap, el);
        wrap.appendChild(el);
        """,
        "testid 유지 이름 변경 + 이동 (i18n 재렌더, click)",
        "testid",
    ),
    # --- 3단계: 문구만 미세하게 변경 (A/B 테스트 모사) ---
    #     CSS 경로도 함께 바꿔야 4단계로 새지 않고 3단계에서 해결된다.
    #     원래 경로의 요소가 사라지므로(NODE_DETACHED) 부작용 액션도 사다리를 돈다.
    MutationCase(
        "s09_ad_rotation",
        "장바구니 담기",
        """
        const el = document.getElementById('cart');
        el.textContent = '장바구니에 담기';
        el.removeAttribute('id');
        const wrap = document.createElement('section');
        el.parentNode.insertBefore(wrap, el);
        wrap.appendChild(el);
        """,
        "문구 미세 변경 (A/B 테스트)",
        "text_similarity",
    ),
    MutationCase(
        "s01_login",
        "로그인",
        """
        const el = document.getElementById('submit');
        el.textContent = '로그인하기';
        el.removeAttribute('id');
        const wrap = document.createElement('section');
        el.parentNode.insertBefore(wrap, el);
        wrap.appendChild(el);
        """,
        "버튼 문구 변경 (짧은 라벨 접미 확장)",
        "text_similarity",
    ),
    # --- 4단계: role과 name이 바뀌고 CSS 경로만 남음 — 읽기 액션 ---
    #     (검증 NB-5: extract 는 디스패처가 element_id 를 받지 않아 제품 경로에서 치유에 닿지
    #     않는다 — 제품에서 경로 치유에 닿는 읽기 액션인 hover 로 둘 다 유발한다.)
    MutationCase(
        "s02_twofactor",
        "인증 확인",
        """
        const el = document.getElementById('verify');
        el.textContent = '전혀 다른 문구입니다';
        el.setAttribute('role', 'menuitem');
        """,
        "role/name 동시 변경 (경로 유지, hover)",
        "css_path",
        ActionType.HOVER,
    ),
    MutationCase(
        "s02_twofactor",
        "인증 확인",
        "document.getElementById('verify').textContent = '전혀 다른 문구입니다';",
        "name 만 변경 (경로 유지, hover)",
        "css_path",
        ActionType.HOVER,
    ),
    # --- 음성: 부작용 액션 + 제자리 교체 → 앞단 가드 거부가 정답 (WS-37 R1) ---
    MutationCase(
        "s02_twofactor",
        "인증 확인",
        "document.getElementById('verify').textContent = '회원 탈퇴';",
        "제자리 교체 name 만 (click, 거부)",
        FRONT_GUARD,
        ActionType.CLICK,
        expect_refusal="name",
    ),
    MutationCase(
        "s02_twofactor",
        "인증 확인",
        "document.getElementById('verify').setAttribute('role', 'checkbox');",
        "제자리 교체 role 만 (click, 거부)",
        FRONT_GUARD,
        ActionType.CLICK,
        expect_refusal="role",
    ),
    MutationCase(
        "s02_twofactor",
        "인증 확인",
        """
        const el = document.getElementById('verify');
        el.textContent = '광고: 대출 상담';
        el.setAttribute('role', 'link');
        """,
        "제자리 교체 role/name 동시 (click, 거부)",
        FRONT_GUARD,
        ActionType.CLICK,
        expect_refusal="both",
    ),
    # 검증 B1: testid 가 남은 채 같은 자리 요소가 바뀌면 2단계가 그 요소를 골랐다.
    MutationCase(
        "s13_spa",
        "설정으로 이동",
        "document.getElementById('go-settings').textContent = '회원 탈퇴';",
        "제자리 교체 testid 유지 (click, 거부)",
        FRONT_GUARD,
        ActionType.CLICK,
        expect_refusal="testid",
    ),
    # 검증 NB-2: 의미가 반전된 비슷한 이름(규칙 3)은 3단계가 그 요소를 골랐다.
    MutationCase(
        "s02_twofactor",
        "인증 확인",
        "document.getElementById('verify').textContent = '인증 확인 취소';",
        "제자리 교체 비슷한 이름 '인증 확인 취소' (click, 거부)",
        FRONT_GUARD,
        ActionType.CLICK,
        expect_refusal="similar",
    ),
)


#: 2단계 유발을 위해 관찰 시점에 testid를 심는 사전 스크립트 (s13 의 모든 케이스)
_TESTID_PRE = (
    "document.getElementById('go-settings').setAttribute('data-testid', 'settings-nav');"
)
_PRE_SCRIPTS: Dict[str, str] = {
    c.label: _TESTID_PRE for c in MUTATION_CASES if c.site_id == "s13_spa"
}

#: 디스패처 음성 케이스에서 '눌렸는가'를 보는 페이지 상태(클릭 핸들러가 남기는 흔적).
_PAGE_MARK_JS = "() => [location.href, document.body.getAttribute('data-result')]"


async def _run(tasks: int) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from actions import (
        ActionDispatcher,
        DispatchContext,
        HealingCandidate,
        heal,
        identity_change_refused,
        verify_staleness,
    )
    from perception import PerceptionEngine

    healed = 0
    total = 0
    failures: List[str] = []
    strategies: Dict[str, int] = {}
    stage_mismatch: List[str] = []
    refusals: Dict[str, int] = {}
    wrongful_heals: List[str] = []
    guard_total = 0

    with MockServer() as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": thresholds.VIEWPORT_WIDTH,
                          "height": thresholds.VIEWPORT_HEIGHT}
            )
            page = await context.new_page()

            idx = 0
            while total < tasks:
                case = MUTATION_CASES[idx % len(MUTATION_CASES)]
                idx += 1

                engine = PerceptionEngine()
                await page.goto(
                    server.site_url(case.site_id), wait_until="domcontentloaded"
                )
                await page.wait_for_timeout(200)

                # 관찰 이전에 심어야 하는 속성 (testid 등)
                pre = _PRE_SCRIPTS.get(case.label)
                if pre:
                    await page.evaluate(f"() => {{ {pre} }}")

                # 1) 최초 관찰 -> 타깃 핸들 확보
                observation = await engine.observe_page(page=page, prune_top_n=50)
                target_handle = None
                target_id = ""
                for element in observation.elements:
                    if case.target_name in element.name:
                        target_handle = engine.get_handle(element.element_id)
                        target_id = element.element_id
                        break

                if target_handle is None:
                    if case.expect_refusal:
                        guard_total += 1
                        note = f"{case.label}: 타깃 요소를 찾지 못해 거부를 측정하지 못함"
                        if note not in stage_mismatch:
                            stage_mismatch.append(note)
                    else:
                        total += 1
                        failures.append(f"{case.label}: 타깃 요소를 찾지 못함")
                    continue

                # 2) DOM 변형 -> element_id가 stale이 된다
                await page.evaluate(f"() => {{ {case.script} }}")
                await page.wait_for_timeout(100)

                if case.expect_refusal:
                    # 음성 케이스: 성공률 표본이 아니다. 실제 디스패처(제품 경로)로 돌려
                    # 앞단 가드가 사다리 전에 거부하는지 본다.
                    guard_total += 1
                    mark_before = await page.evaluate(_PAGE_MARK_JS)
                    dispatcher = ActionDispatcher(DispatchContext(page=page, engine=engine))
                    r = await dispatcher.dispatch(case.action, {"element_id": target_id})
                    mark_after = await page.evaluate(_PAGE_MARK_JS)
                    if r.success or r.healed or mark_after != mark_before:
                        wrongful_heals.append(
                            f"{case.label}: {case.action.value} 인데 치유·실행됨 "
                            f"(success={r.success}, healed={r.healed}, "
                            f"page {mark_before} -> {mark_after})"
                        )
                    elif (
                        r.error_code is ErrorCode.TOCTOU_MISMATCH
                        and r.reobserve_required
                        and "element_changed" in r.data
                        and "healing_attempts" not in r.data
                    ):
                        refusals[case.expect_refusal] = refusals.get(case.expect_refusal, 0) + 1
                    else:
                        note = (
                            f"{case.label}: 앞단 가드 거부 기대, 다른 사유로 실패 "
                            f"({r.error_code.value if r.error_code else None}, "
                            f"attempts={r.data.get('healing_attempts')})"
                        )
                        if note not in stage_mismatch:
                            stage_mismatch.append(note)
                    continue

                # 양성: 제품 경로에서 이 시나리오가 사다리에 닿는가(규칙 2). 부작용 액션 +
                # 신원 변경이면 디스패처가 앞단에서 거부하므로 그 단계를 측정하지 못한다.
                staleness = await verify_staleness(page, target_handle, engine.epoch)
                if identity_change_refused(staleness.reason, case.action):
                    note = (
                        f"{case.label}: {case.action.value} + {staleness.reason.value} 는 "
                        f"제품 경로에서 앞단 가드가 거부 — {case.expected_stage} 미측정"
                    )
                    if note not in stage_mismatch:
                        stage_mismatch.append(note)

                # 3) 재관찰 후 치유 사다리 가동
                fresh = await engine.observe_page(page=page, prune_top_n=50)
                candidates = []
                for element in fresh.elements:
                    h = engine.get_handle(element.element_id)
                    candidates.append(
                        HealingCandidate(
                            element_id=element.element_id,
                            role=element.role,
                            name=element.name,
                            css_path=h.css_path if h else "",
                            testid=h.testid if h else None,
                            is_shadow=element.is_shadow,
                        )
                    )

                result = heal(target_handle, candidates, action=case.action)
                total += 1
                if result.healed and result.strategy:
                    healed += 1
                    key = result.strategy.value
                    strategies[key] = strategies.get(key, 0) + 1
                    if key != case.expected_stage:
                        note = f"{case.label}: {case.expected_stage} 기대, {key} 사용"
                        if note not in stage_mismatch:
                            stage_mismatch.append(note)
                else:
                    failures.append(f"{case.label}: 치유 실패 ({result.reason})")

            await context.close()
            await browser.close()

    return {
        "rate": round(healed / total, 4) if total else 0.0,
        "samples": total,
        "failures": failures,
        "strategies": strategies,
        "stage_mismatch": stage_mismatch,
        "refusals": refusals,
        "wrongful_heals": wrongful_heals,
        "guard_total": guard_total,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="자가 치유 성공률 측정")
    parser.add_argument("--tasks", type=int, default=100, help="변형 시나리오 실행 수")
    parser.add_argument(
        "--allow-partial-stages",
        action="store_true",
        help="사다리 단계 커버리지 검사를 생략한다 (디버깅 전용).",
    )
    args = parser.parse_args()

    try:
        from actions import heal  # noqa: F401
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "self_healing_rate",
                    "자가 치유 모듈(WS-3 actions/)이 아직 구현되지 않아 측정할 수 없습니다.",
                )
            )
        )

    try:
        metrics = asyncio.run(_run(args.tasks))
    except ImportError:
        sys.exit(
            int(emit_error("self_healing_rate", "playwright 미설치."))
        )
    except Exception as exc:  # noqa: BLE001
        sys.exit(int(emit_error("self_healing_rate", f"{type(exc).__name__}: {exc}")))

    for failure in metrics["failures"][:10]:
        print(f"[-] {failure}", file=sys.stderr)
    for note in metrics["stage_mismatch"]:
        print(f"[-] 단계 불일치: {note}", file=sys.stderr)

    # --- 사다리 커버리지 검증 -------------------------------------------
    # 한 단계라도 사용되지 않았다면 그 단계가 파손돼 있어도 성공률은
    # 1.0으로 나온다. 지표를 신뢰할 수 없으므로 실패시킨다.
    used = set(metrics["strategies"])
    unused = [s for s in REQUIRED_STAGES if s not in used]
    if unused and not args.allow_partial_stages:
        for stage in unused:
            print(f"[-] 미검증 치유 단계: {stage}", file=sys.stderr)
        sys.exit(
            int(
                emit_error(
                    "self_healing_rate",
                    f"치유 사다리 {len(unused)}개 단계({', '.join(unused)})가 "
                    "한 번도 사용되지 않았습니다. 해당 단계가 파손돼도 지표가 "
                    "1.0을 보고하므로 측정을 신뢰할 수 없습니다.",
                )
            )
        )

    # --- 제자리 교체 거부 검증 (WS-37) -----------------------------------
    # 부작용 액션으로 같은 자리의 다른 요소를 '치유'하면 그 요소를 누르게 된다.
    for note in metrics["wrongful_heals"]:
        print(f"[-] 잘못된 치유: {note}", file=sys.stderr)
    if metrics["wrongful_heals"]:
        sys.exit(
            int(
                emit_error(
                    "self_healing_rate",
                    f"부작용 액션의 제자리 교체 {len(metrics['wrongful_heals'])}건을 "
                    "치유했습니다(거부가 정답). 교체된 요소를 누르게 됩니다.",
                )
            )
        )
    unrefused = [k for k in REQUIRED_REFUSALS if k not in metrics["refusals"]]
    if unrefused and not args.allow_partial_stages:
        sys.exit(
            int(
                emit_error(
                    "self_healing_rate",
                    f"제자리 교체 거부 {', '.join(unrefused)} 종류가 측정되지 않았습니다.",
                )
            )
        )

    # --- 단계 정합성 검증 -------------------------------------------------
    # 시나리오가 의도한 단계가 아닌 다른 단계로 해결되면, 그 단계는
    # '측정된 것처럼 보이지만' 실제로는 검증되지 않은 상태다.
    # (예: 3단계를 노린 시나리오가 4단계로 해결되면 3단계는 미검증)
    if metrics["stage_mismatch"] and not args.allow_partial_stages:
        sys.exit(
            int(
                emit_error(
                    "self_healing_rate",
                    f"시나리오 {len(metrics['stage_mismatch'])}건이 의도한 단계가 "
                    "아닌 다른 단계로 해결됐습니다. 해당 단계는 실제로 검증되지 "
                    "않았으므로 시나리오를 교정해야 합니다.",
                )
            )
        )

    result = MetricResult(
        metric="self_healing_rate",
        value=metrics["rate"],
        threshold=thresholds.SELF_HEALING_RATE,
        samples=metrics["samples"],
        comparison="gte",
        extra={
            "strategy_breakdown": metrics["strategies"],
            "stages_covered": sorted(used),
            "stages_required": list(REQUIRED_STAGES),
            "stage_mismatch": metrics["stage_mismatch"] or None,
            "guard_cases": metrics["guard_total"],
            "guard_refusals": metrics["refusals"],
            "refusals_required": list(REQUIRED_REFUSALS),
            "wrongful_heals": len(metrics["wrongful_heals"]),
            "failures": metrics["failures"][:10] or None,
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
