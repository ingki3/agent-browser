"""액션 실행 성공률 측정 (Gate 3-A 항목 2, >= 92.0%).

`python -m harness.actions_test --tasks 100`

Mock 사이트에서 실제 액션을 실행해 성공률을 측정한다. 성공 판정은
디스패처의 자기 보고가 아니라 **사후조건 검증 결과**에 근거한다.

측정 대상 액션은 Mock 사이트에서 결정론적으로 검증 가능한 것만 포함한다.
외부 네트워크나 타이밍에 의존하는 액션은 플레이키 요인이므로 제외한다.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from contracts import ActionType, thresholds

from harness.mock_sites import MockServer
from harness.result import MetricResult, emit, emit_error


@dataclass
class ActionCase:
    """단일 액션 시나리오."""

    site_id: str
    action: ActionType
    #: 대상 요소를 고르는 조건 (role, name 부분 일치)
    target_role: Optional[str] = None
    target_name: Optional[str] = None
    params: Dict[str, Any] = None  # type: ignore[assignment]
    label: str = ""

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = {}


#: Gate 3-A 측정 시나리오. 각 사이트의 실제 요소를 대상으로 한다.
ACTION_CASES: Tuple[ActionCase, ...] = (
    ActionCase("s01_login", ActionType.CLICK, "button", "로그인", label="폼 제출 버튼 클릭"),
    ActionCase(
        "s01_login",
        ActionType.TYPE_TEXT,
        "textbox",
        "아이디",
        params={"text": "testuser", "clear_before": True},
        label="텍스트 입력",
    ),
    ActionCase("s02_twofactor", ActionType.CLICK, "button", "인증 확인", label="OTP 확인"),
    ActionCase(
        "s02_twofactor",
        ActionType.TYPE_TEXT,
        "textbox",
        "otp",
        params={"text": "123456"},
        label="OTP 코드 입력",
    ),
    ActionCase("s03_multistep", ActionType.CLICK, "button", "다음 단계", label="다단계 폼 진행"),
    ActionCase("s09_ad_rotation", ActionType.CLICK, "button", "장바구니 담기",
               label="광고 로테이션 중 클릭"),
    ActionCase("s13_spa", ActionType.CLICK, "button", "설정으로 이동", label="SPA 라우팅"),
    ActionCase("s14_lazy", ActionType.CLICK, "button", "지연 로딩 버튼", label="지연 로딩 요소"),
    ActionCase("s06_open_shadow", ActionType.CLICK, "button", "주문 확정",
               label="Open Shadow DOM 클릭"),
    ActionCase("s08_infinite", ActionType.SCROLL, params={"direction": "down"},
               label="무한 스크롤"),
    ActionCase("s01_login", ActionType.OBSERVE_PAGE, label="페이지 관찰"),
    ActionCase("s04_download", ActionType.EXTRACT,
               params={"selector": "h1"}, label="텍스트 추출"),
    ActionCase("s11_popup", ActionType.WAIT_FOR,
               params={"condition": "stabilize"}, label="안정화 대기"),
    ActionCase("s01_login", ActionType.TAKE_SCREENSHOT, label="스크린샷"),
    ActionCase("s05_iframe", ActionType.SWITCH_FRAME,
               params={"frame_selector": "#outer"}, label="프레임 전환"),
)


async def _find_element_id(engine, page, role: Optional[str], name: Optional[str]):
    """관찰 결과에서 조건에 맞는 element_id를 찾는다."""
    result = await engine.observe_page(page=page, prune_top_n=50)
    for element in result.elements:
        if role and element.role != role:
            continue
        if name and name.lower() not in element.name.lower():
            continue
        return element.element_id
    return None


async def _run_cases(tasks: int) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    successes = 0
    total = 0
    failures: List[str] = []
    latencies: List[float] = []

    with MockServer() as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": thresholds.VIEWPORT_WIDTH,
                          "height": thresholds.VIEWPORT_HEIGHT}
            )
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)

            idx = 0
            while total < tasks:
                case = ACTION_CASES[idx % len(ACTION_CASES)]
                idx += 1

                engine = PerceptionEngine()
                dispatcher = ActionDispatcher(
                    DispatchContext(page=page, engine=engine, cdp=cdp)
                )

                await page.goto(
                    server.site_url(case.site_id), wait_until="domcontentloaded"
                )
                await page.wait_for_timeout(350)  # 지연 로딩 안정화

                params = dict(case.params)
                if case.target_role or case.target_name:
                    element_id = await _find_element_id(
                        engine, page, case.target_role, case.target_name
                    )
                    if element_id is None:
                        total += 1
                        failures.append(f"{case.label}: 대상 요소 미발견")
                        continue
                    params["element_id"] = element_id
                else:
                    # 요소 비대상 액션도 핸들 등록을 위해 1회 관찰한다.
                    await engine.observe_page(page=page)

                result = await dispatcher.dispatch(case.action, params)
                total += 1
                latencies.append(float(result.data.get("latency_ms", 0.0)))

                if result.success:
                    successes += 1
                else:
                    failures.append(
                        f"{case.label}: {result.error_code.value if result.error_code else '?'}"
                        f" — {result.error_message or ''}"
                    )

            await context.close()
            await browser.close()

    return {
        "success_rate": round(successes / total, 4) if total else 0.0,
        "samples": total,
        "failures": failures,
        "p50_latency_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="액션 실행 성공률 측정")
    parser.add_argument("--tasks", type=int, default=100, help="실행할 액션 수")
    args = parser.parse_args()

    try:
        from actions import ActionDispatcher  # noqa: F401
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "action_success_rate",
                    "액션 디스패처(WS-3 actions/)가 아직 구현되지 않아 측정할 수 없습니다.",
                )
            )
        )

    try:
        metrics = asyncio.run(_run_cases(args.tasks))
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "action_success_rate",
                    "playwright 미설치. 'playwright install chromium' 실행 필요.",
                )
            )
        )
    except Exception as exc:  # noqa: BLE001
        sys.exit(
            int(emit_error("action_success_rate", f"{type(exc).__name__}: {exc}"))
        )

    for failure in metrics["failures"][:10]:
        print(f"[-] {failure}", file=sys.stderr)

    result = MetricResult(
        metric="action_success_rate",
        value=metrics["success_rate"],
        threshold=thresholds.ACTION_SUCCESS_RATE,
        samples=metrics["samples"],
        comparison="gte",
        extra={
            "failure_count": len(metrics["failures"]),
            "failures": metrics["failures"][:10] or None,
            "p50_latency_ms": metrics["p50_latency_ms"],
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
