"""WS-64 — Dispatcher.dispatch 거부 백스톱(`agent-browser run` 경로: 디스패처 직접 호출). 서버 관문과 같은 판정·같은 모양.

목록은 주입(DispatchContext.manual_sites) — 기본 빈 목록이라 harness·시험은 홈 파일을 읽지 않는다. 로컬 모형만.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from contracts import ActionType, ErrorCode
from security import manual_sites as ms
from security.manual_sites import ManualSites

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "interface"))
from test_run_cli import requires_chromium  # noqa: E402
from ws64_pages import SrtMock  # noqa: E402


async def _dispatcher(pw, url, sites):
    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    browser = await pw.chromium.launch(headless=True)
    page = await (await browser.new_context()).new_page()
    await page.goto(url)
    engine = PerceptionEngine()
    return browser, page, engine, ActionDispatcher(DispatchContext(page=page, engine=engine, manual_sites=sites))


async def _eid(engine, page, name):
    obs = await engine.observe_page(page=page)
    for e in obs.elements:
        if e.name == name:
            return e.element_id
    raise AssertionError([e.name for e in obs.elements])


@requires_chromium
async def test_dispatch_backstop_refuses_listed_and_runs_others():
    from playwright.async_api import async_playwright

    with SrtMock() as m:
        async with async_playwright() as pw:
            browser, page, engine, d = await _dispatcher(pw, m.listed("/srt"), ManualSites.of([m.entry]))
            try:
                eid = await _eid(engine, page, "조회하기")
                r = await d.dispatch(ActionType.CLICK, {"element_id": eid, "epoch": engine.epoch})
                assert r.success is False and r.retry_safe is False
                assert r.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED
                assert r.error_message == ms.MANUAL_SITE_MESSAGE
                assert r.data == ms.handoff_data(ActionType.CLICK, available=False,
                                                 target={"role": "button", "name": "조회하기"})
                assert await page.evaluate("window.__clicks") == 0
                # 차단 9종은 dispatch 입구에서(요소 해석·비밀 치환 전) 거부.
                for action in ms.MANUAL_GATED:
                    out = await d.dispatch(action, {"element_id": eid, "epoch": engine.epoch, "key": "Enter",
                                                    "text": "x", "file_paths": ["/nonexistent"], "save_dir": "/tmp"})
                    assert out.error_code is ErrorCode.HITL_UNATTENDED_BLOCKED, action
                # 허용(관찰·추출)은 그대로.
                ok = await d.dispatch(ActionType.EXTRACT, {"selector": "#dptDt", "attributes": ["value"]})
                assert ok.success, ok.error_message
                # 목록 밖 출처는 실행된다.
                await page.goto(m.unlisted("/plain"))
                eid2 = await _eid(engine, page, "일반 버튼")
                r2 = await d.dispatch(ActionType.CLICK, {"element_id": eid2, "epoch": engine.epoch})
                assert r2.success, r2.error_message
            finally:
                await browser.close()


@requires_chromium
async def test_dispatch_default_context_has_no_list():
    """기본 DispatchContext(목록 미주입)는 목록 사이트 주소에서도 실행한다 — 홈 파일을 읽지 않는다."""
    from playwright.async_api import async_playwright

    with SrtMock() as m:
        async with async_playwright() as pw:
            browser, page, engine, d = await _dispatcher(pw, m.listed("/srt"), None)
            try:
                assert d.ctx.manual_sites is None
                eid = await _eid(engine, page, "조회하기")
                r = await d.dispatch(ActionType.CLICK, {"element_id": eid, "epoch": engine.epoch})
                assert r.success, r.error_message
            finally:
                await browser.close()


def test_run_cli_path_gets_process_default(monkeypatch, tmp_path):
    """agent-browser run(고정 파일 run_cli) — cli.main 이 실행 직전 프로세스 기본 목록을 넣는다."""
    from interface import cli, run_cli

    path = tmp_path / "manual_sites.json"
    ms.add_site("srail.kr", path)
    monkeypatch.setenv(ms.MANUAL_SITES_ENV, str(path))
    seen = {}

    def fake_run(args):
        from actions.dispatcher import DispatchContext

        seen["texts"] = DispatchContext(page=None, engine=None).manual_sites.current().texts()  # type: ignore[arg-type]
        return 0

    monkeypatch.setattr(run_cli, "run", fake_run)
    assert cli.main(["run", "--url", "http://127.0.0.1:1/", "--goal", "x"]) == 0
    assert seen["texts"] == ["srail.kr"]
    assert ms.process_default() is None  # run 이 끝나면 원래대로(같은 프로세스의 다른 코드에 새지 않음)
