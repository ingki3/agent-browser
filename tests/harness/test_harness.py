"""WS-6 하네스 자체 검증 테스트 (Gate 1 항목 5).

하네스는 이후 모든 게이트의 판정 근거이므로, 하네스 자신이 올바른지
먼저 증명해야 한다. 특히 "임계값 미달 시 exit 1"이 지켜지지 않으면
모든 게이트가 무력화된다.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

import pytest

from contracts import thresholds
from harness import (
    GOLDEN_SET,
    MOCK_SITES,
    SITE_INDEX,
    ExitCode,
    MetricResult,
    MockServer,
    Scenario,
    covered_scenarios,
    missing_scenarios,
    validate_golden_set,
)
from harness.recall import reference_extractor
from harness.result import REQUIRED_KEYS


# ---------------------------------------------------------------------------
# 1. 출력 규약 (가장 중요)
# ---------------------------------------------------------------------------


def test_metric_result_emits_required_keys():
    payload = MetricResult(
        metric="m", value=1.0, threshold=1.0, samples=1
    ).to_dict()
    for key in REQUIRED_KEYS:
        assert key in payload


def test_metric_result_is_single_json_line():
    line = MetricResult(metric="m", value=0.5, threshold=1.0, samples=3).to_json()
    assert "\n" not in line
    assert json.loads(line)["metric"] == "m"


def test_gte_comparison_semantics():
    assert MetricResult(metric="m", value=0.95, threshold=0.95, samples=1).passed
    assert not MetricResult(metric="m", value=0.94, threshold=0.95, samples=1).passed


def test_lte_comparison_semantics():
    assert MetricResult(
        metric="m", value=0.02, threshold=0.02, samples=1, comparison="lte"
    ).passed
    assert not MetricResult(
        metric="m", value=0.03, threshold=0.02, samples=1, comparison="lte"
    ).passed


def test_extra_cannot_override_contract_keys():
    """extra가 passed를 덮어쓰면 게이트를 속일 수 있다."""
    payload = MetricResult(
        metric="m",
        value=0.0,
        threshold=1.0,
        samples=1,
        extra={"passed": True, "note": "ok"},
    ).to_dict()
    assert payload["passed"] is False
    assert payload["note"] == "ok"


def test_exit_codes_are_distinct():
    assert ExitCode.PASSED == 0
    assert ExitCode.THRESHOLD_NOT_MET == 1
    assert ExitCode.EXECUTION_ERROR == 2


# ---------------------------------------------------------------------------
# 2. Mock 사이트 정의
# ---------------------------------------------------------------------------


def test_exactly_20_mock_sites():
    assert len(MOCK_SITES) == 20


def test_site_ids_are_unique():
    ids = [s.site_id for s in MOCK_SITES]
    assert len(set(ids)) == len(ids)


def test_all_13_scenarios_are_covered():
    """빈 HTML 20개로 게이트를 통과하는 것을 방지한다."""
    assert len(Scenario) == 13
    assert missing_scenarios() == []


def test_each_site_declares_at_least_one_scenario():
    for site in MOCK_SITES:
        assert site.scenarios, f"{site.site_id}에 시나리오 선언이 없음"


def test_high_risk_scenarios_have_dedicated_sites():
    """Closed Shadow DOM과 광고 로테이션은 반드시 존재해야 한다."""
    coverage = covered_scenarios()
    assert coverage[Scenario.CLOSED_SHADOW_DOM]
    assert coverage[Scenario.AD_ROTATION]


# ---------------------------------------------------------------------------
# 3. Mock 서버 동작
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server():
    with MockServer() as srv:
        yield srv


def _get(url: str) -> tuple[int, str]:
    import urllib.error

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def test_every_site_serves_200(server):
    for site in MOCK_SITES:
        status, body = _get(server.site_url(site.site_id))
        assert status == 200, f"{site.site_id} -> {status}"
        assert site.title in body


def test_session_expiry_returns_401(server):
    status, _ = _get(f"{server.site_url('s12_session_expiry')}/protected")
    assert status == 401


def test_csv_download_serves_csv(server):
    status, body = _get(f"{server.site_url('s04_download')}/report.csv")
    assert status == 200
    assert "id,name,amount" in body


def test_nested_iframe_children_are_served(server):
    for path in ("/s05_iframe/outer", "/s05_iframe/inner"):
        status, _ = _get(f"{server.base_url}{path}")
        assert status == 200


def test_unknown_path_returns_404(server):
    status, _ = _get(f"{server.base_url}/no_such_site")
    assert status == 404


# ---------------------------------------------------------------------------
# 4. 골든셋
# ---------------------------------------------------------------------------


def test_golden_set_has_10_cases():
    assert len(GOLDEN_SET) == 10


def test_golden_set_matches_site_definitions():
    assert validate_golden_set() == []


def test_golden_targets_exist_in_site_html():
    for case in GOLDEN_SET:
        site = SITE_INDEX[case.site_id]
        assert case.expected_name in site.html


def test_reference_extractor_finds_every_golden_target():
    """참조 추출기가 골든 정답을 모두 찾아야 recall 1.0이 성립한다."""
    for case in GOLDEN_SET:
        site = SITE_INDEX[case.site_id]
        found = reference_extractor(site.html, thresholds.DEFAULT_PRUNE_TOP_N)
        assert (case.expected_role, case.expected_name) in found, case.site_id


# ---------------------------------------------------------------------------
# 5. 실행 모듈 종료 코드 (게이트 판정의 핵심)
# ---------------------------------------------------------------------------


def _run_module(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", *args], capture_output=True, text=True
    )


def test_contract_selftest_passes():
    proc = _run_module("harness.contract_selftest")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout.strip())["passed"] is True


def test_selfcheck_passes_with_20_sites():
    proc = _run_module("harness.selfcheck", "--mock-sites", "20")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["passed"] is True
    assert payload["scenarios_covered"] == 13


def test_selfcheck_fails_when_expecting_wrong_count():
    """개수가 어긋나면 반드시 실패해야 한다."""
    proc = _run_module("harness.selfcheck", "--mock-sites", "25")
    assert proc.returncode == 1
    assert json.loads(proc.stdout.strip())["passed"] is False


def test_golden_recall_is_exactly_one():
    proc = _run_module("harness.recall", "--golden")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["value"] == 1.0
    assert payload["passed"] is True


def _chromium_available() -> bool:
    """Chromium 바이너리 가용 여부. 서브프로세스 하네스 검증에 사용한다."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Chromium 바이너리 없음"
)


