"""자가 치유 성공률 측정 (Gate 3-A 항목 3, >= 80.0%).

`python -m harness.self_healing --tasks 100`

관찰 이후 DOM을 의도적으로 변형해 element_id를 stale로 만든 뒤,
자가 치유 사다리가 대체 요소를 찾아내는 비율을 측정한다.

변형 시나리오는 실제 웹에서 흔한 패턴을 재현한다:
* 클래스명 변경 (CSS-in-JS 해시 재생성)
* 요소 재삽입 (React 리렌더)
* 부모 래핑 (레이아웃 변경)
* 형제 노드 삽입 (광고/배너 삽입으로 nth-of-type 밀림)

치유가 불가능해야 마땅한 경우(요소 자체가 사라짐)는 측정에서 제외한다.
그것까지 성공으로 세면 지표가 왜곡된다.
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


@dataclass
class MutationCase:
    """DOM 변형 시나리오."""

    site_id: str
    target_name: str
    #: 관찰 후 실행할 변형 스크립트
    script: str
    label: str


MUTATION_CASES: Tuple[MutationCase, ...] = (
    MutationCase(
        "s01_login",
        "로그인",
        # CSS-in-JS 해시 재생성 모사
        "document.getElementById('submit').className = 'btn-a1b2c3';",
        "클래스명 변경",
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
    ),
    MutationCase(
        "s01_login",
        "로그인",
        # 부모 래핑으로 CSS 경로가 깨지는 경우
        """
        const el = document.getElementById('submit');
        const wrap = document.createElement('div');
        wrap.className = 'wrapper';
        el.parentNode.insertBefore(wrap, el);
        wrap.appendChild(el);
        """,
        "부모 래핑",
    ),
    MutationCase(
        "s09_ad_rotation",
        "장바구니 담기",
        # 형제 삽입으로 nth-of-type이 밀리는 경우
        """
        const el = document.getElementById('cart');
        const ad = document.createElement('button');
        ad.textContent = '광고 배너';
        el.parentNode.insertBefore(ad, el);
        """,
        "형제 노드 삽입",
    ),
    MutationCase(
        "s02_twofactor",
        "인증 확인",
        "document.getElementById('verify').removeAttribute('id');",
        "id 속성 제거",
    ),
    MutationCase(
        "s13_spa",
        "설정으로 이동",
        """
        const el = document.getElementById('go-settings');
        el.setAttribute('data-testid', 'settings-nav');
        el.className = 'x' + Date.now();
        """,
        "testid 추가 + 클래스 변경",
    ),
)


async def _run(tasks: int) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from actions import HealingCandidate, heal
    from perception import PerceptionEngine

    healed = 0
    total = 0
    failures: List[str] = []
    strategies: Dict[str, int] = {}

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
                else:
                    failures.append(f"{case.label}: 치유 실패 ({result.reason})")

            await context.close()
            await browser.close()

    return {
        "rate": round(healed / total, 4) if total else 0.0,
        "samples": total,
        "failures": failures,
        "strategies": strategies,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="자가 치유 성공률 측정")
    parser.add_argument("--tasks", type=int, default=100, help="변형 시나리오 실행 수")
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

    result = MetricResult(
        metric="self_healing_rate",
        value=metrics["rate"],
        threshold=thresholds.SELF_HEALING_RATE,
        samples=metrics["samples"],
        comparison="gte",
        extra={
            "strategy_breakdown": metrics["strategies"],
            "failures": metrics["failures"][:10] or None,
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
