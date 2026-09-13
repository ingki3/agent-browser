"""Gate 4 하네스 `harness.tier2_som` 자체 검증 (Stage 4 Task 8).

하네스 함수를 인프로세스로 구동한다(`--vlm mock`, Chromium 필요). 핵심은
AGENTS.md §5 규칙 5 사보타주 — 하네스가 실제로 결함을 잡아내는지:

(a) 에스컬레이션 임계를 1회로 낮추고 일반 페이지에서 Tier-1이 한 번
    실패하게 만들면 발동률이 0.10을 넘겨 FAIL해야 한다.
(b) 그라운더가 **틀린** 태그/좌표를 고르면 자기보고(`result.success`)는
    성공이라도 독립 검증(`body[data-result]`)이 실패해 FAIL해야 한다.
"""

from __future__ import annotations

import pytest

from agent import loop as loop_mod
from harness import tier2_som


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium 바이너리 없음")


def _run(**kwargs):
    kwargs.setdefault("runs", 10)
    kwargs.setdefault("vlm", "mock")
    return tier2_som.run_harness(**kwargs)


def test_task_mix_is_fixed_90_10():
    mix = tier2_som.build_task_mix(50)
    assert len(mix) == 50
    tier2 = [t for t in mix if t.tier2]
    assert len(tier2) == 5
    assert {t.site_id for t in tier2} == set(tier2_som.TIER2_SITES)
    ordinary_ids = {site_id for site_id, _ in tier2_som.ORDINARY_SITES}
    assert all(t.site_id in ordinary_ids for t in mix if not t.tier2)


def test_live_mode_without_api_key_is_execution_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(tier2_som, "_live_config", lambda: None)
    result = _run(vlm="live")
    assert result.error and "OPENROUTER_API_KEY" in result.error


def test_mock_run_passes_and_reports_coverage():
    result = _run()
    payload = result.to_dict()
    assert payload["passed"] is True, payload
    assert payload["trigger_rate"] == 0.1
    assert payload["value"] == 0.1
    assert payload["threshold"] == 0.10
    assert payload["samples"] == 10
    assert payload["covered_triggered"] >= 1
    assert payload["covered_untriggered"] >= 1
    assert payload["tier2_runs"] == 1
    assert payload["tier2_verified"] == 1
    assert payload["tier2_success_rate"] == 1.0
    # 유형 D 신호 — p95가 정확히 0이면 지연을 측정하지 않은 것이다.
    assert 0 < payload["p95_latency_ms"] <= payload["latency_threshold_ms"]
    assert payload["latency_threshold_ms"] == 3500
    assert payload["reason"] is None


def test_sabotage_a_lower_trigger_threshold_fails(monkeypatch):
    """임계 1회 + 일반 페이지 1회 실패 -> 모든 런이 발동 -> FAIL."""
    monkeypatch.setattr(loop_mod, "TIER2_TRIGGER_FAILURES", 1)
    result = _run(ordinary_bogus_clicks=1)
    payload = result.to_dict()
    print("SABOTAGE_A", result.to_json())
    assert payload["trigger_rate"] > 0.10
    assert payload["passed"] is False
    assert payload["covered_triggered"] == 10
    # 미발동 런이 0이므로 커버리지 미달 — CLI에서는 exit 2로 무효화된다.
    assert payload["covered_untriggered"] == 0 and result.error


def test_sabotage_b_wrong_grounder_fails():
    """오답 그라운더 -> 자기보고는 성공이지만 독립 검증 실패 -> FAIL."""
    result = _run(grounder_factory=tier2_som.wrong_grounder)
    payload = result.to_dict()
    print("SABOTAGE_B", result.to_json())
    assert payload["tier2_runs"] == 1
    assert payload["tier2_verified"] == 0
    assert payload["passed"] is False
    assert "tier2_verified" in payload["reason"]
    # 발동률·지연 자체는 여전히 통과 — 독립 검증만이 이 결함을 잡는다.
    assert payload["trigger_rate"] == 0.1
