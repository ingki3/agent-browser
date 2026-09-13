"""Tier-2 SoM 무인 발동 빈도·VLM 지연 측정 (Gate 4 항목 2, PRD §1.5/§3.1).

`python -m harness.tier2_som --runs 50 --mode unattended [--vlm mock|live]`

에이전트 루프(`AgentLoop`, `som_enabled=True`)를 내장 MockServer에 대해 N회
돌리고 두 지표를 낸다:

* `trigger_rate`   — Tier-2가 한 번이라도 발동한 런 / 전체 런 (<= 0.10)
* `p95_latency_ms` — 모든 Tier-2 스텝의 VLM 왕복 지연 p95     (<= 3500)

**태스크 믹스는 고정 상수다 (무작위 아님).** 발동률 임계 0.10은 "Tier-1이
풀 수 있는 페이지에서는 발동하지 않고, 텍스트로 풀 수 없는 페이지에서만
발동한다"를 검증하기 위한 값이다. 믹스를 무작위로 뽑으면 Tier-2 유발
페이지 비율에 따라 발동률이 흔들려 게이트가 실행마다 다른 것을 재고,
사보타주(임계를 1회로 낮추기)로 발동률이 오르는지 확인할 기준선도
사라진다. 따라서 90%는 골든셋 사이트(Tier-1 정답이 있는 페이지), 10%는
Tier-2 유발 페이지 3종(icon-buttons / obfuscated-labels / canvas-ui 순환)
으로 고정한다. `--runs 50`이면 45 + 5, 기대 발동률은 정확히 0.10(경계).

**독립 검증.** `tier2_verified`는 Tier-2 유발 런에서 루프 종료 후
`body[data-result] == "ok"`를 JS로 직접 읽은 수다. 자기보고(`result.success`)
만 세면 "발동은 했지만 엉뚱한 요소를 눌렀다"가 통과한다(유형 D 거짓 통과).
`tier2_verified < tier2_runs`면 발동률·지연이 임계 안이어도 FAIL이다.

**`--vlm mock`(기본, CI).** LLM/VLM 네트워크 호출 없이 루프를 결정론적으로
구동한다. Tier-1 판단은 가짜 클라이언트가 내린다 — 일반 페이지에서는 골든
타깃을 클릭하고 완료, Tier-2 페이지에서는 존재하지 않는 element_id로 2회
실패해 에스컬레이션을 유발한다. 그라운더는 정답 태그(또는 canvas-ui의
장바구니 사각형 중심 좌표)를 돌려주며, 지연은 no-op 전후를 실측한다 —
p95는 ~0ms이지만 0은 아니다. 정확히 0.0이면 지연을 측정하지 않은 결함이다.

**`--vlm live`.** 실제 `OpenRouterClient` + `vision.ground`. OPENROUTER_API_KEY
가 없으면 exit 2. CI에서는 돌리지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from contracts import thresholds
from harness.mock_sites import MockServer
from harness.result import ExitCode, MetricResult, emit, emit_error

METRIC = "tier2_trigger_rate"

#: Tier-1이 풀어야 하는 일반 페이지 — golden_set.GOLDEN_SET 중 클릭이
#: 다이얼로그/지연 삽입 없이 즉시 성공하는 사이트만 골랐다.
#: (s10_dialog·s19_checkout은 confirm 다이얼로그, s14_lazy는 300ms 지연 삽입)
ORDINARY_SITES: Tuple[Tuple[str, str], ...] = (
    ("s01_login", "로그인"),
    ("s02_twofactor", "인증 확인"),
    ("s03_multistep", "다음 단계"),
    ("s09_ad_rotation", "장바구니 담기"),
    ("s13_spa", "설정으로 이동"),
    ("s22_dense", "주문 결제하기"),
)

#: Tier-2 유발 페이지 — (site_id, 정답 CSS 셀렉터 또는 None(좌표 모드), 목표)
TIER2_SITES: Dict[str, Tuple[Optional[str], str]] = {
    "icon-buttons": ("#ic3", "장바구니 열기"),
    "obfuscated-labels": ("#k2", "검색 열기"),
    "canvas-ui": (None, "장바구니 열기"),
}

#: canvas-ui 장바구니 사각형(x 220..380, y 100..200)의 중심. body margin 0,
#: canvas가 문서 원점에 있으므로 뷰포트 좌표와 같다.
CANVAS_CART_POINT: Tuple[int, int] = (300, 150)
#: 오답 좌표 — '검색' 사각형 중심. 클릭하면 data-result='wrong'.
CANVAS_WRONG_POINT: Tuple[int, int] = (100, 150)

#: Tier-2 유발 페이지 비율 (10%). 위 모듈 설명 참조.
TIER2_SHARE = 0.10

#: Tier-2 페이지에서 가짜 Tier-1이 내는 연속 실패 수. 루프의 발동 임계
#: (agent.loop.TIER2_TRIGGER_FAILURES = 2)와 같다.
TIER2_BOGUS_CLICKS = 2

#: 존재하지 않는 element_id — 루프가 디스패치 전에 걸러내며, 이 실패는
#: 구식 참조(ELEMENT_NOT_FOUND)가 아니므로 Tier-2 압력으로 집계된다.
BOGUS_ELEMENT_ID = "@e999"


@dataclass(frozen=True)
class Task:
    site_id: str
    goal: str
    tier2: bool
    #: 일반 페이지: 골든 타깃 이름. Tier-2: None.
    target_name: Optional[str] = None
    #: Tier-2 태그 모드: 정답 CSS 셀렉터. 좌표 모드/일반: None.
    target_selector: Optional[str] = None


def build_task_mix(runs: int) -> List[Task]:
    """고정 믹스: 앞쪽 90%는 일반 페이지 순환, 뒤쪽 10%는 Tier-2 페이지 순환.

    Tier-2 런 수는 `round(runs * 0.10)`이되 runs >= 1이면 최소 1 —
    발동 런과 미발동 런이 각각 1개 이상 있어야 커버리지가 성립한다.
    """
    if runs < 2:
        raise ValueError("runs는 2 이상이어야 발동/미발동 런을 모두 커버할 수 있습니다.")
    n_tier2 = max(1, int(round(runs * TIER2_SHARE)))
    n_ordinary = runs - n_tier2
    tasks: List[Task] = []
    for i in range(n_ordinary):
        site_id, name = ORDINARY_SITES[i % len(ORDINARY_SITES)]
        tasks.append(Task(site_id=site_id, goal=f"'{name}' 버튼을 클릭한다", tier2=False,
                          target_name=name))
    tier2_ids = list(TIER2_SITES)
    for i in range(n_tier2):
        site_id = tier2_ids[i % len(tier2_ids)]
        selector, goal = TIER2_SITES[site_id]
        tasks.append(Task(site_id=site_id, goal=goal, tier2=True, target_selector=selector))
    return tasks


# ---------------------------------------------------------------------------
# mock 모드 — 가짜 Tier-1 LLM 클라이언트와 결정론적 그라운더
# ---------------------------------------------------------------------------


class _MockTier1Client:
    """`OpenRouterClient` 대역. `complete()`가 루프의 관찰 핸들을 보고 판단한다.

    * 처음 `bogus_clicks`회: 존재하지 않는 element_id 클릭 (실패 유발)
    * 그다음: 목표 이름과 일치하는 요소 클릭 (없으면 give_up)
    * 클릭이 한 번 성공하면 finish
    """

    def __init__(self, engine: Any, task: Task, bogus_clicks: int) -> None:
        self._engine = engine
        self._task = task
        self._bogus_left = bogus_clicks
        self._clicked = False
        self.calls = 0

    async def __aenter__(self) -> "_MockTier1Client":
        return self

    async def close(self) -> None:
        pass

    async def complete(self, messages: Any, max_tokens: int = 0) -> Any:
        from llm.client import LLMResponse

        self.calls += 1
        if self._clicked:
            payload: Dict[str, Any] = {"action": "finish", "reason": "mock: 클릭 완료"}
        elif self._bogus_left > 0:
            self._bogus_left -= 1
            payload = {"action": "click", "element_id": BOGUS_ELEMENT_ID, "reason": "mock: 오답"}
        else:
            element_id = self._find_target()
            if element_id is None:
                payload = {"action": "give_up", "reason": "mock: 타깃 없음"}
            else:
                self._clicked = True
                payload = {"action": "click", "element_id": element_id, "reason": "mock: 정답"}
        return LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            model="mock", prompt_tokens=0, completion_tokens=0, cost_usd=0.0,
        )

    def _find_target(self) -> Optional[str]:
        name = self._task.target_name
        if not name:
            return None
        for element_id, handle in self._engine.handles.items():
            if handle.epoch == self._engine.epoch and name in (handle.name or ""):
                return element_id
        return None


async def _matches(page: Any, candidates: Sequence[Any], selector: str) -> List[Any]:
    """후보 중 `selector`가 가리키는 요소와 같은 요소를 고른다 (JS 독립 판정)."""
    out = []
    for c in candidates:
        try:
            same = await page.evaluate(
                "([a, b]) => { const x = document.querySelector(a);"
                " const y = document.querySelector(b); return !!x && x === y; }",
                [c.selector_path, selector],
            )
        except Exception:  # noqa: BLE001
            same = False
        if same:
            out.append(c)
    return out


def _timed(latency_fn: Callable[[], Any]) -> float:
    """no-op 전후 실측 지연(ms). 0이 아니어야 유형 D 신호가 아니다."""
    started = time.perf_counter()
    latency_fn()
    return (time.perf_counter() - started) * 1000


GrounderFactory = Callable[[Any, Task], Callable[..., Any]]


def correct_grounder(page: Any, task: Task) -> Callable[..., Any]:
    """정답 태그(또는 canvas 장바구니 좌표)를 돌려주는 결정론적 그라운더."""
    from vision.grounder import GroundingResult

    async def ground(client, png, candidates, goal, *, failure_context=""):
        # 실제 호출이 하는 직렬화만큼의 no-op — 지연은 실측값이다.
        latency = _timed(lambda: json.dumps([c.tag for c in candidates]))
        if task.tier2 and task.target_selector is None:
            return GroundingResult(point=CANVAS_CART_POINT, reason="mock: 장바구니 중심",
                                   latency_ms=latency)
        if task.target_selector:
            hits = await _matches(page, candidates, task.target_selector)
        else:
            hits = [c for c in candidates if task.target_name and task.target_name in (c.name or "")]
        if not hits:
            return GroundingResult(reason="mock: 정답 후보 없음", latency_ms=latency)
        return GroundingResult(tag=hits[0].tag, reason="mock: 정답", latency_ms=latency)

    return ground


def wrong_grounder(page: Any, task: Task) -> Callable[..., Any]:
    """사보타주용 — 항상 **오답**을 고른다 (자기보고는 성공, 독립 검증은 실패)."""
    from vision.grounder import GroundingResult

    async def ground(client, png, candidates, goal, *, failure_context=""):
        latency = _timed(lambda: None)
        if task.tier2 and task.target_selector is None:
            return GroundingResult(point=CANVAS_WRONG_POINT, reason="sabotage", latency_ms=latency)
        if task.target_selector:
            hits = {c.tag for c in await _matches(page, candidates, task.target_selector)}
        else:
            hits = {c.tag for c in candidates
                    if task.target_name and task.target_name in (c.name or "")}
        for c in candidates:
            if c.tag not in hits:
                return GroundingResult(tag=c.tag, reason="sabotage", latency_ms=latency)
        return GroundingResult(reason="sabotage: 오답 후보 없음", latency_ms=latency)

    return ground


@contextlib.contextmanager
def _patched_client(factory: Callable[..., Any]) -> Iterator[None]:
    """루프가 모듈 전역 `OpenRouterClient`를 직접 생성하므로 그 자리를 바꾼다."""
    from agent import loop as loop_mod

    original = loop_mod.OpenRouterClient
    loop_mod.OpenRouterClient = factory  # type: ignore[assignment]
    try:
        yield
    finally:
        loop_mod.OpenRouterClient = original  # type: ignore[assignment]


def _live_config() -> Optional[Any]:
    """live 모드 설정. 키가 없으면 None."""
    from llm import load_config

    config = load_config()
    return config if config.configured else None


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


async def _run_all(
    tasks: Sequence[Task],
    *,
    vlm: str,
    config: Any,
    ordinary_bogus_clicks: int,
    grounder_factory: Optional[GrounderFactory],
) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from agent import AgentLoop
    from llm import BudgetGuard
    from perception import PerceptionEngine

    records: List[Dict[str, Any]] = []
    with MockServer() as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": thresholds.VIEWPORT_WIDTH,
                          "height": thresholds.VIEWPORT_HEIGHT}
            )
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            try:
                for index, task in enumerate(tasks):
                    await page.goto(server.site_url(task.site_id), wait_until="domcontentloaded")
                    await page.wait_for_timeout(150)
                    engine = PerceptionEngine()
                    dispatcher = ActionDispatcher(
                        DispatchContext(page=page, engine=engine, cdp=cdp, som_enabled=True)
                    )
                    if vlm == "mock":
                        bogus = TIER2_BOGUS_CLICKS if task.tier2 else ordinary_bogus_clicks
                        client = _MockTier1Client(engine, task, bogus)
                        factory = grounder_factory or correct_grounder
                        loop = AgentLoop(
                            page=page, engine=engine, dispatcher=dispatcher, config=None,
                            budget=BudgetGuard(), max_steps=12, som_enabled=True,
                            unattended=True, grounder=factory(page, task),
                        )
                        with _patched_client(lambda *a, **k: client):
                            run = await loop.run(task.goal)
                    else:
                        loop = AgentLoop(
                            page=page, engine=engine, dispatcher=dispatcher, config=config,
                            budget=BudgetGuard(), max_steps=12, som_enabled=True,
                            unattended=True,
                            grounder=grounder_factory(page, task) if grounder_factory else None,
                        )
                        run = await loop.run(task.goal)

                    tier2_steps = [s for s in run.steps if s.note.startswith("tier2")]
                    verified: Optional[bool] = None
                    if task.tier2:
                        try:
                            verified = (
                                await page.evaluate("document.body.getAttribute('data-result')")
                            ) == "ok"
                        except Exception:  # noqa: BLE001
                            verified = False
                    records.append(
                        {
                            "index": index,
                            "site_id": task.site_id,
                            "tier2_page": task.tier2,
                            "tier2_calls": run.tier2_calls,
                            "completed": run.completed,
                            "terminal_reason": run.terminal_reason,
                            "tier2_latencies_ms": [s.vision_latency_ms for s in tier2_steps],
                            "tier2_successes": sum(1 for s in tier2_steps if s.succeeded),
                            "tier2_steps": len(tier2_steps),
                            "verified": verified,
                            "trace": [s.summary() for s in run.steps],
                        }
                    )
            finally:
                await context.close()
                await browser.close()
    return {"records": records}


def _p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


@dataclass(frozen=True)
class Tier2Result(MetricResult):
    """`passed`를 발동률뿐 아니라 지연·독립 검증까지 묶어 판정한다."""

    error: Optional[str] = None
    fail_reason: Optional[str] = None

    @property
    def passed(self) -> bool:  # type: ignore[override]
        if self.error or self.fail_reason:
            return False
        return MetricResult.passed.fget(self)  # type: ignore[attr-defined]


def run_harness(
    *,
    runs: int = 50,
    mode: str = "unattended",
    vlm: str = "mock",
    ordinary_bogus_clicks: int = 0,
    grounder_factory: Optional[GrounderFactory] = None,
) -> Tier2Result:
    """하네스 본체. 테스트에서 인프로세스로 호출한다.

    `ordinary_bogus_clicks`/`grounder_factory`는 사보타주 검증용 훅이다.
    """
    if mode != "unattended":
        return Tier2Result(METRIC, 0.0, 0.0, 0, error=f"지원하지 않는 mode: {mode}")
    if vlm not in ("mock", "live"):
        return Tier2Result(METRIC, 0.0, 0.0, 0, error=f"지원하지 않는 vlm: {vlm}")

    config = None
    if vlm == "live":
        config = _live_config()
        if config is None:
            return Tier2Result(
                METRIC, 0.0, 0.0, 0,
                error="--vlm live에는 OPENROUTER_API_KEY가 필요합니다 (.env 또는 환경변수).",
            )

    try:
        tasks = build_task_mix(runs)
        metrics = asyncio.run(
            _run_all(tasks, vlm=vlm, config=config,
                     ordinary_bogus_clicks=ordinary_bogus_clicks,
                     grounder_factory=grounder_factory)
        )
    except Exception as exc:  # noqa: BLE001
        return Tier2Result(METRIC, 0.0, 0.0, 0, error=f"{type(exc).__name__}: {exc}")

    records = metrics["records"]
    triggered = [r for r in records if r["tier2_calls"] > 0]
    untriggered = [r for r in records if r["tier2_calls"] == 0]
    tier2_runs = [r for r in records if r["tier2_page"]]
    tier2_verified = sum(1 for r in tier2_runs if r["verified"])
    latencies = [ms for r in records for ms in r["tier2_latencies_ms"]]
    tier2_steps = sum(r["tier2_steps"] for r in records)
    tier2_successes = sum(r["tier2_successes"] for r in records)

    trigger_rate = round(len(triggered) / len(records), 4) if records else 0.0
    p95 = round(_p95(latencies), 4)
    latency_ok = p95 <= thresholds.TIER2_VLM_LATENCY_MS_P95

    reason: Optional[str] = None
    error: Optional[str] = None
    if not triggered or not untriggered:
        # 커버리지 미달 — 발동/미발동 경로 중 하나가 아예 측정되지 않았다.
        # 지표는 그대로 싣되(사보타주 판독용) 측정 자체를 무효화한다(exit 2).
        error = (
            f"커버리지 미달: 발동 런 {len(triggered)}, 미발동 런 {len(untriggered)} — "
            "둘 다 1 이상이어야 발동률을 신뢰할 수 있습니다."
        )
    if tier2_verified < len(tier2_runs):
        reason = (
            f"tier2_verified {tier2_verified} < tier2_runs {len(tier2_runs)}: "
            "Tier-2가 발동했지만 독립 검증(body[data-result]=='ok')에 실패한 런이 있습니다."
        )
    elif p95 <= 0.0:
        reason = "p95_latency_ms가 0.0 — 지연이 측정되지 않았습니다(하네스 결함)."
    elif not latency_ok:
        reason = f"p95_latency_ms {p95} > {thresholds.TIER2_VLM_LATENCY_MS_P95}"
    elif trigger_rate > thresholds.TIER2_TRIGGER_RATE:
        reason = f"trigger_rate {trigger_rate} > {thresholds.TIER2_TRIGGER_RATE}"

    return Tier2Result(
        metric=METRIC,
        value=trigger_rate,
        threshold=thresholds.TIER2_TRIGGER_RATE,
        samples=len(records),
        comparison="lte",
        error=error,
        fail_reason=reason,
        extra={
            "trigger_rate": trigger_rate,
            "p95_latency_ms": p95,
            "latency_threshold_ms": thresholds.TIER2_VLM_LATENCY_MS_P95,
            "latency_ok": latency_ok,
            "covered_triggered": len(triggered),
            "covered_untriggered": len(untriggered),
            "tier2_runs": len(tier2_runs),
            "tier2_verified": tier2_verified,
            "tier2_steps": tier2_steps,
            "tier2_success_rate": round(tier2_successes / tier2_steps, 4) if tier2_steps else 0.0,
            "vlm": vlm,
            "mode": mode,
            "reason": reason or error,
            "unexpected_triggers": [
                r["site_id"] for r in triggered if not r["tier2_page"]
            ] or None,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Tier-2 SoM 발동률·VLM 지연 측정")
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--mode", choices=("unattended",), default="unattended")
    parser.add_argument("--vlm", choices=("mock", "live"), default="mock")
    parser.add_argument("--report", type=str, default=None, help="런별 기록 JSON 저장 경로")
    args = parser.parse_args()

    result = run_harness(runs=args.runs, mode=args.mode, vlm=args.vlm)
    if result.error:
        # 실행 불가/커버리지 미달 — 임계값 미달(exit 1)과 구분되는 exit 2.
        if result.extra:
            print(result.to_json(), file=sys.stderr)
        sys.exit(int(emit_error(METRIC, result.error)))

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2)
    if result.fail_reason:
        print(f"[-] {result.fail_reason}", file=sys.stderr)
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
