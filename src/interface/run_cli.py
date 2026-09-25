"""`agent-browser run` — 목표 문장 1개를 제품 에이전트(AgentLoop)로 실행한다 (WS-22).

실행부는 harness/agent_eval.py 의 `_run_task` 를 따른다(BudgetGuard 기본값, 루프를
벽시계 wait_for 로 한 번 더 감싼다). 결과는 JSON 한 덩어리 — 표준출력에는 본문
(final_page_text)을 뺀 요약을, `--out` 파일에는 전부를 권한 0600 으로 쓴다.

모드:
  * 기본: headless Chromium(1280x720).
  * `--human`: 실제 창 + 실제 창 크기 + ko-KR 언어(위장 없음).
  * `--user-chrome`: 설치된 Chrome 을 자동화 플래그 없이 전용 프로필로 띄우고
    connect_over_cdp 로 붙는다. 끝나면 우리가 띄운 Chrome 만 닫는다(`--keep-open` 이면 둔다).
  * `--handoff`: 차단·캡차 화면을 만나면 멈추고 사람에게 넘긴다. 사람이 창에서 해결한 뒤
    신호 파일(기본 ~/.agent-browser/handoff.done)을 만들거나, 터미널(표준입력이 TTY)에서
    Enter 를 누르면 — 먼저 오는 쪽으로 — 같은 목표로 이어 간다. 오탐이면 "f"/"force"
    입력 후 Enter(또는 신호 파일 내용 "force")로 강제 계속 — 같은 판정은 다시 넘기지 않는다.

**캡차를 풀거나 차단을 우회하지 않는다.** 막히면 사람에게 넘기고, 사람이 해결했다는
신호도 믿지 않고 화면을 다시 확인한다(AgentLoop._check_challenge).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from contracts.thresholds import MAX_WALL_CLOCK_SECONDS

#: 사람 인계 완료 신호 파일 기본값
DEFAULT_HANDOFF_FILE = Path.home() / ".agent-browser" / "handoff.done"
#: 루프 자체 상한이 먼저 걸리도록 두는 여유(agent_eval 과 같다)
WALL_MARGIN_S = 60
#: 결과에 남기는 최종 페이지 본문 길이
PAGE_TEXT_LIMIT = 6000
_POLL_S = 0.25


def add_parser(sub: Any) -> argparse.ArgumentParser:
    """`run` 서브커맨드 인자를 등록한다(interface/cli.py 에서 부른다)."""
    p = sub.add_parser(
        "run",
        help="목표 1개를 에이전트로 실행하고 결과를 JSON 으로 냅니다.",
    )
    p.add_argument("--url", required=True, metavar="URL", help="시작 주소")
    p.add_argument("--goal", required=True, metavar="GOAL", help="목표 문장")
    p.add_argument("--max-steps", type=int, default=15, metavar="N", help="최대 스텝(기본 15)")
    p.add_argument("--out", default="", metavar="PATH",
                   help="결과 JSON 전체를 쓸 파일(권한 0600 — 페이지 본문 포함)")
    p.add_argument("--headed", action="store_true", help="창 보이는 브라우저(기본 headless)")
    p.add_argument("--human", action="store_true",
                   help="창 보임 + 실제 창 크기 + ko-KR 언어(위장 없음)")
    p.add_argument("--user-chrome", action="store_true",
                   help="설치된 Chrome 을 자동화 플래그 없이 전용 프로필로 띄워 CDP 로 연결")
    p.add_argument("--chrome-profile", default="", metavar="DIR",
                   help="--user-chrome 전용 프로필 폴더(기본 ~/.agent-browser/chrome-profile)")
    p.add_argument("--keep-open", action="store_true",
                   help="--user-chrome: 끝나도 띄운 Chrome 을 닫지 않음")
    p.add_argument("--handoff", action="store_true",
                   help="차단·캡차 화면에서 멈추고 사람에게 넘김(신호 파일 또는 Enter 로 재개)")
    p.add_argument("--handoff-wait", type=float, default=300, metavar="S",
                   help="사람 대기 최대 초(기본 300)")
    p.add_argument("--handoff-file", default=str(DEFAULT_HANDOFF_FILE), metavar="PATH",
                   help=f"사람 해결 신호 파일(기본 {DEFAULT_HANDOFF_FILE})")
    return p


def _load_config() -> Any:
    from llm import load_config

    return load_config()


async def _read_body_text(page: Any) -> str:
    return await page.inner_text("body", timeout=5000)


def write_result(path: Path, record: Dict[str, Any]) -> None:
    """결과 JSON 을 권한 0600 으로 쓴다(페이지 본문이 들어간다). 기존 파일도 0600 으로 조인다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(json.dumps(record, ensure_ascii=False, indent=2))
    finally:
        if fd >= 0:
            os.close(fd)


