"""WS-36: `harness.ipi_test` 가 제품 경로(MCP 결과의 data.injection_suspected)로 재는지.

* 표본 구조: 패턴마다 1:1 고유 표본(그 패턴 하나만 걸림), 정상 표본은 어느 패턴에도 안 걸림.
* 판정 로직(evaluate): 고유 표본이 신호를 못 받으면 커버리지 미달, 정상 표본 신호 = 오탐,
  여러 줄 표본은 관찰 경로를 요구하지 않음.
* 실제 서버: 연결 코드(attach_injection_signal)를 무력화하면 하네스가 미탐으로 떨어진다.
"""

from __future__ import annotations

import pytest

from harness import ipi_test
from security import detect_injection
from security.prompt_isolation import _INJECTION_PATTERNS

from test_harness import requires_chromium  # noqa: E402,F401 - Chromium 유무 판정 재사용


def test_unique_cases_cover_every_pattern_one_to_one():
    names = [name for _, name in _INJECTION_PATTERNS]
    assert sorted(p for _, p in ipi_test.UNIQUE_ATTACK_CASES) == sorted(names)
    for text, pattern in ipi_test.UNIQUE_ATTACK_CASES:
        assert detect_injection(text).patterns == (pattern,), (pattern, detect_injection(text).patterns)


def test_existing_samples_are_kept():
    """WS-36 은 기존 표본을 줄이지 않는다(공격 14 · 정상 8)."""
    assert len(ipi_test.ATTACK_CASES) == 14
    assert len(ipi_test.BENIGN_CASES) == 8


def test_benign_cases_are_not_flagged_by_detector():
    for text, kind in ipi_test.BENIGN_CASES + ipi_test.EXTRA_BENIGN_CASES:
        assert not detect_injection(text).is_attack, kind


def _sig(*patterns):
    return {"patterns": list(patterns), "where": ["items.text"], "hint": "h"}


def _rows(signal_for):
    rows = []
    for s in ipi_test.build_samples():
        obs, ext = signal_for(s)
        rows.append({"sample": s, "observe": obs, "extract": ext, "extract_ok": True})
    return {"rows": rows, "mock_rows": [{"site": "s01", "observe": None, "extract": None,
                                         "read_ok": True}]}


def _perfect(s):
    if not s.attack:
        return None, None
    pats = [s.unique_pattern] if s.unique_pattern else ["x"]
    sig = _sig(*pats)
    return (sig if s.single_line else None), sig


def test_evaluate_perfect():
    m = ipi_test.evaluate(_rows(_perfect))
    assert m["block_rate"] == 1.0 and m["fpr"] == 0.0
    assert m["patterns_covered"] == m["patterns_required"] == len(_INJECTION_PATTERNS)
    assert m["patterns_missing"] == []
    assert m["benign_samples"] == len(ipi_test.BENIGN_CASES) + len(ipi_test.EXTRA_BENIGN_CASES) + 1


def test_evaluate_no_signal_is_zero():
    m = ipi_test.evaluate(_rows(lambda s: (None, None)))
    assert m["block_rate"] == 0.0
    assert len(m["patterns_missing"]) == len(_INJECTION_PATTERNS)


@pytest.mark.parametrize("pattern", [name for _, name in _INJECTION_PATTERNS])
def test_evaluate_one_pattern_gone_is_coverage_failure(pattern):
    """패턴 하나가 빠지면 탐지율은 0.97 로 임계값을 넘지만 커버리지(patterns_missing)로 잡힌다."""
    def signal_for(s):
        if s.unique_pattern == pattern:
            return None, None
        return _perfect(s)

    m = ipi_test.evaluate(_rows(signal_for))
    assert m["patterns_missing"] == [pattern]
    assert m["block_rate"] >= 0.9


def test_evaluate_extra_pattern_on_unique_sample_is_mismatch():
    def signal_for(s):
        obs, ext = _perfect(s)
        if s.unique_pattern == "safety_disable":
            ext = _sig("safety_disable", "constraint_release_claim")
        return obs, ext

    m = ipi_test.evaluate(_rows(signal_for))
    assert m["patterns_missing"] == ["safety_disable"]
    assert m["unique_mismatch"]


def test_evaluate_observe_required_only_for_single_line():
    def signal_for(s):
        _, ext = _perfect(s)
        return None, ext  # 관찰 경로 신호 없음

    m = ipi_test.evaluate(_rows(signal_for))
    multi = [s for s in ipi_test.build_samples() if s.attack and not s.single_line]
    assert multi, "여러 줄 표본이 있어야 이 분기를 잰다"
    assert m["block_rate"] == round(len(multi) / m["attack_samples"], 4)


def test_evaluate_benign_signal_is_false_positive():
    def signal_for(s):
        if not s.attack and s.kind == "무시 버튼":
            return _sig("prior_instruction_override"), None
        return _perfect(s)

    m = ipi_test.evaluate(_rows(signal_for))
    assert len(m["false_positives"]) == 1 and m["fpr"] > 0


def test_unread_payload_is_reported():
    data = _rows(_perfect)
    data["rows"][0]["extract_ok"] = False
    data["mock_rows"][0]["read_ok"] = False
    m = ipi_test.evaluate(data)
    assert m["payload_unread"] == [data["rows"][0]["sample"].sample_id, "mock:s01"]


def _subset():
    s = ipi_test.build_samples()
    by = {x.sample_id: x for x in s}
    return [by["a0"], by["u5"], by["b0"]]  # 한 줄 공격, 여러 줄 고유, 정상


@requires_chromium
async def test_product_path_measures_mcp_signal():
    measured = await ipi_test.measure_product_path(_subset(), include_mock_sites=False)
    rows = {r["sample"].sample_id: r for r in measured["rows"]}
    assert all(r["extract_ok"] for r in rows.values())
    assert rows["a0"]["observe"] and rows["a0"]["extract"]
    assert rows["u5"]["extract"]["patterns"] == ["separator_forgery"]
    assert rows["b0"]["observe"] is None and rows["b0"]["extract"] is None


@requires_chromium
async def test_product_path_fails_without_wiring(monkeypatch):
    """사보타주: 서버의 신호 부착을 무력화하면 하네스가 미탐으로 떨어진다(탐지기는 그대로)."""
    import security.injection_signal as sig_mod

    monkeypatch.setattr(sig_mod, "attach_injection_signal", lambda result: False)
    measured = await ipi_test.measure_product_path(_subset(), include_mock_sites=False)
    rows = {r["sample"].sample_id: r for r in measured["rows"]}
    assert rows["a0"]["observe"] is None and rows["a0"]["extract"] is None
    assert detect_injection(rows["a0"]["sample"].text).is_attack, "탐지기 단독은 여전히 잡는다"
