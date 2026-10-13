"""WS-48 R2 — 조용한 예외 점검에서 추가한 debug 로그 10곳이 예외 원문을 남기지 않는다.

예외 메시지·traceback·원인 체인(`__cause__`/`__context__`)에는 페이지 텍스트·입력값·URL query·비밀이 담길 수
있다. 추가한 로그는 **고정 문구 + 예외 클래스 이름**만 남기고(`exc_info` 없음), 반환·흐름은 그대로다.

1. AST 하네스: 6개 파일에서 `# WS-48: 흐름 불변` 표식이 붙은 `logger.debug` 핸들러를 정확히 10곳 찾아, 각 핸들러를
   합성 비밀/URL query/페이지 텍스트를 담은 예외(원인 체인 포함)로 실행하고 DEBUG 출력에 표식이 0개인지 본다.
2. 실제 호출: dispatcher `_capture_root`·doc_status `on_response`·site_session `_emit`·robots 신호 부착,
   그리고 실제 Playwright 페이지 getter 가 입력값을 던지는 경우.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import io
import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from test_run_cli import requires_chromium

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "src/actions/dispatcher.py",
    "src/browser/doc_status.py",
    "src/browser/site_session.py",
    "src/interface/session_cli.py",
    "src/recipes/service.py",
    "src/security/robots_signal.py",
)
#: (파일, 둘러싼 함수) — 추가 로그 10곳 전수.
EXPECTED_SITES = [
    ("src/actions/dispatcher.py", "_capture_root"),
    ("src/actions/dispatcher.py", "_top_change_attributable"),
    ("src/browser/doc_status.py", "_on_response"),
    ("src/browser/doc_status.py", "on_response"),
    ("src/browser/doc_status.py", "_resolve_pending"),
    ("src/browser/site_session.py", "_emit"),
    ("src/interface/session_cli.py", "_run_remove"),
    ("src/recipes/service.py", "observe"),
    ("src/security/robots_signal.py", "_populate"),
    ("src/security/robots_signal.py", "attach_robots_signal"),
]
#: 핸들러가 그대로 돌려주는 값(나머지는 return None / pass → None).
HANDLER_RETURNS = {("src/actions/dispatcher.py", "_top_change_attributable"): False}
MARKERS = ("SYNTHETIC_PAGE_TEXT", "SYNTHETIC_QUERY", "SYNTHETIC_SECRET", "SYNTHETIC_CAUSE")


class _SyntheticError(RuntimeError):
    pass


def _synthetic_exception() -> BaseException:
    """원문·원인 체인 모두에 표식이 들어 있는 예외."""
    try:
        raise ValueError("SYNTHETIC_CAUSE password=SYNTHETIC_SECRET")
    except ValueError as cause:
        try:
            raise _SyntheticError(
                "SYNTHETIC_PAGE_TEXT http://mock.invalid/p?token=SYNTHETIC_QUERY SYNTHETIC_SECRET") from cause
        except _SyntheticError as exc:
            return exc


def _handlers() -> Iterator[Tuple[str, str, ast.ExceptHandler, int]]:
    """표식이 붙은 logger.debug 를 담은 except 핸들러와 그것을 직접 둘러싼(가장 안쪽) 함수 이름."""
    for rel in FILES:
        source = (ROOT / rel).read_text()
        lines = source.splitlines()
        tree = ast.parse(source)
        parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            for stmt in node.body:
                if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                        and ast.unparse(stmt.value.func) == "logger.debug"
                        and "WS-48: 흐름 불변" in lines[stmt.lineno - 1]):
                    fn: ast.AST = node
                    while not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        fn = parent[fn]
                    yield rel, fn.name, node, stmt.lineno


SITES = {(rel, fn): (node, line) for rel, fn, node, line in _handlers()}


@contextmanager
def _capture(name: str) -> Iterator[Tuple[logging.Logger, io.StringIO, List[logging.LogRecord]]]:
    """DEBUG 기록을 가로채고, 끝나면 로거 handlers·propagate·level 을 원래대로 돌린다."""
    sink = io.StringIO()
    records: List[logging.LogRecord] = []
    log = logging.getLogger(name)
    saved = (list(log.handlers), log.propagate, log.level)
    handler = logging.StreamHandler(sink)
    # 기본 Formatter 는 exc_info·stack_info 를 그대로 붙인다 — 붙일 것이 없어야 한다.
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    log.handlers = [handler, _Keep()]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    try:
        yield log, sink, records
    finally:
        log.handlers, log.propagate = saved[0], saved[1]
        log.setLevel(saved[2])


def _assert_clean(text: str, records: List[logging.LogRecord], expect_class: str) -> None:
    assert records, "로그가 남지 않음(진단 로그 자체가 사라짐)"
    for marker in MARKERS:
        assert marker not in text, f"{marker} 가 로그에 남음:\n{text}"
    for record in records:
        assert record.exc_info is None and record.exc_text is None and record.stack_info is None
        assert all(isinstance(a, str) for a in (record.args or ())), record.args
    assert expect_class in text, text


def test_exactly_the_ten_added_sites():
    assert sorted(SITES) == sorted(EXPECTED_SITES)
    assert len(list(_handlers())) == 10


@pytest.mark.parametrize("site", EXPECTED_SITES, ids=[f"{f.split('/')[-1]}:{n}" for f, n in EXPECTED_SITES])
def test_handler_logs_only_fixed_text_and_class_name(site):
    """핸들러 AST 를 그대로 떼어 합성 예외로 실행한다(검증자 log_safety_probe 방식)."""
    node, line = SITES[site]
    exc = _synthetic_exception()
    probe = ast.FunctionDef(
        name="probe",
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[ast.Try(body=[ast.Raise(exc=ast.Name(id="_EXC", ctx=ast.Load()), cause=None)],
                      handlers=[copy.deepcopy(node)], orelse=[], finalbody=[])],
        decorator_list=[], returns=None, type_comment=None)
    module = ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[]))
    with _capture(f"ws48.r2.{site[1]}") as (log, sink, records):
        namespace: Dict[str, Any] = {"logger": log, "_EXC": exc}
        exec(compile(module, site[0], "exec"), namespace)  # noqa: S102 - 저장소 소스의 핸들러만 실행
        assert namespace["probe"]() is HANDLER_RETURNS.get(site)  # 원래 return / pass 흐름 유지
    _assert_clean(sink.getvalue(), records, "_SyntheticError")


# --------------------------------------------------------------------------- 실제 호출


async def test_dispatcher_capture_root_real_call():
    from actions.dispatcher import ActionDispatcher

    class Page:
        async def evaluate(self, *args: Any) -> Any:
            raise _synthetic_exception()

    d = ActionDispatcher.__new__(ActionDispatcher)
    d.ctx = SimpleNamespace(root_page=Page())
    with _capture("actions.dispatcher") as (_, sink, records):
        assert await d._capture_root() is None
    _assert_clean(sink.getvalue(), records, "_SyntheticError")


def test_doc_status_on_response_real_call():
    from browser import doc_status

    class Response:
        @property
        def request(self) -> Any:
            raise _synthetic_exception()

    with _capture("browser.doc_status") as (_, sink, records):
        assert doc_status.PageDocumentStatus().on_response(Response()) is None
    _assert_clean(sink.getvalue(), records, "_SyntheticError")


def test_site_session_emit_real_call():
    from browser.site_session import LoginTracer

    def sink_fn(ev: Dict[str, Any]) -> None:
        raise _synthetic_exception()

    tracer = LoginTracer(None, "mock.invalid", [], sink=sink_fn)
    with _capture("browser.site_session") as (_, sink, records):
        assert tracer._emit("probe") is None
    _assert_clean(sink.getvalue(), records, "_SyntheticError")


def test_robots_attach_signal_real_call():
    from contracts import ActionResult, ActionType
    from security import robots_signal

    class Service:
        async def signal(self, *args: Any) -> Any:
            raise _synthetic_exception()

    result = ActionResult(success=True, action=ActionType.NAVIGATE, current_url="http://mock.invalid/",
                          snapshot_epoch=1, tab_id="t", healed=False, reobserve_required=False, retry_safe=True,
                          data={})
    page = SimpleNamespace(context=None, url="http://mock.invalid/?token=SYNTHETIC_QUERY")
    with _capture("security.robots_signal") as (_, sink, records):
        asyncio.run(robots_signal.attach_robots_signal(Service(), result, page, object()))
    assert "robots" not in result.data
    _assert_clean(sink.getvalue(), records, "_SyntheticError")


@requires_chromium
async def test_real_playwright_page_secret_not_logged():
    """검증자 real_log_probe: 페이지 getter 가 password 입력값을 던져도 로그에 남지 않는다."""
    from interface.mcp_server import BrowserMCPServer

    with _capture("actions.dispatcher") as (_, sink, records):
        async with BrowserMCPServer(recipes=False) as server:
            page = server._page
            await page.set_content("<input type=password id=p>")
            await page.locator("#p").fill("VERIFY48_SYNTHETIC_PASSWORD")
            await page.evaluate("Object.defineProperty(document.body,'innerText',"
                                "{get(){throw new Error(document.querySelector('#p').value)}})")
            server._dispatcher.ctx.root_page = page
            assert await server._dispatcher._capture_root() is None
    text = sink.getvalue()
    assert records and "VERIFY48_SYNTHETIC_PASSWORD" not in text, text
    assert all(r.exc_info is None for r in records)
