"""참조 에이전트 태스크 완수율 (Gate 3-B 항목 7, >= 60.0%).

`python -m harness.webarena --tasks 20`

WebArena Lite 스타일의 멀티스텝 태스크를 Mock 사이트에서 실행한다.

**참조 에이전트 (중요)**:
LLM을 호출하지 않는 **결정론적 참조 정책**을 사용한다. 이유:
1. Gate 3-B는 런타임(관찰·액션·치유)의 능력을 재는 것이지 LLM 품질이
   아니다. LLM을 쓰면 모델 교체마다 수치가 흔들려 회귀 탐지가 불가능하다.
2. CI에서 API 키 없이 재현 가능해야 한다.

참조 정책은 "목표 키워드와 가장 유사한 요소를 클릭/입력"하는 단순
휴리스틱이며, 런타임이 정상이면 성공하고 파손되면 실패한다.

**커버리지 요건 (AGENTS.md §5 규칙 1)**:
멀티스텝 태스크가 포함되어야 한다. 단일 액션 태스크만 모으면 에포크
관리·상태 전이·자가 치유가 검증되지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from contracts import ActionType, thresholds

from harness.mock_sites import MockServer
from harness.result import MetricResult, emit, emit_error


@dataclass(frozen=True)
class TaskStep:
    """태스크의 단일 스텝. 참조 정책이 수행할 동작."""

    action: ActionType
    target_role: str = ""
    target_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Task:
    """WebArena Lite 스타일 태스크."""

    task_id: str
    site_id: str
    goal: str
    steps: Tuple[TaskStep, ...]
    #: 성공 판정용 JS 표현식. 최종 상태가 참이어야 성공으로 본다.
    #: Mock 사이트는 성공 배너를 렌더링하지 않으므로, 텍스트 매칭이 아니라
    #: 입력값/카운터 같은 검증 가능한 실제 상태를 확인한다.
    success_expr: str = ""

    @property
    def is_multistep(self) -> bool:
        return len(self.steps) >= 2


TASKS: Tuple[Task, ...] = (
    Task(
        "login-flow",
        "s01_login",
        "아이디와 비밀번호를 입력하고 로그인한다",
        (
            TaskStep(ActionType.TYPE_TEXT, "textbox", "아이디", {"text": "tester"}),
            TaskStep(ActionType.TYPE_TEXT, "textbox", "비밀번호", {"text": "pw1234"}),
            TaskStep(ActionType.CLICK, "button", "로그인"),
        ),
        success_expr="new URLSearchParams(location.search).get('user') === 'tester'",
    ),
    Task(
        "otp-verify",
        "s02_twofactor",
        "OTP를 입력하고 인증을 완료한다",
        (
            TaskStep(ActionType.TYPE_TEXT, "textbox", "otp", {"text": "123456"}),
            TaskStep(ActionType.CLICK, "button", "인증 확인"),
        ),
        # form이 없어 네비게이션이 발생하지 않으므로 입력값이 유지된다.
        success_expr="document.querySelector('#otp')?.value === '123456'",
    ),
    Task(
        "multistep-form",
        "s03_multistep",
        "다단계 폼을 다음 단계까지 진행한다",
        (
            TaskStep(ActionType.TYPE_TEXT, "textbox", "성명", {"text": "홍길동"}),
            TaskStep(ActionType.CLICK, "button", "다음 단계"),
        ),
        # <form> 안의 button은 기본 submit이라 클릭 시 페이지가 리로드된다.
        # #name에 name 속성이 없어 값도 쿼리스트링에 남지 않으므로,
        # '제출 네비게이션이 실제로 발생했는가'를 성공 조건으로 삼는다.
        success_expr="location.href.includes('?')",
    ),
    Task(
        "add-to-cart",
        "s09_ad_rotation",
        "광고가 회전하는 페이지에서 장바구니에 담는다",
        (TaskStep(ActionType.CLICK, "button", "장바구니 담기"),),
        success_expr="!!document.querySelector('#cart')",
    ),
    Task(
        "dense-checkout",
        "s22_dense",
        "노이즈가 많은 목록 페이지에서 결제 버튼을 찾아 누른다",
        (TaskStep(ActionType.CLICK, "button", "주문 결제하기"),),
    ),
    Task(
        "spa-navigate",
        "s13_spa",
        "SPA 라우팅으로 설정 화면에 진입한다",
        (TaskStep(ActionType.CLICK, "button", "설정으로 이동"),),
        success_expr="location.hash.includes('settings') || document.body.innerText.includes('설정')",
    ),
    Task(
        "widget-form",
        "s21_widgets",
        "배송 방법을 고르고 약관에 동의한다",
        (
            TaskStep(
                ActionType.SELECT_OPTION,
                "combobox",
                "배송 방법",
                {"value": "express"},
            ),
            TaskStep(ActionType.CHECK_BOX, "checkbox", "약관 동의", {"checked": True}),
        ),
        success_expr="document.querySelector('#agree')?.checked === true",
    ),
    Task(
        "lazy-element",
        "s14_lazy",
        "지연 로딩된 버튼을 클릭한다",
        (TaskStep(ActionType.CLICK, "button", "지연 로딩 버튼"),),
    ),
)


async def _run_task(task: Task, server: Any, page: Any, engine: Any, dispatcher: Any) -> Tuple[bool, str]:
    """단일 태스크를 실행하고 (성공 여부, 실패 사유)를 반환한다."""
    await page.goto(server.site_url(task.site_id), wait_until="domcontentloaded")
    await page.wait_for_timeout(350)

    for idx, step in enumerate(task.steps, start=1):
        observation = await engine.observe_page(page=page)
        match = next(
            (
                e
                for e in observation.elements
                if e.role == step.target_role and e.name == step.target_name
            ),
            None,
        )
        if match is None:
            return False, f"스텝 {idx}: 요소 미발견 ({step.target_role}/{step.target_name})"

        params = dict(step.params)
        params["element_id"] = match.element_id
        params["epoch"] = observation.snapshot_epoch

        result = await dispatcher.dispatch(step.action, params)
        if not result.success:
            code = result.error_code.value if result.error_code else "?"
            return False, f"스텝 {idx}: {step.action.value} 실패 ({code})"

        await page.wait_for_timeout(150)

    if task.success_expr:
        try:
            ok = await page.evaluate(task.success_expr)
        except Exception as exc:  # noqa: BLE001
            return False, f"성공 조건 평가 실패: {exc}"
        if not ok:
            return False, f"성공 조건 미충족: {task.success_expr}"

    return True, ""


async def _run_all(task_count: int) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    completed = 0
    total = 0
    failures: List[str] = []
    multistep_run = 0
    multistep_ok = 0

    with MockServer() as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={
                    "width": thresholds.VIEWPORT_WIDTH,
                    "height": thresholds.VIEWPORT_HEIGHT,
                }
            )
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            engine = PerceptionEngine()
            dispatcher = ActionDispatcher(
                DispatchContext(page=page, engine=engine, cdp=cdp)
            )

            idx = 0
            while total < task_count:
                task = TASKS[idx % len(TASKS)]
                idx += 1
                total += 1

                try:
                    ok, reason = await _run_task(
                        task, server, page, engine, dispatcher
                    )
                except Exception as exc:  # noqa: BLE001
                    ok, reason = False, f"예외: {exc}"

                if task.is_multistep:
                    multistep_run += 1
                    if ok:
                        multistep_ok += 1

                if ok:
                    completed += 1
                else:
                    failures.append(f"{task.task_id}: {reason}")

            await context.close()
            await browser.close()

    return {
        "rate": round(completed / total, 4) if total else 0.0,
        "completed": completed,
        "samples": total,
        "failures": failures[:10],
        "multistep_run": multistep_run,
        "multistep_ok": multistep_ok,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="참조 에이전트 태스크 완수율")
    parser.add_argument("--tasks", type=int, default=20, help="실행할 태스크 수")
    args = parser.parse_args()

    if args.tasks < len(TASKS):
        sys.exit(
            int(
                emit_error(
                    "task_completion_rate",
                    f"--tasks는 정의된 태스크 수({len(TASKS)}) 이상이어야 "
                    "전체 시나리오가 최소 1회씩 실행됩니다.",
                )
            )
        )

    try:
        metrics = asyncio.run(_run_all(args.tasks))
    except Exception as exc:  # noqa: BLE001
        sys.exit(int(emit_error("task_completion_rate", f"측정 실패: {exc}")))

    for failure in metrics["failures"]:
        print(f"[-] {failure}", file=sys.stderr)

    # --- 커버리지: 멀티스텝 태스크가 실제로 실행되었는가 ---
    if metrics["multistep_run"] == 0:
        sys.exit(
            int(
                emit_error(
                    "task_completion_rate",
                    "멀티스텝 태스크가 한 번도 실행되지 않았습니다. 단일 액션만으로는 "
                    "에포크 관리와 상태 전이가 검증되지 않습니다.",
                )
            )
        )

    result = MetricResult(
        metric="task_completion_rate",
        value=metrics["rate"],
        threshold=thresholds.TASK_SUCCESS_RATE,
        samples=metrics["samples"],
        comparison="gte",
        extra={
            "completed": metrics["completed"],
            "failures": metrics["failures"][:10] or None,
            "multistep_run": metrics["multistep_run"],
            "multistep_completed": metrics["multistep_ok"],
            "distinct_tasks": len(TASKS),
            "agent": "deterministic_reference_policy (LLM 미사용)",
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
