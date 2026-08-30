"""실환경 에이전트 태스크 완수율 측정 (WS-9).

`python -m harness.agent_eval --report artifacts/agent_eval.json`

**판정 게이트가 아니다.** 외부 사이트·네트워크·LLM 과금에 의존하므로
CI 필수 체크로 등록하지 않는다. Mock 환경(Gate 3-B 완수율 100%) 대비
실제 격차를 수치로 드러내는 것이 목적이다.

성공 판정 원칙:
* **에이전트의 `finish` 선언을 신뢰하지 않는다.** 최종 페이지 상태를
  JS로 독립 검증한다. 자기 보고만 믿으면 "했다고 주장하지만 안 한"
  경우를 걸러낼 수 없다.
* 에이전트가 `give_up`했어도 상태가 목표를 만족하면 성공으로 센다.
  실제로 달성했는데 스스로 모르는 경우가 있다.

**커버리지 요건 (AGENTS.md §5 규칙 1)**:
난이도 3단계(easy/medium/hard)가 모두 실행되어야 한다. 쉬운 태스크만
돌리면 완수율이 부풀려져 실제 능력을 오해하게 된다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.real_tasks import (
    DIFFICULTY_LEVELS,
    TASKS,
    RealTask,
    tasks_by_difficulty,
    validate_taskset,
)
from harness.result import MetricResult, emit, emit_error


async def _verify_success(page: Any, task: RealTask) -> tuple:
    """최종 상태를 독립 검증한다. (성공여부, 사유)"""
    try:
        ok = await page.evaluate(f"() => !!({task.success_expr})")
        return bool(ok), "" if ok else "최종 상태가 성공 조건을 만족하지 않음"
    except Exception as exc:  # noqa: BLE001
        return False, f"검증식 평가 실패: {type(exc).__name__}"


async def _run_task(browser: Any, task: RealTask, config: Any) -> Dict[str, Any]:
    from actions import ActionDispatcher, DispatchContext
    from agent import AgentLoop
    from llm import BudgetGuard
    from perception import PerceptionEngine

    context = await browser.new_context(viewport={"width": 1280, "height": 720})
    page = await context.new_page()
    record: Dict[str, Any] = {
        "task_id": task.task_id,
        "difficulty": task.difficulty,
        "capability": task.capability,
        "goal": task.goal,
    }
    started = time.perf_counter()

    try:
        await page.goto(task.url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(900)

        cdp = await context.new_cdp_session(page)
        engine = PerceptionEngine()
        dispatcher = ActionDispatcher(
            DispatchContext(page=page, engine=engine, cdp=cdp)
        )
        loop = AgentLoop(
            page=page,
            engine=engine,
            dispatcher=dispatcher,
            config=config,
            budget=BudgetGuard(),
            max_steps=task.max_steps,
        )
        run = await loop.run(task.goal)

        # 에이전트 선언과 무관하게 최종 상태를 독립 검증한다.
        verified, reason = await _verify_success(page, task)

        record.update(
            {
                "verified_success": verified,
                "agent_claimed": run.completed,
                "steps": run.step_count,
                "action_success_rate": round(run.action_success_rate, 4),
                "terminal_reason": run.terminal_reason[:160],
                "tokens": run.budget.get("tokens", 0),
                "usd": round(run.budget.get("usd", 0.0), 6),
                "final_url": run.final_url[:120],
                "trace": [s.summary() for s in run.steps],
                "failure_reason": "" if verified else reason,
            }
        )
    except Exception as exc:  # noqa: BLE001
        record.update(
            {
                "verified_success": False,
                "agent_claimed": False,
                "steps": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                "failure_reason": "실행 오류",
            }
        )
    finally:
        record["elapsed_s"] = round(time.perf_counter() - started, 2)
        await context.close()

    return record


async def _run_all(
    task_filter: Optional[str], repeat: int
) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from llm import load_config

    config = load_config()
    selected = [
        t
        for t in TASKS
        if not task_filter or task_filter in (t.task_id, t.difficulty)
    ]
    records: List[Dict[str, Any]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for _ in range(repeat):
            for task in selected:
                record = await _run_task(browser, task, config)
                status = "OK  " if record["verified_success"] else "FAIL"
                print(
                    f"[{status}] {record['task_id']:18} "
                    f"{record['difficulty']:6} "
                    f"{record['steps']}스텝 "
                    f"${record.get('usd', 0):.6f} "
                    f"{record.get('failure_reason', '')[:50]}",
                    file=sys.stderr,
                )
                records.append(record)
        await browser.close()

    return {"records": records, "selected": len(selected)}


def _summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(records)
    verified = sum(1 for r in records if r.get("verified_success"))
    claimed = sum(1 for r in records if r.get("agent_claimed"))
    # 자기 보고와 실제가 어긋난 경우 — 가장 중요한 관측값
    false_claims = sum(
        1 for r in records if r.get("agent_claimed") and not r.get("verified_success")
    )
    silent_wins = sum(
        1 for r in records if not r.get("agent_claimed") and r.get("verified_success")
    )

    by_difficulty: Dict[str, Dict[str, Any]] = {}
    for level in DIFFICULTY_LEVELS:
        subset = [r for r in records if r.get("difficulty") == level]
        if subset:
            by_difficulty[level] = {
                "total": len(subset),
                "success": sum(1 for r in subset if r.get("verified_success")),
                "rate": round(
                    sum(1 for r in subset if r.get("verified_success")) / len(subset), 4
                ),
            }

    usd = [r.get("usd", 0.0) for r in records]
    steps = [r.get("steps", 0) for r in records if r.get("steps")]
    elapsed = [r.get("elapsed_s", 0.0) for r in records]

    return {
        "completion_rate": round(verified / total, 4) if total else 0.0,
        "verified_success": verified,
        "agent_claimed": claimed,
        "false_claims": false_claims,
        "silent_wins": silent_wins,
        "total": total,
        "by_difficulty": by_difficulty,
        "total_usd": round(sum(usd), 6),
        "median_usd_per_task": round(statistics.median(usd), 6) if usd else 0.0,
        "median_steps": int(statistics.median(steps)) if steps else 0,
        "median_elapsed_s": round(statistics.median(elapsed), 2) if elapsed else 0.0,
        "failure_reasons": dict(
            Counter(
                r.get("failure_reason", "")
                for r in records
                if not r.get("verified_success")
            )
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="실환경 에이전트 완수율 측정")
    parser.add_argument("--task", default="", help="특정 task_id 또는 난이도만 실행")
    parser.add_argument("--repeat", type=int, default=1, help="반복 횟수")
    parser.add_argument("--report", default="", help="JSON 리포트 저장 경로")
    args = parser.parse_args()

    problems = validate_taskset()
    if problems:
        for p in problems:
            print(f"[-] 태스크셋 결함: {p}", file=sys.stderr)
        sys.exit(
            int(emit_error("agent_completion_rate", f"태스크셋 결함 {len(problems)}건"))
        )

    try:
        from llm import load_config
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "agent_completion_rate",
                    "LLM 어댑터(WS-7)가 없어 측정할 수 없습니다.",
                )
            )
        )

    if not load_config().configured:
        sys.exit(
            int(
                emit_error(
                    "agent_completion_rate",
                    "OPENROUTER_API_KEY가 없습니다. .env를 확인하십시오.",
                )
            )
        )

    try:
        data = asyncio.run(_run_all(args.task or None, args.repeat))
    except Exception as exc:  # noqa: BLE001
        sys.exit(int(emit_error("agent_completion_rate", f"측정 실패: {exc}")))

    records = data["records"]
    if not records:
        sys.exit(
            int(emit_error("agent_completion_rate", "실행된 태스크가 없습니다."))
        )

    summary = _summarize(records)

    # --- 커버리지: 난이도 3단계가 모두 실행되었는가 ---
    covered = set(summary["by_difficulty"])
    if not args.task and covered != set(DIFFICULTY_LEVELS):
        missing = sorted(set(DIFFICULTY_LEVELS) - covered)
        sys.exit(
            int(
                emit_error(
                    "agent_completion_rate",
                    f"난이도 {missing}가 실행되지 않았습니다. 쉬운 태스크만 "
                    "측정하면 완수율이 부풀려집니다.",
                )
            )
        )

    report = {
        "summary": summary,
        "records": records,
        "taskset": {
            "total_defined": len(TASKS),
            "by_difficulty": tasks_by_difficulty(),
        },
    }

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[*] 리포트 저장: {path}", file=sys.stderr)

    result = MetricResult(
        metric="agent_completion_rate",
        value=summary["completion_rate"],
        threshold=0.6,  # PRD §1.5 태스크 완수율 목표 (참고값)
        samples=summary["total"],
        comparison="gte",
        extra={
            "verified_success": summary["verified_success"],
            "agent_claimed": summary["agent_claimed"],
            "false_claims": summary["false_claims"],
            "silent_wins": summary["silent_wins"],
            "by_difficulty": summary["by_difficulty"],
            "difficulties_covered": len(covered),
            "difficulties_required": len(DIFFICULTY_LEVELS),
            "median_usd_per_task": summary["median_usd_per_task"],
            "total_usd": summary["total_usd"],
            "median_steps": summary["median_steps"],
            "median_elapsed_s": summary["median_elapsed_s"],
            "note": "판정 게이트 아님 — Mock 대비 실환경 격차 관측용",
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
