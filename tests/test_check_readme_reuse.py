"""scripts/check_readme.py 의 통과 수 출처 — 앞 단계 pytest 출력 재사용(v1.12.0 릴리스 게이트 900초 초과)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_readme.py"
_spec = importlib.util.spec_from_file_location("check_readme_under_test", _PATH)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)  # type: ignore[union-attr]

GOOD = "....\n3138 passed, 7 skipped, 4 xfailed, 18 warnings in 965.43s (0:16:05)\n"


@pytest.mark.parametrize("text,expected", [
    (GOOD, 3138),
    ("3 passed in 0.10s\n", 3),
    ("== 12 passed, 1 skipped in 2.00s ==\n", 12),
    # 실패·오류가 섞인 요약은 통과 수로 쓰지 않는다
    ("1 failed, 3137 passed, 7 skipped in 1034.38s (0:17:14)\n", -1),
    ("3137 passed, 2 errors in 10.00s\n", -1),
    # 요약 없음(중간에 끊긴 출력)
    ("....F...\n", -1),
    ("", -1),
    # 여러 요약이 있으면 마지막 것
    ("5 passed in 1.00s\n...\n1 failed, 4 passed in 2.00s\n", -1),
    ("1 failed in 1.00s\n...\n9 passed in 2.00s\n", 9),
])
def test_passed_from_log(text, expected):
    assert cr.passed_from_log(text) == expected


def test_reuses_log_without_running(tmp_path, monkeypatch):
    log = tmp_path / "pytest-full.log"
    log.write_text(GOOD, encoding="utf-8")
    monkeypatch.setenv(cr.PYTEST_LOG_ENV, str(log))
    monkeypatch.setattr(cr, "run", lambda *a, **k: pytest.fail("앞 단계 출력이 있으면 다시 돌리면 안 된다"))
    count, source = cr.pytest_passed_count()
    assert count == 3138 and str(log) in source


def test_missing_log_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(cr.PYTEST_LOG_ENV, str(tmp_path / "nope.log"))
    monkeypatch.setattr(cr, "run", lambda *a, **k: (0, GOOD))
    count, source = cr.pytest_passed_count()
    assert count == -1 and "없음" in source


def test_failed_log_is_not_counted(tmp_path, monkeypatch):
    log = tmp_path / "pytest-full.log"
    log.write_text("1 failed, 3137 passed in 9.0s\n", encoding="utf-8")
    monkeypatch.setenv(cr.PYTEST_LOG_ENV, str(log))
    assert cr.pytest_passed_count()[0] == -1


def test_without_env_runs_suite(monkeypatch):
    monkeypatch.delenv(cr.PYTEST_LOG_ENV, raising=False)
    calls = []
    monkeypatch.setattr(cr, "run", lambda cmd, timeout=300: calls.append((cmd, timeout)) or (0, GOOD))
    assert cr.pytest_passed_count() == (3138, "직접 실행")
    assert calls and calls[0][0] == "uv run pytest tests -q"


def test_ci_release_gate_wires_log():
    ci = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'tee "$RUNNER_TEMP/pytest-full.log"' in ci and "code=${PIPESTATUS[0]}" in ci
    assert "AGENT_BROWSER_PYTEST_LOG: ${{ runner.temp }}/pytest-full.log" in ci