#: 강제 계속 신호 — 터미널 입력 또는 신호 파일 내용(앞뒤 공백·대소문자 무시)
FORCE_WORDS = frozenset({"f", "force"})
_FILE_READ_LIMIT = 64


def _is_force(text: str) -> bool:
    return text.strip().casefold() in FORCE_WORDS


def _read_signal_file(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(_FILE_READ_LIMIT)
    except OSError:
        return ""


async def wait_for_human_signal(
    done_file: Path, wait_s: float, *, stdin: Any = None
) -> Optional[Tuple[str, bool]]:
    """사람 신호를 기다린다. (via, forced) | None(시간 초과).

    via 는 "file" | "enter". 신호 파일이 생기거나, 표준입력이 TTY 일 때 한 줄이
    들어오면 — 먼저 오는 쪽. 입력 줄·파일 내용이 "f"/"force" 면 forced=True
    (빈 Enter·빈 파일은 "해결했다").
    """
    stdin = sys.stdin if stdin is None else stdin
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    typed: Dict[str, str] = {"line": ""}
    fd = -1
    try:
        if stdin is not None and stdin.isatty():
            fd = stdin.fileno()

            def _on_input() -> None:
                try:
                    typed["line"] = stdin.readline() or ""
                except Exception:  # noqa: BLE001
                    pass
                entered.set()

            loop.add_reader(fd, _on_input)
    except Exception:  # noqa: BLE001 - 입력을 못 보면 파일 신호만 쓴다
        fd = -1
    try:
        deadline = time.monotonic() + wait_s
        while True:
            if Path(done_file).exists():
                return "file", _is_force(_read_signal_file(Path(done_file)))
            if entered.is_set():
                return "enter", _is_force(typed["line"])
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                await asyncio.wait_for(entered.wait(), timeout=min(_POLL_S, left))
            except asyncio.TimeoutError:
                pass
    finally:
        if fd >= 0:
            loop.remove_reader(fd)


async def wait_for_human(done_file: Path, wait_s: float, *, stdin: Any = None) -> Optional[str]:
    """사람 해결 신호를 기다린다. "file" | "enter" | None(시간 초과). (호환용)"""
    got = await wait_for_human_signal(done_file, wait_s, stdin=stdin)
    return got[0] if got is not None else None


def make_handoff(record: Dict[str, Any], wait_s: float, done_file: Path, *, stdin: Any = None):
    """AgentLoop.on_challenge 훅: 안내 출력 → 사람 신호(파일/Enter)를 최대 wait_s 기다린다.

    반환: HandoffOutcome — UNRESOLVED(시간 초과) / RESOLVED(해결, 루프가 재확인) /
    FORCE(오탐이니 그냥 계속 — 같은 판정은 그 실행 동안 다시 넘기지 않는다).
    """
    from agent.loop import HandoffOutcome

    done_file = Path(done_file).expanduser()

    async def on_challenge(challenge: Any) -> HandoffOutcome:
        try:
            done_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            pass
        done_file.unlink(missing_ok=True)
        t0 = time.monotonic()
        print(
            f"\n[사람 확인 필요] {challenge.kind.value} ({challenge.vendor}) — {challenge.reason}\n"
            f"  브라우저 창에서 직접 해결한 뒤 Enter 를 누르거나: touch {done_file}\n"
            f"  오탐(정상 화면)이면 강제 계속: f 또는 force 입력 후 Enter, "
            f"또는: echo force > {done_file}\n"
            f"  최대 {wait_s:.0f}초 기다립니다.",
            file=sys.stderr, flush=True,
        )
        got = await wait_for_human_signal(done_file, wait_s, stdin=stdin)
        done_file.unlink(missing_ok=True)
        via, forced = got if got is not None else (None, False)
        record["handoffs"].append({
            "kind": challenge.kind.value,
            "vendor": challenge.vendor,
            "reason": challenge.reason,
            "resolved_signal": via is not None,
            "via": via,
            "forced": bool(forced),
            "waited_s": round(time.monotonic() - t0, 2),
        })
        record["human_wait_s"] = round(sum(h["waited_s"] for h in record["handoffs"]), 2)
        if via is None:
            return HandoffOutcome.UNRESOLVED
        return HandoffOutcome.FORCE if forced else HandoffOutcome.RESOLVED

    return on_challenge


async def _open_browser(pw: Any, args: argparse.Namespace, record: Dict[str, Any]):
    """(browser, context, page, user_chrome_or_None)."""
    if args.user_chrome:
        from browser.user_chrome import DEFAULT_PROFILE_DIR, connect_user_chrome, launch_user_chrome

        uc = await launch_user_chrome(
            profile_dir=Path(args.chrome_profile) if args.chrome_profile else DEFAULT_PROFILE_DIR,
        )
        record["user_chrome"] = {"port": uc.port, "keep_open": bool(args.keep_open)}
        try:
            browser, context, page = await connect_user_chrome(pw, uc)
        except BaseException:
            uc.close()
            raise
        return browser, context, page, uc
    if args.human:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(no_viewport=True, locale="ko-KR")
    else:
        browser = await pw.chromium.launch(headless=not args.headed)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
    return browser, context, await context.new_page(), None


async def run_goal(args: argparse.Namespace, *, stdin: Any = None) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from agent import AgentLoop
    from llm import BudgetGuard
    from perception import PerceptionEngine

    config = _load_config()
    record: Dict[str, Any] = {
        "goal": args.goal,
        "start_url": args.url,
        "headed": bool(args.headed or args.human or args.user_chrome),
        "human": bool(args.human),
        "handoff": bool(args.handoff),
        "user_chrome_mode": bool(args.user_chrome),
        "challenge": None,
        "handoffs": [],
        "human_wait_s": 0.0,
        "http_status": None,
    }
    started = time.perf_counter()
    async with async_playwright() as pw:
        browser, context, page, uc = await _open_browser(pw, args, record)
        try:
            resp = await page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            record["http_status"] = resp.status if resp is not None else None
            await page.wait_for_timeout(900)
            cdp = await context.new_cdp_session(page)
            engine = PerceptionEngine()
            dispatcher = ActionDispatcher(DispatchContext(page=page, engine=engine, cdp=cdp))
            loop = AgentLoop(
                page=page,
                engine=engine,
                dispatcher=dispatcher,
                config=config,
                budget=BudgetGuard(),
                max_steps=args.max_steps,
                on_challenge=(
                    make_handoff(record, args.handoff_wait, Path(args.handoff_file), stdin=stdin)
                    if args.handoff else None
                ),
            )
            # 루프 자체 상한이 먼저 걸리게 여유를 두고, 사람 대기 시간만큼 늘린다.
            wall = MAX_WALL_CLOCK_SECONDS + WALL_MARGIN_S + (args.handoff_wait if args.handoff else 0)
            try:
                run = await asyncio.wait_for(loop.run(args.goal), timeout=wall)
            except asyncio.TimeoutError:
                run = None
                record.update({"completed": False, "terminal_reason": f"실행 시간 초과({wall:.0f}s)"})
            if run is not None:
                finals = [s for s in run.steps if s.decision.action == "finish"]
                record.update({
                    "final_url": run.final_url,
                    "completed": run.completed,
                    "terminal_reason": run.terminal_reason,
                    "challenge": run.challenge,
                    "step_count": run.step_count,
                    "steps": [s.summary() for s in run.steps],
                    "decided_by": [s.decided_by for s in run.steps],
                    "decided_by_counts": dict(Counter(s.decided_by for s in run.steps)),
                    "usd": run.budget.get("usd"),
                    "tokens": run.budget.get("tokens"),
                    "final_answer": finals[-1].decision.reason if finals else "",
                })
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            record.setdefault("completed", False)
            record.setdefault("terminal_reason", "실행 오류")
        finally:
            # 어떤 경로로 끝나도 결과 키는 모두 채운다(소비자가 KeyError 로 죽지 않게).
            for key, default in (("step_count", 0), ("steps", []), ("decided_by", []),
                                 ("decided_by_counts", {}), ("usd", 0.0), ("tokens", 0),
                                 ("final_answer", "")):
                record.setdefault(key, default)
            try:
                record.setdefault("final_url", page.url)
                text = await _read_body_text(page)
                record["final_page_text"] = text[:PAGE_TEXT_LIMIT]
            except Exception as exc:  # noqa: BLE001
                record.setdefault("final_url", "")
                record["final_page_text"] = f"<읽기 실패 {type(exc).__name__}>"
            record["elapsed_s"] = round(time.perf_counter() - started, 2)
            if uc is not None:
                # CDP 연결만 끊는다(사용자 창의 기본 context 는 닫지 않는다).
                try:
                    await browser.close()
                finally:
                    if not args.keep_open:
                        uc.close()  # 우리가 띄운 Chrome 만 종료
            else:
                await context.close()
                await browser.close()
    return record


def run(args: argparse.Namespace) -> int:
    """`agent-browser run` 진입점. 결과 JSON 을 만들었으면 0."""
    record = asyncio.run(run_goal(args))
    if args.out:
        write_result(Path(args.out).expanduser(), record)
    print(json.dumps({k: v for k, v in record.items() if k != "final_page_text"},
                     ensure_ascii=False, indent=2))
    return 0