@requires_chromium
def test_recall_measures_engine_now_that_perception_exists():
    """WS-2 구현 이후에는 실제 Recall 측정이 이뤄져야 한다.

    WS-6 단계에서는 perception 모듈이 없어 exit 2(측정 불가)였고,
    WS-2 완료로 exit 0(실측)으로 전환되는 것이 정상이다.
    실브라우저가 필요하므로 Chromium 부재 환경에서는 skip한다.
    """
    proc = _run_module("harness.recall", "--pages", "10", "--top-n", "20")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["metric"] == "element_recall_at_20"
    assert payload["samples"] == 10
    assert "error" not in payload
    # 예산 판정 필드가 함께 보고되어야 한다.
    assert "p50_tokens" in payload
    assert "p50_latency_ms" in payload


def test_recall_reports_error_when_browser_unavailable():
    """브라우저가 없으면 exit 2(측정 불가)로 명확히 구분되어야 한다.

    0.0을 반환해 '측정했으나 미달'로 위장하면 게이트 판정이 왜곡된다.
    """
    if _chromium_available():
        pytest.skip("Chromium이 존재하는 환경에서는 해당 경로를 재현할 수 없음")
    proc = _run_module("harness.recall", "--pages", "10", "--top-n", "20")
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip())
    assert payload["passed"] is False
    assert "error" in payload


def test_egress_measures_zero_leaks_now_that_security_exists():
    """WS-4 구현 이후에는 실제 측정이 이뤄져야 한다.

    WS-6 단계에서는 security 모듈이 없어 exit 2(측정 불가)였고,
    WS-4 완료로 exit 0(유출 0건)으로 전환되는 것이 정상이다.
    """
    proc = _run_module("harness.egress_test")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["passed"] is True
    assert payload["value"] == 0.0
    assert "error" not in payload
