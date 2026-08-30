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
* 이름 변경 + testid 유지 (i18n 전환)   -> 2단계
* 문구 미세 변경 (A/B 테스트)           -> 3단계
* role/name 동시 변경, 경로 유지        -> 4단계
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from contracts import thresholds

from harness.mock_sites import MockServer
from harness.result import MetricResult, emit, emit_error

#: 사다리 4단계가 모두 검증되어야 지표를 신뢰할 수 있다.
REQUIRED_STAGES = ("role_name", "testid", "text_similarity", "css_path")


@dataclass
class MutationCase:
    """DOM 변형 시나리오."""

    site_id: str
    target_name: str
    #: 관찰 후 실행할 변형 스크립트
    script: str
    label: str
    #: 이 변형이 유발해야 하는 치유 단계 (커버리지 검증용)
    expected_stage: str


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
    MutationCase(
        "s13_spa",
        "설정으로 이동",
        """
        const el = document.getElementById('go-settings');
        el.setAttribute('data-testid', 'settings-nav');
        """,
        "testid 부여 후 이름 변경 (i18n)",
        "testid",
    ),
    # --- 3단계: 문구만 미세하게 변경 (A/B 테스트 모사) ---
    MutationCase(
        "s09_ad_rotation",
        "장바구니 담기",
        "document.getElementById('cart').textContent = '장바구니에 담기';",
        "문구 미세 변경 (A/B 테스트)",
        "text_similarity",
    ),
    MutationCase(
        "s01_login",
        "로그인",
        "document.getElementById('submit').textContent = '로그인하기';",
        "버튼 문구 변경",
        "text_similarity",
    ),
    # --- 4단계: role과 name이 모두 바뀌고 CSS 경로만 남음 ---
    MutationCase(
        "s02_twofactor",
        "인증 확인",
        """
        const el = document.getElementById('verify');
        el.textContent = '전혀 다른 문구입니다';
        el.setAttribute('role', 'menuitem');
        """,
        "role/name 동시 변경 (경로 유지)",
        "css_path",
    ),
)


#: 2단계 유발을 위해 관찰 시점에 testid를 심는 사전 스크립트
_PRE_SCRIPTS: Dict[str, str] = {
    "testid 부여 후 이름 변경 (i18n)": (
        "document.getElementById('go-settings')"
        ".setAttribute('data-testid', 'settings-nav');"
    ),
}

#: 2단계 시나리오는 변형 시 이름을 바꿔 role_name 매칭을 끊는다.
_POST_RENAME: Dict[str, str] = {
    "testid 부여 후 이름 변경 (i18n)": (
        "document.getElementById('go-settings').textContent = 'Go to Settings';"
    ),
}


async def _run(tasks: int) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from actions import HealingCandidate, heal
    from perception import PerceptionEngine

    healed = 0
    total = 0
    failures: List[str] = []
    strategies: Dict[str, int] = {}
    stage_mismatch: List[str] = []

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
                for element in observation.elements:
                    if case.target_name in element.name:
                        target_handle = engine.get_handle(element.element_id)
                        break

                if target_handle is None:
                    total += 1
                    failures.append(f"{case.label}: 타깃 요소를 찾지 못함")
                    continue

                # 2) DOM 변형 -> element_id가 stale이 된다
                await page.evaluate(f"() => {{ {case.script} }}")
                rename = _POST_RENAME.get(case.label)
                if rename:
                    await page.evaluate(f"() => {{ {rename} }}")
                await page.wait_for_timeout(100)

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

                result = heal(target_handle, candidates)
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
        print(f"[!] 단계 불일치: {note}", file=sys.stderr)

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
            "failures": metrics["failures"][:10] or None,
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
